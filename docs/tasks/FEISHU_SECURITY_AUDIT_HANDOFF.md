# 本期 IM 通道安全审计交接

## 目标与边界

用户要求开发后重点审查普通用户 IM、群助手、文件、秘密和越权，再提交打包。本片只用合成 owner、原生产入口及本地 HTTP；未读取生产正文/真实密钥，未发真实 IM/云请求，未部署、重启用户服务或推送分支。

设计合同：[FEISHU_OUTBOUND_SECURITY.md](../design/FEISHU_OUTBOUND_SECURITY.md)。root 负责全局配置/台账/目录树/总测试/错误分类和最后发行 gate，本片不重复写总计。

## 已确认并修复

1. 原 `urlopen` 会把 Bearer 认证交给跨 origin 的 302 第二跳；统一原拒绝重定向模式到 `common/authenticated_http.py`，各固定认证接口只发原请求。
2. 原飞书失败渲染/上传、QQ token debug、统一投递异常、Gateway 回送和卡片 callback 诊断可回显 API 正文或 exception str；现在只投影数值、type 和匿名引用，正常用户/重投回复不删。
3. 原 Tool 的归属/hash 前检之后先发文字，文字副作用可将本 owner artifact 替换成 foreign symlink；原 Feishu multipart 重开 Path 会上传 foreign 内容并假 `sent`。统一完整集合在首副作用前由原严格 FD 核验，现有 attempt 内固定 bytes，实际上传同 bytes。
4. 原 multipart filename 可插入 CRLF 部件头；通用 quoted-string 规则保留合法引号/反斜杠并拒绝控制字符，正式服务也在文本发送前拒绝非法文件名。
5. 原 Tool 对空 registry hash 会从当前文件补证明，且提前重复无界读文件；只承运原 ready record hash/size，缺原证明拒绝，安全读取只在 Delivery 一处。
6. 原 strict binary read 未把 `require_dir_fd` 透传原 parent directory 的双 flag 核验；缺原语的合成负例现在失败，不制造另一 FD 实现。
7. 普通用户跨 notify_channel 本已拒绝但原 code 被降 `UNKNOWN_ERROR`；root 登记原 `CHANNEL_TARGET_OUT_OF_SCOPE` 和新 `CHANNEL_ATTACHMENT_CONTENT_UNSUPPORTED`。不改变授权方向。

前三项原攻击的匿名证据 `/tmp/my-agent-feishu-security-audit-probes.json`。只有可控攻击的 bool；不能声称现网已实际泄露或恶意利用。

## 修改入口

新增：`common/authenticated_http.py`、`delivery/attachment_content.py`、`adapter/feishu_attachment.py`。

原载体与唯一出口：`conversation/channels.py`、`conversation/background_delivery.py` 仅附件投影、`capability/channel_message_tool.py`、`delivery/service.py`、`delivery/registry.py`、`adapter/base.py`、`adapter/feishu.py`。

原运输/安全诊断：`adapter/feishu_media.py`、`feishu_cloud.py` 仅共用 opener、`feishu_card.py`、`feishu_typing.py`、`feishu_profile.py`、`feishu_app_registration.py`、`qq.py`、`private_controls.py`、`manager.py`、`delivery.py`、`feishu_ws.py`。`common/nofollow_fs.py` 仅严格参数透传及双注释。

原只有两处 production `ChannelAttachment` 构造点，全部以宿主 owner root 生成；原 serialized 字段不被当权限或 content。未知接口无 Path fallback。QQ 保留无附件能力。

测试新增 `test_feishu_transport_security.py`、`test_delivery_attachment_security.py`。原 HTTP mocks 移到实际安全 opener，原附件 fake 消费 bytes 而不替产品回读。旧无 G1 的 owner/ingress 夹具用原 ensure 与真实客户端 header 补齐，不放宽 product 门。

## 当前证据

- 三项原攻击：`/tmp/my-agent-feishu-security-red.xml` 3 red → `/tmp/my-agent-feishu-security-green-1.xml` 3 green。
- multipart + registry diagnostic 边界：`/tmp/my-agent-attachment-boundaries-red.xml` 保留；其中 cold fixture 原缺 `ok=True` 造成一项夹具错误，已补原字段，不列为产品漏洞。
- 真实 watcher/WS/card/Gateway 诊断：`/tmp/my-agent-feishu-adapter-diagnostics-red-valid.xml` 3 red → `...-green.xml` 3 green。第一次 fixture 缺 credential/类型名/参数形状保存 `...-red.xml`，不算产品证据。
- 正常工具/后台附件/媒体/Delivery 首轮 6 文件 85 green；HTTP/quote/cloud 8 文件 184 green。
- 15 文件首轮 253 项中 244 green、9 red：4 缺 G1 旧夹具，4 target code 缺分类，1 JSON carrier 早取字段。按真实原合同修复，未降低原断言。fixture 编辑缩进 collection error 也保存，非产品失败。
- 最终 30 focused 文件 **560/560**，0 fail/error/skip，`/tmp/my-agent-feishu-security-final-focused.xml`。两新文件为 38 附件 + 21 运输（以 XML 为准）；多轮重叠结果不相加。
- 全仓 Ruff、doc sync、diff check 通过；严格 size `/tmp/my-agent-feishu-security-final-size.json` hard=0。clean-package 和最后发行联合 gate 仍由 root 统一执行。
- 非作者独立附件/HTTP/云 10 文件 **199/199**，`/tmp/IM_ATTACHMENT_CLOUD_INDEPENDENT_FINAL.xml`；实际 Feishu typed bytes 本机攻击 probe `/tmp/IM_ATTACHMENT_INDEPENDENT_PROBE.json`：host alias 冻结后改 foreign 仍只上传原字节、冷重试拒 foreign 零副作用、缺双 flag 拒绝。没有新安全阻断。
- 产品和本片文档冻结。后续 root 授权 `test_gateway_conversation_control.py` 的旧 HTTP 正例带原 G1 宿主头，24/24：`/tmp/my-agent-gateway-control-g1-http-fixtures.xml`，没有修改 payload、middleware 或原业务断言。
- root 首轮 Gateway 广测 54 failed XML 已完成但解释器退出仍被旧 non-daemon 假 worker 阻塞。只修两份停止夹具的异常清理：真实 stop/耗时事实在 cleanup 前固定，异常始终向外传播；cleanup 不设置成功事件。两个独立临时子进程注入 HTTP403/control 异常，旧夹具 2/2 red（解释器退出超时）→两负例与两原正例 4/4 green，`/tmp/my-agent-stop-fixture-cleanup-red.xml`、`...-green.xml`。只终止测试自己超时的子进程，没有停止生产服务；不把原失败 pytest 计通过。

## 追加：Gateway 鉴权启动与原始身份头

这部分沿原配置、G1、角色和 owner 入口修复，没有新增身份协议、令牌、状态或权限层。参考成熟 Gateway 的启动配置先校验、连接认证再分派模式（OpenClaw `src/gateway/server-runtime-config.ts`、`src/gateway/auth.ts`），未复制它的代理或认证模式。

实际复现及修复：

- 有真实 Agent、`auth_enabled=True`，但库调用漏传 middleware：原本无凭据 `/ask` 返回 202 并入队；现在服务启动从唯一 `auth/configuration.py` factory 自动接原 middleware，仍在网络/凭据准备前检查配置。CLI 的旧名字只保留直接 import alias，不保留转发函数或第二实现。
- `auth_enabled=False` 与多 owner 或任何 IM 凭据声明不能安全隔离。仅明确关闭 owner 分流、没有 Feishu/QQ 声明的单机模式合法；`agent=None` 原 utility 合同保持，不猜宿主配置。
- 鉴权开启而注入的 `AuthManager` 实际关闭，是宿主依赖矛盾，不是 HTTP 用户直接可造的输入；同 `AUTH_CONFIGURATION_UNSAFE` 拒绝。合法启用的自定义 middleware/token 保持原对象，不替换。
- **真实危险部署组合**：开启鉴权、关闭 owner 分流、声明 IM 凭据。实际普通 Feishu 私聊/群 actor 的 HTTP 角色仍为 USER，却落到 local/main；原 `/settings show model_reasoning_effort` 在真实 HTTP 返回 200/ok，Registry 也成为 `main_agent` 并指向管理员 home。唯一配置门现在禁止任意鉴权状态的这类组合；合法管理员仍使用分流及原私聊绑定，不改 owner resolver 或控制角色逻辑。
- IM 声明包括 Feishu app id/secret、verification token、encrypt key 和 QQ app id/secret，按字段存在值判断，不解引用或回显秘密。scope/manager 的关闭真值与原消费者一致，None/0/空串不能绕门；无鉴权单机例外仍须显式 `scope=False`。CLI 校验在原 setup/recovery/workers 前；Agent 初始化与目录准备仍可能先发生，不能称作完全零文件副作用。
- raw HTTPMessage 原先被 dict 折叠，冲突身份头可能投影为缺头管理员，或把 credential 冲突误报缺少 G1。middleware、原四包装及 HTTP 的 identity/channel/denial 三投影保留原副本统一裁决；同值副本允许，有效宿主凭据且完全缺身份头的合法本机终端仍为 admin/chat。实际路由在 body/业务前拒绝歧义。

本片新增/改变的正式入口：`auth/configuration.py`、`auth/middleware.py`、`gateway_parts/http_service.py`、`gateway_parts/http_handlers.py` 三投影、`cli/gateway_process.py`。root 已登记 `AUTH_CONFIGURATION_UNSAFE` 并维护主导航与本机信任设计。新测试为 `test_gateway_auth_configuration.py`（28）和 `test_gateway_header_ambiguity.py`（12）；原五处旧 HTTP 正例只补正式 G1/原身份/会话头，原业务断言不降低。

证据边界与计数：

- 初始库入口/无鉴权部署 8 案中 7 red，`/tmp/my-agent-auth-configuration-red.xml`；注入 disabled manager 1 red，`/tmp/my-agent-auth-injected-manager-red.xml`；关闭 IM 分流 8 red，`/tmp/my-agent-im-shared-scope-startup-red.xml`；同因 None/0/空串 6 red，`/tmp/my-agent-auth-false-equivalent-red.xml`。每次修复后的最终原断言统一由下方 XML 覆盖，不把重叠轮次相加。
- 原 raw HTTP 8 冲突 red 与 2 合法 green、内部投影 2 red 的证据分别在 `/tmp/my-agent-gateway-header-ambiguity-red.xml`、`/tmp/my-agent-raw-handler-projection-red.xml`。有效 G1 的 holder 健壮性问题与漏接 middleware 的未凭据开放分别报告，没有把两者混为新增外部授权。
- 最终 16 focused 文件 **416/416**，0 fail/error/skip，`/tmp/my-agent-auth-lifecycle-final-v3.xml`，含 admin 绑定、owner 分流、正式控制/记忆/历史/显示/客户端凭据等。非作者新两文件最终 **40/40**，`/tmp/IM_AUTH_CONFIGURATION_INDEPENDENT_V3.xml`；此前 77/34 邻域只保留历史，不累加最终新配置计数。
- 仓库外真实随机 HTTP 原 24 个 protected routes × missing/wrong G1 共 48 请求均 403、零磁盘业务，公开 status/metrics、独立插件 token、配置 token、原客户端重读及坏 G1 保留均通过：`/tmp/my-agent-g1-independent-http-probe-final.json`。插件 probe 用合成内部激活引用，不是已安装真实插件验收。
- 危险组合的真实 owner/control/Registry 匿名事实：`/tmp/my-agent-auth-scoping-boundary-probe.json`；启动修复对照：`/tmp/my-agent-g1-library-boundary-final.json`。全部只用测试私有临时根，不代表生产已经被利用。
- Ruff、diff、doc sync 通过，严格 size `/tmp/my-agent-auth-lifecycle-final-size.json` hard=0。整体 clean-package、最终 focused 联合与安装包由 root 执行；本片没有提交、打包或部署。

root 首轮 348 文件联合自然退出 1 后追加的三处旧 G1 夹具迁移：`test_decision_experiment_notice.py`、`test_host_notices.py`、`test_im_delegation_ingress.py`。定向首红 `/tmp/my-agent-im-repair-20261013.9/g1-notice-fixtures-red.xml` 三项全部失败：前两项未按测试宿主根创建/加载原 G1，后一项真实 HTTP 未带凭据。没有确认新增生产缺陷。

提示投递测试按正式 CLI 接线把 poll client 绑定到真实临时宿主的 `gateway_client_credentials(agent)`，用原 ensure 创建 G1；只将旧 urlopen 替身移到当前 `open_authenticated_request` 运输出口，同时检查原用户/渠道/请求编号与实际凭据匹配，避免测试误连本机 8420。没有 mock `credentials.headers`、更改 middleware/角色或降低原 notice/delivery/replay 断言。委派测试直接带同一真实 Gateway.start 生成的 G1 及原普通 IM 身份头提交，派工与重送语义不改。

三文件邻域加原客户端凭据两文件最终 **95/95**（8 + 8 + 1 + 56 + 22），0 fail/error/skip，`/tmp/my-agent-im-repair-20261013.9/g1-notice-fixtures-final.xml`。定向红与第一次绿 XML 均保留；不累加重复运行。仅测试/本交接文档改变，窄 Ruff 与 diff check 通过，生产源继续冻结。

## 追加：Feishu 验真边界的只读复核

正式 `_FeishuCallbackHandler` 与 `verify_and_decode_webhook` 的仓库外本地 HTTP 合成探针：message/card 两类各测空 verification+encrypt、错误 token、错误签名，六阴性均 403 且零消息/卡片 callback；合法 token 与合法签名四阳性各 200、只调用一次对应出口。缺 `lark-oapi` 的 WS 客户端明确异常、零 payload callback；源码没有 WS 失败转 webhook 的分支，默认仍是 WS。

证据 `/tmp/my-agent-feishu-webhook-auth-probe.json`；原 crypto/Feishu/WS 三文件 **45/45**，`/tmp/my-agent-webhook-auth-independent.xml`。探针只替换认证后下游 callback 为计数器，未跳过原验真；它不覆盖真实租户握手、真实卡片送达或应用权限，也不证明生产 payload 的存在。这次复核没有改变 webhook/WS 产品代码。

## 剩余边界与建议下一步

先由非作者复查 immutable bytes/精确 owner/冷重试与 strict FD，再跑 root 联合 focused 和严格 gate，通过后由 root 提交并构建新候选。各片可并行只读复验，但不要改共享出口、扩大 scopes、复制生产密钥或把旧包当新源验收。真实租户权限/推送仍未验证，平台原语不具备时明确拒绝上传。

本片 source/tests/docs 已最终冻结；root 完成联合 gate 后，再独立审同源新包的认证初始化/G1/真实请求头。非法旧部署应明确拒绝，不自动改用户配置、迁移 owner 或替换 token。
