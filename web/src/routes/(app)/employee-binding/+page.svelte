<script lang="ts">
    type Leave = { id: string; startDate: string; endDate: string; status: string }
    let message = $state('')
    let busy = $state(false)
    let requests = $state<Leave[] | null>(null)

    async function openHrPage(target: 'me' | 'approvals') {
        message = ''
        try {
            await openHr(target)
        } catch (err) {
            message = err instanceof Error ? err.message : '暂时无法打开 HR 页面'
        }
    }

    async function run(action: 'renew' | 'unlink' | 'myRequests') {
        if (busy) return
        busy = true
        requests = null
        message = ''
        try {
            const response = await fetch('/api/avatar2', {
                method: 'POST', headers: { 'content-type': 'application/json' },
                body: JSON.stringify({ action }),
            })
            const result = await response.json()
            if (!response.ok) throw new Error(result.detail ?? result.message ?? '操作失败')
            if (action === 'renew') message = result.handle + ' 已绑定，有效至 ' + new Date(result.expiresAt).toLocaleString()
            if (action === 'unlink') message = '已解除绑定'
            if (action === 'myRequests') requests = result.requests
        } catch (err) {
            message = err instanceof Error ? err.message : '员工服务暂时不可用'
        } finally { busy = false }
    }
</script>

<main class="mx-auto max-w-2xl space-y-4 overflow-auto p-6">
    <h1 class="text-xl font-semibold">员工身份绑定</h1>
    <p>使用当前工作台账号关联的 HR 身份，有效期为两小时。</p>
    <div class="flex flex-wrap gap-3">
        <button class="rounded border px-3 py-2" disabled={busy} onclick={() => run('renew')}>绑定或续期</button>
        <button class="rounded border px-3 py-2" disabled={busy} onclick={() => run('unlink')}>解除绑定</button>
        <button class="rounded border px-3 py-2" disabled={busy} onclick={() => run('myRequests')}>查询我的申请</button>
        <button class="rounded border px-3 py-2" disabled={busy} onclick={() => openHrPage('me')}>打开我的 HR 页面</button>
        <button class="rounded border px-3 py-2" disabled={busy} onclick={() => openHrPage('approvals')}>我的待审批（HR）</button>
    </div>
    <p aria-live="polite">{message}</p>
    {#if requests}
        <h2 class="font-semibold">我的请假申请</h2>
        {#each requests as row (row.id)}
            <p>{row.id} · {row.startDate} 至 {row.endDate} · {row.status}</p>
        {:else}<p>暂无请假申请</p>{/each}
    {/if}
</main>
