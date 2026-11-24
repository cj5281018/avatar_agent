/**
 * Builds the step tree shown in the trace panel.
 *
 * The ledger guarantees order (`seq`) and the writer records who initiated a
 * step (`parentSpanId`). Ordering alone cannot express "this tool belongs to
 * that model call" - parallel tools start and finish interleaved - so nesting
 * always follows the explicit parent link. A parent that is missing from the
 * data leaves the node visible at the top level and marked as unlinked instead
 * of inventing a relationship.
 */

export type TraceEventInput = {
    seq: number
    spanId: string
    parentSpanId: string | null
    type: string
    ts: string
    payload: Record<string, unknown>
}

export type SpanKind = 'model' | 'retrieval' | 'tool'
export type SpanStatus = 'ok' | 'error' | 'running'

export type SpanNode = {
    spanId: string
    parentSpanId: string | null
    kind: SpanKind
    title: string
    subtitle: string
    status: SpanStatus
    durationMs: number | null
    startedAt: string | null
    endedAt: string | null
    firstSeq: number
    /** Parent was declared but is not present in the stored events. */
    orphan: boolean
    details: { label: string; value: string }[]
    children: SpanNode[]
}

const TOOL_LABELS: Record<string, string> = {
    search: '知识检索',
    hr_preview_leave: 'HR · 起草请假',
    hr_list_my_requests: 'HR · 查询本人申请',
}

function text(value: unknown): string {
    return typeof value === 'string' ? value : ''
}

function number(value: unknown): number | null {
    return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function detail(label: string, value: unknown, suffix = ''): { label: string; value: string } | null {
    if (typeof value === 'number' && Number.isFinite(value)) {
        return { label, value: `${value}${suffix}` }
    }
    if (typeof value === 'string' && value) return { label, value }
    if (typeof value === 'boolean') return { label, value: value ? '是' : '否' }
    return null
}

type Group = {
    firstSeq: number
    kind: SpanKind
    started?: TraceEventInput
    finished?: TraceEventInput
}

export function buildTraceTree(events: TraceEventInput[]): SpanNode[] {
    const groups = new Map<string, Group>()
    for (const event of events) {
        if (!event.type.startsWith('model.') && !event.type.startsWith('tool.')) continue
        const kind: SpanKind = event.type.startsWith('model.')
            ? 'model'
            : event.payload.kind === 'retrieval'
              ? 'retrieval'
              : 'tool'
        const group =
            groups.get(event.spanId) ?? ({ firstSeq: event.seq, kind } satisfies Group)
        if (event.type.endsWith('.started')) group.started = event
        else group.finished = event
        groups.set(event.spanId, group)
    }

    const nodes = new Map<string, SpanNode>()
    for (const [spanId, group] of groups) {
        const payload = { ...(group.started?.payload ?? {}), ...(group.finished?.payload ?? {}) }
        const tool = text(payload.tool)
        const callIndex = number(payload.callIndex)
        const title =
            group.kind === 'model'
                ? `模型调用${callIndex ? ` ${callIndex}` : ''}`
                : (TOOL_LABELS[tool] ?? (group.kind === 'retrieval' ? '知识检索' : tool || '工具调用'))
        const finishedType = group.finished?.type ?? ''
        const status: SpanStatus = !group.finished
            ? 'running'
            : finishedType.endsWith('failed')
              ? 'error'
              : 'ok'

        const details = [
            detail('模型', payload.model),
            detail('工具名', tool),
            detail('本轮第几次模型调用', callIndex),
            detail('耗时', number(payload.durationMs), ' ms'),
            detail('首个可见文本', number(payload.firstTextMs), ' ms'),
            detail('命中结果', number(payload.hits), ' 条'),
            detail('查询摘要', payload.queryPreview),
            detail('命中标题', Array.isArray(payload.titles) ? payload.titles.join('；') : null),
            detail('操作编号', payload.operationId),
            detail('请假单号', payload.leaveId),
            detail('失败原因', payload.reason),
            detail('错误类别', payload.error),
        ].filter((item): item is { label: string; value: string } => item !== null)

        nodes.set(spanId, {
            spanId,
            parentSpanId: group.started?.parentSpanId ?? group.finished?.parentSpanId ?? null,
            kind: group.kind,
            title,
            subtitle: text(payload.model),
            status,
            durationMs: number(payload.durationMs),
            startedAt: group.started?.ts ?? null,
            endedAt: group.finished?.ts ?? null,
            firstSeq: group.firstSeq,
            orphan: false,
            details,
            children: [],
        })
    }

    const roots: SpanNode[] = []
    for (const node of nodes.values()) {
        if (node.parentSpanId && nodes.has(node.parentSpanId)) {
            nodes.get(node.parentSpanId)!.children.push(node)
        } else {
            node.orphan = Boolean(node.parentSpanId)
            roots.push(node)
        }
    }
    const bySeq = (a: SpanNode, b: SpanNode) => a.firstSeq - b.firstSeq
    roots.sort(bySeq)
    for (const node of nodes.values()) node.children.sort(bySeq)
    return roots
}

export function summarize(events: TraceEventInput[]) {
    return {
        modelCalls: events.filter((event) => event.type === 'model.started').length,
        toolCalls: events.filter((event) => event.type === 'tool.started').length,
        toolErrors: events.filter((event) => event.type === 'tool.failed').length,
    }
}
