# P4-B 普通 owner 决策设置工具修复交接

## 目标与改动边界

普通可信 user owner 的主模型应能从原 `user_config` 工具读取、修改、读回本人的 owner/thread 决策设置；`main_agent` 仍保留原本机配置能力，group/未知 owner 不增加此工具。普通 user 的模型 schema 和执行 handler 均拒绝 legacy `view/set`，因为它们依赖进程级 `MY_AGENT_CONFIG`，不能跨 owner 暴露。设置仍由原 `decision_settings` 服务、原 owner/thread 身份、CAS 与权限检查管理，无第二套设置服务或中文关键词路由。

本片只拥有 `agent_py_agent/agent/core.py` 注册分支、`agent_py_agent/agent/tooling/user_config_tool.py` 对应模型声明和执行门、`agent_py_agent/tests/test_user_config_owner_scope.py`，以及本交接文档；共享设计/Goal/TESTS/树由主线整合。

## 已完成的本地首片

- `core.py` 仅为 `main_agent` 与 `user` 注册 `UserConfigTool`，`GatewayStatusTool` 仍只为 `main_agent` 注册。group 不扩权。
- 同一 `UserConfigTool` 按可信 `home_paths` 对普通 user 生成 decision-only 模型 schema：action 必填，只有 `decision_read/patch/reset/models/probe/experiment_revoke`，不含 legacy `key/value`。handler 对绕过 schema 的 legacy 调用再次拒绝；主代理完整 schema 与 legacy 行为仍保留。
- 新测试覆盖本机及远程 user 的 manifest、group 无配置工具、直接/原执行器双重 legacy 拒绝、Alice/Bob owner 隔离、旧 revision CAS、main_agent 完整工具。

## 真实复测一：工具已出现，设置仍未成功

单台私有 8431 Gateway PID 72526，新 TUI 会话 `sess_1790146509_4864d5fe`，只发送一次普通中文需求：“请把这个会话的决策模型打开，通用等待上限调到6秒，子代理选模型设为只观察。保存后告诉我实际生效值。”唯一 request 为 `gwreq-1790146533-c215be1cc06c45b28ff42f0d1951fb14`，thread 为 `thread-8b25eebda66b45c1`。原 tool manifest 的 `owner_type=user`，可见且可执行的 `user_config` action enum 只有上列六项，证明工具注册/模型 schema 修复在真实请求生效。原始请求、chunk、context bundle、完整工具输出与最终回复均保留在私有隔离 home；脱敏、可复核的版本及工具账摘要在仓外私有测试目录的 `p4b-natural-fix/20260923T065429Z/{baseline,round1-summary}.json`。

原工具账显示模型自主调用 `user_config` 10 次：五次 `decision_read` 成功，完整原输出每次均为 `revision={owner:11,thread:0}`、`scope=owner`、`thread_id=""`，生效 enabled=true、stage_timeout_seconds=4、subagent_model.mode=apply；第一次 `decision_patch scope=thread` 被 `TOOL_PERMISSION_DENIED`（“当前运行没有可信会话”），随后四次错误改用 `scope=owner` 且传入 owner revision 5/6/7/8，均被原 CAS 以 `STALE_VERSION` 拒绝。主模型最终误称存在其他会话并发修改。实际被测 user owner profile 完整 SHA256 与前测相同、revision 11 未变，thread revision 0/overrides 空；没有并发写入证据，也没有要恢复的临时设置。TUI 已退出，8431 已停止且无监听；日常 8420 仍为原 PID 2543。

这里的 profile 身份三元组是 `local/user/providers/local/users/decision-live-validation`；虽然配置文件放在全局 `config/model-profiles` 目录，却是按 owner 身份哈希命名的被测 user 文件，不能误称 main owner 配置。测试前建立的私有 `baseline.json` 字段 `main_profile_sha256` 是命名错误；该 SHA 实际指向被测 user profile，本交接以身份三元组和 `round1-summary.json` 更正解释，不改动当时的基线字节。

## 第二片底层修复与复测

1. 原 Gateway `request_execution._gateway_task_attributes` 把可信 `conversation_thread_id` 绑定在当前线程本地 `_current_run_params.task_attributes`；`UserConfigTool._decision` / `_decision_model_operation` 只读 `current_task_attributes()`。Gateway 主会话后者为空，导致原服务读写时拿不到当前 thread。现共用 `_decision_thread_scope`：子代理自己的 runner thread 优先，只有非子代理主回合可从当前线程本地 RunParams 取 Gateway 绑定；子代理缺自己的 thread 时拒绝父线程回退，模型参数仍不能指定身份。回执标出实际身份来源。
2. `decision_read` 工具输出约 8.9k 字符；原 `json.dumps(sort_keys=True)` 把 `revision` 排到偏移约 5943，原有界 4000 字符展示/归档没有 CAS 版本。现保留原设置服务的投影字段及顺序，先展示 `revision` 和被请求点值，不放宽 CAS 或增加猜版本分支。

两条复现用例先红后绿：可信 Gateway RunParams 的 thread 用于 read→thread patch，以及 `decision_read` 的前 4000 字符包含 `revision`/`subagent_model`；另有子代理缺线程不得借父线程的防护用例。原 `user_config`/实验授权四文件 91 项、原设置/Registry/manifest 三文件 63 项通过；Ruff、py_compile、doc sync、diff check 均通过。此轮没有修改设置 schema、Gateway、TUI 或共享导航。

第二轮在同一隔离 home、唯一 8431 Gateway PID 52452 和新 TUI 会话 `sess_1790148997_0d1e98b6` 中，只向被测主模型发送一次相同的普通中文需求。单一 request `gwreq-1790149034-d86c85489ffc4bd69169f3eafe03af6f` 的原 manifest `owner_type=user`，`user_config` 可见；模型自主 `decision_read(scope=thread)` 得准确 thread `thread-579561c810014dda` 与 revision owner 14/thread 0，随后 `decision_patch(scope=thread, expected_revision={owner:14,thread:0})` 成功。patch 原回执带已持久保存的 revision 14/1、`enabled=true`、`stage_timeout_seconds=6`、`points.subagent_model.mode=observe` 及 before 投影；主模型最终回复与此一致。原线程文件也显示 revision 1 和上述三项覆盖。模型未额外调用第三次 `decision_read`；本次“读回实际生效值”的证据来自原 patch 事务的持久回执与测试者只读核验，不能写成三次模型工具调用。脱敏基线与工具账在仓外私有测试目录的 `p4b-natural-fix/20260923T073539Z/{baseline,round2-summary}.json`。该请求主模型 MiniMax-M2.7 / anthropic_compatible 三次、决策模型 jev-latest / typesafe_decision 一次，均有原账；所选 MiniMax profile 的 API base 为官方 `https://api.minimaxi.com/anthropic`，未做逐 HTTP URL 抓包。

验收后只为清理私有样本，用原 `execute_decision_settings_operation` 读取完整 CAS 14/1，然后 `reset(scope=thread, fields=三项, expected_revision=14/1)`；thread 升至 14/2、覆盖清空，生效恢复 stage 4/mode apply。被测 user owner profile revision 保持 14，整体 SHA256 与第二轮基线相同。清理证据另存同目录 `cleanup.json`。TUI 已退出、8431 已由原 CLI 停止且无监听，日常 8420 仍为 PID 2543；生产设置未被测试者代填。

主线复核又收紧普通 user 的 owner 身份门：`owner_provider`、`owner_kind`、`owner_id` 任一缺失或为空时，即使其余本机管理员判据偶然成立，也不能获取 legacy 配置动作。新增缺失身份用例后，覆盖本片、设置、实验授权、Gateway 模型采用与子代理首次请求的 13 文件联合 **353 passed**；相关 Ruff 通过。此项是本地合同加强，不增加第二轮真实 TUI 的调用次数或结论。

## 建议下一步

主线先审阅 `core.py` / `user_config_tool.py` 的身份与执行双门，并把本片纳入共享设计/Goal/TESTS/树；若准备远端提交，再按仓库严格 gate 跑对应测试及打包检查。可由其他 agent 并行审核长期 owner 设置或其它接入点，但不要把普通 user 的 `view/set` 打开、从普通中文解析运行权限，或把 patch 事务读回误称独立第三次模型 read。若产品要求主模型每次保存后**另发一次** `decision_read`，应另立软提示/验收准则并核对额外时延，不应改原 CAS/授权门。
