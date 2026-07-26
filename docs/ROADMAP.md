# ROADMAP

状态标记：

```text
设计中       已形成方向，还没写代码
部分落地     已有基础结构，但还没完整接入运行链路
待验证       已写代码，但还需要真实场景验证
暂停         暂时不做，但保留背景
```

每条记录必须包含"解决问题"，说明它解决哪个用户痛点、系统风险或交付缺口。

更新时机：开工前从这里领取任务，收工后更新状态或移到 COMPLETED.md。

---

## 下一版优先级

来自 STATUS.md，当前最急迫的任务已清空，下面保留中长期项。

### Agent 基础能力单一主链收敛

状态：待验证

解决问题：能力自述、Shared/Skill、Memory/Persona、Scheduler、Workflow 和验证证据存在重复事实源或
主链接入不完整，导致模型能力与真实运行状态漂移，并增加多用户串权和长任务不可靠风险。

当前进展：用户已确认 `docs/design/FEATURE-20260718-agent-foundation-convergence.md`；owner/channel
registry、能力自述、Skill 单一逐轮 snapshot、Memory 稳定 ID CRUD/batch、Persona 单一 repository/
版本/CAS/回滚、owner-scoped 持久 Scheduler，以及 Workflow 收敛为 Skill + 当前 task plan + 原生
tools/subagents 均已完成聚焦验证；旧 workflow package/mode/config/CLI/extension/index 已删除。
被动验证证据也已接入公共工具出口，能保留真实命令/exit/targeted/full 并在文件写后过期。
工具参数也已收敛到单一 ToolSpec Schema：provider 展示、文本/native 入口、MCP、运行门和 handler
前校验不再各保留 required/type 副本；保守强类型纠正、嵌套/枚举/范围/额外字段校验及协议同名参数
碰撞已完成本地回归，并由本地 8899 与 MiniMax-M2.7 的隔离工具失败恢复链验证。当前后续切片又把
缺失参数限制为逐字段明示的安全默认值或 Registry 可信上下文绑定，并为每个有效输入保留脱敏
`source/source_ref`；旧 `read_artifact` 运行身份和进程工作目录专项补参已删除，待完整门禁、模型、
1.10 与正式双用户复验后发布。
外部多写已继续复用唯一 operation store，没有新增 Saga 或自动回滚。提供方结果只有在权威终态保存
成功后才能作为成功返回；保存失败降级为 unknown，结构化状态会穿过 archive、control-plane event 与
compact，语义摘要另保留中段非成功副作用事实。完整门禁、本地 8899 基础 CLI、MiniMax 长链/极端
CLI、1.10 双 Feishu owner scope 与真实出站均已通过；新的桌面客户端入站仍按产品事实页保留为外部
验收边界。
极端 MiniMax CLI 发现并删除了旧“绝对路径写飞后静默搬进 task output”兼容层。显式绝对路径现在保留
原目标身份，由唯一写边界返回明确成功或 `WRITE_FORBIDDEN`；相对 `output/`、`work/` 任务落位不变。
修复后的真实链已在 CLI 和 1.10 正式 owner scope 验证“成功—拒绝—继续成功”及模型准确部分结果。
`max_active_agents` 与结构化 owner 写入口已接同一 quota lock；Gateway 已按有界 owner page 自动执行
基于结构化终态、二次校验、trash tombstone、legal hold 和审计的 retention。应用门不能覆盖任意
Shell/PTY/LSP 进程写盘，正式规模部署仍需 filesystem/project quota。完整本地 CI、严格 code-size、
本轮 Schema 改动现已通过完整 pytest、Ruff、import/offline/code-size/doc-sync、compile、diff、
distribution boundary 和干净 wheel artifact gate。未跟踪运行数据继续由 worktree clean-package
正确阻断且不得进入 wheel。Schema 这一切片已随精确提交进入远程 `main`，部署 1.10 唯一正式
Gateway/Feishu，并完成本地 8899、MiniMax-M2.7 和两个既有真实飞书 owner 的只读工具调用复验；
更大范围的基础能力长稳、真实群组和规模验证仍属于本路线项。

验收：完整本地 CI 通过，推送远端 main，部署 1.10，并完成既有飞书双用户、多长任务真实 LLM 标准。

补充记录：2026-05-05 已完成一轮 CI 回归修复，解决 gateway 测试互相污染、日志证据测试缺导入、runner 解析失败结果写回不一致等问题；细节已移入 `docs/COMPLETED.md`。

---

## 设计中（未开工）

### 用户真实痛点：Subagent 假完成与失控

状态：设计中

解决问题：Fake Done、父会话放养、子代理静默/卡死、输出不落盘、缺独立验收层等系统性问题。

待做：自动周期 due-check、自动心跳、真实入口验收、URL 来源核查、截图/日志证据自动执行器。

设计台账：DESIGN_LEDGER.md "用户真实痛点：Subagent 假完成与失控"

### 注释重构规范

状态：设计已记录，待分阶段落地

解决问题：大量函数 docstring 是一句话说明，缺少 LLM/人类双层说明，新接手的 AI 或开发者难以理解副作用和边界。

待做：按模块分阶段补齐双层注释（config → tools → core → CLI → subagent）。

设计台账：DESIGN_LEDGER.md "注释重构规范"

---

## 部分落地（有骨架但未完整接通）

### Subagent Channel Probe

状态：部分落地

解决问题：通道故障和任务失败必须分开判断，接管前先确认 workdir、机器 JSON、写入现场是否正常。

已有：本地工单现场 probe、channel_status 状态机、CLI 命令。

待做：模型接口 probe、ACP/adapter/session probe、派工前自动 probe 和失败阻断。

### Grant 注入执行上下文

状态：部分落地，grant 后自动唤醒已落地

解决问题：capability grant 不能只停留在运行记录里，必须能变成子代理实际可读取的执行包。

已有：`SubAgentExecutionContext`、`build_execution_context()`、`write_execution_context()`、CLI 命令；capability grant 后会创建 observation/wake signal，并把 `capability_grant_wake` 写回 task。

待做：按 token 预算裁剪 execution context；子代理写 capability request 统一入口继续精简。

### 层级能力上抛

状态：部分落地，授权后下发唤醒已落地

解决问题：子代理遇到问题不应自己全局搜索 skill/tool，应描述能力缺口，由父代理发现和下发。

已有：`CapabilityRequest`/`CapabilityGrant`/`CapabilityGap`、单个子代理 runner 入口、capability request 路由、grant 后 wake signal 和同 run follow-up。

待做：跨多层级的批量上抛汇总；tool failure mode 和显式 alternative group 参与排序。

### 本地恢复、诊断、worker 和 adapter 第一版

状态：部分落地，runner session heartbeat 已落地

解决问题：LocalStore 一致性诊断、gateway 请求崩溃恢复、adapter 文件协议。

已有：`local-doctor`、`local-rebuild`、gateway failed 归档、processing lease、保守 worker pool、runner session heartbeat 账本、`adapter file`、启动恢复结构化检测错误、后台 dispatch 启动标记错误报告；普通回复/主动消息/附件已共用 DeliveryService 与 adapter registry；当前会话 `/status`、单次 `/btw` 引导和 `/stop` 请求取消已接入 CLI/IM 共用控制入口。普通派工/等待/完成新增瘦身 LLM 回复轮与最终 delivery snapshot；后台 claim 具备 90 秒 fail-safe 和同进程域死 owner 立即接管，Gateway signal shutdown 有 typed forensics。

待做：LocalStore compact/backup/export、gateway 请求优先级、独立子进程隔离版 runner worker、adapter HTTP/WebSocket 版，以及第二个生产 IM 的真实 API/媒体/重启复验；会话控制仍待 1.10 真实 Feishu 长任务复验。

### Runtime 配置层与错误报告全链路

状态：部分落地，子代理 run/task overlay 与主要 best-effort 结构化已落地

解决问题：运行时 overlay、owner/task/run scoped 配置、错误报告和恢复摘要必须成为一条真实链路，不能只停在合同测试或局部 helper。

已有：基础配置来源链、`RuntimeConfigLayer`、CLI runtime overlay 环境入口、子代理 `config_overlay_ref` 创建继承/接管记录/worker 装载、后台启动和启动恢复错误可见化、gateway/audit 旁路错误结构化。

待做：远端 session/ACP 入口接入 scoped config；继续扩展少数低频 gateway adapter/supervisor 旁路异常的统一持久化。

### 可见真实环境测试台 Live Lab

状态：部分落地

解决问题：开发 agent 不能只靠单元测试，需要能在用户看得见的终端里跑真实 runtime。

已有：`live_agent_lab.py`、隔离配置、命令执行、transcript 和 summary、`open_live_lab.sh`。

待做：memory 长任务、tools 边界任务、问题任务和多轮恢复任务场景。

### 并行开发 Workstream 工作台

状态：部分落地

解决问题：多个 AI 可以并行，但必须先把目录、分支、职责边界和交接格式定清楚。

已有：`WORKSTREAMS.md`、`HANDOFF_TEMPLATE.md`、worktree 创建/状态/打开脚本。

待做：`workstream_sync.sh`、`workstream_handoff_check.sh`、主线集成固定 checklist。

### 外部痛点文档：DISPATCH_PAIN_POINTS / PAIN_POINTS

状态：已记录，部分约束已落地

解决问题：workdir 不可靠、子代理启动后不建工单、completion notification 不等于完成、BLOCKED 不能当终点等系统性痛点。

已落地：标准工单目录模板、`ACTION_RECEIPTS.md`/`TEST_CHECKLIST.md`/`BUGS.md`/`SKILL_USAGE.md`/`HANDOFF.md`。

未落地：顶层任务现场模板、SPEC→TEST_CHECKLIST 追踪链、Tool failure log、Browser/PWA/Game 自动验收器、数据规模断言器、安全任务隔离策略。

---

## 待验证

### 普通 Feishu Agent 对话与工作真实验收

旧部署的双 owner 真测确认身份与产物隔离，但也暴露 `/btw` 会被辅助回复轮提前消费、以及 task-scoped
history 让补充要求在续轮中丢失。当前候选已禁止辅助轮消费 active-turn input，并按 会话运行时 的 transcript
commit 语义把成功接收的 `/btw` 作为真实 UserTurn 幂等写入唯一 thread history；compact 续轮使用 typed
carrier，不再维护 task-id guidance history。focused tests 已通过，待发布后在 1.10 重新做两用户真实
Feishu 长任务、连续 `/btw` 和独立产物验收。

解决问题：同一用户多轮忘记、旧任务污染闲聊、首条消息被密码卡吞掉，以及长任务超过同步等待窗后真实结果无法送达。

已验：1.10 MiniMax M2.7 双 owner/thread 长任务、compact、旧聊天检索、跨用户/跨话题隔离、
`/verbose`、人设、USER 偏好、SOUL 确认卡与长任务回送；生产 compact 阈值已恢复 90%。

本轮待部署后复验：`/goal` 同 thread 持续续跑/暂停/恢复，`/audit` 显式结构化激活，
`/btw` 写入并在原 live turn 下一安全点真正消费且不创建后台重复执行器，`/stop` 能立即打断模型流，
之后模型以精确结构化 task id 重开原 workspace，失败选择不能新建旁路任务，以及前台聊天和后台
长任务并行时不串 prompt/workspace。还需验证前台让出后两个用户的普通聊天都能在后台主代理继续工作时及时回答、
后台可按任务结构自主派工、最终消息包含真实目录/功能/测试结果且不泄露内部协议。

### chat 交互体验：中文输入和后台输出

状态：待验证

解决问题：`input()` 和后台线程同时输出时破坏输入行，中文删除有视觉残留。

待做：确认 `prompt_toolkit` + `patch_stdout()` 在各终端下稳定。
