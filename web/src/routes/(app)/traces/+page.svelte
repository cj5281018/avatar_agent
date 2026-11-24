<script lang="ts">
    import { onMount } from 'svelte'
    import { Sheet } from '$lib/components/ui/sheet'
    import TraceTimeline from '$lib/components/trace-timeline.svelte'

    type RunRow = {
        runId: string
        chatId: string
        chatTitle: string | null
        status: string
        attempt: number
        modelName: string | null
        steps: number
        startedAt: string
        durationMs: number | null
    }

    const STATUS: Record<string, string> = {
        completed: '已完成',
        failed: '失败',
        cancelled: '已取消',
        running: '执行中',
        interrupted: '中断',
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
        每次问答执行一行。展开可以查看本轮实际发生的模型调用与工具步骤，以及记录完整性校验结果。
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
                <li class="flex flex-wrap items-center justify-between gap-2 rounded-md border p-3">
                    <div class="min-w-0 space-y-1">
                        <p class="truncate text-sm font-medium">
                            <a class="underline decoration-dotted" href={`/chat/${run.chatId}`}>
                                {run.chatTitle ?? '未命名会话'}
                            </a>
                        </p>
                        <p class="text-xs text-muted-foreground">
                            {new Date(run.startedAt).toLocaleString()} · {run.steps} 条事件
                            {#if run.durationMs !== null}· {(run.durationMs / 1000).toFixed(1)} 秒{/if}
                            {#if run.attempt > 1}· 第 {run.attempt} 次尝试{/if}
                            · {STATUS[run.status] ?? run.status}
                        </p>
                    </div>
                    <button
                        type="button"
                        class="rounded border px-3 py-1 text-sm"
                        data-testid={`trace-open-${run.runId}`}
                        onclick={() => inspect(run)}>
                        查看执行过程
                    </button>
                </li>
            {/each}
        </ul>
    {/if}

    <Sheet.Root bind:open>
        <Sheet.Content side="right" class="w-full overflow-y-auto sm:max-w-lg">
            <Sheet.Header>
                <Sheet.Title>执行过程</Sheet.Title>
                <Sheet.Description>{selected?.chatTitle ?? '会话'}</Sheet.Description>
            </Sheet.Header>
            <div class="px-4 pb-6">
                {#if selected}
                    <TraceTimeline chatId={selected.chatId} runId={selected.runId} />
                {/if}
            </div>
        </Sheet.Content>
    </Sheet.Root>
</main>
