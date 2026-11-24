<script lang="ts">
    import { onMount } from 'svelte'
    type Leave = { id: string; requester: { displayName: string }; startDate: string; endDate: string; reason: string; leaveType: string }
    let rows = $state<Leave[]>([])
    let selected = $state<Leave>()
    let busy = $state(true)
    let notice = $state('')
    let loaded = $state(false)
    const kinds: Record<string, string> = { annual: '年假', sick: '病假', personal: '事假', compensatory: '调休', other: '其他' }

    async function request(action: string, leaveId?: string) {
        const r = await fetch('/api/avatar2', { method: 'POST', headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ action, leaveId }) })
        const data = await r.json()
        if (!r.ok) throw new Error(data.detail ?? data.message ?? 'HR 服务暂时不可用')
        return data
    }

    async function refresh() {
        busy = true
        loaded = false
        selected = undefined
        notice = ''
        try {
            rows = (await request('pendingReviews')).requests
            loaded = true
        } catch (e) { notice = e instanceof Error ? e.message : '加载失败' }
        finally { busy = false }
    }

    async function approve(item: Leave) {
        if (busy) return
        busy = true
        try {
            const result = await request('approve', item.id)
            if (result.id !== item.id) throw new Error('审批回执单号不一致，请刷新核对')
            notice = result.applied ? '已批准 ' + result.id : '审批状态已更新：' + result.status + ' · ' + result.id
            rows = rows.filter(row => row.id !== item.id)
            selected = undefined
        } catch (e) { notice = e instanceof Error ? e.message : '审批失败' }
        finally { busy = false }
    }
    onMount(() => { void refresh() })
</script>

<main class="mx-auto w-full max-w-3xl space-y-5 overflow-auto p-6">
    <div class="flex items-center justify-between"><h1 class="text-xl font-semibold">待审批请假单</h1>
        <button class="rounded border px-3 py-1" onclick={refresh} disabled={busy}>刷新列表</button></div>
    <p aria-live="polite">{busy ? '正在处理…' : notice}</p>
    {#if loaded}
        {#each rows as row (row.id)}
            <button class="block w-full rounded border p-3 text-left" disabled={busy}
                onclick={() => { selected = row; notice = '' }}>{row.requester.displayName} · {row.startDate} · {row.id}</button>
        {:else}<p>暂无待审批请假单</p>{/each}
    {:else if !busy}<a href="/employee-binding" class="underline">员工身份绑定</a>{/if}
    {#if selected}
        <section class="space-y-3 rounded border p-4" aria-label="申请详情">
            <h2 class="font-semibold">申请详情</h2><p>单号 {selected.id}</p>
            <p>{selected.requester.displayName} · {kinds[selected.leaveType] ?? selected.leaveType}</p>
            <p>{selected.startDate} 至 {selected.endDate}</p><p>事由：{selected.reason}</p>
            <button class="rounded border px-4 py-2 font-semibold" disabled={busy} onclick={() => selected && approve(selected)}>批准</button>
        </section>
    {/if}
</main>
