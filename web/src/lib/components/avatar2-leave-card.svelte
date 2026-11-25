<script lang="ts">
    import { onMount } from 'svelte'
    import { openHr } from '$lib/hr-entry'
    import ActionCard from './action-card.svelte'

    type Descriptor = {
        title: string
        label: string
        department: string
        action: string
        operationId: string
        rows: { label: string; value: string }[]
        buttons: { id: string; label: string; disabled?: boolean }[]
        allowed: string[]
        note?: string
    }

    let { chatId, operationId }: { chatId: string; operationId: string } = $props()
    let item = $state<{ status: string; leaveId?: string; hrStatus?: string; input: Record<string, string> }>()
    let descriptor = $state<Descriptor | null>(null)
    let busy = $state(false)
    let message = $state('')

    async function loadDescriptor() {
        try {
            const response = await fetch(`/api/action/${chatId}/${operationId}`)
            if (response.ok) descriptor = (await response.json()) as Descriptor
        } catch {
            descriptor = null
        }
    }

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
            await loadDescriptor()
        } catch (error) {
            message = error instanceof Error ? error.message : '部门服务暂时不可用'
        } finally {
            busy = false
        }
    }

    async function handle(id: string) {
        if (id === 'confirm') await update('confirm')
        else if (id === 'status') await update('status')
        else if (id === 'open_hr') {
            try {
                await openHr('me', { chatId, operationId })
            } catch (error) {
                message = error instanceof Error ? error.message : '暂时无法打开 HR 页面'
            }
        }
    }

    onMount(() => {
        void update('status')
    })
</script>

{#if descriptor}
    <ActionCard
        title={descriptor.title}
        label={descriptor.label}
        subtitle={`${descriptor.department} · ${descriptor.action}`}
        rows={descriptor.rows}
        buttons={descriptor.buttons.map((button) => ({ ...button, disabled: busy }))}
        note={message || descriptor.note || ''}
        onaction={handle} />
{:else if item}
    <ActionCard
        title="请假申请"
        label="请假确认卡"
        rows={[
            { label: '开始日期', value: item.input.startDate },
            { label: '结束日期', value: item.input.endDate },
            { label: '假别', value: item.input.leaveType },
            { label: '事由', value: item.input.reason },
            { label: 'HR 单号', value: item.leaveId ?? '尚未提交' },
            { label: '状态', value: item.hrStatus ?? item.status },
        ]}
        buttons={[
            ...(item.leaveId ? [] : [{ id: 'confirm', label: '确认提交' }]),
            { id: 'status', label: '回查同一申请' },
            { id: 'open_hr', label: '在 HR 中查看' },
        ].map((button) => ({ ...button, disabled: busy }))}
        note={message}
        onaction={handle} />
{:else}
    <p class="my-3 text-sm text-muted-foreground">{message || '正在读取请假卡…'}</p>
{/if}
