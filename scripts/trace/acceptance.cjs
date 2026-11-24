/**
 * Trace acceptance - real Web flows.
 *
 * Covers: plain answer, regulation retrieval, two questions in one chat,
 * draft -> employee confirmation -> manager approval -> business link,
 * tool failure, and cross-user access control.
 *
 * Requires playwright-core (NODE_PATH) and these env vars:
 *   BASE, MODEL_ID, BOB_EMAIL/PASSWORD, ALICE_EMAIL/PASSWORD, ADMIN_EMAIL/PASSWORD
 */
const { chromium } = require('playwright-core')
const assert = require('node:assert/strict')
const { execFileSync } = require('node:child_process')

const base = process.env.BASE || 'http://localhost:8301'
const modelId = process.env.MODEL_ID

const psql = (sql) =>
    execFileSync(
        'docker',
        [
            'exec',
            '-i',
            'avatar2-source-postgres-1',
            'sh',
            '-c',
            'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At',
        ],
        { input: sql, encoding: 'utf8' },
    ).trim()

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

async function newChat(page) {
    const created = await page.request.post(base + '/api/chat', { data: { modelId } })
    assert(created.ok(), 'chat create failed ' + created.status())
    return (await created.json()).chatId
}

async function turn(page, chatId, content) {
    const posted = await page.request.post(`${base}/api/chat/${chatId}/messages`, { data: { content } })
    assert(posted.ok(), 'message failed ' + posted.status())
    const stream = await page.request.get(`${base}/api/chat/${chatId}/stream`, {
        headers: { accept: 'text/event-stream' },
        timeout: 150000,
    })
    const body = await stream.text()
    assert(!body.includes('event: stream_error'), 'stream error: ' + body.slice(-600))
    return body
}

async function runs(page, chatId) {
    const response = await page.request.get(`${base}/api/chat/${chatId}/runs`)
    assert(response.ok(), 'runs failed ' + response.status())
    return (await response.json()).runs
}

async function detail(page, chatId, runId, suffix = '') {
    const response = await page.request.get(`${base}/api/chat/${chatId}/runs/${runId}${suffix}`)
    return { status: response.status(), body: response.ok() ? await response.json() : null }
}

function payloadOf(body, type, predicate = () => true) {
    return body.events.find((event) => event.type === type && predicate(event.payload))
}

;(async () => {
    const browser = await chromium.launch({ channel: 'msedge', headless: true })
    const evidence = {}
    try {
        // ---------- 1 & 2 & 3: one chat, two questions ----------
        const bobContext = await browser.newContext()
        const bob = await bobContext.newPage()
        await login(bob, process.env.BOB_EMAIL, process.env.BOB_PASSWORD)
        await bind(bob)

        const chat = await newChat(bob)
        await turn(bob, chat, '请用一句话介绍你自己。')
        await turn(bob, chat, '请实际搜索工作台里的中国带薪年休假法规，并给出命中的文件和来源。')

        const chatRuns = await runs(bob, chat)
        assert.equal(chatRuns.length, 2, 'expected two runs in one chat')
        const [second, first] = chatRuns
        assert.match(first.messageText ?? '', /介绍你自己/, 'first run question')
        assert.match(second.messageText ?? '', /带薪年休假法规/, 'second run question')
        assert.notEqual(first.runId, second.runId)

        const firstDetail = await detail(bob, chat, first.runId)
        assert.equal(firstDetail.status, 200)
        const plainTypes = firstDetail.body.events.map((event) => event.type)
        assert(plainTypes.includes('model.started'), 'plain model step')
        assert(!plainTypes.some((type) => type.startsWith('tool.')), 'plain has no tool steps')
        assert.equal(firstDetail.body.run.executionState, 'completed')
        assert.equal(firstDetail.body.run.captureStatus, 'ok')
        assert.equal(firstDetail.body.verification.status, 'ok')
        evidence.plain = first.runId

        const secondDetail = await detail(bob, chat, second.runId)
        const retrieval = payloadOf(secondDetail.body, 'tool.completed', (p) => p.tool === 'search')
        assert(retrieval, 'retrieval step missing')
        assert(Number(retrieval.payload.hits) > 0, 'retrieval hits')
        evidence.retrieval = { runId: second.runId, hits: retrieval.payload.hits }

        // Open from the chat entry: the first question must open the first run.
        await bob.goto(`${base}/chat/${chat}`)
        await bob.getByTestId('trace-entry').first().click()
        const runLine = bob.getByText(/runId [0-9a-f]{32}/)
        await runLine.waitFor({ timeout: 20000 })
        assert((await runLine.innerText()).includes(first.runId), 'chat entry mapped to wrong run')

        // Open from the trace centre: newest row is the second question.
        await bob.goto(base + '/traces')
        await bob.getByTestId('trace-center-list').locator('li').first().waitFor({ timeout: 20000 })
        const questions = await bob.getByTestId('trace-question').allInnerTexts()
        assert(questions[0].includes('带薪年休假法规'), 'trace centre newest row is the second question')
        assert(questions.some((text) => text.includes('介绍你自己')), 'trace centre lists both questions')
        await bob.getByTestId(`trace-open-${second.runId}`).click()
        await bob.getByText(/runId [0-9a-f]{32}/).waitFor({ timeout: 20000 })
        const listRun = await bob.getByText(/runId [0-9a-f]{32}/).innerText()
        assert(listRun.includes(second.runId), 'trace centre opened the wrong run')
        evidence.multiTurn = { chatId: chat, runs: chatRuns.map((run) => run.runId) }

        // ---------- 4: draft -> confirm -> approve -> business link ----------
        const draftChat = await newChat(bob)
        const day = process.env.LEAVE_DAY
        await turn(
            bob,
            draftChat,
            `请调用 hr_preview_leave 工具，为我起草 ${day} 一天年假，事由为家庭安排，leaveType 使用 annual。`,
        )
        const draftRuns = await runs(bob, draftChat)
        const draftRun = draftRuns[0]
        const draftDetail = await detail(bob, draftChat, draftRun.runId)
        const draftEvent = payloadOf(draftDetail.body, 'tool.completed', (p) => p.operationId)
        assert(draftEvent, 'draft operation id missing')
        const operationId = draftEvent.payload.operationId
        const sealBefore = psql(
            `SELECT COALESCE(head_digest,'') FROM agent_trace_runs WHERE run_id='${draftRun.runId}';`,
        )

        await bob.goto(`${base}/chat/${draftChat}`)
        const card = bob.getByRole('region', { name: '请假确认卡' }).last()
        await card.getByRole('button', { name: '确认提交' }).click({ timeout: 30000 })
        const numberLine = card.getByText(/HR 单号 leave_/)
        await numberLine.waitFor({ timeout: 30000 })
        const leaveId = (await numberLine.innerText()).match(/leave_[A-Za-z0-9_-]+/)[0]
        evidence.draft = { runId: draftRun.runId, operationId, leaveId }

        // Manager approves in the workbench.
        const adminContext = await browser.newContext()
        const admin = await adminContext.newPage()
        await login(admin, process.env.ADMIN_EMAIL, process.env.ADMIN_PASSWORD)
        await bind(admin)
        await admin.goto(base + '/hr-approvals')
        await admin.getByRole('button', { name: new RegExp(leaveId) }).click({ timeout: 30000 })
        const details = admin.getByRole('region', { name: '申请详情' })
        await details.getByRole('button', { name: '批准', exact: true }).click()
        await admin.getByText(`已批准 ${leaveId}`, { exact: true }).waitFor({ timeout: 20000 })

        // Employee: original card reflects the approval.
        await bob.bringToFront()
        await bob.goto(`${base}/chat/${draftChat}`)
        const reopened = bob.getByRole('region', { name: '请假确认卡' }).last()
        await reopened.getByRole('button', { name: '回查同一申请' }).click()
        await reopened.getByText(new RegExp(`${leaveId}.*approved`)).waitFor({ timeout: 20000 })

        // Trace business area: same leave id and latest status.
        await bob.goto(`${base}/chat/${draftChat}`)
        await bob.getByTestId('trace-entry').last().click()
        const business = bob.getByTestId('trace-business')
        await business.waitFor({ timeout: 20000 })
        // The association loads when the panel opens; wait for it, then use
        // the refresh control once it is enabled.
        await business.getByText(leaveId).waitFor({ timeout: 40000 })
        await business.getByRole('button', { name: '刷新办理状态' }).click({ timeout: 15000 })
        await business.getByText(leaveId).waitFor({ timeout: 20000 })
        const businessText = await business.innerText()
        assert(businessText.includes('approved'), 'business status not approved: ' + businessText)
        assert(businessText.includes(operationId), 'business operation id missing')

        const sealAfter = psql(
            `SELECT COALESCE(head_digest,'') FROM agent_trace_runs WHERE run_id='${draftRun.runId}';`,
        )
        assert.equal(sealAfter, sealBefore, 'sealed trace changed after approval')
        const afterApproval = await detail(bob, draftChat, draftRun.runId)
        assert.equal(afterApproval.body.verification.status, 'ok', 'trace no longer verifies')
        evidence.business = { leaveId, operationId, approved: true, sealUnchanged: true }

        // ---------- 5: tool failure ----------
        const failedChat = await newChat(bob)
        await turn(
            bob,
            failedChat,
            '请调用 hr_preview_leave 工具，参数 startDate=2026-12-16，endDate=2026-12-16，leaveType=not_a_leave_type，reason=test。只调用一次，不要修改参数。',
        )
        const failedRuns = await runs(bob, failedChat)
        assert.equal(failedRuns[0].toolError, true, 'list should flag a tool failure')
        const failedDetail = await detail(bob, failedChat, failedRuns[0].runId)
        const failedEvent = payloadOf(failedDetail.body, 'tool.failed')
        assert(failedEvent, 'tool failure step missing')
        assert(failedDetail.body.run.executionState === 'completed', 'turn itself completed')
        evidence.toolFailure = failedRuns[0].runId

        // ---------- 10: access control ----------
        const aliceContext = await browser.newContext()
        const alice = await aliceContext.newPage()
        await login(alice, process.env.ALICE_EMAIL, process.env.ALICE_PASSWORD)
        const deniedList = await alice.request.get(`${base}/api/chat/${chat}/runs`)
        const deniedDetail = await alice.request.get(
            `${base}/api/chat/${chat}/runs/${first.runId}?afterSeq=1`,
        )
        const deniedBusiness = await alice.request.get(
            `${base}/api/chat/${draftChat}/runs/${draftRun.runId}/business`,
        )
        assert.equal(deniedList.status(), 403, 'list read should be forbidden')
        assert.equal(deniedDetail.status(), 403, 'incremental read should be forbidden')
        assert.equal(deniedBusiness.status(), 403, 'business read should be forbidden')
        // A manager can approve but still cannot read the requester's chat.
        const adminTrace = await admin.request.get(`${base}/api/chat/${chat}/runs/${first.runId}`)
        assert.equal(adminTrace.status(), 403, 'manager should not read employee trace')
        evidence.access = { list: 403, detail: 403, business: 403, manager: 403 }

        console.log('PASS acceptance', JSON.stringify(evidence, null, 0))
    } finally {
        await browser.close()
    }
})().catch((error) => {
    console.error('FAIL', error.message)
    process.exitCode = 1
})
