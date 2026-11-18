# 第 5 步 · Linux Docker Compose 试点

Compose 启动两个容器。Avatar 使用固定版本的 Hermes 镜像 v0.23.1，在容器内启动 ACP 子进程。服务间通信使用 Compose 网络。宿主机通过 127.0.0.1:18788 访问 Avatar，通过 127.0.0.1:18891 访问 HR 管理页面。两个命名卷分别保存 Avatar 账户、会话、Receiver 投递、Hermes 目录和 HR 数据、加密密钥。首次启动使用新模拟数据。此前 Windows 的数据库和密码不需要上传。

已有部署升级时，可先用 `docker compose ls` 查看当前项目名，再将 `COMPOSE_PROJECT_NAME` 设为该名称，继续使用已有命名卷。沿用外部模型或自定义契约路径时，在部署环境中配置 `AVATAR_INTERNAL_LLM_API_KEY` 和 `AVATAR_HR_CONTRACT_PATH` 的对应值。

## 1. 从 Windows 上传部署文件

在 PowerShell 中替换 SSH 用户名和主机名。打包只包含源码和部署文件。

```powershell
# 从仓库根目录执行
$server = '你的用户名@实验室服务器'
$archive = Join-Path $env:TEMP 'avatar-pilot-source.tar.gz'
tar --exclude='__pycache__' -czf $archive compose.yaml Dockerfile .dockerignore .env.example docs/AVATAR_STEP5.md requirements.txt avatar_contract avatar_service hr_service deploy
if ($LASTEXITCODE -ne 0) { throw '打包失败' }
scp $archive "${server}:avatar-pilot-source.tar.gz"
ssh $server
```

## 2. 在 Linux 构建和启动

需要 Docker Engine 和 Compose 插件。构建时需要能访问镜像仓库和 Python 软件源，运行时 Avatar 容器需要能访问所选模型的 API。启动前确认 18788 未被其他服务占用。

```bash
mkdir -p ~/avatar-pilot
tar -xzf ~/avatar-pilot-source.tar.gz -C ~/avatar-pilot
cd ~/avatar-pilot

docker compose version
(umask 077; cp -n .env.example .env)
chmod 600 .env
nano .env
```

云端部署使用 `PY_AVATAR_MODEL_PROVIDER=deepseek` 和 `DEEPSEEK_API_KEY`。接公司内网模型时，设置 `PY_AVATAR_MODEL_PROVIDER=custom`，并填写 `PY_AVATAR_MODEL`（推理服务发布的模型 ID）、`PY_AVATAR_MODEL_BASE_URL`（例如容器可访问的 `https://llm.company.internal/v1`）和 `AVATAR_INTERNAL_LLM_API_KEY`（免鉴权端点可留空）。凭据由部署环境注入，Hermes 配置只保存凭据变量名。内网推理服务需要支持 OpenAI 兼容的 Chat Completions 及工具调用。

更新配置后重新部署 Avatar。切回云端时把 provider 和模型 ID 改回云端值，并清空 `PY_AVATAR_MODEL_BASE_URL`。显式设置 provider 后，普通用户和 HR Receiver 下一次启动 Hermes 时都会应用部署的模型配置。模型路由变化会开启新的 Hermes ACP 会话，原 Avatar 会话、消息和业务记录依旧可查。直接启动 Python Avatar 时可在启动它的终端设置同名环境变量。若使用常驻 Windows Hermes Gateway，更新环境后也要重启对应 Gateway 以加载新凭据。

Compose 配置可以用 `docker compose config --quiet` 校验。

```bash
docker compose up -d --build --wait
docker compose ps
curl -fsS http://127.0.0.1:18788/health
```

构建 Avatar 时会执行 Hermes ACP 自检。HTTP 健康检查确认应用可访问，`hermesAcpReady` 会在首次成功生成模型回复后变为 true。

### 初始化账号并配对 HR

```bash
docker compose exec avatar /opt/avatar-venv/bin/python -m avatar_service init-demo
docker compose exec hr /opt/avatar-venv/bin/python -m hr_service setup-code
docker compose exec avatar /opt/avatar-venv/bin/python /app/deploy/pair_hr.py
```

保存首次打印的 `admin` 和 `marco` 密码。用上面的配对脚本输入新生成的管理员密码与 Setup Code，成功时会显示 `Paired: hr-leave-python`。这三条命令用于首次部署。重建镜像或重启容器时原配对仍在卷里。

## 3. 用 Windows 桌面端验收

另开一个 PowerShell 窗口并保持运行：

```powershell
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:18788:127.0.0.1:18788 你的用户名@实验室服务器
```

可以在另一窗口用 `Invoke-RestMethod http://127.0.0.1:18788/health` 验证隧道。打开原 Avatar 桌面端，登录地址填 `http://127.0.0.1:18788`，使用本次 Linux 部署生成的账号密码。

1. 用 Marco 登录，先发一条普通消息，看是否有真实模型的流式回复。
2. 新建会话发送「请通过 hr-leave-python 提交 2026-12-28 至 2026-12-29 的模拟年假，事由是 Linux Compose 验收。我确认提交，请只提交一次并告诉我正式单号。」记下单号，等待管理员收到通知。若工具返回 queued，表示后台仍在处理。
3. 切换到 admin，在收到的 Receiver 会话中发送「我明确同意请假单 <实际单号>，请通过 hr-leave-python 正式批准」。
4. 切回 Marco，在原会话确认收到回执，再用 `list_my_requests` 按单号查询。正式结果应为 `approved`，审批人为 `admin`。
5. 在服务器检查两个方向的投递日志。替换以下命令中的单号，两行都应为 `mode=receiver`。

```bash
docker compose logs --no-color avatar | grep -F 'HR leave_实际单号 '
```

第一轮使用新卷，可以覆盖两个 Receiver 首次启动。验收完等两条投递都落库，再执行 `docker compose restart`。重新登录检查原会话、通知和 HR 单据，再发一轮聊天，确认持久化和重启后的可用性。停止 SSH 转发后，本机 18788 应不可访问；重新启动转发后恢复。
