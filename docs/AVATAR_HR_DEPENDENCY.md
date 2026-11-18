# 架构复盘：从互相复制源码到独立部署 Avatar 和 HR

## 背景与案例

Avatar 负责登录身份、工具发现、确认卡和调用追踪；HR 负责岗位授权、招聘阶段和业务记录。用例取自测试中的虚构招聘专员 Bob 和应聘单 APP-01。下面把已有的业务测试与版本兼容规则串成一个场景，用来说明重构必须保住哪些行为。

1. Bob 查询“小林”。HR 根据岗位授权只返回 Bob 有权查看的 APP-01。同名候选人可能对应多条应聘记录，因此后续写入绑定应聘单编号。
2. Bob 要求把 APP-01 从“待筛选”推进到“面试中”，理由为“初筛完成”。Avatar 根据动作 recruitment.advance 创建待确认卡，保存申请参数、操作者、服务配对标识、契约版本和摘要。此时 HR 的招聘阶段还没有变化。
3. 假设卡片生成时使用 HR 契约 1.0.0，员工点确认前 HR 升到 1.1.0，增加了 Offer 和入职动作。整份工具清单的摘要随之变化，但 recruitment.advance 的定义保持一致。
4. 重构前，Avatar 确认时直接调用 HR 源码里的 allows_previous。它只认可特定旧指纹、指定动作且动作定义一致的组合。Avatar 还要校验当前配对、登录者和动作权限。HR 收到调用也会复核契约、岗位权限和业务状态。
5. 条件满足时，HR 记录阶段变化和操作结果。同一操作在重启后重试会拿到原结果；拿旧记录版本发起一个新的推进操作会得到 409。升级兼容、业务授权和重复提交保护分别解决三个不同的问题。

## 重构前的依赖和影响

| 导入方 | 被导入的内容 | 原因与影响 |
| --- | --- | --- |
| Avatar 的 services.py | hr_service.registry 的 ACTIONS、MANIFEST、FINGERPRINT | 平台使用 HR 的 Python 对象构造内置 HR 服务。registry 又导入招聘和入职模块，把业务实现带入了平台进程。 |
| Avatar 的 operations.py | hr_service.registry.allows_previous | 通用确认流程写入了 HR 专属的旧卡兼容判断。 |
| HR 的 app.py 和 registry.py | avatar_service.service_contract 的 schema_error、manifest_digest | HR 接受输入和生成契约摘要时需要平台目录中的通用工具。 |

这是服务包之间的双向源码依赖。Python 不一定报循环导入错误：两个服务都在同一源码树中时，上述模块可以正常加载。拆成镜像后，若每个镜像只复制本服务目录，HR 将找不到 avatar_service.service_contract，Avatar 将找不到 hr_service.registry。即使双方的 HTTP 地址都正确，导入依赖仍要在应用启动阶段满足。

## 两轮迭代的实际改动

第一轮先补齐镜像中缺少的文件。当时本地 35 项测试通过，两个镜像仍依赖对方的源码。第二轮改变了依赖方向。

- [公共协议包](../avatar_contract/__init__.py)承载输入校验、清单校验和摘要计算。Avatar 和 HR 的契约调用方直接导入该包。历史摘要算法保持一致。
- [HR 注册表](../hr_service/registry.py)负责动作分发和语义兼容。HR 通过 GET /manifest 提供工具定义，发布时运行 `python -m hr_service release-config` 导出受信配置，包括当前版本、清单摘要、允许的动作及旧卡兼容范围。
- [Avatar 服务目录](../avatar_service/services.py)从已配对的 HR 获取清单，按部署方批准的版本和摘要核验后读取工具定义。[确认流程](../avatar_service/operations.py)检查配对、操作者和动作权限；版本变化时，还要求旧版本、旧清单摘要、动作名和已验证的当前动作定义摘要同时匹配。HR 执行前独立复核业务授权。
- [镜像构建](../Dockerfile)分别复制自身代码和公共协议包。Avatar 附带默认的[发布配置](../deploy/hr-contract.json)。Compose 使用只读挂载，可通过 AVATAR_HR_CONTRACT_PATH 指定宿主机上的受信配置文件。以后升级 HR 时，审核导出的配置并重新创建 Avatar 容器即可加载，无需重新构建 Avatar 镜像。

旧数据库保存了卡片指纹，没有完整历史清单。因此 HR 在发布时比较新旧动作定义，导出认可的旧指纹及旧动作摘要，Avatar 在确认时核对。这使已发出的卡片无需数据库迁移，也保留了部署方批准契约变更的环节。1.1.0 的清单内容及摘要保持原值。

## 实测结果（2026-11-18）

| 验证 | 方法和结果 |
| --- | --- |
| HR 独立启动 | 构建 Linux 镜像，以默认入口和 Docker 的 --network none 启动。从容器内请求 8891 的 /health 得到 HTTP 200、ok=true、paired=false。Python 能找到 avatar_contract，找不到 avatar_service。 |
| Avatar 独立启动 | 停止 HR 后单独启动 Avatar，从容器内请求 8788 的 /health 得到 HTTP 200、ok=true，Python 找不到 hr_service。Avatar 使用 mock 模式。镜像构建包含 Hermes ACP 自检，本次没有调用真实模型。 |
| 旧卡跨版本执行 | [新增回归测试](../tests/test_hr_upgrade.py)使用 1.0.0 的历史清单和固定发布指纹，通过工具路由生成 APP-01 的招聘推进卡。重启两个 FastAPI 应用并沿用数据库和配对；升级到 1.1.0 后，移除兼容许可时确认返回 409，恢复许可后同一卡连续确认两次均返回 succeeded。数据库只新增一条“面试中”事件。 |
| 验证范围 | 旧卡测试使用真实 ASGI 路由、身份回调、SQLite 事务和进程内传输。测试账号沿用 Marco 登录夹具并授予 JOB-PY 权限。复现了历史契约，未运行历史二进制或 Electron 界面。 |
| 完整回归 | 运行 `python -B -m unittest discover -s tests -q`，37 项全部通过，耗时 17.907 秒。Windows asyncio 仍报告一条未关闭事件循环的 ResourceWarning。Compose 配置校验通过。 |

## 面试表达

“从一张升级前生成的招聘确认卡，我发现平台直接导入了 HR 注册表，HR 又从平台导入参数校验工具。我先补齐镜像里缺失的代码，再把通用协议抽出，让 HR 发布动作清单和兼容范围。验收时把两个镜像分别断网启动，两边健康检查都返回 200，镜像内也确实找不到对方的包。我还验证了升级前的同一张卡：撤掉兼容许可会拒绝，恢复许可后连续确认两次，数据库只推进一次。”
