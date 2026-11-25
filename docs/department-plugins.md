# 部门插件接入说明

Avatar2 的动作网关负责发现、路由和执行前校验。部门服务维护自己的业务规则、权限和数据。HR 是当前接入的真实部门。

## 声明部门和动作

在 `plugins.py` 的契约基础上定义部门插件。每个部门包含稳定标识、显示名称、版本，以及一组动作。各动作对外使用全局唯一的工具名。

| 字段 | 用途 |
| --- | --- |
| 部门 `id`、`display_name`、`version` | 标识部门，供工具目录和 Trace 使用。 |
| `web_entry` | 可选的独立部门网页入口，按需从工作台打开。 |
| 动作 `id`、`tool` | 部门内动作标识和对外工具名，例如 `hr.preview_leave` 对应 `hr_preview_leave`。 |
| `input_schema` | 模型调用工具时使用的 JSON Schema。 |
| `policy` | `read` 表示读取，`draft` 表示起草，`write` 表示正式写入。普通工具入口仅允许读取和起草。 |
| `handler` | 执行动作的处理函数，接收 `ActionContext` 和业务参数。 |
| `summary_fields`、`describe` | 可选的摘要字段和描述适配器，供工作台通用动作卡使用。 |

注册时会检查重复部门、重复动作、重复工具名、缺失处理函数及未知策略。登记失败时整份插件不会注册。

## 从 Agent 调用到部门办理

1. Agent 发现工具后按工具名调用。MCP 的 `tools/call` 与工作台内部工具入口共用 `execute_action`，根据注册记录定位部门与动作。
2. 网关从会话及员工绑定取得调用身份。模型提供日期、假别等业务参数。未知工具、非法参数和执行失败通过结构化错误返回。
3. 以请假为例，Agent 起草后工作台展示确认卡。员工确认提交，指定主管在独立 HR 网页审批，HR 服务保管正式单据和状态。
4. 工作台读取卡片描述时，网关根据操作记录核对用户、聊天、部门和动作，再调用对应的 `describe`。员工可从原卡和 Trace 回查同一单据。

## 接入新部门

1. 实现部门服务及业务权限。
2. 参考 [示例插件](../plugins_sample.py) 定义 `DepartmentPlugin`，为动作配置工具参数、策略及处理函数。需要动作卡时，同时登记描述适配器。
3. 在网关中注册插件，按部署需要配置服务地址和凭据。
4. 在隔离测试环境验证工具发现、调用、失败反馈和卡片描述。可临时启用 `AVATAR2_ENABLE_SAMPLE_PLUGIN=1` 或 `AVATAR2_ENABLE_TEST_PLUGIN=1`，重启网关验证，测试结束后关闭。

## 工具目录与 Trace

`tools/list` 返回 MCP 工具定义。`GET /internal/catalog` 提供部门、动作、版本和策略等元数据，仅供持有服务凭据的接入层使用。Trace 从注册记录取得 `department`、`departmentName`、`action` 和 `pluginVersion`，用于追踪调用归属。
