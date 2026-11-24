import { describe, expect, it } from 'vitest'
import { canonicalJson, eventDigest, sealDigest, verifyRun, type TraceEventRow } from './digest'

/**
 * These are the same golden vectors the writer pins in
 * `services/ai/tests/unit/test_tracing.py`. If either side changes its
 * canonical encoding, one of the two suites fails.
 */
describe('trace digest', () => {
    it('matches the golden event digest produced by the writer', () => {
        expect(
            eventDigest({
                runId: 'run-golden',
                seq: 1,
                eventId: 'evt-golden',
                spanId: 'span-golden',
                parentSpanId: null,
                type: 'model.started',
                ts: '2026-11-24T09:00:00.000Z',
                payload: { model: 'deepseek-flash', n: 2, ok: true, note: '年假' },
                previousDigest: null,
            }),
        ).toBe('8b11e8634c1bcc5ef3f8c8d2e91e89dbb3f23d3910ad26060fffb241be8d814a')
    })

    it('sorts keys and keeps non-ASCII unescaped', () => {
        expect(canonicalJson({ b: 1, a: '年假', c: [true, null] })).toBe(
            '{"a":"年假","b":1,"c":[true,null]}',
        )
    })

    it('matches the golden seal', () => {
        expect(
            sealDigest(
                'k1',
                'run-golden',
                1,
                '8b11e8634c1bcc5ef3f8c8d2e91e89dbb3f23d3910ad26060fffb241be8d814a',
            ),
        ).toBe('5d90cd0816d8e348113f0935af70002cc1f7ef62d2c59ed7ced8ed617383cacd')
    })
})


describe('seal states', () => {
    const event: TraceEventRow = {
        runId: 'r1',
        seq: 1,
        eventId: 'e1',
        spanId: 's1',
        parentSpanId: null,
        type: 'run.started',
        ts: '2026-11-24T09:00:00.000Z',
        payload: {},
        previousDigest: null,
        digest: '',
    }
    const chain = { ...event, digest: eventDigest(event) }
    const baseRun = {
        runId: 'r1',
        status: 'completed',
        lastSeq: 1,
        headDigest: chain.digest,
        sealVersion: null as string | null,
        seal: null as string | null,
    }

    it('reports a run written without a seal as not_sealed', () => {
        expect(verifyRun(baseRun, [chain], 'k1').status).toBe('not_sealed')
    })

    it('separates unverifiable (sealed, no key) from verified', () => {
        const sealed = {
            ...baseRun,
            sealVersion: 'hmac-sha256.v1',
            seal: sealDigest('k1', 'r1', 1, chain.digest),
        }
        expect(verifyRun(sealed, [chain], null).status).toBe('unverifiable')
        expect(verifyRun(sealed, [chain], 'k1').status).toBe('ok')
        expect(verifyRun(sealed, [chain], 'wrong').status).toBe('mismatch')
    })

    it('keeps running records unverifiable until they finish', () => {
        expect(verifyRun({ ...baseRun, status: 'running' }, [chain], 'k1').status).toBe('running')
    })
})
