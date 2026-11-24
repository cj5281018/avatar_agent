import { json } from '@sveltejs/kit'
import type { RequestHandler } from './$types.js'
import { listRunsForUser } from '$lib/server/db/trace'
import { wireRun } from '$lib/server/trace/summary'

export const GET: RequestHandler = async ({ locals, url }) => {
    if (!locals.user) return json({ error: '请先登录' }, { status: 401 })

    const limit = Math.min(100, Math.max(1, Number(url.searchParams.get('limit') ?? 50) || 50))
    const now = Date.now()
    const runs = await listRunsForUser(locals.user.id, limit)
    return json(
        { runs: runs.map((run) => wireRun(run, now)) },
        { headers: { 'cache-control': 'no-store' } },
    )
}
