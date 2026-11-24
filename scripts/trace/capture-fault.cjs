/**
 * Capture-fault acceptance (run with AVATAR2_TRACE_FAULT_EVENT set on the AI
 * service). Proves that a dropped event degrades the run, the business result
 * is unaffected, and the degradation survives a restart.
 *
 * Modes:
 *   node capture-fault.cjs turn   - perform a draft turn and submit it
 *   node capture-fault.cjs check  - re-read the stored run after a restart
 */
const { chromium } = require('playwright-core')
const assert = require('node:assert/strict')

const base = process.env.BASE || 'http://localhost:8301'
const modelId = process.env.MODEL_ID
const faultDay = process.env.FAULT_DAY || '2026-12-18'

async function login(page, email, password) {
    await page.goto(base + '/login')
    await page.locator('[name=email]').fill(email)
    await page.locator('[name=password]').fill(password)
    await page.locator('button[type=submit]').click()
    await page.waitForURL((url) => url.pathname !== '/login')
}

;(async () => {
    const mode = process.argv[2] ?? 'turn'
    const browser = await chromium.launch({ channel: 'msedge', headless: true })
    try {
        const page = await (await browser.newContext()).newPage()
        await login(page, process.env.BOB_EMAIL, process.env.BOB_PASSWORD)
        await page.goto(base + '/employee-binding')
        await page.getByRole('button', { name: '绑定或续期' }).click()
        await page.getByText(/已绑定，有效至/).waitFor({ timeout: 15000 })

        if (mode === 'check') {
            const runId = process.env.FAULT_RUN_ID
            assert(runId, 'FAULT_RUN_ID required')
            const chatId = process.env.FAULT_CHAT_ID
            assert(chatId, 'FAULT_CHAT_ID required')
            const response = await page.request.get(`${base}/api/chat/${chatId}/runs/${runId}`)
            assert(response.ok(), 'detail ' + response.status())
            const body = await response.json()
            assert.equal(body.run.captureStatus, 'degraded', 'capture degradation lost after restart')
            assert(body.run.droppedEvents >= 1, 'dropped count lost')
            console.log(
                'PASS capture degradation persisted:',
                body.run.captureStatus,
                'dropped=' + body.run.droppedEvents,
                'integrity=' + body.verification.status,
            )
            return
        }

        const created = await page.request.post(base + '/api/chat', { data: { modelId } })
        const { chatId } = await created.json()
        const posted = await page.request.post(`${base}/api/chat/${chatId}/messages`, {
            data: {
                content:
                    `请调用 hr_preview_leave 工具，为我起草 ${faultDay} 一天年假，事由为家庭安排，leaveType 使用 annual。`,
            },
        })
        assert(posted.ok(), 'message failed')
        const stream = await page.request.get(`${base}/api/chat/${chatId}/stream`, {
            headers: { accept: 'text/event-stream' },
            timeout: 150000,
        })
        const raw = await stream.text()
        assert(!raw.includes('event: stream_error'), 'business turn failed: ' + raw.slice(-500))

        const list = await (await page.request.get(`${base}/api/chat/${chatId}/runs`)).json()
        const run = list.runs[0]
        assert.equal(run.captureStatus, 'degraded', 'expected degraded capture, saw ' + run.captureStatus)
        assert(run.droppedEvents >= 1, 'expected dropped count')

        // The business result is unaffected: the card can be confirmed normally.
        await page.goto(`${base}/chat/${chatId}`)
        const card = page.getByRole('region', { name: '请假确认卡' }).last()
        await card.getByRole('button', { name: '确认提交' }).click({ timeout: 30000 })
        const line = card.getByText(/HR 单号 leave_/)
        await line.waitFor({ timeout: 30000 })
        const leaveId = (await line.innerText()).match(/leave_[A-Za-z0-9_-]+/)[0]

        const after = await (await page.request.get(`${base}/api/chat/${chatId}/runs/${run.runId}`)).json()
        assert.equal(after.run.captureStatus, 'degraded', 'degradation was cleared')
        console.log(
            'PASS capture fault: degraded=' + after.run.captureStatus,
            'dropped=' + after.run.droppedEvents,
            'integrity=' + after.verification.status,
            'leave=' + leaveId,
            'runId=' + run.runId,
            'chatId=' + chatId,
        )
    } finally {
        await browser.close()
    }
})().catch((error) => {
    console.error('FAIL', error.message)
    process.exitCode = 1
})
