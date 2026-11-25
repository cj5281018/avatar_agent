<script lang="ts">
    type CardRow = { label: string; value: string }
    type CardButton = { id: string; label: string; disabled?: boolean }

    let {
        title,
        subtitle = '',
        rows,
        buttons,
        note = '',
        label = title,
        onaction,
    }: {
        title: string
        label?: string
        subtitle?: string
        rows: CardRow[]
        buttons: CardButton[]
        note?: string
        onaction: (id: string) => void
    } = $props()
</script>

<!--
    Generic department action card. It renders a server-provided descriptor
    (summary rows + allowed actions) and knows nothing about a specific
    department; the caller supplies the action handlers.
-->
<section class="my-3 rounded-lg border p-4" aria-label={label} data-testid="generic-action-card">
    <h3 class="font-semibold">{title}</h3>
    {#if subtitle}<p class="text-sm text-muted-foreground">{subtitle}</p>{/if}
    <dl class="mt-2 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
        {#each rows as row (row.label)}
            <dt class="text-muted-foreground">{row.label}</dt>
            <dd class="break-all">{row.value}</dd>
        {/each}
    </dl>
    {#if buttons.length > 0}
        <div class="mt-3 flex flex-wrap gap-2">
            {#each buttons as button (button.id)}
                <button
                    type="button"
                    class="rounded border px-3 py-1"
                    disabled={button.disabled}
                    data-action={button.id}
                    onclick={() => onaction(button.id)}>
                    {button.label}
                </button>
            {/each}
        </div>
    {/if}
    {#if note}<p class="mt-2 text-xs text-muted-foreground">{note}</p>{/if}
</section>
