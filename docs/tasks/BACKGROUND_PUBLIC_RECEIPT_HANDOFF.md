# A 后台公开回执交接

状态：A 产品及两新文件定向验证已完成，已冻结；IM 委派组合的 N01 身份接线由其作者收尾。仅主线程提交/推送/打包，不修改线上数据。

## 实现

- 原 ledger 真实 finished 出口签发响应来源，核对 request/run/task/thread/attempt/latest call/purpose；普通任务不新增模型请求。
- `delivery.reply_policy` 缺省 always，显式 origin+explicit 才装配 `background-result.v1`。内部结果、公开正文和公开意图沿原结果/投递链分开。
- valid 主轮 0 新调用；已明确 publish 的缺正文或插话欠账最多一次原零工具表达修复。附件义务不能静默取消，物理空供应商回复仍修复/失败。
- 不根据 JSON、占位符或正文关键词裁决。偏题内容来源仍未知；来源绑定不是语义保证。
- 完成协议失败在保存已有内部结果/原生事实后，沿原 scheduler 的 typed 异常结算；不得按无投递回执释放并重跑业务。冷重投只消费原冻结信封，不重跑业务或重建私有 seal。
- 追加进度边界：原 `BackgroundIMProgress` 启动前核对准确根任务/原 TaskLink 线程与活动调度 run，读取本 run 冻结 `delivery`。`local/none/origin+explicit` 不启动窗口，避免最终静默前先发进度；旧 always 和普通委派保留，前台 120 秒 feedback 不变。
- 与 N01 的协作点：`conversation/runtime.py::_run_params` 用原 `request.task_id` 或准确 `_session_task_run_id(request)` 读取 `background_host_actor`；首次 SessionTask 唤醒没有 claim task_id 时沿原宿主会话任务引用，不读模型 attrs/latest sender。

## 文件

共享版本与 typed 错误：`contracts/background_public_receipt.py`。

来源与完成出口：`agent_core/model/response_provenance.py`、`model/call_runtime.py`、`tool_model_generation.py`、`tool_loop/background_public_receipt.py`、`natural_user_reply.py`、`response_decision.py`、`_tool_loop_service.py`、`_finalization_service.py`、`models.py`、`backends/base.py`。

冻结装配/原结果/原投递：`conversation/background_receipt_contract.py`、`background_execution.py`、`background_delivery.py`、`runtime.py`、`scheduler/delivery_policy.py`。N01 已协调 `scheduler/tool.py` 文档/参数入口；主线程集中添加开关、taxonomy、主树/ledger/TESTS。

追加进度 guard 使用原 `conversation/background_im_progress.py` 和原 `test_background_im_progress.py`，无新增队列/观察者状态/测试文件。

设计入口：[后台内部结果与公开回执合同](../design/BACKGROUND_PUBLIC_RECEIPT_CONTRACT.md)。

## 验证与接手

新测试：`agent_py_agent/tests/test_background_public_receipt_contract.py`、`test_background_public_receipt_integration.py`；均只用隔离 owner 的正式状态事务、fake backend 和 fake channel，无真实密钥/IM/业务正文。

组合清单：`test_scheduler_delivery_policy.py`、`test_tool_model_generation.py`、`test_model_usage_tags.py`、`test_model_call_ledger.py`、`test_model_call_ledger_partitions.py`、`test_response_decision_native_tool_use.py`、`test_unknown_outcome_tool_halt.py`、`test_im_delegation_runtime.py`、`test_delivery_contract_prompting.py`、`test_subagent_finalize_helpers.py`、`test_provider_retry_final_sink.py`、`test_architecture_guardrails.py`、`test_background_owner_delivery_commit.py`。

本切片最终证据（有重叠，不能相加当作唯一用例总数）：

- 两新文件 **39 passed**：`/tmp/background-public-receipt-integration-final.xml`。新增关闭能力开关的完整 scheduler→history typed code、0 模型/0 外发/下 tick 不重跑用例；普通 no-tools JSON 的 wrong purpose/run/thread 也从真实 core 入口明确拒绝。
- A 加既有模型计量、native、unknown-effect、finalization、delivery-contract 和架构组合 **220 passed**：`/tmp/background-public-receipt-focused.xml`。
- 原调度投递/冻结重投组合本轮 **85 passed、4 failed**；失败为 N01 待收尾的旧 IM 委派身份夹具/首次 SessionTask 专有身份接线，已交其作者修复，未放宽生产来源核验。联合 gate 不能沿用这个未全绿批次作为最终验收。
- A 文件 Ruff 与 `git diff --check` 通过；全树 strict code-size **hard=0、blocked=False**：`/tmp/receipt-code-size-final2.md`。`record_background_response` 通过将原 wake 编号读取放回原冻结入口，移除 DTO 的重复 derived ID 收敛，没有新增旁路。

合成 integration 曾确认一条必须修的接缝：只返回 failed 的空公开正文，会被旧 wake 收口认为未投递并释放；已改为保存内部事实后抛已登记 typed 错误，沿原 scheduler 失败结算。坏 schema 的后一 tick 已实测不再请求模型；原冷重投仍只投递完整冻结信封。

进度追加的修前只读证据：`/tmp/background-receipt-progress-readonly.json`。标准 scheduled 线程 explicit/local/none 均 0 进度外发；正式 repository 的 followup 运行沿已有委派线程时，三类策略均能复现 1 send+1 close edit。修复不把这个证据说成“所有默认新计划均外发”，而是在原观察启动点按同一冻结策略统一补齐边界。

追加修复已重新冻结：原后台进度＋A 两新文件＋前台工具/模型等待与进度草稿组合 **85 passed**，证据 `/tmp/background-receipt-progress-gate.xml`。正式 repo/原 RootLink 用例覆盖 explicit/local/none 不建窗口、旧 origin/always 和普通委派保留 120 秒反馈、job 更新不能改本 run 冻结策略，以及错误 run/thread scope 不冒认当前调度。A 文件 Ruff、diff check 和全树 strict size 通过（`/tmp/receipt-progress-code-size.md`，hard=0、blocked=False）；该批与前面的39/220有重叠，不累加为独立用例总数。

## .7 真实失败与完整 JSON 包装修复

`.20261013.7` 独立 MiniMax-M2.7 验收首先在 `run_now` 后立即 tick，尚未越过原 `dispatch_after=预约时间+2 秒`，所以 0 模型、history missing 不算真实业务失败。主线程随后沿同一原 run 唤醒；真实原生请求里含完整完成合同，供应商确实返回四字段 `internal_only`、空 `public_reply` 和非空内部总结，但用一份完整 `json` 代码围栏包装。原 `json.loads` 误拒绝它，正式历史记录 `BACKGROUND_RECEIPT_CONTRACT_INVALID/failed`，没有对外投递；完整请求和响应保留在独立合成原生消息账。

修复只增加 `common/structured_output.py::json_value_from_complete_response` 完整包装解码：原完整 JSON 或全响应唯一明确标注 `json` 围栏可解码；前后说明、多 JSON/多块、非 JSON 标签、缺围栏和嵌套提取均不许可。`parse_background_result` 继续原严格 schema/来源/公开义务校验，不复用任意正文扫描器，不加关键词静默规则、不改普通文本/JSON路径、不增模型请求。旧失败 run 保持终态，未重跑业务或补产物；修后候选 `.20261013.8` 的真实验收由主线程另行执行，当前不能声称已真实通过。

本增量重新冻结：通用结构化输出＋A 合同＋A integration **71 passed**，证据 `/tmp/background-receipt-json-fence-focused.xml`；架构、运行模块边界和原生工具接线 **63 passed**，证据 `/tmp/background-receipt-json-fence-boundary.xml`。Ruff、diff check、doc sync 均通过；strict size 为 hard=0、blocked=False（`/tmp/background-receipt-json-fence-size.md`）。计数与前批重叠，不累加为独立用例总数。

建议下一步：由主线程把 `test_structured_output.py` 纳入联合严格 gate，再构建新 sourceless 候选并做少量独立真实模型验收。非作者可并行只读核对完整解码、来源和冷重投边界；保持旧失败证据，不恢复已失败业务，不将格式修复说成模型语义保证。
