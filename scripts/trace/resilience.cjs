/**
 * Resilience checks for the trace panel:
 *   - switching runs cancels the previous load and a late response cannot
 *     overwrite the newly opened run
 *   - a dropped poll mid-run is recovered without duplicate step nodes
 *   - polling stops once the run is terminal
 */
const { chromium } = require('playwright-core')
const assert = require('node:assert/strict')

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

async function newTurn(page, content) {
    const created = await page.request.post(base + '/api/chat', { data: { modelId } })
    const { chatId } = await created.json()
    await page.request.post(`${base}/api/chat/${chatId}/messages`, { data: { content } })
    const stream = await page.request.get(`${base}/api/chat/${chatId}/stream`, {
        headers: { accept: 'text/event-stream' },
        timeout: 150000,
    })
    await stream.text()
    const runs = (await (await page.request.get(`${base}/api/chat/${chatId}/runs`)).json()).runs
    return { chatId, runId: runs[0].runId }
}

function distinctSpans(events) {
    return new Set(
        events
            .filter((event) => event.type.startsWith('model.') || event.type.startsWith('tool.'))
            .map((event) => event.spanId),
    ).size
}

;(async () => {
    const browser = await chromium.launch({ channel: 'msedge', headless: true })
    try {
        const page = await (await browser.newContext()).newPage()
        await login(page, process.env.BOB_EMAIL, process.env.BOB_PASSWORD)

        // --- switch away from a slow run ---
        const first = await newTurn(page, '请用一句话说明为什么需要请假审批。')
        const second = await newTurn(page, '请用一句话说明年假余额和工资的关系。')

        await page.route(`**/api/chat/${first.chatId}/runs/${first.runId}*`, async (route) => {
            await sleep(4000)
            await route.continue().catch(() => {})
        })
        await page.goto(base + '/traces')
        await page.getByTestId(`trace-open-${first.runId}`).click()
        await sleep(400)
        await page.keyboard.press('Escape')
        await page.getByTestId(`trace-open-${second.runId}`).click()
        const runLine = page.getByText(/runId [0-9a-f]{32}/)
        await runLine.waitFor({ timeout: 20000 })
        assert((await runLine.innerText()).includes(second.runId), 'second run did not open')
        await sleep(5000)
        const afterLate = await page.getByText(/runId [0-9a-f]{32}/).innerText()
        assert(afterLate.includes(second.runId), 'late response overwrote the current run')
        await page.unroute(`**/api/chat/${first.chatId}/runs/${first.runId}*`)
        console.log('PASS switching runs: late response did not overwrite')

        // --- dropped poll during a live run ---
        const created = await page.request.post(base + '/api/chat', { data: { modelId } })
        const { chatId } = await created.json()
        await page.request.post(`${base}/api/chat/${chatId}/messages`, {
            data: { content: '请实际搜索中国带薪年休假法规，并总结两条要点。' },
        })
        const streamDone = page.request
            .get(`${base}/api/chat/${chatId}/stream`, {
                headers: { accept: 'text/event-stream' },
                timeout: 150000,
            })
            .then((response) => response.text())

        // Wait until the run row exists, then open the panel while the turn is
        // still streaming, so at least one incremental poll happens.
        for (let attempt = 0; attempt < 60; attempt += 1) {
            const list = await (await page.request.get(`${base}/api/chat/${chatId}/runs`)).json()
            if (list.runs.length > 0) break
            await sleep(250)
        }
        await page.goto(`${base}/chat/${chatId}`)
        const entry = page.getByTestId('trace-entry').last()
        await entry.waitFor({ timeout: 20000 })

        let aborted = false
        // Drop the first detail request (snapshot or poll) once, then let the
        // panel's retry path take over.
        await page.route(`**/api/chat/${chatId}/runs/*`, async (route) => {
            if (!aborted) {
                aborted = true
                await route.abort('failed').catch(() => {})
                return
            }
            await route.continue().catch(() => {})
        })
        await entry.click()
        await page.getByTestId('trace-execution').waitFor({ timeout: 20000 })

        await streamDone
        await page.getByTestId('trace-execution').filter({ hasText: '问答已结束' }).waitFor({ timeout: 60000 })
        assert(aborted, 'no poll was aborted; recovery path not exercised')
        await sleep(2500)
        await page.unroute(`**/api/chat/${chatId}/runs/*`)

        const runId = (await page.getByText(/runId [0-9a-f]{32}/).innerText()).trim().split(' ')[1]
        const detail = await (await page.request.get(`${base}/api/chat/${chatId}/runs/${runId}`)).json()
        const expected = distinctSpans(detail.events)
        const rendered = await page.locator('[data-testid^="trace-step-"]').count()
        assert.equal(rendered, expected, `duplicate or missing nodes: ${rendered} vs ${expected}`)
        console.log('PASS dropped poll recovered: nodes=' + rendered + ' expected=' + expected)

        // --- polling stops at terminal ---
        let detailRequests = 0
        const counter = (request) => {
            if (/\/runs\/[0-9a-f]{32}/.test(request.url())) detailRequests += 1
        }
        page.on('request', counter)
        const before = detailRequests
        await sleep(5000)
        assert(detailRequests - before <= 1, 'polling continued after the run finished: ' + (detailRequests - before))
        console.log('PASS polling stopped after terminal state')
    } finally {
        await browser.close()
    }
})().catch((error) => {
    console.error('FAIL', error.message)
    process.exitCode = 1
})
