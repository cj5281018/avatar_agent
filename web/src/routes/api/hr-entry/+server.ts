import { env } from '$env/dynamic/private'
import { error, json } from '@sveltejs/kit'
import type { RequestHandler } from './$types.js'

const TARGETS = new Set(['me', 'approvals'])

/**
 * Issues a short-lived, single-use ticket that lets the signed-in employee open
 * the independent HR pages without a second password. The ticket is redeemed by
 * the HR service; it never appears in a trace, log or final URL.
 */
export const POST: RequestHandler = async ({ locals, request, url }) => {
    if (!locals.user) error(401, '请先登录')
    if (request.headers.get('origin') !== url.origin) error(403, '来源无效')
    if (!env.AVATAR2_GATEWAY_URL || !env.AVATAR2_INTERNAL_TOKEN) error(503, '部门服务未配置')
    if (!env.AVATAR2_HR_WEB_URL) error(503, 'HR 网页地址未配置')

    const body = await request.json().catch(() => ({}))
    const target = body?.target ?? 'me'
    if (typeof target !== 'string' || !TARGETS.has(target)) error(400, '目标页面无效')

    const response = await fetch(env.AVATAR2_GATEWAY_URL + '/internal/hr/web-ticket', {
        method: 'POST',
        headers: {
            authorization: 'Bearer ' + env.AVATAR2_INTERNAL_TOKEN,
            'content-type': 'application/json',
        },
        body: JSON.stringify({ user_id: locals.user.id, target }),
        signal: AbortSignal.timeout(10_000),
    }).catch(() => null)

    if (response?.status === 403) error(403, '请先在员工服务中绑定或续期 HR 身份')
    if (!response?.ok) error(502, '暂时无法打开 HR 页面，请稍后重试')
    const ticket = (await response.json()) as { ticket?: string }
    if (!ticket.ticket) error(502, '票据签发失败')

    // The ticket is handed over in a POST body; it never becomes a URL.
    return json(
        {
            action: env.AVATAR2_HR_WEB_URL.replace(/\/$/, '') + '/web/ticket',
            ticket: ticket.ticket,
            target,
        },
        {
            headers: {
                'cache-control': 'no-store',
                'referrer-policy': 'no-referrer',
            },
        },
    )
}
