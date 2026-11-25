/**
 * Opens an independent HR page through a POST exchange: the ticket is placed in
 * a hidden form field, never in a URL, and the new tab lands on a ticket-free
 * address after the 303.
 */
export async function openHr(
    target: 'me' | 'approvals',
    card?: { chatId: string; operationId: string },
): Promise<void> {
    const response = await fetch('/api/hr-entry', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(card ? { target, ...card } : { target }),
    })
    const data = await response.json()
    if (!response.ok) throw new Error(data.message ?? '暂时无法打开 HR 页面')
    if (typeof data.action !== 'string' || typeof data.ticket !== 'string') {
        throw new Error('票据签发失败')
    }
    const form = document.createElement('form')
    form.method = 'POST'
    form.action = data.action
    form.target = '_blank'
    form.rel = 'noopener'
    form.style.display = 'none'
    const field = document.createElement('input')
    field.type = 'hidden'
    field.name = 'ticket'
    field.value = data.ticket
    form.appendChild(field)
    document.body.appendChild(form)
    form.submit()
    form.remove()
}
