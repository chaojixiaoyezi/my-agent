# P5-D Stage B：Gateway 主会话模型观察交接

状态：本地实现与定向验收完成，文件稳定，未提交。日期：2026-09-22。
负责人：decision_http_max；工作线 decision-model-plan，基线 `ef497a904` 与已协调的并行改动。
本片只实现主会话建议与用量记录，不启用 Stage C 自动采用，也不增加逐工作片用户操作或确认。

## 先核对的事实与方案

- 已读本仓库 `AGENTS.md`、`LLM_GUIDE.md`、`docs/WORKSTREAMS.md`、原 P5-D 审计、Gateway 的
  `request_execution/request_context/request_binding`、`conversation/run_claim` 与决策服务/原用量收口。
- 对照本机 Codex `codex-rs/core/src/state/turn.rs`：活动工作片、普通/Compact 任务和取消令牌有明确运行身份；
  本片仅借鉴分离活动回合事实与展示/建议的边界，没有移植其执行器，未声称逐行审阅整个参考仓库。
- 保留原 `selected_model_scope` 在排队前冻结模型。复用此次 `selected_model_config` 已读 owner 设置，
  通过当前 ContextVar 生命周期传递，退出清空；不建立全局设置缓存。
- 原 lane/claim 取得后，初次 `gateway_conversation_context` 在 `_load_gateway_thread` 原读取之后、
  repair/Compact 之前调用内部宿主 hook。请求 JSON 无法传入此回调，强制 Compact 和重载不携带它。
- off 只用原 schema/默认映射计算 `enabled` 与 `points.model_selection.mode`，不解析连接或共享目录。
  owner 快退使用原排队前读取，thread 使用车道后新读取。排队期间 owner 从关闭改开启在下一新请求生效；
  已预先开启的请求发送前后仍由原 service 检查当前 owner/thread，读取到关闭或撤销即沿原取消/失效合同处理。

## 当前实施

- 新 `gateway_parts/model_observation.py` 仅为原入口适配：普通新 ask、准确 thread/claim/attempt 才有资格。
  已有运行身份、active-turn 恢复、既有观察标记、显式归档恢复或控制任务均跳过。普通中文不参与机器判断。
- `request_binding.GatewayModelObservationWriter` 在原 active-turn T 锁与原 JSON 原子更新中先写
  `model_selection_observation`；标记含原 request/thread/claim/attempt 和稳定 operation ID。
  网络在锁外，发送前再次沿原准确 active-turn 准入。任意既有标记，包括尚未收口的 started，均不重调。
- 候选仅来自原目录中当前可访问的 agentic 配置，显式部署 default 通过 retain_original 表达。
  只发送编号、模型/后端名、声明窗口；端点、密钥、自定义认证头和价格不进入建议输入或标记。
- 原 `begin_decision_stage/decide` 负责绝对期限、关闭重检、可选并发、取消和原账本。此次只消费结构化
  choice；observe 与 apply 配置在 Stage B 都只写 `adopted=false`，不会修改模型快照或线程选择。
- 标记分别记录排队前 `frozen_profile_id` 和车道后 `observed_thread_profile_id/observed_selection_revision`，
  两者不是同一时刻，不能拼成未来自动采用票据。建议回执明确 `adoption_eligibility=not_evaluated`。
- 初次准备阶段在进入原 runner 前失败，且尚未发布 runtime authority 时，复用原
  `FinalizationService.settle_model_usage` 收口真实决策用量。原 runner 开始后仍由其唯一 finalizer 收口。
  不增加价格计算、第二账本或新运行身份。

## 精确文件范围

- `agent/gateway_parts/request_execution.py`：原冻结生命周期、车道后的内部 hook、准备失败收口。
- `agent/gateway_parts/request_context.py`：内部 `on_thread_loaded` 输入及原读取后的调用点。
- `agent/gateway_parts/request_binding.py`：原 request 的一次性观察标记事务。
- `agent/gateway_parts/model_observation.py`：只读候选与 observe 适配。
- `agent/settings/model_profiles.py`：只捕获原 `selected_model_config` 的首次已读脱敏事实。
- `agent/settings/decision_settings_projection.py`：纯开关计算，共用原字段映射与范围登记。
- `tests/test_gateway_model_observation.py`：独立组合验收。
- `tests/test_thread_model_selection.py`：原 exact-thread 测试 fake 接收内部 captured 关键字；原断言不变。
- `tests/test_gateway_request_runtime_errors.py`：旧 Compact fixture 补已有 `model_surface=None` 字段，
  保留它验证原 typed Compact 错误不被 AttributeError 覆盖的断言。

未修改 A 的 subagent runner/loop/model_scope、设置恢复事务或共享总文档。前述文件已有并行整合内容均保留。
新长期文件与此 handoff 的 `CODEBASE_TREE.md` 登记由主线统一完成。

## 测试与验收

新增 focused **31 passed / 3.56 秒**；最终 11 文件联合 **382 passed / 38.56 秒**。

```bash
python3 -m pytest agent_py_agent/tests/test_gateway_model_observation.py agent_py_agent/tests/test_thread_model_selection.py agent_py_agent/tests/test_model_profiles.py agent_py_agent/tests/test_gateway_model_profiles.py agent_py_agent/tests/test_gateway_request_runtime_errors.py agent_py_agent/tests/test_gateway_conversation_compact.py agent_py_agent/tests/test_gateway_capability_compact.py agent_py_agent/tests/test_gateway_conversation_control.py agent_py_agent/tests/test_gateway_runtime_status_errors.py agent_py_agent/tests/test_decision_service.py agent_py_agent/tests/test_decision_settings_scope.py -q --tb=short -o addopts= -o faulthandler_timeout=30
ruff check agent_py_agent/agent/gateway_parts/model_observation.py agent_py_agent/agent/gateway_parts/request_binding.py agent_py_agent/agent/gateway_parts/request_execution.py agent_py_agent/agent/gateway_parts/request_context.py agent_py_agent/agent/settings/model_profiles.py agent_py_agent/agent/settings/decision_settings_projection.py agent_py_agent/tests/test_gateway_model_observation.py agent_py_agent/tests/test_gateway_request_runtime_errors.py agent_py_agent/tests/test_thread_model_selection.py
python3 scripts/check_doc_sync.py
git diff --check
```

以上全部通过；对 6 个认领生产文件调用原 `check_code_size._check_ast`，按
`CODE_SIZE_BASELINE.json` 与 `compute_strict_blockers` 验证 **0 blocker**，未生成或覆盖共享尺寸报告。

具体证据：

- 原 service → bounded worker → provider attempt observer → 原 ledger：一次 HTTP 尝试、输入 token=17。
- 原真实 Gateway handler → echo runner → 原 finalizer：只落一次决策用量，未使用真实供应商。
- observe/apply 均不切模型；普通错误/超时/坏答案继续原主链，取消与 KeyboardInterrupt 原样传播。
- off 与空观察参考路径的 **Gateway → runner 输入字节**相同，原 `Path.read_text/write_text` 路径序列一致；
  比较仅归一原子临时文件的随机 nonce。这是 Gateway 接线等价证据，不冒称逐供应商 HTTP payload 的逐字节实测。
- 线程开关在排队期间改变后读取最新值；owner 快退冻结边界与撤销服务复核分别测试。
- 排队期间显式换模型，本工作片仍用排队前快照；观察期间显式同值选择前进版本且不被回执覆盖。
- Compact reload 不带 hook；旧恢复/控制/标记跳过；回执写失败留下 started，重新构造宿主对象不重发。
- 准备失败沿原 finalizer 持久化决策 token，无新的用量存储或费用计算。

首轮联合曾有 7 个失败：1 个为本线补齐的旧 Compact fixture，6 个为并行 loop 参数接缝，主线已修复
`service.current_params` 未设置时保留原初始参数。最终 382 项在这些修复后的同一 worktree 上重跑通过。
没有启动真实 Gateway、外部模型、全仓 pytest 或远端 CI。

## 仍然明确的限制

- Stage B 没有完整请求容量证明。此时只有新 prompt 与原摘要，完整历史、system/native schemas、图像、
  实际输出/推理预留均标记未知。250K/1M 仅声明窗口，不能据此认为可容纳或自动切换。
- observe 回执是历史建议；原 service 检查发送前后设置/连接，消费不伪造 may_apply，也不宣称目录/线程在
  记录之后仍有效。真正自动采用必须另接 Stage C 的完整冻结输入、版本/目录 guard 与同次线程 CAS。
- 崩溃发生在 marker 写入后、实际 HTTP 前会放弃这次可选建议。started 不证明已发送，但它足以禁止重启重调。
  进程崩溃前尚未持久化的 provider 用量无法从 marker 反推；原账本/恢复合同仍是唯一事实。
- 本片不改变排队、租约、Compact、历史 repair、停止、主模型重试或后台任务的原执行语义。

## 建议下一步

先由主线复核关闭路径等价与精确一次事务后整合；Stage C 可单独推进完整主请求投影与原目录/选择 CAS，
应与当前 subagent 首次发送线串行协调共享接口，不能将本片观察标记直接当作采用授权。
