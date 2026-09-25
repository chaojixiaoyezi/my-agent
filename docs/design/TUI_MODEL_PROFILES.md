# /model：用户模型配置

## 解决问题与状态

用户无需改 YAML，即可在 TUI 新增和选择模型，同时设置接口、模型名、地址、密钥和上下文窗口。
菜单与运行配置已实现；R214 真实 TUI 已验证本地 Qwen 短对话和欢迎区随模型选择更新。
本地模型仅做基础验证，不追加大型任务或子代理任务。跨模型 child 的真实 TUI 验收仍未完成，
不能用配置定向测试代替；BUG-146/147 和原底座封板矩阵继续保留。

## 未配置状态

软件不预填模型、协议或 API 地址；dataclass 与发布 YAML 均留空。未配置仍可启动 Gateway、
打开 TUI、查看历史及使用 `/model`，生成或能力探针返回 `MODEL_NOT_CONFIGURED`，不发网络请求，
也不回退到离线模拟或其他模型。`echo` 仅在显式配置时用于离线开发。

模型菜单只列真实保存或显式部署的连接，空部署项不显示。用户保存的新会话偏好、会话独立选择、
子代理继承和显式部署配置保留；这里取消的是软件自带的默认值，不是用户自己的选择。
`gateway_status` 不再返回启动模型；`caller_model` 只表示本次执行快照的请求模型，不能鉴定厂商内部路由。
后台记忆策展同样遵守显式选择：选定配置损坏时保留未配置状态与诊断，不回退到另一个启动模型。

## 约定

- 菜单包含选择、新增、服务商管理、登录认证与退出；模型接口为 Chat、Messages 或 Responses，
  Auth 支持订阅和通用 OAuth 设备码登录，见 [账号登录](MODEL_OAUTH.md)。表单有保存、返回，退出不保存草稿。
- 模型记录按 owner 隔离，存于宿主 config/model-profiles/<owner 身份摘要>.json；这是唯一配置源，
  不放入业务 workspace、聊天记录、模型 prompt 或日志。目录 0700，配置原子落盘为 0600。
- 已有显式部署配置仍可选择，不复制密钥给客户端。新增密钥输入掩码显示，读取列表不返回密钥。
- 保存只新增，不覆盖同名记录；使用稳定配置 ID 区分同名、不同接口/地址的模型。选择另行确认。
- “选择已有模型”只改变当前 canonical 会话；同一用户其他 TUI/IM 会话不受影响。
  “新会话默认模型”只初始化未来新会话，正在执行的工作片冻结原配置；当前 child 不被菜单中断。
  会话固定编号及恢复语义见 [会话模型选择](SESSION_MODEL_SELECTION.md)。
- 管理员可逐个显式发布或撤销共享模型；私有配置不会自动共享。共享目录只保存引用，
  普通用户调用由 Gateway 解析管理员原私有配置，不复制或返回密钥。见 [共享模型目录](SHARED_MODEL_CATALOG.md)。
- 上下文窗口是用户显式配置的总容量（tokens），不是输出长度；用于真实压力/Compact 计算。
  切模型保留 canonical 历史，但不同供应商的缓存不保证复用，较小窗口按原 Compact 流程处理。
- 打开、保存、选择菜单不向 provider 发探针或 LLM 请求；格式校验不冒充连接成功。
  Gateway 只解析可信owner并访问canonical配置，不构建冷owner Agent/工具/Memory服务。
- 选中快照须继承到HTTP保护线程；线程内外的config/backend/prompts一致，不能只切换父线程展示。
  真实切换验收同时核对provider侧请求；日志里的调用前标签与TUI窗口不能单独作为模型证据。
- 模型名、后端、地址、密钥、窗口与输出上限采用同一显式选择优先级；旧 task/run 配置不能拆开覆盖。
  无关运行配置叠加必须保留宿主 profile ID，孙代理创建/重启继续引用同一配置，不能默默恢复部署默认。

## 代理自助管理（`manage_models` 工具）

- 用户不必自己进 `/model` 填表：主会话代理可用 `manage_models` 工具执行同一套结构化操作——`list`、`add`（服务商+模型一步保存）、
  `save_provider`/`save_model`（新建或 `editing=true` 编辑）、`select`（只改当前 canonical 会话）、`set_default`（只改新会话默认）、
  `delete_model`/`delete_provider`、`probe`、`discover`。唯一写入口仍是 `execute_model_profile_operation`，菜单与工具共用同一份
  owner 目录、同一把文件锁和同一套校验；新建时 profile_id 由工具生成 UUID，模型不自造编号。
- 权限按结构化 effect 裁决：`list/probe/discover` 只读；`delete_provider` 为 dangerous，经统一危险动作审批门（密钥不可恢复）；
  其余为 mutating，由当前 owner 的审批模式决定是否逐次确认。不解析用户自然语言判定授权。
- 会话身份只读当前回合的结构化 `conversation_thread_id`；没有会话的上下文调用 `select` 返回 `MODEL_PROFILE_NO_THREAD`，
  要求改用 `set_default`。子代理运行里该工具不可用，子代理仍只能继承或按 `create_subagents.model` 指定模型。
- 密钥边界：用户把密钥告诉代理时，它会经过一次模型上下文和工具参数；工具回执只含 `has_key`，工具账本归档按字段名脱敏 `api_key`，
  错误文案不回显参数值。校验失败（`MODEL_PROFILE_INVALID`）都发生在落盘前，标记 `not_started`；探针失败是服务商侧事实，
  以 `MODEL_PROBE_FAILED`/`MODEL_DISCOVER_FAILED` 返回，不冒充配置错误。
- 开关：主配置 `enable_model_profile_tool`（默认开启）；关闭后不注册该工具，仍可用 `/model` 手动维护。
  回归：`test_model_profile_tool.py`。

## 子代理单独指定模型

- `create_subagents.model` 或单项 `items[].model` 可填写本 owner 已新增模型的精确名称或配置编号；
  单项选择优先于批次选择，省略时继承直接父级创建时的模型配置。
- 用户可正常说“你继续用模型 A，把这部分交给用模型 B 的子代理”。由模型填写结构化工具参数，
  运行时不扫描用户正文来猜模型，也不把这句话变成自动派工命令。
- 子代理引用独立配置，不改变主代理已选模型；孙代理默认继承该 child，恢复继续读取原配置编号。
  不存在、重名未消歧或跨 owner 的配置在整批创建前报错，不静默改用模型 A。
- 端点和密钥先在 `/model` 私密保存，不通过派工参数、聊天或公开回执传递。

## 采样参数

- `model_temperature_explicit` 在 Chat、Messages、Responses 中含义一致，默认 `false`：
  未明确启用时不发送部署中的 `temperature`，由提供方选择默认采样，避免兼容模型被隐式压到 0.2。
  手写部署配置须同时设置温度与此开关；`/model` 填写单模型温度会自动启用，填 0 也算显式值。
  显式摘要请求自己的温度覆盖仍保留；不删除用户保存的温度，不改变已冻结工作片。
  这是修复跨接口配置语义不一致，不承诺采样调整能消除模型退化、循环或任务质量问题。

- 模型编辑可填写 `model_queue_wait_seconds`（额外排队预算，0 至 86400 秒），留空继承部署值，默认 0。
  只加到动态首事件预算，适合单槽推理服务器排队；不延长流静默或取消响应，不是承诺上游一定成功。
  该配置不能修复 schema 编译错误或输出长度耗尽；不得因此无界重试或替换模型。

- `top_p` 是可选核采样概率，YAML 默认 `null`；`/model` 每模型可填写 0 至 1 的有限数值。
  模型表单留空表示继承部署值；部署同样留空时由适配器决定是否发送。布尔/NaN/无穷大/越界值无效。
- 普通 Chat、Responses、Anthropic 未配置时不新增 top_p；显式填写时透传，供应商支持性由其协议决定。
- 仅 HTTPS 默认端口的 `api.deepseek.com`（空路径或 `/v1`）、`工具运行时.ai`（`/zen/v1` 或 `/zen/go/v1`）
  且精确 V4 Flash/0731 型号采用 0.95 默认。不通过子域、端口、任意私有路径或模型名含 deepseek 猜方言。
- 已知 Flash 思考模式遵守 0.95 下限；显式更小值保留在配置、出站按下限提升，显式非思考请求使用 1。
  已知 Flash 的非显式部署温度不发送，显式温度保留；其思考模式上游可能按协议忽略温度。
- top_p 与模型/连接整组进入工作片快照及 backend 缓存键；编辑不会热改进行中工作片，后续工作片读取新值。
  main/child/Compact 共用后端，不修改会话、重试或历史。该改动不证明旧 400 或复读已修复。
- 对照在线最新 工具运行时 transform.ts
  的 `topP/temperature`，以及 [DeepSeek 思考参数](https://api-docs.deepseek.com/guides/thinking_mode/)。
  本地 工具运行时 检出较旧，未用它代替在线最新版的采样规则；公开源码也不能证明线上部署具体路由版本。

## 对照

- 终端交互 src/commands/model/model.tsx：菜单选择与取消独立，配置命令不成为模型聊天请求。
- 会话运行时 会话运行时-rs/core/src/config/edit.rs：结构化模型配置编辑、明确选择、原子写入。
- 会话运行时 会话运行时-rs/config/src/config_layer_source.rs 与 core/src/config/config_loader_tests.rs：按真实配置层保留来源和优先级；
  本项目在既有来源记录保留宿主 profile ID，不另建一份模型身份。
- 本项目复用 owner scope、现有两种后端、前台/后台工作片和子代理配置继承，不另造模型执行链。

## 验证要求

定向验证无效 URL/窗口、密钥脱敏、原子写入、跨 owner、两窗口并发选择、作用域清理及 child 继承。
随后在单 Gateway 的真实 TUI 中走新增/取消/Auth/选择/重开，再用 MiniMax-M2.7 执行普通任务，
检查模型账本与真实 context，不能仅凭菜单显示“保存成功”宣布通过。
