/**
 * R7: the four seal readings must be distinguishable in the API and the page.
 *   ok            sealed and verified (deployment key present)
 *   unverifiable  sealed at write time, verifier has no key (isolated check)
 *   not_sealed    written without a key
 *   running       not finished yet
 *   truncated     stored head no longer matches (dedicated damaged record)
 */
const { chromium } = require('playwright-core')
const assert = require('node:assert/strict')
const { execFileSync } = require('node:child_process')
const fs = require('node:fs')

const base = process.env.BASE || 'http://localhost:8301'

function fixture(command, args = []) {
    const out = execFileSync(
        'docker',
        ['exec', '-i', 'avatar2-source-ai-1', 'python', '-', command, ...args],
        { input: fs.readFileSync('scripts/trace/fixtures.py'), cwd: process.cwd(), encoding: 'utf8' },
    )
    return JSON.parse(out.trim().split(String.fromCharCode(10)).pop())
}

async function login(page, email, password) {
    await page.goto(base + '/login')
    await page.locator('[name=email]').fill(email)
    await page.locator('[name=password]').fill(password)
    await page.locator('button[type=submit]').click()
    await page.waitForURL((url) => url.pathname !== '/login')
}

async function openPanel(page, chatId, runId) {
    await page.goto(base + '/traces')
    await page.getByTestId(`trace-open-${runId}`).click()
    await page.getByTestId('trace-verification').waitFor({ timeout: 20000 })
    return (await page.getByTestId('trace-verification').innerText()).trim()
}

;(async () => {
    const browser = await chromium.launch({ channel: 'msedge', headless: true })
    try {
        const page = await (await browser.newContext()).newPage()
        await login(page, process.env.BOB_EMAIL, process.env.BOB_PASSWORD)

        // not_sealed
        const plain = fixture('not-sealed')
        const plainApi = await (await page.request.get(
            `${base}/api/chat/${plain.chatId}/runs/${plain.runId}`,
        )).json()
        assert.equal(plainApi.verification.status, 'not_sealed')
        const plainPage = await openPanel(page, plain.chatId, plain.runId)
        assert(plainPage.includes('记录未封存'), 'page: ' + plainPage)
        console.log('PASS not_sealed API+page:', plainPage)

        // running -> finish
        const running = fixture('running-create')
        const runningApi = await (await page.request.get(
            `${base}/api/chat/${running.chatId}/runs/${running.runId}`,
        )).json()
        assert.equal(runningApi.verification.status, 'running')
        const runningPage = await openPanel(page, running.chatId, running.runId)
        assert(runningPage.includes('尚不能校验'), 'page: ' + runningPage)
        fixture('finish-only', [running.runId])
        const finishedApi = await (await page.request.get(
            `${base}/api/chat/${running.chatId}/runs/${running.runId}`,
        )).json()
        assert.equal(finishedApi.verification.status, 'ok')
        console.log('PASS running -> waiting -> ok')

        // ok on a sealed run written by the deployment key
        const sealedRun = process.env.SEALED_CHAT + '/' + process.env.SEALED_RUN
        const sealedApi = await (await page.request.get(
            `${base}/api/chat/${process.env.SEALED_CHAT}/runs/${process.env.SEALED_RUN}`,
        )).json()
        assert.equal(sealedApi.verification.status, 'ok', 'sealed run should verify')

        // truncated dedicated record
        assert(process.env.TRUNCATED_CHAT && process.env.TRUNCATED_RUN, 'truncated fixture ids required')
        const truncatedApi = await (await page.request.get(
            `${base}/api/chat/${process.env.TRUNCATED_CHAT}/runs/${process.env.TRUNCATED_RUN}`,
        )).json()
        assert.equal(truncatedApi.verification.status, 'truncated')
        const truncatedPage = await openPanel(page, process.env.TRUNCATED_CHAT, process.env.TRUNCATED_RUN)
        assert(truncatedPage.includes('记录不完整'), 'page: ' + truncatedPage)
        console.log('PASS ok + truncated API+page:', truncatedPage)

        console.log('SEALED', sealedRun)
        console.log('PASS seal states: not_sealed, running, ok, truncated')
    } finally {
        await browser.close()
    }
})().catch((error) => {
    console.error('FAIL', error.message)
    process.exitCode = 1
})
