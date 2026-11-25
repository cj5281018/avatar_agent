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
 * Server-approved descriptor for the generic action card: which department and
 * action this operation belongs to, the summary to show, and the operations the
 * current user may take. The model's wording is never used for this.
 */
export const GET: RequestHandler = async ({ locals, params }) => {
    if (!locals.user) error(401, '请先登录')
    const chat = await chatRepository.get(params.chatId)
    if (!chat) error(404, '找不到该会话')
    if (chat.userId !== locals.user.id) error(403, '无权查看该卡片')
    if (!env.AVATAR2_GATEWAY_URL || !env.AVATAR2_INTERNAL_TOKEN) error(503, '部门服务未配置')

    const response = await fetch(
        `${env.AVATAR2_GATEWAY_URL}/internal/cards/${params.chatId}/${params.operation}/status`,
        {
            method: 'POST',
            headers: {
                authorization: 'Bearer ' + env.AVATAR2_INTERNAL_TOKEN,
                'content-type': 'application/json',
            },
            body: JSON.stringify({ user_id: locals.user.id }),
            signal: AbortSignal.timeout(15_000),
        },
    ).catch(() => null)

    if (!response) {
        return json(
            { department: 'hr', action: 'leave', allowed: [], note: '办理状态暂不可用。' },
            { headers: { 'cache-control': 'no-store' } },
        )
    }
    if (response.status === 403) {
        return json(
            {
                department: 'hr',
                action: 'leave',
                operationId: params.operation,
                allowed: [],
                note: '请先在账号菜单的“员工服务”中绑定或续期 HR 身份。',
            },
            { headers: { 'cache-control': 'no-store' } },
        )
    }
    if (!response.ok) {
        return json(
            { department: 'hr', action: 'leave', allowed: [], note: '办理状态暂不可用。' },
            { headers: { 'cache-control': 'no-store' } },
        )
    }

    const card = (await response.json()) as {
        input?: Record<string, string>
        leaveId?: string | null
        status?: string
        hrStatus?: string | null
    }
    const input = card.input ?? {}
    const submitted = Boolean(card.leaveId)
    const pending = card.status === 'pending' || card.status === 'confirmed'
    const allowed = submitted ? ['status', 'open_hr'] : pending ? ['confirm', 'status', 'open_hr'] : ['status', 'open_hr']

    return json(
        {
            department: 'hr',
            departmentName: '人力资源',
            action: 'leave',
            pluginVersion: '1.0.0',
            operationId: params.operation,
            title: '请假申请',
            label: '请假确认卡',
            rows: [
                { label: '开始日期', value: input.startDate ?? '—' },
                { label: '结束日期', value: input.endDate ?? '—' },
                { label: '假别', value: input.leaveType ?? '—' },
                { label: '事由', value: input.reason ?? '—' },
                { label: 'HR 单号', value: card.leaveId ?? '尚未提交' },
                { label: '状态', value: card.hrStatus ?? card.status ?? '—' },
            ],
            buttons: allowed.map((id) => ({ id, label: BUTTON_LABELS[id] ?? id })),
            allowed,
        },
        { headers: { 'cache-control': 'no-store' } },
    )
}
