<script lang="ts">
    import { buildTraceTree, type SpanNode, type TraceEventInput } from '$lib/utils/trace-tree'

    type RunInfo = {
        runId: string
        status: string
        executionState: 'running' | 'stale' | 'completed' | 'failed' | 'cancelled' | 'interrupted'
        terminal: boolean
        attempt: number
        modelName: string | null
        errorCode: string | null
        captureStatus: 'ok' | 'degraded' | 'unknown'
        captureError: string | null
        droppedEvents: number
        toolError: boolean
        startedAt: string
        endedAt: string | null
        durationMs: number | null
    }
    type Verification = {
        status: 'ok' | 'mismatch' | 'truncated' | 'not_sealed' | 'unverifiable' | 'running'
        firstBadSeq?: number
        detail?: string
    }
    type DraftStatus = {
        spanId: string
        operationId: string
        available: boolean
        reason?: string
        detail?: string
        cardStatus?: string | null
        leaveId?: string | null
        hrStatus?: string | null
    }
    type BusinessState = { drafts: DraftStatus[]; detail?: string }

    let { chatId, runId }: { chatId: string; runId: string } = $props()

    let events = $state<TraceEventInput[]>([])
    let run = $state<RunInfo | null>(null)
    let verification = $state<Verification | null>(null)
    let failure = $state('')
    let loading = $state(true)

    let business = $state<BusinessState | null>(null)
    let businessLoading = $state(false)
    let businessFailure = $state('')
    let businessController: AbortController | null = null

    let generation = 0

    const EXECUTION: Record<RunInfo['executionState'], { label: string; tone: string }> = {
        running: { label: '问答进行中', tone: 'text-muted-foreground' },
        stale: { label: '最近无心跳，仍在等待结束', tone: 'text-amber-700 dark:text-amber-400' },
        completed: { label: '问答已结束', tone: 'text-muted-foreground' },
        failed: { label: '问答失败', tone: 'text-destructive' },
        cancelled: { label: '已取消', tone: 'text-muted-foreground' },
        interrupted: { label: '已中断', tone: 'text-amber-700 dark:text-amber-400' },
    }
    const CAPTURE: Record<RunInfo['captureStatus'], { label: string; tone: string }> = {
        ok: { label: '采集正常', tone: 'text-muted-foreground' },
        degraded: { label: '部分步骤可能缺失', tone: 'text-destructive' },
        unknown: { label: '采集健康未知', tone: 'text-amber-700 dark:text-amber-400' },
    }
    const INTEGRITY: Record<Verification['status'], { label: string; tone: string }> = {
        ok: { label: '已存记录校验通过', tone: 'text-emerald-700 dark:text-emerald-400' },
        mismatch: { label: '记录异常', tone: 'text-destructive' },
        truncated: { label: '记录不完整', tone: 'text-destructive' },
        not_sealed: { label: '记录未封存', tone: 'text-amber-700 dark:text-amber-400' },
        unverifiable: { label: '已封存，当前无法验证', tone: 'text-amber-700 dark:text-amber-400' },
        running: { label: '问答未结束，尚不能校验', tone: 'text-muted-foreground' },
    }
    const TERMINAL = new Set(['completed', 'failed', 'cancelled', 'interrupted'])

    let tree = $derived(buildTraceTree(events))
    let execution = $derived(run ? EXECUTION[run.executionState] : null)
    let capture = $derived(run ? CAPTURE[run.captureStatus] : null)
    let integrity = $derived(verification ? INTEGRITY[verification.status] : null)

    async function fetchBusiness(controller: AbortController) {
        businessLoading = true
        businessFailure = ''
        try {
            const response = await fetch(`/api/chat/${chatId}/runs/${runId}/business`, {
                signal: controller.signal,
            })
            const body = await response.json()
            if (controller.signal.aborted) return
            if (!response.ok) throw new Error(body.message ?? body.detail ?? '办理状态暂不可用')
            business = body as BusinessState
        } catch (cause) {
            if (controller.signal.aborted) return
            businessFailure = cause instanceof Error ? cause.message : '办理状态暂不可用'
        } finally {
            if (!controller.signal.aborted) businessLoading = false
        }
    }

    function refreshBusiness() {
        businessController?.abort()
        const controller = new AbortController()
        businessController = controller
        void fetchBusiness(controller)
    }

    $effect(() => {
        const currentChat = chatId
        const currentRun = runId
        const mine = ++generation
        let cancelled = false
        let timer: ReturnType<typeof setTimeout> | undefined
        let detailController: AbortController | null = null
        let cursor = 0
        let retries = 0

        events = []
        run = null
        verification = null
        failure = ''
        business = null
        businessFailure = ''
        businessLoading = false
        loading = true

        const active = () => !cancelled && mine === generation
        const schedule = (delay: number) => {
            if (timer) clearTimeout(timer)
            timer = setTimeout(poll, delay)
        }

        async function fetchDetail(afterSeq: number, mode: 'snapshot' | 'delta') {
            detailController?.abort()
            const controller = new AbortController()
            detailController = controller
            const suffix = afterSeq > 0 ? `?afterSeq=${afterSeq}` : ''
            const response = await fetch(`/api/chat/${currentChat}/runs/${currentRun}${suffix}`, {
                signal: controller.signal,
            })
            const body = await response.json()
            if (!response.ok) throw new Error(body.message ?? body.detail ?? '执行记录读取失败')
            if (!active() || controller.signal.aborted) return

            if (mode === 'delta') {
                const firstNew = body.events[0]?.seq
                if (firstNew !== undefined && firstNew !== cursor + 1) {
                    // The delta does not continue our snapshot: re-read it whole
                    // rather than render a tree with a hole.
                    const full = await fetch(`/api/chat/${currentChat}/runs/${currentRun}`, {
                        signal: controller.signal,
                    })
                    if (!full.ok) throw new Error('执行记录快照重读失败')
                    const snapshot = await full.json()
                    if (!active() || controller.signal.aborted) return
                    events = snapshot.events
                    run = snapshot.run
                    verification = snapshot.verification
                    cursor = snapshot.cursor
                } else {
                    const merged = [...events]
                    const known = new Set(merged.map((event) => event.seq))
                    for (const event of body.events) {
                        if (!known.has(event.seq)) merged.push(event)
                    }
                    merged.sort((a, b) => a.seq - b.seq)
                    events = merged
                    run = body.run
                    verification = body.verification
                    cursor = body.cursor
                }
            } else {
                events = body.events
                run = body.run
                verification = body.verification
                cursor = body.cursor
            }
            retries = 0
            failure = ''
            if (!run || TERMINAL.has(run.executionState)) return
            // A run with no recent heartbeat may still finish; keep reading at
            // a lower rate instead of freezing on "possibly interrupted".
            schedule(run.executionState === 'stale' ? 6000 : 2000)
        }

        async function poll() {
            if (!active()) return
            try {
                await fetchDetail(cursor, 'delta')
            } catch (cause) {
                if (!active()) return
                if (cause instanceof DOMException && cause.name === 'AbortError') return
                failure = cause instanceof Error ? cause.message : '执行记录读取失败'
                retries += 1
                schedule(Math.min(2000 * 2 ** Math.min(retries, 2), 8000))
            }
        }

        ;(async () => {
            try {
                await fetchDetail(0, 'snapshot')
            } catch (cause) {
                if (!active()) return
                if (cause instanceof DOMException && cause.name === 'AbortError') return
                failure = cause instanceof Error ? cause.message : '执行记录读取失败'
                // First snapshot failed: keep retrying while the panel stays
                // open, with a capped backoff, so recovery needs no refresh.
                schedule(2000)
            } finally {
                if (active()) loading = false
            }
        })()

        return () => {
            cancelled = true
            if (timer) clearTimeout(timer)
            detailController?.abort()
        }
    })

    $effect(() => {
        // Only the run identity is a dependency. State is written from the
        // async callbacks, so this effect cannot invalidate itself and abort
        // the request it just started.
        const controller = new AbortController()
        businessController?.abort()
        businessController = controller
        business = null
        void fetchBusiness(controller)
        return () => businessController?.abort()
    })

    function seconds(ms: number | null): string {
        return ms === null ? '—' : `${(ms / 1000).toFixed(1)} 秒`
    }
</script>

{#snippet step(node: SpanNode)}
    <details
        class="rounded-md border p-2"
        open={node.status !== 'ok'}
        data-testid={`trace-step-${node.kind}`}>
        <summary class="flex cursor-pointer flex-wrap items-center justify-between gap-2 text-sm">
            <span class="font-medium">
                {node.kind === 'model' ? '◆' : node.kind === 'retrieval' ? '⌕' : '▣'}
                {node.title}
                {#if node.sub && node.kind !== 'model'}
                    <span class="font-normal text-muted-foreground">{node.sub}</span>
                {/if}
                {#if node.orphan}
                    <span class="ml-1 text-xs text-amber-700 dark:text-amber-400">（发起它的步骤未记录）</span>
                {/if}
            </span>
            <span class="text-xs text-muted-foreground">
                {#if node.status === 'running'}进行中{:else if node.status === 'error'}失败{:else}完成{/if}
                {#if node.durationMs !== null}· {node.durationMs} ms{/if}
            </span>
        </summary>
        {#if node.details.length > 0}
            <dl class="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
                {#each node.details as item (item.label)}
                    <dt class="text-muted-foreground">{item.label}</dt>
                    <dd class="break-all">{item.value}</dd>
                {/each}
            </dl>
        {/if}
        {#if node.startedAt}
            <p class="mt-2 text-xs text-muted-foreground">
                起 {new Date(node.startedAt).toLocaleTimeString()}
                {#if node.endedAt}· 止 {new Date(node.endedAt).toLocaleTimeString()}{/if}
            </p>
        {/if}
        {#if node.children.length > 0}
            <div class="mt-2 space-y-2 border-l pl-3">
                {#each node.children as child (child.spanId)}
                    {@render step(child)}
                {/each}
            </div>
        {/if}
    </details>
{/snippet}

<div class="space-y-3 text-sm">
    {#if loading && !run}
        <p class="text-muted-foreground">正在读取执行记录…</p>
    {:else if run}
        <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs" data-testid="trace-status">
            <span class={execution?.tone} data-testid="trace-execution">{execution?.label}</span>
            <span class={capture?.tone} data-testid="trace-capture">· {capture?.label}</span>
            <span class={integrity?.tone} data-testid="trace-verification">
                · {integrity?.label}
                {#if verification?.firstBadSeq}（第 {verification.firstBadSeq} 条）{/if}
            </span>
        </div>
        {#if failure}
            <p class="text-xs text-amber-700 dark:text-amber-400" role="status" data-testid="trace-recovering">
                {failure}，正在重试…
            </p>
        {/if}
        <p class="text-xs text-muted-foreground">
            {run.modelName ?? '模型'} · {events.length} 条事件 · 整轮 {seconds(run.durationMs)}
            {#if run.attempt > 1}· 第 {run.attempt} 次尝试{/if}
        </p>
        <p class="text-xs text-muted-foreground">
            整轮耗时含未单独打点的阶段；模型流耗时指模型开始返回到结束；首个正文等待从模型开始返回起算；并行或父子步骤可能重叠。
        </p>

        {#if run.captureStatus === 'degraded'}
            <p class="rounded-md border border-destructive/40 p-2 text-xs" data-testid="trace-capture-note">
                采集过程中有 {run.droppedEvents} 次失败未能写入（计数为已观察到的失败次数，不等于漏记条数），可能缺少部分步骤
                {#if run.captureError}（{run.captureError}）{/if}。现有步骤仍可查看，校验只覆盖已存记录。
            </p>
        {:else if run.captureStatus === 'unknown'}
            <p class="rounded-md border p-2 text-xs text-muted-foreground">
                这条记录产生于采集健康统计上线之前，无法判断当时是否漏记。
            </p>
        {/if}

        {#if verification?.detail}
            <p class="text-xs text-muted-foreground" data-testid="trace-verification-detail">
                {verification.detail}
            </p>
        {/if}

        {#if tree.length === 0}
            <p class="text-muted-foreground">本轮没有记录到模型或工具步骤。</p>
        {:else}
            <ol class="space-y-2" data-testid="trace-steps">
                {#each tree as node (node.spanId)}
                    <li>{@render step(node)}</li>
                {/each}
            </ol>
        {/if}

        <section class="rounded-md border p-3" data-testid="trace-business" aria-label="关联业务">
            <div class="flex items-center justify-between gap-2">
                <h3 class="text-sm font-medium">关联业务</h3>
                <button
                    type="button"
                    class="rounded border px-2 py-0.5 text-xs"
                    disabled={businessLoading}
                    onclick={refreshBusiness}>
                    {businessLoading ? '读取中…' : '刷新办理状态'}
                </button>
            </div>
            {#if businessFailure}
                <p class="mt-2 text-xs text-amber-700 dark:text-amber-400" role="status">{businessFailure}</p>
            {:else if business}
                {#if business.drafts.length === 0}
                    <p class="mt-2 text-xs text-muted-foreground">
                        {business.detail ?? '这一轮没有可关联的业务。'}
                    </p>
                {:else}
                    {#if business.detail}
                        <p class="mt-2 text-xs text-muted-foreground">{business.detail}</p>
                    {/if}
                    <div class="mt-2 space-y-2">
                        {#each business.drafts as draft (draft.spanId)}
                            <div class="rounded border p-2 text-xs" data-testid="trace-business-draft">
                                <p class="text-muted-foreground">
                                    起草步骤 {draft.spanId.slice(0, 8)} · 操作
                                    <span class="break-all">{draft.operationId}</span>
                                </p>
                                {#if draft.available}
                                    <p>请假单号：{draft.leaveId ?? '尚未提交（草稿）'}</p>
                                    <p>
                                        卡片状态：{draft.cardStatus ?? '—'} · 审批状态：{draft.hrStatus ??
                                            (draft.leaveId ? '—' : '员工确认后产生单号')}
                                    </p>
                                {:else}
                                    <p class="text-muted-foreground">{draft.detail ?? '办理状态暂不可用。'}</p>
                                {/if}
                            </div>
                        {/each}
                    </div>
                {/if}
            {:else if businessLoading}
                <p class="mt-2 text-xs text-muted-foreground">正在读取办理状态…</p>
            {/if}
            <p class="mt-2 text-xs text-muted-foreground">
                以下为问答结束后的办理进度，按当前权限实时读取，不会改写已封存的执行记录，也不会触发提交或审批。
            </p>
        </section>

        <p class="text-xs text-muted-foreground">
            runId {run.runId}
            {#if run.errorCode}· 结束原因 {run.errorCode}{/if}
            {#if run.toolError}· 本轮有工具失败{/if}
        </p>
    {:else}
        <p class="text-destructive" role="alert" data-testid="trace-load-failure">
            {failure || '执行记录读取失败'}
        </p>
    {/if}
</div>
