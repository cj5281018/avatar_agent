import { error, json } from '@sveltejs/kit'
import type { RequestHandler } from './$types.js'
import { chatRepository } from '$lib/server/db/chats'
import { listChatRuns, listRunsByUserMessages } from '$lib/server/db/trace'

const ULID = /^[0-9A-HJKMNP-TV-Z]{26}$/

function durationMs(startedAt: Date, endedAt: Date | null): number | null {
    if (!endedAt) return null
    return Math.max(0, new Date(endedAt).getTime() - new Date(startedAt).getTime())
}

export const GET: RequestHandler = async ({ locals, params, url }) => {
    if (!locals.user) error(401, '请先登录')

    const chat = await chatRepository.get(params.chatId)
    if (!chat) error(404, '找不到该会话')
    // Looking at a trace follows chat ownership: being able to approve a leave
    // request does not grant access to the requester's conversation.
    if (chat.userId !== locals.user.id) error(403, '无权查看该会话')

    const messageIds = url.searchParams
        .getAll('messageId')
        .filter((value) => ULID.test(value))
        .slice(0, 100)
    const runs = messageIds.length
        ? await listRunsByUserMessages(params.chatId, messageIds)
        : await listChatRuns(params.chatId)

    return json(
        {
            runs: runs.map((run) => ({
                runId: run.runId,
                userMessageId: run.userMessageId,
                status: run.status,
                attempt: run.attempt,
                modelName: run.modelName,
                errorCode: run.errorCode,
                steps: run.steps,
                startedAt: new Date(run.startedAt).toISOString(),
                endedAt: run.endedAt ? new Date(run.endedAt).toISOString() : null,
                durationMs: durationMs(run.startedAt, run.endedAt),
            })),
        },
        { headers: { 'cache-control': 'no-store' } },
    )
}
