# desktop-lite

自有工具型插件：在用户**本机桌面**上做三件小事——发系统通知、用系统默认程序打开工作区里的文档、把文本放进剪贴板。
只调用本机自带或常见的系统程序，不联网、不调用模型、不写工作区。当前已完成本地标准包、独立 MCP 进程组件验证和
一次 macOS 真实调用（osascript / open / pbcopy 退出码 0，剪贴板中文内容正确），真实 TUI 验收尚未完成。

## 平台支持

| 功能 | macOS | Linux | 其他平台 |
| --- | --- | --- | --- |
| `notify` 通知 | `osascript`（系统自带） | `notify-send`（libnotify） | 不可用 |
| `open` 打开文件 | `open`（系统自带） | `xdg-open`（xdg-utils） | 不可用 |
| `clipboard` 剪贴板 | `pbcopy`（系统自带） | Wayland 优先 `wl-copy`，否则 `xclip` | 不可用 |

找不到程序或平台不支持时，工具明确返回 `DESKTOP_UNAVAILABLE`，文案形如
"桌面通知不可用：未找到 notify-send（可在设置 notify_send_path 指定）"，不会改用别的程序。
macOS 上 osascript 发出的通知归属"脚本编辑器"，若系统设置里关闭了它的通知，命令成功但看不到横幅。

## 开发构建

以下命令从仓库根执行，构建离线、不下载依赖、不改用户插件安装表，输出已存在时明确失败：

```bash
python scripts/build_plugin_api.py --wheel-dir /tmp/plugin-build/wheels
python scripts/build_plugin_package.py \
  --project plugins/desktop-lite \
  --declaration desktop_lite/declaration.json \
  --wheel /tmp/plugin-build/wheels/my_agent_plugin_api-0.2.0-py3-none-any.whl \
  --output /tmp/plugin-build/desktop-lite.zip
```

## 使用

安装并启用后提供三个动作，与同名工具一一对应，三个工具都声明 `mutating`（会影响用户桌面，走宿主原审批链）：

```text
/plugins@desktop-lite notify --title 构建完成 --message "测试全部通过，可以合并了"
/plugins@desktop-lite open --path docs/报告.pdf
/plugins@desktop-lite clipboard --text "git checkout -b feature/x"
```

中文示例：普通对话里说"跑完测试后给我弹个通知"或"用预览打开 out/chart.png"，模型会调用对应工具。
成功时返回 `{"tool", "program", "exit_code"}`（`program` 是实际调用的程序绝对路径），`open` 另带打开的绝对路径 `path`，
`clipboard` 另带字符数 `chars`；不回显通知或剪贴板正文。

## 设置

- `osascript_path` / `notify_send_path` / `open_path` / `clipboard_path`：对应系统程序路径，默认空（按当前平台在 PATH 中探测）。
  宿主启动插件时只透传 `PATH` 等基线环境变量；Gateway 的 `PATH` 里找不到时请填绝对路径。填了就只用该值，不再自动探测。
- `command_timeout_seconds`：单次调用超时，默认 10，范围 1–60；超时终止该进程并返回 `COMMAND_TIMEOUT`。

取值范围见唯一声明 `src/desktop_lite/declaration.json`；设置不增加任何读写权限。

## 安全边界

- **不注入**：用户文本只作为独立 argv 或 stdin 交给子进程，从不经过 shell。macOS 通知用固定 AppleScript（`on run argv`）
  从 stdin 交给 `osascript -`，标题与内容只作为 argv 进入脚本，引号、反斜杠、`& do shell script` 都按普通文字显示；
  Linux `notify-send` 前加 `--`，以 `-` 开头的标题不会被当成选项。剪贴板文本只经 stdin 传入。
- **长度**：标题 ≤ 80 字、内容 ≤ 300 字、剪贴板 ≤ 20000 字，含 NUL 的文本拒绝。
- **open 只开文档**：路径经宿主逐次读取上下文授权，`..` 上溯与越出读取范围拒绝；用 SDK no-follow 逐段打开确认是单链接
  普通文件（符号链接、硬链接、目录一律拒绝）；带任何可执行权限位的文件拒绝；`.app/.pkg/.dmg/.command/.sh/.bat/.exe/
  .ps1/.scpt/.workflow/.jar/.desktop/.webloc/.url` 等会被系统"运行"而非"查看"的扩展名拒绝（完整清单见
  `opening.py` 的 `BLOCKED_SUFFIXES`）。这是拒绝清单：其余文件交给系统按类型选默认程序。校验与调用 `open` 之间仍有
  极短窗口，这是交给系统默认程序固有的边界。
- **进程**：子进程与插件同进程组（不 `start_new_session`），宿主回收插件时一并回收；超时 kill 并等待退出；
  非零退出返回 `COMMAND_FAILED` 与退出码。macOS 未设 locale 时为子进程补 `LANG=en_US.UTF-8`，否则 pbcopy 会丢掉中文。
