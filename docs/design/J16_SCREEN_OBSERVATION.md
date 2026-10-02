# J16 屏幕识别：结构化观察 + 动作候选

- **状态：已确认设计、未实施**（2026-10-02，3a 审定）。本文没有对应的产品代码，测试、变异和真实验收都还没做。
- **依据**：
  - 用户第 13 条：要做，功能做全，先像观察模式那样只给建议、不自动点；
  - 3a 的方向和审定意见；
  - 前置合同：[插件观察候选结构](PLUGIN_OBSERVATION_CANDIDATES.md)第 2–6 节（已实施）；
  - 原阻塞记录：同文第 7 节；
  - 现有接入：[Computer Use](computer-use.md)。
- **分工**：
  - ef 做完第 14 条后，从片 A 开工，按 A→G 分片交付；
  - ae 做每一片的设计评审；
  - 片 E（macOS 后端）可以另派人并行。

## 1. 一句话

在我们自己的 Computer Use 适配器（`computer_use_server.py`，宿主起的 stdio MCP 子进程）里新增工具，直接调操作系统和库的公开接口，给出带稳定窗口身份、截图代次、坐标变换和成功状态的结构化观察。观察和动作都走插件线已有的同一套观察合同（`plugin_observation`：校验、铸号、写归档、新鲜度、动作前复核），`action_candidate` 决策点不用改就能接上：
- observe 只记 Jev 的建议；
- apply 给主模型软提示；
- 另有一个默认关闭的“自动执行”开关，打开后宿主按建议自动点一下，执行前两层复核代次没变。

## 2. 边界

- **原阻塞**（第 7 节）：上游 `computer-control-mcp==0.3.13` 的公开工具缺 4 样东西：
  - OCR 只给 tuple 文本；
  - 窗口没有稳定身份；
  - 没有截图代次和坐标变换；
  - 按窗 OCR 会激活窗口，所以不是只读。

  在“只组合上游公开 MCP 工具”的边界内做不出来。
- **新边界**（用户放宽为“别等上游”）：适配器可以自己实现观察，但只用操作系统和库的公开接口。
  - 不调上游 `computer_control_mcp` 的内部对象（`upstream.gw`、`_mss_screenshot`、`engine` 等）；
  - 不改上游包，也不调私有 API。
- **不新增第三方依赖**：
  - 上游依赖 `pywinctl==0.4.1`：在 macOS 上会带 `pyobjc`（含 Quartz、ScreenCaptureKit、ApplicationServices），在 Linux 上会带 `python-xlib` 和 `ewmhlib`；
  - 上游直接依赖 `mss`、`rapidocr-onnxruntime`、`pillow`。
  - 实施时把用到的这几个写进 `computer-use` extra，显式钉版本。
- 上游原有的 16 个工具保持不变（开关、effect、审批都不变），新工具与它们并列。
- **属主范围**：和现有 Computer Use 完全一样，必须同时满足结构化 `local/main`、最终裁决为 Full Access、`computer_use_enabled`，再加第 7 节的新开关。
  - 普通用户是隔离 owner，不能看到宿主屏幕：他们的工具目录里没有这些工具，按名字直接调也会被拒。
  - 这条有专门用例（第 8 节）。

## 3. 新工具（Computer Use 适配器内）

| 工具 | 注册时机 | effect / 审批 | 观察声明 | 作用 |
| --- | --- | --- | --- | --- |
| `observe_window` | 片 B | `read_only`，审批策略 `always` | `observation{target_kind: "window", max_candidates: 64}` | 只读采样一个窗口：不切焦点、不激活、不移鼠标、不写文件 |
| `click_candidate` | 片 B | `dangerous` | `observation_ref{target_kind: "window", param: "candidate_id"}` | 点击候选的中心点 |
| `type_into_candidate` | **片 G** | `dangerous` | `observation_ref{target_kind: "window", param: "candidate_id"}` | 先点可编辑控件让它拿到焦点，再输入显式文字（`text`、`clear_existing`） |

- **读屏审批**：读屏涉及隐私，`observe_window` 用现有的 `ApprovalPolicy(mode="always")`，每次都审批。审批面板是现有的“允许一次 / 本会话允许同一操作 / 拒绝”。effect 如实记 `read_only`，观察合同不用放宽。用户已被告知“每个会话第一次读屏会问你”。
- **何时注册输入工具**：OCR 区域没有“可编辑”这个事实，`type_into_candidate` 在片 G 之前不进工具目录（75 在做工具瘦身，不加用不上的工具）。片 G 有了无障碍树里的可编辑控件，它才和片 G 一起注册。
- **声明来源**：观察声明由宿主在 `computer_use_profile.py` 里写死，和插件 manifest v5 同形；不接受握手自报，不能自己降 effect。
- **参数**：动作工具的模型可见参数只有 `candidate_id`、`text`、`clear_existing`，不暴露 x/y、选择器或窗口号。文字只来自主模型显式填的、经过审批绑定的参数，绝不从 label 复制。

### 3.1 `observe_window`

输入：`window`（可选）。可以是上一轮观察给的窗口别名，不填就取当前最前的普通窗口。不接受标题模糊匹配，按标题找窗口用上游原有的 `list_windows`。

成功时，`structuredContent.my_agent_observation`（`plugin_observation.v1` 格式，加第 4 节的通用几何扩展）：

```json
{"schema": "plugin_observation.v1",
 "target": {"ref": "win:<boot>:<instance>", "generation": "<boot>-<instance>-<seq>"},
 "frame": {"space": "screen_points", "origin": [0, 0], "size": [800, 600], "scale": [1, 1],
           "captured_at": 1790000000.123, "capture": "window_image", "occluded": false},
 "candidates": [{"key": "t3", "role": "ocr_text", "label": "提交", "actions": ["click_candidate"],
                 "region": [10, 20, 60, 18]}]}
```

- **窗口身份**：
  - Linux X11：XID，配 `_NET_WM_PID`、`WM_CLASS`；
  - macOS：`kCGWindowNumber`，配 owner PID。
  - 适配器进程启动时生成随机 `boot`，并在内存里给每个窗口实例编号：同一个 XID 或窗口号消失后又出现，算新实例。适配器重启，`boot` 就变，旧观察全部作废。
- **代次**：`<boot>-<instance>-<seq>`，`seq` 是这个窗口每次采样递增的序号。适配器按代次在内存里保留最近 N 份快照（几何、缩放、各候选区域的像素摘要），动作时复核用。
- **坐标**：
  - Linux X11：全局坐标就是像素，缩放 1；
  - macOS：用 point，Retina 屏缩放 2.0。
  - `region` 是候选在窗口截图像素里的外框，宿主按 `frame` 换成全局点。
- **采样**（都不改焦点）：
  - Linux X11：用 EWMH 的 `_NET_CLIENT_LIST_STACKING`（需要窗口管理器，测试用 openbox）列窗口，`translate_coords` 换算到根窗口坐标，用 `mss` 按区域截图；再按叠放次序算有没有上层窗口压在这块区域上（`occluded`）。
  - macOS：用 `CGWindowListCopyWindowInfo` 列窗口，用 ScreenCaptureKit 的单窗口截图（macOS 14 以上的公开接口）；拿不到时退回 `mss` 区域截图，同样算遮挡。不用已废弃的 `CGWindowListCreateImage`。
- **候选**：
  - 片 B 起：RapidOCR 公开调用 `RapidOCR()(image)` 的文字区域。`role` 固定为 `ocr_text`，`actions` 只有 `click_candidate`；label 去控制字符，截到 120 字，只作外部数据（external_data）。
  - 片 G 起：无障碍树里的控件（见第 6 节），role 取自系统的结构化角色，可编辑控件才有 `type_into_candidate`。
- **失败**：MCP `isError` 加 `structuredContent.my_agent_observation_error{code}`，code 取 `window_not_found | not_viewable | capture_failed | ocr_failed | occluded`。没有候选时不凭空造候选。

### 3.2 动作前两层复核

1. **宿主层**，发送前；沿用插件线的 `resolve_action_candidate`：
   - `candidate_id` 必须属于本 run/task 这个窗口的最新一次观察（按 `tool_completed` 事件序）；
   - 候选的 `actions` 里必须有这个工具。
   - 候选所属观察的 `activation_id` 必须等于当前绑定的（提供方换代——MCP 连接重建或插件重新激活——后旧候选一律过期）。这是对所有提供方都成立的唯一一层保证：第三方 MCP 服务不一定实现下面第 2 层复核；插件侧旧代理随激活撤销已不可用，不会误伤。
   - 不满足就返回 `OBSERVATION_STALE` 或 `OBSERVATION_CANDIDATE_UNKNOWN`，记 `not_started`，不发送。
   - 满足就把 `{observation_id, key, target{ref, generation}}` 放进 `_meta["my-agent/observation"]`。
2. **适配器层**，真动之前：按 `_meta` 里的代次找到当时的快照（找不到就是 `not_found`），然后重新只读采样这个窗口，逐项核对：
   - `boot` 和实例相同；
   - 窗口可见、没最小化；
   - 原点、尺寸、缩放没变；
   - 点击点不被遮挡；
   - 候选外框区域的像素摘要和当时一样。

   任何一项不符都返回 `my_agent_observation_error{code: "stale"}`，零副作用。宿主按已有逻辑提升为 `OBSERVATION_STALE`，记 `not_started`。
   - 只比候选所在区域，不比整窗，免得时钟、光标闪动导致每次都判过期。
   - 已知风险：候选区域里如果有闪动的光标，可能误判过期，验收时统计误判率。
3. **复核通过才动手**：
   - 外框中心 → 全局点 → `pyautogui.click(x, y)`；
   - 输入时再调现有的 `type_desktop_text`。
   - 返回值只证明“提交了”，结果要靠下一次 `observe_window` 确认。

## 4. 宿主侧：观察三件套抽成通用的

- **现在**：只有 `PluginProxyTool` 有这三件套：
  - `_attach_observation`：解析、铸号、写归档、改写模型可见投影；
  - `_observation_action_meta`：发送前复核，附 `_meta`；
  - `_lift_observation_error`：把插件层的拒绝提升为宿主错误码。
- **改成**：抽成一个与来源无关的 `ObservationBinding`，`PluginProxyTool` 和 `MCPProxyTool` 共用。
  - 插件的声明来自 manifest，Computer Use 的声明来自 profile 写死的表。
  - `activation_id`：插件用激活编号；Computer Use 用“本次 MCP 连接代次”，子进程重启就换代。
- **通用几何扩展**：`plugin_observation.parse_observation` 加可选的 `frame` 和候选 `region`。
  - 宿主校验：有限数、正面积、在截图范围内，`space`、`scale` 合法；不合规就整份拒绝，原因码 `frame`、`candidate_region`。
  - 几何只进原归档和 `tool_completed` 事件，不进模型可见投影。
  - 任何观察提供方都能用，不是屏幕专项合同。
- **不新建观察账本**：唯一权威仍是该次调用的 `tool_result_envelope.observation` 和 `tool_completed` 事件。
- **片 A 先核实**：`MCPProxyTool` 能否按工具声明 effect 和审批策略（`always`）。插件工具可以；MCP 这边如果还不行，就用同一张声明表补上，不另开一条路。
  - 核实结果（2026-10-02）：不行。`build_proxy_tool` 只按 effect 组策略，`approval_policy` 一直是默认 dangerous；配置里只有 `tool_effects`。片 A 补上了下面的声明表。
- **片 A 实施定稿（2026-10-02，ae 定、ef 实施，分支 `claude/ef-j16-slice-a`）**：
  - 三件套在 `agent/tooling/observation_binding.py` 的 `ObservationBinding`，`PluginProxyTool` 与 `MCPProxyTool` 共用；绑定为 None 的 MCP 代理行为逐字节不变。声明复用 manifest v5 的 `PluginToolObservation / PluginToolObservationRef`（已搬到 `plugin_observation.py`，manifest 与 MCP 共用同一条配对规则 `validate_observation_declaration`）。
  - 来源字段只留 `provider_id`（`plugin:<id>` / `mcp:<server>`）：记录、上下文、归档信封统一改名，新信封不再写 `plugin_id`；旧归档不迁移，宿主没有逻辑读它。
  - MCP 的 `activation_id` = `mcp:<server>:<连接随机串前 16 位>`：随机串由宿主在 `MCPTransport` 构造时 `uuid4` 生成，不依赖 ps 的进程出生身份（macOS 只精确到秒、取不到时为空，同一秒重启会撞车），也不做取不到再退回的兜底；子进程重启或重连就换代。宿主复核多一条（ae 复审裁决保留，已写进 3.2 节第 1 层）：候选所属观察的 activation_id 必须等于本绑定的，否则 `OBSERVATION_STALE`、不发送。
  - MCP 逐工具声明表：`mcp_servers.<server>.tool_approvals`（只能等于或严于默认 dangerous：dangerous / mutating / always；`never` 配置非法）与 `tool_observations`（和 v5 同形），部署者也可对第三方服务声明；Computer Use 由 `computer_use_profile.py` 一张表产出三项（片 A 只搬现有工具，审批与观察为空）。
  - 核对只按本次发现到的工具算：坏项（observation 挂非只读工具、ref 参数不在 schema 或不是可选 string、ref 没有同类观察配对、工具 schema 本身不可规范化 `input_schema_invalid`）整个服务拒绝发布，`MCPDeclarationError` 带结构化 reasons，不连带别的服务；`tool_observations` / `tool_approvals` 里声明了但没发现的工具只记 notice。发布结果记成客户端的 `publication`（published / rejected / unavailable，含 code / reasons / notices），`mcp_registration.mcp_server_facts(registry)` 是只读投影；挂到哪个面板由集成时定。
  - 几何扩展：`frame` 必填 space / origin / size / scale（origin 可为负），可选 captured_at / capture / occluded，多余键拒绝，数值判定排除 bool；候选 `region=[x,y,w,h]` 用截图像素、`x+w ≤ size_w×scale_x`，有 region 必须有 frame；原因码 `frame` / `candidate_region`。region 进候选规范形式（参与 content_hash 与 observation_id），frame 不进；几何只进归档信封与 `tool_completed` 事件（候选 region、frame 的 size/scale），不进模型投影。
  - 审批核实三层：合同单测（profile → `from_mapping` → 发布 → `approval_policy.mode == "always"` 且 effect read_only）、`tool_manifest_contract` 投影、`ActionPolicy` 在自主（auto）模式下仍 ask；真链路读屏审批放到片 F 的真实验收。

## 5. 决策点 `action_candidate`

- **触发条件不变**：本次归档带 `observation`，且候选不少于 2 个。
- **发给 Jev**：
  - 本轮请求的有界摘要；
  - 候选别名 `c1…cN`、role、截断后的 label、actions；
  - 加一项通用粗位置：外框中心按窗口尺寸归一到 0–1，保留一位小数，取自几何扩展。
  - 不发绝对坐标、窗口号、标题或路径。
- **Jev 只做选择题**：选一个别名，或 `not_needed / no_match / abstain / need_data`。不生成文字、坐标或参数。“点哪”由 Jev 选，“输入什么”只由主模型填。
- **各模式**：
  - off：不发请求。
  - observe：生产默认，只把建议记进决策账（观察编号、候选、阶段），不提示、不执行。
  - apply：沿用已有语义，在工具结果后追加一行带 `candidate_id` 的软提示。
  - apply + 自动执行：见第 7 节，默认关。
- 模式本身不授予任何执行权限。

## 6. 片 G：无障碍树候选（让输入有真实目标）

- **为什么要做**：只靠 OCR，永远拿不到“这是输入框、可编辑”这个事实，`type_into_candidate` 就是摆设。用户要“做全”，所以单列一片。
- **macOS AX（先做）**：用 pyobjc 已带的 ApplicationServices 公开接口：
  - 读目标窗口所属进程的 `AXUIElement` 树（`AXRole`、`AXSubrole`、`AXTitle`/`AXDescription`、`AXFrame`、`AXEnabled`、可编辑属性）；
  - 只读遍历，有深度和数量上限，不调任何 `AXPerformAction`；
  - 候选的 role 取系统角色（如 `AXButton`、`AXTextField`），可编辑的控件才有 `type_into_candidate`；
  - 需要用户在系统设置里授予“辅助功能”权限，没授权时返回结构化错误 `accessibility_not_permitted`，只退回 OCR 候选。
- **Linux AT-SPI（可选项，只评估，不进默认依赖）**：
  - 需要系统包（Debian/Ubuntu：`python3-gi gir1.2-atspi-2.0 at-spi2-core`；RPM 系：`python3-gobject at-spi2-core`），以及会话 D-Bus 和支持无障碍的应用（GTK/Qt）；Tk 测试窗口不暴露 AT-SPI。
  - 评估结论和安装方式写进 Computer Use 文档；能 import 时才启用，否则只用 OCR。
- **候选合并**：同一窗口的 OCR 区域和无障碍控件按外框重叠去重，无障碍控件优先（它有结构化角色和可编辑标记）。key 带来源前缀（`ax:`、`ocr:`）。
- **`type_into_candidate`**：随片 G 注册，只接受 actions 里有它的候选，也就是可编辑控件。

## 7. 自动执行（功能做全，默认关）

- **开关**：`capability_config.yaml` 新增 `action_candidate_auto_execute_enabled: false`。source 记 capability，safety 记 boundary；只有管理员能经 `/settings` 改（和 C4 同一个白名单），模型不能改。
- **生效条件**，全部同时满足：
  - 开关为 true；
  - 这个点的有效模式是 `apply`；
  - Jev 选中的候选 `actions` 里有 `click_candidate`；
  - 第 2 节的属主范围成立；
  - 这个观察还没自动执行过（幂等键是观察编号）。
- **只自动点击，不自动输入**：文字只能来自主模型或用户。
- **执行路径**：
  - 宿主以“决策建议”为来源，构造一次 `click_candidate(candidate_id=…)`，走同一个 Tool Gateway：operation、审批、取消、超时、两层复核、归档都和手动调用完全一样。
  - 工具账和决策账都记 `actor=decision` 和决策结果编号。
  - 执行结果作为宿主事件进下一轮模型上下文，由主模型决定下一步。
- **不执行的情况**：
  - 复核拒绝：只记账，不重试，也不改选别的候选；
  - 主模型同一轮已经对这个观察发了动作：以主模型为准；
  - 子代理回合：首期不开放。

## 8. 开关、测试与验收

**开关**（新增的都默认关）：

| 配置 | 位置 | 默认 | 说明 |
| --- | --- | --- | --- |
| `computer_use_enabled` | 主配置（已有） | false | 总开关，不变 |
| `computer_use_observation_enabled` | 主配置（新增） | false | 是否注册新工具；关时工具目录不变 |
| `points.action_candidate.mode` | 决策设置（已有） | off | 生产建议 observe |
| `action_candidate_auto_execute_enabled` | 能力配置（新增，管理员边界） | false | 第 7 节 |

YAML 中文注释、dataclass 默认值、参数中心和设置白名单同步更新。

**测试**：全程不碰用户真实屏幕。开发和验收在 Linux 车道容器里，用 Xvfb、openbox 和专门的 Tk 测试窗口：两个文字相近的按钮、一个输入框、一个点击后会变的状态行。真实 macOS 桌面最多做一次只读截图核对，需用户同意，由 3a 安排。

1. **合同单测**（假后端）：
   - 几何校验；铸号稳定；新观察让旧观察过期；
   - 适配器复核：换实例、移动、缩放、遮挡、区域像素变化都拒绝；
   - `_meta` 不可伪造；label 注入不进参数；
   - 自动执行的各项生效条件；
   - **属主范围**：非 local/main 的 owner（local/user、飞书用户）工具目录里没有这些工具，按名字直接调也被拒。
2. **假模型 + 假 Jev**：
   - off、observe、apply、apply+自动执行四档；
   - 候选少于 2 个时零决策请求；
   - 自动执行幂等；主模型已动作时宿主不执行。
3. **Xvfb 集成**（真适配器、真 python-xlib/mss/RapidOCR、真点击）：
   - 观察 → 点击 → 再观察，确认状态行变了；
   - 移动窗口、关掉再开、改内容后，动作得到 `OBSERVATION_STALE`，测试窗口没收到点击；
   - `/stop` 能中断慢 OCR。
4. **变异**，至少 6 个，都要被抓住：
   - 跳过几何校验；
   - 跳过代次复核；
   - 忽略窗口实例；
   - 只比整窗不比区域；
   - 开关关闭时仍自动执行；
   - 自动执行了输入。
5. **真实模型验收**（Linux 车道容器，主模型 MiniMax M3，Jev 真实）：
   - 四档各发一次 prompt；
   - 记录 Jev 用量、选中候选、提示和执行是否发生、复核拒绝次数、M3 是否采纳。
6. **macOS 后端和片 G**：假 Quartz、假 ScreenCaptureKit、假 AX 单测；真机只读核对另行安排。

## 9. 给能看图的主模型附截图（第二期）

能看图的模型（档案声明 `input_modalities` 含 `image`，如 M3）可以随观察结果附一张缩小后的窗口截图。**必须复用 ef 第 14 条（派子代理时把图片带过去）建的那条“图片进模型请求”通道**，不另开一条。第 14 条交付后再接；在那之前观察只给结构化候选。

## 10. 分片与工作量（agent 工时）

| 片 | 内容 | 估计 |
| --- | --- | --- |
| A | 观察三件套抽成通用的 `ObservationBinding`；几何扩展校验；核实并补齐 MCP 工具的审批策略声明；插件回归不变 | 3–4 h |
| B | Linux X11 后端 + `observe_window`、`click_candidate` + 适配器复核 + profile 声明 + `computer_use_observation_enabled` + 属主范围用例 | 6–8 h |
| C | 车道镜像加 Xvfb、openbox、Tk 测试窗口；集成测试；变异 | 4–5 h |
| D | `action_candidate` 接粗位置；自动执行路径和能力开关；假 Jev 四档 | 5–6 h |
| E | macOS 后端（Quartz + ScreenCaptureKit + 回退）及单测 | 4–5 h |
| F | 真实验收（M3 + Jev，Linux 车道）+ 文档、台账、TESTS | 3–4 h |
| G | macOS AX 候选 + `type_into_candidate` 注册；Linux AT-SPI 可选评估和安装说明；合并去重 | 5–7 h |

合计约 30–39 h。每片单独提交、单独评审（ae 审设计）。第 9 节等第 14 条交付后再排。
