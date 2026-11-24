<script lang="ts">
    import { onMount } from 'svelte'
    let { chatId, operationId }: { chatId: string; operationId: string } = $props()
    let item = $state<{ status: string; leaveId?: string; hrStatus?: string; input: Record<string, string> }>()
    let busy = $state(false)
    let message = $state('')

    async function update(action: 'status' | 'confirm') {
        busy = true
        message = ''
        try {
            const response = await fetch('/api/avatar2', {
                method: 'POST',
                headers: { 'content-type': 'application/json' },
                body: JSON.stringify({ action, chatId, operationId }),
            })
            const data = await response.json()
            if (!response.ok) throw new Error(data.detail ?? data.message ?? '操作失败')
            item = data
        } catch (error) {
            message = error instanceof Error ? error.message : '部门服务暂时不可用'
        } finally {
            busy = false
        }
    }

    onMount(() => { void update('status') })
</script>

<section class="my-3 rounded-lg border p-4" aria-label="请假确认卡">
    <h3 class="font-semibold">请假申请</h3>
    {#if item}
        <p>{item.input.startDate} 至 {item.input.endDate} · {item.input.leaveType}</p>
        <p>{item.input.reason}</p>
        {#if item.leaveId}
            <p>HR 单号 {item.leaveId}{item.hrStatus ? " · " + item.hrStatus : ""}</p>
            <button class="mt-2 rounded border px-3 py-1" onclick={() => update('status')} disabled={busy}>回查同一申请</button>
        {:else if item.status === 'pending' || item.status === 'confirmed'}
            <button class="mt-2 rounded border px-3 py-1" onclick={() => update('confirm')} disabled={busy}>确认提交</button>
        {:else}
            <p>{item.status}</p>
        {/if}
    {/if}
    <p aria-live="polite">{busy ? '正在处理…' : message}</p>
</section>

