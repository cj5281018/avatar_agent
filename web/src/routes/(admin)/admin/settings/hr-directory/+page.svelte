<script lang="ts">
    import type { ActionData } from './$types'
    let { form }: { form: ActionData } = $props()
</script>

<main class="mx-auto max-w-lg space-y-5 p-6">
    <h1 class="text-xl font-semibold">HR 身份关联</h1>
    <p>为有效的工作台账号关联现有 HR 身份。保存后，员工可在员工服务中自助绑定。</p>
    <form method="POST" class="space-y-4">
        <label class="block">
            工作台邮箱
            <input class="mt-1 block w-full rounded border p-2" name="email" type="email" maxlength="254" required />
        </label>
        <label class="block">
            HR 用户名
            <input class="mt-1 block w-full rounded border p-2" name="handle" maxlength="64" required />
        </label>
        <button class="rounded border px-4 py-2 font-semibold" type="submit">保存关联</button>
    </form>
    <p role="status">{form?.message ?? ''}</p>
</main>
