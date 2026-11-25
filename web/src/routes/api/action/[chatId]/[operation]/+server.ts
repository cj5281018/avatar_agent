import { env } from '$env/dynamic/private'
import { error, json } from '@sveltejs/kit'
import type { RequestHandler } from './$types.js'
import { chatRepository } from '$lib/server/db/chats'

const BUTTON_LABELS: Record<string, string> = {
    confirm: '确认提交',
    status: '回查同一申请',
    open_hr: '在 HR 中查看',
}

/**
 * Generic descriptor entry. The public route only proves that the caller owns
 * the chat; the department and action come from the trusted operation record,
 * and the department's adapter supplies the summary and allowed operations.
 */
export const GET: RequestHandler = async ({ locals, params }) => {
    if (!locals.user) error(401, '请先登录')
    const chat = await chatRepository.get(params.chatId)
    if (!chat) error(404, '找不到该会话')
    if (chat.userId !== locals.user.id) error(403, '无权查看该卡片')
    if (!env.AVATAR2_GATEWAY_URL || !env.AVATAR2_INTERNAL_TOKEN) error(503, '部门服务未配置')

    const headers = {
        authorization: 'Bearer ' + env.AVATAR2_INTERNAL_TOKEN,
        'content-type': 'application/json',
    }
    const recordResponse = await fetch(
        `${env.AVATAR2_GATEWAY_URL}/internal/operations/${params.operation}`,
        { headers, signal: AbortSignal.timeout(10_000) },
    ).catch(() => null)
    if (!recordResponse?.ok) error(404, '找不到该操作')
    const record = (await recordResponse.json()) as {
        department: string
        action: string
        userId?: string
        chatId?: string | null
        legacy?: boolean
    }
    if (!record.legacy) {
        if (record.userId !== locals.user.id) error(403, '无权查看该操作')
        if (record.chatId && record.chatId !== params.chatId) error(403, '操作不属于该会话')
    }

    const describeResponse = await fetch(
        `${env.AVATAR2_GATEWAY_URL}/internal/adapters/${record.department}/${record.action}/describe`,
        {
            method: 'POST',
            headers,
            body: JSON.stringify({
                operationId: params.operation,
                user_id: locals.user.id,
                chatId: params.chatId,
            }),
            signal: AbortSignal.timeout(15_000),
        },
    ).catch(() => null)
    if (describeResponse?.status === 403) error(403, '无权查看该操作')
    if (describeResponse?.status === 404) error(404, '找不到该操作')
    if (!describeResponse?.ok) error(502, '暂时无法读取卡片描述')

    const descriptor = (await describeResponse.json()) as {
        allowed?: string[]
        [key: string]: unknown
    }
    return json(
        {
            ...descriptor,
            buttons: (descriptor.allowed ?? []).map((id) => ({
                id,
                label: BUTTON_LABELS[id] ?? id,
            })),
        },
        { headers: { 'cache-control': 'no-store' } },
    )
}
