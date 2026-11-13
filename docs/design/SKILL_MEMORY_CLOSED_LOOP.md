# Skill 与记忆闭环（skill-memory-closed-loop）

状态：W0/W1/W2/W4/W5/W6/W7/W8 已在集成分支 `sm/integration-20261113` 合入并各自通过切片门；W3（组合根 live 接线、后台总结第二通道）与 W9（`skill_feedback` 四动作、`self_map` 遥测）仍在开发，合入后由 W10 Stage B 补齐组合根全链路测试与本文对应小节的验收结论。本文是这次闭环改造的唯一模块设计；`DESIGN_LEDGER.md` 只保留摘要和链接，S3 自动总结的原始设计见 [自动总结 Skill](SKILL_AUTO_SUMMARY.md)。

## 1. 背景与用户决定

改造前的自学习链路有几个缺口：默认关闭、维护默认只观察；50 条上限只限新建、不会整理腾位；使用统计把搜索展示也算作使用，没有按版本、按任务去重；回滚目标可能是从未被证明有效的试用版；lesson 与 Skill 可能对同一条经验各发一份；召回路径还会整库读取长期记忆。

用户的锁定决定（R-1..R-7）与 V2 补充（V2-1..V2-5）：

- 出厂即自治：`enable_self_learning=true`，`self_learning_maintenance_mode="autonomous"`，不设观察期。用户随时可以改回 `false` 或 `observe`，下一次操作即生效，无需重启。
- 每个 owner 最多 50 条在役的机器托管 Skill；内置、共享、插件、工作区、owner 手写和用户改过的 Skill 一律不计入、不被自动动。
- 新 Skill 与已有托管 Skill 的相似度达到 70% 时先尝试合并（同一请求最多重试一次），仍判为同族重复就拒绝，不悄悄多发一条。
- 负面反馈零打扰：用户往往不知道用了哪个 Skill，因此不依赖人工评分，由三个通道自动归因，同一任务最多记 1 击。
- 质量阶梯 3/4/5：无负面时需 3 次不同任务的有效使用确立；第 1 击清零并要求 4 次；第 2 击清零并要求 5 次；第 3 击立即回滚到已证明有效的旧版本，没有就退役并阻止同名同族再生。
- 闲置 7 天冷藏、累计闲置 15 天归档、归档后再过 15 天清除；可在宽限期内恢复。
- lesson 归档永久保留在磁盘上，不按年龄删除。
- 自动流程不再弹出人工确认；反馈只在主会话可用，子代理不能调用。

`AGENTS.md`/`SOUL.md` 仍只能由当前用户当场修改或确认，本次改造不改变这条边界。

## 2. 出厂默认值与配置

全部沿用既有配置权威：`agent_config.yaml`（中文注释）→ `AgentConfig` → 规范化 → `parameter_registry.py`（可调键、生效方式）→ `live_switches.py` 文件 stat 变化即重载。主会话可经 `user_config` 工具或 `/settings` 调整，子代理不能修改自学习开关、也不能删除或停用 Skill。

| 键 | 默认 | 含义（`0` 的语义） |
|----|------|------|
| `enable_self_learning` | `true` | 自学习总开关；关闭后自动写入全部停止，人工显式操作仍可用 |
| `self_learning_maintenance_mode` | `autonomous` | `observe` 只记账和推演，不搬目录、不删提案 |
| `self_learning_max_skills` | `50` | 每 owner 在役机器托管 Skill 上限；`0` 不限 |
| `self_learning_recommended_limit` | `50` | 默认目录推荐条数；`0` 不限，不等于所有权上限 |
| `self_learning_recommended_explore_percent` | `20` | 推荐名额中留给“确证从未使用”新条目的比例（默认 50 条中 10 条） |
| `self_learning_merge_similarity_percent` | `70` | 合并判定的 Jaccard 阈值；`0` 关闭合并策略 |
| `self_learning_keep_versions` | `5` | 每条保留的历史版本数；当前版、已证明的回滚目标、在途引用与未完成意图始终保留 |
| `self_learning_cool_seconds` | `604800`（7 天） | 闲置冷藏；`0` 关闭冷藏 |
| `self_learning_archive_seconds` | `1296000`（15 天） | 累计闲置归档，不依赖冷藏是否开启；`0` 关闭 |
| `self_learning_evict_seconds` | `1296000`（15 天） | 归档后的宽限期，到期物理清除；`0` 永久保留归档并且不过期 |
| `memory_lesson_archive_days` | `15` | lesson 未被路由且未命中多久后归档；`0` 关闭 lesson 归档（索引与热度修复照常） |

周期复用 `memory_curator_interval_seconds`（默认 10800 秒），单轮工作预算复用 `retention_scan_slice_entries/bytes/ms`（128 条 / 8 MiB / 25 ms），不新增维护间隔键，也不新增任何缓存、索引或有界扫描的布尔开关。

自动破坏性计时器在总开关关闭或 `observe` 模式下的有效值为 `0`；状态读口同时给出配置值与有效值。

## 3. 权威位置

| 事实 | 唯一权威 | 说明 |
|------|----------|------|
| 自学 Skill 归属、生命周期、计数投影 | `<owner_home>/data/skill_learning/registry.json` | schema 仍为 v1，新增字段都有默认值；`pending_operation` 记录进行中的转换 |
| 使用明细与汇总 | `data/skill_learning/usage_ledger.jsonl`、`usage_summary.json` | 不另建 `data/skill_usage` 镜像；汇总完整时明细轮转不算丢失生命周期数据 |
| 精确去重回执 | owner 既有 `runtime.db` 的 metadata 命名空间 `sm-use:v1:` / `sm-use-total:v1:` / `sm-hurt:v1:` | 唯一计数来源；不新建数据库或表 |
| 在役正文 | `<owner_home>/skills/learned/<name>/SKILL.md` | 只由登记表判定是否自学 |
| Skill 归档 | `data/skill_learning/archived/<name>/SKILL.md` | 宽限期内可恢复 |
| lesson 正文 | `<owner_home>/memory/lessons/<canonical>.md` | 正式正文权威 |
| lesson 归档 | `memory/lessons/archive/YYYY-MM/<canonical>.md` | 永久保留，活跃扫描不进入 |
| lesson 热度 | `memory/routing/lesson_heat.json` | 可重建投影，不是正文权威 |

两个序号域互不比较：`registry.usage_watermark` 是 JSONL 明细序号，`maintenance.usage_receipt_sequence` 是 runtime 聚合序号。`OwnerReadIndex`、目录投影、`lesson_heat` 都是可重建视图，不能作为授权或去重依据。

运行库提供方是晚绑定、按执行模式区分的宿主回调：显式 `LOCAL_UNMANAGED` 即使磁盘上残留 `runtime.db` 也不读写；只读路径从不创建数据库；缺少运行库时精确覆盖标为不可用，不伪造为零。

## 4. 托管资格与容量准入（W1）

“托管”只认一次归属核验：`layout=learned`、规范 owner 根、正文 hash 与登记一致、没有交给用户。计数、换出选择和实际搬迁共用同一判据。归属不明时抛 `SKILL_LEARNING_EVIDENCE_INCOMPLETE`，不返回不完整的列表冒充完整。

发布与恢复在同一个 owner 配额 → state 锁事务内完成最终准入、换出和新条目写入：

- 换出顺序：确证从未使用（`ever_used=False` 且覆盖 `exact`）按 baseline 由旧到新；其次已冷藏条目按最后真实使用、独立使用次数排序；最后其它在役条目。使用情况未知的不算“从未使用”。
- `SKILL_LEARNING_LIMIT_REACHED` 只表示证据完整且所有名额都受保护；证据不全和 `observe` 拒绝各有独立结果码。
- 容量换出获得完整的 15 天归档宽限，因此 30 天总计只适用于普通闲置路径。
- 闲置清除不加黑名单；用户删除和第三击退役会把名字与任务族指纹加入阻止名单。

每个会搬目录的转换（归档、恢复、换出发布、清除、删除、回滚）都先在登记表里持久化 `pending_operation`（身份、期望 revision/hash、源/目标路径、清理路径、阶段），再执行搬迁。进程在任一单点退出后，下一次维护或发布入口先恢复；条目行被删除后清理身份仍保留，直到版本与回执清理完毕。

## 5. 使用回执与质量阶梯（W2）

- 新的使用事件携带宿主实际交付的正文 hash、incarnation/activation、strike epoch、规范任务键和正文字符数。`manual_get` / `host_injected` 只有 `body_chars>0` 才算真实使用；搜索展示和能力包不计入。
- 每次真实使用在同一个 runtime 事务里写入 `sm-use` 回执并递增聚合，同一任务同一激活只计一次；登记表只是投影，可按聚合序号补齐。
- 负面回执键为 `sm-hurt:v1:<incarnation>:<activation>:<sha256(任务键)>`，任务键依次取 `root_task_id`、`task_id`、`request_id`，都没有时才用认证回执编号。三个通道在同一任务上对同一激活最多记 1 击：
  - 通道 A：主会话模型在已交付正文的 Skill 导致返工或用户不满时调用 `skill_feedback(action="ineffective")`（W9）；
  - 通道 B：后台总结判定已交付 Skill 明显误导执行时输出 `decision="ineffective"`（W3）；
  - 通道 C：包检查确定性失败且该任务有采用声明或正文交付回执时自动归因（W2）。网络、超时、限流、环境类错误不归因。
- 阶梯严格为 0 击需 3 次、1 击清零需 4 次、2 击清零需 5 次；第 3 击立即回滚到已证明有效的旧版本（新激活、计数清零、`quality_basis=rollback_established`），没有已证明版本就退役并阻止同名同族。第 3 击的 SQL 提交本身就是恢复触发器，进程在保存后续状态前退出也能在重启后继续完成，且不会记第 4 击。
- 只有带 `established_at` 证明的版本才能成为 `previous_stable`；旧数据里未证明的指针保留正文但不作为回滚授权。

## 6. 有界维护与保护（W2）

- 一次维护按“恢复 → 使用折叠 → 归属核验 → 生命周期与容量 → 版本清理 → 回执清理”推进，所有阶段共用同一份条数/字节/时间预算，用尽时把游标写入 `maintenance_cursor`，下次准入继续。坏的首条记录不会饿死后续条目。
- 保护只读活跃索引：未结任务、运行中的子代理和未删除的调度引用；不再全量列历史线程。任何截断或读错都返回 `protection_complete=False`，此时破坏性动作全部暂停。
- 时间预算只阻止领取下一项，不能中断已进入的文件系统调用，因此不承诺硬性毫秒上限。
- 恢复动作重置闲置基线但不伪造使用事件；归档内有额外用户文件或 hash 不符时拒绝恢复。

## 7. 目录推荐与语义指纹（W4）

`recommended_learning` 先放固定条目，再按 20% 预留探索名额给确证从未使用的新条目（新者优先），其余按“已确立、最近真实使用、独立使用次数、名字”排序，两类名额不足时相互补齐。

目录快照的指纹只覆盖语义事实：布局、名字与 hash、版本与冻结正文、启用状态、实际推荐成员、隐藏的待导入名、宿主使用绑定（incarnation/activation/strike epoch）；当前推荐上限与探索比例并入缓存键。纯计数或时间戳变化不重建快照；跨过推荐边界才重建，并且只失效该 owner 的投影，不清内置/共享的进程级缓存。stat memo 沿 `resident_owners` 配额有界，坏文件从不缓存。私有使用绑定放在既有 `SkillSnapshotEntry.asset_fields["_learning_usage_binding"]`，不出现在模型可见字段里。

## 8. 互斥载体与提案卫生（W5、W8）

`lesson_carrier(candidate)` 用既有过程校验器只看候选原文：通过过程结构校验的走 Skill 学习 FIFO，其余留在 lesson 路径。子代理结果服务和记忆晋升的预检与提交都调用同一个纯函数，因此同一条经验不会既发 lesson/HOT 又发 Skill；过程性候选在记忆侧以 `DELEGATED_TO_SKILL_CARRIER` 撤回，不弹人工确认。显式的 S1 确认/拒绝 API 与已安装的提案保持兼容。

过期提案按 `prune_learning_proposals` 有界清理：只删除未排队的过期候选与已结束的提案记录，保留已接受资产；S1 目录锁先释放再取学习 state 锁，不反转锁序。

## 9. lesson 生命周期与有界召回（W6、W7、W8）

- 同一 `subject_key` 与规范 scope 只有一条活跃 lesson：内容相同复用，内容不同冲突，带精确 revision 的替代生成 revision+1 的继任者，旧条目标记 `superseded` 并永久保留正文；对应的旧 HOT 退役，不把旧规则抄给新内容。
- 新增、替代、归档与 HOT 晋升共用路由索引锁作为生命周期锁；锁内只做精确目标与 HOT 元数据重读，固定引用在每次搬迁前于锁内重读，读不完整就推迟归档。
- `LessonRepository.read_refs` 按规范引用逐个 nofollow 读取，零引用零打开；召回只按已路由引用读正文，不再列目录。命中先在内存累计，维护窗口合并进 `lesson_heat.json`，并发新增的命中不会被清掉。
- `JsonlMemory.read_ids` 复用读索引按 ID 取回，代替决策召回后的 `memory.all()` 刷新；`scope_pairs` 是按权威 generation、墓碑和最近过期时间失效的有界 LRU 投影。目录或分段被手动删除时静默降级，不抛致命错误。
- 记忆整理器的 `run_maintenance_if_due` 不依赖辅助模型是否可用、也不依赖对话轮数阈值，按既有间隔运行 lesson 维护；`partial` 覆盖不推进节流时间戳。整理器校验把 lesson revision 绑定到候选，仅在宿主核验的精确版本下自动执行 lesson 替代/合并。

## 10. 遥测（W9，待合入）

`self_map` 通过既有 `SectionProjection.counts` 与条目元数据报告 `SkillTelemetry`/`MemoryTelemetry`：当前生效规则、在役/冷藏/归档计数、`user_owned_exempt_from_cap=true`、`shared_exempt_from_cap=true`、`required_uses_formula="3 + hurt_count"`、`retire_at_hurt_count=3`、`archive_auto_delete=false`。计数未知时为 `null` 并附原因与覆盖，不当作 0；遥测不递归扫描归档、不构造 owner、不读正文。

## 11. 规模与故障模型

验收以工作计数与受控夹具为准，按“5,000 owner、每 owner 约 275 条历史自学 Skill 与 216 条 lesson、长驻进程、单点故障”核算：

- 冷 owner 不实例化 Agent；单 owner 保护只读活跃索引。
- 275 条历史自学 Skill 在有界多轮维护中收敛到 50 条，保留已知使用与全部豁免层；每轮状态变更不超过批量预算。
- 零引用零 lesson 打开；按 ID 刷新的读取量与总历史无关；scope 投影命中不重扫。
- 每个转换边界单点进程退出都能前向收敛；不串联多个独立系统调用连续失败来构造场景。

本次没有执行 5,000 owner 真实流量压测，也没有执行 365 天连续运行试验；这里只声明已执行的计数与夹具验证。

## 12. 测试

| 文件 | 覆盖 |
|------|------|
| `test_sm_w0_live_policy.py` | 默认值、可调键、主会话可调/子代理拒绝、live 重载 |
| `test_sm_w1_store_transitions.py` | 托管判据、容量准入与换出、六类转换的子进程崩溃恢复、显式动作 |
| `test_sm_w2_usage_strikes.py`、`test_sm_w2_bounded_maintenance.py` | 精确回执、3/4/5 阶梯与三通道去重、活跃索引保护、80/20 推荐、7/15/15 生命周期、游标维护 |
| `test_sm_w4_catalog_projection.py` | 语义指纹、推荐边界、owner 局部失效、有界 memo、绑定私密性 |
| `test_sm_w5_carrier_hygiene.py` | 互斥载体分类、提案有界清理与锁序 |
| `test_sm_w6_lesson_repository.py` | lesson 身份、替代与 HOT 退役、热度与永久归档、协调写入重放 |
| `test_sm_w7_recall_bounds.py` | 有界 lesson/HOT 读取、`read_ids`/`scope_pairs`、缺损自愈、命中去重 |
| `test_sm_w8_promotion_curator.py` | lesson CAS 晋升、载体委派、整理器维护与版本绑定 |
| `test_sm_w10_closed_loop.py` | 跨切片组合：275→50 收敛、容量收敛崩溃恢复、5,000 owner 隔离、工作计数、载体与纠正；W3/W9 合入后补组合根全链路 |

旧策略断言按锁定决定有意更新：默认 false/observe/上限 0、两正两负质量规则、未证明的回滚目标、子代理结果自动生成待确认提案。来源、操作者、revision、nofollow、权限、错误副作用、FIFO 与显式覆盖合同全部保留。`test_skill_learning_confirmation.py` 经全仓扫描无外部引用后删除，候选存储与迁移覆盖在 `test_sm_w1_store_transitions.py` 与 `test_sm_w3_intake_runtime.py`；`skill_proposal_commands.py`、`skill_learning_commands.py`、`decision_skill_proposal_review.py` 及其测试保留，CLI 144 条解析路径不变。

## 13. 已知限制与后续

- W3/W9 合入前，组合根 live 接线、后台总结的 `ineffective` 通道与 `skill_feedback` 四动作只在各自切片内验证，尚未有集成提交上的全链路证据。
- 合并判定用 token Jaccard，属于确定性近似，不做语义改写；语义相近但措辞差异大的条目仍可能并存。
- 旧版明细缺少身份字段，迁移只保留 `ever_used` 与保守的最近活动上界，独立使用次数标为下界或未知，不伪造历史唯一性。
