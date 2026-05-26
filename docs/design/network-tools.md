# Network Tools Design

本页记录网络工具的长期形态，避免后续又把网页研究、API 调用和安全边界混在一起。

## 四个工具

`web_search` 只负责找候选来源。它返回标题、URL、摘要、provider 和失败的 provider 列表，不把搜索结果当最终事实。当前内置 DuckDuckGo HTML fallback，后续接 Exa、Parallel、Tavily、Firecrawl 时应走 provider registry，不能写成只认固定后端的封闭枚举。

`web_fetch` 负责打开一个确定 URL。它支持 `auto`、`markdown`、`text`、`html` 输出。HTML 默认转成 Markdown；二进制或文档类内容保存为 artifact，只把路径、hash、大小和 MIME 返回给模型。

`web_extract` 负责批量抽取多个 URL。它保存每页全文 artifact，并返回标题、预览、hash 和失败列表，适合研究任务留证据。

`http_request` 负责 API 请求。GET/HEAD 是只读语义；POST/PUT/PATCH/DELETE 是变更语义。变更请求会给出 `idempotency_key` advisory，但不靠流程硬门直接中断普通任务。

## 安全边界

网络安全参考 通道运行时、会话运行时、终端交互 的共同点：安全检查放在工具入口，不放在自然语言 prompt 里。

当前 `web_fetch`、`web_extract`、`fetch_url` 兼容入口和 `http_request` 都会经过 `network_safety`：

- 拒绝 `file://`。
- 拒绝 localhost、metadata host、link-local、私网、保留地址和明显的私网 DNS 解析。
- 允许通过结构化配置显式放行普通私网解析，适配代理、VPN、测试网。
- metadata/link-local 是底线，即使开启私网解析也不放行。

这不是交付质量门，也不是“必须先怎样”的流程门。网络 provider 失败、网页 404、搜索无结果都应该返回结构化错误，让模型换关键词、换来源或说明阻塞，而不是直接判整个任务失败。

## 能力分工

- 长期助手：学习多 provider 搜索、抽取、爬取分层。
- 工具运行时：学习 `format=text/markdown/html`、超时、大小限制、图片/二进制 attachment 思路。
- 终端交互：学习 domain permission、缓存、大内容持久化和 WebFetch 用户代理。
- 通道运行时：学习 SSRF、DNS、redirect、proxy 这些网络边界放到统一 fetch guard。
- 会话运行时：学习网络审批和网络策略的会话级缓存。
- 轻量运行时：学习 HTTP dispatcher/proxy 配置，不把代理细节塞进模型 prompt。

## 不做的事

- 不要求模型先写中间 JSON 才能联网。
- 不把搜索结果直接当事实。
- 不因为 provider 不在内置表里就拒绝；开放世界 provider 以后可以通过配置扩展。
- 不把网页阅读和 API 调用塞进一个含糊工具。
