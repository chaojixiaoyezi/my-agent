# web-board

自有"网页界面型"工具插件：在插件进程内用标准库 `http.server` 起一个只读网页，用浏览器浏览指定工作区目录——
目录列表（名称、大小、类型，可点进子目录/文件）、文本/Markdown 预览、HTML 沙箱预览、图片预览。
当前已完成本地标准包和独立 MCP 进程组件验证（见 `agent_py_agent/tests/test_web_board_package.py`），
真实 TUI 装卸与审批验收尚未完成。

## 开发构建

插件只依赖精确版本 SDK `my-agent-plugin-api==0.2.0`，无其它第三方依赖。以下命令从仓库根执行，
构建离线、不下载依赖、不改用户插件安装表，输出已存在时明确失败：

```bash
python scripts/build_plugin_api.py --wheel-dir /tmp/plugin-build/wheels
python scripts/build_plugin_package.py \
  --project plugins/web-board \
  --declaration web_board/declaration.json \
  --wheel /tmp/plugin-build/wheels/my_agent_plugin_api-0.2.0-py3-none-any.whl \
  --output /tmp/plugin-build/web-board.zip
```

## 使用

完成宿主安装和启用后：

```text
/plugins@web-board serve --path docs      # 启动，返回 http://127.0.0.1:<端口>/?token=<令牌>
/plugins@web-board status                 # 是否在服务、地址（含令牌）、目录、已处理请求数
/plugins@web-board stop                   # 停止并释放端口
```

中文示例：普通对话里说"用 web-board 把 site 目录开成网页给我看"，模型会调用 `serve`（写类工具，按宿主原审批），
把返回的地址交给你，在本机浏览器打开即可。

- 页面：`/` 目录列表（`/?p=子目录`），`/view?p=相对路径` 预览，`/raw?p=相对路径` 原始文件。
- 文本、Markdown 及未知扩展名按 UTF-8 转义后放在 `<pre>` 里；`.html/.htm` 放进 `sandbox=""` 的 iframe（srcdoc）；
  图片用 `<img>` 走 `/raw`；含空字节的文件只提示二进制。
- 同一时间只有一个服务：再次 `serve` 先停旧服务（新目录验证失败时旧服务保持不变）。
- 设置：`max_preview_bytes`（预览最多读取字节，默认 256 KiB）、`idle_stop_seconds`（无请求多久自动停止，默认 600 秒）；
  范围见唯一声明 `src/web_board/declaration.json`。

## 安全边界

- **只绑回环**：只监听 `127.0.0.1`，端口由系统随机分配；不监听局域网或公网地址，不访问外网。
- **令牌**：每次 `serve` 生成新的随机令牌；每个请求都要带正确令牌（查询参数 `token`，或首次带令牌访问后下发的
  `HttpOnly; SameSite=Strict` cookie），否则 403。响应带 `Referrer-Policy: no-referrer`、`Cache-Control: no-store`，
  且不写访问日志，避免带令牌的地址外泄。令牌会出现在工具返回里（即进入模型上下文和会话记录），不要把地址转发给他人。
- **只读**：只接受 GET/HEAD，写方法返回 405；插件从不写工作区。页面 CSP 禁止脚本和外部资源，
  `/raw` 附加 `CSP sandbox` 与 `nosniff`，直接打开 HTML/SVG 原文件也不会执行脚本。
- **只限指定目录**：`serve` 时冻结本次调用的宿主读取上下文；之后每个请求的路径必须是该目录下的相对路径（拒绝 `..`、
  绝对路径），先经冻结上下文检查，再用 SDK no-follow 从文件系统根逐段打开——路径上任何符号链接（无论指向内外）、
  多链接文件和非普通对象都被拒绝。之后宿主权限变化不会扩大已开服务的范围；要按新权限浏览，重新 `serve`。
- **回收**：`stop`、空闲超时、再次 `serve`、插件进程退出（stdin EOF / SIGTERM）都会关闭服务并释放端口；
  服务线程均为 daemon，不会拖住进程退出。
