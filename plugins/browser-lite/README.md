# browser-lite

自有工具型插件：在插件专属的无头浏览器上下文里打开受控页面、读取元素、点击和填写测试表单。
插件本身只依赖标准库和 SDK `my-agent-plugin-api==0.2.0`，**不打包 Playwright 或任何浏览器**，
而是用标准库实现的最小 CDP（Chrome DevTools Protocol）客户端驱动本机已有的 Chrome/Chromium。
当前已完成本地标准包、独立 MCP 进程与真实浏览器的组件验证；宿主审批链与真实 TUI 验收尚未完成。

## 开发构建

先安装主项目开发依赖（`setuptools>=77` 是标准 wheel 构建后端）。构建离线、不下载依赖、不改用户插件安装表；
以下命令从仓库根执行，输出目录由开发者选择，输出已存在时明确失败：

```bash
python scripts/build_plugin_api.py --wheel-dir /tmp/plugin-build/wheels
python scripts/build_plugin_package.py \
  --project plugins/browser-lite \
  --declaration browser_lite/declaration.json \
  --wheel /tmp/plugin-build/wheels/my_agent_plugin_api-0.2.0-py3-none-any.whl \
  --output /tmp/plugin-build/browser-lite.zip
```

## 依赖准备

插件环境不需要额外 Python 包，但本机必须有 Chrome 或 Chromium：

- 设置 `chrome_path` 时只使用该路径（不存在就报不可用，不会回退到自动探测）。
- 未设置时按平台探测：
  - macOS：`/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`、`/Applications/Chromium.app/...`，
    以及 `~/Library/Caches/ms-playwright/chromium-*/chrome-mac*/*.app/Contents/MacOS/*`（按修订号取最新）；
  - Linux：`PATH` 里的 `google-chrome`、`google-chrome-stable`、`chromium`、`chromium-browser`，
    以及 `~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome`。
- 都找不到时每个工具都返回 `BROWSER_UNAVAILABLE`：
  “浏览器不可用：未找到 Chrome/Chromium（可在设置 chrome_path 指定）”，不会假装成功。

## 使用

一个插件进程内至多一个浏览器和一个页面，动作与同名工具一一对应：

```text
/plugins@browser-lite open --url form.html
/plugins@browser-lite read
/plugins@browser-lite fill --selector "#name" --value "张三"
/plugins@browser-lite fill --selector "#choice" --value "乙"
/plugins@browser-lite click --selector "#submit"
/plugins@browser-lite read --selector "#result"
/plugins@browser-lite close
```

| 工具 | 效果声明 | 说明 |
| --- | --- | --- |
| `open` | `mutating` | 启动/复用浏览器，导航并等待 load；返回最终 URL、标题、可见文字前 2000 字和浏览器 pid |
| `read` | `read_only` | 返回匹配元素（最多 50 个）的标签、文字、value、name/id、是否可见；下拉框带选项；不给 selector 时列出 input/textarea/select/button |
| `click` | `mutating` | 只点击唯一匹配元素（0 个或多个都报错），500ms 内出现导航就等 load，返回新 URL/标题 |
| `fill` | `mutating` | 只填唯一匹配的 input/textarea/select，触发 input/change 事件；下拉可按选项 value 或文字 |
| `close` | `mutating` | 关闭浏览器并删除本次 profile 下的缓存（保留 profile 目录本身） |

`--url` 不带协议时按当前工作区相对路径处理（如 `form.html`），也可写完整 `file://` 或 `http(s)://` 地址。
错误都是中文结构化结果（`code` + `message`）：`URL_NOT_ALLOWED`、`BROWSER_UNAVAILABLE`、`BROWSER_START_FAILED`、
`NO_PAGE`、`SELECTOR_NOT_FOUND`、`SELECTOR_AMBIGUOUS`、`INVALID_SELECTOR`、`NOT_FILLABLE`、`OPTION_NOT_FOUND`、
`TIMEOUT`、`PAGE_CRASHED`、`BROWSER_DISCONNECTED`、`NAVIGATION_FAILED` 等，不输出堆栈。

## 设置

- `chrome_path`：浏览器可执行文件，默认空（自动探测）。
- `allowed_hosts`：允许访问的 http(s) 主机名，默认 `["127.0.0.1", "localhost"]`，按主机名精确匹配、不含端口。
- `idle_close_seconds`：空闲多少秒后自动关闭浏览器，默认 120。
- `command_timeout_seconds`：每个 CDP 命令（含启动、页面加载）的超时，默认 15。

取值范围见唯一声明 `src/browser_lite/declaration.json`。

## 安全边界

- **专属上下文**：`--user-data-dir` 固定为插件数据目录（`MY_AGENT_PLUGIN_DATA_DIR`）下的 `profile/`，
  以 `--headless=new --remote-debugging-port=0 --no-first-run --no-default-browser-check` 等参数启动，
  绝不使用用户日常 Chrome 配置和登录态；`profile/` 是符号链接时拒绝启动。浏览器每次关闭（close、空闲、插件退出）
  都清空 profile 内容，所以 cookie 等状态不跨会话保留。
- **可访问地址**：只允许 (1) 本次宿主读取上下文允许读取的工作区内 `file://` 页面（经 SDK `check` 裁决，
  符号链接按真实目标判断）；(2) `allowed_hosts` 里的 http(s) 主机。其它协议一律拒绝。
- **请求拦截**：页面启用 CDP `Fetch` 拦截，页面自身、重定向、子资源和脚本发起的 http(s)/file 请求都按同一规则裁决，
  不允许的请求以 `BlockedByClient` 失败；导航或点击后跳到不允许地址时立即停到空白页并报 `URL_NOT_ALLOWED`。
  `data:`/`blob:` 等不出网的内嵌资源不拦；WebSocket 等不经 `Fetch` 的通道不在拦截范围内。
- **调试端口**：端口由 Chrome 随机分配并只监听 127.0.0.1，浏览器运行期间同机其它进程理论上可连接，
  请只打开测试页面，不要在其中输入真实凭据。
- **进程回收**：浏览器是插件进程的普通子进程（不另开会话），宿主回收插件进程组时一并结束；插件在 stdin EOF
  或 SIGTERM 时主动关闭浏览器；空闲超过 `idle_close_seconds` 自动关闭；页面崩溃或连接断开后整体关闭，下次 `open` 重启。
- 标准输出只承载 MCP 协议，浏览器的 stdout/stderr 全部丢弃。
