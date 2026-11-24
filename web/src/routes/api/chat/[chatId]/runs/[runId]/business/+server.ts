import { env } from '$env/dynamic/private'
import { error, json } from '@sveltejs/kit'
import type { RequestHandler } from './$types.js'
import { chatRepository } from '$lib/server/db/chats'
import { getSnapshot, listDraftSteps } from '$lib/server/db/trace'

type DraftStatus = {
    spanId: string
    operationId: string
    available: boolean
    reason?: string
    detail?: string
    cardStatus?: string | null
    leaveId?: string | null
    hrStatus?: string | null
}

const MAX_DRAFTS = 20

/**
 * Links drafting steps to the business records they produced.
 *
 * Only successful `hr_preview_leave` steps of *this* run qualify, so a later
 * tool that happens to return an operation id cannot steal the association.
 * `?spanId=` narrows the answer to one step. The call is strictly read-only:
 * it can never submit, approve or retry anything.
 */
export const GET: RequestHandler = async ({ locals, params, url }) => {
    if (!locals.user) error(401, '请先登录')

    const chat = await chatRepository.get(params.chatId)
    if (!chat) error(404, '找不到该会话')
    if (chat.userId !== locals.user.id) error(403, '无权查看该会话')

    const snapshot = await getSnapshot(params.chatId, params.runId)
    if (!snapshot) error(404, '找不到该执行记录')

    const draftSteps = await listDraftSteps(snapshot.run.runId)
    const requestedSpan = url.searchParams.get('spanId')
    if (requestedSpan && !draftSteps.some((step) => step.spanId === requestedSpan)) {
        error(404, '该步骤没有可关联的起草记录')
    }
    const selected = (requestedSpan
        ? draftSteps.filter((step) => step.spanId === requestedSpan)
        : draftSteps
    ).slice(0, MAX_DRAFTS)

    if (selected.length === 0) {
        return json(
            {
                drafts: [],
                detail: '这一轮没有产生可关联的业务操作。旧记录缺少起草步骤标识时也无法可靠关联。',
            },
            { headers: { 'cache-control': 'no-store' } },
        )
    }

    const drafts: DraftStatus[] = []
    for (const step of selected) {
        const base = { spanId: step.spanId, operationId: step.operationId }
        if (!env.AVATAR2_GATEWAY_URL || !env.AVATAR2_INTERNAL_TOKEN) {
            drafts.push({
                ...base,
                available: false,
                reason: 'not_configured',
                detail: '办理状态暂不可用（部门服务未配置）。',
            })
            continue
        }

        let response: Response
        try {
            response = await fetch(
                `${env.AVATAR2_GATEWAY_URL}/internal/cards/${params.chatId}/${step.operationId}/status`,
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
            drafts.push({
                ...base,
                available: false,
                reason: 'hr_unavailable',
                detail: '办理状态暂不可用，执行记录本身不受影响。',
            })
            continue
        }

        if (response.status === 403) {
            drafts.push({
                ...base,
                available: false,
                reason: 'binding_required',
                detail: '请先在“员工服务”绑定或续期 HR 身份，再查看办理进度。',
            })
            continue
        }
        if (response.status === 404) {
            drafts.push({
                ...base,
                available: false,
                reason: 'not_found',
                detail: '部门服务中找不到这笔操作。',
            })
            continue
        }
        if (!response.ok) {
            drafts.push({
                ...base,
                available: false,
                reason: 'hr_unavailable',
                detail: '办理状态暂不可用，请稍后重试。',
            })
            continue
        }

        const card = (await response.json()) as {
            status?: string
            leaveId?: string | null
            hrStatus?: string | null
        }
        drafts.push({
            ...base,
            available: true,
            cardStatus: card.status ?? null,
            leaveId: card.leaveId ?? null,
            hrStatus: card.hrStatus ?? null,
        })
    }

    return json(
        {
            drafts,
            detail:
                drafts.length > 1
                    ? '本轮有多个起草步骤，各自对应一笔业务；未确认的草稿不会生成单号。'
                    : undefined,
        },
        { headers: { 'cache-control': 'no-store' } },
    )
}
