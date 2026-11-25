<script lang="ts">
    import ActionCard from '$lib/components/action-card.svelte'

    // Fixture from another department: the same component renders it without
    // any HR-specific branch. Buttons are inert because this is a demo page.
    let note = $state('')
</script>

<main class="mx-auto max-w-2xl space-y-4 p-6">
    <h1 class="text-xl font-semibold">通用动作卡 · 部门夹具</h1>
    <p class="text-sm text-muted-foreground">
        同一个组件由服务端描述驱动：部门、动作、允许操作和摘要字段均来自描述，而不是按部门写前端分支。
    </p>
    <ActionCard
        title="接入测试"
        label="测试部门动作卡"
        subtitle="testkit · echo · v0.0.1"
        rows={[
            { label: '部门', value: 'testkit' },
            { label: '动作', value: 'echo' },
            { label: '结果', value: '只读回显，无业务写入' },
        ]}
        buttons={[{ id: 'read', label: '查看结果' }]}
        note={note}
        onaction={() => (note = '测试动作卡已渲染同一组件，未调用任何部门写入。')} />
</main>
