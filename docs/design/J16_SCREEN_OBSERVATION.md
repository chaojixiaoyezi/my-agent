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
| `observe_window` | 片 B（已实施） | `read_only`，审批策略 `always` | `observation{target_kind: "window", max_candidates: 64}` | 只读采样一个窗口：不切焦点、不激活、不移鼠标、不写文件 |
| `click_candidate` | 片 B（已实施） | `dangerous` | `observation_ref{target_kind: "window", param: "candidate_id"}` | 点击候选的中心点 |
| `type_into_candidate` | **片 G**（已实施，只在后端能给控件候选时注册） | `dangerous` | `observation_ref{target_kind: "window", param: "candidate_id"}` | 先点可编辑控件让它拿到焦点，再输入显式文字（`text`、`clear_existing`） |

- **读屏审批**：读屏涉及隐私，`observe_window` 用现有的 `ApprovalPolicy(mode="always")`，每次都审批。审批面板是现有的“允许一次 / 本会话允许同一操作 / 拒绝”。effect 如实记 `read_only`，观察合同不用放宽。用户已被告知“每个会话第一次读屏会问你”。
- **何时注册输入工具**：OCR 区域没有“可编辑”这个事实，`type_into_candidate` 在片 G 之前不进工具目录（75 在做工具瘦身，不加用不上的工具）。片 G 有了无障碍树里的可编辑控件，它才和片 G 一起注册。
- **声明来源**：观察声明由宿主在 `computer_use_profile.py` 里写死，和插件 manifest v5 同形；不接受握手自报，不能自己降 effect。
- **参数**：动作工具的模型可见参数只有 `candidate_id`、`text`、`clear_existing`，不暴露 x/y、选择器或窗口号。文字只来自主模型显式填的、经过审批绑定的参数，绝不从 label 复制。
- **`observe_window` 的 `window` 参数（片 F，ae 定规则，真实验收发现模型会直接传标题）**：解析顺序固定——留空 → 叠放最顶的普通可见窗口；以 `win:` 开头 → 只认本进程发过的
  别名（不看可见性，之后由 not_viewable 说明）；其它字符串 → 和可见普通窗口的**展示标题**完全相等（`window_title` = `sanitize_label` 后截到 64 字，就是清单和 observe 结果里给模型看的那个
  字符串），恰好一个才用：0 个 `window_not_found`，2 个以上 `window_ambiguous`（只带命中的那几个）。不 strip、不忽略大小写、不做子串/模糊，不可见的同名窗口不参与；
  真实标题以 `win:` 开头的窗口不能按标题选，请用别名。`window_not_found` / `window_ambiguous` 的 `my_agent_observation_error` 里带 `windows: [{alias, title}]`（可见普通窗口，顶在前，
  最多 16 条，超出 `truncated: true`）。已知限制：清单会把当前可见窗口的标题给模型看——上游 Computer Use 的 `list_windows` 本来就返回标题，不是新增的暴露面。

### 3.1 `observe_window`

输入：`window`（可选）。可以是上一轮观察给的窗口别名，不填就取当前最前的普通窗口。不接受标题模糊匹配，按标题找窗口用上游原有的 `list_windows`。

成功时，`structuredContent.my_agent_observation`（`plugin_observation.v1` 格式，加第 4 节的通用几何扩展）：

```json
{"schema": "plugin_observation.v1",
 "target": {"ref": "win:<boot>:<instance>", "generation": "<boot>-<instance>-<seq>"},
 "frame": {"space": "screen_points", "origin": [0, 0], "size": [800, 600], "scale": [1, 1],
           "captured_at": 1790000000.123, "capture": "screen_region", "occluded": false},
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
  - `frame.scale` 是这张截图自己的像素/点比（核心按截图像素 ÷ 外框点算，宽高两向对不上就 `capture_failed`），不取显示器的（片 E 起）。
  - `frame.capture` 由后端如实报：`window_image`（单窗口内容，被压住的部分也拍得到，macOS ScreenCaptureKit）或 `screen_region`（按屏幕区域截屏，压在上面的窗口也会被拍进去：X11 的 mss、macOS 的回退）。
- **采样**（都不改焦点）：
  - Linux X11：用 EWMH 的 `_NET_CLIENT_LIST_STACKING`（需要窗口管理器，测试用 openbox）列窗口，`translate_coords` 换算到根窗口坐标，用 `mss` 按区域截图；再按叠放次序算有没有上层窗口压在这块区域上（`occluded`）。
  - macOS：用 `CGWindowListCopyWindowInfo` 列窗口，用 ScreenCaptureKit 的单窗口截图（macOS 14 以上的公开接口）；拿不到时退回 `mss` 区域截图，同样算遮挡。主路径不用已废弃的 `CGWindowListCreateImage`（回退用的 mss 内部用它，见 3.2 第 5 条已知限制）。片 E 实施细节见 3.2 第 5 条。
- **候选**：
  - 片 B 起：RapidOCR 公开调用 `RapidOCR()(image)` 的文字区域。`role` 固定为 `ocr_text`，`actions` 只有 `click_candidate`；label 去控制字符，截到 120 字，只作外部数据（external_data）。
  - 片 G 起：无障碍树里的控件（见第 6 节），role 取自系统的结构化角色，可编辑控件才有 `type_into_candidate`。控件和 OCR 合并去重后，
    key 带来源前缀 `ax:<n>` / `ocr:<n>`（片 B 的 `t<n>` 改成 `ocr:<n>`，按留下的 OCR 区域连续编号），先控件后 OCR，合计不超过 64。
- **失败**：MCP `isError` 加 `structuredContent.my_agent_observation_error{code}`，code 取 `window_not_found | not_viewable | capture_failed | ocr_failed | occluded | cancelled | screen_recording_not_permitted | screen_locked | accessibility_not_permitted | focus_not_acquired | clear_unsupported | clear_failed | type_failed`（片 C：宿主已取消；片 E：macOS 没有屏幕录制权限、点击前没有辅助功能权限；vho：macOS 屏幕已锁定；片 G：输入相关，见第 6 节）等。点击之后才发生的失败（输入时拿不到焦点、全选失败、点完被取消、打字中途失败）在错误对象里另带 `clicked: true`，如实说明“已点击、未输入”。码集合是开放的：宿主只把 `stale` / `not_found` 提升为宿主错误码，其它码原样透传，新增码不用改宿主。没有候选时不凭空造候选。
- **采样方式回退**：主路径拿不到、退回别的采样方式时，结果顶层（与 `window`、`generation` 并列）带 `capture_fallback{reason}`；不进 `frame`（宿主的 frame 只认固定键，多一个键整份拒绝），也不进观察载荷。
- **控件树不完整**（片 G）：读到上限被截断，或一个控件都读不到（没授权、绑定缺失、窗口对不上、超时、出错），结果顶层带 `ui_tree{status, reason}`（status 取 `truncated` / `unavailable`，reason 是开放的短原因码），只在不完整时出现，放法同 `capture_fallback`；读不到时 OCR 候选照出，观察不失败。

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
   - 复核和点击之间一定有一小段时间差，绕不开：复核通过后立刻点击，中间不做任何别的 I/O。
   - 返回值只证明“提交了”，结果要靠下一次 `observe_window` 确认。
4. **片 B 实施定稿（2026-10-02，ae 定、ef 实施）**：
   - 适配器层不要求“最新一代”：按 `_meta` 的代次在环里找快照，找不到或 `key` 不在那一代 → `not_found`；找到后按上面第 2 层的顺序逐项核对，任一项不过 → `stale`。宿主层已经只放行最新观察，适配器再加“必须最新”会误伤并发的几个 run，而像素、几何、遮挡复核已经保证安全。
   - 快照：`screen_observation_store.SnapshotStore`，每个窗口实例最近 4 代（`SNAPSHOT_RETAIN_COUNT`），最多跟踪 64 个窗口（`TRACKED_WINDOWS_MAX_COUNT`，按最近采样淘汰）；只存几何、可见事实和逐候选的 `{region, grid}`，不存整幅图。`boot` 用 `token_hex(8)`；窗口原生 ID 在上一次列表里不存在、这次出现就算新实例（ID 复用也算新），消失的窗口连快照一起忘。
   - 区域摘要（`screen_region_digest`）：灰度 → 按面积平均缩成 16×8 格 → 量化 16 级；sha 相等走快路径，否则“量化级相差 ≥2 的格子不超过 4 个”算没变。数字都是带单位后缀的模块常数，不做配置项。亮光标缩成格子后整列都会动，可能超过 4 格（已知风险）：片 C 的 Xvfb 集成加带闪动光标的输入框统计误判次数，现在不调容差。
   - 遮挡：X11 用 `_NET_CLIENT_LIST_STACKING`（底→顶）里排在目标之后且可见的窗口，加根窗口下可见的 override-redirect 子窗口（菜单、tooltip），矩形带 `_NET_FRAME_EXTENTS` 边框；`frame.occluded` = 任一上层矩形与窗口相交，整个被盖住 → 错误码 `occluded`；点击点在动作时重新查叠放。跨桌面、未映射、`_NET_WM_STATE_HIDDEN` 一律 `not_viewable`。macOS 同口径留片 E。
   - 已知限制：半透明窗口也算遮挡；异形窗口按外框算；两次采样之间 XID 先消失又复用的情况靠几何和像素复核兜底。
   - 慢 OCR 与取消（片 C）：观察处理器把采样 + OCR 放到工作线程里 `await`（一次只跑一个，锁），事件循环保持可读，宿主 `/stop` 发来的 `notifications/cancelled` 能被底层 Server 处理——宿主马上拿到 `CANCELLED`、结果被丢弃，线程里的 OCR 跑完自然结束。有锁意味着取消后的下一次观察要排在被丢弃的那次 OCR 之后才开始；不被堵的是事件循环（取消通知、ping、其他工具）。协程被取消会置位本次调用的取消标记：排在锁后面的调用拿到锁先看它，点击在复核完、真正点之前再看一次，已取消就零副作用返回 `cancelled`——用户按了停止之后屏幕上不会多点一下。
5. **片 E 实施定稿（2026-10-02，ae 定、75 实施，macOS 同口径）**：
   - 列窗口：`CGWindowListCopyWindowInfo(kCGWindowListOptionAll | kCGWindowListExcludeDesktopElements)`，仍用 OptionAll 保留最小化、别的桌面窗口和实例身份；不再相信 OptionAll 的返回顺序。另查 `kCGWindowListOptionOnScreenOnly`（同样排除桌面元素），它的在屏窗口按前→后排列；后端按该序整理，并翻成 `list_windows` 的底→顶合同。OptionAll 中未出现在 OnScreenOnly 的窗口保留在结果末尾。身份是 `(kCGWindowNumber, owner PID)`，窗口号被别的进程复用算新实例。`kCGWindowLayer == 0` 才算普通窗口。
   - 可见：`kCGWindowIsOnscreen` 且 alpha>0、外框面积为正、窗口中心落在某块显示器上。公开接口分不清最小化、在别的桌面、应用被隐藏，三者一律 `not_viewable`（与 X11 同口径）。
   - 遮挡：`CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenAboveWindow, 目标窗口号)` 返回压在目标前面的在屏窗口，去 alpha=0 的。只排除层级等于 `CGWindowLevelForKey(kCGDockWindowLevelKey)` 且外框覆盖整块活动显示器的窗口；显示器边界从 `CGGetActiveDisplayList` + `CGDisplayBounds` 获取，不按程序名或标题判断。Dock 自己铺满显示器的系统背景窗不算遮挡；层级 0 的真实全屏应用、层级 100 浮窗、菜单/输入法窗，以及只覆盖屏幕一部分的 Dock 条仍按普通遮挡规则计算。`observe` 与 `recheck` 共用 `above_rects`，判定口径保持一致。
   - **V-H 真机事实（2026-10-03）**：OptionAll 顺序实测不是叠放顺序，OnScreenOnly 才给前→后顺序；程序坞还报告层级 20、alpha 1.0、外框覆盖整块显示器 `(0, 0, 1512, 982)` 的窗口，旧逻辑因此令普通窗口全被判遮挡。修复改用两种结构化系统事实，没有按程序名筛除。
   - 坐标与缩放：外框与 `pyautogui.click` 同一套全局点（主屏左上为原点，副屏可为负）。截图请求按窗口中心所在显示器的显示模式（像素宽 ÷ 点宽）出图；`frame.scale` 由核心按截图算（主路径 2.0、回退 1.0）。观察和复核的 scale 不同（比如观察走主路径、复核退回区域截图）→ 区域摘要不在同一套像素里，判 `stale`。非整数比时 scale 往上挪到“点数 × scale ≥ 像素数”（`covering_scale`）：宿主按 `size × scale` 校验候选 `x+w`，71 点宽、124 像素宽的截图直接相除乘回来是 123.99999999999999，贴右边缘的候选会让整份观察被拒。
   - 权限：每次列窗口前先 `CGPreflightScreenCaptureAccess()`（只查不弹窗），没授权 → `screen_recording_not_permitted`，放在找窗口之前，免得标题拿不到被误报 `window_not_found`。绝不调用 `CGRequestScreenCaptureAccess`。
   - 点击权限：pyautogui 在 macOS 上用 `CGEventPost` 发事件，宿主没有辅助功能权限时系统会悄悄丢掉、不报错，工具却会报“已点击”（片 D 的自动执行会记成假的成功）。所以核心在复核之前先调后端的 `ensure_click_permitted()`：macOS 用 `AXIsProcessTrusted()`（只查不弹窗，绝不用带提示选项的 `AXIsProcessTrustedWithOptions`），没权限或 ApplicationServices 绑定导入失败 → `accessibility_not_permitted`（码与第 6 节片 G 同一个），零副作用；放在复核之前而不是“复核通过 → 点击”之间，保持复核完立刻点击。X11 注入点击不需要额外授权，什么都不做。
   - 截图主路径：ScreenCaptureKit 先查可分享内容找到窗口号，`SCContentFilter(desktopIndependentWindow:)`，不带光标、不带阴影；两个异步回调各带超时（`MACOS_SCREENCAPTURE_CALLBACK_TIMEOUT_SECONDS`），超时后晚到的结果丢掉，每次调用的等待点互不共享。
   - 回退到 mss 区域截图，原因码：`screencapturekit_unavailable`（导入失败或 pyobjc 没有 `SCScreenshotManager`，要 pyobjc 10 以上）、`screencapturekit_too_old`（`platform.mac_ver()` 低于 14）、`screencapturekit_timeout`、`screencapturekit_failed`。不回退的两种：窗口不在可分享内容里（刚关掉或应用禁止截图）→ `capture_failed`，回退会把后面别的窗口拍成它的候选；用户拒绝授权（按 NSError 的 `SCStreamErrorDomain` + `SCStreamErrorUserDeclined` 判，不看文字）→ `screen_recording_not_permitted`，没权限时区域截图只会拍到壁纸。
   - 已知限制：回退用的 mss（10.x）在 macOS 上内部调已废弃的 `CGWindowListCreateImage`，并固定按名义分辨率出图，所以回退截图清晰度较低（scale 1）；ScreenCaptureKit 拍的是单窗口内容，候选可能落在被压住的部分，点击前复核会判 `stale`；半透明但 alpha>0 的覆盖窗口也算遮挡；横跨两块缩放不同显示器的窗口按中心所在那块算。
   - 测试防线：两个后端真会碰桌面的库都只经一个入口拿——macOS 是 `computer_use_macos.load_real_macos_frameworks()`，X11 是 `computer_use_x11.load_real_x11_libraries()`（打开 X 连接、mss、pyautogui；pyautogui 在 Mac 上也能点真屏幕）。conftest 在会话级把两个入口换成直接抛 `RealScreenAccessForbidden`（BaseException，核心吞不掉），X11 那个只在 Linux 车道容器（`MY_AGENT_XVFB_LANE=1`）里不换。防线自检先把真实库的模块名换成一碰就炸的绊线，防线退化时也只会碰到绊线。子进程一层由扫描守卫兜住：递归扫描 tests/ 下所有 `.py`（含子目录和辅助模块，只排除守卫模块自己），同时“打开屏幕观察”和“拉起 MCP 子进程”的文件必须带 `MY_AGENT_XVFB_LANE` 车道跳过标记。真机只读核对不在本片，由 3a 另行安排。
   - 适配器接入：固定的 MCP SDK 1.13 里 FastMCP 和底层 Server 都不能把 `CallToolResult` 原样返回，而观察合同要求失败结果是 `isError` + `structuredContent.my_agent_observation_error`，所以适配器改成单一运行路径——底层 `Server` 收发 stdio，`tools/list` 与普通 `tools/call` 交给 FastMCP 的公开协程，只有 `observe_window` / `click_candidate` 由 `computer_use_observation_tools` 的接管层按名字处理（读 `_meta` 里的观察上下文、自己编码结果）；不碰 FastMCP 私有属性。两个工具只在宿主写了 `MY_AGENT_COMPUTER_USE_OBSERVATION=1`（主配置 `computer_use_observation_enabled` 为 true 且 Computer Use 已装配）时注册。

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
  - 加一项通用粗位置：外框中心按窗口尺寸归一到 0–1，保留一位小数，取自几何扩展。（片 D 已实施：候选有 `region` 且观察有合法
    `frame.size`/`frame.scale` 时材料行多一项 `position{x, y}`，先夹到 [0, 1] 再保留一位小数；算不出就没有这一项，不补默认值。）
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
- **片 G 实施定稿（2026-10-02，ae 定、75 实施，分支 `claude/75-j16-slice-g`，基于 `claude/3a-step17h` `afb15947b`）**：
  - 分层：合并去重、动作判定、复核、输入顺序都在核心（`screen_ui_candidates.py` + `screen_observation.py`）；后端只给事实，鸭子接口新增 `ui_candidates_supported`（结构化能力）、`ui_scan`、`ui_facts`、`ui_focused`、`ui_select_all`、`type_text`、`press_delete`。macOS 的 AX 读取在 `computer_use_macos_ax.py`；X11 不读控件树（`ui_candidates_supported=False`，`ui_scan` 返回空、不带原因）。
  - 读取：先 `AXIsProcessTrusted()`（只查不弹窗）；AX 窗口按 `AXPosition`+`AXSize` 与 CG 外框比，多个再比标题，0 个或多个都不猜（`window_unmatched`）；不用私有的 `_AXUIElementGetWindow`。广度优先，深度 16、节点 600、总预算 2 秒、单条消息超时 0.5 秒（`AXUIElementSetMessagingTimeout`，系统级元素上设置只影响本进程），都是模块常数，V-H 之后按实测调；碰到上限停下、已读的照用。外框和可见范围（窗口 ∩ 各级祖先外框）不相交的子树整棵跳过、不读子节点；没有外框的容器沿用上级范围。只读属性和动作名，不调任何 `AXPerformAction`。
  - 候选判定（不写死角色表）：可点 = `AXEnabled` 且（有 `AXPress` 动作或可编辑）；可编辑 = `AXValue` 可写且是文字；可输入 = 可编辑且不是密码框。role 取 `AXRole`，不合宿主短标识规则的控件丢掉（它的 OCR 文字照常单列）。
  - 去重与 label：OCR 区域面积至少 50%（`UI_DEDUPE_OCR_INSIDE_MIN_PERCENT`，按 OCR 面积、不用 IoU）落在控件里就算同一个，归给面积最小（最里层）的控件；像素区域完全相同的两个控件留后读到的（更深的）。label 依次取 `AXTitle` / `AXDescription` / `AXPlaceholderValue`，都没有就用被吸收的 OCR 文字，再没有就用 role。
  - 值的隐私：密码框（subrole `AXSecureTextField`）一律不读 `AXValue`、不给 `type_into_candidate`（工具参数会原样进归档，密码会明文落盘）；其它控件的 `AXValue` 只判“是不是文字”，读完即丢，不存、不记日志、不算哈希、不进结果；输入后不读回核对，结果 `application_verified=False`。
  - 复核：OCR 候选照旧比区域像素摘要；控件候选（包括吸收了 OCR 文字的）不比像素——输入框里光标会闪，片 C 多轮车道统计 20 次循环误判 3–8 次（0.15–0.40）——改比结构化事实 `UiFacts`：role、enabled、原始外框、label 来源（三项）的 sha、值是否可写（另记选区是否可写，供清空判断），整份相等才算没变；控件没了也是 `stale`。
  - 输入顺序：校验文字（≤500 字 `TYPE_INTO_TEXT_MAX_CHARS`，超了拒绝、不截断；拒绝 Unicode 类别 Cc（含 `\n`、`\t`）与孤立代理码点；空文字只在 `clear_existing=true` 时允许；都在任何副作用之前）→ 点击权限 → 两层复核 → 候选真有输入动作（否则 `invalid_arguments`）、要清空时选区可写（否则 `clear_unsupported`）→ 看取消 → 点击 → 在 0.5 秒内等 `AXFocused`（拿不到 → `focus_not_acquired`）→ 清空时：看取消 → AX 把 `AXSelectedTextRange` 设成 `(0, 字符数)` 并读回核对（失败 → `clear_failed`）→ 看取消 → 用既有 `computer_text_input.type_desktop_text`（Quartz Unicode 键盘事件，不碰剪贴板）打字，替换选区；text 为空时按删除键（`kVK_Delete`，物理键码与布局无关）。打字中途出错 → `type_failed`。点击之后的每一步（等焦点、全选、打字）出了非结构化异常（如 pyobjc 转换失败）也一样带 `clicked: true`：等焦点出错记 `focus_not_acquired`、全选出错记 `clear_failed`，正文写“已点击、未输入”；复核时重新读控件事实抛了非结构化异常按 `stale` 处理（确认不了控件还是原样就不点）。（ae 复审补）
  - 注册：适配器按 `observer.supports_ui_candidates`（后端声明的结构化能力）决定是否注册 `type_into_candidate`，不按平台名；profile 照样声明，没被发现时按片 A 的规则只记 notice。`text` 在 schema 里必填，所以按片 D 的自动执行规则（必填项 ⊆ {候选参数}）它在结构上就不会被自动执行。
  - Linux AT-SPI：只评估、写安装说明（见 computer-use.md），不进默认依赖，没有代码路径。
  - 已知限制：多行文字填不了（拒绝换行，以后要提交另加结构化参数）；`AXValueCreate` 传 `(0, n)` 元组、`AXUIElementSetMessagingTimeout` 设在系统级元素上，这两处 pyobjc 的实际行为没在真机上跑过（V-H 核对）；同一应用里外框和标题都相同的两个窗口读不到控件；网页内容的树可能超过深度或节点上限（只截断不失败）。

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

**片 D 实施定稿（2026-10-02，ef，ae 定规则）**：

- **开关**：`capability_config.yaml` 的 `action_candidate_auto_execute_enabled`（dataclass `CapabilityConfig` 同名字段，默认 false；在
  `USER_SETTINGS_BOUNDARY_KEYS` 里，模型不可写，管理员 `/settings` 可翻；参数中心 source=capability、writable=false）。运行时经
  `capability_config_for_agent` 读，只认 `True`；替身对象上的“真值”不是配置。
- **规划点**：`tool_context/decision_action_execute.plan_auto_execution`，在 `action_candidate` 的建议**采用之后**（来源复核、新鲜度与期限都过了）才问：
  1. 开关关 → 什么都不做、不记账；
  2. 属主：本轮工具快照 `owner_type == main_agent` **且** home 身份是字段齐全的本机 local/main（`is_complete_local_admin_owner`），否则记 `owner_scope`；
  3. 可自动执行的动作 = 所选候选 `actions` 里、本轮快照有、处理器公开属性 `observation_binding` 带 `observation_ref`、且 schema `required ⊆ {observation_ref.param}`
     的工具。恰好一个才执行；0 个记 `no_auto_action`（要文字的输入动作永远不自动执行），≥2 个记 `ambiguous_action`，不按工具名排优先级；
  4. 幂等：读 owner 权威库 runtime_events 里当前 run/task 的 `observation_action` 事实（动作工具按候选发送过就有，不论成功、失败或被提供方拒绝；
     宿主执行与模型自己的动作共用这条事实），同一观察已被碰过记 `already_acted`。
  通过后把一次宿主 `ToolCall` 计划进 `params.host_actions`：`call_id = host-action-<observation_id>`、`operation_id = action_candidate:auto:<observation_id>`
  （幂等键由它派生）、参数只有 `{observation_ref.param: candidate_id}`，run/turn/attempt/协议沿原观察调用。
- **执行点**：`tool_loop/round_execution._run_host_actions` 在观察调用记录完（`record_one`）之后立刻取走计划，`_execute_host_action` 走模型调用同一条链：同一
  `execute_one`（ActionPolicy、绑定新鲜度复核、适配器五项复核）、同一 `_resolve_tool_approval`（审批 binding 多 `actor=decision` 与 `decision_ref`，说明前缀
  `[决策自动执行 actor=decision]`；用户拒绝只记录不重试）、同一 `_record_execution`。索引取 `len(本轮模型调用) + 观察调用索引`，不与模型调用撞号。执行前已
  中断/取消就不执行，决策账记 `interrupted`。`actor=decision` 的请求不再取计划（防递归）。
- **两本账**：归档多 `actor`（`model` / `decision`）与 `decision_ref`（`<决策阶段操作编号>#<响应输入摘要前 16 位>`）；`tool_completed` 事件载荷同样带
  `actor`、`decision_ref`，动作调用另带 `observation_action{observation_id, candidate_id, tool, task_id}`；决策账追加 `record_kind=auto_execution` 补充行
  （`result_category = auto_execution:executed | auto_execution:skipped:<原因码>`，`auto_execution{operation_id, tool, ok, error_code, reported_error_code,
  effect_outcome, handler_executed, status}` 或 `{reason}`），与工具账共用 `decision_ref`。
- **模型看到什么**：观察记录后的提示改成“宿主将按建议以 actor=decision 自动执行候选 X：工具；是否执行、结果如何以随后的 `[host-action-record]` 记录为准，没有该记录即未执行。
  宿主执行后，不要对同一候选重复执行；需要再操作请先重新观察”（ae 复审建议：点击后按钮外观可能不变，两层复核都会放行，再点就是重复提交；软提示，不加硬门）；
  宿主调用的记录以 `[host-action-record round=R index=K actor=decision]` / `[host-action-output-record …]` 块进 tool_context（text 下同链展示；native 下它不是 IR 承载条目，
  经 runtime.guidance 转发，不伪造 assistant tool_use，也不进原生 IR 配对）。
- **一次只做一次**：复核拒绝（stale / not_found）、执行失败、用户拒绝、取消都只记账，不重试、不改选候选；同一观察第二次进决策点会被幂等事实挡下（`already_acted`）。
- **真实验收（片 F，2026-10-02，Linux 车道容器，M3 + 真 Jev）**：四档各一次 prompt 都跑通，auto 档宿主以 actor=decision 自动点击、两本账都带 decision_ref；
  结果与一条待定问题见第 8 节第 5 条。`observe_window` 之外的来源（插件/MCP）只要声明了 `observation_ref` 且只需候选编号，规则一样适用，但没有真实验收。
- **同一 run 最多自动执行一次（片 F 真实验收发现后 ae 定，3a 同意）**：模型按提示重新观察后，新观察是新的 observation_id、候选编号也换，Jev 又选中同一个按钮，
  按观察编号的幂等挡不住重复提交（第二轮 auto 档 Tk 收到 2 次点击）。收紧：模块常数 `AUTO_EXECUTIONS_PER_RUN_MAX_COUNT = 1`（不做配置项），判定事实 = 当前 run/task
  里 actor=decision 的 observation_action 事实计数（runtime_events），到上限记 `run_limit_reached` 进决策账补充行；不按目标分（OCR 候选跨观察没有稳定身份）；主模型自己的
  动作不计入上限，模型点过的观察仍按 `already_acted` 不执行。另加软约束：本 run 宿主已自动执行过的动作（候选 role、label、粗位置、结果 ok/failed:<码>，取自本 run 内存归档）
  以 `executed_actions` 外部数据进 Jev 材料，题面不变，Jev 仍可选 not_needed；它不替代上限。

## 8. 开关、测试与验收

**开关**（新增的都默认关）：

| 配置 | 位置 | 默认 | 说明 |
| --- | --- | --- | --- |
| `computer_use_enabled` | 主配置（已有） | false | 总开关，不变 |
| `computer_use_observation_enabled` | 主配置（新增） | false | 是否注册新工具；关时工具目录不变 |
| `points.action_candidate.mode` | 决策设置（已有） | off | 生产建议 observe |
| `action_candidate_auto_execute_enabled` | 能力配置（新增，管理员边界） | false | 第 7 节 |

YAML 中文注释、dataclass 默认值、参数中心和设置白名单同步更新。

### 8.1 只看档（vho，2026-10-04）

两个开关的组合决定四个显式档位（不是缺依赖时自动降级；档位只由结构化开关与权限决定）：

| 档位 | `computer_use_enabled` | `computer_use_observation_enabled` | 交出什么 |
| --- | --- | --- | --- |
| 都关（默认） | false | false | 没有 Computer Use 适配器 |
| **只看档** | **false** | **true** | 同一个服务，只交出 `observe_window`；审批 always，每次都问本人 |
| 完整档 | true | true | 上游全部工具 + `observe_window` + 点击/输入候选工具 |
| 只开总开关 | true | false | 上游全部工具，没有观察工具 |

- 背景：生产按计划只装了观察要用的依赖（pyobjc 三件套、mss、rapidocr、pillow），没装 `pyautogui` 与 `computer-control-mcp`。原适配器顶层 `import pyautogui`，开观察就会整体失败，一个工具都交不出来。
- 只看档下：宿主在服务环境里写 `MY_AGENT_COMPUTER_USE_OBSERVE_ONLY=1`，声明表只含 `observe_window`；适配器读到这个标记就不装载上游、不注册上游工具，`tools/list` 恰好只有 `observe_window`。上游 import 全部收进 `_load_upstream()`，只在完整档调用。
- 不交出 `click_candidate` / `type_into_candidate`：只看档的点击/输入无意义（上游依赖不在），也不给"只看"以外的动作面。这两项只在完整档注册。
- 仍然是一个服务、一条 stdio 运行路径，权限照旧（结构化 local/main + Full Access）。
- 覆盖点：宿主装配（`core.py` 计算档位 → `computer_use_mcp_servers(observe_only=...)` → `with_computer_use_observation` 不覆盖只看档）、适配器装配（`build_adapter_server` 按标记只注册 observe_window）、模块顶层无上游 import（AST 用例钉住）。
- **依赖（vho 补丁，2026-10-04）**：`computer-use-observe` 必须包含 MCP 的 Python SDK（`mcp==1.13.0`）——适配器靠它收发 stdio，只看档也离不开；生产原来是从上游 `computer-control-mcp` 间接带进来的，本 extra 不装上游就必须自己声明。它不带点击能力（点击来自 pyautogui 与 computer-control-mcp，都不在本 extra 里）。守护：`test_packaging.py` 扫只看档路径各模块的第三方 import 逐个核对清单。
- **档位标记（vho 补丁）**：完整档写 `MY_AGENT_COMPUTER_USE_OBSERVATION=1`，只看档只写 `MY_AGENT_COMPUTER_USE_OBSERVE_ONLY=1`（宿主在只看档不写观察标记）；适配器"要不要装载观察工具"认这两个标记中任意一个，注册范围再按档位收窄。真机曾因只认后者而交出空目录。
- **锁屏（vho 补丁，2026-10-04）**：macOS 后端在列窗口之前先查锁屏（`CGSSessionCopyCurrentDictionary()` 的 `CGSSessionScreenIsLocked`，以及 `CGGetActiveDisplayList` 的活动显示器数为 0），命中返回 `screen_locked`（消息"屏幕已锁定，解锁后再观察"）。顺序在屏幕录制权限预检之后、列窗之前：锁屏时所有窗口都被盖住，先给这条比让模型看到 `occluded` 更准确。两条查询都只读、不弹授权框；查不到（键缺失 / 抛错）按"无法确认"继续原流程，不因查不到锁屏而拒绝观察。Linux X11 后端没有对应概念，行为不变。

- **只要看、不点：执行层也收窄（vho 补丁三，2026-10-04）**：目录层（`tools/list` 只有一个 `observe_window`）已经能挡住正常调用，但那是"宿主只转发目录里的工具"这一层保证；执行层原来对工具名不做档位判断，绕开目录直接点名 `click_candidate` / `type_into_candidate` 仍会真的点到 observer 的点击/输入方法（ds3 只读探针 P2 实测 `is_error=False`）。现在 `call_observation_tool(..., observe_only=True)` 先判档位：只看档下除 `observe_window` 之外的任何工具名一律返回结构化错误 `tool_not_available_in_observe_only`，不调用 observer 的任何方法。档位仍只来自同一个结构化环境标记 `MY_AGENT_COMPUTER_USE_OBSERVE_ONLY`（`build_adapter_server` 读一次，透传给 `install_observation_handler` → `observation_call_handler`），不新增来源、不按工具名或调用方身份猜。

**测试**：全程不碰用户真实屏幕。开发和验收在 Linux 车道容器里，用 Xvfb、openbox 和专门的 Tk 测试窗口：两个文字相近的按钮、一个输入框、一个点击后会变的状态行。真实 macOS 桌面最多做一次只读截图核对，需用户同意，由 3a 安排。

1. **合同单测**（假后端）：
   - 几何校验；铸号稳定；新观察让旧观察过期；
   - 适配器复核：换实例、移动、缩放、遮挡、区域像素变化都拒绝；
   - `_meta` 不可伪造；label 注入不进参数；
   - 自动执行的各项生效条件；
   - **属主范围**：非 local/main 的 owner（local/user、飞书用户）工具目录里没有这些工具，按名字直接调也被拒。
2. **假模型 + 假 Jev**（片 D 已实施：`test_decision_action_execute.py`）：
   - off、observe、apply、apply+自动执行四档；
   - 候选少于 2 个时零决策请求；
   - 自动执行幂等；主模型已动作时宿主不执行；
   - 两个可执行动作不执行；ask 审批带 actor=decision、拒绝只记一次；中断不执行；动作工具没有只凭候选编号的绑定不执行；两本账都带 actor 与决策结果编号。
3. **Xvfb 集成**（真适配器、真 python-xlib/mss/RapidOCR、真点击）：
   - 观察 → 点击 → 再观察，确认状态行变了；
   - 移动窗口、关掉再开、改内容后，动作得到 `OBSERVATION_STALE`，测试窗口没收到点击；（片 B/C 已验证）
   - `/stop` 能中断慢 OCR。（片 C 已验证：宿主 0.16 s 拿到 CANCELLED；随后的观察排在被丢弃的 OCR 之后完成，证据记 after_stop_observe_seconds）
4. **变异**，至少 6 个，都要被抓住：
   - 跳过几何校验；
   - 跳过代次复核；
   - 忽略窗口实例；
   - 只比整窗不比区域；
   - 开关关闭时仍自动执行；
   - 自动执行了输入；
   - （片 D 追加）actor 没记成 decision；中断后仍执行；拒绝后改选另一个候选；两个可执行动作挑第一个；不查幂等；不查属主；粗位置不夹不舍入；
     绑定发送后不记动作事实；两本账缺 actor/decision_ref/observation_action；宿主记录进原生 IR；审批请求不带 actor。
5. **真实模型验收**（Linux 车道容器，主模型 MiniMax M3，Jev 真实）：
   - 四档各发一次 prompt；
   - 记录 Jev 用量、选中候选、提示和执行是否发生、复核拒绝次数、M3 是否采纳。
   - **片 F 结果（2026-10-02，ef；证据 `~/.my-agent/decision-evidence/j16-slice-f-<sha>/`）**：容器内 Xvfb + openbox + Tk 测试窗口，隔离 home（local/main、
     Full Access、`computer_use_*` 两开关、`max_tool_rounds=12`），模型目录只抽 MiniMax 官网 M3 与生产同一个 Jev 端点（jev-1.13.0），审批由测试方接收器替用户批准并逐条记
     `binding.actor`。第二轮（prompt“请先用 observe_window 看一下当前最前面的窗口，再点击里面的 Submit 按钮…”）：off → M3 4 次调用、Jev 0，模型 observe→click_candidate→
     再 observe，Tk 收到 1 次点击；observe → M3 4、Jev 2（1.24 s / 1.22 s，只记账），模型自己点，Tk 1；apply → M3 4、Jev 2（1.55 s / 1.22 s，提示），模型点了 Submit 候选，Tk 1；
     auto → M3 3、Jev 2（1.32 s / 1.27 s），宿主以 actor=decision 自动点击两次（两次观察各一次，审批 binding 带 actor=decision/decision_ref，runtime_events 与决策账补充行
     都 ok/confirmed），Tk 2 次——见上面“待定”。复核拒绝（stale）0 次。总用量：M3 15 次 / 约 42 万输入 token；Jev 6 次 = 预计 6 次。
     第一轮（prompt 写了“标题为 J16 Smoke 的窗口”，证据 `run1/`）：模型把标题当 `window` 参数传，而该参数只接受上一次观察的别名或留空，于是 observe_window 连续
     `OBSERVATION_WINDOW_NOT_FOUND`；off / observe 两档模型改用上游 OCR + click_screen（没点中 Submit），apply 档第 4 次留空成功后 Jev 选中、模型点中；auto 档因 harness
     在建实例后才写能力开关（快照已缓存）没跑。第一轮 M3 26 次 / 约 77 万输入，Jev 2 次。两轮都保留，不挑成功。
     按标题选窗口实施后的补跑（证据 `run3/`，同一句提到标题的 prompt，只跑 apply）：`observe_window(window="J16 Smoke")` 第一次就命中，失败码序列为空；Jev 2 次选中，
     模型点中 Submit 并按别名再观察确认；M3 4 次 / 约 11 万输入，耗时 14.3 s。
6. **macOS 后端和片 G**：假 Quartz、假 ScreenCaptureKit、假 AX 单测；真机只读核对另行安排。片 E 已按此实施（`test_computer_use_macos.py`、`test_screen_capture_guard.py`），片 G 同（`test_screen_ui_candidates.py`、`test_computer_use_macos_ax.py`，假 AX 在 `tests/_fake_macos_ax.py`）。
   - **V-H 真机核对清单**（J16 各片合完后由 3a 一次性向用户申请，会弹“屏幕录制”与“辅助功能”两个授权）：真实窗口的列窗、遮挡与 Retina 缩放；ScreenCaptureKit 主路径与 mss 回退；AX 窗口匹配、真实应用与网页的树深和节点数（据此调上限）；`AXValueCreate((0, n))` 全选与读回、`AXUIElementSetMessagingTimeout` 设在系统级元素上；中文输入法开着时，`type_desktop_text` 用的“键码 0 + Unicode 字符串”事件会不会被输入法截走当成拼音；AZERTY 等非 QWERTY 布局下，现有 `type_text` 的清空（`pyautogui.hotkey("command","a")` 按美式键位发 a）会不会变成 ⌘Q。

## 9. 给能看图的主模型附截图（第二期）

能看图的模型（档案声明 `input_modalities` 含 `image`，如 M3）可以随观察结果附一张缩小后的窗口截图。**必须复用 ef 第 14 条（派子代理时把图片带过去）建的那条“图片进模型请求”通道**，不另开一条。第 14 条交付后再接；在那之前观察只给结构化候选。

## 10. 分片与工作量（agent 工时）

| 片 | 内容 | 估计 |
| --- | --- | --- |
| A | 观察三件套抽成通用的 `ObservationBinding`；几何扩展校验；核实并补齐 MCP 工具的审批策略声明；插件回归不变 | 3–4 h |
| B | Linux X11 后端 + `observe_window`、`click_candidate` + 适配器复核 + profile 声明 + `computer_use_observation_enabled` + 属主范围用例 | 6–8 h |
| C | 车道镜像加 Xvfb、openbox、Tk 测试窗口；集成测试；变异（已实施：`test_computer_use_xvfb_cases.py` 覆盖关掉再开、改内容、/stop 中断慢 OCR、闪动光标误判统计 0.15–0.20；派生镜像 Dockerfile.desktop + xvfb_lane.sh 待 3a 落位；Debian 包清单与 pymonctl 要 xrandr 的硬性要求见 computer-use.md 当前边界） | 4–5 h |
| D | `action_candidate` 接粗位置；自动执行路径和能力开关；假 Jev 四档（已实施：第 7 节“片 D 实施定稿”；真实模型验收留给片 F） | 5–6 h |
| E | macOS 后端（Quartz + ScreenCaptureKit + 回退）及单测（已实施 2026-10-02，75；ae 两轮复审通过，已集成 step17h） | 4–5 h |
| F | 真实验收（M3 + Jev，Linux 车道）+ 文档、台账、TESTS（已实施：第 8 节第 5 条；另加 `/plugins list` 末尾的 MCP 服务段，TUI 与 IM 同一段） | 3–4 h |
| G | macOS AX 候选 + `type_into_candidate` 注册；Linux AT-SPI 可选评估和安装说明；合并去重（已实施 2026-10-02，75，分支 `claude/75-j16-slice-g`，待 ae 复审） | 5–7 h |

合计约 30–39 h。每片单独提交、单独评审（ae 审设计）。第 9 节等第 14 条交付后再排。
