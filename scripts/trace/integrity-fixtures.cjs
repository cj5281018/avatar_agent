/**
 * Integrity fixtures for the trace centre.
 *
 * Creates dedicated throwaway runs, then:
 *   - edits an event payload  -> mismatch, locating the first bad sequence
 *   - deletes the last event  -> truncated
 *   - verifies a sealed run with no key available -> unsealed (never "ok")
 *   - checks the incremental contract: afterSeq returns only newer events
 *
 * The damaged runs are test records and are left in place as evidence.
 */
const { chromium } = require('playwright-core')
const assert = require('node:assert/strict')
const { execFileSync } = require('node:child_process')

const base = process.env.BASE || 'http://localhost:8301'
const modelId = process.env.MODEL_ID

const psql = (sql) =>
    execFileSync(
        'docker',
        ['exec', '-i', 'avatar2-source-postgres-1', 'sh', '-c',
         'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At'],
        { input: sql, encoding: 'utf8' },
    ).trim()

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
    assert.equal(runs.length, 1, 'one run expected')
    return { chatId, runId: runs[0].runId }
}

;(async () => {
    const browser = await chromium.launch({ channel: 'msedge', headless: true })
    try {
        const page = await (await browser.newContext()).newPage()
        await login(page, process.env.BOB_EMAIL, process.env.BOB_PASSWORD)
        const evidence = {}

        // --- incremental contract ---
        const inc = await newTurn(page, '请用一句话说明今天适不适合请假。')
        const snapshot = await (await page.request.get(`${base}/api/chat/${inc.chatId}/runs/${inc.runId}`)).json()
        const cursor = snapshot.cursor
        assert(cursor >= 3, 'expected several events')
        const tail = await (await page.request.get(
            `${base}/api/chat/${inc.chatId}/runs/${inc.runId}?afterSeq=${cursor}`,
        )).json()
        assert.deepEqual(tail.events, [], 'afterSeq=cursor must return no events')
        const lastOnly = await (await page.request.get(
            `${base}/api/chat/${inc.chatId}/runs/${inc.runId}?afterSeq=${cursor - 1}`,
        )).json()
        assert.deepEqual(lastOnly.events.map((event) => event.seq), [cursor])
        evidence.incremental = { cursor, tail: 0, lastOnly: cursor }

        // --- tamper ---
        const tamper = await newTurn(page, '请用一句话解释什么是年假。')
        psql(
            `UPDATE agent_trace_events SET payload = payload || '{"tampered": 1}'::jsonb ` +
                `WHERE run_id = '${tamper.runId}' AND seq = 2;`,
        )
        const tampered = await (await page.request.get(
            `${base}/api/chat/${tamper.chatId}/runs/${tamper.runId}`,
        )).json()
        assert.equal(tampered.verification.status, 'mismatch', 'tamper not detected')
        assert.equal(tampered.verification.firstBadSeq, 2)
        evidence.tamper = { runId: tamper.runId, firstBadSeq: 2 }

        // --- truncation ---
        const cut = await newTurn(page, '请用一句话说明年假和事假的区别。')
        const lastSeq = Number(
            psql(`SELECT last_seq FROM agent_trace_runs WHERE run_id='${cut.runId}';`),
        )
        psql(`DELETE FROM agent_trace_events WHERE run_id='${cut.runId}' AND seq=${lastSeq};`)
        const truncated = await (await page.request.get(
            `${base}/api/chat/${cut.chatId}/runs/${cut.runId}`,
        )).json()
        assert.equal(truncated.verification.status, 'truncated', 'truncation not detected')
        evidence.truncated = { runId: cut.runId, removedSeq: lastSeq }

        // --- no seal key: a sealed run must not pass as ok ---
        const { verifyRun } = await import('../../web/src/lib/server/trace/digest.ts')
        const runJson = JSON.parse(
            psql(`SELECT row_to_json(r) FROM agent_trace_runs r WHERE run_id='${inc.runId}';`),
        )
        const eventJson = JSON.parse(
            psql(
                `SELECT json_agg(row_to_json(e) ORDER BY seq) FROM agent_trace_events e WHERE run_id='${inc.runId}';`,
            ),
        )
        const mapped = {
            runId: runJson.run_id,
            status: runJson.status,
            lastSeq: Number(runJson.last_seq),
            headDigest: runJson.head_digest,
            sealVersion: runJson.seal_version,
            seal: runJson.seal,
        }
        const mappedEvents = eventJson.map((event) => ({
            runId: event.run_id,
            seq: Number(event.seq),
            eventId: event.event_id,
            spanId: event.span_id,
            parentSpanId: event.parent_span_id,
            type: event.type,
            ts: event.ts,
            payload: event.payload,
            previousDigest: event.previous_digest,
            digest: event.digest,
        }))
        // The server verifies with the deployment key; the API result is the
        // authoritative "sealed and intact" reading.
        assert.equal(snapshot.verification.status, 'ok', 'sealed run should verify through the API')
        // A reader without the key must never report the seal as verified.
        assert.equal(verifyRun(mapped, mappedEvents, null).status, 'unsealed', 'missing key must not read ok')
        evidence.noKey = 'unsealed'

        console.log('PASS integrity fixtures', JSON.stringify(evidence))
    } finally {
        await browser.close()
    }
})().catch((error) => {
    console.error('FAIL', error.message)
    process.exitCode = 1
})
