# 当前产品事实

更新时间：2026-07-13。本文是 `my-agent` 当前能力状态的唯一权威页；README、路线图和历史审计
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
| 本地文件工具、结构化 Tool Gateway、错误分类 | 稳定 | 正式工具调用统一经过注册、授权、参数、路径、限流、effect 和幂等门；不得通过直接新增旁路执行器绕开。 |
| 发布干净度检查 | 稳定 | 工作树模式检查 tracked 和未忽略 untracked；制品模式直接检查 wheel/zip/tar 成员、运行目录、路径穿越和大小预算。 |
| 单用户 owner home、文件记忆、SQLite/FTS | 稳定 | 适用于本地/单节点；不是 PostgreSQL、RLS 或在线迁移的替代证明。 |
| 多用户 owner scope 与 Linux shell 隔离 | 部分可用 | owner-scoped 前后台 shell 必须经 bwrap；不可用时结构化 fail-closed，禁止宿主降级。Docker 真机已验，Kubernetes 目标集群仍需节点 profile 分发与验收。 |
| 一键容器安装 | 部分可用 | P0 容器与 bwrap 改动已进入远程 `main`；安装器可生成透明 `my-agent` 包装器。scale K8s 清单已有 migration、stable/canary ingress+worker、monitor、灾备 Job；目标节点 profile、镜像签名/SBOM 和集群滚动验收尚未完成。 |
| Feishu 接入、会话/身份边界 | 部分可用 | 默认长连接、密码/确认卡片、per-user owner 与普通自然语言对话主链已接通。同一用户按真实 `chat_id + thread/root_id` 累计 raw transcript，达到统一阈值后按 thread 自动 compact 并继续累计；旧聊天有 owner-local 检索投影。同一会话严格按序，不同用户/会话隔离；普通聊天不再自动注入旧 active task。当前新增链仍待 1.10 双用户真测，且尚未完成十万用户连接、限流、故障切换和长期运营验证。 |
| Gateway、持久请求、lease/recovery | 部分可用 | 普通用户默认 gateway 仍是本地文件事实源；scale profile 另有 PostgreSQL SKIP-LOCKED 队列、Redis 跨副本准入/租约和真实 Agent worker。目标集群故障切换与容量仍未验证。 |
| 子代理、任务账本、compact/resume、closeout | 部分可用 | 有正式运行链和大量回归；真实 Qwen 已证明双 runner 同时心跳、结构化取消和 PID 终止，且 timeout 不再被末拍心跳覆盖。takeover 控制面已由真实 TIMEOUT 源创建 replacement run；完成质量和最多 5 个长期并发仍不作规模承诺。GitHub API、PyPI、npm 三路真实保证档已完成单一连续段超过 24 小时的逐拍 proof。 |
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

### 2026-07-13 普通飞书对话与工作链发布收口

- 普通用户不需要触发词：聊天、做事、派工和定时都先进入同一条常规对话链，由模型按自然语言
  选择工具。只有显式 `/audit`、`/goal` 保留特殊模式；本轮没有扩展它们。
- 飞书入站把真实 `chat_id` 作为会话 ID，话题消息再叠加 `thread_id/root_id`；owner 用户身份仍是
  独立隔离维度。网关每轮先读同一会话最近的 user/assistant 历史，当前消息保持独立的
  `# User Task`，回答后将本轮双方消息写回；不会把旧任务目标包进当前消息。
- 网关准入新增同会话单飞：同一用户同一会话一次只执行一条，后一条必须等前一条回答落库；
  同一用户的不同会话仍受每用户/全局上限并行。
- per-user owner 默认开启；远程身份缺失或 owner agent 创建失败时以
  `OWNER_SCOPE_UNAVAILABLE` 终态拒绝，不会回退共享 main owner 串户。
- 普通对话只有在真实调用 `task_progress`、`create_subagents`、`wait` 等结构化任务工具时，才在内部
  绑定后台任务；用户无需知道 lane 或 `task_ref`。未绑定的新聊天只看到只读 active 候选，模型确认
  用户确实在续接时才用 `task_progress action=select` 选择；结构化交付收口后候选自动关闭。
- 会话任务使用两份非竞争索引：`task_ids` 是完整历史事实，供后台策略和审计精确读取；
  `active_task_ids` 只保存普通聊天可见的活跃候选。终态任务从热索引移除但不删除历史链接。子代理
  虽继承父任务的会话引用用于归档产物和进度，但它自己的收口无权关闭父会话任务；关闭入口按
  结构化 run source + 精确 task id 拒绝子任务关闭父链接；若子任务存在与自身 task id 完全相同的
  会话链接，则允许它关闭自己的 DONE 链接；子任务和 `bg-main-*` 内部链接即使处于 active/completed，
  也不会进入普通用户可选择候选。后台自动续跑也显式携带当前 thread/task link；只有其结构化
  closeout 通过后才把该任务从活跃候选移除，
  避免“已经交付却仍被定时器重复做”。最近完成的任务另以只读候选注入；用户自然语言明确要求
  继续/修改时，模型必须先用结构化 `task_progress select` 重新打开原工作区，误建的本轮任务链接会
  标为 `superseded`，普通聊天不会自动绑定旧项目。若同一会话已有候选而模型直接写新任务进度，
  结构化闸门会要求先二选一：`select` 续接或 `start` 新建；这不要求普通用户输入触发词。
  `select` 后本轮唯一当前工作区立即切到旧任务，后续所有工具参数中仍引用本轮占位目录的完整路径
  会在统一工具执行口改写到所选根目录，避免“进度账续上旧任务、文件却写进新目录”。
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
- Gateway、飞书回复与 assistant transcript 现在共用用户回复投影：前台完成轮和后台自动续跑轮的
  `MAIN_AGENT/RUN/SUBAGENT` 内部完成/运行协议都不得写入普通聊天；后台报告也只返回投影后的人话，
  完成协议里的服务器路径和验收字段只留作结构化机器事实。这样 compact 与旧聊天检索不会再把
  后台机器协议混进用户上下文。
- 模型已注册唯一通道无关的 `send_message`：收件人固定为当前 scoped owner，不让模型传任意飞书 ID；
  附件必须命中该 owner 的 task artifact registry，且文件仍在 owner 根内、状态 ready、hash 未漂移。
  assistant transcript metadata 保留最近产物引用；下一轮用户只说“发我”时直接复用并原生发送，
  不重新搜索、复制或生成文件。长任务的逐工具过程可由用户用 `/verbose` 显式开启；更高层、低频的
  长任务阶段汇报仍是后续体验项。
- 任务交付目录扫描会排除 `node_modules`、`.venv/venv`、`_deps`、`site-packages`、`.tox`、Python/test/lint cache 和 `.git`；这些运行文件
  不会占满有界 artifact 清单并把真正的代码/报告挤掉。该规则只影响交付识别和展示，不删除用户文件，
  显式写入记录仍保留审计事实。HTML 产物中的成对 Jinja/Django/ERB/JS 模板资源表达式延迟到运行时
  解析，不再被静态图片存在性检查误报；普通 `missing.png` 等真实静态路径仍严格校验。
- 默认规则改为随 wheel 发布的 `builtin:prompts/default.md`，不再依赖 systemd WorkingDirectory。
  owner 的 `AGENTS.md → SOUL.md → USER.md` 仍从唯一 owner 路径逐轮注入。USER 画像/偏好可由 Agent
  更新；SOUL/AGENTS 只能走 `update_persona`，飞书必须由发起人点击确认卡片后才写，基础文件工具、
  patch 和 owner-scoped bwrap shell 均不能绕过。
- Feishu 默认 `long_connection`、私聊密码锁默认开启；首次无密码时发设置卡但不吞掉第一条消息，
  只有设过密码且闲置超时后才拦截并要求解锁。显式配置可关闭密码锁或改用 webhook。
- 单机 Feishu 回调不再同步等待模型：提交 Gateway 后立即返回，pending/sent 回送记录持久化，后台
  worker 可跨进程重启继续轮询同一 request_id，超过旧 60 秒窗口仍送达真实结果且不重新提交任务。
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

### 2026-07-13 owner/thread memory + compact 当前工作树

- 实现与离线定向回归已完成，覆盖超过旧最近轮数仍累计、自动 compact、raw transcript 不丢、旧消息
  可搜索、owner LocalStore 隔离、per-thread verbose、typed progress、progress cursor 与最终回复不重跑。
- 参考范围与设计合同见 `docs/design/CONVERSATION_CONTEXT_DESIGN.md`。这里复用 通道运行时/长期助手 的
  “稳定 IM 会话键贯穿 compact/memory”和 per-session verbose 作用域，不复制它们的摘要算法或
  profile-wide memory 默认值。
- 这仍是当前工作树事实，不是 1.10 双用户 MiniMax 长任务、50% 自动 compact 或 90% 恢复已经通过的
  声明；完成部署和真实验收后才能更新为已证明。

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
  只有可解析结构化结果或已通过的 runtime delivery closeout 能进入成功态。
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
