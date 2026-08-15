# 开发计划：确定性优先 + 现有代码缺口补齐（2026-06-11）

状态：**A1/A2/A3 + B1/B2 已实施**（2026-06-11，详见 docs/modules/subagent/02-progress.md
与 docs/modules/memory/02-progress.md）；C/D/E1 待实施。本计划只补现有代码的缺失和
不足，不开发全新功能；每条都指向真实代码位置与验收钉子。实施顺序按用户给定优先级。

实施落差记录（实施时发现与计划的偏差）：
- A1 比计划更轻：archive_tool_calls（含 ok/error_code/parameters）在 AgentRunResult
  里早已存在，A1 收敛为"提取失败摘要→runner_result→attributes 账本→closeout 投影"
  四段接线，无需改工具循环本体。
- B1 发现并修复生产端既有缺陷：_build_memory_query 的 len(goal)>50 条件把中文短
  goal 整个丢弃，推模式此前在中文任务上形同虚设；修复后失败点注入同步受益。
- B1 的 dispatch 注入点裁定不做：dispatch 无独立 LLM 决策面（planner 即其决策面，
  失败重试已有 failure 注入），机器路径注入无意义。
- A1 账本来源语义裁决（全量回归抓到后定稿）：finalize 端 result 缺
  archive_tool_calls 字段（替身/异常路径）= "拿不到系统数据"传 None（不覆盖旧账本），
  与 archive 为空 list（正常轮）= "系统确认零失败"传 [] 严格区分；
  钉子 test_finalize_passes_none_when_result_lacks_archive_field。

## 统领原则：确定性优先（用户头号诉求）

用户原话："agent 的各种工作一定要稳，不能这次成功、下次失败。"
翻译成技术约束 = **同一任务在相同输入下结果可重复**。三条落地准则贯穿所有方向：

1. **决策读系统结构化事实，不读模型自然语言转述**（AGENTS.md 第一铁律的执行缺口）。
   R4b 实证：子代理模型口头报 `WRITE_FORBIDDEN`，而机制实测 `ALLOWED`、`locked_files=[]`；
   对账/watch 若采信模型转述就会"同样的写边界时成时败"。
2. **失败路径幂等可重入**：自省、重试、拆分不得引入时序依赖的状态漂移。
3. **已暴露的失败模式 fixture 化为离线确定性 replay**，防回归。

---

## 方向A：模型行为可靠性 + 观测性（最高优先，直接服务"稳"）

### A1. 子代理工具调用结果的系统级账本（根治归因幻觉混淆）
- **现状缺口**：`agent/tooling/registry_invoke.py:83` 已产出
  `ToolExecutionResult(ok=False, error_code="WRITE_FORBIDDEN")`（系统事实），但子代理最终
  `runner_result.message`（落 work_log 的那条）是**模型在 structured output 里自己写的
  总结**，不是系统逐工具 error_code 的聚合。R4b 里模型转述与系统事实矛盾时，下游
  closeout/watch/对账只看到模型转述。
- **改什么**：在子代理工具循环（复用 `agent_core/tool_loop`）把每次工具调用的
  `(tool, ok, error_code, target_path)` 结构化追加到子代理 work_order 的权威账本
  （ACTION_RECEIPTS / artifacts.jsonl 已有载体，补 error_code 字段）；
  `runner_result` 的失败诊断**优先聚合系统 error_code**，模型 message 降级为辅助说明。
- **验收钉子**：R4b 场景 fixture——构造"机制 ALLOWED 但模型 message 声称 WRITE_FORBIDDEN"，
  断言系统账本显示零 `WRITE_FORBIDDEN`、对账以系统事实判定（不被模型转述带偏）。
- **风险**：不得把账本做成硬门拦主链路；只作观测与对账事实源。

### A2. runner 执行期 write_boundary 与 canonical 一致性钉子
- **现状缺口**：`runner_context_service._build_write_boundary` 用 task 字段构造，与 canonical
  同源；但 R4b 未单独落盘 runner 运行时 boundary 快照，无法 100% 排除"早期轮次
  allowed_write_roots 尚未注入交付区 → 该轮真 WRITE_FORBIDDEN → 后续 canonical 看起来放行"
  的时序性时成时败。
- **改什么**：确证子代理每轮工具循环读到的 write_boundary 恒等于 canonical；若发现时序
  窗口，把交付区授权前移到 boundary 首次构造。
- **验收钉子**：真实类集成测试——子代理首轮即可写交付区，boundary 在任意轮次恒含
  `output_write_grant_roots`。
- **风险**：低，纯收紧时序一致性。

### A3. capability 闭环的结构化引导（让子项②在真实跑里被触发）
- **现状缺口**：R4b 主代理 `resolve_capability_requests` 调用 **0 次**——工具在、模型没用，
  选择主线程自写绕过（且继承了子代理的幻觉归因）。
- **改什么**：`run` 模式存在 open capability_request + 子代理 BLOCKED 时，在 closeout/watch
  报告给主代理一条**结构化建议信号**（"建议 resolve_capability_requests grant/deny"），
  对照 长期助手 activity-based 软引导。**软引导，非硬门**（硬门违反"深度/质量类不前置卡死"）。
- **验收钉子**：闭环 fixture——存在 open request 时报告含该结构化建议字段。
- **风险**：避免做成硬门或忙轮询。

---

## 方向B：记忆推模式补全（落实"推模式"核心架构诉求）

### B1. 注入点从单一 failure 点扩展到 planner/dispatch
- **现状缺口**：`push_relevant_memories` 仅在 `agent_core/runner/gate.py:203`（runner 失败点）
  调用注入；memory `feedback_memory_push_mode` 明确要求"**所有**关键决策点
  （dispatch/planner/runner/failure）自动注入，不靠 agent 主动查"。planner/dispatch 缺。
- **改什么**：在 planner_service、dispatch 决策点接入同一 `push_relevant_memories` +
  `format_memories_for_injection`，复用现有函数（不新增机制）。
- **验收钉子**：planner/dispatch 决策时相关教训被注入上下文（真实类断言注入内容）。

### B2. 注入幂等
- **现状缺口**：多决策点注入可能重复同一教训。
- **改什么**：同 thread/task/轮次去重（参考 progress_policy 的同轮去重先例）。
- **验收钉子**：同轮同一教训只注入一次。

---

## 方向C：失败自省深化（呼应"永不停机"）

### C1. 教训记忆带结构化触发条件
- **现状缺口**：`failure_analysis_service` 规则分类产出 `split_suggestions`/`new_timeout_seconds`，
  但教训记忆是自然语言文本，决策靠文本匹配；memory 要求"带触发条件
  （文件>N行 + 模型<X tok/s + 超时×3 → 调 2x）"。
- **改什么**：教训记忆 schema 加结构化触发条件字段，决策读字段而非自然语言。
- **验收钉子**：触发条件命中时自动选用对应策略（结构化匹配）。

### C2. "自省→调参/拆分→继续派"的永不停机闭环钉子
- **现状**：本轮已打通 split 消费链（`_apply_introspection_split`，默认关）；dispatch 闭环
  `dispatch_max_consecutive_rounds=20` 已在。
- **改什么**：补端到端钉子，验证失败后自动调参/拆分/继续，只有穷尽策略才停下通知用户。
- **验收钉子**：真实类——连续同类失败触发策略升级（调参→拆分→通知），不死循环不早停。

### C3. split 自动拆分的真实任务验证
- **现状**：机制已通、默认关（`subagent_failure_auto_split_enabled=false`）。
- **改什么**：开关开启下做一次真实任务验证拆分子任务可被下一轮 dispatch 真实执行。
- **风险**：拆分改变任务状态，先小范围真实验证再考虑默认值。

---

## 方向D：多通道/多租户会话架构补全（10 场景）

### D1. 管理员跨通道接续 + user_id 隔离 + 通知路由
- **现状缺口**：`agent/user_space/identity_store.py` 在；10 场景（管理员跨通道共享任务池、
  普通用户 user_id 隔离、通知路由 发起通道→活跃通道→离线存档）未全落地。
- **改什么**：按 `project_session_architecture` 的 10 场景逐项核对现状、补缺。
- **验收钉子**：管理员在终端布置、飞书续问命中同一任务池；普通用户互相隔离。

### D2. 并发冲突处理
- **现状缺口**：多终端同时操作同一任务的乐观锁/读写锁。注意复用 `agent/io/jsonl.py`
  线程锁 + flock 双层模式，**不要误用 scoped_locks**（那是进程级单例，见 gateway 04-structure）。
- **验收钉子**：并发操作同一任务无丢失更新（参考 test_real_io_concurrency 场景）。

---

## 横切：确定性回归设施

### E1. 真实失败模式 fixture 化进 live_lab 离线 replay
- **现状**：`scripts/live_lab/` 已有 main_agent_artifact_case / complex_case / log_analysis_replay
  等离线 replay（不调真实模型）。
- **改什么**：把 R4b（模型幻觉归因）等已暴露失败模式 fixture 化为离线 replay case，纳入
  `live_agent_lab.py --suite`，作为"时成时败"的确定性回归防线。
- **验收钉子**：replay 稳定复现"系统事实 vs 模型转述"判定，结果可重复。

---

## 实施建议

- **并行性**：方向A三项内部串行（A1→A2→A3 有依赖），A 与 B/E 可并行（不同模块）；
  C 依赖 A1 的系统事实账本；D 相对独立，可单独 agent 承接。
- **每步收尾**：focused tests → 全量回归对照基线零新增 → ruff → check_doc_sync →
  check_code_size strict → offline_contract_matrix → clean_package → git diff --check。
- **不提交**：沿用当前"用户下令才 commit"约定。
- **铁律守住**：软引导不做成硬门；配置三同步；一个概念一个权威；参考 长期助手/会话运行时/
  通道运行时 先于自己发明。
