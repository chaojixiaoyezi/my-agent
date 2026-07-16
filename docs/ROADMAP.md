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

补充记录：2026-05-05 已完成一轮 CI 回归修复，解决 gateway 测试互相污染、日志证据测试缺导入、runner 解析失败结果写回不一致等问题；细节已移入 `docs/COMPLETED.md`。

---

## 设计中（未开工）

### Memory 第二批：恢复权威源细化与更多联合场景

状态：设计中

解决问题：第一批闭环已经打通 archive、route、compression 和 doctor，但更多跨天、跨入口、异常恢复场景还需要继续扩展，避免只在标准 happy path 上可靠。

待做：补更多 route 冲突/缺文件/损坏 snapshot 场景，扩展 gateway、subagent、local-doctor 联合恢复演练。

设计台账：DESIGN_LEDGER.md "Memory 第一批痛点归档"

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

状态：主链已收口；本轮双用户真测先修复 `/btw` 双执行轮与 `/stop` 等待模型超时，随后又发现
持久 task guidance 消费 ID 错位和失败 select 可绕过工作区决策门。第二批候选已通过 focused tests，
完整门禁、发布与 1.10 复验进行中。

解决问题：同一用户多轮忘记、旧任务污染闲聊、首条消息被密码卡吞掉，以及长任务超过同步等待窗后真实结果无法送达。

已验：1.10 MiniMax M2.7 双 owner/thread 长任务、compact、旧聊天检索、跨用户/跨话题隔离、
`/verbose`、人设、USER 偏好、SOUL 确认卡与长任务回送；生产 compact 阈值已恢复 90%。

本轮待部署后复验：`/goal` 同 thread 持续续跑/暂停/恢复，`/audit` 显式结构化激活，
`/btw` 写入并在原 live turn 下一安全点真正消费且不创建后台重复执行器，`/stop` 能立即打断模型流，
之后模型以精确结构化 task id 重开原 workspace，失败选择不能新建旁路任务，以及前台聊天和后台
长任务并行时不串 prompt/workspace。

### chat 交互体验：中文输入和后台输出

状态：待验证

解决问题：`input()` 和后台线程同时输出时破坏输入行，中文删除有视觉残留。

待做：确认 `prompt_toolkit` + `patch_stdout()` 在各终端下稳定。
