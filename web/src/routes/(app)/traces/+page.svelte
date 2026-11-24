<script lang="ts">
    import { onMount } from 'svelte'
    import * as Sheet from '$lib/components/ui/sheet'
    import TraceTimeline from '$lib/components/trace-timeline.svelte'

    type RunRow = {
        runId: string
        chatId: string
        chatTitle: string | null
        userMessageId: string | null
        messageText: string | null
        status: string
        executionState: string
        terminal: boolean
        attempt: number
        modelName: string | null
        steps: number
        startedAt: string
        durationMs: number | null
        captureStatus: 'ok' | 'degraded' | 'unknown'
        droppedEvents: number
        toolError: boolean
    }

    const STATUS: Record<string, string> = {
        completed: '已结束',
        failed: '问答失败',
        cancelled: '已取消',
        running: '进行中',
        stale: '可能已中断',
        interrupted: '已中断',
    }

    let rows = $state<RunRow[]>([])
    let loading = $state(true)
    let failure = $state('')
    let selected = $state<RunRow | null>(null)
    let open = $state(false)

    async function refresh() {
        loading = true
        failure = ''
        try {
            const response = await fetch('/api/traces')
            if (!response.ok) throw new Error('执行记录读取失败')
            const body = (await response.json()) as { runs: RunRow[] }
            rows = body.runs
        } catch (cause) {
            failure = cause instanceof Error ? cause.message : '执行记录读取失败'
        } finally {
            loading = false
        }
    }

    function inspect(run: RunRow) {
        selected = run
        open = true
    }

    function question(run: RunRow): string {
        const text = run.messageText?.trim()
        if (text) return text
        return run.userMessageId ? '（未保存提问文本）' : '（旧记录，未关联提问）'
    }

    function chatLink(run: RunRow): string {
        return run.userMessageId
            ? `/chat/${run.chatId}#question-${run.userMessageId}`
            : `/chat/${run.chatId}`
    }

    onMount(() => {
        void refresh()
    })
</script>

<main class="mx-auto w-full max-w-3xl space-y-4 overflow-auto p-6">
    <div class="flex items-center justify-between">
        <h1 class="text-xl font-semibold">追踪中心</h1>
        <button class="rounded border px-3 py-1 text-sm" onclick={refresh} disabled={loading}>
            刷新
        </button>
    </div>
    <p class="text-sm text-muted-foreground">
        每次问答执行一行，标题是该轮的实际提问。展开可以查看模型调用与工具步骤、采集健康与记录完整性。
    </p>

    {#if loading}
        <p class="text-muted-foreground">正在读取执行记录…</p>
    {:else if failure}
        <p class="text-destructive" role="alert">{failure}</p>
    {:else if rows.length === 0}
        <p class="text-muted-foreground">还没有执行记录。发起一次对话后回来查看。</p>
    {:else}
        <ul class="space-y-2" data-testid="trace-center-list">
            {#each rows as run (run.runId)}
                <li class="flex flex-wrap items-start justify-between gap-2 rounded-md border p-3">
                    <div class="min-w-0 flex-1 space-y-1">
                        <p class="truncate text-sm font-medium" title={question(run)} data-testid="trace-question">
                            {question(run)}
                        </p>
                        <p class="text-xs text-muted-foreground">
                            <a class="underline decoration-dotted" href={chatLink(run)}>
                                {run.chatTitle ?? '未命名会话'}
                            </a>
                            · {new Date(run.startedAt).toLocaleString()}
                            · {run.steps} 条事件
                            {#if run.durationMs !== null}· {(run.durationMs / 1000).toFixed(1)} 秒{/if}
                            {#if run.attempt > 1}· 第 {run.attempt} 次尝试{/if}
                            · {STATUS[run.executionState] ?? run.status}
                        </p>
                        <div class="flex flex-wrap gap-1 text-xs">
                            {#if run.captureStatus === 'degraded'}
                                <span class="rounded border border-destructive/40 px-1.5 py-0.5 text-destructive">
                                    部分步骤可能缺失（{run.droppedEvents} 次）
                                </span>
                            {:else if run.captureStatus === 'unknown'}
                                <span class="rounded border px-1.5 py-0.5 text-muted-foreground">采集健康未知</span>
                            {/if}
                            {#if run.toolError}
                                <span class="rounded border border-amber-500/40 px-1.5 py-0.5 text-amber-700 dark:text-amber-400">
                                    轮次已结束，其中有工具失败
                                </span>
                            {/if}
                        </div>
                    </div>
                    <div class="flex shrink-0 gap-2">
                        <a class="rounded border px-3 py-1 text-sm" href={chatLink(run)}>回到提问</a>
                        <button
                            type="button"
                            class="rounded border px-3 py-1 text-sm"
                            data-testid={`trace-open-${run.runId}`}
                            onclick={() => inspect(run)}>
                            查看执行过程
                        </button>
                    </div>
                </li>
            {/each}
        </ul>
    {/if}

    <Sheet.Root bind:open>
        <Sheet.Content side="right" class="w-full overflow-y-auto sm:max-w-lg">
            <Sheet.Header>
                <Sheet.Title>执行过程</Sheet.Title>
                <Sheet.Description>{selected?.messageText ?? selected?.chatTitle ?? '会话'}</Sheet.Description>
            </Sheet.Header>
            <div class="px-4 pb-6">
                {#if selected}
                    <TraceTimeline chatId={selected.chatId} runId={selected.runId} />
                {/if}
            </div>
        </Sheet.Content>
    </Sheet.Root>
</main>
