# 决策模型 P1—P5：最终交接（2026-09-25，按 HANDOFF_TEMPLATE 格式）

## 基本信息

- workstream：接入决策模型（P1—P5，GOAL 18 项）。唯一持续更新的 TODO 是 [DECISION_MODEL_GOAL.md](DECISION_MODEL_GOAL.md)，[09-23 暂停快照](DECISION_MODEL_TAKEOVER_HANDOFF.md)只作历史参考。
- branch：决策线只提交短分支 `claude/decision-*`，由主线 owner（模块重构线）合入并部署。本次更新时 main 为 `6a50d84aa`，双机运行时为 `runtime-step11f-c6ab15e7`；本交接随分支 `claude/decision-action-candidate` 合入，之后以 Git 为准。
- worktree：临时 worktree，合入后清理。真实验收只在决策线测试机的隔离目录 `decision-acceptance-*` 和唯一测试端口 8431 上做。唯一例外是动作候选：它需要浏览器而该测试机没有 Chrome/Chromium，改在本机隔离临时目录跑（独立 `MY_AGENT_HOME`，隔离 Gateway 仍用 8431），本机 8420 与日常数据未动。
- owner：决策线（Claude 会话）；合并与部署：主线 owner。
- date：2026-09-25。

## 本线目标

给 my-agent 加一个可选的"决策模型"（Jev）：在选模型、召回记忆、推荐工具、子代理派工、整理记忆等节点上给出很短的建议。用户可以逐点打开、只观察或关闭，也能调整等待时间。超时、没额度、出错时一律自动回到原来的做法，不拖垮主流程、工具或记忆链路。整条线复用原有的配置、授权、取消、调用账本、子代理、记忆和上下文机制，不迁入外部 agent 框架。

## 实际完成

- 18 项全部完成，细分清单全部勾选，Goal 已标 complete。2026-09-25 做过一次对账，第 07、08、12 行标完成时漏勾的 P2-B/D/F、P3-E、P4-F 都已对照证据补齐，说明见 GOAL。
- 第 15 项最后一块动作候选 `action_candidate` 已完成：插件线落地[观察候选结构](../design/PLUGIN_OBSERVATION_CANDIDATES.md)后，决策侧改用真实合同，并在隔离 owner 上用 browser-lite 做了 off/observe/apply/过期四档[真实验收](DECISION_MODEL_REAL_VALIDATION.md#15-动作候选-action_candidate-的-browser-lite-真实验收2026-09-25)。真实运行先暴露宿主不给只读工具写完成事件的缺口，插件线已在 main `6a50d84aa` 修复。
- 官方 Jev 按尝试累计 **113 次**，全部在隔离目录与唯一测试 Gateway 8431 上完成。日常 8420、用户真实 owner 和日常模型从未改动，每次测试后都经原 CAS 恢复决策设置。
- 凭据：Jev 凭据只在隔离私有模型目录；语义召回的嵌入密钥只由启动器注入测试 Gateway 进程环境，YAML 只写环境变量名；仓库、日志与本文件都不含密钥。

### 18 项状态

| 项 | 状态 | 结论与边界 |
| --- | --- | --- |
| 01—06 / P1 | ✅ | 配置、原生协议、有界等待、设置服务、账本、首条决策链 |
| 07 / P2-A/B | ✅ | 子代理创建前选模：自动核验并采用；模态按保守规则，带图片不换模型 |
| 08 / P2-C/D/E/F | ✅ | Curator 前置标注：本地组合加 2026-09-25 真实 Curator 样本 |
| 09—11 / P3-A/B/C/D、P4-A/B/C | ✅ | 召回重排与复用、能力推荐与插件减量、设置入口与代操作 |
| 12 / P3-E、P4-D/F | ✅ | 窗口、缓存与并发故障；12.1—12.7；Gateway 关闭取消与 Curator/插件点并发组合（`25650830d`） |
| 13 / P4-E/G | ✅ | 真实 Jev + MiniMax TUI 验收与 [P4-G 汇总报告](DECISION_MODEL_P4G_REPORT.md)；真实额度耗尽只由离线矩阵覆盖 |
| 14 / P5-A/B | ✅ | 语义召回下补充查询有真实收益（3 条已知漏召回中 2 条稳定补回）；关系提示让真实提取把更新类事实归入原条目 |
| 15 / P5-C | ✅ | 外部材料顺序、Todo 规划、交付复核焦点、自学习 S1/S2、动作候选六点都已接并真实验收；都没有业务收益结论 |
| 16 / P5-D | ✅ | 主会话真实跨模型采用（长资料任务采用 M3 并答对），普通短任务两次保留当前模型 |
| 17 / P5-E/F/G/H | ✅ | 授权、经验输入上界、发送硬门、按实际结算、E2 记录、F1 评估与授权内晋升；拒绝与正向晋升都有真实样本 |
| 18 / 最终集成 | ✅ | 全仓回归 21,406 passed，4 个失败全部归因；本交接 |

### 证据类型汇总

按 GOAL 要求把几类证据分开写，不混成"整体通过"。"—"表示该类证据不适用或没有。

| 范围 | 源码与合同 | 离线（fake / 本地 HTTP） | 真实接口（官方 Jev） | 实际 TUI | 模型判断质量 |
| --- | --- | --- | --- | --- | --- |
| P1 配置、协议、期限与故障隔离 | decision 用途 schema、Jev 协议严格校验、设置 CAS、原账本接线 | 本地 HTTP 组合；故障矩阵八类（连接被拒/DNS/TLS/5xx/慢响应/429 额度/429 限流/402 计费） | 首批真实记录；多 owner 断网、挂起、瞬时故障与退避复测 | 用量行显示决策输入 | — |
| P2 子代理选模 | 候选冻结、幂等、提交原因码、模态保守闸 | 容量门与跨窗口组合；带图片 child 的反例测试 | 六轮派工 20 次 | 六父任务十 child 全部完成 | 自然选中 M3、受限候选选中 DeepSeek 各 1 次；样本小，不代表选中率 |
| P2 Curator 前置标注 | 临时注释不落盘，逐题独立 | 父侧组合与 lease 头寸 | 真实 Curator 五次运行（Jev 4 次） | 素材经 TUI 生成，点本身在后台 | 16 题全部是合法回答；没有提取质量收益证据 |
| P3 召回重排与复用 | 原权限/来源内排序，失败回原序 | 父侧组合 | 无单独真实样本 | — | 无 |
| P3 能力推荐与插件减量 | 快照内短名单，不扩权 | 10.1—10.4 组合 | 首批 8 次与 Gateway 回合 | 集成安装版 off/2 秒/4 秒对照 | 改成每候选一题后 4 个 include、4 个 not_needed 全对；该样本总输入没有节省 |
| P4 设置入口与代操作 | 设置服务、原菜单、user_config | 243 项组合 | 普通中文 read→thread patch 一条 | 原 TUI 菜单 | — |
| P3-E/P4-D/F 窗口、缓存与并发 | 两层容量门、稳定前缀、关闭取消原语 | 12.1—12.6；Gateway 关闭取消与并发组合 | 12.7 缓存读回；准备阶段压缩真实触发 | 12.4 真实 TUI 压缩 | 缓存回执存在，不证明净收益 |
| P5-A 召回前补充 | 只追加、不跳过原召回 | 136 项 | 语义召回 6 轮 apply | 真实 TUI 对照 | 3 条漏召回中 2 条稳定补回，1 条稳定选错片段 |
| P5-B 记忆关系 | 提示只进临时 prompt | 组合测试 | 12 对短样本与真实 Curator 2 对 | 素材经 TUI 生成 | 预设三类关系标对 5/6；更新类归入原条目 2/2 |
| P5-C 外部材料、Todo、交付复核 | 只追加软提示 | 各自组合与变异 | 外部材料 2 组、Todo 3 次、交付复核 1 次 | 交付复核 2 个任务 off/apply | 链路成立；都没有业务收益结论，交付复核 4 轮均未实际提示 |
| P5-C 自学习 S1/S2 | 提案只经用户 CLI 确认；`record_lesson` 结构化来源 | 组合与变异 | S2 真实 4 次 | 端到端：真实子代理 lesson→提案→确认→新会话可见 | S2 只给出 normal，排序是否有帮助没有证据 |
| P5-C 动作候选 | 只选宿主铸的候选、只追加 ID 与 role；新鲜度只问插件线权威 | 82 项、19 种变异全杀；集成用例走真实写入口与真实新鲜度 | 6 次（修复前临时构建 3、合并后 3） | browser-lite off/observe/apply/过期四档 | 6 次都选输入框；同一请求下 apply 时主模型改用提示的候选 ID；样本小，没有质量结论 |
| P5-D 主会话选模 | 新工作片边界、发送前 CAS | 本地组合 302 项 | 8 个真实样本 | 真实 TUI 会话 | 修正后跨模型采用 1/1 答对；短任务 2 次正确保留 |
| P5-E/F/G/H 有预算自测 | 授权信封、发送硬门、按实际结算、CAS 晋升 | 组合与变异 | 授权发送 2 次；E2/F1 实验回合 7 轮，另有晋升后普通回合 1 轮 | 真实 TUI `/experiment` | 结算额等于计费；晋升只看召回与节省，不评模型质量 |

### 本接手期间合入 main 的决策线工作（节选，按合入顺序）

| 合入 | 内容 | 真实验收 |
| --- | --- | --- |
| `d69f30cf3` | 设置无锁读取已提交版本；HTTP 故障矩阵 | 并发会话不再互挤 |
| `3933b1db1` | 连续失败翻倍冷却 | 挂起复测通过 |
| `42f7e57e1`、`6b48b5270` | 子代理提交原因码；压缩百分比只接受真实生效值；准备阶段压缩真实样本 | 第 12 项收口 |
| `4ec0e11f3`、`92ce09fb5` | 模型用途标签；采用模式问题说明 | 真实跨模型采用成功 |
| `bd4bca4d8`、`ab23a2666` | 能力推荐观测写进请求记录；召回来源写进上下文包 | 支撑后续实验 |
| `e9ead5ae3`、`1132fd9d0`、`52e0190e1` | 自学习 S1、S2 审核顺序、子代理结构化 lesson 通道 `record_lesson` | 端到端真实验收通过 |
| `8c6d29c5f` | P5-A/B 真实收益记录、P4-G 报告、TUI 菜单跟随 schema、交付复核焦点 | 真实实验 |
| `e489ffff0`、`3da83ca72` | 验证分类：cd 前缀、管道与后台、126/127、pytest 范围、`&&` 串联 | 真实样本暴露后修复 |
| `dfa8e498b`、`5fca6a194`、`0bb6f3a88` | `/experiment` 授权入口、经验输入上界、发送硬门；回执显式参数 | 结算额等于计费 |
| `d9a34fc76` | 主会话短任务保留样本 | — |
| `84d873c9b`、`fb5be8262` | E2 对照记录、F1 评估与授权内晋升；正向晋升真实样本 | 拒绝与正向晋升都成立 |
| `1b334200e`、`6c3ffc2ad` | 插件观察候选结构设计稿及评审结论 | — |
| `25650830d` | Gateway 关闭时主动取消在途决策；Curator 与插件点并发组合 | 本地真实传输栈 |
| `b0e79ec57` | GOAL 细分清单对账；整理标签 `curator` 真实 Curator 样本；本交接按模板重写 | 真实 Curator 五次运行 |
| `761ef2ab2` | 线程中断标志绑定立旗时的线程对象，ident 复用不再误取消新线程 | 原先稳定复现失败的 63 文件组合转为全通过 |
| `6a50d84aa`（插件线） | 无门工具也写 `tool_completed` 事件；事件窗口保留最新行 | 由决策线真实链路暴露 |
| `claude/decision-action-candidate`（随本交接合入） | 动作候选 `action_candidate` | browser-lite 四档真实验收 |

## 改动文件

按领域列关键入口，逐片细节见 [TESTS](../../TESTS.md) 与 [CODEBASE_TREE](../../CODEBASE_TREE.md)。

- 协议与传输：`agent/backends/decision_protocol.py`、`typesafe_decision.py`、`typesafe_decision_wire.py`、`provider_send_gate.py`。
- 决策服务：`agent/conversation/decision_service.py`、`decision_policy.py`、`decision_model_call.py`、`decision_send_permit.py`、`decision_experiment.py`、`decision_experiment_evaluation.py`。
- 设置：`agent/settings/decision_settings.py`、`decision_settings_schema.py`、`decision_settings_defaults.py`、`decision_experiment_schema.py`、`config.py`，以及 `config/agent_config.yaml`。
- 接入点：
  - 子代理选模：`agent_core/orchestration/decision_subagent.py`、`agent_core/subagent/model_selection.py`；
  - Curator 标注与记忆关系：`memory_store/decision_curator.py`、`decision_curator_relation.py`；
  - 召回：`memory_store/decision_recall.py`；
  - 规划：`agent_core/decision_planning.py`；
  - 能力推荐：`capability/decision_recommendation.py`；
  - 外部材料、交付复核与动作候选：`agent_core/tool_context/external_material_order.py`、`decision_delivery_quality.py`、`decision_action_candidate.py`；
  - 自学习：`capability/skill_proposals.py`、`decision_skill_proposal_review.py`、`subagents/lesson_ledger.py`、`agent_core/runtime/record_lesson_tool.py`；
  - 主会话选模：`gateway_model_adoption.py`、`gateway_model_observation.py`；
  - 自测与调参：`gateway_parts/request_experiment.py`、`request_experiment_records.py`、`request_experiment_promotion.py`。
- 界面与收尾：`cli/chat_parts/tui_decision_menu.py`；`cli/gateway_process.py` 只加了一行关闭取消（主线 owner 同意）。
- 验证账（交付复核样本暴露后修）：`verification/project_facts.py` 等。

## 测试命令和结果

远端提交前的严格门（每片都跑，并带上架构守卫测试）：

```bash
python3 -m pytest <与改动直接相关的 test_*.py> agent_py_agent/tests/test_architecture_guardrails.py -q --tb=short
ruff check agent_py_agent scripts
python3 scripts/check_doc_sync.py
python3 scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json
git diff --check
python3 scripts/check_clean_package.py .
```

结果：

- 收尾全仓回归（main `5fca6a194`）：21,406 passed、4 failed，全部归因（1 个由决策线修复，2 个由插件线修复，1 个为负载下时序偶发）。
- Gateway 关闭取消与并发组合：63 个决策/Gateway 相关文件 1428 passed、1 failed；14 种变异全杀；代码体量与 main 相比无新增项。
- 那个失败是 `test_subagent_first_request_selection.py` 真实 child 用例的第一组参数，根因是线程中断标志按 ident 记、线程退出后被复用的 ident 继承了旧旗。已由 `761ef2ab2` 修复：同一组合 1429 passed、0 failed，中断与有界调用测试族 760 passed。
- 动作候选（rebase 到 main `6a50d84aa` 后）：57 个文件 1530 passed、1 xpassed（既有）；19 种变异全杀；代码体量与 main 相比无新增项。
- 真实验收全部记录在[真实验收](DECISION_MODEL_REAL_VALIDATION.md)；测试机证据在隔离目录 `decision-acceptance-20260923/*`、`20260924/*`、`20260925/*` 的 `artifacts/` 与 `wire*/`。已完成目录的 venv 已删除，复现时用目录内的 wheel 重建。动作候选的本机结构化证据在仓外证据目录，隔离 home 与私有模型目录副本已删除。

## 影响范围

- 所有决策点默认关闭。关闭时零请求、零准备，行为与未接入前等价。
- 用户可见的新入口：TUI 决策设置菜单、`/experiment` 授权命令、`my-agent skills proposals` 审核命令；子代理多了一个专属工具 `record_lesson`。
- 每个接入点都有 AgentConfig/YAML 三个字段（mode、timeout_seconds、profile_id），与设置服务共用一份来源。
- Gateway 停止时会主动取消本进程在途决策，等待中的调用立即回到原方案。

## 需要主线重点复查

- `cli/gateway_process.py::_cmd_gateway_run_cleanup` 中决策取消那一段：必须保持在置位停止事件之后、停 HTTP 之前，并继续被 try/except 包住。
- `tooling/registry.py` 的 `_DEFAULT_HIDDEN_TOOL_NAMES` 里的 `record_lesson`：它保证主线程看不到这个子代理专属工具。
- 动作候选与插件线的三条接口约定（新鲜度函数签名、`actions` 用宿主注册名、ID 形状）已按落地提交逐条对照，形状规则直接复用插件线定义；`_optional_result_hints` 里三个点按结构化触发事实互斥。

## 需要其他线协调

- 插件线：
  - 动作候选已接入并真实验收。插件线后续：browser-lite 的相对路径按宿主 `workspace_root` 解析，`file://` 与本机 http 被宿主 URL 参数门先拦，工具描述与宿主门需要对齐（已记入观察候选设计第 6 节）；
  - 停止时未结束模型调用的结构化"被中断/未结算"结清已落地（`db4d46398`）；runner worker 账本的同类结清由主线跟进。
- 主线：Memory v2 迁移把新 owner 的模板标题生成待审候选（主线已记待办）；模型经后台进程起的 http.server 监听所有网卡，Gateway 停止后仍在运行（已登记台账，待主线评估）。

## 剩余风险

- 判断质量的证据都是小样本、自建中文任务，不能外推为线上整体质量。多数点只证明链路成立，没有业务收益结论。
- 真实额度耗尽没有样本，只由离线矩阵覆盖。
- 两处保守取舍：采用前复核按整份策略版本判断，owner 级任何设置改动都会让同 owner 其他点的在途建议作废；冷却按连接共享，后台超时会让同连接的前台点在冷却期直接保留原方案（2026-09-26 已修订：超时只冷却本点位，连接错误才冷却整条连接，见 `docs/design/DECISION_MODEL_INTEGRATION.md`）。两者都只会少一条建议。
- 模态只按保守规则处理：带图片的子任务不会换到能看图的候选。
- 自学习：被取消的 run 不收取 lesson 账本；S1 草稿的场景标签措辞不准；S2 的 CLI 进程各自持有冷却表，含查询串的 URL 仍会外发。
- 决策实验自动晋升后，TUI 没有主动提示。
- Gateway 停止时，runner worker 账本里的在途调用还没有同类结清（主线跟进）。
- 动作候选只用 browser-lite 一个插件、一张简单测试页验证过；OCR/computer_use 还没有声明观察候选。工具描述让模型以为能用 `file://`，真实样本里两次被宿主拦下、页面没打开。

## 后续建议

1. **（已完成）动作候选**：已接入并真实验收，Goal 关闭。插件线的 browser-lite 地址与宿主门对齐是独立后续，可并行。
2. **（已完成）测试顺序问题**：根因是线程中断标志随 ident 复用，已由 `761ef2ab2` 修复并部署。
3. **按收益处理已知缺口**：先做 P5-A 补充片段材料和记忆关系对按相关度挑选（两点都有真实收益证据），再做自动晋升提示和 `record_lesson` 的小缺口。
4. **风险边界**：所有决策点保持默认关闭；不从自然语言做机器判断；不加专项分支；远端提交前跑本地严格门并带上架构守卫测试。
