# 主会话模型自动采用 Stage C

## 基本信息

- workstream：P5-D / Stage C，Gateway 普通新主会话请求。
- branch：`codex/decision-model-integration`，独立 `decision-model-plan` worktree。
- 基线：`ef497a90481203b7941c79b1d640142545173808`；在已协调的共享未提交变更之上实施。
- owner：`decision_http_max`，Astra max。
- 状态：本地 fake/合同验收通过；未提交、未推送、未部署、未启动 Gateway；真实 Jev/生成供应商调用均为 0。

## 解决问题与实际完成

Stage B 只记录建议。本片让原 `model_selection=apply` 在当前请求的完整模型输入准备后验证候选，并在真正进入 provider 之前提交采用。用户无需逐工作片选择、补资料或确认；关闭、观察或不能可靠准备时沿原模型。

默认关闭保持原排队前模型冻结时点。宿主仍在原 lane 取得后、原 thread 读取之后只请求一次 Jev，原请求 marker 防止 Compact、重试或重启重新选择。首次协议探针、历史修复及准备消耗同一绝对 deadline，过期不能重新起时钟。

完整请求沿原 PromptBuilder gather 一次。候选用同份冻结 system、动态段、运行事实、规范历史 IR、原工具 snapshot/schema/ToolChoice 和原 backend `project_generate_payload`；未建立第二 renderer、工具注册表、模型目录或用量账本。原参数的可变 IR、provider 历史、tool context 和 live archive state 在候选准备前复制，拒绝候选不会把其临时历史混入原请求。

候选工具支持沿原 `select_tool_protocol` 探针，有界 worker 复用原 `call_with_deadline(optional=True)` 和 provider budget。worker 只返回验证材料，没有提交权；非协作探针超时后实际 worker 仍按原有界调用合同保留，晚返回不能采用。配置、凭据或目录代次在解析候选前后不匹配时，连候选探针也不发送。

## 最终采用事务

1. caller 在请求内 ExitStack 暂时绑定完整候选依赖和 params。绑定跨越本次原生成、后续工具轮、Gateway Compact 及 finalization，不改 Agent 全局默认。
2. 原 `_invoke_backend_generate` 的 observer/准入内部，在实际 backend 调用前再次投影最终 payload，与准备字典精确比较。
3. 原目录 generation guard → 准确 Gateway active-turn T → 原 thread CAS；锁内没有网络。复查已存在的 runtime authority、claim 与有效期、策略 revision、原选择 profile/revision、Compact generation、当前候选配置/权限和绝对 deadline。
4. 同一次 thread CAS 写 `model_profile_id`、`model_selection_revision + 1`、`source=automatic`，保留 `model_selection_last_explicit_revision`；同 ID 显式选择也会让旧建议失效。新的自动选择不是永久 pin。
5. 线程 metadata 仅保留一条 `gateway_model_selection.v1`，后来请求覆盖旧记录。其 `status=send_intent_uncertain` 表示发送意图，不证明供应商已经接收；HTTP 次数仍读原 provider attempt 观察器。原队列 marker 只是结果投影，线程选择与原事务为权威。

目录 guard 的无秘密 generation 覆盖 private provider/model/凭据/OAuth 变化；共享候选沿原 guard 同时保护 owner、admin 和共享目录。**shared decision profile + private 生成候选** 当前不能用已有单候选 guard 同时锁定共享决策目录，因此保守保留；本片没有扩建多目录锁 API。

目录版本与策略都在实际发送前复核。Jev 的 DecisionStage 保留车道时的 pre-run 身份，不伪造绑定成稍后创建的 runner；最终采用另外核对当前队列实际发布的 `run_id/task_id/attempt_id`。该回调只接受本请求和当前 Agent，task_local 子代理不借主会话资格。

## 发送前拒绝、取消和写盘失败

- 明确局部拒绝使用 `ModelRequestSelectionRejected`，由原 caller 撤销临时 ContextVar 绑定、恢复原 params/prompt，然后只执行一次原生成入口；不会重新调用 Jev。
- 原账本如实保留候选的零 HTTP 失败模型尝试，不删除记录来美化计数。7 类最终拒绝矩阵在无工具探针干扰下证明：2 个物理模型尝试、1 个主模型 HTTP，候选业务 HTTP 为 0。
- 实际 HTTP 已开始后的任何普通错误或 provider context overflow 都沿原生成/Compact 语义，不切回原模型重发。无可压历史时，原“无法继续压缩”错误保留。
- InterruptedError、ToolCancelled、KeyboardInterrupt 均不能变成普通失败回退；记录保留回执时发生停止也不发送原模型。
- `submitted` 只在原 thread CAS 成功返回且本次意图匹配时设置。写盘异常使用原 thread 读回：原选择版本确实未变才确认未提交并回退；确认已写或读回 UNKNOWN 时不发送任何业务请求、不跨模型重发。原请求尽力记录 `commit_unknown` 或已确认的发送意图，失败不恢复 once 资格。

## 容量口径和边界

本片没有新增 tokenizer 或价格预算，原 `estimate_tokens` 仍是统一估算器。候选 text/tool 子集额外使用保守工程预算：

```text
conservative_input_bound = max(
  estimate_tokens(完整实际 payload),
  完整 JSON UTF-8 字节数 + 4096 + 256 × messages 条数 + 1024 × tools 条数
)
conservative_input_bound + 实际 provider max_tokens < 显式配置窗口
```

记录包含 `estimated=true`、`budget_basis=utf8_with_protocol_margin`、原估算、保守输入预算、真实输出 cap 和窗口。**这是保守工程预算，不是供应商精确 tokenizer 数量或数学保证**；原 provider overflow/Compact 处理仍必须保留。完整序列化包括中文、工具 schema、工具参数/结果和历史，不按父窗口大小限制只升不降。

在 adapter 可能丢弃未知块之前检查原 IR/历史。实际 list/冻结 tuple 的文本及工具往返可处理；图片、签名/删隐推理、未知 provider 块、无法获得原 payload builder、未知输出 cap 或未显式窗口均保留原模型。不是宣称这些模型不支持相应能力，而是本片没有完整跨模型兼容与模态计数事实。

目前生产同源纯 builder 为 Anthropic-compatible 和 OpenAI Chat。fake 验收覆盖 Anthropic→Anthropic 与 Anthropic→OpenAI Chat，含工具开启/关闭；其它后端继续未知保留。已有历史转换和 Compact 规则未改写。

## 改动文件与 API

- 新 `agent/agent_core/model/request_selection.py`：请求 ContextVar 宿主钩子；`model_request_selection_scope`、`render_selected_request`、`select_request_model`、`before_model_request_send`、`reject_request_selection`。仅内部 host 创建，不读客户端配置或创建额外身份。
- 新 `agent/gateway_parts/model_adoption.py`：同次建议候选代次、完整 payload 验证、临时依赖与最终采用事务。
- `agent/gateway_parts/model_observation.py`：原单次观察追加 apply 对象和请求 scope，observe/off 不采用。
- `agent/gateway_parts/request_binding.py::GatewayModelObservationWriter.record_adoption`：原请求结果投影；原 once 标记和 T/JSON 锁保持。
- `agent/gateway_parts/request_execution.py::_run_gateway_ask_with_model`：请求 scope 包住原执行/Compact/finalization。
- `agent/agent_core/_tool_loop_service.py::_render_tool_loop_prompt`、`next_tool_loop_model_response`：原渲染/完整参数采用与一次局部回退。
- `agent/agent_core/tool_model_generation.py::_invoke_backend_generate`：原真实 provider 前宿主复核。
- 新 `tests/test_gateway_model_adoption.py`：34 个定向案例。

本片没有修改 settings schema/defaults/YAML、decision_service、decision_model_call、model_call_ledger、user_config_tool、loop_support、model_scope 或子代理采用实现。上述设置/服务范围已明确释放，主线可继续 E1。

参考依据是 Stage B 已记录的 Gateway 车道/恢复审计，以及当前原 child 首请求、PromptBuilder 纯渲染、ToolLoopRequestInput、实际 backend builder、model_scope 和原 thread update_atomic。复用成熟原合同，没有增加外部依赖。共享总文档及 CODEBASE_TREE 由主线登记，本交接没有并写它们。

## 测试和结果

联合 11 文件（在最后增加 4 个本片用例之前）：**302 passed in 27.72s**。

```bash
python3 -m pytest \
  agent_py_agent/tests/test_gateway_model_adoption.py \
  agent_py_agent/tests/test_gateway_model_observation.py \
  agent_py_agent/tests/test_gateway_request_runtime_errors.py \
  agent_py_agent/tests/test_gateway_conversation_compact.py \
  agent_py_agent/tests/test_gateway_capability_compact.py \
  agent_py_agent/tests/test_thread_model_selection.py \
  agent_py_agent/tests/test_tool_model_generation.py \
  agent_py_agent/tests/test_tool_request_projection.py \
  agent_py_agent/tests/test_subagent_first_request_selection.py \
  agent_py_agent/tests/test_model_scope_dependencies.py \
  agent_py_agent/tests/test_runtime_context_pressure.py -q --tb=short -o addopts=
```

最新本片：**34 passed in 5.47s**。

```bash
python3 -m pytest agent_py_agent/tests/test_gateway_model_adoption.py -q --tb=short -o addopts=
```

证据覆盖：原实际 payload 相等；250K↔1M 双向；后续真实工具轮继续同模型/schema；中文/schema/历史低估负向；手动同值、关闭、凭据、Compact、到期、晚 payload 和 runner 身份更改；原探针之前目录轮换；晚探针；HTTP 后错误；取消/KeyboardInterrupt；磁盘 replace 前失败、replace 后失败、读回未知；重开原 store 后发送意图存在、once marker 不重调 Jev。fake Decision 经过原 service/worker/账本，输入 17 token 可核对。原 probe 的 HTTP 与业务 HTTP 在测试里分别采集，不把“候选业务未发送”混称“未发生任何探针”。

本片 Ruff、doc_sync、git diff --check、未跟踪文件空白检查及 7 个直接生产文件的严格 AST 检查通过；没有写共享 CODE_SIZE_REPORT，没有运行全仓 pytest，也没有以线上 CI 或真实供应商作为验收来源。

共享 core 在 A 的隔离测试期间冻结，SHA256：

- `_tool_loop_service.py`：`cdf1c80225ebd37034035bc6a34ecea1bafe7485315d84cb0df9dc915e65e97f`
- `tool_model_generation.py`：`e5302d61d41f1da5b0f2fd0b72f060ad3927c16b3ed75b1b602cd6c67d95ff33`
- `runtime_mixin.py`（本片未修改）：`f1c26b6fdb07570f9d03bd62a490df267cc85f2f5d0893c41bf6dafb72ad70e9`
- 新 `model/request_selection.py`：`cc47e26511f1b74dc76455a6188115cdbffa23f9fc698f9cb349f7aaf0518f20`

## 建议下一步

主线先复核原目录→T→线程锁序、写盘不确定判据和工程容量口径，再登记 2 个新生产文件、测试与本交接到共享导航。随后使用唯一隔离 Gateway 做少量真实供应商主会话验收，和 A 的 child 样本分开标识；不能将本片 fake 阳性当真实计数或质量证明。E1 设置/预算可以独立并行；本片 Gateway/core ownership 在交接后释放，任何与 A 真实样本共享的 core 修改应先通知并记录新 SHA。

## 后续集成位置更新

上文文件路径与 SHA 是原 Stage C 交接时的历史冻结证据。全仓 import-boundary gate 发现 Gateway 底层反向引用 core 后，主线已将同一实质实现移动到 `agent/model_request_selection.py`、`agent/gateway_model_observation.py`、`agent/gateway_model_adoption.py`；同 turn Compact 的跨层构造现位于 `agent/gateway_compact_context.py`，原 runner 线程身份位于 `agent/runtime_context.py`。没有保留旧路径转发模块。移位后的 Gateway 观察/采用 **65 passed**，Gateway/Compact **85 passed**，运行身份及相关接缝 10 文件 **268 passed、4 xfailed**，import-boundary 零发现；完整严格 gate 与真实跨模型采用仍未完成。
