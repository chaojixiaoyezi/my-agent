# 决策模型 P1-C/D 有界调用组件交接

## 基本信息

- workstream：可选决策模型 P1—P5，本片只交付通用等待/取消/准入组件。
- branch：`codex/decision-model-integration`；独立受管 worktree，基线 `4e33f7593`。
- owner：Astra max 实施 worker/取消及 Curator 迁移；主代理实施原准入与组合；high 只读复核准入及调查后续账本。
- date：2026-09-22。

## 本线目标与实际完成

调用者到期能返回，但不能把仍存活的网络/清理资源当作已经退出，也不能让可选请求占满普通模型名额。
从原 Curator 迁出 `call_with_deadline`，删除原 Queue/Thread/在途集合/join 副本；同步 callable 复用唯一原语。
精确 InterruptHandle 在启动前认领，取消事实不可清旗撤销；唤醒只置 Event，不排在慢网络关闭后面。
每句柄只启动一个清理线程，worker/cleanup 真实退出前保留记录；创建清理失败也不伪造退出，容量有界。
同资源拒绝重叠，单进程保留上限 32；optional 原子保留 1 个普通有界调用名额，不新增持久任务账。
原模型准入使用同一 Condition/计数，optional 不排队并预留 1 个普通 LLM 名额；普通等待默认不变。

## 改动文件

- `agent_py_agent/agent/backends/bounded_call.py`：唯一有界 callable 及准确资源保留。
- `agent_py_agent/agent/concurrency/interrupt.py`：一次性精确句柄、去重清理、即时取消唤醒。
- `agent_py_agent/agent/memory_store/curator_backend.py`：迁移调用，保留 Curator 缩批/游标/重试语义。
- `agent_py_agent/agent/llm_scale/concurrency.py`、`hot_path.py`：原准入的非阻塞可选领取与普通名额保留。
- 新增 `test_bounded_call.py`、`test_decision_call_resources.py`；更新原 interrupt/准入/request_scope 测试。
- 完整 Goal 顶部新增用户可见 18 项 TODO；同步设计、入口、树、测试、路线及已完成文档。

## 测试命令和结果

对以下 18 个文件运行 `python3 -m pytest <文件列表> -o addopts='' -q --tb=short`：

```text
agent_py_agent/tests/test_bounded_call.py
agent_py_agent/tests/test_thread_interrupt.py
agent_py_agent/tests/test_provider_request_scope.py
agent_py_agent/tests/test_decision_call_resources.py
agent_py_agent/tests/test_llm_admission.py
agent_py_agent/tests/test_llm_hot_path_admission.py
agent_py_agent/tests/test_worker_handler.py
agent_py_agent/tests/test_cost_closure.py
agent_py_agent/tests/test_memory_bounds.py
agent_py_agent/tests/test_env_parse.py
agent_py_agent/tests/test_memory_curator_v2.py
agent_py_agent/tests/test_curator_adaptive_timeout.py
agent_py_agent/tests/test_curator_timeout_adaptive.py
agent_py_agent/tests/test_curator_timeout_observability.py
agent_py_agent/tests/test_gateway_helpers.py
agent_py_agent/tests/test_gateway_strict_request.py
agent_py_agent/tests/test_typesafe_decision.py
agent_py_agent/tests/test_runtime_module_boundaries.py
```

首轮联合 313 项通过；补清理线程构造失败与普通清旗后唤醒两项后，最终 **315 passed in 15.38s**，无失败/错误/跳过。
用户取消优先、启动失败回收、迟到结果拒绝、大有限期限不溢出及 caller BaseException 原样抛出均有覆盖。
组合证明 caller 返回后 worker 仍持模型名额，同资源/额外决策被拒绝，普通 LLM 可以继续，真实退出后再接受新请求。
本地严格 gate 已通过：Ruff、doc sync、strict code-size、diff 和 clean-package 全部通过，尺寸 baseline 未改。
文档检查首次指出缺少记忆模块进度/结构同步，补充后复验通过。
没有真实模型调用、日常设置变更或实际 TUI；本片不推送/部署/重启，线上 CI 不是验收来源。

## 影响范围与重点复查

Curator 超时不再 join 0.5 秒，所以合作后端在返回瞬间仍存活时也记 still-running，后续退出证据独立核对。
只有旧请求已实际退出才可缩批重试；仍存活/未知资源不重叠，游标和提取事务不变。
资源上限是单进程保留保护，不是跨机器配额。阻塞 DNS 不能强杀；清理启动失败可保留到进程退出，不能无限加线程绕过。
通用 worker 没有模型准入/账本/业务提交权；实际决策服务需在 worker 内持原准入与 observer，caller 独占逻辑终态。

## 需要其他线协调

插件线确认 concurrency/interrupt、curator_backend、hot_path/concurrency 与本片无重叠。
对方后续 SDK、样本及 nofollow_fs 仍独立；双方均未安排 Gateway 控制。共享文档按精确段落集成。

## 剩余风险与建议下一步

P1-C/D 整项仍未完成：时间设置、策略冻结、冷却、关闭后拒绝旧建议及实际 decision service 尚未接线。
下一批并行 04 共用设置与 05 原账本，再串行接 06 首条可用决策链；对应用户 TODO，避免继续扩展本片范围。
账本调查发现终态可被迟到 first_token 重开、部分 usage 冒充实际输入、线程 observer 未传播等问题，须通用修复。
接入用量时保留原统计行：增加决策输入，输出留白、不显示价格，不改聊天轮次、最近缓存或速度。
