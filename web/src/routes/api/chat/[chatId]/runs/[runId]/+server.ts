import { env } from '$env/dynamic/private'
import { error, json } from '@sveltejs/kit'
import type { RequestHandler } from './$types.js'
import { chatRepository } from '$lib/server/db/chats'
import { getRun, listEvents } from '$lib/server/db/trace'
import { verifyRun } from '$lib/server/trace/digest'
import { wireRun } from '$lib/server/trace/summary'

/**
 * Detail reads support `?afterSeq=N` for incremental polling. Verification is
 * always recomputed over the complete stored chain, never over the delta alone,
 * so a partial read cannot be mistaken for a valid history.
 */
export const GET: RequestHandler = async ({ locals, params, url }) => {
    if (!locals.user) error(401, '请先登录')

    const chat = await chatRepository.get(params.chatId)
    if (!chat) error(404, '找不到该会话')
    if (chat.userId !== locals.user.id) error(403, '无权查看该会话')

    const run = await getRun(params.chatId, params.runId)
    if (!run) error(404, '找不到该执行记录')

    const requested = Number(url.searchParams.get('afterSeq') ?? 0)
    const afterSeq = Number.isFinite(requested) && requested > 0 ? Math.floor(requested) : 0

    const events = await listEvents(run.runId)
    const wire = wireRun(run)
    const sealKey = env.AGENT_TRACE_SEAL_KEY?.trim() || null
    const verification = verifyRun(run, events, sealKey)

    return json(
        {
            run: wire,
            events: events
                .filter((event) => event.seq > afterSeq)
                .map((event) => ({
                    seq: event.seq,
                    spanId: event.spanId,
                    parentSpanId: event.parentSpanId,
                    type: event.type,
                    ts: new Date(event.ts).toISOString(),
                    payload: event.payload,
                })),
            cursor: run.lastSeq,
            verification,
        },
        { headers: { 'cache-control': 'no-store' } },
    )
}
