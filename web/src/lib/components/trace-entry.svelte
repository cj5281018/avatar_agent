<script lang="ts">
    import { Sheet } from '$lib/components/ui/sheet'
    import TraceTimeline from './trace-timeline.svelte'

    type RunSummary = {
        runId: string
        status: string
        steps: number
        durationMs: number | null
    }

    let { chatId, run }: { chatId: string; run: RunSummary | undefined } = $props()
    let open = $state(false)
</script>

{#if run}
    <button
        type="button"
        class="mt-1 self-start text-xs text-muted-foreground underline decoration-dotted hover:text-foreground"
        data-testid="trace-entry"
        onclick={() => (open = true)}>
        查看执行过程 · {run.steps} 步
        {#if run.durationMs !== null}· {(run.durationMs / 1000).toFixed(1)} 秒{/if}
    </button>
    <Sheet.Root bind:open>
        <Sheet.Content side="right" class="w-full overflow-y-auto sm:max-w-lg">
            <Sheet.Header>
                <Sheet.Title>执行过程</Sheet.Title>
                <Sheet.Description>
                    本轮问答实际发生的模型调用与工具步骤。记录由服务端写入并按轮次校验。
                </Sheet.Description>
            </Sheet.Header>
            <div class="px-4 pb-6">
                <TraceTimeline {chatId} runId={run.runId} />
            </div>
        </Sheet.Content>
    </Sheet.Root>
{/if}
