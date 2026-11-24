import { json } from '@sveltejs/kit'
import type { RequestHandler } from './$types.js'
import { listRunsForUser } from '$lib/server/db/trace'

export const GET: RequestHandler = async ({ locals, url }) => {
    if (!locals.user) return json({ error: '请先登录' }, { status: 401 })

    const limit = Math.min(100, Math.max(1, Number(url.searchParams.get('limit') ?? 50) || 50))
    const runs = await listRunsForUser(locals.user.id, limit)

    return json(
        {
            runs: runs.map((run) => ({
                runId: run.runId,
                chatId: run.chatId,
                chatTitle: run.chatTitle,
                status: run.status,
                attempt: run.attempt,
                modelName: run.modelName,
                steps: run.steps,
                startedAt: new Date(run.startedAt).toISOString(),
                durationMs: run.endedAt
                    ? Math.max(0, new Date(run.endedAt).getTime() - new Date(run.startedAt).getTime())
                    : null,
            })),
        },
        { headers: { 'cache-control': 'no-store' } },
    )
}
