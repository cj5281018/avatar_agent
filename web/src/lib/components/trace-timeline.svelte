<script lang="ts">
    type TraceEvent = {
        seq: number
        spanId: string
        parentSpanId: string | null
        type: string
        ts: string
        payload: Record<string, unknown>
    }
    type Verification = {
        status: 'ok' | 'mismatch' | 'truncated' | 'unsealed' | 'running'
        firstBadSeq?: number
        detail?: string
    }
    type Detail = {
        run: {
            runId: string
            status: string
            attempt: number
            modelName: string | null
            startedAt: string
            endedAt: string | null
            lastSeq: number
            sealed: boolean
        }
        events: TraceEvent[]
        verification: Verification
    }
    type Node = {
        spanId: string
        parentSpanId: string | null
        kind: 'model' | 'retrieval' | 'tool'
        title: string
        sub: string
        status: 'ok' | 'error' | 'running'
        durationMs: number | null
        chips: string[]
    }

    let { chatId, runId }: { chatId: string; runId: string } = $props()

    let detail = $state<Detail | null>(null)
    let failure = $state('')
    let loading = $state(true)

    const TOOL_LABELS: Record<string, string> = {
        search: '知识检索',
        hr_preview_leave: 'HR · 起草请假',
        hr_list_my_requests: 'HR · 查询本人申请',
    }
    const VERIFICATION: Record<Verification['status'], { label: string; tone: string }> = {
        ok: { label: '校验通过', tone: 'text-emerald-700 dark:text-emerald-400' },
        mismatch: { label: '发现异常', tone: 'text-destructive' },
        truncated: { label: '记录不完整', tone: 'text-destructive' },
        unsealed: { label: '未封存', tone: 'text-amber-700 dark:text-amber-400' },
        running: { label: '执行中', tone: 'text-muted-foreground' },
    }

    $effect(() => {
        let cancelled = false
        loading = true
        failure = ''
        fetch(`/api/chat/${chatId}/runs/${runId}`, { headers: { accept: 'application/json' } })
            .then(async (response) => {
                const body = await response.json()
                if (!response.ok) throw new Error(body.message ?? body.detail ?? '执行记录读取失败')
                if (!cancelled) detail = body
            })
            .catch((cause) => {
                if (!cancelled) failure = cause instanceof Error ? cause.message : '执行记录读取失败'
            })
            .finally(() => {
                if (!cancelled) loading = false
            })
        return () => {
            cancelled = true
        }
    })

    $effect(() => {
        // A run that is still executing keeps growing; poll until it finishes.
        if (!detail || detail.run.status !== 'running') return
        const timer = setTimeout(async () => {
            try {
                const response = await fetch(`/api/chat/${chatId}/runs/${runId}`)
                if (response.ok) detail = await response.json()
            } catch {
                // Keep the last successful snapshot.
            }
        }, 2000)
        return () => clearTimeout(timer)
    })

    function number(value: unknown): number | null {
        return typeof value === 'number' && Number.isFinite(value) ? value : null
    }

    function text(value: unknown): string {
        return typeof value === 'string' ? value : ''
    }

    let nodes = $derived.by<Node[]>(() => {
        if (!detail) return []
        const spans = new Map<
            string,
            {
                first: number
                started?: TraceEvent
                finished?: TraceEvent
                type: string
                kind: 'model' | 'retrieval' | 'tool'
            }
        >()
        for (const event of detail.events) {
            if (!event.type.startsWith('model.') && !event.type.startsWith('tool.')) continue
            const kind = event.type.startsWith('model.')
                ? 'model'
                : event.payload.kind === 'retrieval'
                  ? 'retrieval'
                  : 'tool'
            const entry =
                spans.get(event.spanId) ??
                ({ first: event.seq, type: event.type, kind } as {
                    first: number
                    started?: TraceEvent
                    finished?: TraceEvent
                    type: string
                    kind: 'model' | 'retrieval' | 'tool'
                })
            if (event.type.endsWith('.started')) entry.started = event
            else entry.finished = event
            spans.set(event.spanId, entry)
        }

        return [...spans.entries()]
            .sort((a, b) => a[1].first - b[1].first)
            .map(([spanId, entry]) => {
                const payload = { ...(entry.started?.payload ?? {}), ...(entry.finished?.payload ?? {}) }
                const tool = text(payload.tool)
                const title =
                    entry.kind === 'model'
                        ? '模型调用'
                        : entry.kind === 'retrieval'
                          ? (TOOL_LABELS[tool] ?? '知识检索')
                          : (TOOL_LABELS[tool] ?? tool ?? '工具调用')
                const chips: string[] = []
                const hits = number(payload.hits)
                if (hits !== null) chips.push(`${hits} 条结果`)
                const operationId = text(payload.operationId)
                if (operationId) chips.push(`操作 ${operationId}`)
                const leaveId = text(payload.leaveId)
                if (leaveId) chips.push(`单号 ${leaveId}`)
                const error = text(payload.error) || text(payload.message)
                if (error) chips.push(error)
                return {
                    spanId,
                    parentSpanId: entry.started?.parentSpanId ?? entry.finished?.parentSpanId ?? null,
                    kind: entry.kind,
                    title,
                    sub: entry.kind === 'model' ? text(payload.model) : '',
                    status: !entry.finished
                        ? ('running' as const)
                        : entry.finished.type.endsWith('failed')
                          ? ('error' as const)
                          : ('ok' as const),
                    durationMs: number(entry.finished?.payload.durationMs ?? null),
                    chips,
                }
            })
    })

    let totalMs = $derived.by(() => {
        if (!detail?.run.endedAt) return null
        return new Date(detail.run.endedAt).getTime() - new Date(detail.run.startedAt).getTime()
    })
    let verification = $derived(detail ? VERIFICATION[detail.verification.status] : null)
</script>

<div class="space-y-3 text-sm">
    {#if loading}
        <p class="text-muted-foreground">正在读取执行记录…</p>
    {:else if failure}
        <p class="text-destructive" role="alert">{failure}</p>
    {:else if detail}
        <div class="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <span>{detail.run.modelName ?? '模型'}</span>
            {#if totalMs !== null}<span>· 耗时 {(totalMs / 1000).toFixed(1)} 秒</span>{/if}
            {#if detail.run.attempt > 1}<span>· 第 {detail.run.attempt} 次尝试</span>{/if}
            <span>· {detail.events.length} 条事件</span>
            {#if verification}
                <span class={verification.tone} data-testid="trace-verification">
                    · {verification.label}
                    {#if detail.verification.firstBadSeq}
                        （第 {detail.verification.firstBadSeq} 条）
                    {/if}
                </span>
            {/if}
        </div>

        {#if nodes.length === 0}
            <p class="text-muted-foreground">本轮没有记录到模型或工具步骤。</p>
        {/if}

        <ol class="space-y-2" data-testid="trace-steps">
            {#each nodes as node (node.spanId)}
                <li
                    class="rounded-md border p-2"
                    class:ml-6={nodes.some((candidate) => candidate.spanId === node.parentSpanId)}
                    data-testid={`trace-step-${node.kind}`}>
                    <div class="flex items-center justify-between gap-2">
                        <span class="font-medium">
                            {node.kind === 'model' ? '◆' : node.kind === 'retrieval' ? '⌕' : '▣'}
                            {node.title}
                            {#if node.sub}<span class="font-normal text-muted-foreground">{node.sub}</span>{/if}
                        </span>
                        <span class="text-xs text-muted-foreground">
                            {#if node.status === 'running'}
                                进行中
                            {:else if node.status === 'error'}
                                失败
                            {:else}
                                完成
                            {/if}
                            {#if node.durationMs !== null}· {node.durationMs} ms{/if}
                        </span>
                    </div>
                    {#if node.chips.length > 0}
                        <div class="mt-1 flex flex-wrap gap-1">
                            {#each node.chips as chip}
                                <span class="rounded bg-muted px-1.5 py-0.5 text-xs">{chip}</span>
                            {/each}
                        </div>
                    {/if}
                </li>
            {/each}
        </ol>

        {#if detail.verification.detail}
            <p class="text-xs text-muted-foreground" data-testid="trace-verification-detail">
                {detail.verification.detail}
            </p>
        {/if}
    {/if}
</div>
