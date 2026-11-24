/**
 * R2/R4 browser checks:
 *  A first snapshot failure recovers without refresh
 *  B incremental failure keeps steps and resumes without duplicates
 *  C switching runs cancels the old request
 *  D closing the panel cancels pending detail/business requests and stops polling
 *  E terminal runs stop polling
 *  F a stale run recovers when it later finishes (needs STALE_RUN_ID/SPAN)
 */
const { chromium } = require('playwright-core')
const assert = require('node:assert/strict')
const { execFileSync } = require('node:child_process')
const fs = require('node:fs')

const base = process.env.BASE || 'http://localhost:8301'
const modelId = process.env.MODEL_ID
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
const detailPath = (chat, run) => `/api/chat/${chat}/runs/${run}`

async function login(page, email, password) {
    await page.goto(base + '/login')
    await page.locator('[name=email]').fill(email)
    await page.locator('[name=password]').fill(password)
    await page.locator('button[type=submit]').click()
    await page.waitForURL((url) => url.pathname !== '/login')
}

function isDetail(url, chat, run, withCursor) {
    const parsed = new URL(url)
    if (parsed.pathname !== detailPath(chat, run)) return false
    return withCursor === undefined ? true : parsed.searchParams.has('afterSeq') === withCursor
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
    return { chatId, runId: runs[0].runId, events: (await (await page.request.get(
        `${base}/api/chat/${chatId}/runs/${runs[0].runId}`,
    )).json()).events }
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
        // ---------------- A: first snapshot failure ----------------
        {
            const context = await browser.newContext()
            const page = await context.newPage()
            await login(page, process.env.BOB_EMAIL, process.env.BOB_PASSWORD)
            const sample = await newTurn(page, '请用一句话说明请假需要主管确认的原因。')
            let aborted = 0
            await page.route(
                (url) => isDetail(url.href, sample.chatId, sample.runId, false),
                async (route) => {
                    if (aborted === 0) {
                        aborted += 1
                        await route.abort('failed')
                        return
                    }
                    await route.continue().catch(() => {})
                },
            )
            await page.goto(base + '/traces')
            await page.getByTestId(`trace-open-${sample.runId}`).click()
            // No refresh: the panel retries the snapshot itself.
            await page.getByTestId('trace-steps').waitFor({ timeout: 30000 })
            await page.getByTestId('trace-verification').filter({ hasText: '校验通过' }).waitFor({ timeout: 20000 })
            assert.equal(aborted, 1, 'first snapshot was not the one interrupted')
            console.log('PASS A first snapshot failure recovered without refresh')
            await context.close()
        }

        // ---------------- B: incremental failure ----------------
        {
            const context = await browser.newContext()
            const page = await context.newPage()
            await login(page, process.env.BOB_EMAIL, process.env.BOB_PASSWORD)
            // Deterministic running fixture: the panel polls while the run is
            // still open, then the fixture finishes it.
            const created = JSON.parse(
                execFileSync(
                    'docker',
                    ['exec', '-i', 'avatar2-source-ai-1', 'python', '-', 'running-create'],
                    { input: fs.readFileSync('scripts/trace/fixtures.py'), cwd: process.cwd(), encoding: 'utf8' },
                )
                    .trim()
                    .split(String.fromCharCode(10))
                    .pop(),
            )
            let aborted = 0
            await page.route(
                (url) => {
                    const parsed = new URL(url.href)
                    return (
                        parsed.pathname === detailPath(created.chatId, created.runId) &&
                        parsed.searchParams.has('afterSeq')
                    )
                },
                async (route) => {
                    if (aborted === 0) {
                        aborted += 1
                        await route.abort('failed')
                        return
                    }
                    await route.continue().catch(() => {})
                },
            )
            await page.goto(base + '/traces')
            await page.getByTestId(`trace-open-${created.runId}`).click()
            await page.getByTestId('trace-execution').filter({ hasText: '问答进行中' }).waitFor({ timeout: 20000 })
            await sleep(4000)
            assert.equal(aborted, 1, 'incremental request was not interrupted')
            // The snapshot and its existing steps survive the failed poll.
            await page.getByTestId('trace-step-model').first().waitFor({ timeout: 10000 })
            execFileSync(
                'docker',
                [
                    'exec', '-i', 'avatar2-source-ai-1', 'python', '-', 'running-finish',
                    created.runId, created.modelSpanId,
                ],
                { input: fs.readFileSync('scripts/trace/fixtures.py'), cwd: process.cwd() },
            )
            await page.getByTestId('trace-execution').filter({ hasText: '问答已结束' }).waitFor({ timeout: 30000 })
            const detail = await (
                await page.request.get(`${base}/api/chat/${created.chatId}/runs/${created.runId}`)
            ).json()
            await sleep(1000)
            const rendered = await page.locator('[data-testid^="trace-step-"]').count()
            assert.equal(rendered, distinctSpans(detail.events), 'steps missing or duplicated after recovery')
            console.log('PASS B incremental failure recovered without duplicates')
            await context.close()
        }

        // ---------------- C: switching cancels, D: close cancels ----------------
        {
            const context = await browser.newContext()
            const page = await context.newPage()
            await login(page, process.env.BOB_EMAIL, process.env.BOB_PASSWORD)
            const first = await newTurn(page, '请用一句话说明年假折算的依据。')
            const second = await newTurn(page, '请用一句话说明事假和年假的区别。')

            const cancelledA = page.waitForEvent('requestfailed', {
                predicate: (request) => isDetail(request.url(), first.chatId, first.runId, undefined),
                timeout: 20000,
            })
            await page.route(
                (url) => isDetail(url.href, first.chatId, first.runId, undefined),
                async (route) => {
                    await sleep(5000)
                    await route.continue().catch(() => {})
                },
            )
            await page.goto(base + '/traces')
            await page.getByTestId(`trace-open-${first.runId}`).click()
            await sleep(500)
            await page.keyboard.press('Escape')
            await page.getByTestId(`trace-open-${second.runId}`).click()
            const runLine = page.getByText(/runId [0-9a-f]{32}/)
            await runLine.waitFor({ timeout: 20000 })
            assert((await runLine.innerText()).includes(second.runId), 'second run did not open')
            const failure = await cancelledA
            assert.match(failure.failure().errorText, /ABORT/i, 'old request was not cancelled')
            console.log('PASS C switching cancelled the previous request')

            // D: close while the business lookup is pending.
            const businessPending = page.waitForEvent('requestfailed', {
                predicate: (request) => request.url().includes('/business'),
                timeout: 20000,
            })
            await page.route('**/business', async (route) => {
                await sleep(8000)
                await route.continue().catch(() => {})
            })
            await page.keyboard.press('Escape')
            await sleep(300)
            await page.getByTestId(`trace-open-${first.runId}`).click()
            await page.getByTestId('trace-business').waitFor({ timeout: 20000 })
            await sleep(400)
            await page.keyboard.press('Escape')
            const businessFailure = await businessPending
            assert.match(businessFailure.failure().errorText, /ABORT/i, 'business request was not cancelled')
            let detailRequests = 0
            const counter = (request) => {
                if (request.url().includes(`/runs/${first.runId}`)) detailRequests += 1
            }
            page.on('request', counter)
            await sleep(3000)
            assert.equal(detailRequests, 0, 'panel kept polling after close')
            console.log('PASS D closing cancelled in-flight requests and stopped polling')
            await context.close()
        }

        // ---------------- E: terminal stops polling ----------------
        {
            const context = await browser.newContext()
            const page = await context.newPage()
            await login(page, process.env.BOB_EMAIL, process.env.BOB_PASSWORD)
            const sample = await newTurn(page, '请用一句话说明年假申请的一般流程。')
            await page.goto(base + '/traces')
            await page.getByTestId(`trace-open-${sample.runId}`).click()
            await page.getByTestId('trace-execution').filter({ hasText: '问答已结束' }).waitFor({ timeout: 20000 })
            await sleep(1000)
            let counted = 0
            page.on('request', (request) => {
                if (isDetail(request.url(), sample.chatId, sample.runId, true)) counted += 1
            })
            await sleep(6000)
            assert.equal(counted, 0, 'terminal run still polled: ' + counted)
            console.log('PASS E terminal run stopped polling')
            await context.close()
        }

        // ---------------- F: stale run recovers ----------------
        if (process.env.STALE_CHAT && process.env.STALE_RUN && process.env.STALE_SPAN) {
            const context = await browser.newContext()
            const page = await context.newPage()
            await login(page, process.env.BOB_EMAIL, process.env.BOB_PASSWORD)
            await page.goto(base + '/traces')
            await page.getByTestId(`trace-open-${process.env.STALE_RUN}`).click()
            await page.getByTestId('trace-execution').filter({ hasText: '最近无心跳' }).waitFor({ timeout: 20000 })
            execFileSync(
                'docker',
                [
                    'exec', '-i', 'avatar2-source-ai-1', 'python', '-', 'stale-finish',
                    process.env.STALE_RUN, process.env.STALE_SPAN,
                ],
                { input: fs.readFileSync('scripts/trace/fixtures.py'), cwd: process.cwd() },
            )
            await page.getByTestId('trace-execution').filter({ hasText: '问答已结束' }).waitFor({ timeout: 25000 })
            await page.getByTestId('trace-verification').filter({ hasText: '校验通过' }).waitFor({ timeout: 20000 })
            console.log('PASS F stale run recovered when it later finished')
            await context.close()
        }
    } finally {
        await browser.close()
    }
})().catch((error) => {
    console.error('FAIL', error.message)
    process.exitCode = 1
})
