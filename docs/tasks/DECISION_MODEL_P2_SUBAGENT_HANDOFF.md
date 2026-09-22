# TODO07 子代理模型选择交接

- 分工：decision_http_max，Astra max；父侧统一集成、文档总入口和提交。
- 起点：`d57368ed1`；本片最后核对主线已本地提交至 `0dc960034`，本片仍未提交。
- 目标：把可选决策接入已有子代理创建服务，显式模型和原继承、授权、创建幂等不变。

## 已实现

根入口 `execute_create_subagents_service` 和递归 `execute_child_creation` 使用原创建锁做准备，
释放锁后一次性请求整批建议，再回到原锁重新检查容量、模型引用、原任务/上下文和持久复用。
最终仍调用原 `resolve_create_run` / hierarchy scheduler 及原发布流程，没有第二条创建链或注册表。

显式模型在原整批验证阶段解析，不进入自动选择。每个未复用的孩子有独立 Choice，
整批只有一个 stage 和一次 decision_service 请求；自动选择只改原 `host_model_profile.v1`，
不改原用户 `model`、ToolOperation 或派工意图身份。已有孩子及恢复重放直接走原复用。

候选来自原 owner/private 与显式共享目录，只含可用 agentic 模型；重新解析授权和连接，
连接变更使用原 HMAC 摘要。工具 schema 直接来自原 ToolExecutor 传入的 ToolInvocationContext 冻结快照，
只在 scoped handler 中通过 ContextVar 引用，退出恢复；不向 RunParams 补不存在的工具快照字段。
候选供应商工具支持明确为未知，真实探针留在原 child runner，不增加“必须曾有阳性缓存”的门槛。
最终消费调用原 `decision_outcome_is_current`，并复查候选和每个孩子的输入摘要。
关闭路径不准备候选或新选择身份。observe 发起原决策调用但保持原模型；普通错误、迟到或过期保持原模型，用户取消继续传播。

宿主明确声明 `retain_original`、`need_data`、`not_needed`、`no_match`、`abstain`；协议错误题独立失效，
不抹掉其他有效孩子建议。来源/选择摘要存于原 task attributes，不保存凭据或完整响应。

复现并修复了原递归默认命名的重放边界：同一显式幂等合同会因展示续号不同而重复创建。
现在从未补默认的原输入记录 `host_agent_name_origin.v1.explicit`；原 schedule_idempotency
只有在双方均有“系统生成”事实且存在原显式 idempotency_contract 时才忽略名称差异。
不按名称字符串或正则猜来源；显式改名、无合同和旧记录缺来源仍保留原行为。

## 文件

- 新 `agent_core/orchestration/decision_subagent.py`：建议准备、授权候选、锁外调用及最终复核。
- `agent_core/orchestration_tools.py`：根创建准备/物化接缝，取消原传播。
- `agent_core/hierarchy_tools.py`：递归接缝与可信命名来源事实。
- `agent_core/orchestration/create_policy.py`：移除调用方伪造的宿主建议/命名来源。
- `subagents/services/hierarchy/schedule_idempotency.py`：原显式幂等边界修复。
- 新 `tests/test_decision_subagent.py`：真实配置、真实任务存储、协议响应替身。

## 验证

```bash
python3 -m pytest agent_py_agent/tests/test_decision_subagent.py agent_py_agent/tests/test_orchestration_create_subagents_items.py agent_py_agent/tests/test_orchestration_create_subagents_idempotency.py agent_py_agent/tests/test_orchestration_create_subagents_tool.py agent_py_agent/tests/test_create_subagents_contract_edges.py agent_py_agent/tests/test_model_profiles.py agent_py_agent/tests/test_subagent_hierarchy_scheduler.py agent_py_agent/tests/test_subagent_hierarchy_duplicate_guard.py agent_py_agent/tests/test_subagent_hierarchy_scope_guards.py agent_py_agent/tests/test_tool_runtime_unification.py -o addopts='' -q --tb=short
```

结果：联合 209 项通过（8.26 秒）；新接线 25 项通过，包含原 ToolExecutor → execute_scoped、
真实 RunParams、enable_tools=True、冷候选不探针且真实 canonical 子任务采用模型。
所改生产/测试文件 Ruff 通过，所改生产文件 strict code-size 为 0 blocker，
`git diff --check` 通过。没有全仓 pytest、收费模型、远端 CI、提交、推送或部署。

## 明确限制

容量只对纯内联任务复用原 token 估算、已有 system 指令片段/工具 schema 与配置输出预算；
不以候选窗口必须大于父窗口为门槛。未展开的文件/上下文引用记为 `capacity_unknown` 并保留继承。
这不是完整 child prompt、跨协议 token 或多模态容量验证，后续 TODO12 仍需沿原准备链完成；
不能把本片测试当作 250K/1M 真实上下文验收。没有逐模型价格或视觉能力推断，也不新建能力表。

当前产品协议是 native-only，未恢复 text fallback；是否真实支持工具仍由原 child runner 探针裁决，候选阶段不作已验证承诺。
内部直接调用缺少冻结工具快照且启用了工具时保留继承；正常模型派工的 ToolExecutor 入口有真实快照并已验证采用。
最终执行的连接/凭据组仍由原 model_scope 冻结；本片冻结选定 profile 引用和决策候选版本，不把凭据复制到 task。
并行首次相同显式合同可能各获得一次建议，但最终持久复用只保留原唯一孩子；实际 ToolOperation 重放仍先由原执行账本处理。
旧递归记录缺少命名来源不能反推补写，因此保留其原名称比较行为。

## 建议下一步

父侧先核对两个锁外接缝、原 freshness helper 和递归命名来源边界，再将本片加入统一 focused/gate。
同步 `CODEBASE_TREE.md`、执行 Goal 和设计状态，明确容量/模态仍待 TODO12。
Curator 与本片文件无交叉，可继续并行；工具能力留给原 child runner 裁决，容量未知继续保留原模型，勿把估算写成完整窗口保证。

父侧补充：收紧同修复身份但不同幂等身份的默认命名复用；只有幂等身份实际相等才忽略自动序号。新增两例及07/09原业务组合83项通过。
