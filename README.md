# avatar2

This checkout contains a remote tool gateway and authenticated leave-operation APIs.
It uses the existing employee login store and the paired HR service. Run a single
gateway worker because the downstream service validates short-lived call grants
against the gateway process.

## Local startup

Use the Python environment of your existing HR deployment. Add its source
directory and this checkout to PYTHONPATH. Set AVATAR2_DATA_DIR to the employee
platform's data directory, PY_AVATAR_HR_URL to the paired HR endpoint, and
PY_AVATAR_PUBLIC_URL to the gateway's callback URL. The HR process must use that
same callback URL for pairing and identity checks. Use an isolated deployment
for trial submissions.

Run `python -m uvicorn gateway:create_app --factory --port 8790`. An administrator
can pair HR using `POST /v1/admin/hr/setup`. The gateway accepts individual
employee login bearer tokens from the existing identity service. An optional
AVATAR2_DISCOVERY_TOKEN only permits listing tools. It cannot execute them.

## Integration

- Register `/mcp` as a remote tools endpoint. Per-user OAuth integration remains
  required for the stock workbench to forward individual credentials.
- `hr_preview_leave` validates parameters without saving or submitting a request.
  A trusted Web backend must turn the preview into a card using
  `POST /sessions` and `POST /sessions/{session}/leave-drafts`.
- Confirm using
  `POST /v1/sessions/{session}/hr/leave-confirmations/{operation}/confirm`.
  Fetch card status from `GET /sessions/{session}/leave-drafts/{operation}`.
- Connect the official [annual leave regulation](https://xzfg.moj.gov.cn/front/law/detail?LawID=208)
  and [implementation rules](https://www.mohrss.gov.cn/xxgk2020/gzk/gz/202112/P020211229590352867273.pdf)
  via the workbench file connector. Configure the
  [Chinese benchmark](https://github.com/sjtu-suzhou-ai-zhiwei-tech/bank-rag-benchmark)
  in an isolated index and query a small fixed sample against its expected sources.

## Source build

The independent build uses the source directories in this checkout. Copy
.env.example to .env for a new installation and set unique database and
encryption credentials. The default local override binds port 8300. Stop another
instance listening there, or use a different port and matching APP_URL.

    docker compose -p avatar2-source --env-file .env -f docker/docker-compose.yml -f docker/avatar2.compose.yaml --profile filesystem up -d --build web connector-manager filesystem-connector gateway hr

The working tree includes the Web and AI services, searcher, indexer, connector
manager, migrations, filesystem connector, and supporting Rust workspace
members from the local upstream checkout. The remaining workspace members are
included to preserve lockfile and build resolution. Employee identity,
contract, HR, IT helpdesk and tests are under avatar/. The model runtime is
copied verbatim. The upstream license is included at the repository root.

Use /knowledge as the filesystem source path. The source build needs its own
model settings and test accounts. Employee binding, chat tool registration,
in-chat confirmation, manager approval and same-request lookups are implemented
and exercised end to end.

## Source provenance

The local source snapshots were taken from Omni HEAD
c55816d7e36cce9d47c022382af4953e63af8625 and Avatar HEAD
b87c16a7532e8c80f88c268df9498f0c99992bf4. The copy preserves the local source
contents, including any local edits. SHA-256 comparison found zero differences
between the copied AI Python files and the local source checkout.

## Per-turn trace

Every assistant attempt is stored as one run with an append-only event ledger:

- `agent_trace_runs` holds one row per attempt (chat, user message, status,
  model, last sequence and the sealed chain head).
- `agent_trace_events` holds the events of that run. Each event's digest is a
  SHA-256 over a canonical encoding that includes the previous digest, so a
  later reader can detect edited, reordered or removed events.
- Finished runs are sealed with HMAC-SHA256 (`AGENT_TRACE_SEAL_KEY`). Without a
  key traces are still recorded, and the viewer shows `unsealed` instead of
  `ok`.

Recording happens outside the agent loop: the chat router wraps the stream it
already receives, and the search and department tool handlers are decorated so
tool spans carry the operation or request identifiers they produced. A failed
trace write never fails or replays the business request.

Where to look:

- Each question in a chat has a `查看执行过程` entry; the sidebar has
  `追踪中心` (`/traces`) listing recent runs for the signed-in user.
- `GET /api/chat/{chatId}/runs` - run summaries for one chat (owner only),
  including the question text, capture health and whether any tool failed.
- `GET /api/chat/{chatId}/runs/{runId}` - events plus a recomputed
  verification result: `ok`, `mismatch`, `truncated`, `unsealed` or `running`.
  `?afterSeq=N` returns only newer events, for incremental polling.
- `GET /api/chat/{chatId}/runs/{runId}/business` - read-only link from the
  drafting turn to its leave card and latest HR status.
- `GET /api/traces` - the caller's recent runs across chats.

The panel reports three independent facts, because they can disagree:

| Fact | Values | Meaning |
|---|---|---|
| Turn execution | running / ended / failed / cancelled / stale | whether the answer finished; `stale` means running with no heartbeat for 2 minutes |
| Capture health | ok / degraded / unknown | whether every observed step was stored; once degraded, later writes cannot restore it |
| Record integrity | ok / mismatch / truncated / not_sealed / unverifiable / running | whether the stored events, the chain head and the seal still agree; `unverifiable` means sealed at write time but this verifier has no key, `not_sealed` means the record was written without a seal |

A run whose process died mid-answer keeps `status = running`; the reader uses
the last successfully written event as a heartbeat and downgrades it to
`stale`, so an old half-written run is never shown as still in progress.

Capture-health acceptance uses an explicit fixture: set
`AVATAR2_TRACE_FAULT_EVENT=model.started` (or another event type) on the AI
service and the first matching event of each process is dropped. The run must
then show `degraded` while the business result is unaffected. Leave the variable
empty in normal operation.

Verification is recomputed on read by
`web/src/lib/server/trace/digest.ts`; the writer's canonical encoding lives in
`services/ai/tracing/canonical.py`. Both suites pin the same golden vectors, so
the two implementations cannot drift silently:

    cd services/ai && python -m unittest discover -s tests/unit -p test_tracing.py -v
    cd web && npm run test:integ -- src/lib/server/trace/digest.test.ts

Operational notes:

- `AVATAR2_TRACE_ENABLED=0` disables recording without touching the chat path.
- Traces are scoped to chat ownership. A manager who can approve a leave
  request cannot read the requester's conversation traces.
- Payloads use a whitelist (model, tool, durations, counts, operation and leave
  identifiers, error codes). Leave reasons, credentials and model input are not
  recorded.
- A run left in `running` after a crash is reported as such; the viewer never
  infers that a tool succeeded and never retries a side-effecting tool.


### Acceptance scripts

`scripts/trace/` contains the re-runnable checks used for this feature. They
drive the real workbench with a test employee/manager and read the ledger
directly for fixtures:

    # real flows: plain, retrieval, two questions, draft -> approve -> business, tool failure, access control
    node scripts/trace/acceptance.cjs
    # switching runs, dropped poll recovery, polling stops at terminal
    node scripts/trace/resilience.cjs
    # tamper, truncation, missing seal key, afterSeq contract
    node scripts/trace/integrity-fixtures.cjs
    # first-snapshot/incremental recovery, cancellation, stale recovery
    node scripts/trace/resilience2.cjs
    # cursor/events/state/seal read from one snapshot (R5)
    node scripts/trace/consistency.cjs
    # step-level business association for two drafts in one run (R6)
    node scripts/trace/business-drafts.cjs
    # seal states not_sealed / running / ok / truncated in API and page (R7)
    node scripts/trace/seal-states.cjs
    # with AVATAR2_TRACE_FAULT_EVENT set on the AI service
    node scripts/trace/capture-fault.cjs turn
    node scripts/trace/capture-fault.cjs check   # after a restart, with FAULT_RUN_ID/FAULT_CHAT_ID

`scripts/trace/fixtures.py` is piped into the AI container
(`docker exec -i avatar2-source-ai-1 python - <subcommand>`) to create
controlled runs: two real drafts in one run with a decoy operation id, a
running run, a stale run, a record written without a seal, a malformed-frame
run, and a leak-marker sanitisation run. These are test records and are labelled
as such by their fixture model name.

Note: the HR service de-duplicates by requester + dates + leave type + reason,
so acceptance runs must use dates that were not used before; otherwise HR
returns the existing request and the new operation is linked to it.

They need Node with Playwright (`NODE_PATH` to a directory containing
`playwright-core`) and these variables: `BASE`, `MODEL_ID`,
`BOB_EMAIL/PASSWORD`, `ALICE_EMAIL/PASSWORD`, `ADMIN_EMAIL/PASSWORD`. The
scripts never print or store credentials, and the damaged runs they create are
clearly labelled test records.
