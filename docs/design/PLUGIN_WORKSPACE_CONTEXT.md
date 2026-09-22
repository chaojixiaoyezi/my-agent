# 插件逐次工作区读取上下文

状态：第 4 步运输已进入本地源码；首个样本和 SDK 构建仍待实现，未发布部署或完成实际 TUI 验收。

## 解决问题

隔离插件进程的 cwd 属于自己的环境，不能拿它解释用户的相对路径。安装配置也不能承载每个会话的工作目录。
普通模型调用与显式命令已有可信的 workspace、owner 路径策略及精确读取范围；本片将它们沿原
`RegistryToolInvokeRequest → ToolInvocationContext → PluginProxyTool → tools/call` 逐次传递。
不修改共享代理、进程 cwd、安装设置或业务参数，也不增加另一套执行器、授权记录或参数哈希。

## 只读协议

- 原握手 `capabilities.experimental` 下明确声明 `my-agent/workspace-read-context: {"versions": ["1"]}`。
- 沿已有 `enable_plugins`、明确安装及启用控制，不新增默认生效的外部服务或额外配置开关。
- 只有宿主管理的插件代理会消费此声明；普通外部 MCP 即使自述支持，也不因此收到宿主路径。
- 支持能力只读代理绑定的固定 transport，不能追随 client 当前新连接。未声明或未支持版本时沿旧调用协议。
- 支持时将同名扩展放进 `tools/call` 参数的 `_meta`，与 `arguments` 并列。缺少可信上下文则在发送前失败。
- 值包含 `version`、canonical `cwd`、`read_roots`、冻结的原路径策略，以及宿主明确授权墙外读取根和对应原策略。
  不携带 Agent、Store、回调、整份 runtime snapshot、密钥或安装私有设置。
- 版本 1 的读取上界限定当前 cwd 子树；原 `read_scope_mode=exact` 时再与 `allowed_read_roots` 取交集。
  空交集就是空数组，不能回退 cwd；普通模式的补充 roots 不扩大此插件读取上界。
- 每次实际读取和遍历子项都还须经过原 `PathAccessPolicy`。根目录通过不代表子项获准；墙外授权
  只可覆盖原 `PATH_OWNER_SCOPE_BLOCKED`，其它 owner、宿主控制面、危险路径等原拒绝保持。
- 路径策略冻结时保留宿主解析的数据根及危险根；插件端不能从自己的 HOME 或环境重新猜测豁免根。

这是一份受信插件应遵守的宿主上下文，不是操作系统文件沙箱。隔离 venv 负责依赖隔离，不能阻止恶意
Python 绕过协议直接访问系统；不把声明 `read_only` 或一份元数据当成沙箱证明。插件仍须经过原管理员安装、
执行审批、固定激活准入和精确撤销。首个样本只使用同一份公共纯路径裁决，不复制权限逻辑。

## 开发与验收

运输组件先验两工作区并发、不改业务参数、固定连接声明、缺失上下文、exact 空交集、
墙外授权不扩权及自定义数据根。样本另验符号链接、目录逐项裁决、有界读取和分页。
这些开发组件测试不计入真实 TUI；发布后仍要管理、插件业务、核心任务至少三路实际 TUI。

## 轻量 SDK 构建边界（待实现）

宿主保留唯一 `path_access_policy.py` 与 `workspace_read_context.py` 源文件。开发构建脚本将这两个文件
按固定清单原字节投影到临时 `src/my_agent_plugin_api/`，再调用标准 setuptools/pip wheel；不递归复制宿主，
不改写 import，不手写 wheel，不在仓库维护第二份实现。SDK 仅依赖标准库，不要求安装整个 my-agent。
构建后核对成员、源码字节和零宿主依赖；插件声明 SDK 精确版本并把该 wheel 一同放入原本地依赖闭包。
语义变更须更新 SDK 版本及样本依赖。构建发生在开发阶段，安装和启用只消费预构建 wheel。
两个发行布局仅通过 JSON 协议交互，不用跨命名空间的 Python 类型身份、pickle 或运行时源码复制。

定向核对了本地 Codex `core/src/mcp_tool_call.rs::augment_mcp_tool_request_meta_with_sandbox_state`：
按服务能力选择逐次请求元数据，不复制其配置或沙箱实现。MCP 字段位置依据官方
[生命周期](https://modelcontextprotocol.io/specification/2024-11-05/basic/lifecycle)与
[消息基础](https://modelcontextprotocol.io/specification/2024-11-05/basic)。未完整审阅参考仓库。
