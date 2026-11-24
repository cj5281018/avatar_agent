/**
 * Brand verification: visible title, manifest, apple title, favicon, and
 * light/dark screenshots of the login page.
 */
const { chromium } = require('playwright-core')
const assert = require('node:assert/strict')

const base = process.env.BASE || 'http://localhost:8301'

;(async () => {
    const browser = await chromium.launch({ channel: 'msedge', headless: true })
    try {
        for (const scheme of ['light', 'dark']) {
            const context = await browser.newContext({
                colorScheme: scheme,
                viewport: { width: 1280, height: 800 },
            })
            await context.addInitScript(
                (theme) => {
                    localStorage.setItem(
                        'omni-user-preferences',
                        JSON.stringify({ theme, themeColorScheme: theme }),
                    )
                },
                scheme,
            )
            const page = await context.newPage()
            await page.goto(base + '/login')
            await page.waitForTimeout(1200)
            assert.match(await page.title(), /Avatar/, 'login title')
            await page.screenshot({ path: `docs/brand/login-${scheme}.png` })
            await context.close()
        }

        const context = await browser.newContext()
        const page = await context.newPage()
        const manifestResponse = await page.request.get(base + '/manifest.webmanifest')
        const manifest = await manifestResponse.json()
        assert.equal(manifest.name, 'Avatar')
        assert.equal(manifest.short_name, 'Avatar')
        assert(manifest.icons.some((icon) => icon.src === '/avatar-mark.svg'))
        const html = await (await page.request.get(base + '/login')).text()
        const apple = /apple-mobile-web-app-title" content="([^"]+)"/.exec(html)?.[1]
        assert.equal(apple, 'Avatar')
        assert(html.includes('/avatar-mark.svg'), 'favicon should be the Avatar mark')
        console.log(
            'PASS brand: title=' + (await page.title()),
            'manifest=' + manifest.name + '/' + manifest.short_name,
            'apple=' + apple,
            'screenshots=docs/brand/login-{light,dark}.png',
        )
        await context.close()
    } finally {
        await browser.close()
    }
})().catch((error) => {
    console.error('FAIL', error.message)
    process.exitCode = 1
})
