import { env } from '$env/dynamic/private'
import { error, json } from '@sveltejs/kit'
import type { RequestHandler } from './$types.js'
import { chatRepository } from '$lib/server/db/chats'
import { getRun, listEvents } from '$lib/server/db/trace'
import { verifyRun } from '$lib/server/trace/digest'

export const GET: RequestHandler = async ({ locals, params }) => {
    if (!locals.user) error(401, '请先登录')

    const chat = await chatRepository.get(params.chatId)
    if (!chat) error(404, '找不到该会话')
    if (chat.userId !== locals.user.id) error(403, '无权查看该会话')

    const run = await getRun(params.chatId, params.runId)
    if (!run) error(404, '找不到该执行记录')

    const events = await listEvents(run.runId)
    const sealKey = env.AGENT_TRACE_SEAL_KEY?.trim() || null
    const verification = verifyRun(run, events, sealKey)

    return json(
        {
            run: {
                runId: run.runId,
                chatId: run.chatId,
                attempt: run.attempt,
                status: run.status,
                modelName: run.modelName,
                errorCode: run.errorCode,
                userMessageId: run.userMessageId,
                startedAt: new Date(run.startedAt).toISOString(),
                endedAt: run.endedAt ? new Date(run.endedAt).toISOString() : null,
                lastSeq: run.lastSeq,
                sealed: Boolean(run.sealVersion && run.seal),
            },
            events: events.map((event) => ({
                seq: event.seq,
                spanId: event.spanId,
                parentSpanId: event.parentSpanId,
                type: event.type,
                ts: new Date(event.ts).toISOString(),
                payload: event.payload,
            })),
            verification,
        },
        { headers: { 'cache-control': 'no-store' } },
    )
}
