import { env } from '$env/dynamic/private'
import { error, json } from '@sveltejs/kit'
import type { RequestHandler } from './$types'
import { chatRepository } from '$lib/server/db/chats'

export const POST: RequestHandler = async ({ request, locals, url }) => {
    if (!locals.user) error(401, '请先登录')
    if (request.headers.get('origin') !== url.origin) error(403, '来源无效')
    if (!env.AVATAR2_GATEWAY_URL || !env.AVATAR2_INTERNAL_TOKEN) error(503, '部门服务未配置')

    const body = await request.json()
    const user_id = locals.user.id
    let path: string
    let method = 'POST'
    let payload: object = { user_id }
    if (body.action === 'bind' && typeof body.token === 'string') {
        path = '/internal/bindings'
        payload = { user_id, token: body.token }
    } else if (body.action === 'renew') {
        path = '/internal/bindings/renew'
    } else if (body.action === 'myRequests') {
        path = '/internal/my-requests'
    } else if (body.action === 'pendingReviews') {
        path = '/internal/reviews'
    } else if (body.action === 'approve' && typeof body.leaveId === 'string' &&
            /^leave_[A-Za-z0-9_-]{1,100}$/.test(body.leaveId)) {
        path = '/internal/reviews/' + body.leaveId + '/approve'
    } else if (body.action === 'unlink') {
        path = '/internal/bindings/' + encodeURIComponent(user_id)
        method = 'DELETE'
    } else if (['status', 'confirm'].includes(body.action)) {
        if (typeof body.chatId !== 'string' || !/^[0-9A-HJKMNP-TV-Z]{26}$/.test(body.chatId) ||
            typeof body.operationId !== 'string' || !/^lop_[A-Za-z0-9_-]+$/.test(body.operationId)) {
            error(400, '请假操作编号无效')
        }
        const chat = await chatRepository.get(body.chatId)
        if (!chat || chat.userId !== user_id) error(404, '找不到该会话')
        path = '/internal/cards/' + body.chatId + '/' + body.operationId + '/' + body.action
    } else {
        error(400, '操作无效')
    }

    const response = await fetch(env.AVATAR2_GATEWAY_URL + path, {
        method,
        headers: {
            authorization: 'Bearer ' + env.AVATAR2_INTERNAL_TOKEN,
            'content-type': 'application/json',
        },
        body: method === 'DELETE' ? undefined : JSON.stringify(payload),
        signal: AbortSignal.timeout(25_000),
    })
    const result = response.status === 204 ? { ok: true } : await response.json()
    return json(result, { status: response.status === 204 ? 200 : response.status,
                          headers: { 'cache-control': 'no-store' } })
}
