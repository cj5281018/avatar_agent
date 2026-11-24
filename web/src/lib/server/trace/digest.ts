import { createHash, createHmac, timingSafeEqual } from 'node:crypto'

/**
 * Re-computation of the per-run trace hash chain.
 *
 * The writer lives in the AI service (`services/ai/tracing/canonical.py`).
 * Both sides must produce byte-identical input to SHA-256, so the rules are
 * fixed here and pinned by golden vectors in
 * `services/ai/tests/unit/test_tracing.py` and `digest.test.ts`:
 *
 * - object keys sorted, no extra whitespace;
 * - non-ASCII emitted as UTF-8 rather than escaped;
 * - payload values are strings, integers, booleans, null or nested containers
 *   of those (the writer stringifies floats before they reach the chain).
 */

export const ALGORITHM = 'sha256.canonical-json.v1'
export const SEAL_VERSION = 'hmac-sha256.v1'

export type TraceEventRow = {
    runId: string
    seq: number
    eventId: string
    spanId: string
    parentSpanId: string | null
    type: string
    ts: Date | string
    payload: Record<string, unknown>
    previousDigest: string | null
    digest: string
}

export type TraceRunRow = {
    runId: string
    status: string
    lastSeq: number
    headDigest: string | null
    sealVersion: string | null
    seal: string | null
}

export type Verification = {
    status: 'ok' | 'mismatch' | 'truncated' | 'unsealed' | 'running'
    firstBadSeq?: number
    detail?: string
}

export function canonicalJson(value: unknown): string {
    if (value === null || typeof value !== 'object') return JSON.stringify(value)
    if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`
    const record = value as Record<string, unknown>
    const keys = Object.keys(record).sort()
    return `{${keys.map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`).join(',')}}`
}

export function formatTs(value: Date | string): string {
    const date = value instanceof Date ? value : new Date(value)
    return date.toISOString()
}

export function eventDigest(event: Omit<TraceEventRow, 'digest'>): string {
    const envelope = {
        algorithm: ALGORITHM,
        event: event.payload,
        eventId: event.eventId,
        parentSpanId: event.parentSpanId,
        previousDigest: event.previousDigest,
        runId: event.runId,
        seq: event.seq,
        spanId: event.spanId,
        ts: formatTs(event.ts),
        type: event.type,
    }
    return createHash('sha256').update(canonicalJson(envelope), 'utf8').digest('hex')
}

export function sealDigest(
    key: string,
    runId: string,
    lastSeq: number,
    headDigest: string | null,
): string {
    const body = canonicalJson({
        algorithm: SEAL_VERSION,
        headDigest,
        lastSeq,
        runId,
    })
    return createHmac('sha256', key).update(body, 'utf8').digest('hex')
}

function safeEqual(a: string, b: string): boolean {
    const left = Buffer.from(a, 'utf8')
    const right = Buffer.from(b, 'utf8')
    return left.length === right.length && timingSafeEqual(left, right)
}

/** Rebuild the chain and report the first place it stops matching. */
export function verifyRun(
    run: TraceRunRow,
    events: TraceEventRow[],
    sealKey: string | null,
): Verification {
    const ordered = [...events].sort((a, b) => a.seq - b.seq)
    let previous: string | null = null
    let expectedSeq = 1

    for (const event of ordered) {
        if (event.seq !== expectedSeq) {
            return { status: 'mismatch', firstBadSeq: event.seq, detail: 'event sequence has a gap' }
        }
        if ((event.previousDigest ?? null) !== previous) {
            return {
                status: 'mismatch',
                firstBadSeq: event.seq,
                detail: 'previous digest does not match the preceding event',
            }
        }
        const recomputed = eventDigest(event)
        if (!safeEqual(recomputed, event.digest)) {
            return { status: 'mismatch', firstBadSeq: event.seq, detail: 'event content changed' }
        }
        previous = recomputed
        expectedSeq += 1
    }

    if (run.status === 'running') {
        return { status: 'running', detail: 'this run has not finished yet' }
    }
    if (ordered.length !== run.lastSeq || previous !== (run.headDigest ?? null)) {
        return {
            status: 'truncated',
            detail: 'stored chain head does not match the recorded last sequence',
        }
    }
    if (run.sealVersion && run.seal) {
        if (!sealKey) return { status: 'unsealed', detail: 'seal key is not configured' }
        const expected = sealDigest(sealKey, run.runId, run.lastSeq, run.headDigest)
        if (!safeEqual(expected, run.seal)) {
            return { status: 'mismatch', detail: 'chain head seal does not match' }
        }
        return { status: 'ok' }
    }
    return { status: 'unsealed', detail: 'this run was recorded without a seal' }
}
