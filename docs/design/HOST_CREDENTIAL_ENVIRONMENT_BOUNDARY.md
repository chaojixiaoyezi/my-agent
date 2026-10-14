# 宿主凭据的子进程环境边界

状态：2026-10-13 已实现，隔离 Linux 源码候选复验通过；整合 gate、提交和最终包复验由主线完成。

## 问题与权威来源

原 Shell 的环境过滤覆盖固定自管变量名和常见密钥命名。`SecretRef` 允许 `env:任意名称`，例如通道凭据引用不含密钥关键词的名称，普通 owner 的命令仍可继承该值。原受控命令和同步核验入口也会默认继承宿主环境。

修复只按结构化来源做决定：原 `AgentConfig.api_key_env`，schema metadata 声明的六个真实通道 SecretRef 字段，以及这些字段和 `api_key` 的准确来源记录。不能扫描值猜秘密，也不能依据命令、模型文本或测试名称决定权限。

`settings/secret_ref.py` 的 `managed_secret_environment_names` 是来源投影入口，不读取环境值或密钥文件。`freeze_managed_secret_environment` 在选模型、owner 克隆和运行覆盖前保存原来源上界。已有源不能因后来清掉引用或切换模型而消失；新来源只能扩大擦洗范围。

普通 `system_prompt`、自定义说明和其他配置内容即使以 `env:HOME` 开头，也不是凭据来源，不能改变环境过滤。原型的全字符串遍历已用正式反例证明过宽，现已删除；元数据只附在既有实际 resolver 消费字段，不维护第二份秘密名称目录。

内部载体是 `AgentConfig.host_credential_env_names`，类型为可 JSON 快照的 tuple，`repr=False`、`host_only=True`。它不是用户配置开关，原 `INTERNAL_RUNTIME_CONFIG_FIELDS` 拒绝 YAML/任务覆盖输入，参数中心跳过宿主专有字段。模型调用参数和用户模型档案不能覆盖该字段。没有新增凭据或来源持久账本。

## 单一擦洗与实际接线

所有下列启动都用 `tooling/_subprocess_env_scrub.py::scrub_subprocess_env`。宿主准确来源和原固定自管变量不能被普通 `passthrough` 覆盖；POSIX 按准确来源名称，Windows 按平台不区分大小写语义处理。函数返回环境副本，宿主 `os.environ` 不变。

| 入口 | 宿主来源传递 |
| --- | --- |
| Shell 前台、Windows 路径与后台 | Core → 原 ToolRegistryParams → ShellToolOptions → 原 spawn |
| PTY | 原 Shell options → SandboxHostPolicy → 本次 PTY 冻结输入 → 同一环境入口 |
| 子代理受控命令 | 原 registry handler → ControlledExecToolRequest → 原 shell gateway 执行；来源不进入决策审计或模型参数 |
| 能力包检查程序 | 原核验作用域 → PackVerifierRequest → AttemptSandboxSpec → SandboxProcessOptions → 原同步进程 |
| 插件离线准备 | 原 PluginManagementContext → PluginEnablePolicy → 原 PluginEnvironmentOperation → 准备环境 |
| MCP/插件进程 | 原宿主构建 DTO → 原 MCPServerConfig；重连沿同一配置，面板/事件仍共用原池 |

普通环境变量、owner 缓存、工作目录、Shell stdin、取消、超时、进程资源归属及原审批不变。权限视图重建和子孙任务的配置覆盖仍复用原配置优先级，dataclass 克隆保留内部来源上界。传来源仅用于剥离环境，不授予文件、网络或工具权限。

## MCP/native 显式授权的区别

MCP 原默认基线含 `PATH/HOME` 和 `XDG_*`。这些名称也可能成为准确 SecretRef 来源，不能仅靠“安全前缀”自动继承。默认基线先经统一擦洗，再叠加原受管 MCP 配置的显式 `env`。

显式 native `env` 是原管理员配置或插件授权通道，不是模型的透传参数；其原有行为保留。插件私有设置和按原 `host_api=[read]` 获得的短期受管令牌仍可交给该插件，不把普通命令的擦洗规则误当成撤销 native 能力。来源字段只由宿主创建期装配，raw MCP 映射中的同名字段无效。

## 验收与边界

- 合成 `env:任意名称` 通过真实 CLI SecretRef 解析；修前 Linux 原包的普通 owner Shell 能看到变量，修后候选看不到。
- 实际 Linux 原 bwrap/原进程链验证前台、后台、PTY 的普通和 Full Access 两档，受控、Attempt 同步与插件准备入口均看不到合成来源，普通变量保留、宿主环境不改。
- 真实 Root→MCP 握手验证默认 `XDG_*` 来源不可见，明确 native `env` 仍有效；没有调用真实 IM、供应商或线上服务。
- YAML、任务覆盖、用户模型档案、工具参数和参数目录不能注入或展示内部来源。切模清掉 `api_key_env`、子孙覆盖清掉引用之后，上界仍在。

环境擦洗保护新启动的子进程，不会追溯修改已运行进程。最终发行更新仍需走原排空与新客户端启动流程。

Full Access 原可信操作者的文件访问能力保持不变：本次没有宣称该档无法读取自身获准的配置文件。完整未知第三方环境值不做内容扫描；宿主自管保护依赖准确配置来源和原固定自管名。普通用户、审批与跨 owner 文件墙继续由原权限合同负责。

建议下一步：主线组合权限/审批、子孙、背景监控、MCP 和插件回归并运行严格 gate，再验证最终无源码包；独立 agent 可只读复核来源和所有 spawn，不新增兼容旁路或读取线上密钥。
