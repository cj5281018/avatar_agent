/**
 * HR personal pages, ticket handover and the full cross-system flow.
 *
 * Real browser only: tickets are exchanged by a POST form, the supervisor enters
 * through the actual Avatar account menu, approval happens on the HR page, and
 * the original card/trace are re-read afterwards. Requests only assert data; the
 * flows below use page clicks.
 */
const { chromium } = require('playwright-core')
const assert = require('node:assert/strict')
const crypto = require('node:crypto')
const { execFileSync } = require('node:child_process')

const base = process.env.BASE || 'http://localhost:8301'
const hrBase = process.env.HR_BASE || 'http://localhost:8892'
const modelId = process.env.MODEL_ID

async function login(page, email, password) {
    await page.goto(base + '/login')
    await page.locator('[name=email]').fill(email)
    await page.locator('[name=password]').fill(password)
    await page.locator('button[type=submit]').click()
    await page.waitForURL((url) => url.pathname !== '/login')
}

async function bind(page) {
    await page.getByTestId('sidebar-user-menu-trigger').click()
    await page.getByTestId('menu-employee-service').click()
    await page.getByRole('button', { name: '绑定或续期' }).click()
    await page.getByText(/已绑定，有效至/).waitFor({ timeout: 15000 })
}

async function ticketAction(page, target) {
    const response = await page.request.post(base + '/api/hr-entry', {
        data: { target },
        headers: { origin: base },
    })
    assert(response.ok(), 'ticket: ' + response.status())
    const body = await response.json()
    assert(!('url' in body), 'ticket must not be handed over in a URL')
    return body
}

function expireTicket(ticket) {
    const hash = crypto.createHash('sha256').update(ticket).digest('hex')
    execFileSync('docker', [
        'exec', 'avatar2-source-gateway-1', 'python', '-c',
        `import sqlite3; db=sqlite3.connect('/data/avatar.sqlite3'); db.execute("UPDATE web_tickets SET expires_at=0 WHERE ticket_hash=?", ("${hash}",)); db.commit()`,
    ])
}

;(async () => {
    const browser = await chromium.launch({ channel: 'msedge', headless: true })
    const evidence = {}
    try {
        const bobContext = await browser.newContext()
        const bob = await bobContext.newPage()
        await login(bob, process.env.BOB_EMAIL, process.env.BOB_PASSWORD)
        await bind(bob)

        // ---- one real message: regulation + draft ----
        const created = await bob.request.post(base + '/api/chat', { data: { modelId } })
        const { chatId } = await created.json()
        const day = process.env.LEAVE_DAY || '2027-05-12'
        await bob.request.post(`${base}/api/chat/${chatId}/messages`, {
            data: { content: `帮我查一下年休假规定，然后再请 ${day} 一天年假，事由家庭安排，假别年假。` },
        })
        const stream = await bob.request.get(`${base}/api/chat/${chatId}/stream`, {
            headers: { accept: 'text/event-stream' },
            timeout: 180000,
        })
        await stream.text()
        const runsBefore = (await (await bob.request.get(`${base}/api/chat/${chatId}/runs`)).json()).runs
        const before = await (await bob.request.get(`${base}/api/chat/${chatId}/runs/${runsBefore[0].runId}`)).json()
        const retrieval = before.events.find(
            (event) => event.type === 'tool.completed' && event.payload.tool === 'search',
        )
        assert(retrieval, 'same turn should retrieve regulations')
        await bob.goto(`${base}/chat/${chatId}`)
        const card = bob.getByRole('region', { name: '请假确认卡' }).last()
        await card.waitFor({ timeout: 30000 })
        assert((await bob.getByTestId('generic-action-card').count()) > 0, 'generic card not rendered')
        const pendingBefore = execFileSync('docker', [
            'exec', 'avatar2-source-hr-1', 'python', '-c',
            `import sqlite3,glob; db=sqlite3.connect(glob.glob('/var/lib/hr/*.sqlite3')[0]); print(db.execute("select count(*) from leave_requests where requester_handle='bob' and start_date=?", ("${day}",)).fetchone()[0])`,
        ], { encoding: 'utf8' }).trim()
        assert.equal(pendingBefore, '0', 'no formal request before confirmation')

        // Concurrency: double confirm must not create a second request.
        await card.getByRole('button', { name: '确认提交' }).dblclick({ timeout: 30000 })
        const line = card.getByText(/HR 单号 leave_/)
        await line.waitFor({ timeout: 40000 })
        const leaveId = (await line.innerText()).match(/leave_[A-Za-z0-9_-]+/)[0]
        evidence.chain = {
            chatId,
            runId: runsBefore[0].runId,
            operationId: before.events.find((event) => event.payload?.operationId)?.payload.operationId,
            leaveId,
            retrievalHits: retrieval.payload.hits,
        }
        const countRows = execFileSync('docker', [
            'exec', 'avatar2-source-hr-1', 'python', '-c',
            `import sqlite3,glob; db=sqlite3.connect(glob.glob('/var/lib/hr/*.sqlite3')[0]); print(db.execute("select count(*) from leave_requests where requester_handle='bob' and start_date=?", ("${day}",)).fetchone()[0])`,
        ], { encoding: 'utf8' }).trim()
        assert.equal(countRows, '1', 'double confirm must yield exactly one request')
        const sealBefore = execFileSync('docker', [
            'exec', 'avatar2-source-postgres-1', 'sh', '-c',
            `psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select head_digest from agent_trace_runs where run_id='${runsBefore[0].runId}'"`,
        ], { encoding: 'utf8' }).trim()

        // ---- ticket POST exchange ----
        const sample = await ticketAction(bob, 'me')
        const fresh = await browser.newContext()
        const first = await fresh.request.post(sample.action, { form: { ticket: sample.ticket }, maxRedirects: 0 })
        assert.equal(first.status(), 303)
        const reuse = await fresh.request.post(sample.action, { form: { ticket: sample.ticket }, maxRedirects: 0 })
        assert.equal(reuse.status(), 410)
        await fresh.close()

        const concurrent = await ticketAction(bob, 'me')
        const a = await browser.newContext()
        const b = await browser.newContext()
        const responses = await Promise.all([
            a.request.post(concurrent.action, { form: { ticket: concurrent.ticket }, maxRedirects: 0 }),
            b.request.post(concurrent.action, { form: { ticket: concurrent.ticket }, maxRedirects: 0 }),
        ])
        assert.deepEqual(responses.map((r) => r.status()).sort(), [303, 410])
        await a.close()
        await b.close()

        const expired = await ticketAction(bob, 'me')
        expireTicket(expired.ticket)
        const expiredContext = await browser.newContext()
        const expiredResponse = await expiredContext.request.post(expired.action, {
            form: { ticket: expired.ticket },
            maxRedirects: 0,
        })
        assert.equal(expiredResponse.status(), 410)
        await expiredContext.close()
        console.log('PASS ticket: POST only, single use, concurrent once, expiry rejected')

        // ---- Bob opens HR from the card button; URL has no ticket ----
        const [bobHr] = await Promise.all([
            bobContext.waitForEvent('page'),
            card.getByRole('button', { name: '在 HR 中查看' }).click(),
        ])
        await bobHr.waitForLoadState('domcontentloaded')
        assert(!bobHr.url().includes('ticket='), 'ticket leaked into the URL: ' + bobHr.url())
        assert(bobHr.url().includes('/web/me'), 'should land on /web/me: ' + bobHr.url())
        await bobHr.getByText('我的申请').first().waitFor({ timeout: 20000 })
        assert((await bobHr.locator('main').innerText()).includes(leaveId))
        const logs = execFileSync('docker', ['logs', '--tail', '400', 'avatar2-source-gateway-1'], { encoding: 'utf8' })
            + execFileSync('docker', ['logs', '--tail', '400', 'avatar2-source-hr-1'], { encoding: 'utf8' })
        assert(!logs.includes(sample.ticket), 'ticket appears in service logs')
        console.log('PASS Bob entered HR without a password; ticket absent from URL and logs')

        // ---- Supervisor through the actual Avatar account menu ----
        const adminContext = await browser.newContext()
        const admin = await adminContext.newPage()
        await login(admin, process.env.ADMIN_EMAIL, process.env.ADMIN_PASSWORD)
        await bind(admin)
        await admin.getByTestId('sidebar-user-menu-trigger').click()
        const [adminHr] = await Promise.all([
            adminContext.waitForEvent('page'),
            admin.getByTestId('menu-hr-approvals').click(),
        ])
        await adminHr.getByText('我的待审批').first().waitFor({ timeout: 20000 })
        assert(!adminHr.url().includes('ticket='))
        assert((await adminHr.locator('main').innerText()).includes(leaveId))
        await adminHr.locator('article').filter({ hasText: leaveId })
            .getByRole('button', { name: '批准' })
            .click({ timeout: 20000 })
        await adminHr.waitForURL('**/web/approvals', { timeout: 20000 })
        const status = execFileSync('docker', [
            'exec', 'avatar2-source-hr-1', 'python', '-c',
            `import sqlite3,glob; db=sqlite3.connect(glob.glob('/var/lib/hr/*.sqlite3')[0]); print(db.execute("select status from leave_requests where id=?", ("${leaveId}",)).fetchone()[0])`,
        ], { encoding: 'utf8' }).trim()
        assert.equal(status, 'approved')
        console.log('PASS supervisor clicked the Avatar menu entry and approved on the HR page')

        // ---- unassigned supervisor refused ----
        const aliceContext = await browser.newContext()
        const alice = await aliceContext.newPage()
        await login(alice, process.env.ALICE_EMAIL, process.env.ALICE_PASSWORD)
        await bind(alice)
        const aliceHr = await aliceContext.newPage()
        const aliceTicket = await ticketAction(alice, 'approvals')
        await aliceHr.request.post(aliceTicket.action, {
            form: { ticket: aliceTicket.ticket },
            maxRedirects: 0,
        })
        await aliceHr.goto(hrBase + '/web/approvals')
        assert(!(await aliceHr.locator('main').innerText()).includes(leaveId), 'unassigned supervisor sees the request')
        await aliceContext.close()

        // ---- Bob re-reads card and trace business link; seal unchanged ----
        await bob.reload({ waitUntil: 'domcontentloaded' })
        const reopened = bob.getByRole('region', { name: '请假确认卡' }).last()
        await reopened.getByRole('button', { name: '回查同一申请' }).click()
        await reopened.getByText(new RegExp(`${leaveId}.*approved`)).waitFor({ timeout: 20000 })
        const business = await (await bob.request.get(
            `${base}/api/chat/${chatId}/runs/${evidence.chain.runId}/business`,
        )).json()
        assert(business.drafts.some((draft) => draft.leaveId === leaveId && draft.hrStatus === 'approved'))
        const sealAfter = execFileSync('docker', [
            'exec', 'avatar2-source-postgres-1', 'sh', '-c',
            `psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select head_digest from agent_trace_runs where run_id='${evidence.chain.runId}'"`,
        ], { encoding: 'utf8' }).trim()
        assert.equal(sealAfter, sealBefore, 'seal changed after approval')
        console.log('PASS card + trace business link approved; seal unchanged')

        // ---- generic card for another department ----
        await bob.goto(base + '/action-card-demo')
        const demo = bob.getByTestId('generic-action-card')
        await demo.waitFor({ timeout: 10000 })
        assert((await demo.innerText()).includes('testkit'))
        await demo.getByRole('button', { name: '查看结果' }).click()
        await demo.getByText(/已渲染同一组件/).waitFor({ timeout: 5000 })
        console.log('PASS generic card renders a testkit descriptor without HR branches')

        // ---- rebinding invalidates the old session ----
        await bob.goto(base + '/employee-binding')
        await bob.getByRole('button', { name: '解除绑定' }).click()
        await bob.getByText('已解除绑定', { exact: true }).waitFor({ timeout: 15000 })
        await bobHr.reload({ waitUntil: 'domcontentloaded' })
        await bobHr.locator('body').getByText(/重新进入|绑定不可用|无法访问/).first().waitFor({ timeout: 20000 })
        console.log('PASS unbinding invalidated the open HR session')
        evidence.unbindBlocked = true

        console.log('EVIDENCE', JSON.stringify(evidence))
    } finally {
        await browser.close()
    }
})().catch((error) => {
    console.error('FAIL', error.message)
    process.exitCode = 1
})
