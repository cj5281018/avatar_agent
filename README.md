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
model settings and test accounts. Workbench employee binding, chat tool
registration, in-chat confirmation and end-to-end verification are pending.

## Source provenance

The local source snapshots were taken from Omni HEAD
c55816d7e36cce9d47c022382af4953e63af8625 and Avatar HEAD
b87c16a7532e8c80f88c268df9498f0c99992bf4. The copy preserves the local source
contents, including any local edits. SHA-256 comparison found zero differences
between the copied AI Python files and the local source checkout.
