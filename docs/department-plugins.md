# Department plugin contract (phase 1)

A department is a plugin registered with the avatar2 action gateway. The
gateway owns discovery and routing only; the department owns its data and rules.

## 1. What a plugin declares

`plugins.py` defines the metadata. Each plugin has a stable id, a display name,
a version, and one or more actions. Each action publishes exactly one external
tool name:

| Field | Meaning |
|---|---|
| plugin `id` | Stable department id, e.g. `hr`. Duplicate ids are rejected. |
| `display_name` / `version` | Shown in the tool catalog and recorded in traces. |
| `web_entry` | Optional department-owned page reachable from the workbench (not a sidebar menu). |
| action `id` | Stable action within the department, e.g. `preview_leave`. |
| action `tool` | External MCP tool name, e.g. `hr_preview_leave`. Must be globally unique. |
| `input_schema` | JSON Schema the model sees. |
| `policy` | `read` / `draft` / `write`. Advertised as `readOnlyHint` only for `read`; unknown policies are rejected and the AI layer denies/requires approval. |
| `handler` | Executable handler `(ActionContext, arguments) -> dict`; missing handlers are rejected. |
| `summary_fields` | Fields a generic action card may show. |

Registration fails loudly on duplicate department ids, duplicate action ids in
one department, a tool name already owned by another department, a missing
handler, or an unknown policy. The plugin is committed atomically; a failure
leaves nothing registered. There is no silent override.

## 2. How a call is routed

1. The model already discovered the tool; there is no "select a department"
   tool.
2. Both entrances — MCP `tools/call` and the internal tool call — use the same
   `execute_action` dispatcher and the same `ActionContext`.
3. `catalog.route(tool_name)` yields exactly one `(plugin, action)` pair; the
   handler runs with trusted identity from the session/binding, never from
   arguments. Unknown tools, invalid parameters and handler failures return a
   structured `{errorCode, message}` before any business side effect.
4. Identity still comes from the signed-in employee and their binding; the
   model supplies business parameters only.
5. Formal submission and approval stay manual (employee card confirmation,
   supervisor approval) and are not exposed as chat tools.

## 3. Adding the next department

Minimum changes:

1. Implement the department service and its own business rules.
2. Add a `DepartmentPlugin` entry with its actions (tool names, schemas,
   read-only flags, summary fields) in `plugins.py`.
3. Bind the handler in the gateway (or have the department expose HTTP and let
   the handler proxy to it).
4. Configure credentials/URLs in the environment.

No gateway branch per department, no workbench menu entry, and no change to the
agent loop.

## 4. Test-only plugin

`testkit_echo` exists only to prove the contract. It is registered only when
`AVATAR2_ENABLE_TEST_PLUGIN=1`:

    AVATAR2_ENABLE_TEST_PLUGIN=1   # gateway restart
    # tools/list shows testkit_echo alongside hr_*; call it for a read-only echo
    AVATAR2_ENABLE_TEST_PLUGIN=0   # restart: it disappears, HR keeps working

It is never shown when the variable is unset/0.

## 5. Tool protocol vs trace metadata

`tools/list` returns protocol-shaped definitions only. Department, action,
policy, version and summary fields are served by `GET /internal/catalog`
(service credentials). The AI layer merges them for approval decisions and
trace attribution; the model never sees vendor metadata.

## 6. Trace fields

Tool events in a trace carry `department`, `departmentName`, `action` and
`pluginVersion`, taken from the catalog at discovery time. The model is never
asked to report its department.
