# IM 与多用户安全复核（2026-10-13）

2026-10-14 发行验收追加：`.20261014.1` 源码 `83b95547`，相对 `f9abc5d4` 仅改耐久重试用例与文档，生产代码不变。
包内原生 803 项为 795 通过、0 失败/错误、8 跳过（7 macOS、1 既有心跳模拟限制）；自然退出 0。
新包独立清单/递归字节码/源码归档、鉴权攻击、原实例更新与合成数据保留、TUI 正常退出及官方 MiniMax-M2.7 验收通过。
静默监控为真实模型、合成飞书运输，done/suppressed、零外发，不代表真实租户链路。
后续文档提交不改变包源码身份。准确 SHA 见 DESIGN_LEDGER 与同包验证清单；原 `.9` 失败证据保留。
新用户提出的指定告警机器人无 @订阅已核实平台权限可行，仍待底座实现，不包含在此修复包。

状态：确认问题的本地修复和最终源码门已完成。348文件联合6103项中6092通过、11项明确跳过；各线成绩不相加。包内验证与发布身份由同包发行清单记录，本报告不替代该证据。

## 范围与事实边界

用户授权在本期修复完成后追加普通用户、管理员和群助手的越权/安全复核，再提交 main、打包。所有主动探针使用临时 owner、随机端口、合成 token/marker 和自有测试机目录；不发送真实飞书消息、不修改生产监控、不下载或记录真实凭据。

复核入口包含真实 Registry/ToolExecutor、HTTP Gateway、原 ChannelManager 入站和投递、原 ConversationStore/SessionTask/SchedulerRepository、Shell/PTY/子进程及权限合同。实际飞书租户协作者、事件订阅、应用权限、SDK 到客户端显示等需要租户证据，本轮替身运输不能证明这些已通过。

## 已有证据的问题及原链修复

| 问题 | 原始复现 | 权威修复 |
| --- | --- | --- |
| Gateway 生命周期漏接鉴权 | 真实 Agent auth=true、直接 server 缺 middleware 时，无 token 请求入队；正常 CLI 配置入口拒绝同请求；auth=false 与多 owner/已声明 IM 组合也开放 | 已修复；原配置到中间件的唯一 factory 下沉复用，服务初始化自动接线，CLI 在恢复前检查；有 IM 声明必须启用认证及 owner 隔离，禁止普通身份落入 local/main |
| 无凭据的回环可被当成管理员 | 旧迁移配置 false；测试机新 exec 的端口注册为空，但能连接回环 | 完成原 G2b 退场：身份只认 G1/部署 token，路由原 credential/admin 档在处理请求正文前守门；无 token、未知来源不赋管理员 |
| SecretRef 的任意环境变量进入普通命令 | 真实 Shell/沙箱使用不含 KEY/TOKEN 字样的合成 env 引用仍收到 marker | 从宿主原配置来源冻结准确 env 名上界，贯穿 owner clone、Shell/PTY/后台及受控执行；passthrough 不能恢复宿主名 |
| 认证 HTTP 跳转泄露请求认证 | 原 Feishu HTTP 方法收到 302 后第二 origin 收到合成 bearer | stdlib 共用不可重定向 opener，沿原供应商与回环认证请求入口接线；保留原数值 HTTP 错误和超时合同 |
| 上传附件校验后变更路径泄露外 owner 字节 | 原 SendMessageTool 校验后，正文发送回调把原文件换成外 owner symlink，真实 multipart 收到外部 marker | 发送任何外部内容前按可信 owner 根、原 artifact size/hash 做有界 no-follow 读；同一不可变 bytes 上传，不再打开原 Path |
| multipart 文件名能插入头行 | 合成 CR/LF 文件名进入原 multipart 头 | 通用 multipart 头值转义；正常文件名/文件类型仍走原协议 |
| API 错误和适配器异常日志包含秘密 | 合成供应商错误 msg/data、factory/watcher 异常包含 marker | 日志只保存异常类型、数值状态和静态错误码，不记录原 body/msg/异常 str |
| 错落的会话目标记录被本 owner 接受 | 原 Registry/Executor 在本账本中遇到声明外 owner/home 的目标仍排队 | send_session_message/create_session_task 复用同一 owner/home 判据，冲突拒绝；准确 canonical 旧 local 记录保留 |
| 损坏的用户网络策略恢复成放行 | 原 permissions.network=false 时普通云工具零 HTTP；同一临时策略损坏 JSON 后重建 Agent，network=true 且实际发出合成请求 | 已修复；沿原策略 load_error 与有效权限关闭网络，坏文件不重写；每次原工具调用核对当前策略并与主/子/孙冻结上界取交集，原合法 bootstrap 保留 |
| 普通监控工具读取宿主秘密 | 真实普通 owner 原 Registry/Executor 的 HTTP SecretRef env/file 两条把合成宿主 marker 发给自选 toy HTTP；正常 file tool 已拒绝同一外 owner 路径 | 已修复；所有请求共用原 fetch 出口，冻结真实 ToolInvocationContext；禁止 env 引用，file 经 owner 原读权限与有界 no-follow，私人 credential_ref 沿原 TaskCredential 当前执行授权 |
| 外发越界原因丢失 | 普通 owner 请求另一个渠道被拒，但原码未登记而变成 UNKNOWN_ERROR | 登记原 CHANNEL_TARGET_OUT_OF_SCOPE，保留拒绝与零副作用，可读错误不弱化边界 |

错落会话记录用例是存储完整性故障注入，未证明普通 IM 用户能写该宿主记录。进程端口注册缺失探针是合成新 exec，不冒充生产子代理利用链。真实租户是否曾发生跨 owner 投递仍需准确投递记录，不能靠模型转述确认。

## 复用及单一权威

- 身份和路由继续沿原认证通道、host actor、owner/thread/root/task/attempt；不信模型正文、自报角色、raw target 或任意附件 dict。
- G1 文件仍在原宿主 secrets，缺失才生成、重启复用；坏权限/内容/根拒绝并保留原件。客户端失败零 HTTP，不降级无凭据请求。
- 旧 gateway_require_local_credential 不再是配置字段或设置命令；存量 YAML 的旧键不能重开宽松档。更新器原退出/停服/保留数据/恢复服务流程不变。
- host_credential_env_names 为宿主内存来源事实，不进入公开参数、用户 YAML/overlay、工具 schema 或新的秘密账本。它不包含秘密值。
- VerifiedAttachmentContent 仅限一次原 DeliveryAttempt 的内存快照；原 artifact/投递记录仍是唯一持久账。冷重试重新安全读原 refs；缺少原 owner/hash/size 的旧记录明确失败，不自动证明旧字节。
- 群附件引用只核准确 parent 和同群/话题，不复制父发送者权限、不猜最新文件；原 owner 下载与文件读取权限继续生效。
- 主代理、孩子、孙代理、自动委派和计划执行使用同一 owner 权限上界。审批只批准原请求，不授予其它 owner 或管理员资源。

上述已确认入口已在本地修复，独立复核及同源源码严格 gate 已通过；新包仍必须通过原生验收才允许发布。原始证据 /tmp/SOURCE_HTTP_OWNER_SECURITY_PROBE.json 只保存布尔、请求计数与拒绝码；各片成绩不代替发行验收。

## 模块交接

[环境来源与所有 spawn](HOST_CREDENTIAL_ENVIRONMENT_BOUNDARY.md)、[当前网络策略](OWNER_NETWORK_POLICY_INTEGRITY.md)、[监控来源私人凭据](WATCH_SOURCE_SECRET_AUTHORITY.md)、[通道与附件](FEISHU_OUTBOUND_SECURITY.md)、[Session/计划独审](../tasks/IM_TASK_SCHEDULER_SECURITY_HANDOFF.md) 分别保存准确入口与证据。公开资料引用文件入口见 [群引用附件](FEISHU_QUOTED_MEDIA.md)。

## 验收与已知边界

验证分为合同单测、fake tool/LLM、原入口 replay，再到自有 Linux 安装包/真实官方模型；不替被测代理补产物。首轮完整348文件的8个失败保留，按原身份和生命周期合同迁移旧夹具；完整重验使用不重叠文件清单、独立临时根的3个pytest进程，分别要求自然退出0。最终发布 focused 联合需包含旧真实业务正例及新增拒绝例，静态 strict gate 全通过才推 main。

覆盖：首设/锁定/解锁与告警独立、普通/admin/full 权限、群 mention 与代码块、文件归属及交叉读、发送目标和不可变附件、审批/子孙上下文、计划 actor/控制/历史/恢复与静默投递、认证跳转/日志/子进程秘密、原云资源授权和命令解析。原真实的调用目的/provenance 门继续保留，旧替身只能迁移到原后端 seam，不能绕门。

明确边界：

- auth_enabled=false 只允许显式关闭 owner 隔离且没有 IM 凭据声明的单机部署；IM 部署必须启用认证及 owner 隔离，不安全组合在任何恢复或监听前拒绝。真实 Agent 开启认证但调用方缺 middleware 时补接唯一原 factory。
- 管理员 full-access 是可信操作员，能读宿主文件；不能声称 full-access 下宿主文件凭据对模型保密。准确来源 env scrub 仍生效，原端口拒绝只是纵深措施，不取代 G1。
- Linux 旧内核无法使用 Landlock 网络规则时，必须按原结构化事实展示不可用。Ubuntu 22.04 未完成当前原生验证；本次测试机为 Ubuntu 24.04。
- 已授权插件的显式凭据注入由原插件合同管理；审计不能把合法声明的插件环境全删掉。
- 不保证消除模型生成无关文字。后台是否对外通知由结构化 reply_policy/intent/public_reply 和真实调用来源控制；旧 always 策略不静默迁移，不能靠正文关键词当安全裁决。
- 飞书获取引用消息与下载资源需要应用在群内及实际 message/resource 权限；用户引用文件并 @机器人，不能替租户管理员授予飞书 scope。
- Webhook 原本机 Handler 合成测试：消息/卡片的空验证配置、错误 token、错误签名全部 403、零 callback；正确 token/签名只一次对应 callback。缺 WS SDK 明确失败，没有 WS→webhook 降级。这不代表真实租户订阅已验。
- 网络撤权在下一次工具入口及新进程生效，不声称即时撤销已经启动的长进程或 PTY；旧 false 权限上界不因修好文件扩大。
- 旧 raw env 秘密来源会被明确拒绝；缺当前原执行证明的 detached/cold 秘密来源也拒绝。需要在原任务中通过私人卡重新绑定，不自动改监控、拷贝管理员密钥或借最新前台身份恢复。

建议下一步：冻结全部修复后完成同一源码 gate、包内原生测试和更新演练，再 fast-forward 推 main；可由独立 agent 并行只读复核，不能并行修改生产监控或秘密配置。
