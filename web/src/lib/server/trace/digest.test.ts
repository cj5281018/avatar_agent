import { describe, expect, it } from 'vitest'
import { canonicalJson, eventDigest, sealDigest } from './digest'

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
