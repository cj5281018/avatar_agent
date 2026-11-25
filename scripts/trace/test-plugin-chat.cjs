/**
 * Real-chat test for a newly registered plugin.
 *
 * Run with AVATAR2_ENABLE_TEST_PLUGIN=1 on the gateway: the model discovers
 * testkit_echo through the normal catalog, calls it, and the trace attributes
 * the step to the testkit department. No public gateway, AI handler or
 * workbench change is involved.
 */
const { chromium } = require('playwright-core')
const assert = require('node:assert/strict')

const base = process.env.BASE || 'http://localhost:8301'

async function login(page, email, password) {
    await page.goto(base + '/login')
    await page.locator('[name=email]').fill(email)
    await page.locator('[name=password]').fill(password)
    await page.locator('button[type=submit]').click()
    await page.waitForURL((url) => url.pathname !== '/login')
}

;(async () => {
    const browser = await chromium.launch({ channel: 'msedge', headless: true })
    try {
        const page = await (await browser.newContext()).newPage()
        await login(page, process.env.BOB_EMAIL, process.env.BOB_PASSWORD)
        await page.goto(base + '/employee-binding')
        await page.getByRole('button', { name: '绑定或续期' }).click()
        await page.getByText(/已绑定，有效至/).waitFor({ timeout: 15000 })

        const created = await page.request.post(base + '/api/chat', {
            data: { modelId: process.env.MODEL_ID },
        })
        const { chatId } = await created.json()
        await page.request.post(`${base}/api/chat/${chatId}/messages`, {
            data: {
                content:
                    '请调用 testkit_echo 工具，参数 message="接入测试"。只调用一次，不要解释。',
            },
        })
        const stream = await page.request.get(`${base}/api/chat/${chatId}/stream`, {
            headers: { accept: 'text/event-stream' },
            timeout: 150000,
        })
        await stream.text()

        const runs = (await (await page.request.get(`${base}/api/chat/${chatId}/runs`)).json()).runs
        assert(runs.length === 1, 'one run expected')
        const detail = await (await page.request.get(
            `${base}/api/chat/${chatId}/runs/${runs[0].runId}`,
        )).json()
        const event = detail.events.find(
            (item) => item.type === 'tool.completed' && item.payload.tool === 'testkit_echo',
        )
        assert(event, 'testkit_echo was not called: ' + detail.events.map((e) => e.type).join(','))
        assert.equal(event.payload.department, 'testkit')
        assert.equal(event.payload.action, 'echo')
        assert.equal(event.payload.pluginVersion, '0.0.1')
        console.log(
            'PASS real chat called a newly registered plugin:',
            event.payload.department + '.' + event.payload.action,
            'runId=' + runs[0].runId,
        )
    } finally {
        await browser.close()
    }
})().catch((error) => {
    console.error('FAIL', error.message)
    process.exitCode = 1
})
