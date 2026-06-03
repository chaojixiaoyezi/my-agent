# clients

Future outbound service client layer for non-model external dependencies.

以后接 GitHub、Slack、数据库、浏览器、MCP server、远端 agent 时，协议调用代码放这里。
client 只负责鉴权、请求、超时、重试和错误映射，不负责业务判断。
