# Cell router — Stage 1

本组件只接收飞书 webhook，并为个人 HTTP 凭据选择组织所属 Cell。不修改 Python。
目录是经过校验的 JSON 快照；SQLite 仅保存投递回执和同步卡片结果。

## 构建与启动

```sh
cd deploy/cell-router
go build -o bin/cell-router ./cmd/cell-router
bin/cell-router -config /etc/cell-router/config/config.json
docker build -t my-agent-cell-router:latest .
```

构建工具链使用 Go 1.26，模块最低版本为 Go 1.24。唯一直接第三方依赖为
`modernc.org/sqlite v1.38.2`；`go.mod` 的 indirect 项均为该依赖的传递依赖。
Docker 构建 `CGO_ENABLED=0`，最终为非 root 静态镜像；构建产物不提交。

配置及目录示例在 [`../k8s/cell-router.yaml`](../k8s/cell-router.yaml)。
运行前必须提供其中引用的 Secret 文件，并把示例应用、租户、组织、owner 和容量改成经批准的目录记录。
示例 principal 是 disabled 状态，不能用于登录。个人凭据的原文只交给持有人，
目录只保存其 sha256 小写十六进制值。Cell 自己的管理员 ID 不得出现在 principal 中；
模板禁用通道管理员身份，Cell 的最终角色拒绝还依赖 S1-W5。

## 目录与分配

顶层为 `version`、`cells`、`orgs`、`owners`、`principals`、`feishu_apps`。
列名与 `C-S1W3-DIRECTORY` 一致，不增加目录数据库或隐式回退路由。

- owner 查询键是 `(provider, app_id, tenant_key, owner_kind, provider_owner_id)`。
- 组织是放置单位；每个 owner 指向一个组织，组织指向一个 Cell。
- 飞书 `local_owner_id` 必须等于其原始 open_id/chat_id，禁止 `admin`、空白和路径片段。
  同一 Cell 的不同复合身份不能共享同一个本地 home。Web 身份为 `web:<uuid>`。
- 每个 Cell 只有一个飞书应用。一个 by_owner 应用的所有 Cell 引用同一个应用记录及凭据目录；
  重复应用记录、冲突绑定、外键缺失、无效枚举、非法个人凭据摘要都会整体拒绝。
- `cells.token_secret_ref` 是纯 Cell 凭据文件；`feishu_apps.credential_ref` 指向包含
  `encrypt_key`、`verification_token` 的目录，两文件必须存在且至少一个非空。
- `Assign` 按服务中 Cell 的剩余 owner 容量为新组织分配位置，平局按 Cell ID 排序；
  添加 Cell 不重算已有组织位置。已有组织移动是显式运维操作。
- 目录与 Secret 文件只在启动和 SIGHUP 时读取；失败保留整个旧快照，日志只输出诊断码。
  修改 ConfigMap 后，应先等挂载内容更新，再向路由器进程发送 SIGHUP。

**应用凭据轮换不是普通热重载。**为保持已提交的原始加密正文可重放，进程存活期间拒绝
变更已见应用的 Encrypt Key/Verification Token；删除后重新加入该应用也不能绕过检查。
诊断码为 `DIRECTORY_APP_CREDENTIAL_CHANGE_REQUIRES_DRAIN_RESTART`。
先暂停平台入口、用旧凭据排空 pending/delivering 并处理全部 DLQ，等平台旧凭据重投窗口结束，
确认没有仍需旧凭据重放的回执，再协调更新 Cell 和路由器 Secret 并重启。
禁止在未处理旧账本时强制轮换。Stage 1 没有双凭据接受期，不改变冻结回执表来假装支持无缝轮换。

## 飞书路径及交付语义

公开入口：`POST /feishu/{app_id}/callback`。路由由路径选择凭据，先核验原字节签名及时间窗，
再解析/解密；无 Encrypt Key 时核验正文 token。未知应用 404，验证失败 403，均不写库。
`routing=single_cell` 不经过此入口，长连接仍由 Cell 适配器持有。

- `url_verification` 同步返回 challenge，不入库、不转发。
- 普通事件只在回执事务 **COMMIT 成功后**返回 `200 {"code":0}`；存储失败返回 503。
  事件键依次为 header.event_id、uuid、原始正文 sha256。未知 owner 仍可靠接收，
  状态为 `dlq`、原因为 `OWNER_UNKNOWN`，绝不选择默认 Cell。
- 工人按持久化到期时间唤醒，不固定轮询队列。每次尝试使用当前目录解析目标，记录 target_cell，
  原始正文不重排、不重新加密；时间戳每次从当前时钟计算，nonce 每次由系统随机源生成。
  同一秒内可能得到同一秒级时间戳，但 nonce/签名不复用。无 Encrypt Key 时不发送签名头。
- 只有 Cell 返回 2xx 才记 delivered；其他状态和传输错误指数退避，超过窗口进入 RETRY_HORIZON 死信。
  frozen 暂缓投递，draining 继续服务。重启时 delivering 恢复 pending。
- 卡片按 operator 身份或群 context.open_chat_id 同步代理，完整 JSON/status 原样返回；
  有 header.event_id 时成功结果持久缓存，并合并同进程并发重投。超时明确 504、不缓存；
  Cell 其他状态原样返回，结果缓存提交失败返回 503。真实私聊/群聊卡片载荷仍需平台联调。

这是 **至少一次** 投递，不是恰好一次。Cell 成功后路由器尚未提交 delivered 就崩溃的窗口必须靠
Cell 的 `GatewayIngressStore.put_if_absent` 吸收；本地测试用稳定事件键模拟该语义。
卡片副作用同样不能仅靠路由器结果缓存宣称跨崩溃恰好一次。
回执时间列和 card_results.created_at 使用 Unix 毫秒，签名时间戳使用飞书的 Unix 秒。
保留期只清理 delivered 和卡片结果，pending/delivering/DLQ 永不因 TTL 静默删除。

## HTTP 边缘与管理入口

个人凭据使用 `X-Gateway-Token`，无该字段时可使用 Bearer。重复字段只有完全相同才接受；
冲突或未知凭据 401，不联系 Cell。向 Cell 的请求清除所有大小写形式的客户端身份字段后，
只设置一个 `X-Gateway-Token`、`X-User-Id`、`X-Channel`。`X-Cell-*` 和其他认证字段不透传。
NDJSON 使用逐帧刷新，公共服务器没有写超时。frozen 返回 503 和 Retry-After。
`GET /authz` 只鉴权，返回三身份头及 `X-Cell-Upstream`，供受信任 nginx auth_request 使用；
它会返回 Cell 凭据，所以前置代理不得把此内部响应头暴露给浏览器。

管理监听器提供 `/healthz`、`/readyz`、`/configz`、`/receipts`、`/dlq`、`/dlq/replay`，
这些路由不在公开监听器注册。管理口不内置用户登录，必须保持 ClusterIP，
并通过 NetworkPolicy/RBAC 限制为运维入口。回执查询不返回原始正文或请求头。

```sh
curl "$ROUTER_ADMIN/receipts?app_id=app-example&event_id=event-example"
curl "$ROUTER_ADMIN/dlq?reason=OWNER_UNKNOWN&limit=10"
curl -X POST "$ROUTER_ADMIN/dlq/replay" -H 'Content-Type: application/json' \
  --data '{"app_id":"app-example","event_id":"event-example"}'
```

重放仅允许 DLQ 回到 pending，清零 attempts 并立即调度；不会把已 delivered 的行倒退。
先更正目录并成功重载，再重放 OWNER_UNKNOWN。

## 运维参数（class E）

所有值由 `internal/config.Config` 管理，未知 JSON 字段/非法边界启动即报错。
`directory_seed_path` 与 `receipts_db_path` 必填；其余默认如下，持续时间使用 Go 字符串格式。
启动日志和管理 `/configz` 返回完全相同的有效配置，不包含 Secret 内容。

| 字段 | 默认 |
|---|---|
| listen_addr / admin_addr | :8080 / :8081 |
| read_header_timeout / timestamp_tolerance | 5s / 300s |
| forward_timeout / forward_workers | 10s / 8 |
| forward_backoff_initial / forward_backoff_max | 1s / 60s |
| forward_retry_horizon / card_proxy_timeout | 30m / 2500ms |
| receipt_retention / shutdown_timeout | 24h / 25s |
| max_header_bytes / max_body_bytes / max_response_bytes | 65536 / 4194304 / 4194304 |
| admin_list_limit | 100 |
| http_max_idle_connections / http_idle_timeout | 128 / 60s |

配置文件改动需重启；SIGHUP 只重载目录。SIGTERM/SIGINT 与 SIGHUP 分开通知槽，
避免重载拥挤导致终止通知丢失；先摘就绪、排空请求，再取消工人。

## Cell 实际进程接线

前台入口是 `my-agent --config /etc/my-agent/cell/agent_config.cell.yaml gateway supervisor`，
不是 `supervisor run`，也不是会后台化的 `supervisor start`。已用 CLI help 与
`cli/subcommands_gateway.py:216-223`、`cli/supervisor.py:_supervisor_unit_text` 对照。
清单显式保留镜像的 tini 进程组信号转发。

`FeishuAdapter(` 的读取点包括 `cli/adapter.py:196`、`agent/scale_downstream.py:276` 和
`agent/delivery/registry.py:502`。其中接收 webhook 的实例由 `cli/adapter.py` 的通道管理器建立，
**不是 supervisor 自己内联持有**。`gateway_parts/supervisor.py:717-746,770-791` 只恢复已登记 running 的
适配器，不负责空 home 的首次注册。因此清单在 postStart 使用相同配置/home 执行
`my-agent ... adapter start --channel feishu --daemon`，后续交给原 supervisor 监护。
这修正冷启动缺接收器的问题，不增加第二个业务 worker，也不修改 Python。
此接线已静态核验，容器冷启动与故障恢复仍须在真实节点复核。

Secret `cell-a-auth` 必须有 `gateway.yaml` 和 `token` 两键，前者仅放 Cell runtime 层的
gateway_auth_token，后者放与之相同的纯值供路由器读取。`cell-a-feishu` 包含 app_id、app_secret、
verification_token、encrypt_key；所有 Cell 的应用凭据和对应路由器挂载必须一致。
模板刻意没有 gateway_auth_token 配置行，不把其误当作支持 env: 的字段。

## 验证与边界

```sh
go vet ./...
go test -race -count=1 ./...
go test ./internal/edge -run '^$' -bench BenchmarkEdgeOverhead -benchtime=1000x
```

测试覆盖 16 Cell/2000 条目录语料、复合身份、追加分配、非法目录保留旧版、持久提交边界、
两处崩溃窗口、重复事件、每次重签、未知 owner 重载后重放、假时钟退避与 DLQ、
挑战、加密/明文验证、完整卡片结果及缓存/超时、身份头重写、逐行 NDJSON。
参考签名 fixture 由 Python `feishu_crypto.compute_signature` 计算（无秘密的空 key 向量，
不用于认证）；加密和投递测试在临时目录生成真实测试密钥。
`cmd/cell-router` 的独立进程测试通过真正双监听器验证启动、HTTP/飞书入口、账本查询、
SIGHUP 有效/无效目录与 SIGTERM 退出，不以编译代替启动证明。

真实飞书时序、真实卡片会话形状、k8s apply、CNI 策略、Linux 镜像运行、Cell50 双 Cell 联调、
生产负载下吞吐/RSS、外部网络可达性不由这些本机测试证明。
上线前顺序：S1-W5 → S1-INT → Cells；S1-INT 前模板四个新键的 unknown-key 警告是已知接缝。
