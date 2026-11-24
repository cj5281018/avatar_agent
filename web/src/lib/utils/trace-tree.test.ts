import { describe, expect, it } from 'vitest'
import { buildTraceTree, summarize, type TraceEventInput } from './trace-tree'

function event(
    seq: number,
    type: string,
    spanId: string,
    parentSpanId: string | null,
    payload: Record<string, unknown> = {},
): TraceEventInput {
    return { seq, spanId, parentSpanId, type, ts: new Date(Date.UTC(2026, 10, 24, 9, 0, 0, seq)).toISOString(), payload }
}

describe('buildTraceTree', () => {
    it('nests tool steps under the model call that requested them', () => {
        const tree = buildTraceTree([
            event(1, 'run.started', 'run', null),
            event(2, 'model.started', 'm1', null, { model: 'deepseek-flash', callIndex: 1 }),
            event(3, 'model.completed', 'm1', null, { durationMs: 230, callIndex: 1 }),
            event(4, 'tool.started', 't1', 'm1', { tool: 'search', kind: 'retrieval' }),
            event(5, 'tool.completed', 't1', 'm1', { tool: 'search', durationMs: 40, hits: 11 }),
            event(6, 'run.finished', 'run', null),
        ])
        expect(tree).toHaveLength(1)
        expect(tree[0].kind).toBe('model')
        expect(tree[0].children).toHaveLength(1)
        expect(tree[0].children[0].title).toBe('知识检索')
        expect(tree[0].children[0].details).toContainEqual({ label: '命中结果', value: '11 条' })
    })

    it('keeps parallel tools as siblings ordered by start sequence', () => {
        const tree = buildTraceTree([
            event(1, 'model.started', 'm1', null, { callIndex: 1 }),
            event(2, 'model.completed', 'm1', null, {}),
            event(3, 'tool.started', 't1', 'm1', { tool: 'search', kind: 'retrieval' }),
            event(4, 'tool.started', 't2', 'm1', { tool: 'hr_list_my_requests' }),
            event(5, 'tool.completed', 't2', 'm1', { tool: 'hr_list_my_requests', durationMs: 25 }),
            event(6, 'tool.failed', 't1', 'm1', { tool: 'search', durationMs: 900, error: 'Timeout' }),
        ])
        expect(tree[0].children.map((node) => node.spanId)).toEqual(['t1', 't2'])
        expect(tree[0].children[0].status).toBe('error')
        expect(tree[0].children[1].status).toBe('ok')
    })

    it('keeps a step visible when its parent is missing, marked as unlinked', () => {
        const tree = buildTraceTree([
            event(1, 'tool.started', 't9', 'ghost', { tool: 'search' }),
            event(2, 'tool.completed', 't9', 'ghost', { tool: 'search', durationMs: 10 }),
        ])
        expect(tree).toHaveLength(1)
        expect(tree[0].orphan).toBe(true)
        expect(tree[0].status).toBe('ok')
        expect(tree[0].durationMs).toBe(10)
    })

    it('marks an unfinished span as running and ignores run-level events', () => {
        const tree = buildTraceTree([
            event(1, 'run.started', 'run', null),
            event(2, 'model.started', 'm1', null, { callIndex: 1 }),
        ])
        expect(tree).toHaveLength(1)
        expect(tree[0].status).toBe('running')
        expect(summarize([
            event(1, 'model.started', 'm1', null),
            event(2, 'tool.failed', 't1', 'm1', { tool: 'search' }),
        ])).toEqual({ modelCalls: 1, toolCalls: 0, toolErrors: 1 })
    })
})
