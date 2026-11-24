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
    const [span1, span2] = (process.env.SPAN_IDS || '').split(',')
    const decoySpan = process.env.DECOY_SPAN
    assert(chatId && runId && draft1 && draft2 && span1 && span2 && decoySpan, 'fixture ids required')

    const browser = await chromium.launch({ channel: 'msedge', headless: true })
    try {
        const page = await (await browser.newContext()).newPage()
        await login(page, process.env.BOB_EMAIL, process.env.BOB_PASSWORD)

        const all = await page.request.get(`${base}/api/chat/${chatId}/runs/${runId}/business`)
        assert(all.ok(), 'business ' + all.status())
        const body = await all.json()
        assert.equal(body.drafts.length, 2, 'expected exactly two drafts, saw ' + body.drafts.length)
        const first = body.drafts.find((draft) => draft.operationId === draft1)
        const second = body.drafts.find((draft) => draft.operationId === draft2)
        assert(first && second, 'draft ids not associated')
        assert.equal(first.spanId, span1)
        assert.equal(second.spanId, span2)
        assert.equal(first.leaveId, process.env.LEAVE_ID, 'confirmed draft should expose its leave id')
        assert(second.leaveId === null && second.cardStatus === 'pending', 'second draft must stay pending')
        assert(!body.drafts.some((draft) => draft.operationId.includes('DECOY')), 'decoy must not be associated')

        const one = await page.request.get(
            `${base}/api/chat/${chatId}/runs/${runId}/business?spanId=${span2}`,
        )
        const oneBody = await one.json()
        assert.equal(oneBody.drafts.length, 1, 'span filter should return one draft')
        assert.equal(oneBody.drafts[0].operationId, draft2)

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
            body.drafts.map((draft) => `${draft.operationId}:${draft.leaveId ?? 'pending'}`).join(' '),
        )
    } finally {
        await browser.close()
    }
})().catch((error) => {
    console.error('FAIL', error.message)
    process.exitCode = 1
})
