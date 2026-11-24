import type { TraceRunSummary } from '$lib/server/db/trace'

/**
 * A run that was never finalised because the process died keeps `status =
 * 'running'`. Its heartbeat (the last successfully written event) is the only
 * evidence we have, so the reader downgrades it to `stale` instead of showing
 * an answer that is still "in progress" hours later.
 */
export const STALE_HEARTBEAT_MS = 120_000

export type ExecutionState = 'running' | 'stale' | 'completed' | 'failed' | 'cancelled' | 'interrupted'

export function executionState(run: TraceRunSummary, now = Date.now()): ExecutionState {
    if (run.status !== 'running') return run.status as ExecutionState
    const heartbeat = run.lastEventAt ?? run.startedAt
    return now - new Date(heartbeat).getTime() > STALE_HEARTBEAT_MS ? 'stale' : 'running'
}

export function isTerminal(state: ExecutionState): boolean {
    return state !== 'running'
}

export function iso(value: Date | string | null): string | null {
    if (!value) return null
    return new Date(value).toISOString()
}

export function wireRun(run: TraceRunSummary, now = Date.now()) {
    const state = executionState(run, now)
    return {
        runId: run.runId,
        chatId: run.chatId,
        chatTitle: run.chatTitle,
        userMessageId: run.userMessageId,
        messageText: run.messageText?.trim() || null,
        attempt: run.attempt,
        status: run.status,
        executionState: state,
        terminal: isTerminal(state),
        modelName: run.modelName,
        errorCode: run.errorCode,
        captureStatus: run.captureStatus,
        captureError: run.captureError,
        droppedEvents: run.droppedEvents,
        toolError: run.toolError,
        steps: run.steps,
        startedAt: iso(run.startedAt),
        endedAt: iso(run.endedAt),
        lastEventAt: iso(run.lastEventAt),
        durationMs:
            run.endedAt === null
                ? null
                : Math.max(0, new Date(run.endedAt).getTime() - new Date(run.startedAt).getTime()),
    }
}
