# P4-B 普通中文调整决策设置：只读证据审计

状态：只读审计；未占用或启动隔离 8431 Gateway，未修改生产配置/代码，未发送真实模型请求。本隔离 worktree 为 `ef497a904` 加并行未提交切片。目标是区分“工具已接通”与“主模型在普通中文要求下实际选择调用”。

## 已有的结构化操作

`agent/tooling/user_config_tool.py::UserConfigTool` 是原主代理配置工具，`core.py::_register_orchestration_tools` 仅在 `owner_type=main_agent` 时注册。模型可见 schema/说明包含 `decision_read`、`decision_patch`、`decision_reset`、只读 `decision_models` 和须用户明确要求的 `decision_probe`。读回给 `revision={owner,thread}`、有效值/来源；patch/reset 携该双层 expected revision 经 `settings/decision_settings.py::execute_decision_settings_operation` 原服务作 CAS。`scope=owner` 为长期覆盖，`thread` 只取 `agent_core/runner/context.py::current_task_attributes` 中可信会话；模型不能传 owner/thread/run/task 身份去跨域操作。默认关、时间上限和逐点模式仍由原设置表/YAML/dataclass 管，工具不另建一份设置。

`decision_models` 列当前 owner 私有及显式共享的脱敏 Decision profile，不初始化普通聊天模型、不发探测；`decision_probe` 只接受明确 profile ID 和有限正秒数，经原操作真实联网、计量、脱敏。读目录/保存不自动 probe。`user_config` 整体是原 ToolRegistry 的 mutating、串行、operation 幂等工具；普通权限/工具快照/参数/中央执行门仍生效。当前 `ApprovalPolicy` 默认只对 dangerous 要求确认，而该工具声明 mutating，因此既有许可内的读改一般不会机械弹出逐次确认；更严格的显式审批策略或工具不可见/权限拒绝仍按原合同，不能由中文意图绕开。

`tests/test_user_config_capability.py` 覆盖 read→patch→冲突→reset、可信 thread 与跨 owner 拒绝；`tests/test_user_config_decision_operations.py` 覆盖目录/显式 probe 的字段和身份拒绝、真实本地 HTTP 与原用量、取消及工具门；`tests/test_decision_model_operations.py` 覆盖底层模型目录操作；`tests/test_tui_decision_menu.py` 用 Gateway stub 覆盖人工菜单。`docs/tasks/DECISION_MODEL_USER_CONFIG_OPERATIONS_HANDOFF.md` 的“71 passed”属于当时三个工具/服务文件的本地验证。此次另运行四文件联合 `python3 -m pytest -o addopts='' agent_py_agent/tests/test_user_config_decision_operations.py agent_py_agent/tests/test_user_config_capability.py agent_py_agent/tests/test_decision_model_operations.py agent_py_agent/tests/test_tui_decision_menu.py -q --tb=short`：**86 passed**。这证明工具及菜单合同，不证明真实主模型在普通中文里会选择 `user_config`。

## 普通中文进入模型的路径和证据缺口

TUI 与普通终端都由 `cli/chat_parts/gateway_client.py::submit_chat_request` 将当前 `job.user` 作为 `GatewayAskParams.prompt` 入原队列。`gateway_parts/request_execution.py::_execute_gateway_conversation_turn` 只把显式系统 slash 命令拦在模型前，普通中文落原会话历史并经 `_gateway_run_params` 送 `agent.run`；`request_prompt.py` 将其作为用户上下文，不解析“开启/关闭/4秒”等自然语言来直接写设置。主模型必须自己调用原 `user_config`，之后原工具再用结构化 action/字段落账。这与 `/model` 的 `cli/chat_parts/tui_decision_menu.py` 人工菜单是两条消费同一设置服务的入口：菜单通过不等于中文代操作通过。

`docs/tasks/DECISION_MODEL_REAL_VALIDATION.md` 已有普通中文 TUI 回答并产生 Jev 用量、普通中文派工且三个 child 保留 M2.7、后续一次 child 自动采用官方 M3 并完成的原始链记录；`docs/tasks/DECISION_MODEL_CHILD_LIVE_HANDOFF.md` 更细列 M3 的原 advice、thread 自动采用版本、真实出站端点及后续工具轮。它们证明主代理派工后的**宿主自动选模**可运行，用户没有逐 child 选择/确认/补判断资料；它们并非“中文要求修改决策设置”的样本。已查记录未见一次普通中文配置请求同时具备主模型 `user_config/decision_read→decision_patch|reset→decision_read` 工具账、配置版本变化和读回。因此 P4-B 的工具合同可标已实现，**自然中文调用与成功读回仍待真实验收**；Goal 中该项保持未勾是准确的。

## 最小真实验收及判据

待 8431 当前 owner 释放后，仅沿已有**单台隔离 Gateway** 和独立 TUI 会话运行，不重启/抢占它、不改日常 8420。先只读记下隔离 owner/thread 的设置双层 revision、有效值、目录/配置 hash、当前 provider/端点和原工具可见性；使用已保存 Decision profile，不在 prompt 放 key、内部 ID 或工具 JSON。测试者只输入一次普通中文，例如“把这个会话的决策模型总开关打开、等待上限调成 4 秒，并把子代理选模型设为应用；改完告诉我实际生效值。”主模型应自主调用 `user_config` 的 `decision_read`、带刚读到的 revision 作 `decision_patch`、再 `decision_read` 复核；工具调用来自实际主模型响应，不能由测试者脚本代填，也不能靠回复正文推断成功。若它先要列出合法模型，可自行 `decision_models`，但没有用户要求就不得 `decision_probe`；连接不可用时也应能修改关闭设置或如实报告具体失败，不能让 Jev 服务故障阻断原配置管理。

核对同一 request/thread 的 canonical tool call/result、operation、owner/thread revision、effective/sources 与最终模型回复；TUI 显示只是交付投影。`scope=thread` 只影响该会话，另一会话保持原值；设置须按原 CAS 在隔离 owner 恢复，读回恢复后的 revision/effective。再给一次普通中文派工样本（不写模型名/候选 ID、不让用户逐 child 操作），看原 child 创建、Jev 选择或保留、首请求真实 profile/端点与完成状态；配置成功**不能**替代后续真实生效证明。非选择、期限或额度失败时 child 应自动保留原继承模型继续；若明确权限/版本冲突，主模型须如实处理，不能静默重放旧写。真实验收只记录输入 token 和原使用事实，不显示或推算 Jev 价格。

这组样本可判断“普通中文→主模型工具选择→原设置 CAS→读回”是否有效，但单次命中不能推出一般可靠率；若主模型误答或不调用，先查本轮工具可见性、工具 schema/引导、实际模型 tool-use 与原失败码，再决定底层修复，不能在 Gateway 按中文关键词增加第二条配置写入口。自动 child 选模由宿主链完成，用户只给整体意愿，不参与逐子代理的模型选择。

建议下一步：主线等待现有 8431 验收 owner 结束并复用其隔离配置，执行上述一次中文设置样本和一次后续派工样本；此审计与其它只读工作可并行，真实 TUI/Gateway 操作必须由唯一验收 owner 串行持有。若 P4-B 实测失败，再按工具可见性、模型调用、原设置服务的实际证据定位，不新造自然语言路由。
