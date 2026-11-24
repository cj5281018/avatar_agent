/**
 * R5: cursor, events, run state and verification must describe one snapshot.
 *
 * Hammers the detail endpoint while a fixture appends events concurrently,
 * then checks that a finished run still reports terminal/seal changes and
 * capture changes when no new events exist.
 */
const { chromium } = require('playwright-core')
const assert = require('node:assert/strict')
const { execFileSync, spawn } = require('node:child_process')
const fs = require('node:fs')

const base = process.env.BASE || 'http://localhost:8301'
const script = () => fs.readFileSync('scripts/trace/fixtures.py')

function fixture(command, args = []) {
    const out = execFileSync(
        'docker',
        ['exec', '-i', 'avatar2-source-ai-1', 'python', '-', command, ...args],
        { input: script(), cwd: process.cwd(), encoding: 'utf8' },
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

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

;(async () => {
    const browser = await chromium.launch({ channel: 'msedge', headless: true })
    try {
        const page = await (await browser.newContext()).newPage()
        await login(page, process.env.BOB_EMAIL, process.env.BOB_PASSWORD)

        const run = fixture('running-create')
        const writer = spawn(
            'docker',
            ['exec', '-i', 'avatar2-source-ai-1', 'python', '-', 'append-many', run.runId, run.modelSpanId, '15'],
            { stdio: ['pipe', 'pipe', 'pipe'] },
        )
        const exited = new Promise((resolve) => writer.on('exit', resolve))
        writer.stdin.end(script())

        const samples = []
        for (let index = 0; index < 25; index += 1) {
            const response = await page.request.get(`${base}/api/chat/${run.chatId}/runs/${run.runId}`)
            assert(response.ok(), 'detail ' + response.status())
            const body = await response.json()
            const maxSeq = body.events.reduce((max, event) => Math.max(max, event.seq), 0)
            assert(
                maxSeq <= body.cursor,
                `event ${maxSeq} beyond cursor ${body.cursor} — snapshot is inconsistent`,
            )
            if (body.run.terminal) {
                assert.equal(body.cursor, body.run.lastSeq ?? body.cursor)
            }
            samples.push({
                cursor: body.cursor,
                returned: body.events.length,
                status: body.run.executionState,
                verification: body.verification.status,
            })
            await sleep(120)
        }
        await exited

        // finish-only: terminal and seal appear with no new events.
        const beforeFinish = await (
            await page.request.get(`${base}/api/chat/${run.chatId}/runs/${run.runId}`)
        ).json()
        fixture('finish-only', [run.runId])
        const finished = await (
            await page.request.get(`${base}/api/chat/${run.chatId}/runs/${run.runId}`)
        ).json()
        assert.equal(finished.run.executionState, 'completed', 'finish-only did not make the run terminal')
        assert.equal(finished.verification.status, 'ok', 'seal did not verify after finish-only')
        const delta = await (
            await page.request.get(
                `${base}/api/chat/${run.chatId}/runs/${run.runId}?afterSeq=${finished.cursor}`,
            )
        ).json()
        assert.deepEqual(delta.events, [], 'afterSeq=cursor should return no events')
        assert.equal(delta.run.executionState, 'completed', 'terminal state missing from empty delta')
        assert.equal(delta.verification.status, 'ok')

        // Capture change with no new events.
        execFileSync(
            'docker',
            ['exec', '-i', 'avatar2-source-postgres-1', 'sh', '-c',
             `psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "UPDATE agent_trace_runs SET capture_status='degraded', capture_error='consistency_fixture' WHERE run_id='${run.runId}'"`],
        )
        const degraded = await (
            await page.request.get(
                `${base}/api/chat/${run.chatId}/runs/${run.runId}?afterSeq=${finished.cursor}`,
            )
        ).json()
        assert.deepEqual(degraded.events, [])
        assert.equal(degraded.run.captureStatus, 'degraded', 'capture change missing from empty delta')

        console.log(
            'PASS snapshot consistency:',
            JSON.stringify({
                samples: samples.length,
                first: samples[0],
                last: samples[samples.length - 1],
                beforeFinish: { cursor: beforeFinish.cursor, status: beforeFinish.run.executionState },
                finishOnly: { events: 0, status: delta.run.executionState, verification: delta.verification.status },
                captureOnly: { events: 0, capture: degraded.run.captureStatus },
            }),
        )
    } finally {
        await browser.close()
    }
})().catch((error) => {
    console.error('FAIL', error.message)
    process.exitCode = 1
})
