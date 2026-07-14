# COMPLETED

本文件不再保存历史流水。当前完成项以 git 历史和模块 `02-progress.md` 为准。

最近收口重点：

- 当前 owner home 路径成为唯一默认运行路径。
- 文件大小硬门已改成报告提示。
- 多个旧转发层和历史路径模块已删除。
- owner-scoped 前后台 shell 已改为 bwrap fail-closed，并由 worker/K8s 复用真实 readiness 自检。
- 默认一键安装已进入透明容器 CLI；工作树与 wheel/tar 发布干净度使用同一结构化检查器。
- P0 收敛已完成本地验收：产品事实页、根目录 pytest、配置同步、Ruff、真实 blocker/advisory 报告语义、MCP effect 硬门、sandbox fail-closed 与未跟踪运行数据检查均已闭环；发布状态仍以 `docs/PRODUCT_FACTS.md` 为准。
- P1 主链收敛已完成本地验收：完整 import 矩阵进入 CI，生产 wheel 剥离测试/offline harness，默认 gateway/正式入口/显式插件链收口，真实 embedding 工具检索、POSIX PTY、stdio LSP 与 OpenAI native tools 已接主链；真实生态与规模承诺仍按 `docs/PRODUCT_FACTS.md` 的部分可用/实验性边界描述。
- P2 scale 主链接线已在当前工作树完成：显式 fail-closed profile、PG/ASGI/RLS、Redis 共享准入、OTLP、独立在线迁移 Job、真实 Agent worker 和 continuous-monitor proof 机制均已接线并做本机真依赖 smoke；十万用户、目标集群灰度和 24 小时真实异构来源仍未证明。
- 普通 Feishu 对话与工作主链已收口：真实 chat/topic 多轮 transcript、跨会话隔离、同会话顺序执行、结构化任务选择/提升/完成、内置默认 prompt、USER 自主画像与 SOUL/AGENTS 卡片确认、首条消息不被密码 onboarding 吞掉、长任务异步可恢复回送均已落地；scale worker 复用同一执行链。
- 普通会话累计上下文已接入 owner/thread scope：复用现有 compact 阈值、token 估算和模型后端生成 thread summary，raw transcript 保留；旧聊天进入 owner-local `session_search` 索引。`/verbose off|on|full` 及 typed 工具进度复用持久化回送链，不重提任务。
- 多 IM 投递底座已在当前工作树收敛：普通最终回复、后台主动消息和显式 `send_message` 共用 `DeliveryService`；收件上下文与回复信封分离，adapter/capabilities/target validator 统一注册，第二个 fake IM 契约无需修改投递主流程即可接入。生产第二平台与正式部署复验仍按产品事实页标注。
- 普通会话即时控制已在当前工作树收敛：CLI/Feishu 共用 `/status`、当前 request 一次性 `/btw` 和 `/stop`；控制入口绕过普通同会话队列，stop 持久化后中断主工具循环、前台命令与子代理，旧永久 `/btw` 注入和 `/btw-clear` 已移除。真实 1.10 Feishu 长任务复验仍按产品事实页标注。
- root 部署的 owner 路径边界已收紧：宿主 home 危险根豁免只给无 owner scope 的本地管理员，远程 Feishu owner 保留 `/root` 拒绝边界，同时继续以精确 owner home 白名单访问自己的数据；Linux root 场景已加入确定性回归。
