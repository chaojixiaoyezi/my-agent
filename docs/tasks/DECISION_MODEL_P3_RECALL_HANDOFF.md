# Workstream Handoff

## 基本信息

- workstream：决策模型 TODO09 / P3 召回后重排
- branch：`codex/decision-model-integration`
- worktree：现有 `decision-model-plan/my-agent-dsh` 独立工作台
- owner：父代理下的 decision_settings_review 子代理
- date：2026-09-22
- 相邻基线：TODO08 由父侧提交至 `0dc960034`；本片未提交。

## 本线目标

在原授权召回和原字符预算已经选好记录后，可选重排长期事实。所有已选材料仍保留，HOT/lesson 固定在原槽位，决策不改变权限、正文或预算。

## 实际完成

- 新入口 `rerank_recalled_memories(agent, request, records, *, recall_scope, refresh)` 返回 `(当前合法记录, 固定诊断)`。
- 唯一生产接线在 `loop_support._formal_memories_for_request` 原 dedupe / `_budgeted_formal_memories` 之后；再进入原 `PreparedRuntimeContext.memories`、context bundle 和工具循环。没有新增记忆库、持久 cache 或调度层。
- `RuntimeContextRequest` 原样给共用服务；只从已有 request_id/run_id/task_id 派生稳定 operation 引用，不从用户正文或记忆借身份。主/子请求依原 agent_thread_id 和 runner 身份；无合法身份沿原链。
- 原 task_local/control_plane 路径继续直接空召回，不调用决策。Compact 不新建召回决策，本轮原 memories 列表沿现有循环参数复用；新请求重新走原准备流程。
- begin 一次冻结阶段，`enabled_points` 明确关闭立即原样返回，不编码额外材料。不可用设置/普通错误/超时/冷却/observe 都保留原顺序。
- 每条长期事实一题 priority choice；已删除没有供应商依据的 64 题固定上限，题量由原决策协议资源帽和 Jev 请求窗口门把关，超限时不截断原记忆而保留原排序。HOT/lesson 仅进入绑定上下文，不作为可排序题。
- 每题显式提供 not_needed/need_data/no_match/abstain；need_data 只绑定本记录的 memory_source_ref，没有补资料读取权。任一题非选择或失败保留整批当前顺序，诊断保留各非选择与 invalid/missing answer 的区别，不猜补分数。
- 全部有效时只对长期事实原槽位做稳定排序，同分保留原序。记录数量、对象内容、HOT/lesson 位置、选中集合和原预算不因建议改变；低优先级不意味着删除或省略。
- 采用前 `_refresh_recall_candidates` 只读取原正式 HOT/lesson/active long-term 仓库，复用原 scope 和预算，只投影原选中的 entry_id；不重新检索，不增加 touch，不把新 ID 混入候选。
- 原来源删除、过期、范围撤销或版本改变时，以当前合法记录的原顺序继续，拒绝旧建议；不会把旧记录作为增强兜底重新带回。读取失败只拒绝增强，不伪造新的记忆状态。
- 候选摘要绑定完整正文/版本/属性/来源、当前查询、scope、实际生成模型名称和原窗口配置。主模型实例或投影身份改变、请求属性改变、候选版本不符都拒绝旧排序；不持久记录连接或凭据。
- 原来源刷新/排列之后调用父侧公开的 `decision_outcome_is_current`，复核同次原策略版本、决策连接摘要、准确截止时间和用户取消；不复制设置算法、不重置期限。随后再次核对主模型与请求属性。
- 诊断复用 `routed_context.findings`，例如 `memory_recall_decision:apply:retain_order:need_data`。不向主模型附加概率表或第二套记忆上下文；用量沿原 decision/auxiliary 账本归本次精确 thread/run/task。

## 改动文件

- 新增 `agent_py_agent/agent/memory_store/decision_recall.py`
- `agent_py_agent/agent/agent_core/runtime/loop_support.py`：仅 `_formal_memories_for_request` 接线和 `_refresh_recall_candidates` 小辅助函数
- 新增 `agent_py_agent/tests/test_decision_recall.py`
- 本交接文件

没有改 core.py、Curator、settings、service、model_call、metrics、TUI/Gateway 或共享文档。公共消费后复核 helper 由父侧实现。

## 测试命令和结果

```bash
python3 -m pytest agent_py_agent/tests/test_decision_recall.py agent_py_agent/tests/test_memory_recall_v2.py agent_py_agent/tests/test_memory_first_loop.py -q --tb=short
python3 -m ruff check agent_py_agent/agent/memory_store/decision_recall.py agent_py_agent/agent/agent_core/runtime/loop_support.py agent_py_agent/tests/test_decision_recall.py
git diff --check
```

结果：56 项通过（本片 40 项、原相关 16 项）；相关 Ruff 和 diff 检查通过。没有收费调用、全仓 pytest、提交、推送或部署。

覆盖：关闭不准备、observe/失败原序、固定 HOT/lesson 槽位、稳定同分、四类非选择及题级错误区别、缺项 refs、材料/范围/模型/属性变更、删除来源当前合法投影、最终公共设置复查、取消、题数上限、task_local、原预算先选、真实原配置→service→worker→ledger→原召回接线、来源刷新期间关闭、主子线程身份、完整 prepare→context bundle→loop 参数复用和正常 prompt 保持。

## 影响范围

- 默认关闭；正常关闭路径不构造决策材料或后端，不增加模型调用。
- 只重排本次已授权且已预算的长期事实。必要材料不会因决策排序被再次裁剪；后续唯一 memory-context 投影仍执行原安全扫描。
- observe/off 和同分结果的原记忆 prompt 保持一致；apply 改变动态记忆顺序，真实服务端缓存与收益仍须后验。
- 没有持久结构变更，无 schema migration。

## 需要主线重点复查

- 公共 `decision_outcome_is_current` 在最后一个采用边界执行，基础 helper 的身份/配置/期限测试仍归父侧。
- 候选刷新按原 active/scope/正式投影/预算规则处理，而非简单拿旧列表兜底复活已撤销记录。
- 原无可复用持久结果载体，所以本片只使用当前 PreparedRuntimeContext；没有额外缓存或跨请求复用。
- 父侧统一更新 CODEBASE_TREE、memory 模块文档、执行 TODO 和最终验证汇总，再跑 doc-sync/strict-size/包 gate。

## 需要其他线协调

不接 Skill/tool 推荐，不碰插件目录；该后续切片必须等实际目录稳定。子代理模型选择与此片可并行。

## 剩余风险

- 测试使用自有 fake 供应商并走原 worker/账本；未验真实 Jev 质量、服务端缓存收益、实际 TUI 或部署。
- 来源复读是同步原文件 I/O；阶段期限会拒绝迟到采用，但不硬中断单次操作系统读调用。未为此增加后台线程或第二资源池。
- 原候选刷新成功时可按原有效性规则移除已撤销记录或采用更新后的合法正文，这是原事实变化，不是模型筛除。

## 后续建议

父侧先审阅准确消费者边界和本片测试，再统一 focused / strict gate 后本地提交。可并行完成子代理选择；暂不加排序缓存或扩大召回范围。真实模型对照应比较关闭、观察和采用，同时核对原完整材料、调用账和缓存事实，不能仅凭模型说“更相关”验收。
