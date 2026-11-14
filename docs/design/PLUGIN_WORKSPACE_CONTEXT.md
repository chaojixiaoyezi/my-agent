# 插件逐次工作区读取上下文

状态：第 4 步运输、轻量 SDK 和首个 workspace-peek 样本已有本地实现与组件验证；未发布部署或完成实际 TUI 验收。

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

## 轻量 SDK 构建边界（本地已实现）

宿主保留唯一 `path_access_policy.py`、`workspace_read_context.py` 与 `common/nofollow_fs.py` 源文件。
后者只提供原通用安全 I/O，不拥有权限；只读 fd 打开供分页使用，避免在样本复制逐段 no-follow 实现。
开发构建脚本将这三个文件
按固定清单原字节投影到临时 `src/my_agent_plugin_api/`，再调用标准 setuptools/pip wheel；不递归复制宿主，
不改写 import，不手写 wheel，不在仓库维护第二份实现。SDK 仅依赖标准库，不要求安装整个 my-agent。
构建后核对成员、源码字节和零宿主依赖；插件声明 SDK 精确版本并把该 wheel 一同放入原本地依赖闭包。
语义变更须更新 SDK 版本及样本依赖。构建发生在开发阶段，安装和启用只消费预构建 wheel。
两个发行布局仅通过 JSON 协议交互，不用跨命名空间的 Python 类型身份、pickle 或运行时源码复制。

定向核对了本地 Codex `core/src/mcp_tool_call.rs::augment_mcp_tool_request_meta_with_sandbox_state`：
按服务能力选择逐次请求元数据，不复制其配置或沙箱实现。MCP 字段位置依据官方
[生命周期](https://modelcontextprotocol.io/specification/2024-11-05/basic/lifecycle)与
[消息基础](https://modelcontextprotocol.io/specification/2024-11-05/basic)。未完整审阅参考仓库。

## 跨语言一致性用例（2026-09-25）

非 Python 插件不能直接用 Python SDK，需要各自移植读取检查。`plugins/sdk/conformance/workspace_read_check.json` 给出目录树、
上下文、逐条期望裁决和必须拒绝的畸形上下文，期望值由本文件描述的宿主实现裁决（pytest 同时校验参考实现）。
Node 样例移植全部通过，典型错误写法（先按字面折叠 `..`、直接用语言自带 realpath、按字符串前缀判断包含等）均能被用例抓出。
写入上下文暂无跨语言用例。详见[任意语言插件](PLUGIN_ANY_LANGUAGE.md#第-2-项实测非-python-插件怎么检查工作区路径)。

## 业务范围中的原始聚合库保护（SDK 0.3.0，2026-11-13）

受理时冻结了业务空间/项目的回合，通用文件读取、递归搜索和目录发现不能将本 owner 的正式 Memory、会话/索引/遥测派生账或业务上传/blob/catalog 原始聚合库当工作区输入。宿主沿原 typed 业务上下文或已核验前台空间快照冻结保护，后代沿原任务属性继承；模型填写 exact 路径或同名 attributes 不能授予原始库读取权。普通工程里的 `memory/`、用户 `data/uploads/` 和工作区文件不因此被拒。未选空间的本人维护仍走原权限；业务回合的默认 full 模式不能隐式解除保护。

`path_policy` 可携带 `business_aggregate_roots`，每个真实目标仍由唯一 PathAccessPolicy 核验，大小写别名按实际文件身份判定。SDK 0.3.0 与仓内 Node 实现消费同一收窄字段；核心 Shell/PTY 复用原 hidden paths 强制隐藏，未建立另一套沙箱。Memory/历史应使用 `memory_search`、`remember(action=list)`、`session_search`；业务正文使用 `read_business_asset` 按获准的精确 space/project/asset/version 读取。

SDK 0.2.0 的严格字段解析会拒绝带此字段的受限上下文，即使本次请求的文件本身不敏感，也不能静默删掉保护字段兼容。未携带该字段的原无空间调用保持原协议形状。旧已安装插件不自动升级；需要按原包构建、确认与启用链更新 SDK 依赖。此协议是合作式插件读取合同，不宣称能阻止恶意原生 MCP 进程绕过 SDK 直接调用操作系统文件 API。
