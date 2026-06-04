## 2026-05-06 structure update
- 中文说明：LOG 的 work-order 模型、规划、实体分类、gap detail、trace-case 查询组装都拆到更具体的文件。外部命令不变，内部结构更适合后续加 detector 和 analyst。
- `dispatch/work_orders/models.py` now owns work-order dataclasses; `planning.py` owns planning flow only.
- `analytics/detectors/classifier_entities.py` now owns entity comparison, entity extraction, and gap detail helpers; `classifiers.py` owns event predicates and weak-signal classification.
- `tools/query_trace.py` owns trace-case parameter and query assembly helpers; `tools/query_functions.py` owns callable query functions.
# Log Analysis：结构树和详细说明

## 模块结构

```text
agent_py_agent/agent/log_analysis/
|-- models.py                 # Source、Event、Finding、Case、EvidenceRef 等数据模型
|-- config.py                 # 日志分析配置，默认关闭、坏值 warning、危险能力安全默认
|-- doctor.py                 # 轻量状态体检，不加载 storage/ML/worker 重依赖
|-- parsers/                  # SecurityAlertV1 等输入格式解析
|-- ingest/                   # checkpoint、dedup、dead letter、pipeline
|-- storage/                  # 本地 JSONL store 和查询接口
|-- analytics/                # 软检测器、baseline、安全规则
|-- security/                 # 关联、实体图、hunting、attack chain
|-- cases/                    # case 合并、证据、调度
|-- agents/                   # analyst / reviewer 合同和 prompt
|-- dispatch/                 # 受控 analyst work order / queue / engine
|-- tools.py                  # security_query / hunt / trace 工具封装，返回摘要和 evidence refs
`-- reports.py                # first response report 和取证包渲染
```

## 核心文件

- `models.py`：先理解数据长什么样，这是后面所有流程的共同语言。
- `config.py`：理解 LOG 模块为什么默认关闭，以及坏配置如何采用安全默认值。
- `doctor.py`：理解轻量体检如何只看配置、路径和 feature gates，不启动重型后端。
- `parsers/security_alert_v1.py`：把 CSV / JSONL 变成统一事件。
- `storage/__init__.py`：使用 lazy import 解决 `query → evidence → base` 循环依赖。
- `storage/local_store.py` 和 `storage/query.py`：保存事件并按条件查回来。
- `tools.py`：把 query、hunt、trace 包装成 agent 工具，只返回摘要、预览行和 evidence refs。
- `tools/tool_classes.py`：query、hunt、trace 的 BaseTool 入口；每个工具 spec 都声明 `effect=read_only`，供统一 Tool Manifest Gate 和 side-effect gate 消费。
- `analytics/detectors.py`：从事件里找可疑 finding。
- `cases/case_store.py` 和 `reports.py`：把 finding 汇成 case，再变成可读报告。
- `dispatch/work_orders.py`：把 case 翻译成 analyst/reviewer 可执行的受控工作单。
- `agent_py_agent/cli/logs.py`：用户从命令行接入这个模块。

## 数据流

1. 用户通过 `my-agent logs ingest <file>` 或 Live Lab replay 输入日志文件。
2. parser 把 CSV / JSONL 解析成标准事件。
3. ingest pipeline 做去重、checkpoint 和坏数据处理。
4. storage 写入本地 JSONL store。
5. query / detector 从 store 读事件，生成 finding。
6. cases 把相关 finding 合并成 case，并保留 evidence refs。
7. reports 生成第一响应报告和取证包。
8. dispatch 可以把 case 变成 analyst/reviewer work order，后续再接真实 subagent。

## 给初学编程学生的学习路径

1. 先看 `agent_py_agent/cli/logs.py`，理解用户命令怎么触发 ingest 和 query。
2. 再看 `models.py`，理解事件、发现、案件、证据引用这些名词。
3. 再看 `parsers/security_alert_v1.py`，学习外部文件怎么变成内部对象。
4. 再看 `config.py` 和 `doctor.py`，理解可选模块如何默认关闭、如何安全体检。
5. 再看 `storage/query.py`，学习程序怎么按条件过滤数据。
6. 再看 `tools.py`，理解 agent 工具为什么返回 evidence refs 而不是原始日志全文。
7. 再看 `analytics/detectors.py`，学习规则怎样从数据里发现问题。
8. 最后看 `agent_py_agent/tests/test_log_analysis_*.py`，用测试理解每一步应该保证什么。

## 当前第一版索引 / 待补齐

本页先描述主结构和学习路径。后续应补充每条 CLI 命令的输入输出样例、工具返回 JSON 样例，以及 Live Lab replay 的产物路径说明。
## 2026-05-06 structure update
- 中文说明：ingestion、work-order dispatch、parser 和 security 模块现在使用小 helper 和参数 bundle 承接长链路调用；公开包结构和 CLI 行为保持稳定。
- Ingestion, work-order dispatch, parser, and security modules now use small helper functions and parameter bundles for long internal call paths.
- Public log-analysis package layout and external command behavior remain stable; the cleanup is an internal maintainability pass.

## 2026-05-07 bundle structure update
- 中文说明：LOG 各层入口用领域专属 Params/Options 表达多字段输入，`bounded_query`、work-order creation、parser、ingest、report、evidence、scheduler、storage query 和 tool query 都不再依赖开放式 kwargs 作为产品接口。
- `bounded_query.py` exposes `BoundedQueryParams` for file-tail query options; old explicit keyword fields are normalized into that bundle at the boundary.
- `dispatch/work_orders/creation.py` now calls subagent creation with `CreateRunParams`, so LOG dispatch no longer expands arbitrary task fields across the module boundary. It sets `normalize_role=False` for LOG work orders so `analyst` / `reviewer` remain domain-visible while still carrying parent-gated quality contracts.
- The parser, ingest, report, evidence, scheduler, storage query, and tool query helpers use domain-specific Params/Options records for multi-field inputs; no LOG product function keeps a var-keyword service signature.

## 2026-05-07 hard/soft structure update
- 中文说明：security prompt、case evidence、dispatch queue、ingest、entity graph、storage query 和 trace-case 的多字段输入都收进 bundle；code-size 只作为趋势提示。
- `agents/prompts.py` uses `SecurityPromptScope` as the security prompt scope bundle; tests now exercise the bundle call shape directly.
- `cases/evidence.py`, `dispatch/queue.py`, `ingest/*`, `security/entity_graph.py`, `storage/query.py`, and `tools/query_trace.py` use small dataclass bundles for multi-field calls.
- The strict code-size report now has no hard or soft findings for log-analysis; remaining items are high-risk near-soft warnings for follow-up refactors.
- Detector rule helpers and query evidence writers use focused params objects at construction time, keeping storage facts and query summaries stable while near-soft cleanup splits implementation details.

## 2026-05-07 high-risk zero update
- 中文说明：LOG ingest finalize、storage query projection 等高风险路径继续拆分；manifest/dedup/checkpoint 和 row matching/preview projection 分别落到专门文件，strict report 已没有 LOG high-risk。
- `ingest/pipeline_finalize.py` now owns manifest, dedup completion, and checkpoint finalization; `ingest/pipeline_enrich.py` stays focused on one-file ingest orchestration and storage flushing.
- `storage/query_projection.py` now owns row matching, summaries, and preview projection; `storage/query.py` stays focused on bounded query execution and evidence persistence.
- Parser, evidence, checkpoint, health, entity graph, trace-case, and config warning helpers use explicit Params/Request bundles at internal boundaries; the strict report has no remaining log-analysis high-risk entries.

## 2026-05-07 annotation structure update
- 中文说明：结构文档把代码里的双层注释当作架构同步内容。新增 LOG 文件、服务、bundle 或公开方法时，要同步更新结构页和 in-code 注释。
- Module structure docs now treat the definition-level double-layer comments as part of the code architecture: `LLM:` records model-facing contract/caller/side-effect notes, and `函数用途:` / `类用途:` records beginner-readable purpose and edit guidance.
- New files, services, bundles, or public methods must update both this structure page and the in-code comments at the same time.
- The global file tree in `CODEBASE_TREE.md` now includes a current architecture map for CLI, agent core, gateway, memory, log-analysis, subagent, tooling, and settings boundaries.

## 2026-05-22 runtime manifest structure update
- 中文说明：LOG 工具现在把副作用类型作为机器字段写进 `ToolSpec.effect`。registry 执行前的 Tool Manifest Gate 直接读取该字段，不从工具描述或 prompt 文本推断。
- `security_query`、`security_hunt_ip` 和 `security_trace_case` 都是 read-only evidence query wrappers；如果后续新增会写文件、发消息或执行处置的 LOG 工具，必须显式声明 mutating/dangerous、幂等键和审批策略。

## 2026-06-02 contract comment cleanup
- 中文说明：`agents/contracts.py` 的 analyst/reviewer 合同继续作为 LOG 子任务输入输出的结构边界；本轮只清理生成式模板注释，并把内部 helper 名字改成 `_compact_string_list`，表示它会裁剪、规范化列表输入。
- Public LOG analyst/reviewer payload shape stays stable: `case_id`、`summary`、`evidence_refs`、facts/inferences/gaps、review decision fields and validation errors keep the same serialized meaning.
