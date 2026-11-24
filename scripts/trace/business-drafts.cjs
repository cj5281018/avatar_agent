/**
 * Step-level business association (R6).
 *
 * Uses the multi-draft fixture: two real drafts produced in one run plus a
 * decoy tool event that also returns an operation id. Expects exactly two
 * drafts, the first confirmed with a leave id, the second still pending.
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
    const chatId = process.env.CHAT_ID
    const runId = process.env.RUN_ID
    const [draft1, draft2] = (process.env.OP_IDS || '').split(',')
    const [span1, span2, span3] = (process.env.SPAN_IDS || '').split(',')
    const decoySpan = process.env.DECOY_SPAN
    assert(chatId && runId && draft1 && draft2 && span1 && span2 && span3 && decoySpan, 'fixture ids required')

    const browser = await chromium.launch({ channel: 'msedge', headless: true })
    try {
        const page = await (await browser.newContext()).newPage()
        await login(page, process.env.BOB_EMAIL, process.env.BOB_PASSWORD)

        const all = await page.request.get(`${base}/api/chat/${chatId}/runs/${runId}/business`)
        assert(all.ok(), 'business ' + all.status())
        const body = await all.json()
        assert.equal(body.drafts.length, 3, 'expected three drafting steps, saw ' + body.drafts.length)
        const bySpan = new Map(body.drafts.map((draft) => [draft.spanId, draft]))
        assert(bySpan.get(span1)?.operationId === draft1, 'first span association')
        assert(bySpan.get(span2)?.operationId === draft2, 'second span association')
        assert(bySpan.get(span3)?.operationId === draft1, 'third span must reuse the first operation')
        assert.equal(bySpan.get(span1).leaveId, process.env.LEAVE_ID)
        assert.equal(bySpan.get(span3).leaveId, process.env.LEAVE_ID, 'same operation resolves to the same leave')
        assert(bySpan.get(span2).leaveId === null && bySpan.get(span2).cardStatus === 'pending')
        assert(!body.drafts.some((draft) => draft.operationId.includes('DECOY')), 'decoy must not be associated')

        const one = await page.request.get(
            `${base}/api/chat/${chatId}/runs/${runId}/business?spanId=${span3}`,
        )
        const oneBody = await one.json()
        assert.equal(oneBody.drafts.length, 1, 'span filter should return one draft')
        assert.equal(oneBody.drafts[0].operationId, draft1)

        const decoy = await page.request.get(
            `${base}/api/chat/${chatId}/runs/${runId}/business?spanId=${decoySpan}`,
        )
        assert.equal(decoy.status(), 404, 'non-draft step must not expose an association')

        // Access control on the step-level route.
        const aliceContext = await browser.newContext()
        const alice = await aliceContext.newPage()
        await login(alice, process.env.ALICE_EMAIL, process.env.ALICE_PASSWORD)
        const aliceBusiness = await alice.request.get(
            `${base}/api/chat/${chatId}/runs/${runId}/business`,
        )
        assert.equal(aliceBusiness.status(), 403, 'alice business read')

        const adminContext = await browser.newContext()
        const admin = await adminContext.newPage()
        await login(admin, process.env.ADMIN_EMAIL, process.env.ADMIN_PASSWORD)
        const adminBusiness = await admin.request.get(
            `${base}/api/chat/${chatId}/runs/${runId}/business?spanId=${span1}`,
        )
        assert.equal(adminBusiness.status(), 403, 'manager business read')

        console.log(
            'PASS business drafts:',
            body.drafts.map((draft) => `${draft.spanId.slice(0, 6)}→${draft.operationId}:${draft.leaveId ?? 'pending'}`).join(' '),
        )

        if (process.env.MANY_CHAT && process.env.MANY_RUN) {
            const many = await page.request.get(
                `${base}/api/chat/${process.env.MANY_CHAT}/runs/${process.env.MANY_RUN}/business`,
            )
            const manyBody = await many.json()
            assert(manyBody.drafts.length >= 7, 'seven drafts must all be reachable')
            await page.goto(base + '/traces')
            await page.getByTestId(`trace-open-${process.env.MANY_RUN}`).click()
            await page.getByTestId('trace-business').getByTestId('trace-business-draft').first().waitFor({ timeout: 20000 })
            const rendered = await page.getByTestId('trace-business-draft').count()
            assert(rendered >= 7, 'panel should render every drafting step, saw ' + rendered)
            console.log('PASS seven drafts reachable:', rendered)
        }
    } finally {
        await browser.close()
    }
})().catch((error) => {
    console.error('FAIL', error.message)
    process.exitCode = 1
})
