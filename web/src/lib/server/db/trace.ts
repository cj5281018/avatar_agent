import { sql } from 'drizzle-orm'
import { db } from './index'
import type { TraceEventRow, TraceRunRow } from '$lib/server/trace/digest'

export type TraceRunSummary = TraceRunRow & {
    chatId: string
    chatTitle: string | null
    userMessageId: string | null
    attempt: number
    modelName: string | null
    errorCode: string | null
    status: string
    startedAt: Date
    endedAt: Date | null
    steps: number
}

function mapRun(row: Record<string, unknown>): TraceRunSummary {
    return {
        runId: row.run_id as string,
        chatId: row.chat_id as string,
        chatTitle: (row.chat_title as string | null) ?? null,
        userMessageId: (row.user_message_id as string | null) ?? null,
        attempt: Number(row.attempt ?? 1),
        status: row.status as string,
        modelName: (row.model_name as string | null) ?? null,
        errorCode: (row.error_code as string | null) ?? null,
        startedAt: row.started_at as Date,
        endedAt: (row.ended_at as Date | null) ?? null,
        lastSeq: Number(row.last_seq ?? 0),
        headDigest: (row.head_digest as string | null) ?? null,
        sealVersion: (row.seal_version as string | null) ?? null,
        seal: (row.seal as string | null) ?? null,
        steps: Number(row.steps ?? 0),
    }
}

const RUN_COLUMNS = sql`
    r.run_id, r.chat_id, r.user_message_id, r.attempt, r.status, r.model_name,
    r.error_code, r.started_at, r.ended_at, r.last_seq, r.head_digest,
    r.seal_version, r.seal
`

/** Runs for one chat, newest first, with the number of recorded events. */
export async function listChatRuns(chatId: string, limit = 100): Promise<TraceRunSummary[]> {
    const rows = await db.execute(sql`
        SELECT ${RUN_COLUMNS},
            (SELECT count(*)::int FROM agent_trace_events e WHERE e.run_id = r.run_id) AS steps
        FROM agent_trace_runs r
        WHERE r.chat_id = ${chatId}
        ORDER BY r.started_at DESC
        LIMIT ${limit}
    `)
    return (rows as Record<string, unknown>[]).map(mapRun)
}

/** The most recent run that answered a specific user message. */
export async function listRunsByUserMessages(
    chatId: string,
    messageIds: string[],
): Promise<TraceRunSummary[]> {
    if (messageIds.length === 0) return []
    const rows = await db.execute(sql`
        SELECT ${RUN_COLUMNS},
            (SELECT count(*)::int FROM agent_trace_events e WHERE e.run_id = r.run_id) AS steps
        FROM agent_trace_runs r
        WHERE r.chat_id = ${chatId} AND r.user_message_id = ANY(${messageIds}::text[])
        ORDER BY r.started_at DESC
    `)
    return (rows as Record<string, unknown>[]).map(mapRun)
}

export async function getRun(chatId: string, runId: string): Promise<TraceRunSummary | null> {
    const rows = await db.execute(sql`
        SELECT ${RUN_COLUMNS}, c.title AS chat_title,
            (SELECT count(*)::int FROM agent_trace_events e WHERE e.run_id = r.run_id) AS steps
        FROM agent_trace_runs r
        LEFT JOIN chats c ON c.id = r.chat_id
        WHERE r.run_id = ${runId} AND r.chat_id = ${chatId}
    `)
    const row = (rows as Record<string, unknown>[])[0]
    return row ? mapRun(row) : null
}

export async function listEvents(runId: string): Promise<TraceEventRow[]> {
    const rows = await db.execute(sql`
        SELECT run_id, seq, event_id, span_id, parent_span_id, type, ts, payload,
               previous_digest, digest
        FROM agent_trace_events
        WHERE run_id = ${runId}
        ORDER BY seq ASC
    `)
    return (rows as Record<string, unknown>[]).map((row) => ({
        runId: row.run_id as string,
        seq: Number(row.seq),
        eventId: row.event_id as string,
        spanId: row.span_id as string,
        parentSpanId: (row.parent_span_id as string | null) ?? null,
        type: row.type as string,
        ts: row.ts as Date,
        payload: (row.payload as Record<string, unknown>) ?? {},
        previousDigest: (row.previous_digest as string | null) ?? null,
        digest: row.digest as string,
    }))
}

/** Recent runs across all of a person's chats, for the trace centre list. */
export async function listRunsForUser(userId: string, limit = 50): Promise<TraceRunSummary[]> {
    const rows = await db.execute(sql`
        SELECT ${RUN_COLUMNS}, c.title AS chat_title,
            (SELECT count(*)::int FROM agent_trace_events e WHERE e.run_id = r.run_id) AS steps
        FROM agent_trace_runs r
        JOIN chats c ON c.id = r.chat_id
        WHERE r.user_id = ${userId}
        ORDER BY r.started_at DESC
        LIMIT ${limit}
    `)
    return (rows as Record<string, unknown>[]).map(mapRun)
}
