# P5-C DOM/OCR/工具动作候选：只读合同审计

状态：只读审计，未接 Jev、未启动 Gateway/浏览器/桌面或真实模型。本隔离 worktree 的 HEAD 为 `ef497a904`，另有并行未提交切片；以下“现有”仅指此次核对的生产入口，不把旧设计或离线夹具当作上线能力。

## 已核对的能力与缺口

- `agent/tooling/browser_session.py::BrowserSessionManager` 含 Playwright `aria_snapshot()`、本页 `ref_map`、`eN` click/type 和点击后的新快照；但全仓生产引用搜索只有该文件自身，当前没有注册 Browser 工具调用它。它是代码库存量，不是主代理当前可调用的 DOM 候选来源。`contracts/offline_channel_browser_contract.py` 只验证离线事件（如 `dom_lookup`、click 后变化），也不是在线浏览器动作执行器。
- 当前桌面主链是 `core.py` 装配 `tooling/computer_use_profile.py::computer_use_mcp_servers`，仅在开关、local/main 身份和最终 `full-access` 同时满足时注册保留名 `computer_use` MCP；否则删掉同名 server。`tooling/computer_use_server.py` 用 FastMCP 公开目录组合上游工具，本地仅替换 `type_text`、增加滚轮。`get_screen_size/list_windows/wait` 为只读，焦点/滚轮等为 mutating，截图/OCR、点击/键入等按 dangerous；不是 Jev 可自行变更的分类。
- `tooling/mcp_registration.py::MCPProxyTool._execute` 传原参数给已绑定 transport，MCP `isError` 只转工具结果，内容及可选 `structuredContent` 按 `external_data` 处理。`docs/design/computer-use.md` 记录文本 MiniMax 主链使用 `take_screenshot_with_ocr` 的文字和坐标，动作后还须第二次 OCR/目标应用读回；上游曾返回失败文字但未设置 `isError=true`。本审计环境 `computer_control_mcp` 与 `mcp` 包未安装，未能核对实际 OCR wire schema；不能声称它已有稳定 candidate ID、屏幕观察代次、焦点/窗口绑定或可靠成功事实。原始 image block 也没有进入当前文本模型视觉主链。
- `capability/decision_recommendation.py::recommend_capabilities` 已有 P5-B `skill_tool`：从原 `ToolRuntimeSnapshot` 给工具/Skill 做默认关闭的短名单展示选择，不换 handler、不扩 `allowed_tools`、不生成参数或执行。`runtime/loop_support.py::_resolve_tool_sections` 又从同一快照生成工具目录和推荐正文。新的动作候选点不能复制“选工具名”当成 DOM/OCR 目标选择，更不能和既有短名单维护两份可用性/权限状态。

## 参数、权限、执行权威

原主模型的 typed `ToolCall` 必须走 `agent_core/tool_call_runtime.py::execute_traced_tool_call` → `tooling/registry.py::ToolRegistry.execute_tool` → `tooling/executor.py::ToolExecutor.execute`。`_normalized_call` 仅以 runtime policy 安全默认值和可信上下文补缺失字段，显式参数不被建议覆盖；`tooling/action_policy.py::ActionPolicy.decide` 随后核对原快照/availability/schema、effect、required action、路径/URL/命令、task boundary、幂等、精确批准、护栏与限流，再进入 handler。`contracts/gates/tool_approval_binding.py` 用 tool/run/operation/idempotency/`args_hash` 绑定原批准；改参数必须重新经过它，不能把 Jev 输出当已批准。执行结果、effect outcome、归档和后置观察属于原工具/operation 链，MCP 文案或 Jev 建议均不升格为成功。

因此 Jev 只能在主模型**下一次选择动作之前**提供软建议：不能直接提交 `ToolCall`，不能生成任意命令、路径、URL、CSS selector、点击坐标、待输入文字或批准记录；不能让未曝光/不可用的 Computer Use 工具变可用。主模型仍决定是否调用原工具并填写参数，原工具照常完整校验；动作后的新 DOM/OCR/应用状态必须由原观察得到，不以模型/Jev 口头“完成”代替。

## 可复用的最小接缝及前置条件

最小可行接缝是现有 `agent_core/_tool_loop_service.py::_record_tool_call`：原观察工具执行并归档后，`render_tool_result_for_live_prompt` 的结果会同时进入 text `tool_context` 与 native IR；未来独立 `decision_action_candidate` 只可在这里附一段有界的“可先检查候选 X”提示，不改原输出、调用或归档。该函数当前由并行工作认领，本审计不碰它。`tool_request_projection.py` 只投影冻结请求，不该变成第二个读取 OCR/授权/执行入口。

接线之前**必须先建立原观察适配器**：仅从已成功的原工具结果生成有限、宿主验证的 `observation_id` 和 `candidate_id`，记录来源 tool call、run/task、窗口/页面身份、观察内容 hash/代次、候选 role/标签及来源定位；候选正文是 `external_data`，需沿原脱敏/不可信边界投影。对 DOM，当前无生产 Browser 工具，不能从 `browser_session.py` 的内部 `ref_map` 越界借用；要先通过正式工具注册和快照返回合同接入。对 OCR，先核对固定上游真实 schema、坐标系、窗口偏移、缩放及多屏语义；OCR 文本中看似坐标的自由字符串不能直接变成可信候选。缺少任一身份、hash 或可安全投影的候选时不发 Jev 请求。

Jev 只从当前观察的 exact ID 集合选一个候选，或返回 `not_needed/no_match/abstain/need_data`；不能提出集合外动作。采用前必须重核开关/模式、绝对短期限、owner/run/task/权限/工具快照、原观察 hash 与窗口/页面/焦点是否仍当前；页面变化、遮挡、滚动、激活窗口变化时丢弃旧建议。即便提示有效，真正点击前仍要原工具的目标有效性/审批；若没有可靠的执行前状态复核机制，首片最多提示“先重新观察该目标”，不自动点击。`off` 零请求/零准备，`observe` 仅留原决策用量与观察、不附提示，普通错误/超时/格式错/非选择都保留原模型流程，取消继续传播。`need_data` 不替主代理读取屏幕或逐次询问用户。设置须沿原独立点、YAML/dataclass/逐点用户配置/TUI 的同一来源，不能借 P5-B `skill_tool` 的开关。

一个稳妥的首片应限于**已接入生产且具可信候选 ID 的观察工具**，只提示主模型下一步优先核对哪个现有候选；不要同时跨 DOM、OCR 和任意工具。按本次源码事实，这个先决条件尚未满足，故当前不能把该点标成“可直接接线”。可以先在假观察 envelope 中验证绑定/失效合同，待正式生产观察字段存在后再接 Jev。

## 本机参考与证据边界

已核对本机 `/Users/xiaoyezi/study-agent/all-agent/hermes-agent-main/tools/browser_tool.py` 的 accessibility snapshot、ref 和会话隔离方向，以及 `/Users/xiaoyezi/study-agent/all-agent/openclaw-main/extensions/browser/src/browser-tool.schema.ts` 的平面动作 schema/参数仍由执行器校验；它们说明“观察到 ref → 模型选动作 → 原执行器校验”的分层。未审查这些仓库的完整运行链，也未验证其当前网络最新版；不能据此推定本仓库已具有相同 Browser 能力。本仓库还核对 `docs/design/computer-use.md`、`docs/design/MAINTAINABILITY_AND_JEV_REVIEW.md`、`LLM_GUIDE.md`、`docs/ROADMAP.md`、`docs/design/DECISION_MODEL_INTEGRATION.md` 和上述源码；参考合同索引与实际生产引用已分开确认。

只读 focused 命令：`python3 -m pytest -o addopts='' agent_py_agent/tests/test_computer_use_profile.py agent_py_agent/tests/test_computer_text_input.py agent_py_agent/tests/test_mcp_registration.py agent_py_agent/tests/test_tool_input_completion_provenance.py agent_py_agent/tests/test_tool_gateway_contract.py agent_py_agent/tests/test_tool_operation_idempotency.py agent_py_agent/tests/test_offline_channel_browser_contract.py -q --tb=short`，结果 `128 passed`。它们证明现有配置、文本输入、MCP/参数/工具协议和离线浏览器合同回归，不证明 Jev 选目标准确或真实 GUI 安全。文档审计不修改生产代码，Ruff 不适用。

实施所有权建议：先由 Computer Use/Browser 工具 owner 独占观察 envelope 与真实候选 ID 合同、源 schema 测试；Jev owner 只负责独立 action-candidate 模块、逐点配置及 fake 决策测试；`_record_tool_call` hook 等并行 owner 完成后再串行集成。真实验收在隔离单 Gateway、专用测试窗口下使用实际模型/固定上游执行器，对照 off/observe/apply，记录 Jev 输入 token、时延、选中 exact ID、原批准、执行和第二次 OCR/应用读回；覆盖页面/焦点变化、过期候选、取消、上游失败文本却 `isError=false`、OCR 缺数据与服务超时。用户只给正常任务，不逐次替代理选择工具或目标。

建议下一步：先补或确认一个生产观察工具的结构化候选来源及执行前失效复核；在此之前保留 P5-C 此点为审计状态，可与其他不碰 `_tool_loop_service.py` 的工作并行，但不能为了完成进度而以自由文本坐标、假 Browser ref 或工具名推荐冒充动作候选。
