# 测试与发布验收

## 原则

开发反馈优先定向合同、工具替身、模型替身和脱敏回放；真实 TUI 是最终验收最低要求。测试任务由被测代理完成，测试者不能代写产物后计为通过。

普通需求用自然中文表达。权限、参数、隔离、状态和恢复由底座控制，不靠在提示词里写特殊限制规避缺陷。详见 [测试分层](docs/design/main-agent-contract-testing.md) 与 [测试清单](TEST_CHECKLIST.md)。

## 必测模块

长时间运行增量矩阵见 [设计与验收](docs/design/LONG_RUNNING_EXECUTION.md)。真实测试使用一条本地慢模型
TUI（无子代理）及多条正常模型 TUI，共用单 Gateway，定向回放和真实通过分别记账。

| 模块 | 验证要点 |
|---|---|
| 配置与模型 | 服务商协议、密钥引用、上下文容量、会话选择、用户默认、子代理继承与显式覆盖 |
| 身份与工作区 | 多用户同 Gateway、家目录隔离、管理员显式越界、工具权限与真实路径 |
| 主子代理 | 创建、插话、停止、恢复、换代、结果落账、父级唤醒、重复及乱序事件 |
| 历史与压缩 | 未压缩历史完整性、Unicode JSONL、展示分页、长输出引用、压缩计数、模型切换 |
| 工具 | 参数校验、成功/失败状态、文件读写、搜索、补丁、命令/PTY、网络、MCP |
| 记忆与技能 | owner 隔离、自主记忆维护、人格确认、索引发现与按需读取；技能选代表场景 |
| TUI | 输入回显、换行、粘贴、滚轮、复制、完整展开、到底部、主子代理视角、Todo、活动状态 |
| 调度与交付 | 普通回合与目标模式、挂起唤醒、断线、后台交付和恢复；IM 无环境时标明未测 |

## 重点定向回归入口

- 仅思考响应：Chat/Messages 的流式与非流式不得因无正文丢弃有效思考、用量或隐藏重试；
  真空白仍报错。`test_native_tool_use_ir_messages_flow.py` 验证两次无工具续跑逐条保存、
  OpenAI 实际出站回放、下一工具轮和最终保存不重复；`test_response_decision_native_tool_use.py`
  验证坏工具修复不回放未执行工具。联合原超时探针、截断、插话及中断历史回归。
  真实抓包先检查仅思考响应是否漏入下一请求，再评价真实任务完成，不能仅凭缓存高称通过。

- 渠道失败提示：`test_tool_failure_channel_hint.py` 联合错误语义与工具执行回归，覆盖测试/编译非零、
  参数/状态/权限拒绝、取消、未知、真实网络不可用、重复回执和新回执覆盖旧失败。
  错误正文不能提升为控制码，缺 call_id 不猜新事件；关闭阈值和每工具一次不变。
  真实 TUI 核对失败码、下一次请求是否误加换渠道提示及实际排错进展；不能把提示过滤通过当作模型不再循环。

- 后台失败退避：`test_gateway_lane_retry.py`、`test_gateway_loops_resilience.py`、
  `test_background_main_wake_recall.py`、`test_model_unconfigured.py` 与会话模型选择联合验证。
  覆盖缺配置长时间不重跑、模型引用删除/恢复、精确旧会话改选、默认选择不串会话、零值冷却、
  远端拒绝不误判本地缺配置、同 owner 健康车道、跨 owner、短锁与有界回收。
  联合 `test_background_supply_backoff.py` 和 Goal 测试核对 scheduler 不提前关闭目标/消费 wake；
  真实执行错误和额度限制仍受原保护，不把原已暂停或受阻目标无条件激活。
  真 TUI 在未配置会话设置目标，再通过 /model 选模型，核对原目标恢复、唯一最终回复及原 wake；
  另一路正常任务并行，不能把手工改任务文件或替身模型当作真实恢复验收。

- 重启与持久回执：`test_tui_worker_paths.py` 验证已提交消息在 PID 暂不可见时仍读取原 terminal；
  没有终态沿既有超时返回，不再入队；未提交请求仍报告服务停止。真实 TUI 将重启与消息投递交错，
  区分队列提交、实际执行、模型 final 和前端展示，不把服务启动命令退出当作已经就绪。

- 旧计划续写与多用户路径：`test_task_progress_advisory.py` 验证精确旧账更新、缺省状态保留、跨会话、
  子代理/独立后台目标拒绝和无隐式重绑；`test_gateway_chat_conversation_context.py` 验证本地队列的
  自定义 owner、外部/未知来源、final/实时/历史路径一致。真实 TUI 用原会话追加验证笔记并索要完整路径，
  对照 native final、canonical public row 和终端画面；不能把宿主脱敏误记成模型漏答。
  路径样例必须真实含 owner/request 标识，分别覆盖绝对路径、Windows 路径和相对目录；
  仅用不含标识的示例不能检出第二层替换。外部来源不因该修复暴露完整宿主路径。

- 进度部分更新：`test_task_progress_coverage.py`、`test_task_progress_advisory.py` 与派工对账测试，
  覆盖只补备注/元数据、空状态、各规范状态、更正标记、新项默认及模型/展示一致；没有 ID 仍按参数错误返回。
  已完成项须先写入旧备注，再更新并读回新备注；只断言状态未变或空备注成功不算覆盖。
  原生 Schema 必须明确 ID 必填，不能为了部分更新把全部字段都标成可选；标题/状态仍允许按需更新。
  真实慢任务的计划状态和原工具回执并行核对，旧数据不推测重写，不以勾选进度替代产物验收。
- 显式采样：`test_provider_sampling.py` 联合三种 backend 测试，覆盖默认省略温度、显式零值/范围端点、
  单次摘要覆盖、工作片冻结与子代理继承。慢模型客户端对照严格串行，切换前检查原请求及服务端槽位退出；
  真实出站诊断只写私有测试目录，不记录认证头、不改请求协议，不把参数回放当完整 TUI 任务通过。
- 批次执行事实：`test_current_turn_execution.py`、`test_native_tool_use_ir_messages_flow.py`，覆盖
  只追加当前批次、Compact 轮号重置后的身份区分、未知副作用、批准来源、有界省略及全轮核验保留。
  连续请求逐字节保留此前缀和全部工具对，旧会话不强制清理。真实出站核对新增事实大小及实际任务进展。
- 代理树重复查询：`test_agent_tree_model_view.py` 联合工具重复观测回归，验证仅时钟/心跳变化继续计数，
  实际工具进展、终态和产物改变重新计数；原查询结果、权限和生命周期不改变，不用耗时判死。
  子代理查自己的子树时，从规范范围裁决排除自身查询活动；主代理显式查询该孩子仍保留其真实进展。
  正常模型并行验收与慢模型串行对照同时进行，不能把正常模型的轮询浪费漏记为慢模型专属问题。

- 子代理模型续派：`test_orchestration_background_dispatch.py`、`test_model_profiles.py`、
  `test_thread_model_selection.py` 及 worker/timeout 测试。覆盖 Gateway 无默认模型、父子异模型、
  child thread 改选后的恢复、并发显式注入和并行工具线程的依赖传递；旧捕获函数回放须能重现配置/连接不一致。
  真实验收区分普通父子交接与 coordinator 等待孙代理后的重新派工；没有真正产生孙代理的不计后者通过。

- 中断历史：`test_native_tool_use_ir_messages_flow.py`、`test_cli_run_conversation.py`、
  `test_gateway_chat_conversation_context.py`、`test_subagent_runtime_compact.py`、
  `test_background_main_agent_runtime.py`、`test_background_owner_delivery_commit.py` 联合验证
  原生调用/结果保留、未知副作用占位、空正文与异常不改成功、后台静默/外发失败仍留事实而不伪造送达、
  原请求幂等、同一 repair 补交。真实 TUI 用执行中 Esc 后继续，核对下一轮真实输入和已发生的工具事实；
  一路慢模型不派子代理，正常模型并行验证父/子与普通后续轮。历史旧缺口不按显示文字补造成功。

- 客户端计时：`test_gateway_client.py`、`test_gateway_admission_wait.py`、`test_tui_worker_paths.py`，
  覆盖时钟前跳/回拨、失联超时和活动租约续期。真实 TUI 可隔离替换客户端模块时钟注入跳变，
  不修改系统时钟、不影响 Gateway/模型计时；单独记录注入已发生、真实终态及任务产物，不能把替身当真实模型。
- 账号认证：`test_model_oauth.py`、`test_model_oauth_transport.py`、`test_tui_model_menu.py`，
  联合模型配置/共享目录/会话选择/原后端测试。覆盖跨 owner、冻结引用、刷新轮换、取消和退出竞态、
  私密参数保留/清除、重定向拒绝及协议复用。真实 TUI 的设备码确认另验；替身不作为实际账号权益证明。
- 用量增量：`test_model_call_ledger.py`、`test_tui_model_metrics.py`、`test_reproject_model_usage.py`，
  成功/异常/取消共用结算；累计容器重建换代，来源切换不重复算，旧账与缺报不得估算重写。
  真 TUI 中断后追加、Goal 后台交接、子代理及 Compact 必须按 provider 分项对账。
- 慢模型额外排队：模型配置与首事件估算定向测试，默认 0、按模型覆盖、无穷大/布尔/非法值拒绝。
  真实单槽并发等待、滚动输入、Esc 分开验；额外预算不能修复 schema 编译错误或输出截断。

- 工具重复恢复：`test_tool_guardrail_gate.py`、`test_tool_call_guardrail_runtime.py`，覆盖 300 次自身拒绝
  与 400 次成功调用的持续计数/提醒、不同归档引用同正文及相同预览不同尾部。
  不清计数、不挤掉原观测、真实失败/不同结果/实际写入及零阈值；拒绝经真实归档和 native 投影后仍有
  计数及换路说明。`test_tooling_filesystem.py` 验证行/字符非文本失败提示及无额外文件转换。
  真实 TUI 复验单文件动画与正常连续工具任务；没有触发重复门的真实任务只算正常链路验收。

- 模型资源与后台策展：`test_provider_request_scope.py`、`test_memory_curator_v2.py`，覆盖同端点前台
  优先、退出释放、pending/游标保留、pre_compact 屏障、request-local 预算、取消连接及旧请求未退出不重试。
  真机只开一路本地慢模型且不派子代理；官网正常模型可并行对照。缓存核对需同时查推理服务槽位日志，
  外部请求/代理别名和缓存容量不能从 TUI 百分比推断。

- 状态读取：`test_agent_tree_model_view.py`、`test_agent_tree_three_layer_status.py`、`test_orchestration_tools.py`，
  覆盖规范原状态、scope 裁决、恢复路径不外泄、实际报告与缺失报告、八节点直接可读及大树省略计数；
  与 `test_tool_context_reducer.py` 联合核对输出外置后仍保留状态和精确逻辑回读入口。
  真实 TUI 验证运行中查询、完成后交接和真实文件读取，不以最终 DONE 替代工具调用证据。
  无活动任务目录时验证当前会话过滤，显式主请求根验证 parent_id 子树；已有终态报告需实际读取。
- 思考预览：`test_tui_renderer.py` 覆盖流式折叠行数、接收字符数、无换行长段落和窄终端，
  与 `test_thinking_display_boundaries.py`、`test_tui_complete_detail.py` 联测；真实 TUI 需捕获多帧计数增长。

- 派工一致性：`test_orchestration_dispatch_state_contract.py`、`test_subagent_prompt_contract.py`、
  `test_subagent_role_templates.py`，覆盖启动/运行/终态混合快照不推导父级动作、角色正文隔离、冻结自定义角色、
  主代理保留自身分工与用户限制；`test_tool_context_reducer.py` 验证精简回执保留唯一动作建议。
  真实 TUI 分开记录父级独立工作、分层是否如实创建、活跃范围是否重复写、确实依赖结果时是否正常等待。

- 子代理交接：`test_subagent_registered_artifact_handoff.py`、`test_subagent_output_alignment.py`，
  覆盖自然/结构化结果、孙级身份、最新文件、删除、日志排除、账本链接拒绝、cwd 与相对/绝对路径一致、
  不从输出声明增权、不从内部同名文件隐式搬运。真实 TUI 另核对创建谱系与完成信封中的实际路径。
- 补丁交接：`test_artifact_registry.py`、`test_tools/test_filesystem_tools.py`，真实 handler 到归档再到自然收口，
  覆盖新增、修改、移动、删除、部分失败、同路径不同历史 artifact_id 与当前删除状态，保留权限和执行事实。

- 生命周期：`test_dispatch_liveness_and_revive.py`、`test_subagent_runner_result_state.py`、`test_direct_parent_lifecycle.py`。
- 父子并行：`test_direct_parent_lifecycle.py`、`test_runtime_guidance.py`、`test_subagent_activity_diagnostics.py`、
  `test_runner_session_pool.py`，覆盖逐个完成、同时释放去重、模型答复/登记等待竞态、忙父级交接、
  慢流不误杀、阶段/审批诊断、旧 attempt、进度快照覆盖、通知重试和心跳回调失败；真实 TUI 组合另列。
- 退出与积压：`test_executor_exit_recovery.py`、`test_closeout_recovery_paging.py`，包含 exact attempt、慢执行存活、
  未知副作用封存、超过分页窗口、消费去重和重启游标；实际模型/故障注入仍需独立 TUI 证据。
- 历史：`test_conversation_store.py`、`test_background_history_snapshot.py`。
- 目标：`test_conversation_goal_tools.py`、`test_goal_lifecycle_recovery.py`、
  `test_agent_goals.py`、`test_background_main_agent_runtime.py`、`test_gateway_conversation_control.py`、`test_run_audit_terminal.py`。
  中断增量另联测 `test_tui_input.py`、`test_tui_agent_navigation.py`、`test_r103_ledger_selfheal.py`：
  空白补全、前后台插话、Esc 与明确暂停分离、同任务换代、恢复总账及历史关闭事件保留。
  后台参数构造必须走到真实回执消费，不能仅断言邮箱写入；先后完成的历史目标不得误触发并行冲突迁移。
  子代理在 Goal 后台轮创建再回报时，持久 task ID 不需要伪造 user 消息；并测 active/complete 与混合普通请求，后者真实缺失仍报错。
  覆盖默认工具可见、无工具/无 Todo 的安全续跑、审批/暂停/错误边界、旧绑定显式迁移、
  命名目标的精确回合上下文、前后台共享时钟；普通模式不得因此自动续跑。
  另覆盖每代理一个未结束目标、父子计费与权限隔离、编辑版本冲突、暂停后保存不恢复、
  子 Goal 在同一 attempt 中跨轮与 Compact 续接、独立历史不覆盖；实际草稿键盘操作仍须 TUI 验收。
  当前真实 TUI 已覆盖主子保存、放弃、编辑中停止，以及旧版本冲突保留草稿；详情与未测组合见持续目标设计。
  `test_saved_goal_guidance_reaches_its_agent_and_can_cross_provider_boundary` 复现运行中改主目标被子代理误领，
  覆盖主/子消息隔离与提交模型、确认消费完整链路；共享 root task 不能授予父级邮箱。
- 模型：`test_model_provider_management.py`、`test_provider_sampling.py`、`test_model_unconfigured.py`；
  未配置可进设置但不发请求，发布默认值为空，用户显式选择仍保留。
- TUI：`test_tui_interaction.py`、`test_tui_markdown.py`、`test_tui_pty.py`。
- 模型统计：`test_tui_model_metrics.py`，覆盖协议缓存分母、缺报、重放去重、明细裁剪、重试、主子隔离、重连和宽字符窄屏；独立压缩成功/失败均落账，绑定工作片的不重复结算；统计字段不得影响模型上下文。
- 开发检查：`test_contract_test_pyramid_gate.py`。

文件位于 `agent_py_agent/tests/`；改模块时补充对应边界用例，不以此短列表代替所有模块回归。

## 真实 TUI 记录

工具正文完整性：`test_tool_output_externalizer.py` 必须经过生产 `ToolExecutor` 与
`archive_tool_output_projection`，而不是仅手造完整 `ToolResult` 给 reducer；覆盖预览阈值以上的
完整文件、分页及继续游标、归档读取 JSON 和显式保留正文，同时保留大输出外置/脱敏回归。
慢模型复读验收沿原始任务和输入文件建立独立 owner/TUI，记录真实出站回执、重复调用、
产物与独立测试结果；不更改测试项目或用硬停计为通过，不并发占用慢模型。
`test_tools/test_shell_tool.py` 另从 Schema、规范执行入口及真实本地进程验证长命令，
与空输入、危险命令、owner 沙箱、超时和非零退出联测；不把旧长度拒绝当安全边界。

后台进程重复观测：`test_process_sessions.py` 回放 33 次 uptime 变化但状态/输出不变的等待，
并核对原始结果哈希、软提示频率、日志同尾增长和退出后重置；真实 TUI 单独记录模型是否采纳提示。

每次公布 tmux 名称；使用隔离测试用户和同一 Gateway。记录开始/结束、版本、供应商/接口、会话与请求身份、实际工具结果、最终产物、失败和未测边界。不写真实密钥或私人对话。

本轮慢模型只启用一路 TUI、不派子代理，优先验证长等待、流式、插话与停止；正常远端模型可多路并行。
本地缓存诊断同时核对界面最近一次比例、输入用量和推理服务实际预填充，不用延迟反推缓存，更不把缓存未命中当成上下文丢失。

验收分为启动/简单工具、连续多任务、多子代理、长上下文与慢模型组合。普通真实模型测试使用官方 MiniMax-M2.7；协议兼容测试按明确目标选择服务商，不静默改用户日常模型。

默认配置行为必须核对实际合并结果：旧安装若把完整默认 `system_prompt` 或工具延迟目录另存为显式
覆盖，仅升级 wheel 不会替换这些值。测试可在备份后移除测试配置中已确认是旧默认副本的字段，
不能直接覆盖用户定制提示。模型声明、界面 Goal、目标账本、任务绑定和最终工具结果分别取证。

## 提交前严格 gate

```bash
python3 -m pytest <直接相关测试文件> -q --tb=short
ruff check agent_py_agent scripts
python3 scripts/check_doc_sync.py
python3 scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json
git diff --check
python3 scripts/check_clean_package.py .
```

默认 focused tests。生产代码与测试代码累计增删约 10,000 行或明确另有要求时追加全仓 pytest；文档清理不算实施代码变动。线上 CI 未运行时如实说明，不替代本地严格 gate。

## 发布资料清理验证

注释与示例清理要比较生产 Python AST、默认配置值、协议与依赖标识。允许的人类展示字符串变化需单列；构建包检查 LICENSE/NOTICE、vendor 许可和不含秘密数据。历史重写须先备份、只改授权引用、带 lease 更新，验证发布树不变。
