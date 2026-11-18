# Avatar 可插拔企业 Agent 平台

Avatar 是面向企业员工的内部业务助手。Avatar 的设计理念是“万物皆插件”：每个部门把自己的业务能力作为独立服务接入，员工通过同一个助手调用自己有权使用的功能。当前已接入 HR 服务。

## 部门服务如何接入

部门通过版本化 Manifest 声明工具、参数和确认要求。Avatar 与部门服务配对并核验清单后，按员工身份提供可用工具；部门负责业务授权、执行和数据存储。

## 平台架构

员工通过 Avatar Client 与自己的助手对话。Avatar Agent Server 通过 ACP 管理 Hermes 会话，并将已配对部门服务的工具提供给助手。部门服务负责执行业务规则和保存业务记录。

| 组件 | 在一次办事请求中的职责 |
| --- | --- |
| Avatar Client | 独立的 Electron 桌面客户端，通过 HTTP 连接服务端，展示对话、工具活动和确认卡。客户端单独维护。 |
| Avatar Agent Server | 企业侧的控制层。Python 实现位于 `avatar_service/`，负责身份、会话、部门服务配对和操作确认，通过 ACP 启动并管理 Hermes。 |
| Hermes Agent | 运行 Agent 会话，通过平台提供的 stdio MCP 工具发现和调用业务能力。 |
| 部门服务 | 独立运行并维护自己的业务数据。本仓库的 `hr_service/` 实现了请假审批、招聘以及 Offer 到入职流程。 |

## 已落地的 HR 业务

| 场景 | 业务流程 |
| --- | --- |
| 请假与审批 | 员工查询审批人、提交或撤销申请；审批人查看待办、同意或拒绝，员工可以查询处理结果。 |
| 招聘 | 招聘人员按岗位权限查应聘记录、生成 JD 草稿和周报，并通过确认卡推进候选人阶段。 |
| Offer 与入职演示 | 登记 Offer 发出及候选人回复，接受后生成入职清单，支持改期、报到、办理待办和结案。 |

例如，招聘人员要求把某条应聘记录从“待筛选”推进到“面试中”。Avatar 先展示待确认的单号、原阶段和目标阶段。用户确认后，HR 再检查岗位权限和记录版本，将阶段变化与操作回执写入同一个 SQLite 事务。相同操作重复提交时返回已有结果。

## 代码结构

```text
avatar_agent/
├── avatar_service/          # Agent 会话与部门工具调用
│   ├── hermes.py            # 通过 ACP 管理 Hermes 会话
│   ├── services.py          # 部门服务接入与工具调用
│   └── operations.py        # 操作确认与执行记录
├── hr_service/              # HR 业务服务
│   ├── leaves.py            # 请假申请与审批
│   ├── recruitment.py       # 招聘查询与阶段推进
│   ├── lifecycle.py         # Offer 与入职流程
│   └── public/              # HR 管理网页
├── avatar_contract/         # 服务间共用的契约校验
├── deploy/                  # 启动脚本与配对配置
├── tests/                   # 自动化测试
├── docs/                    # 部署指南与架构说明
├── Dockerfile               # 构建服务镜像
└── compose.yaml             # 启动 Avatar 和 HR 服务
```

## 使用 Docker Compose 启动

需要 Docker 和 Docker Compose。在仓库根目录将 `.env.example` 复制为 `.env`。如需通过 Hermes 进行模型对话，按模板填写模型提供方和凭据。要体验虚构岗位、候选人、Offer 和入职流程，请在首次启动演示环境前设置 `HR_RECRUITMENT_DEMO=1`。

```sh
docker compose up -d --build --wait
docker compose ps
curl http://127.0.0.1:18788/health
```

打开 [HR 管理页面](http://127.0.0.1:18891/) 即可访问随 HR 服务提供的网页。运行 `docker compose exec hr /opt/avatar-venv/bin/python -m hr_service set-admin-password` 设置后台密码。两个端口均仅绑定本机。若端口已被占用，在 `.env` 中修改 `AVATAR_HOST_PORT` 或 `HR_HOST_PORT`，并在访问地址中使用对应端口。

首次部署后，按顺序初始化模拟账号、获取 HR 配对码并完成配对。第三条命令会提示输入管理员密码和配对码。

```sh
docker compose exec avatar /opt/avatar-venv/bin/python -m avatar_service init-demo
docker compose exec hr /opt/avatar-venv/bin/python -m hr_service setup-code
docker compose exec avatar /opt/avatar-venv/bin/python /app/deploy/pair_hr.py
```

员工聊天需要另行获取 Avatar Client。登录地址填写 `http://127.0.0.1:18788`。数据库、会话及密钥保存在 Docker 命名卷中。[部署与配对指南](docs/AVATAR_STEP5.md)

## 体验账号与登录入口

| 想体验什么 | 在哪里登录 | 账号和密码 |
| --- | --- | --- |
| 员工请假、查看自己的申请 | Avatar Client，服务地址填 Avatar 的地址 | 用户名 `marco`；使用首次运行 `init-demo` 时打印的员工密码。 |
| Avatar 管理与 HR 配对 | Avatar Client 或配对脚本 | 用户名 `admin`；使用同次初始化打印的管理员密码。 |
| HR 网页管理 | 浏览器打开 HR 服务根地址 | 用户名 `admin`；密码由本机运行 `set-admin-password` 的人设置，与 Avatar 管理员密码独立。 |
| 招聘、Offer 与入职 | Avatar Client | 启用招聘演示数据后，另建用户 `bob`，可操作模拟岗位 `JOB-PY`。 |

请在初始化时保存终端打印的随机密码。再次执行 `init-demo` 会保留已有账号及密码。HR 配对码仅用于首次连接两个服务。Docker 用户可运行以下命令创建招聘演示账号，并按提示设置自己的密码：

```sh
docker compose exec avatar /opt/avatar-venv/bin/python -m avatar_service add-demo-user --handle bob --name Bob
```

## 从 Python 源码启动

本地开发可以直接启动两个 FastAPI 服务。以下为 Windows PowerShell 示例；每个终端都从仓库根目录开始。首次安装使用 Python 3.12：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**终端一，启动 HR 和网页。** 首次设置网页密码并记下配对码。网页由 HR 服务同进程提供，打开 `http://127.0.0.1:8891/` 即可登录。

```powershell
$env:HR_AVATAR_BASE_URL = 'http://127.0.0.1:8788'
$env:HR_PUBLIC_URL = 'http://127.0.0.1:8891'
$env:HR_RECRUITMENT_DEMO = '1'
.\.venv\Scripts\python.exe -m hr_service set-admin-password
.\.venv\Scripts\python.exe -m hr_service setup-code
.\.venv\Scripts\python.exe -m uvicorn hr_service.app:app --host 127.0.0.1 --port 8891
```

**终端二，启动 Avatar。** 首次初始化时保存两个账号的密码。桌面客户端的服务地址填 `http://127.0.0.1:8788`。

```powershell
$env:PY_AVATAR_PUBLIC_URL = 'http://127.0.0.1:8788'
$env:PY_AVATAR_HR_URL = 'http://127.0.0.1:8891'
.\.venv\Scripts\python.exe -m avatar_service init-demo
.\.venv\Scripts\python.exe -m uvicorn avatar_service.app:app --host 127.0.0.1 --port 8788
```

**终端三，首次配对。** 输入 Avatar 管理员密码和终端一的 HR 配对码。

```powershell
.\.venv\Scripts\python.exe deploy/pair_hr.py
```

以上命令可体验登录、HR 网页、配对和模拟回复。要通过自然语言实际调用部门工具，建议使用上面的 Docker 方式并配置模型凭据；源码模式需先安装支持 ACP 的 Hermes，在启动 Avatar 的终端设置 `PY_AVATAR_HERMES_BIN` 和对应的模型环境变量。本地源码启动时，直接在终端设置环境变量，Compose 的 `.env` 不会自动加载。

## 运行测试

从仓库根目录用 Python 3.12 运行 `python -m venv .venv`。Linux 或 macOS 使用 `source .venv/bin/activate` 激活环境，Windows PowerShell 使用 `.\.venv\Scripts\Activate.ps1`。测试使用临时数据库和模拟回调，可以独立于桌面客户端和模型 API 运行。

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```
