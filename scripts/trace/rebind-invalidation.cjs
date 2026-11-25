/**
 * Rebinding must invalidate personal sessions and unused tickets, including a
 * rebind of the same employee after unbinding.
 */
const { chromium } = require('playwright-core')
const assert = require('node:assert/strict')
const { execFileSync } = require('node:child_process')

const base = process.env.BASE || 'http://localhost:8301'
const hrBase = process.env.HR_BASE || 'http://localhost:8892'

async function login(page, email, password) {
    await page.goto(base + '/login')
    await page.locator('[name=email]').fill(email)
    await page.locator('[name=password]').fill(password)
    await page.locator('button[type=submit]').click()
    await page.waitForURL((url) => url.pathname !== '/login')
}

async function renew(page) {
    await page.goto(base + '/employee-binding')
    await page.getByRole('button', { name: '绑定或续期' }).click()
    await page.getByText(/已绑定，有效至/).waitFor({ timeout: 15000 })
    await page.waitForTimeout(300)
}

async function ticket(page) {
    const response = await page.request.post(base + '/api/hr-entry', {
        data: { target: 'me' },
        headers: { origin: base },
    })
    assert(response.ok(), 'ticket ' + response.status())
    return response.json()
}

function expireLogins() {
    execFileSync('docker', [
        'exec', 'avatar2-source-gateway-1', 'python', '-c',
        "import sqlite3; db=sqlite3.connect('/data/avatar.sqlite3'); db.execute(\"UPDATE logins SET expires_at=0 WHERE user_id=(SELECT id FROM users WHERE handle='bob')\"); db.commit()",
    ])
}

;(async () => {
    const browser = await chromium.launch({ channel: 'msedge', headless: true })
    try {
        const context = await browser.newContext()
        const page = await context.newPage()
        await login(page, process.env.BOB_EMAIL, process.env.BOB_PASSWORD)
        await renew(page)

        // Open a personal session.
        const hr = await context.newPage()
        const first = await ticket(page)
        await hr.request.post(first.action, { form: { ticket: first.ticket } })
        await hr.goto(hrBase + '/web/me')
        await hr.getByText('我的申请').first().waitFor({ timeout: 20000 })

        // Renew (rebind the same employee): the open session must stop working.
        await renew(page)
        await hr.reload({ waitUntil: 'domcontentloaded' })
        await hr.locator('body').getByText(/重新进入|绑定已变更|无法访问/).first().waitFor({ timeout: 20000 })
        console.log('PASS renewal invalidated the open HR session')

        // A ticket issued before a renewal must not be redeemable after it.
        const stale = await ticket(page)
        await renew(page)
        const staleResponse = await context.request.post(stale.action, {
            form: { ticket: stale.ticket },
            maxRedirects: 0,
        })
        assert.equal(staleResponse.status(), 409, 'stale ticket status ' + staleResponse.status())
        console.log('PASS ticket issued before rebinding rejected at exchange (409)')

        // Expiring the binding blocks both a fresh ticket and the open session.
        const expired = await ticket(page)
        expireLogins()
        const expiredResponse = await context.request.post(expired.action, {
            form: { ticket: expired.ticket },
            maxRedirects: 0,
        })
        assert.equal(expiredResponse.status(), 403, 'expired binding ticket ' + expiredResponse.status())
        await hr.reload({ waitUntil: 'domcontentloaded' })
        await hr.locator('body').getByText(/绑定不可用|重新进入|无法访问/).first().waitFor({ timeout: 20000 })
        console.log('PASS expired binding blocks ticket exchange and the open session')

        // Rebinding the same employee after unbinding produces a new generation.
        await renew(page)
        await page.goto(base + '/employee-binding')
        await page.getByRole('button', { name: '解除绑定' }).click()
        await page.getByText('已解除绑定', { exact: true }).waitFor({ timeout: 15000 })
        const beforeRebind = await (async () => {
            try {
                return await page.request.post(base + '/api/hr-entry', {
                    data: { target: 'me' },
                    headers: { origin: base },
                })
            } catch {
                return null
            }
        })()
        if (beforeRebind) assert.equal(beforeRebind.status(), 403, 'unbound ticket should be refused')
        await renew(page)
        await hr.reload({ waitUntil: 'domcontentloaded' })
        await hr.locator('body').getByText(/重新进入|绑定已变更|无法访问/).first().waitFor({ timeout: 20000 })
        const after = await ticket(page)
        const ok = await context.request.post(after.action, { form: { ticket: after.ticket }, maxRedirects: 0 })
        assert.equal(ok.status(), 303)
        console.log('PASS unbind + rebind: old session rejected, new ticket works')
    } finally {
        await browser.close()
    }
})().catch((error) => {
    console.error('FAIL', error.message)
    process.exitCode = 1
})
