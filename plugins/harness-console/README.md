# harness-console

自有"界面型"插件：类似 Codex 桌面版的 agent 工作台，用网页或独立桌面窗口展示 my-agent 的运行状态。
它是[宿主只读 API](../../docs/design/PLUGIN_HOST_API.md)（包描述 v4，`"host_api": ["read"]`）的第一个样本，
目的是证明"界面型插件"这一类可行，界面只做到粗糙可用。当前已完成本地标准包和独立 MCP 进程组件验证
（见 `agent_py_agent/tests/test_harness_console_package.py`，宿主 API 为测试内假服务），
真实 Gateway、TUI 装卸与审批验收尚未完成。

## 开发构建

插件不依赖 SDK，也无其它第三方依赖。以下命令从仓库根执行，构建离线、不改用户插件安装表，输出已存在时明确失败：

```bash
python scripts/build_plugin_package.py \
  --project plugins/harness-console \
  --declaration harness_console/declaration.json \
  --output /tmp/plugin-build/harness-console.zip
```

声明里写了 `"host_api": ["read"]`，构建脚本会自动生成 v4 包描述。

## 使用

完成宿主安装和启用后，在普通对话里说：

- "请用 harness-console 打开工作台" —— 模型调用 `open`，启动（或复用）工作台网页并用默认浏览器打开；
- "用桌面窗口打开工作台" —— 模型调用 `desktop`，用 Chrome/Chromium 的独立应用窗口（无地址栏、专属 profile）打开；
- "关掉工作台" —— 模型调用 `stop`，停止服务并关闭 `desktop` 启动的窗口。

三个工具都是写类（会开端口或窗口），按宿主原审批。也可用显式命令 `/plugins@harness-console open|desktop|stop`，
但显式命令每次是一次性连接，命令结束服务随之停止；要持续查看请用普通中文让模型调用。

页面（单页，每 `poll_seconds` 秒轮询一次）：

- 顶栏：my-agent 工作台、Gateway 进程号与端口、宿主 API 状态；
- 左栏：最近线程（标题、状态、相对更新时间、压缩代次），点击切换；
- 主区：选中线程的运行状态（工作中/等待审批/空闲 + 当前活动 + 已进行时长）与上下文用量条
  （当前/窗口、红色触发线、消息/引导/工具目录三段、压缩次数）；
- 右栏：已安装插件（启用/停用）。

设置（唯一声明 `src/harness_console/declaration.json`）：`open_browser`（默认 true）、`chrome_path`（可选，
留空时自动探测）、`idle_stop_seconds`（默认 1800）、`poll_seconds`（默认 2，1–30）。

## 安全边界

- **宿主令牌只在服务端**：`MY_AGENT_HOST_API_URL` / `MY_AGENT_HOST_API_TOKEN` 只由插件进程读取并放在请求头里调宿主 API；
  浏览器只拿插件自己的会话令牌，页面、`/api/state` 响应、工具结果和日志都不含宿主令牌。
  宿主 API 403（插件停用、换代）时页面显示"插件已停用或令牌失效"并停止轮询，插件也不再请求；
  直连模式没有宿主 API 时显示"宿主 API 不可用"。
- **只读白名单**：插件服务端对宿主结果只挑线程/活动/上下文/插件/Gateway 的白名单字段返回浏览器，
  并做 1 秒缓存，多开窗口不放大宿主请求。插件没有任何写入或控制入口。
- **只绑回环 + 会话令牌**：网页只监听 `127.0.0.1` 随机端口；每次启动生成新令牌，首页需带令牌，首次访问后下发
  `HttpOnly; SameSite=Strict` cookie，`/api/state` 只认 cookie；只接受 GET，其余 405。
  完整带令牌链接只写入插件数据目录的 `last-link.txt`（0600），工具结果只给不含令牌的地址；页面加载后地址栏会去掉令牌。
- **无外部资源**：页面内联 CSS/JS，CSP 为 `default-src 'none'`，只放行内联脚本/样式与同源 `connect-src 'self'`；
  宿主数据一律用 `textContent` 渲染，不当作 HTML。
- **回收**：`stop`、空闲超时、插件进程退出（stdin EOF / SIGTERM）都会关闭服务；`stop` 与进程退出还会结束
  `desktop` 启动的窗口进程（插件进程的普通子进程，不另开会话）。窗口使用插件数据目录下的 `app-profile`，
  不碰日常浏览器配置。找不到浏览器时 `desktop` 回退默认浏览器并在结果里说明，两者都不行时返回 `window=unavailable`。
