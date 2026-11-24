import { sql } from 'drizzle-orm'
import { db } from './index'
import type { TraceEventRow, TraceRunRow } from '$lib/server/trace/digest'

export type TraceRunSummary = TraceRunRow & {
    chatId: string
    chatTitle: string | null
    userMessageId: string | null
    messageText: string | null
    attempt: number
    modelName: string | null
    errorCode: string | null
    status: string
    captureStatus: string
    captureError: string | null
    droppedEvents: number
    toolError: boolean
    startedAt: Date
    endedAt: Date | null
    lastEventAt: Date | null
    steps: number
}

function mapRun(row: Record<string, unknown>): TraceRunSummary {
    return {
        runId: row.run_id as string,
        chatId: row.chat_id as string,
        chatTitle: (row.chat_title as string | null) ?? null,
        userMessageId: (row.user_message_id as string | null) ?? null,
        messageText: (row.message_text as string | null) ?? null,
        attempt: Number(row.attempt ?? 1),
        status: row.status as string,
        modelName: (row.model_name as string | null) ?? null,
        errorCode: (row.error_code as string | null) ?? null,
        captureStatus: (row.capture_status as string | null) ?? 'unknown',
        captureError: (row.capture_error as string | null) ?? null,
        droppedEvents: Number(row.dropped_events ?? 0),
        toolError: Boolean(row.tool_error),
        startedAt: row.started_at as Date,
        endedAt: (row.ended_at as Date | null) ?? null,
        lastEventAt: (row.last_event_at as Date | null) ?? null,
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
    r.seal_version, r.seal, r.capture_status, r.capture_error,
    r.dropped_events, r.last_event_at
`

/** Chat runs, newest first, with the question that produced each one. */
export async function listChatRuns(chatId: string, limit = 100): Promise<TraceRunSummary[]> {
    const rows = await db.execute(sql`
        SELECT ${RUN_COLUMNS},
            c.title AS chat_title,
            LEFT(COALESCE(m.content_text, ''), 200) AS message_text,
            (SELECT count(*)::int FROM agent_trace_events e WHERE e.run_id = r.run_id) AS steps,
            EXISTS (
                SELECT 1 FROM agent_trace_events e
                WHERE e.run_id = r.run_id AND e.type = 'tool.failed'
            ) AS tool_error
        FROM agent_trace_runs r
        LEFT JOIN chats c ON c.id = r.chat_id
        LEFT JOIN chat_messages m ON m.id = r.user_message_id
        WHERE r.chat_id = ${chatId}
        ORDER BY r.started_at DESC
        LIMIT ${limit}
    `)
    return (rows as Record<string, unknown>[]).map(mapRun)
}

export async function getRun(chatId: string, runId: string): Promise<TraceRunSummary | null> {
    const rows = await db.execute(sql`
        SELECT ${RUN_COLUMNS},
            c.title AS chat_title,
            LEFT(COALESCE(m.content_text, ''), 200) AS message_text,
            (SELECT count(*)::int FROM agent_trace_events e WHERE e.run_id = r.run_id) AS steps,
            EXISTS (
                SELECT 1 FROM agent_trace_events e
                WHERE e.run_id = r.run_id AND e.type = 'tool.failed'
            ) AS tool_error
        FROM agent_trace_runs r
        LEFT JOIN chats c ON c.id = r.chat_id
        LEFT JOIN chat_messages m ON m.id = r.user_message_id
        WHERE r.run_id = ${runId} AND r.chat_id = ${chatId}
    `)
    const row = (rows as Record<string, unknown>[])[0]
    return row ? mapRun(row) : null
}

/** Events of a run, optionally only those after a cursor for incremental reads. */
export async function listEvents(runId: string, afterSeq = 0): Promise<TraceEventRow[]> {
    const rows = await db.execute(sql`
        SELECT run_id, seq, event_id, span_id, parent_span_id, type, ts, payload,
               previous_digest, digest
        FROM agent_trace_events
        WHERE run_id = ${runId} AND seq > ${afterSeq}
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

/** Drafting steps of a run: the HR preview calls that actually produced a draft. */
export type DraftStep = { spanId: string; operationId: string; seq: number }

export async function listDraftSteps(runId: string): Promise<DraftStep[]> {
    const rows = await db.execute(sql`
        SELECT span_id, payload->>'operationId' AS operation_id, seq
        FROM agent_trace_events
        WHERE run_id = ${runId}
          AND type = 'tool.completed'
          AND payload->>'tool' = 'hr_preview_leave'
          AND payload->>'status' = 'ok'
          AND payload ? 'operationId'
        ORDER BY seq ASC
    `)
    // Keyed by span, not by operation: two drafting steps may legitimately
    // resolve to the same business record and must both stay reachable.
    const seen = new Set<string>()
    const drafts: DraftStep[] = []
    for (const row of rows as Record<string, unknown>[]) {
        const spanId = row.span_id as string
        if (seen.has(spanId)) continue
        seen.add(spanId)
        drafts.push({ spanId, operationId: row.operation_id as string, seq: Number(row.seq) })
    }
    return drafts
}

function mapEventJson(raw: Record<string, unknown>): TraceEventRow {
    return {
        runId: raw.run_id as string,
        seq: Number(raw.seq),
        eventId: raw.event_id as string,
        spanId: raw.span_id as string,
        parentSpanId: (raw.parent_span_id as string | null) ?? null,
        type: raw.type as string,
        ts: raw.ts as string,
        payload: (raw.payload as Record<string, unknown>) ?? {},
        previousDigest: (raw.previous_digest as string | null) ?? null,
        digest: raw.digest as string,
    }
}

/**
 * Reads the run row and its events from one statement, so the cursor, the
 * events it covers, the head digest and the seal all belong to the same
 * snapshot even while the writer is appending.
 */
export async function getSnapshot(
    chatId: string,
    runId: string,
): Promise<{ run: TraceRunSummary; events: TraceEventRow[] } | null> {
    const rows = await db.execute(sql`
        SELECT row_to_json(r) AS run_json,
               COALESCE(
                   json_agg(e ORDER BY e.seq) FILTER (WHERE e.seq IS NOT NULL),
                   '[]'::json
               ) AS events_json,
               c.title AS chat_title,
               LEFT(COALESCE(m.content_text, ''), 200) AS message_text
        FROM agent_trace_runs r
        LEFT JOIN chats c ON c.id = r.chat_id
        LEFT JOIN chat_messages m ON m.id = r.user_message_id
        LEFT JOIN agent_trace_events e ON e.run_id = r.run_id
        WHERE r.run_id = ${runId} AND r.chat_id = ${chatId}
        GROUP BY r.run_id, c.title, m.content_text
    `)
    const row = (rows as Record<string, unknown>[])[0]
    if (!row) return null
    const rawRun = row.run_json as Record<string, unknown>
    const events = (row.events_json as Record<string, unknown>[]).map(mapEventJson)
    const run = mapRun({
        ...rawRun,
        chat_id: rawRun.chat_id,
        chat_title: row.chat_title,
        message_text: row.message_text,
        steps: events.length,
        tool_error: events.some((event) => event.type === 'tool.failed'),
    })
    return { run, events }
}

/** Recent runs across all of a person's chats, for the trace centre list. */
export async function listRunsForUser(userId: string, limit = 50): Promise<TraceRunSummary[]> {
    const rows = await db.execute(sql`
        SELECT ${RUN_COLUMNS},
            c.title AS chat_title,
            LEFT(COALESCE(m.content_text, ''), 200) AS message_text,
            (SELECT count(*)::int FROM agent_trace_events e WHERE e.run_id = r.run_id) AS steps,
            EXISTS (
                SELECT 1 FROM agent_trace_events e
                WHERE e.run_id = r.run_id AND e.type = 'tool.failed'
            ) AS tool_error
        FROM agent_trace_runs r
        JOIN chats c ON c.id = r.chat_id
        LEFT JOIN chat_messages m ON m.id = r.user_message_id
        WHERE r.user_id = ${userId}
        ORDER BY r.started_at DESC
        LIMIT ${limit}
    `)
    return (rows as Record<string, unknown>[]).map(mapRun)
}
