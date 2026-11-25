/**
 * Independent HR pages + ticket SSO.
 *
 * Real browser: Bob and the assigned supervisor open their own HR pages without
 * a second password; Bob's leave is visible only to Bob and his supervisor;
 * an unassigned supervisor cannot approve; ticket reuse, concurrency and expiry
 * are rejected; unbinding blocks an already-open page.
 */
const { chromium } = require('playwright-core')
const assert = require('node:assert/strict')
const crypto = require('node:crypto')
const { execFileSync } = require('node:child_process')

const base = process.env.BASE || 'http://localhost:8301'
const modelId = process.env.MODEL_ID
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

async function login(page, email, password) {
    await page.goto(base + '/login')
    await page.locator('[name=email]').fill(email)
    await page.locator('[name=password]').fill(password)
    await page.locator('button[type=submit]').click()
    await page.waitForURL((url) => url.pathname !== '/login')
}

async function bind(page) {
    await page.goto(base + '/employee-binding')
    await page.getByRole('button', { name: '绑定或续期' }).click()
    await page.getByText(/已绑定，有效至/).waitFor({ timeout: 15000 })
}

async function ticket(page, target) {
    const response = await page.request.post(base + '/api/hr-entry', {
        data: { target },
        headers: { origin: base },
    })
    assert(response.ok(), 'ticket: ' + response.status())
    return (await response.json()).url
}

function expireTicket(url) {
    const ticket = new URL(url).searchParams.get('ticket')
    const hash = crypto.createHash('sha256').update(ticket).digest('hex')
    execFileSync('docker', [
        'exec', 'avatar2-source-gateway-1', 'python', '-c',
        `import sqlite3,os; db=sqlite3.connect('/data/avatar.sqlite3'); db.execute("UPDATE web_tickets SET expires_at=0 WHERE ticket_hash=?", ("${hash}",)); db.commit()`,
    ])
}

;(async () => {
    const browser = await chromium.launch({ channel: 'msedge', headless: true })
    const evidence = {}
    try {
        // ---- Bob drafts and confirms a real leave ----
        const bobContext = await browser.newContext()
        const bob = await bobContext.newPage()
        await login(bob, process.env.BOB_EMAIL, process.env.BOB_PASSWORD)
        await bind(bob)

        const created = await bob.request.post(base + '/api/chat', { data: { modelId } })
        const { chatId } = await created.json()
        const day = process.env.LEAVE_DAY || '2027-05-06'
        await bob.request.post(`${base}/api/chat/${chatId}/messages`, {
            data: { content: `请调用 hr_preview_leave 工具，为我起草 ${day} 一天年假，事由为家庭安排，leaveType 使用 annual。` },
        })
        const stream = await bob.request.get(`${base}/api/chat/${chatId}/stream`, {
            headers: { accept: 'text/event-stream' },
            timeout: 150000,
        })
        await stream.text()
        const runsBefore = (await (await bob.request.get(`${base}/api/chat/${chatId}/runs`)).json()).runs
        const beforeRun = await (await bob.request.get(
            `${base}/api/chat/${chatId}/runs/${runsBefore[0].runId}`,
        )).json()
        void beforeRun
        const sealBefore = execFileSync('docker', [
            'exec', 'avatar2-source-postgres-1', 'sh', '-c',
            `psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select head_digest from agent_trace_runs where run_id='${runsBefore[0].runId}'"`,
        ], { encoding: 'utf8' }).trim()
        const pendingBefore = execFileSync('docker', [
            'exec', 'avatar2-source-hr-1', 'python', '-c',
            `import sqlite3,glob; db=sqlite3.connect(glob.glob('/var/lib/hr/*.sqlite3')[0]); print(db.execute("select count(*) from leave_requests where requester_handle='bob' and start_date='${day}'").fetchone()[0])`,
        ], { encoding: 'utf8' }).trim()
        assert.equal(pendingBefore, '0', 'no formal request should exist before confirmation')
        await bob.goto(`${base}/chat/${chatId}`)
        const card = bob.getByRole('region', { name: '请假确认卡' }).last()
        await card.getByRole('button', { name: '确认提交' }).click({ timeout: 30000 })
        const line = card.getByText(/HR 单号 leave_/)
        await line.waitFor({ timeout: 30000 })
        const leaveId = (await line.innerText()).match(/leave_[A-Za-z0-9_-]+/)[0]

        const runs = (await (await bob.request.get(`${base}/api/chat/${chatId}/runs`)).json()).runs
        const detail = await (await bob.request.get(`${base}/api/chat/${chatId}/runs/${runs[0].runId}`)).json()
        const draft = detail.events.find((event) => event.payload?.operationId)
        evidence.chain = {
            chatId,
            runId: runs[0].runId,
            spanId: draft?.spanId,
            operationId: draft?.payload.operationId,
            leaveId,
        }

        // ---- Single-use ticket ----
        const firstUrl = await ticket(bob, 'me')
        const fresh = await browser.newContext()
        const redeemed = await fresh.request.get(firstUrl, { maxRedirects: 0 })
        assert.equal(redeemed.status(), 303, 'first redemption should redirect')
        const reuse = await fresh.request.get(firstUrl, { maxRedirects: 0 })
        assert.equal(reuse.status(), 410, 'reused ticket must be rejected')
        await fresh.close()
        console.log('PASS ticket single use: 303 then 410')

        // ---- Concurrent redemption: exactly one wins ----
        const concurrentUrl = await ticket(bob, 'me')
        const a = await browser.newContext()
        const b = await browser.newContext()
        const results = await Promise.all([
            a.request.get(concurrentUrl, { maxRedirects: 0 }),
            b.request.get(concurrentUrl, { maxRedirects: 0 }),
        ])
        const statuses = results.map((response) => response.status()).sort()
        assert.deepEqual(statuses, [303, 410], 'concurrent redemption: ' + statuses.join(','))
        await a.close()
        await b.close()
        console.log('PASS concurrent redemption: exactly one succeeded')

        // ---- Expired ticket ----
        const expiredUrl = await ticket(bob, 'me')
        expireTicket(expiredUrl)
        const expiredContext = await browser.newContext()
        const expiredResponse = await expiredContext.request.get(expiredUrl, { maxRedirects: 0 })
        assert.equal(expiredResponse.status(), 410, 'expired ticket must be rejected')
        await expiredContext.close()
        console.log('PASS expired ticket: 410')

        // ---- Bob opens his own HR page without a password ----
        const bobHrContext = await browser.newContext()
        const bobHr = await bobHrContext.newPage()
        await bobHr.goto(await ticket(bob, 'me'), { waitUntil: 'domcontentloaded' })
        await bobHr.getByText('我的申请').first().waitFor({ timeout: 20000 })
        const bobText = await bobHr.locator('main').innerText()
        assert(bobText.includes(leaveId), 'Bob should see his own request')
        assert((await bobHr.locator('input[type=password]').count()) === 0, 'no password prompt')
        evidence.bobPage = leaveId
        console.log('PASS Bob opened HR without a second password')

        // ---- Other employee cannot see Bob's request ----
        const aliceContext = await browser.newContext()
        const alice = await aliceContext.newPage()
        await login(alice, process.env.ALICE_EMAIL, process.env.ALICE_PASSWORD)
        await bind(alice)
        const aliceHrContext = await browser.newContext()
        const aliceHr = await aliceHrContext.newPage()
        await aliceHr.goto(await ticket(alice, 'me'), { waitUntil: 'domcontentloaded' })
        await aliceHr.getByText('我的申请').first().waitFor({ timeout: 20000 })
        const aliceText = await aliceHr.locator('main').innerText()
        assert(!aliceText.includes(leaveId), 'Alice must not see Bob request')
        const aliceCsrf = await aliceHr.locator('meta[name=hr-csrf]').getAttribute('content')
        const denied = await aliceHr.request.post(
            `http://localhost:8892/web/approvals/${leaveId}/approve`,
            { form: { csrf: aliceCsrf ?? '' }, maxRedirects: 0 },
        )
        assert.equal(denied.status(), 200, 'form-post denial is rendered as a page')
        const deniedText = await denied.text()
        assert(deniedText.includes('没有分配给你') || deniedText.includes('已处理'), 'unassigned approval must be refused')
        evidence.unassigned = 'refused'
        console.log('PASS Alice cannot read or approve Bob request')

        // ---- Assigned supervisor approves through the HR page ----
        const adminContext = await browser.newContext()
        const admin = await adminContext.newPage()
        await login(admin, process.env.ADMIN_EMAIL, process.env.ADMIN_PASSWORD)
        await bind(admin)
        const adminHrContext = await browser.newContext()
        const adminHr = await adminHrContext.newPage()
        await adminHr.goto(await ticket(admin, 'approvals'), { waitUntil: 'domcontentloaded' })
        await adminHr.getByText('我的待审批').first().waitFor({ timeout: 20000 })
        const adminText = await adminHr.locator('main').innerText()
        assert(adminText.includes(leaveId), 'assigned supervisor should see the request')
        const adminCard = adminHr.locator('article').filter({ hasText: leaveId })
        await adminCard.getByRole('button', { name: '批准' }).click({ timeout: 20000 })
        await adminHr.waitForURL('**/web/approvals', { timeout: 20000 })
        await sleep(500)
        const status = execFileSync('docker', [
            'exec', 'avatar2-source-hr-1', 'python', '-c',
            `import sqlite3,glob; db=sqlite3.connect(glob.glob('/var/lib/hr/*.sqlite3')[0]); print(db.execute("select status from leave_requests where id=?", ("${leaveId}",)).fetchone()[0])`,
        ], { encoding: 'utf8' }).trim()
        assert.equal(status, 'approved', 'HR record should be approved, saw ' + status)
        evidence.approved = status
        console.log('PASS assigned supervisor approved via HR page:', status)

        // Bob's original card and the trace business area see the same record.
        await bob.goto(`${base}/chat/${chatId}`)
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
        const afterDetail = await (await bob.request.get(
            `${base}/api/chat/${chatId}/runs/${evidence.chain.runId}`,
        )).json()
        assert.equal(afterDetail.verification.status, 'ok', 'trace should still verify')
        console.log('PASS original card + trace business link approved; seal unchanged')

        // ---- Unbind blocks the already-open page ----
        await bob.goto(base + '/employee-binding')
        await bob.getByRole('button', { name: '解除绑定' }).click()
        await bob.getByText('已解除绑定', { exact: true }).waitFor({ timeout: 15000 })
        await bobHr.reload({ waitUntil: 'domcontentloaded' })
        await bobHr.getByText(/绑定不可用|请从工作台|无法访问/).first().waitFor({ timeout: 20000 })
        console.log('PASS unbinding blocked the already-open HR page')
        evidence.unbindBlocked = true

        console.log('EVIDENCE', JSON.stringify(evidence))
    } finally {
        await browser.close()
    }
})().catch((error) => {
    console.error('FAIL', error.message)
    process.exitCode = 1
})
