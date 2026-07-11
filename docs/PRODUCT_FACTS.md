# 当前产品事实

更新时间：2026-07-10。本文是 `my-agent` 当前能力状态的唯一权威页；README、路线图和历史审计
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
| Feishu 接入、会话/身份边界 | 部分可用 | webhook/长连接和 owner 解析已有实现；尚未完成十万用户连接、限流、故障切换和长期运营验证。 |
| Gateway、持久请求、lease/recovery | 部分可用 | 普通用户默认 gateway 仍是本地文件事实源；scale profile 另有 PostgreSQL SKIP-LOCKED 队列、Redis 跨副本准入/租约和真实 Agent worker。目标集群故障切换与容量仍未验证。 |
| 子代理、任务账本、compact/resume、closeout | 部分可用 | 有正式运行链和大量回归；真实 Qwen 已证明双 runner 同时心跳、结构化取消和 PID 终止，且 timeout 不再被末拍心跳覆盖。takeover 控制面已由真实 TIMEOUT 源创建 replacement run；完成质量和最多 5 个长期并发仍不作规模承诺。GitHub API、PyPI、npm 三路真实保证档正在 24 小时 proof，目前只因时长不足未 proven。 |
| MCP stdio 工具 | 实验性 | 未声明工具默认 `dangerous` 并进入统一 effect/幂等/审批门；只有部署配置可逐工具声明更低 effect。当前 wheel 已由本地 Qwen 驱动 `@modelcontextprotocol/server-filesystem` 完成 bwrap/stdio 握手、14 工具发现和 `list_allowed_directories → read_text_file`；写工具仍在 client call 前被审批门阻断。主流 server 生态仍需扩大验证。 |
| 工具检索 | 部分可用 | 关键词与真实 embedding 语义通道已经接入同一混合检索器；工具向量按目录版本缓存，端点失败降级关键词并由 `list_tools.tool_retrieval` 暴露状态。动态 MCP/LSP 工具已有通用来源/用途元数据；57 工具的完整 `list_tools` 仍归档 107,039 bytes，模型侧只回灌 11,331 bytes 紧凑索引和恢复锚点。默认未配置 embedding model 时不会伪装成语义可用。 |
| ASGI、SQLAlchemy/PostgreSQL、RLS scale profile | 部分可用 | scale worker 已要求 PG/RLS owner manifest + versioned S3 objects，Pod 只用 emptyDir；无 S3/bucket versioning 时 fail-closed，真 PG+MinIO API 已验。目标 Kubernetes context 当前不存在，尚未做真实集群灰度。 |
| 扩展插件加载 | 实验性 | 唯一加载链接受管理员显式配置的已安装模块或 `my_agent.plugins` entry point；不扫描用户可写目录，缺失/重复/注册失败会阻断启动。尚缺第三方生态兼容矩阵。 |
| PTY 交互终端 | 部分可用 | POSIX 已有真实 PTY start/write/read/close、增量游标与有界缓冲，并复用 shell 路径、命令策略和 bwrap。当前 wheel 已在 1.9/1.10 由本地 Qwen 经 4000 真实完成 Python REPL 五/八轮操作并 close；Windows ConPTY 尚未实现。 |
| LSP | 实验性 | 已有管理员配置、惰性 stdio server、initialize/request/didOpen/diagnostics/shutdown 全链；路径限于工作区，多用户 server 经 bwrap。1.10 已用主流 `typescript-language-server 5.3.0 + TypeScript 5.9.3` 返回 TS2322/hover，并由本地 Qwen 两轮真实 LSP 工具账本复验；其他语言服务器兼容矩阵与长稳仍未验证。 |
| OpenAI 原生工具调用 | 实验性 | OpenAI-compatible `tools/tool_calls/role=tool` 的非流式和 SSE 分片链已接入统一 ToolSpec/IR；坏参数进入截断恢复。本地 Qwen 已完成三轮真实文件工具调用，但尚未扩大 provider/model、streaming 和长稳矩阵。 |
| Redis、OpenTelemetry、在线迁移 | 部分可用 | Redis Lua、OTLP exporter、迁移 1–10 和应用只读版本门已接主链；release channel、Gateway API canary、分析门、PG backup/隔离 restore 清单已存在。尚无目标集群 HA、collector 后端、真实流量灰度和灾备演练。 |
| 十万用户以上容量与可靠性证明 | 仅设计 | 尚无正式容量模型、SLO、压测、故障演练和长期异构来源运行证据。 |

## P0 已完成的冻结范围

P0 期间停止扩展新功能，只允许修复以下收敛项；该范围已经完成并进入远程 `main`：

1. 本事实页保持唯一权威，并同步 README、设计账本和测试入口。
2. 根目录 pytest 可直接运行；当前已知失败与默认配置漂移清零。
3. `ruff check agent_py_agent scripts` 清零；门禁报告必须区分 blocker 与 advisory。
4. MCP 工具不能伪装成只读绕过 effect gate。
5. 多用户 sandbox 不可用必须 fail-closed，不允许用户可见审批或宿主 fallback。
6. clean-package 必须发现未跟踪运行数据和真实制品污染。

P0 只说明当前底线可信，不说明十万用户规模已完成。P1 已进入远程 `main`；P2 的正式 scale 配置、
Redis、OTel、在线迁移和 RLS 主链已在当前工作树落地，但集群灰度、容量/SLO、灾备与长期真实来源
proof 仍未完成。

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
| 连续监控 proof | 正在真实运行 | 只认 wall-clock、连续采样、保证档、源新鲜度和异构签名；三路真实公网来源已开始 24 小时计时，当前不能写 proven。 |
| 十万用户 | 未证明 | 没有容量压测、SLO、故障演练、成本模型实测；状态仍为仅设计。 |

P2 实现已由 commit `6c00bf18` 提交；本发布事实同步随后一并推送到远程 `main`。

### 2026-07-10 P2 第二轮启动验收

- Kubernetes：stable/canary release channel 已进入队列事实，Gateway API 初始权重为 0，分析 Job
  检查 canary readiness、失败行与陈旧积压；本机没有 kube context，未应用到目标集群。
- 灾备：新增小时 PG dump、版本化 S3 owner objects 和隔离 restore Job；尚未在目标账户执行恢复演练。
- Owner 存储：worker 的 RWX PVC 已替换为 emptyDir cache + PG/RLS manifest + S3；真 PG/MinIO API
  两文件恢复和跨租户 0 行通过。monitor watch/proof 仍使用 RWO 状态卷。
- 连续 proof：已修复真实 home 深递归、harvester sidecar 重复计数、历史成功冒充健康三项失真；
  LaunchAgent 正在运行，未满 86400 秒前保持 `duration_too_short`。

本节实现已由 commit `7b6146a3` 提交；本发布事实同步随后一并推送到远程 `main`。24 小时 proof
仍在运行，提交不改变它的未完成状态。

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
- 24 小时 proof 在 2026-07-11T06:00:00Z 左右的复核为 75,914 秒、3 路健康、3 个异构签名；仍未满
  86,400 秒。真实运行发现的
  response-body 读取超时已收回公共 fetch runtime，monitor 重载跨拍间隔 23.051 秒，stderr 未再增长。
- 后续两机同时经 4000 的真实 Qwen 调用均完成。LAN 地址漂移暴露出旧端点会占满 600 秒的问题；
  当前 provider transport 已把 connect 默认限制为 10 秒，并保留长 reasoning 的 600 秒 read/request 窗口。
  旧地址在 10 秒失败、正常请求持续约 7–8 分钟后成功，仍需 DHCP reservation 或受管 DNS 消除地址漂移。
- runner-session 心跳不再每 5 秒触发完整 workspace/compact/projection 保存；takeover replacement 现在从
  `context_bundle.takeover` 直接获得有界结构化来源 handoff，不再被提示去用普通文件工具读取受管状态面。

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
