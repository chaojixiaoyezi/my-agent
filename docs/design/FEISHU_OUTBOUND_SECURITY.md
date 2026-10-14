# IM 附件与认证运输安全合同

状态：已实现，作者 30 文件 560 项及非作者 10 文件 199 项通过；最终发行联合 gate 由 root 收口。这里只描述通用底座，不根据正文关键词授予权限。

## 权威与内容载体

- 收件权威仍是原 `DeliveryContext`：普通私聊/群由当前 owner 解析，本地管理员由原绑定表解析。附件、父消息发送者和模型参数不能指定新收件人。
- `ChannelAttachment` 只承运原产物 ID、路径、版本 hash/size 和宿主生成的 owner 根。owner 根由当前已核验宿主提供，冷重投不读序列化 `owner_root` 或 `content` 来获得权限。
- 原 `ready` artifact record 提供版本证明。缺 hash/size 的旧记录不得从当前文件重签；应重新完成原验证/登记。hash 是版本事实，不能代替归属授权。
- 唯一 `DeliveryService` 在第一个外部副作用前准备完整附件集合：归属、版本、平台能力或上传上限有一项失败，正文和附件都不发送。

## 原 FD 与单次不可变内容

`delivery/attachment_content.py` 复用 `common/nofollow_fs.py`：合法 alias root 先规范化，owner 内相对路径逐段 no-follow 打开，最终文件必须为单链接普通文件。实际读取按原 size 和 provider 上限有界，拒绝 FIFO、目录、硬链接、跨 owner、读取期符号链接替换和 hash/size 漂移。

`require_dir_fd=True` 必须透传原目录入口；缺 dirfd、`O_NOFOLLOW` 或 `O_DIRECTORY` 就失败，不能换成普通 `Path.read_bytes`。本轮 Mac 的正常平台与缺能力合成负例已覆盖；没有声称全部操作系统均具备这些原语。

校验成功的 bytes 放在现有 `_DeliveryAttempt` 的内存 tuple 中；同一份 bytes 交给 adapter 形成实际 multipart，不重新打开 path。文本发送期间即便原文件被替换、删掉或改成另一 owner 的符号链接，上传仍只能是原已校验内容。

不新增附件账、缓存目录、持久副本或可由模型声明的“可信 blob”。冷重投仍从原冻结引用与当前准确 owner 重新安全读取，hash 漂移失败会走原未送达收口，不能记 `sent`。

## 适配器能力和上限

- 原注册表新增明确的 `frozen_attachment_content` 能力及原 provider 文件/图片字节上限。
- `BaseChannelAdapter` 默认无此能力。正式附件投递只调用 `send_attachment_content`；未知第三方或旧 Path-only adapter 明确返回 `CHANNEL_ATTACHMENT_CONTENT_UNSUPPORTED`，不回落路径读取。
- 飞书通过 `FeishuAttachmentMixin` 消费宿主 typed bytes，并在上传端复核同 bytes 的 size/hash。保留原文件类型、图片选择、multipart 和稳定 uuid；未知格式仍沿原 stream 文件路由。
- 飞书现有文件上限 30 MiB、图片 10 MiB 没有放宽；原 `send_message` 工具最多十个附件。完整集合在内存冻结，单次工具调用的极端文件集合会占用最多约 300 MiB，另有单个 multipart 开销；没有新增磁盘副本。
- 直接 SDK Path 接口只保留原宿主调用兼容，它没有 owner 收件授权职责；正式模型工具/后台投递全部使用统一安全服务的 typed bytes，不把 SDK 接口作为降级通路。

## 固定认证 HTTP 和错误投影

`common/authenticated_http.py` 从原云接口的拒绝跳转模式统一出 stdlib opener。携带 token、应用 secret、device/client secret 或 Gateway 宿主凭据的固定接口拒绝所有 HTTP 重定向，原 URL、超时、状态裁决、重试与请求正文不变。

覆盖：飞书消息/回复/编辑、token、上传/资源下载、reaction、卡片、用户资料、云接口、应用注册；QQ token/消息/gateway；AdapterManager 的提交与四种原对账；私人审批/凭据卡原回环提交。QQ 不新增文件能力。

provider API 错误只记录数值 code/HTTP status、异常类型和有界引用。不得输出原 API `msg`/`data`、token 响应、exception str 或卡片表单内容。Gateway watcher 的正常模型回复和原冻结重投正文保持完整；只改变诊断投影，不能靠删业务回复“脱敏”。multipart 名称按 quoted-string 转义，引号/反斜杠保留，CR/LF/C0/DEL 控制字符拒绝，不按格式或样例文件名设置专项门。

## 验证范围

本轮攻击使用两个本地 HTTP origin、临时 owner 文件和生产 Tool/Delivery/Feishu multipart 路径。验证前后三项原攻击（跳转凭据、诊断正文、文本副作用期间附件 TOCTOU）有红绿证据。另覆盖原版本证明缺失、全量首发准备、冷重投、typed 接口、六种角色组合和平台严格能力负例。

未调用真实 Feishu/QQ 租户，未读取生产消息或密钥。不能用合成通过外推真实应用 scopes、管理员绑定正确性、现网已经发生泄露或所有外部 adapter 的实现质量。真实租户验证仍需保留原最小权限和只读/测试群边界。
