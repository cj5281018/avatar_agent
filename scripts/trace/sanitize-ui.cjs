const { chromium } = require('playwright-core')
const assert = require('node:assert/strict')

const base = process.env.BASE || 'http://localhost:8301'

;(async () => {
    const browser = await chromium.launch({ channel: 'msedge', headless: true })
    try {
        const page = await (await browser.newContext()).newPage()
        await page.goto(base + '/login')
        await page.locator('[name=email]').fill(process.env.BOB_EMAIL)
        await page.locator('[name=password]').fill(process.env.BOB_PASSWORD)
        await page.locator('button[type=submit]').click()
        await page.waitForURL((url) => url.pathname !== '/login')

        const chatId = process.env.CHAT_ID
        const runId = process.env.RUN_ID
        const detail = await (await page.request.get(`${base}/api/chat/${chatId}/runs/${runId}`)).json()
        const failed = detail.events.find((event) => event.type === 'tool.failed' || event.type === 'run.error')
        assert(failed, 'no failure event')
        const serialized = JSON.stringify(detail)
        assert(!serialized.includes('LEAK_MARKER'), 'ledger/API leaked the marker')
        for (const forbidden of ['operationId', 'leaveId', 'titles', 'hits', 'queryPreview']) {
            assert(!(forbidden in failed.payload), 'failed payload contains ' + forbidden)
        }
        assert(typeof failed.payload.reasonCode === 'string' && failed.payload.reasonCode.length > 0)

        const delta = await (await page.request.get(
            `${base}/api/chat/${chatId}/runs/${runId}?afterSeq=1`,
        )).json()
        assert(!JSON.stringify(delta).includes('LEAK_MARKER'), 'incremental API leaked the marker')

        await page.goto(base + '/traces')
        await page.getByTestId(`trace-open-${runId}`).click()
        await page.getByTestId('trace-status').waitFor({ timeout: 20000 })
        await page.waitForTimeout(800)
        const text = await page.locator('body').innerText()
        assert(!text.includes('LEAK_MARKER'), 'page leaked the marker')
        console.log('PASS sanitized failure across ledger, detail, incremental API and page')
    } finally {
        await browser.close()
    }
})().catch((error) => {
    console.error('FAIL', error.message)
    process.exitCode = 1
})
