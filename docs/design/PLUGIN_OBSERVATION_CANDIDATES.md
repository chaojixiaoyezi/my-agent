# 插件观察候选结构（设计稿）

状态：插件线部分**已实施**（2026-09-24 晚，见第 6 节实施记录）；决策线 `action_candidate` 点待接。2026-09-25 插件线评审通过，结论见第 5 节（已并入正文）。它是第 15 项 P5-C"动作候选"的前置条件：没有这层结构，决策模型就无法在不读自由文本的前提下参与"下一步先看哪个元素"。
归属：manifest、代理结果校验、执行前复核与示例插件归插件线；决策接入点归决策线。前期审计见[动作候选审计](../tasks/DECISION_MODEL_ACTION_CANDIDATE_AUDIT.md)，台账摘要见 [DESIGN_LEDGER](../../DESIGN_LEDGER.md)。

## 1. 现状与缺口

- **插件怎么返回结果**：插件是 MCP 进程，宿主的约定只有两处：
  - manifest（`plugin.json`，协议 v1—v4）：声明工具的名称、说明、输入 schema 与 `requested_effect`。
  - 握手扩展（`capabilities.experimental`，如 `my-agent/workspace-read-context` v1）：宿主把可信上下文放进 `tools/call` 的 `_meta`。
  插件 SDK `my_agent_plugin_api` 只是四个宿主模块的构建期投影（路径裁决与工作区读写上下文），**没有工具结果合同**。
- **宿主怎么处理结果**：`MCPProxyTool._execute` 只对 `content`、`content_blocks`、`structuredContent` 脱敏后原样放进工具输出，信任级别 `external_data`，不解释任何字段。
- **browser-lite 的实际形状**：
  - `read` 返回 `{url, title, count, items}`：最多 50 项，每项有 tag、截断到 200 字的文字、name、id、是否可见，以及可选的 value/type/options。结果以 MCP 文本块里的 JSON 字符串返回。
  - `click`/`fill` 按"唯一匹配"的 CSS 选择器执行。
- **缺口**：宿主没有"这次观察看到了哪些可操作对象"的结构事实：没有观察 ID、候选 ID、内容哈希，也没有页面代次。决策模型若参与，只能读自由文本，这违反"自然语言不做机器决策"。也不能为 browser-lite 写专项解析，这违反"禁止专项合同"。

## 2. 设计：三层分工

```text
插件声明（manifest）→ 插件在只读工具结果里给出候选（structuredContent）
  → 宿主校验形状、铸造自己的 ID、写进该次调用的原归档
  → 决策点只从这些 ID 里选一个、给软提示
  → 动作执行前：宿主按调用顺序查新鲜度，插件按页面代次再复核
```

### 2.1 插件声明（manifest，插件线）

新的包协议版本（暂称 v5）允许工具项多两个可选字段：

```json
{"name": "read",  "requested_effect": "read_only", "observation": {"target_kind": "page", "max_candidates": 50}}
{"name": "click", "requested_effect": "mutating",  "observation_ref": {"target_kind": "page", "param": "candidate_id"}}
```

- `observation` 只允许出现在 `requested_effect == "read_only"` 的工具上，否则安装拒绝：观察本身不能有副作用。
- `target_kind` 是开放字符串，只校验形状（如 `^[a-z][a-z0-9_-]{0,31}$`），不设封闭枚举。它只用于把观察和动作配对。
- `max_candidates` 由宿主再夹一次上限（建议 ≤ 64）。
- `observation_ref.param` 指向动作工具输入 schema 里的一个**可选**字符串参数，名字由插件自定，宿主只读 manifest 映射。安装时校验：该参数存在、类型为 string、不在 `required` 里，且不与 `_meta` 保留键或宿主注入参数同名。
- **声明只放 manifest，不另加握手扩展**：已安装的包快照本来就是工具元数据的唯一权威，安装、帮助与审阅时都能看到；若再用握手动态声明，就会出现两个可能冲突的来源。

### 2.2 结果载荷（插件输出）

声明了 `observation` 的工具在成功结果的 `structuredContent` 里放一个保留键：

```json
{"my_agent_observation": {
  "schema": "plugin_observation.v1",
  "target": {"ref": "tab-3", "generation": "17"},
  "candidates": [
    {"key": "e5", "role": "button", "label": "提交订单", "actions": ["click"]},
    {"key": "e9", "role": "textbox", "label": "收货人", "actions": ["fill"]}
  ]
}}
```

- `target.ref`（≤ 128 字符）与 `target.generation`（≤ 64 字符）都由插件定义。页面导航、刷新、弹窗或 DOM 大幅变化时，由插件把代次换新；宿主不猜代次。
- `key`（≤ 64 字符，同一观察内唯一）是插件自己在"同一目标、同一代次"下能解析回对象的键。宿主不解释它，也不把它当选择器、坐标或命令。建议插件用内部序号，不直接用 CSS 选择器。
- `role`（开放短字符串）与 `label`（≤ 120 字符）是 `external_data`：只进有界投影，并按原脱敏处理。宿主不拿它们做任何机器判断。
- `actions` 必须是本插件 manifest 里声明了**同一 `target_kind`** 的 `observation_ref` 工具名。
- 形状不合规就**整份丢弃**这次观察，不部分采纳：超长、key 重复、候选超限、缺 target 或代次、actions 越界都算不合规。工具结果照常交给主模型，归档里只记结构化原因码 `observation_rejected:<code>`。

### 2.3 宿主校验与铸 ID（插件线，代理结果路径）

- **位置**：`PluginProxyTool` 拿到成功结果之后、写归档之前。只处理 manifest 声明了 `observation` 且本次调用成功的只读工具；其它调用零开销。
- **铸造**（身份全部取宿主上下文，不取插件自报，与 `record_lesson` 同一原则）：
  - `observation_id = "obs-" + sha256(run_id, task_id, operation_id, activation_id, tool_name, target.ref, target.generation, canonical_json(candidates))` 取前 24 位
  - `candidate_id = "cand-" + sha256(observation_id, key)` 取前 16 位
- **唯一权威**：观察记录写进该次调用原归档的 `tool_result_envelope.observation`。字段为：
  - `observation_id`、`plugin_id`、`activation_id`、`target_kind`、`target_ref_hash`、`generation`、`content_hash`
  - `candidates[{candidate_id, key, role, label, actions}]`

  不另建观察账本；索引与投影只读这份归档。
- **模型可见投影**：主模型必须看到宿主铸的 `candidate_id` 才能填动作参数，所以宿主在模型可见的结果投影里，把 `my_agent_observation` 重写为有界投影 `{observation_id, candidates[{candidate_id, role, label, actions}]}`，隐去插件的 key、target.ref 与代次。归档仍是唯一权威，投影只做展示；形状被拒时投影里不出现任何候选。
- **"当前观察"判定**：同一 run/task、同一 `activation_id` 与 `target_ref_hash` 下，最新的一次成功观察为 current，更早的都算 stale。调用序取 runtime 工具账（`tool_operations`），不按模型历史。同一 run/task 的后台续跑 attempt 共享这些观察：归属按 run/task，不按 attempt。

### 2.4 执行前复核（两层，插件线）

主模型调用声明了 `observation_ref` 的动作工具，并填了 `param` 指定的参数（值为某个 `candidate_id`）时：

1. **宿主层**（发送前）：在当前 run/task 的归档里查这个 `candidate_id`，新鲜度按 `tool_operations` 调用序判定。查不到、所属观察已 stale、或该候选的 `actions` 不含本工具时，直接拒绝：`OBSERVATION_STALE` / `OBSERVATION_CANDIDATE_UNKNOWN`，`effect_outcome=not_started`，不发送。通过时，只在 `tools/call` 的 `_meta["my-agent/observation-ref"]` 附上 `{"version": "1", "target_ref": ..., "generation": ..., "key": ...}`；模型给的 `arguments` 原样不动，原审批绑定的 `args_hash` 也不受影响。这与 workspace-read-context 放 `_meta` 的方式相同。
2. **插件层**（执行前）：插件核对自己的当前代次等于 `_meta` 里的代次、`key` 能唯一解析，才执行。否则返回 `isError`，并在 `structuredContent` 里带 `{"my_agent_observation_error": {"code": "stale" | "not_found"}}`，不产生副作用。

不填该参数的动作调用保持现状（例如 browser-lite 继续按选择器执行），与候选语义无关。首片不要求动作工具必须走候选。

### 2.5 决策接入点 `action_candidate`（决策线，第 15 项剩余）

- **开关与设置**：独立点，默认 off。沿用原决策设置来源（YAML、AgentConfig、设置服务、TUI 菜单）与逐点的 profile/timeout，不借 `skill_tool` 的开关。
- **触发**：挂在 `_optional_result_hints` 链上，与 `external_material_order`、`delivery_quality` 并列。只在本次归档带 `tool_result_envelope.observation` 且候选 ≥ 2 时触发。
- **发给 Jev**：
  - 本轮用户请求的有界摘要，只作 LLM 上下文；
  - 候选别名 `c1…cN`、role、截断后的 label、actions。

  不发 key、target.ref、URL、插件 ID 或路径。
- **Jev 输出**：每个观察一道 choice：选一个别名，或 `not_needed/no_match/abstain/need_data`。集合外的答案整体作废。
- **采用前复核**：开关与模式、期限、该观察仍为 current、候选仍在观察内、对应动作工具在本轮快照中仍可用、owner/run/task 一致。任一不成立就不附提示。
- **采用效果**：只在该次工具结果展示后追加一行软提示，例如"决策建议：下一步可先核对候选「提交订单」（candidate_id=cand-…）；是否操作、如何操作仍由你按原工具与审批决定"。不改工具结果、参数、审批或归档，也不自动执行。
- **observe 与 off**：observe 只记账、不附提示；off 零请求、零准备。
- **记账**：沿用原决策账，记 observation_id、选中的 candidate_id 与阶段。

## 3. 安全与失效边界

- **注入**：label 可能夹带提示注入，只以"外部数据"身份出现在有界投影里，任何动作参数都不从 label 生成。
- **过期**：新观察一到，旧观察就 stale；页面变化由插件换代次，两层复核都会拦下旧候选。
- **能力边界**：Jev 不能提交工具调用，不能生成选择器、坐标、文字或批准，也不能让不可用的工具变可用。
- **适用范围**：首片只接"manifest 声明且结果合规"的插件观察工具。computer_use 的 OCR 要等上游 schema、坐标系与窗口身份稳定后，由其 owner 按同一结构适配，不在本片内。

## 4. 分工与落地顺序

1. **插件线**：
   - manifest v5 的 `observation`/`observation_ref` 与安装校验；
   - 代理结果路径的观察解析、ID 铸造与归档字段；
   - 发送前新鲜度检查与 `_meta` 附加；
   - browser-lite 输出观察载荷，动作工具加可选 `candidate_id` 与代次复核；
   - 合同测试：形状拒绝、ID 稳定、stale 判定、代次不符不执行。
2. **决策线**：`action_candidate` 点，含配置、设置、TUI 菜单、fake Jev 测试、采用前复核和 off/observe/apply。
3. **真实验收**：
   - 环境：隔离 owner，装 browser-lite，打开含两个相似按钮的本地测试页；
   - 对照 off/observe/apply；
   - 记录 Jev 用量、选中的 ID、提示是否出现、主模型是否采纳，以及页面变化后旧建议是否被拦下。

## 6. 实施记录（插件线，2026-09-24 晚）

- **manifest v5**（`plugin_manifest.py`）：`PLUGIN_PACKAGE_SCHEMA_V5`；工具项可选 `observation{target_kind,max_candidates}`（只允许 read_only）与
  `observation_ref{target_kind,param}`（param 须是输入 schema 里可选的 string，形状 `[A-Za-z][A-Za-z0-9_]{0,63}`，天然排除 `__` 与 `_meta`）；
  一个工具不能两者兼有；同包内按 target_kind 双向配对（引用无观察、观察无引用都拒绝）；v5 至少一个工具带观察字段；v1–v4 字节不变。
  `scripts/build_plugin_package.py` 见到观察字段自动选 v5。
- **宿主实现**（新模块 `agent/plugin_observation.py`）：`parse_observation` 整份接受/拒绝（原因码 schema / unknown_field / target / target_ref /
  target_generation / candidate_count / duplicate_key / candidate_shape / candidate_key / candidate_role / candidate_label / candidate_actions /
  action_not_declared），铸 `obs-`+sha256(run_id, task_id, operation_id, activation_id, 注册工具名, target.ref, generation, 规范候选)[:24] 与
  `cand-`+sha256(observation_id, key)[:16]；候选 `actions` 换成宿主注册名。
- **代理结果路径**（`plugin_runtime.PluginProxyTool`）：插件代理声明宿主参数 `__operation_id`、`__run_scope`（run/task 身份，发送前过滤不转发）；
  只读观察工具成功后把 `structuredContent.my_agent_observation` 校验、铸 ID，模型可见结果改写为 `{observation_id, candidates[{candidate_id,
  role, label, actions}]}`，归档 `tool_result_envelope.observation` 保留完整记录（含插件 `target_ref` 与代次——动作时要原样交还插件复核，
  这是相对原稿"只存 hash"的一处补充）；形状不合规删掉候选、信封记 `observation_rejected:<code>`。归档白名单新增 `observation` / `observation_rejected`。
  合同：观察只进 `structuredContent`，文本正文不重复（否则模型仍会看到插件 key）。
- **新鲜度权威**：`tool_runtime_ledger._append_runtime_event` 在同一条 `tool_completed` 事件载荷里附观察的查找投影（ID、activation、target_ref/hash、
  代次、task/operation、候选 `{candidate_id,key,actions}`）；`runtime_db.repository.events_for_agent_run` 按 seq 读同 agent_run 全部事件（跨 attempt
  共享）。`plugin_observation.current_observation / observation_is_current / resolve_action_candidate` 只读这条事件流；库不可用、没有权威 AgentRun 行、
  失败调用都按"不新鲜"。原稿写的 `tool_operations` 调用序在实现里落为权威事件流的 seq 序，语义相同。
- **发送前复核**：动作工具填了 `observation_ref.param` 时，代理按 `resolve_action_candidate` 复核，未知/过期直接返回 `TOOL_INVALID_ARGUMENTS`
  （reported_error_code `OBSERVATION_CANDIDATE_UNKNOWN` / `OBSERVATION_STALE`，`effect_outcome=not_started`，不发送）；通过后把
  `{version, observation_id, key, target{ref,generation}}` 放进 `_meta["my-agent/observation"]`。库引用经 `ToolRegistry` 构造参数 `plugin_runtime_repo`
  （= `agent.subagents.runtime_db`）传给 `PluginMCPClient(runtime_repo=)`，不另开连接。
- **browser-lite**：`read` 声明 `observation(page, 50)`，`click`/`fill` 声明 `observation_ref(page, candidate_id)` 且 `selector` 改为可选；
  页面代次随 `open` 与点击后导航推进；候选 key = 该次 read 选择器哈希前 8 位 + "." + 序号，插件按同一选择器重新查询并要求总数不变；
  代次不符返 `my_agent_observation_error.code=stale`，key 解析不到返 `not_found`，都不产生副作用；观察与错误只进 `structuredContent`。
- **测试**：`test_plugin_package.py`（v5 往返与 13 种非法声明）、`test_plugin_observation.py`（形状码、ID 稳定、投影、事件序新鲜度、候选复核）、
  `test_plugin_proxy_observation.py`（代理改写与信封、整份拒绝、动作 _meta、未知/过期不发送、不填参数沿旧路径）、
  `test_browser_lite_package.py`（v5 描述；真实浏览器下按候选填写/点击、not_found/stale/缺上下文）。
- **真实链路发现并修复（决策线在隔离 Gateway 上跑 browser-lite，2026-09-24 深夜）**：`tool_runtime_ledger.persist_tool_runtime_ledger`
  原本在归档没有 `runtime_gate` 时提前 return，只读工具（观察工具正是只读）永远不进 `tool_completed` 事件流，`observation_is_current`
  恒为 False；我的合同测试直接调 `_append_runtime_event` 并手造了 gate，没走到这个入口。现改为每次工具完成都追加事件（无门时
  status 按 ok 记 done/failed，不记 blocked），legacy 门账本仍只在有门时写；`events_for_agent_run` / `events_for_attempt` 改为取最新
  limit 条再升序返回，长运行不会把最新观察或收尾事件挤出窗口。测试改走 `persist_tool_runtime_ledger`，并加真实 SQLite 库的
  2100 条填充事件用例。
- **同批记录、待插件线后续处理的 browser-lite 发现**：Gateway 模式下 `open url=相对路径` 按 `request.workspace_root` 解析而不是模型
  以为的 TUI 当前目录；`file://` 被宿主的 URL 参数门先拦（`NETWORK_FILE_URL_BLOCKED`），`http://localhost` 被宿主私网门拦
  （`NETWORK_PRIVATE_HOST_BLOCKED`），README 里"file:// 与 allowed_hosts 默认放行 localhost"的说法只对插件层成立，需要与宿主门对齐。
- **未做**：决策线 `action_candidate` 点（`claude/decision-action-candidate` 待 rebase）；computer_use 观察适配；真实 TUI 端到端验收待决策点接入后一起做。

## 5. 评审结论（插件线，2026-09-25）

1. **声明位置**：放 manifest，不用握手扩展。握手扩展只承载逐次可信上下文，静态声明只能有一个权威。
2. **动作参数名**：按 `observation_ref.param` 由插件自定，宿主只读 manifest 映射。另加安装校验：参数名不能与 `_meta` 保留键或宿主注入参数同名。
3. **保留键**：`my_agent_observation` / `my_agent_observation_error` 与现有插件不冲突。评审补了一个缺口：候选 ID 必须出现在模型可见的结果投影里，已并入 2.3。
4. **大小**：64 个候选、label 120 字（约 10 KB）可以接受。观察按次调用归档，随原工具结果受 `max_chars` 约束，不另建账本，不会累积。
5. **browser-lite 的 key**：用插件内序号加代次。CSS 选择器不稳定，也会诱使模型或宿主把它当选择器用。

另加两条边界（已并入 2.3/2.4）：发送前新鲜度按 `tool_operations` 调用序判定；同一 run/task 的后台续跑 attempt 共享观察。
