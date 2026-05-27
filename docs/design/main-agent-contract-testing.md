# 主代理合同驱动测试与阶段计划

这份文档定义主代理后续开发的主路线。

核心切换只有一句话：

> 真实环境测试保留，但不再作为主要开发方式；主开发流程切到“合同单测 + fake tool + fake LLM + replay + 少量真实验收”。

---

## 1. 为什么要切换

过去这几轮暴露了同一个问题：

- 一边跑真实任务一边修
- 每次等待模型、工具、网络、文件系统、上下文一起变化
- 问题复现慢
- 修复反馈慢
- 失败很难稳定复现

真实环境里经常混杂这些变量：

- 模型输出随机性
- 工具执行速度
- 网络抖动
- 真实文件状态变化
- prompt 长度和上下文波动
- 外部资源返回不稳定

这会让“修一个小 bug”变成“重新跑一遍整个链路才知道有没有用”。

所以后面要把真实环境的职责收窄成：

- 证明最终整条链路真的能跑通
- 暴露新的真实失败样本

而不是拿它做第一层调试工具。

---

## 2. 参考项目与借鉴点

本计划默认先参考 `/Users/example/study-agent/all-agent/` 下的项目，再做本仓库实现。

### 会话运行时-main

重点学习：

- 结构化事件流
- thread/session 持久化恢复
- 工具调用与中间状态分离
- output schema / structured status

我们借鉴的方向：

- 机器事实必须结构化
- 工具、状态、恢复都要能 replay
- 中间事件不能只靠自然语言摘要

### 通道运行时-main

重点学习：

- control plane
- task/run/session registry
- stopReason / pendingToolCalls / session 状态表达
- 执行态与聊天态分离

我们借鉴的方向：

- 主代理和后续子代理都要有清楚的 task/run 状态台账
- runtime 问题必须能从结构化状态里看出来

### 长期助手-agent-main

重点学习：

- activity / inactivity tracking
- inactivity-based timeout
- 长任务稳定性
- 工具网关和后台任务恢复

我们借鉴的方向：

- timeout 不只看墙钟时间
- “没进展”要靠活动/产物/状态判断
- 长任务要能恢复，不要轻易误杀

### 终端应用-main / 模型助手 Code

重点学习：

- background session / resume 体验
- 少量软约束
- 工具只在必要时调用

我们借鉴的方向：

- 自然语言软约束可以有，但数量少、边界清楚
- 不要把流程偏好写成一堆硬规则

### claw-code-main

重点学习：

- TaskRegistry
- parity harness
- replay-safe contract

我们借鉴的方向：

- 失败样本和回放应该成为正式开发资产

### openai-agents-python-main

重点学习：

- handoff
- guardrail 边界
- agent orchestration 抽象

我们借鉴的方向：

- guardrail 应该是少而硬的边界，不是流程经理

### langgraph-main

重点学习：

- 显式状态机
- 有状态流程图

我们借鉴的方向：

- 主代理状态机必须可画图、可测试、可恢复

### agentscope-main

重点学习：

- MsgHub
- session + SQLite
- 消息路由

我们借鉴的方向：

- 后续 message/card/runtime 控制面可以参考它的消息组织方式

### openhuman-main

重点学习：

- memory tree
- SQLite + markdown knowledge base
- 长期记忆组织

我们借鉴的方向：

- 记忆要分层、可索引、可压缩、可落盘为可读文件

---

## 2026-05-25 多代理协作上下文穿透验收

这轮真实用例不是测 IP 业务，而是测一个通用协作问题：

- 父代理先读到一条小型线索
- 父代理创建多个子代理
- 子代理需要共享这条父级已知线索，同时保留各自的资料 refs
- 最终由协作链路产出结构化结果

本轮修复原则：

- 不新增专项任务模板
- 不按 `IP/hostname/日志` 这类业务词写死规则
- 只做父级已读小型上下文到 `context_packs` 的 refs-first 透传
- 只展示开放 context pack 小字段，不维护封闭字段表

已验证：

- 一次性脚本验证 `read_file` 的嵌套 `filesystem.path` 参数可以生成 `parent_recent_read`
- focused orchestration tests 通过
- 真实 run `/Users/example/my_agent/live-agent-runs/generic-ip-clue-e2e-20260525-135034` 已生成正确 `investigation_result.json`

待继续：

- 用补丁后的代码重跑一次同类普通中文 prompt，确认新子代理实际拿到 `parent_recent_read`
- 继续压缩最终 closeout/dispatch 收口耗时，避免产物已经正确但主进程仍长时间不退出

### 终端交互-main / openclaude-main / langchain-master

这些作为补充参考：

- 看模块拆分、任务层、hook chain、service 化方向
- 不作为第一优先主参照

### my-agent-architecture-review-20260519-clean / my-agent-feature-card-message-runtime

重点学习：

- 当前架构问题基线
- Card / Message / Task / Worker 的近似目标形态

我们借鉴的方向：

- 后续只把通用 runtime card 和 message route 变成机器合同
- 专项真实任务只留在测试 fixture 和最终收口，不进入生产合同

---

## 3. 测试金字塔

后续测试按五层执行。

### 2026-05-24 运行门与验收触发收口

最新运行门说明见 `docs/design/main-agent-runtime-gates.md`。这一轮的核心变化是把“阻止模型走歪”和“限制模型怎么开工”分开：

- 保留真正硬门：工具 schema、路径/URL/command 边界、审批绑定、幂等、最终 delivery closeout。
- open write session 是写入事务保护：关键收口、读未提交目标、覆盖未提交目标会被拉回；非冲突工具继续执行，并按模型回合周期提醒，不再由它自己按次数 blocked。
- 探索熔断改成配置化；本地进展门改成只提醒不阻断，默认 `0` 表示只按固定间隔给软提示。
- closeout 返工预算只分“产物齐全但不合格”和“必交产物缺失/无法定位”两类，默认各 3 次，`0` 表示持续返工不按次数停。
- delivery repair 独立运行门已删除；closeout 失败后统一通过 `[delivery-contract-check]` 的 `repair_guidance`、failed artifacts、failed gates 和 recovery actions 指导模型返工，读/搜空转由探索熔断和本地进展门统一处理。
- 新增显式 `submit_for_acceptance`，并保留“无工具最终回复触发隐式验收”。
- delivery contract Doctor 第一次返回结构化返工，第二次仍不可运行则 `DELIVERY_CONTRACT_DOCTOR_BLOCKED`，避免坏机器合同导致无限循环；普通产物失败仍走 closeout 返工循环。
- 删除 bootstrap 开工物化硬门，避免普通任务被迫先写系统指定中间文件。

后续真实任务测试必须按普通用户 prompt 开始：说清任务、要求、输出目录和目标产物格式即可。测试失败时先沉淀离线失败样本，再修通用底座；禁止把失败修成专项模板或新的开工前置硬门。

### 2026-05-25 多代理协作上下文穿透

普通中文协作调查真实 run 暴露两个通用问题：父级先读到的小型线索没有稳定下发给资料源子代理；模型传入 `context_packs.files` 后，runner prompt 只显示包名，不显示文件 refs。

修复方向不是新增 IP 专项合同，而是把协作控制面的信息流补硬：

- 工具归档后生成父级 `parent_recent_read` brief 缓存。
- `create_subagents` 给每个 child 追加 bounded context pack，让子代理看到父级已经读到的短线索。
- Context Pack 渲染改成开放字段小型展示，支持 `files/refs/name/source_tool` 等后续扩展字段。

这层只负责上下文穿透，不负责替模型判断业务结论；最终质量仍由子代理执行、证据 refs 和 closeout 验收负责。

同日下午复验继续暴露一个收口层问题：父级调度返回里已经有每个 child 的摘要和 `output_json`，但这些信息排在庞大的 `records` 后面，模型读取外置 artifact 时可能先被 records 截断，导致协调汇总漏掉某个 child 的发现。修复方向仍是通用的 refs-first 控制面：

- `dispatch_subagents` 顶层先返回 `child_result_index`，再返回详细 `records`。
- `subagent_board` 顶层也返回 `child_result_index`，让父级“只看状态”时同样能先看到 child 摘要和 refs。
- 子代理状态摘要在未完成时附带 child 摘要和 refs，作为返工提示，而不是只列 run_id/status。
- 同批创建的子代理会拿到 `sibling_roster` context pack，里面只有 peer 的 `run_id/name/role/goal` 等控制面身份事实，不包含未来产物路径。这样 coordinator 不必靠父级自然语言记住 10 个兄弟是谁，也不会把 peer 的未来 `output_refs/output_files` 当成当前可读资料。

这不是新收口交给父级仍然要由 LLM 自己判断如何继续调度、接管、修正汇总或向用户报告阻塞。

随后真实复验 `/Users/example/my_agent/live-agent-runs/generic-ip-clue-e2e-20260525-144053` 暴露过“创建子代理记录不等于执行任务”的问题。早期曾用 `orchestration_contract.v1` 做最终回答硬门，后来证明这会把协作流程卡得太死。

当前规则改为：协作链路只保留 tree/refs/dispatch 状态事实和日志观察，系统不再用专门 orchestration 合同阻断 root 最终回答。主代理或父代理需要继续推进时，应读取代理树、dispatch 返回索引和子代理产物引用，再自行判断下一步。

同轮复验还显示模型会把 dispatch 参数写成 `{"orchestration": {"run_ids": [...], "concurrency": 5, "mode": "parallel"}}`。这是通用工具协议漂移，不是业务专项问题。`dispatch_subagents` 现在会展开 `orchestration` wrapper，并把 `concurrency` 映射到 `max_runners`、把 `mode=parallel/async/execute/run/real` 映射到真实执行开关；dry-run/plan/preview 仍保留为预览语义。

### 运行硬门阶段 0-6

2026-05-22 已把“门必须装在运行边界上”落到代码里，作为真实 LLM 测试前的基础线。

阶段 0：参考项目对照。先看 `/Users/example/study-agent/all-agent/` 的 xlsx 索引，再看源码；主要借 长期助手 的集中工具守门、通道运行时 的结构化产物记录、终端交互 的路径权限边界、会话运行时 的结构化工具协议。

阶段 1：Run Contract Gate。每次 closeout 都必须带 request/run/task/workspace 和 effective contract hash。

阶段 2：Tool Gateway Gate 证据保留。工具归档记录必须保存 runtime_gate、operation_id、idempotency_key 和小型 result refs。

阶段 3：Artifact Provenance Gate。产物验收通过不等于任务完成，还必须证明该产物由当前 run 的工具调用生成。

阶段 4：State Transition Gate。完成前必须有结构化状态门，不能从 running 直接口头成功。

阶段 5：Final Closeout Gate。最终成功只认 run contract、runtime、state、acceptance 四个子门都允许。

阶段 6：Recovery Lineage Gate。恢复任务继承旧产物必须显式带 source_run_id、operation_id 和 artifact/path ref。

这 7 个阶段仍然是通用底座，不写购物站、论文、GitHub 表格等专项规则；这些只能出现在测试 fixture 和真实验收任务里。

### Runtime Tool Gateway 缺口 1-4

2026-05-22 继续把四个高风险缺口前移到 `execute_registry_call`，作为真实工具执行前的统一硬门：

1. Tool Manifest Gate：工具注册必须带 effect、参数 schema、幂等策略、审批策略和输出 ref 声明。read_only / mutating / dangerous 是机器枚举，不从说明文本推断。
2. Path / URL / Command Gate：所有工具 payload 里的 path/url/command 字段统一过边界检查。路径必须落在 workspace_roots 内；symlink 解析后越界要拒绝；file URL、私网 URL 和未显式允许的 shell 操作符默认拒绝。
3. Approval Binding Gate：dangerous + real action 必须匹配可信 approved_actions，绑定 tool、run_id、operation_id、idempotency_key 和 args_hash。
4. Idempotency Ledger Gate：mutating / dangerous 调用必须带 idempotency_key；同 key 同 args 的完成记录只能复用，不重新执行；同 key 不同 args 拒绝。

这四个门的共同点：

- 装在工具入口，不等 final verifier 事后补救。
- 输出 `runtime_gate` 结构化证据，后续 closeout、replay、audit 都读这个字段。
- `tool_manifest_payload` 和 registry 使用同一批 ToolSpec 字段，避免模型可见工具清单和真实执行策略分裂。
- 不写任务专项逻辑；路径、URL、命令、审批、幂等都是通用运行合同。

### 入口合同与韧性阶段 1-6

2026-05-24 开始把真实 LLM 测试前的“入口级合同门”补齐。目标是让普通用户提示词先被物化成结构化合同，再由通用门守住执行边界，而不是靠专项模板或长提示词约束模型。

阶段 1：Contract Schema。新增 `delivery_contract_doctor.py`，对 `delivery_contract.v1` 做轻量 schema 自检；坏字段输出结构化 finding 和 `rematerialize_delivery_contract` 返工动作，schema version 不匹配只 warning，保持向后兼容。

阶段 2：Contract Doctor。`delivery_requirement_materializer.py` 保留原始物化结果的 Doctor findings；`main_agent_delivery_closeout.py` 在验收 artifact 前先跑 Doctor，合同本身坏了就写 `.agent_delivery/contract_doctor.json` 并把 `[delivery-contract-doctor]` 注入下一轮返工上下文。

阶段 3：标准合同测试套件。新增 `tests/support/delivery_contract_suite.py`，复用同一组合同用例测试 artifacts 类型错误、未知格式显式扩展、路径越界和版本漂移，避免每个入口散写一套。

阶段 3 补充：交付意图与开放格式。`artifact_locator.py` 不再试图靠底层行业知识理解“CAD 图纸、视频、文档”等大类。入口物化器需要把这类大类转成结构化交付意图：`kind_label` 只保存用户可读标签，真正用于定位的是 `file_extensions`、`acceptable_extensions`、`preferred_extension`、`artifact_intent.acceptable_extensions` 或 `mime_type`。locator 的优先级是：显式路径 -> 显式扩展名/交付意图 -> 少量已知别名优化 -> MIME 或未知明确后缀的开放兜底。这样 `spreadsheet` 可以作为表格族优化，但新格式不需要改代码；同时 “CAD 图纸” 不会被底层误解成必须寻找 `.cad`。参考 通道运行时、长期助手、会话运行时、工具运行时、终端交互、轻量运行时 后，这个方向与它们一致：扩展名/MIME 表只用于媒体、二进制检测、图像 provider、UI 图标等局部工具能力，不作为通用交付产物的唯一硬判定。

阶段 4：韧性层。新增 `registry_resilience.py`，只读工具失败可有限重试；大输出会归档到 `.agent_tool_outputs/` 并给模型短摘要和 artifact ref；mutating/dangerous 工具仍必须先通过幂等、审批、路径等入口门。

阶段 5：入口合同门接主运行链路。`registry_execution.py` 的真实工具入口已接入韧性层；delivery closeout 已接入 Doctor，合同结构失败走返工循环，不再静默跳过或假完成。

阶段 6：非真实环境补测。新增 focused tests 覆盖 Doctor、工具韧性、物化器接线、closeout 接线，并回归 bootstrap、repair、collection、staged writer 等现有入口门。当前仍坚持：真实任务只做最终收口，日常开发以离线合同、fake tool、fake model 和 replay 为主。

阶段 6 补充：子代理状态面路径收敛。`subagent_board` 和 `inspect_agent_tree` 这类模型可见状态工具只暴露当前 task workspace、agent run workspace、artifact refs、recovery refs，不再把旧式 work-order 目录作为主路径字段返回。旧路径仍可留在兼容恢复文件中供系统迁移使用，但不能作为父代理接管/读取产物时的默认候选，避免模型从 `data/subagents/<run_id>/...` 这类旧布局误读到不存在路径。

阶段 6 补充：路径不存在恢复。参考 工具运行时/终端交互/长期助手 的 file read / grep / glob 行为，缺失路径应返回可行动候选，而不是只抛“不存在”。`read_file`、`list_files`、`search_text` 现在共享同一层 `path_not_found` 恢复面：候选只从允许的 workspace roots 中找，结果包含 `candidate_paths` 和建议动作；系统不会自动读取候选，也不会因为路径缺失终止任务。通道运行时 的路径边界经验也保留：越界、symlink 逃逸和权限问题仍走边界错误，不能被包装成普通缺文件。

### Delivery Quality Gate 阶段 0-6

2026-05-22 增加了 `delivery_quality` 门，目的是解决“文件存在但交付质量不可靠”的问题。这个门不是 GitHub、论文、购物站等专项合同，而是对所有资料整理、表格、报告、PDF、网页等交付都能复用的数据质量门。

阶段 0：参考项目对照。先读 `/Users/example/study-agent/all-agent/` 的 长期助手/通道运行时/终端交互 合同索引，再看源码入口。共同结论是：成熟项目会把门装在运行边界上，例如 长期助手 的工具/审批/重复调用守卫，通道运行时 的 provider/approval/workspace/task runtime 合同，终端交互 的权限/路径/只读执行校验。

阶段 1：数据合同入口。任务如果声明 `delivery_quality_contract`、`quality_contract` 或 `data_contract`，closeout 必须执行质量门；没有声明时门显式 `ALLOW` 并写明 `declared=false`，避免隐式猜测。

阶段 2：质量 payload 来源。质量门只从 `delivery_quality_payload_ref`、`quality_payload_ref`、`source_data_ref` 或 artifact 的 `validation_contract.staging_contract` JSON ref 读取机器数据，不从用户 prompt、final prose、报告正文里抽事实。

阶段 3：指标口径门。`metric_contracts` 通过结构化 `field`、`expected_kind`、`required_window`、`allow_estimated`、`require_limitations_for_estimates` 表达要求。比如 `time_window_delta` 必须来自 claim/source 的 `metric_kind=time_window_delta`，不能用 `point_in_time_total` 冒充。

阶段 4：证据与估算门。复用 `evidence_contract` 检查 `source_refs` 和 `claims`；估算值必须显式 `value_type=estimated`，并按合同提供 `methodology` 和 `reserved.limitations`，不能把“不确定”藏在自然语言说明里。

阶段 5：语言字段门。`language_contract` 只检查合同声明的字段，例如 `target_language=zh`、`fields=["summary_zh"]`。它用字符统计做确定性检查，不用 LLM 打分，也不从 prompt 猜哪些字段应该是中文。

阶段 6：合同 hash 绑定与 trace。质量门把 closeout 产物绑定到当前 `effective_contract_hash`；旧合同验收过的产物不能在新合同下直接收口。每次质量门结果追加到 `.agent_delivery/delivery_quality_gate.jsonl`，真实 run 复盘能直接看到 `finding_codes`。

这套门的开发铁律：

- 代码层不得依赖普通自然语言文本作为机器事实来源。
- 任务专项只允许出现在测试 fixture、真实任务 prompt 和任务生成的数据合同里。
- 生产合同代码只做通用检查机：字段、来源、口径、语言、hash、trace。
- 发现真实任务问题时，先把失败转成结构化合同测试，再修通用门。

### 第 1 层：合同/纯函数单测

验证这些：

- 状态机迁移
- verifier
- closeout 判断
- guard 触发条件
- 错误分类
- tool manifest
- path / artifact / checkpoint 校验

特点：

- 不调真实模型
- 不调真实工具
- 几秒内跑完

### 第 2 层：fake tool 测试

用假工具模拟：

- 读文件成功/失败
- 写文件成功/失败
- builder 成功/失败
- 查询结果为空/非空

目标：

- 验证工具结果如何影响状态机和 verifier

### 第 3 层：fake LLM 测试

用假模型故意输出坏行为：

- 说完成但没产物
- 写空产物
- 重复同一工具
- 调不存在工具
- 忘记最终报告

目标：

- 验证即使模型胡说，框架也不能被骗

### 第 4 层：trace replay

把真实环境里出现过的问题沉淀成轨迹：

- contract_ref
- tool_result
- final
- 后续可扩展 state/event closeout 快照

当前推荐最小事件类型：

- `contract_ref`
- `tool_result`
- `runtime_issue`
- `state_snapshot`
- `closeout_snapshot`
- `acceptance_report`
- `final`

目标：

- 不重跑真实环境，也能复现已知失败

### 第 5 层：真实环境验收

真实环境只回答一个问题：

> 结构化底座在真实模型、真实工具、真实文件、真实网络下还能不能跑通。

它不是日常主调试方式。

---

## 4. 第一到第五阶段

下面五个阶段是当前开发主线。

### 第一阶段：测试底座骨架

目标：

- 建立 contracts / fake_tools / fake_llm / replay 基础目录和最小 runner

落地点：

- `agent_py_agent/tests/contracts/`
- `agent_py_agent/tests/fake_tools/`
- `agent_py_agent/tests/fake_llm/`
- `agent_py_agent/tests/replay/`
- `agent_py_agent/tests/scenario_packs/`
- `agent_py_agent/tests/support/`

完成标准：

- 能跑最小合同 fixture
- 能跑 fake tool
- 能跑 fake LLM
- 能跑 replay case
- fixture / fake / replay 都能表达 runtime issue、state snapshot、closeout snapshot 这些机器事实

### 第二阶段：主代理通用合同

目标：

- 禁止专项合同
- 把任务需求上升为通用 artifact / staging / recovery / verification 合同

建议分类：

- `single_file_artifact`
- `multi_file_artifact`
- `structured_data_artifact`
- `builder_backed_artifact`
- `renderable_document_artifact`

完成标准：

- 新任务不需要专门加一条底层逻辑
- 只能通过组合通用合同表达差异

### 第三阶段：统一状态机

当前状态：

- 已完成（2026-05-21）

目标：

- 主代理生命周期统一到一套结构化状态机

建议状态：

- `PLANNING`
- `RUNNING`
- `WAITING_FOR_TOOL`
- `WAITING_FOR_LOCAL_PROGRESS`
- `WAITING_FOR_USER`
- `VERIFYING`
- `BLOCKED`
- `FAILED`
- `DONE`

完成标准：

- 每个状态迁移都有结构化条件
- 不能靠自然语言文本决定关键状态
- `state_machine_transitions.py` 提供共享状态迁移合同
- replay 会检查 `state_snapshot` 序列是否合法

### 第四阶段：工具与错误合同

当前状态：

- 已完成（2026-05-21）

目标：

- 工具失败、builder 失败、artifact 缺失、checkpoint 坏掉，都走统一错误分类和恢复动作

建议错误类型：

- `TOOL_UNAVAILABLE`
- `TOOL_RESULT_INVALID`
- `APPROVAL_REQUIRED`
- `ARTIFACT_MISSING`
- `ARTIFACT_EMPTY`
- `CHECKPOINT_INVALID`
- `CHECKPOINT_NO_ROWS`
- `NO_PROGRESS`
- `APPROVAL_REQUIRED`

完成标准：

- 系统知道失败后该重试、回退、阻塞还是等待
- 不靠 prompt 文案兜底
- `tool_manifest_contract.py` 统一输出 visible/executable tools、failure taxonomy 和 failure contracts
- context bundle 与 `list_tools` 使用同一份共享 tool manifest payload

### 第五阶段：Replay 正式化

当前状态：

- 已完成（2026-05-21）

目标：

- 真实环境失败样本全部沉淀成 golden trace

建议最小轨迹：

- `contract_ref`
- `tool_result`
- `final`

后续可扩展：

- `state_snapshot`
- `closeout_snapshot`
- `runtime_issue`

完成标准：

- 真实环境出现的新失败，24 小时内要变成 replay case
- 修复后 replay 必过，再上真实环境
- `tests/replay/specs/*.json` 成为 declarative replay case
- `scripts/check_replay_contracts.py` 成为 replay gate

---

## 5. 第一批必须沉淀的失败样本

这一批是当前最值钱的。

- `missing_artifact_should_fail`
- `empty_artifact_should_fail`
- `tool_failed_cannot_complete`
- `bootstrap_materialization_required`
- `repeated_exploration_should_redirect_or_block`
- `staged_json_no_rows_cannot_complete`
- `builder_not_called_cannot_complete`
- `model_claims_done_without_evidence_should_fail`

这些样本优先做成：

- contract fixture
- fake LLM case
- replay case
- 对同一类回归，再组合一个 `scenario_pack`

建议合同字段继续保持通用：

- `artifacts.required[*].required_sections`
- `artifacts.required[*].json_requirements.required_keys`
- `artifacts.required[*].json_requirements.required_non_empty_paths`
- `tools.required_calls`
- `tools.required_successful_calls`
- `runtime.required_issue_codes`
- `runtime.forbid_succeeded_when_issue_codes_present`
- `final_status.allow_succeeded_only_if`

---

## 6. 真环境和测试的比例

建议执行比例：

| 类型 | 占比 |
| --- | ---: |
| 状态机 / verifier / 合同单测 | 30% |
| fake tool / fake LLM 集成测试 | 30% |
| replay 测试 | 20% |
| focused integration | 15% |
| 真实环境验收 | 5% |

真实环境不再承担主要开发反馈功能。

---

## 7. 交付纪律

以后每修一个真实问题，都按这个顺序：

1. 先从真实任务里抽一个最小失败样本
2. 做成 contract fixture / fake LLM / replay
3. 先让测试稳定复现失败
4. 再改产品代码
5. 测试变绿
6. 最后才回真实环境验收

---

## 8. 当前状态

当前仓库已经开始具备这条路线的第一批骨架：

- 已有 delivery closeout / contract / verifier 相关测试
- 已有 `tests/contracts`
- 已有 `tests/fake_llm`
- 已有 `tests/replay`
- 已有 `tests/scenario_packs`
- 已有 `tests/support` 里的最小 runner

但还不够完整：

- fake tool 还很薄
- replay 事件模型还偏小
- 失败样本库还不够多
- 还没有把这周的真实任务问题全部沉淀进去

所以后续第一优先级不是再盲跑真实环境，而是把这套底座补齐。

---

## 9. Phase 2.5：主代理核心稳定化阶段 1-6

这些阶段来自 `/Users/example/study-agent/all-agent/2.txt` 的路线收束：先把单个主代理跑硬，再扩大真实任务和子代理。

参考项目对照：

- 通道运行时：学习它的 effective tool policy pipeline。工具权限和可见性由可信 session / config / sandbox 元数据合并，不从模型文本里猜。
- 长期助手：学习它的 inactivity-based timeout、工具边界活动记录、运行状态持久化和原子写入。
- 会话运行时：学习它的结构化工具事件和 approval/patch/exec 边界。

### 阶段 1：冻结主代理核心入口

新增合同：

- `agent_py_agent/agent/contracts/main_agent_core_entrypoints.py`
- `agent_py_agent/tests/test_main_agent_core_entrypoints.py`

它冻结这些核心入口：

- 状态机
- 工具执行器
- 验收闸门
- RunLog
- ToolTrace
- ApprovalGate
- effective contract 快照

核心不变量：

- 成功必须经过验收。
- 工具调用必须经过统一 executor。
- 状态修改只能走事件式合同。
- 机器事实只能来自结构化字段。

### 阶段 2：固定 dry-run 主线

新增合同：

- `agent_py_agent/agent/contracts/dry_run_mainline_contract.py`
- `agent_py_agent/tests/test_dry_run_mainline_contract.py`

测试 fixture 使用告警分析 dry-run，但产品代码保持通用：只看 `required_inputs`、`tool_results`、`artifact_refs`、`evidence_refs`、`action_intents` 和 `executed_actions` 这些结构字段。

它解决的问题：

- 模型说完成但没有证据。
- 工具失败但继续报喜。
- dry-run 被误当真实执行。
- 真实副作用混入 dry-run 主线。

### 阶段 3：真实 LLM + Fake Tools 行为验证

新增合同：

- `agent_py_agent/agent/contracts/live_llm_fake_tool_contract.py`
- `agent_py_agent/tests/test_live_llm_fake_tool_contract.py`

它不直接调用模型，而是规定真实模型试跑必须落下可回放事实：

- prompt ref
- response ref
- tool trace ref
- contract hash
- verifier 结果
- unknown tool / schema error / fake completion / dry-run claimed real 指标

这样以后真实 LLM 试跑的问题，可以先变成离线回归，再修底座。

### 阶段 4：只读 / dry-run 工具适配器就绪合同

新增合同：

- `agent_py_agent/agent/contracts/tool_adapter_readiness_contract.py`
- `agent_py_agent/tests/test_tool_adapter_readiness_contract.py`

它要求工具适配器在暴露给真实任务前先声明：

- effect：`read_only`、`mutating`、`dangerous`
- result schema ref
- read-only 常见失败覆盖：成功、超时、鉴权失败、空结果、大输出、脱敏
- dry-run mode 字段
- 真实执行是否需要审批
- 副作用工具是否强制幂等键

这一步不是某个工具专项逻辑，而是所有真实只读工具和 dry-run 工具共用的上线门槛。

### 阶段 5：真实工具 dry-run 验证

新增合同：

- `agent_py_agent/agent/contracts/real_tool_dry_run_contract.py`
- `agent_py_agent/tests/test_real_tool_dry_run_contract.py`

这一阶段和阶段 4 的区别：

- 阶段 4 检查 adapter 元数据和覆盖声明是否齐。
- 阶段 5 要实际跑过真实工具包装层，至少证明只读工具和 dry-run 工具都经过统一工具执行器。

当前本地可验证入口：

- `read_file` 通过 `ToolRegistry.execute_call` 真实读取隔离 workspace 内文本文件。
- `controlled_exec` 通过 `ToolRegistry.execute_call` 和父级 `controlled_exec_grants` 真实生成 shell dry-run plan，不执行 shell。

机器合同检查：

- 每个 probe 必须有 `probe_id`、`operation_id`、`tool`。
- 每个 probe 必须记录可信 `tool_executor_ref`，例如 `tool_registry.execute_call`。
- 每个 probe 必须记录 `result_schema_ref` 和结构化 `result.ok`。
- `read_only` 工具只能以 `read_only` 模式出现，不能记录 `executed_actions` 或副作用 refs。
- `mutating` / `dangerous` 工具在阶段 5 只能以 `dry_run` 模式出现，result payload 的 `mode` 也必须是 `dry_run`。
- 副作用工具必须保留 `idempotency_key` 和 `args_hash`，方便重试、回放和去重。
- 任何真实执行动作都会被 `REAL_TOOL_SIDE_EFFECT_EXECUTED` 阻断。

这一步学习 会话运行时 的统一工具执行入口，学习 通道运行时 的工具策略元数据合并，也学习 长期助手 的执行活动记录；产品代码只保留通用 probe 合同，不写“日志平台、飞书、防火墙”等业务专项规则。

外部真实工具如飞书测试机器人、真实日志查询、真实防火墙 dry-run，需要等对应环境和凭据进入隔离配置后补 live probe；它们接入时也必须走同一 `real_tool_dry_run_contract`，不能绕过统一工具执行器。

### 阶段 6 预备：Shadow Mode 影子模式

新增合同：

- `agent_py_agent/agent/contracts/shadow_mode_contract.py`
- `agent_py_agent/tests/test_shadow_mode_contract.py`

Shadow Mode 的定位：

- Agent 可以真实分析。
- Agent 可以调用真实只读工具。
- Agent 可以生成风险评分、证据链、建议动作、审批卡片草稿、工单草稿、dry-run 结果。
- Agent 不能真实执行副作用动作。
- 人工复核必须结构化记录，用来比较 Agent 建议和人的实际判断。

当前机器合同检查：

- `mode` 必须是 `shadow`。
- 风险评分必须是结构化 `risk.score`，范围 0-100。
- 证据必须有 `source_type` 和 `source_ref`。
- 建议动作只能是 `recommend` / `draft` / `dry_run` 这类可复核模式。
- `mutating` / `dangerous` 动作必须有 `operator_review_ref`。
- `dangerous` 动作必须有匹配的 dry-run 成功结果和审批草稿 ref。
- `executed_actions` 必须为空。
- `human_review` 必须有 `review_id`、`review_ref`、`decision`、`agreement`；如果人工不同意，必须给结构化原因。

这一步学习 通道运行时 的可信元数据/工具策略边界，也学习 长期助手 的运行状态和人工可复核记录；但代码只保留通用影子账本合同，不引入“封禁 IP、告警、工单”等业务专项判断。

阶段 6 真正跑起来还需要运行闭环合同：

- `agent_py_agent/agent/contracts/shadow_mode_runtime_contract.py`
- `agent_py_agent/tests/test_shadow_mode_runtime_contract.py`

它补上静态 Shadow 合同没有覆盖的一层：Shadow run 必须引用阶段 5 的真实工具 probe，必须有人工对比 artifact，且 runtime 层同样不能出现 `executed_actions`。这样可以防止“只写了一份影子报告，但其实没经过真实只读/dry-run wrapper”的假影子模式。

### 阶段 7：TaskTree 前置账本

新增合同：

- `agent_py_agent/agent/contracts/task_tree_ledger_contract.py`
- `agent_py_agent/tests/test_task_tree_ledger_contract.py`

TaskTree 是多 Agent 前置能力，不是真正启动子 Agent。第一版只做父子任务结构：

- 父任务知道 `child_ids`。
- 子任务有 `parent_id`。
- 节点有 `task_contract_ref`、`artifact_refs`、`acceptance_result_ref`、`state_ref`。
- 依赖只能指向同一棵树里的真实 task id。
- 父任务进入 `SUCCEEDED` / `VERIFIED` 前，关键子任务必须也完成或验收通过。

这一步参考 通道运行时 的 task/run registry 和控制面，但保留本仓库通用字段，不引入真实业务专项节点类型。

### 阶段 8：单 Agent 长任务恢复闭环

新增合同：

- `agent_py_agent/agent/contracts/long_task_recovery_contract.py`
- `agent_py_agent/tests/test_long_task_recovery_contract.py`

真实复杂任务前，主代理自己要能恢复长任务。机器合同检查：

- 必须有 `run_scope_ref`。
- 必须有 checkpoint refs、state refs、artifact refs。
- compact cycle 必须同时有 `bundle_ref`、`apply_ref`、`resume_ref`。
- latest resume packet 必须有恢复状态 refs 和下一步 action refs。
- side effect ledger 必须有 idempotency state，且恢复后不能重放已执行副作用。

这一步借鉴 长期助手 的长任务活动记录和 会话运行时 的 resume/refs-only 思路。

### 阶段 9：Replay / 失败样本库补硬

新增合同：

- `agent_py_agent/agent/contracts/failure_sample_library_contract.py`
- `agent_py_agent/tests/test_failure_sample_library_contract.py`

每个真实或合成失败样本必须能离线复现：

- `contract_fixture_ref`
- `fake_tool_trace_ref`
- `fake_llm_trace_ref`
- `replay_spec_ref`
- `expected_error_codes`
- `regression_test_ref`

真实环境新问题不能只留在聊天记录或日志里，必须沉淀成可 replay 的失败样本。

### 阶段 10：小型真实验收闸门

新增合同：

- `agent_py_agent/agent/contracts/small_real_acceptance_gate.py`
- `agent_py_agent/tests/test_small_real_acceptance_gate.py`

进入大型真实任务前，先跑小型真实验收。case 必须满足：

- `complexity` 只能是 `small` 或 `medium`。
- 必须有隔离 workspace ref。
- 只允许 `read_only` / `dry_run` 工具模式。
- 不允许真实副作用执行。
- 必须有 expected artifact contract、verification refs 和 replay capture。
- 单 case 默认最长 900 秒，避免小验收变成长任务调试。

大型真实任务只有在阶段 10 通过后再开始。

阶段 10 现在有可执行的预真实任务 runner：

- `agent_py_agent/agent/contracts/small_real_acceptance_runner.py`
- `agent_py_agent/agent/contracts/failure_sample_capture.py`
- `agent_py_agent/agent/contracts/task_tree_scenario.py`
- `agent_py_agent/agent/contracts/long_task_recovery_scenario.py`
- `agent_py_agent/agent/contracts/medium_real_acceptance_runner.py`
- `agent_py_agent/agent/contracts/pre_real_task_validation.py`
- `agent_py_agent/tests/test_pre_real_task_validation.py`

对应 1-6 步：

1. 小真实验收 runner 就绪，先证明它能写 refs-first 报告。
2. 小真实批次跑 `read_file` 真实 wrapper、`controlled_exec` dry-run wrapper 和 Shadow runtime。
3. 失败 case 通过 `failure_sample_capture` 转成合同 fixture、fake tool trace、fake LLM trace、replay spec 和回归测试 ref。
4. TaskTree 小场景写父子账本、节点状态、产物 ref 和事件流水，再跑 `task_tree_ledger_contract`。
5. 长任务恢复小场景写 RunScope、checkpoint、state、artifact、compact apply、resume packet 和 idempotency ledger，再跑 `long_task_recovery_contract`。
6. 中型验收批次在小真实报告通过后跑多文件项目和静态站点项目，仍然只允许 read-only/dry-run 事实，不执行真实副作用。

这一步参考了三类成熟做法：通道运行时 的运行状态/heartbeat 思路、长期助手 的 `resume_pending` 可恢复状态字段、会话运行时 的 JSONL 事件输出和 refs-first 事件处理。落地到本仓库时只保留通用合同：状态、refs、事件、恢复、失败样本，不把任何具体业务任务写死进生产代码。

### 阶段 11：LLM 上场前 1-7 总闸门

新增合同：

- `agent_py_agent/agent/contracts/llm_activation_readiness.py`
- `agent_py_agent/tests/test_llm_activation_readiness.py`

这个阶段不是直接调用真实 LLM，而是确认真实 LLM canary 开始前的七个入口都已经可审计：

1. 模型适配器入口合同：检查 `tool_call_id`、流式半截 JSON、重试预算和模型切换 schema。
2. Prompt / Context 组装合同：检查 `contract_hash`、`allowed_tools`、`required_artifacts`、`acceptance_contract_ref`、`run_scope_ref` 和 `tool_manifest_ref` 没被截断或丢失。
3. 工具副作用闸门：检查工具 `effect`、副作用幂等键、replay 阻断和 dry-run / real-run 隔离。
4. 小型 LLM canary 设计：只保存 `prompt_ref`、workspace ref、预期产物 ref 和验收 ref，不把 prompt 正文当机器事实。
5. LLM trace / replay capture：真实 LLM 后续必须保存 `llm_input_ref`、`llm_output_ref`、`tool_trace_ref`、`state_events_ref`、`artifact_refs`、`acceptance_report_ref`、`replay_spec_ref` 和 `failure_sample_ref`。
6. 模型输入阶段超时预算：用 `ModelCallLedger` 的 5K / 10K probe 样本估算首 token 超时，不把缓存命中当正常输入速度。
7. 总门禁：只有 `pre_real_task_validation` 通过后，才允许进入真实 LLM canary。

参考项目对照：

- 通道运行时：借鉴它的 active run / heartbeat / busy 状态由结构化字段发布，而不是从日志文本猜系统是否还在工作。
- 会话运行时：借鉴它的 JSONL event processor，把工具、状态、错误和 token 用量都落成事件。
- LangGraph：借鉴 checkpoint / serde 版本化思路，恢复和 replay 看快照/ref，不看自然语言总结。

完成标准：

- `run_llm_activation_readiness()` 能写出 refs-first 的 7 阶段报告。
- 缺少 1-6 预真实任务报告时，总门禁必须失败。
- Canary case 只能包含 prompt ref 和结构化验收字段，不能内联普通自然语言 prompt 作为系统事实。
- 超时预算必须来自结构化模型调用账本。

---

## 10. 2026-05-22 六步执行规程

这一节是当前继续开发时的工作顺序，用来防止再次跑偏。

核心原则：

- 不频繁提交代码；只有完成一个稳定批次、验证通过后再提交。
- 真实任务不是主要调试方式；真实任务只做最终收口和失败样本来源。
- 发现问题先看 `/Users/example/study-agent/all-agent/` 下的参考项目，再做本仓库通用修复。
- 禁止专项合同；产品代码不能为了某个网页、表格、论文、站点或 prompt 样例写专门分支。
- 禁止代码依赖普通自然语言文本作为机器事实来源；机器判断必须来自结构化字段、状态、refs、schema、工具记录、文件系统事实或显式配置。

### 第 1 步：补离线测试门

目标：

- 先确认合同单测、fake tool、fake LLM、replay、离线矩阵和代码尺寸门禁还在工作。

建议命令：

```bash
python3 scripts/check_contract_test_pyramid.py
python3 scripts/check_offline_contract_matrix.py --repo-root /Users/example/my_agent/my-agent-main --json
python3 scripts/check_replay_contracts.py
python3 scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json
```

完成标准：

- `high-risk=0`、`soft=0`。
- 缺失区域必须先补测试或补合同登记。
- 如果失败来自真实任务历史样本，先转成 fake/replay regression，再修产品代码。

### 第 2 步：跑主代理 fast 验证

目标：

- 在不调用真实 LLM 的前提下，验证主代理核心合同、工具协议、状态机、恢复、产物验收和 replay 闭环没有回归。

建议命令：

```bash
python3 -m pytest -q -m "not slow and not e2e" --tb=short
ruff check agent_py_agent scripts
git diff --check
```

完成标准：

- fast tests 通过。
- ruff 通过。
- 不出现新的架构 guardrail 问题。

### 第 3 步：补 P0 合同硬点

目标：

- 优先补会导致假完成、越权、丢状态、重复副作用、无法恢复的硬合同。

当前优先检查：

- 合同 schema / 合同冲突 / effective contract 快照。
- Verifier 防伪证据。
- 事件重复、乱序、迟到。
- Prompt / Context 组装不能丢合同。
- 工具副作用分类、dry-run / real-run 隔离、审批参数 hash。
- symlink / Unicode 路径越界。
- 通道消息去重和重复审批。

完成标准：

- 每个修复都有离线 regression。
- 修复点落在通用合同、通用状态、通用工具协议、通用恢复动作或通用 validator 注册项。
- 不新增自然语言关键字判断。

### 第 4 步：做少量真实 LLM canary

目标：

- 用很小的真实 LLM 任务验证模型适配器、工具调用、trace capture、artifact refs 和验收报告能串起来。

硬要求：

- 只跑隔离 workspace。
- 一次 prompt 后只观察，不手动替被测主代理补产物。
- 必须保存 `llm_input_ref`、`llm_output_ref`、`tool_trace_ref`、`state_events_ref`、`artifact_refs`、`acceptance_report_ref`、`replay_spec_ref`。
- 失败必须转成 fake/replay/contract regression。

完成标准：

- canary 产物由被测主代理自己生成。
- 验收报告基于结构化事实通过或失败。
- 失败时有可重放样本，不需要人读长日志猜原因。

2026-05-22 执行记录：

- 命令：`python3 scripts/live_agent_lab.py --suite main-artifact --real-llm --runs-dir /Users/example/my_agent/live-lab-runs --run-id 20260522-main-artifact-canary-01 --timeout 600 --keep-going`
- 结果：`LIVE_LAB_PASS`。
- 通过 case：`health`、`main_artifact_readback`、`main_compact_resume_roundtrip`。
- 证据根：`/Users/example/my_agent/live-lab-runs/20260522-main-artifact-canary-01`。
- 关键产物：`fixture_project/lab_outputs/artifact-readback/report.md`。
- 关键观察：主代理自己调用 `read_file`，再按结构化 `artifact_ref` 调用 `read_artifact` 读回外置大输出，随后写报告；compact apply 与 resume handoff 都生成 refs-first 结构化恢复线索。

### 第 5 步：上复杂真实任务并行

目标：

- 在 canary 通过后，再让多个主代理并行跑不同复杂任务，验证并发、长任务、产物验收和恢复。

执行纪律：

- 多主代理可以并行，但每个主代理必须有独立 workspace、run id、artifact root、recovery packet。
- 监督者只能观察日志、合同、产物和验收结果，不能替被测主代理完成任务。
- 复杂任务必须允许阶段产物：数据 checkpoint、脚本 checkpoint、draft artifact、最终 artifact 都要有 refs。

完成标准：

- 每个任务不是只看 exit code，而是看 artifact contract、tool trace、acceptance report 和 recovery packet。
- 未完成不算失败修好了；必须明确是 `BLOCKED`、`FAILED`、`TIMEOUT`、`NEEDS_RECOVERY` 还是 `SUCCEEDED`。

2026-05-22 执行记录：

- 命令：`python3 scripts/live_agent_lab.py --suite main-complex --real-llm --runs-dir /Users/example/my_agent/live-lab-runs --run-id 20260522-main-complex-01 --timeout 900 --keep-going`
- 结果：5 个 case 通过，`main_direct_web_app` 失败。
- 通过 case：`health`、`main_tool_failure_recovery`、`main_artifact_readback`、`main_compact_resume_roundtrip`、`main_large_log_audit`。
- 失败证据根：`/Users/example/my_agent/live-lab-runs/20260522-main-complex-01/fixture_project/lab_outputs/main-web-app`。
- 历史根因：长 HTML 曾被恢复到 `file_write_session`，随后又引入 open-session 阻断和专门返工路径，导致普通写作任务被事务流程卡住。
- 当前纠偏：`file_write_session` 及其提交边界已废弃；复杂产物统一回到 `write_file`、`apply_patch` 和授权命令生成，最终质量由 closeout / artifact acceptance 验收。
- 参考项目借鉴：长期助手 的隔离 `长期助手_HOME`、一次性工具轨迹和原子写入思路；通道运行时 的运行态/工具边界可见性；会话运行时 的工具提交边界先验收再落事实。
- 命令：`python3 scripts/live_agent_lab.py --suite main-complex --real-llm --runs-dir /Users/example/my_agent/live-lab-runs --run-id 20260522-main-complex-03 --timeout 900 --keep-going`
- 结果：5 个 case 通过，`main_direct_web_app` 仍失败，但坏页面不再提交。
- 新根因：默认 `write_file` 长内容在约 4K 时被流式边界过早切到专用 session writer，模型随后混用直接写入和 session 写入。这不是 HTML 专项问题，而是专用写入流程把普通任务复杂化。
- 通用修复：默认流式 inline 写入边界放宽到 32K，同时保留显式小阈值测试入口；常见完整 HTML/CSS/JS 可以自然收尾，真正失控的长流仍会进入 staged writer 恢复。
- 复验命令：web-only 真 LLM 复验，run id `20260522-main-web-only-04`，只跑 `health` 和 `main_direct_web_app`。
- 复验结果：`LIVE_LAB_PASS`，`main_direct_web_app` 245.40 秒完成，产物目录 `lab_outputs/main-web-app` 下生成 `index.html`、`styles.css`、`app.js`、`README.md`，本仓库静态验收通过。

长期助手 对照：

- 隔离目录：`/Users/example/my_agent/长期助手-lab/20260522-main-web-app`。
- 运行方式：源码版 长期助手，隔离 `长期助手_HOME`、`HOME`、`XDG_STATE_HOME`，一次性 venv，只安装缺失依赖，不读取或修改正式 `~/.长期助手`。
- 同题结果：长期助手 用同一 MiniMax 模型完成 `lab_outputs/main-web-app` 的 4 个文件。
- 我们的静态验收器结果：`OUR_STATIC_VALIDATOR=PASS`，artifact integrity `ok=True`。
- 对照观察：长期助手 产物本轮通过，主要靠模型一次性写出完整文件并主动用工具检查引用；它的 `write_file` 对 HTML 显示 `lint skipped`，所以我们仍需要把 HTML 完整性放到自己的工具提交边界，而不是只学习 prompt 或最终回复。

### 2026-05-28 最终产物客观验收纠偏

- 真实任务里出现过 XLSX 先生成成功、后续修复脚本原地覆盖成坏 zip 包的情况。新的底线是：最终交付物必须先过客观格式验证，再进入 registry 的 `ready` 状态；坏候选只能登记为 `invalid`，不能被父代理、tree、closeout 或最终汇报当成事实。
- `write_file` 对常见二进制交付物采用“临时文件 -> 真实 reader / 签名验证 -> 原子替换”的路径。以 XLSX 为例，必须有 `[Content_Types].xml`、workbook、worksheet，并能被 `openpyxl` 打开；失败时保留原文件。
- closeout 只硬挡客观错误：缺文件、越界、空文件、格式打不开、工具/registry 明确失败。内容厚度、来源覆盖、证据充分性、字段质量、中文推荐理由好坏等仍会写进 finding，但统一是 `warning`，交给模型自检和返工，不再制造新硬门。
- 这次纠偏保留开放世界原则：未知格式继续走存在性、非空和已知签名检查；不会因为没有内置枚举就拒绝用户要求的新产物类型。

### 第 6 步：真实问题沉淀为离线回归

目标：

- 真实环境暴露的问题不能只留在日志或聊天里，必须沉淀为可重复运行的离线资产。

每个问题至少生成：

- `contract_fixture_ref`
- `fake_tool_trace_ref`
- `fake_llm_trace_ref`
- `replay_spec_ref`
- `expected_error_codes`
- `regression_test_ref`

完成标准：

- 新问题能在真实环境之外复现。
- 产品修复后离线 regression 先变绿，再做真实验收。
- 若参考项目已有成熟做法，文档里记录借鉴点和取舍；若参考项目也兜不住，记录本仓库为什么要扩展。

2026-05-22 执行记录：

- 问题来源：4 个普通 `my-agent run` 并行复杂任务里，GitHub/PDF 任务停在探索熔断，数据分析任务只写出几百行 CSV/JSON 却声称完成，Web 任务产物存在但缺关键 DOM。
- 根因归类：复杂任务没有进入受控 `delivery_contract` 入口时，系统只能靠探索熔断阻止空转，不能持续按机器合同修到最终产物合格。
- 通用修复：主代理真实任务 runner 继续坚持“一次普通自然语言 prompt”，但 prompt 和机器交付合同分离；命令只传一个 prompt，结构化 `delivery_contract.json` 由 runner 单独传入并落盘。
- 新增通用任务形状：`data_analysis_package`，覆盖 `source_data.json`、`analysis.xlsx`、`report.pdf`、`dashboard.html` 四类产物；验收只读 artifact kind、path、staging、collection、row count、required columns 等结构化字段。
- 新增回归：`test_main_agent_task_data_analysis_rejects_partial_source_rows` 复现“几百行冒充一千行”的失败，要求 collection gate 返回 `COLLECTION_TOO_FEW_ITEMS`。
- 参考取舍：沿用 长期助手/通道运行时/会话运行时 共同的“少数强制门”做法，把约束放在 runner、tool gateway、artifact acceptance 和 delivery closeout，而不是把更多中文约束塞进 prompt。
