import { error, json } from '@sveltejs/kit'
import type { RequestHandler } from './$types.js'
import { chatRepository } from '$lib/server/db/chats'
import { listChatRuns } from '$lib/server/db/trace'
import { wireRun } from '$lib/server/trace/summary'

export const GET: RequestHandler = async ({ locals, params }) => {
    if (!locals.user) error(401, '请先登录')

    const chat = await chatRepository.get(params.chatId)
    if (!chat) error(404, '找不到该会话')
    // Looking at a trace follows chat ownership: being able to approve a leave
    // request does not grant access to the requester's conversation.
    if (chat.userId !== locals.user.id) error(403, '无权查看该会话')

    const now = Date.now()
    const runs = await listChatRuns(params.chatId)
    return json(
        { runs: runs.map((run) => wireRun(run, now)) },
        { headers: { 'cache-control': 'no-store' } },
    )
}
