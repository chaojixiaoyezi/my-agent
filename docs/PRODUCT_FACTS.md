# 当前产品事实

更新时间：2026-07-17。本文是 `my-agent` 当前能力状态的唯一权威页；README、路线图和历史审计
只能引用这里，不能把“代码存在”“测试存在”或“设计完成”写成已经稳定可用。

## 状态定义

- **稳定**：在明确支持范围内有正式入口、默认配置、失败语义和持续门禁，可作为当前产品承诺。
- **部分可用**：主链已存在，但平台、部署、规模或真实场景验收仍有明确缺口。
- **实验性**：有代码或专项验证，不是默认生产入口，配置、兼容性或长期运维契约仍可能变化。
- **仅设计**：只有方案、接口预留或文档，不应向用户宣称可用。

状态只描述当前工作树。它不等同于已发布版本；未提交、未推送的能力不属于远程 `main`。

## 当前能力矩阵

| 能力 | 状态 | 当前事实与承诺边界 |
| --- | --- | --- |
| Python 包、`my-agent` CLI、默认 gateway/chat 主循环 | 稳定 | 唯一正式普通用户入口是无子命令 `my-agent`，自动确保 gateway 存活并 attach chat client。`run` 与 `chat --direct` 是脚本/调试面，不是另一套默认 runtime。稳定范围不包含十万用户容量承诺。 |
| 本地文件工具、结构化 Tool Gateway、错误分类 | 稳定 | 正式工具调用统一经过注册、授权、参数、路径、限流、effect 和幂等门；不得通过直接新增旁路执行器绕开。POSIX shell 使用 `pipefail`，管道末端成功不能掩盖前段失败；前台超时、用户中断、后台 kill 与日志上限共用完整后代进程树终止，覆盖 bwrap 内层新 session，随后只做有界 pipe drain。控制流使用归一错误类别，同时保留提供方原始错误码和脱敏输入形状供审计。后代树候选已通过本地真实进程回归，1.10 部署复验尚未完成。 |
| 发布干净度检查 | 稳定 | 工作树模式检查 tracked 和未忽略 untracked；制品模式直接检查 wheel/zip/tar 成员、运行目录、路径穿越和大小预算。 |
| 单用户 owner home、文件记忆、SQLite/FTS | 稳定 | 适用于本地/单节点；不是 PostgreSQL、RLS 或在线迁移的替代证明。 |
| 多用户 owner scope 与 Linux shell 隔离 | 部分可用 | owner-scoped 前后台 shell 必须经 bwrap；不可用时结构化 fail-closed，禁止宿主降级。root 部署的宿主 home 放宽仅限无 owner scope 的本地管理员。远程 owner 默认只能访问自己的 owner home 和管理员显式发布的 `~/.my-agent/shared/`；其他 user/group owner、根模板、旧顶层私有目录与未授权宿主路径在 full mode 下也拒绝。只有当前轮的结构化 capability/delivery contract 可精确加入额外 workspace root，且不能覆盖凭据文件或其他 owner 拒绝。子代理 shell/后台命令/PTY/LSP 的本地候选已改为 owner home 只读基座加精确 `allowed_write_roots` 可写叠层，并阻止 PTY/LSP 跨任务复用权限；聚焦回归通过，尚待发布及 1.10 真机反证。随 wheel 发布的 builtin tools/skills 是公共代码能力。Docker 真机已验，Kubernetes 目标集群仍需节点 profile 分发与验收。 |
| 一键容器安装 | 部分可用 | P0 容器与 bwrap 改动已进入远程 `main`；安装器可生成透明 `my-agent` 包装器。scale K8s 清单已有 migration、stable/canary ingress+worker、monitor、灾备 Job；目标节点 profile、镜像签名/SBOM 和集群滚动验收尚未完成。 |
| Feishu 接入、会话/身份边界 | 部分可用 | 默认长连接、密码/确认卡片、per-user/per-group owner 与普通自然语言入口已接通。Feishu 只负责入站和投递，不拥有会话、任务、compact 或 memory 语义。私聊按 user、群聊按结构化 chat_id 落入独立 owner；同一 owner 按真实 `chat_id + thread/root_id` 使用唯一 thread transcript，达到阈值后在原 history 上 compact 并继续累计，provider 上下文窗口缺失时才使用本地配置。task link、workspace、progress、wake 和子代理树只是同一 thread 的运行事实，不过滤、复制或替换对话历史。CLI/IM 共用 `/status`、`/btw`、会话运行时 式 `/stop` 与 `/goal`；`/btw` 在当前执行安全点作为真实 UserTurn 注入并幂等写回同一 transcript，`/stop` 保留 transcript/workspace/memory。普通回复由 LLM 根据真实运行事实生成，内部协议和子代理命令不进入用户正文。1.10 已完成两个 Feishu-scoped 合成用户的长任务、上下文续接、隔离和当前任务引导矩阵；这证明 Gateway 服务器侧主链，不等于 Feishu 平台真实客户端入站，也不构成十万用户容量、故障切换或长期运营证明。删除第二套 task history/task compact 后的当前候选已通过聚焦回归，尚待完整门禁、推送并以同一提交部署复验，因此仍不宣称生产稳定。 |
| `/goal` 持续目标与 `/audit` 特殊模式 | 部分可用 | `/goal` 按 会话运行时 语义实现为同一 conversation thread 的持久 overlay：每 thread 一个未结束目标，公开字段为 `threadId/objective/status/tokenBudget?/tokensUsed/timeUsedSeconds/createdAt/updatedAt`，状态只允许 `active/paused/blocked/usage_limited/budget_limited/complete`。模型工具只有 `get_goal/create_goal/update_goal`；create 只接受用户或系统显式请求，update 只允许 `complete/blocked`。没有子代理时，active 目标只在本轮真实调用过工具且没有排队用户工作时去重续跑；有非终态子代理时由其结构化生命周期事件叫回。全部终态后主代理使用同一 thread history 整合、验证并显式 `update_goal`。`/stop` 后的普通续接由模型选择精确旧 `task_id`，同时恢复匹配的 paused goal 和原工作区，不解析“继续”等自然语言作为机器权限。task progress 与 recovery refs 只用于运行恢复，不是另一份上下文或 compact。零工具轮停止，token 预算、provider 用量限制和回合错误分别进入结构化状态。`/audit` 仍只有显式前缀才激活，普通任务不能自行升级保证档。1.10 上两个 Feishu-scoped 合成用户已分别完成 Hyperfine 与 Tokei 的 `/goal` 长任务；当前底座候选仍待完整门禁、发布和同提交部署反证。 |
| Gateway、持久请求、lease/recovery | 部分可用 | 普通用户默认 gateway 仍是本地文件事实源；无限 watch 未收到 stop 却自行返回时记录明确 termination reason 并以非零码失败，计划停止、有限轮完成和清理 drain 分开记账。scale profile 另有 PostgreSQL SKIP-LOCKED 队列、Redis 跨副本准入/租约和真实 Agent worker。目标集群故障切换与容量仍未验证。 |
| 子代理、任务账本、会话 compact/resume | 部分可用 | 有正式运行链和大量回归；创建记录、调度接收、runner 实际运行三层事实分开，公开回执不再把 accepted 虚报为 running。模型按真实独立工作项自主决定数量：单个 `goal` 只建一个 child，多个 child 必须用不同 `items` 明确拆分，重复项或超出本批/任务/owner/全局容量都会整批拒绝；模型入口不再提供 `count` 克隆，管理员低层 CLI 不在此范围。主代理是唯一用户聚合出口，子代理内部评论、命令和协议不入 transcript；子代理结果、artifact refs 和能力请求只作为结构化事实交给主代理判断与汇总。普通任务与 会话运行时 一样由模型基于真实工具和测试事实给出自然最终回复，不再经过目录扫描验收器、完成 marker 或 `submit_for_acceptance`。根任务不再生成 task compact/rollup package；主代理只压缩同一 thread history，每个子代理只压缩自己的 session。当前候选另有不执行 LLM 的独立孤儿回收线程、分页 owner 发现，以及按 `runner_session.in_process` 区分协作中断与子进程信号的取消边界；聚焦回归通过，尚待完整门禁、发布与 1.10 部署反证。完成质量、长期并发和十万 owner 恢复时延仍不作规模承诺。GitHub API、PyPI、npm 三路真实保证档已完成单一连续段超过 24 小时的逐拍 proof。 |
| MCP stdio 工具 | 实验性 | 未声明工具默认 `dangerous` 并进入统一 effect/幂等/审批门；只有部署配置可逐工具声明更低 effect。当前 wheel 已由本地 Qwen 驱动 `@modelcontextprotocol/server-filesystem` 完成 bwrap/stdio 握手、14 工具发现和 `list_allowed_directories → read_text_file`；写工具仍在 client call 前被审批门阻断。主流 server 生态仍需扩大验证。 |
| 工具检索 | 部分可用 | 关键词与真实 embedding 语义通道已经接入同一混合检索器；工具向量按目录版本缓存，端点失败降级关键词并由 `list_tools.tool_retrieval` 暴露状态。动态 MCP/LSP 工具已有通用来源/用途元数据；57 工具的完整 `list_tools` 仍归档 107,039 bytes，模型侧只回灌 11,331 bytes 紧凑索引和恢复锚点。默认未配置 embedding model 时不会伪装成语义可用。 |
| ASGI、SQLAlchemy/PostgreSQL、RLS scale profile | 部分可用 | scale worker 已要求 PG/RLS owner manifest + versioned S3 objects，Pod 只用 emptyDir；无 S3/bucket versioning 时 fail-closed，真 PG+MinIO API 已验。目标 Kubernetes context 当前不存在，尚未做真实集群灰度。 |
| 扩展插件加载 | 实验性 | 唯一加载链接受管理员显式配置的已安装模块或 `my_agent.plugins` entry point；不扫描用户可写目录，缺失/重复/注册失败会阻断启动。尚缺第三方生态兼容矩阵。 |
| PTY 交互终端 | 部分可用 | POSIX 已有真实 PTY start/write/read/close、增量游标与有界缓冲，并复用 shell 路径、命令策略和 bwrap。当前 wheel 已在 1.9/1.10 由本地 Qwen 经 4000 真实完成 Python REPL 五/八轮操作并 close；Windows ConPTY 尚未实现。 |
| LSP | 实验性 | 已有管理员配置、惰性 stdio server、initialize/request/didOpen/diagnostics/shutdown 全链；路径限于工作区，多用户 server 经 bwrap。1.10 已用主流 `typescript-language-server 5.3.0 + TypeScript 5.9.3` 返回 TS2322/hover，并由本地 Qwen 两轮真实 LSP 工具账本复验；其他语言服务器兼容矩阵与长稳仍未验证。 |
| OpenAI 原生工具调用 | 实验性 | OpenAI-compatible `tools/tool_calls/role=tool` 的非流式和 SSE 分片链已接入统一 ToolSpec/IR；坏参数进入截断恢复。本地 Qwen 已完成三轮真实文件工具调用，但尚未扩大 provider/model、streaming 和长稳矩阵。 |
| Redis、OpenTelemetry、在线迁移 | 部分可用 | Redis Lua、OTLP exporter、迁移 1–10 和应用只读版本门已接主链；release channel、Gateway API canary、分析门、PG backup/隔离 restore 清单已存在。尚无目标集群 HA、collector 后端、真实流量灰度和灾备演练。 |
| 十万用户以上容量与可靠性证明 | 仅设计 | 已有一次三路真实异构来源连续 24 小时保证 proof，但尚无正式容量模型、SLO、十万用户压测、故障演练和多周期运行证据；该 proof 不能外推为容量证明。 |

## P0 已完成的冻结范围

P0 期间停止扩展新功能，只允许修复以下收敛项；该范围已经完成并进入远程 `main`：

1. 本事实页保持唯一权威，并同步 README、设计账本和测试入口。
2. 根目录 pytest 可直接运行；当前已知失败与默认配置漂移清零。
3. `ruff check agent_py_agent scripts` 清零；门禁报告必须区分 blocker 与 advisory。
4. MCP 工具不能伪装成只读绕过 effect gate。
5. 多用户 sandbox 不可用必须 fail-closed，不允许用户可见审批或宿主 fallback。
6. clean-package 必须发现未跟踪运行数据和真实制品污染。

P0 只说明当前底线可信，不说明十万用户规模已完成。P1 已进入远程 `main`；P2 的正式 scale 配置、
Redis、OTel、在线迁移和 RLS 主链已落地，单一连续段 24 小时真实异构来源 proof 已完成；集群灰度、
容量/SLO、灾备演练与多周期长稳仍未完成。

### 2026-07-09 P0 验收快照

| 项目 | 结果 | 证据边界 |
| --- | --- | --- |
| 产品事实页 | 完成 | 本页已接入 README、设计账本和代码树。 |
| 根目录 pytest 与 4 个历史失败 | 完成 | 根目录全量通过；配置漂移、offline gate 误判、cwd 路径依赖、孤儿进程身份误判均已闭环。 |
| Ruff | 完成 | 正式 CI 范围 `agent_py_agent scripts` 为 0。 |
| 门禁报告 | 完成 | code-size 的 high-risk/soft 继续如实展示；只有权威 `blocked` 决策会阻断 offline matrix。 |
| MCP effect 绕过 | 完成 | 未声明 MCP 工具默认 dangerous；无 approval/幂等绑定时在实际 client call 前阻断。 |
| 多用户 sandbox | 完成（P0 范围） | owner-scoped shell 没有宿主 fallback；bwrap probe 不通过即 worker fail-closed。目标 K8s 集群规模验收仍属后续部署工作。 |
| clean-package | 完成 | 当前未跟踪 `data/` 被正确阻断并报告大目录；实际构建 wheel 的 artifact 检查通过。用户运行数据未删除。 |

该 P0 快照已由 commit `b8927837` 推送到远程 `main`。真实工作树 clean-package 仍会因
`data/` 运行数据正确失败；应继续把运行数据排除在制品/Docker context 外，不能删数据换绿。

### 2026-07-09 P1 验收

| 项目 | 当前结果 | 证据边界 |
| --- | --- | --- |
| import 边界 | 已接主门禁 | 全部分层矩阵进入 CI；28 条既有反向依赖按精确 source/target 冻结，新增即失败，旧债未伪装成清零。 |
| 生产包剥离 | 已验证 | 真实 wheel 成员 1828→974，tests 801→0、offline contract 21→0、`real_e2e_commands` 不发布；脏 `build/lib` 注入复验仍为 0。 |
| 默认入口 / gateway | 已验证 | 无子命令 `my-agent` 仍是正式入口；`chat` 默认 gateway，只有显式 `--direct` 前台直连。 |
| 插件链 | 已验证 | 显式 module/entrypoint 发现、固定注册顺序、重复/缺失/注册失败 fail-closed；不扫描 `extensions_dir` 执行代码。 |
| 语义工具检索 | 已验证（确定性） | 真实 EmbeddingProvider 调用、缓存、cosine 与可观测降级均有测试；未在本轮调用付费外部 embedding provider。 |
| PTY / LSP | 已验证（本机协议） | 真实 PTY REPL 和真实 stdio JSON-RPC fake language server 全链通过；不是 Windows/主流 server 兼容声明。 |
| OpenAI native tools | 已验证（协议） | 非流式、SSE 参数分片、IR 历史、schema/消息转换和坏 JSON 截断均通过；未在本轮调用真实付费模型。 |
| 完整发布门 | 已通过（源码/制品） | 最终全量 pytest 100% 通过；Ruff、import boundary、doc-sync、offline matrix、git diff 和真实 wheel 两道制品门均通过。code-size `hard=0 / high-risk=9 / soft=2 / blocked=False`，与 P0 基线一致。 |

P1 已由 commit `82b287b0` 推送到远程 `main`。

### 2026-07-10 P2 当前工作树验收

| 项目 | 当前结果 | 证据边界 |
| --- | --- | --- |
| scale fail-closed | 已接线 | 缺 PG/Redis/OTLP/app role/tenant/真实 handler+downstream/持久路径或回复凭据会在领取消息前失败。 |
| PG / RLS / migration | 本机真依赖通过 | 第一轮真 PG 迁移 1–6；第二轮实现扩到 1–10，owner manifest 同样 FORCE RLS，跨租户直查为 0。 |
| Redis | 本机真依赖通过 | Redis 7 容器中两个独立 client 共享限流、预算、全局并发租约；不是 Redis Cluster/故障切换证明。 |
| OTel | 本机真 collector 通过 | OTLP HTTP protobuf 可解析，span 与 W3C trace id 一致；不是生产 Tempo/Jaeger 后端可用性证明。 |
| 真实 worker | 已接线 | 内置 owner-scoped Agent 下游与飞书原消息回复；当前工作树在回复前提交 PG/RLS + S3 owner 快照，RWX 回退被配置门禁止。 |
| 连续监控 proof | 已证明（单一连续段） | 2026-07-11T09:11:17Z 至 2026-07-12T09:27:49Z 的 87,391 秒连续段逐拍满足三路真实保证源、900 秒 freshness、至少两个异构签名和 120 秒采样 gap；独立复核为 2,879 拍、3 路/3 签名、最大 freshness 451.773 秒、最大段内 gap 31.381 秒。 |
| 十万用户 | 未证明 | 没有容量压测、SLO、故障演练、成本模型实测；状态仍为仅设计。 |

P2 实现已由 commit `6c00bf18` 提交；本发布事实同步随后一并推送到远程 `main`。

### 2026-07-10 P2 第二轮启动验收

- Kubernetes：stable/canary release channel 已进入队列事实，Gateway API 初始权重为 0，分析 Job
  检查 canary readiness、失败行与陈旧积压；本机没有 kube context，未应用到目标集群。
- 灾备：新增小时 PG dump、版本化 S3 owner objects 和隔离 restore Job；尚未在目标账户执行恢复演练。
- Owner 存储：worker 的 RWX PVC 已替换为 emptyDir cache + PG/RLS manifest + S3；真 PG/MinIO API
  两文件恢复和跨租户 0 行通过。monitor watch/proof 仍使用 RWO 状态卷。
- 连续 proof：已修复真实 home 深递归、harvester sidecar 重复计数、历史成功冒充健康及旧 evaluator
  跨失格区间累计时长等失真；修正后的新连续段已超过 86,400 秒并独立逐拍复核通过。

本节实现已由 commit `7b6146a3` 提交；本发布事实同步随后一并推送到远程 `main`。最终 24 小时
proof 的事实见下方 2026-07-12 收口快照。

### 2026-07-12 修正后 24 小时 proof 收口

- 公共 summary 在 `continuous_seconds=87391` 时给出 `proven=true / reason=ok`。
- 独立扫描完整 NDJSON 共 5,768 拍、零 malformed；最后一次失格后只取当前连续段，共 2,879 拍，
  时间为 2026-07-11T09:11:17Z 至 2026-07-12T09:27:49Z，持续 87,391.407 秒。
- 当前连续段每一拍最少 3 路健康保证源、最少 3 个异构签名；所有健康源最大 freshness
  451.773 秒，段内最大采样 gap 31.381 秒，分别低于 900 秒和 120 秒硬门。
- 收口时 monitor PID 55241 已连续运行超过 24 小时，stderr 仍为 23,791 bytes，mtime 保持
  2026-07-10T07:04:09-0700；1.9 gateway 为 active，1.10 gateway 按计划保持 inactive。
- 该结果只证明这一个三路真实异构来源连续段满足保证合同，不证明十万用户容量、Kubernetes HA、
  多周期长稳、真实流量灰度或灾备恢复。

### 2026-07-17 两用户长任务、当前任务引导与独立产物验收

- 测试环境是 1.10、MiniMax M2.7，以及两组稳定且彼此隔离的 Feishu-scoped 合成身份。请求经 Gateway
  `/ask` 进入与 Feishu adapter 相同的 owner/channel/conversation 主链；没有伪装成 Feishu 平台客户端
  入站或引用回复验收。1.9 全程未改动。
- A 用户完成 Hyperfine 的一次性 `/goal` 复刻、分步 Zoxide 复刻、8 个项目深度对比、SL 与 Pastel
  两个协作任务；B 用户完成 Tokei 的一次性 `/goal` 复刻、分步 Navi 复刻、9 个项目深度对比、
  Tealdeer 与 Miniserve 两个协作任务。A、B 的口令、thread、workspace、task root、产物和搜索结果保持
  owner 隔离；B 长任务运行期间，A 的普通追问在 9.417 秒完成并正确沿用 A 自己的上下文。
- 全矩阵共选取 17 个不同的 `/btw` 输入：3 个落在 request 执行轮，14 个落在持久 task。17 个均只确认
  一次、待投递数最终为 0，并以真实 UserTurn 保存在同一 thread transcript 的准确工具往返位置。原始
  `guidance/*.jsonl` 是不可变追加记录，实际确认状态由独立 `guidance_delivered.json` 保存；不能用原始
  记录里的初始 `delivered_at=0` 误判为丢失。
- Miniserve 后续修复继续使用原 task `req_1784264255535_1355192_2`、原 thread 和原 workspace；没有建立
  第二个任务，也没有额外创建子代理。首次独立黑盒验收为 54/58，真实暴露 3 类上传残留和 1 个 WebDAV
  符号链接泄漏；同一任务修复后，从干净 wheel 新装并经真实 HTTP/HTTPS/TLS/WebDAV/上传路径重跑为
  58/58。wheel SHA-256 为 `444132b2177cd7556556e8c7a0f3639c5d818b5a09cda8d861f50c6f75a25962`；
  官方源码与只读快照验收前后均为 28 个文件，指纹保持
  `6bff9669dc47d384fe54a512a473a0177006f7ce5b9fe3d90f8afb5ba95497eb`，且没有构建缓存或临时上传残留。
- 本轮没有把独立验收器加入 my-agent 运行时，也没有恢复已删除的目录扫描验收/closeout 子系统。验收是
  用户外部黑盒复核；普通任务仍由模型基于真实工具、测试和子代理事实自然结束。
- 本轮 90% 正式 compact 阈值没有触发主 thread compact，因此不能拿该矩阵声称“本轮再次证明 compact”。
  主会话 compact 由既有 50% 真机证据与当前聚焦回归单独证明；根 task 不再拥有第二份 compact/history。
- 本轮代码边界复核固定在 会话运行时 `03bb3b12367397e14a8facc2e018d645ff4d8e83`、通道运行时
  `f2a46b0661206a0b7264ad05749e2304fbfe6a61`、长期助手
  `7d0246ab5715e9e18e156eb08912f4e24bd8d175`。这些引用只用于控制、会话和子代理边界对照；my-agent
  仍使用自己的 owner-scoped 文件事实源。

### 2026-07-13 普通飞书对话与工作链发布收口

- 普通用户不需要触发词：聊天、做事、派工和定时都先进入同一条常规对话链，由模型按自然语言
  选择工具。只有显式 `/audit`、`/goal` 保留特殊模式；其当前语义以上方能力矩阵和本轮设计文档为准。
- 飞书入站把真实 `chat_id` 作为会话 ID，话题消息再叠加 `thread_id/root_id`；owner 用户身份仍是
  独立隔离维度。网关每轮先读同一会话最近的 user/assistant 历史，当前消息保持独立的
  `# User Task`，回答后将本轮双方消息写回；不会把旧任务目标包进当前消息。
- 网关准入新增同会话单飞：同一用户同一会话一次只执行一条，后一条必须等前一条回答落库；
  同一用户的不同会话仍受每用户/全局上限并行。
- per-user owner 默认开启；远程身份缺失或 owner agent 创建失败时以
  `OWNER_SCOPE_UNAVAILABLE` 终态拒绝，不会回退共享 main owner 串户。
- 普通对话只有在真实调用 `task_progress`、`create_subagents`、`wait` 等结构化任务工具时，才在内部
  建立 task/workspace 运行记录；用户无需知道内部 task id。未绑定的新消息仍在同一 thread 历史中，并可看到
  active/interrupted
  候选，模型要工作时只能用 `task_progress action=select, task_id=<精确候选>` 续接；确实要另开工作时用
  `action=start, new_task=true` 明确新建。存在候选却省略 `new_task=true` 时底层直接拒绝，不让一次模型误判
  产生第二个工作区；最终运行事件关闭结构化候选，但不切换或重建会话历史。代码不解析用户
  自然语言决定任务身份。
- 会话任务使用两份非竞争索引：`task_ids` 是完整历史事实，供后台策略和审计精确读取；
  `active_task_ids` 只保存普通聊天可见的活跃候选。终态任务从热索引移除但不删除历史链接。子代理
  虽继承父任务的会话引用用于归档产物和进度，但它自己的收口无权关闭父会话任务；关闭入口按
  结构化 run source + 精确 task id 拒绝子任务关闭父链接；若子任务存在与自身 task id 完全相同的
  会话链接，则允许它关闭自己的 DONE 链接；子任务和 `bg-main-*` 内部链接即使处于 active/completed，
  也不会进入普通用户可选择候选。后台自动续跑也显式携带当前 thread/task link；只有结构化
  任务终态确认后才把该任务从活跃候选移除，
  避免“已经交付却仍被定时器重复做”。最近完成的任务另以只读候选注入；用户自然语言明确要求
  继续/修改时，模型必须先用结构化 `task_progress select` 重新打开原工作区；普通聊天不会自动绑定
  旧项目。若同一会话已有候选，所有带 `promotes_task` 的文件写入、命令、浏览器、PTY、LSP、派工和
  wait 入口都会要求先二选一：`select` 续接或 `start + new_task=true` 新建；失败的 select 和未确认的 start
  都不能再落入懒晋升。
  这不要求普通用户输入触发词。
  `select` 后本轮唯一当前工作区立即切到旧任务，后续所有工具参数中仍引用本轮占位目录的完整路径
  会在统一工具执行口改写到所选根目录，避免“进度账续上旧任务、文件却写进新目录”。当前 gateway 用户
  消息始终只落在同一 thread transcript；`select` 只切换结构化 task/workspace lineage，不再把用户正文复制到
  task guidance/history。落账、选择和权限失败均 fail-closed；代码不解析“继续、第二步”等自然语言决定归属。
  前台安全让出的模型回执同时携带本轮用户请求；若本轮刚 select 旧 task 且尚未写入新进度，旧任务摘要会
  从展示事实包排除，避免把上一小步误说成当前进展。`ddfd942a` 的 1.10 第三步真测中，B 的回执与后台
  Navi 执行均正确且独立复验 62 项测试通过；A 虽已选择原 Zoxide 工作区并在后台继续，辅助表达轮却因一次
  失败 update 与自身零工具视图错误声称“没有工具”。当前本地候选已改为只认可 `ok=true` 的进度 transition，
  并把成功选择工作区、成功访问运行时作为表达事实；不解析正文、不改变任务状态，待下一 wheel 真机复验。
  若子任务在根任务已 completed/superseded 后才迟到结束，其持久 wake 记录会直接归档，不再唤醒
  旧根任务或把旧任务回复写回当前普通聊天。
- 会话 transcript 是普通多轮的唯一对话事实源：不会再把每轮对话自动写入 owner-global memory。
  旧库中的 dialogue 记录会在检索层排除并先扩量再过滤，不会挤掉 USER preference/lesson。
- 当前工作树已把固定“最近 20 轮”从遗忘边界改成 compact 后的保留尾部：同一 thread 在阈值前注入
  完整未压缩段；到达现有 `memory_compact_auto_trigger_percent` 阈值时，用同一 token 估算和当前模型
  生成 thread summary，原始 JSONL 不删除，thread JSON 原子记录 message+byte cursor/generation；
  首次 compact 后直接从 byte cursor 读取新增尾部，不再每轮重扫旧前缀。旧消息另做
  `conversation_message` 派生索引，只写入当前 owner 的 LocalStore，供既有 `session_search` 召回。
  正式默认与包内 YAML 已统一为 90%；50% 只用于本轮真实压力验证。主运行仍用厂商 usage 与本地
  prompt 估算的较大值保护低报场景，累计账本不参与当前轮触发判断。
- `/verbose off|on|full`（以及 `/v` 查询）按 thread 持久化。Gateway 写 typed 工具事件，USER 只能经
  身份校验读取自己的 `/progress/<request_id>`；已有 delivery worker 按 cursor 回送，不重提任务。
  `on` 只发步骤摘要，`full` 才附脱敏且限长的工具结果，`off` 只保留占位和最终答复。
- 用户消息在调用模型前必须可靠落账，否则 fail-closed；模型已经完成后若 assistant 落账短暂失败，
  真实结果仍先返回并写持久化 repair，下轮幂等补账，避免重跑工具造成重复副作用。
- Gateway、飞书回复与 assistant transcript 共用用户回复投影：内部运行协议不得写入普通聊天；后台报告
  也只返回投影后的模型正文。这样 compact 与旧聊天检索不会再把机器协议混进用户上下文。
- 2026-07-16 双 owner 对比任务真测发现 MiniMax 会在自然回执中降级输出 Markdown
  `tool_call` 代码围栏；旧投影只覆盖 bracket/XML/direct-tag 形态，导致 B 用户看到 `task_progress`
  参数。当前统一出口增加 fenced tool/function call/result/output 清洗，保留围栏前后的自然正文；Gateway、
  transcript 和所有 IM 共用，不在飞书适配器另加关键词表。该边界对照 通道运行时 的
  `sanitizeAssistantVisibleTextWithProfile`/结构化 tool message 分离和 长期助手 的 tool_calls 字段分离；
  聚焦投影回归通过，尚待发布后 1.10 复测。
- 2026-07-17 Tealdeer 复刻真测确认模型已生成自然回执，但旧的 interim 完成语义正则把“分析完成后”
  跨句误判成“整个工作已完成”，最终公开响应为空。当前本地候选删除完成/ETA/大小语义正则和对应死
  代码；回执出口仅校验 typed runtime status、结构化工具调用、空正文和内部协议，任务终态仍只认运行
  事件。聚焦回归通过，尚未发布到 1.10，故暂不宣称空回执已在生产修复。
- 同轮 Sl 真测捕获 `/btw` 的前台到后台交接窄窗：linked request 已移入 done，而同一 durable task 仍
  active，旧检查错误返回“任务已切换”。当前本地候选用结构化 `current/retired/mismatch/unavailable`
  状态区分请求退休和任务切换；只有 retired 才回落核对同 thread 当前 TaskRun 并补幂等 wake，真实
  mismatch/读失败仍拒绝。单测已覆盖交接接受和切换拒绝，尚待 1.10 发布复测。
- 同一 Sl 任务在 `/stop` 后用一句“继续”已精确恢复原任务、工作区和文件访问，但辅助回执仍错误声称
  “没有之前的上下文”。候选工作树现把精确 owner store 与 durable task id 下的原 goal、workspace
  basename 和最近 8 条已确认 guidance 投影给回执模型；其他 task、待确认引导和宿主绝对路径不进入。
  这是表达事实补全，不解析用户自然语言，也不参与任务选择、状态或权限；仍待 1.10 真机复验。
- 同一续接轮还记录到：真实模型在约 14 秒已说出下一步，但用户直到 94.342 秒协作让出才收到自然回执。
  当前本地候选只在第一次工具开始边界，把已生成的真实模型正文净化成一条 typed
  `assistant_commentary`，独立于 `/verbose` 工具日志和最终回复；provider/runtime notice 不可进入，失败
  进度投递只前移 cursor、不会阻塞耐久最终交付。该设计已对照 长期助手 commentary 与 通道运行时 block
  reply 管线并通过聚焦回归，尚未发布到 1.10，故首次真实飞书可见延迟仍是待复验缺口。
- 普通任务最终答复直接采用模型基于当前对话、工具、测试和子代理事实写出的自然正文；不再压成文件清单，
  也不再经过提交工具、目录扫描验收器、完成 marker 或第二次摘要重写。外部投递边界移除内部协议并把
  宿主绝对路径降成文件名，内部 transcript 保留真实路径供后续同用户续接。
- 模型已注册唯一通道无关的 `send_message`：收件人固定为当前 scoped owner，不让模型传任意飞书 ID；
  附件必须命中该 owner 的 task artifact registry，且文件仍在 owner 根内、状态 ready、hash 未漂移。
  assistant transcript metadata 保留最近产物引用；下一轮用户只说“发我”时直接复用并原生发送，
  不重新搜索、复制或生成文件。长任务的逐工具过程可由用户用 `/verbose` 显式开启；更高层、低频的
  长任务阶段汇报仍是后续体验项。
- artifact registry 只登记工具显式写出或结构化返回的产物，不再扫描整个任务目录推断任务是否完成。
  `node_modules`、虚拟环境、缓存和 `.git` 等运行文件不会被自动升级成用户附件；用户文件不会被删除。
  HTML、压缩包和文档格式的客观完整性仍在对应写入工具边界检查，坏候选不能覆盖已有好文件。
- 默认规则改为随 wheel 发布的 `builtin:prompts/default.md`，不再依赖 systemd WorkingDirectory。
  owner 的 `AGENTS.md → SOUL.md → USER.md` 仍从唯一 owner 路径逐轮注入。USER 画像/偏好可由 Agent
  更新，但 add/replace/remove 必须以当前用户消息的逐字 `source_quote` 为依据、画像值必须出现在该
  引用中，且一次只处理一个单行事实；模型自行补出的称呼、身份或偏好不会落盘。SOUL/AGENTS 只能走
  `update_persona`，飞书必须由发起人点击确认卡片后才写。基础文件工具、patch 和 owner-scoped bwrap
  shell 均不能绕过；专用人格路由在普通工作任务晋升之前执行，因此不会被任务工作区选择错误遮住。
- Feishu 默认 `long_connection`、私聊密码锁默认开启；首次无密码时发设置卡但不吞掉第一条消息，
  只有设过密码且闲置超时后才拦截并要求解锁。显式配置可关闭密码锁或改用 webhook。
- 单机 Feishu 回调不再同步等待模型：提交 Gateway 后立即返回，pending/sent 回送记录持久化，后台
  worker 可跨进程重启继续轮询同一 request_id，超过旧 60 秒窗口仍送达真实结果且不重新提交任务。
- `/goal` 子代理阶段使用持久化 task/goal/run 状态驱动：有非终态子代理时不再登记即时自唤醒或用
  `wait` 轮询，完成事件会叫回同一个目标；全部终态后，主代理在同一目标上下文完成产物整合、客观
  验证和 `update_goal`。未终态自动轮、能力事件和迟到的已终态事件不进入普通 transcript；只有目标
  真正 complete/blocked 等终态才允许模型自然回复进入用户出口。该判定不解析模型或用户自然语言。
- Feishu 主动发送和引用回复共用 8000 字符分片边界，长报告不会再在最终引用回复时整篇一次提交；
  所有分片都会尝试发送，任一失败由既有持久回送记录继续重试。
- typed progress 现在与被认领的权威请求记录共址；per-owner 执行不会再把过程流写到 owner 私有
  Gateway 目录，`/progress` 和 Feishu delivery worker 能读取同一份经过 owner 鉴权的事件。
- USER 读取 `/result/<request_id>` 时，queued/processing/done/failed 都统一从请求记录解析 owner；
  响应文件不再承担身份事实。完成态同一用户可读、跨用户不可读；请求归档缺失、损坏或身份冲突时
  fail-closed。普通 USER 的完成结果使用顶层字段白名单，只返回状态、用户正文和安全计数；服务器
  path、lease、prompt 与内部错误细节不公开。管理员和 Feishu delivery worker 的可信回环路径保持
  完整诊断语义。
- scale worker 同样复用 gateway transcript/历史预算/任务候选主链，topic-aware lane 串行；ASGI 卡片
  action 明确分流，未配置 handler 时返回 503，不再误进普通消息队列和 dead-letter。
- 离线验收覆盖真实会话 ID 传递、两轮历史落库/重放、不同会话隔离、旧任务不污染、同会话并发排队、
  owner fail-closed、默认 prompt、人格卡片 owner 绑定、密码锁和 sandbox 只读人格文件。真实 1.10
  MiniMax 验证也已完成：`e947237e` 部署后，MiniMax M2.7 对同一会话正确回忆“蓝杉-472”，不同
  会话只答“不知道”；gateway/Feishu 两服务均 active、零重启、无 error 级日志。
- owner 修复提交 `36f5eb81` 的远端 Lint、Python 3.10/3.11/3.12、macOS 与 Windows CI 全绿。当前
  发布候选收集 8,337 项，完整 pytest 100% 且无失败；Ruff、import、offline、code-size strict、doc-sync、
  diff 均通过。worktree 检查按设计拒绝未跟踪运行数据；实际 2.66MB wheel 的
  distribution/artifact 两道门均为 `ok=true`、零 findings。1.10 已部署同一提交，Gateway/Feishu
  active、零重启、近 10 分钟零 error 日志；同一 USER 读取既有完成响应为 200/“蓝杉-472”，另一个
  USER 为 403，证明完成态可读和跨用户隔离同时成立。
- 1.10 的双用户 MiniMax M2.7 长链验收已经完成。A 用户先后完成健康预约平台和社区图书馆两个长任务；
  图书馆任务在后续自然语言检查/结案时继续使用原工作区，最终 30/30 自动测试、10/10 冒烟检查通过。
  B 用户把校园闲置交易网站分步布置，第二步完成后再次自然语言续接，22 条工具记录中 21 条引用原始
  task root、新请求占位 root 为 0，回归与新增测试共 37 项通过。任务终态后后台 claim 与 active link
  都正常清空；迟到 wake 没有复活旧分支。
- 50% 临时阈值下，A 的同一 Feishu thread 累计到 48 条、401,392 bytes 后自动推进到 compact generation
  4；byte cursor 为 211,732，正好落在 JSONL 行边界并与第 38 条 message ID 对齐，cursor 后仍保留
  10 条 raw tail。原始 JSONL 只增不删。压缩后的“银杏桥 / 每周四 19:20 / 青瓦计划 7426”可直接从
  summary 正确回答；更早的图书馆数据由 `session_search` 一轮成功找回 30/30 与 10/10。
- B 对同一 A 专属细节执行两次 owner-local `session_search`，结果明确为无记录；B 的工具索引没有 A 的
  owner path，A/B 的 transcript、thread state 和 LocalStore 数据库均位于不同 owner root。`/verbose`
  设置也在各自 thread 上持久保持 A=`on`、B=`full`。这证明本轮 owner/thread 数据隔离成立，不等于
  已完成十万用户并发容量证明。
- 真实召回测试发现 MiniMax 从 native tool-use 降级后会输出边界明确但非 JSON 的
  `tool => ... / --argument value` 调用；旧解析器连续拒绝，导致首次旧记录查询用 11 轮仍失败。统一解析层
  现只兼容无歧义、单行 JSON 值、无重复参数的该方言，随后仍走原有 schema/auth/path/runtime gates；
  多行含糊命令和夹带正文继续拒绝。部署 `da1a55e8` 后同一问题 1 轮搜索成功且零 parse error。
- 本轮双用户请求使用与 Feishu adapter 相同的 `X-Channel=feishu`、稳定 chat/user identity 和 Gateway
  `/ask` 主链，但测试 open_id 是隔离的合成身份，不能由真实 Feishu 服务端投递；因此证明的是服务器侧
  Feishu-scoped 会话/任务/compact/memory 链，不声称真实客户端收到了这些合成用户的引用回复或进度卡片。
  试验结束后 1.10 源码与运行配置均恢复 90%，Gateway/Feishu 为 active，健康探针返回预期 404。

### 2026-07-13 多 IM 统一投递当前工作树

- 普通最终回复、后台主动消息和显式 `send_message` 不再各自直调 adapter，统一进入
  `DeliveryService`。可信 `DeliveryContext` 持有 channel/target/reply_to，模型可影响的
  `ReplyEnvelope` 只持正文和已校验附件，没有收件人字段。
- `ChannelAdapterRegistry` 是 adapter、懒工厂、capabilities 和 target validator 的唯一解析入口。
  Feishu 的 `open_id` 合同位于注册层；未知通道不会回落到默认平台，新增 IM 不需要修改投递服务。
- `reply` 模式保留 adapter 的 `finalize_response`，所以 Feishu 仍会撤掉 typing reaction 并引用回复
  原消息；`proactive` 模式继续使用原生 text/image/file API。内部运行协议在统一出口净化或抑制。
- 普通回送的 pending/sent receipt 与显式工具的 owner 持久化 receipt 保持原有幂等边界；发送失败
  不会重新提交 Gateway 任务，也不会重新生成附件。
- 定向回归与受影响的后台会话测试已经通过；第二个 fake IM 通过纯注册接入，证明主流程无平台分支。
  这不是第二个生产 IM 已可用的声明，也尚未替代下一次正式 Feishu 部署后的真实引用回复/附件复验。
- 详细合同见 `docs/design/CHANNEL_DELIVERY_DESIGN.md`。

### 2026-07-13 owner/thread memory + compact 部署证据

- 实现、离线定向回归与上述 1.10 双用户 MiniMax 验收均已完成，覆盖超过旧最近轮数仍累计、自动
  compact、raw transcript 不丢、旧消息可搜索、owner LocalStore 隔离、per-thread verbose、typed
  progress、progress cursor 与最终回复不重跑。
- 参考范围与设计合同见 `docs/design/CONVERSATION_CONTEXT_DESIGN.md`。这里复用 通道运行时/长期助手 的
  “稳定 IM 会话键贯穿 compact/memory”和 per-session verbose 作用域，不复制它们的摘要算法或
  profile-wide memory 默认值。
- 本轮已经证明 1.10 单机、两个 Feishu-scoped 合成用户、MiniMax M2.7、50% 自动 compact 与恢复 90%
  的组合；真实 Feishu 客户端投递、更多并发用户、跨节点迁移和长期故障恢复仍未由这次测试覆盖。
- `session_search` 的主链已证明可用，但一次查询可能只召回问题的一部分：本轮首次成功查询准确找回测试
  数量，却只找回部分“已知限制”。模型能继续改写查询，但当前不保证每次都自动扩大搜索直到信息完整，
  这是后续召回排序与多查询收敛仍需补强的点。

### 2026-07-14 普通聊天与后台工作运行时加固

- 普通聊天不再在请求入口预创建 task workspace。只有 `write/edit/patch/shell/browser/PTY` 等注册表中
  明确带 `promotes_task` 的工作工具，或 `task_progress/create_subagents/wait` 等结构化任务动作，才把
  当前会话提升为 TaskRun 并在该 owner 的任务树中惰性创建工作区；不解析“帮我做一下”等自然语言。
- `create_subagents` 成功后，wake-capable 的 IM/Gateway 请求会进入一次无工具的短模型轮并随即释放
  同会话顺序槽。模型只收到调度账本确认的 recorded、accepted、runner-confirmed running 和 failed
  事实，再用自己的语气回复；系统不复用本轮 `[TOOL_CALL]` 文本，也不拼固定回执。模型若输出内部协议、
  工具调用或空正文会重试一次，仍不合格就记结构化失败并抑制，不用“正在处理”冒充回答。
- 同一会话仍按顺序写 transcript，避免并发 assistant 回复乱序；后台子代理继续使用独立 TaskRun、工单、
  状态和产物账本。用户随后发来的闲聊/补充要求进入同一 thread，但不会注入子代理的 shell 命令、碎碎念
  或原始工具结果。完成/阻塞/需决策事件再由后台主代理通过统一投递出口回到用户。
- 自动派工监督现在保存结构化 material signature：状态、进度、阻塞、能力申请、产物与结果摘要都没变时，
  只顺延下一检查，不花一次模型调用；变化后才唤醒。显式 `wait` 和数据源定时巡检不套这条跳过规则。
- 多个成功子任务在默认 5 秒窗口内连续结束时合并为一次主代理整合；若同一 root 仍有兄弟任务在跑，
  这轮模型正文只作内部推进，不写普通聊天也不主动发 IM。失败/阻塞/需决策和全部结束仍立即公开，
  避免每个子代理都向用户发碎片进度或重复派工回执。
- 子代理完成时现在先原子发布 wake，再追加带反向 wake ID 的 observation；调度器不再可能卡在两次写入
  之间，把同一完成既当 observation 又当 wake 各跑一轮。若 wake 队列写失败，observation 仍作为兜底，
  且按同一完成事件投递规则处理。内部 `wait`/自动续推在仍有子任务运行时也不写占位回复进普通聊天。
- 后台续跑从结构化 task link 恢复原 goal、task path 和 owner task index 标题。`定时唤醒`、wait reason、
  子代理 runner prompt 不能再生成旁路任务目录或覆盖父任务身份；任务事实仍只认原 owner workspace。
- `ConversationStore.bind_task` 现在是并发安全的“首次创建或补齐空字段”，不是任意 upsert：已有非空
  goal/task_path/created_at 永久保留，跨 thread 重绑直接拒绝，终态不会被遗漏 status 的调用重新放回活跃索引。
  后续计划、wait reason 和子代理 prompt 必须写进各自账本，不能借绑定接口改写父任务身份。
- persona 工具使用稳定 entry ID 做 list/add/replace/remove；USER 可由所属 Agent 维护，SOUL/AGENTS 的
  replace/remove 仍须所属用户卡片确认。密码、OTP 和临时验证码在写 owner durable memory 前被结构化拒绝。
- POSIX shell foreground/background 与 bwrap 路径统一用 `bash -o pipefail -c`；结果 envelope 明确记录
  `return_code/command_succeeded`。工具错误同时保留归一控制码与原始报码，审计只保存输入字段名、类型和
  不可逆摘要，不复制命令、密钥或正文。
- 强制 Tool Gateway 的错误码注册检查已覆盖实际执行管线及直接子门，不再只扫描 manifest/effect：
  tool protocol、path/command、owner scope、guardrail、rate limit/circuit、approval binding、idempotency
  和 gate pipeline 的拒绝原因都必须命中统一 taxonomy。命令引号未闭合会保留为可修复的
  `COMMAND_PARSE_FAILED`，动态 artifact-ref finding 先归一成稳定协议码；未知新报码仍 fail closed，
  但不能再由已知门禁静默退化成 `UNKNOWN_ERROR`。本地专项与全管线防漏测试已通过，1.10 待当前长任务
  自然结束后随最新 wheel 部署复验。
- 工具调用的 `RunScope` 已按结构化运行身份区分主代理与 task-local 子代理：后台主代理不再把
  `bg-main-*` 临时轮次当成子代理 ID 查询账本，也不再为每个工具调用附带虚假的子代理
  `FileNotFoundError`；其 `root_task_id` 保留真实持久任务 ID。真正的子代理仍读取 canonical task
  lineage，账本真实损坏时仍保留结构化 load error。该修复已随 `606fe20a` wheel 部署 1.10；部署后两个
  全新 Feishu-scoped owner 的后台任务均未再出现 `tool_call_scope.subagents.load` 假错，服务零重启。
- 协作工具现在复用同一 `RunScope.root_task_id` 绑定当前持久任务：后台主代理不必让模型重复提供
  `task_id/thread_id`，也不再因只看子代理 runner context 而返回 `thread_required`。协作域原始报码与统一
  控制码分开保留，例如 `THREAD_REQUIRED` 对应可修复的 `TOOL_PARAMETER_REQUIRED`；工具输出索引同时保存
  两者，不再只留下 `UNKNOWN_ERROR`。专项协作/错误契约/归档测试已通过，1.10 待当前长任务自然结束后
  随最新 wheel 部署复验。
- 后台主代理的公开完成回复使用实际落账/投递时间，不再沿用可能早了数分钟的 wake/request 时间；
  append-only transcript 仍以追加顺序为会话权威，thread `updated_at` 同时保证单调递增，延迟事件不能把
  刚活跃的会话排回旧位置。该模式与 通道运行时 在实际完成 hook 时记录 `Date.now()`、会话运行时 在结果返回后
  emit completed event 的边界一致。
- 1.10 双用户复验确认普通聊天能在长任务让出后继续，且分别找回各自代号；同时发现派工自然回执会把
  模型可见事实键 `runner_confirmed_running` 翻成用户正文。当前工作树已把表达层事实改为 planned/ready/
  started/failed-to-start 四个用户语义字段，保留内部生命周期精度但不再把 runner 词汇交给回复模型；
  专项回归已通过，待下一 wheel 真机复验。首次自然回执仍约 50 秒，继续作为性能缺口。
- Gateway watch 的计划 stop、有限 max-cycles 完成和意外返回已有不同终态；无限 watch 无 stop 返回会写
  `GATEWAY_WATCH_UNEXPECTED_RETURN` 并以 2 退出。cleanup 记录 heartbeat/request/background 三线程是否
  drain 完成，未在期限内退出则改记 `GATEWAY_DRAIN_INCOMPLETE`，不再显示成普通 stopped。
- 同一模型轮可连续提交多次 `create_subagents`，运行时按原顺序逐个执行并把各次 lifecycle 事实聚合后
  交给同一个无工具模型回复轮；仍依赖前一次返回 ID 的 dispatch/inspect 等动作继续延到下一轮，并返回
  `ORCHESTRATION_CALL_DEFERRED`，不再丢成 `UNKNOWN_ERROR`。这使“5 个不同子任务”既不会虚报，也不会
  因原生工具调用批次只实际创建第一个。
- 2026-07-16 双用户长任务实测又发现 MiniMax 会发出空参数 `create_subagents`：根因是旧 schema 没把
  `goal` 标为 required。当前工作树已把顶层 `goal` 设为机器必填；`items` 模式也必须带总 goal，并保留
  每项独立 goal。相关编排、原生工具和网关回归已通过，1.10 尚待当前长任务自然结束后部署复测。
- 同轮 `/stop` 实测还发现终态请求已移入 `done/`、task link 与 response 均为 `interrupted`，但归档 JSON
  残留 `processing`。当前工作树已改成以 response 终态覆盖旧 lease 状态；本地专项回归通过，1.10 部署
  验证尚未完成。
- 同一实测还复现了恢复后的 request id 与原持久任务 id 分叉：进度工具写原任务账本，旧验收器却读取
  新 request 的空账，随后普通追问又选中旧任务并重复执行。当前工作树保留唯一结构化任务身份解析器供
  guidance、task_progress、需求/派工 seed、coverage、wait、监督提醒和子代理归属使用；同时彻底移除
  普通任务的独立验收器。default 主代理使用 `conversation_task_id`，task_local 子代理继续按自己的 run
  隔离。恢复账本与子代理归属专项回归已通过；1.10 尚待部署后真机复测。
- 当前任务已经结构化建立后，模型若把交付文件臆造到该 owner home 的任务外位置，且用户没有显式指定
  该目录，创建策略会把路径及 goal/plan 中的同一引用一起归回当前任务 `output/`；从另一个任务复制来的
  绝对路径只保留文件名，避免在新任务中套出第二棵 `tasks/...`。用户明确指定的目录仍按原权限合同处理。
- 2026-07-16 的 1.10 分步续作真测进一步证明，仅做路径归一还不够：A 在新建 TaskRun 后用绝对路径
  回写了同一 owner 的旧 Zoxide 任务。当前候选在共用 runtime ledger chokepoint 按结构化
  `provider + owner_scope_root + task_root` 自动生成任务级 `allowed_write_roots`：远程普通 owner 仍可读
  自己的旧任务作参考，但文件工具与 bwrap shell 只能写当前任务；已有子代理窄授权不会被放大，兄弟任务
  授权被过滤，畸形 task root 显式空白名单 fail-closed。local 入口和有效 admin bypass 保持原权限。
  该实现不解析“继续”等自然语言；已对照 会话运行时 的 thread/turn identity 与 writable roots、通道运行时 的
  scoped session key，聚焦 runtime/write-boundary/sandbox 回归通过，尚待发布后 1.10 反证。
- 后台自动续跑、定时监督和子代理终态唤醒不以自然语言正文判断任务状态；是否仍在后台运行、是否已经
  协作式让出以及 conversation task 是否结束都来自结构化 task attributes 和 lifecycle 事实。最终公开正文
  仍由模型生成，内部监督/等待信号不进入普通 transcript 或 IM。
- 普通用户回复新增“模型正文 / 运行状态”硬边界：除显式控制命令外，通道正文只接受模型自然文本；
  `RUN/MAIN_AGENT/SUBAGENT` 协议和非阻塞状态只保留在结构化字段。只有内部协议而没有模型正文时不再
  写空占位 assistant 消息，也不再投递固定“任务正在处理”句子。
- 普通任务不必先进入 `/goal` 才能派子代理。长工作、子代理事件、定时唤醒和 compact 后续轮都在同一
  thread 历史中继续；耐久 progress policy 只负责崩溃恢复和后续调度，任务已停止或不再 active 时不得
  复活。非阻塞 `wait` 仍会结束当前 turn 后由 scheduler 续接，这一点尚未与 会话运行时 的同 turn `wait_agent`
  完全一致，但不会建立第二条聊天轨道或第二份上下文。
- `create_subagents.goal` 是模型工具调用里的整批派工说明，不是用户 `/goal` 模式开关。该边界与 会话运行时
  `spawn_agent.message`、通道运行时 `sessions_spawn.task` 一致：普通任务可以自主派工，但每个新执行单元都
  必须拿到明确工作说明。my-agent 的 `items` 批量形态还要求顶层整批说明和每项独立说明同时存在。
- 1.10 部署后的并行真测发现，后台根任务运行时，后一条普通聊天仍可能由模型调用 `task_progress select`
  变成第二个执行器，继而重复检查工作区，并把“前台工具轮数”等内部事实复述给用户。当前本地候选在
  任务选择卡口只读精确 task link、有效 background claim 与 enabled progress policy：已经执行的任务只
  作为 `running_in_background` 只读背景，第二次 select 返回已注册的
  `CONVERSATION_TASK_ALREADY_RUNNING`；状态损坏或不可读时返回
  `CONVERSATION_TASK_STATE_UNAVAILABLE` 并 fail-closed。运行来源和精确工具轮数不再进入模型回复事实。
  同一 task/kind 的前台续跑 policy 也会复用，避免重复唤醒。以上候选已通过专项回归，尚未部署 1.10。
- 普通任务最终正文不再经过冻结 snapshot 或第二个模型重写；主模型已经写出的项目目录、主要功能、测试
  结果和限制原样进入统一通道投影。真正交给 IM adapter 的 `ReplyEnvelope` 只装净化后的人话，内部运行
  协议和宿主绝对路径不进入外部正文；内部 transcript 保留真实路径供后续工作续接。
- `047e24f7` 的历史部署曾证明耐久后台派工可以完成，也暴露同一任务第二执行器和并行 turn 的语义问题；
  该结果只作为缺陷证据，不代表当前单一 history/compact 候选已发布或已通过真实 Feishu 验收。

### 2026-07-10 两机日志与真实 LLM 加固快照

- 1.9 / 1.10 的近 24 小时日志已逐项审计；1.10 service-cwd 的真实 memory 索引缺口已通过
  非破坏性 `local-rebuild --source memory` 从 0/8 修复到 8/8，复查 doctor 为绿色。
- 当前工作树已修复日志 secret 泄漏、Feishu 无效目标外发、结构化大批截断、取消态误恢复、原生工具
  历史 role 漂移、真实测试假绿、delivery materializer 隐藏硬门、Markdown prose 路径误判，以及
  LiteLLM `available context size` 未进入统一 compact/resume 错误分类的问题。
- 本地 4000 端口后的 Qwen 已通过 preflight、三轮原生工具、48 项结构化判断、长报告
  compact/resume、失败工具恢复、gateway ask、1 个真实子代理、两机 PTY、LSP stdio 和 MCP 调用。
  两台正式安装现已升级到同线 wheel，配置为 `openai_compatible` / `local-qwen` / `125184`；1.9 正式
  gateway ask 以 295.122 秒成功，1.10 服务保持受控停机。共享模型在多个 23k–70k token 后台请求下
  让新探针超过 120 秒，因此连接已证明，容量、尾延迟、隔离和十万用户规模仍未证明。
- 完整发现、参考文件、证据边界和未测清单见
  `docs/audits/REAL_LLM_24H_HARDENING_20260710.md`。
- 24 小时 proof 的旧 evaluator 在 86,457 秒一度给出 `proven=true`，完成审计发现它只检查最后一拍
  健康，却把整份 evidence 首尾当连续时长。真实 NDJSON 中 2026-07-10T19:53:35Z–19:56:36Z
  有一路超过 900 秒 freshness 门，因此旧结果已作废。公共 evaluator 现逐拍检查并在失格时重置，
  同一证据正确回退到约 46,984 秒、`duration_too_short`。response-body timeout 修复仍有效，
  monitor stderr 自 2026-07-10T07:04:09Z 后未增长。
  新 evaluator 重载时原 86,400 秒 watch 窗口已经到期，三路 harvester 按设计停止并导致最新
  freshness 失格；测试窗口保留原游标/账本延长到 200,000 秒后已恢复，pull 计数继续增长。
  因最新失格不能跨段拼接，2026-07-11T09:11:47Z 的新连续段从约 30 秒重新计时。
- 后续两机同时经 4000 的真实 Qwen 调用均完成。LAN 地址漂移暴露出旧端点会占满 600 秒的问题；
  当前 provider transport 已把 connect 默认限制为 10 秒，并保留长 reasoning 的 600 秒 read/request 窗口。
  旧地址在 10 秒失败、正常请求持续约 7–8 分钟后成功，仍需 DHCP reservation 或受管 DNS 消除地址漂移。
- runner-session 心跳不再每 5 秒触发完整 workspace/compact/projection 保存；takeover replacement 现在从
  `context_bundle.takeover` 直接获得有界结构化来源 handoff，不再被提示去用普通文件工具读取受管状态面。
- 真实第 5 个 runner 还暴露出 unstructured fallback 假绿：原始与 repair 正文都为空时，旧 finalizer
  仍写 `DONE/VERIFIED`。当前已改为 `BLOCKED/UNVERIFIED/structured_output_parse_error` fail-closed；
  只有可解析的结构化 runner result 能进入成功态。
  同一个 run 在隔离配置把输出预算从 512 提到 2048 后，真实返回结构化摘要
  “结构化接管成功 / 子任务1”，0 工具调用并进入 DONE/VERIFIED；没有创建第 6 个测试子代理。

## 本轮参考核对

- 先查阅 `长期助手_contract_code_files.xlsx`、`通道运行时_contract_code_files.xlsx`、
  `终端交互_contract_code_files.xlsx` 缩小 Tool Gateway / permission 范围。
- 长期助手 实际源码核对 `agent/tool_executor.py` 与 `agent/tool_guardrails.py`：桥接/检索得到的
  真实底层工具名必须先进入统一 hook、guardrail 和 scope 判定，再 dispatch。
- 通道运行时 实际源码核对 `src/plugins/tools.ts`、`src/skills/runtime/tool-dispatch.ts` 与
  `src/mcp/plugin-tools-serve.ts`：动态工具仍由 host 注册和执行边界持有，不以模型文本作为权限事实。
- 终端交互 实际源码核对 `src/services/tools/toolExecution.ts`、`toolOrchestration.ts` 与
  `src/hooks/useCanUseTool.tsx`：正式执行路径在动作边界统一调用 permission decision。

本轮复用的是“动态工具也必须穿过不可绕过的 host 执行门”这一模式，不复制参考项目的工具数量或 UI。

普通对话收口另核对了 长期助手、会话运行时 与 `fable_my-agent-claw`：复用了稳定 thread、逐轮历史和
结构化工具续接；没有照搬 长期助手/会话运行时 的旧 goal 自动注入，也没有照搬 claw 的群聊首位发言人
owner 和双套 SOUL/USER 路径。当前权威顺序是基础系统规则、内置产品规则、单一 owner 人格/画像、
同会话历史、当前用户消息。

本轮原生附件与统一投递继续核对 通道运行时 的 per-run current channel/target、typed reply/media、
provider plugin 和 Feishu dispatcher/outbound，以及 长期助手 `tools/send_message_tool.py` 的通道无关
`send_message` + adapter 路由。项目采用“可信上下文与回复信封分离、一个统一消息工具、adapter 注册”
三个 chokepoint，不复制它们的工具数量、平台枚举或 `MEDIA:path` 正文标记。

P2 的参考文件和取舍见 `docs/design/P2_SCALE_MAINLINE.md`。

## 发布判定

发布前至少同时满足：

```bash
python3 -m pytest -q --tb=short
ruff check agent_py_agent scripts setup.py package_boundary_policy.py
python3 scripts/check_import_boundaries.py
python3 scripts/check_offline_contract_matrix.py --repo-root . --json
python3 scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json
python3 scripts/check_doc_sync.py
python3 scripts/check_clean_package.py --mode worktree .
git diff --check
```

容器发布还必须在最终镜像执行完整 sandbox probe；制品发布必须对实际 wheel/tar 运行
`check_distribution_boundary.py <wheel>` 和 `check_clean_package.py --mode artifact`。工作树有用户运行数据时，clean-package 正确失败，
不得通过删除、忽略或缩小统计口径把失败改成绿色。
