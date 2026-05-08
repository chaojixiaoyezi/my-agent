# 设计思路台账

这份文档用来记录我们在交流中形成的新思路，避免后续开发时忘记上下文。

后续 AI 开发者必须先读：
- `LLM_GUIDE.md`（总入口，含开工前/收工后清单）
- `docs/ROADMAP.md`（待做功能）
- `docs/COMPLETED.md`（已落地功能）
- 本文件（设计决策来源）
- 必要时读取 `docs/design/` 下对应模块设计文档

每次出现新的架构想法、命令语义、能力边界或长期方向，都要在这里追加记录，并标明是否已经落地。主设计台账只放导航和摘要；超过约 100 行的模块细节放到 `docs/design/`。

## 状态标记

```text
已落地       已经有代码或配置实现
部分落地     已有基础结构，但还没完整接入运行链路
设计中       已形成方向，还没写代码
待验证       已写代码，但还需要真实场景验证
暂停         暂时不做，但保留背景
```

## 2026-04-30 / Subagent 质量契约与用户少说派工

状态：部分落地

模块设计文档：[docs/design/subagent-quality-contract.md](docs/design/subagent-quality-contract.md)

摘要：
- 子代理质量差的核心原因通常不是能力不足，而是父会话没有把目标质量、成功样本、交付红线和验收标准结构化传下去。
- 子代理应从“独立负责人”降级为“受控施工队”：负责生产材料、局部检查、挑错和修指定缺陷；不能定义完成标准，不能决定最终交付。
- 后续引入 `QualityContract`、context pack、context manifest、producer/critic/reviewer 角色拆分和父会话反验收。
- 新增痛点到解决项映射：防 fake done、防机械 PASS、防长 prompt 稀释重点、防多个 worker 各自当总负责人、防成功样本只留在聊天记忆里。
- 设计方向调整为“内置 workflow 模板，不内置固定 subagent 角色”：模板描述拆工拓扑、证据要求和验收闸门，worker 职责在运行时动态生成。
- workflow 支持 `auto | manual | off` 三档；默认可以自动套模板，用户也可以关闭、手动指定或复制内置模板到用户目录后修改。
- 用户少说模式是目标：用户只表达任务和偏好，系统自动选 profile、写质量契约、派 producer/critic、落证据和验收报告。
- 规范补充：后续每一个开发项都要显式写“解决问题”，说明它解决哪个用户痛点、系统风险或交付缺口，避免只罗列模块名和 workflow 名。
- 这条规范追溯适用于已经写进模块设计文档的旧 Phase；旧 Phase 后续被补录、拆工或复盘时，也要补上“解决问题”，不只约束新增 workflow。

已落地：
- `SUBAGENT_RUNBOOK.md` 已记录质量契约、受控施工队、上下文包、反验收和阶段路线。
- `TEST_CHECKLIST.md` 已补充相关检查项。
- `docs/design/subagent-quality-contract.md` 承载完整模块设计。
- Phase 1 已落地：
  - `AgentConfig` 增加 `subagent_workflow_mode: auto | manual | off`、内置模板开关、用户模板目录和 review rounds，非法值会回退并记录 warning。
  - 新增 `agent_py_agent/agent/subagent_workflows/`，支持加载内置 JSON workflow、用户 JSON 覆盖模板、模板校验和 `solves` 字段。
  - `SubAgentTask` / `SubAgentExecutionContext` 增加 `QualityContract`、`ContextManifest` 和 `context_packs`，执行上下文 Markdown 明确子代理不能自判最终完成。
  - 验证：Subagent workflow 专项组合 14 passed；packaging/agent/tools 回归 63 passed；全量 `python -m pytest` 186 passed。

后续方向：
- 第一批并行 worker 已完成：配置与开关、模板 schema/store、QualityContract/Context Pack。解决问题：先把用户少说模式、可控开关、模板持久化和交付质量契约打底，缓解“父会话说不清、worker 各干各的、成功标准只在聊天里”的痛点。
- 第二批并行 worker 建议拆为 Workflow Router、Workflow Compiler、Parent Gate / Acceptance Planner。解决问题：把自然语言任务稳定路由到合适 workflow，并在派工前生成可执行计划和父级验收闸门，缓解“派错工、漏验收、机械 PASS、fake done”的痛点。
- 详细开发步骤、内置项、开关项、用户必须表达的内容和 workflow 模板库计划见模块设计文档。

## 2026-04-30 / Log Analysis 第一版验收与 worker 切片

状态：部分落地

模块设计文档：[docs/design/log-analysis.md](docs/design/log-analysis.md)

摘要：
- 日志分析第一版已经有 SecurityAlertV1 接入、JSONL store、受控 query、软检测器、case、route/report 和 analyst/reviewer 合同。
- 父会话初验发现 ingest/query 默认路径、EvidenceRef、dispatch contract、case dedup、缺时间戳关联、report finding 过滤等 P0/P1 问题，并已修复复验。
- 第二轮已接入 `my-agent logs status/ingest/query/hunt-ip/trace-case`、ToolRegistry 安全工具授权、结构化 query plan 和 prompt 工具名统一。
- 当前复验：LOG CLI/tools/detector/model 组合测试 52 passed，全量 172 passed，手工 logs ingest -> query 查回 3 rows。
- 剩余主要是 runtime capability 自动接线、storage audit、Live Lab replay 和 README 快速开始。

后续方向：
- 下一批 worker 优先做 runtime capability 自动接线、storage audit、Live Lab replay 和用户文档。
- 是否引入 DuckDB/Parquet/Kafka/ML 依赖仍由父会话裁决，不交给 worker 默认决定。

## 2026-04-29 / 可见真实环境测试台 Live Lab

状态：部分落地

思路：
- 开发 agent 不能只靠单元测试，需要能在用户看得见的终端里跑真实 runtime。
- 测试台必须显示发给 my-agent 的 prompt、实际命令、stdout/stderr、耗时、退出码和证据路径。
- 默认必须隔离 workspace，避免真实测试污染开发仓库。
- 默认不烧真实 API；只有显式 `--real-llm` 才跑真实 LLM case。

已落地：
- `scripts/live_agent_lab.py` 作为薄入口。
- `scripts/live_lab/cli.py` 管参数。
- `scripts/live_lab/runner.py` 管隔离配置、命令执行、transcript 和 summary。
- `scripts/live_lab/cases.py` 管 health、bad-weather、gateway ask 和 long subagent 场景。
- `scripts/open_live_lab.sh` 可在 macOS 新开可见 Terminal。
- `validation/live_lab/` 已加入 `.gitignore`。

后续方向：
- 增加 memory 长任务、tools 边界任务、问题任务和多轮恢复任务。
- 把真实测试结果摘要索引进 LocalStore，方便之后按 case 和失败类型搜索。
- 增加可配置任务矩阵，支持批量跑大量真实 LLM case。

## 2026-04-29 / 并行开发 Workstream 工作台

状态：部分落地

思路：
- 多个 会话运行时/AI 可以并行，但必须先把目录、分支、职责边界和交接格式定清楚。
- 主工作区只做集成和验收；每条开发线用独立 `git worktree` 和 `workstream/<name>` 分支。
- 并行线不直接合并 main；完成后写 handoff，由主线统一检查 diff、跑测试、解决冲突和提交。

已落地：
- `WORKSTREAMS.md` 定义 workstream 规则、预置开发线和主线集成流程。
- `HANDOFF_TEMPLATE.md` 定义交接格式。
- `scripts/workstream_create.sh` 创建 worktree 和分支。
- `scripts/workstream_status.sh` 查看主仓库和所有 worktree 状态。
- `scripts/open_workstream.sh` 打开某条开发线的可见终端。
- `scripts/workstream_common.sh` 统一路径、分支命名和名称校验。

预置开发线：
- `memory`
- `framework-runtime`
- `tools-boundary`
- `live-lab-test`

后续方向：
- 根据真实使用情况加入 `workstream_sync.sh`、`workstream_handoff_check.sh`。
- 主线集成时增加“读取 handoff + diff + 测试结果”的固定 checklist。
- 如果并行线数量增加，再考虑自动生成每条线的专属 会话运行时 prompt。

## 2026-04-29 / Memory 第一批痛点归档

状态：设计中

思路：
- 记忆系统的问题不是“没有记忆”，而是层级、召回、任务状态、flush、lesson 抽象和未来 skill 沉淀之间没有稳定同步。
- 关键规则不能只依赖 RAG；需要 HOT 层、INDEX 和固定引用入口。
- 任务状态不能只写进 memory，必须以任务目录、证据和测试结果为准。
- 历史 session 里的“已完成/已修复”只能作为线索，不能直接当当前事实。
- 子代理成果必须进入任务目录和可恢复摘要，不能只留在聊天汇报里。
- 自学习先不做，只预留 Skill Draft / Learning Candidate 的位置。

已落地：
- 新增 `MEMORY_BACKLOG.md`，把第一批痛点去重成 15 类，并记录初步设计原则。

后续方向：
- 等第二批痛点输入后继续合并去重。
- 再讨论 memory 最小闭环：`memory doctor`、`memory flush`、结构化 `memory write`、HOT/INDEX 入口和子代理收束摘要。
- 在正式开发前先明确 memory 分层和写入验证规则。

## 2026-04-30 / Memory 全量归档等级与压缩前 Hook

状态：设计中

思路：
- 原始会话可以做全量冷归档，但不能直接进入 prompt。
- 普通用户不应该面对一堆细碎开关，更适合一个 `memory_archive_level`。
- `memory_archive_level` 采用 0-3 级：0 最完整，3 最小但仍保留最大化恢复任务所需字段。
- 多轮上下文压缩后仍会遗忘，所以压缩前必须有 hook，先保存结构化恢复快照，再允许压缩。
- hook 快照不和 daily memory 混放，按天写入独立 `memory/hooks/YYYY-MM-DD.jsonl`。
- hook 快照默认保留 7 天，用户可以按硬盘情况调大或不限制。
- 记忆要按功能分目录：daily、hooks、raw、index、hot、lessons、toolchains、tasks 各自独立。
- token / context budget 必须实时可见，不能等用户已经丢状态才发现。
- 记忆配置解析必须安全默认：乱码、注入字符、非法枚举、越界数字都不能直接生效，要回落默认/安全值并可观察。

已落地：
- `MEMORY_BACKLOG.md` 记录了 raw archive 0-3 等级、等级 3 的恢复底线、压缩前 hook 字段、每日 hook 文件、默认 7 天保留期、token 可见性要求和配置安全默认规则。

后续方向：
- 设计 `memory_archive_level` 配置项。
- 设计 `memory_hook_retention_days`、`memory_hook_enabled` 和 `memory_hook_archive_level` 配置项。
- 设计 memory 配置解析器和 `memory config doctor`，展示最终生效值与回落原因。
- 设计 compression snapshot 存储格式。
- 在 `status` / chat / Live Lab 中展示 token 估算和压缩风险。
- 实现前先定义脱敏策略，避免全量归档写入 key、cookie、token。

## 2026-04-30 / Memory 长期规则索引化与强制路由

状态：设计中

思路：
- 长期规则不能一条条塞进常驻 memory，否则用久后会变成第二个臃肿上下文。
- 常驻 memory 只做入口导航：从 `MEMORY.md` 指向 routing index，再由 index 指向正式规则文件。
- 正式规则文件可以写得详细，但默认不进入 prompt；只有命中触发条件时才读取。
- 只靠提示词要求 AI “记得去读 index”不可靠，运行时必须做一层确定性 memory router。

建议结构：
- `MEMORY.md`：极短顶层导航，只告诉模型有哪些 index。
- `memory/routing/INDEX.md` 或分主题 routing 文件：写 trigger、aliases、scope、authority_path、priority、stale_check。
- `references/.../*.md` 或 `memory/rules/.../*.md`：正式长期规则正文。

防止 AI 不遵守的工程手段：
- 每轮用户输入先过关键词 / FTS / route matcher，产出 `required_read_paths`。
- 高置信和 strict 规则由代码自动读取；中置信规则作为候选提示；低置信只记录。
- 正式任务、安全边界、工具权限、记忆写入等关键场景开启 strict gate：命中规则但没有读取权威文件时，不允许直接给最终结论。
- 每次读取写 `memory_read_receipt`，记录 route_id、路径、hash、耗时和触发原因。
- `memory doctor` 检查索引路径是否存在、触发词是否冲突、正式规则是否过期。
- 加路由测试：给定触发词，必须命中指定 index 和 authority_path。

已落地：
- `MEMORY_BACKLOG.md` 记录了长期规则索引化、两级/三级导航、索引字段、strict/soft 路由模式和 receipt 思路。

后续方向：
- 定义 `MemoryRoute` 数据结构。
- 实现 `memory-route` / `memory doctor` 的最小版本。
- 把路由命中结果接入 chat、gateway request 和正式任务恢复流程。

## 2026-04-30 / Memory 压缩方式调研

状态：设计中

调研对象：
- LangGraph / LangChain：短期记忆超上下文后支持 trim、delete、summarize 和 checkpoint。
- OpenAI Realtime：支持 auto / disabled truncation，也支持 retention ratio；说明了截断会从最旧消息开始丢上下文。
- OpenAI Agents SDK：`OpenAIResponsesCompactionSession` 通过 trigger hook 自动 compact，并会重写 session history。
- LlamaIndex：短期 chat history 有 token ratio，超出后把旧消息 flush 到长期 memory blocks。
- OpenAI Agents SDK sandbox memory：run 结束后先做 conversation extraction，再由 consolidation agent 汇总到 `MEMORY.md` / `memory_summary.md`。
- MemGPT：把上下文窗口当快内存、外部存储当慢内存，走操作系统式分层记忆。
- 模型助手 Code：启动入口保持短规则，复杂流程放 skill / scoped rules，避免常驻上下文膨胀。

结论：
- 不采用单一压缩方式；直接截断太危险，纯摘要会漂移，纯 RAG 不可靠。
- 第一版采用组合策略：实时 token 预算 -> 压缩前 hook -> 滚动摘要或结构化摘要 -> raw 冷归档 -> 后台提取 daily/task/lesson 候选。
- 压缩摘要只服务“下一轮模型继续推理”，不能当事实源；事实源必须回到 task `STATUS/HANDOFF/ACCEPTANCE/TESTS`、daily memory 和 hook/raw archive。
- 严禁无 snapshot 的 truncation。即使是应急滑窗，也必须先写 `memory/hooks/YYYY-MM-DD.jsonl` 并 readback 验证。
- 正式任务默认使用 `structured_summary`，普通聊天默认 `rolling_summary`，后台再用 `extract_then_consolidate` 做长期沉淀。

已落地：
- `MEMORY_BACKLOG.md` 新增“压缩方式调研”章节，记录各家做法、优缺点和我们的模式枚举：`off`、`truncate_after_snapshot`、`rolling_summary`、`structured_summary`、`extract_then_consolidate`。

后续方向：
- 定义 compression snapshot JSON schema。
- 定义摘要 prompt，要求输出 snapshot/task refs，禁止把摘要写成事实结论。
- 定义 token budget 估算器，把 system、messages、tools、tool results、中文文本都纳入估算范围。
- 在恢复链路实现固定顺序：compression snapshot -> task 权威文件 -> daily memory -> HOT/routes -> raw archive/RAG。

## 2026-04-30 / Memory 第一版骨架并行落地

状态：部分落地

思路：
- memory 可以先开工，但第一版只做可测试骨架，不急着把所有流程接进主循环。
- 三条线可以并行：配置安全默认、长期规则 router、压缩前 hook/raw archive。
- 主线负责收口语义一致性，尤其是安全默认值不能和 router 行为冲突。

已落地：
- 配置线：新增 `MemorySettings`、`MemoryConfigWarning`、配置规范化和 warning receipt；`AgentConfig` 新增 memory 配置字段；`agent_config.yaml` 写入中文注释。
- 路由线：新增 `memory_routing/`，支持 JSON/Markdown route index、关键词/别名匹配、soft/strict path resolution、route 诊断和 read receipt 结构。
- 归档线：新增 `memory_archive/`，支持 `CompressionSnapshot`、`RawMemoryEvent`、每日 hook/raw JSONL、snapshot readback 验证、hook retention 和保守 token 估算。
- 主线修正：`memory_rule_auto_read_limit=0` 明确为“不自动选择读取路径”，避免配置写 0 时扩大读取范围。
- `agent/` 根目录只保留 `memory_settings.py` 兼容门面，真实实现归到 `settings/memory.py`，继续遵守分层原则。

后续方向：
- 把 router 命中结果接入 `SimpleAgent.run()` 的 prompt 构造前置步骤。
- 增加 `memory doctor` / `memory-route` CLI，展示配置 warning、route 诊断、hook 留存状态和 read receipt。
- 把 `append_snapshot` 接到真实压缩前 hook；目前还没有真实压缩流程，所以只先保留存储 API。
- 设计 raw 大正文落盘和脱敏策略，避免把 key/cookie/token 全量写入冷归档。

## 2026-04-30 / Memory 可见诊断与主循环薄接入

状态：部分落地

思路：
- memory 骨架不能只停在库函数；必须让用户和开发者能看见它怎么路由、哪里有配置回退、归档目录有没有动静。
- 接主循环时要薄：不重写工具循环，不让长期规则全文常驻，只在命中 route 时注入短 authority section。
- raw archive 先跟随 `save/auto_save_memory`，用户 `--no-save` 时不写冷归档，避免违反显式不保存语义。

已落地：
- 新增 `memory-route`：给定 query，读取 `memory/routing/INDEX.md` 或 `--index` 指定文件，输出 route matches、required/candidate paths 和诊断。
- 新增 `memory-doctor`：展示 memory effective config、config warnings、route index 校验、hook/raw 目录状态。
- 新增 runtime routing context：安全限制在 workspace root 内读取 authority 文件，生成 injected sections 和 read receipts。
- 新增 raw archive run helper：把一轮 run 的 user/assistant/tool metadata 写入 `memory/raw/YYYY-MM-DD.jsonl`，生成稳定 event_id/content_hash 和 token estimate。
- `SimpleAgent.run()` 已接入：
  - 调模型前读取命中 route 的短 authority section 并注入 prompt。
  - 保存对话时写 raw archive；`save=False` 时不写。
  - `AgentRunResult` 增加 routed rule 和 archive 统计字段。

后续方向：
- 把 read receipts 写入 LocalStore event，方便 timeline/doctor 查“本轮到底读了哪些规则”。
- 增加 route index 默认模板和创建命令，降低用户第一次配置成本。
- 接入真正压缩前 hook，当前 raw archive 已接主循环，但 compression snapshot 还只是存储 API。
- 做脱敏策略和大正文 blob 存储，不把工具输出正文直接塞进 raw event preview。

## 2026-04-29 / 大文件拆分第一步

状态：部分落地

思路：
- `__main__.py` 应该是 CLI 入口，不应该长期承载 gateway 文件协议细节。
- gateway 是独立协议边界：路径、请求队列、adapter、恢复、响应、索引重建都可以单独成模块。
- 新拆出的模块必须按“两层注释”写：第一行给 LLM 技术契约，第二行开始给人讲大白话。

已落地：
- 新增 `agent_py_agent/agent/gateway.py`。
- 从 `__main__.py` 拆出 gateway 路径、adapter 路径、JSON 文件读写、pid/日志小工具、请求提交、响应等待、processing 恢复、adapter inbox/outbox 处理、gateway 请求执行和 gateway 索引重建。
- `__main__.py` 保留旧导入兼容，测试仍可从 `agent_py_agent.__main__` import 旧函数名。
- 新增 `ARCHITECTURE_GUIDE.md`，记录架构边界、拆分原则和两层注释规范。
- `gateway.py` 的关键函数都补了 LLM contract + Human version 两层注释。

后续方向：
- `subagent.py` 继续拆：models、rendering、parsing、dispatch、acceptance。
- `__main__.py` 继续拆：scenario-test、local-doctor/local-rebuild。
- 后续每拆一块，都要保留测试命令和变更说明。

## 2026-04-29 / 大文件按职责拆分完成

状态：已落地

思路：
- 不是为了凑行数拆文件，而是按变化原因拆：CLI、工具、gateway 协议、LocalStore、SimpleAgent 编排、subagent 管理各自成边界。
- 旧入口继续兼容，避免一次重构让测试、脚本和历史导入全部断掉。
- 新拆出的包都用模块 docstring 写“两层注释”：第一段给 LLM 技术契约，后面给人讲白话边界。

已落地：
- `agent_py_agent/cli/`：把 `__main__.py` 拆成 common、local、subagents、daemon、gateway、adapter、scenario、chat、parser。
- `agent_py_agent/agent/agent_core/`：把 `core.py` 拆成 runtime、subagent、dispatch、planner、runner prompt、runner dispatch、orchestration tools。
- `agent_py_agent/agent/tooling/`：把 `tools.py` 拆成 models、filesystem、web、parser、registry、write_boundary。
- `agent_py_agent/agent/gateway_parts/`：把 `gateway.py` 拆成 paths、io、process_control、logging、recovery、runtime、adapter。
- `agent_py_agent/agent/local_storage/`：把 `local_store.py` 拆成 models、schema、records、search、events、maintenance。
- `agent_py_agent/agent/subagents/`：`subagent.py` 继续作为兼容入口，manager 能力拆成 models/reports/rendering/parsing/policies/probe 和多组 manager mixin。

当前约束：
- 生产 Python 文件当前没有超过 500 行；最大文件 `agent_py_agent/cli/gateway_process.py` 为 498 行。
- 不鼓励跨模块引用 internal 细节；新功能优先走兼容入口或对应职责包公开 API。
- 历史函数 docstring 还会随着后续触碰继续补齐到完整“两层注释”。

验证记录：
- `python3 -m py_compile agent_py_agent/__main__.py agent_py_agent/cli/*.py agent_py_agent/agent/*.py agent_py_agent/agent/*/*.py`
- `agent_py_agent.tests.test_tools`
- `agent_py_agent.tests.test_agent`
- `agent_py_agent.tests.test_local_store`
- `agent_py_agent.tests.test_packaging`
- `agent_py_agent.tests.test_cli_reference`
- `python3 -m agent_py_agent --help`
- `python3 -m agent_py_agent local-doctor --json`
- `python3 -m agent_py_agent gateway status`

后续方向：
- 继续把 `manager_*` 里少数 100 行以上复杂函数细拆成 validator/policy/render/repository。
- 给迁移出来的历史函数逐个补齐更细的人类大白话注释。
- 增加 import 边界测试，防止后续从上层模块反向依赖底层实现。

## 2026-04-29 / 第二轮目录归位

状态：已落地

思路：
- 第一轮解决“文件太大”，第二轮解决“根目录仍然平铺”。
- 根目录只保留兼容门面，真实实现进职责目录。
- 未来方向可以先建目录和 README 定义边界，但不写无意义 wrapper 或空代码。

已落地：
- `backend.py` -> `backends/base.py`
- `config.py` -> `settings/config.py`
- `capabilities.py` / `capability_config.py` / `skills.py` -> `capability/`
- `file_io.py` -> `io/jsonl.py`
- `memory.py` -> `memory_store/jsonl.py`
- `prompting.py` -> `prompting_parts/builder.py`
- 新增 `agent_py_agent/agent/DIRECTORY_GUIDE.md` 作为 agent 目录地图。
- 新增未来目录并用 README 固定含义：`clients/`、`repositories/`、`observability/`、`security/`、`validators/`。

保留兼容：
- `agent.backend`
- `agent.config`
- `agent.capabilities`
- `agent.capability_config`
- `agent.skills`
- `agent.file_io`
- `agent.memory`
- `agent.prompting`

后续方向：
- 为目录边界补 import-lint 风格测试，防止上层反向依赖底层。
- 未来新增外部依赖时优先进入 `clients/`；新增安全硬门禁时优先进入 `security/`；新增跨领域校验时优先进入 `validators/`。

## 2026-04-29 / 本地事实源

状态：部分落地

思路：
- 本地 agent 先保留“文件优先”的可读性，但补一层结构化账本。
- SQLite 负责记录卡片、来源、元数据、更新时间和审计事件。
- FTS5 负责本地全文检索；如果运行环境不支持，就自动退回 LIKE。
- 文件系统保存正文和未来大 artifact，避免数据库变成一个难维护的大黑盒。
- JSONL 保存追加式事件流水，适合人工排查、备份和未来上传同步。

已落地：
- `agent_py_agent/agent/local_store.py`：LocalStore 第一版。
- `JsonlMemory` 双写：记忆继续写 JSONL，同时索引到 LocalStore。
- `local-store-status`、`local-search`、`local-index-memory` 三个 CLI 命令。
- `status` / `timeline` 两个观察入口：一个看当前总览，一个看最近事件。
- 配置项：`local_store_path`、`local_store_files_dir`、`local_store_events_path`、`local_store_fts_enabled`。
- gateway request、gateway 生命周期、subagent run、work log、runner result、execution context、acceptance、patch review、dispatch、watch、parent planner、capability route、action apply、channel probe 已接入 LocalStore。

后续方向：
- 增加定期 compact/rebuild/backup 命令。
- 公司级使用时，本地仍为第一事实源，远端只做同步、备份、组织视图和跨设备协作。
- 向量检索后续可以作为附加索引，而不是替代 SQLite/FTS5/文件/JSONL 这一层。

## 2026-04-28 / chat 交互体验

### 后台队列

状态：已落地

思路：
- 用户发出一条消息后，不应该卡死在模型响应上。
- chat 模式应允许继续输入，后续输入进入后台队列。
- 同一时间先保持只跑一个模型请求，避免记忆和工具调用乱序。

落地位置：
- `agent_py_agent/__main__.py`

后续注意：
- 需要继续补 `/queue`、`/cancel`、`/queue-clear`。
- 当前取消只能取消本地等待或未开始任务，不能撤回已经发到服务端的请求。

### 中文输入和后台输出

状态：待验证

思路：
- 普通 `input()` 和后台线程同时输出时，会破坏正在编辑的输入行。
- 中文输入删除时还会出现视觉残留。
- 真实交互终端应优先使用 `prompt_toolkit` 和 `patch_stdout()` 保护输入行。

落地位置：
- `agent_py_agent/__main__.py`

后续注意：
- 不要再用终端标题栏 escape 序列显示读秒；部分 IDE 会把控制码打印成正文。
- 如需读秒，优先使用 `prompt_toolkit` bottom toolbar，或保留 `/status` 主动查询。

## 2026-04-28 / `/btw` 语义

状态：部分落地

思路：
- `/btw` 不是传统意义的 inject 管理命令。
- 它代表“顺便说一下 / 打断补充 / 修正上下文”。
- 用户可以用它补充约束、纠正目标、改变当前任务倾向。

当前行为：
- `/btw` 显示当前运行时补充上下文。
- `/btw <内容>` 追加一条补充上下文。
- `/btw-clear` 清空补充上下文。

后续方向：
- `/btw-once <内容>`：只对下一条任务生效。
- `/btw-pop`：撤回最后一条 btw。
- 任务正在执行时，`/btw` 不能真正修改已经发到服务端的 prompt，但可以影响排队中和后续任务。

## 2026-04-28 / 自学习

状态：部分落地

思路：
- 个人通用助手一定需要自学习能力。
- 自学习不能直接改正式 skill，必须先生成学习候选草稿。
- 用户确认后，草稿才能进入正式 skill。

已落地：
- `enable_self_learning` 配置开关，默认关闭。
- `AGENTS.md` 里写明自学习约束。

落地位置：
- `agent_py_agent/config/agent_config.yaml`
- `agent_py_agent/agent/config.py`
- `AGENTS.md`

后续方向：
- `agent_py_agent/data/learning_drafts/`：保存学习候选草稿。
- `/learn`：查看候选。
- `/learn accept <id>`：确认写入 skill。
- `/learn reject <id>`：拒绝候选。

## 2026-04-28 / Skill 使用方式

状态：部分落地

思路：
- 不采用“全量 skill 常驻 prompt”。
- 不让子代理直接看到全局 skill 宇宙。
- 最佳方向是“检索门控的渐进式 skill 路由”。

目标加载顺序：

```text
L0: Capability Taxonomy
L1: Skill Cards
L2: Candidate Cards
L3: SKILL.md 正文
L4: references/templates/scripts 附件
```

后续原则：
- 常驻内容要少。
- Skill Card 可被本地检索。
- 真正命中后才加载 `SKILL.md`。
- 附件必须按需读取。

已落地底座：
- `agent_py_agent/agent/skills.py`：扫描和解析 `SKILL.md` 成 Skill Card。
- `agent_py_agent/agent/capabilities.py`：把 Skill Card 和 Tool Card 合并进统一 Capability Router。
- `agent_py_agent/tests/test_capabilities.py`：覆盖 skill 解析、tool card 映射和候选检索。

尚未落地：
- 还未完整接入 chat/subagent 主链路。
- 还未实现 capability_request / capability_grant 协议。

## 2026-04-28 / Tool 使用方式

状态：部分落地

思路：
- tools 和 skills 应统一纳入 Capability 系统。
- tool 比 skill 更危险，因为 tool 会产生实际动作。
- 子代理不应直接看到全量工具，只应拿到父代理授权的最小 tool bundle。

关键原则：

```text
工具失败 ≠ 能力失败
当前授权失败 ≠ 系统无能力
能力缺口要上抛，而不是伪装完成
```

典型场景：
- 有 5 个 web 搜索工具。
- 子代理只拿到其中一个，调用失败。
- 子代理不能直接说“不行”或假完成。
- 它应该上抛 capability_request，让父代理寻找替代 tool 或 skill。

后续方向：
- Tool Card 需要记录 `fallback_group`、`failure_modes`、`risk_level`、`side_effects`。
- 父代理可以下发替代工具 bundle。
- 失败经验应反向更新 Tool Card，避免下次继续错误路由。

已落地底座：
- `agent_py_agent/agent/capabilities.py` 已能把现有 `ToolSpec` 映射成 Tool Capability Card。
- 当前已补基础风险分类和副作用分类，例如 `filesystem_write`、`network_request`。

尚未落地：
- `fallback_group` 和 `failure_modes` 还没进入 Tool Card。
- 还没有父代理下发 tool bundle 的运行链路。

## 2026-04-28 / 层级能力上抛

状态：部分落地

思路：
- 子代理遇到问题，不应该自己全局搜索 skill/tool。
- 子代理只描述能力缺口。
- 父代理查自己的 Skill/Tool Cards。
- 父代理找不到就继续向上抛。
- 更高层找到能力后，可以沿原链路下发给真正需要的下级代理。
- 中间层不需要读 skill 全文，只转发 skill/tool card 或 capability grant。

目标流程：

```text
子代理执行任务
  -> 遇到能力缺口
  -> capability_request 给父代理
  -> 父代理查自己的 skill/tool cards
      -> 找到：下发 capability_grant
      -> 没找到：继续向上抛
  -> 更高层找到能力
  -> 沿原链路下发 grant
  -> 子代理按需加载正文或工具说明
  -> 完成任务或记录 capability_gap
```

核心原则：

```text
下级代理只暴露问题，不搜索全局能力；
上级代理负责能力发现、授权和下发；
中间代理可以只转发 capability grant，不必展开 skill/tool 全文。
```

已落地底座：
- `agent_py_agent/agent/subagent.py` 已新增 `CapabilityRequest`、`CapabilityGrant`、`CapabilityGap`。
- `SubAgentTask` 已升级成兼容旧名字的轻量 SubAgentRun，支持 `parent_id`、`root_id`、`depth`、`allowed_skills`、`allowed_tools`。
- `SubAgentManager` 已支持记录能力请求、能力授权和能力缺口。
- `agent_py_agent/tests/test_agent.py` 已覆盖父子关系和能力记录读写。
- `SubAgentManager.route_capability_requests()` 已能把 open request 路由到 skill/tool card。
- `python3 -m agent_py_agent subagents-route-capabilities` 已能 dry-run 或 apply 生成 grant/gap。

尚未落地：
- 已有单个子代理 runner 入口，但还没有并行 worker / process / session 调度。
- capability_request 已能在当前父代理能力范围内路由，但还没有跨层级自动上抛。
- capability_grant 已能写入运行记录、生成执行上下文，并由 `subagent-run` 读取。

## 2026-04-28 / Subagent 框架

状态：部分落地

思路：
- subagent 不只是“拆出 thought/plan”，而是一个可追踪的运行节点。
- 每个运行节点都应该有父子关系、能力边界、状态、结果和能力协商记录。
- 先逻辑隔离，再考虑未来是否物理隔离成独立进程、终端或远程 agent。

已落地底座：
- `SubAgentCard`：角色卡，描述角色、默认工具、能否写文件、能否生成子代理、输出契约。
- `SubAgentTask`：兼容旧调用的运行记录。
- `task.json` 和 `run.json` 双写。
- `thought.md` 增加 Capability Boundary 区块。

后续方向：
- 增加 `/subagents` 查看运行树。
- 增加 `/subagent <id>` 查看单个运行详情。
- 增加子代理状态流转：`PLANNING`、`RUNNING`、`BLOCKED`、`DONE`、`FAILED`。
- 接入 Capability Router，为子代理初始化 allowed skill/tool bundle。
- 接入真正执行循环，让子代理拥有独立上下文。

已落地补充：
- `SubAgentBoardItem` / `SubAgentBoard`。
- `subagent_board.json` 机器事实源。
- `SUBAGENT_BOARD.md` 人类红绿灯摘要。
- `python3 -m agent_py_agent subagents` 查看 summary / hot list / recent。
- `python3 -m agent_py_agent subagent <run_id>` 查看单个运行详情。
- 默认 hot list 会浮出 `BLOCKED`、`FAILED`、`TIMEOUT`、`CHANNEL_ERROR`、`DONE` 无证据、open capability request/gap、takeover、工单文件缺失等风险。
- `python3 -m agent_py_agent subagents-due-check` 生成父代理巡检报告。

## 2026-04-29 / Subagent Due-check

状态：部分落地

思路：
- 父代理不能 spawn 后放养，必须周期性查看子代理真实产出。
- due-check 的第一阶段只做“发现问题 + 建议动作”，不自动接管。
- 机器事实源应先落盘，后续自动接管、重派、缩小目标都读这份报告。

已落地：
- `SubAgentManager.due_check()`：扫描所有子代理运行记录。
- `SubAgentManager.write_due_check()`：写出 `subagent_due_check.json` 和 `SUBAGENT_DUE_CHECK.md`。
- `python3 -m agent_py_agent subagents-due-check`：CLI 触发巡检。
- 巡检会标出 P0/P1/P2 问题：
  - 工单关键文件缺失。
  - `DONE` 缺验收证据。
  - `DONE` 但未验证。
  - `FAILED` / `TIMEOUT` / `CHANNEL_ERROR` / `BLOCKED`。
  - 心跳停滞。
  - 运行超时。
  - open capability request。
  - open capability gap。

尚未落地：
- 自动周期触发 due-check。
- 根据 due-check 已能生成 dry-run action plan，但还没有自动 apply。
- channel probe 已有本地工单现场检查，但还没有接入模型 session / ACP adapter / 真实执行器。

## 2026-04-29 / Due-check Action Plan

状态：部分落地

思路：
- due-check 负责发现问题，action plan 负责把问题转成下一步动作。
- 第一阶段必须 dry-run，不自动接管、不自动重派、不自动改任务状态。
- 同一个 run 的重复问题要去重合并，例如 `heartbeat_stale` 和 `run_timeout` 合并成一次 `takeover_or_reassign`。

已落地：
- `ActionPlanItem` / `ActionPlanReport` 数据结构。
- `SubAgentManager.plan_actions()`：把 due-check issue 映射成动作。
- `SubAgentManager.write_action_plan()`：写出 `subagent_action_plan.json` 和 `SUBAGENT_ACTION_PLAN.md`。
- `python3 -m agent_py_agent subagents-plan-actions`：CLI 查看 dry-run 动作计划。

当前动作类型：
- `probe_or_repair_channel`：通道坏或缺 probe 证据时，优先检查 / 修复通道。
- `inspect_channel_probe`：通道降级时，先读 probe 证据。
- `repair_work_order`：工单关键路径缺失时修复现场。
- `reopen_for_evidence`：DONE 但缺证据时重开补验收。
- `run_acceptance`：DONE 但未 VERIFIED 时运行验收。
- `takeover_or_reassign`：超时或心跳停滞时准备接管、重派或缩小目标。
- `inspect_failure`：FAILED 时读取现场日志再决定处理方式。
- `classify_blocker`：BLOCKED 时分类原因。
- `route_capability_request`：处理能力请求并准备下发 skill/tool grant。
- `triage_capability_gap`：能力缺口进入学习或工具建设队列。

尚未落地：
- `--apply` 已有保守执行层，但还没有自动周期执行。
- 自动 takeover 已有受限 apply；自动 reassign / shrink-scope 还没有。
- 自动 route capability request。
- apply 前的交互式用户确认。

## 2026-04-29 / Action Apply v1

状态：部分落地

思路：
- action apply 默认必须 dry-run。
- 只有显式 `--apply` 才能改子代理运行记录。
- 第一版只执行低风险动作，不自动删文件、不覆盖已有人工产物、不自动下发 skill/tool。
- 所有 apply 必须留下机器审计日志和人类审计日志。

已落地：
- `ActionApplyRecord` / `ActionApplyReport` 数据结构。
- `SubAgentManager.apply_actions()`：执行或 dry-run 执行动作计划。
- `SubAgentManager.write_action_apply_report()`：写出 `subagent_action_apply_report.json` 和 `SUBAGENT_ACTION_APPLY.md`。
- `python3 -m agent_py_agent subagents-apply-actions`：默认 dry-run。
- `python3 -m agent_py_agent subagents-apply-actions --apply ...`：显式执行。
- 真正执行时追加：
  - `subagent_action_apply_log.jsonl`
  - `ACTION_APPLY_LOG.md`
  - 子代理自己的 `WORK_LOG.md`

当前允许执行的动作：
- `probe_or_repair_channel` / `inspect_channel_probe`：执行 channel probe 并记录证据。
- `repair_work_order`：补齐标准工单文件，只写缺失文件，不覆盖已有内容。
- `reopen_for_evidence`：把缺证据 DONE 改为 `BLOCKED`，标记 `failure_type=missing_evidence`。
- `run_acceptance`：只标记 `verification_status=NEEDS_ACCEPTANCE`，不自动跑未知命令。
- `takeover_or_reassign`：当前只做 takeover；必须提供 `--take-over-by`，会写 `TAKEOVER.md`。
- `route_capability_request` / `triage_capability_gap` / `inspect_failure` / `classify_blocker`：只写入待人工处理日志，不自动改授权或学习。

安全边界：
- 默认 dry-run。
- `takeover_or_reassign` 必须显式指定接管者。
- takeover 前会先确认 channel 为 `OK`，否则拒绝接管。
- 不删除文件。
- 不覆盖已有 `STATUS.md` / `WORK_LOG.md` / `ACCEPTANCE.md`。

尚未落地：
- 自动 reassign。
- 自动 shrink-scope。
- action apply 尚未直接触发 capability route；需要单独运行 `subagents-route-capabilities`。
- apply 前的交互式二次确认。
- apply 后的统一验收执行器。

## 2026-04-29 / Capability Request Routing

状态：部分落地

思路：
- 子代理不直接看全局 skill/tool 宇宙，只提交 capability request。
- 父代理用自己的 Capability Router 查 skill/tool card。
- 命中则生成 capability grant，未命中则生成 capability gap。
- 第一版默认 dry-run，只有显式 `--apply` 才写 grant/gap。

已落地：
- `CapabilityRouteRecord` / `CapabilityRouteReport` 数据结构。
- `SubAgentManager.route_capability_requests()`：路由 open capability request。
- `SubAgentManager.write_capability_route_report()`：写出 `subagent_capability_route_report.json` 和 `SUBAGENT_CAPABILITY_ROUTE.md`。
- `python3 -m agent_py_agent subagents-route-capabilities`：CLI 路由能力请求。
- 支持 `--skill-dir` 额外加载本地 skill card。
- apply 命中时会调用 `record_capability_grant()`，并把 request 标记为 `GRANTED`。
- apply 未命中时会调用 `record_capability_gap()`，并把 request 标记为 `GAP`。
- 真正 apply 时追加：
  - `subagent_capability_route_log.jsonl`
  - `CAPABILITY_ROUTE_LOG.md`
  - 子代理自己的 `WORK_LOG.md`

安全边界：
- 默认 dry-run。
- 只下发 skill/tool card，不加载完整 `SKILL.md` 正文。
- 不执行工具。
- 不自动学习新 skill。
- grant/gap 都可审计。

尚未落地：
- 跨父/爷/更高层级的自动上抛。
- grant 已能生成执行上下文文件，并可由 `subagent-run` 作为单 run 执行入口读取。
- 根据 route 结果自动重新唤醒子代理。
- tool fallback group 和 failure mode 还没有参与排序。

## 2026-04-29 / Grant 注入执行上下文

状态：部分落地

思路：
- capability grant 不能只停留在运行记录里，必须能变成子代理实际可读取的执行包。
- 执行包只包含父级授权后的能力，不给子代理全局 skill/tool 宇宙。
- 子代理缺能力时继续写 capability request，而不是自己到处搜索或假完成。

已落地：
- `SubAgentExecutionContext` 数据结构。
- `SubAgentManager.build_execution_context()`：生成单个 run 的最小执行上下文。
- `SubAgentManager.write_execution_context()`：写出 `execution_context.json` 和 `EXECUTION_CONTEXT.md`。
- `python3 -m agent_py_agent subagent-context <run_id>`：CLI 生成执行上下文。
- 执行上下文包含：
  - goal / thought / plan。
  - owner / supervisor / final_owner。
  - allowed skills / allowed tools。
  - granted cards。
  - acceptance checks / evidence。
  - allowed_write_roots / forbidden_write_roots / locked_files。
  - open capability request / gap。
  - 子代理执行硬规则。

安全边界：
- 不展开完整 `SKILL.md` 正文。
- 不注入未授权工具。
- 不读取全局 registry。
- runner 执行时只渲染并允许调用 `allowed_tools` 内的工具。
- 默认 dry-run；显式 `--execute` 才会调用模型。

尚未落地：
- grant 后自动重新唤醒或继续子代理。
- 按 token 预算裁剪 card 描述和上下文正文。
- 子代理写 capability request 的统一入口。

## 2026-04-29 / Subagent Runner Entry

状态：部分落地

思路：
- 子代理 runner 第一版先不做真正并发进程池，只打通一条可审计链路：
  execution context -> runner prompt -> 模型执行或 dry-run -> 工单回写。
- runner 默认 dry-run，避免误触真实 API。
- 真执行时必须先经过工具 allowlist，不能看到或调用未授权工具。

已落地：
- `SimpleAgent.run(..., allowed_tools=[...])`：限制工具目录、推荐工具和实际工具调用。
- `SimpleAgent.run_subagent()`：读取并刷新执行上下文，生成 runner prompt。
- `SubAgentManager.record_runner_result()`：把 runner 结果写回工单。
- `parse_subagent_runner_output()`：解析 `[SUBAGENT_RESULT]` 结构化 JSON。
- `python3 -m agent_py_agent subagent-run <run_id>`：默认 dry-run。
- `python3 -m agent_py_agent subagent-run <run_id> --execute`：显式调用模型执行。
- 结构化输出中的 `evidence` 会写入 `VerificationEvidence`。
- 结构化输出中的 `capability_requests` 会写成 open `CapabilityRequest`。
- 结构化输出中的 `artifacts` / `tests` / `patches` 会写进 `output.json`，供验收器和集成器读取。
- 结构化输出中的 `lessons` / `next_actions` 会写进 `output.json`，并追加到 `DEBRIEF.md`。
- 未授权的 `used_tools` / `used_skills` 会被忽略并写入 `output.json` 审计。
- runner 输出文件：
  - `RUNNER_RESULT.md`
  - `reports/runner_result.json`
  - `logs/runner_prompt.md`
  - `logs/runner_response.md`
  - `output.json`

安全边界：
- `--execute` 才会调用模型，默认只生成 prompt 和报告。
- 执行前默认跑 channel probe，BROKEN 时直接标记 `CHANNEL_ERROR`，不继续模型调用。
- 模型即使输出未授权工具调用，也会被 `ToolRegistry.execute_call(..., allowed_tools=...)` 拦住。
- 模型完成后只标记 `AWAITING_ACCEPTANCE` / `NEEDS_ACCEPTANCE`，不直接标记 DONE，避免假完成。

尚未落地：
- 真正的并行 worker / process / session 管理。
- runner 还不会自动应用 patch；patches 目前只是计划/状态记录，必须由父代理或集成器验收后处理。
- runner 还没有把 lessons 自动转成 skill 草稿。
- 多子代理统一调度、超时接管和重派。

## 2026-04-29 / Subagent Channel Probe

状态：部分落地

思路：
- 通道故障和任务失败必须分开判断。
- 在自动接管或重派前，父代理应该先确认 workdir、机器 JSON、写入现场和 probe 证据是否正常。
- 第一阶段先做本地运行现场 probe；后续再接模型 session、ACP adapter、远程 runtime。

已落地：
- `ChannelProbeCheck` / `ChannelProbeResult` / `ChannelProbeReport` 数据结构。
- `SubAgentTask.channel_status`：`UNKNOWN` / `OK` / `DEGRADED` / `BROKEN`。
- `SubAgentTask.last_probe_at` 和 `channel_checks`。
- `SubAgentManager.probe_channel(run_id)`：检查单个子代理运行现场。
- `SubAgentManager.write_channel_probe_report()`：写出 `subagent_channel_probe.json` 和 `SUBAGENT_CHANNEL_PROBE.md`。
- `python3 -m agent_py_agent subagents-probe`：CLI 触发通道健康检查。
- 单个 run 会写 `CHANNEL_PROBE.md` 和 `logs/last_channel_probe.json` 作为证据。
- due-check 会识别 `channel_broken` / `channel_degraded` / `channel_probe_missing`。

当前检查项：
- 标准工单目录和关键文件是否完整。
- `task.json` / `run.json` / `output.json` / `dependencies.json` 是否可读。
- `scratch/` 是否可写。
- probe 证据是否能写入任务目录。

尚未落地：
- 模型接口 probe。
- ACP / adapter / session probe。
- 工具通道 probe。
- 派工前自动 probe 和失败阻断。
- channel probe 失败后的自动修复建议。

## 2026-04-28 / 能力路由配置

状态：已落地

思路：
- 子代理、skill/tool 授权、能力上抛相关参数不放进 `agent_config.yaml`。
- 单独使用能力路由配置，避免主配置变成杂物间。
- 所有数字限制项统一约定：`0` 表示不限制。

已落地：
- `agent_py_agent/config/capability_config.yaml`
- `agent_py_agent/agent/capability_config.py`

默认正常限制：

```yaml
capability_request_max_tokens: 600
capability_escalation_max_hops: 4
capability_candidate_limit: 5
capability_bundle_max_tokens: 3000
capability_fallback_max_attempts: 3
```

其他数字限制项默认 `0`，由用户后续自行收紧。

## 2026-04-28 / Capability Gap

状态：部分落地

思路：
- 如果最终没有任何上级代理能找到 skill/tool 解决问题，不应该只报失败。
- 应记录 capability gap，作为后续自学习或工具建设的输入。

建议字段：

```yaml
missing_capability: 缺少什么能力
source_task: 来源任务
attempted_skills: 已尝试 skill
attempted_tools: 已尝试 tool
why_failed: 为什么失败
needed_outputs: 需要产出什么
suggested_skill: 是否建议沉淀成 skill
suggested_tool: 是否建议开发成 tool
```

后续方向：
- `/gaps` 查看能力缺口。
- `/gaps learn <id>` 生成学习候选。
- `/gaps close <id>` 标记已解决。

## 2026-04-28 / 用户真实痛点：Subagent 假完成与失控

状态：设计中

来源：
- 用户基于真实使用经验总结。

核心痛点：
- Fake Done / 假完成：子代理说“完成了”，但实际文件缺、功能缺、按钮没反应、没有真实验证证据。
- 只测设计路径，不测用户真实入口：内部路由 PASS，但直接打开短路径、刷新、复制链接会 404 或不可用。
- 父会话派完就放养：只 spawn，不持续 due-check，不主动看实际产出。
- 子代理长时间静默 / 卡死 / timeout 不合理：父会话没有及时接管、改派或缩小目标。
- 子代理输出不直接落盘：只在聊天里汇报，压缩或丢会话后成果难恢复、难验收。
- 任务拆得太粗或太散：太粗吃不下，太散难集成、互相覆盖。
- 没有明确 owner / supervisor / final_owner：并行任务没人统一收口，容易双写或漏验收。
- 缺独立验收层：子代理自己说通过就算通过，缺 dev → test → verify / reviewer / smoke test 闭环。
- 验收只看文件存在，不看行为可用：文件存在不等于功能可用，必须跑命令、测流程、看日志或截图。
- 缺真实来源核查：调研类任务不能只凭模型知识，必须访问真实 URL 并记录来源。
- 没有 skill 匹配和使用证据：子代理是否真的读了 skill 不可审计。
- 通道故障和任务失败混在一起：runtime/session/adapter 故障不等于任务本身失败。
- 工具失败后停住：一个工具没结果就卡住，没有 fallback 链继续尝试。
- 上下文断片 / 历史 session 污染判断：旧 session 的“完成”可能误导当前状态。
- 收口不完整：代码做完但 RESULT / EVIDENCE / TEST_CHECKLIST / SKILL_SPARK / lessons 没补。
- 没有把踩坑沉淀成 skill：BLOCKED 修好了但没提炼成长期规则。
- 并行之后缺统一集成验证：子任务各自通过，但合起来可能路由断、样式冲突、接口不通。
- 对“完成”的标准不够硬：必须按“功能全、流程通、入口测、证据齐、验收过”收口。

框架约束：
- 子代理不能只返回自然语言“完成”；必须产出结构化验收证据。
- 子代理运行记录必须落盘，不能只存在聊天上下文。
- 每个任务必须有 owner、supervisor、final_owner。
- 父代理必须做 due-check，不能 spawn 后放养。
- 任务状态要区分 `DONE`、`FAILED`、`BLOCKED`、`CHANNEL_ERROR`、`TIMEOUT`。
- 工具失败要记录 failure mode，并触发 fallback 或 capability_request。
- 完成前必须经过独立 verification 或 final_owner 收口。
- 真实用户入口、刷新、短路径、复制链接等要进入验收 checklist。
- 调研任务必须记录 URL / 来源 / 访问时间。
- 子代理必须记录使用了哪些 skill/tool card。
- 未解决或反复出现的问题要生成 capability_gap 或 learning_draft。

后续优先落地：
- `SubAgentTask` 增加 owner / supervisor / final_owner。
- `SubAgentTask` 增加 verification_status / evidence / acceptance_checks / failure_type。
- 能力配置增加 heartbeat / due-check / timeout / evidence 最小要求。
- 增加 `/subagents` 和 `/subagent <id>` 查看运行树与验收状态。
- 增加 Fake Done 防护：没有 evidence 的任务不能标记为 DONE。

已落地底座：
- `SubAgentTask` 已有 owner / supervisor / final_owner。
- `SubAgentTask` 已有 verification_status / evidence / acceptance_checks / failure_type。
- `CapabilityConfig` 已有 heartbeat / due-check / run timeout / minimum evidence 配置。
- `SubAgentManager.set_status(..., require_evidence=True)` 已禁止无证据 DONE。
- `agent_py_agent/tests/test_agent.py` 已覆盖 Fake Done 防护。

尚未落地：
- 父代理已有手动 due-check 报告，但还没有自动周期巡检和自动接管。
- 子代理还没有自动心跳。
- 真实入口验收、URL 来源核查、截图/日志证据还只是字段结构，没有自动执行器。
- 收口文档 RESULT / EVIDENCE / TEST_CHECKLIST / lessons 还没有自动检查。

## 2026-04-29 / 外部痛点文档：DISPATCH_PAIN_POINTS

状态：已记录，部分约束已落地

来源：
- `/Users/example/Downloads/DISPATCH_PAIN_POINTS.md`
- 文档生成时间：2026-04-28
- 原归属目录：`/Users/example/.长期助手/tasks/2026-04-28/dispatch-pain-points/`

定位：
- 这是派工 / 子代理 / 小傻妞痛点全集。
- 内容覆盖 P0 / P1 / P2 三层痛点、派工前 Checklist、标准派工 Prompt 骨架。
- 本文档是后续 subagent、capability routing、自学习、验收闭环的重要输入源。

相对已有台账新增或强调的关键点：
- workdir 参数不可靠，子代理产物可能散落 HOME。
- 子代理启动后必须立刻创建工单目录，并写 `STATUS.md` / `WORK_LOG.md`。
- 派工前必须做通道健康 probe，ACP / runtime / adapter / output path 坏了不能批量派工。
- completion notification 不等于完成，完成后父代理必须读取产物并独立验收。
- takeover 必须写 `TAKEOVER.md`，锁定文件，避免 parent/child 双写。
- `BLOCKED` 不能当终点，必须分类处理：工具/环境、任务太难、缺信息、权限禁止。
- 强依赖任务不能并行猜产物，必须通过 `output.json` / `dependencies.json` 传递。
- Markdown 台账不是事实源，机器判断必须读 JSON / JSONL。
- 中途汇报必须和最终汇报区分，避免用户误以为已经结束。
- 禁止用 sleep / heartbeat 伪装长任务，长任务必须有真实阶段产出。
- 子代理输出必须短摘要 + 证据路径，长日志落盘。
- 任务现场文件名需要统一，例如 `STATUS.md`、`WORK_LOG.md`、`RESULT.md`、`EVIDENCE.md`、`TESTS.md`、`ACCEPTANCE.md`、`DEBRIEF.md`。
- 子代理报告必须有问题分级 P0/P1/P2。
- 派工前要做成本判断：秒级可验证用工具，多步骤判断/迭代/验收才派 subagent。

应转成框架的硬约束：
- Subagent run 必须有 `workdir` / `task_dir` / `artifact_dir`，并默认禁止写 HOME、Desktop、Downloads、`.通道运行时`。
- Subagent run 必须有 `status_file`、`work_log_file`、`acceptance_file`、`debrief_file` 等标准产物路径。
- Subagent run 必须支持 `dependencies` 和 `output_json`。
- Subagent run 必须记录 `channel_status` / `failure_type`，区分任务失败和通道失败。
- Subagent run 必须支持 `takeover_by`、`locked_files`、`takeover_reason`。
- Subagent run 必须记录 `model_name` 和实际运行模型。
- Subagent run 必须记录 `skill_paths_read` / `tool_cards_used`，方便审计。
- 父代理必须维护 machine-readable board，而不是只看 Markdown。
- 完成状态必须经过 `ACCEPTANCE PASS` 或 final_owner 收口。

已落地相关底座：
- `SubAgentTask` 已有 owner / supervisor / final_owner。
- `SubAgentTask` 已有 evidence / acceptance_checks / failure_type。
- `CapabilityConfig` 已有 due-check、heartbeat、run timeout、minimum evidence 配置。
- `CapabilityRequest` / `CapabilityGrant` / `CapabilityGap` 已有数据结构。
- `SkillCard` / `CapabilityCard` / `CapabilityRouter` 已有底座。

已落地底座：
- 标准工单目录模板。
- `task_dir` / `data_dir` / `output_dir` / `tests_dir` / `reports_dir` / `logs_dir` / `scratch_dir`。
- `allowed_write_roots` / `forbidden_write_roots`。
- `STATUS.md` / `WORK_LOG.md` / `ACCEPTANCE.md` / `DEBRIEF.md` 自动初始化。
- `TAKEOVER.md` 接管记录。
- `output.json` / `dependencies.json` 自动初始化。
- `validate_work_order(run_id)` 校验关键目录和文件是否存在。
- `record_takeover()` 记录 takeover_by、reason、locked_files，并把任务状态置为 `TAKEN_OVER`。
- `subagents-probe` 可以检查本地工单现场通道健康，并记录 `channel_status`。

尚未落地但优先级高：
- channel probe 还需要接模型接口、ACP / adapter / session 和工具通道。
- P0/P1/P2 问题分级已进入 due-check 报告，但还没接入执行器、用户汇报和自动接管流程。
- 子代理模型字段。
- 派工前工具 vs subagent 成本判断。

## 2026-04-29 / 外部痛点文档：PAIN_POINTS 小中大型/超大型任务

状态：已记录，部分约束已落地

来源：
- `/Users/example/Downloads/PAIN_POINTS.md`
- 文档更新时间：2026-04-29T00:23:18
- 范围：不重复派工痛点本身，聚焦小众、大型、超大型任务里的上下文、证据、边界、验收和恢复问题。

去重判断：
- 已由现有框架覆盖或部分覆盖：Fake Done 防护、父代理独立验收、runner 不直接 DONE、子代理工单落盘、due-check、channel probe、gateway 恢复、LocalStore/timeline、目录边界字段、patch review、防止空心 heartbeat。
- 与 `DISPATCH_PAIN_POINTS` 重叠：completion notification 不等于完成、父代理不能放养、通道故障和任务失败要分离、P0/P1/P2 分级、子代理输出必须落盘、工具失败要 fallback。

新增或强调的关键痛点：
- 小任务容易裸做：不建现场、不测入口、不留 Action Receipt，把 import PASS 当功能 PASS。
- 中任务容易边界不清：缺 SPEC、缺 `SKILL_USAGE.md`、测试清单不是从需求正推、只测核心路径不测异常路径。
- 大任务容易集成漏项：缺架构/接口契约，横向拆太多但没有竖向闭环，并行后缺集成验收。
- 超大型任务容易状态失控：跨天/压缩/多会话后上下文断片，任务树、owner、交付物、依赖关系失控。
- 不能把时长当工作量，禁止 sleep/heartbeat/cron 空循环凑自治时长。
- 长上下文恢复必须靠任务目录和最小恢复入口，而不是靠聊天记忆。
- 安全/敏感任务要和普通 QQ/聊天主会话隔离，短摘要回流，失败要区分模型风控、工具缺失、权限、网络、证据不足。
- 小众领域不能用通用模板糊过去，开工前要读 skill / references / 外部知识库，并记录使用证据。
- 数据规模必须可断言，例如 `stats.json` 或测试脚本断言，不接受少量 demo 数据冒充规模交付。
- 浏览器/Web/PWA/游戏验收不能只看首页，要覆盖导航、输入、点击、状态变化、localStorage、控制台错误、移动端触控、失败提示。
- 文件很多时必须明确当前事实源：`STATUS.md` 当前状态，`ACCEPTANCE.md` 验收，`TESTS.md` / `TEST_CHECKLIST.md` 验证，`DEBRIEF.md` 复盘。

本轮已落地：
- 子代理标准工单新增恢复/证据入口：
  - `ACTION_RECEIPTS.md`
  - `TEST_CHECKLIST.md`
  - `BUGS.md`
  - `SKILL_USAGE.md`
  - `HANDOFF.md`
- `validate_work_order()` 会把这些新增文件纳入工单完整性检查；旧工单可通过 `repair_work_order` 补齐。
- execution context 的 write boundary 会把这些入口路径下发给 runner，方便子代理按最小事实源写证据。

仍未开发，先记录：
- 顶层任务现场模板：按 small / medium / large / huge 生成 `STATUS.md`、`SPEC.md`、`HANDOFF.md`、`BUGS.md`、`ACTION_RECEIPTS.md`、`SKILL_USAGE.md`、`TEST_CHECKLIST.md`。
- SPEC 编号到 TEST_CHECKLIST / Evidence 的追踪链，避免功能数量多时漏项。
- Tool fallback log：`TOOL_FALLBACK_LOG.md` / `diagnostics.md`，并把工具可用性反馈回 Tool Card。
- Browser/PWA/Game 自动验收器：关键路径、控制台错误、移动端触控、重载恢复、localStorage 检查。
- 数据规模断言器：读取 `stats.json` 或生成测试，确认文档数、切片数、问题数、视图数等不缩水。
- 安全任务隔离策略：独立任务目录、短摘要回流、模型风控分类、`TRIED / FINDINGS / NEXT_ANGLES`。
- 跨天 daily memory checkpoint / handoff 自动生成。
- 顶层 boundary doctor：检查任务是否越权写 HOME、Desktop、Downloads、`.通道运行时` 等禁区。
- “竖向闭环优先”调度策略：大任务先跑一条从入口到验收的可用链路，再横向扩规模。

## 2026-04-29 / 文档基线更新

状态：已落地

思路：
- 之前 README 仍停留在早期简版，已经跟当前 capability / subagent / runner 体系不匹配。
- 睡前需要把主链路、命令、协议、安全边界和测试策略写清楚，避免后续 AI 接手时只靠上下文记忆。

已落地：
- 重写根目录 `README.md`：项目总览、快速开始、配置、关键文档、安全边界和测试提醒。
- 重写 `agent_py_agent/README.md`：包内 CLI 使用说明、工具、记忆、subagent 命令和 runner 协议入口。
- 新增 `SUBAGENT_RUNBOOK.md`：详细说明 subagent / capability / runner 的运行流、工单目录、命令、结构化输出协议、写回规则和安全边界。
- 更新 `TESTS.md`：区分安全定向测试和可能触发真实 API 的完整冒烟脚本。
- 更新 `TEST_CHECKLIST.md`：按普通 run、工具、capability、subagent、runner、文档分组列检查项。
- 更新 `CODEBASE_TREE.md`：加入 `SUBAGENT_RUNBOOK.md` 并说明职责。

后续要求：
- 改 subagent / capability / runner 主链路时，除了代码和测试，也要同步检查 `SUBAGENT_RUNBOOK.md`。

## 2026-04-29 / Subagent 验收器

状态：已落地

思路：
- runner 完成后只能进入 `AWAITING_ACCEPTANCE` / `NEEDS_ACCEPTANCE`，不能自己标记 DONE。
- 父代理需要一个独立验收入口，读取 evidence、tests、patches、blockers、capability request/gap 和 runner 结构化输出。
- 默认必须 dry-run，只有显式 apply 才能写回状态。

已落地：
- `AcceptanceReviewFinding` / `AcceptanceReviewRecord` / `AcceptanceReviewReport`。
- `SubAgentManager.review_acceptances()`：批量验收等待验收的 run。
- `SubAgentManager.write_acceptance_review_report()`：写出 `subagent_acceptance_report.json` 和 `SUBAGENT_ACCEPTANCE.md`。
- `python3 -m agent_py_agent subagents-acceptance`：默认 dry-run。
- `python3 -m agent_py_agent subagents-acceptance --apply --run-id <run_id>`：验收通过时标记 `DONE/VERIFIED`，失败时标记 `BLOCKED/FAILED`。

当前验收检查：
- 工单现场完整。
- 任务确实处于等待验收状态。
- 通道不是 `BROKEN`。
- runner 结构化输出可解析，或至少能按人工证据验收。
- 至少有一条 ok evidence。
- 没有失败 evidence。
- 没有 open capability request/gap。
- `output.json` 没有 blocker。
- tests 不失败。
- patches 没有 `planned` / `blocked` 未处理项。

后续方向：
- 验收器可以接入可执行测试命令，但必须先做命令 allowlist 和超时审计。
- patch apply 需要独立审核链路，不能由验收器直接应用未知 patch。

## 2026-04-29 / 测试默认真实 API

状态：已落地

思路：
- 后续验收级、冒烟和回归测试默认直接调用真实 API。
- echo/fake backend 只能用于纯函数、解析器和局部单元定位，不能作为最终通过依据。
- 完整冒烟入口 `python3 agent_py_agent/tests/run_tests.py` 应按当前配置请求真实模型 API。

已落地：
- 更新 `TESTS.md`，把真实 API 作为标准完整冒烟要求。
- 更新 `TEST_CHECKLIST.md`，要求收口前运行完整冒烟并确认使用真实 API。
- `agent_py_agent/tests/run_tests.py` 改为自动发现并运行所有 `test_*.py` / `test_` 函数，避免手写清单漏掉新增测试。

后续注意：
- 如果真实 API 不稳定，要记录失败类型，而不是直接降级成 echo 后端通过。
- 新增测试命令时，区分“局部定位测试”和“真实 API 收口测试”。
- 新增 `test_*.py` 或 `test_` 函数后，不需要手动加入完整冒烟清单，但必须确认完整冒烟脚本发现了它。

## 2026-04-29 / 真实 API E2E、测试隔离和 patch 审核链

状态：已落地

思路：
- 完整冒烟不能只跑普通 `run/chat`，还要覆盖真实 API 的 `subagent-run --execute`。
- 冒烟测试不能污染默认记忆和默认 subagent 工单目录。
- runner 声明的 `patches` 需要独立审核链路；验收器不能直接把未审核 patch 当成完成。

已落地：
- `agent_py_agent/tests/run_tests.py` 创建临时配置，隔离 `memory_path` 和 `subagent_workspace`。
- 完整冒烟会创建真实 API 子代理工单，运行 `subagent-run --execute`，确认 backend 不是 echo、工具调用发生、结构化输出可解析、evidence 写回，并由父代理验收为 `DONE/VERIFIED`。
- 新增 `PatchReviewRecord` / `PatchReviewReport`。
- 新增 `SubAgentManager.review_patches()` 和 `write_patch_review_report()`。
- 新增 `python3 -m agent_py_agent subagents-patches`，默认 dry-run，显式 `--apply` 才写回 `review_status`。
- 验收器新增 `patches_reviewed` 和 `patch_status_valid` 检查：未审核的 applied patch、planned/blocked patch、未知状态 patch 都不能进入 `DONE/VERIFIED`。

后续注意：
- patch 审核器目前只审核 runner 已声明的 patch 状态，不自动应用 diff。
- 后续真正做 patch 集成器时，需要 owner、写入边界、diff 审计和测试命令 allowlist。

## 2026-04-29 / 父代理一轮调度器

状态：已落地

思路：
- 当前 chat 退出后父代理不常驻，但工单状态已经可恢复。
- 在做 daemon 前，先把“一轮父代理应该如何推进任务树”做成可审计命令。
- 调度器必须默认 dry-run；真实 runner 调用需要比普通 apply 更明确的开关。

已落地：
- 新增 `DispatchRecord` / `DispatchReport`。
- 新增 `SimpleAgent.dispatch_subagents()`：按 due-check、action apply、capability route、runner、patch review、acceptance 顺序执行一轮调度。
- 新增 `python3 -m agent_py_agent subagents-dispatch`。
- `subagents-dispatch --apply` 会写回低风险动作、能力路由、patch 审核和验收，并写调度审计日志。
- `subagents-dispatch --apply --execute-runners` 才会调用模型 runner。
- 新增 dispatch 单测：dry-run 规划、patch 审核后验收、runner 执行后验收。

后续方向：
- 在 dispatch 稳定后加 `--watch` 或独立 daemon。
- daemon 需要运行锁、停止信号、轮询间隔、最大 API 消耗和崩溃恢复记录。

## 2026-04-29 / 父代理 watch 模式

状态：已落地

思路：
- 一轮 `subagents-dispatch` 已经能推进任务树，但退出后不会继续巡检。
- 在独立 daemon 前，先让同一个命令支持 watch 循环，并保持 CLI 可测试、可退出。
- watch 必须有运行锁，避免两个父代理同时推进同一批工单。

已落地：
- 新增 `DispatchWatchRecord` / `DispatchWatchReport`。
- 新增 `SimpleAgent.watch_subagents()`。
- `python3 -m agent_py_agent subagents-dispatch --watch` 会持续循环 dispatch。
- `--interval` 控制轮询间隔，`--max-cycles` 控制安全退出，`--force-lock` 用于人工处理残留 lock。
- watch 写 `subagent_dispatch_watch_heartbeat.json`、`subagent_dispatch_watch_report.json`、`SUBAGENT_DISPATCH_WATCH.md`、`subagent_dispatch_watch_log.jsonl` 和 `DISPATCH_WATCH_LOG.md`。
- `--execute-runners` 现在必须和 `--apply` 同时使用。

后续方向：
- 增加外部停止信号或 stop 文件。
- 增加 API 消耗预算、每轮最大耗时和 daemon/service 包装。

## 2026-04-29 / 安装后命令入口

状态：已落地

思路：
- 现在可以用 `python3 -m agent_py_agent` 运行，但正式 agent 更应该安装后直接敲命令名。
- 暂定命令名为 `my-agent`，后续品牌名确定后只改 packaging script，不改业务入口。

已落地：
- 新增 `pyproject.toml`。
- 新增 console script：`my-agent = agent_py_agent.__main__:main`。
- 新增 `agent_py_agent/__init__.py`，让包在标准 packaging 下更明确。

后续方向：
- 增加 gateway 命令族：`my-agent gateway start/status/stop`。
- gateway 负责后台常驻、pid/lock/heartbeat、日志和外部控制；现有 `subagents-dispatch --watch` 作为 gateway 的核心工作循环。

## 2026-04-29 / 父代理 LLM planner

状态：已落地

思路：
- 仅靠 heartbeat 容易变成“报平安”，不能保证进入完整 LLM 决策链。
- watch 的规则调度器能推进 runner/验收/能力路由，但缺少父代理自己读状态并给出行动建议的一层。
- planner 必须有 gate：有 active/pending/stalled/needs-intervention 时，不允许模型只返回 `HEARTBEAT_OK`。

已落地：
- 新增 `ParentPlannerParsedOutput` / `ParentPlannerRecord` / `ParentPlannerReport`。
- 新增 `SimpleAgent.run_parent_planner()`，通过 `SimpleAgent.run()` 触发完整父代理 LLM turn，允许只读工具核对状态。
- `subagents-dispatch --planner` 会先收集 board、due-check、action plan、runner candidates、patch review、acceptance、open capability request/gap。
- gate 非空时调用父代理 LLM；如果模型只回 `HEARTBEAT_OK`，记录为失败。
- planner 输出 `runner_instruction` 可作为本轮 runner 的补充指令；`suggested_max_runners` 只能降低 CLI 上限，不能提高。
- 新增 planner prompt/response/report/log 审计文件。

后续方向：
- 让 planner 的 action plan 接入更丰富的受控动作，如 spawn-subagents、reassign、takeover。
- gateway 后台化后，把 planner tick 作为后台事件的一等公民。

## 2026-04-29 / 配置驱动 daemon 入口

状态：已落地

思路：
- `subagents-dispatch --watch --planner --apply --execute-runners --interval 30 --max-runners 1` 太长，不适合作为日常启动命令。
- 在 gateway/service 形态确定前，先提供 `my-agent daemon` 前台入口，并把常驻参数移到 `agent_config.yaml`。
- 默认配置必须安全：不写回、不执行 runner；用户确认后再把 `daemon_apply` 和 `daemon_execute_runners` 改为 true。

已落地：
- 新增 `daemon_*` 配置：planner、apply、execute_runners、interval、max_runners、limit、max_cycles、max_cards、probe、reviewer、runner_instruction。
- 新增 `my-agent daemon`，按配置启动前台常驻调度。
- daemon 支持少量 CLI override，用于临时测试或手动覆盖配置。
- 明确 `0` 值语义：`max_cycles=0` 持续运行，`max_runners=0` 不执行 runner，`max_cards=0` 不限制，`interval=0` 不等待且主要用于测试。

后续方向：
- 在这个入口之上做真正后台 `gateway start/status/stop/restart`。
- 增加 pid 文件、stdout/stderr 日志轮转和 Windows 后台进程管理。

## 2026-04-29 / Gateway 参数分层

状态：已落地配置层

思路：
- 普通用户不应该理解 `interval`、`max-runners`、`limit` 这类底层调度参数。
- 用户层只关心任务规模，比如最多多少子代理/孙代理；不关心每轮调度多少条。
- 高级用户可以自己调，但默认应该是 `auto` 或“不设硬上限”。

已落地：
- 新增 `GATEWAY_DESIGN.md`，记录 通道运行时、长期助手、模型助手 session 和 会话运行时 云任务形态的参考。
- 新增 `GATEWAY_RESEARCH.md`，扩展调研到进程守护、AI gateway、Jupyter kernel、Celery/RQ/n8n、Temporal/LangGraph/CrewAI、Node-RED/Home Assistant、Ollama/PM2/Supervisor/Task Scheduler 等方案。
- 新增用户层任务规模配置：`task_max_subagents`、`task_max_grandchildren`，默认 `0` 表示不设硬上限。
- 新增未来 gateway 策略配置：`scheduler_mode`、`runner_concurrency`、`runner_start_rate`、`runner_timeout_seconds`、`runner_failure_policy`，默认 `auto`。
- `daemon_max_runners` 默认改为 `"auto"`；当前前台 daemon 会映射成保守值 1。
- `daemon_limit` 默认改为 `0`，表示每个阶段不限制记录条数。

后续方向：
- 做真正的 `my-agent gateway start/status/stop/restart`。
- 把 `runner_concurrency` 和 `runner_start_rate` 接到后台 worker pool，而不是前台同步循环。

## 2026-04-29 / Gateway 独立主体与委托模型

状态：已落地设计

思路：
- 多 gateway 不应该是“主完整、副低配”的关系。
- 每个 gateway 都是完整独立 agent，有自己的身份、任务账本、记忆、工具、密钥和能力目录。
- 上下级只表示授权、委托、汇报和协调关系，不削弱任何 gateway 的自身能力。

已落地：
- `GATEWAY_DESIGN.md` 增加多 gateway 组织模型。
- 明确 `identity 决定所有权，grant 决定访问权，delegation 决定协调权`。
- 明确 root gateway 可保留 reclaim 协调权，但 active coordinator 不自动获得 root 的全部私有状态。
- 第一版单机 gateway 的 schema 预留 `gateway_id`、`agent_identity_id`、`coordination_epoch`、`delegation_id`、`grant_scope`、`attempt_id` 等字段。

后续方向：
- 第一版仍从单机 gateway 做起，但任务账本和事件日志提前带 gateway/identity/delegation 字段。
- 后续再做跨机器通信、授权交换、本体备份和迁移。

## 2026-04-29 / Organization Gateway Model

状态：已落地设计

思路：
- my-agent 可以组成组织：root gateway 创建组织，其他人或机器通过 invite key 安装并注册自己的完整 my-agent。
- 副 gateway 可以在授权范围内继续邀请自己的下级 gateway，形成公司/部门/成员式组织树。
- 组织关系随时可调整，但调整的是协调权和授权范围，不是 gateway 自身能力。

已落地：
- `GATEWAY_DESIGN.md` 增加 Organization Gateway Model。
- 设计 invite key 字段：`org_id`、`issued_by_gateway_id`、`parent_gateway_id`、`allowed_scopes`、`can_invite_children`、`expires_at`。
- 明确组织架构要有独立状态：parent/children、membership、capability summary、last_seen、trust level、grants、delegations、coordination epoch。
- 明确组织事件日志：join/leave/suspend/revoke/delegate/reclaim/reparent/invite/grant/revoke。
- 增加开工前待补充清单：invite 签名撤销、secret 管理、artifact 同步、离线接管、版本兼容、权限审计和隐私策略。

后续方向：
- 第一版 gateway 先落单机 `gateway_id` / `agent_identity_id` / `org_id` / event log。
- 第二阶段再实现 invite、组织账本和跨 gateway 通信。

## 2026-04-29 / 第一版本地 Gateway 控制面

状态：已落地

思路：
- 先做薄而稳的本地后台外壳，不直接上 worker pool、HTTP API、多机器和组织通信。
- 用户命令面先稳定为 `my-agent gateway start/status/stop/restart/logs`。
- 内部暂时复用现有 daemon/watch 调度，后续再替换成 SQLite jobs 和 worker subprocess pool。

已落地：
- 新增 `gateway` 命令族：`start`、`status`、`stop`、`restart`、`logs`、内部 `run`。
- 新增 gateway 控制面文件：`gateway.pid`、`gateway_state.json`、`gateway_heartbeat.json`、`gateway_stop.request`、`gateway.log`。
- 新增 `gateway_workspace`、`gateway_heartbeat_interval`、`gateway_stale_seconds`、`gateway_stop_timeout` 配置。
- `watch_subagents()` 支持 stop file，gateway stop 可以在调度轮次之间正常退出。

后续方向：
- `chat` / TUI attach 到 gateway。
- 接入 SQLite task ledger、jobs、leases 和 worker pool。
- 增加 systemd / launchd / Windows Task Scheduler 安装入口。

## 2026-04-29 / Gateway 本地消息入口

状态：已落地

思路：
- gateway 不能只是后台调度壳子，还需要接受用户消息并触发完整 LLM turn。
- 第一版先用本地文件 inbox/response 队列，不急着引入 HTTP server、WebSocket 或 SQLite。
- CLI 客户端先验证协议：请求落盘、gateway worker 取走、模型调用、响应落盘、客户端等待或稍后读取。

已落地：
- 新增 `my-agent gateway ask "<prompt>"`，向后台 gateway 投递聊天/任务请求。
- 新增 `my-agent gateway result <request_id>`，读取异步请求结果。
- 新增 gateway 请求目录：`requests/pending`、`requests/processing`、`requests/done`、`responses` 和 `gateway_requests.jsonl`。
- gateway 后台进程启动 request worker，和 dispatch watch 并行常驻。
- `gateway status`/heartbeat 会带 request counts，方便判断是否堆积。

后续方向：
- 让 `my-agent chat` 默认 attach 到 gateway，而不是只在前台进程里跑。
- 将文件队列升级为 SQLite jobs/leases，支持崩溃恢复、重试、超时和 worker pool。
- 再向上接 TUI、HTTP/WebSocket、本地托盘服务和跨 gateway 通信。

## 2026-04-29 / Gateway 说明白话化

状态：已落地

思路：
- `gateway ask/result` 会保留，但它们不是最终普通用户每天必须敲的命令。
- 它们的定位是本地协议验证口、开发者调试口、聊天工具/TUI 接入前的最小客户端。
- 文档和代码注释要把“为什么要有这些命令”“以后接聊天工具后谁来调用它们”“每个队列目录是什么意思”说清楚。

已落地：
- `GATEWAY_DESIGN.md` 增加 `gateway ask/result` 大白话解释和本地队列目录说明。
- `CLI_REFERENCE.md` 增加同步/异步示例、请求流转和定位说明。
- `README.md`、`agent_py_agent/README.md` 增加普通用户视角解释。
- `agent_config.yaml` 增加 gateway 请求队列配置注释。
- `agent_py_agent/__main__.py` 增加 `GatewayPaths`、请求写入、崩溃恢复、worker loop 和 response 输出的代码注释。

后续方向：
- `chat` / TUI 接入后，把 `gateway ask/result` 收到“开发者命令”层级。
- 外部聊天工具接入时复用同一个 request/response 协议，并把响应自动回发给用户。

## 2026-04-29 / Chat 接入 Gateway

状态：已落地第一版

思路：
- 先不改变 `my-agent chat` 的默认行为，避免破坏现有前台交互路径。
- 新增 `my-agent chat --gateway`，让 chat 成为 gateway 客户端。
- 普通自然语言消息走 gateway request/response；本地命令 `/memory`、`/remember`、`/subagents` 暂时继续在当前 CLI 里处理。

已落地：
- `chat --gateway` 启动时检查 gateway 是否正在运行；未运行时提示先 `my-agent gateway start`。
- chat worker 复用 `submit_gateway_ask()` 写入 gateway inbox，并等待 response。
- `/status` 在 gateway 模式下额外显示 gateway 存活状态和 pending/processing/done/responses 数量。
- 新增 `--gateway-timeout`，可临时覆盖单条消息等待时间。

后续方向：
- 稳定后考虑让 `my-agent chat` 默认 attach 到 gateway。
- 把更多 slash command 也改成 gateway 请求，减少前台 CLI 对本地状态的直接操作。
- 给 chat/TUI 增加异步完成通知，而不是每条消息都由当前 worker 等 response。

## 2026-04-29 / 默认入口自动进入 Gateway Chat

状态：已落地

思路：
- 用户安装后应该能直接敲 `my-agent` 使用，不需要先学习 `gateway start` 和 `chat --gateway`。
- 默认入口应该自动确保 gateway 存活，然后进入 gateway chat。
- 退出 chat 不关闭 gateway，保持“前台客户端可退出，后台本体继续值班”的体验。

已落地：
- argparse 子命令改为可选；没有子命令时进入 `cmd_default()`。
- `cmd_default()` 调用 `ensure_gateway_started()`，未运行则自动 `gateway start`。
- gateway 存活后，默认入口进入 `cmd_chat()` 的 gateway 模式。

后续方向：
- 观察默认入口稳定性，再考虑是否让显式 `my-agent chat` 也默认 attach gateway。
- 补更好的首次启动引导，例如配置 API key、模型后端和 gateway 状态提示。

## 2026-04-29 / 主代理自然语言派工工具

状态：已落地

背景：
- 之前 chat 里的主代理会建议“可以拆给 subagent”，但普通自然语言消息不能稳定地真正创建工单和触发调度。
- CLI 已有 `spawn-subagents`、`subagents-dispatch` 等命令，但它们还没有进入主代理可调用工具目录。
- 后续要做隔离版全流程场景测试，需要从 chat/gateway 入口模拟真实用户派活，而不是只靠手动 CLI 拼流程。

已落地：
- 新增 `create_subagents` 主代理工具：把自然语言里的拆分/派工意图落成子代理工单。
- 新增 `subagent_board` 主代理工具：让主代理读取子代理看板、状态和风险旗标。
- 新增 `dispatch_subagents` 主代理工具：让主代理触发一轮父代理调度。
- `dispatch_subagents` 默认 dry-run；`execute_runners=true` 必须配合 `apply=true`，避免误触发真实 runner/API。
- `create_subagents` 默认授予 read-only 工具；只有 `tool_preset="coding"` 或显式 `allowed_tools` 才给写文件能力。

测试：
- 新增工具级回归：模拟模型在普通 `agent.run()` 中调用 `create_subagents`，确认子代理工单真实落盘。
- 同一测试覆盖 `subagent_board`、`dispatch_subagents` dry-run，以及未 `apply=true` 时拒绝真实 runner。

后续方向：
- 基于这些工具增加隔离 fixture 全流程场景测试：chat -> 创建多个子代理 -> dispatch -> runner -> 验收 -> 汇报。
- 给真实场景测试固定临时 `memory_path`、`subagent_workspace`、`gateway_workspace` 和 fixture 工作区，确保不污染当前开发仓库。

## 2026-04-29 / 隔离全流程场景测试

状态：已落地第一版

背景：
- 用户希望能“看完整流程”，而不是只跑分散的单元测试或 smoke。
- 全流程测试必须能调用真实 API，但不能污染当前开发仓库，也不能把 runner 文件写到项目源码里。
- gateway、主代理自然语言派工、subagent runner 和父代理验收需要被串起来观察。

已落地：
- 新增配置 `workspace_root`：为空时保持默认项目根；设置后 memory、subagent、gateway、prompt_files 和文件工具都以该目录为根。
- 新增 `my-agent scenario-test`：
  - 每次创建独立 `scenario-*` 临时目录和 `fixture_project`。
  - 写入隔离配置，把 `workspace_root` 指向 fixture。
  - 默认启动隔离 gateway，使用 `gateway ask` 触发主代理创建多个子代理。
  - 随后由当前进程执行 dispatch，允许真实 runner/API、写入 fixture 内报告，并执行父代理验收。
  - 输出看板、runner 证据文件、`scenario_summary.json` 和 `SCENARIO_SUMMARY.md`。
- 修复真实场景暴露的问题：
  - 同一轮 `run()` 内重复的编排工具调用会被去重，避免模型反复创建相同子代理。
  - 达到工具轮数上限时会再让模型生成最终回答，不再直接返回最后一个工具调用块。
  - 工具解析器兼容模型把 `[TOOL_CALL]` 误写成 `[SUBAGENT_CALL]` 的常见情况。
  - runner 回写会记录系统真实执行过的工具；验收不再相信模型自称的 `used_tools`。
  - 验收会按 `acceptance_checks` 核对 `read_file/write_file` 证据，并检查本地 artifact 路径是否真实存在。
  - 有工具执行记录时，prompt 会把 `Tool Transcript` 放在用户任务之后，并追加继续指令，避免模型每轮被末尾任务说明拉回起点、重复调用同一个工具。

安全边界：
- 默认不复用开发仓库的 `data/`。
- runner 写文件工具只能看到 fixture 工作区。
- `--direct` 可跳过 gateway，便于定位 gateway 和主代理自身的问题差异。
- `--dry-run` 可跳过真实 runner API，只观察派工和调度计划。

后续方向：
- 增加更极端的 fixture：runner 失败、结构化输出损坏、能力请求、stalled、gateway 重启恢复。
- 把场景测试报告做成更适合前端/TUI 展示的事件时间线。

## 2026-04-29 / 坏天气场景测试第一版

状态：已落地第一版

背景：
- happy path 已经能证明主链路能跑通，但真正要长期可靠，需要把失败和恢复场景也做成可重复测试。
- 这些场景必须继续隔离运行，不能污染开发仓库。

已落地：
- `my-agent scenario-test --case verification`
  - 构造一个伪造完成的子代理：声称 read/write 成功，也声称有 artifact。
  - 实际不写 artifact 文件。
  - 父代理验收必须拒绝，并把任务标成 `BLOCKED / FAILED`。
- `my-agent scenario-test --case gateway-restart`
  - 模拟旧 gateway 崩溃时请求卡在 `requests/processing`。
  - 执行 gateway 启动恢复步骤，把请求退回 `requests/pending`。
- `my-agent scenario-test --case structured-repair`
  - 模拟 runner 已回复但 `[SUBAGENT_RESULT]` JSON 损坏。
  - 父代理触发修复回合，只整理格式，不新增事实。
  - `runner_result.json` 记录 `structured_repair_attempted=true` 和 `structured_repair_ok=true`，修复后进入验收。
- `my-agent scenario-test --case runner-retry`
  - 模拟 runner 第一次模型调用出现临时错误。
  - 任务先落成 `BLOCKED / runner_error`，记录 `runner_attempts=1` 和最后错误。
  - 下一轮 dispatch 识别为可重试错误，执行 `retry_runner`，成功后进入父代理验收并变成 `DONE / VERIFIED`。
- `my-agent scenario-test --case all`
  - 依次运行 `verification`、`gateway-restart`、`structured-repair`、`runner-retry`、`happy`。

调度策略：
- 新增 runner 尝试计数：`runner_attempts`、`runner_last_attempt_at`、`runner_last_error`。
- `runner_failure_policy: "auto"` 当前表示总尝试次数为 2；`"off"` 表示不自动重试；数字字符串如 `"3"` 表示最多尝试 3 次。
- 自动重试只覆盖 `runner_error`、`structured_output_parse_error`、`tool_result_missing`、`model_error`、`api_error`、`transient_error`。
- 不自动重试能力缺口、验收失败、通道 BROKEN、接管任务，避免父代理在权限或事实不明时重复消耗 API。

后续方向：
- 增加 stalled 接管、能力缺口上抛再 rerun 的场景。
- 给 `scenario_summary` 增加事件时间线，方便 TUI/网页观察。

## 2026-04-29 / Qwen XML-ish 工具调用兼容

状态：已落地

背景：
- 真实模型或其他 agent runtime 可能不会严格输出我们提示词里的 `[TOOL_CALL]` JSON，而是输出类似 Qwen/通道运行时 的 XML-ish 方言：`<tool_call><function=read><parameter=file_path>...</parameter></function></tool_call>`。
- 之前这类输出会落在解析器外；如果 runtime 侧尝试解析不完整片段，还可能出现 `Failed to parse input at pos ...` 一类错误。

已落地：
- `ToolRegistry.parse_tool_calls()` 继续优先支持标准 `[TOOL_CALL]` / `[SUBAGENT_CALL]` JSON 块。
- 新增 XML-ish `<tool_call>` 方言解析：`read` -> `read_file`、`write` -> `write_file`、`search`/`grep` -> `search_text`、`list`/`ls` -> `list_files` 等常见别名会自动归一化。
- 参数别名会归一化：`file_path`、`filepath`、`filename`、`file` -> `path`。
- 参数内容会做 HTML entity 解码，简单 JSON 值会尽量还原成对象/数组/数字/布尔值。
- 如果 `<tool_call>` 只有半截、缺少 `</tool_call>`，不会让主循环崩溃，而是返回 `__parse_error__`，让后续模型回合有机会修正格式。

测试：
- 新增工具解析回归：标准 `[SUBAGENT_CALL]` alias、XML-ish read、XML-ish write、半截 XML-ish parse error。

后续方向：
- 如果接入更多模型方言，再把解析器扩展成显式 adapter 列表，并记录每种方言的命中率和失败样本。

## 2026-04-29 / Runner actual_tools 系统证据

状态：已落地

背景：
- 真实 runner 可能确实调用了 `read_file` / `write_file`，但结构化 evidence 里只写“读取 README 成功”“写入报告成功”，没有把工具名写进 `kind`、`command` 或 `summary`。
- 旧验收逻辑会因此误判缺少 `read_file` 证据，哪怕系统自己的 `actual_tools` 已经记录了真实工具调用。

已落地：
- `record_runner_result(..., actual_tools=[...])` 会把实际成功执行过的授权工具落成系统证据，格式为 `kind=<tool>`、`command=<tool>`。
- 验收继续优先信任系统真实工具记录，而不是模型自称的 `used_tools`。
- 保留 artifact 路径存在性等独立验收项，避免只有工具名而没有真实交付物时误过。

测试：
- 新增回归：模型 evidence 不写 `read_file` / `write_file` 字符串，但 `actual_tools` 记录真实执行时，父代理验收应通过。

## 2026-04-29 / 代码体检与后续拆分计划

状态：已完成检查，暂不做大规模重构

当前规模：
- tracked 文本总行数约 1.77 万行。
- Python 约 1.35 万行，Markdown 约 0.39 万行。
- 最大文件集中在：
  - `agent_py_agent/agent/subagent.py`：约 4.8k 行，承担子代理模型、存储、看板、due-check、action、capability route、runner、patch、acceptance、dispatch 等职责。
  - `agent_py_agent/__main__.py`：约 2.7k 行，承担 argparse、chat、gateway、scenario-test、daemon、subagent CLI 等职责。
  - `agent_py_agent/agent/core.py`：约 1.6k 行，承担主循环、runner、planner、dispatch 编排工具等职责。
  - `agent_py_agent/agent/tools.py`：约 1.0k 行，承担工具规格、工具实现、检索和工具调用解析。

文档状态：
- `README.md`、`CLI_REFERENCE.md`、`TESTS.md`、`SUBAGENT_RUNBOOK.md`、`GATEWAY_DESIGN.md`、`GATEWAY_RESEARCH.md`、`CODEBASE_TREE.md` 已覆盖当前主链路。
- 本轮补充 `CODEBASE_TREE.md`，加入 `test_cli_reference.py`、`test_packaging.py`，并补充 XML-ish 工具调用解析和 `actual_tools` 系统证据说明。

判断：
- 当前先不急着重构，因为 gateway / scenario-test / runner / acceptance 正处在高频变化阶段，大规模移动代码会增加回归成本。
- 但 `subagent.py` 和 `__main__.py` 已经明显超过长期维护舒适区，后续做 gateway worker pool、SQLite ledger、多 gateway 组织通信前，应该分阶段拆分。

建议拆分顺序：
1. 先拆纯数据和纯解析，风险最低：
   - `agent/subagent_models.py`：dataclass / enum / schema。
   - `agent/subagent_parsers.py`：`parse_subagent_runner_output()`、parent planner parser、结构化输出修复相关纯函数。
   - `agent/tool_call_parser.py`：`[TOOL_CALL]` JSON 和 XML-ish 方言解析。
2. 再拆子代理业务域：
   - `agent/subagent_storage.py`：路径、读写、工单目录初始化。
   - `agent/subagent_acceptance.py`：验收 finding / report / apply。
   - `agent/subagent_dispatch.py`：due-check、action plan、dispatch/watch。
   - `agent/subagent_runner.py`：execution context、runner result、actual_tools 证据。
3. 再拆 CLI：
   - `cli/parser.py`：argparse 构造。
   - `cli/gateway.py`：gateway start/status/stop/ask/result。
   - `cli/scenario.py`：scenario-test fixture 和坏天气场景。
   - `cli/chat.py`：前台 chat 和 gateway chat 客户端。
4. 最后拆测试文件：
   - `test_agent.py` 按 acceptance、dispatch、runner、capability、board/probe 分文件。

约束：
- 每次拆分只移动一个低耦合区域，先保持 import 兼容，跑完整 `run_tests.py` 后再继续。
- 不在同一轮同时改行为和大移动文件，避免不知道失败来自重构还是功能变化。

## 2026-04-29 / 本地恢复、诊断、worker 和 adapter 第一版

状态：部分落地

已落地：
- `local-doctor`：诊断 LocalStore、memory JSONL、gateway 队列和 subagent 工单目录是否一致。
- `local-doctor --repair`：处理超时的 gateway `processing` 请求，未超尝试次数则退回 `pending`，超过则写响应并归档到 `failed`。
- `local-rebuild`：从 memory、gateway、subagent 文件事实源重建 LocalStore，支持 `--source` 和 `--reset`。
- `status` 增加 `suggested_actions`，人类输出也显示建议下一步动作。
- gateway 请求队列新增 `failed` 目录、processing lease、attempts、超时重排和失败归档。
- `gateway_request_workers`：gateway ask/request 第一版保守 worker pool，默认 1 个 worker。
- `runner_concurrency`：runner 并发第一版，默认 `auto -> 1`，只有显式数字才会并行多个不同 run。
- `adapter file`：文件协议适配器，外部聊天工具/TUI 可写 `inbox/*.json`，adapter 投递 gateway 后写 `outbox/*.json`。

仍未开发，先记录：
- LocalStore compact / backup / export / import 命令。
- LocalStore rebuild 的更完整来源覆盖：dispatch/watch/planner 全量历史、任意 `reports/*.json` 的类型化恢复、artifact 大文件索引。
- gateway 请求取消、优先级、租约续期、迟到响应去重策略和更细的错误分类。
- gateway worker 的 API 预算、启动速率、自适应并发和 per-model 限流。
- runner pool 的进程级隔离、session 复用、超时硬中断和跨进程锁。
- adapter 的 HTTP/WebSocket/平台插件版，以及鉴权、会话映射、去重、消息编辑/撤回。
- TUI 观察面板，复用 `status` / `timeline` / `adapter file` / gateway request protocol。

## 2026-04-29 / 注释重构规范

状态：设计已记录，待分阶段落地

背景：
- 当前 Python 代码里函数/类定义约 650 个，其中大量 docstring 是一句话说明，`__main__.py`、`subagent.py`、`core.py`、`tools.py` 尤其需要更清晰的 LLM/人类双层说明。
- 直接全仓一次性重写注释会让 diff 过大，也会进一步放大已经偏大的文件体积；应分模块、分职责迁移。

目标注释格式：

```python
def example(...):
    """LLM: technical summary of the contract, side effects and invariants.

    人话说明：
    这个函数用来做什么，什么时候会被调用。

    具体例子：
    - 输入什么，输出什么。
    - 它会写哪些文件、调用哪些模型或工具。
    - 它失败时怎么表现，调用方应该怎么处理。

    注意边界：
    - 哪些参数不能乱传。
    - 哪些副作用需要审计。
    """
```

规范：
- 第一行给 LLM/后续维护者读，使用技术语言，写清 contract / invariant / side effects。
- 第二行开始给人读，用大白话解释，默认小白能懂。
- 对有副作用的函数必须写清：会不会写文件、调用 API、启动进程、改状态、消费真实模型。
- 对恢复/调度/验收函数必须写清：输入事实源、状态流转、失败时的 fallback。
- 对工具和 adapter 必须写例子，例如一个 JSON 输入如何变成输出文件。
- 测试函数不强求长注释；测试名和断言清楚即可。

建议落地顺序：
1. 先改稳定的小模块：`config.py`、`memory.py`、`local_store.py`、`backend.py`。
2. 再改工具边界：`tools.py`，重点写清读写边界、网络工具、工具解析器。
3. 再改核心编排：`core.py`，重点是 `run()`、`run_subagent()`、dispatch/watch/planner。
4. 再改 CLI：拆分 `__main__.py` 后分别注释 gateway、adapter、scenario、chat。
5. 最后改 `subagent.py`：先拆 models/storage/acceptance/dispatch/runner，再补双层注释。

不建议：
- 不在当前大文件状态下把 650 个函数一次性塞长注释；这会让 `__main__.py` 和 `subagent.py` 更难读。
- 不把注释当设计替代品；复杂流程仍应通过类型、报告 JSON、测试和 runbook 表达。
## 2026-04-30 / 模块文档四件套导航

状态：部分落地

导航：新的模块级长期文档入口是 [docs/modules/README.md](docs/modules/README.md)。每个功能模块使用四件套：`01-discussion.md` 记录灵感和讨论，`02-progress.md` 记录推进、解决的问题和测试，`03-purpose.md` 解释初心和设计想法，`04-structure.md` 说明结构树、核心文件、数据流和新手学习路径。

模块索引已建立：
- [docs/modules/subagent/](docs/modules/subagent/)：链接 subagent 质量契约、受控派工、workflow preview 和旧 runbook。
- [docs/modules/log-analysis/](docs/modules/log-analysis/)：链接日志分析 design、backlog、acceptance、evidence 和当前 work order 方向。
- [docs/modules/memory/](docs/modules/memory/)：链接长期记忆、规则路由、raw archive、恢复和诊断。
- [docs/modules/gateway/](docs/modules/gateway/)：链接后台 gateway、本地请求队列、chat attach、恢复和 adapter。
- [docs/modules/live-lab/](docs/modules/live-lab/)：链接可见真实环境演练、离线 replay、suite/case 产物。

同步门：`scripts/check_doc_sync.py` 已覆盖 `log-analysis`、`subagent`、`memory`、`gateway`、`live-lab`。covered module 改代码时，需要同步更新模块 `02-progress.md`、`04-structure.md`，实现代码新增时还要同文件补注释或 docstring。

维护约定：旧文档暂不搬迁；本台账继续只放摘要和导航，模块细节后续优先追加到 `docs/modules/<module>/`。

## 2026-05-02 / Subagent Autonomous Dispatch 自动化派发

状态：设计中

模块设计文档：[docs/design/subagent-autonomous-dispatch.md](docs/design/subagent-autonomous-dispatch.md)

摘要：

新增 `subagent_automation_level` 配置（1/2/3），控制主代理多大程度优先使用子代理完成任务：
- 级别1：几乎所有多轮任务都派子代理（>= 2 轮就派）
- 级别2：中型任务派子代理（>= 4 轮就派）
- 级别3：大型/超大型或用户指定才派（>= 8 轮）

核心机制：
1. **任务规模预判**：基于 goal 关键词、plan 步骤数、工具数量估算任务轮数
2. **模型速度感知**：`bench-model` 命令测试不同上下文大小的速度，建立速度模型
3. **动态超时**：根据输入 token 数和速度模型计算合理超时，避免"一刀切"
4. **失败分析器**：分析子代理失败根因（超时/能力缺口/任务太大等），给出建议
5. **自适应重派**：根据分析结果调整策略（提高超时/拆分任务/补充能力），不是机械重派
6. **主代理代劳防护**：级别1时主代理不能绕过子代理直接执行多轮任务

待做：
- 实现 estimate_task_complexity()
- 实现 bench-model CLI
- 实现 SubAgentFailureAnalyzer
- 实现 adaptive_retry() 和 split_task()
- 集成到 dispatch 循环

## 2026-05-02 / Acceptance Real Execution 验收真实执行

状态：设计中

模块设计文档：[docs/design/acceptance-real-execution.md](docs/design/acceptance-real-execution.md)

摘要：

验收不能只看子代理"填表"，必须有系统级真实执行验证：
1. **TestExecutionRecord**：记录测试命令的真实退出码/stdout/stderr
2. **TestExecutor**：执行测试命令，支持三种验证方式（command/file_check/content_check）
3. **命令安全**：allowlist + 阻止高风险 shell 字符 + 超时限制
4. **集成验收**：验收时自动执行测试，结果作为验收依据
5. **存储**：test_execution.json 保存执行记录，支持 CLI 查看

待做：
- 实现 TestExecutor 类
- 集成到 acceptance_helpers.py
- 新增 test_execution.json 存储
- 新增 subagents-tests CLI 命令
- 更新配置项

## 2026-05-08 / Agent Runtime Control Plane 预留边界

状态：设计中

摘要：

在 Agent Runtime Control Plane、subagent workspace、memory gate 和 tool output 外置继续推进前，需要预留四类长期扩展边界，避免第一版把结构写死：

1. **扩展备用字段和接口**：核心记录、事件和控制面 API 需要预留 `reserved` / `extensions` / `metadata` 等受控扩展槽，新增实验字段优先进入保留槽，稳定后再提升为正式字段。接口设计优先使用 Request/Options/Result bundle，避免后续靠不断加散参扩展。
2. **父子代理继承上下文**：子代理需要能继承父代理的相关能力、约束、记忆路由、上下文包、工具授权和质量契约，但继承必须可裁剪、可覆盖、可撤销，不能默认把父代理全部上下文灌入子代理。当前已落地第一版 explicit inheritance manifest，记录 inherited / overridden / dropped 项，并写入 `reports/inheritance_manifest.json`；它是审计与接管事实，不是自动展开父级上下文的开关。
3. **层级查询和接管视图**：控制面查询不应只假设“主代理查子代理”。任意上级代理（主代理或中间子代理）都可能需要查询自己的下级树；后续还要预留同级协调者、兄弟父级、takeover/rescue 代理在授权范围内查询某个 subtree 或 blocked run 的能力。第一版字段里需要保留 requester / scope / visibility / takeover_hint 这类扩展位，但事实源仍回到 task/run workspace。当前 `status --json` / 人类 `status` 和 `subagents` 看板已经能展示共享进度摘要。
4. **共享进度反馈面板**：子代理之间需要共享任务级反馈面，包含 progress、current step、blockers、messages、findings、evidence packets 和 parent rollup。当前已落地第一版 `SharedProgressPanel` 查询面，把 runtime query、rollup、blocked runs、inheritance manifest refs 和 failure handoff refs 合成上级/接管代理可读状态包；shared blackboard 可以作为协作摘要，但不是事实源；事实仍来自 verified finding、evidence packet、artifact manifest、failure handoff 和 checkpoint。
5. **Failure Handoff / 失败交接**：子代理可以失败、超时或被黑盒大输出拖垮，但不能“白死”。当黑盒工具、外部系统或未知输出可能瞬间撑爆上下文时，子代理必须尽量先保存 checkpoint / tool-output artifact / minimal failure event，再进入外置和压缩流程；如果最终仍挂掉，也要留下警告、现场锚点、避坑提示、恢复建议和最小证据，方便后续 takeover/rescue 子代理不要机械重复同一个坑。当前已落地第一版 `FailureHandoff`，失败/阻塞保存时会写 `reports/failure_handoff.json`，记录 `failure_type`、`risk_level`、`warning`、`last_safe_checkpoint_ref`、`artifact_refs`、`avoid_next_time` 和 `recommended_next_action`；它只做审计和恢复线索，`auto_rescue=false`，不自动重试或接管。当前还会写 `reports/takeover_readiness.json` 和 `TAKEOVER_READINESS.md` 接管前必读包，把 failure handoff、checkpoint、status report、artifact manifest、evidence/artifact refs 排成 recommended read order；Shared Progress/status/subagents 只展示 takeover packet 数量和 refs，不读取正文。
6. **Security Signal / 安全信号预留**：安全劫持、安全欺骗、prompt injection、工具权限异常、插件/供应链风险等不能靠后续口头补救，第一版需要先在 task/run 记录里留审计字段。当前已落地 `SecuritySignal` 和 `security_review_required`，只保存 `signal_type`、`severity`、`summary`、`evidence_refs`、`artifact_refs` 和 `reserved`，LocalStore metadata 暴露 signal count / types / review flag；它不拦截、不判罪、不自动改授权，只给后续 security policy/gate 接入预留事实入口。通道运行时、长期助手 等公开安全问题可作为后续研究输入，但必须先核验来源和复现场景，再沉淀成 risk pattern 或 test fixture，不能直接把未验证传闻写进策略。
7. **Principal / Conversation / Run 记忆隔离口子**：飞书、微信、Web、CLI 多入口并发时，“主代理”不能等同于全局唯一 my-agent。Agent Service 属于 `service_owner_id`，但每次聊天应有 `requester_id` / `effective_principal_id` / `conversation_id` / `root_run_id`；员工通过管理员的 my-agent 发起任务时，员工会话自己的 root run 和子代理运行树仍要独立保存 artifact、snapshot、checkpoint、failure handoff 和 tool-output refs。长期记忆先预留 namespace 与 policy 字段，不默认为每个员工开启独立长期记忆；默认只隔离 session/run 产物，后续可在显式授权后增加 `user_memory`、`team_memory`、`org_memory`、`project_memory` 等层级，并通过 visibility / retention / consent policy 控制是否写入和召回。
8. **任务记忆和员工会话记忆分离**：子代理的大量上下文属于 task/run working memory，任务完成后可以按保留策略清理，只留下 summary、verified facts、artifact refs、snapshot refs、failure handoff、acceptance/test evidence 和必要审计索引，方便反查、复盘和接管；这些临时记忆不应沉淀成员工长期记忆。员工 conversation memory 则是产品层能力：用于记住员工偏好、常用项目、授权过的工作方式、未完成事项和跨会话协作习惯。第一版不默认做员工长期记忆，只保留 `conversation_memory_policy` / `memory_namespace` / `promotion_policy` 口子；后续如要启用，应把“从任务事实提升到员工记忆”的流程做成显式 promotion：必须有来源 refs、可解释摘要、可撤销记录、保留期限和权限范围，不能把子代理临时上下文或未验证 finding 直接写成员工长期记忆。
9. **配置隔离和实验沙箱**：员工可能会让自己的 conversation 做测试、改配置、试权限或开玩笑触发危险配置；这些操作默认只能写入 conversation/run scoped config overlay，不能直接改全局配置、组织配置、管理员个人配置或服务级安全边界。配置写入需要区分 `global_config`、`tenant_config`、`project_config`、`principal_config`、`conversation_overlay`、`run_override`；越靠上的层级越需要显式授权、审计和回滚。my-agent 的定位要同时覆盖个人、个体、组织、企业甚至国家级部署，所以第一版必须把配置作用域、继承链、override 来源、effective config diff 和 rollback ref 留出来，避免某个员工会话的测试行为污染全局运行。

后续方向：

- 在 Agent Runtime Control Plane v1 里统一保留扩展槽和 bundle-first API。
- 继续把 inheritance manifest 接入执行上下文摘要和接管视图，但仍保持 audit-only；后续如需真实继承策略，应先加显式 policy/gate，而不是默认扩大子代理上下文。
- 继续把 `SharedProgressPanel` 接入 CLI/status 展示和 shared workspace facts；面板只暴露 refs 和投影摘要，不读取 artifact 正文，也不替代 task/run workspace 事实源。
- tool output externalizer 前已加入 fail-safe recovery snapshot，ToolContextReducer 也已接入 live prompt 注入前：大工具输出写 artifact 前先记录工具名、hash、大小、run/task/request id 和下一步建议；下一轮 prompt 只放 artifact 摘要、路径和 checkpoint refs，不再直接塞回完整大正文。
- takeover/rescue 第一段已接入 `takeover_readiness_ref`：action plan 的 `rescue_context_refs` 和 takeover apply 的 `evidence_paths` 会先暴露 `reports/takeover_readiness.json`，再按包里的 recommended read order 显式列出 failure handoff、checkpoint、status report、artifact manifest 或 artifact refs；第一版仍保持 refs-only，不自动读取大 artifact 正文，也不自动接管或重试。
- rescue packet / rescue action plan 第一段已落地：`ActionPlanItem` 和 `ActionApplyRecord` 带 `rescue_packet`，记录 `dedupe_key`、`issue_kinds`、`repeat_count`、`retry_policy.max_attempts`、上抛目标、人工确认建议和 `recovery_entrypoints`；`auto_retry=false`、`auto_execute=false`、`reads_artifact_bodies=false` 是当前边界。它只做计划和审计，不自动 rescue。
- 后续做 Security Gate 时优先基于 `SecuritySignal` 扩展：先补外部案例调研、风险分类、detector fixture 和 audit report，再决定是否接入权限收窄、工具隔离或人工确认流程。
- 后续接外部 IM / 企业用户时，先实现 `RuntimeIdentity` / `ConversationScope` 这类轻量身份包，把 service owner、effective principal、conversation、root run 和 memory namespace 写进 artifact/snapshot/control-plane metadata；员工长期记忆是否启用保持 policy 决策，不和第一版 artifact 隔离绑死。
- 后续做员工记忆时，先实现 task/run working memory 的清理与保留包，再实现 conversation memory 的显式 promotion 队列；主代理可以读取员工授权范围内的 conversation summary / preference card / open tasks，但不能默认读取员工私有 run artifacts 或管理员个人记忆。
- 后续做配置系统时，先实现 scoped config overlay 和 effective config viewer：员工会话里的配置测试默认落到 `conversation_overlay` / `run_override`，只有通过明确的 admin approval / policy gate 才能 promote 到 project、tenant 或 global 层；所有 promote 都要写 audit event、diff、rollback ref 和发起人的 effective principal。

## 2026-05-08 / Memory-resume fail-safe checkpoint refs
状态：已落地

摘要：
- `memory-resume --from-compact` 现在会把工具输出外置前写入的 metadata-only fail-safe checkpoint 纳入恢复入口。
- checkpoint 来自 compact restore refs 指向的 hook JSONL；恢复包只展示 path、line_no、snapshot_id、工具名、hash、size、next_actions 等摘要，不读取 artifact 正文。
- `compact_resume_handoff` 和 Compact Resume Context 新增 `Fail Safe Checkpoints` 小节，接管者先读 checkpoint 摘要，再决定是否显式读取 artifact。
- 这条边界继续遵守：checkpoint 不是 compact，summary 不是 verified fact，artifact ref 不是任意文件路径，完整 artifact body 不是默认 prompt 内容。
