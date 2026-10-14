当前实施导航：已采用固定官方v1.0.98公开Go扩展构建随包后端，沿原feishu_cloud/Delivery；本人授权工具渐进发现。用户身份的官方预查询与单次业务请求都使用同一宿主UAT，并核对host actor；应用身份不加载本人token。具体配置及验收边界见 [实施合同](IM_UNATTENDED_IMPLEMENTATION.md)，下面保留原可行性评估。

# 普通用户与群助手的飞书 CLI 接入评估

状态：2026-10-14，可行性与隔离原生运行已核查；产品接线未实施。用户要求本轮只处理 CLI，先前安全专项已暂停。本评估不改变已发行 `.20261014.2`，不操作真实飞书账号、群消息或默认 Gateway。

后续群子代理审批反馈与一个管理员/几十万员工的运行目标见[无人值守综合评估](IM_UNATTENDED_EXECUTION_REVIEW.md)。CLI 是飞书能力后端，不能代替原停止、审批、输入等待资源和内网凭据合同。

## 当前结论

官方 `larksuite/cli` 可作为 my-agent 的飞书能力后端，普通私聊用户和群助手都是接入目标。不能把管理员在服务器登录过的裸 CLI 当成所有用户已经可用的接口。

当前源码和 `.20261014.2` 包没有 `lark-cli` 接线；1.19 测试机没有 Node/npm 或预装 CLI。本轮把官方 v1.0.98 原生 Linux amd64 二进制放在专用评估目录，版本、帮助、消息和表格命令可运行，不依赖 Node。尚未证明 my-agent 普通/群 owner 的真实工具执行、授权和飞书读写完成。

## 身份与使用方式

| 场景 | 接入方式 | 已有机制与缺口 |
| --- | --- | --- |
| 普通私聊使用已共享文档、表格、wiki | 沿当前 owner 的云资源许可，通过宿主应用身份执行已许可操作 | 复用 `feishu_cloud`、本人协作者/所有者校验与表格保护范围；CLI 后端待接 |
| 普通用户要求以本人身份访问个人资源 | 本人 OAuth 用户授权 | 当前代码没有飞书普通用户 OAuth 登录及刷新接线，不能借管理员登录代替 |
| 群助手操作群共享资料或发回本群 | 机器人应用身份，固定当前群与该次可信发起人 | 复用群 owner、`channel_conversation_id`、原资源许可及投递；群不是个人 OAuth 身份 |
| 群中涉及某成员的个人资源 | 使用准确发起人的本人授权，明确结果目的地 | 不能把群任意成员或管理员的令牌当群共享授权 |

用户通过 IM 自然对话使用能力；无需每个用户到服务器安装或手工运行 CLI。应用所需飞书 scopes 和资源分享仍需实际具备；CLI 不会额外授予文档或个人权限。

## 官方接口核查

核查固定发行 tag `v1.0.98`，对应 `ac81f5056ab88f37d581d25916f3dab85abfca76`，不是只参考 main 新接口。许可为 MIT。

- [`extension/credential/types.go`](https://github.com/larksuite/cli/blob/v1.0.98/extension/credential/types.go)：`Provider.ResolveAccount` 与 `ResolveToken` 可由宿主提供应用或用户令牌；`TokenSpec` 只有 Type/AppID，没有 my-agent owner/actor。调用上下文的可信映射必须由接入方提供。
- [`extension/platform`](https://github.com/larksuite/cli/tree/v1.0.98/extension/platform)：复用 Allow/Identities/MaxRisk/FailClosed 和 Wrap，不单靠移除 Skill 隐藏命令。现有 ToolExecutor 的批准、超时、取消与操作账保持权威。
- 默认配置支持 `LARKSUITE_CLI_CONFIG_DIR`；profile 主要选应用。默认配置读取首个 Users 账号，不能据此认定当前 IM 用户身份。
- [`main_authsidecar.go`](https://github.com/larksuite/cli/blob/v1.0.98/main_authsidecar.go) 的代理能力需要 `authsidecar` 构建标签；本轮官方下载二进制实测拒绝代理模式，不能当成已支持多用户代理。
- 模型可在沙箱直接调用且令牌留宿主的方式，需要编译带官方 Credential/Platform 扩展的 Go wrapper 或代理构建。若只由宿主调用固定 CLI 操作，可提供当前调用的应用 token 而不向用户进程开放裸命令，但仍须核对 CLI 的运输、重试和输出合同；本轮不直接替换原写请求。Go 是扩展构建依赖；原生可执行文件不要求用户运行 Node/npm 或 Go。

## 最小实现方向（待实施）

先保留原 `feishu_cloud` 的模型入口及文档/表格/wiki 授权和保护范围校验，把已经许可的准确操作交给宿主 CLI 后端。复用适配器现有应用 token 获取，不建立第二份 app_secret 配置；本人 OAuth 的新增生命周期单独接原 owner 身份，未授权返回明确状态。

宿主每次冻结 owner、当前 actor、会话、所选身份、资源与操作；CLI 后端从该次可信上下文取得凭据和命令范围。CLI 配置/缓存按 owner 处理，安装版本由原发行机制管理。模型不自行选择服务器默认 profile，不在用户工作目录保管共享应用登录。

CLI 输出进入原结构化工具结果、归档、分页/大输出和操作账；消息与文件投递沿原 Delivery，不能绕过已有接收目标绑定。先覆盖现有六项云操作，再逐项扩所需能力，避免同时维持两个相近的云文档工具和授权来源。

运输合同已核：v1.0.98 自带 RetryTransport 默认 MaxRetries=0；其 SDK v3.7.2 只对 DialFailedError 再尝试，不把类存在误判为默认重放未知写入。但 stock HTTP client 允许同源重定向，307/308 可保持写方法与正文，且 raw api 没有禁用重定向的参数，不满足原 `request_cloud_api` 认证请求一律拒绝重定向的合同。正式接入应编译宿主 wrapper 固定拒绝重定向、保留原未知写入判据；不能为省接线直接用 stock 替换写后端。

CLI 成功是 `{ok:true,identity,data}`，供应商 code/msg 已被 CLI 投影；失败是 stderr 结构化 error 与非零退出码，不能按成功退出码或错误文案推测某次追加已执行。实际后端必须对齐原工具的数值错误与副作用结果后才开放给普通/群用户。

stock 的 `checkAPIError` 对缺失或不可转数字的供应商 code 也可返回 nil，后续 2xx 可被包装为 ok:true；当前宿主要求准确整数 code=0。正式 wrapper 必须保留并核验原始供应商结果，不能把 CLI ok:true 伪造回 code=0。此项为源码接口差异，不是真实租户故障或已完成产品修复。

## 实测记录

- 测试机：`192.168.1.19`，Ubuntu 24.04 x86_64；专用目录 `/home/mdzhu/my-agent-cli-evaluation-20261014`，未改生产安装或 PATH。
- 官方归档 `lark-cli-1.0.98-linux-amd64.tar.gz`，SHA256 `4a12dcb7a0aab25986d8bcd046cb45b08278531a9aac3a12b53cf3d94738fe80`，与官方 checksums 一致；仅提取准确普通文件 `lark-cli`。
- 原生版本为 `lark-cli version 1.0.98`；根帮助、schema 帮助、IM 发送帮助、表格帮助成功。空独立配置的 `auth status` 返回结构化 `config/not_configured`，没有登录或真实 API 验收。
- 以合成 app ID/token、独立空目录运行原生 `api --as bot --data - --dry-run` 和 `api --as user --dry-run`，分别成功产生文档写计划与文件读计划；没有执行这些计划，也不等于 my-agent 普通/群工具链已接好。单次为 0.083/0.081 秒。
- 准备失误保留：`docx.document.get`、`im.message.create`、`im.messages.create` 不在该版 schema 目录，返回准确参数错误，未计为通过。空配置 `calendar +agenda --as user --dry-run` 探针超时，停止专用评估 PID；未据此证明有登录的实际日程失败。
- 原二进制设置 `LARKSUITE_CLI_AUTH_PROXY` 后明确返回未编译 `authsidecar`，退出 2。全量已保存命令输出位于评估目录 `native-offline-results.json`；无真实 app_id、secret 或 OAuth token。

## 建议下一步

先完成现有文档/表格/wiki 操作的 CLI 后端与普通、群工具链验收，再扩本人 OAuth；可并行核对 CLI 操作映射，接线与发布由同一负责人收口。必须保留当前 owner/发起人/群目标和原授权来源，真实租户验收需本人授权与明确测试资源，本报告不冒充已接入。
