import { env } from '$env/dynamic/private'
import { error, json } from '@sveltejs/kit'
import type { RequestHandler } from './$types.js'
import { chatRepository } from '$lib/server/db/chats'
import { findOperationId, getRun } from '$lib/server/db/trace'

/**
 * Links a drafting turn to the business record it produced.
 *
 * `runId -> operationId` comes from the trace ledger; everything after that is
 * read through the authoritative leave card status path, with the signed-in
 * employee's own binding. This endpoint is strictly read-only: it can never
 * submit, approve or retry anything.
 */
export const GET: RequestHandler = async ({ locals, params }) => {
    if (!locals.user) error(401, '请先登录')

    const chat = await chatRepository.get(params.chatId)
    if (!chat) error(404, '找不到该会话')
    if (chat.userId !== locals.user.id) error(403, '无权查看该会话')

    const run = await getRun(params.chatId, params.runId)
    if (!run) error(404, '找不到该执行记录')

    const operationId = await findOperationId(run.runId)
    if (!operationId) {
        return json(
            { available: false, reason: 'no_operation', detail: '这一轮没有产生可关联的业务操作。' },
            { headers: { 'cache-control': 'no-store' } },
        )
    }

    if (!env.AVATAR2_GATEWAY_URL || !env.AVATAR2_INTERNAL_TOKEN) {
        return json(
            {
                available: false,
                reason: 'not_configured',
                operationId,
                detail: '办理状态暂不可用（部门服务未配置）。',
            },
            { headers: { 'cache-control': 'no-store' } },
        )
    }

    let response: Response
    try {
        response = await fetch(
            `${env.AVATAR2_GATEWAY_URL}/internal/cards/${params.chatId}/${operationId}/status`,
            {
                method: 'POST',
                headers: {
                    authorization: 'Bearer ' + env.AVATAR2_INTERNAL_TOKEN,
                    'content-type': 'application/json',
                },
                body: JSON.stringify({ user_id: locals.user.id }),
                signal: AbortSignal.timeout(15_000),
            },
        )
    } catch {
        return json(
            {
                available: false,
                reason: 'hr_unavailable',
                operationId,
                detail: '办理状态暂不可用，执行记录本身不受影响。',
            },
            { headers: { 'cache-control': 'no-store' } },
        )
    }

    if (response.status === 403) {
        return json(
            {
                available: false,
                reason: 'binding_required',
                operationId,
                detail: '请先在“员工服务”绑定或续期 HR 身份，再查看办理进度。',
            },
            { headers: { 'cache-control': 'no-store' } },
        )
    }
    if (response.status === 404) {
        return json(
            {
                available: false,
                reason: 'not_found',
                operationId,
                detail: '部门服务中找不到这笔操作。',
            },
            { headers: { 'cache-control': 'no-store' } },
        )
    }
    if (!response.ok) {
        return json(
            {
                available: false,
                reason: 'hr_unavailable',
                operationId,
                detail: '办理状态暂不可用，请稍后重试。',
            },
            { headers: { 'cache-control': 'no-store' } },
        )
    }

    const card = (await response.json()) as {
        status?: string
        leaveId?: string | null
        hrStatus?: string | null
    }
    return json(
        {
            available: true,
            operationId,
            cardStatus: card.status ?? null,
            leaveId: card.leaveId ?? null,
            hrStatus: card.hrStatus ?? null,
        },
        { headers: { 'cache-control': 'no-store' } },
    )
}
