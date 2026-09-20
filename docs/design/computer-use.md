# Computer Use 开源执行器接入

## 解决问题

my-agent 需要操作终端之外的系统界面：查看窗口、读取屏幕文字、点击、键入、按键和等待状态变化。但鼠标、
键盘、截图、OCR、跨平台窗口 API 都是成熟基础设施，底座不应再造一套难以维护且难以审计的实现。

本设计把 my-agent 保持为控制面：模型选择工具，Tool Gateway 负责身份、审批、取消、超时、幂等和审计；真正
的桌面动作交给独立开源 MCP 执行器。

## 对照与选型

| 候选 | 许可证/发布 | 结论 |
|---|---|---|
| 会话运行时 Computer Use plugin | 独立 `computer-use@openai-bundled` / `@oai/sky` 执行器 | 证明“执行器独立、Agent 只做控制面”方向；不是本项目可直接发布的公共 Python 依赖 |
| 终端交互 | 使用外部 Ant Computer Use 包 | 公开快照没有可独立审计和打包的完整执行器，不选 |
| Agent-S | Apache-2.0，完整 GUI Agent | 约一整套智能体且以模型生成 PyAutoGUI 代码执行，过重且与 my-agent 控制面重复 |
| UI-TARS Desktop | Apache-2.0，Electron/VLM 完整桌面应用 | 适合独立桌面产品，不适合 Python Gateway 的轻薄执行器 |
| OpenACI / open-computer-use | Apache/MIT 声明，实验或缺独立许可证/稳定发布 | 发布与失败语义不够稳，不作为底座依赖 |
| zavora-ai/computer-use-mcp | MIT，Node/Rust MCP | 工具完整，但 Linux 的 AT-SPI2 无障碍树明确尚未实现，文本模型只能收到不可读图片；同时引入第二套 Node+Rust 运行时，当前不选 |
| `computer-control-mcp` | MIT，PyPI `0.3.13`，Python MCP | 选用；提供窗口、截图、OCR、鼠标和键盘，能直接走现有 stdio MCP |

审计基线：`computer-control-mcp` commit `e74cbb14b16ba616f0dc251a39c9f7732fca8a25`，PyPI 版本
`0.3.13`。本项目不复制其源文件，只在 `pyproject.toml` 的 `computer-use` extra 固定版本。
适配入口通过 MCP 公开注册接口组合上游工具，仅补滚轮与可靠文本输入；不改 SDK 私有注册表。
真实 macOS TUI 暴露即时键入丢字符和中文静默忽略，因此同名 `type_text` 使用已有依赖的公开接口：
macOS 走 Quartz Unicode 键盘事件，其他平台用带间隔的 PyAutoGUI；不支持的字符在输入前报错。
`clear_existing` 是显式参数，按平台发送全选/删除，不解析普通自然语言触发清空。
该修复沿用既有 Computer Use 开关、Full Access 与危险工具审批，不新增依赖或剪贴板状态。
返回值只证明提交了字符，必须另行读取目标应用确认；部分应用可能不接受 Unicode 事件。

接口依据：[PyAutoGUI 键盘文档](https://pyautogui.readthedocs.io/en/latest/keyboard.html)、
[Apple Unicode 键盘事件](https://developer.apple.com/documentation/coregraphics/cgevent/keyboardsetunicodestring(stringlength:unicodestring:))。

## 唯一装配路径

```text
computer_use_enabled
  + structured local/main
  + effective access_mode=full-access
             |
             v
computer_use_profile.py
             |
             v
existing MCP stdio client -> ToolRegistry -> Tool Gateway
             |
             v
computer_use_server.py (组合上游工具、滚轮和文本输入)
             |
             v
computer-control-mcp (PyAutoGUI / RapidOCR / ONNX)
```

- `computer_use_enabled=false` 时不启动子进程，也不承担 OCR/ONNX 运行开销。
- `computer_use` 是保留 MCP server 名。关闭或身份不符时会从合并后的 server 配置移除，不能靠同名手写配置
  绕过开关。
- 只有结构化 `local/main` 且最终裁决为 `full-access` 时注入。远程 owner 即使继承全局开关或自行写
  `full-access`，最终权限仍会被 owner wall 降级，Computer Use 不注册。
- 当前 Gateway 的 local/main Agent 只创建一份执行器；多个 TUI 会话复用同一个 ToolRegistry，不各起一套
  OCR 进程。其他 owner 的 scoped Agent 因身份门不启动它。
- 普通第三方 MCP 默认归入 `catalog_category=mcp` 并由 `tool_search` 渐进披露；官方 Computer Use 明确归入
  `computer_use`，其 16 个稳定工具 Schema 在第一轮直接可见。原因不是权限特例，而是当前 MiniMax-M2.7
  不支持 会话运行时/终端交互 的原生 Tool Search 引用协议。分类只改变 provider 工具目录，不能改变 owner、
  Full Access、effect、审批或执行快照；同一会话中这组稳定 Schema 保持顺序不变，便于前缀缓存复用。
- 子进程使用当前 Python 的 `-m agent_py_agent.agent.tooling.computer_use_server`；公开目录中的上游函数
  注册到唯一 FastMCP server，文本输入由本地实现接管并补充滚轮，不依赖 PATH 中其他 Python。
- Linux 只显式透传当前 `DISPLAY`、`WAYLAND_DISPLAY`、`XAUTHORITY`、`DBUS_SESSION_BUS_ADDRESS`；MCP 安全环境
  仍不继承 API Key、Token 或 Cookie。适配器固定上游 `ENV=development`，只为把其诊断输出送到 stderr，
  防止日志混入 stdout 的 JSON-RPC 数据流。

## 工具风险

| 工具 | effect | 原因 |
|---|---|---|
| `get_screen_size`、`list_windows`、`wait_milliseconds` | `read_only` | 只读显示事实或计时 |
| `move_mouse`、`activate_window`、`scroll_screen` | `mutating` | 改变指针、焦点或当前可见区域，但不直接输入内容 |
| 截图、OCR、点击、键入、按键、拖拽、mouse/key down/up | `dangerous` | 读取敏感屏幕或产生真实外部界面副作用 |

所有工具继续生成 `mcp__computer_use__*` typed 调用，并进入现有 operation、审批绑定、取消和工具输出归档。
上游返回的自然语言成功/失败文本只按 `external_data` 给模型参考，不能反向成为宿主完成或授权事实；真实测试
必须用第二次 OCR、窗口状态或目标应用状态做后置验证。

## 当前边界

- 当前上游固定 `onnxruntime==1.22.0`，实测没有 Python 3.14 wheel；官方 extra 目前支持项目部署所用的
  Python 3.10--3.13，Python 3.14 安装会明确失败，不能静默伪装可用。
- MiniMax-M2.7 是文本工具模型，当前主链不把 MCP image block 直接送进 provider。应调用
  `take_screenshot_with_ocr` 获得文字与绝对坐标，再用第二次 OCR 验证动作；原始 screenshot 工具的 image
  只说明外部执行器确实支持，不能宣称模型已视觉理解。
- macOS 的 Unicode 适配已通过真实 TUI 的英文和中文替换、保存及读回复验；其他平台的 Unicode
  输入尚未覆盖。Quartz 提交成功也不证明每个应用都接受文字，必须保留应用侧读回。
- 纯 OCR 对空白控件和窗口定位仍可能失败；本次空白输入框曾被错点成标签，加占位文字的夹具才完成定位。
  输入修复不等于解决视觉理解、任意控件定位或全部应用兼容性。
- 浏览器内页面优先现有 Browser 工具。Computer Use 只处理浏览器能力覆盖不到的系统 UI、原生应用和桌面。
- Linux 头less 验收需要 Xvfb 之外再运行窗口管理器；没有窗口管理器时窗口枚举、激活和按窗口 OCR 不能作为
  可用证据。RPM 系测试环境还需 `xorg-x11-server-Xvfb`、`xterm`、`xorg-x11-xauth`、
  `xorg-x11-server-utils`、`openbox` 和 `python3-tkinter`。

## 工具披露对照

- 会话运行时 `会话运行时-rs/core/src/mcp_tool_exposure.rs` 只在真实 search tool 可用时把 MCP 标成 Deferred，否则是
  Direct；当前 `ToolSearch` feature 也已标为 Removed/default false。
- 终端交互 `src/services/api/模型助手.ts` 先按具体 model/provider 判定 `isToolSearchEnabled`；不支持时删除
  ToolSearch 自身，并把普通工具直接放入请求，不会把能力藏在模型无法消费的引用协议后面。
- my-agent 当前尚无 provider-native Tool Search 能力协商，不能仅因本地存在一个文本 `tool_search` 就假定
  所有模型都会先调用它。因此采用逐 server 的结构化目录分类，不按用户 prompt 或工具名称做机器判断。

## 真 TUI 验收

测试必须通过目标机器上的唯一 Gateway，模型使用官方 MiniMax-M2.7，并核对实际 provider 与端点。
远端图形环境用独立 Xvfb/xterm；本机按用户授权使用专用测试窗口。普通中文 prompt 只描述用户目标：识别指定窗口文字、激活窗口、输入一段
ASCII、按 Enter，再读取结果文字。通过条件同时包括：

1. TUI 能发现 `mcp__computer_use__*`，且危险动作经过 exact approval；
2. 窗口枚举和 OCR 返回真实 Xvfb 内容；
3. 输入/按键后的目标应用状态改变，并由后置 OCR 或应用输出确认；
4. `/stop` 能中断慢 OCR，Gateway 和 MCP 子进程不遗留失控调用；
5. 同时连接多个 TUI 时仍只有一个 Gateway、local/main 只保留一个 Computer Use MCP 子进程；
6. 一个普通 owner 在相同全局配置下看不到 Computer Use 工具。

## 2026-09-19 本机增量验收

本机 Python 3.12、固定执行器版本，经真实 TUI 和官方 MiniMax-M2.7 完成观察、点击、输入、保存、读回。
最初出现英文丢字符、中文静默忽略，已保留失败记录；修复后新会话连续读回 `DESKTOP-OK-2026`
与 `中文输入验收`，观察者另用只读无障碍树核对最终输入框和保存状态。测试者未代执行点击或填写。
慢 OCR 期间的 TUI 停止已返回工具中断；旧版本的远端隔离验收不计入本轮本机通过数。
四项文本事件回归和现有接入/认证相关定向共 103 项通过；替身不计入真实桌面次数。
截图、原始 OCR、窗口坐标和本机目录只保存在仓库外，脱敏场景见 [本轮矩阵](MAINTAINABILITY_AND_JEV_REVIEW.md#本轮真实-tui-验收矩阵)。

## 2026-09-01 最终验收记录

| 项目 | 结果 |
|---|---|
| 发布物 | commit `8eab29c`；wheel SHA-256 `c6b1fa3d3d4c302984d2c4a5cd333bd0768f22c8becc64995b787ebacf20822e`；Python 3.11；`computer-control-mcp 0.3.13` |
| 单实例 | Gateway `ma-gateway-8eab29c-cu-110` / PID `133160`；Computer Use PID `133203`；验收后均为 1 个 |
| 完整闭环 | `ma-r123-110-u376-computer-use`：activate → screenshot/OCR → scroll → OCR 读出 `SCROLL-R122-927` → click → type → Enter → OCR 读出 `COMPUTER_USE_PASS R122` |
| 工具审批 | screenshot、OCR、click、type、press 均出现 exact TUI approval；activate/scroll 按配置为 mutating，继续进入统一 operation 账 |
| 取消 | `ma-r123-110-u377-computer-stop` 的 120 秒 `wait_milliseconds` 在 Running 时收到 `/stop`，工具立即显示“已中断”；这是与慢 OCR 共用的 MCP client cancellation 通道 |
| owner 隔离 | `ma-r123-110-u378-computer-isolation` 使用普通 owner，模型明确没有桌面操作能力，未出现任何 `mcp__computer_use__*` |
| 启动 | fresh runtime 的唯一 Gateway 从启动到 `/status` 为 `3203ms`；当前已低于 3--4 秒目标，首次冷加载仍受 RapidOCR/ONNX 缓存影响 |

上游 `take_screenshot_with_ocr` 首次按窗口读取曾返回失败文本但没有设置 MCP `isError=true`；MiniMax 随后改用
全屏 OCR 并通过后置读取完成。这个样本再次确认：上游成功文案和 `isError` 只作外部数据，不能代替目标应用
状态验证。原始 screenshot 的 image block 在文本 MiniMax 请求里也没有被冒充成视觉理解。
