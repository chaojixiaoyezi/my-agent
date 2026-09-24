# 决策模型 P5-C：自学习候选筛选只读审计

状态：2026-09-22 只读审计完成，建议待主线评审；未实现自学习决策点、未调用真实 Jev、未创建或修改正式 Skill。审计工作台 `codex/decision-model-integration`，HEAD `ef497a904`；仅拥有本文件。
后续（2026-09-24）：本文建议的唯一 Skill 提案/确认主链（S1）已合入 main `e9ead5ae3`；自学习决策点（S2）已在本地分支 `claude/self-learning-proposal-review-order` 实施、待审，定名 `skill_proposal_review`，只对待确认提案给审核顺序，不要求 `enable_self_learning`（该开关只管生成），见 [DESIGN_LEDGER](../../DESIGN_LEDGER.md) “自学习 S1/S2”条目。下文“当前事实”描述的是审计当时的代码。

## 结论与当前事实

用户想让 my-agent 自动发现可复用经验，同时保证不好用的增强可撤下，基础能力不受影响。Jev 适合给**已有、带来源的学习候选**排序或指出缺证据，不能替代候选来源、正式审核或 Skill 写入权限。

当前仓库没有活跃的 `enable_self_learning` 配置字段、`learn` 命令或生产 `learning_drafts` 草稿仓库。`CLI_REFERENCE.md` 明确旧 `learn` / `subagents-memory-gate` 已删除，`memory_store/migration.py` 仅为一次性迁移读取旧 `learning_drafts`，稳态写入方已删除。`README.md`、`AGENTS.md` 和 `LLM_GUIDE.md` 仍保留旧自学习描述或“正式 Skill 必须用户确认”的规则；前者不能当作已实现功能，后者仍是本项目的安全约束。需要主线另行同步过时说明。

现行真实来源是 `subagents/services/runner_result_service.py::_post_result_side_effects`：成功、非 dry-run 的结构化 runner lessons/findings 经 `SubAgentMemoryCandidateService.record_result_candidates` 进入 owner 唯一 `memory/candidates.jsonl`。失败自省也只生成 `model_inferred` lesson Candidate。`CandidateService` 管候选身份、证据、幂等、状态；`MemoryPromotionService` 只支持 long-term、Persona、lesson、HOT 等正式记忆落点，`candidate_models.PROMOTION_TARGETS` **没有 Skill**。这些是记忆候选，不能改名当 Skill 草稿，也不能让 Jev 直接改审核状态或晋升目标。

2026-09-25 补充（分支 `claude/subagent-lesson-ledger`，待合入；已端到端真实验收）：真实子代理只做自然回复、不出结构化输出，上面的来源在产品里实际走不到。现在增加第二个结构化来源：子代理用 `record_lesson` 写本 run 的 `lessons.jsonl`，同一出口读回账本，自然回复也会把账本经验记成带账本引用的 `subagent_lesson` 候选。宿主仍不从回复正文提取经验。

每个子代理工作区会建 `SKILL_SPARKS.md` 模板，说明需后续审核；目前只有模板生成和文件路径接入，没有结构化提案读取、确认或正式 Skill 提交链。`SkillsService` 从 owner/workspace/shared/builtin 的现有 `SKILL.md` 生成逐轮快照；`SkillSnapshot.read_body` 校验 hash 和 guard；`skill_guard` 为读取/安装风险检查，不是“用户已确认写入”的凭据。源码中未找到独立的 Skill 草稿状态机或带确认回执的正式写入口。通用文件工具仍受其自身权限控制，但不能据此宣称已经有受治理的自学习晋升。

## 最小安全接缝

| 阶段 | 可复用权威 | Jev 可做 | Jev 不能做 |
| --- | --- | --- | --- |
| 来源收集 | 原 runner result、精确 task/run、artifact/evidence refs；owner CandidateService | 在来源已成功写回后，针对精确候选提出“值得起草 Skill / 缺证据 / 不适用”的短建议 | 伪造来源、删除原 lesson/finding、因高分直接晋升记忆 |
| 学习提案 | **尚无生产服务**；应新增单一、带版本和来源的 Skill proposal（不复活旧 `learning_drafts`） | 对宿主提供的有限候选 ID 选择优先级或缺资料类型 | 自由输出新目标路径、覆盖现有 Skill、直接写 `SKILL.md` |
| 审核与采用 | 项目规则要求用户确认；正式 Skill 读取沿 SkillsService 快照和 guard | 在确认前提供可忽略的解释与证据清单 | 自行批准、把模型 confidence 当用户确认、绕过原文件权限 |

**可先落地的 `observe` 片**：只对原 `CandidateService` 中具备精确 task/run/source refs 的 lesson 类候选运行一次短决策，结果进入原 decision 调用账和固定诊断；不写候选状态、`SKILL_SPARKS.md` 或正式 Skill。为了防止背景任务借前台会话，建议按真实 owner 后台 run 使用 `begin_decision_stage(..., scope="owner_background")`，或在前台原工作片内使用准确 thread/run；两种身份不能混用。开启条件须是独立 `self_learning` 决策点加原自学习授权，总开关默认关，现有设置服务/模型 profile/超时/冷却/取消/CAS 共用。当前原自学习授权不存在，故只登记设计，不可假装打开决策点就产生学习草稿。

**未来可落地的 `apply` 片**：先建立单一、可审核的 Skill proposal 主链，来源可以引用原 Candidate ID 和原任务证据，但提案正文、目标 owner/workspace Skill、版本、当前目标 hash、状态和确认回执必须有自己明确的权威。Jev 只能在创建提案前选择“优先起草/保留原序/缺证据”等既定选项，或给已存在的未审核提案排序；不能自行决定不生成原允许的草稿、拒绝用户候选或直接写正式目录。提案服务在用户明确确认后，复查 owner、目标路径、现有 Skill hash、proposal 版本、SkillGuard 与当前权限，再原子提交 `SKILL.md`；失败保留提案和原 Skill。现有 `CandidateService` 状态机可借鉴和复用通用 JSON/CAS 原语，但不要把 Skill 塞入 memory promotion 目标并让记忆服务承担 Skill 写权限。

TypeSafe/Jev wire 只支持 `choice`、`score`、`noul`，无自由正文生成。候选 ID、答案选项和 `need_data` 所需 refs 必须由宿主冻结；Jev 不能生成 Skill 内容或任意操作。无来源、无用户授权、无有效提案、`not_needed` / `need_data` / `no_match` / `abstain`、坏答案、关闭、observe、超时、额度错误或设置/来源版本变化，都保持原候选与原 Skill 不变。用户取消按原取消机制传播；自学习可选失败不得使聊天/子代理失败。建议只记录输入 token，不做决策费用账；任何长期新增网络请求、文件写入和授权开关必须在原 YAML/dataclass/设置服务同步。

## 与参考实现的边界

本机项目索引核对：`CONTRACTS_MAP.md` 的 Memory/Task workspace 权威入口，`CODEBASE_TREE.md` 的 Candidate、SkillService/SkillSnapshot、`docs/design/README.md` 和 `docs/design/DECISION_MODEL_INTEGRATION.md` 的 P5-C 项；源码核对范围限上文列出的 runner 结果、候选/晋升、Skill 发现/guard、决策服务与设置 schema，没有全仓逐行审计。

外部本机参考只读检查了 `study-agent/all-agent/openclaw-main/docs/tools/skill-workshop.md`：提案与活动 `SKILL.md` 分开，目标 hash、扫描与回滚在 apply 前复查；这只是可借鉴的边界，本项目用户确认要求更严格，不能照搬其默认 agent apply 行为。另看 `study-agent/all-agent/hermes-agent-main/tools/skill_manager_tool.py` 的 staged write gate；其配置默认可直接写，导入 gate 失败还有 fail-open，**不借用该默认与降级**。未审 OpenClaw/Hermes 的全部测试、安装包或运行表现，不把参考文档当作 my-agent 的实现证据。

## 验证、缺口和建议下一步

本轮只读定向测试：

```text
python3 -m pytest -o addopts='' agent_py_agent/tests/test_subagent_memory_candidates_v2.py agent_py_agent/tests/test_memory_migration_v2.py agent_py_agent/tests/test_skills_service.py agent_py_agent/tests/test_skill_guard_gate.py -q --tb=short
```

结果 **48 passed**。它只证明现行候选、旧草稿迁移、Skill 快照和 guard 的相关边界；没有测试或真实 Jev 自学习筛选，因为生产接缝尚不存在。

后续定向测试应依次覆盖：默认关闭零请求/零草稿写入；owner/thread 身份隔离与取消；每题非选择/错误保留原候选；来源删除、版本变化和确认前目标 Skill 被改后的 CAS 拒绝；未确认不能写正式 `SKILL.md`；明确确认、Guard 与路径许可后唯一提交；失败保留可重试提案；多窗口同提案幂等；原 Skill 快照在下一轮可见而当前轮不突变。真实验收要用普通中文任务让被测 agent 自己产出结构化 lesson，再观察提案、用户确认和 Skill 读回；测试者只发任务并查账，不代写草稿或 Skill。

建议主线先决定是否实施单一 Skill proposal/确认服务，并同步清理过时 `enable_self_learning`、`learn` 描述；没有它时 P5-C 自学习点只能排入观察研究，不能算 `apply` 已接通。这条服务可与模型选择、外部材料排序并行设计；Skill 写入/确认链应由一位 owner 独占实施，Jev 只在其稳定后接筛选，守住“决策不授予写权”的边界。本审计仅写本文件，共享设计导航、Goal、CODEBASE_TREE 与测试汇总由主线统一更新。
