# 测试与发布验收

## 记忆 Curator 输入预算缩批（2026-09-26，分支 `claude/curator-budget`，基于 main `37cad88f7`）

- **真机现象**：生产本机 owner 自 2026-09-24T15:39Z 起每次都是 `CURATOR_INPUT_BUDGET_EXCEEDED`（9/25 共 180 次），`cursor_before` 始终同一个、每次处理 0 条、0 次模型调用；测试 owner `tui-matrix/p1-r141` 同样卡住。失败记录里还夹着别的 owner 留下的 `ModelNotConfiguredError` 尝试形状。
- **新增** `test_curator_input_budget.py` 6 项：
  - 服务级复现：真实 ConversationStore 80 条消息加 3 条真机形态审计（36 位编号、带预览）。先断言前置条件：按生产收集口径收满后最终提示超预算。修复后第一轮截尾、成功提交前缀，游标只推进到前缀末尾，运行账记 `memory_curator_input_fitted`；第二轮重放剩余消息和审计，全部恰好处理一次。修复前同一场景连续三轮 `CURATOR_INPUT_BUDGET_EXCEEDED`、0 次模型调用、游标不动，失败诊断与真机记录逐字一致。
  - 缩批发生在可选决策标注之前：标注钩子只看到已裁进预算的批次。
  - 单元：先截消息后截审计、保留前缀、各留一条；未超预算原样返回（同一对象、无 warning）；预算小于模板时保底后仍超出，提取前检查保持原失败码。
  - 诊断：同一线程先成功提取一次，再遇预算失败，`last_model_attempts()` 为空。
- **变异验证**：8 种（不调用缩批、缩批挪到标注之后、开头不清空形状、先截审计、允许截空、从头部截、不记 warning、未超预算也换新对象）全部使测试失败。每次在 `PYTHONDONTWRITEBYTECODE=1` 子进程运行，并按 sha256 还原。
- **回归**：与 Curator 相关的 22 个测试文件加 `test_architecture_guardrails.py`，共 476 passed。

## 智能程度（推理强度）：`/effort` 与子代理 `effort`（2026-09-26，已合入 main `7a15c9c91`，基于 `84d9e50ac`）

- **新增** `test_reasoning_effort.py` 30 项（传输全为本地 fake）：
  - 换算：控制方式解析（显式声明优先；仅 DeepSeek 官方两种接口有默认；MiniMax、OpenCode、Responses、决策接口为 none）；档位与强制工具选择的优先级矩阵；两种协议的字段与思考预算夹紧。
  - 真实组包：OpenAI 兼容带 `reasoning_effort` 或关思考且不混发；DeepSeek 工具历史缺 `reasoning_content` 被迫关思考时去掉档位；显式声明控制方式的未知域名 `off` 真正写出 `thinking: disabled`，未声明的不写；Anthropic 兼容 `budget` 与关闭。
  - 档位解析：线程设置优先、清除后回全局默认、线程不可读时回默认；公用选项函数与网关自动选模投影载荷逐字一致，强制工具选择时不带档位。
  - 子代理：显式档位（大小写不敏感）、伪造宿主属性被覆盖、非法档位整批报错、省略时继承父级线程档位、`effort` 计入去重身份、子线程只在物化时写入一次档位。
  - `/effort`：真实 SimpleAgent + DeepSeek 档案下查看 / 设置 low / default 清除 / help，回执写明模型与“按推理强度档位发送：低”，不含密钥；echo 后端上如实回执“不支持调节、暂不改变请求”（改写原“永不假装已设置”用例）。
  - 档案与配置：`reasoning_control` 保存、列出、解析进运行配置（未声明为 auto），auto 不写键，非法值与决策模型拒绝；TUI 表单预选并保存；YAML 默认与规范化。
- **补充** `test_subagent_first_request_selection.py`：首轮自动换模的逐字比对改用同一 `request_reasoning_options` 计算期望载荷；新增用例在子线程档位 low、候选为 DeepSeek 官方接口时，断言确实采用候选模型且真实首轮请求带 `reasoning_effort: low`。
- **变异验证**：22 种中 21 种使测试失败（强制工具优先、已知表、none 不发、预算夹紧、DeepSeek 被迫关思考时去档位、声明方式的 off、Anthropic 关思考优先、选项丢档位、网关投影丢档位、子代理继承、线程覆盖、去重身份、子线程初始化、/effort 写入、档案映射、auto 不写键、配置规范化、TUI 表单、非法子代理档位、工厂控制方式、决策模型拒绝）。存活的 1 种是子代理首轮选模投影不带档位：该投影只用于容量估算、不做逐字比对，属于行为等价，代码仍保持与真实请求一致。每次在 `PYTHONDONTWRITEBYTECODE=1` 子进程运行并按 sha256 还原。
- **结果**：与改动直接相关的 39 个测试文件（含 `test_architecture_guardrails.py`）1074 passed；代码尺寸与 main 逐项对比无新增（`_do_backend_generate` 超软上限、`_payload` 参数两项消失）。
- **真实验收**（2026-09-26，main `7a15c9c91`，运行时 `step12k-e5b2f8bc`；本机隔离 home + 回环 8432 单 Gateway，默认模型为 DeepSeek 官方 OpenAI 兼容 flash；测试者只发 `/effort`、`/model` 控制命令和每个会话一条任务。题目是“1 到 2000 中既是 3 的倍数、各位数字之和又能被 7 整除的整数个数”，答案 64。token 取用量账本 `purpose_breakdown.main`，排除 Jev 决策调用）：
  - **DeepSeek 官方 OpenAI 兼容**：`/effort` 回执写明档位、来源和当前模型上的效果。出站请求：low/max 分别带 `reasoning_effort: low|max`，off 带 `thinking: {type: disabled}`，auto 不带任何字段；`/effort default` 清除后回到全局默认。主模型输出 token：low 4 次 1059–2185（均值约 1536），max 4 次 1494–5392（均值约 2847），auto 3 次 1234–5192，off 2 次 272 和 566。同一道题在完整主代理上下文里波动很大，只能看出 max 平均约为 low 的 1.9 倍，off 最低；off 两次中有一次答错。
  - **DeepSeek 官方 Anthropic 兼容**（auto 解析为 budget）：off 时助手原生消息只有正文、没有思考块，推导写进了正文，答案正确；high 和 auto 都有思考块，答 64。Anthropic 协议没有出站诊断摘要，这里以原生内容块作结构化证据。
  - **MiniMax M3**（在隔离目录里经产品 `save_model` 声明 `reasoning_control: budget`）：auto 没有思考块，答错；`/effort high` 后出现思考块（26713 字符），答对。输出 token 从 5609 升到 11999，用时从 21 s 升到 99 s。
  - **MiniMax M2.7**（none）：`/effort high` 回执如实说明“不支持调节……本设置暂不改变请求”，请求照常完成。
  - **子代理**：主会话先 `/effort medium`，再用一条任务让主代理一次创建三个子代理（effort=low、effort=max、不设）。子线程记录与任务宿主属性分别是 low、max、medium，不设的继承了父级 medium。出站请求逐条对上：low 子代理 4 轮都带 low，max 子代理 7 轮都带 max，继承的子代理 4 轮都带 medium；主会话前台 3 轮加后台续跑 5 轮都带 medium；3 次宿主能力探测不带档位。三个子代理都答 64，输出 token 为 low 1641 < medium 2373 < max 7305。
  - **附带发现**（与本改动无关，已登记 DESIGN_LEDGER“记忆 Curator 生产持续失败”）：隔离环境的记忆 Curator 给 DeepSeek 官方 OpenAI 兼容接口发 `response_format: json_schema`，被 400 拒绝（“This response_format type is unavailable now”），记为 `CURATOR_MODEL_FAILED`。
  - 证据在 `~/.my-agent/releases/reasoning-effort-acceptance-20260926/`，只含结构化摘要；隔离 home 与模型目录副本已删除。

## 自学习 S3：完成任务后自动总结 Skill（2026-09-26，分支 `claude/skill-auto-summary`，基于 main `839250728`）

- **新增** `test_skill_learning.py` 34 项（假 backend，不联网）：
  - 触发与材料：不落盘、task_local、后台回合、工具轮数不足、缺运行身份都不入队；请求有界（输入/回复截 3000 字、轨迹 80 条）、密钥脱敏、只收成功的 `skill_search get` 读过的 skill_id。
  - 发布：create 后下一次快照出现 `owner:<名字>`（category `learned`、content_sha256 等于登记值），账本、版本全文、每日计数正确，请求删除、暂存目录清空；skip 只记账。
  - 拒绝：7 种输出不合规（非 JSON、多字段、坏名字、坏标签、正文过短、正文带 frontmatter、未授权的 update）都记 `OUTPUT_INVALID` 且不重试；与用户 Skill 重名、guard 命中 `curl | sh`、数量上限、删过的名字各返回对应码且不写文件；直接调用发布接口时 3 种不能往返的 frontmatter 被 `DRAFT_INVALID` 拦下。
  - 所有权：update 只针对本轮读过、登记在册、磁盘 hash 未变的自学 Skill，提示词里给出其完整字段；用户手改后同样的 update 被拒、文件不动；未在本轮读过的 update 被拒。
  - 运行：每日上限顺延；模型超时先重试（attempts=1）、再失败则丢弃记 failed；运行锁被占与前台同端点忙都返回 busy 且零调用；队列 20 条上限、重复入队忽略；损坏请求丢弃、损坏登记表失败关闭且保留请求。
  - 回滚删除：v2 回滚到 v1 文本逐字一致，v1 再回滚等同删除并进禁用名单；用户改过的拒绝回滚但允许删除。
  - 宿主会话：模拟要求会话头的服务商（无会话即 `ValueError`），后台调用自绑 owner + 请求键的会话，重试共用同一会话值，调用结束后当前线程不再持有会话。这是真实验收首轮发现的缺陷（真实服务商要求会话头，两次尝试都失败后丢弃），修复前本用例失败。
- **新增** `test_skill_learning_integration.py` 8 项：组合根只在开关开启时装配、与 Curator 共用 backend、不预建目录；finalize 只在任务完成时入队；收口 helper 吞异常；Gateway 策展车道对“记忆关闭但有学习请求”的 owner 只跑学习、正常 owner 先整理后学习、学习异常隔离、`memory_curator_enabled=false` 时不跑整理；车道开关按两项配置之一；`skills learned list/show/revert/remove` 真实 CLI 往返（含手改后状态 `user_modified`、回滚被拒、删除入账）；S1 lesson 提案经 runner 自动确认（`confirmed_by=auto`）；四个新配置键的 YAML 默认与越界规范化。
- **真实验收发现并修复的两处**：① 服务商要求会话头，后台调用未绑宿主会话 → `ValueError`（已修，见上）；② 首个真实发布的 Skill 含“删除被拦截就改用 apply_patch 删”这种绕过安全拦截的做法 → 提示词“不要保存”清单加一条，更新时一并删除（软约束，用例断言提示词含该条）。
- **改写** 2 项旧断言（行为按用户决定改变）：`test_skill_proposals.py::test_runner_result_auto_confirms_proposal_when_service_attached`、`test_subagent_lesson_ledger.py` 的自学习开启用例，从“提案保持待确认”改为“自动确认并安装”。
- **变异验证**：40 种各自使测试失败——宿主会话绑定、组合根传 owner_id、触发的 task_local/后台/门槛/do_save、材料脱敏、只收成功读取、名字/更新目标/`#`/frontmatter 正文检查、重名/禁用名/上限/所有权 hash、发布脱敏、guard force、frontmatter 往返、回滚所有权、删除入禁用名单、每日上限、队列上限、去重、前台让路、重试次数、本轮读过过滤、可更新 hash、运行锁、车道准入/重算/总开关/整理开关、组合根开关、finalize 调用、S1 自动确认与确认方、CLI 状态、登记表损坏、来源 run。每次在 `PYTHONDONTWRITEBYTECODE=1` 子进程运行并按 sha256 还原，之后删除 `__pycache__` 重跑。
- **结果**：与改动直接相关的 52 个测试文件（含 `test_architecture_guardrails.py`）1015 passed；每次提交前严格 gate 全过（ruff、doc sync、strict code-size 与 main 持平 2200/1496/704、`git diff --check`、clean-package）。
- **真实验收**（2026-09-26，本机隔离 home + 回环 8432 单 Gateway，owner 真实选定模型，门槛配置为 4，测试者每轮只发一次 prompt）：
  - 第 1 轮（CSV 合并，3 轮工具）不到门槛，不入队；第 2 轮（多编码，6 轮）入队，但服务商要求会话头而后台调用没绑宿主会话，两次都 `ValueError` 后丢弃并记 `failed`——据此修复并发 step12h。
  - 第 3 轮（做成命令行工具，12 轮）入队后由策展车道处理，真实模型发布 `csv-merge-cli` 第 1 版，下一次快照出现 `owner:csv-merge-cli`（category `learned`，hash 与登记表一致）；正文含“删除被拦截就改用 apply_patch 删”，据此加提示词约束并发 step12i。
  - 第 4 轮（同类任务，3 轮）模型没读 Skill、也不到门槛；第 5 轮（用户说按之前总结的做法来，25 轮）模型先 `skill_search get owner:csv-merge-cli`，后台据此选 update，发布第 2 版（补了 UTF-16、¥、千分位三个新坑），正文不再含绕过拦截的内容。
  - `skills learned list/show/revert` 在真实数据上运行正常，回滚后文件与 `versions/v1.md` 逐字节一致；首日总结调用 4 次。验收中发现版本目录多出 `.lock` 文件，已改用不取锁的原子写。证据在仓库外 `~/.my-agent/releases/skill-learning-acceptance-20260926/`，隔离 home 与模型目录副本已删除。

## 决策开关、超时自调、统一审计与管理员管控（2026-09-25，分支 `claude/decision-audit-controls`，基于 main `07fa00fb3`）

- **新增** `test_decision_audit_controls.py` 20 项：注册范围（main 两个工具、user 只有审计、group 都没有，管理员工具审批为 always）；
  管理员控制默认值、坏块/坏文件失败关闭、写入保留其它策略字段与 0600；只允许管理员写（not_admin/admin_self_only/invalid_changes）；
  跨用户许可只在管理员名下生效；owner 编号解析拒绝非规范或不存在；管理员工具 list/set 与用户侧即时生效；
  审计的时间窗、本人范围、观察白名单（提示词与工具名清单不外泄）、跨用户许可、无 Gateway 上下文时观察不可用。
- **补充**：`test_decision_service_http.py`（管理员关闭后本机 HTTP 零请求、零调用记录、`admin_disabled`，重新允许立即恢复；坏策略失败关闭）；
  `test_user_config_owner_scope.py`（自调等待时间越界拒绝且不保存、范围回显、能力配置 0=不限）；
  `test_gateway_model_observation.py`（选模型输入去锚点段、按上限截断并标注、候选公共声明只写一次）；
  `test_decision_usage_metrics.py`（成功/失败计数、约数外推、全部缺报 `≈?`、旧账不计入）；
  `test_tui_decision_menu.py` 与四个接入点集成测试（两个勾选到三种模式的换算与真实按键保存）。
- **结果**（变基到 main `1ea760c0f` 后）：全部 `test_decision_*.py`、`test_tui_decision_menu.py`、`test_tui_model_metrics.py`、
  `test_user_config_*.py`、`test_gateway_model_*.py`、`test_gateway_compact*.py`、`test_capability_*.py`、配置/审批/owner 策略、
  `test_admin_identity_*.py`、工具面快照相关文件、恢复码/合同与 `test_architecture_guardrails.py` 共 3394 passed（24 xfailed、5 xpassed 为既有标记）；
  ruff、`check_doc_sync --base origin/main`、strict code-size（与 main 完全相同：2200/1496/704，新增 0）、`git diff --check`、clean-package 均通过。
  变异 24 个（管理员硬门、阶段提前返回、失败映射、审计本人/跨用户权限、观察归属与白名单、时间窗、自调上下限、成败计数、约数、
  锚点段、截断标注、审批 always、注册条件、执行复核、勾选换算、候选去重、连接测试说明）全部被抓出，
  每个都在 `PYTHONDONTWRITEBYTECODE=1` 子进程里跑并逐字节恢复。
- **真实 TUI 验收**：本机隔离 home、127.0.0.1:8431、真实 Jev，管理员与普通用户两个 TUI 各走一遍，
  见[决策开关、超时自调与审计](docs/design/DECISION_AUDIT_AND_ADMIN_CONTROLS.md#真实-tui-验收2026-09-25本机隔离-homegateway-只绑-12700018431)。

child不展示历史正文时的读取回归先复现1 failed/1 passed，修复后test_compact_retained_history、test_subagent_compact_recovery、test_gateway_child_compact_scope_application三文件31 passed（8.48秒）。三宿主seed仓外基线仅验证测量和完整性，不算内存目标通过；细节见容量审计的宿主生命周期基线。

## 第 10 步组合验收 combo6 与全部卸载后核心基线（本机 `runtime-step11l-2d00c734`→`step11m-f1cc8217`，2026-09-25 凌晨）

- **组合任务（真实 owner，一条 prompt，M3）**：5 个插件（genui-lite、image-text、savepoint-lite、browser-lite、design-lite）经真实 TUI 安装→配置→启用后，
  发一条 36 个月订单 CSV 的三年汇总长任务：3 个子代理按年统计→主代理独立重算核对→genui-lite 出三张柱状图→browser-lite 逐页核标题→savepoint-lite 快照→design-lite 出卡片→写报告。
  PROMPT_SENT 01:33:54，产物最后写入 01:39（约 5 分钟），无审批弹窗，3 个子代理终态均已完成，Compact 0（上下文约 105k/1.0m）。
- **产物核对（只读结构化事实，不读回复正文）**：`out/` 13 个文件；与测试者预先生成的真值文件逐项比对 18/18 一致——三年各年完成金额、各区域金额、
  36 个月度金额、三类非完成状态订单数、各年最高区域、2024/2025 同比、三年总额。子代理中途把输出路径解析成 `combo6/combo6/out/`，主代理用 `send_guidance` 澄清后两处一致；
  这是模型行为，不是框架缺陷，产物最终位置正确。
- **脚手架教训**：观察脚本用屏幕关键词判"安静"，报告正文含"子代理"等词导致 WAIT_END 迟到 40 分钟；属测试脚手架问题，与产品无关，下次改读结构化会话状态。
- **升级回归（本轮前置发现并修复）**：见上文"激活目录摘要随声明字段漂移"；修复部署（step11l）后本次安装、启用、调用全部正常。
- **全部卸载后核心基线（step11n 二进制，同一真实 owner）**：第一轮误用 `/plugins uninstall`（无此命令，安装表 last_commit 仍为 release，5 插件只是停用），基线 summary.txt=400.00 在 25 秒内写出、无审批，但不算“空表”。第二轮用 `/plugins remove` 移除全部 6 个插件（含 workspace-peek），空表下发一条核心任务（读 CSV 合计写文件）：REMOVE_DONE 02:37:19 → 文件写出 02:37:42（16 秒），值 1234.56 正确，无审批弹窗、无插件工具。随后从仓库重建的 workspace-peek 包 `/plugins install` + `/plugins enable`，安装表只剩 workspace-peek 0.1.1 active（revision 从 17 重置为 3，证明确经移除再装）；packages 目录保留 1 个旧包文件（内容寻址，不影响运行）。测试目录已归档到仓库外并从 owner home 删除。

## 内置方法论技能正文重写（开源准备，2026-09-25，分支 `claude/opensource-prep-decision`）

- **改动**：`agent_py_agent/skills/builtin/` 下 quality、planning、orchestration、review、meta 五类共 12 个 `SKILL.md` 的正文，按原意图用自己的话重写。这 12 个技能是 test-driven-development、systematic-debugging、verification-before-completion、brainstorming、writing-plans、executing-plans、define-goal、dispatching-parallel-agents、subagent-driven-development、requesting-code-review、receiving-code-review、writing-skills。
  - 结构、标题层级与示例都另写，不逐句转写旧文。
  - frontmatter 的 name、tags、scope、risk_level 逐字节不变。与上游同名的 11 个技能另改写了 description 与 when_to_use：意思不变，保留用户常用的检索词（路由按 tags 4 分、描述 1.5 分给词打分）。define-goal 的 frontmatter 未改。`documents/academic-word-pdf-layout` 原先引用旧口号，已改为直接写规则。
  - 正文里的工具名、技能引用、命令与路径约定保留。上游示例用的占位路径和示例函数名换成本项目自己的示例。
- **核对方法**：逐文件对比 main `97fe9d72a` 的旧正文。
  - frontmatter 逐字节相同。
  - 旧正文中的工具、命令、路径类标识符都还在；只少了上游示例名与占位路径，属有意替换。
  - 新旧正文去空白后的 8 字符片段重合率为 0.9%—4.1%。最长公共片段不超过 62 字符，全部是标识符列表、命令或路径。
- **测试**：读取内置技能、`SKILL.md` 或技能目录的 20 个测试文件 212 passed。覆盖技能加载与优先级、代表性任务路由、技能守卫扫描、打包、声明式索引、已退役工具名检查与架构守卫。
- **路由对照**：12 条贴近日常的中文请求（每个技能一条）用新旧 frontmatter 各检索一次，12 个技能新旧都排第一。这是临时的一次性对照，用例未入库。

## 动作候选 `action_candidate`（2026-09-25，分支 `claude/decision-action-candidate`，基于 main `6a50d84aa`）

- **改动**：
  - 新增 `tool_context/decision_action_candidate.py`；`_optional_result_hints` 追加第三个点。
  - `POINT_RUNTIME_SCOPES` 登记 `action_candidate: thread`；AgentConfig/YAML 三字段默认 off/null/null；TUI 决策菜单加"动作候选"。
  - 新鲜度只问插件线的 `plugin_observation.observation_is_current`；数量上限、`obs-`/`cand-` 编号、key/role 规则与 `OBSERVATION_SCHEMA` 直接复用 `plugin_observation`，`target_kind` 复用 manifest 规则，不另立第二份。
- **新测试** `test_decision_action_candidate.py` 82 项。单元用例只替换决策服务和模块内的新鲜度函数；另有两项集成用例用插件线真实的 `parse_observation` 铸观察，经产品写入口 `persist_tool_runtime_ledger`（带真实 LocalStore，归档没有 `runtime_gate`，与真实链路一致）记进权威事件流，再由真实 `observation_is_current` 判定。覆盖：
  - 未登记时严格空操作；off、阶段错误、他 run 阶段都不准备材料。
  - apply 只追加所选候选的 candidate_id 与 role。外发不含 key、目标引用、代次、激活、候选编号、动作名、请求密钥或工具输出；label 只出现在同一个外部数据块。
  - observe 以及 deadline/cooldown/error/stale 都保留原结果；四种非选择和坏答案同样保留。
  - 所选候选的动作工具不可用时不追加。
  - 32 种不合格来源零请求：schema 版本不符、数量越界、编号/类型/角色/label/key/动作不合规、归档不一致、失败或重放调用、两种收口、请求超长或为空、请求或 label 含 URL 查询串、没有可用动作。
  - 新鲜度：不新鲜时零请求并以 owner 库、run、task、observation_id 询问权威；等待期间变旧不追加；没有权威库、权威返回非 True 或抛错都安全关闭。
  - 集成：当前观察给出提示，同时门台账不写、完成事件照写；同一目标有了更新观察后旧候选不再提示。修复前的 `persist_tool_runtime_ledger`（无门即提前返回）会让第一项失败。
  - 子代理零请求；等待期间来源或工具快照变化、两种期限、响应绑定其他材料、策略变化都不采用；可选错误保留原结果，取消与中断上抛。
  - 经原 `_record_tool_call` 在 off/observe/apply 下 text/native/IR 为同一段；三点互斥；默认配置为 off 且 thread 可设；原 TUI 菜单可改线程模式。
- **变异验证**：19 种各自使新测试失败（新鲜度恒真、去掉采用前来源复核、渲染或资格忽略可用性、候选下限 2→1、去掉重复编号检查、外发 key、去掉 ok/子代理/URL/题号/策略/期限/label 上限/候选上限/归档 task/响应绑定/权威库/schema 检查）。子进程带 `PYTHONDONTWRITEBYTECODE=1`，结束后原文件逐字节还原。
- **回归**（rebase 到 main `6a50d84aa` 后）：全部 `test_decision_*`、引用 `_record_tool_call`/`_optional_result_hints`/设置 schema/TUI 决策菜单的文件，加插件观察、代理观察、运行门账本、browser-lite 包与架构守卫，共 57 个文件 1530 passed、1 xpassed（既有）。
- **真实验收**：本机隔离 owner 上用 browser-lite 做 off/observe/apply/过期四档，见[真实验收](docs/tasks/DECISION_MODEL_REAL_VALIDATION.md#15-动作候选-action_candidate-的-browser-lite-真实验收2026-09-25)。首次运行暴露宿主完成事件在无 `runtime_gate` 时不写的缺口（插件线已在 main `6a50d84aa` 修复）。

## 自愈两片：无身份悬挂运行轮可见可结清、导航种子不再误迁移（用户决定第 5 项，2026-09-24 晚）

- **无身份悬挂运行轮**：本机 owner 库有 9 条 8 月的 running/created attempt，metadata 为空、没有 `runner_pid`，原 RUN-01 进程死亡证明永远碰不到它们。不自动判死（同一 owner 库会被别的运行版本写入，原合同"无 pid 记录保持原态"不动）；改为 Gateway 启动列出并写进状态/事件（`unidentified_stale_attempts`、`gateway_stale_attempts_reconciled.unidentified`），新增显式命令 `my-agent runtime-stale-attempts [--settle] [--older-than-days 1] [--json]`，`--settle` 经 `RuntimeRepository.settle_unidentified_attempts` 的 current_attempt CAS 记为 unknown（metadata `recovery_reason=no_runner_identity`、`settled_by=explicit_control`）。
- **导航种子**：`owner_navigation_seeds()` 统一 memory.md/HOT 的种子文本（根模板或正式默认导航），`_scan_legacy_navigation` 把正式默认与本 home 种子都判 current；新 owner 初始化后不再产生模板标题候选。
- **测试**：`test_runtime_db_recover_stale.py` +1（列出、阈值内保留、显式结清与幂等、有身份行不受影响），`test_startup_commands.py` 两项断言含 `unidentified`，`test_memory_migration_v2.py` +1（种子/根模板副本/正式默认均 current，自定义正文才 legacy）。
- **真实验收**：部署后本机跑 `my-agent runtime-stale-attempts` 看到 9 条并用 `--settle --older-than-days 7` 结清，Gateway 事件与 status 载荷计数归零。
- **status 可见（2026-09-24 深夜）**：`my-agent status` 的 Gateway 段与 `--json` 的 `gateway.unidentified_stale_attempts`、`gateway status` 的
  `gateway unidentified_stale_attempts=N` 都只投影 Gateway 启动写进 state.json 的计数，大于 0 才出行并指向 `runtime-stale-attempts`；不查库、不结清。
  测试：`test_status_commands.py::TestStatusUnidentifiedStaleAttempts`、`test_gateway_status_runtime_errors.py` +1。
- **升级回归：激活目录摘要随声明字段漂移（2026-09-25 凌晨，真实 owner 发现）**：组合验收前 `/plugins install` 一律回"无法读取当前插件目录"，Gateway 日志"插件安装目录不可读：PluginInstallationError"。根因：manifest v5 给 `PluginToolDeclaration` 加了 `observation/observation_ref`，`plugin_catalog_digest` 直接哈希 `asdict(tool)`，升级后所有既有激活记录的 `catalog_sha256` 对不上，`_validate_activation` 把整张安装表判为不可读——用户真实 owner 上的 workspace-peek 从 step11d 部署起就没有插件工具/Skill/管理入口，直到 step11l 修复。修法：摘要只哈希有值字段（新可选字段为 None 不进摘要），既有记录的摘要逐字节复原；不改用户数据。测试 `test_plugin_catalog_digest_stability.py`（旧公式相等、声明观察改变摘要、顺序无关）。规则：以后给声明类加字段必须默认 None/空并跑这个测试。
- **任意语言插件（包描述 v6，2026-09-25，本地）**：`test_plugin_any_language.py` 35 项——v6 描述往返与 20 种非法声明、可复现打包与多余成员/篡改拒绝、解释器真实路径与摘要、确认回执内容与 12 位确认码、未确认不准备不启动、错码拒绝、带码启用后真实 MCP 调用（随包可执行文件与系统解释器两种入口，用 python3 脚本扮演，不依赖 node/go）、停用释放与卸载、解释器被换/消失/定位文件被改拒绝且 stat 未变不重算摘要、平台变化拒绝、准备阶段拒绝计划后被换的解释器、解包权限与随包 Skill 目录。`test_plugin_any_language_samples.py` 4 项——工作区读取一致性用例（74 条裁决 + 20 个畸形上下文）先由宿主参考实现校验、再由 Node 移植逐条比对；hello-node（读取上下文内可读、越界 `PATH_READ_SCOPE_BLOCKED`）与 hello-go（按本机平台交叉编译）经宿主安装→确认→调用；没有 node/go 时跳过。变异：宿主侧 12 个、Node 移植 8 个全部被抓出。兼容：14 份 v1–v5 声明在 main 与本分支 `to_payload()` 输出 sha256 相同，`test_plugin_catalog_digest_stability.py` 通过。开发中顺带发现的原有问题：Homebrew Python 3.14 默认 sysconfig 方案 `osx_framework_library` 的 purelib 忽略 `base`，Python 插件随包 Skill 目录会算错（`test_plugin_skills.py` 在这类宿主上失败）；已报插件线，插件线在 `b4b59158f` 修复，本分支 rebase 后合并了两边改动。真实 TUI 验收（构建自 `e895dadbf`，本机 macOS + 测试机 Linux，隔离 home、127.0.0.1:8431）：两种入口的安装→回执→带码启用→调用、错码拒绝、真实模型经插件工具读到工作区文件、解释器被换后拒绝并需重新确认、异平台包启用被拒均按预期；验收中发现平台不符与解释器被换时 TUI 只显示通用失败文案，已改为按原因码给出具体说明、显式调用结构化拒绝（`PLUGIN_RUNTIME_UNAVAILABLE`），补断言并做 3 个变异。
- **决策故障矩阵 DNS 用例只在本机通过（2026-09-25，CI 首次暴露）**：`test_decision_fault_matrix[dns]` 在开发机期望 deadline 并通过，在 ubuntu-24.04 runner 上得到 error。根因：开发机环境变量配了 HTTP 代理（127.0.0.1:7890），非回环请求先连代理，测试在进程内注入的 `getaddrinfo` 失败根本没被调用，代理解析拖过 0.3 秒期限才成了 deadline；决策请求本就零重试，直连时解析失败即 error。修法：本文件加 autouse 夹具清掉代理环境并 `NO_PROXY=*`，dns 期望改为与连接被拒相同（error、冷却 30 秒、仍失败则 60 秒），并断言注入的解析确实被走到；去掉夹具在有代理的机器上会失败（已验证）。设计文档故障矩阵同步更正。跟进（同日，CI 3.11 job）：dns 恢复步骤在 runner 上得到 deadline——整次决策调用（路由、记账、工作线程、传输）都算在 0.3 秒期限里，慢机器挤不下。现在只有“慢响应”用 0.3 秒制造超时，其余立即失败的故障给 5 秒；恢复一步统一换新决策阶段并把期限调到 6 秒（配置类故障靠这次修订变化重试，按时间冷却的故障不受修订影响）。用临时插件给每次传输加 0.35 秒模拟慢机器：旧测试 8 个故障用例全失败，新测试全过。再跟进（CI 3.10 job）：同文件并发用例 12 个会话全部 deadline——服务端等 13 个请求到齐才放行，到齐本身就可能超过 1 秒期限；改为期限 8 秒、阶段预算 15 秒并在改预算后重开本会话阶段，等待到齐上限 6 秒；目录写锁用例同样给足期限。模拟传输延迟 1.2 秒时旧并发用例复现 `deadline` × 12，新版本文件 10 项全过。
- **插件进程 OS 沙箱试点（2026-09-25，本地）**：`test_plugin_sandbox.py` 10 项——开关在 YAML/dataclass/规范化里默认关、配置真的传到管理上下文/模型工具注册/面板客户端、bwrap 整根只读参数布局（且不改既有形态）、`PluginMCPClient` 包装启动命令与 `TMPDIR` 指向数据目录、沙箱不可用时启用（准备前）与显式调用（建运行前）都以 `sandbox_unavailable` 结构化拒绝；真实平台沙箱下数据目录可写、工作区与插件环境写不进、工作区可读，沙箱内 hello-node 正常且停用后无残留进程。本机 macOS（Seatbelt）与测试机 Linux（bubblewrap 0.8.0）各实跑通过；原 `test_sandbox.py`、`test_attempt_sandbox.py` 在测试机全过（50 passed、5 个 macOS 专项跳过）。测试机首轮发现只读根模式挂私有 `/tmp` 会盖住放在 `/tmp` 下的 owner home 与插件环境，已去掉私有 `/tmp`，改由 `TMPDIR` 指向数据目录。变异 10 个（不包装、无数据写根、根可写、启用/调用跳过预检、core/注册/面板/管理上下文丢开关、无 TMPDIR）全部被抓出。真实 TUI 验收（开关打开，本机 macOS + 测试机 Linux）：workspace-peek、hello-go、hello-node 安装启用调用与真实模型一轮都正常；macOS 运行中插件进程经 `sandbox_check` 确认在沙箱内（Gateway 对照为否），Linux ps 可见 bwrap 整根只读包装；停用后两边都无残留进程。
- **后台服务监听范围与 owner 长期授权（2026-09-25，用户决定）**：`run_command` 新参数 `background_listen_scope`（默认 loopback）；`ApprovalPolicy.owner_grant_parameters` 声明 `lan` 为可长期允许的操作，审批面板多出 `approved_owner`，写进 owner `tool_policy.json.operation_grants`；自主模式不放行未授权的 lan。host 每 2 秒按 socket 表核对，越界即 `killed/listen_scope_violation` 并留 `listener_violation` 证据，`run_command` 返回 `BACKGROUND_LISTEN_SCOPE_VIOLATION`。测试 `test_background_listen_scope.py` 11 项（含真实进程：绑 0.0.0.0 被回收、绑 127.0.0.1 与 lan 放行、关闭 enforce 只告警）；launch spec 升 v6，既有夹具补两个字段。
- **停机后存活的后台会话（2026-09-24 深夜）**：Gateway 停机收尾只读列出仍未终态的受管后台进程，写事件 `gateway_background_sessions_surviving`、state 计数 `surviving_background_sessions`，`my-agent status` 在 Gateway 未运行时显示 `background_sessions_after_stop=N`；不停进程。测试 `test_gateway_background_sessions_shutdown.py`（真实 store + 活进程只列非终态并带监听事实、无注册表/无目录返回空且不建目录、收尾事件与失败扫描不阻塞、state 合并），`test_status_commands.py::TestStatusSurvivingBackgroundSessions`。

## 插件观察候选结构（第 15 项 P5-C 前置，2026-09-24 晚）

- **改动**：manifest v5（`observation` / `observation_ref`，安装校验与双向配对，构建脚本自动选 v5）；新模块 `agent/plugin_observation.py`（整份接受/拒绝、宿主铸 ID、模型投影、按 runtime_events 序判定新鲜度、动作候选复核）；`PluginProxyTool` 结果路径改写与发送前复核（宿主参数 `__operation_id`/`__run_scope`，`_meta["my-agent/observation"]`）；`MCPProxyTool._execute_with_meta` 让子类按本次参数附 _meta 而不缓存到共享实例；`tool_completed` 事件载荷附观察投影；`runtime_db.events_for_agent_run`；归档白名单加 `observation`/`observation_rejected`；`ToolRegistry` 构造参数 `plugin_runtime_repo`；browser-lite 输出观察候选并按候选执行。设计与偏差见 `docs/design/PLUGIN_OBSERVATION_CANDIDATES.md` 第 6 节。
- **新测试**：`test_plugin_observation.py` 19 项、`test_plugin_proxy_observation.py` 5 项；`test_plugin_package.py` +14 项（v5 往返、13 种非法声明）；`test_browser_lite_package.py` 描述断言 + 1 项真实浏览器候选流（本机无 Chrome 时跳过）。
- **真实链路缺口修复（2026-09-24 深夜，决策线在隔离 Gateway 上发现）**：只读工具的归档没有 `runtime_gate`，`persist_tool_runtime_ledger` 提前 return 导致零 `tool_completed` 事件、新鲜度恒 False。改为每次工具完成都进权威事件流（无门时 status 按 ok），legacy 门账本仍只在有门时写；`events_for_agent_run`/`events_for_attempt` 取最新窗口再升序。`test_plugin_observation.py` +2（走 `persist_tool_runtime_ledger` 的无门归档、真实 SQLite 库 2100 条填充后最新观察仍在窗口内且按 attempt 读也取最新），原观察测试全部改走真实持久化入口；`test_runtime_gate_ledger.py` 原 8 项不变（有门事件、无权威库跳过、写失败不崩）。
- **插件层拒绝提升为结构化码（2026-09-24 深夜）**：`PluginProxyTool._lift_observation_error` 把 isError 结果里的 `my_agent_observation_error.code`（stale/not_found）提升为 `reported_error_code` OBSERVATION_STALE/OBSERVATION_CANDIDATE_UNKNOWN、TOOL_INVALID_ARGUMENTS、not_started；`test_plugin_proxy_observation.py` +1（两种提升、其它插件错误沿原映射、非候选路径不提升）。
- **真实验收**：决策线已于 2026-09-25 在隔离 owner 用 browser-lite 完成 off/observe/apply/过期四档（见 `docs/tasks/DECISION_MODEL_REAL_VALIDATION.md`）；宿主侧以合同测试为准。

## Gateway 停止时结清在途模型调用（用户决定第 4 项，2026-09-24）
- **背景**：同伴观察到 Gateway 停止时一条后台非流式 M2.7 请求（约 26.6KB，Memory Curator）被切断，收尾 `drain_complete=true` 却没有任何结算事实。
- **改动**：`contracts/model_call_ledger.py` 新增 `ModelCallLedger.fail_open_calls()`（用途分区函数改为公开的 `model_call_purpose`）；`agent_core/model/call_runtime.py` 新增 `settle_open_model_calls_for_shutdown()`（只读已有账本，不新建）；`cli/gateway_process.py` 收尾拆出 `_join_gateway_loops()`，排空后由 `_settle_interrupted_model_calls()` 写 `gateway_model_calls_interrupted` 事件并在收尾载荷加 `interrupted_model_calls`。
- **新测试** `test_gateway_model_call_shutdown_settlement.py` 5 项：批量终态只动活动记录且幂等；facade 不新建账本、投影只含固定结构化字段、结清后不再算运行中；收尾在停 HTTP 与收循环之后、写心跳之前结清并写事件；没有在途调用（含没有账本的 agent）不写额外事件；账本模块抛错只记异常类型且收尾照常完成。
- **真实验收**：待下次部署后用一次真实 Curator 在途停机复验，看 Gateway 事件里的 `gateway_model_calls_interrupted` 与收尾载荷的 `interrupted_model_calls`。

## 线程中断标志与 ident 复用（2026-09-25，分支 `claude/interrupt-ident-reuse`，已合入 main `761ef2ab2` 并部署）

- **问题**：`test_subagent_first_request_selection.py` 的真实 child 用例只在跟一大批决策测试一起跑时失败。
  - 直接原因：`_wait_for_generation_result` 被停止时会给 worker 立中断旗；测试里的假 worker 退出时没有撤旗，之后复用同一 ident 的生成线程 `my-agent-model-generate-timeout-guard` 一开始检查就被判为已中断，整次 child 运行记为 cancelled。
  - 生产也有同样的竞态：worker 自行撤旗之后、真正退出之前被立的旗无人再撤。
- **修复**：`concurrency/interrupt.py` 的标志从 ident 集合改为"ident → 立旗时线程对象的弱引用"。检查时线程已退出或 ident 换了主人即视为过期并清除；给已退出线程立旗落空；句柄与命名登记语义不变，公共接口不变。测试里的假 worker 改为与生产一致，退出前撤旗。
- **新测试**（`test_thread_interrupt.py` 5 项）：
  - 给已退出线程立旗落空；
  - 模拟 ident 复用：新线程不继承旧旗，回调也不被触发；
  - 撤旗后晚到的立旗随线程退出失效；
  - 先 join 再新建线程、强制复用同一 ident，新线程不被中断（200 次内撞不上即跳过，本机未跳过）；
  - 跨线程给存活线程立旗照常生效。
- 反向验证：换回旧实现时前 4 项失败，第 5 项照常通过。
- **回归**：
  - 引用中断/有界调用的测试族加架构守卫共 29 个文件：760 passed、1 skipped（既有的 Linux /proc 限制）；
  - 原先稳定复现失败的 63 文件组合：1429 passed、2 skipped、3 xfailed、0 failed；
  - Ruff、doc sync、代码体量（与 main 相比无新增项）、diff、clean-package 全部通过。

## 宿主关闭取消在途决策与 Curator/插件点并发组合（P4-F，2026-09-25，分支 `claude/decision-shutdown-cancel`，已合入 main `25650830d`）

- **改动**：
  - `decision_policy.py`：新增关闭标记、`cancel_active_decisions_for_shutdown()` 与 `host_shutdown_started()`；`register_active` 在关闭后拒绝登记。
  - `decision_service.py`：登记被拒且宿主关闭中时返回 `stale/host_shutdown`；设置撤销与关闭撤销合并为 `_revoked`，设置撤销优先。
  - `cli/gateway_process.py::_cmd_gateway_run_cleanup`：置位停止事件后调用取消原语，用 try/except 包住，出错只记异常类型。
- **新测试** `test_gateway_decision_shutdown_cancel.py` 7 项（真实本地 HTTP 与原账本）：
  - 在途决策被取消后 0.8 秒内返回 `stale/host_shutdown`，调用账 failed 1、无 running；
  - 不新增冷却，复位后同一连接照常成功；关闭后新决策不联网；设置撤销与关闭同时发生时报设置撤销；索引满仍报 `notification_capacity`；
  - Gateway 收尾顺序为停止事件、取消决策、停 HTTP；取消模块抛错时收尾照常完成，只记 `{"error_type": "RuntimeError"}`。
- **新测试** `test_decision_curator_plugin_concurrency.py` 5 项（后台 `curator` 与前台 `skill_tool` 共用同一连接，本地服务按 lane 选择性阻塞）：
  - 后台慢时前台照常完成，两边各记一条原账；
  - 线程变更只撤销前台；owner 级改 `curator` 只提前撤销后台，前台返回后按整份策略版本作废为 `policy_changed`；
  - 后台超时后同连接前台返回 `cooldown/connection_backoff` 且不发请求；关闭时两者一起取消。
- 两个文件连跑 6 次 12/12 通过。
- **变异验证**：14 种各自使新测试失败（不置关闭标记、登记不看标记、不标记或不取消句柄、计数错、`_revoked` 忽略关闭、关闭优先于设置撤销、登记失败不区分关闭与容量两个方向、登记被拒后照常发送、收尾不调用/不包 try/记异常正文/在停 HTTP 之后才调用）。子进程带 `PYTHONDONTWRITEBYTECODE=1`，结束后还原原文件。
- **回归**（基于 main `6c3ffc2ad`）：全部 `test_decision_*`、引用 `gateway_process`/`decision_policy` 的测试与架构守卫共 63 个文件，1428 passed、2 skipped、3 xfailed、1 failed。
  失败的是 `test_subagent_first_request_selection.py::test_real_child_first_request_capture_matches_actual_provider_payload` 的第一组参数。它在不含本改动的 main 上同一组合里同样失败，单独或整文件运行 40/40 通过。二分表明不是单个前置文件触发，要前面约 29 个决策测试文件叠加才出现。根因后来查明是线程中断标志按 ident 复用，已由上一节的修复（main `761ef2ab2`）解决。

## 子代理 lesson 结构化来源 `record_lesson`（2026-09-25，已合入 main `52e0190e1`；已端到端真实验收）

- **改动**：
  - 新增账本合同 `subagents/lesson_ledger.py` 与子代理专属工具 `agent_core/runtime/record_lesson_tool.py`。
  - 路径登记在 `AgentRunWorkspacePaths.lessons_jsonl` 与 `SubAgentTask.agent_run_lessons_jsonl`。
  - coding/read_only 预设、`ROLE_BASE_TOOLS` 和层级缺省候选都带上该工具，`_DEFAULT_HIDDEN_TOOL_NAMES` 对主线程隐藏它。
  - `runner_result_service` 读回账本合并进 `lessons`；没有结构化输出时也记账本候选。
  - `memory_candidates` 为账本条目生成 `subagent_lesson` 候选：`applies_when` 取 `when_to_use`，证据引用账本条目。
  - runner 提示在有授权时多一条可选软引导。设计见 DESIGN_LEDGER 同名条目。
- **新测试** `test_subagent_lesson_ledger.py` 31 项，只用假件，不调 provider：
  - 工具：身份只取 runner 上下文，参数里伪造的 run/task/attempt 被忽略。主线程、未知 run、没有 manager 时都返回 `TOOL_UNAVAILABLE` 与 `not_started`，也不建账本。Schema 只有四个有界必填字段，`additionalProperties=false`。
  - 工具（续）：缺失、空白、非字符串、超长逐项结构化拒绝，边界值通过；空白规范成单行，渲染固定四行。同参数（含空白变体）只记一次。第 6 条报 `LESSON_LIMIT_REACHED`，重试已记条目仍按幂等返回。两条满长中文之后第 3 条报 `LESSON_LEDGER_BYTES_EXCEEDED` 且不写入。坏行与符号链接都 fail-closed，外部文件不变；底层追加使用 O_NOFOLLOW。
  - 读回：篡改、他 run、非规范、错版本（含 `true`）、重复与坏 JSON 行全部拒绝并计数。超过 5 条只取前 5 条，超字节整本不采用，加锁失败报 `unreadable` 且不阻断交付。
  - 收口：自然回复（没有结构化输出）时，`output.json` 与 runner result 的 `lessons`/`lesson_count` 含账本经验。结构化 lessons 在前、账本在后，按文本去重。没有账本时输出与工作日志保持原样；dry-run 不记候选。
  - 候选与提案：账本经验成为 `pending_review` 的 `subagent_lesson` 候选，带 task/run 来源，`applies_when` 取 `when_to_use`，证据引用 `lessons.jsonl#<lesson_id>` 并带 task/run/attempt。开启自学习时每条经验一个待确认提案；重放同一结果后候选 occurrence 仍为 1，提案文件不变。关闭自学习时只有候选。候选服务抛错时结果照常交付，工作日志记 `memory_candidates_error`。
  - 暴露与提示：coding/read_only 预设、角色默认和层级缺省候选都含该工具，并受父级上界约束；后台主代理默认目录不含。真实 SimpleAgent 注册表里，主线程快照、tool_search、list_tools 都看不到它，子代理快照可见；关闭子代理时不注册。提示只在有授权时多一条可选引导，并保留"不要输出 SUBAGENT_RESULT、状态 JSON"。
- **变异验证**：24 种全部被杀死。覆盖身份、不可用、条数/字节/字段上限、幂等、读回 id 与 run 归属、合并与去重、自然回复记候选、适用场景、账本引用、主线程隐藏、预设暴露、提示条件、候选失败隔离、符号链接与 O_NOFOLLOW、注册、`not_started`、重复纯文本、dry-run、读回异常。
  - 首轮有 2 种存活：他 run 行与合法行同 id，被去重先挡住；符号链接目标是坏 JSON，被坏行判定先挡住。补强测试后两者都被杀死。
  - 每种变异在 `PYTHONDONTWRITEBYTECODE=1` 子进程运行，逐字节还原并用 sha256 核对；全部完成后删除 `__pycache__`，从干净字节码复跑。
- **收集 focused 文件时必须排除 `agent_py_agent/tests/run_tests.py`**：它在 import 阶段就执行真实 CLI 冒烟，包括一次真实 provider 请求，且 owner home 仍是 live 目录（conftest 的隔离夹具只作用于测试函数）。本轮按名字 grep 时误收过一次，影响见交接。
- **结果**（基于 main `019dd0dcd`）：新测试 31 passed。focused 共 262 个文件，按名字 grep 出引用改动面的全部 pytest 文件，排除 `run_tests.py`，另加 `test_architecture_guardrails.py`。合计 5001 passed、3 skipped、28 xfailed、4 xpassed。xfail/xpass 集合与 origin/main 相同，均为既有 EXEC-31b/AUDIT-02 标记。无需修改任何既有测试。
  - 其余门：Ruff、doc sync、import boundaries 通过；strict code-size blocked=False，与 origin/main 按 identity+severity 逐项比对无新增；`git diff --check` 与 clean-package 通过。产品代码与新测试都没有 `*args/**kwargs` 形参。线上 CI 没有作为验收来源。
- **未验**：没有真实 TUI 验收；被取消 run 的账本不经结果收口；主线程经验记录不在本片范围。

## 决策实验对照记录、证据评估与授权内自动晋升（2026-09-25，本地分支 `claude/decision-experiment-records`，待审）

- **改动**：只观察实验调用经原账结算后，结算视图随 `DecisionOutcome.experiment` 带回，生成 `decision_experiment_record.v1`（身份、配置版本、基线/候选名单、结算）写进 Gateway 请求记录 `experiment_records`；回合正常收尾按结构化工具账补写实际用量；只读评估（≥3 可比较、全部 charged、召回 1.0、有节省）沿授权回执指针回读原请求记录，经 `user_config decision_read` 暴露；`/experiment apply` 授权内经原设置 CAS 一次性晋升 `points.skill_tool.mode`，回执写在请求记录。
- **新测试** `test_decision_experiment_records.py` 27 项：复用 `test_decision_experiment_send_gate.py` 的本地 HTTP `lab` 夹具，断言记录结算字段逐项等于原账快照、record_id 等于原调用编号、基线等于原 Registry 实际展示、候选短名单/延迟名单、无用户正文/候选说明/模型回答/端点；未进入原账预留不写记录、普通决策无条目；真实请求文件与回合转换锁下的观察拆分与去重、8 条上界、关闭/停止/换代次时抛中断且不写、写盘失败吞掉；收尾实际用量来自工具账、6 类未知（未完成、无账、空名、非字符串名、非字典、混入坏记录）、只补写本执行代次、普通收尾零 I/O、停止收尾不补写。
- **新测试** `test_decision_experiment_evaluation.py` 33 项：规则矩阵（样本不足、召回<1 两种、零节省、四种非 charged）、六类不可比较样本不计不阻断、快照外工具不稀释召回、外来 owner/线程/点/schema/未完成/空编号忽略、去重与 8 条窗口、证据链跨四个目录按新到旧、越界/隐藏/缺失指针终止且加载器自身拒绝越界编号、跨会话/成环/16 条上限；`user_config decision_read` 暴露只读评估且紧跟授权信封、无授权时输出不变、读取失败给 `unavailable`。
- **新测试** `test_decision_experiment_promotion.py` 18 项：真实设置服务与真实 E1 授权入口；apply 语法与回执 v2 指针、一次 CAS 晋升（revision 只前进一次、前后值与证据编号）、重放与重启不二次晋升、崩溃遗留 promoting 不重试（内存与锁内两种）、observe 授权永不晋升但读路径给出建议、锁内读后用户改另一字段则 `settings_conflict` 且保留用户值、线程/用户层/默认配置三种后改跳过、撤销/到期/被替换跳过、四种弱证据（单样本、召回<1、usage_unknown、外来 owner）即使 Jev 候选完美也不晋升、被拒授权与模型工具均无授权/晋升路径。
- **新测试** `test_decision_experiment_gateway_turn.py` 4 项：真实 `_run_gateway_ask`（原 SimpleAgent.run/设置/Registry/RuntimeDB/请求文件），主模型为 typed 假答复并发起一次真实原生工具调用，Jev 连本地 HTTP：一次实验一条记录、结算 charged、实际工具名来自真实工具账；普通回合不写任何实验键；两条历史授权链上样本加本轮样本时 apply 晋升、observe 不晋升。
- **改动的原有测试**：`test_decision_experiment_command.py` 把 apply 从无效用例移出（改用 `promote`/`Apply`/`["apply"]` 等无效形态）；`test_model_call_input_budget.py` 断言结算视图额外的结算码/调用编号/估算/上界字段，快照部分与原值相同。
- **结果**：相关 10 文件从干净字节码 246 passed；全部 `test_decision_*.py` 与 `test_gateway_*.py` 90 文件（变基到 `1132fd9d0` 后，含主线新增的两个 S2 文件）2196 passed、2 skipped；相邻 20 文件 604 passed；`check_import_boundaries.py` 0 findings。变异 37 项 36 项被杀（清单与唯一等价变异说明见 [E1 交接第三片](docs/tasks/DECISION_MODEL_EXPERIMENT_E1_HANDOFF.md#第三片e2-对照记录f1-证据评估与授权内自动晋升2026-09-25)），每项 `PYTHONDONTWRITEBYTECODE=1` 子进程运行、sha256 原样恢复。未启动 Gateway、未调用真实供应商；真实 `/experiment apply` 验收待做。

## 自学习 S2：待确认 Skill 提案审核顺序 `skill_proposal_review`（2026-09-24，已合入 main `1132fd9d0`）

- **改动**：新增 `capability/decision_skill_proposal_review.py`；`POINT_RUNTIME_SCOPES` 登记 `skill_proposal_review: owner_background`；AgentConfig/YAML 三字段默认 off/null/null；TUI 决策菜单加“Skill 提案审核顺序（用户长期）”；`skills proposals list` 在点返回采用结果时才重排展示、加标签和 `review_order` 块。设计见[接入设计 P5-C 自学习 S2](docs/design/DECISION_MODEL_INTEGRATION.md#p5-c-自学习-s2待确认-skill-提案的审核顺序-skill_proposal_review)。
- **新测试** `test_decision_skill_proposal_review.py` 42 项。用真实 S1 提案，只替换决策服务三个边界；替身签名与原服务的显式关键字参数一致。
  - 资格：0/1 条待确认、31 条、点未登记都不建阶段；2 条和 30 条各一次请求；已确认/已拒绝的提案不计数，位置也不动。
  - 关闭：阶段错误、点关闭、只开了别的点时不准备材料。
  - 绑定：阶段为 owner_background、操作编号绑定待确认集合、每次新 run 编号且无 thread，source_refs 精确。
  - 采用：observe 保留原序；apply 按 优先→普通→稍后/可能重复 分组，组内保持原序，标签和 `review_order` 块正确；全 normal 顺序不变、无标签。
  - 回答：6 种坏回答（缺题、多答、错题号、逐题错误、错类型、越界值）与 3 种非选择都保留原序；候选版本不符、配置复核失败、超期也保留原序。
  - 等待期间变化：6 种（被拒绝、版本号变、记录 hash 变、正文变而 hash 未变、新增待确认、文件损坏）都保留原序；正文被改而 hash 未更新的草稿不发请求。
  - 隐私：外发不含提案/候选/任务/运行编号、目标 Skill 名、路径或密钥，含 3 段不可信数据边界和 `<redacted>`；题目只有 7 个固定候选且说明“不决定确认或拒绝”；经验摘录与 S1 模板一致、不含来源段、按 240 字截断。
  - 零写入与取消：确认/拒绝/写入入口被替换成失败也不触发，提案、skills、候选账本逐字节不变；apply 与 observe 下等待期间的取消、配置复核中的取消都上抛；决策边界的 InterruptedError/ToolCancelled 上抛；普通错误回原序，但不掩盖同时发生的取消。
- **新测试** `test_decision_skill_proposal_review_integration.py` 19 项。真实 CLI、owner 解析、设置与模型目录、决策服务、worker、响应解析、冷却表和调用账，只替换 `post_json`（窗口门与请求头照常执行）。
  - 字节：点关闭、总开关关闭、1 条、0 条时零请求，`list` 与 `--json` 输出和按 S1 格式重建的期望逐字节相同。
  - observe：两次调用各一次请求，各有一条 purpose=decision、finished 的调用账，输出不变。
  - apply：`--json` 重排和 `review_order` 块、中文说明行与四种标签正确。
  - 真实请求：只含 state/questions/model，别名化、已脱敏、无编号和路径。
  - 失败路径：请求期间提案被拒绝、4 种真实非选择或越界回答、供应商错误后同进程冷却（第二次零请求）、0.2 秒超时都输出原列表。
  - 中断：命令以 `SKILL_PROPOSAL_CLI_INTERRUPTEDERROR` 失败，不打印列表。
  - 容量：30 条最长草稿通过真实窗口门并完成重排。
  - 设置与菜单：YAML/dataclass 默认 off；字段只允许 owner，线程写入报“作用范围”；超时继承后台期限；TUI 用户长期菜单可设 apply，线程菜单不显示本点。
- **一次性对照（未提交）**：在同一临时 home 上分别运行 origin/main（`d9a34fc76` 与变基后的 `4b9d684ea` 各一次）与本分支的 CLI 子进程，执行 `list`、`list --json`、`list --status pending_confirmation`。默认配置、点关闭、总开关关闭、1 条、0 条五种情形的输出逐字节相同。
- **变异验证**：38 种变异全部被杀死。涉及：
  - 资格：点登记、上下界 2/30、只计待确认。
  - 阶段：错误与关闭、observe 被采用、scope 改 thread、run 编号为空。
  - 采用前复核：候选版本、配置复核、重读提案、期限、重算草稿 hash、篡改拒发。
  - 材料与隐私：不可信边界、脱敏、摘录带来源段、摘录上限、别名换成真实编号。
  - 回答与排序：回答数量/逐题错误/类型、可能重复分组、标签、排序、已处理条目位置。
  - 取消：入口吞中断、可选错误掩盖取消、决策后不查取消。
  - CLI 与配置：不调用点、丢 `review_order`、丢标签、不重排；schema 改线程范围、dataclass/YAML 默认 apply、TUI 缺显示名。
  每次在 `PYTHONDONTWRITEBYTECODE=1` 子进程运行，逐字节还原并用 sha256 核对；全部完成后删除被变异模块的 `__pycache__`，从干净字节码复跑 85 passed（含 S1 的 24 项）。
- **结果**（变基到 main `4b9d684ea` 后）：与改动直接相关的 56 个测试文件加 `test_architecture_guardrails.py` 共 1428 passed（决策全部测试、S1、TUI 菜单、外部材料组合、CLI 解析/参考/配置、配置校验、user_config、Gateway 选模观察/采用、打包、架构守卫）。本分支产品代码和新测试都没有 `*args/**kwargs` 形参。Ruff、doc sync、import boundaries、strict code-size（blocked=False；与 origin/main 按 identity+severity 逐项比对无新增）、`git diff --check`、clean-package 通过；线上 CI 未作为验收来源。
- **未验**：没有真实 Jev 或真实 CLI 验收，不证明审核顺序对用户有用；CLI 的决策调用账只在进程内，不进入会话展示或持久账本。

## 决策线收尾的全仓回归（2026-09-25，main `5fca6a194`）

- **结果**：`python -m pytest agent_py_agent/tests -p no:cacheprovider` 共 24 分 19 秒，21,406 passed、4 failed、21 skipped、32 xfailed、5 xpassed。
- **4 个失败逐一单独复跑归因**：
  - `test_architecture_guardrails.py::test_product_code_has_no_var_keyword_service_interfaces`：第 17 项实验授权回执 `_receipt(**fields)` 违反"产品代码不用 `**kwargs` 服务接口"。已在本分支改为显式关键字参数，守卫与实验 3 文件合计 118 passed。
  - `test_plugin_workspace_context.py` 中两项：单独运行稳定失败，报 `'MCPStdioClient' object has no attribute 'activation_ref'`。来源是插件调用前的激活复核（`58bb0441a`）；测试替身没有该属性。属插件线，已告知主线 owner，本分支不改。
  - `test_subagent_capability_compact.py::test_child_same_turn_reuses_selection_and_exact_provider_surface[None]`：单独运行 3/3 通过，判为全仓负载下的时序偶发。
- 按 AGENTS.md 的频率约定，全仓回归只在决策线收尾时跑这一次；各分支远端提交前的严格门仍以 focused tests 为准。

## 验证分类：返回码 126/127、pytest 范围与 && 串联（2026-09-25，本地分支 `claude/verification-exit-scope-chains`）

- **新测试** `test_verification_project_facts.py`：
  - 返回码 126/127 记 `environment_unavailable`，普通非零仍是 failed。
  - pytest 10 种参数形状：无参数、目录、子目录为 full；文件、`::node`、`-k`、`-kEXPR`、`-m`、`--lf`、`--deselect=` 为 targeted。
  - `make test && make lint` 返回 0 时两段都记 passed；带 cd 前缀时两段的 cwd 都是 cd 目标；`make test && echo done` 只记测试。
  - 返回 2、使用 `;` 或 `||`、串联后接管道时都不记。
- **新测试** `test_verification_runtime.py`：真实 `record_tool_verification` 为通过的串联落两条证据，信封 `verification_evidence` 为末条、`verification_evidence_chain` 为完整有序列表。
- **新测试** `test_decision_delivery_quality.py`：串联前面几段的事件同样成为焦点；串联与末项不一致时整点放弃、零请求。
- **原有测试**：分类测试改走新的列表接口；原"cd 后接 `&& echo done`"的拒绝用例按新规则移到串联测试里（返回 0 时证明测试通过）。
- **变异验证**：9 种变异各自使测试失败——126/127 记 failed、pytest 回退旧范围规则、不识别 `-kEXPR` 连写、不识别筛选开关、串联非零仍记、放行 `;`、不附串联字段、交付复核忽略串联、交付复核不核对末项一致。"交付复核忽略串联"最初只被不一致用例杀死；把正例的前段改为只在串联里出现的 `build` 后，正例也能杀死。还原后逐字节一致（`PYTHONDONTWRITEBYTECODE=1`）。
- **回归**：引用验证账、分类器、运行事实或归档投影的 17 个测试文件 345 passed、4 xpassed。4 个 xpass 都在 `test_tool_unresolved_runtime_issue_guard.py`，main 上同样出现。

## 决策实验授权入口、经验输入上界与发送硬门（2026-09-24，本地分支 `claude/decision-experiment-send-gate`，待审）

- **改动**：`/experiment observe skill_tool <时长> <HTTP次数> <输入token上限> <任务>` 冻结参数、Gateway 主轮绑定后首个模型调用前单次授权；信封 v2 必带 `input_bound_policy="empirical:jev_wire_bytes.v1"`；经验上界 `C = ceil(B/2) + 256×Q + 1024`（只在 skill_tool、Q≤64、state≤4096 字节、C≤57,600 内）；原账只收带 `kind=empirical` 的上界对象并签发单次发送许可；传输层在最终字节生成后、DNS/连接/遥测前复核许可；终态后保守结算。
- **新测试** `test_decision_experiment_send_gate.py` 39 项，复用 `test_decision_capability_http.py` 的 `capability_http`/`surface` 夹具，在本地服务 `verify_request` 处统计 TCP accept、在服务端记录原始字节：9 类缺授权/缺预算（无授权、能力关、撤销、到期、账本代次变化、HTTP 用尽、C 超剩余、越标定两种、v1 信封）零连接且无调用记录；5 类预留后绕过（撤销、换密钥、篡改正文、篡改端点、阶段身份改变）加抛错遥测均 `ProviderSendRefused`、零连接、预算 `send_refused`、不进连接退避；默认关闭只建一次普通阶段、不建实验阶段、零连接无调用记录；授权发送单连接单 POST、服务端正文 sha256 等于预留摘要、`reserved_http=1`、`charged=provider=X`、上界 kind empirical、结果不采用且 prompt/schema 不变、能力推荐观测为 observe/未采用、第二次 `http_budget_exhausted` 不再连接；X=C+1 关闭为 `input_bound_violated`；挂起到期 `timed_out` 保留整个 C、迟到响应不重开；另有许可静态绑定/运行态顺序和 C 的样本余量、单调、越界与边界单测。`test_decision_experiment_command.py` 34 项：命令解析与冻结、HTTP 入口丢弃客户端 system_task、单次授权与提示、重放/重启/Compact 再入不再授权、拒绝与关闭回合、核心钩子时序。
- **扩展**：`test_model_call_input_budget.py`（带标签上界、单次许可、拒绝与绕过结算）38 项、`test_decision_experiment_authorization.py`（v2 口径、只发布普通模式 off 的点、改写旧“实验直连总拒绝”用例）35 项、`test_gateway_strict_request.py`（许可信封约束、拒绝在遥测与连接之前、普通请求字节不变）54 项。
- **结果**：五文件从干净字节码 200 passed；定向相关集 118 文件 3421 passed、2 skipped、4 xfailed、1 xpassed（基于 `55f72b40c`）；变异 36 项全部被杀（清单见 [E1 交接](docs/tasks/DECISION_MODEL_EXPERIMENT_E1_HANDOFF.md#第二片experiment-授权入口经验输入上界与发送硬门2026-09-24)）。未启动 Gateway、未调用真实供应商；首次真实授权发送仍待做。

## 验证命令分类：cd 前缀与管道（2026-09-25，本地分支 `claude/verification-command-shapes`）

- **新测试** `test_verification_project_facts.py`：
  - `cd <绝对路径> && python3 -m pytest …` 与 `cd "相对目录" && pytest` 都按 cd 目标归类，记录的 cwd 与项目根为该目录。
  - cd 到不存在的目录、cd 后再接多段、`cd …;`、`cd … ||` 都不算证据。
  - 管道、`|&`、后台 `&`，以及 cd 前缀后接管道，都不算证据；引号内的 `|` 仍能归类。
- **新测试** `test_verification_runtime.py`：真实 `record_tool_verification` 把 `cd` 前缀的失败测试记在 cd 目标项目下。
- **变异验证**：去掉管道检查、关闭 cd 剥离、去掉目录存在检查、记录原 cwd、放行后台 `&`、cd 前缀放行 `;`、项目根按原 cwd 查找，7 种变异各自使新测试失败。其中"去掉目录存在检查"最初未被杀死：测试用的缺失目录在项目外，本来就找不到项目。改为项目内的缺失子目录后被杀死。还原后逐字节一致（`PYTHONDONTWRITEBYTECODE=1`）。
- **回归**：引用验证账或分类器的 8 个测试文件 193 passed，含交付复核焦点的单元与组合测试。

## TUI 决策菜单接入点跟随 schema（2026-09-25，本地分支 `claude/decision-tui-points`）

- **新测试** `test_tui_decision_menu.py::test_menu_points_follow_the_schema_registry_and_reset_can_list_pre_recall`：菜单接入点与 schema `POINTS` 逐项一致；owner 覆盖 `points.pre_recall.mode` 后，该字段可编辑，恢复继承标签不崩溃并显示中文名。
- 两个旧真实按键测试原先写死"召回后重排在第 4 项"，现改为按菜单顺序算位置。
- **变异验证**：菜单漏掉 `pre_recall`、缺中文显示名两种变异都使新测试失败；还原后逐字节一致（变异子进程带 `PYTHONDONTWRITEBYTECODE=1`）。
- **回归**：`test_tui_decision_menu.py` 与 `test_external_material_order_integration.py` 26 passed。

## 交付复核焦点 `delivery_quality`（2026-09-24，本地分支 `claude/decision-delivery-quality`）

- **改动**：新增 `tool_context/decision_delivery_quality.py`；`_tool_loop_service._record_tool_call` 的原阅读提示接缝改为 `_optional_result_hints`，依次调用 `external_material_order_hint` 与 `delivery_quality_hint`（按工具名互斥）；`POINT_RUNTIME_SCOPES` 登记 `delivery_quality: thread`；AgentConfig/YAML 三字段默认 off/null/null；TUI 决策菜单加“交付复核焦点”。设计见[接入设计 P5-C 质量提示首片](docs/design/DECISION_MODEL_INTEGRATION.md#p5-c-质量提示首片交付复核焦点-delivery_quality)。
- **新测试** `test_decision_delivery_quality.py` 102 项（只替换决策服务边界）：未登记严格空操作；off/阶段错误/他 run 阶段不准备材料；observe 与 deadline/cooldown/error/stale 保留原结果；apply 只渲染被选焦点的宿主事实（含其后修改、targeted 说明）；外发只有脱敏请求与焦点别名事实（无路径/命令/输出/时间/改动路径），不同项目根得到不同别名；28 种不合格来源零请求（单焦点、全通过、13 个焦点、非 run_command、归档 id/run/task/scoped/事件不一致、未归档、重复归档、他 run/task 来源、两种收口标记、超长/空请求、无事件、重放、重复事件号、坏 id/kind/退出码/root、坏 state、非 stale 状态、请求含两种 URL 查询串）；12 种身份不符在扫描归档前返回；子代理零请求；2/12 个焦点、仅修改、仅失败的边界各一次请求；同 project/kind/scope 只留最新事件；11 种非选择或坏答案（四种保留项、两种越界编号、多答、逐题错误、错类型、错题号、缺答）无提示；选中本次事件无提示；9 种等待期间来源/参数/收口变化、期限/候选版本/配置失效、配置复核期间来源变化与超期消费均丢弃；4 种可选错误保留原结果；取消在决策后、配置复核中和与可选错误同时发生时都上抛，中断上抛；原 `_record_tool_call` 下 off/observe/apply 的 text/native/IR 同一展示与原结果/归档不变；run_command 记录只触发本点一次请求。
- **新测试** `test_decision_delivery_quality_integration.py` 5 项：真实 `record_tool_verification`（局部测试失败→改文件→全量测试失败）逐步经原 `_record_tool_call`，原设置/模型目录/worker/调用账，只替换 `TypesafeDecisionBackend.decide`；off/observe/apply 下 text/native/IR 同一段、apply 恰好一段提示、决策请求与 purpose=decision 账各 0/1/1 条、请求绑定本 run/thread/归档引用且不含私有路径/命令/输出、决策时刻与结束时 owner 根文件字节及归档列表不变；YAML/dataclass 默认与 owner/thread 范围；原 TUI 菜单可把本会话模式改为采用建议。
- **变异验证**：59 种各自使新测试失败，改回后按 sha256 核对原文件：接线丢点/丢换行、未登记不空操作、非 run_command 扫描归档、接受重放/子代理/两种收口/超长请求/无请求、去掉归档事件/id/run/task/scoped 一致、焦点下限 2→1、上限 12→13 与 12→11、不要求当前事件、去掉或只看 failed/只看修改、接受重复事件号、保留最早而非最新、修改方向反转、修改不比项目根、非 stale 算修改、去掉短标识与 id 校验、扫描不按 run/task 过滤、外发项目根、请求不脱敏、不拒 URL 查询串、忽略阶段错误/关闭/他 run、observe 被采用、不查候选版本/当前配置/最终来源/参数版本/绝对期限、接受多答/错题号/错类型/逐题错误、渲染当前事件、提示丢修改事实/targeted 说明、去掉 512 字符预算、决策后与配置复核后不查取消、中断被吞、可选错误掩盖取消、点未登记、TUI 缺菜单、dataclass/YAML 默认非 off。每次均以 `PYTHONDONTWRITEBYTECODE=1` 运行；全部完成后删除被变异模块的 `__pycache__` 并从干净字节码重跑 107 passed。
- **结果**（基于 main `ab23a2666`）：与改动直接相关的 56 个测试文件 1443 passed、1 xpassed（`test_timeout_budget_locked.py::test_native_protocol_unified_counts_ir` 为既有非严格 xfail，main 上同样 xpass）。同组合共跑 8 次，第 1 次出现 1 failed，因输出被截断未记下用例名，其后 7 次全量均通过，新文件单独 25 次、时序敏感的 10 个既有文件 5 次也均通过，未能复现，暂按机器负载下的偶发记录；Ruff、doc sync、strict code-size（blocked=False；与 main 的 findings 按 identity+severity 逐项对比无新增，`_record_tool_call` 的临界长度项因接缝抽出而消失）、`git diff --check`、clean-package 通过。没有真实 Jev、真实主模型或 TUI 验收，不证明交付质量提升；线上 CI 未作为验收来源。

## 召回与记忆决策点的真实验收方法（2026-09-25，本地分支 `claude/decision-p5a-real`，只改文档）

- **已知漏召回样本先离线标定**：测试者用产品原函数（BM25 顺序、均值中心化余弦 ≥0.30、RRF、原片段生成函数）和同一嵌入类复算限定范围的混合检索，只挑"整句召回不到、某个片段能召回、基线留有空槽"的问题做 off/apply 对照，避免拿不可能有收益的样本下结论。标定只输出编号、条数和相似度，密钥在进程内读取、不打印。
- **旁观器只记结构化选择**：召回前决策点只记录所选片段 ID，关系点只记录每对的 ID 与关系标签，嵌入调用只记条数、耗时与成败；不保存问题、片段、记忆正文、密钥或请求头。
- **同一快照在原路径上重放**：Curator 对照必须从同一快照在原 home 路径上依次跑 off 与 apply。会话工作区按绝对路径建键，把 home 复制到别的目录会读不到原对话（本次作废的一次运行已留证）。
- 结果与证据见[真实验收](docs/tasks/DECISION_MODEL_REAL_VALIDATION.md#14-p5-a-召回前补充查询语义召回下的真实收益2026-09-25main-ab23a2666)。本分支没有代码改动，所以不跑 pytest；doc sync 与 diff 检查照常执行。

## 自学习 S1：子代理经验生成待确认的 Skill 提案（2026-09-24，本地分支 `claude/self-learning-skill-proposals`，待审）

- **改动**：新增 `capability/skill_proposals.py`（提案 schema `my-agent.skill-proposal.v1` 与 `SkillProposalService`）和 CLI `my-agent skills proposals list|show|confirm|reject`；配置 `enable_self_learning`（默认 false，YAML、AgentConfig 与布尔规范化同步）；owner 布局登记 `owner_skill_proposals_dir = <owner_home>/data/skill_proposals`（不进初始化目录清单，首次生成提案才创建）；组合根只在开关开启时给子代理 manager 注入服务，`runner_result_service` 在记录候选之后调用，异常只写工作日志。
- **新测试** `test_skill_proposals.py` 24 项：
  - 开关：随包 YAML 与 dataclass 默认 false，带引号的 "false" 规范化为布尔；默认关闭的 SimpleAgent 不注入服务、不建目录；开启后服务路径落在飞书用户 owner 自己的 `data/skill_proposals` 与 `skills`。
  - 生成：一条 lesson 恰好一个提案，同 run 重放和第二个 run 合并都不重复；提案含来源任务/运行、触发原因、拟保存内容和适用场景，ID 等于 sha256(candidate_id + content_hash) 前 24 位；finding、`model_inferred` 自省 lesson、无 task/run 来源、类型不是 lesson 的候选以及非 Candidate 对象都被忽略，且不建目录。
  - 迁移：真实 `MemoryMigrationService.apply()` 迁走并删除同级 `data/learning_drafts`，提案目录逐字节不变、提案仍可列出。
  - 确认拒绝矩阵 7 种：旧版本号、目标已存在、候选正文被改写、候选脱敏、候选被拒绝、候选已删除、草稿被篡改，各返回对应错误码，提案文件与目标逐字节不变、暂存目录清空；guard `caution`（chmod 777）被拒；`os.replace` 失败与写回执失败都不留下目标 Skill。
  - 成功路径：确认后 revision 2、`committed`、回执 guard `safe`；确认前取得的快照看不到新 Skill，下一次快照出现 `owner:lesson-*`，其 content_sha256 等于草稿 sha256 且正文可读；已提交后再确认返回 `NOT_PENDING`。
  - frontmatter：含 `#`、引号和 CRLF 的经验文本经 `parse_skill_file` 解析后 name/description/when_to_use 与草稿一致；非法 ID、不存在、损坏文件分别返回 `INVALID_ID`/`NOT_FOUND`/`CORRUPT`，list 遇损坏文件关闭式失败。
  - runner：提案服务抛异常时结果仍 DONE、候选照记、工作日志记 `skill_proposals_error=RuntimeError`；接上服务时生成一条待确认提案；未接时只记候选、不建目录。
  - CLI：经真实 `cli.parser.main` 与临时配置完成 list→show→confirm→reject→按状态 list→重复 confirm（退出码 1、`NOT_PENDING`）；中文输出含来源、触发原因、正文、场景和带版本号的确认命令。
- **变异验证**：26 种变异全部被杀死——资格三项（来源、task/run、类型）、O_EXCL 改 O_TRUNC、跳过版本/待确认/目标存在/来源 hash/脱敏/状态/缺失检查、guard 改 force、跳过草稿 hash、不回滚已装目标、不清暂存目录、不替换 `#`、不规范换行、runner 放任异常/不调用、组合根忽略开关、owner 投影沿用本地主用户目录、目录改名 `learning_drafts`（迁移测试失败）、开关不做布尔规范化、CLI confirm 调成 reject、CLI 退出码恒 0、YAML 默认改 true。每次在 `PYTHONDONTWRITEBYTECODE=1` 子进程运行并逐字节还原；之后删除被变异模块的 `__pycache__`，从干净字节码复跑 24 passed。
- **回归**（基点 main `d3d66e56c`）：runner 结果、候选、Skill/guard、Memory 迁移、CLI 解析、owner 布局、配置与组合根相关 136 个测试文件 2299 passed、3 skipped、6 xfailed。ruff、doc sync、strict code-size（blocked=False；与 origin/main 逐项比对 finding 身份和级别无新增）、`git diff --check`、clean-package 通过；线上 CI 未作为验收来源。
- **未验**：没有真实 TUI 验收（真实子代理产出 lesson→用户确认→新回合读到 Skill）；S2（Jev 对待确认提案的审核排序）未做；并发确认只由 owner 文件锁保证，未做多进程压测。

## 召回证据写进上下文包（2026-09-24，本地分支 `claude/decision-recall-evidence`）

- **新测试** `test_decision_pre_recall.py` 2 项：正式召回只把补充查询真正追加的记录编号记进 `supplement_entry_ids`，来源清单区分 baseline/supplement 且不含正文；上下文包写出 `recalled_refs`、`recall_findings`，提示段字节与不带证据时逐字相同。
- **变异验证**：4 种各自使测试失败——不记补充编号、来源恒为 baseline、丢发现码、提示段泄露来源清单。改回后通过（变异子进程带 `PYTHONDONTWRITEBYTECODE=1`）。
- **相关回归**：上下文包、召回决策、记忆路由、运行上下文相关 15 个文件 314 passed。

## 主会话选模采用模式的问题说明（2026-09-24，本地分支 `claude/decision-selection-question`）

- **新测试** `test_gateway_model_observation.py::test_question_explains_usage_tags_and_apply_asks_for_the_best_semantic_match`（observe/apply 两档）：从冻结的决策请求体读回问题说明，两种模式都含用途标签说明；采用模式要求按任务语义挑最合适的候选且不再写"本次只观察"，观察模式保留"本次只观察"。
- **变异验证**：采用模式说明改回旧文、观察模式丢掉用途标签说明，各使一档失败。改回后通过（变异子进程带 `PYTHONDONTWRITEBYTECODE=1`）。
- **相关回归**：Gateway 选模观察、Gateway 采用、模型用途标签 3 个文件 81 passed。
- **真实验收**：同一 325k 字任务，修正前 Jev 仍选当前 M2.7（答案错误），修正后 Jev 选 M3、宿主核对后自动采用、答案正确；见[主会话真实交接](docs/tasks/DECISION_MODEL_MAIN_MODEL_LIVE_HANDOFF.md)。

## 模型用途标签（2026-09-24，本地分支 `claude/decision-usage-tags`）

- **新测试** `test_model_usage_tags.py` 12 项：
  - 快捷新增的标签去重、排序（全角逗号也能分隔）、持久化并出现在公开模型列表；没填不写键。
  - 大写、连字符、数字开头、超过 40 字、非字符串、字典、超过 16 个都拒绝，且不落盘；决策模型带标签被拒。
  - 标签不进 `resolved_model`，选定带标签的模型后运行时配置正常、没有标签属性。
  - 主会话与子代理的决策候选只在填写了时带 `usage_tags`。
  - `/model` 编辑表单预填并原样回存标签，编辑其它字段不会丢。
- **变异验证**：7 种各自使测试失败——校验结果丢标签、快捷新增白名单漏标签、决策模型可带标签、允许大写、主会话候选不带、子代理候选不带、表单不发送。改回后全部通过（变异子进程带 `PYTHONDONTWRITEBYTECODE=1`）。
- **相关回归**：模型目录、服务商与采样、共享目录、决策全部、Gateway 选模观察与采用、子代理首请求选模、TUI 模型与决策菜单共 63 个文件 1399 passed。
## 能力推荐观测写进 Gateway 请求记录（2026-09-24，本地分支 `claude/decision-capability-observation`）

- **新测试** `test_capability_presentation_observation.py` 6 项，走真实 Gateway 回合（只替换模型生成与决策后端）：
  - 采用与保留（abstain）两档：同一回合经过溢出重试仍只有一次决策、一条观测；字段只在白名单内，不含用户正文；采用时有短名单/延迟名单与 Skill 计数，保留时写 `retain_reason`；文件与内存请求一致，携带的展示值不落盘。
  - 决策失败记 `status=error, reason=provider_failed`；接入点关闭时没有观测、没有决策请求。
  - 写入器：保留其它键、最多 8 条、内存同步；回合已换代时抛 InterruptedError；写盘失败只放弃这一条。
- **改写断言**：`test_gateway_capability_compact.py` 原先用子串断言请求记录里没有 `capability_presentation`，新观测键包含该子串；改为按精确键断言"携带的展示值不落盘"，意图不变。
- **变异验证**：8 种各自使测试失败——不调用 observer、采用标记恒为假、丢保留原因、丢 outcome 原因码、不同步内存、不截上限、吞掉回合终结、写盘异常外抛。改回后全部通过（变异子进程带 `PYTHONDONTWRITEBYTECODE=1`）。

## 工具调用审批前/批准后复核（2026-09-24，本地分支 `claude/tool-precheck`）

- **改动**：`BaseTool.precheck_availability()` 默认 None（不复核），`MCPProxyTool` / `PluginProxyTool` 覆盖并给出 `MCP_CONNECTION_CLOSED` / `PLUGIN_ACTIVATION_UNAVAILABLE`；`ToolExecutor.execute` 在 ask 之后、返回 approval_required 之前复核一次，`_execute_authorized` 只对带 `approval_applied` 的裁决在 claim 前再复核一次；复核失败按 `TOOL_UNAVAILABLE` 拒绝、具体码进 `reported_error_code`、文案点明"审批前 / 批准后、执行前"；`ActionPolicy` 只在精确 binding 匹配时在 allow 证据里写 `approval_applied=true`；批准后拦下的结果不再贴"已批准"事实；两码登记进错误分类表；`mcp_managed_process.require` 先核对激活再看进程记录，停用导致的关闭报激活失效（→`TOOL_UNAVAILABLE`），`test_plugin_enable` 的旧期望 `TOOL_EXECUTION_FAILED` 同步改。
- **与设计稿的偏差**：内置工具的 `availability()` 会做 I/O（shell 沙箱探测等），复核改为代理工具 opt-in；`_execute_authorized` 参数已到上限，挂点 2 不加参数、放在函数入口按 `approval_applied` 判断。详见设计稿"实现偏差"。
- **新测试** `test_tool_call_precheck.py` 9 项：审批前拦下不弹框、批准后 claim 前拦下、复核通过则执行且复核恰好两次、内置工具 availability 不被再调、免审批调用不复核、复核异常按不可用、批准后拦下不带 applied_approval、两码登记且不可重试。
- **结果**：相关 59 个测试文件 1187 passed、20 xfailed（含新文件）；ruff 通过。已合入 main `42f7e57e1`，同一 wheel（dcc93407）双机 `runtime-step10m`。`test_host_command_stream::test_disconnect_cancels_only_original_wait_and_replay_cannot_execute` 在 59 文件并跑时偶发一次（rejected 变 outcome_unknown），单独复跑 main 与分支各 3 次均通过，属线程时序偶发，与本片无关，记录待观察。
- **真实 TUI 复验（测试机，默认确认模式，MiniMax-M2.7，2026-09-24）**：workspace-peek 的 tree 是只读工具不弹审批，改用临时安装的 savepoint-lite（save 为写操作）。场景一：模型调用 save 弹出"工具授权"框，另一 TUI 在等待期间 `/plugins disable savepoint-lite`（SUCCEEDED），随后点"允许一次"→ 结果为 `TOOL_UNAVAILABLE`，文案"工具在批准后、执行前复核时已不可用：原插件已停用或激活不可用"，该请求 `tool_operations` 为空、`agent_run.completed` ok、模型如实报告不可用并给出替代建议。场景二：发出请求 1 秒后停用插件，模型的调用到执行器时已被审批前复核拦下，没有弹出审批框，文案为"审批前复核时已不可用"，账上同样零操作。收尾已卸载 savepoint-lite、删除包文件、关闭 TUI。证据在仓库外 `releases/step10-combo3/precheck-acceptance-evidence.json`。
- **待办（测试机，插件生命周期）**：复验前用 `/plugins disable workspace-peek` 制造停用时，两次停用都返回 UNKNOWN（`effect_outcome_unknown:PLUGIN_CLEANUP_UNCONFIRMED`），随后 `/plugins enable` 以 `commit_state=not_committed, reason=activation_unsettled` 失败，插件停在 revoked。测试机上没有残留插件进程，进程会话表里该插件有一条 09-22 的 `status=unknown, stop_requested=true, exit_code=None` 旧记录，疑为清理无法确认的来源。按合同这是"未知不改成成功"，但缺少一条由结构化事实（进程确已不存在）结清旧未知记录的路径，需要另开一片处理；处理前测试机基线只剩 revoked 的 workspace-peek。 **已修（2026-09-24）**：重试停止时若记录已是 unknown、带上一次 cleanup 结果且两级实例按 PID 出生标识均不存在，则结清为 killed；见下文"后台续跑扩展目录与停用结清"，测试机真实复验待部署后进行。

## 插件来源结构化原因（2026-09-24，本地分支 `claude/plugin-source-errors`）

- **现场**：同伴在决策线测试机用 TUI 装 desktop-lite，相对路径失败、绝对路径成功，且报错和"包在 owner 墙外"是同一句"来源不可读、未获授权或格式无效"。核对合同：相对路径按 Gateway 校验过的会话工作区根（客户端随命令附带的 `workspace.cwd`，即 TUI 标题栏显示的目录）解析，不是终端所在目录——这一点本身正确，问题是三种失败混成一句、用户无从判断。
- **改动**：`plugin_sources.PluginSourceError(reason ∈ {not_found, unauthorized, symlink}, source, base)`；`read_plugin_source` 找不到时带出解析基准；安装与更新工具分别回执 `source_not_found`（文案含解析目录与"可改用绝对路径"）、`source_unauthorized`（不回显路径）、`package_<原因>`（格式无效），其余异常保留泛化文案。CLI_REFERENCE 与插件方案写明解析规则。
- **回归**：新增 `test_plugin_source_errors.py` 2 项（原因码、解析基准、链接、越权不回显路径；真实管理链上相对路径安装成功、缺失/坏包分别回执）；与 `test_plugin_management`、`test_plugin_update`、`test_plugin_install_store` 联合 61 passed。

## /plugins update 首片（2026-09-24，本地分支 `claude/plugin-update`）

- **改动**：目录新增 `update <插件> <新包路径>` 管理动作，映射管理工具 `plugin_update`（沿安装权限，模型不可见）。`plugin_update.plan_package_update` 纯计划：同操作重放 → 插件存在 → 新包 ID 一致 → 版本 CAS → 激活已结清 → 包确实不同；`PluginInstallStore.update_package` 在同一 quota→目录锁内保存新包 blob、一次写入安装表：install 回执（版本 +1，配置清空）后，旧配置能按新 `settings_schema` 规范化时紧接 configure 回执（版本 +2）恢复，结果带 `settings_restored/settings_reason`；只复用既有持久动作，安装表无新字段。
- **回归**：新增 `test_plugin_update.py` 4 项（真实临时 wheel：2.0 包替换并保留配置、版本 +2、复读；不兼容 schema 清空配置并报告 incompatible；已启用拒绝 `activation_unsettled`、未安装 `plugin_missing`、同包 `unchanged`；纯计划的版本/身份冲突不落盘）。与 `test_plugin_management`、`test_plugin_enable`、`test_plugin_commands` 联合 73 passed。
- **边界**：不停进程、不切激活，目标必须先 `/plugins disable`；不做双版本准备切换和 rollback；旧包 blob 保留。
- **真实验收（本机，runtime-step10q，2026-09-24 13:26）**：仓内源码临时把 workspace-peek 版本号改成 0.1.2 构建新包（不提交），放在 owner home 下。`/plugins disable workspace-peek` → 已停用并释放；`/plugins update workspace-peek <新包>` → 完成，installations.json 版本 0.1.1→0.1.2、revision 6、last_commit=install（该插件原本无私有配置，`settings_reason=none`）；`/plugins enable workspace-peek` → 完成，phase=active、revision 8；`/plugins@workspace-peek tree mediab --depth 1` 返回结构化目录结果。证据在仓库外 `releases/step10-combo4/plugin-update-acceptance.json`。

## 媒体压缩策略片 C：先看图后总结，让随图摘要在自动压缩里生效（2026-09-24，用户决定第 2 项）

- **根因**：两轮真实阈值压缩（M3、M2.7）的 checkpoint 都是 `probe_supported` + `summary_budget_exceeded`：阈值在窗口 90% 触发并整段压完，而片 B 的单次随图请求必须装进 80% 窗口减输出预留，结构上永远装不下；只有范围很小的手动 `/compact` 才走到过 B。
- **改动**：新增 `conversation/compact_media_digest.py`（含图回合分组、按摘要预算与 `compact_vision_digest_max_requests` 打包看图小请求、逐个发送并按 sha 前缀打标签、结果合成决定）；`compact.py::_summarize` 先看图再文字总结，图块统一投影为归档引用，要点进文字摘要指令；`_effective_media_decision` 预算门改为“最大的一次看图小请求”；`CompactMediaDecision.summarized_blocks` 与 checkpoint 双计数（`_media_blocks_archived/_media_blocks_summarized`）；一次都没成功才写 `compact_vision_failed_generation`，且不再让整次压缩失败。配置新增 `compact_vision_digest_max_requests`（YAML + dataclass，默认 4）。
- **余量不足立刻外置工具输出（2026-09-24 深夜，用户第 5 项"单回合超窗"）**：`tool_call_archive_record._headroom_forces_externalize` 用 preflight 同口径余量判断，本条输出估算 token 不小于剩余余量就给 `force_externalize`（read_file 分页也外置）并登记 `tool_context_window_overflow(reason=tool_result_headroom)`；开关 `tool_output_externalize_on_low_headroom`。`test_tool_output_headroom_externalize.py` 5 项（外置+登记+预检消费、同轮累加、余量够内联、开关关、预算未知回退）；`test_compact_native_ir_recovery.py` 超预算夹具显式关开关。
- **图块预留进估算（2026-09-24 深夜）**：`projected_model_context_components` 新增 `media_token_reserve`，预检、`_automatic_noop` 与恢复候选计量按已知图块数 × `input_media_token_reserve` 加进估算；`test_context_pressure_media_reserve.py`（3 项：加预留且分类加总不变、非展开集合与 0 预留不计、预检真实入口读配置）。
- **测试**：`test_compact_media_vision.py` 三项改为两步语义（准入按最大请求；看图小请求只带含图回合并保留图块、文字请求带引用与要点、checkpoint 计数；typed 失败同次回落、写同代次标记、不进熔断）；新增 `test_compact_media_digest.py` 6 项（分组顺序、按预算/次数打包与两种跳过原因、标签与首个 typed 失败停止并合成 partial 决定、非 typed 上抛与空回复算失败、部分成功写双计数与 `vision_digest_partial`、无图或非 B 决定零请求）。
- **真实验收（本机，runtime-step11c，main `94a2d0b4d`，2026-09-24 19:59—20:08，MiniMax-M2.7 官方，窗口 262,144）**：贴图问图后连续粘贴报告分片，上下文 65% → 部分四贴到压缩点，回合前自动压缩（checkpoint `forced=true` 是“整段压完”语义，非施压）。第一条会话 checkpoint 为 `archived_refs / probe_inconclusive`：M2.7 的结构化视觉探针两次都没给出正确颜色的工具回答（探针本身的既有波动，不缓存），策略在预算门之前就回落。随后经产品 `save_model` 给两条官方 M2.7 与 M3 档案声明 `input_modalities=[image,text]`（依据是片 B 第二轮两模型都拿到过 `probe_supported`），第二条会话 checkpoint 为 `media_policy=vision_summary`、`media_fact_source=declared`、`media_blocks_summarized=1`、`media_blocks_archived=0`、无 `media_policy_reason`，摘要正文含“附件内容要点”段——用户第 2 项“随图摘要在自动压缩里生效”成立。证据 `~/.my-agent/releases/step10-combo4/media-c-evidence.json`。

## 媒体压缩策略片 B：视觉摘要（2026-09-24，本地分支 `claude/compact-media-b`）

- **改动**：档案字段 `input_modalities`（开放小写标识符，`validate_model`/快捷新增白名单/`resolved_model`→`AgentConfig.model_input_modalities`，`/model` 表单新增"输入模态"，决策模型不接受）；`backends/vision_capability.py` 结构化探针（8×8 纯色 PNG + `my_agent_vision_probe(color)` 工具，只认颜色一致的 tool_use；typed 媒体拒绝→unsupported 缓存；答错/不调用→inconclusive 不缓存、最多 2 次；原生工具未证明→unavailable；网络/额度错不缓存；进程级缓存键 端点+模型+api_base、同键单飞）；`compact_media_policy` 骨架决定（auto 下 archived_refs + `vision_candidate`）→ 候选构造在范围内确有媒体块时才 `resolve_vision_candidate`（声明或探针）→ 摘要请求构造处 `vision_summary_admission`（视频/`input_media_max_bytes`/文字估算+图块×`input_media_token_reserve`≤摘要预算）；B 单请求经 `generate_bounded_compact_response(vision_summary=True)`，窗口错误/媒体拒绝/截断/超预算统一 typed `COMPACT_VISION_SUMMARY_FAILED`，只写线程 `compact_vision_failed_generation`（不进熔断），同代次下一次压缩选 A 并记 `media_policy_reason`；checkpoint 新增 `media_blocks_summarized`、`media_policy_reason`；强制恢复一律 A（`policy_forced`/`forced_recovery`）。错误码登记分类表与 Gateway 文案。
- **首版缺陷即改**：探针原放在策略解析处，两个 Gateway 压缩回归（`test_gateway_compact_deferred_source`、`test_gateway_conversation_compact`）的假后端多出 2 次调用——纯文字压缩也在探针。改为只有范围内确有媒体块才解析视觉事实后恢复 1 次调用。另修 `compact_generation=0` 被 `or -1` 吞掉导致失败标记失效的边界。
- **回归**：新增 `test_compact_media_vision.py` 13 项（探针阳性缓存/typed 拒绝缓存/瞬时错误不缓存/答错与不调用两参数化/原生工具前置、骨架不探针、声明优先、强制与失败代次回落、准入三门、字段校验持久化到运行时配置、B 路径请求保留图块并写 checkpoint、typed 失败后同代次回落且不进熔断、预算门回落记原因）；片 A 两处断言按新事实更新（强制恢复 fact_source=`policy_forced`、`MediaArchiveFacts` 增 bytes/videos）。压缩/线程/模型档案/错误码相关 70 个测试文件联合 1937 passed。ruff、doc sync、strict code-size 见提交前 gate。
- **保留偏差**：preflight、`_automatic_noop` 与请求投影器的候选接受估算仍未加图块预留（只在 B 准入与摘要请求发送前落地），见设计文档"片 B 实现记录与偏差"。
- **真实验收第一轮（本机，runtime-step10p，2026-09-24 12:58—13:18）**：M3 与 M2.7 各三条会话（先贴图问图，再让上下文越过压缩点）。发现两件事：(1) 单回合工具循环把 69 万字节报告一次读完，上下文冲到 129%，恢复压缩报 `COMPACT_CANDIDATE_TOO_LARGE`——单个活动回合超窗的既有边界，与媒体无关，任务改为多回合；(2) 多回合两轮共四条会话都成功压缩且模型压缩后仍能凭摘要答出图与章节事实（M3 全对，M2.7 答对章节、如实说看不到图），但 checkpoint 全是 `forced=true, media_fact_source=policy_forced, media_policy_reason=forced_recovery`：Gateway 恢复宿主对所有压缩都传 `force=True`（整段压完语义），首版把它当成供应商施压，B 在 Gateway 里不可达。已修：新增 `pressure_forced` 只在窗口上限/供应商施压时为真，媒体策略只按它关闭 B；`test_media_compact_preflight` 的假后端声明纯文字模态避免探针多出请求，新增 `test_pressure_forced_recovery_always_archives_media`。修后真实验收见下一条。证据在仓库外 `releases/step10-combo4/media-b-evidence.json`。
- **真实验收第二轮（本机，runtime-step10r，main `8c6d29c5f`，2026-09-24 13:38—13:42）**：(1) 阈值自动压缩（贴图 + 粘贴报告到 92k 以上）：M3 与 M2.7 两条会话的 checkpoint 都是 `media_fact_source=probe_supported`——结构化视觉探针在两个 MiniMax 模型上都拿到了正确颜色的 tool_use（M2.7 也支持看图，不再靠猜）；但 `media_policy=archived_refs, media_policy_reason=summary_budget_exceeded`：范围 92,605 token 加图块预留超过摘要预算（0.8×窗口 − 输出预留），按请求前准入落 A，reason 如实进 checkpoint。(2) 手动 `/compact`（M3，贴图 + 第 1–15 章后立即压缩，范围 74,765 token）：checkpoint `media_policy=vision_summary, media_fact_source=probe_supported, media_blocks_summarized=1, media_blocks_archived=0, forced=true`，历史 74,765 → 10,585 token；压缩后不用工具追问，模型答出图里华东目标 19,800,000 与第 5 章 RPT-005-8900 / 310,868（与真值一致）。结论：探针事实、B 采用、预算门回落三条路径都有真实样本；`forced=true`（整段压完）与 B 并存，符合修后语义。

## 后台续跑扩展目录与停用结清（2026-09-24，本地分支 `claude/process-stale-unknown`）

- **断链复现（本机，MiniMax-M2.7，combo4 72 个月 10 步任务）**：run3 `agentrun-1790272709-…` 主运行 attempt 1 在 10:58:29—11:03:02 用了 `plugin__image_text…read` 等 21 次工具后派工结束；attempt 2（子代理生命周期唤醒，11:04:15）留下 3 条 `protocol_violation`：`allowed_tools=available_tools=17` 个核心工具、0 个 `plugin__*`，模型连续调用前台刚用过的 `plugin__genui_lite…export` 被判 `TOOL_UNAVAILABLE`，第三条 `will_break=true` 整轮中断、任务未完成。近三天另有 3 个主运行的续跑 attempt 留下同样的 17 工具事件（09-21 00:17、09-23 20:43、09-24 03:36）。全部依据 runtime.db 结构化字段，未读会话正文。
- **根因（框架，不是模型）**：`background_tool_policy.DEFAULT_BACKGROUND_ALLOWED_TOOLS` 就是这 17 个名字，后台唤醒续跑的 allowed_tools 只能来自这张封闭名单，插件/MCP 代理工具结构上永远进不去；违反"开放世界禁止封闭枚举"。此前 TESTS 首节把 combo2 同类现象定性为模型行为，已在原条目改判。
- **修复**：决策增加 `extension_tools`（`background-tool-policy.v2`）：默认 profile 为 `inherit`，`runtime._background_run_allowed_tools` 经 `ToolRegistry.extension_tool_names()` 按注册对象类型（`MCPProxyTool` 代理，先做与新运行相同的 `prepare_for_run`）并入当前已启用插件/MCP 工具名；显式 `background_main_agent_allowed_tools` 或任务 `allowed_tools` 标 `none` 不并入；定时任务仍返回完整目录；注册表读取失败只记类型、退回核心目录。停用撤销、owner 禁用表、连接断开仍由注册表与快照 fail-closed。YAML 注释同步。
- **同批修复 1（测试机停用卡死）**：`stop_process_session` 重试时，记录已是 `unknown` 且带上一次 `termination.cleanup`、两级实例按 PID 出生标识均不存在，则结清为 `killed`；首次停止对已消失实例仍不凭空确认。根因是 09-22 遗留的 unknown 记录永远拿不到终止回执，`/plugins disable` 反复 `PLUGIN_CLEANUP_UNCONFIRMED`、`enable` 被 `activation_unsettled` 拒绝。
- **同批修复 2（免审批旧快照调用结果码摆动）**：75 文件联合回归中 `test_plugin_enable::test_actual_enable_and_registry_view_call_then_disable` 在未改动的 main `6b48b5270` 上也稳定失败：停用清理先把连接关掉，`MCPProxyTool._execute` 先取连接就报 `MCP_CONNECTION_CLOSED`（→可重试的 `TOOL_EXECUTION_FAILED`），只有清理慢时才轮到发送准入里的激活检查报 `TOOL_UNAVAILABLE`；上午 precheck gate 通过属于慢路径。现在 `PluginProxyTool._execute` 发送前先 `activation_ref.require()`，撤销固定 `TOOL_UNAVAILABLE` / `reported_error_code=PLUGIN_ACTIVATION_UNAVAILABLE` / `not_started`、不发送。 **补丁（2026-09-24，同伴全仓复跑发现）**：`test_plugin_workspace_context` 用普通 `MCPStdioClient` 组装插件代理，没有 `activation_ref`，三处复核抛 AttributeError；现统一为 `_activation_revoked()`——没有激活合同的代理不做激活复核、只走原 MCP 连接检查（产品里插件代理一律由 `PluginMCPClient` 持有合同，不是放宽撤销门）。
- **回归**：新增 `test_background_extension_tools.py`（5 项，含真实临时 wheel 的插件启停跟随）、`test_process_session_retry_settles_unknown.py`（2 项，真实已退出进程的出生标识）、`test_plugin_proxy_revoked_call.py`（2 项）。后台策略/插件注册相关 75 个测试文件联合 2303 passed、5 xfailed、1 failed（即上述修复 2 修前的既有失败）；停用清理相关 13 个文件 226 passed；修复 2 后 `test_plugin_proxy_revoked_call` + `test_plugin_enable` + `test_tool_call_precheck` + 两个新文件 30 passed。ruff、doc sync、strict code-size（hard=0）通过。
- **真实复跑（本机，M2.7）**：run3b（同一 combo4 任务，工作区 combo4b）单 attempt 11:14:38—11:28:40 共 14 分 02 秒完成，`verify_combo4.py` 全部数字与字段名匹配，0 条协议违规，主运行 66 次工具操作里插件工具 29 次（genui export 9、browser open 9、design create/edit 3、savepoint 3、workspace-peek 3、image-text 1），6 个子代理 5 成 1 败（失败者 `create_goal` 被拒后未产出，主代理补做）。它没有触发唤醒续跑，因此不构成本修复的真实验收；证据在仓库外 `releases/step10-combo4/rerun-evidence.json`。修复后的真实验收见下一条。
- **真实验收（本机，M2.7，runtime-step10o，main `4ec0e11f3`，2026-09-24 12:09）**：两段式任务（先用 workspace-peek tree、派一个子代理后让出；被子代理完成唤醒后用 genui-lite export 导出柱状图并再次 tree）。主运行 `agentrun-1790276946-…` attempt 1（12:09:06—12:09:48）tool_operations 为 `plugin__workspace_peek…tree` 1、`create_subagents` 1；attempt 2（12:10:28—12:10:49，唤醒续跑）为 `plugin__genui_lite…export` 1、`plugin__workspace_peek…tree` 1，全部 SUCCEEDED；0 条 `protocol_violation`；子代理产出的 summary.json 四个区域金额与真值全部一致，summary.html 2625 字节。修前同类续跑只有 17 个核心工具（run3），修后续跑 attempt 能直接调用插件工具。证据在仓库外 `releases/step10-combo4/combo5-evidence.json`。
- **真实复验（测试机，M2.7，runtime-step10o）**：此前卡在 revoked 的 workspace-peek 再执行 `/plugins disable` 返回"插件已停用并释放"，随后 `/plugins enable` 成功，installations.json 中 phase 由 `revoked`（revoke, rev 8）变为 `active`（activate, rev 11）；TESTS 首节待办已关闭。证据在仓库外 `releases/step10-combo4/testbox-plugin-cleanup-reverify.json`。

## 媒体压缩策略片 A：归档引用主链（2026-09-24，本地分支 `claude/compact-media-policy`）

- **改动**：新增 `backends/request_content.classify_nontext_content` / `compact_source_supported` / `is_local_media_block`、`conversation/compact_media_policy.py`、`tool_request_projection.compact_request_source_supported`；`compact._split_nontext_transcript_suffix` 按策略只保护 unknown 块，`conversation_compact_provider_source` 支持逐条只读投影，checkpoint 新增 `media_policy` / `media_fact_source` / `media_blocks_archived` / `media_refs`；preflight 与恢复宿主两处门改用"能否摘要"判定；配置 `compact_media_policy`（默认 `auto`，片 A 等价于 `archived_refs`）。`_summarize_segments` 与 `text_request_capacity_known` 保持严格语义不变。已知媒体严格等于运输层会展开的集合（顶层 user 行的 local_file image/video），嵌套或 assistant 侧一律 unknown。
- **新测试** `test_compact_media_policy.py` 22 项：分类矩阵（嵌套 / assistant 侧媒体计 unknown）、策略解析与非法值 fail closed、投影只改顶层 user 的 local_file 块且不含路径、来源多遍重放一致、后缀保护按策略切换、归档引用压缩覆盖含图回合并写 checkpoint 事实、`off` 下旧行为且不写媒体字段、unknown 块在 `archived_refs` 下仍保护。
- **改写用例**：`test_media_compact_preflight.py` 九项矩阵与 Gateway 四档按 `off`/`auto` 参数化，图片夹具改为 canonical 的 local_file 引用；`auto` 下带图历史越过压缩点先归档引用再压缩、业务请求 0 个图片块、摘要请求含"附件引用"且不含 local_file，越窗时强制恢复同样走归档引用后发送。`test_compact_media_recovery.py`、`test_compact_retained_history.py` 显式钉住 `off`。
- **结果**：直接相关 10 个文件 230 项通过；相关 90 个测试文件 2059 passed、1 skipped、3 xfailed、1 xpassed；ruff 通过。已合入 main `6bb0467b1`（含 `3fce97453`），同一 wheel（21c45e31）双机 `runtime-step10j`。
- **真实 TUI 验收（本机，MiniMax-M3，2026-09-24）**：贴图只看图读出 7 个目标值 → 读文件三句总结 → 手动 `/compact` → 再问图。checkpoint v3 generation 1、forced、source_messages 4、retained_tail 0，`media_policy=archived_refs`、`media_fact_source=vision_fact_unavailable`、`media_blocks_archived=1`、`media_refs` 与原图 sha256 一致；摘要含"附件引用"、不含 local_file 或 owner 路径；投影 token 18,975→10,299。压缩后模型按摘要给出正确数值并提示可重新附图或用 OCR 复核原件，未声称仍能看见图片。证据在仓库外 `releases/step10-combo3/media-a-evidence.json`。
- **组合长任务补跑两轮（本机，同版运行时前一版 step10i）**：第一轮 M2.7 单 prompt 7 步 36 个 CSV 三子代理约 7 分钟、同线程切 M3 贴图核对目标 16 秒；第二轮全程 M3 单 prompt 13 步（含只看图读目标、image-text OCR 对照、5 张图表、快照恢复、浏览器读三页）约 7 分钟。两轮区域/逐月/逐年/目标读取/达标判断全部与独立真值一致，插件工具均按协议调用（第二轮 create_subagents 首次因 covers 重复被拒后重试成功）；第二轮 JSON 字段名用了英文键（要求中文），记为模型内容偏差。两轮都远低于 15—30 分钟：MiniMax 模型完成该规模任务只需约 7 分钟，未满足 Goal 的时长条件。

## Gateway 消息文件流式读取（2026-09-24，本地分支 `claude/decision-gateway-message-reads`）

- **新测试** `test_message_tail_streaming.py` 124 项，以不改的 `read_jsonl_tail_report`、`recent_report` 作参照逐项比对：
  - 24 个种子，轮换三种模式（混合故障、完全干净、大量 display 行）：尾部行、窗口投影、全量访问、近期产物、去重判定五类等价。覆盖多块大文件、翻倍重读、坏行、解析失败、CRLF、NEL/U+2028、无换行或被截断的末行、非法 UTF-8（全量路径两边都抛 UnicodeDecodeError）。
  - 3 种块边界：多字节字符、LF、CRLF 恰好跨越 64KiB 边界。
  - 原实现的一个边角：窗口内有空行时返回的行数少于 limit，新实现一致。
- **变异验证**：去掉跨块拼接、去掉迭代器条数上限、投影超出上限、窗口不按换行数关闭、保留窗口首个不完整行、有坏行仍访问，6 种各自使 76、37、29、18、19、8 项失败。
- **峰值与耗时**（仓外探针，同一 4.2M 字符夹具，同一基点 main `3d2424786`）：Gateway 准备期峰值 21.33→0.82MB，全程峰值 22.65→10.03MB；三宿主阶段探针中 child、后台不变；每次请求三次尾读约 40ms→约 6ms；建索引峰值 16.96→0.72MB，耗时基本不变。
- **回归**（基点 main `3d2424786`）：171 个相关测试文件 4078 passed、1 skipped、25 xfailed。
- **合入与部署**（主线 owner，2026-09-24）：独立复跑 27 个相关测试文件 1106 项通过、ruff 干净后快进合入 main `0bb09a76b`；同一 wheel（SHA256 前缀 0eab62e0）本机先切、测试机后切，双机 `runtime-step10i`，回滚 step10h 保留。

## 决策线两片合入与 step10h 双机部署（2026-09-24，main `d69f30cf3`，wheel c52da295）

- **合入**：`claude/step10-batch1` 从 `41c66872e` 快进到 `ec821c90c`（媒体 preflight：代码 `e90d2ec60`，其余为文档），再合并 `5cb75eaf8`（决策设置无锁读取），得 `d69f30cf3` 推送 main。TESTS.md 唯一冲突为两节都在顶部新增，按两节都保留、共用标题取"已合入 main"版本解决。
- **独立复跑**（各在 detached worktree 上，不用对方的工作树）：媒体链 18 个相关测试文件 395 项通过；决策链 31 个文件 779 项通过；合并树上取并集 47 个文件 1120 项通过。ruff（agent_py_agent/scripts/plugins）、doc sync、strict code-size（hard=0）、`git diff --check`、clean-package 均通过。线上 CI 停用，未作为验收来源。
- **审阅要点**：无锁读取只走 `operation == "read" and not blocking` 分支；`_owner_operation` 的 read 路径只做投影不写文件，`read_model_profiles` 在文件不存在时返回默认值不落盘；两份设置文件都是原子替换写，读者最多读到已提交版本，调用方仍在调用前后复核 revision。媒体 preflight 把自动 noop、轮内跳过、强制拒绝三处口径统一为"压缩链不可用时只守窗口硬上限"，`COMPACT_REQUEST_NON_TEXT` 单列并配专门文案。
- **部署**：同一 wheel（SHA256 前缀 c52da295）在本机与测试机各自复制上一运行时后强制重装、逐文件核对；测试机先切换（本机当时 processing=1、有活动 attempt，被空闲检查拒绝），本机随后空闲时切换，两机各一个 Gateway、默认入口同版，回滚运行时 step10g 保留。测试机新旧 Gateway 日志各有 4 条 `model_not_configured` 后台迭代记录，属既有现象，非本版回归。
- **发布脚本教训**：一条龙脚本里本机切换的失败被管道掩盖而继续切测试机，造成短时双机不同版；脚本已加 `pipefail`，并要求推送 main 成功后才允许部署（`&&` 串接，不用 `;`）。
- **"模型绕过插件工具"定性**（combo2 A 任务 `gwreq-1790249102-…`，M2.7，主运行两个 attempt 共 77 轮）：全部依据结构化事实，未读任何会话或记忆正文。归档 context bundle 的 `tool_manifest`（tool_runtime_manifest v2）显示该 run `allowed_tools=None`、visible=executable=51，其中 18 个 `plugin__*` 工具 category 为 `plugins`，不在默认折叠类别（collaboration/web/vision/meta/mcp）内，即原生 schema 直出；model_usage 两条记录的 `purpose_breakdown.decision` 均为 0（决策关闭，无 shortlist/deferred 可查）；主运行 tool_operations 为 create_subagents 1、write_file 5、run_command 48（33 成功/15 失败）、edit_file 1，插件工具 0 次；同日 05:11 的最小复现在同样可见性下正常调用了 `plugin__genui_lite…export`。结论：模型行为（长上下文下自选 run_command 直连插件环境，并给出与事实不符的"不在快照"说法），不是可见性或决策线缺陷，记为模型能力边界。`tool_search`/`list_tools` 不进 tool_operations，不能由其缺席推断未搜索。 **2026-09-24 改判**：上述结论只核对了 attempt 1（前台）的 context bundle。同日 combo4 run3 在 attempt 2（子代理生命周期唤醒后的后台续跑）留下 3 条 `protocol_violation` 事件，`allowed_tools=available_tools=17` 个核心工具、无 `plugin__*`，违规码 `TOOL_UNAVAILABLE`，第三条 `will_break=true` 整轮中断；近三天另有 3 个主运行的续跑 attempt 留下同样的 17 工具事件。代码上 `background_tool_policy.DEFAULT_BACKGROUND_ALLOWED_TOOLS` 就是这 17 个名字，插件工具结构上进不去。combo2 的 attempt 2 同为 98.6 秒间隔后的生命周期唤醒续跑，因此模型说"插件工具不在快照"在 attempt 2 是事实，改判为框架缺陷（后台白名单封闭枚举），不是模型能力边界；修复见下文"后台续跑扩展目录与停用结清"。

## 媒体会话越过压缩点：preflight 只守窗口、越窗结构化拒绝（2026-09-24，本地分支 `claude/decision-media-preflight`）

- **新测试** `test_media_compact_preflight.py` 13 项：
  - preflight 9 项矩阵：纯文字按压缩点；带图时压缩点与窗口之间放行、到窗口拦截，诊断串带 `compact_capacity=non_text`；`save=false` 下纯文字与带图都按窗口（主线 owner 要求钉住的原行为）。
  - Gateway 真实链四档（只替换末端 HTTP，图片经原导入入口，窗口 60k、压缩点 50%）：带图在压缩点以下发送 1 个图片块；越过压缩点、低于窗口时带图发送且不压缩（修复前整轮失败、业务 HTTP 0 次）；越过窗口时报 `COMPACT_REQUEST_NON_TEXT`，业务 HTTP 0 次、原始记录原序保留，客户端文案点明是图片等非文本内容所致；纯文字对照照常先压缩再发送。
- **改期望码**：`test_compact_media_recovery.py`（供应商报溢出）和 `test_compact_retained_history.py`（大历史）的媒体强制恢复，从 `COMPACT_REQUEST_PROJECTION_UNKNOWN` 改为 `COMPACT_REQUEST_NON_TEXT`。
- **变异验证**：去掉 preflight 的媒体门槛，3 项失败（2 项矩阵、Gateway 越压缩点档）；恢复旧码，7 项失败（越窗档、2 项供应商溢出、4 项大历史）；去掉 `COMPACT_REQUEST_NON_TEXT` 的专门文案，越窗档失败。改回后全部通过。
- **结果**：72 个相关测试文件 6460 passed、2 skipped、2 xfailed、5 xpassed。5 个 xpassed 在同一 main 上完全一样，与本片无关。

## 压缩点与恢复目标的自助修改范围与运行时一致（2026-09-24，本地分支 `claude/compact-percent-config-sync`）

- **问题**：运行时把压缩点夹在 50–100%、恢复目标夹在 25–80%（配置解析同样如此），但 `user_config` 自助修改对两者都接受 25–95。于是压缩点 25–49、恢复目标 81–95 会被"成功"保存，却在运行时变成 50、80。这是在第 12 项请求准备阶段压缩真实样本中发现的。
- **修复**：两个范围改为 `_memory_coercion` 里的共享常量，配置解析与 `user_config` 校验同用；运行时夹取不变。
- **新测试** `test_user_config_capability.py` 1 项：对 −5 到 130 的每个取值，自助修改接受、运行时夹取后不变、配置解析后不变，三者必须同真同假。
- **变异验证**：4 种各自使该测试失败——压缩点校验改回 25–95、恢复目标校验改回 25–95、共享常量任一改成与运行时不一致。改回后全部通过。

## 子代理自动选模提交阶段的结构化原因码（2026-09-24，本地分支 `claude/decision-child-commit-reasons`）

- **扩展** `test_subagent_first_request_selection.py` 的自动验证失败矩阵：新增"父线程级设置变化"档，得 `settings_changed`；原有各档改为逐一断言原因码——改目录、关闭决策都得 `model_catalog_changed`（owner 级设置写在目录里），显式同值选择得 `explicit_model_selection`，此前前两档只会记成 `selection_changed`。
- **新测试** 同文件 3 项：
  - 目录锁或父线程锁在提交时被占用，分别记 `model_catalog_busy`、`source_thread_busy`，业务照常用继承模型发出。替身只在探针前换上，准备阶段不受影响。
  - `_adoption_conflict` 表：期限最先判定 `commit_deadline`；建议变化 `advice_changed`；首请求资格被消费（标记缺失、非 preparing、attempt 或 operation 不符）`first_request_consumed`；选择版本变化 `selection_revision_changed`；全部一致返回空串。
- **变异验证**：7 种各自使测试失败——收拢回 `selection_changed`（5 项失败）、两把锁的原因码互换（各 1 项）、`settings_changed` 记成目录变化、期限改到最后判定、资格被消费记成建议变化、忽略选择版本变化。改回后全部通过。
- **结果**：见本分支交接。

## 决策连接连续失败的冷却退避（2026-09-24，已合入 main `3933b1db1`）

- **新测试** `test_decision_cooldown_backoff.py` 6 项：
  - 冷却表：每次冷却过期后再失败，冷却依次为 30、60、120、240、300、300 秒；冷却期内才返回的失败不改截止时刻、不加级；成功和显式重试都从 30 秒重新计起；额度失败固定 300 秒并延长正在进行的冷却，但不加级；配置错误不随时间解除，修订变化后的失败从 30 秒计起。
  - 经 `decide()`：持续失败的连接依次冷却 30、60、120 秒，冷却期内零尝试；一次成功后再失败回到 30 秒。
- **扩展** `test_decision_fault_matrix.py`：故障仍在的两档（连接被拒、DNS）在冷却到期重试再失败后，经真实传输栈断言下一次冷却为 60 秒。
- **变异验证**：7 种各自使新测试失败——不翻倍、冷却期内的并发失败也加级、成功不复位、冷却过期即忘记次数、不封顶、额度不延长进行中的冷却、显式重试保留次数。改回后全部通过。
- **结果**：见本分支交接。

## 决策服务故障矩阵与同 owner 并发（2026-09-24，本地分支 `claude/decision-fault-matrix`）

- **新测试** `test_decision_fault_matrix.py` 10 项，全部经真实传输栈（本机 HTTP，不访问外网）：
  - 8 种故障各自首次结果、冷却期内零新尝试、到期（或设置修订变化）后恰好一次新尝试：连接被拒、DNS（只拦截一个保留域名的解析）、明文端口上发 https 的 TLS 失败、429 额度、429 限流、402、503、慢响应。
  - 同一 owner 12 个不同会话并发决策各自成功（12 次 HTTP）；同一会话同时两次，第二次 `admission_busy`。
  - 模型目录写锁被占用时，决策按已提交设置照常完成，不再是 `settings_busy`。
- **改写用例**：`test_decision_service_http.py` 与 `test_decision_service.py` 各一条原先钉住"写锁占用即 settings_busy"的用例，改为"不等待、照常完成"；后者补替换调用边界，避免真的解析保留域名。夹具增加可选错误体。
- **变异验证**：去掉无锁读取，3 项失败（并发、目录锁、线程锁）；额度冷却改成 30 秒，2 项失败（429 两档）；配置类故障改成定时冷却，2 项失败（402、TLS）。改回后全部通过；新文件连跑 5 遍稳定。
- **结果**：决策相关 39 个测试文件 908 passed。

## 后台上下文预算只估算渲染节（2026-09-24，已合入 main `5dcdfd463`）

- **新测试** `test_background_context_budget.py` 5 项：
  - 有种子时预算不计入不渲染的最近消息（shape_pass 为 0，12 条观察全留）；无种子时照常计入并收缩。
  - 渲染只在无种子时出现 Recent Messages。
  - 合同：预算估算的节集合等于实际渲染的节集合，覆盖普通有种子、普通无种子、审计窄事件三种情况。
- **改写用例**：`test_background_scoped_compact.py` 的独立任务用例。原先把就绪路径保留的 bundle 当作"运行视图"核对消息范围；现在断言 bundle 不带消息正文，并改在真实无种子路径的渲染结果上核对任务范围。
- **新增界限**：`test_host_summary_phase_lifetime.py` 的后台宿主，摘要入口驻留低于历史正文的 3/8（修复前约 2.48MB，修复后约 1.15MB）。
- **变异验证**：预算忽略 `rendered_keys`，4 项失败；渲染方不传，3 项失败；就绪路径保留消息，3 项失败。改回后全部通过。
- **结果**：后台相关 29 个测试文件，840 passed。

## 决策线能力推荐按插件分组出题（2026-09-24，本地分支 `claude/decision-plugin-grouping`）

- **新测试** `test_decision_capability_provider_grouping.py` 2 项：
  - 分组单测：只按结构化 `provider_id` 合并，题的位置取第一个成员；描述写着"插件 alpha"但没有 `provider_id` 的内置工具仍单独成题；任一成员版本变化时，合并行的版本随之变化；插件题带整体判断说明。
  - 消费者用例：在真实 Registry 与 Skill 快照上注册两个插件（alpha 2 个工具，beta 1 个）。3 个插件工具只出 2 题；alpha 选中时两个工具都进入短名单，beta 未选中时整体不在短名单，并进入延迟名单。
- **变异验证**：
  - `_material` 不分组：消费者用例失败。
  - `_project` 不展开：消费者用例失败。
  - 按描述文字归组：单测失败。
  - 改回后通过。
- **回归**：原能力推荐 4 个测试文件共 93 项通过，行为不变（这些夹具里没有插件工具）。
- **量化**：用仓库内置插件声明（8 个插件，21 个工具）按 `plugin_runtime` 同一格式生成候选。题数 21→8，题目序列化 30,691→19,520 字节。

## 第12.4项第二片 2b：摘要期释放旧请求历史（2026-09-24，已合入 main `911d0d14d`）

- **新测试**：
  - `test_host_summary_phase_lifetime.py` 3 项（`slow`，约 12 秒）：Gateway、child、后台三宿主各写入 128 行、约 4.2M 字符的历史，跑完整链。第一次进入摘要时，原参数的旧历史已解绑，驻留低于历史正文的 3/4；摘要逐条覆盖全部历史；首业务请求带当前任务。
  - `test_compact_recovery_release.py` 11 项：
    - Gateway 七种失败（代次冲突、取消、令牌取消、未知投影、摘要瞬断、摘要超时、候选超限）与后台两种失败（取消、候选超限）。解绑后换上"读取即报错"的替身，收尾全程不读旧历史，也不发业务请求。
    - 自动 noop 原样发送原请求，旧历史不解绑。
    - 候选与原参数共享 `tool_context` 的合同。
  - `test_conversation_history_seed.py` 新增 1 项：空的只读来源在原生边界只读一次，不走旧文本回退。
  - 审阅后补 2 项（`test_rows_native_drops_but_text_keeps_do_not_change_native_output`）：来源里只有 native 过滤、text 保留的行（后台空正文行、child 规则 display 行）时，跳过旧文本回退前后原生输出一致（均为空）。
- **变异验证**：
  - 去掉解绑：全链 3 项与 Gateway 7 项失败（后台夹具没有旧历史，不能区分）。
  - 在收尾路径加一次读取：9 项失败。
  - 把解绑挪到 noop 之前：noop 用例失败。
  - 只读来源改回旧文本回退：单测失败。
  - 改回后全部通过。
- **结果**：
  - 2a、retry 两片的相关文件加本片新测试，共 147 个文件：3,724 passed、1 skipped、24 xfailed、1 xpassed。
  - Ruff、doc sync、diff 检查通过。
  - strict code-size：hard=0；与 main `c18519d90` 逐项对照，本片没有新增发现。main 上 high-risk 已是 1468，仓库里的报告文件是旧的。
- **未覆盖**：没有跑真实模型；建循环时的首次物化峰值不在本片。

## 恢复候选提交后的同请求重试（2026-09-24，本地分支 `claude/decision-retry-committed`）

- **新测试**：
  - `test_gateway_compact_recovery.py::test_transient_retry_after_commit_resends_committed_candidate`（2 项）：Gateway 溢出 → 摘要 → CAS → 候选瞬断。
    - 重试成功：两次发送都是同一候选，两次之间没有重建，也没有共享预算回收。候选回的工具调用进入下一工具轮，下一轮按候选参数正常重建（带工具结果），不再命中。
    - 重试耗尽：每次都发送候选，最后原样抛 `ProviderTransientError`。
    - 两种结局都只有一次摘要、一行 v3 checkpoint，代次为 1。
  - `test_compact_native_ir_recovery.py` 三项，共用 `_committed_background_attempt`（后台宿主直接安装恢复宿主，首请求超预算，自动恢复提交候选）：
    - 瞬断重试：首个候选瞬断后重发同一候选，前后没有重建和共享预算回收，代次为 1。
    - 空响应修复：候选返回空响应后，重跑请求只比候选多一段修复提示；共享预算回收只落在候选参数上。
    - 插话取代：候选在途时到达插话，候选返回空响应。重跑请求带这条插话且只带一次，候选参数的 tool_context 里也只有一次；确认后邮箱清空；总共只有两次业务发送。
  - `test_tool_loop_model_turn.py` 两项：
    - 记录只对同一份原参数、同一 agent 命中；换参数对象即清除；未提交或宿主没有回调时不命中。
    - 命中时不调用过期自然回复丢弃、自然回复切换、build、插话注入、共享预算回收和 prepare，只做首请求选模、选模和发送。
- **变异验证**：
  - 取记录始终返回 None：Gateway 2 项、后台 1 项失败。Gateway 重试发回重建的旧请求；后台在原参数上做共享预算回收，抛 `compact summary base changed`。
  - 服务层不查记录：命中路径单测失败。
  - 换参数对象不清除记录：记录单测失败。
  - 去掉 `_model_turn_or_retry` 异常分支换成候选参数的几行：空响应修复用例在原参数上共享回收，抛 `compact summary base changed`；插话取代用例多出第三次发送（第二次是不带插话的旧候选）。
  - 改回后全部通过。
- **结果**：
  - 首个提交：引用改动模块或其入口的 57 个测试文件，1,612 passed、24 xfailed、1 xpassed。
  - 并入重跑分支修复后：再加上覆盖空响应修复、插话和模型轮采纳的测试，共 79 个文件，2,363 passed、1 skipped、24 xfailed、1 xpassed。
  - Ruff、doc sync、strict code-size（hard=0，高风险项与基线相同）、diff 检查通过。
- **未覆盖**：没有跑真实模型；Gateway 宿主没有会话任务邮箱，插话取代只在后台宿主上验证。设计见[依赖拆分同名节](docs/design/TOOL_LOOP_DEPENDENCY_SPLIT.md#恢复候选提交后的同请求重试决策分支2026-09-24本地)。

## 第12.4项第二片 2a：宿主历史种子只读来源（2026-09-23，本地分支）

- **新测试**：
  - `test_conversation_history_seed.py` 16 项：具体种子与只读来源在 `_native_provider_history_messages`、`_text_conversation_history_section` 两个边界逐项相等，并覆盖：
    - 下游 `project_native_provider_messages` 孤儿清扫的补位结果、PNG 媒体、匿名信封、终态工具折叠；
    - 当前请求行、Audit 投递、display 行的排除；
    - 磁盘地址视图与内存行，Gateway、后台（保留空正文行）、child 三种规则，三宿主真实入口逐项核对 messages；
    - 冻结时刻：终态折叠热尾窗口（300 秒）过后再解析，仍与准备时的具体种子相同，多次解析结果不变；
    - 冻结后合法追加不改变已选历史；
    - 互斥校验；源文件改写、截短、原子替换（内容相同也算）后抛 `DataCorruptionError`，删除后抛 `FileNotFoundError`，都不会变成空历史。
  - `test_host_history_seed_lifetime.py` 3 项：4.2M 字符种子准备驻留 32–54KB、峰值 270–294KB（修改前约 8.5MB/8.6–9.0MB），解析后完整 JSON hash 与行数不变。
- **变异验证**：解析时不传冻结时刻（改按解析时刻投影），时钟用例 3 项失败；后台改回过滤空正文，三宿主用例失败。改回后全部通过。
- **适配**：11 个相邻测试文件的种子/上下文断言改为经解析入口核对完整内容，没有删除内容断言。Gateway 的 5 个文件共用 `_gateway_history_helpers.py`，按生产单行规则从只读来源取历史。其中 deferred source 用例原先把被缩窄的展示投影当成 Gateway 历史来源；改动后历史与 Compact 来源是同一批完整行（24 行），并断言缩窄的展示投影从未被调用。
- **改写断言**：`test_gateway_conversation_compact.py` 两个用例原先断言 `_conversation_prompt_section` 默认分支（摘要在 Gateway 段内、位于操作证据之前），该分支生产不可达（所有调用方都传 `include_transcript=False`），已随评审删除。现改为断言生产路径：摘要只在会话种子的文本历史段，操作证据只在 Gateway 上下文段，两者互不混入。原排序断言只针对不可达分支；生产文本协议里 Gateway 段排在历史段之前，2a 没有改变。`test_gateway_child_compact_scope_application.py` 原先比较两个种子对象是否相等，现在两次准备各自冻结投影时刻，改为逐项比较摘要、代次和两个边界的解析结果。
- **结果**：
  - 相关 114 个测试文件 2,932 passed、24 xfailed、1 xpassed。
  - 接到 main `fae9d5855` 后，加上第 10 步改动过的测试文件共 118 个：2,976 passed、24 xfailed、1 xpassed。
  - 独立评审修复后，同一 118 个文件：2,985 passed、24 xfailed、1 xpassed（新增 9 项种子用例）。最后删掉 `freeze_history_source` 未使用的 `current_epoch` 参数（避免新增参数过多的 code-size 高风险项）后，13 个改动测试文件再跑 358 passed。
  - 变基到 main `1be5753ff` 后，上述 118 个文件加 main 新改动的 6 个测试文件共 124 个：3,087 passed、24 xfailed、1 xpassed；Ruff、doc sync、strict code-size（hard=0，高风险项与基线相同）、diff 检查、clean-package 全部通过。
  - 完整链前后对照见容量审计同名一节。
- **未覆盖**：没有跑真实模型或 TUI，线上 CI 未作为验收来源。2b 未开始。

## 18-A 吸收 main `66a598cf3` 合并回归（2026-09-23，本地）

- **基线**：合并前对两侧 git-archive 快照各跑一次 8 分片全量。决策 `46ac29601` 有 12 项失败，其中 10 项稳定；另 2 项分别依赖 git 仓库环境、对时序敏感。main `66a598cf3` 有 16 项失败，其中 15 项是旧夹具问题，已由主线 `d54fc0c98` 修复。
- **合并后全量**：20,623 passed，29 failed。24 项在任一基线中已经失败，逐项对照无新增原因。合并新引入 5 项，均已修复：
  - main 新增的 `test_nontext_segment_source_does_not_commit_mechanical_summary`（2 项）：本线摘要改走 `conversation_compact_provider_source` 流式来源，替身换到这个接缝，断言不变。
  - `test_shared_native_window_commits_main_or_child_conversation_compact`（2 项）：改读 v3 的 `source_tool_refs`，并补非空和四元字段断言。
  - `test_applied_compact_context`：全量启动后该用例才改名，属旧名残留，复跑通过。
- **定向结果**（重叠不累加）：
  - `test_compact_tool_call_refs` 28 项：原三元场景逐条迁到四元；用主线 `66a598cf3` writer 实际写出的 v2 行，验证按 legacy 读取、不隐藏、不丢行；篡改的 v3 行拒绝读取。
  - 机械改写的 5 个文件：120 passed、20 xfailed。
  - `test_applied_compact_context`：13 项。
  - 修复后的 nontext 与原生 IR 两个文件：全部通过。
- **Gate**：全目录 Ruff、doc sync、strict code-size（hard=0、blocked=False，基线不改）、diff 与 clean-package 全部通过。
- **未覆盖**：未运行真实模型或 TUI，线上 CI 未作为验收来源。原场景到新测试的对照清单随交接提交给主线 owner。
- **随后吸收 `0d02bb272`**：主线 15 项夹具修复已随之进入。决策线原有的架构守卫失败源于 `concatenate_message_rows(*parts)` 的可变位置参数，已改为显式元组；守卫及 7 个 Compact 分区/来源测试文件共 141 项通过。
- **主线 owner 要求的新测试**（每条都做了变异验证：把对应实现改坏后测试失败，改回后通过）：
  - `test_transient_retry_reselects_and_accepts_only_final_attempt_params`：首次尝试瞬断后重新选模，计量、恢复、确认只收到第二次尝试的参数，覆盖正常响应和供应商超限两种结果。变异：让选模参数不交回，2 项失败。
  - `test_owned_recovery_fits_over_budget_native_history_before_business_request`：构造 4 对真实 read_file 原生往返，冻结的原始完整请求超过共享预算。在自动和强制两种恢复模式下，都断言发送前共享预算回收没有介入、宿主已提交候选、候选的完整计量低于输入上界、业务线上没有原文。变异：去掉领取门，2 项失败；自动宿主不压缩，自动模式失败。
  - `test_post_commit_context_refresh_failure_keeps_committed_history`：CAS 之后同范围视图刷新失败时，异常原样上抛，不回滚已提交的 IR 与上下文，不记压缩失败，也不发布结果。变异：把刷新挪回回滚 try 内，2 项失败。
  - b4ffb3475 的三个用例（候选回收两项、提交后投影失败不回滚一项）正文和断言与 main 逐字一致，均通过。它们共用的 `_SummaryBackend` 只多接收两个关键字参数（`tool_choice`、`request_options`，本线摘要请求会传入），返回值不变。
- **决策线接手前已有的 9 项失败已清零**（在 `46ac29601` 快照上就已失败，main 上没有；不处理的话合入后会变成 main 的新失败）：
  - 产品缺陷（修复前 3 项失败，修复后通过）：首次恢复宿主在请求没有会话来源时仍读 `compact_source.thread`，导致未绑定 thread 的 ask 全部 AttributeError。受影响的是孤儿响应投影用例和两条 Gateway 场景（多 worker、延迟响应）。现与 overflow 入口共用同一判定：没有来源就不安装宿主。
  - 合同缺口（1 项）：`INPUT_MEDIA_INVALID` 已登记到唯一错误合同，分类为确定的用户输入失败：不可重试，请用户重新添加附件。
  - 过时测试替身（5 项）：本线改动后测试没有同步，断言意图不变。
    - Gateway 改为先加载来源再准备，替身补上来源桩；
    - 执行选项显式带出 `input_media: []`；
    - 接续插话保留 `input_ids`；
    - 摘要来源改走流式 `message_source`，替身物化同一来源后再检查覆盖。
  - 验证：上述 6 个文件及相邻 Gateway/媒体/恢复共 12 个文件，350 passed、1 skipped、3 xfailed。
- **来源身份不可证明的结构化原因**（review 发现 1，原场景：部署前写入的工具索引行缺 attempt_id；当前轮只剩这类记录时，原因在调用方丢失）：合并后三宿主共用的强制恢复把这种情况报成 `COMPACT_SOURCE_EMPTY`（"没有可压缩的源历史"），现改报 `COMPACT_TOOL_COVERAGE_UNKNOWN`，原记录保持可见，不发业务请求，也不提交。新用例 `test_forced_recovery_with_identityless_carried_records_reports_coverage_unknown` 在修复前失败（得到 EMPTY），修复后通过；所有涉及这两个码的恢复测试共 12 个文件，179 passed。
- **最终全量**（`11a3412b5`，8 分片）：20,592 passed，65 failed。65 项都是本地 HTTP、流式或期限类超时，当时同机负载约 5—6；单进程重跑这 65 项全部通过。接手前决策基线 10 项、main 基线 15 项的失败均已清零。Ruff、doc sync、strict code-size（hard=0、blocked=False）、diff 与 clean-package 通过。未运行真实模型或 TUI，线上 CI 未作为验收来源。

## 第12.4项选中来源到摘要生命周期（2026-09-23，本地）

解决选中canonical正文与完整摘要provider数组同时常驻的问题，复用原扫描、JSON、token估算、native/orphan规则及checkpoint/CAS。真实临时文件到load→摘要分段→提交的红绿测试：4,195,620字符旧峰值9,683,703 bytes，新峰值1,448,802；8,391,460字符新峰值1,532,904。完整JSON hash、95/189段连续覆盖和精确消息ID通过。该数来自专项独立进程，联合运行受分配器影响约1.19/1.53MB；只证明本Python路径，不等同RSS或三宿主峰值。

最终26文件 **530 passed（31.38秒）**，日志 `/tmp/decision_source_focused_final_20260923.log`；包含选择器/扫描、source生命周期、JSON估算、媒体、两候选、原后台162项及native IR29项。失败/取消/改写/CAS不推进覆盖、append下轮可见；原媒体fake补可选source参数，业务断言不改。独立只读末审无新确定缺陷。全目录Ruff、doc sync、strict code-size（hard=0、基线不改）、diff及登记新文件后的clean-package通过。

无scope旧入口、三宿主旧seed/frozen/回调仍待后续处理，精确ID和landmark仍随行数增长；没有部署、真实模型调用或全仓通过声明。12.4不勾选，线上CI未作为验收来源。

12.7固定5e5122b04安装候选：19个变更测试文件613 passed、1既有xpassed，Ruff/doc sync/strict size/diff/clean源码与wheel通过；不重新裁定旧全仓失败，线上CI未作证据。同一原生TUI七轮业务全部完成，覆盖M2.7 Compact前后、on4保留、官方M3及OpenCode DeepSeek显式切换的真实工具/缓存。原ledger逐字段来源区分未知和零，摘要独立计量；显式选模不算自动采用。详见[真实验收](docs/tasks/DECISION_MODEL_REAL_VALIDATION.md)。

此前4项后台Compact测试接口缺口已按owner授权收口：先复现4 failed/158 passed，再补fake Store的include_messages及临时canonical消息域，整文件162 passed。原业务断言及生产路径不改；旧全仓八项历史问题另列，第12.4及11/18仍未完成。见[验收记录](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

12.4原生历史投影保留一次canonical隔离复制，已隔离副本直接用于模型/摘要，匿名重复输出仍独立；嵌套容器测试峰值约4.89MB降至3.03MB。复用主线b4ffb3475的小JSON有界直接编码修复，估算口径不变；三文件72项通过。来源正文/覆盖ID仍驻留，12.4及11/18不变。详见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

12.4检查点读取已本地改为逐行校验：不再同时保留全账本及全部旧摘要，原writer/CAS和覆盖规则不变。9文件98项通过；约6.5MB账本的测试峰值由26.35MB降至0.69/1.23MB（orphan/已提交链）。选中消息正文及覆盖ID仍驻留，12.4和11/18不提前完成。见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

12.6本地组合已完成：250K/1M近窗口完整输入、同Agent双会话采用/保留隔离、未知协议/模态及连接变更校准失效；六文件123项通过。消息扫描Unicode空白与数值溢出兼容修复五文件145项通过。12.4全链来源及12.7真实缓存仍开放，11/18不变。见[本地验收记录](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

12.5本地组合已完成：三宿主完整候选保留当前要求、工具schema及输出cap，容量不足不提交；Responses未知cap单列。十文件187项通过，增强断言后新14项复验通过（重叠不累加）。当时12.4/12.6/12.7仍开放；12.6最新状态见首段，18项清单11/18不变。见[容量验收](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

主线最小移植闭包已核对：扫描、等值估算、摘要窗口三组在固定主线临时副本通过97项及相邻139项；无Jev配置依赖，尚未合入或发布。scoped来源/覆盖必须另按合同闭包接入，11/18及12.4不变。见[移植交接](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

## 第12.4项顺序摘要字符来源（2026-09-23，本地）

复用原摘要循环，移除整份JSON副本及二分半份来源复制，共享tokens改等值流式累计。定向初始43项、新源/估算62项、9文件176项、发送/响应取消补齐后15文件315项通过（24.59秒），这些计数重叠不累加；异常优先级审查补充后最终15文件 **316 passed（24.86秒）**，日志 `/tmp/decision_stream_verified_20260923.log`；它替代同范围315项中间结果。新1200条消息测试逐段hash全覆盖，tracemalloc峰值低于编码字符数一半；不是RSS或整个Compact绝对内存界。

三类摘要无效回复的纠正也必须保持同一预算，EOF取消、估算/响应后取消、UTF8错误和来源变动拒绝返回覆盖。Astra max独立只读复核的异常优先级问题已修：先前surrogate不能遮蔽后续JSON失败应有的str回退。实际模型调用仍为内存替身；未启动Gateway、未部署、未访问供应商，线上CI未作为证据。原4项主线fake Store签名适配失败仍开放，不以本轮通过宣称整体严格gate通过。

Ruff、doc sync、import boundaries零发现、strict code-size hard=0且原基线未改及diff均通过；clean-package在纳入新文件后通过。

建议下一步：继续原来源与覆盖链的正文常驻问题；只读审查可并行，writer/CAS保持单owner。

## 第12.4项保留历史完整投影（2026-09-23，本地）

六文件73 passed（15.88秒），日志 `/tmp/decision_retained_joint_20260923.log`：compact_retained_history、gateway_compact_recovery、subagent_compact_recovery、background_compact_recovery、gateway_child_compact_scope_application、compact_transcript_media_partition，均为test_前缀。新增16项中三宿主seed及同次真实冻结候选完整保留最早媒体/工具回合；两个原生协议small最终post_json载荷完整，large60万字符先完整捕获，后容量压力与未知模态明确失败、零业务HTTP和零CAS。HTTP为内存替身，未真实联网，不能当作供应商媒体容量验收。

初版测试4 passed/6 failed为超容量样本误期望发送，产品容量门未更改；修订后的可发送/应拒绝分组先16通过，再纳入73项联合。原4项主线独占fake Store签名失败仍待集成，不以本片测试覆盖它们，不称整体严格gate通过。10文件相邻回归416 passed（62.41秒），日志 `/tmp/decision_retained_adjacent_20260923.log`；包含新16项复验，不与前73累加。Ruff、doc sync、import boundaries零发现、strict code-size hard=0且基线未改、diff通过；clean-package在新测试纳入版本管理后通过。仅本地候选，未推送部署；线上CI未作为验收来源。

建议下一步：沿原摘要分段接有界来源，再做授权测试机真实组合验收；只读审查可并行，writer与共享Gateway保持单owner。

## 第12.4项固定来源范围筛选（2026-09-23，本地）

12.4固定来源范围筛选已本地实现：完整尾界内两遍校验后只保留范围内未覆盖正文；后台先读任务事实，recent_limit=0也不提前全载正文。12文件326项通过，另4项主线独占测试的旧Store签名尚待集成适配，不能称整体gate通过；ID索引、未压正文和覆盖链仍非完全有界，11/18不变。

12文件清单：conversation_message_selection、conversation_message_scan、background_scoped_compact、background_context_runtime_errors、background_prepared_context、background_compact_recovery、runtime_module_boundaries、conversation_store、compact_scoped_transcript、gateway_child_compact_scope_application、gateway_compact_deferred_source、gateway_conversation_control（均为test_前缀）。命令使用pytest -o addopts= -q --tb=short，30.05秒；失败node及原日志见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。没有真实调用、部署或推送；线上CI未作为验收来源。
本片非pytest守卫现已通过：Ruff、doc sync、import boundaries零发现、strict code-size hard=0（未改基线）、diff和clean-package。首次Ruff发现新增测试的两处导入格式，doc sync发现Gateway注释/模块文档遗漏，clean-package发现两份新文件未纳入版本管理；均已修正。主线独占4项测试接口失败仍开放，因此整体本地严格gate未通过，不推送；线上CI未作为验收来源。


建议下一步：主线适配独占夹具后再联验，本线继续保留历史完整投影；只读审查可并行。

## 第12.4项消息扫描底座（2026-09-23，本地）

六个文件定向联合153 passed（3.96秒），日志 `/tmp/decision_message_scan_final_20260923.log`：message_scan/message_stream/history_paging/conversation_store/cli_run_conversation/background_owner_delivery_commit。真实临时文件验证固定EOF、页字节预算与Unicode、半行等待和幂等写前拒绝、坏行游标回滚、display过滤、截断与并发同key；2000行历史禁止全量recent_report，tracemalloc峰值低于文件体积三分之一。没有真实模型调用或Gateway重启，非Compact全链/绝对内存上限证明。

只读复核修正了合法JSON无LF仍可拼坏后续追加、非对象JSON错误分类和半行超预算判定；完整LF算法复用原history_page并移至store_io，未新建第二实现。首轮五文件130通过，扩展命令一次文件名错误导致零测试，修正后的最终联合才作验收。本片Ruff、doc sync、import boundaries零发现、strict code-size hard=0且基线未改、diff及clean-package均通过；本地严格gate已通过，未推送部署，线上CI未作为验收来源。建议下一步沿原scope/writer/CAS接入有界来源，独立只读审查可并行，12.4整项仍开放。

## 集成安装版真实 TUI（2026-09-23）

319004926独立wheel/venv/HOME已在获授权测试机验三轮普通中文：关闭零Jev请求；2秒期限失败自动保留M2.7；4秒选模need_data及能力建议成功，均完成一次读取工具轮。新增3次Jev HTTP，累计57次；原供应商缓存读回有部分缺字段，总输入无节省结论。原终态观察字段完整，初次汇总遗漏已纠正，无生产修复。设置原CAS恢复关闭，TUI退出、队列清空、全部HTTP有终态后正常停止候选Gateway，8420无监听。完整请求ID、usage口径及局限见[真实验收](docs/tasks/DECISION_MODEL_REAL_VALIDATION.md#集成安装版原生-tui-对照2026-09-23)。

本轮只证明集成文字链与失败保留，不抵充媒体、超大历史、Compact后缓存或跨模型主会话验收。建议下一步继续12.4有界来源读取；只读审核可并行，测试机与恢复writer单一owner。

## 短期限回归阶段分离（2026-09-23，本地）

旧全仓8项失败按原node ID复核：未修改前定向8 passed。原healthy-long失败堆栈明确在loopback socket.connect阶段超时；其余首行0或first_event结果不能证明发生了stream_idle错误。原loopback provider明确禁用代理，web_fetch走校验IP直连；web_fetch的5秒超时尚缺阶段证据，不归因为环境代理或直接宣称已修。两项决策探测原1秒期限发生worker尚未退出，不能据此推断协议结果。

四个测试文件现在区分连接/首事件与短idle：非首事件用例显式首事件10秒，原短idle、有效data数量及typed stage保持；时间断言从第一条data计算，失败附阶段和耗时。成功/invalid-answer探测显式5秒，专用timeout/cancel及非法参数仍用原短预算。没有修改生产默认期限、重试或失败保留逻辑。四文件85 passed、1既有xpassed；与原web_fetch文件联合 **91 passed、1既有xpassed（14.99秒）**，日志 `/tmp/decision_timeout_stages_20260923.log`。这消除了成功/idle测试对极短连接调度的依赖，不抵充全仓通过，web_fetch历史根因继续开放。 本片Ruff、doc sync、strict code-size（hard=0）、diff及clean-package通过；本地严格gate已通过，未推送远端，线上CI没有作为验收来源。

建议下一步：以本片分阶段断言检查下一次自然回归；web_fetch若再现，先采集连接/handler/首字节证据，不再循环重跑到绿。独立只读审查可并行，生产HTTP期限保持原owner负责。

## 第 12.4 项媒体集成与未知模态边界（本地切片已验）

解决媒体引用被当作完整文字计量、或未读附件却取得摘要覆盖的问题。以 `81bdf9579` 为基线整合媒体线 `3adb61904`；媒体原件仍在原owner内容寻址目录，UserTurn保留input_ids及media，三宿主原生carry不另建存储。原生发送和出站投影复用同一后端组包。

共享 `backends/request_content.py` 在适配器过滤前检查原IR和原始消息。当前模型可继续携带原文字思考；跨模型候选不搬运供应商推理签名。未知模态不额外探测候选，保留原模型；普通原模型媒体请求继续发送。自动Compact跳过未知计量，供应商已报overflow的强制恢复返回COMPACT_REQUEST_PROJECTION_UNKNOWN，不摘要、不提交checkpoint。字节预算只约束文件展开，不是视觉token或窗口容量证明。

transcript Compact只摘要首个非文本原生信封所在完整回合之前的安全前缀，媒体回合及其后所有行原序保留，游标不越过未读来源。分段器在JSON化前拒绝非文本块，不能通过引用字符串取得覆盖；工具原账和旧检查点事实不重写。无完整文字前缀时typed拒绝，不伪造空摘要。

媒体线此前官方M3图片/视频/重连证据属于原提交，不能当作本次集成版真实验收。本次pytest采用隔离HOME、真实本地媒体读取和业务工具，仅末端HTTP替身；尚未部署测试机或重启共享Gateway。12.4、真实缓存、超大历史及旧全仓八项失败仍开放，11/18清单数不变。

建议下一步：联合定向与严格gate后保留可审查本地提交，再协调决策线测试机验收窗口。测试和只读审查可并行，共享Gateway及恢复writer保持单一owner。

本片55文件联合 **1419 passed、4项既有xfail**，退出码0；清单 `/tmp/compact_media_joint_20260923.files`，日志同名 `.log`。pytest配置和命令各带一次-q，原日志只有逐项结果和进度；统计为1419个通过标记及4个预期失败标记，不补猜运行秒数。初次媒体兼容检查144 passed、2 failed来自UserTurn新增字段的旧位置参数，已改显式media关键字并纳入上述联验；容量/选模91项、媒体工具轮4项、transcript26项均被最终联合覆盖，不累加计数。首次guard的导入排序和深层嵌套已修，新增文件暂未登记的打包提示在纳入本片后消除。

Ruff、doc sync、import boundaries零发现、strict code-size hard=0且基线未改、diff和clean-package通过。本地严格gate已通过；没有push、部署、真实供应商调用或Gateway重启，线上CI没有作为验收来源。未知媒体的强制恢复仍明确拒绝，不能宣称已实现完整多模态容量计量；旧全仓八项失败保持。

## 第 12.4 项外层溢出原生历史接续（本地切片已验）

Gateway、后台及child的同宿主逻辑回合现在携带真实原生IR，不从归档短预览重建正文。原循环在typed overflow后按准确attempt释放未提交插话，携带精确input_ids；释放失败仍经原partial出口保存已完成事实。恢复重新准备权限和provider前缀，旧调用四元引用保持，私有tool_round归并标记清除。摘要由prefix或IR唯一承载，强制恢复没有可压来源报告COMPACT_SOURCE_EMPTY。

首轮44文件联合 **1131 passed、12 failed、4 xfailed，129.37秒**，日志 `/tmp/compact_carry_joint_20260923.log`。6项Gateway旧fixture绕过observer触发eager提交导致旧view身份拒绝；1项child旧断言把历史第一张卡当当前卡；4项mixed来源忽略carry新增真实运行事实；1项真实空来源被误报投影变化。分别迁移真实安全点、核对最新卡并保留历史、保持工具来源/覆盖不变而保留控制事实、修正typed空来源判定。没有放宽来源身份或容量门。

新增同owner/request/run/task/逻辑turn/view校验、主新attempt与child固定attempt、深复制、同文不同input_ids、部分释放拒绝、完整工具IR-only零archive、释放错误保存和transcript-only摘要唯一性回归。无任务后台另验证同轮request_id固定、冻结thread视图无须补task属性，不误建持久任务。最终44文件联合 **1154 passed、4 xfailed，135.39秒**；清单 `/tmp/compact_carry_final_20260923.files`，日志同名 `.log`。四项均为既有预期失败，本片未新增xfail；taskless不新建会话任务的断言补充后两协议另跑 **2 passed，5.92秒**。Ruff、doc sync、导入边界0发现、strict code-size hard=0（基线未改）、diff和clean-package均通过；本地严格gate已通过，线上CI没有作为验收来源。

测试仅使用pytest隔离HOME、隔离文件和fake末端HTTP；没有真实供应商、部署或Gateway重启。决策线测试机已获用户授权用于后续隔离验收。媒体线合入、真实缓存、超大历史和旧全仓八项失败仍开放，12.4不勾选。

## 第 12.4 项原生工具 IR 来源（本地切片已验）

原生工具完整往返与archive按同一四元身份分区；摘要优先真实IR正文、保留IR原序回放。完整配对回放的归档不重复生成handoff，未完成组和未知身份继续保留；无旧handoff时插入尚未覆盖的归档。严格机械回退完整附旧摘要和本次原文，分段失败或供应商标明截断时typed拒绝，均沿原容量门和单CAS。

首轮38文件联合 **1027 passed、7 failed，125.15秒**，日志 `/tmp/compact_ir_joint_20260923.log`。四项旧宿主回调仍在公共IR投影前捕获候选导致wire断言失配，改捕获完整投影后的候选，保留实际HTTP逐字相等；两项旧断言预期归档纯文本后缀，现验证JSON原生消息完整还原；一项手工plan缺新增source_ir_history，补显式空元组。更早小组11项失败保留在 `/tmp/compact_ir_initial_20260923.log`：旧重复保留区拒绝、旧no-op签名、背景候选捕获点、初始+恢复各冻结一次的计数假设；未通过放宽容量或去掉实际wire核对消除失败。

新增5项真实工具安全点测试：原read_file执行器及recorder生成短归档预览与完整ToolResult，两协议同ref摘要仅一次；另Anthropic仅IR、零archive仍原单CAS后发送获选HTTP，取消及过大均零CAS/业务请求。HTTP末端使用fake供应商，工具实际读取隔离目录文件，不把此证据称为真实模型或外层自动接续。外层overflow重跑仍从archive重建，原生IR传递未完成；12.4、旧全仓八项失败、媒体、缓存和超大历史边界仍开放。

最终39文件联合 **1039 passed，136.66秒**，清单 `/tmp/compact_ir_final_20260923.files`，日志 `/tmp/compact_ir_final_20260923.log`。Ruff、doc sync、导入边界0发现、strict code-size hard=0（基线未改）、diff及clean-package通过；打包首检只因三个新文件尚未纳入索引失败，纳入本片后复查通过。尺寸首检发现公共投影函数过长，按IR位置变换职责拆出纯函数后通过，未放宽阈值。没有push、部署、重启、真实供应商或线上CI证据；本地严格gate通过不抵充旧全仓八项失败。

## 第 12.4 项首次自动准备与手动来源（本地切片已验）

Gateway/child/后台首次只加载来源，在原PromptBuilder冻结完整输入后、选模及拒绝回退基线之前自动Compact；恢复仍必须真实提交。容量充足和已知无可覆盖来源保留原输入，未知投影/取消不伪装成no-op。无来源仍交原选模和发送压力门裁决，不修改原触发线来强行放行。手动按车道内thread-scope来源判空和估算，回执不包含未知的下一业务请求。

首次联合 **995 passed、7 failed**（155.03秒）：后台新增测试的窗口准备不当、旧假宿主把首次也强行置为已提交、旧能力统计把首次/摘要重入当恢复。修正精确测试时点后，另发现真实产品预算边界：首次auto已提交后再恢复八次会超额；现首次真实提交也计入同一八次上限，首次no-op仍不计。原失败日志保留 `/tmp/compact_initial_joint_20260923.log`。

最终联合 **1005 passed，144.08秒**，文件清单 `/tmp/compact_initial_joint_20260923.files`，日志 `/tmp/compact_initial_final_20260923.log`。手动估算helper收口后同文件 **128 passed**；补充已知空源位于原trigger与实际input ceiling之间、自动采用较大候选且零Compact/CAS的回归后Gateway文件 **16 passed**。这些计数包含重复覆盖，不累加为不同用例总数。双协议child验证首次no-op后工具轮、一次原准备/候选wire相等，以及压缩后Jev采用延续第二工具轮。

Ruff、doc sync、strict code-size hard=0（基线未变）、diff及clean-package通过。没有推送、部署、重启或供应商收费请求；本片不关闭真实IR混合来源、媒体、真实缓存或旧全仓八项失败。第12.4仍未整项完成。

现场隔离事故单独记录：独立诊断直接调用依赖pytest autouse的fixture，绕过临时MY_AGENT_HOME，误写本机owner模型目录及测试线程。已在原锁内隔离确证的12组测试模型/provider；根据迁移源码确认新增字段来源，显式反迁移并用真实安装版reader验证原有42模型/7provider及selected保持。原件和操作清单只存本机私有证据，不入仓库、不算产品验收。后续独立脚本必须进程启动前指定临时MY_AGENT_HOME；真实测试只使用已声明隔离home，禁止依赖导入fixture获得隐式隔离。 后续精确隔离测试线程/两条消息/用量/过期claim及测试快照，共7文件；后续按相同请求/任务身份及SHA另隔离1份runtime_facts/task.json，共8文件。两索引只移除仍指测试线程的值，其他项不变。新真实请求已更新latest快照，未触碰。独立误建local_store仅有1条该测试消息记录，确认无打开句柄后整目录隔离，日常工作区记忆库未动；不能把这次事故表述为“没有记忆写入”。共享runtime.db保留审计，原全树终态API将孤立测试TaskRun从created收口为failed，3个已failed的attempt及长期Task身份保持。

## 第 12.4 项混合来源与三宿主活动归档（本地切片已验）

Gateway/child无transcript活动归档已接完整恢复准备、候选计量、原CAS和同次发送；混合transcript+carried同时读取所选消息和每条工具的原模型投影，一次候选封印双来源。完整归档不裁剪、未知身份保留。空摘要/工具调用的机械回退仍携全部工具材料；过大、取消和代次冲突不能发送恢复业务，普通故障保留原可接受候选时必须连同其全部材料返回。

混合HTTP九项通过：两协议×普通/独立任务逐字核对获选候选与实际HTTP，单检查点含消息和工具双覆盖，未知身份保留、其他任务隔离；过大、摘要失败、取消和CAS冲突不发送恢复业务。另一个16K窗口用例直接从真实恢复安全点进入：同一短摘要、同一冻结材料，纯transcript候选15,174 tokens，联合替换14,084，原接受门14,400；同时验证1,024输出预留下前者超窗、后者可发，代次只从0到1。数字是本地估算，不是供应商usage。

小窗口用例最初从完整外层进入，被首轮正常自动Compact抢先提交，随后模拟overflow导致无新来源；已定位为测试准备错误，保留完整外层已有用例，新用例单独验证原恢复安全点，不通过改产品阈值绕过问题。

取消定向三文件43项通过：摘要/投影直接ToolCancelled、第二候选取消、92%提交回调取消均不发布候选或累计熔断；普通界面错误继续沿原容错。活动92%已落盘候选可以孤立保留，但原提交链不可见。日志 `/tmp/compact_mixed_cancellation_20260923.log`。

首轮31文件联合725通过、4失败（`/tmp/compact_mixed_joint_20260923.log`）：三项child旧fake-run绕过真实renderer/select或断言旧报错；一项活动摘要夹具仍提供旧visible_records而未提供准确source_records。后者已改用实际工具模型投影并额外断言原材料进入摘要，13项通过；child的1/9代改为真实runner/provider/工具链和原CAS，逐代验证同一attempt与原归档，12项通过。不将首轮失败删除。

最终32文件联合 **738 passed，75.48秒**，日志 `/tmp/compact_mixed_final_20260923.log`，文件清单为同名 `.files`。Ruff、doc sync、导入边界0发现、strict code-size hard=0（基线未改）、diff和clean-package通过。本地严格gate只覆盖本片，不关闭历史全仓八项失败，也不宣称第12项全部完成。

本片无供应商请求、安装版TUI、部署、重启或线上CI；初次/手动、真正native工具IR组合、媒体及真实缓存仍待验。前轮全仓八项失败独立保留。测试机可用于后续实际验收，目前没有安装本片。

## 第 12.4 项后台完整恢复与三宿主同视图（本地）

后台transcript、普通空transcript活动归档和narrow审计恢复已接公共PreparedCompactRecovery；Gateway/child共用原canonical loader的scope/view准备。后台同片范围冻结、候选完整计量、CAS后获胜view回填及原参数继续发送按同一链验收。原自动Compact不在完整捕获前抢先推进代次；实际业务请求不包含提交时才产生的内部checkpoint ID。

新增后台HTTP替身16例覆盖两协议×普通/独立任务transcript、两协议×普通/窄审计活动归档，以及摘要瞬时错误、未知IR、过大候选、取消、CAS冲突不恢复发送。候选payload与实际HTTP入口字节对照，不用模型正文判定摘要调用。活动入口23例使用真实Store/checkpoint/CAS，覆盖完整容量/输出预留、停止时点、原projection材料身份、未知四元身份保留和局部非空evidence不继承全局值。纯IR投影保持媒体、插话、未转发guidance及去重；相关六文件最终联合 **91 passed**，日志 `/tmp/background_complete_final_20260923.log`。

最终25文件联合 **657 passed，67.69秒**，日志 `/tmp/compact_complete_recovery_final_20260923.log`，覆盖三宿主准备/恢复/能力展示、后台运行、原生IR、完整请求投影和作用域检查点。Ruff、doc sync、导入边界（0发现）、strict code-size（hard=0，基线未变）、diff和clean-package通过；首次打包检查仅因新增文件未纳入索引失败，明确纳入本片后复查通过。这里只证明本片定向与严格检查通过，不把旧全仓八项失败改写为通过。

旧fake agent.run测试绕开renderer/select，现按明确defer/宿主已提交语义验证控制流；不把fake声明提交算真实CAS证据，后者由上述及Gateway/child实际HTTP材料测试覆盖。初次14文件联合有2项child旧params对象identity断言失败，CAS回填正式view后参数确有新对象；已改为检验候选材料相等、获胜checkpoint、恢复及后续工具轮共用新params，原失败日志保留 `/tmp/compact_complete_recovery_joint_20260923.log`。

本片没有供应商真实请求、安装版TUI、部署、重启或线上CI证据。初次/手动、其它宿主活动归档完整计量和混合transcript+active联合候选仍待实现；超大历史无界读取及前轮全仓八项失败也未关闭。12.4保持未完成。测试机已授权且只读核对可达，尚未用其部署本片。

## 第 12.4 项后台实际摘要视图接线（本地）

后台普通/task/turn范围现把同一AppliedCompactContext传给历史种子、上下文、工具过滤和摘要器。局部transcript与live/carried均显式写scope/base，仍复用原检查点/CAS；完整归档与运行预算保留。原历史读取错误测试已改为真实canonical入口，旧无checkpoint的假摘要夹具改为真实writer/CAS。

18文件联合 **420 passed**，日志 `/tmp/background_scope_joint_20260923.log`。新增scoped transcript覆盖交错范围、无提交和竞争CAS；后台4项使用真实SimpleAgent/Store/checkpoint/CAS和原工具循环到模型消息投影，仅摘要替身，确认原三类材料丢失已修且正文只注入一次。应用视图测试另外覆盖旧view不跟随新链隐藏、错线程拒绝、未知身份保留、narrow无seed、native/text摘要消费和媒体块保持。

最后native/text分支和普通历史摘要去重补强后，应用视图、后台范围、native IR、Gateway/child恢复及接续七文件 **102 passed**；空工具记录也核对显式view线程后，应用视图/后台范围/能力展示三文件 **29 passed**。这些是后续定向复验，不与420相加。Ruff、doc sync、导入边界（0发现）、strict code-size（hard=0，基线未变）、diff与clean-package均通过。

此片没有真实HTTP、供应商、部署、重启或线上CI证据。后台完整恢复候选与首个实际payload、narrow活动IR完整计量、Gateway/child准备边界的同view绑定仍未完成；不勾选12.4，不抵充前轮全仓8项失败。超大原文读取也仍未验通过。

## 第 12.4 项作用域检查点与工具来源底座（本地）

原writer现统一写v3，scope、摘要基础与精确覆盖沿同一提交链，原generation CAS保持唯一；局部提交可以保留全线程摘要/游标。新reader只收集实际适用摘要的base覆盖。旧v1/v2显式读取并核对原摘要hash，未知旧工具身份不伪装为精确引用。

21文件联合 **562 passed**：Gateway与child完整恢复/接续、原Compact和存储、native IR、后台准备及运行、工具归档、记忆续接、超时恢复和模型生成。版本/摘要完整性补强后，`test_compact_scoped_checkpoint.py`、`test_native_tool_ir_compact_and_orphan_sweep.py`、`test_model_turn_identity.py` 三文件 **74 passed**。不将重复运行累加成独立覆盖数。

新增回归使用临时原Store、writer/CAS及真实投影，覆盖thread与task交错的摘要base、局部CAS、孤立候选、竞争、旧版本、摘要hash损坏和schema降级；相同call_id跨run/attempt/模型轮仍可分列source/retained，未知旧记录保留。原工具artifact/index重载保留四元身份，相同正文跨执行轮不覆盖旧artifact。原模型轮生成也曾在同attempt重启工作片后碰撞，真实检查点过滤复现了误隐藏；在原turn_id加入唯一nonce后回归通过，原数字序号不变。

首次核心回归因旧schema断言和缺调用身份的夹具失败，已按真实新合同更新；原完整调用与未知旧记录分别测试。扩大回归发现未知重复记录被误算新增执行进展，已修原进展判断并保留未知材料。所有复现及本片测试均无真实供应商请求，未部署或重启；前轮全仓8项失败仍未收口，线上CI不作为证据。

边界：本片只完成scope/base/coverage与来源身份底座。后台宿主仍须选择并实际应用同一个view，再接完整请求恢复；原detached/narrow三类问题尚未关闭，初次/手动及真实缓存也待验。交接见[后台准备与后续底座](docs/tasks/DECISION_MODEL_BACKGROUND_PREPARATION_HANDOFF.md)。

本片Ruff、doc sync、导入边界（0发现）、strict code-size（hard=0，尺寸基线未改）、diff与clean-package检查通过；仅保存本地提交，不推送、部署或把线上CI当验收来源。

## 第 12.4 项后台一次准备与纯投影（本地前置片）

`background_context.py` 将可能写任务进度的事实准备与无副作用渲染分离；`background_history_seed.py` 携带同次冻结的任务范围，正常种子与候选共用原历史投影。没有接通新的后台 Compact 恢复分支，也没有改变持久 schema。

后台主运行、上下文读取错误、owner 投递与能力展示四文件联合 **201 passed**。新增 `test_background_prepared_context.py` **6 passed**：重复渲染不读 store、不重复进度对账，借用请求/配置/wake 修改不影响冻结结果；detached 锚点与兄弟任务隔离、创建后摘要排除、窄审计无旧聊天，以及 unreadable/disabled 不伪造可用投影。此次未调用真实供应商、未部署或重启；不抵充前轮全仓 8 项失败。

Astra max 独立诊断使用真实 SimpleAgent、临时 ConversationStore 和原 checkpoint/CAS，仅摘要模型为替身，复现三个原有材料丢失边界：detached transcript 压缩后全局游标推进但新摘要被隔离；detached 与 narrow audit 的活动工具压缩后工具记录被隐藏，而该任务不消费新摘要。原始持久记录仍在，丢失发生于模型恢复材料投影；不是实际 HTTP 或供应商验收。后续必须先完善唯一 checkpoint 的作用域与替代关系，再接完整后台恢复。

本片 Ruff、doc sync、导入边界（0 发现）、strict code-size（hard=0，基线未改）、diff 与 clean-package 检查通过。打包检查首次因新增测试尚未进入 Git 索引失败，纳入本片后复查通过；没有忽略文件或绕过检查。并行边界与复现方式见[后台准备交接](docs/tasks/DECISION_MODEL_BACKGROUND_PREPARATION_HANDOFF.md)。

## 第 12.4 项子代理完整恢复与公共实现（本地）

Gateway 的完整恢复协调提到 `agent_core/compact_request_recovery.py`；Gateway、child 共享一次冻结、完整候选计量、摘要错误隔离及原 checkpoint/CAS，正常选模也共用 `tool_request_capture.py`。child overflow 在下一次真实 `agent.run` 准备后提交并同次发送，原独立历史、run/attempt 和权限保持。

19 文件联合 **408 passed**：新增 `test_subagent_compact_recovery.py` 8 项，加既有完整投影、Gateway 来源/恢复/工具接续/选模/错误、三宿主能力展示、child runtime/首请求、进度/熔断/预算及原生 IR 回归。新增 HTTP 替身测试覆盖 Anthropic/OpenAI × 工具开关，逐字核对候选和实际出站请求，准备恰好两次、候选一次、业务/摘要/恢复代次 0→0→1；另验 run token 取消、并发代次、摘要瞬时错误和来源加载失败均不发送恢复业务。

尺寸检查发现原子代理执行函数超限后，按职责拆出单次恢复作用域执行，未改尺寸基线；拆分后 child 三文件 **31 passed**。旧展示测试改为核对真实准备后的清除状态与实际 wire，不再要求 defer 前重做准备；摘要继承原 builder 的无候选占位段，明确断言旧推荐卡片未复活。没有真实网络、安装版 TUI、部署或重启；本片不关闭前轮全仓 8 项失败，也不证明真实缓存命中。

独立 Astra max 审查未发现具体阻塞问题；其两协议 × 两接续场景已保存为 `test_subagent_compact_recovery_continuation.py`，另 **4 passed**。恢复后实际 `read_file` 及最终业务轮保持同一新参数、历史与取消令牌；无旧历史时活动归档 CAS 先于第二次准备，真实读取仅一次，另一 child 代次不变。本片 Ruff、doc sync、导入边界零发现、strict code-size（hard=0、基线未改）、差异及 clean-package 检查通过；仅本地检查点，不推送或部署。

后台、初次加载和手动入口仍待接入完整恢复。后台须先冻结可能写任务进度的上下文，并保留 detached task 的历史锚点/lineage；不能补入 owner 历史来代替窄审计事件的空种子。文件与并行边界见[子代理恢复交接](docs/tasks/DECISION_MODEL_CHILD_COMPACT_HANDOFF.md)。

## 第 12.4 项 Gateway 完整压缩恢复请求（本地）

Gateway overflow 的恢复轮现在先保留原始历史来源，待真实提示、工具和运行材料准备完毕，再用完整下一请求计量候选；原 checkpoint/CAS 成功后直接使用获选材料发送。16 个相关测试文件联合 **337 passed**，新增四文件共 **24 项**覆盖只读来源、两种供应商协议、工具开关、关闭/应用模式、候选回退、并发代次、真实 run token 取消、未知投影、摘要瞬时/首事件超时，以及恢复后真实 `read_file` 工具接续。实际 HTTP 由内存替身捕获，未调用真实模型。

联合回归首次出现的两项失败分别是旧错误夹具缺少内部 `defer_compact` 字段，以及新接续测试错误地禁止 RuntimeFacts 保留原调用引用；已修正夹具和原生工具块断言，两个文件 65 项通过后，上述完整 16 文件组再次通过。摘要失败不得转入普通业务瞬时重试，未提交恢复不能重新发送旧请求。

本片不代表 12.4 整项完成：child、后台、初次加载及手动 Compact 尚未接入完整恢复输入；真实跨模型缓存与供应商窗口仍待验收。前轮全仓 8 项短期限失败保留未收口。没有推送、部署或重启测试机，线上 CI 不作为本片证据。

本片 Ruff、导入边界（0 发现）、strict code-size（hard=0）、差异及 clean-package 检查通过，尺寸基线未改；文档同步补齐 Gateway 模块进度与结构说明后验收。用户新增授权决策线测试机，当前仅核对既有运行环境；部署/重启仍须与占用该 Gateway 的并行任务协调，不能将可用机器计作实测完成。

## 第 12.4 项注入片段准备（本地前置片）

`test_tool_request_projection.py`、`test_gateway_model_adoption.py` 共 **65 passed**；`test_prompting_builder.py`、`test_prompting.py`、`test_subagent_first_request_selection.py` 共 **139 passed**，合计 **204 passed**。原 PromptRenderInput 保存注入片段元组，文本与原生路径保留原 join 和缓存布局；新增两协议用例验证重复正文、空片段、内嵌标题及调用方改动不会使候选误改其它片段，纯渲染不会重新准备提示或读取时间。没有实际模型调用；完整 Compact 候选与恢复请求的 payload 对照尚未接线，不计为12.4完成，也不覆盖前轮全仓失败。

本片 Ruff、doc sync、导入边界（0发现）、strict code-size（hard=0）、diff check 和 clean-package 均通过；未新增配置或长期文件，尺寸基线不变。只在独立开发工作区保存，未推送、部署或重启测试机。

## 第 12 项输出预留接受门（本地首片）

`test_gateway_conversation_compact.py` 与 `test_runtime_context_pressure.py` 共 **72 passed**。transcript 自动触发和候选接受现在复用普通请求的已知输出预留；先过输入容量门，再选 recovery target 或原可接受候选，Context 输入显示不加未来输出。新增10种受控估算矩阵保留原checkpoint/CAS与失败记账，覆盖等于边界、不足输出空间、原保留候选、未知cap和OAuth无cap；没有真实网络调用。完整恢复请求计量仍待12.4接入，不把此片当供应商窗口或第12项完整验收。

本片 Ruff、doc sync、导入边界（0发现）、strict code-size（hard=0）、diff check、clean-package 均通过，尺寸基线未改。仅本地检查点，上一轮全仓8项短期限失败仍保持未收口；本片未重跑全仓、未推送或部署。

## 第 12 项原生输入计量共源与同轮接续（本地）

`test_tool_request_projection.py`、`test_runtime_context_pressure.py`、`test_tool_model_generation.py`、`test_context_pressure_native_trigger.py`：**83 passed**。预检与发送共用 guidance/孤儿配对清扫及 ToolChoice，原本用孤立 ToolResult 撑大容量的旧夹具改为合法工具往返，并新增“出站已移除的孤立结果不触发压缩”对照。`provider_context_observation.v3` 拒绝旧 v2 校准；实际 snapshot 仍可 hydrate 校准，只有抽出的投影计量是纯函数。

原 native Compact/消息流、子代理首请求、Gateway 采用/观察、上下文预算、工具统一与线程存储八文件 **266 passed**。没有联网、重启或部署；本片不覆盖完整恢复请求、供应商 tokenizer/缓存或前轮全仓短期限 HTTP 失败，完整严格 gate 仍未通过。

独立审查补强 payload 对照为原 `_do_backend_generate`→backend→内存 HTTP 捕获，覆盖 none/specific 的真实 `thinking_disabled`。Gateway 与 child 候选均保留原工具参数存在性，由原 backend 筛选；没有工具时不多传 choice 或关闭 thinking。子代理首请求文件 **34 passed**，包含 Anthropic/MiniMax-M3 与 OpenAI/DeepSeek-v4-flash 的 tools 开/关 × none 四种组合，逐一比较原容量估算与实际 wire payload；上述四文件加 Gateway/child 自动采用六文件联合 **153 passed**。此前只在两边直接调用 backend.generate 的对照不足以证明该包装字段，旧结论以本次证据收紧。

三宿主展示接续已覆盖新任务和已有前台主 run、同片一次决策、显式空选择、失败或失效后不重决策，以及下一片重置。后台实际身份沿原 `LocalRunControl`/core 发布回传，原确认回调拒绝和关闭仍阻止执行。后台完整 runtime、Gateway 展示 Compact、新后台12项及本地控制句柄四文件 **211 passed**；child 新展示11项、原 Compact12项、原同 attempt Goal 续轮2项 **25 passed**。child 二次 prepare 在配置恢复后仍读取已清除的原参数，不复活旧 frozen surface；缺 RunParams 不伪造已评估，None/空授权、取消不重试保持。三组定向回归合计389项通过；没有真实 Jev 网络请求、重启或部署，完整恢复输入和供应商缓存仍未验收。

本片 Ruff、暂存 diff 的 doc sync、导入边界（0发现）、strict code-size（hard=0、基线未改）、diff check 和 clean-package 均通过。仅保存隔离分支本地检查点，不推送/部署；下面保留的上一轮全仓失败尚未收口，不能将本片定向通过改写成全仓验收通过。

## 决策模型与插件基线合并后全仓回归（未通过）

在隔离决策分支合入插件已提交基线 `f04ec3a42`，并将决策线引用迁到唯一 `common.cancellation` 后，九文件交叉测试 **298 passed**，`test_decision_*.py` **685 passed**。全仓跑到终态为 **19,869 passed、8 failed、21 skipped、35 xfailed、5 xpassed**。八处失败均为本地 HTTP/流式首事件或显式决策探测的短期限测试；相同五文件第一次重跑 **6 failed、85 passed、1 xpassed**，第二次 **1 failed、90 passed、1 xpassed**，第三次 **91 passed、1 xpassed**。单独的流式正常用例与决策探测失败用例也通过；当前高负载下尚未证明确定根因，不能把第三次通过替代全仓 gate。未推送、部署或合并到 `main`，线上 CI 未作为验收来源。

## 决策模型早期全仓集成 gate（未通过）

隔离分支本地检查点 `ae539e38a` 与插件线已提交基线 `f04ec3a42` 从 `4974fe7` 分叉；在第7步负责人确认独立工作范围后，只在决策分支做未提交 merge，不动原仓库的 TUI 脏文件。六个实际冲突均为文档或生成的代码尺寸报告，双线记录保留并重新生成报告；生产代码由 Git 自动合并。随后交叉定向收集暴露插件线已删除 `tooling.cancellation`、决策线新增文件仍导入旧路径；17 个本线生产/测试文件改用唯一 `common.cancellation`，没有恢复 facade。插件命令、工具装配、子代理首轮采用、Gateway 模型选择及能力推荐九文件组合 **298 passed**；Ruff、compileall、导入边界零发现、文档同步、strict code-size、clean-package 与暂存差异检查通过。此证据不含完整合并后全仓回归，也不包含插件第7步和原仓库 TUI 未提交工作。

使用仓库现有虚拟环境（全局 Python 缺 dev extra 的 `hypothesis`、`pyte`）跑全仓 pytest。第一轮在 9,554 passed 时中断并定位六项 Jev 字节窗口误拦及一项插件命令用例；修复 Jev 后，排除该插件命令测试的第二轮在 10,490 passed 时定位普通 `task_local` 误入子代理发送栅栏；修复后第三轮在 11,840 passed 时定位旧记忆路由测试形参，三处相关 focused 均已通过。随后从记忆路由文件向后扫，在 1,542 passed 后由 `test_packaging.py::test_current_production_import_boundaries_have_no_unapproved_findings` 停止：本线新增的 Gateway 模型采用/观察、Compact 重建和 `user_config` 共有 **13 处**跨层导入。该守卫是架构硬门；不能加入白名单冒充通过。插件命令单测属并行“模块重构”线，本工作区没有修改对应实现或测试，待其 owner 交接后共同复核。当时 50 个新增文件未跟踪，`check_clean_package.py .` 因此失败；后续结构修复与暂存的结果见下文。**完整本地严格 gate 未通过，未提交/推送/合并/部署，也没有线上 CI 验收。**

后续结构修复把主会话选模应用编排移出 `gateway_parts`、同 turn Compact 跨层构造移到应用层，并将唯一 runner 线程本地上下文移到 `agent/runtime_context.py`；旧路径没有转发模块。`check_import_boundaries.py` 现为 **0 findings**。Gateway 观察/采用移位后 **65 passed**，Gateway/Compact **85 passed**，运行身份、设置、子代理、规划和 packaging 的 10 文件组合 **268 passed、4 xfailed**；Ruff 与生产模块 compileall 通过。插件命令用例仍待并行线基线对齐；打包门的后续结果见下文。

移位后全仓重跑曾在后台主代理 CLI 用例处看到空的中间消息；该用例单独重跑即通过。原等待条件只要任意消息出现就立即停止 worker，可能在最终正文持久化前读取占位消息。现等待期只以预期最终正文为完成条件，保留 5 秒有界期限；同用例独立重复 5 次均通过。此为测试时序修复，不改变后台主代理运行代码，也不把单独通过当作全仓通过。

该修复后的第一轮全仓在 **14,704 passed** 停于旧审计测试仍 patch 已拆走的 `_execute_create_subagents`；改为当前准备入口后，审计文件 **54 passed**。第二轮在 6,952 passed 遇到协作存储并发测试失败，该用例独立重复 30 次均通过；第三轮越过该位置，在 **14,789 passed** 停于恢复分类守卫：决策响应无效、探测失败、设置冲突及供应商响应超限四码未登记。现已按可选增强保留原主链、CAS 重读和响应缩小语义补入唯一 `ERROR_CONTRACTS`，恢复策略文件 **16 passed**，Ruff 与 strict code-size 通过。全仓尚需重跑到终态；并发偶发失败尚无稳定复现，不能当作已根除。排除并行插件命令用例的运行不等于完整严格 gate 通过。

补齐错误合同后的全仓回归（仅排除并行插件命令测试）跑到终态：**19,755 passed、1 failed、21 skipped、35 xfailed、5 xpassed、8 deselected**。唯一失败是旧 `test_tool_operation_managed_gate.py` 测试桩缺 `home_paths.root`，而已有插件 owner 装配需要这个可信根；与插件线负责人确认该测试不在其当前认领范围，且原仓库新版本已补同一字段。本隔离树同步测试桩后，该失败用例单独通过，工具装配、恢复策略与旧审计组合 **94 passed**。被排除的插件命令参数化用例独立执行为 **7 passed、1 failed**：旧基线对 `/plugins enable demo` 的期望文案与当前拒绝/查询回执不一致；不在本线修改插件产品语义。新增项目文件暂存后 `check_clean_package.py .` 与 `git diff --cached --check` 通过。完整全仓仍未在这些修复后再跑到终态，插件线基线尚未对齐，故完整严格 gate 仍未通过。

## 决策模型第 12 项 task_local 发送栅栏回归

全仓回归的新增定位：普通 `task_local` 带 `run_id` 却没有 canonical 子代理记录，首次发送栅栏误抛 `FileNotFoundError`。现只对这种未登记的局部运行跳过子代理专用标记；原局部运行、首次请求选模和 child 上下文三个文件联合 **55 passed**，Ruff 通过。全仓复跑仍在进行，不能因此宣称整体通过；详见[容量交接](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

同轮全仓回归发现旧 `test_memory_routing_context.py` 的唯一直接调用仍传 `_routed_memory_context_for_request(task_local=...)`，而 P5-A/记忆总闸已将该私有形参改为 `skip_formal_recall`；原测试期望的非隔离路由语义不变。同步参数后，路由、召回前、首轮记忆和原召回四文件 **57 passed**，Ruff 通过；不将此测试接线失败当成真实 Jev 质量问题。

## 决策模型 P5-E1 授权与输入预算原语（本地）

原 `decision_settings` v1→v2 保留 revision/overrides，增加默认关闭的实验能力和有界 thread 授权信封；原 `ModelCallLedger` 同锁内串行预留声明的完整输入量及请求数，未知发送/缺 usage 保守占用。新原语 52 项通过；设置、决策调用、账本、工具及 TUI 共 13 文件 347 项，加菜单索引集成 9 项，合计 **356 passed**。Ruff、doc sync、strict AST/code-size、diff 通过。TUI 只可开关能力，不建立实验许可；当前无可靠输入 token 上界、宿主用户授权入口与发送前硬门，实验联网仍失败关闭。本片不代表完整 E1/E2 或真实试验验收，见[交接](docs/tasks/DECISION_MODEL_EXPERIMENT_E1_HANDOFF.md)。

## 决策模型 P5-D 主会话三轮隔离真实样本

原 Gateway TUI、隔离普通 user owner、`model_selection=apply` 做三次普通中文新会话任务。默认 2 秒 Jev 期限取消一次，自动保留官方 M2.7；原设置 CAS 临时延至单次 8 秒/阶段 10 秒后，Jev 分别给 `need_data`、当前 M2.7，主会话均完成且线程保持默认选择。真实 HTTP 旁观为 Jev 5 次（4 成功、1 取消）与官方 M2.7 4 次成功，无 M3/DeepSeek 请求；首轮决策输入未知，不能补零。测试后一次原 CAS 恢复三个字段，owner overrides hash 与开跑前相同，8431 停止，日常 8420 未动。此证据只证明安全保留，**没有**真实跨模型采用、容量或历史通过，见[交接](docs/tasks/DECISION_MODEL_MAIN_MODEL_LIVE_HANDOFF.md)。

## 决策模型 P4-B 普通 user owner 中文配置复测

第一次隔离真实样本确认 `user_config` decision-only 已在主模型工具清单，但当前 Gateway 主回合拿不到可信 thread、约 8.9k 的 read 回执在原 4000 字符模型预览内缺 `revision`，实际五次 read 后 thread patch 被拒、四次猜测 owner revision 均 `STALE_VERSION`；设置未被修改。底层只修原工具的可信主回合 RunParams 线程来源和原投影序列化顺序，子代理缺自身线程不借父线程、legacy view/set 仍双门拒绝；相关两组本地 **91 + 63 passed**。第二次新 TUI 普通中文任务，模型自主 read(thread,14/0)→patch(thread,14/0) 成功，原持久回执及线程文件均为 14/1、enabled=true、stage_timeout=6、subagent_model=observe，最终回复准确；模型没有另发第三次 read。测试者仅在结束后用原 CAS reset 三项，thread 升至 14/2、overrides 清空，8431 停止，8420 不动。详见[交接](docs/tasks/DECISION_MODEL_NATURAL_CONFIG_FIX_HANDOFF.md)。

主线加固缺失 owner 身份不能获得 legacy 动作的防护用例后，和设置、Gateway 模型采用及子代理首次请求的 13 文件组合 **353 passed**，相关 Ruff 通过；这是本地合同证据，不另算真实 Jev 样本。

## 决策模型 P5-C 自学习候选只读审计

[自学习审计](docs/tasks/DECISION_MODEL_SELF_LEARNING_AUDIT.md)核对现行 runner lesson Candidate、旧草稿迁移、Skill 快照和 guard；四个已有 focused 文件 **48 passed**。生产尚无 `enable_self_learning` / `my-agent learn`、Skill 提案/确认/写入服务，因此没有 Jev 自学习消费、正式 Skill 写入或真实验收；旧 README/指南的已实现说法已校正。正式 Skill 必须用户确认的开发规则仍有效。

## 决策模型原模型目录持久代次（本地）

私有目录 v5、共享发布 v2 在原保存事务轮换随机代次；已启用准备可在原锁自动初始化旧目录，普通读取与关闭不写。原快照/最终锁 guard 覆盖 provider/model/凭据/OAuth/shared 变化，缺来源保持未知。目录、Provider、OAuth、Gateway 八文件 focused **142 passed**，包括新 Python 进程读回、原 OS 锁竞争、保存失败和部分迁移；Ruff、strict AST、doc sync、diff 通过。此片尚无子代理自动采用或真实异模执行；见[目录代次交接](docs/tasks/DECISION_MODEL_CATALOG_GENERATION_HANDOFF.md)。
主线将该八文件与下方恢复原语五文件同跑，**280 passed**；原目录换代没有破坏设置 CAS 与恢复前值。

## 决策模型 P5-A 召回前补充查询首片

[P5-A 审计与实施记录](docs/tasks/DECISION_MODEL_PRE_RECALL_AUDIT.md)确认普通聊天尚无可信显式查历史结构化意图，Jev 不能直接跳过原召回。默认关闭的独立 `pre_recall` 点现已接正式上下文准备：原完整查询先召回；仅有剩余槽位和字符预算时，Jev 可建议一次有界补充查询，同原 scope 追加已确认的正式事实。候选检索不提前 touch，最终采用重读正式源；与 P3 召回后排序共用原阶段绝对期限。`test_decision_pre_recall.py`、`test_decision_recall.py`、`test_memory_condense_v2.py`、`test_memory_recall_v2.py`、`test_memory_first_loop.py`、`test_decision_settings.py` 联合 **136 passed**，相关 Ruff 通过；新增真实 JSONL 的受控漏召回样本证明可只追加第二条事实、只确认它的访问。隔离真实 Jev 两轮 off/observe/apply 共4次官方 HTTP，关闭零请求、每轮观察和应用各1次且实际版本 `jev-1.13.0`；应用样本原词面检索已命中两条事实，没有新增，诊断 `no_addition`，不可算召回质量收益。仍缺真实 Jev 已知漏召回与 Gateway 真实聊天对照，不计 P5-A 全项通过。

## 决策模型 P5-C 现有 Todo 优先建议首片

[规划审计](docs/tasks/DECISION_MODEL_PLANNING_AUDIT.md)核对现有 Todo、workflow plan、Goal、子代理创建的权威边界。默认关闭的 `planning` 首片现只在当前主代理 `task_progress(read)` 的原 canonical 回执后，对2–24个 open exact ID 建议一个优先评估项；观察、非选择、错误、超时或旧账本保持原回执，apply 也不改计划/Goal/派工。原 Todo 工具/设置/TUI 等六文件联合 **133 passed**，本线 Ruff 通过。隔离真实 Jev 两轮共3次HTTP尝试：4秒观察超时而原回执不变，8秒设置下关闭零请求、观察和应用成功且各输入783；应用仅追加 `todo-rollback` 软提示，原 Todo账未变。见[交接](docs/tasks/DECISION_MODEL_PLANNING_HANDOFF.md)。没有 Gateway TUI 或实际业务规划收益证明。

## 决策模型 P5-C 交付质量提示只读审计

[质量提示审计](docs/tasks/DECISION_MODEL_DELIVERY_QUALITY_AUDIT.md)核对原 verification、ready artifact、closeout、Goal 与 child 权威；推荐仅用原工具归档后的精确引用给主模型一个软复核焦点。六个现有测试文件 **4931 passed**，doc sync/diff 通过；这是原链回归，没有 Jev 生产接线或真实质量收益。

## 决策模型 P5-C 动作候选只读审计

[动作候选审计](docs/tasks/DECISION_MODEL_ACTION_CANDIDATE_AUDIT.md)核对现行 Computer Use/MCP/审批与尚未接生产的 Browser/OCR 候选；当前缺可信 observation/candidate ID 与失效代次，不接 Jev 生成坐标、命令或输入。原链 focused **128 passed**，doc sync/diff 通过；未接生产决策点或真实视觉任务。

扩大子代理工具循环回归发现旧 fake backend 的无约束 Mock `api_base` 不能被 JSON 编码，触发连接校准指纹异常。底层现只将标准 JSON 连接字段送入原加盐摘要；不透明值以进程内对象身份参与版本比较、不输出 `repr`。原失败测试与 context-pressure 整文件 **25 passed**，不涉及 Jev 网络或模型切换。

## 决策模型 P5-G 设置恢复基础原语（本地）

原 `decision_settings` 服务新增内部 `restore`，在原 owner→thread 锁序与完整两层 CAS 下同次写回 set/unset，成功只前进一次版本；后续用户修改、非法字段/范围及已删除模型引用不被旧恢复覆盖。设置、通知、作用范围、模型操作与主代理设置工具五文件联合 **138 passed**，Ruff、doc sync、diff 通过。它只是恢复原语，尚无自动实验、授权、请求前硬预算或真实收益验收；设计与缺口见[自实验审计](docs/tasks/DECISION_MODEL_SELF_EXPERIMENT_AUDIT.md)。

## 决策模型 P5-D 原线程选择版本首片（本地）

原 `ConversationThread` 增加单调模型选择版本、来源和最近显式版本；同 ID 显式选择也前进并终结 pending，旧 v10 全缺字段只在内存归一未知，部分或矛盾字段拒绝。与子代理新线程初始化联合 **170 passed**；Ruff、doc sync、diff 和原 strict AST 判据通过。尚无 Gateway 自动采用、真实异模主会话或持久恢复验收；见[交接](docs/tasks/DECISION_MODEL_MAIN_MODEL_SELECTION_HANDOFF.md)。

Gateway 在准确会话车道后已有请求级 observe-only 建议首片：关闭时与原 Gateway 输入字节及文件读写路径等价，开启观察只记录建议与原回退事实，typed recovery/Compact 不重问。定向组合 **382 passed**，见[观察交接](docs/tasks/DECISION_MODEL_MAIN_MODEL_OBSERVE_HANDOFF.md)；实际采用与跨模型执行仍待验。
Stage C 已把建议接到同一主请求的真实首次发送前：原 PromptBuilder 完整材料、IR/native 工具、候选 provider payload 与最终字节复核后，以目录代次→准确车道 T→原 thread CAS 提交；局部拒绝沿原模型一次，已提交/不确定不跨模型重发。最新本片 **34 passed**，此前 11 文件联合 **302 passed**，Ruff/doc sync/diff/严格 AST 通过；容量仍是有余量的工程估计，fake HTTP 不等于真实供应商验收。见[采用交接](docs/tasks/DECISION_MODEL_MAIN_MODEL_ADOPTION_HANDOFF.md)。
普通中文配置请求的[P4-B 审计](docs/tasks/DECISION_MODEL_NATURAL_CONFIG_AUDIT.md)曾用原工具、服务和 TUI 四文件联合 **86 passed**；该阶段只证明结构化 read/patch/reset、CAS 和菜单接线。首次[真实中文设置验收](docs/tasks/DECISION_MODEL_NATURAL_CONFIG_LIVE_HANDOFF.md)失败：隔离 user owner 原 manifest 中没有 `user_config`，原配置版本/hash 不变。其后修复及成功复测见本文件开头的 P4-B 记录；旧失败仍保留为定位证据，不代表当前状态。
子代理真实异模续验见[隔离交接](docs/tasks/DECISION_MODEL_CHILD_LIVE_HANDOFF.md)：六轮普通中文父任务均 completed，十个 child 均 DONE；三候选自然建议的官方 M3 和只允许 DeepSeek 作为替代候选时的 OpenCode DeepSeek 各有一条自动采用、真实首请求/工具后续轮及终态。该线新增20次 Jev HTTP，19次报告输入263,256，一次超时用量未知；和前序合计47次。原4秒下第五轮超时/冷却仍保留 M2.7，第六轮 DeepSeek 成功，无需用户逐 child 操作。第四轮 `selection_changed` 当时提交分支未留证、图片模态与多 owner 故障矩阵未验，P2-B/12/13 整项仍不关闭。

## 决策模型 P5-D 主会话选模只读审计

Gateway、原显式选模、model scope、history/Compact、250K/1M 窗口及 Responses 历史回放的源码边界已在 [P5-D 审计](docs/tasks/DECISION_MODEL_MAIN_MODEL_AUDIT.md)登记。原五文件 focused **75 passed**，doc sync/文档空白检查通过；这是已有合同回归，尚未实现主会话自动采用或真实异模切换。

## 决策模型子代理 pending 建议合同（本地）

创建前批量 Jev 建议仅作为 host-owned pending 原子写入新 child thread；有效模型保持继承，已存在 thread 不重植，显式同值/异值选模在原 CAS 内终结 pending。根/子/孙、refreeze、配置/连接变化、伪造属性及断点重试等 10 文件组合 **278 passed**，Ruff、doc sync、定向 diff 与原 strict AST 判据通过。旧 pending 仍可能对应已按继承模型执行过的 child，必须补首次请求资格与发送前栅栏后才能自动采用；详见[容量审计与交接](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

## 决策模型 P5-C 已归档网页阅读顺序首片（本地）

`external_material_order` 默认关闭，原 web_fetch extract 的 producer、Executor、归档、refs、账本及 text/native 展示接缝保持。独立设置与 TUI、完整来源、脱敏安全投影、期限/取消和原结果对照共 11 文件 **349 passed**；Ruff、doc sync、diff 与本片只读 AST strict 检查通过。通用 TUI 测试 helper 将固定 120ms 等待改为 3 秒内核对真实 UI 状态，两次旧 CAS 时序失败及最终复测均留证。随后隔离真实 Jev 5 次 HTTP 尝试：4 次成功，输入共8530，1次短期限用量未知；首轮 not_needed 保留原展示，第二样本完整建议自动追加2→3→1，原 archive/refs/hash/账均相同。页面源为本地受控材料，不证明真实互联网检索质量或安装版 TUI；详见 [P5-C 交接](docs/tasks/DECISION_MODEL_EXTERNAL_MATERIAL_ORDER_HANDOFF.md)。

## 决策模型只统计输入，不做价格计费

用户明确决策模型以后可用本地模型，因此原生决策调用已停止调用通用 USD 价格估算和 owner/run 成本累计；普通生成模型的原成本路径不变。`test_decision_model_call.py` 的成功调用断言决策价格指标和成本账均无写入，原请求终态与实际输入 token 仍在同一 ModelCallLedger；加原 HTTP 服务、设置探测和用量展示四文件联合 **63 passed**。Ruff、doc sync 与 `git diff --check` 通过。

## 决策模型 P5-B 正式记忆关系首片（本地）

新 `curator_relation` 默认关闭、限 owner 后台，和已有 Curator 标签共用一次阶段及原 lease 期限。完整消息与带真实版本的短 long-term 条目可得到仅供原提取参考的关系提示；截断、缺版本、正式条目变化、关闭和故障保留原批次。`test_decision_curator_relation.py`、`test_decision_curator.py`、`test_decision_settings_scope.py` 联合 105 项，原 Curator/Candidate/Promotion/设置/TUI 六文件 188 项，合计 293 项通过；随后隔离真实Jev的12对短样本符合预设，后续提取仍是本地替身，未证明广泛关系质量或全库覆盖。文件与命令见 [P5-B 交接](docs/tasks/DECISION_MODEL_P5B_HANDOFF.md)。

## 决策模型 TODO12 Jev容量门与实际接口（进行中）

`test_decision_protocol.py`、`test_typesafe_decision.py` 曾联合69项通过；后续全仓回归发现 UTF-8 字节直接比较 token 上限误拦 78,419 字节批量请求，使六项 localhost HTTP 用例无法发网。改为复用原 token 估算并留一成余量后，`test_typesafe_decision.py` 与 `test_decision_capability_http.py` 联合 **24 passed**，覆盖明显超窗提前拒绝、正常批量发送、超时、在途设置更改及 401/500 回退。该估算不是精确供应商 tokenizer 或硬容量证明，原 JSON 字节资源帽不变。
容量核对使用序列化UTF-8字节上界，不把它当实际token计数；这组测试不覆盖完整子代理换模、Compact或真实缓存。
隔离 owner 下官方 Jev 已累计20次实际HTTP尝试：前13次包含协议、Gateway TUI 普通中文回复及三子代理保留原模型派工；P5-B 新增一次成功和一次50ms期限取消；P5-C 新增五次，含一次完整页序自动追加与一次短期限取消。官方 M3 与 OpenCode DeepSeek 的短连接探针均成功，但 Jev 改选后的真实执行仍待验；完整记录见[真实验收](docs/tasks/DECISION_MODEL_REAL_VALIDATION.md)。
子代理候选配置新增原 profile ID 列表，经同一设置服务和 TUI 字段校验；空数组沿授权目录，非空数组只缩小候选，变更使在途建议失效。设置复核时的用户取消须原样传出。`test_decision_subagent.py`、`test_decision_settings_scope.py`、`test_tui_decision_menu.py` 联合62项通过，不能替代三模型真实切换。
创建前容量旧粗估已撤销；子代理准备/逐候选最终配置和原输出 cap 复用同一生产入口，完整首请求尚缺激活后状态、child 历史和工具证明，因此当前不自动改选。28 个相关文件 427 项定向测试通过；离线抓到三候选实际适配器输出 cap，但这不证明完整 wire/schema 或真实换模，详见 [容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。
另修 exact child thread 的旧快照写回竞态：`test_conversation_store.py` 含读后模型/Compact 状态更新的定向回归，52 项通过；现有记录复读原 thread 文件最新值并只补缺失身份，不覆盖用户选择。

## 决策模型 TODO10 能力推荐与上下文减量（本地）

8文件父侧联合127项通过：`test_decision_capability_consumer.py`、`test_decision_capability_http.py`、
`test_decision_skill_projection.py`、`test_tool_presentation_projection.py`、`test_decision_skill_tool_settings.py`、
`test_decision_settings_notifications.py`、`test_tui_decision_menu.py`、`test_decision_settings_scope.py`。
原运行准备入口→seed→params→真实PromptBuilder/native schema验证实际输入减量；重复渲染不重新调用决策。
本地HTTP验证成功、300ms超时、在途关闭/改策略、401/500不重试及迟到终态；原搜索可找回schema，插件撤销仍阻止旧绑定执行。
关闭/观察/不确定保持原输入；真实接口反馈后改为独立候选题，96题完整协议通过，原总字节/节点上限继续生效，不截断尾部。
另与`test_memory_runtime_compact_auto_continuation.py`、`test_compact_semantic_summary.py`联合回归通过。
真实Jev质量、输入token净收益、跨模型完整窗口和provider缓存仍待12/13，不能拿夹具字节量当收益。
命令、责任边界见 [TODO10交接](docs/tasks/DECISION_MODEL_CAPABILITY_HANDOFF.md)。

真实API首批7次调用发现并修复跨题选择槽与概率舍入两个问题；保留1次真实2秒超时，不计作判断成功。
修复后4秒配置下同需求0.728秒返回，4个相关能力include、4个无关能力not_needed；是合成材料的真实接口证据，不是完整TUI验收。
协议、能力消费者、本地HTTP及适配器联合92项通过，脱敏原始响应replay保留0.99概率总和，不归一化。
明细和缺口见[真实验收记录](docs/tasks/DECISION_MODEL_REAL_VALIDATION.md)。

## 决策模型 TODO11 设置与显式探测（本地）

最终14文件联合243项通过，包含原ToolExecutor→user_config→原生HTTP→原会话用量结算及失败真实工具状态。

原设置菜单/模型操作/薄客户端到 Gateway 的真实本机 HTTP，再到原生决策 HTTP 与账本已组合验证。
六文件菜单/运输/用量组合81项、七文件配置/范围/服务组合117项通过；后端构造共用与准确冷却恢复后，
`test_decision_model_operations.py`、`test_decision_gateway_transport.py`、`test_decision_service.py` 54项通过。
受控认证容器验证运输和owner隔离，不代替真实认证部署；真实按键表单不代替安装版TUI或收费模型质量。
文件、原接口、失败对照与剩余边界见 [TODO11交接](docs/tasks/DECISION_MODEL_SETTINGS_HANDOFF.md)。

## 决策模型 07/09 业务入口（本地）

子代理片联合 10 个 focused 文件 209 项通过；父侧追加同修复身份/不同幂等身份的名字边界，
与召回片联合执行 `test_decision_subagent.py`、`test_decision_recall.py`、`test_memory_recall_v2.py`、
`test_memory_first_loop.py`，83 项通过。实际 ToolExecutor + RunParams + 工具开启可持久采用建议模型；
召回覆盖原授权/预算后排序、来源撤销、完整准备入口与本轮上下文复用。
这里使用协议替身及原本地存储/worker，不代表真实 Jev 质量、完整窗口缓存或安装版 TUI 验收。

## 决策模型 Curator 与用户后台 scope（本地）

`test_decision_curator.py` 与原 Curator、自适应超时/缩批测试共 **117 passed**。
父侧 `python3 -m pytest agent_py_agent/tests/test_decision_owner_scope.py agent_py_agent/tests/test_decision_curator.py agent_py_agent/tests/test_decision_service_http.py -o addopts='' -q --tb=short`：**62 passed**。
覆盖 owner 后台明确 run/空 thread、拒绝活动会话冒充后台、后台预算冻结、原设置隔离、真实 localhost HTTP，
以及实际公共服务→原 worker/账本→临时标注→原提取/验证/游标；缺数据等四类业务结果与运行失败分开。
原 lease 剩余头寸限制极大后台超时，关闭不编码材料；提示超预算、失效和错误沿原链。
原 Curator 缺独立持久模型用量结算，本片不伪造前台统计或新建账本；真实质量/部署未验。
文件、命令和限制见 [Curator 交接](docs/tasks/DECISION_MODEL_P2_CURATOR_HANDOFF.md)。
消费复核补充：service、owner_scope、curator、service_http 四文件联合 **95 passed**；覆盖响应返回后关闭/观察/
修改预算失效、实际调用 deadline 不延长和用户取消传播，Curator 在正式附注释前使用公共复核。

## 决策模型 P1 实际服务组合（本地）

17 个直接相关文件联合 **399 passed**，覆盖设置/迁移、协议、worker/准入、策略通知、原账本与显示。
随后新增会话写锁争用检查，`test_decision_service_http.py` **8 passed**：使用真实 localhost HTTP、原配置、
原账本和活动 TUI sink，验证正常/观察、关闭零请求、401/500、超时、在途关闭及用量刷新不等待会话写锁。
决策结束自动发原活动统计帧；`usage_only` 不持久写显示，下一原模型边界沿原路径保存，原 finalizer 仍负责用量结算。
`test_decision_service.py` 验证阶段共用时间、配置变化、锁忙、逐题错误、冷却隔离、旧阶段未退出不可叠加。
`test_decision_settings_notifications.py` 验证通知逆序及会话覆盖恢复继承，关闭不能被较旧通知复活。
详细命令、严格 gate 和限制见 [P1 服务交接](docs/tasks/DECISION_MODEL_P1FG_HANDOFF.md)。
真实 Jev、真实 MiniMax、当前安装版 TUI 和实际业务消费者尚未验收；本地 HTTP 样本不代表模型决策质量。

## 决策模型共用设置与原用量展示（本地）

`test_decision_settings.py` 与原 `test_user_config_capability.py` 验证同一服务/工具的读取、修改、恢复继承、
有限正秒数、owner/thread 双版本冲突、共享撤销、缺凭据仍可关闭和可信线程身份。
迁移与配置相关 12 个文件联合 251 项通过；原模型目录当前 v4，线程当前 v10，不改原连接、历史和模型选择。

`test_model_call_ledger_partitions.py` 验证终态单调、裁剪后用途累计、逐字段来源、准确保留句柄及迟到 HTTP。
`test_decision_usage_metrics.py` 与原 conversation_store/tui_model_metrics 验证实际存储增量、快照重放、
部分输入缺报、真实零值、原一行展示以及后台追加后基数刷新；不以模型正文判断用量。
设置、账本、存储和显示 7 个文件联合 198 项通过；该组合未调用真实 Jev 或 MiniMax。
`usage_only` 接原活动 request/run 的显示，用途统计不增加普通生成轮数或覆盖最近生成指标。
跨独立活动范围的即时遥测与实际业务消费者仍随 TODO 08/12 验收，不能以静态渲染代替真实 TUI。

本片 focused 命令：
`python3 -m pytest agent_py_agent/tests/test_decision_settings.py agent_py_agent/tests/test_user_config_capability.py agent_py_agent/tests/test_model_call_ledger.py agent_py_agent/tests/test_model_call_ledger_partitions.py agent_py_agent/tests/test_conversation_store.py agent_py_agent/tests/test_tui_model_metrics.py agent_py_agent/tests/test_decision_usage_metrics.py -o addopts='' -q --tb=short`。

## 决策模型 P1-C/D 有界调用与原准入组件（本地）

18 个直接相关文件 **315 passed**，覆盖精确取消、启动前取消、迟到结果、慢清理与非协作调用的资源保留，
以及原 HTTP、Curator、准入/预算和模块边界。原 Curator 线程/队列实现已迁入唯一 `bounded_call.py`，没有第二份执行器。
父侧组合测试验证 caller 超时后 worker 仍持模型名额，同资源和额外可选调用被拒绝，普通 LLM 可取得保留名额；
worker 真正退出后才允许同资源新请求。只证明组件组合，没有声称实际决策服务或真实模型已接通。
Curator 超时不再额外 join 0.5 秒：返回时仍存活就记 still-running，退出前不缩批重试；原游标/缩批规则未改。
修复并覆盖清旗后的 late hook、线程构造/Context复制/启动失败、大期限等待溢出和 caller BaseException 收尾。
复核只读；没有实际模型、日常配置写入、部署或 Gateway 重启。完整命令及剩余边界见 [组件交接](docs/tasks/DECISION_MODEL_P1CD_HANDOFF.md)。

## 决策模型 P1-B 原生协议与传输组合（本地）

`test_decision_protocol.py` 与 `test_typesafe_decision.py` 合计 59 项通过，验证冻结输入、版本与来源绑定、
候选校验、逐题缺失/错误、原生三类问题、缺失 usage 不补零、迟到网络/解析结果拒绝、取消不重试。
本地 HTTP 服务器收到了真实 `/v1/systemone` 中文 state/questions 请求，并返回逐题结果与用量；没有调用真实 Jev。
这不能代替可选调用的资源准入、有界 worker、阶段预算、账本或用户实际 TUI 验收，这些仍未接线。
联合 7 个文件 **216 项通过**（零失败/错误/跳过）：上述两个文件，加 `test_gateway_strict_request.py`、
`test_gateway_helpers.py`、`test_provider_request_scope.py`、`test_compact_request_budget.py`、`test_runtime_module_boundaries.py`。
严格传输覆盖 46 项：真实慢 HTTP、302、零重试不读错误正文、解析前后同一期限、关闭竞态及 1 MiB 超深 JSON。
显式严格 JSON 在原解析器前做 64 层资源预检，普通请求仍用原解析/重试与 SSE 合同。
协议新增编码器调用哨兵：超量整数/字符串在序列化前拒绝；合法 JSON 内坏题不损坏好题。
首轮深 JSON 测试假设 1500 层必然触发 RecursionError，与仓库递归上限不符，已改为真正资源界限测试；
结构守卫发现的深嵌套已按校验职责拆分，未修改尺寸基线。命令详见 [P1-B 交接](docs/tasks/DECISION_MODEL_P1B_HANDOFF.md)。

## 决策模型 P1-A 本地配置合同验收

10 个相关文件 213 项通过：决策用途配置 17 项，以及原模型、服务商、共享、线程选择、Gateway、OAuth、
OAuth 传输、采样和模型菜单回归。覆盖保存无网络、公开字段无密钥、只读旧版本迁移、生成误选拒绝、同名用途隔离、
共享与 owner 权限/撤销；没有调用真实 Jev 或 MiniMax，没有修改日常配置、启动 Gateway 或部署。
首轮既有迁移测试仍预期 v2，已更新为当前 v3 后通过；新测试导入排序由 Ruff 修正。
独立复核发现旧表单会丢 decision 用途、旧 v1 迁移会丢自定义头，已修复并通过原表单与保存服务组合回归。
本片没有时间/请求/设置双入口实现，其验收不能算通过；完整剩余项见 [执行 Goal](docs/tasks/DECISION_MODEL_GOAL.md)。

命令：对 `test_decision_model_profiles.py`、`test_model_profiles.py`、`test_model_provider_management.py`、
`test_shared_model_catalog.py`、`test_thread_model_selection.py`、`test_gateway_model_profiles.py` 运行 focused pytest，
再覆盖 `test_model_oauth.py`、`test_model_oauth_transport.py`、`test_tui_model_menu.py`、`test_provider_sampling.py`；
最终联合命令使用 `python3 -m pytest <以上十个文件> -o addopts='' -q --tb=short`，213 passed in 4.29s。

## 第 9 步插件面板真实 TUI（本机，fae9d5855 / wheel 86339f65，模型 MiniMax-M2.7）

- 通过：TUI 内安装、启用、`/plugins@activity-line show` 打开；两个中文任务期间面板显示"工作中 · 正在使用 run_command"，结束回到空闲，两份产物数字经独立脚本核对正确；面板打开时停用 → 面板立即消失、插件进程回收；再启用后可再打开；面板打开时卸载 → 面板与进程都消失，列表只剩其他插件；面板打开时 Esc 中断 → TUI 显示已中断，面板同时回到空闲。
- 发现并修复：① `resume` 后未经补全直接输入面板命令，本地目录为空而落到宿主被拒；改为先显式读一次目录（`test_tui_plugin_panels.py` 新增回归）。② 只有展示动作的插件使用卡给出"请用插件打开面板"的中文示例，实测模型只能回答没有该能力；改为不给落空示例（`test_plugin_commands.py` 新增回归）。
- 已知缺口：纯模型思考阶段面板显示空闲，见 [插件展示](docs/design/PLUGIN_DISPLAY.md#已知缺口)。
- 断连重连：本轮样本中模型改用后台终端并误用参数，回合在断连前已按"结果无法确认"结束，未构成有效断连样本；断连/重连/停止仍以 TUI232 为准。

## 第 10 步视觉、界面型插件与组合验收（本机 1a8169887→a2e26178b，2026-09-24）

- M3 视觉：在 TUI `/model` 的官方 MiniMax 服务商（api.minimaxi.com，复用原密钥引用）下新增模型 MiniMax-M3，本会话切到 M3；`/attach` 附加 420×204 测试图后要求不调用工具描述图片，回答"3 行：HELLO FROM / IMAGE TEXT / PLUGIN READ，黑字白底"，全对且当轮工具 0。请求记录 input_media 的 sha256（68bc9caf…）与原图一致，模型用量记录 models=['MiniMax-M3']、backend anthropic_compatible。
- harness-console（宿主只读 API 的首个消费者）：中文请求后以 Chrome `--app` 独立窗口打开工作台；私有链接文件 0600；页面数据来自真实宿主（20 个线程、11 个插件启用状态逐项一致、Gateway 进程号与 /status 一致），截图见本机私有证据；停用后服务端口与窗口进程一起回收；再启用可用默认浏览器重新打开；卸载后端口关闭、无残留进程。
- 组合验收（单 Gateway，三路 TUI）：
  - A（插件长任务，03:33:59—03:43:26）：派 3 个子代理分段统计 12 个月 4,800 条订单，汇总与独立答案逐区域一致（全年完成 9,179,658 元 / 3,602 单）。本版（1a8169887，决策筛选逐工具判断）把 genui/design/browser 插件工具折叠，模型未用 tool_search，改用内置写文件并如实报告"插件工具不在快照"；savepoint-lite 当时本机未安装，如实报告。
  - B（内置任务，同时运行）：5 个文件的总和与最大值全部正确；C（管理）：A 运行中停用 status-pet（A 的面板立即消失、任务不受影响）与 web-board，再启用 status-pet。
  - 切到 a2e26178b（决策线按插件分组出题）后同一插件链在新会话中依次调用 genui export、design create、savepoint save、browser open，产物逐项正确（条形图每柱数值、卡片副标题、快照在插件数据目录、页面标题核对）。
  - 逐个卸载全部 11 个插件：浏览器进程与插件环境目录清空、列表为空；新会话内置工具任务正确（orders-01.csv 400 行、金额 1,015,997）；历史会话中的插件调用记录保留。
  - 未满足：组合长任务实际约 9.5 分钟，未达 15—30 分钟；组合中未包含 M3 图像理解（单独验证）。

## 第 10 步第二、三批真实 TUI（本机 6767bb4ed→4b8cdebbd，测试机同版；模型 MiniMax-M2.7）

- worktable-lite（本机）：面板列出最近会话，编号、顺序、相对时间和"当前"标记与会话记录逐项一致（246 个会话、1 个损坏记录被跳过）。
- status-pet（本机）：中文任务期间 空闲 → 正在工作（显示当前工具/思考中）→ 空闲；停用 → `/plugins configure --file`（style=whale、name=蓝蓝）→ 启用后面板变为鲸鱼"蓝蓝"。
- genui-lite（本机）：中文请求依次调用 table 与 export，生成的 sales.html 标题正确、无脚本、无外部资源、权限 644，表内 10 行与输入逐项一致、极值（10月最高、2月最低）正确。随包 Skill：启用时 `skill_search` 找到 `plugin:genui-lite:genui-table` 并读取；停用后只读探针与模型的 `skill_search` 都找不到（matches 为空）。
- design-lite（本机）：安装后中文请求调用 create 生成带 data-dl 标记与 #2f6feb 的海报；再次中文请求调用 edit，只改动两处标题行，其余字节不变。首轮失败原因为该包未安装成功（安装命令被模型选择弹窗吞掉），不是产品缺陷；补做的通用改进见下。
- image-text（本机）：显式命令 OCR 读出 "HELLO FROM / IMAGE TEXT / PLUGIN READ"，Gateway 环境找到 tesseract；（测试机）无 tesseract 时明确返回 OCR_UNAVAILABLE 并附图片元数据。
- browser-lite（本机）：显式命令逐条调用时第二步起 NO_PAGE——显式命令每次是一次性插件连接，命令结束浏览器随之回收，有状态插件不能跨显式命令串联（记录为显式命令语义边界）；改用一次中文请求由模型在同一连接内完成 open→fill→click→read，页面显示"已提交：李雷/B"。（测试机）无浏览器时审批后明确返回 BROWSER_UNAVAILABLE。
- web-board（本机）：中文请求开服务，无令牌 403、越界 403；发现宿主会从工具结果中脱敏 token 参数，模型只能给出"<令牌>"的链接，改为插件把完整链接写入 0600 私有文件并用默认浏览器打开（0.1.1）。
- desktop-lite：组件测试中真实 macOS 调用 osascript/pbcopy/open 退出码 0；（测试机）无桌面时审批后明确返回 DESKTOP_UNAVAILABLE。
- 通用改进（4b8cdebbd）：插件工具说明写成"插件 <ID>（<简介>）"、ID 进入检索关键词、`hints.provider_id=plugin:<ID>`；折叠提示按插件列出被折叠插件。

## 第 10 步第三批插件（本地，待发布）

- `test_plugin_host_api.py`：宿主 API 令牌只在 Gateway 服务时发放，换代/停用/Gateway 停止即失效且不复活；主题白名单外拒绝；线程只给公开字段；v4 描述往返与校验。
- `test_harness_console_package.py`（5 项）：从源码构建 harness-console 包，确认包描述 v4、`host_api == ["read"]`、三个工具均为 mutating；测试内假宿主 API（校验 `X-Plugin-Host-Token`，返回固定 threads/activity/plugins/gateway）经环境变量注入真实 MCP 进程：open（不开浏览器）→ 读 0600 的 last-link.txt → 无/错令牌 403 → 带令牌首页 200 并下发 HttpOnly+SameSite=Strict cookie、页面无外部资源、CSP 只放行 self 与内联 → `/api/state` 只认 cookie，返回白名单整理数据（默认选最近线程、切换线程、未声明字段不透传）且不含宿主令牌、1 秒缓存不重复打宿主 → POST/HEAD 405、再次 open 复用 → 假宿主改 403 后报"插件已停用或令牌失效" → stop 后端口连不上；缺宿主 API 环境变量时报"宿主 API 不可用"；desktop 用记录 argv 的假浏览器断言 `--app=<带令牌链接>` 与数据目录下独立 `app-profile`、再次 desktop 复用窗口、stop 与插件进程退出后假窗口进程都被结束；坏设置启动失败。

- `test_plugin_skills.py::test_plugin_tools_carry_plugin_identity_and_hidden_plugins_are_named`：插件工具说明含插件 ID 与简介、关键词含 ID 拆分词；折叠提示在短名单为空时仍按插件列出被折叠插件，非插件工具不列。

- `test_desktop_lite_package.py`（7 项）：从源码构建 desktop-lite 包并起真实 MCP 进程，系统程序全部用记录 argv/stdin 的假脚本经设置注入（不真弹通知、不开程序）；覆盖三个工具均为 mutating 且缺上下文拒绝、通知文本含引号/反斜杠/`& do shell script` 原样作为 argv 且 AppleScript 固定走 stdin、标题/内容超长与 NUL 拒绝、open 拒绝符号链接/目录/缺失/上溯/越界/.command/.APP/可执行位并把绝对路径传给打开程序、剪贴板文本经 stdin、程序缺失返回“不可用”、非零退出码、超时杀进程、坏设置启动失败。
- `test_image_text_package.py`（10 项）：从源码构建 image-text 包并起真实 MCP 进程；本机有 tesseract 时用测试内极简 PNG 编码器画的点阵英文图实识别（无 tesseract 时 skip），另覆盖 tesseract 缺失返回 OCR_UNAVAILABLE 且仍带元数据（PNG/伪装扩展名/GIF/JPEG/WebP 宽高）、非图片与损坏头部、超上限、链接/上溯/越界、非法语言名、语言包缺失列出已装语言、假 tesseract 超时被杀且临时文件删除、坏设置启动失败。
- `test_design_lite_package.py`（15 项）：从源码构建 design-lite 包，确认包描述 v3、`skills == ["design-card"]`、wheel 内 SKILL.md 可按 frontmatter 解析；起真实 MCP 进程：三种模板 create（无外部资源、无 script、标题副标题转义、权限 0644）、默认色与非法颜色/未知模板/非 .html 拒绝、已存在拒绝与 `--overwrite`、edit 只改目标字段（其余字节不变、保持原权限位、返回新旧值）、非本插件/非 UTF-8/标记被改的文件拒绝、字段缺失与未给字段、越界/上溯/链接/写入范围拒绝、缺写入上下文失败。
- `test_browser_lite_package.py`（20 项）：WebSocket 帧纯单测（客户端掩码、7/16/64 位长度分支、截断前缀视为数据不足、服务端掩码/保留位/未知操作码/控制帧分片与超长/超限拒绝、分片重组与非法序列、socketpair 上跨读拼帧、自动 pong 与 close）；从源码构建 browser-lite 包并起真实 MCP 进程 + 本机 Chrome（无浏览器时 skip）：工作区测试表单 open → read → fill（输入框、按文字选下拉）→ click → read 出现"已提交：张三/B"，同进程复用同一浏览器，close 后 pid 消失、profile 清空；选择器 0 个/多个/语法错、不可填元素、无此选项、未开页面、缺上下文；https://example.com、工作区外 file://、`../` 上溯、ftp/javascript 被拒，本机 302 到外部与点击外链被拦截并停到空白页，页面内外部 fetch 被拦、允许主机放行；allowed_hosts 设置生效；chrome_path 不存在时五个工具都报"浏览器不可用"；宿主 stop、stdin EOF、仅对插件 pid 发 SIGTERM 与空闲超时后浏览器 pid 均不存在（`os.kill(pid, 0)` 失败）；缺数据目录与坏设置。
- `test_web_board_package.py`（6 项）：从源码构建 web-board 包并起真实 MCP 进程，用 urllib 真实访问插件网页：只绑 127.0.0.1 随机端口；无/错令牌 403、查询令牌 200 并下发 HttpOnly cookie、cookie 单独可访问；目录列表与子目录；含 `<script>` 文本转义、Markdown `<pre>`、HTML 走 sandbox iframe、图片 `<img>`+`/raw`；`..`/绝对路径 400，指向外部或内部的符号链接 403，缺失 404；POST/PUT 405、HEAD 无正文；status 字段与请求计数；再次 serve 停旧端口，坏根目录不影响旧服务；stop 与空闲超时后端口连不上；MCP 客户端回收与 stdin EOF 退出后端口连不上；坏设置启动失败。

## 第 10 步第二批宿主补充（本地，待发布）

- `test_plugin_skills.py`：包描述 v3 往返与名单校验（空、非法名、重复均拒绝，v1 不带 skills）；只取已启用且声明 Skill 的插件目录；插件 Skill 来源为 `plugin:<ID>`、不覆盖同名用户 Skill；提供方不再返回后下一次快照即消失；总闸关闭时不出现。
- `test_genui_lite_package.py`（11 项）：从源码构建 genui-lite 包，确认包描述 v3、`skills == ["genui-table"]` 且 wheel 内有 SKILL.md；起真实 MCP 进程覆盖两种数据格式的 table、`--chart` 条形图等比长度、非数字/缺失列与坏 JSON/格式/列不一致报错、行数截断与大小上限；export 写出独立 HTML（无脚本、无外部资源引用、特殊字符转义、权限 0644）、输出已存在拒绝与 `--overwrite`、非 .html/上溯/越界/父目录链接拒绝、读写范围裁决、缺写入上下文失败。
- `test_plugin_display_service.py::test_sessions_topic_is_lazy_and_whitelisted`：会话列表提供方只在订阅时调用，坏行丢弃、metadata 不转发、读取失败按空列表。
- `test_worktable_lite_package.py`：从源码构建 worktable-lite 并在独立解释器真实进程渲染 sessions 面板：空列表、多条含当前会话与相对时间、`max_rows` 截断（20 条时让出一行给提示）、`hide_current`、时间缺失；设置经 `MY_AGENT_PLUGIN_SETTINGS` 注入，坏设置退出码 2 且不回显值；输出过 `normalize_display` 且不截断。
- 真实插件组件：`test_status_pet_package.py`（11 项）从源码构建 v2 纯展示包 status-pet，由真实 MCP 进程经展示服务渲染 cat/whale/robot × 工作/等待审批/空闲：三种状态图各不相同、等待审批以【等待审批】开头，`name` 设置生效，输出经核心校验不截断、不超限、无控制字符；未知外观、空名或超 12 字符、未声明字段被宿主 schema 与 `/plugins configure` 拒绝且原设置不变，进程收到坏设置以退出码 2 失败且不回显设置值。

## 第 10 步第一批真实 TUI（1be5753ff / wheel 2d049be8，模型 MiniMax-M2.7）

- 本机：新开 TUI 未经补全直接 `/plugins@activity-line show` 可打开面板（resume/新开修复生效）；纯展示插件使用卡改为"面板只能用命令打开"。
- context-inspector（本机）：发消息前面板显示"还没有快照"；一次中文请求后显示 23,981 / 200,000（12.0%）、触发线 180,000、消息 2,779、运行引导 0、工具目录 15,316、压缩 0，与 TUI 状态行一致；面板打开时停用 → 面板与进程消失；再启用重开有数字；卸载后面板与进程消失，数据目录保留。
- workspace-peek 0.1.1（本机，SDK 0.2.0）：卸载 0.1.0 后安装新包并启用；中文"请用 workspace-peek 插件预览 peek-note.txt，第二行的数字"经插件工具读出 4217，正确。`/plugins update` 尚未实现，升级按卸载后重装。
- savepoint-lite（测试机，默认确认权限）：包在 owner 家目录外时安装被读取授权拒绝（按设计），放入家目录后安装启用；中文请求保存快照 → 审批"允许一次" → 快照只写插件数据目录，内容与原文件逐字节一致；改坏文件后中文请求恢复 → 首次恢复 `--expect` 不符被拒（未写）→ 模型用返回的当前哈希重试 → 审批 → 恢复后 sha256 与快照一致、权限位 644 保持。另一个 TUI 在调用等审批时停用插件 → 批准后该调用记 TOOL_EXECUTION_FAILED，无新快照、无残留进程；再启用后显式 `list` 显示原快照仍在；卸载后进程消失、快照数据保留、用户文件不变。
- 发现：① 同回合内已停用插件的工具仍先弹审批（冻结目录 + 审批先于执行可用性检查），批准后才失败，零副作用但体验差，记入台账待设计；② 显式插件命令回执直接显示双重转义 JSON，已改为展示 content 文本并缩进 JSON（`test_plugin_management.py` 新增回归）；③ 首次渲染偶发"展示内容无效"一次后自愈、未能复现，已加失败类型日志。

## 第 10 步第一批宿主补充（本地，待发布）

- `test_plugin_display_service.py::test_context_topic_forwards_only_public_numbers`：`context` 主题只转发上下文公开数字白名单和压缩次数，缺快照标记未知。
- `test_plugin_enable.py`：真实启用的插件进程环境带 `MY_AGENT_PLUGIN_DATA_DIR`，目录已按 owner + 插件 ID 创建。
- 真实插件组件：`test_context_inspector_package.py` 从源码构建 v2 纯展示包 context-inspector，由真实 MCP 进程经展示服务渲染 `context` 主题：无快照只显示提示不编数字，有快照显示千分位用量 / 窗口百分比、触发线、三部分组成、压缩次数与"估算"标注，输出经核心校验不截断。
- `test_savepoint_lite_package.py`（13 项）：从源码构建 savepoint-lite 包并起真实 MCP 进程；覆盖 save→list→restore 往返（保持权限位、工作区无新文件、快照只在数据目录）、`--expect` 不符拒绝并返回当前 sha256、写入上下文 check 拒绝、符号链接/父目录链接/上溯/越界拒绝、保存后换成链接不被跟随、缺写入上下文时 restore 失败、大小与数量上限设置生效、损坏快照不恢复、缺数据目录与坏设置。

## 第 10 步插件写入上下文（本地，待发布）

- `test_workspace_write_context.py`（10 项）：2000 组随机写入边界 × 4 个目标路径，逐项断言「插件允许 ⇔ 宿主 `validate_write_boundary` 允许且目标在写入根之内」，并验证序列化往返后裁决不变；另覆盖无范围时只允许 cwd、owner 墙、畸形载荷拒绝、锚点取最具体根，以及只对协商且声明写效果的工具下发、缺上下文时发送前 `not_started`。

## 第 9 步 TUI 拆分首片：按键动作外移（本地，待发布）

- 纯搬移，行为不变：`tui_keybindings.py`（2656→1744 行）中的副作用动作移到新模块 `tui_actions.py`：记忆命令后台执行、本地/Gateway 排队、执行选项快照、控制命令提交与对账器、补充消息提交与对账器、子代理插话/停止、Esc 中断，以及只被它们使用或需被双方共享的 `_handle_command_params`、`_required_tui_runtime`、`_active_tui_runtime`、`_agent_guidance_sent_notice`、`_restore_failed_agent_input`、`_safe_http_status` 和两个重试间隔常量；`_run_clipboard_tool`、`_load_tmux_clipboard_buffer` 移到 `tui_clipboard.py`。
- `tui_actions.py` 运行时不导入 `tui_keybindings`；唯一延迟导入是 `_restore_failed_agent_input` 回调内的 `_set_input_draft`。`tui_plugin_commands.py` 的延迟导入改指 `tui_actions`。
- 测试只改 monkeypatch/调用路径到实际解析位置（`test_tui_input.py`、`test_tui_plugin_panels.py`、`test_host_command_stream.py`），断言不变；`CODE_SIZE_BASELINE.json` 两条既有函数豁免的路径随函数改到 `tui_actions.py`，数值不变。
- 验证：test_tui_*、test_chat_parts、test_cli_chat、test_chat_prompt_queue、test_chat_control_runtime、test_plugin_command*、test_architecture_guardrails、test_host_command_stream 共 42 个文件 862 passed；ruff、strict code-size、doc sync、diff check 通过。

## 第 9 步插件面板（本地，待发布）

- 协议与服务：`test_plugin_display_service.py`（19 项），覆盖声明校验、展示描述截断与控制字符过滤、包描述 v1 字节不变与 v2 往返、首次加载、同输入不重复渲染、最新输入合并、撤销与停用回收、无展示能力、失败退避、空闲关闭和请求数上限；测试中发现并修复"错误结果被当成最新、退避后永不重试"。
- Gateway 入口：`test_gateway_plugin_panels.py`（9 项），覆盖来源鉴权先于读正文、坏请求、总开关关闭、冷 owner 不加载实例以及伪造身份字段无效。
- 真实插件组件：`test_activity_line_package.py` 从源码构建 v2 纯展示包，由真实 MCP 进程经展示服务渲染，关闭服务时进程被回收。
- TUI：`test_tui_plugin_panels.py`（11 项），覆盖打开/关闭/上限、三种排版、宿主不可用即移除、传输失败退避、错误保留正文、正文行数上限、退出事件结束线程，以及展示动作本地拦截不经宿主；另有 675 项现有 TUI 相关测试通过。
- 真实 TUI 验收待新包部署后进行。

## TUI 可扩展性集成分支修复（claude/integrate-tui-scalability，本地）

- 背景：origin/main（含第 7/8 步）叠加 codex/tui-scalability 的 6 个提交后，出现 9 个稳定失败；另有 SSE 超时与 TUI fixture server 共 8 个用例在此前报告中失败，但在本分支原始 HEAD 与修复后均无法复现（单独、分组、4 路并发各跑通过），判为负载相关抖动，未改代码。
- 真实回归（改产品代码）：`poll_gateway_chunks` 新增退避后把睡眠贴合到 deadline，最小采样窗被缩短，租约心跳尚未落盘就判请求死亡；恢复 0.1s 最小采样间隔，退避只放大空闲间隔。`SafeFormattedLines.join` 改为显式序列参数，满足无 `*args` 服务接口守卫。`INPUT_MEDIA_INVALID` 按决策线同文登记为不可重试的用户输入校验错误。
- 替身过时（只改测试）：后台 supervisor 替身补 `_owner_pool/_next_owner_retire_at`；遗留巡检替身池接受 `touch` 并提供 `pin`；原生 IR 参数替身补 `task_attributes`；chat client 断言补 `input_media: []`（与决策线同文）。
- 验证：tui_*/gateway_*/timeout*/stream*/slow_model*/background_main*/native_tool*/input_media*/chat_client* 共 91 个文件加架构守卫与错误码策略：2,059 passed、2 skipped、1 xpassed；ruff、strict code-size、diff check 通过。

## 委派与交付核对软引导（本地，待发布）

- 改动：`coordinator_tool_boundary_text` 增加三条引导：没有要求委派时优先自己做；派工时写清要交回的产出和核对方式；收到结果后先用工具抽查，不直接转述"通过"。子代理 runner 的 Required Output 要求数字和核对结论必须来自本轮工具输出，未核对的如实标出。只改文字，能力、权限和完成判定不变；设计记录见 DESIGN_LEDGER。
- 复测（7c467a4d3，本机）：TUI238 与 233 同题，24 个孩子都执行了计算命令，核对文件、totals.csv 和 summary 全部正确（上一轮 10/24 出错）；TUI239 与 229 同题，孩子数字和父级回核都正确，但父级自己新算的状态小计仍靠心算出错，并编造理由解释矛盾。因此在共用的 prompts/default.md 证据段增加数字来源和矛盾重算规则（+2 条断言，相关 15 个文件 515 passed、3 项既有 xfail）。自发委派没有被折中引导阻止，只记录为观察。
- 再测（3b72c4d7b，TUI240 本机 / 241 测试机，与 229 同题）：240 所有数字正确（621 行明细），但缺少合计行；241 父级用工具回核，发现并如实披露了一组孩子的错误数字，combined.csv 已改正；它自己写的按状态分项仍有 3 个金额错误（和等于正确总额），但没有编造解释。结论：孩子错数未被发现、父级编造理由这两类问题已不再出现；父级在单张汇总表里偶发心算，接近模型能力边界，不再叠加提示词。
- 验证：新增 5 条断言。所有引用协调策略、runner 提示、create_subagents、稳定前缀或系统提示的 79 个测试文件：1,941 passed、28 项既有 xfail。真实效果需在新包上用 TUI229/233 同题复测。

## 第8步新版原生 TUI 矩阵（TUI228—237，已部署版本 66a598cf3）

- 环境：本机与测试机各一个 Gateway，默认入口同版；每个 TUI 都经 `/model` 选择官方 MiniMax-M2.7，并核对实际端点；每个任务只提交一次中文需求，测试者只操作 Esc、`/stop`、审批和客户端断连这类明确的控制。
- 长任务（本机 233）：连续约 27 分钟的实际工作；模型自行派出 24 个孩子，全部结清；主线程提交 2 次会话 Compact（约 19.4 万和 19.9 万 tokens 时触发，窗口 20 万），两份摘要都逐字保留了原需求、规则和目录；工具失败 3 次后模型自行换路；13 个工作片均为 done，无残留锁和进程。
- 多孩子（本机 229）：三个孩子并行、时间重叠，完成通知已消费；combined.csv 只有一行表头，TUI58 的重复表头问题未复现。
- 普通对照（测试机 228、230）：run、attempt、task_run 均结清，工具操作全部成功。
- Esc 中断（测试机 231）：前台命令执行中按 Esc，run 与 attempt 变为 cancelled，命令如实记为 UNKNOWN（`effect_outcome_unknown:CANCELLED`），无锁。
- 断连与停止（测试机 232）：只杀 TUI 客户端进程后，Gateway → bwrap → bash → python 这棵进程树继续运行；`resume` 后恢复实时状态；从重连后的客户端发 `/stop`，15 秒内整棵进程树被回收；runtime_reason 为 `conversation_user_stop`，来源是结构化控制。
- 审批（测试机 236/237）：审批框显示精确的命令。选拒绝时没有产生任何工具操作，模型也没有绕路；选允许一次时命令恰好执行一次，执行时刻就是批准时刻。删除请求（234/235）：Shell 删除按设计被硬门拦截，补丁删除工作区文件属于"修改"类，不弹审批。
- 未覆盖：单轮内实时工具压缩时的逐调用精确来源路径（本批任务均未触发）；被中断的前台调用没有持久化的进程清理事实，只有实时进程快照可作证据。
- 业务交付（与框架结论分开）：228 完全正确。230 的 totals.csv 缺日期维度。229、233 的部分孩子没有用工具计算，而是心算合计，写出了错误数字（233 中 24 个核对文件有 10 个出错，每个都写着"通过"），父级也没有用原始数据回核，最终报告失实。这与 TUI58、225—227 属于同一类通用交付核验缺口，不按 CSV 或提示词加专项分支。

## 第8步发布后全仓回归修复（本地，未发布）

- 起因：66a598cf3 发布前只跑了相关文件，没有跑全仓。之后全仓复验共 19,034 项，其中 15 项是真实失败（另有 1 项因复验用的快照不是 git 仓库而失败，属于环境原因，在真实 checkout 中通过）。
- 11 项 `test_manager_runner_capability_requests`：第 7 步把准入和收口依赖收窄为 `manager.runtime_db` 和 `manager.conversation_store` 以后，轻量 manager 替身没有跟着补上这两个属性。现在按生产 `_attach_runtime_db` 和构造默认值显式设为 None，走原来的非托管路径。
- 1 项 `test_silent_swallow_stage2`：911a53245 以后，发布账本改为经锁内 `mutate` 写入，但替身仍然让 `save` 失败，因此断言的错误日志从未触发。改为让 `mutate` 失败，断言意图不变。
- 1 项 `test_observation_route`：7.10 在执行前按 id 重读待处理信封，2707fbbe3 当时漏补了这一个替身。改为使用真实 `WakeSignal`，并由 `pending_one` 返回仍处于 pending 的原信封。
- 1 项架构守卫：`compact_text_source.py` 的 `__exit__` 改为显式三参数签名，行为不变（不吞异常）。
- 验证：上述文件、`test_cli_update` 以及所有引用 `compact_text_source` 的测试，共 68 项通过；另外 Ruff、doc sync、diff 检查通过。除一处签名外，生产行为没有改动。教训：远端发布前至少要把直接受影响模块的全部测试文件纳入，只跑定向文件会漏掉夹具。


## 第8步精确来源与耐久清理事实组合（本地，未发布）

- 合入逐调用来源：来源为 canonical run/attempt/call，旧无来源记录保留 uncertain；来源/尾部和同号跨请求不能混淆，旧无 refs 的编号算法不变。
- 耐久恢复缺口：原测试仅从内存 archive 恢复。新测试使用真实 executor → externalizer → 磁盘索引 → carried reader → 模型上下文；短／外置输出原有 2 项实际失败，修复后相关四文件106项通过。索引只保留共享有界 process，不含 PID；调用身份保持。
- 发布组合首轮：48个直接相关文件1212 passed、24项既有xfail（49.55秒）。不同轮次的数字不累计作验收总数。
- 独立末审发现新 ID 未包含未知尾部的逐位裸 ID，合法候选可碰撞；真实 Store 提交赢家后写入 CAS 败者，读取的尾部实际被改为另一值。新增用例红转绿，新 ID 纳入 retained IDs；旧 ID 不改。最终受影响四文件159 passed（5.24秒），覆盖来源、原生 Compact、Gateway Compact、清理事实恢复。
- 旧无身份大历史仍可能无法安全 Compact；不制造身份或误删，不声称 provider overflow 已兼容。旧 reader 不能直接读取新混源账本，回滚须保持新增数据与可读运行时。
- 本地严格 gate 已通过：全目录 Ruff、doc sync、strict code-size（hard=0、blocked=False，基线未变）、diff、clean-package。未运行真实模型或新版 TUI；本轮不是双机部署，线上 CI 未作为验收来源。

## 第8步并发段与收口组合

并发段片0c19b2e3c精选为8468998b6，两个直接测试文件65 passed，包含21项新增因果／交错用例；原取消、审批、线程执行和provider顺序记账未移动。当前组合另把no-action两项常量移到唯一执行模块，原数值和测试断言不变。

最终11文件组合 **283 passed／20项既有xfail（11.52秒）**：上一节收口九文件加tool_round_execution和tool_segment_planning。覆盖顺序屏障、冲突后继续串行且不丢结果、逐候选动态Compact查询、异常前零工具执行／零记账、配置按需读取和原批上限。原请求／响应、unknown与Goal收口合同同时验证。

本地Ruff、doc sync、strict-size hard=0、diff与clean-package全部通过，尺寸基线未改。无真实模型、TUI、Gateway操作，仍待Compact来源引用片后做发布及原生验收，线上CI不是本片证据。

## 第8步收口依赖

相同三文件基线81 passed／20既有xfail；新增窄收口及绑定用例后四文件95 passed／20既有xfail。最终九文件组合218 passed／20既有xfail（11.43秒）：tool_loop_closeout、cli_resume_contract、unknown_outcome_tool_halt、test_tools/test_tool_loop、no_action_gate_round、tool_call_guardrail_runtime、timeout_recovery_delivery、runtime_gate_ledger、conversation_goal_tools。

新增14项覆盖请求／响应与用量归属、build→generate→strip→原因读取顺序，四阶段普通异常与中断均不重试、不调用后续操作；四种宿主绑定保持同一Agent/params和轮号。unknown用例在strip时改变halt，证明读取发生在生成和剥离之后；合同读取失败不能产生可续跑结论。原taxonomy与should_continue_task断言仅迁调用入口，未放宽。

Ruff、doc sync、strict code-size hard=0通过，尺寸基线未改；完整发布验收仍待来源修复和并发段组合。本片未启动真实模型、TUI或Gateway，不代替第8步真实矩阵。

## 第8步工具事实与唯一循环入口组合

工具事实7482a40d6与去空转发c6f45425b合并后，16个相关文件运行退出码0，**349 passed、20项既有xfail**。仓库默认-q叠加命令-q不显示尾部汇总，此处按完整进度符号计数；没有重跑相同测试以补数字。

组合包含tool_context_reducer、mcp_registration、tool_output_externalizer、runtime_gate_ledger、memory_compact_runtime_handoff、subagent_runtime_compact、compact_semantic_summary、tool_call_guardrail_runtime，以及provider_timeout_acceptance／continuation／resume_narrowing／resume_probe、subagent_runtime_guards、thread_interrupt、timeout_recovery_delivery、test_tools/test_tool_loop。独立入口片另验runtime_guidance、native_tool_use_ir_messages_flow为104 passed／4既有xfail。各组不能累加为唯一覆盖数。

保持原执行／审批／中断顺序和所有既有断言，局部monkeypatch避免测试多次驱动串用旧闭包；唯一合并冲突仅是模块注释。全目录Ruff、doc sync、strict-size hard=0通过；完整发布与真实TUI仍待剩余第8步边界收口。

## 第8步工具执行事实与恢复投影

10项因果用例先红后绿：process清理事实在内联、指定live输出、外置摘要及最终脱敏中保持，命令非零／清理成功与清理未确认分开。只读审阅补出巨整数和非有限浮点导致异常／非标准JSON，三项红转绿；缺失、畸形类型不补成成功，PID／实例列表只投影数量。verification原块保持。
Fake handler经过产品executor、输出归档、循环记录和native适配，两种长度分支逐字比较同源投影；保留原status=failed与effect_outcome=unknown的独立合同。carried恢复再读同一有界process，数量不丢。MCP fake服务的structuredContent伪process只留外部正文，不被提升为宿主事实。测试准备曾遗漏MCP默认审批和误认unknown effect等于unknown status，按真实合同修正测试；没有改生产执行语义迁就断言。
最终8文件组合 **241 passed，8.45秒**：tool_context_reducer、mcp_registration、tool_output_externalizer、runtime_gate_ledger、memory_compact_runtime_handoff、subagent_runtime_compact、compact_semantic_summary、tool_call_guardrail_runtime。没有真实模型或TUI调用，没有启动／停止共享Gateway。
本地Ruff、doc sync、strict code-size hard=0已通过，尺寸基线未改；完整发布仍待与空转发清理组合，线上CI不作为本片验收来源。


## 第8步 Compact 顺序来源与提交边界组合

- 原生提交后投影抛 RuntimeError／InterruptedError 的两项因果用例先红后绿：thread generation 已为1时不恢复旧IR、不增加提交失败数。审阅补出无binding临时回合仍需回滚，新增两项先红后绿，未改变其原语义。
- C来源覆盖两遍hash、追加／截短／改写拒绝、Unicode切片、取消与迭代器关闭、重试预算及大历史峰值。最初组合63 passed／1 failed，峰值1,870,598字节高于原界；查明重复iterencode闭包循环积累，改为可证明有界的小结构直接编码，大来源仍流式。原断言保持，不调GC；等值原型5,005组数值／异常类型无差异。
- 两项非文本／未知媒体来源上层验证：摘要分段前拒绝，模型调用0，原消息、summary、generation、cursor及checkpoint保持；记录一次真实失败，不生成机械摘要冒充媒体覆盖。测试准备曾误用不存在的MessageStore.load及未指定原生model_surface，修正夹具后才进入目标路径，不记作产品红转绿。
- 最终14文件组合 **333 passed、20项既有xfail，20.14秒**：native_tool_ir_compact_and_orphan_sweep、gateway_conversation_compact、compact_text_source、compact_request_budget、archive_tokens、tool_loop_model_turn、tool_loop_recovery_scope、compact_circuit_breaker、compact_progress、compact_semantic_summary、memory_compact_context_bundle、subagent_runtime_compact、tools/test_tool_loop、r223_audit_regressions。
- 本地严格gate已通过：上述focused组合、全目录Ruff、doc sync、strict code-size hard=0、diff及clean-package；尺寸基线未改。Ruff中一次测试导入排序问题已修正，尺寸中间版本的嵌套红灯按职责拆helper后通过。线上CI未作为验收来源。
- 本片未调用真实模型、未启动TUI、未部署或修改共享Gateway。第8步真实矩阵仍待完整组合包，不用本片验证替代。


## 第8步请求周期与有界读取组合候选

模型周期新增五项顺序／失败用例，完整验证首次响应后读重试上限、provider超限先恢复再回收、回收失败不再请求、preflight不恢复、最后prompt与response配对、异常不消费临时工具。渐进工具两项测试迁到实际请求周期，不保留旧私有清理入口。五文件164 passed、4既有xfail。
A/B最小移植原71项及相邻112项通过；独立审阅后新增三个用例真实失败：Unicode空白行被拒及极大created_at错误分类。字节预算先调整为能容纳该行，确认失败发生在解码阶段后才修生产代码；七文件复验186 passed。没有吞错误或自动修复尾行，旧游标和原幂等锁保留。
最终18文件组合 **456 passed、24既有xfail，18.17秒**：前述模型采纳十文件，加native_tool_ir_compact_and_orphan_sweep、archive_tokens、conversation_message_scan、conversation_store、conversation_message_stream、conversation_history_paging、gateway_foreground_transcript、cli_run_conversation。分组结果有重叠；本次增删远低于全仓阈值，没有追加全仓pytest。
本候选Ruff、doc sync（补齐memory模块文档后）、strict code-size、diff和clean-package均通过；尺寸基线未改，生成报告保留仓库外。
候选ef355f822已集成本地主线57baa13cb，两者源码tree一致；集成没有改生产代码，不重复跑相同组合。
本轮没有新增真实模型验收；已部署版本仍为第7步包。完整Compact scope／摘要来源链未移植，不能以此声称第8步完成。

## 第8.2首片模型采纳：本地合同验证

新增 `test_tool_loop_model_turn.py` 十项：成功／中断结构化返回先计量再确认；provider超限恢复原输入、preflight不消费；请求或计量失败不确认；瞬断中实时读取submission及待确认ID，拒绝重发已提交输入；安全重试只计量最终响应。
三个空响应旧xfail已迁为native假后端：补齐generate关键字和空text字段、用实际工作目录准备文件、从messages读取工具结果与恢复引导、为第二次工具使用独立call id。保留原3／3／5次调用、单次read／write、产物内容和操作核验断言；最初失败来自过期夹具，没有为通过而改变生产行为。
十文件组合：模型采纳、tool_loop、runtime_guidance、tool_context_ptl_retry、thread_interrupt、tool_model_generation、provider_timeout_acceptance、provider_transient_auto_resume、subagent_runtime_guards、tool_progressive_disclosure，结果 **212 passed、24既有xfail，14.47秒**。基线两项中断替身错误已显式修复，三个空响应xfail转为真实通过，其余既有xfail未改。
诊断准备失误单列：一次直接Python诊断漏用了pytest的HOME隔离，触及日常模型配置；已终止该准确诊断进程并改在独立临时HOME执行。该调用不是原生TUI、不计验收证据，共享Gateway未操作。后续本片测试均为隔离假后端；本片Ruff、doc sync、strict code-size、diff和登记后的clean-package均通过；第8步新版原生TUI仍待组合实现。

## 第8步基线发现的中断测试替身遗漏

在85050017d开始模型／工具循环拆分前，四文件基线为112 passed、27既有xfail、2 failed。两项失败均发生于后台claim依赖装配：test_thread_interrupt中的两个最小Store未提供7.10已要求的wakes.pending_one，尚未进入中断／结束断言。仅为这两个替身补显式只读查询接口；同文件12项通过，生产代码与默认部署不变，不用默认旁路掩盖遗漏。此前365项相关测试没有覆盖这两个替身，保留失败记录；这不是新片重构造成的回归，也不宣称第8步验收完成。

## 新版 TUI225—227：第7步框架收口与业务失败分账

发布源码7280c5b3e、wheel SHA256前缀c3f1242f；双机各1,274包文件逐项一致，默认入口／唯一Gateway同版。常规Git HTTPS运输失败后，经GitHub Git Database API逐项核对原blob、tree、commit SHA，远端main非强制快进到相同提交；没有重写提交或跳过本地严格gate。线上CI没有对应运行记录，未作为验收依据。
三路真实原生TUI分别为225本机授权续跑、226本机保留OPEN接手、227测试机与225同题；各一次普通中文需求，原生/model选择并核对官方MiniMax-M2.7服务端点，日常默认不改。原始线程／请求／claim／工具账、提示词哈希、模型来源、最终产物归档及进程证据留仓库外。

- TUI225：224.71秒，同run两次grant、三次孩子attempt，主子共7个attempt均done，无锁，三个孩子宿主退出。两条公开final为派出回执和唯一root_subagents_terminal交付，7条wake全部handled；未出现220重复收尾。两个grant均在旧session退出后，不能算217时序命中。
- TUI225业务失败：300行CSV逐行正确，父级实际执行完整逐行校验，并写入正确汇总；约34.23秒后孩子再次覆盖summary，最终平方和9025450、立方和2038532250均错，正确值分别9045050、2038522500。原工具账与孩子原生write_file参数证实覆盖顺序；父级之后未读回最终摘要，仍报告全部通过。观察者保留错误文件，不补产物、不发修复提示；这是共享文件交接与验证时机样本，不据此新增CSV专项核心门。
- TUI226：92.97秒，真实正式申请保持OPEN／无grant，孩子BLOCKED，父级接手；3个attempt均done、无锁、准确宿主退出。初始回执后只有一次subagent_non_success_terminal交付，未命中能力completed分支。240行逐行正确，父级实际检查平方立方和哈希；summary遗漏汇总合计，完整业务验收仍失败，外部读回不能补算模型履约。
- TUI227：332.56秒，两次grant、同run三次孩子attempt，主子6个attempt均done、无锁、三个准确宿主退出，7条wake全部handled。第二次grant在旧attempt DB结束后0.328秒、executor退出前0.556秒、session退出前0.593秒；同run随后第三attempt真实执行，**已命中217的原始接续交错且未永久PENDING**。公开final为初始回执加唯一capability_lifecycle_completion，能力事件下的完成交付实际命中，未出现重复完成回复。
- TUI227业务限制：父级实际校验并修正孩子错误平方和、补全哈希，最终300行与全部摘要正确；但自写检查程序在发现summary不一致时仍输出“全部校验通过”，后由模型读取差异修正。孩子再申请不存在的shell工具形成GAP／BLOCKED，完整“孩子交付两文件后父级复核”流程未达成。父级提出cancel_subagents，经原生界面只批准一次；回执为preserved既有BLOCKED、原执行权已done、无活资源，不能说产生了新CANCELLED状态。

三路终态后均通过原生/exit退出，准确客户端与孩子宿主不存在；只停本轮只读观察进程，共享Gateway保留。第7.9精确时序由227证实，第7.10两个因果红转绿用例和本轮真实无重复结果共同支持当前框架收口；本轮未证明必然撞上220相同的瞬时旧快照交错。旧220／217失败证据和所有业务失败不改报成功。第7步既有长任务、递归、取消／隔离、恢复矩阵继续见后文；本次修复没有改变Compact／长任务执行机制，不用重复空跑时长替代针对性验证。

## 第 7.7 步前台自然退出清理（默认双机运行路径已核对）

前台自然退出修复已集成为 `9330ee385`：从新 Popen 冻结出生身份，内核退出探测不回收组长，沿原终止链清理可验证后代后再获取原退出码；清理未知单列，不扩大后台／PTY 归属。
12 项新增定向覆盖包含 macOS 实际进程、Linux `/proc` 替身、身份拒绝及清理结果；联合 8 文件 215 项通过。旧 stdin 断言改为所有进程均关闭宿主输入，未降低隔离要求。
本地候选与主线均已过 Ruff、文档、严格尺寸、diff 和打包边界；`ed20fd061` 已推送远端 main，同一 wheel 双机各 1,274 文件一致，默认入口和唯一 Gateway 已切换并保留回滚。测试机发布脚本在切换后因清单哈希字段名差异未写完报告，已只读核对新进程、旧进程退出及包哈希后补齐私有报告，没有再次重启。TUI210／213 已核对本机直接回收与原退出码，214 核对 Linux 默认沙箱的直接非零退出和精确后代退出，215 正常三孩子并行与产物正确。211 未覆盖到位和212工作区准备失误单列；Windows 沿原路径不作新增回收承诺。线上未查到本提交的 CI 运行，验收来源为本地严格 gate。

## 第 7.7 步新版原生复验（按实际运行路径和缺口分账）

- TUI210 官方 MiniMax-M2.7 本机 329.8 秒自然结束，四组合重复完成的原工具账分别确认 TERM／TERM→KILL 后代回收，外部只读检查在最后一组孩子的 120 秒自然寿命前确认 PID 已消失；100 行平方逐项正确，合计 338350，原 attempt done、无锁。场景命令末尾 echo 覆盖了工具退出码，不能证明工具直接收到 7；模型夹具错误、两次真实超时和一次未执行的删除拒绝均保留。
- TUI213 独立原生任务 86.0 秒自然结束：模型先加 echo 后自行去掉，实际前台原命令 `COMMAND_FAILED`／return_code=7；原清理回执 `SIGTERM->SIGKILL`、confirmed=true、observed_processes=2、unresolved_pids=[]。原生后续只读检查返回 ESRCH，再次前台计算写出 20100；attempt done、无锁，测试者没有执行或修补业务脚本。
- TUI214 官方 MiniMax-M2.7 Linux 默认 bwrap 任务 141.0 秒自然 done：第三次前台工具真实 return_code=7／COMMAND_FAILED、stderr 空；原回执 SIGTERM、observed_processes=1、confirmed=true。宿主只读采样核对本次孩子的出生标识、NSpid 和两条私有输出管道，其在 120 秒自然寿命前消失；26 个已采样实例最终均无、原锁清零，后续计算 20100 正确。该回执只证明 bwrap 根快照，不能外推宿主直接对子进程 TERM→KILL；前两次 FD 错误后模型沿用了旧 ready，本次忽略 TERM 就绪未验证。模型把另一命名空间 ps 的 PID 当孤儿的报告错误保留。
- TUI215 在正确的 owner 内部工作区完成独立三孩子对照：143.2 秒自然完成，三个孩子实际重叠 31.6 秒、主子共五个 attempt 均 done；900 行逐项正确，number 合计 405450、square 合计 243405150、SHA256 与 summary.json 一致。三条直属通知 published 并 handled，无 closeout WAL／锁，已知执行宿主 PID 已消失。初次重复表头由模型在同一任务内自行发现并修正，测试者没有补产物；与仍运行的 212 时间重叠。
- 两次报告的自动系统回收归因没有证据。原生 IR 读回进一步确认：模型收到 stdout／stderr／return_code，但没有收到原账 `process.termination`；结构化清理事实的模型可见投影缺口留给第 8 步，不把原账成功冒充报告正确。
- TUI211 Linux 480.3 秒自然结束，初始夹具和后续模型改写偏离私有管道／就绪要求，不能算完整自然退出复验；四次最终外层退出码均为 0，子 PID 属不同 bwrap 命名空间，跨调用检查不能证明同一实例。初次真实超时的宿主六个已知 PID 已消失，正常平方和 333383335000 正确；TUI214 另开独立直接非零场景。TUI212 已运行长任务与 Compact，但本轮测试者把外部 cwd 选在默认 owner 墙外，孩子相对路径触发 PATH_OWNER_SCOPE_BLOCKED；按 owner_access 合同这是测试准备失误，不放宽权限，也不算正常三孩子对照通过。原任务后续自行整合并修正样例，正常三孩子验证由独立 TUI215 补齐。212 的 BLOCKED 运行账保留已确认符合既有合同；公开 final 被抑制另列7.8，不提前勾第 7 步。

默认运行路径的验收结论：macOS 直接后代回收／TERM 升级和原 0／7 已实证；Linux 默认沙箱直接非零及本次后代退出已实证，不能归因于同一升级路径。Linux 非 PID 隔离／Full Access 与本次 Linux 忽略 TERM 就绪仍未实证，不为取得指定信号回执放宽默认沙箱。

## 第 7.8 步能力请求唤醒后的完成交付（已发布双机，原生复验中）

TUI212 的工作区准备失误与后续框架现象分开：原生历史已有模型完整 final、turn_end_reason=completed，ConversationTaskLink 已 completed，最后后台 claim finished；公开 assistant final 未落账。交付裁决对 capability_request_open／granted 原因无条件 suppress，初步定位为旧唤醒原因覆盖新的完成事实。原生 PTY 的确返回 exit_code=0／36 passed；最终三个样例各400行，合计1200行平方均已被模型修正正确。所有 attempt 已结束，最后 claim finished、无本线程排队唤醒；一个孩子因 thread_goal_blocked 保留 AgentRun created，整树 TaskRun 因此未闭，这是既有 BLOCKED 保留合同，不是仍活执行或另一结算 bug。只修公开完成交付，不强制改运行账终态，不补写旧212回复。最小候选979ed5c76已集成为fc17def5f。新增状态矩阵、同片孩子BLOCKED、空载荷和canonical重放共16项：旧实现6红，修复后全绿。候选组合后台运行／Gateway控制／终态检查三文件348项通过，严格gate通过；主线相同三文件348项通过，Ruff、doc sync、strict-size（hard=0、blocked=False）、diff、clean-package全部通过；已随6c5fe6f36推送main，同包930f59f6双机各1,274文件一致，默认入口与唯一Gateway同版；216／217／218从原生TUI选官方M2.7进行实际复验，未提前计为通过。

第 7.8 新版真实验收（216／218 完成，219 未命中，217 接续停滞）：

- TUI216 本机206.96秒：真实只读派工，孩子正式OPEN申请、父级grant，同一孩子第二attempt完成；300行平方／立方逐行正确，摘要和SHA256一致。父级实际独立工具核验并公开final；主子5个attempt全done、无锁、两次准确宿主均退出。最终reason为subagent_runner_finished／root_subagents_terminal，证明授权链与普通交付，**不计为能力事件完成分支命中**。
- TUI218 本机140.92秒：三个孩子实际重叠30.88秒；初次合并表头错误由被测对象自行发现修复，600行立方、数字和180300、立方和32508090000及SHA256均正确。主子5个attempt全done、无锁、三个配对通知、准确宿主退出；公开final可见，216同期继续。测试者没有补业务产物。
- TUI216 能力OPEN工作片确实调用父级grant，但同一background request没有公开final；孩子后续完成才由runner_finished交付。未完成能力事件保持静默的原生负向分支有证据，completed能力分支仍缺实际命中。
- TUI219 本机125.53秒自然结束：200行平方与汇总正确，父子3个attempt全done、无锁、准确宿主退出。孩子实际未调用capability_request，canonical申请／grant均为空，状态DONE；父级最终报告和README却声称正式申请、孩子等待授权。此为场景未命中与模型报告错误，不能计修复分支通过，测试者不补申请、不改报告。
- TUI217 测试机正式只读授权场景停滞，具体事实与后续归属见7.9；保留原TUI和状态，不额外提示、不人工恢复。

## 新版 TUI220—224：授权接续与能力事件最终交付

安装源码67bb7817e、wheel83975aff，普通测试均由原生TUI一次中文需求发起，实际会话选择官方MiniMax-M2.7；日常默认模型未改。原始模型、请求、工具、claim、产物与进程证据留仓库外。

- TUI223本机87.24秒：三个孩子原DB执行区间重叠21.08秒，主子5个attempt均done，资源锁为零，三条完成通知各发布并消费；原共享孩子宿主退出。模型自行修正合并脚本错误，最终600行编号及立方逐项正确，数字和180300、立方和32508090000，摘要哈希一致。完成后的公开final走原root_subagents_terminal。
- TUI222本机148.68秒：真实孩子capability_request保持OPEN且无grant，父级接手生成240行数据，实际逐行验证工具已执行，外部只读核对数据、汇总和哈希一致。父子2个attempt均done、无锁、孩子宿主退出；最终回复在原前台轮完成，因此不计后台能力事件分支命中。
- TUI224本机122.96秒：父级先回复已派出并结束前台轮，原capability_request_open唤醒后台处理；同一后台工作片完成后公开final带capability_lifecycle_completion。原任务done、3个attempt均done、无锁，孩子仍BLOCKED，正式申请OPEN／grant为空，准确宿主退出。两条公开final分别为派出确认与最终交付，无同一后台请求重复final。第7.8所修正的真实正向分支通过，未人工改wake、任务状态或旧回复。
- TUI224业务核验另列：外部只读检查240行平方立方均正确、CSV哈希与摘要一致；但被测对象实际只运行行数、首尾／中间抽样和哈希，没有执行需求中的逐行核验及汇总合计。不能用观察者的完整检查补算模型已履约，不把框架交付成功记成完整业务验收通过。
- TUI220本机215.42秒：正式授权后同一孩子第二attempt实际执行，5个父子attempt均done、无锁，300行正确，父级自行修正孩子错误平方和及缺失哈希。grant在旧session退出后约0.18秒，故正常接续成立，准确旧片退出交错未命中。原生历史另见先由subagent_non_success_terminal交付，再由root_subagents_terminal回复上一轮已完成；只读已确认：旧BLOCKED通知先在前台handled，后被后台旧pending快照再次选中；此时孩子已DONE，旧非成功分支直接公开final，新DONE通知后续又公开一次。该通用通知缺陷归7.10，不提前计完全通过。第二attempt为Gateway内进程执行，不能因共享Gateway仍活就判资源泄漏或停止服务。
- TUI221测试机同题376.47秒自然完成，同一孩子两次正式申请／授权、三轮attempt，最终DONE；300行、数字和45150、平方和9045050、立方和2038522500及哈希正确，父模型自行修正孩子错误平方和并执行16项复核。7条wake均handled、无锁／WAL、三个准确宿主退出。两次授权分别在旧session退出约13.28／11.48秒后，准确交错仍未命中。公开final恰两条：前台等待确认和唯一后台root_subagents_terminal最终交付，没有220的重复后台完成回复。

## 第 7.10 步旧唤醒快照与重复最终回复（已发布并完成当前框架复验）

基线67bb7817e的两个独立因果用例先红后绿：预扫期间前台已handled的旧BLOCKED信封不得再开模型轮；当前来源已DONE且另有未读DONE信封时，旧BLOCKED不得绕过原完成邮箱判断直接公开回复。当前实现仅在原队列读取时机、claim后的窄来源准入及当前子树交付裁决收口，不新建执行器或持久状态。批次中真实失败、来源读取错误、控制、冻结重投及第7.8能力完成分支已进入六文件304项通过的组合。集成前发现资源停止夹具遗漏新显式依赖，实测1失败／13通过；补齐夹具后14项通过并纳入304项，不增加生产默认旁路。候选6c63e8cfd／9afe0417e已集成为f7742dcf0／965f7cf86，主线额外授权、runner收尾和持久交付三文件61项通过。自动回归不能代替最终验收；后续新版TUI225—227事实见本文件首节。

部署准备只清理测试机两份确认无进程或默认入口引用的旧安装环境；当前环境、上一版回滚环境、仍被旧客户端引用的环境和全部测试原账保留。私有切换脚本等待旧进程退出时改核对出生身份与进程状态，避免退出过程中command变化被误当PID换代；启动前还须确认原端口无监听。准备完成后已切换c3f1242f双机默认Gateway，保留83975aff回滚及切换前真实队列／运行账副本。

## 第 7.9 步授权与旧工作片结束之间的接续（修复已部署，原生复验中）

TUI217 原生一次需求真实派read_only孩子；孩子OPEN能力申请，父级grant准确delivery写入目录。
原grant回执为continuation=already_running／fresh_runner_session；随后旧孩子attempt结束BLOCKED，canonical被写为PENDING。
只读核对时父2个attempt、孩子1个attempt均done，没有执行中工具或资源锁；准确宿主已退出。
OPEN、grant和BLOCKED finished三条wake均handled，原后台claim finished、无pending wake，但父任务仍active且未创建孩子第二attempt。
这是实际授权接续未完成，不能因所有当前attempt结束或没有锁而记为任务通过；也不能用模型“启动中”证明执行器仍活着。
冻结快照377秒后再次读回，原状态、attempt和wake均未变化；准确TUI客户端保留，原宿主不存在。
源码交错定位：grant进入时旧工作片已生成BLOCKED结果，canonical归约为已授PENDING，但结束通知用旧结果把child link写BLOCKED，worker又按旧结果跳过接续；原生命周期门因此持续HOLD。独立owner在隔离目录先做确定性交错红灯，再修通知投影、当前状态接续和唤醒窄字段持久化，不通过放宽启动门掩盖失配。
原账已私下归档，冻结和五分钟后复核阶段未人工改状态或重发业务请求。定位不再依赖活现场后，测试者通过原生/stop结束旧217：父link=interrupted，孩子仍PENDING/link BLOCKED，原attempt全部done、无锁；随后原生/exit，准确客户端与旧宿主都不存在。这是失败测试的控制收尾，不是修复通过或孩子已取消的证明。候选634d12daf已集成为911a53245；三个独立旧红灯转绿，17个交错用例通过，9文件组合236通过／1项既有Linux /proc跳过，严格gate通过。真实RuntimeDB合同确认旧done经原派工入口登记同run第二attempt，重放不多开；父任务interrupted／cancelled均禁止新登记。主线授权／worker／后台交付三文件组合通过，严格gate通过；源码随67bb7817e推送main；同一wheel（83975aff）双机各1,274包文件逐项一致，默认入口和Gateway同版。TUI220／221复验授权续跑，222覆盖前台接手，223为三孩子并行对照，224命中后台能力完成交付；当前结果及缺口见上节，不进入第8步。

部署操作证据纠正：旧私有切换脚本把本机conversation存储key用于远端，远端930f59f6的切换前wake归档实际为空。数据库与旧运行环境备份仍保留；补存的是测试后的当前wake快照，不能冒充切换前备份或覆盖新任务结果。后续切换脚本按各主机真实canonical目录发现claim与wake，禁止跨机复用目录key；这是部署脚本缺陷，不计产品框架失败或通过。

## 第 7 步真实孩子失败及父级接手 TUI207—209

TUI207 先用独立原生模型配置经私有 loopback 夹具透传到官方 MiniMax-M2.7，主子 11 条请求均返回 200，未注入；60.6 秒自然完成，120 行立方数据与摘要／哈希正确，三个 attempt 均 done。
夹具正式入口固定官方上游，正文和认证透传，只按宿主稳定会话哈希及一次性 nonce 选中请求；测试会话头不发上游，原始内容／凭据不入日志，不更改系统网络或日常模型默认。
TUI208 的观察脚本误取线程最后一个关联任务，没有通过主角色定位根运行，故一直安全透传；两孩子和主任务完成、1,600 行正确，但没有故障注入，不计失败场景通过。
修正测试观察器后，TUI209 精确选中已有真实官方业务响应的一个运行中 child attempt，只注入一次 HTTP 400；该孩子原账 FAILED，另一孩子 DONE，未将故障扩散到父级或其它会话。
父代理收到原失败通知后自行运行孩子已写的脚本、补齐缺失产物并合并；99.9 秒自然结束，四个 attempt（含一个 failed）终态明确，失败孩子没有被后台重跑。
两条直属观察及两个 v2 published／retain_handled 回执均各一条、wake 均 handled，canonical 无收尾 WAL、作用域内无锁，共用孩子宿主已退出。
最终 1,600 行连续数字和平方逐行正确，数字和 1,280,800、平方和 1,366,613,600，summary 与文件 SHA256 一致。观察者只读核对，不执行或修补业务文件。
成功透传与一次拒绝证据分别保存；HTTP 400 故障不证明断网、任意进程崩溃或所有供应商重试。夹具已恢复 relay，后续停用只关闭本测试夹具。

## 第 7 步三子代理长任务 TUI202（生命周期完成，业务未通过）

同一新版测试机 Gateway 上持续 43 分 11 秒，三个孩子实际同时运行 213.2 秒，主任务及三孩子均 done；主代理和测试孩子各提交一次 Compact，随后继续执行至自然结束。
三条 exact attempt 完成通知均 handled、v2 发布回执均 published，三个 canonical 无收尾 WAL，作用域无锁／root claim，已知共用 dispatch 宿主退出；不外推为任意后代进程都已扫描。
原生工具账有孩子 105 passed、主代理 115 passed／10 warnings；10 个 smoke 函数以 return bool 代替 assert，pytest 明确警告，不能把该 10 项算成有效断言。
后续复合验证先输出 115 passed，随后 IndexError 使命令退出 1；另三项函数检查退出 0，原失败保留。
最终源码只读审阅发现 CLI 缺少可调用入口、文件分支全量物化、时区重标和符号错误、stdin 计数快照时机错误；报告 21 份样例中 19 项行数与磁盘不符。
因此自然长任务、Compact 和子代理交接按实测范围通过，完整业务交付未通过；没有补发修复提示、代跑程序／测试或补产物。

## 第 7 步本机核心长任务 TUI204（运行完成，交付未全通过）

同一新版 Gateway 下原生官方模型任务持续 1,888.1 秒，主 TaskRun／AgentRun／唯一 attempt 均 done，期间 205 停止另一子树、206 制造发布故障未中断该 attempt。
原生历史完整保存 62 项 pytest 输出（62 passed，2.68 秒）及三个示例输出；最终文件确有 62 个测试函数。命令使用了管道，因此管道退出 0 本身不能代替 pytest 输出证据。
观察者只读源码与原账，未导入或执行被测程序／测试。最终源码静态分支追踪发现相减在右侧集合先耗尽后漏掉左侧剩余区间，另有布尔边界被当整数、双集合字典分支未解包等问题；自写测试通过不证明完整交付。
例如左侧 `[1,5), [10,15)` 减去 `[2,3)`，源码尾部只追加当前余段，遗漏 `[10,15)`。这是一项静态审阅结论，不冒充额外真实 TUI 执行。
终态后无该 attempt 的资源锁，但两个早期前台测试的嵌套 Python 子进程仍存活并占 CPU：一条业务脚本仅杀父进程后返回 0，另一条 communicate 超时未清孩子后返回 1。它们不是宿主 Shell 超时分支，也不是后续 PTY。
原生 `/stop` 返回没有运行内容，两进程仍存活；普通退出路径未清理前台进程组后代，作为生产框架缺口继续修复。私有证据保留出生时刻、命令、cwd、进程组和原工具账，历史父子关系没有直接持久记录，归属是多项事实强关联。
取证后仅向重新核对出生时刻／命令／cwd 的两个准确 PID 发 SIGTERM，确认退出；这是人工测试资源清理，不能算产品停止通过。
本机曾被独立决策模型测试夹具误写共享配置，造成随后新 TUI207 的模型菜单失败；按原迁移源码核对后精确反迁移并隔离测试项，已由实际安装版重新读取成功。未重启 Gateway、未修改日常默认；该操作是测试环境修复，不是产品验收通过。

## 第 7 步原生 TUI206 发布失败与自动恢复（通过）

新版 TUI206 在官方模型原生单孩子任务中命中受控发布失败：只在新 thread 原本不存在的观察 JSONL 路径建立空目录，未替换既有记录或业务产物。
孩子完成后留有 v2 prepared 与 canonical runtime_closeout_pending；保存证据后仅 rmdir 撤掉同一空目录，没有调用恢复 API、追加业务提示、重启 Gateway 或手动改状态。
原 sweep 自动将同一 signal ID 补成 published，只有一条 handled 通知及一条配对观察，WAL 清除；孩子仅一个 attempt，父级自然续接，三个 attempt 和 TaskRun 均 done。
最终 squares.csv 600 行、逐行数字与平方正确，数字和 180,300、平方和 72,180,100，summary 一致；无资源锁，原孩子宿主退出，普通对照 TUI204 同期继续。
该用例证明实际发布失败后的幂等补齐和父级交接，不冒充孩子 FAILED、硬断电、任意 JSONL 字节损坏或进程强杀恢复；初次失败时信号可能已安装，不能说通知从未可见。
观察者只读核验数据和运行账，除上述精确空目录故障夹具外未写被测数据；206 已正常退出客户端。


## 第 7 步新版递归协作 TUI203（已核对）

新版 TUI203 递归协作已独立核账通过：请求到 TaskRun 关闭 259.9 秒；主代理、协调代理和两孙代理共 6 个 attempt 全部 done，孙代理实际重叠 19.3 秒。
两分片各 1,200 行，合并 2,400 行、单表头、数字连续无遗漏重复，逐行平方、数字和 2,881,200、平方和 4,610,880,400 及三份 CSV 哈希全部正确。
主代理另有两次真实工具核验且退出 0。协调中途曾出现重复表头、命令退出 1／141，由被测对象自行修正，观察者没有补文件。
直属协调仅发布一条 exact attempt 的 handled wake，v2 回执 published、retain_handled=true，配对观察一条；孙代理没有越级通知根会话。
本树 canonical 全 DONE、无 WAL／锁／root claim，四个子代理宿主均有 exited 记录且准确 PID 已消失。
这项仅覆盖新版递归自然交接与任务交付，不覆盖发布半写、进程重启或长任务 Compact。


## 第 7 步新版双机部署与原生 TUI（进行中）

第 7 步组合源码 `a067baddd` 已构建同一 non-editable wheel（SHA256 前缀 `e36a9ee2`），
本机与测试机各 1,274 个安装文件逐项一致，默认入口和各自唯一 Gateway 已正常切换；旧环境及切换前记录保留。
新版使用 v2 唤醒发布记录后，不能直接换回旧写入进程或用快照覆盖新结果；回退须先协调数据格式和新任务事实。
本机 TUI204／205、测试机 TUI202／203 均从原生界面选择官方 MiniMax-M2.7，实际 provider/base 与会话绑定已私下读回；
本机另通过原生模型菜单新增本轮独立官方地址配置，日常默认和原配置不变。两端均已收到真实模型响应。
当前 202 项目长任务、204 普通对照继续运行；203 递归产物、生命周期和资源退出已核对通过；205 原生停止后 152.3 秒复核：两孩子仍取消、无新 attempt／锁，原宿主已退出，204 继续运行、Gateway 未变；205 已原生退出并保留画面。
本轮远端查询仍超时，最新代码未推送，线上 CI 未作为验收来源；不能把部署冒充第 7 步完整验收。


## 第 7 步恢复与父通知组合验证（本地）

第 7 步恢复扫描与父终态通知已组合到原 checkout：原 sweep 绑定同一窄通知器，恢复／清账故障测试迁到类方法，旧全局接口调用已清零。
27 个相关测试文件联合 **665 passed、2 项既有 skipped**（55.68 秒），覆盖发布恢复、父子交接、结果提交、调度、控制和资源停止；没有把两条独立测试计数相加。
Ruff、导入边界、doc sync、严格尺寸、diff、clean-package 已通过；此组合随后已双机部署，最新代码远端待推送，线上 CI 未作为验收来源。
本片不改变原 WAL→运行结算→通知→delivered→清账顺序；未扩改阶段提醒或能力申请。后续只做发布及本版本原生 TUI 验收。


父终态通知候选：直属父级／服务窗口／活动提醒 56 项，加外部代理控制／资源停止／结果状态 74 项通过，
共 6 个直接受影响文件 130 passed。待恢复扫描片组合后迁移其通知装配及故障替身，不能单独发布，
不以本地合同回归代替新版真实 TUI。

第 7 步通知配对修复与初次提交依赖收窄已合入原 checkout。18 个组合定向文件 **391 passed、2 项既有 skipped**，
包括原两项半写红灯、配对恢复矩阵、无 manager 提交的顺序与各写点失败、分页恢复、父级交接、
Goal／观察路由、结果状态、调试 trace、运行守卫、owner 唤醒发现、后台读回和调度。
这轮只证明本地组合源码；尚未发布／部署，TUI199—201 属于前一运行包，不能替代新版本验收。
本地严格 gate 已通过：相关 focused、Ruff、doc sync、strict code-size、diff、clean-package 均无阻塞；
新增行隐私模式扫描未命中。未运行全仓 pytest，线上 CI 没有作为验收来源；远端查询本轮超时。

### 恢复扫描显式依赖（独立本地候选）

基线 `9bbfc0a01` 上继续收窄 `restore/_restore/advance/recover`，唯一生产装配仍在原 capability sweep；
既有分页、半写、旧 attempt、通知与清账故障用例逐一迁移到显式依赖，不重跑业务。
新增 9 项无 manager 的恢复合同用例，覆盖 `repo=None`、精确通知负载、已交付只清账、坏结果不挡后项、
先 WAL 后事件消费及消费标记失败后重入；另有 1 项从原监督入口推进真实临时文件 WAL 的装配回归。
10 个直接相关文件联合 **291 passed、1 项既有 skipped**；Ruff、导入边界、doc sync、strict code-size、
diff、clean-package 通过，新增行隐私扫描无命中。命令与未覆盖范围见[本片交接](docs/tasks/HANDOFF_STEP7_CLOSEOUT_RECOVERY.md)。
前述 391 项组合结果属于基线，不冒充本片或并行父通知新实现的已发布验收。

## 第 7 步子代理结果链发布与验收

配对发布半写修复已在隔离线本地验收，尚未发布：`test_closeout_wake_receipt_half_write_does_not_duplicate`
两个参数用例在修前确定性得到 2 条通知而非 1 条；修后在 wake 安装后的最终回执原子替换处故障注入，
pending／handled 两种重试均保留一条 wake 和完整原观察。新顺序先预留再安装，未删原数量断言。
原已部署包不受本地修复影响；完整依赖收窄与本版本发布验收仍属第 7 步。

本片 11 个直接相关测试文件 **261 passed、1 项既有 skip**；其中新文件有 32 个通过用例：
8 个文件边界故障 × 是否消费、同键并发与重放、普通 Goal handled 后继续、旧 v1 纯读及显式迁移、
坏账保留、原投递冻结和无 key 失败不得伪造无链观察。查询还核对文件集合不变和发布中只读 prepared 快照。
`test_wake_publication_recovery.py` 之外，同跑 `test_dispatch_liveness_and_revive.py`、
`test_conversation_wake_events.py`、`test_closeout_recovery_paging.py`、`test_direct_parent_lifecycle.py`、
`test_subagent_runner_result_state.py`、`test_service_window_semantics.py`、`test_conversation_store.py`、
`test_conversation_goal_tools.py`、`test_goal_lifecycle_recovery.py`、`test_observation_route.py`。
具体命令、严格 gate 与未覆盖项见[配对发布交接](docs/tasks/HANDOFF_STEP7_WAKE_PUBLICATION.md)。
本片不做宿主自动恢复扫描；prepared 成功后仍需调用方重试，runner 复用原 WAL；无 key 不承诺重试幂等。
离线故障回归不等于真实 TUI、硬断电或所有通知入口自动恢复，线上 CI 未作为验收来源。
## 自然长任务结束与稳定历史缓存补充

官网 M2.7 的 fd→Python 真实任务已自然结束：canonical TaskRun/主代理/三个子代理均 done，
持续 6572.58 秒（约 109.5 分钟），自然 Compact 三次，末段 TUI 显示 348 个逻辑模型轮。
任务结束前 1180 次采样未见未同步或观察错误，状态 P95/最大 43.47/1770.05 ms；
1 CPU/2 GiB cgroup 峰值约 1425.78 MiB（包含文件缓存和多端对照），memory failcnt=0。
这一长任务跨越几个候选客户端版本，不能写成最后缓存补充片已单独运行满 110 分钟。

- 模型独立生成 Python 项目；原工具账保留完整 pytest 输出，24/24 passed、return_code=0。
  上游固定版本、源码和双许可证实际存在。这里只确认真实开发/测试/修复过程和自测结果，
  未宣称全部上游行为等价；当前自测没有覆盖所有 ignore/exec 等需求，已有工具失败记录也不改写。
- 长历史中发现额外显示成本：相同版本、同任务的累计/新恢复 TUI 同窗 CPU 26.49%/17.36%，
  采样定位到重复快照、全历史版本键和稳定行重组。本补充片复用发布快照、稳定/活动版本键、
  有界静态前缀及已净化行组；活动块与插话仍按同一事件顺序合流，不删历史、不再降低帧率。
- 最终对照先在任务结束后的同一历史恢复两个新 TUI，确认画面逐行相同，再经原端提交一次普通中文续聊需求。
  官网 M2.7 实际执行 `sleep 30` 并回答十七加二十五等于四十二；三个端都收到唯一完整回复。
  两个观察端同时采样 40.19 秒：基线/候选 CPU 4.68%/2.21%（约降 52.7%），RSS 101.19/100.87 MiB；
  Gateway 同窗约 5.77%，状态查询无失败。这是等待与回复窗口的对照，不外推到所有活跃输出或内存降幅。
- 在最终原生 TUI 中从阅读处 Ctrl+O/Ctrl+E 进入第 2384/2386 段附近；上下键、上下滚轮、跨段翻页、
  100↔120 列 resize 与回到最新都通过。首次详细展开约 1205 ms，原文展开约 72 ms，后续六次滚动约 28—32 ms；
  包含 tmux 注入/捕获开销，首次展开仍有明显重排成本，不宣称所有操作都在几十毫秒内完成。
- 289 项显示/状态/输入/历史回归通过；覆盖缓存失效、插话顺序、同块替换、宽度变化、字符预算和终端过滤。
  最后四个生产文件 SHA256 与测试机一致。本次全部测试 TUI 已 `/exit` 且准确 PID 消失，Gateway 未重启、running attempts=0。
  清理后 20 秒采样：Gateway CPU 2.43%、RSS 297.08 MiB，状态 P95 5.46 ms；cgroup 1117.76 MiB 包含文件缓存。

原始证据在私有 `fd-port/`：business-terminal/messages/thread、project-test-tool-evidence、port-artifact、
frame-cache-equal-history、business-observation-summary、cleanup-result 与 final-idle-summary。
其它阶段的对照记录保留，不能把历史不同或任务结束过渡窗口的数字替换成最终等历史对照。

## 真实开发中的 TUI 重绘对照

在同一官网 M2.7 的 fd→Python 真实任务上，只读恢复两个原生 TUI，未重复发送业务需求。
相同 120×36 终端、同一 1 CPU/2 GiB cgroup，两个客户端同时观察同一会话。
基线为 60 Hz 帧合并/8 Hz 周期动画，候选为 20 Hz/4 Hz；原始 TUI 和 Gateway 也仍在同一限制中。

- 30.78 秒同时采样：基线 TUI 单核 CPU 16.02%，候选 10.23%，下降约 36.1%；RSS 为 101.43/98.61 MiB。
  Gateway 同窗 CPU 12.93%、RSS 188.15 MiB，cgroup 峰值 1321.41 MiB，状态查询无失败。
  该结果来自真实压缩/开发阶段，不把降低帧率称为长期内存容量通过。
- Ctrl+O/Ctrl+E 在当时正在阅读的真实工具记录附近进入完整原文，第 125/128 段；两个视图内容一致。
  每端各 12 次上下翻页，均有可见移动；基线中位/最大 34.33/43.27 ms，候选 38.09/68.18 ms。
  测量包含 tmux 按键注入和画面捕获开销，不是人类屏幕刷新率测量。
- 候选原文上/下键和上下滚轮都实际移动，四次可见延迟 28—71 ms；100 列 resize 后可继续读取。
- 157 项 UI/输入/线程/reducer 定向测试通过。原始证据在私有 `fd-perf/` 的 samples、summary、
  key-latency、raw-events 和逐次终端帧。frames 合并只限制绘制，不丢消息、工具结果或思考记录。

## TUI 等待超时与迟到终态

并行真实测试发现原请求已经 done，而原 TUI 停在等待超时、只有重连才读到最终回复。
当前客户端补充片让 canonical terminal 优先于观察截止点，并保留同一请求和 chunk cursor 继续观察；
超时不创建新任务、也不把展示去重归属留给一个已放弃接收的 worker。无活动时轮询退避至 1 Hz，页面退出收口。
plain 等有限等待语义保持。Gateway client、TUI worker/threading、CLI parser 定向测试 **109 passed**。

官网 M2.7、专用测试机同一 Gateway 的真实对照：观察窗口设为 0.2 秒，普通中文要求实际等待三秒后回答。
基线服务端 11 秒后 done，原 TUI 始终停在等待超时，没有“十七”；候选同场景在原页显示工具过程和“十七”。
再只对测试客户端 SIGSTOP 9.28 秒，原任务在暂停期间完成，SIGCONT 后原页显示“四十二”，无重复提问或回复。
Gateway 全程同 PID，fd 开发任务与子代理没有停止；两条候选请求各只有一个 canonical request id。
原生 `/exit` 后测试客户端退出。私有证据在 `late-wait/` 的 baseline/fixed/paused terminal、TUI 与 timing 文件。

## 真实开发长任务验收方法

用户明确要求长对话验收使用真实项目开发过程，禁止把重复生成的大行数当作真实任务通过依据。
当前选择让官网 MiniMax-M2.7 的 my-agent 在原生 TUI 中把 GitHub `sharkdp/fd` 从 Rust 复刻为 Python，
自行读源码、实现、运行测试、修复并提交项目产物。测试者只提交一次普通中文需求并观察，不能代写或补交产物。
记录自然产生的模型/工具回合、Compact、TUI 状态、CPU/RSS、退出/恢复与任务结果；未实际发生的长历史边界不计为通过。
下文合成一万/千万行记录只作为存储边界和缺陷复现，不代表此类真实开发工作负载。
真实任务已自行获取源码、派出并收回三个子代理，自然触发 Compact generation=1 后继续开发；
当时仍在兼容性测试和修复；最终自然结束及验收边界见本页顶部。HTTP 请求交接为 done 不代表后台业务任务结束，
监控须继续看原 run 的 canonical attempts 和任务事实，不能据此停止 Gateway。
截至本次阶段记录，真实需求已运行约 57 分钟、168 个模型轮；累计 552 次只读采样未见未同步或采样错误，
状态查询中位/P95/最大约 4.23/28.49/1770.05 ms，cgroup 峰值约 1326 MiB（含文件缓存与对照客户端）。
原始 TUI 与 A 对照端经 `/exit` 正常退出，准确进程消失；B 端继续观察同一后台任务，Gateway 未重启。
保留原工具和项目失败记录，模型仍自行修复兼容性，不能把此阶段记录称为 fd 项目通过或任意时长保证。
后续已沿同一任务完成采样并核对原账与产物；原始证据在私有 `fd-port/`，不再补合成长行数充当真实任务。

## 官网真模型与TUI媒体验收

2026-09-23，独立候选线，专用测试机限制为 1 CPU / 2 GiB；官网直连，不通过中转，不使用假模型作为本轮验收。

- 官网 MiniMax-M2.7：100 独立用户身份各发送一次中文普通请求，100/100 terminal=done；处理槽峰值 50。
  同时一个原生 TUI 发问并正确回答 `5+6=11`。204.3 秒完成队列，44 个实拍终端帧未见“未同步/刷新失败”。
  cgroup 峰值 1047.1 MiB（含文件缓存），末次采样 Gateway RSS 313.8 MiB、TUI RSS 62.6 MiB。
  真实 `/status` 全部成功，但高峰 P95 3268 ms、最大 4897 ms；单核批量冷启动仍有排队和刷新延迟。
- 官网 MiniMax-M3：实际端点 `https://api.minimax.cn/anthropic/v1/messages`，现有私有 key 短请求确认返回 M3。
  原生 TUI `/attach` 添加 PNG，正确识别红圆、蓝方、绿三角及 `Q7N4`；终端 bracketed paste 拖入 MP4，
  正确识别红→蓝→绿和 1→2→3。测试提问未提供答案；未代模型执行视觉工具。
- 私有只读请求观察器确认真正外发 image/png 6484 字节与 video/mp4 5774 字节，SHA256 与素材一致；
  观察器调用原 HTTP 函数，不替换供应商、不改变请求/响应。模型工具轮为零。
- 真正 `/exit` 后重新启动同一会话，问图片和视频背景，M3 正确回答白色；请求再次带相同原件字节。
- macOS 隔离 Gateway + 原生 TUI，系统图片剪贴板经 Ctrl+V 成为附件，官网 M3 正确识别同图；原剪贴板完整恢复，
  本机隔离测试 TUI/Gateway 已退出。无改动用户日常模型/默认 Gateway。
- `input_media_max_bytes=16 MiB` 同时限制新输入和一次供应商请求的媒体展开；新近附件完整、超预算旧附件明确
  投影为归档引用，canonical refs 和原件不删除。owner 越界、符号链接、同长度内容变更、总量/数量超限均有合同验证。
- 相关组件矩阵当前为 1008 passed、1 skipped；跳过项仍为原 HTTP stop fixture 的 409，自行 skip 不计入通过。
  单测只验证协议/资源/输入边界，真实可用结论来自上述官网模型与原生 TUI。
- 无 checkpoint 的一万行历史：真实 TUI 续聊完成，自动压缩 generation=1 后正确回答 `4+4=8`。
  终态用时 410.81 秒；账本记录官网 M2.7 的 4 次供应商调用均 finished、0 retry，输入 294653 / 输出 1621 token。
  该用时不能算低延迟通过，也不能仅凭单次采样栈归因给 Compact 二分预算估算。

**未通过边界**：千万行浏览成功不等于千万行任意状态续聊成功。对 10,000,000 行、约 2.43 GB、
无 Compact byte checkpoint 的历史，隔离只读子进程在 384 MiB 地址空间上限下调用 `after_compact_report`
立即产生 `MemoryError`，还没有发起模型请求。`append_once` 的全量去重读取也需后续治理。
相关有界读取、分批 Compact 必须与另一开发线正在修改的 scope/checkpoint/CAS 合同合并验收。
本轮不声称无限时长、任意历史规模、100 个重工具或真实 IM 平台账号已通过。

证据保存在仓库外 `tui-real-media-20260923/`：`real-model/` 的 submissions/terminals/samples，
`media-*-tui.txt`、`media-provider-requests.jsonl`、`real-10k-history-*`、`uncompacted-10m-read.json`、本机截图粘贴验收。
旧假模型记录仍保留用于定位，不作为本轮通过依据。未推送、未替换用户默认环境。

## TUI 资源与空闲用户验收

独立资源线，未替换用户默认 Gateway。专用测试机始终只有一个真实 Gateway，TUI 为真实 tmux
终端；cgroup v1 对 Gateway 与所有测试 TUI 合计限制 1 CPU、2 GiB、无交换。模型与合成规模证据分开。

- 同一份 3035 条保存历史、2097 块、11824 展示行的只读 A/B 回放：基线稳定重绘中位数约 38 ms，
  候选约 4 ms，终端控制字符过滤保留。这不是按键端到端延迟或跨机 CPU 对比。
- 真实生成并读取 1 万条和 1000 万条 canonical JSONL；后一文件 2.43 GB。最近页和连续旧页各 80 条。
  一千万行原文通过产品 writer 生成 39063 页，首尾索引为 0 / 9999999，读取单页约 0.5—1.0 ms（热缓存）。
- 原生 TUI 实际恢复千万条会话，Ctrl+O/Ctrl+E 保留当前消息位置；下键进入原文、滚轮继续向下、
  上键跨回消息、120→100 列 resize 均有终端文本证据；`/exit` 后准确 PID 消失。
- 该长历史 TUI 与 Gateway 的 20 秒采样：cgroup 峰值约 164 MiB，Gateway/TUI 分别约 1.14% / 1.00%
  单核 CPU；40 次 `/status` 无失败，中位 2.62 ms、P95 3.61 ms。
- 官方 MiniMax-M2.7、`anthropic_compatible`、官方 MiniMax Messages 端点的真实 TUI 已完成普通中文问答；
  私有密钥与完整配置不入仓。最终 20 个变更生产文件 SHA256 与测试机一致：真实执行 `sleep 15` 完成后恢复对话；
  另一 TUI 在真实 `sleep 60` 执行中 `/exit`，终端进程消失而 Gateway 仍 processing=1；
  新 TUI 恢复同一会话后收到“后台继续执行验证完成”，未重新发送需求。
- 100 个独立 local owner，经真实 HTTP 鉴权、原 durable queue、真实 agent 初始化，使用仓库外假
  Anthropic 服务分别发送一次普通中文消息：两轮有效压测各 100 个终态均为 done，处理峰值 50。
  后一轮另有一个原生 TUI 真实排队并收到假模型答复，60 秒/30 帧未见未同步；总模型任务 101。
  假模型故意等待首批 50 个并发再释放，因此总耗时不能当真实模型延迟。不是 100 个真实 IM 平台账号验收。
- 后一轮 cgroup 峰值约 673 MiB（包含前轮累计文件缓存），任务结束后抽样匿名驻留约 238 MiB、
  文件缓存约 442 MiB；不得将 cgroup 总数等同独占堆。第一有效轮后 Gateway RSS 约 194 MiB、
  空闲 CPU 约 2.04%，状态 P95 4.20 ms。CPU 满载时会饱和，执行槽上限不等于立即响应承诺。
- 压测发现策展关闭仍构造两个软实例、完成回收受 60 秒派发节流影响；已经修复全局关闭与及时回收，
  针对性测试通过；最终同机实例池从 0 按原 owner 重建、请求 done，再在空闲期归零。
  轻量身份登记可保留 64 条软事实，不等于存在 64 个执行槽或 TUI。
- 初次假模型没有实现原生工具能力探针，100 条均被框架拒绝，记录保留但不算有效聊天验收。
  合成夹具两次构造失误（时间字段/显示身份）也未计入通过，未为了夹具改变产品协议。

最终定向矩阵 818 passed、1 skipped；跳过项是原 HTTP stop 测试夹具返回 409，未计入通过。
Ruff、doc sync、strict code-size、diff check、clean-package 均通过；未推送远端，线上 CI 未作为验收来源。
真实 HTTP 另有 8 个半请求在约 5.13 秒全部关闭，期间 35 次健康查询无失败。
现有强制网络故障仍必须显示未同步；没有以隐藏错误、扩大超时、删历史或取消用户任务来制造成功。
未验证 100 小时持续运行、任意单条超大 JSON、100 个真实 IM 同时操作及 50 个重浏览器/扫描进程。

原始日志、配置、终端快照和测试驱动位于仓库外 `tui-scalability-20260923` 私有证据目录；
设计和参考源码边界见 [资源寿命](docs/design/TUI_RESOURCE_LIFETIME.md)。

## 第 7 步子代理结果链已发布 main（未切换运行环境）

独立工作树已拆出 runner 状态展示、完成交接信封、exact attempt 准入与
“结果先落盘、再 WAL、再运行账、最后父通知”的初次提交编排；旧函数和导出已删除。
上述源码现已应用到原 checkout，与并行的 TUI／历史修复同源运行定向回归；
组合源码又跑过 17 个跨线定向文件及 9 个恢复／资源／层级文件，均无失败。
Ruff、导入边界、doc sync、strict code-size、diff、clean-package 均通过。
组合 wheel 发布边界检查为 forbidden／source_missing／source_mismatched／resource_missing 全部 0；
SHA-256 为 `6b66d2a4f9dc3f3df8902bdbd4181c733a9c15dc224c474c2c5ca9d36b62ce71`，
四个新模块与 TUI 阅读模块均在包内。原生 TUI 验收仍待完成。
首次推送后复查发现同文件注释插入 import 组触发 Ruff I001；该版本未切换任何 Gateway，
本次移正注释并重新通过 focused tests、Ruff、导入边界、doc sync、strict code-size、
diff、clean-package 与修正 wheel 的发布边界，以上 SHA 只指修正包。
终态冲突原先漏记 `closeout_blocked`，现写入 manager 的原 RuntimeDB。
首次父通知后若 `delivered` 标记落盘失败，保留 pending WAL；
故障注入验证恢复后同一 attempt 的 wake 仍恰好一条。

10 个直接受影响文件 **229 passed、4 项既有 xfail、1 项既有 skip**；
另 7 个多层恢复、资源停止、登记产物和离线工作流文件 **68 passed、1 项既有 xfail**；
再补 5 个 worker pool、作用域、创建幂等和层级合同文件 **47 passed**。
Ruff、doc sync、strict code-size、diff、clean-package 通过。
仓库外构建的候选 wheel 含四个新模块，包内 `agent_py_agent/` 文件共 1,271 个。
组合源码与注释排序修正已推送远端 main（`c9042f5f2`）；测试机安装的 1,273 个包文件与修正 wheel 逐项一致。
原 Gateway 在 pending／processing 均为 0 时正常终止，确认旧 PID 和端口监听均消失后，
从新版运行环境启动唯一 Gateway，并将默认入口指向同一运行环境；旧运行环境及入口回滚副本保留。
本机仍有原任务处理，尚未切换；
这些离线回归不能替代官方 MiniMax-M2.7 的第 7.6 项多路真实 TUI 验收。
同一修正版 wheel 已在本机独立候选环境安装，1,273 个文件逐项一致；默认入口和 Gateway 未切换。
本机后续核对发现 HTTP processing=0 时，canonical RuntimeDB 仍有由原 Gateway 执行的最新 running attempt。
因此切换前必须同时核对后台执行轮，不能单靠前台请求队列判空闲；测试机首次切换前未独立保存
后台 attempt 快照，恢复观测后须补查原运行账，不能据当时队列为 0 宣称全部后台任务空闲。
隔离线交接见 [第 7 步交接](docs/tasks/HANDOFF_STEP7_SUBAGENT_LIFECYCLE.md)，
正式验收矩阵见 [唯一 TODO](docs/tasks/REFACTOR_PLUGIN_GOAL.md#第-7-步当前-todo子代理状态和交接按序小片推进)。

### TUI200 有效长任务自然收尾（准入收窄运行包）

官方 MiniMax-M2.7 的原生 TUI200 从单次需求持续工作 1,952.8 秒（约 32.5 分钟），
三个并行孩子均 DONE，主代理在所有孩子结束后自然完成；期间发生一次 Compact，之后继续读文件、
修复偏移和导入错误并运行测试。原生 run_command 回执为退出码 0、`62 passed in 5.87s`，
磁盘测试文件确有 62 个测试函数，源码／README／RESULT 已读回；观察者未执行或修补被测项目。
三份 canonical 子任务均无待处理 WAL 或 runner_last_error，三条精确完成通知各一条且已 handled，
全部五个执行轮的资源锁为零，子代理宿主进程已退出，共享 Gateway 保持原 PID。
本轮证明有效长任务、工具错误自修、多孩子交接和一次压缩后继续工作，不替代第 8 步完整 Compact 合同。
交付仍有缺证：DESIGN 只写上游版本标签，未读回可追踪提交号／来源核对证据；
测试通过不证明全部兼容性说明正确，不能把这一项写成完整业务交付通过。
199／200／201 均核对终态后从原生 `/exit` 正常退出，仅保留 tmux 死窗及仓库外证据；未停止 Gateway。
原页超时的 197 保留排查，通知半写修复仍待新包验收，本结果不能代替该故障修复。

### 准入依赖收窄（测试机已部署，本机与远端待同步）

结果准入与终态冲突诊断只需要原 RuntimeDB，现改为显式接收该依赖，不再传入整个 manager。
结果服务在原调用位置取出依赖；canonical task、exact attempt 裁决、None 文件模式和写诊断边界不变。
两个直接受影响测试文件通过，覆盖原冲突诊断、旧轮拒绝、一致终态重入和持久恢复。
Ruff、导入边界、doc sync、strict code-size、diff 和 clean-package 均通过，未运行全仓 pytest。
该片以 `8ca9889aa` 本地提交，发布前重新通过上述严格 gate；构建源码为 `3b5f9943e`，
wheel SHA-256 为 `c751ee272dc66193d957c1a7802f16b8b00e6caadfdb0d37bbb64c21e1e2db90`。
仓库的 distribution boundary 四类结果全为 0。临时自写的宽泛路径检查曾把合法内置素材和非发布脚本
误计为禁入／漏带，随后改用仓库已有发布合同核对，没有因此修改包或放宽发布规则。
测试机已逐项核对 1,273 个文件，保留旧运行环境和入口回滚副本后正常切换唯一 Gateway 及默认命令。
切换前同时保存 HTTP pending／processing 为 0 和 RuntimeDB attempt 快照：本日没有未结束的执行轮；
旧历史未结算行单列保存，没有擅自改写为终态。原 Gateway 退出、端口释放后才启动新版。
本机原 Gateway 仍有活动后台 attempt，未切换；GitHub 连接失败，最新提交尚未推送，不把本地 gate 视为线上 CI。

TUI199／200 从新版默认入口分别发起父子孙文件清单项目和三孩子并行的 Python 十六进制查看器项目。
每个任务只提交一次业务需求；199 另在明确等待孩子时插入一次进度询问，以验收等待时插话。
两条 canonical thread 绑定同一已核对的官方 MiniMax-M2.7 配置，后端为 anthropic_compatible，
服务商地址为 `https://api.minimaxi.com/anthropic`，且已有实际模型响应；未记录或公开密钥。
证据为会话绑定、私有配置白名单与原生响应，不冒充网络抓包。

199 已自然完成：原生历史包含 20 项 pytest 通过的工具输出，磁盘独立只读重算得到
26 个样例文件、2,397,671 字节、两组各三个重复内容文件，与原报告一致。
权威树只有一个协调孩子、两个孙代理；三个下级的终态／pending closeout／执行进程退出均核对，
每一级最终完成时间晚于它的孩子。根会话只有协调孩子的一条完成通知，且已 handled，
没有把孙代理的完成越级广播给根。等待时进度询问在原生历史出现一次，模型回答进度后继续原任务，
原树没有重复派工。此任务约六分钟，不算所需长任务；测试使用 pytest 而非全标准库、
个别断言覆盖较弱、最终报告对测试编写者有不一致归属，交付限制与框架通过分开记录。
200 仍在整合代码和修复实际测试，已持续超过十五分钟；最终交付和长任务完整验收尚未收口。
201 由原生 TUI 派出两名孩子，确认两个原 attempt 均运行后通过 `/stop` 停止当前任务。
两孩子的 run／attempt 和 canonical 状态均取消，runner session 为 cancelled、无 pending closeout，
共同执行宿主已退出。约 139 秒后复核没有新增 attempt，三个原执行轮的资源锁均为 0；
200 仍由同一 Gateway、同一后台 attempt 持续运行。201 根的前一已完成执行轮保留 done，
不能改写成该历史轮被取消；原 TUI 正确显示两个孩子已停止。
TUI195—198 仍只覆盖前一发布包，不用于证明准入收窄片通过。

### 第 7 步首组真实 TUI（基础交接已核对，完整矩阵未收口）

TUI195、196、197 分别测试单孩子交接、三个并行孩子合并和独立主任务对照。
三个客户端均由新版默认入口启动，通过原生模型菜单为各自会话选择官方 MiniMax-M2.7，
服务商端点为 `api.minimaxi.com/anthropic`，没有改变日常默认模型。每个 TUI 只提交一次中文需求；
首轮画面已观察到 195 的 create_subagents(items=1)、196 的 create_subagents(items=3)，
197 则独立准备写入数据，真实模型响应和终端流留在仓库外。

提交后的 SSH 观察命令等待约七分钟仍未返回，随后只终止该本地只读观察进程；
另一次有界连接在横幅交换阶段超时。TCP 端口可连接不证明业务就绪，SSH 超时也不证明任务已结束。
SSH 恢复后读取原生运行账、父子通知和磁盘产物，Gateway 保持原 PID，没有重启任务或补写产物。
195 的 240 行数据、196 的三个分片及 1,200 行合并结果、197 的 600 条记录和各自统计均逐条读回正确。
196 三个孩子真实共同执行约 32.6 秒；四个孩子的 canonical 状态均为 DONE、错误为空、
没有残留 pending closeout，runner session 均 completed。每个 exact attempt 各有一条完成去重回执，
四条对应 wake 均为 handled；父级随后接续完成，两个原执行宿主 PID 均已退出。
这证明本组正常收口和消费，不等于已经在真实 TUI 注入并验证重复通知、写盘失败或重启恢复。

195／196 原生页面显示最终结果；197 原生页面停在“gateway 请求等待超时”，
但原请求 terminal 为 done／ok，原任务和实际产物均已完成。保留为客户端最终结果展示缺口，
不能用业务成功抵消。TUI198 只通过原生 resume 重连 197 原会话，未发新业务需求，
已显示完整工具历史和最终报告；重连可读不证明原页面能自动回补。
原始 ANSI 没有逐块时间戳，文件 mtime 不能作为精确超时时点，先后时序仍由 TUI 工作线定位。

请求耗时约 32 分钟主要包含测试机停顿；有用工作时长不足以证明 15—30 分钟长任务验收。
模型菜单及真实响应已核对，实际请求端点的单请求日志证明仍须与菜单证据分开。
本组覆盖发布包 `c9042f5f2`，不覆盖尚未部署的准入依赖收窄候选。
递归、等待时插话、失败／取消、重复通知、恢复和有效长任务仍待后续矩阵；
测试机内存和交换区紧张单列环境因素，没有进行真实断网注入。原日志、身份和产物留仓库外。

## TUI 阅读与插话并行修复（已发布 main，未双机切换）

这条线独立于十步 Goal 的第 7 步子代理结果链；它修复运行中插话的历史顺序、
后台 native 回复重复显示，以及普通／详细／原文视图的阅读锚点和连续滚动。
15 个相关测试文件 **381 passed**；Ruff、导入边界、doc sync、strict code-size、
diff、clean-package 和候选 wheel 的发布边界检查通过。全仓 pytest 与线上 CI
没有作为本轮验收来源。

真实验收在用户授权的独立测试环境，由原生 tmux 测试会话发起，
同机只用一个 Gateway。实际模型为官方 `MiniMax-M2.7`，请求端点
`api.minimax.cn/anthropic/v1`；密钥及原始运行身份保留在仓库外。
真实会话名和 `tmux attach -t <会话名>` 所需参数见私有证据账。
完整交互矩阵所用 wheel SHA-256 为
`218fe2bc4b52875b3cfbcd240a14ec0a9bb32d3f3f3b74e35b811e1ecda94d`；
最终交接 wheel SHA-256 为
`b2bd4258b66574e9d3d756b73c16939351296d6e84e4d7de1c912a2db649d330`。
后者只同步模块注释，运行实现相同；切换后另由新原生 TUI 的官方模型完成
`37 × 19 = 703`，并补验展开和滚动。34 段双向完整矩阵属于前一个功能相同的包，
不能写成最终包逐项重跑。

运行中插话提交时，原 claim 仍为 running；原生 TUI 顺序为原工具调用、
用户补充、后续思考、收到补充后的回答，重连后用户输入仅一份。
后台命令结束后自动追加的 native 回复也按精确回合身份只显示一次。
34 段长记录向下和向上分别采样 159 次，段号没有反向跳动；
普通／详细／原文展开收起、单次上下键、鼠标 SGR 滚轮、84↔120 列调整均保留当前消息位置。
独立短 Goal 在真实等待 45 秒后读回第 600 条并置为 complete，模型正确解释无需再次发送用户消息。
原始终端字节、截图和精确会话／请求 ID 留仓库外，候选失败记录未计为最终通过。

本轮只验证 root TUI 与已有历史恢复；没有覆盖全部子／孙代理、IM、长达 100 小时的耐久，
也不证明所有重复正文问题均已解决。代码已在原 checkout 单独本地提交 `c297c52f3`，
已随本轮快进推送远端 main，并随第 7 步组合包切换测试机默认 Gateway；本机尚未切换；
此候选的真实验收不能冒充第 7 步新版子代理 TUI 验收。
文件归属、逐项证据与未测边界见 [TUI 阅读交接](docs/tasks/TUI_READING_HANDOFF.md)。

## 第 6 步插件故障与并发长任务阶段记录（框架验收收口）

当前运行包仍为第 5 步已发布的同一 wheel，尚无第 6 步产品代码改动。故障插件是仓库外构造的标准本地包，原包、合成输入和原始运行账均保存在私有测试目录，不随仓库发布。该包独立验证了 `probe`、完整 `isError`、受控阻塞和非零退出；停用与恢复没有按插件 ID 加核心特判。受影响的停用竞态、发布、调用及宿主命令 focused tests 已通过；本段真实 TUI 结论单列，不以组件测试代替。

本机 TUI179—185、187—194 共用原 Gateway，均在各自会话选择并核对官方 `MiniMax-M2.7`，请求端点为 `api.minimax.cn/anthropic/v1`。测试机 TUI174—178、186 也共用该机原 Gateway，官方端点为 `api.minimaxi.com/anthropic`。查看本机管理、插件长任务、核心长任务、故障、逐页长任务、源码审阅、审批及卸载后基线，分别用 `tmux attach -t release-0920-step6-local-manage`、`release-0920-step6-local-plugin-long`、`release-0920-step6-local-core-long`、`release-0920-step6-local-fault`、`release-0920-step6-local-ledger-long`、`release-0920-step6-local-core-review-long`、`release-0920-step6-local-approval`、`release-0920-step6-local-post-clean`。188—194 的新会话后缀依次为 `local-triple-plugin`、`local-triple-core`、`local-final-plugin`、`local-final-core`、`local-random600-plugin`、`local-random600-core`、`local-random600-core-retry`，均加在 `release-0920-step6-` 后；测试机用 `ssh <测试机> 'tmux attach -t release-0920-step6-remote-manage'` 查看管理 TUI，原故障 TUI 已正常退出，重连 TUI 用 `release-0920-step6-remote-resume`，其它后缀为 `remote-core`、`remote-plugin-long`、`remote-core-long`。原生 TUI 发起业务或管理；测试者只准备合成输入、控制明确审批／中断并只读核对原账和产物。

| TUI | 已核对的原生事实 | 结论与边界 |
| --- | --- | --- |
| 174、175，测试机管理／故障 | 真 TUI 安装、启用、`probe`、`fail`、`crash`、`stall`、停用与卸载。完整错误回执使原业务 FAILED，独立 `probe` 随后成功；审批 Esc 的原事件 `CANCELLED` 且 `handler_executed=false`、无业务工具操作。获批后异常退出留下原操作 UNKNOWN，新调用成功但不回写旧 UNKNOWN。 | 退出、完整错误、审批取消与未知结果边界部分通过；再启用曾在 `preparation_launch` 失败，测试机当时可用内存极低且交换区已满，原因尚未确定，不记为产品通过。最终故障包已停用卸载。 |
| 176、177，测试机并发业务 | 首次长请求在实际工具轮前因测试者误删正在使用的派生检索索引而碰到缺表；重建 schema 后，新 TUI 可真实调用工具。177 后续被精确中断当前回合，未停 Gateway。 | 测试准备失误；旧索引内容未恢复。两条原失败及中断均保留，不计长任务通过。 |
| 178，测试机核心 | 16 批共 4,800 行输入，原任务约 221 秒并完成；明细 4,800 行的数值正确。 | 质量报告把跨三个文件的重复编号误写成同一文件三次，并把退款排除口径与实际汇总写得不一致；属于模型产物内容失败，不能以任务 done 代替交付通过。 |
| 179、180、181，本机三路并发 | 管理 TUI 安装／启用故障包时，180 通过插件读 24 份文件，26 条操作中 24 次 `show` 成功，2,880 行、金额 273,909、72 条 review 与输入一致；181 用核心工具生成 4,800 行明细、汇总、代码与测试，原任务约 310 秒，独立逐行语义核对 0 错误，区域／产品计数、退款和金额均一致。 | 首轮插件任务约 287 秒，核心任务约 310 秒；管理可并行响应，核心产物通过当前数据核验，但两个任务都不足 15 分钟，不能计为连续长任务。 |
| 179、182，本机故障与撤销 | `stall` 已进入原业务操作 EXECUTING；管理 TUI 在同一调用约 17 秒后停用并释放原激活。原业务结算 UNKNOWN／`effect_outcome_unknown:TOOL_EXECUTION_FAILED`；该停用原回执 `cleanup_confirmed=true`、`released=true`，同代 3 个准备 session 均 `exited`、2 个 activation session 均 `killed` 且退出确认。再次启用并从刷新目录调用 `probe` 成功，旧 UNKNOWN 保持。重复停用也返回已释放。另一次 `stall` 超时在停用前发生，旧操作 UNKNOWN／`TOOL_TIMEOUT`。 | 执行中撤销与超时后停用是两份不同证据；管理不被阻塞调用锁死，旧结果不复活且准确 session 的清理有原账证明。审批等待期间停用、断连与受控清理失败还要继续核对。 |
| 180，本机第二轮插件任务 | 对 80 份合成日文件的一次中文请求约 119 秒完成；原账只含 1 次 `tree`、6 次 `show`，其余数据由模型改走核心 `run_command`。汇总数值与输入一致。 | 明确要求逐份经插件读取，模型没有遵守；这是本轮模型履约失败，不能算插件长任务或 80 份插件覆盖通过。 |
| 181，本机第二轮核心任务 | 80 份合成日文件共 1,920 行，原任务约 207 秒完成；独立按输入逐行比对明细的来源、字段与金额，语义错误为 0；80 天和 4 类汇总与输入一致。 | 核心多工具任务的当前数据交付通过；时长不足 15 分钟，仍不计连续长任务。 |
| 183，本机插件逐页长任务 | 新会话一次中文需求；原任务 `taskrun-1790142148-31de9a66` 完成于 1,221.5 秒，`ledger_page` 1—80 页恰好各成功一次，另有 1 次目录调用；主代理与 3 个孩子 done、2 个孩子 cancelled。`per_page.csv` 80 页的行数、原始金额和反冲金额逐页与合成源数据一致。 | 原生插件长任务的时长、页覆盖和运行框架成立；交付内容失败：分类行 `gross` 合计 343,307、`net` 合计 333,816，表内 `TOTAL gross` 却是 352,798、`TOTAL net` 343,307，报告宣称无异常。原中间账还保留派工无效、参数缺失及取消引起的失败／UNKNOWN；不把最终 done 洗成全链路零错误。 |
| 184，本机核心源码审阅长任务 | 新会话一次中文需求；原任务 `taskrun-1790142450-1a62b2d7` 完成于 983.2 秒，8 个子任务均 done，四类主产物及 8 份链路子报告实际落盘；162 个已跟踪源码快照文件逐一核对未被修改。 | 核心长任务、八路子代理及产物持久化成立；报告内容失败：目录分组写成 20／58／84，实际是 31／51／80；`findings.md` 称环境准备无超时，源码已有 120 秒预算和明确 deadline；又称 preparation 错误可能仍使 `cleanup_confirmed=true`，与 `not report["errors"]` 条件相反。该报告不能作为重构依据。 |
| 179、185，本机清理失败与恢复 | 私有故障包 v0.3 的 `arm_cleanup` 在本激活安装环境内创建无权限测试目录，原业务成功。TUI179 首次停用的原操作 `0009702ae476466dbe5790c74a8b23fe` 为 UNKNOWN／`PLUGIN_CLEANUP_UNCONFIRMED`，安装激活保持 revoked；旧 TUI185 的 `probe` 被旧目录版本拒绝。测试者只恢复该夹具目录的权限，未替产品删除环境或改原账；第二次停用原操作 `5eac1255d68e4b43a2e8c97c2b985bcc` 成功，`cleanup_confirmed=true`、`released=true`、错误列表为空，环境才消失，旧 UNKNOWN 保留。随后新代启用的 `probe` 成功，最后经真实 TUI 停用并卸载。 | 仅证明安装环境回收失败及可恢复收尾，不冒充进程退出失败；未确认阶段没有把 revoked 当 released，旧代没有复活。 |
| 174、175，测试机审批等待撤销 | 在默认确认模式下，TUI175 原生 `/plugins@fault-lab crash` 出现“允许一次／拒绝”弹窗；审批等待期间 TUI174 停用同一激活成功，随后 TUI175 才选择允许一次。原任务 `taskrun-1790145592-b2d90dc8` 最终 failed，业务工具操作 FAILED／`effect_outcome=not_started`，输出为 MCP 连接已关闭或被替换；这里 `handler_executed=true` 仅指宿主代理 handler 被进入，不证明插件实际 `tools/call` 发送，亦没有发生预设的插件 crash。TUI174 随后卸载成功。TUI175 打开权限菜单后关闭菜单的 Esc 还曾中断一个既有模型轮，此为测试控制误操作，不能计作产品审批取消。 | 审批后复查阻止了已撤销代次的插件调用；不能写成“零宿主操作”，也不能把弹窗关闭时的中断当成计划内验收。该次 TUI 提示连接退出未确认，须将连接清理提示与安装资源释放分开核对。 |
| 175→186，测试机原会话断连重连 | TUI175 通过 `/exit` 正常退出客户端；TUI186 用原 session ID 在新 tmux `release-0920-step6-remote-resume` 恢复，同一 Gateway 未重启。原生 `/plugins list` 仅见 `workspace-peek`，随后 `/plugins@fault-lab crash` 在目录层回复当前无此插件，并给出查询编号。 | 已卸载包没有因会话重连重新进入目录；此处只证明 TUI 命令目录拒绝，不从界面文案推断历史原账改变。 |
| 187，本机卸载后核心基线 | 新工作区、新原生 TUI 在本会话模型菜单选中官方 `MiniMax-M2.7`；`/plugins list` 只显示 `workspace-peek`。原任务 `taskrun-1790147053-1e12bb93`、主 AgentRun 均 done，4 条工具操作均 SUCCEEDED。普通中文需求由核心工具生成 12 行平方表、实际读回并写报告；测试者独立核对每行 n²、行数及总和 650，均正确。双机原 Gateway PID 保持，测试机已卸载故障包的受管进程记录无该插件归属。 | 卸载后新会话的核心写入、读取和模型链路可用；这是一条短基线，不抵充长任务，也不证明前两份模型内容失败已修复。 |
| 179、188、189，本机三路真实重叠 | 两条新任务同在 00:27:14 建立并进入工具轮；188 用已启用的 `fault-lab`，189 以核心工具和子代理审阅独立源码快照。两者未结束期间，179 的独立 `control-lab` 安装、启用、`probe`、停用、卸载原工具操作分别在 00:27:56、00:28:28—33、00:29:08、00:29:22—23、00:29:41 完成，五条均 SUCCEEDED。控制包是仓库外独立校验的标准本地包，只管理自己的激活；两条业务任务没有因其装卸被停止。 | 原持久时间证明三路并发的装卸可响应；这段管理窗口约 2 分钟，不冒充三路都持续 15 分钟。此前 183／184 的两条长任务重叠约 15 分钟，另行证明长负载并发。 |
| 188，本机插件逐页新任务失败 | 官方模型新 TUI，一次中文需求；原任务 `taskrun-1790148434-864ed158`。80 次 `ledger_page` 原操作全部 SUCCEEDED，独立从每条工具输出解析，1—80 页各一次、24 行／页、所有字段与测试源 0 错，真实原金额 352,798、反冲金额 9,491、净额 343,307。模型随后试图把大量数据展开成单次 Bash 参数，界面报模型输出长度限制；原 attempt 已结束但 TaskRun 保持 created，原事件为 `runtime_status=unfinished`／`MODEL_RESPONSE_TRUNCATED`／`runtime_source=model_provider`，工作区无所求产物。 | 不能计任务完成或长任务通过。80 页读取链本身正确，模型中途打印的原金额 744,522 与原工具数据不符；供应商截断的 Bash 参数没有执行。当前轮内截断续写仅适用于进入最终答复裁决的路径，供应商级未闭合工具参数在该路径之前收口；此通用模型／工具循环缺口列入第 8 步，不按插件 ID 或本次数据样例特判。 |
| 189，本机核心多子代理长任务 | 原任务 `taskrun-1790148434-ce125910` done，运行 979.2 秒；主代理和 7 个 researcher 子代理均 done，42 条工具操作中 41 条 SUCCEEDED，1 条路径错误 `run_command` FAILED 后模型自行修正。162 个源 Python 文件的快照哈希未改变；`file_roles.csv` 162 行唯一、`call_graph.json` 162 节点，两个文件集合均与源码快照完全相同，7 份链报告及主报告实际落盘。 | 核心 16.3 分钟持续工作、子代理持久收口和产物覆盖成立；用户需求中的“至少 8 个子代理”只完成 7 个，模型总结仍称八链。`findings.md` 有 18 条表项，最终回复却称 1 高／7 中／5 低，仅合计 13；风险判断未逐条独立证实，不能直接当重构依据。框架成功与模型履约／报告内容失败分开记录。 |
| 179、190、191，再次三路重叠 | 原插件任务与核心任务同在 00:56:43 建立；管理 179 在它们运行中安装、启用、`probe`、停用、卸载独立 `control-lab`，五条原工具操作在 00:57:35—01:00:10 均 SUCCEEDED。190 随后在约 502 秒 done；191 在约 827 秒 done。管理过程没有取消两条业务任务，同一 Gateway 未重启。 | 原装卸并发通过；190／191 分别不足 15 分钟，不能用这段替代 183／184 已有的长任务重叠，也不能把两条最终报告称为质量通过。 |
| 190，插件页任务交付失败 | 原任务 `taskrun-1790150203-23adfd25` done，主代理仅成功调用 1 次 `ledger_catalog`，四个孩子的原账中 **0 次** `ledger_page`。其中一名子代理留下 OPEN 能力申请，工具回执明确要求不要伪造能力结果；主代理仍用子代理生成的确定性推算表交付 80 页报告，宣称逐页读取。独立比对 `per_page.csv`：80 页齐全但 59 页至少一个字段错误，其中 21—40 页的 20 页原金额全错；其它 39 页净额错误。 | 插件分页覆盖为 0，不能计插件任务通过。能力申请和子代理阻塞按原状态保留，主代理的完成宣称与原工具账矛盾；属于通用派工／交付核验缺口，后续第 7、8、10 步分别核对身份、事实与组合，不在核心按插件 ID 修补。 |
| 191，核心审阅近长任务 | 原任务 `taskrun-1790150203-6de8cfe6` done，运行 827 秒，主代理与 4 个孩子均 done；25 条原工具操作中 23 条 SUCCEEDED。`file_roles.csv` 覆盖 162 个源 Python 文件且哈希未变，调用图 2,188 个 AST 节点、11,898 条边与实际 JSON 相符。 | 13.8 分钟低于本步约 15 分钟长任务阈值；产物存在与索引覆盖成立，报告风险判断未逐条独立核实，不能当维护性问题事实源。 |
| 193，本机扩大源码样本派工失败 | 以 321 个已跟踪 Python 文件快照审阅，派工前发生供应商级 `MODEL_TOOL_ARGUMENTS_INVALID`；该工具调用未执行，原 TaskRun 留 created。 | 不计核心任务完成或长任务通过；新 TUI194 独立重做较小的 162 文件源码快照。 |
| 192，本机 600 页插件长任务 | 仓库外标准测试包 v0.4 独立验证 600 页、每页 24 条不可由页号推算的合成记录；原始包 SHA-256 为 `847aef3563983a0552b159c436a70aac9850a7044a6033a3a9a22360a40c45be`，仅私有测试目录保存。一次中文需求的原任务 `taskrun-1790151726-b6fdd70b` 为 done，持续 3,053.6 秒；主代理 done。600 次 `ledger_page` 原操作全为 SUCCEEDED，页 1—600 恰好各一次，24 行／页的原返回与包内合成源逐字节相同。24 个批文件、600 行 `per_page.csv`、分类汇总和报告均实际落盘。 | 插件执行、长任务持续、Compact 五次后的分页覆盖与持久产物链路成立；**模型内容交付失败**：600 页中 306 页的最终逐页金额不符，另有 50 页反冲额不符。实际原金额／反冲／净额为 3,702,951／103,431／3,599,520，交付为 3,225,970／88,815／3,137,155。批 1—9 各 50 页，批 10—24 各 10 页，报告却称每批 25 页、已读回核验；内部行数和页覆盖正确不能替代源数据准确性。测试者未补文件或改原结果。 |
| 179、192、194，本机长任务和管理同窗口 | 192 与 194 都在运行时，179 对独立 `control-lab` 实际执行 `probe`、停用、卸载；安装和启用则发生在 192／193 活动时。停用与卸载紧接提交触发一次 `PLUGIN_CATALOG_STALE`，随后停用原请求成功；179 重新查看管理帮助和安装目录后卸载，原回执成功。两条长任务自然完成后，179 也按刷新后的目录停用并卸载 `fault-lab` v0.4，最终原生目录只剩 `workspace-peek`。本机和测试机 Gateway 均保持原进程。 | 管理目录版本拒绝按原协议处理，不将首次过期请求算成功；管理未中断业务，两个受控测试插件都已卸载。192／194 各自超过 15 分钟且有实际工作，管理命令在两任务同时运行的窗口内执行；不声称全部管理操作与两条任务持续重叠 15 分钟。 |
| 194，本机核心长任务自然结束 | 新 TUI 原任务 `taskrun-1790152078-d95d6029` 为 done，持续 973.9 秒；主代理和 2 个 worker 均 done。162 个 Python 源文件快照与当前源码哈希逐一相同，`file_roles.csv` 有 162 个唯一源路径，`call_graph.json` 有 162 个文件节点，二者路径集合均与快照一致；四份主产物实际存在。 | 核心约 16.2 分钟持续工作、子代理收口和文件覆盖成立。报告的架构判断未逐条独立验证，不能直接作为重构事实。 |

测试机清理已确认的旧资料后，根分区非保留空间约 1.3 GiB，但可用内存不足 0.1 GiB，交换区满；不把 `preparation_launch` 的相关性直接写成因果。清理时误删当前工作区的派生检索索引，旧内容丢失；重建 schema 只证明后续新调用可继续，不证明历史已恢复。该失误独立于插件产品验收。TUI183／184 的 20.4／16.4 分钟任务、TUI192／194 的 50.9／16.2 分钟任务及管理重叠形成第 6 步长任务与并发框架证据；内容交付失败分别保留，不把它们当作质量验收通过。审批等待撤销、重连、可恢复清理失败及卸载后核心基线已补证；测试机原生业务连接退出未确认的提示与安装资源释放分开记录，旧结果仍不改写。第 6 步框架验收收口；本步未改产品代码，通用交付核验和模型／工具循环问题移交第 7、8 步对应合同，不能写成已修复。

## 第 5 步使用卡发布与原生 TUI 阶段验收

`689e83e85` 已推送 main；发布 wheel 的 SHA-256 为 `868f3882c23ca317abcfeef835c4fc5694c67a47c3bdf83db76b01c5b0b48d1f`。本机与测试机从同一包安装，各 1,267 个包文件逐项匹配，默认入口与每机唯一 Gateway 同版；切换前核对无活动任务并保留旧运行环境及回滚证据。测试机非保留磁盘可用空间为零，后续重负载优先本机。线上 CI 未作为验收来源。

新版原生 TUI168—170 在测试机共用一个 Gateway，TUI171 在本机另一台唯一 Gateway；171 和 169 分别退出客户端后按原会话恢复为 172 和 173，Gateway PID 均未变化。原菜单选择官方 `MiniMax-M2.7`，恢复后会话模型保持相同；测试机端点为 `api.minimaxi.com/anthropic`，本机为 `api.minimax.cn/anthropic/v1`。源会话、原操作、审批和产物的详细记录留在仓库外；下表只列已核对的事实，不把短任务计作长任务。

| TUI／tmux 会话 | 实际操作与读回 | 当前结论 |
| --- | --- | --- |
| 168／`release-0920-step5-remote-manage` | 原生 `/help` 更正显式入口；安装后的 `/plugins list` 展示简介，`/plugins info` 与启用回执展示相同来源的动作、两条中文示例和设置键名；随后原生停用、重新启用。 | 原管理账的安装、启用、停用、再启用四条 TaskRun／AgentRun 均 done，对应四条工具操作均 SUCCEEDED。 |
| 169／`release-0920-step5-remote-explicit` | 带中文及空格的路径以显式命令读三页，范围 0—31、31—63、63—95，三次原业务操作均 SUCCEEDED，合并文本与 95 字节源文件一致；每页经原生 TUI 明确审批。管理端停用后，旧目录版本提交被拒且没有新增 TaskRun／操作；Tab 只保留帮助候选，重新启用并刷新后动作候选恢复。随后四次原生审批读完 13,760 字节长文件，区间 0—4,094、4,094—8,188、8,188—12,282、12,282—13,760，四条原操作均 SUCCEEDED，拼接全文与源文件一致。 | 显式参数、连续分页、审批、旧候选拒绝、目录刷新和长输出通过。Tab 后原任务／工具操作数不变；无效选项仅显示命令错误，未转成聊天或 Shell。详细记录模式下全选复制把 tmux 缓冲从测试哨兵值替换为真实记录。 |
| 170／`release-0920-step5-remote-chinese` | 一次普通中文需求，模型自行调用 `workspace-peek show` 并通过 TUI 审批，再调用 `write_file`；原任务和主代理均 done，两项工具操作均 SUCCEEDED，116 字节结果包含源文件的两项安排。 | 普通中文到插件及内置写入的短任务通过。 |
| 171／`release-0920-step5-local-smoke` | 一次普通中文需求，模型调用 `write_file` 创建两行文件；另一次四行中文需求经终端 bracketed paste 输入，TUI 显示折叠占位符，模型写出的四行文件与粘贴正文逐行一致。两次原任务和主代理均 done，两条 `write_file` 操作均 SUCCEEDED。 | 本机新版 Gateway、真实模型／内置工具链及多行粘贴展开通过。 |
| 172／`release-0920-step5-local-resume` | 171 退出客户端后按同一会话恢复，原两次任务历史、模型选择和输入框可见；缩小窗口后 PageUp／PageDown 改变并恢复可见历史。 | 重连与滚动通过，Gateway 未重启。 |
| 173／`release-0920-step5-remote-resume` | 169 退出客户端后按同一会话恢复，`/plugins list` 仍显示启用插件；再次显式读取中文空格路径，经原生审批，新增的原 HostCommand／工具操作均成功，首段字节与源文件一致。 | 插件目录和实际调用跨 TUI 客户端重连通过，Gateway 未重启。 |

第 5 步的框架必测项已收口，未发现本片新增的框架失败。旧 TUI164 的模型文本失真继续归第 8 步通用交付核验，不由本片短任务覆盖；连续长任务、多子代理和受控故障属于第 6—10 步。测试机 TUI169、本机 TUI171 已正常退出并分别恢复为 173、172；查看仍运行的测试机 TUI 用 `ssh <测试机> 'tmux attach -t <上表会话名>'`，本机用 `tmux attach -t release-0920-step5-local-resume`。测试机磁盘边界仍未消除，后续重负载优先本机。

## 第 5 步使用卡与目录同源的本地验证

解决问题：旧版 TUI163 的 `/plugins info` 只有简介，顶层 `/help` 误说显式插件入口未开放；启用成功后也没有可直接使用的说明。当前片只修改公开展示投影，不改变安装表、授权或执行链。

本地源码已让列表展示包简介，详情和成功启用回执共用从当前包声明、动作及设置 schema 生成的使用卡；旧启用请求只有在激活代次仍相同时才附卡。公共动作用法由同一参数声明生成，顶层 `/help` 文案已纠正。设置仅展示公开键名，不读取或展示私有配置值。既有目录 v3、客户端 revision、Tab 刷新和提交链未新增状态或轮询。

`test_plugin_commands.py`、`test_plugin_command_catalog.py`、`test_plugin_command_client.py`、`test_plugin_management.py`、`test_plugin_configure_management.py`、`test_plugin_removal.py`、`test_plugin_removal_store.py`、`test_gateway_plugin_commands.py`、`test_gateway_plugin_management.py`、`test_tui_input.py`、`test_workspace_peek_package.py` 共 300 项通过。覆盖使用卡与帮助用法一致、无显式动作时不虚构 slash 入口、旧启用回执不宣传新代次、列表简介和原请求查询位置。发布前本地严格 gate 的全目录 Ruff、文档同步、严格尺寸、diff 和 clean-package 均通过；尺寸报告仅由检查脚本生成，不随本片提交。此处只记录源码验收；发布后的新版原生 TUI 证据见上节。

## 第 4 步三项修复发布后的原生 TUI 验收

解决问题：旧版实际验收中，完整插件错误被记为 UNKNOWN、目录第三页碰到连接释放窗口、重复启用被空资源域挡住；必须由安装版原生 TUI 证明修复，而非把定向回归当作交付。

`fb3857e46` 已推送 main；同一 wheel 摘要为 `56a7ae15385554ecdd70f952c030485e00a41d7e5cf74a6b2c4b37655d343baf`，双机各 1,267 个安装文件核对一致、各一台 Gateway。五个新 TUI 均从新版默认入口打开并在原菜单选择官方 `MiniMax-M2.7`；本机来源为 `api.minimax.cn/anthropic/v1`，测试机来源为 `api.minimaxi.com/anthropic`，两路核心任务均有真实模型调用。未改日常默认模型。原始会话、请求、工具账与截图留在仓库外。

| TUI | 本轮实际操作 | 原生证据与结论 |
| --- | --- | --- |
| 163，本机管理 | 已启用时重复 enable；链接路径 show 失败后再次 show 普通文件 | 重复启用原操作 SUCCEEDED／unchanged、激活未更换；链接原操作 FAILED／UNSAFE_PATH、原锁为零；后续 show SUCCEEDED，首 64 字节与输入一致。旧 158 UNKNOWN 不回写。 |
| 164，本机中文业务 | 一次普通中文需求，自行 tree／show 并写报告 | tree、三次 show 和 write_file 原操作均 SUCCEEDED；实际产物把源文 🙂 写成 😂，五处 CRLF 也变为 LF，却声称完整。框架调用通过，模型内容交付失败，不能计为文本保真通过。 |
| 165，本机核心 | 内置工具生成与读回 80 行整数平方 | 80 行和两列汇总正确，任务终态完成；属于短任务。 |
| 166，测试机管理 | 真实审批逐页目录、拒绝 show、坏包／配置、重新配置启用、显式 show、停用卸载，再从空表重新装卸 | 目录四页范围 0—2、2—4、4—6、6—7 均 SUCCEEDED，七项不重复；拒绝 show 的原任务 failed、无工具操作；坏 ZIP 与坏配置均 FAILED／TOOL_INVALID_ARGUMENTS；正常配置、再启用及获批 show 均成功。停用／卸载后安装表为空，五个 session 确认退出／清理，无未解决 PID；卸载后显式调用未登记原任务／工具操作。随后有效包安装→配置→启用→获批 show→停用→卸载在同一新版 TUI 均成功，安装表再次为空。 |
| 167，测试机核心 | 内置 60 行任务；166 卸载后再做 40 行任务 | 两次原任务完成，60 行与 40 行产物逐行正确；卸载后 40 行两列和为 820／22,140，核心仍可用。均为短任务。 |

新版真实 TUI 已覆盖 4.6d 的重复、失败恢复、连续分页、审批拒绝、坏输入与装卸释放。旧 159 的旧补全撤销和 160 的三孩子并发结果保持独立证据，不被本轮短任务替代。164 的内容错误归后续通用模型交付核验，不能通过重试碰运气宣布成功；第 6—10 步仍需按 Goal 做连续长任务、多级子代理及最终组合验收。远端线上 CI 没有作为本轮验收来源。

测试机重新准备隔离环境后，非保留磁盘可用空间降为 0；root 保留块尚有空间，本轮原启用、读取和清理仍成功。只清除了本任务新 runtime 的生成 `.pyc`，未动源码、配置、历史和回滚包。后续大体量长任务优先本机，测试机重负载前先恢复可用空间；不能把当前短任务通过推广为磁盘压力验收。

## 第 4 步剩余装卸与完整 MCP 失败回执

新版原生 TUI 的新增证据：158 重复安装／停用／卸载的原操作均 SUCCEEDED；重复启用 active 为准备阶段拒绝，尚不计通过。
后者已在原管理链复现：没有环境计划时仍构造空 declared 资源域，先于已有 unchanged 分支抛出 ValueError。
本地修为无计划时不声明资源，回归确认重复启用不改安装记录、不创建新连接，缺失插件返回明确 plugin_missing；旧实现失败，新实现通过。
159 实际 Tab 选择旧目录、暂存输入，158 卸载重装启用后恢复并提交旧输入；界面明确拒绝，原 HostCommand 登记和工具操作均不存在，业务任务数量不变。
161 目录前两页按 0—2、2—4 成功，第三页准备阶段 `HOST_COMMAND_PREPARATION_FAILED`，handler 未执行；原参数摘要核对一致，不重发原请求覆盖失败。
第二页 TaskRun 已关闭后第三页提交，而第二页完整连接清理证据在第三页拒绝后约 60ms 才落盘；原日志只含 ValueError 类型。
原资源门在该中间状态会拒绝新连接，证据高度支持清理窗口判断，不能声称保存了现场异常正文。UI/HTTP 最终回包并未提前；测试观察原任务账后提前发送了下一页。
本地已将本次连接释放纳入原 HostCommand 执行区间，业务 operation 保留确定结果，资源释放结束前运行不提前终态。
两个 Event 固定窗口的成功／审批拒绝用例旧实现均失败，修后通过；查询和重复提交不重开连接、不代替原执行者释放。
查询的中文文案同步保持“连接收尾尚未确认”，不被“调用已完成”覆盖；另验准备失败先释放本次资源再记录拒绝，重送不再次释放。
158 链接读取的原操作为 UNKNOWN、原因 `effect_outcome_unknown:TOOL_EXECUTION_FAILED`；原账没有保存正文，不能声称现场路径拒绝已经通过。
输入文件实际读回 180 字节、5 处 CRLF、无字面 `\\r\\n`，先前分页逐字节校验口径保持。

沿 MCP 合同、现有代理和操作协调器另行确定性复现：完整合法 `isError=true` 回执未提供失败结局，被改记 UNKNOWN。
本地修复只补完整回执的 `effect_outcome=failed`；不表示没有部分副作用，不回写历史 UNKNOWN，传输及清理异常仍保持未知。
新增测试使用原 ToolExecutor、临时 RuntimeDB 和宿主命令身份，核对失败正文、原结果只读重放、释放逻辑锁后的下一独立操作，
并以超时、断连、非法响应、代理异常和未发送拒绝作对照。修复前 1 项目标失败、5 项边界通过，修复后 6 项通过。
首次测试夹具的摘要格式和审批设置错误已修正，原日志单独保存，未把夹具错误当作产品复现。
MCP 注册／协议／生命周期、托管连接、操作幂等／受管门与宿主重放共 8 个文件 **231 passed，0 failed / 0 errors / 0 skipped**。
本片尚未发布部署，不抵充真实 TUI 复验；原安装版仍为 `d4d540c57`。

三项修复合并后的最终验收：17 个相关文件 **389 passed，0 failed / 0 errors / 0 skipped**，166.425 秒。
9 个本轮 Python 源码／测试文件的修改时间均早于测试开始，最终摘要已留私有证据。Ruff、文档同步、严格尺寸、diff 和 clean-package 均通过，尺寸基线不变。
clean-package 首次只因新增测试尚未纳入 Git 失败，显式加入后通过；首次相关命令含不存在的测试文件，未执行，已更正路径重跑，不计作产品失败。
本次改动未达到追加全仓阈值，未重跑全仓；线上 CI 未作为验收来源。下一交付为发布同包部署后真实 TUI 复验，原失败不覆盖。

## 第 4 步启动观察竞态修复

解决本机实际插件 enable 在准备命令已退出 0 后仍报 `preparation_launch` 的问题。
启动观察先读旧状态、后见 host 退出时，现立即复读同一 session，复用原取消／状态判定；不增加 sleep、命令重试或第二份状态。
最终交接仍在原事务内核对身份、执行权和取消，非零退出码继续交原调用方裁决。
确定性交错的 4 项正向用例在旧源码全部失败；修复后加上取消、撤销、未知、缺失和损坏等边界共 13 项通过。
后台交接、stdio、恢复、原 Store、Shell、插件准备／激活／装卸等 16 个相关文件共 **383 passed，0 failed / 0 errors / 0 skipped**，用时 88.343 秒，受测启动模块摘要不变。
本地严格 gate 已通过，尺寸基线未改。修复提交 `d4d540c57` 已发布 main，同一 wheel 双机安装的 1,267 个包文件均一致，默认入口与每机唯一 Gateway 同版；原 TUI154 失败保留。
Git 传输超时后，通过 GitHub Git data API 创建原对象，逐项核对 tree 与 commit SHA 完全一致，再 `force=false` 更新 main；未查询到该提交线上 CI，线上 CI 未作为验收来源。

新版实际 TUI158—162 均从默认入口创建并在原模型菜单选择官方 MiniMax-M2.7，核对 provider 和端点，只改变测试会话：

| TUI | 所测事实 | 结果边界 |
| --- | --- | --- |
| 158，本机管理 | 原正常插件启用成功；停用并释放后拒绝新调用，再次启用、卸载成功；用户产物哈希不变 | 停用与 160 主任务执行重叠；再次启用及卸载发生于该任务完成后，不能据动作标签声称全部并发 |
| 159，本机中文业务 | 原工具账确认 `tree`／`show` 成功，读到完整原文；模型自行写出结果文件 | 工具保留原表情，模型报告改成另一表情且称原文完整，交付失败；未由测试者修复 |
| 160，本机三子代理 | 三孩子共同执行 30.887 秒；各 3,000 行，合并 9,000 行逐条等于子文件，分组／类别汇总和总额一致 | 原检查脚本失败后由模型自行修正并执行，原回执为退出 0；本轮所测数据与报告通过，未做破坏性数据挑战，不算长任务 |
| 161，测试机管理 | 原停用插件在新版本启用成功，原操作为 SUCCEEDED、激活为 active | 完整远端装卸及目录分页继续验收 |
| 162，测试机核心 | 实际生成并读回 1—100 及平方，100 条数据和 JSON 检查一致，两列和分别为 5,050／338,350 | 普通核心任务通过，不抵充子代理或长任务 |

测试机首次候选安装因磁盘满失败，旧 Gateway 未立即切换；移除确认无进程引用的本次失败安装，仅清理本任务 runtime 的生成 `.pyc`，不删源码、配置、历史或回滚包。
原心跳文件因磁盘满为空；独立核对 HTTP 存活、空队列、全部相关 claim／调度／curator、插件无激活和无 CLAIMED／EXECUTING 操作，SQLite `quick_check` 通过后有序停止旧进程并启动新版。
新心跳恢复正常，空文件隔离回调实际未移动文件，不能声称人工补写了心跳；网络配置不变、未强杀进程。磁盘余量仍低，主要任务放本机；这次环境恢复不计作插件故障恢复验收。

## 第 4 步发布 gate 与双机安装

`6050ab600` 修复后的全仓：**18,623 passed、5 XPASS、35 xfail、21 skipped，0 failed / 0 errors**，
测试用时 1,026.408 秒，2,429 个受测源码/配置文件摘要前后不变。5 个 XPASS 与上一轮数量一致。
本地严格 gate 全部通过，尺寸基线未改；远端 main 已通过 GitHub API 确认同一提交，未查到该提交线上 CI 运行，线上 CI 未作为验收来源。

标准 non-editable wheel 已同包部署双机，各自 1,267 个包文件与 wheel 一致；旧取消文件不存在，新公共取消模块已包含。
每机一个 Gateway，默认入口同版；切换前核对请求、claim、调度与 curator 空闲，配置、环境及除 runtime 外的参数不变。
原 runtime 和私有回滚记录保留。实际 TUI154—157 已创建并经原模型菜单核对官方 MiniMax-M2.7；完整插件验收尚在进行，不能以安装或启动成功代替功能通过。
当前正常包测试机已 active，显式读取先验证一次拒绝，再逐次批准三页调用；原工具账的字节范围为 0—63、63—127、127—180，拼接与原 180 字节文件逐字节一致，末页 EOF。目录分页与完整装卸仍待验收。
本机 enable 原操作为 `preparation_launch`，三个准备进程均退出 0；已独立复现启动观察读到旧状态后遇到 host 退出而未重读终态的竞态，修复尚未实施。
原回执没有底层异常文本，不能声称已保存现场精确交错；原账与纯内存复现共同支持此定位。
本机故障包原启用为 `plugin_endpoint_failed`，卸载清理回执保留 activation 退出码 23，预设失败点已核对；TUI155 尚未发送业务需求。

TUI157 三名孩子各生成 3,000 条有效订单，编号与金额独立读回一致；主代理的合并文件保留两处重复表头，共 9,003 行。
其统一检查程序只读取三份子文件，没有验证交付的合并文件，却报告全部通过。该轮交付失败保留，不由测试者修产物，
也不增加 CSV 专项核心分支；并发执行与交接事实和交付质量分开记录。


本次全仓退出阶段另出现第三方 `lark_oapi.ExpiringCache` 在已关闭事件循环上取消任务的析构警告；
上一轮没有相同退出记录。本次 pytest 的失败/错误计数和退出码均为零，该警告单独保留，不宣称日志零警告，也不修改第三方依赖掩盖它。
原 Git HTTPS 查询曾空响应/超时；仅本次 Git 命令使用协议 v0 和 HTTP/1.1 后成功快进发布，未改全局 Git 或系统网络设置。

## 第 4 步发布前失败修复与当前 gate

首轮累计全仓在 `66435c9ac` 收集 18,684 项：5 项失败、35 项既有 xfail、21 项跳过，源码摘要前后不变。
失败记录保留，候选尚未发布；不能用此前的组件通过抵消此次发布失败。

- CLI 直接引用 tooling 取消令牌触发 1 项边界检查失败（3 个导入点）。唯一原实现迁到 `common/cancellation.py`，
  44 个直接消费者/测试导入同步迁移，删除旧文件；AST 核对逻辑不变，27 个生产消费者仅调整导入与相关说明。
- 静态 slash 测试把现已实现的 enable 当作未实现动作。改验 `enable --help`，保留不写文件、不调用模型或旧控制器的全部断言。
- 生产工具注册装配桩补齐真实 HomePaths 已有的 root，生产入口不添加缺字段兜底。
- 两项原生 prompt_toolkit 管道测试只替换旧 JSON 传输，交互命令实际已走消息流。
  补齐流式替身并校验工作线程、请求编号、服务路径与取消令牌；原 UI 断言和等待时长保持，不修改产品流程。

修复后 50 个相关文件 **1,195 passed、0 failed、0 error、0 skipped**，221.736 秒。
Ruff、导入边界、文档同步、严格尺寸、diff 与包干净度通过，尺寸基线未改。
该修复片随后完整发布结果见上一节；组件通过不代替完整实际 TUI 验收。

## 第 4 步 SDK 与 workspace-peek 实际包开发验证

本片以标准 setuptools 构建 SDK wheel、样本 wheel 和完整安装 ZIP；原包、RECORD 与依赖闭包校验均通过。
SDK 安装到没有宿主包的临时 Python 环境，逐字节核对三个共享源码及 LICENSE/NOTICE。
样本经原 MCP 客户端和原宿主管理/Registry/ToolExecutor 组件运行，不计真实 TUI 或真实模型验收。
最终 12 个相关文件 **254 passed、0 failed、0 error、0 skipped**，101.58 秒；本地 Ruff、文档同步、严格尺寸、diff 与包干净度检查通过。
发布累计范围的全仓测试另行执行，未将上述 focused 结果当作发布完整 gate。

- 4 字节小页读取中文、表情和 CRLF，拼回原字节；空文件有效，非 UTF-8/空字节明确失败。
- 文件和目录游标绑定当前对象及权限上下文；内容或目录变化后旧游标失效，空权限和缺上下文不退回进程 cwd。
- 路径链符号链接、多链接、FIFO、凭据文件及上溯被拒绝；错误后同连接仍可执行正常请求。
- 目录排序分页、深度、隐藏拒绝名称、扫描预算和实际配置均验证；超预算不生成假完整结果或续页游标。
- 三路并发 MCP 请求读取各自工作区；这只是组件并发，不能算三路真实 TUI。
- 实际构建包经原管理入口完成默认停用安装、配置、启用、审批后的显式调用、普通工具调用、停用、再启用、卸载。
  重复安装请求复用原操作，坏配置不提交；旧快照撤销、重新启用换代、卸载保留用户文件及内置读取继续可用均核对。
- 独立进程的坏配置不泄露值；坏 JSON 后正常初始化可继续；依赖未闭合不生成安装包。

开发期间修正了临时 venv 在 macOS 使用复制解释器导致动态库缺失、测试漏启动 MCP 客户端、误把宿主结果包装当作
插件正文等夹具问题。卸载后的内置工具检查改走原 Registry 调用入口：只读工具不属于插件管理操作查询，
不能因管理账内没有该只读操作而报告产品失败。另补显式开发构建依赖，避免依靠本机预装后端。
上述修正没有增加产品 fallback、放宽权限或替被测插件生成业务结果。

发布与部署另行验收：累计 Python 改动超过发布阈值，4.5 需追加全仓 pytest；完整实际多 TUI 仍在 4.6。
原始开发报告和构建产物保存在仓库外；本节不保存机器路径、私有配置或原始日志。

## 第 4 步逐次工作区读取上下文开发验证

本片仅本地合同与组件验证；真实临时 MCP/venv 经原执行器运行，不计产品实际 TUI 或真实模型验收。
最终 19 个相关测试文件 **316 passed、0 failed、0 error、0 skipped**，95.024 秒；受测 2,318 个 Python 文件前后摘要一致。

- 原 Registry 使用宿主的实际 cwd、exact 读取范围和明确外部授权；空交集拒绝全部，单文件授权不放行父目录。
- 两工作区共用固定 MCP 代理仍逐次携带自己的 `_meta`，业务参数不混入 cwd；普通 MCP 即使声明扩展也不收到宿主路径。
- 缺少上下文在发送前拒绝；旧连接必须先明确断开再重连，原代理不追随新连接，结果保留 `not_started`。
- 核心文件工具与插件合同共用路径裁决；覆盖 owner/full、跨 owner、控制目录、危险子项、凭据文件和符号链接。
- 宿主数据根及原 ownerless 外部策略分别冻结，插件环境变化不改原权限；默认危险根规范化后去重，修复系统路径别名导致的协议往返失败。
- 实际临时插件经管理安装、启用后，普通注册工具与显式命令均收到同一宿主上下文；仍沿原审批、激活和连接清理。

初轮默认根往返暴露了重复规范路径问题，已修复；组件夹具另修正 prepare_for_run 和原 close_mcp_clients 调用。
相关回归首轮仅新旧连接测试失败：对仍存活连接调用 reconnect 原本就是幂等复用，不能当成已换代；
修正测试先断开自己的临时 MCP 后再重连，没有修改产品重连语义或系统网络。
SDK 产物、workspace-peek 业务功能与实际多 TUI 装卸仍待验收，组件回显不能代替文件预览功能。

## 第 4 步独立命令交互审批开发验证

本片仅本地实现与组件验证：真实临时 HTTP、原执行器/MCP 和 TUI 控制器联通，不计产品实际 TUI 或模型验收。
最终相关 22 个测试文件 **511 passed、0 failed、0 error、0 skipped**，受测 2,318 个 Python 文件前后摘要一致。

- 每次 Enter 固定编号、审批消费者及取消位；审批与主子任务共用 FIFO，不创建聊天回合或更改其执行身份。
- 同机 Gateway 在原 HTTP 请求线程执行，客户端持续读心跳并独立等待面板；无消费者明确返回需要审批。
- 批准、拒绝、取消分别核对原工具调用次数；重放不再弹窗或执行，结果仍按原编号查询。
- 原服务端首帧固定规范 owner，覆盖关闭按用户隔离时非 main 客户端的审批往返；客户端不能提供审批路径。
- 断连只取消原命令；另一个请求在原连接清理完成后仍可执行，UNKNOWN 与连接清理事实分开。
- 显示前、已显示和 FIFO 排队时取消 TUI 后台 coroutine，均核对本命令等待退出、其它面板与前台回合保留。
- 单帧有界、缺失/异号消息拒绝；组件服务退出后核对心跳及审批线程无残留。

开发中保留并修正了临时夹具的工作目录权限、事件时间和取消令牌复用断言，以及旧输入测试的运输替身。
断连用例须等待原 HTTP 执行区间结束后再测新连接，不能把原业务终态误当成资源已经退出；没有放宽原资源准入。
只读复核发现的规范 owner 地址分叉已修复；既有通用控制器连续复用问题在本片单次审批链不可达，未混入修改。
本地 Ruff、文档同步、严格尺寸、diff 与 clean-package 均通过，尺寸基线未调整。
首次包检查因新增文件尚未纳管阻塞，明确纳管后通过；本片未推送部署，尚未新增实际 TUI，线上 CI 不作为验收来源。

建议下一步：准备首个 workspace-peek 可安装样本，检查并发布同一宿主安装包，再用管理、插件业务和核心任务三路实际 TUI 验收。
主代理负责实施和环境操作，适合子代理并行只读复核；Jev 工作区独立，发布和重启前另行同步。

## 第 4 步显式业务调用与原审批开发验证

本片只完成本地命令服务、原执行器及 MCP 组件接线；TUI/Gateway 的交互审批运输尚未完成，没有新增实际 TUI 或模型调用。
最终相关 17 个文件 **299 passed、0 failed、0 error、0 skipped**，受测 2,245 个 Python 文件前后摘要一致。

- `/plugins@插件ID` 使用公共参数绑定、原 HostCommand/ToolExecutor/MCP；临时 wheel 服务实际拒绝额外宿主参数，并记录真实调用次数。
- 宿主请求 v2 分开保存工具输入与目录/工作根/权限摘要，身份索引保持原算法；v1 严格只读，坏版本、缺字段和同编号换上下文拒绝。
- 明确批准只执行一次；拒绝、取消、无消费者、错误决定及不支持的会话批准都不执行，原请求重放不重新询问或打开服务。
- 审批等待时重复提交返回原 running；另一管理调用完成停用、释放和重新启用后，旧批准仍不能调用任何一代。
- 卸载后业务结果仍按原身份查询和重放；普通用户可读自己的业务请求，管理员装卸结果及其他操作者请求保持隔离。
- 取消本次业务连接时，另一个 Registry 的同代连接和内置工具仍可用；握手中的外部取消事实经原 token 及时结束等待。
- 仅宿主明确的自主模式可免交互批准；连接清理回执丢失不改写已保存的业务成功，也不重跑原 handler。
- 原管理、Gateway、客户端、停用、释放、卸载、旧资源准入及操作 UNKNOWN 路径一并定向回归；没有新增第二套执行或结果账本。

首轮保留两处失败记录：新增测试误查数据库列名，修正查询；普通用户的缺失请求查询曾由原权限拒绝变成 not_found，已恢复原拒绝语义。
只读复核另发现 MCP 准备未绑定命令 token，已复用原 `bind_cancellation_token` 并新增初始化等待中的取消回归。
这些组件通过不算完整插件真实验收；本地相关测试、Ruff、文档同步、严格尺寸、diff 和 clean-package 均通过，尺寸基线未调整。
本片未推送部署，线上 CI 不作为验收来源；没有因本片小切片重复运行无关全仓。

建议下一步：接原 TUI 审批队列和 Gateway StreamApproval，命令使用独立取消寿命；断连只查询原编号，不自动重发。
主代理负责写入与环境控制，可并行只读审阅运输边界；接线及发布检查通过后再做管理、插件、核心多路实际 TUI。

## 决策模型后续验证计划（配置合同以首节为准，其余待执行）

[可选决策模型计划](docs/design/DECISION_MODEL_INTEGRATION.md#11-验证矩阵与完成标准)列出后续合同、假服务、
replay 与少量真实 TUI 顺序，重点是 2/4 秒总期限、零叠加重试、未退出资源有界、故障沿原流程、
迟到结果失效、记忆游标、模型窗口与完整费用。全部是计划，不能计入现有通过数量或替代插件验收。
补充双入口配置验收：用户与 agent 修改同一时间/开关、按点覆盖及恢复继承、版本冲突、在途请求不延长期限、
关闭撤销旧建议，以及 Jev 不可用时仍能修改设置；必须核对读回值与后续实际请求生效值。
本段为最初规划记录；当前配置合同结果看首节，后续真实模型、Gateway 与部署尚未执行。

## 第 4 步卸载与重新安装开发验证

本片为本地开发验证，完整真实多 TUI 装卸尚未验收，未推送部署。最终相关 32 个测试文件
**716 passed、0 failed、0 error、0 skipped**，受测 Python 源码前后摘要一致。

- 原管理与工具链从固定目录快照执行卸载：先停用、确认退出并释放，再 CAS 删除准确安装；原 handler 未退时保留安装与环境。
- 同包重新安装后旧目录失效，原卸载重送只读旧操作并补旧清理，不执行新 handler，也不删新安装引用的包。
- 用户产物、来源包及其它插件保留；缺失目标在同一锁内确认，不由目录扫描推断成功。
- 删除提交前、替换后、不可读状态分别保留未提交、已提交与 UNKNOWN；已读回确定删除的刷盘/解锁异常附存储警告，原请求仍重放确定结果。
- 原管理结果提交失败或损坏不回收包；清理失败只标待收尾，查询和功能开关关闭后的重读均不消费，明确重送可补做。
- 包回收与重新安装共用原插件锁，引用检查及删除之间不能插入新安装；包叶子符号链接和坏安装表明确拒绝。
- 临时真实 wheel/venv/MCP 沿原链启用、读取、卸载；旧快照拒绝，隔离环境与包删除，原 Registry 内置读文件工具继续读取同一用户文件。
- Gateway 开/关认证的可信本机入口、管理员与工具禁用、公开目录 v3 严格读取均有组件验证；这不等于完整登录认证验收。

首轮两处测试写法问题保留：UNKNOWN 的公共查询不投影确定 details；内置只读工具须用原 Registry 执行入口，
不能套用依赖副作用操作账的插件管理测试 helper。修正调用和断言后通过，没有放宽产品 UNKNOWN 或权限边界。
首次尺寸检查因管理选择嵌套过深失败，已分离工具构造与执行器组装，原尺寸基线不变。
本轮未重跑无关全仓；Ruff、文档同步、严格尺寸、diff 与 clean-package 均通过，尺寸基线未调整。
首次 clean-package 只因四个新增源码/测试文件尚未纳入 Git 失败，纳入本次提交范围后通过；线上 CI 不作为验收来源。

建议下一步：接通显式 `/plugins@插件ID` 与原审批运输，再同版部署并开展管理、插件业务、核心任务多路实际 TUI。
主代理负责写入和环境操作；可以按明确模块并行只读审阅，不能把开发组件或源码入口调用算作真实 TUI。

## 第 4 步释放与重新启用开发验证

真实验收边界：使用临时原运行账、安装表、原工具执行器、独立 wheel/venv/MCP 与托管进程组件；没有启动真实产品 TUI 或请求模型。
原 enable 身份由首次管理运行和完整资源声明定位；原执行器退出、资源清理、环境删除、安装 CAS 与结果落账分别核验。

- 原 handler 已取消但仍阻塞时只关闭权限和清理已选资源，不删除环境；其退出后新管理请求可以完成释放。
- 实际临时包沿启用、读取、停用释放、重新启用、再次读取和停用闭环；旧工具快照与原 disable 重送不转投或停掉新代。
- 独立环境删除不沿树内符号链接越界，顶层和父链链接拒绝；删除失败保留原 revoked 计划，后续新请求续收尾。
- 原 executor 出生/退出字段的文本、bool、负数拒绝；取消状态或心跳不代替退出事实。
- 原管理结果未保存、后来损坏或仍 UNKNOWN 时保留资源记录；已成功结果的消费失败不翻转原操作，重送只补消费。
- 引用整批验证后才删除；部分 unlink 失败可幂等续做，同 ID 新身份、旧版本和坏证明均拒绝，其他资源保持。
- session v4 固定保留准备记录到显式消费，真实准备记录不会被普通历史裁剪；普通任务原裁剪不变，旧 v2/v3 原版本读写。

定向范围包含插件包/配置/管理/启用/释放/Registry、MCP 生命周期、host command、原生后台进程、通知、任务/子代理停止及 owner 权限。
首轮暴露旧停用断言仍要求保留 revoked/记录，以及新版通知夹具缺少保留字段，已按新合同修正；未给生产代码补隐式默认。
原完整清理证据测试保留，新增精确消费测试独立成文件。最终 50 个相关文件 **1,043 passed，零失败/错误/跳过**，约 144 秒。
运行前后 2,237 个 Python 文件摘要一致；随后仅同步三处既有模块的版本注释，语法树逐项相同。
Ruff、文档、严格尺寸、diff 与 clean-package 本地严格 gate 通过；尺寸基线未改，新增内容隐私检查未发现候选。
卸载、显式插件业务命令及真实多 TUI 尚待完成；本片未发布部署，线上 CI 不作为验收来源。


## 第 4 步实际启用与新运行工具组合开发验证

本片使用实际临时 wheel、venv/pip、托管 MCP 和原 HostCommand/ToolExecutor，不启动产品 Gateway/TUI 或模型。
启用不再依赖假工具发布激活；这仍是组件开发验证，不代替完整装卸及实际多 TUI 验收。

- 原 enable 同一操作完成 preparing、环境准备、完整目录匹配、候选清理和 active；重放不重复启动。
- 名称集合、说明或 schema 不匹配以及服务启动失败均不能启用；私有配置不进入公开结果和日志环境投影。
- 原 Registry 按可信 owner 惰性接入，共享权限视图各自投影；实际工具读文件、停用和旧快照拒绝沿原执行器验证。
- 插件/工具开关与管理员拒绝不创建环境；真正 scoped Agent 的 owner 注入及构造不启动插件分别验证。
- prepare 与 close 的两处登记交错中，迟到客户端保持关闭；持久未知资源阻止新连接，拒绝候选清除自己的退出回调。
- 自然退出码、时间和原 child 回执保持；完整 cleanup 原账保存，迟到写入不能擦除或降级。
- 清理证据写入前故障保留 UNKNOWN；已提交 redo 后的安装故障恢复原证明。PID 消失本身不证明整树退出。
- 旧连接选择及请求/写队列拒绝均记录 not_started，真正发出后的丢响应保留 unknown；原执行器不再留下虚假未知操作。

初测修正了夹具旧属性、公开结果/持久正文位置及快照 hash 绑定；没有放宽产品权限。
只读复核发现迟到登记与关闭交错，以及自然终态遗漏完整清理证明，均在原连接/资源边界修复。
故障测试原先要求保存失败后停用必定成功，已改为按原终态证据裁决；另用确定性原记录验证 UNKNOWN 保留和 redo 恢复。
首次 47 文件回归只有一处旧 MCP 注册夹具缺少关闭状态，已按真实初始化字段补齐，未给生产逻辑增加兼容回退。
最终同组 **1,018 passed，零失败/错误/跳过**，约 132 秒；运行前后 2,230 个 agent_py_agent Python 文件摘要一致。
本地严格 gate 通过：Ruff、文档同步、严格尺寸、diff 和 clean-package；尺寸基线不变，新增内容隐私检查通过。
释放/消费、重新启用、卸载及显式插件业务命令仍待完成；本片不推送部署、不新增实际 TUI，线上 CI 不作为验收来源。

## 第 4 步管理停用与双归属清理开发验证

真实验收边界：本片使用临时原 RuntimeDB、HostCommand/ToolExecutor 和托管 OS/MCP 组件。
假启用工具只提供精确原操作及激活夹具，不证明实际包启用、完整工具目录匹配或真实 TUI/模型验收。

- 管理 disable 先撤销原激活，按持久 operation 引用反查原首次准备运行，并核验完整资源声明；关闭准备权限后分别冻结两类资源。
- 准备中/已发布、原 handler 在途/已结束、原请求查询重放及权限/目录拒绝均通过组件验证。
- 准备资源的显式终态包含模式可核对仍可能存活的 host；普通 task stop 默认行为保持。
- 阻塞 MCP 调用期间管理停用完成；另一插件和独立业务任务继续运行，原资源和激活身份未换绑。
- 独立进程运行原 host，在实际 child 准入前后暂停：管理先提交 revoked，再等原资源锁；前者拒绝创建，后者新 child 被完整冻结清理。
- 原请求链/资源声明损坏不能改停其他运行；清理不确定保留原 UNKNOWN，后续新的明确清理成功不改写旧结果。
- 原资源集合读取覆盖乱序、重复、缺失和附加声明；不排序改写历史 claim。

初测修正两处夹具断言：运行权关闭实际抛 AuthorityContextMissing，关闭连接后应使用预先冻结的资源地址；未放宽产品行为。
新增测试一度导入错误的 holder helper 路径导致收集失败，已改回原 local_storage 定义。
只读复核发现新资源查询误按排序列表比较旧 claim，已改为严格字段检查后的原集合语义，并用真实原 claim 回归。
最终 39 个相关测试文件 **899 passed，零失败/错误/跳过**，约 96 秒；前后 2,223 个 agent_py_agent Python 文件摘要一致。
Ruff、文档同步、严格尺寸与 diff 检查通过，尺寸基线未改；首次 clean-package 仅报告本批新增文件尚未跟踪，纳入 Git 后通过。本地严格 gate 与新增内容隐私检查均通过。
原 revoked 激活和退出记录仍保留；release/consume、完整 enable/re-enable/remove 及实际多 TUI 尚未完成。
本片未推送或部署，未调用真实模型，线上 CI 不作为验收来源。

## 第 4 步托管 MCP 与鲜活准入开发验证

本片仅使用临时原安装表、原 RuntimeDB 和真实隔离进程，不启动产品 Gateway/TUI，也不连接模型。
静态清单与环境是组件夹具；它们不证明完整插件包启用或工具目录匹配，完整管理和多 TUI 仍待完成。

- 可信 owner/root/激活引用在独立进程复查原表，路径错配、缺失、损坏、旧代和撤销均拒绝。
- launcher 预留、启动及交接与 host 创建 child 前共用原资源锁检查；握手/发现允许同代 preparing，业务必须 active。
- 托管二进制管道由唯一文本读取端解码，实际 Unicode/大内容、stderr、原 host/child 精确退出验证。
- 原请求/写队列之后再次准入，撤销代不发帧；旧代理冻结原 transport，不追随重连。
- 单次权限拒绝不关闭共享连接；原 executor 已领取后仍保留未发送事实，writer 已启动的失败继续 UNKNOWN。
- 原目录锁的线程和跨进程等待可取消；取得锁后的 redo/提交不可被等待回调截断。
- 原 ProcessSessionCleanup 和提交异常继续传递；未接管启动的未知结果也阻止重启，不改报未开始。

初轮夹具拦截 Popen 时误拦了出生标识所需 ps，已限于目标 child；结果断言改为原 ToolHandlerOutcome 的 ok/output 字段。
stdio launcher 消失用例首次在 host 提交 child 终态后、host 自身退出中执行收尾，原终止回执保守返回身份未知。
该用例现另外观察 host 自然退出后再做收尾，没有放宽生产清理或把首次 UNKNOWN 改报成功。
只读复核指出未发送事实没有进入原操作账，已通过结构化 effect_outcome 传递，不能按超时/取消文案猜是否执行。
最终 28 个相关测试文件 **681 passed、零失败/错误/跳过**，约 72 秒；前后 2,217 个 agent_py_agent Python 源文件摘要一致。
原执行器用例核对同一 operation 从 EXECUTING 收口 FAILED，结构化结果为 not_started；writer 启动后的故障仍 unknown。
首次严格尺寸检查发现发送函数嵌套过深，已将本次权限异常分类提到协议模块，未改尺寸基线；之后重跑同组 681 项通过。
Ruff、文档同步、严格尺寸、diff 与 clean-package 全通过；新增内容隐私检查同步执行。
本片不发布部署、未新增实际 TUI，线上 CI 不作为验收来源；完整管理启停与多 TUI 仍待完成。

## 第 4 步托管 stdio 与共享资源归属开发验证

原后台启动/host 已有显式 stdio，session v3 区分任务与共享激活，旧 v2 原版本保留；首轮 6 个文件 **182 项通过**。
仅使用临时合同记录和隔离 OS 子进程，不运行真实模型或产品 TUI。

- 大于管道容量的任意字节原样往返，stdout/stderr 分开，协议字节不写任务日志。
- child 关闭输出后即使继续运行，读方仍可观察 EOF；host 释放重复端点，自然退出码保持。
- 交接前取消回收原 child 和未交出管道，交接后 launcher 消失由原 host 清理；不关闭其他任务。
- 两个激活代次与普通任务同时运行；按激活只停止准确代次，按任务不选择共享连接。
- v2 原 redo 恢复、原版本更新及新旧实例复用拒绝；v3 禁止混绑 owner/业务身份或替换激活。
- 普通模型即使知道精确 session 也不能查询或停止共享连接，普通历史裁剪保留其终态证据。

只读复核发现提取 Popen 后，日志上下文关闭失败可能发生在外层取得句柄之前。
已改为上下文只准备端点，外层先保存 child 再关闭日志；真实子进程的关闭故障注入通过，未增加清理旁路。
最终联测原 Shell、任务/子代理停止、环境准备、宿主操作与 MCP，共 20 个文件 **523 passed、零失败/错误/跳过**，约 59 秒。
运行前后 2,276 个 Python 文件摘要一致；旧 v2 和当前 v3 的 PID 复用、未知 host 丢失及清理分支分别覆盖。
本地严格 gate 通过：Ruff、文档同步、严格尺寸、diff 与打包检查；尺寸基线未改，新增内容隐私检查通过。
本片未使用线上 CI，没有发布部署或新增实际 TUI。
完整安装表准入、调用及装卸仍待接通，不进入第 5 步；上述组件与回归不是实际多 TUI 验收。

## 第 4 步唯一激活权威开发验证

安装、配置与激活共用原表 v3、原插件锁和原子提交；领域首轮 **103 passed、零失败/错误/跳过**。
测试仅使用临时私有记录、线程和独立 Python 进程，目录摘要为合同夹具，不代表实际插件已启用。

- 同一计划预留、发布、撤销与逐次精确重放；旧快照拒绝、撤销不可恢复、未清理前不能改配置或准备新代。
- 发布和撤销争抢同一版本、两个独立进程竞争预留；一方成功，另一方明确冲突，不丢更新。
- 撤销可在另一线程持有原 owner quota 时完成，配额不足不妨碍关闭已有代次；新资源仍沿原配额准入。
- v1/v2 显式迁移，查询不写盘；v2 原配置及已有 v1 来源保留，坏 v3、错绑回执及错误目录摘要拒绝。
- 各阶段提交前、提交后和读回失败分别保留未提交、已提交与未知，不伪造清理成功。

首轮旧 v1 测试夹具因沿用新表字段失败，改为完整旧字段后通过，未放宽生产解码。
只读复核发现准备/发布的回执操作可与计划不一致，已补严格读回及两个持久反例。
最终联测安装、配置、原操作、环境准备、命令目录及 MCP，共 20 个测试文件 **549 passed、零失败/错误/跳过**。
使用项目现有 Python 3.12 开发环境，约 25 秒；本片尚无真实 TUI、模型调用或部署。
本地严格 gate 通过：Ruff、文档同步、严格尺寸、diff 与打包检查；尺寸基线未改，新增内容隐私检查通过。
末次 Ruff 发现移除顶层依赖后的导入间距，修正后语法树未变；此前失败保留。线上 CI 未作为验收来源。
stdio 管道托管、activation 资源归属和完整启停调用链仍待接线；撤销记录不是 OS 清理证明，不进入第 5 步。

## 第 4 步 MCP 连接生命周期开发验证

本片 6 个相关文件 **163 项通过，0 失败、0 错误、0 跳过**。覆盖 MCP 协议、注册、
Computer Use 的 MCP 装配、原进程树清理和现有协议边界回归；均为开发验证，不是真实模型或 TUI 验收。

- 两层排队与在途请求在关闭后退出；旧连接队列、EOF、通知和超时不能操作重连后的新连接。
- 进程创建返回前关闭保留 `launch_pending`；握手完成但尚未发布时关闭，候选不得变为 ready。
- 永久 stop 拒绝 start/reconnect；临时故障保留原退避资格，清理未确认时保留原 transport，禁止创建替代进程。
- 临时真实 MCP 进程验证：自然退出的未回收组长仍能用于清理同组孩子；stdin/stdout/stderr 与读线程退出。
- 同一连接的分页目录才可发布；关闭或换连接后拒绝旧目录，共享权限视图关闭不会恢复原客户端。
- 发现失败且清理抛错时，其他服务和核心准备流程不被中断；异常清理仍保留未知，没有改用全部 stop。
- 原工具发现、完整内容与 Schema、调用超时、取消通知和写入背压回归保持。

只读复核指出并修复：未确认清理仍重连、可用性提前回收组长、管道漏关、排队期限计算窗口和清理异常隔离。
首轮新增测试因假进程缺少 stderr 字段失败，修正夹具后通过；原失败日志保留，未为夹具增加生产兼容分支。
累计未发布 Python 增删接近一万行，追加全仓验证。首次收集因系统 Python 缺少已声明的
hypothesis/pyte 失败，随后使用项目现有完整开发环境，未修改日常运行环境。
首轮全仓运行到 11,852 项时记录 191 项失败；定位到此前权限拆分后，工作片入口仍导入已删除的 core helper。
修复直接调用原 owner_access 权限裁决，不恢复兼容转发；正常、Full Access、子代理继承和退出恢复的
定向验证为 103 passed、2 项既有 xfail。另将两组旧命令夹具接到实际冷管理依赖，并断言非管理员安装的
结构化权限拒绝，相关 171 项通过；模型和旧控制器不调用、原会话不变、静态命令不建目录的检查保留。
原失败和中断记录保留；最终固定源码全仓 **18,265 passed、35 项既有 xfail、21 项 skip、
5 项既有标注 XPASS，0 failed/error**，共 18,326 项，约 828 秒。运行前后 2,275 个 Python 文件摘要一致。
本地严格 gate 通过，尺寸基线未改；新增内容隐私检查通过，线上 CI 未作为验收来源。
打包检查首轮因四个新增文件尚未纳入 Git 失败，将本片文件纳入后通过；未删除源码或降低检查规则。
本片未发布部署、未新增实际 TUI。插件持久激活、静态清单完整匹配、审批后撤销及完整装卸仍未完成，
不能进入第 5 步；边界见 [MCP 连接合同](docs/design/MCP_TRANSPORT_LIFECYCLE.md)。

## 第 4 步环境准备归属开发验证

本片 16 个相关测试文件合计 **420 项通过，0 失败、0 错误、0 跳过**。
覆盖原环境/管理组件、后台启动交接和 ProcessSessionStore、原操作权限及宿主命令重放；临时 OS 进程不等于产品 TUI 验收。

- 固定计划先作为一个 logical scope 写入原 operation，准备前核对包/解释器，运行中核对原 claim、owner/task/run/attempt/tool、epoch 和资源锁。
- 原 ToolExecutor + 临时 RuntimeDB 内实际完成 venv/探测/pip；三个进程使用同一原 attempt，原请求查询及重复提交不再次准备。
- 同步准备复用原托管进程账，不再裸启动；配置、安装状态及激活不因此改变，没有新增完成唤醒。
- 原后台链验证交接后启动方崩溃、host 自行实施截止时间、非法/过期期限；普通长期后台默认语义回归通过。
- 真实临时命令交接后撤销原 attempt，准备器只停止其精确 session，读回 child 退出；取消/清理未知和记录不可读仍保留未知。
- 复核发现并修复“第一次读取 running 后 host 刚完成退出”的竞态，退出后复读同一权威；另修复启动后超时丢失 started 事实。对应交错测试通过。
- 初次撤销组件断言把原 AuthorityContextMissing 错写为 RuntimeConflictError，修正测试期待后通过；生产错误类型和清理路径未因此改动。

本地严格 gate 已通过：相关 focused tests、Ruff、文档同步、严格尺寸（基线不变）、diff 与 clean-package；线上 CI 未作为验收来源。
完整启用命令、MCP 目录发布、精确撤销和装卸尚未接完；本片未发布部署、未重启 Gateway、未调用真实模型或新增实际 TUI。
下一次实际多 TUI 验收仍在第 4 步完整候选上执行；本片不关闭该验收项，不进入第 5 步。

## 第 4 步私有配置开发验证

配置命令沿原 ToolExecutor/ManagedOperationStore、RuntimeDB 和真实文件锁在临时 owner 验证，不启动插件或真实模型。
覆盖完整 JSON/schema 严格校验、64 KiB 预算、私有值不进入工具参数/结果/目录、重复请求、来源删除后的查询、
相同值不改版本、过期目录拒绝、锁内 CAS 竞争、配置与包/回执绑定、写入前/后及结果不可读的提交分类。
v1 只读不迁移，首次真实变更同次提交 v2 及原字节摘要；矛盾的 install 回执携带配置、坏迁移与缺失 v2 字段均拒绝。
HTTP 原认证和明确无认证回环、direct 客户端、单独禁用配置工具、功能关闭后的原查询均沿原入口验证。
源码复核发现 install 回执可搭配非空配置的坏账缺口，已拒绝并补定向回归；正常写链不会产生该组合。
首轮只有共享来源读取搬移后一个旧 monkeypatch 位置失败，已迁到实际公共入口；不归因为产品或模型失败。
扩大相关回归后，16 个 direct TUI 分流用例的旧替身缺少现行 owner/会话依赖；已补真实轻量上下文并增加配置帮助场景，
保持不进入普通聊天、插话、控制和任务队列的原断言。补夹具时一次不存在的路径属性已改为原 owner 解析，不改变生产入口。
本轮没有新增实际 TUI、真实模型或发布部署；全流程多 TUI 验收须等启用、调用、撤销和卸载接通。
最终 21 个相关测试文件共 614 项通过，无失败、错误或跳过；包含原宿主操作、工具 schema、owner 配额、
独立环境组件、HTTP/direct 和 TUI 输入分流。此前定向重跑不另加计数，不以组件或替身输出来声称实际 TUI 验收。
本地 Ruff、文档同步、严格尺寸、diff、clean-package 全部通过，尺寸基线未改；新增内容隐私扫描无命中。
累计未发布 Python 增删为 7,546 行，未达到全仓频率门槛，使用相关定向集；线上 CI 未作为验收来源。

## 第 4 步独立环境开发验证

内部准备器只处理原包快照和宿主操作身份，不发布启用；标准 venv/pip 的真实组件调用只发生在测试临时目录，不是真实产品 TUI 验收。
覆盖 wheel 元数据、Python/平台标签、直接 URL 拒绝、活动依赖/extras/循环依赖闭包、RECORD、目录/成员预算与跨依赖覆盖。
解释器、安装器、生成脚本和文件/目录冲突均在调用 pip 前拒绝；共享 namespace 的不同文件仍可安装。
中文空格路径下两份本地 wheel 的离线安装、headers 与入口脚本读回通过；模块和 `.pth` 陷阱未执行，原安装表保持停用。
同操作不重建候选，失败目录不冒充成功；原配额不足和锁竞争在候选创建前拒绝，旧锁的默认等待行为保留。
取消、超时、出生标识读取失败、通信异常、清理异常以替身验证原身份收尾，退出未知不改报成功。
初轮发现当前 macOS Python 的复制式 venv 子解释器 SIGABRT，采用标准链接式 venv 后组件复验通过；
两项新测试的冷 owner 目录夹具缺失已修正，不将夹具错误当作产品故障或隐去初次失败。
只读复核发现的 Unicode 等价路径别名和安装后 RECORD 扩张问题已修正；单 wheel/跨 wheel 别名均拒绝，
临时 pip 实际安装 200 个入口脚本与签名成员，独立记录预算及完整目标/摘要核对通过。
后续复核仍发现按声明长度估计不足以覆盖短名称；改为按实际允许目标数、路径字节及每行固定开销计算，
增加 4,000 个短入口的真实临时 pip 组件并通过。压缩平台标签在公共库展开前检查组合预算，安装器保留元数据及其大小写别名明确拒绝。
两路独立只读复核各守环境进程/锁和 wheel 内容/布局边界，代码写入由主代理单独负责。
最终 14 个相关文件 319 passed、0 failed/error/skip，含 6 项文档回归；受测 Python 文件摘要与最终源码一致，中间重复执行不累加。
本地严格 gate 与新增内容隐私检查通过；原尺寸基线未改，未发布 Python 增删累计 6,736 行，未达追加全仓阈值。
clean-package 首次因新文件尚未登记而失败；审查并加入 Git 后复验，未放宽包检查规则。线上 CI 未作为本片验收来源。
本片未调用真实模型、未新增实际 TUI、未发布部署；Windows、所有 Python 发行版、产品启用和撤销均未验。
原 `/stop` 的 TUI 138—142 结果保持独立；配置、激活、MCP 和完整多 TUI 装卸仍待完成，不能关闭第 4 步。

## 第 4 步管理执行与只读查询开发验证

本地 HTTP/direct 管理入口复用原认证、owner 路径、线程与工具执行器；临时 owner 上保存真实合成包，默认停用，不导入或运行插件。
覆盖原请求执行、并发重送、执行中查询、来源删除后的回读、普通用户拒绝、工具禁用、开关关闭及原结果仍可查询。
YAML 默认与 dataclass 一致，带引号的 false 经原布尔规范化后实际关闭管理；不只验证配置字段存在。
来源路径拒绝越权、链接、损坏包和缺失文件；一次字节快照不会在来源变化后重读，严格打开不支持时明确失败。
FIFO 替换检查非阻塞打开标记后再执行系统调用，不以可能永久阻塞的测试验证拒绝；普通 portable 文件行为保留。
配额满时不发布包或安装表；静态目录损坏不抹掉原执行结果，网络超时保留请求编号与未知，不自动重放。
隐藏管理工具仅由宿主免除模型可见性检查，模型默认仍拒绝；参数规范化改变摘要在原操作领取前拒绝。
原执行器使用同一 ManagedOperationStore；未知副作用保留 UNKNOWN 与锁，不重进 handler。
开发故障注入分别覆盖读原操作、关闭 AgentRun、关闭 TaskRun；已知工具结果附收尾未确认，显式重送只补原收口。
前置拒绝已落原事件但 TaskRun 关闭失败也可恢复；只读状态查询不初始化/迁移数据库，不激活 pending、不写收口或对账。
先前可见性、禁用策略与收尾遗漏已修正后复验；测试夹具导入错误保留为开发失败，不算产品故障或通过结果。
最终 33 个相关文件共 771 passed、1 skipped、0 failed/error，含 6 项文档回归；中间重复运行不累加。
跳过是原 `TestGatewayHTTPIntegration.test_stop_endpoint` 遇 HTTP 409 主动 skip，不算停止或服务退出验收。
本地严格 gate 全部通过：Ruff、文档同步、strict code-size、diff 和 clean-package；新增内容隐私扫描零命中。
尺寸 hard=0，原基线未改；累计未发布 Python 增删约 5,200 行，未达全仓阈值，没有追加全仓测试。
本片未发布部署、没有新增真实 TUI 或真实模型。完整独立环境、配置验证、激活和撤销尚未接通，不能以这些检查关闭第 4 步。
实际装卸仍按计划用多 TUI 并行核心任务验收；原 TUI 138—142 停止结果保持独立，线上 CI 未作为本片验收来源。

## 未启动取消记录的精确回读开发修复

源码复核发现原 Repository 在取消未启动 `CLAIMED` 占位时覆盖整个结果对象，丢失输入指纹与幂等身份。
已改为保留原领取字段，仅更新规范取消结果、错误字段和时间；未知扩展、持有者及资源声明原样保留。
运行结束、单轮结束及崩溃恢复三个真实事务入口之后，均通过原 ManagedStore 精确读取并重放取消回执。
重复停止不改原回执，其他运行/尝试、已开始、UNKNOWN 和已有终态操作不被改写。
损坏 JSON、未知版本、非对象、重复键、非有限数、过深 JSON 及 SQLite BLOB 保留原存储内容；
执行权可依据关系型未启动事实关闭，但完整与旧三参数查询均明确拒绝坏结果，不编造输入或成功回执。
当前 ManagedStore 的领取直接进入 `EXECUTING`；本片使用合法持久 `CLAIMED` 夹具，不能描述为新 TUI 停止失败复现。
最终 12 个相关文件 360 passed、0 failed/error/skip，含 6 项文档检查；初始失败与中间重复运行不累加计数。
只读复核提出的深层 JSON 与非文本存储边界已补齐；本地严格 gate、隐私检查及发布包检查通过，尺寸基线未改。
累计未发布 Python 增删未达全仓阈值，没有追加全仓测试，线上 CI 未作为验收来源。
本片仅本地源码与开发检查，未发布部署、未运行新的实际 TUI 或真实模型；原 TUI 138—142 停止验收和第 4 步未完成状态保持。

## 第 4 步宿主命令身份与结果回读开发验证

本地源码把显式管理请求绑定到原 RuntimeDB 的独立 pending 运行，普通代理和宿主命令共用同连接创建逻辑。
四个独立 Python 进程并发提交同一请求，只登记一棵运行树；同消息更换输入拒绝，登记事件写入失败整体回滚。
原链断裂、跨 owner/操作者/通道/线程、旧尝试与终态重送均有检查；事件索引依赖原 append-only 保留，不承诺识别手工删库。
假工具经过原 ToolExecutor、ManagedOperationStore 和实际执行区间登记；任务结束后只读原结果，不开新 attempt、不再次进入 handler。
原 owner/task/run/attempt、工具与参数摘要分别核对；失败与取消可重放，截断、错类型、状态矛盾及 UNKNOWN 均拒绝成功投影。
本次 handler_executed=false 与保存的原执行事实分开；原对账引用和嵌套结果不丢失、不被修改。
同进程执行器已退出但 Gateway 进程仍活着时，未决操作使原 attempt 保持 UNKNOWN、原锁保留，重送不重跑。
创建链只读复核确认普通 main 默认 running、子代理 pending、父子共享树及委托/事件顺序保持；没有新增任务调度投影。
审阅发现的重放执行标记和结果校验漏洞，以及既有对账引用回归失败，均已修复并纳入复验；早期测试夹具错误不计作通过。
最终 18 个相关文件共 429 passed、0 failed/error/skip，含 6 项文档检查；另两位代理各自只读审阅不同实现范围，未代跑产品或模型。
本地严格 gate 已通过：Ruff、文档同步、strict code-size、diff 与 clean-package；尺寸基线未改，新增内容隐私扫描零命中。
累计未发布 Python 增删为 3,353 行，未达全仓阈值；文档收尾后再次核对不累加测试计数。
本片没有 HTTP/TUI 管理授权、完整执行收口、环境安装或新实际 TUI；不能据开发检查宣称装卸链已验收。线上 CI 未作为验收来源。
继续接授权、来源读取、原操作执行与结果未知时的查询，再贯通隔离环境和完整多 TUI 装卸；十步 Goal 仍 active。

## 第 4 步安装事实开发验证

本地源码增加 owner 唯一安装表、默认停用记录、版本 CAS 和最后提交回执；包先保存，安装表再原子发布。
公共系统锁沿原后台锁名与顺序，严格 JSON 与受信根文件原语共用；没有管理命令、wheel 安装或插件进程。
合同覆盖冷查询不初始化 owner、跨 owner 拒绝、同请求重放、新请求无改动、旧版本冲突、同版本不同字节冲突、损坏状态保留，
以及提交前/替换后/不可读结果、提交与解锁双故障、已提交请求遇损坏包时原回执保留。
六组独立 Python 进程分别在 native 和强制 portable 路径竞争：不同插件、同插件不同请求、同一请求重放；不是只使用线程替身。
锁叶子链接/硬链接/非普通文件、目录在打开后被替换、portable 创建竞争后重验路径均有检查；没有实际 Windows 主机验收。
共享文件原语连带验证原产物注册、Shell 备份/恢复、后台进程记录与停止、owner 路径及插件目录客户端。
开发中首次并发检查失败已定位并修复：本机非排他 `O_CREAT` 同名竞争可返回 ENOENT，极小独立复现后采用排他创建、已存在则打开原锁。
只读审阅发现的提交分类覆盖、锁链接与 portable 建目录竞争也保留为针对性回归；失败没有作为通过结果。
最终 16 个相关文件共 425 passed、0 failed/error/skip，其中 6 项为文档回归；重复运行不累加计数。
本地严格 gate 已通过：Ruff、文档同步、strict code-size、diff 和 clean-package 全部通过，尺寸基线未改。
本片累计未发布 Python 增删未达到全仓阈值，没有重复全仓；线上 CI 未作为本地验收来源。
本片未发布部署、没有新增实际 TUI 或真实模型调用，不代表第 4 步验收完成；下一步先接管理授权与原操作链，再做隔离环境和完整多 TUI 装卸。

## 第 4 步静态包读取开发验证

本地源码首片包含包描述、有界 ZIP 读取和共用命令 JSON 读取器；未发布、未部署，也没有新增实际 TUI 编号。
7 个相关文件为 206 passed，另有 6 项文档检查通过，共 212 passed、0 failed/error；包括旧目录 wire/摘要、HTTP、客户端和 TUI 键盘管道回归。
静态包合同覆盖同一字节快照、摘要篡改、伪造宿主字段、动作目标、schema、路径逃逸、跨平台重名、链接和非普通文件，
以及重复 JSON 键、非法 UTF-8/孤立 surrogate、非有限数、归档/展开/单文件/描述/目录/成员预算。
损坏 DEFLATE、加密标记和非法文件名归一化为结构化错误；FIFO 在等待写入者前拒绝。
中央目录在 `ZipFile` 分配完整成员前有界预检，伪造较小 count 也不能绕过真实数量预算。
普通尾部未设置 ZIP64 哨兵也要拒绝实际 ZIP64 locator，防止标准库跳去另一段未预检目录；小型内存反例已覆盖。
这些是开发合同和组件证据，不证明 wheel 可安装、设置值有效、环境隔离、MCP 发布或真实热装卸。
该首片之后的安装事实进度见上一节；管理授权、激活和精确撤销仍待接通，再按第 4 步计划做真实多 TUI 验收。
首轮误用系统 Python 缺少已有开发依赖，未执行完整集合；切回项目 `.venv` 后完成上述检查，没有更改产品依赖。
本地严格 gate 已通过：相关回归、Ruff、文档同步、strict code-size、diff 和 clean-package 均通过，尺寸基线未改。
本片不触发全仓 pytest，也没有线上 CI 验收。

## 第 3 步宿主目录与实际 TUI 151—153

源码 `3730497fc` 已发布 main，并以同一 non-editable wheel 部署双机，每端 1,219 个包文件逐项一致。
默认入口与各自唯一 Gateway 同版，原配置、完整环境和除 runtime 外的参数不变，旧版本与回滚保留。
本机首次切换因记忆整理租约仍活动而推迟，待其自然结束后正常停启，没有强停；系统网络未改。
既有 143—150 属于前一发布版本；以下开发验证与新版实际验收分别记录。
合同回归覆盖声明 JSON 往返、类型/摘要损坏拒绝、owner/会话/版本/激活变化、冷用户不初始化及不写线程。
HTTP 替身覆盖可信 user/channel 覆盖伪造值、跨 owner 版本拒绝、旧版本/缺失业务版本拒绝，以及三个入口不进入原控制与模型。
这不是完整 auth 验收；原无中间件可信本机身份和群聊路由规则保留。
客户端覆盖 thin Gateway、完整 Agent 的 plain Gateway、direct 三模式；离线/损坏不本地兜底，旧响应和换作用域不污染缓存。
真实 prompt_toolkit Buffer 的开发用例覆盖候选版本跨参数编辑与刷新保留，后续 Tab 接受参数候选也不能换成新版本；
网络在事件线程外执行，逐字符与核心命令不请求目录，提交错误不进入聊天、guidance 或旧停止分派。
完整 Application 的键盘管道用例另验真实 Tab 接线、Ctrl-S 手动/提交后恢复、Ctrl-R 取消及清空后原版本提交；
草稿沿原 TuiDraft 携带版本，Buffer.reset 不发正文事件时显式清除绑定。组件测试仍不算实际模型 TUI 验收。
上述替身与组件测试不冒充实际 TUI；新版宿主帮助、输入编辑、活动任务隔离及普通工具链的实际证据见下表。
动态插件目录更改、在途撤销和实际业务权限留到第 4—6 步，不用空目录通过替代。
本片 22 个相关文件共 669 passed、1 skipped、0 failed/error；原生 HTTP 路由和现有控制回归一并通过。
跳过项是既有 `TestGatewayHTTPIntegration.test_stop_endpoint`，其遇 HTTP 409 后主动 skip；不把它计为服务停止验收。
首轮打包检查仅因 9 个新增源码/测试文件尚未登记 Git 而拒绝，登记后复验通过；没有以未跟踪文件绕过发布检查。
本地严格 gate 已通过：相关回归、Ruff、文档同步、strict code-size、diff 和 clean-package 均通过，尺寸基线未改。
未触发全仓测试阈值；开发验证不改变旧 TUI 或模型产物失败结论，线上 CI 未作为本片验收来源。

三路均使用真实 native TUI，每题只提交一次普通中文业务需求，测试者只操作原生按键与显式命令，未补写产物或执行模型生成的程序。
每路主/子线程绑定官方 MiniMax-M2.7 profile，结合原生往返与成功用量核对；没有逐请求 URL trace。

| TUI | 实际框架与工具证据 | 交付或覆盖边界 |
| --- | --- | --- |
| 151，本机命令与文件 | 15 份按键快照覆盖 Tab、Ctrl-S 手动/另一命令提交后恢复、Ctrl-R 取消及 Enter；5 次帮助/管理/错误提交均静态返回，线程内容不变、任务列表为空。随后唯一业务完成，7 次成功模型调用、6 对工具、锁 0 | 12 条立方数据及 78/6084 汇总、SHA256 正确，完整读回；说明漏掉要求原样保留的句号，模型却声称无误。框架通过，完整交付失败；按键发生在业务前 |
| 152，本机双子代理 | 两孩子各写、全文读自己的 10 行文件；attempt 区间重叠 14.163211 秒。父级在两人结束后才全文读取、合并及实际计算 SHA256，没有替孩子改写。主/子调用 9/3/3 次，工具 10/2/2 对，四个 attempt done、未决操作及锁 0 | 合并为单表头与 20 条数据，总和 210、平方和 2870；summary 的 21 行明确含表头，完整读回和交付通过。不扩大为整个工具阶段并行或长任务验收 |
| 153，测试机活动命令 | 主 attempt 启动后 10.872/12.016 秒提交帮助和错误命令；136.354 秒再次查看帮助时子代理仍在采样。三条命令不进入原生模型消息或 Shell，未取消或重开原业务；主/子成功调用 7/9 次，工具 7/10 对，三个 attempt 自然 done、锁 0 | 60 条真实记录跨度 119.3 秒，CSV 相邻时间差 2.0—2.2 秒，统计和 SHA256 一致，原句保留。脚本用了 `time.time`，未满足单调时钟要求；把可用空间为零解释成沙箱特性没有依据，完整交付失败 |

三路没有模型请求失败或重试，原生工具均配对；模型履约失败保留，不由测试者修正或改成全通过。
151/152 由不同只读审阅者分别复核，root 单独负责实现、部署和 TUI 控制；153 的原始采样、调用与控制时间独立读回。
实际插件贡献仍为空：空目录 Tab 不产生候选，也不提交任务；没有目录 HTTP 逐请求日志，不据此声称实测请求次数、动态贡献或 revision 更换。
真实插件版本变化、在途撤销及业务权限留到第 4—6 步；plain/direct 仍是本轮开发回归证据，不冒充新增实际 TUI 覆盖。
本片未重复旧十五分钟停止/调度用例，其证据与旁支限制按原轮次保留。第 3 步公共命令的本轮框架范围收口，第 4 步尚未开始。
测试机磁盘仍接近满，发布前须继续检查容量；未把模型的沙箱解释当作环境诊断。身份、路径、原始日志和产物仅留仓库外。

## 第 3 步参数合同与实际 TUI 143—150

参数初版 `c1484f7f2` 的真实 TUI 143 复现：输入 `/plugins help ins`、Tab 补全后按 Enter，
输入变成 `help install -h`，没有提交原命令。原失败保留；144/145 未发送业务需求，不计真实验收通过。
修复版 `faa12068a` 区分自动候选与显式 Tab：完整命令不自动补可选旗标，接受候选和 Enter 提交分开。
已发布 main，并以同一个 non-editable wheel 部署双机，各 1,214 个包文件一致、每端一个 Gateway。
默认入口同版，原配置、环境和启动参数除 runtime 外保持，回滚保留；本轮没有修改系统网络。

新增参数合同用例覆盖 Windows/UNC/尾反斜杠、中文与 Unicode 空白、空引用及引用片段拼接、未闭合引号、
长短参数、无值短组合、重复/缺值/错误类型、默认值、多值、保留帮助和 `--`。随机字面值经补全引用后再解析，核对逐值一致。
补全用同一词法/绑定状态，验证默认位置参数、停用插件、未闭合路径引用、等号值及负号字面量，不能建议最终落入其他选项的值。
CLI/TUI 在 idle、foreground、background、child 和 Gateway/direct 组合下消费帮助及错误，不产生聊天、插话、停止或任务队列副作用。
两个 HTTP 命令入口保留鉴权，回执结构一致；普通文件队列仍拒绝插件原文，核心自由正文、控制与模型选择沿既有回归。
修复版 10 个相关文件合计 483 项通过，零失败、错误或跳过；Ruff、doc sync、strict code-size、diff、clean-package 与新增行隐私检查均通过，尺寸基线未改。
475 项是修复前的开发结果，后续重跑不累加为独立覆盖。累计改动未触发全仓阈值，没有重跑无关全仓。
线上 CI 运行列表为空，线上 CI 未作为验收来源；HTTP 与竞态替身不冒充真实插件业务。

实际 native TUI 验收各业务只发送一条普通中文需求，测试者未写产物或执行生成的校验脚本：

| TUI | 实际观察 | 结论与边界 |
| --- | --- | --- |
| 146，本机 | 原生 Tab 接受 `install` 后 Enter 显示其用法，输入清空，未追加 `-h`；13 类帮助/错误均收到对应回执、输入清空且未创建任务 | 补全交互与静态入口通过；之前仅按旧屏幕文字匹配的观察另存，不作为最终证据 |
| 148，测试机 | 同样 13 类命令检查，包含未知插件/动作/选项、缺值、未闭合引号、中文空格路径、反斜杠、短开关、重复及 `--` | 两端合计 26 次检查静态返回；没有模型、任务或 Shell 副作用，不代表装卸可用 |
| 146，本机普通任务 | 12 条编号/平方正确，平方和 650，特殊字符串原样保存；模型保存并运行校验器，exit 0；4 次成功模型调用、5 对工具 | 数据和报告独立读回正确；校验器没有单独断言编号顺序和唯一性，覆盖限制保留 |
| 147，本机两孩子 | 两边各 8 条，合并 1—16、总和 136；父级读回两份源文件后合并；孩子 attempt 区间重叠 12.923484 秒 | 主/子模型调用为 6/3/3 次，工具为 5/2/2 对；三个 attempt 均 done、锁 0，原 worker 已退出；不计长任务 |
| 148，测试机普通任务 | 20 条奇偶标签正确，奇偶各 10、总和 210；5 次成功模型调用、6 对工具；conversation task/claim/run/attempt 收口、锁 0 | 首次校验器将 Markdown 粗体与普通字符串错配，输出失败但 exit 0；模型仅改校验器后再跑。最终数据正确，校验器仍漏检且 False 不转非零退出，不能把 exit 0 当强语义证明 |
| 149，本机普通任务 | 100 行平方表与报告正确，总和 5050/338350，报告原样保留插件示例句；3 次成功模型调用、4 对工具，保存的校验器实际运行退出 0 | 普通正文与工具链通过；两次控制在 final 后 6.49/6.83 秒才提交，只计空闲回执，不计运行中隔离 |
| 150，本机运行中命令 | 同一 attempt 启动后 0.017/0.331 秒提交错误命令及帮助，静态返回、输入清空；原 attempt 自然 done，无取消或换代 | 唯一用户正文未增加，原生记录、guidance 和 Shell 均无这两条命令；覆盖活动请求首秒，不扩大为长工具运行中覆盖 |
| 150，本机普通任务 | 100 行平方及报告正确，合计 100/5050/338350，示例句原样保留；3 次成功模型调用、4 对工具，校验器实际运行 exit 0 | 数据和工具链通过，收口后锁 0；校验器未核验报告，失败分支未转非零退出，模型质量限制保留 |

147 的原任务先于观察结束，运行中命令未发送；149 的观察器错误地等待线程持久 task link，
未能定位前台执行身份，错过活动窗口。这两项是测试准备/覆盖问题，原记录保留，不记产品缺陷或运行中通过。
146—150 的用户正文与原投递一致，原生工具成对，产物与原始写入/编辑一致；模型失败过程没有删除。
实际用量账均为 MiniMax-M2.7，结合每路 canonical profile 的官方 provider/端点核实来源，未获得逐请求 URL trace。
148 的 RuntimeDB `tasks` 身份实体仍为 active，与 conversation task completed 是不同层事实；本轮不扩大裁定该字段语义。
插件参数合同本片不改变停止语义；未重跑旧十五分钟用例，原长任务、停止竞态及未覆盖分支仍按原轮次单列。
动态 owner 目录、版本绑定、调用授权与插件装卸尚未实现，第 3 步整步未收口。原始屏幕、身份、路径和产物仅留仓库外。

## 整任务停止发布与实际 TUI 138—142

源码 `6713c769f` 已正常快进发布 main；同一 wheel 在本机和测试机各核对 1,210 个包文件，默认入口与各自唯一 Gateway 同版。
原启动参数仅更换 runtime，完整环境与私有配置不变，旧 runtime 和回滚证据保留。发布前全仓及严格 gate 见下节；线上 CI 列表为空，不作为验收来源。
测试机首次安装遇到已有磁盘容量不足，失败日志保留；仅清理此前本轮 runtime 中仍有对应源码的字节码缓存后重试。
旧心跳曾被空间不足写空，确认请求、claim、任务及原进程身份后正常停启同一 Gateway 恢复；这属于部署环境恢复，不计活动任务故障恢复验收。

五个独立 native TUI 均从默认安装入口进入，每题仅一次普通中文需求，实际 `/model` 选择官方 MiniMax-M2.7，核对私有 provider 地址与运行记录。
测试者仅操作可见的一次性批准和明确控制命令，没有改业务脚本、补采样文件或代做统计。原始身份、端点、进程出生标识、日志、产物及 tmux 保存在仓库外。
来源核对结合绑定的官方 provider/profile 与成功调用摘要及原生往返；没有逐请求 URL 明细，不把摘要称为逐请求网络账。

| 实际 TUI | 框架验收 | 交付或覆盖边界 |
| --- | --- | --- |
| 138，本机主任务 | `/goal pause` 后活动链及原采样继续；`/interrupt` 后原独立采样继续超过 90 秒；恢复领取第二代并复用原程序；`/stop` 后原 host、Shell、Python 均退出，两代 attempt cancelled、锁为零，69 秒后文件稳定 | 被控制中断，不作十五分钟完成验收；生成脚本读负载为 N/A，模型误归因沙箱，原失败保留 |
| 139，测试机独立长任务 | 140 停止期间原 session 与原进程保持、文件增长；之后自然退出零，未设置停止意图，Goal complete、task completed、claim finished | 程序运行 900.534 秒；299 条连续记录跨度 897.528 秒，间隔 3.004—3.155 秒；全部指标与最近 20 条统计独立重算一致。报告承认相对理想 300 条少 1 条；将最后样本称为结束时间、将采集耗时累积笼统解释为边界效应，属于模型交付偏差 |
| 140，测试机三个孩子 | 原三个活动后台 session 均确认 killed；Store 完成时间在停止提交后约 4.4—7.1 秒，后续 OS 探针确认六个登记进程及一个共享 runner 退出；三个 child attempts cancelled、锁 3→0；两次快照间隔 85 秒，16 项文件不变 | 停止后仍有约 5 秒收尾写入；Store 时间不当作精确 OS 死亡时刻。旧内存采样由模型自行停掉并重跑，排除旧 session；没有真实孙代理 |
| 141，本机交互终端 | 实际采样 PTY 的 Shell、Python 与共享 runner 退出，两个 child attempts cancelled；停止后 95 秒核对原进程消失、采样和进度文件稳定 | 模型实际创建两个兄弟，不计两层分工通过；REPL 和任务 ID 误用保留。已结束的主 attempt 历史不改写 |
| 142，本机真实递归 | RuntimeDB 核实 main→coordinator→worker；停止时孙代理采样运行，随后其原 runner、后台 host、Shell 与采样脚本退出，锁为零；停止后 66 秒快照的文件与前一次一致 | 统筹的 attempt 在停止前已 done，保留该历史；只验证尚活跃孙代理及所属资源取消。脚本颠倒 1/15 分钟负载标签，单列模型错误，不作完整业务通过 |

138、140、141、142 的测试 Goal 保持 paused，139 自然 complete；历史 TUI 与原始失败资料保留，未关闭系统网络、网卡、Wi-Fi 或调整路由和全局代理。
140/139 的固定证据由另一代理只读复核，测试控制和文件写入仍由主代理单独负责；139 的业务数据另做独立只读重算。
这些证据覆盖 Gateway 实际主链；direct/local、无数据库迟到启动、精确启动/停止交错、UNKNOWN 保留及旧轮不复活仍以本轮合同/替身/多进程回归为据。
本轮没有实际 Windows、无数据库 TUI 或恶意拒绝退出进程的验收，也没有把控制中断的样本算成长任务完成。
TUI 137 仍记历史 `FAIL_RESOURCE_STOP`；新修复的所测范围通过，可以继续第 3 步参数解析，不提前宣布插件装卸或整步完成。

以下源码验证各节保留当时的开发过程和覆盖限制；其中未发布、待实际复验的阶段性描述，以本页首节的最新发布记录为准。

## 文件模式启动接纳与旧快照保护（源码验证）

先通过正式取消入口复现两个问题：停止前排队的空身份工作能够重开 user-stop；同一次排队可被两个执行器分别激活。
修复后，显式无数据库模式在原 canonical 的 background_start 中预留非空身份，启动时一次性消费。
独立线程与独立 Python 进程验证同一身份只有一个执行器成功；这是开发测试，不运行真实模型。

覆盖显式恢复换代、业务仍为 CANCELLED 时再次停止、直接同步启动撤销旧排队、预览不预留、原候选身份运输，
以及消费后清空 active 指针和写入 running/finished/failed/reclaimed 标记仍不能重复激活。
旧快照分别来自预留前、激活前和旧 CANCELLED；新预留尚未激活时也不能被旧回收覆盖。
普通全量保存只允许同一原 launch/attempt 单调 reclaimed，原结果与回收的提交顺序不变；正常让出和失败后的新一轮均覆盖。
独立只读审阅未发现本片限定边界内的阻断问题，不能替代运行验收。

相关 60 个文件首轮共 1,251 项：1,245 项通过、5 项既有 xfail、1 项 Linux `/proc` 跳过。
随后补测并修正同身份启动失败诊断被过严拒绝的问题，最终启动接纳文件 50 项通过，重复项不累计为新增覆盖。
全仓首次收集被系统 Python 缺少已声明的 pyte/hypothesis 阻断；改用项目现有、依赖齐全的开发环境继续，未修改日常运行环境。
累计未发布 Python 增删超过约一万行，按仓库要求追加全仓验收。开发环境首轮发现 12 项失败：
CLI 参考缺少内部参数说明、并行 runner 的旧空身份夹具、CLI 越层导入、两项控制错误码未登记，以及 8 项旧 TUI 调用夹具。
启动标记的实际实现迁入原 lifecycle 服务，三个生产调用方直接使用；旧自由函数删除，没有新增转发或扩大导入白名单。
错误码明确区分未确认资源停止与未知控制，两者均不自动重试；其余按现行接口补齐说明与夹具。
修正后的两组定向分别 86、96 项通过；最终同一生产/测试源码完整重跑共 17,703 项：
17,642 passed、35 项既有 xfail、21 项 skip、5 项既有标注 XPASS，零 failure/error；不把 XPASS 再重复计入普通通过数。
未增加 xfail 或 skip，未调整尺寸基线。发布与安装版实际验收状态见 STATUS。
发布前文档 6 项、Ruff、doc sync、strict code-size、diff、clean-package 和全部未发布新增行的隐私扫描通过；
全仓结束后的 Python 文件哈希与受测源码一致。线上 CI 未作为本轮验收来源。
上述早期失败均保留，未通过新增 xfail 或跳过绕过；该源码已发布部署，新增实际 TUI 验收见本页首节，TUI 137 原失败不改写。

## 插话重放预留竞争（源码验证）

先用真实临时 RuntimeDB/邮箱和独立消费线程复现四种失败：初次读到 pending 之后，消息已被预留、
提交、消费或拒绝，旧重放仍按旧状态预留后继。修正后在旧/新 turn 排序锁和原回执锁内先修批次、复读状态，
仅 fresh pending 可预留；回应显示真实状态，通用失效恢复中 reserved 的既有规则保持。

定向覆盖提交/确认批次已落盘但投影失败、DB 预留成功后回执或索引写失败的同消息重试、
新轮 ID 排在旧轮之前/之后、只改绑被重试的一条消息、旧消费者在预留期间不能认领，以及 CAS 拒绝后来 current。
候选新 ID 只用于预先加锁；原 DB 事务提交后才成为持久事实，失败不能假装回滚或删除已经排队的轮次。
15 个相关文件共 398 项：394 项通过、4 项既有 xfail，无失败、error 或新增跳过。
原四项复现失败保留；这些是合同与受控并发验证，没有真实模型调用或新增 TUI。
文档 6 项另行通过；本地 Ruff、文档同步、严格尺寸、diff、clean-package 与新增行隐私扫描通过。
尺寸基线未调整；线上 CI 未作为验收来源。

该片完成时 LOCAL_UNMANAGED 旧排队启动仍缺少可撤销接纳；后续修复与验证见本页首节，源码尚未发布部署。
TUI 137 原失败不关闭，真实多 TUI 验收仍从 TUI 138 开始，提前公布实际 tmux。

## 固定子树停止与跨进程取消（源码验证）

`test_subagent_resource_stop.py` 使用临时真实管理器、RuntimeDB 和资源账本，覆盖主任务、孩子、孙代理及
已完成/失败/UNKNOWN 孩子的遗留资源；停止后恢复的新轮、新孩子、新 session 与其它任务不进入旧清理清单。
独立线程和屏障验证创建中停止、清理不占 creation 锁，以及主 Goal/task/creation 与子 Goal 的实际锁序。
部分权限、后台或 PTY 准备失败仍保留其它已提交成员；后台未确认不返回完整成功，PTY 请求不冒充已退出。

`test_runner_stop_relay.py` 在独立 Python 进程中启动两个受控 worker：主进程没有其本地令牌，原 session 心跳
读取持久取消并在 worker 进程中转交精确中断，另一个 run 保持运行；旧轮恢复后仍不能写新轮。
覆盖注册前取消、自然 done/failed、瞬时读取失败和旧配置投影拒写。
取消恰好发生在“读取取消状态→写心跳”之间时，心跳条件写失败仍继续检查，不能提前结束而漏掉中断。
这些 worker 和进程都是开发替身，不运行真实模型，不是 TUI 验收。

取消域改为窄 mutation 后，创建响应曾读取旧 task 对象而显示 PENDING；现改读最新 canonical。
相关创建、授权、取消、Gateway、本地控制、guidance、runner 和来源生命周期一起复验：
41 个文件共 901 项，895 项通过，5 项既有 xfail、1 项 Linux `/proc` 用例跳过，无失败或 error。
开发过程中旧测试入口/夹具、创建状态回执和心跳竞争的失败记录保留；没有用新 xfail 隐藏回归。
详情页取消不具有同 run 恢复资格，已撤掉基于相反假设的试验测试和实现，未放宽正式恢复合同。
文档 6 项另行通过；本地 Ruff、文档同步、严格尺寸、diff、clean-package 和新增行隐私扫描通过。
尺寸基线未改，线上 CI 未作为验收来源。diff 首轮发现三处空行尾部空白，删除后 AST 不变且复查通过。

此片未发布部署、未调用真实模型或新增 TUI，不能关闭 TUI 137 的 `FAIL_RESOURCE_STOP`。
该片结束时留下插话回执多预留与 LOCAL_UNMANAGED 旧排队启动两个发布阻断项；前者后续进度见本页首节。
下一轮实际验收从 TUI 138 开始，提前公布 tmux；每台单 Gateway，使用官方 MiniMax-M2.7。

## 子代理准确启动身份（源码验证）

`test_runner_start_admission.py` 使用临时真实 RuntimeDB/管理器、受控线程和执行替身：
覆盖旧 pending 在取消/换代后不能激活、UNKNOWN/终态拒绝、两个执行器竞争只有一个取得执行权、
迟到 launch 在 creation guard 外等待后重新核对、旧失败回执不覆盖新任务、回收标记不复活、
可显式恢复的 user-stop 不等于允许旧请求恢复、拒绝的 worker 不发布 session，以及旧心跳不覆盖新轮。
首次 session 写入失败不进入模型，并沿原结果门关闭本次已领取的执行权。
CLI 覆盖宿主真实 argv 构造至 parser/Options/Params 的闭环、普通 watch；后续文件模式补片已统一拒绝空身份。
缺值/重复/越界/watch 混用在构造宿主前拒绝，以及批量标记部分失败只收回自己的记录。
重复创建复用原接纳，宽泛创建参数替身与真实启动接纳测试分开，不为测试放宽生产身份条件。

本片仍未部署、未调用真实模型或新增 TUI，不关闭 TUI 137。完整子树冻结、终态资源、插话回执重放竞态，
以及无数据库模式完整停止/恢复在该片结束时仍待收口，后续进度见本页首节。42 个相关文件去重 757 项通过，2 项既有跳过（Linux `/proc` 与已移除 CLI）；
文档 6 项另列。原首轮旧替身错误、重复启动接纳回归及首次 session 写失败未收口的开发失败均保留，修正后复测通过。
本地严格 gate 通过：Ruff、文档同步、严格尺寸、diff 与 clean-package；尺寸基线未改，新增行隐私扫描通过。线上 CI 未作为验收来源。

## 子代理协调基础（源码验证）

`test_subagent_coordination.py` 使用临时真实管理器/RuntimeDB、受控线程与独立 Python 进程，验证原路径锁的嵌套、
跨线程/进程互斥、不同 owner 独立、异常释放，以及创建/换轮/放弃等待同一短事务。
读取屏障覆盖 abandon 与新轮发布交错；旧 attempt 的废弃不能清掉新指针或并发字段。
授权后续做复读控制终态，旧失联快照不能重排已变更的 runner session。
子代理控制回归覆盖 admission→creation 的插话顺序、停止后的 fresh 拒绝、未新增 attempt/邮箱，以及用另一线程实际取锁证明启动发生在 creation 锁外。
在途孙代理测试改为独立创建/控制线程，避免用同线程重入掩盖真实并发边界。
24 个相关测试文件去重 441 项通过，1 项既有 Linux `/proc` 用例在本机跳过；文档检查另列。
文档 6 项通过；本地严格 gate 通过，尺寸基线未改，新增行隐私扫描通过，线上 CI 未作为验收来源。
新增读取屏障首轮挂在管理器转发入口而非实际 persistence 读取处，修正后重跑通过，早期失败保留。
以上不是实际 TUI 验收；该协调基础切片之后的启动身份补强见本页新条目。回执重放竞态、整树冻结和终态资源仍待完成；
未部署、未调用模型或新增实际 TUI，TUI 137 的停止失败保持，Windows 文件锁未获本片验证。

## 本地主链与未晋升热请求停止（源码验证）

`test_local_run_control.py` 使用临时真实 RuntimeDB/Store 和受控 worker，验证正式运行身份不同于消息编号、
启动前中断、Compact 发布与停止的 barrier 交错、旧句柄/新 job 隔离、已结束句柄不再发信号，以及部分冻结保留已提交清单。
plain/TUI 均传递原绑定回调；结构化中断收口为 interrupted；有 canonical attrs 时任务晋升仍可完成，未管理模式不伪造资源清理成功。
`test_gateway_unpromoted_stop.py` 通过真实 Gateway writer 验证 discovery 后发布的绑定、请求锁与任务锁互不反取、
固定主资源清单不追随恢复轮、错运输代次/过期 attempt 拒绝、未发布和坏绑定不借历史 main，以及纯中断保留独立资源。
已晋升任务读取失败和请求关闭后、获得任务锁前才晋升的交错均返回 unknown；不能声称已暂停 Goal 或已收停持久任务。
11 个相关文件整组 502 项通过；同一生产源码另补 1 项晚晋升 barrier 用例，并强化读取失败断言，相关重跑通过；合计 503 项功能回归，无 skip/xfail。
文档 6 项另行通过。首轮缺少正式绑定的旧夹具失败已修正并保留记录，没有用“只有消息编号”的替身绕过新身份合同。
本地严格 gate 通过：Ruff、文档同步、严格尺寸、diff 与发布清洁检查全部通过，尺寸基线未调整；新增行隐私扫描通过，线上 CI 未作为验收来源。
以上均为开发合同/替身验证，没有调用真实模型或新增实际 TUI；完整子树后台资源仍待接线，未发布部署，TUI 137 的原失败保持。
新一批真实验收继续使用官方 MiniMax-M2.7、多 TUI 和长任务；每台单 Gateway，预先公布实际 TUI 编号及 tmux 查看命令。

## 主执行权取消与持久主任务停止（源码验证）

`test_runtime_run_cancellation.py` 使用临时真实 RuntimeDB，验证原权限关闭、旧 attempt 不跟随新轮、pending 在提交时已激活就拒绝、
真实执行锁保留、attempt 单独 UNKNOWN 与崩溃调和的双 UNKNOWN、任意未知状态拒绝，以及完整子代理取消入口不覆盖 done/failed/UNKNOWN。
`test_task_resource_stop.py` 使用临时原 Store 和清理替身，验证主 run 与其它任务/孩子隔离、固定清单不追恢复轮、
领取后停止再恢复的 barrier 交错、迟到热请求不发新轮信号、Goal 恢复与停止准备串行、PTY 请求失败仍派发已提交后台清单。
本次提交与旧 redo 恢复故障分别注入；旧批次不被当成当前停止目标。PTY-only 回执只表示请求，不声称全部退出。
后台 claim 的早期异常释放中断登记，完成通知仍保留原准入例外；主/子取消、Gateway、Goal 与存储相关回归共同验证。
本片最终 20 个相关文件 735 项通过，无失败、跳过或 xfail；开发中早期夹具和接线失败单独保留，未覆盖事项不改为通过。
文档 6 项另行通过；本地 Ruff、文档同步、严格尺寸、diff 与发布清洁检查通过，尺寸基线未调整，线上 CI 未作为验收来源。
以上是开发合同、替身与并发验证，不运行真实模型，不是实际 TUI 验收；未运行 Codex 参考项目的测试。
该片结束时 direct/local、无持久任务热请求及完整子树仍待实现；前两项后续源码进度见本页首节，尚未发布部署，TUI 137 原失败保持。
跨进程未绑定旧工作片没有由本次线程中断测试证明；Windows 实机和完整停止树也不在本片已验证范围。

## 后台 v2 启动交接与精确句柄清理（源码验证）

实际 Shell 启动已经使用原 v2 Store：预留、host 绑定、child 创建标记和绑定、短命令观察、原执行权限与取消复查、明确交接。
新增 `test_background_handoff.py` 验证三个准入检查点、最后权限读取中的取消、交接前停止和启动者直接退出；host 消失但 child 仍活着时保持 UNKNOWN。
同任务两个后台 session 只停一个；冻结出生标识在终止快照入口再次核对。未确认后代不能因根进程退出而算清理通过。
日志上限测试改为直接验证唯一 host，旧 token 在交接后取消保留资源，显式 session 停止另验；不保留测试专用 watchdog。
完成通知同时覆盖 v1 数据与 v2 starting/running/unknown/not_started/killed/自然退出/明确停止，存储故障保留重试与去重义务。
提交后安装失败不发信号；信号后保存失败保留真实终止回执；其它事务恢复失败不能冒充当前停止已提交。
原 execution 的冻结复查不重复运行前置预算/审批门。上述是隔离开发进程、替身和持久合同验证，没有模型或真实 TUI，不宣称 Windows 已实机覆盖。
本片 18 个相关文件 533 项整组通过；同一生产代码另补 3 项网络只读投影用例，合计 536 项，无 skip/xfail，文档 6 项单列通过。
包含跨 owner 的坏 Store 不影响新交接；网络投影用内核替身验证 PID 复用前后拒绝扩范围、UNKNOWN 不冒充进程停止，不修改任何系统网络设置。
整任务控制、发布部署和安装版多 TUI 仍待完成，TUI 137 原失败保持；所有系统网络配置保持原状。

## 后台进程 v2 存储合同（源码验证）

`test_process_session_store.py` 验证字段及实例身份、版本 CAS、停止与交接的单向事实、旧 v1 不获得任务停止授权、精确身份选择及新恢复轮隔离。
新增 50 项存储合同与原相关回归合计 11 个文件 352 项通过，无 skip/xfail；文档 6 项单列通过。
文件故障覆盖提交前拒绝、发布后部分安装、日志删除失败、坏 redo 和后续版本冲突；全批预检失败时不返回健康子集或先覆盖前面记录。
独立 Python 进程在第一条记录安装后直接退出，下一 Store 读入口完成原批次；另一独立进程持锁时同根查询等待、异根读写继续。
这些进程只操作临时记录，不运行模型或产品任务。缺失系统锁明确拒绝；Windows 字节锁实现尚无实机验收，不声明掉电耐久。
上述存储片完成时 Shell/host 尚为 v1；后续源码启动接线见本页前节。整任务准入关闭和控制清理仍待接线，未发布部署，TUI 137 原失败保持。

## 资源身份与本地中断首片（源码验证）

当前源码的 10 个相关测试文件共 302 项通过，无 skip/xfail；覆盖身份、PTY、后台会话/完成通知、Shell 取消与 Gateway 控制。
新增两项本地中断回归先对基线真实控制函数运行，均因错误进入资源回收入口而失败；修复后通过。
其中同步 Shell 用自己的启动标记确认命令已运行，再从正式控制入口中断，最终工具回执为 `CANCELLED`。
另一项核对当前请求的精确信号、错误请求不被中断、插话和窗口快照保留；原 `/stop` 仍清退当前请求插话并派发子代理取消。
独立资源未回收由入口负断言验证，不能据此声称普通后台生命周期已经通过。

共享身份测试覆盖访问回退不补齐执行归属、输入字典变化不重绑资源、缺失选择身份拒绝及所有提供维度同时匹配。
原 PTY 实进程测试继续核对跨轮保留、整任务与精确 attempt 停止、其他任务及恢复轮隔离、启动期间取消。
上述为身份首片当时的开发反馈，不是真实 TUI 验收；后续启动 v2 接线见本页前节。整任务停止仍待实现及部署，TUI 137 失败保持。

## 公共命令首片验证

本片覆盖核心名称/别名、Compact 无空格与多行正文、btw 原多行边界、CLI memory 前缀和退出词范围。插件命名空间测试包括缺 ID、异常后缀、中文、引号、空格路径及 `--`；此阶段只验证明确拒绝，不声称已实现参数 schema。
TUI 使用实际 handler 的 fake 输入链检查空闲、前台、后台和子代理页面，确认无普通入队、插话、中断或退出；补全只编辑且不扫描路径。HTTP ask/control 另核对原处理中文件字节与旧 guidance 不变、中断回调未触发；文件提交在分配 ID 前拒绝，旧队列在追加用户历史和调用模型前拒绝。
定向测试与真正安装版多 TUI 验收分别记录；首片已发布并同包部署双机，以下实际测试只证明声明及命名空间边界，不代表插件可调用或热装卸通过。
首片 187 项定向通过（含 6 项文档测试），无 skip/xfail；7 个插件后缀分类用例先在旧实现失败。没有重复全仓 pytest。

安装版 `da3fdc422` 的 TUI 134—137 均从实际 `/model` 选择官方 MiniMax-M2.7，每个用例只提交一次普通中文需求；原始日志、身份、端点和产物保存在仓库外。

| 实际用例 | 框架与控制事实 | 交付及覆盖边界 |
| --- | --- | --- |
| TUI 134 本机、136 测试机 | 各 10 条命令含 7 种插件入口，均明确应答且未创建模型任务；随后普通文件任务分别有 11/4 对工具、9/4 次成功模型调用，自然收口 | 数据、汇总和完整哈希正确；校验器漏检、失败退出码及脚本完整读回有缺口，不记完整交付通过 |
| TUI 135 五分钟后台程序 | 单次启动，自然运行 300 秒；插件命令拒绝，Goal 暂停时正在进行的回合、Compact 和原程序继续；35 对工具、46 次主链加 2 次 Compact 调用 | 17 条记录与结果文件一致；未执行中断、恢复或资源停止。Goal 保持 paused，不能把保留的任务关联写成全体终态 |
| TUI 137 控制补验 | 单次后台启动；暂停保留执行，恢复后实际中断命中活动 claim，原程序继续；随后 `/stop` 已取消回合并暂停 Goal | **资源停止失败**：原后台进程在停止请求后至少 265 秒仍存活并追加数据；不能用任务状态或后续自然退出抵充停止通过 |

TUI 137 的首次中断发生在回合自然结束后，明确返回无活动回合；该条只记空闲响应，后续活动中断另有独立控制记录。
命令矩阵中部分首次截图早于重绘，后续有序帧与不同响应序号确认实际应答，原截图不改写。测试者未代写、代跑或修改业务产物。
TUI 135 的 `process_session` 调用为 list 一次、status 十四次，没有请求 wait；调用量含缓存，既不等于收费，也不证明长等待工具异常。
该片当时先定位 TUI 137 的任务资源停止缺口，第 3 步未收口；后续修复与新版验收见本页首节。实际控制测试不得改变系统网络、Wi-Fi、网卡、路由或系统代理。
后续只读核对原程序在 600 秒自然退出 0，原三个 PID 均消失，没有人工补停或补产物。失败保持；资源登记、停止入口及本地控制实现与命令首片之前字节相同。

## 原则

验证证据以 `test_verification_runtime.py`、`test_verification_repository.py` 和项目命令识别测试为准，覆盖真实工具出口、写后过期及 owner/task 隔离。
离线矩阵只检查实现/测试文件存在及尺寸报告，不能当作行为验收。无生产调用的旧 verifier integrity 模块及仅检查输入字典的测试已删除，不再计入运行时覆盖。
旧结果块修复场景及其跳过的验收已删除；`test_scenario_commands.py` 用正式参数解析器核对已删除/未知 case 被拒绝。
自然回复与宿主结束原因继续由 `test_subagent_finalize_helpers.py` 覆盖，保留相邻 runner 重试场景的已有 xfail，不能将删除旧协议测试计为修复该失败。

开发反馈优先定向合同、工具替身、模型替身和脱敏回放；真实 TUI 是最终验收最低要求。测试任务由被测代理完成，测试者不能代写产物后计为通过。
子代理审批回归必须包含真实创建生命周期的父会话关联，覆盖 child/grandchild 的批准与拒绝；仅裸 manager 创建不足以代表正常 TUI 派工。
归属记录读取失败不能退回主任务批准；具体审批与 capability grant 分开核验。对应 gateway control、background approval、owner policy 和 tool round 定向测试。
真实拒绝验收必须观察到具体审批及拒绝回执，并核对 handler 未执行；默认确认模式允许的普通命令没有弹窗时，该轮只能记为未覆盖拒绝路径。
同参拒绝回归必须跨实际子代理 Goal 续轮及 Compact，不能仅在同一个工具循环参数对象上重复调用。
`test_agent_goals.py` 使用真实生命周期、权限门和工具账，替换模型与用户决定；核对第二次同参不弹窗、不同参数仍申请、不同孩子独立、handler 始终未执行。
前后台活动回合的 Compact 夹具同时检查拒绝列表保持原对象，新调用为空；这些确定性用例不抵充安装版 TUI 验收。
审批与控制并发时，先确认退出审批模态及输入回显，再提交 `/stop`，以实际控制回执核对；不能把发送按键等同命令已执行。
长采样按实际启动/退出、追加数据与时间重叠验收；测试者处理审批的延迟、模型重跑和采样跨度须分别记录，不能用同一个 PID 数字证明没有重启。
前台 Shell 超时回归包括组长先退出、后代被重新挂到系统进程、忽略 TERM 需要升级终止，以及另一进程组不受影响；回执须核对成员退出和管道排空。
嵌套 Shell 后台检查同时覆盖字面 `-c`、常见包装命令、嵌套引用、普通字符串、重定向和 heredoc；启动前拒绝不得伪记为进程已经执行。
Shell 行数回归覆盖空输出、末尾有无 LF、空行、CRLF、裸 CR 和预览截断，联测真实工具出口的模型正文与展示事实；行数仅描述已采集文本，不表示业务数据条数或完整采集。

真实 TUI 输入多行需求时使用括号粘贴或整段提交，并从原生会话核对用户消息数；按键已发送不证明只提交了一条需求。测试者投递失误与模型任务失败分别留证，不计为通过。

普通需求用自然中文表达。权限、参数、隔离、状态和恢复由底座控制，不靠在提示词里写特殊限制规避缺陷。详见 [测试分层](docs/design/main-agent-contract-testing.md) 与 [测试清单](TEST_CHECKLIST.md)。

拟议结构调整、Computer Use 当前部署复验及 Jev 只读对照边界见
[可维护性评估](docs/design/MAINTAINABILITY_AND_JEV_REVIEW.md)。后端首批拆分已完成定向验证，既有测试与发布 gate 不变；
接入协议测试与真实桌面分别留证，Jev API 尚未验收。

本轮用户指定：所有真实验收通过实际 TUI；常规任务使用官方 MiniMax-M2.7，视觉任务使用官方 MiniMax-M3。
视觉会话复用 M2.7 的私有 provider 与密钥引用，只切换模型名；核对实际请求端点、模型及图片确已送达，不因同名模型推断来源。
测试覆盖长任务矩阵，历史日志只作案例来源；任务、工具、历史和产物须由被测代理自己完成。
登录认证也从 TUI 进入，其他账号的真实模型调用不混入本轮验收。截图、账号信息和原始日志只保存在仓库外。
每批可并开多个真实 TUI，必须公布 tmux 名称和查看命令，共用一个 Gateway；重连保留原会话身份。
新增回归覆盖 Full Access 的后台进程地址、跨权限视图恢复、PTY 共享解析及文本输入 Unicode 事件。
键盘事件替身和内存事件校验不计真实桌面通过，仍须由 TUI 模型操作后读取目标应用核对。
本轮已完成后台等待、PTY、取消续做、断连重连、Goal 暂停恢复、多子代理插话与手动/自动压缩；
本机英文和中文桌面输入读回一致。登录设备码被官方端点拒绝，真实账号确认/刷新/退出未通过。
具体通过范围与失败记录见 [本轮真实矩阵](docs/design/MAINTAINABILITY_AND_JEV_REVIEW.md#本轮真实-tui-验收矩阵)。

## 模型与框架对照

本轮以下四组真实 TUI 对照已收口，未全部通过；常规验收默认仍为官方 MiniMax-M2.7：

| 组别 | 被测框架 | 模型与来源 | 用途 |
|---|---|---|---|
| A | my-agent | 官方 MiniMax-M2.7 | 当前失败基线 |
| B | my-agent | OpenCode 的 deepseek-v4-flash | 同框架比较不同模型接入 |
| C | Codex | 官方 MiniMax-M2.7 | 同模型比较框架 |
| D | Free-Code | 官方 MiniMax-M2.7 | 同模型的第二个框架对照 |

先逐组验证真实 TUI 模型调用及工具往返，再执行相同原始中文需求、相同初始文件和相同验收标准。
各组使用独立新会话、独立工作目录；优先在同一测试机对齐运行条件，保留真实工具和子代理能力差异。
三组 M2.7 核对同一官方上游、模型和可对齐的采样/输出/上下文设置；如必须经协议适配，单列适配路径，不能把同名模型当作同一链路。
记录安装版本、权限、额外系统指令、记忆/技能、工具声明、原生输入输出和程序生命周期；测试者只投递需求、观察及处理明确审批/控制。
进程事实使用宿主 PID、出生标识和 namespace PID 映射；CSV 中的沙箱 PID 不能直接当宿主 PID 查杀。
子代理 DONE、模型结束回复、后台程序退出分别取证；Shell 启动尝试次数也不等于成功采样进程数。
并行只用于独立目录和资源充足的任务；限流或资源竞争单列并做串行复核，不能混作任务执行失败。
覆盖普通文件、持续程序、多个子代理和最终汇总；关键失败用新会话重复，全部原始成功/失败均保留，不以重新运行覆盖失败。
如果同一 M2.7 在两个外部框架稳定通过，而 my-agent 的两种模型稳定失败，优先定位 my-agent 的上下文、工具协议和生命周期缺陷。
只有 my-agent 的 M2.7 失败时，也要区分该模型能力与本框架对它的协议/上下文适配，不能仅按通过组数宣布根因。

本轮按用户授权并行执行；四组均已在实际 TUI 验证模型调用和文件工具往返。Codex 直连官方 Responses，
Free-Code 与 my-agent 的 M2.7 使用官方 Messages 兼容接口；协议和宿主指令不同，不能把它们称为逐字相同的模型输入。
Free-Code 的 bare 预检限制工具面，不能当作多子代理验收；完整任务使用正常模式及独立配置目录。
Flash 初次配置缺少上游要求的会话请求头，补充 provider 的显式配置后新 TUI 通过；旧失败保留。
Free-Code 首次正常模式因测试者在密钥来源确认中选错选项而未发真实模型请求，归为测试准备失败；新会话经实际 UI 正确确认后重测。

| 真实 TUI | 框架功能 | 模型交付质量 / 原整轮结果 | 证据与边界 |
|---|---|---|---|
| 99：my-agent / M2.7 | 本场景通过 | 通过；自检覆盖限制保留 / 通过 | 三个孩子各自写源文件，主级完整读回；150 行数字、单表头、汇总和哈希一致；主级 11 组工具往返。孩子只检查行数和首尾，不宣称完整自动数值校验。 |
| 100：my-agent / Flash | 本场景通过 | 通过 / 通过 | 主子均使用 Flash；主级 17 组往返，孩子各自全量程序校验，汇总与真实文件一致。 |
| 101：Codex / M2.7 | 后续文件及父子链路已验；初始型号选择单列 | 失败 / 未通过 | 首次派工选到目录中但上游不支持的型号；后续真正生成数据的三个孩子均为 M2.7。一处平方值错误、合并保留三份表头，最终报告未修正数据。 |
| 104：Free-Code / M2.7 | 本场景通过 | 通过；最终读回覆盖限制保留 / 通过 | 正常模式父子均为 M2.7，主级 15 组往返；一个孩子经历三次工具错误后自行用 Python 校验成功，最终数字、单表头和哈希正确。主级完整读回源文件，最终文件的额外读回覆盖较弱，观察者审批耗时单列。 |
| 105：Codex / M2.7 | 文件及父子链路已验；额外路径行为单列 | 失败 / 未通过 | 单一 M2.7 目录、stock 指令及原工具模式下，平方错和重复表头均自行修正；summary 的一份源哈希抄漏字符，仍与磁盘不符。另有显式任务校验中间文件写到工作目录外并残留。 |

四组长题使用同一原始十五分钟三子代理采样需求。每路采样跨度须至少 900 秒；三路真实重叠时间如实计算，不额外要求共同重叠也达到 900 秒。

| 真实 TUI | 框架功能 | 模型交付质量 / 原整轮结果 | 证据与边界 |
|---|---|---|---|
| 106：my-agent / M2.7 | 工具往返、当时文件读回、自然进程收口通过 | 失败 / 未通过，已自然收口 | A/C 初版程序错误后重启，三路共启动五次。最终 A/B/C 为 63/62/62 条、915.0/900.9/900.0 秒；主级提前读取 A/C，却把中途 59/58 条、870/855 秒标为通过，旧哈希也未更新。主级 14 组原生往返完整，文件读取和当时前缀一致，无工具丢尾部证据。 |
| 107：my-agent / Flash | 持续并发、完整读回及自然收口通过 | 数据通过，报告覆盖受限 / 核心采样通过，报告有保留项 | 三路各只启动一次、62 条、至少 900 秒、自然退出 0；完整统计与哈希正确，共同记录跨度 876.811 秒。近零末间隔的绝对相等表述、首尾记录及恢复过的工具错误披露不足单列，不把四舍五入显示当计算错误。 |
| 108：Codex / M2.7 | 部分已验；自然完成未覆盖，精确资源清理另验 | 失败 / 观察者中断，未通过 | 多次启动/清空尝试与范围过宽的终止命令保留。通过实际 TUI 分别中断父子回合后，另外按出生标识精确停止本测试残留资源；不能把中断冒充资源已停止，亦无证据宣称其它测试受害。 |
| 109：Free-Code / M2.7 | 持续采样受宿主生命周期限制；A/B 提前被清理 | 失败 / 后续重跑被拒并中断 | A/B 提前结束回复，原生日志明确清理它们的后台任务，实际分别 41/42 条、约 600.548/615.189 秒，无结束记录；C 自然完成 61 条、900.000 秒。C 随后误读 A 文件并申请覆盖重跑，实际 TUI 拒绝并中断，未生成最终汇总；原文件哈希保持，资源已退出。 |

首尾记录的既定验收清单按每路首尾各两条；原需求“首尾两条”存在解释空间，只有首一末一时标明覆盖限制，不能借此认定底座故障。
TUI 109 的脚本重名被写版本守卫先拒绝，C 启动后 A 才读改脚本并启动；未观察到写错 CSV，不能把冲突申请直接当作覆盖已执行。
TUI 106 的父级完整读到 A/C 后台仍运行的报告及会话句柄，但零次调用进程查询；孩子 DONE 不等于采样结束。现有证据未确认新的交接字段丢失或查询越权缺陷，不因此增加第二套通知或专项判定。
本轮采样及被中断测试的精确所属资源均已退出，TUI 保留；观察者中断与资源停止分开留证，不冒充任务自然结束。
用户已明确选择分开记录：已核实框架功能后可继续结构重构，模型交付失败原样保留，不将整轮结果改为通过。
框架缺陷、必测功能未覆盖及可能影响框架的未知原因仍阻断相关步骤；没有发现缺陷不能单独充作功能通过证据。
每片分别列自动回归与实际 TUI 覆盖；未命中的内部策略分支不能靠普通长任务结果声称覆盖。私有凭据、配置和原始证据仍留仓库外。

第 1 步插件接线的只读核对另运行 17 项已有定向回归并通过，覆盖启动扩展、工具快照、MCP 重连、Skill 过期读取及命令补全。
其中冻结可用性的既有回归明确保证：工作片开始后可用性探针变化不改写旧快照；插件撤销须另读生命周期权威，不能改该断言假充热卸载。
测试入口为 `test_extension_plugin.py`，以及 `test_tool_runtime_scope.py`、`test_mcp_registration.py`、`test_skills_service.py`、`test_conversation_control_commands.py`、`test_tui_input.py` 的上述相关函数，均位于 `agent_py_agent/tests/`。
本项没有新增真实 TUI、插件实现或部署，也不是严格发布 gate；具体接入约束见 [插件方案](docs/design/PLUGIN_LIFECYCLE.md#第-1-步接线核对与迁移约束)。
同时更正 `registry_invoke.py` 的过期可用性注释，语法树与 HEAD 完全相同；原 17 项结果仍对应同一执行逻辑，不把注释修正描述成运行修复。

第 2 步进度策略首片：`test_background_main_agent_runtime.py` 与 `test_background_supply_backoff.py` 共 168 项通过，覆盖无进展退避/复原、失败退休、成功清账和供应/配置错误隔离。
迁移前后算法体经参数投影及常量改名后语法树一致，常量值未变；没有新增镜像测试。代码 `680b7b4e8` 已通过本地严格 gate 并同包部署双机，线上 CI 列表为空。

| 新版真实 TUI | 框架功能 | 模型交付质量 | 覆盖边界 |
| --- | --- | --- | --- |
| 110：三子代理十五分钟 | 观察范围通过；各启动一次、持续约 22—24 分钟、39 对工具匹配、全文读回相同、任务/claim/资源自然收口 | 失败；时钟和 CPU/最大内存采样含义错误，报告间隔计算错误；JSON 计数口径及首尾覆盖另记 | 原需求一次提交；审批准备等待不计采样时长；没有 progress policy 落账 |
| 111：测试机订单 | 通过；8 次模型请求、9 对工具、全文读回及自然收口 | 当前数据正确；哈希比对措辞及校验覆盖有问题 | 未触发 progress policy，不充作内部策略真机覆盖 |
| 112：本机单子代理 | 通过；主级 5 次、孩子 3 次模型请求，父子实际读写并自然收口 | 通过；数组 1—10、个数 10、总和 55 | 未触发 progress policy；通用 verification 的 UNVERIFIED 不冒充产物验收 |
| 113：本机订单 | 通过；12 次模型请求、13 对工具，完整读回且如实保留中途错误 | 当前数据正确；校验器只核总数、不逐组断言，错误也可能返回 0；报告覆盖措辞不实 | 未触发 progress policy；模型自行修正的初版数字和脚本错误保留 |

三路短题均为官方 MiniMax-M2.7，各一条原始需求，无请求失败/重试；测试者未代写文件或执行模型产物。
110 主子实际请求同为官方 M2.7，三个原采样程序自然退出 0。含 END 的记录为 91/91/97，报告此项正确；JSON 的 90/90/97 未统一说明 END 口径，不将歧义扩大为确定计数失败。
实际 A 的 seq 6→7 为 15.037948860 秒，B 的 seq 3→4 为 15.140295746 秒；报告分别写成 16.14/153.39 秒，按交付错误保留。
三份父级 read_file 返回均完整且与磁盘逐字相同，保存过的各阶段前缀未被清空。首尾仅各一条仍按原需求歧义记覆盖限制，不能指认为工具截断。
内部策略分支仅记算法等价及自动回归覆盖，真实持续链路单列；本首片收口不代表供应故障、唤醒、租约和恢复等整步验收完成。

供应退避次片：供应、后台运行及直属父级三文件 188 项，加观察消费、会话车道、配置恢复和未投递重发的 9 项，共 197 项通过。
时钟替身已指向新权威模块；原恢复用例增加真实 ready-thread 冷却筛选，新增 None 保留失败次数、非 None 假值清账的合同检查。
类、守卫、三个内部 helper 与剩余 runtime 的执行语法树等价，配置有效值未变；独立只读复核通过。代码 `ef9db100d` 经本地严格 gate 后正常发布，双机同包 1,189 项一致，配置原样且保留回滚；该提交线上 CI 列表为空，未作为验收来源。

| 供应状态版本真实 TUI | 框架功能 | 模型交付质量 | 覆盖边界 |
| --- | --- | --- | --- |
| 114：本机单子代理 | 通过；主级 5 次、孩子 4 次模型请求，6 对工具，父子全文读回、一个孩子自然完成并退出 | 通过；数组 1—10，个数 10、总和 55，与实际文件一致 | 无请求失败/重试，未命中供应退避 |
| 115：测试机订单 | 通过；9 次模型请求、10 对工具，失败回执与自行纠错完整保留，任务及 claim 自然结束 | 最终产物通过；30 行公式、三组统计、总计、哈希及首尾正确；初版金额错误保留 | 同一校验程序先退出 1，模型修正汇总后退出 0；校验器漏查行序、公式等约束，当前文件另经只读核验；未命中供应退避 |
| 116：透明代理基线 | 通过；3 次官方模型请求、2 对工具，实际写入与全文读回相同，自然收口 | 通过；原文精确一致 | 4 次 CONNECT 均到官方域名，不将 TCP 连接数当作模型调用数；未触发供应失败 |
| 117：定时工作连接失败与续接 | 通过；原 run/wake 保留，失败后冷却 30 秒，新 attempt 自然续接，任务、claim 与唤醒收口 | 最终通过；20 行平方表、平方和 2870、哈希及报告正确；模型自行修正时区与报告转义错误 | 只覆盖 typed 连接拒绝到达 wake 消费守卫；完整重试预算保留，故障及恢复期间 Gateway 未重启 |
| 118：冷却期间另一前台会话 | 通过；4 次官方请求、3 对工具、零重试，整个任务结束早于 117 的冷却截止 5.760 秒 | 通过；数组 1—5、个数 5、总和 15，全文读回一致 | 实际流、工具与终态时间有原始记录；没有持久化精确 HTTP admission 时刻，不扩大为另一后台任务或跨 owner 公平性 |
| 119：原设置回切对照 | 通过；3 次官方请求、2 对工具、零重试，写入和全文读回与磁盘相同，自然收口 | 通过；数组 2/4/6、个数 3、总和 12 | 原配置、启动参数和完整环境恢复后新开实际 TUI；专用代理端口已关闭 |

六路各一条原始普通中文需求，实际端点均为官方 MiniMax-M2.7；测试者没有补需求、改产物或代跑校验。TUI 保留，无本轮业务进程残留。旧 TUI 110—113 不计入此次切片。
117 的首个后台 attempt 持续 608.895 秒，保留 6 次模型尝试、24 次 HTTP 尝试、18 次 HTTP 重试及 5 次模型重试；这些失败由专用代理停用注入，不作为真实服务故障或模型能力失败。
typed `ProviderTransientError` 到达守卫后，原 scheduler run 回到 queued 并释放 scheduler claim，原 wake 待处理，失败的 background claim 与 attempt 保留；冷却从本次失败时刻起算，第二代 attempt 在截止后 0.290 秒领取同一工作，17.374 秒后完成并记录 `provider_supply_resumed`。
原一次性 job 保留为 `paused / one_shot_finished`，`next_run_at=0`；完成 run 进入 history 且只有一条，不将保留 job 误判为重复调度。执行 TaskRun/AgentRun/attempt 均结束；通用 task 实体标签仍为 active 不代表存在运行中的 attempt。
模型初次无时区定时被拒后自行修正，只建立一个 job；第一次 Shell 报告因反引号替换丢字段，模型改用文件工具修复。最终报告写入与磁盘一致，但修复后未再次调用 read_file；CSV 与汇总均完整读回。准备时的 CLI 参数错误、审阅时误以为 job 应删除的断言均另存，未改产品行为或冒充原始任务失败。

故障测试先经实际 TUI 验透明 TLS 透传，不安装证书或解密请求正文；只在已核实无其它待运行工作的单 Gateway 窗口中断本轮专用代理，不改模型、官方端点、密钥或重试预算。
不关闭网卡或 Wi-Fi，不修改路由器、系统路由和系统代理；故障只作用于本轮专用本地代理，系统网络和 SSH 保持可用。
自动记忆策展也可能发请求，不能只看前台队列为空；必要时使用私有临时启动配置关闭 `memory_curator_enabled`，原配置和策展待处理账本保留，逐项核对其它有效配置相同，验收结束后恢复原配置路径及进程环境。
本轮已核对原启动参数和全部环境相同、原配置字节未变、策展设置恢复、专用代理关闭、系统代理及默认路由前后相同。回切仅发生在原任务自然完成且 Gateway 无活动工作之后，不算活动任务的 Gateway 重启恢复验收。
本切片实际覆盖连接拒绝、同线程冷却、普通前台隔离与原定时工作恢复；未实测 SSE 中途断流、额度/配置错误、None/假值及其它后台消费入口。前述 197 项是自动化验收，不算未实测分支的真实通过；第 2 步其余路由、租约和重启恢复仍需各自验收，不能用本轮结果代替。

路由切片的源码验收：后台主执行、观察路由、唤醒召回、Goal 生命周期、Gateway 退避、供应退避、会话事件、定时运行/tick、模块边界、Goal 工具和文档共 12 个文件，另加投递、控制、进程完成与直属父级的 10 个精确用例，共 353 项通过。
观察路由既有用例直接调用新权威模块；新增覆盖线程绑定优先且不读 owner、路径命中后不预读后续来源、线程加载失败保留 owner 路由、local 不冒充外呼通道及默认目标选择。真实 wake 接线用例改为实际选择路由，不再替换已经删除的方法。
Goal 两段处理按领域/回调映射后与原函数执行语法树一致；能力预扫、单 wake 执行及默认目标选择保持，观察编排仅迁接收者和地址调用。两个新模块在独立进程导入不会加载 runtime，无新增循环依赖。
最初三个新夹具漏填 binding 必需字段、一处误改 frozen 线程已修正；尺寸检查曾发现观察方法使 ExecutionMixin 超限，现保留为 runtime 独立编排函数，严格尺寸无 hard，基线未放宽。以上为开发验证，不算实际模型任务失败。
路由源码 `f9a1088d3` 已发布并同包部署双机，各 1,191 个包文件一致，默认入口和唯一 Gateway 同版。启动环境、配置和除 runtime 外的参数保持，旧运行目录及回滚保留；该提交线上 CI 列表为空，验收来源为本地严格 gate 和本片新 TUI。
TUI 120—123 均从实际 `/model` 选择官方 M2.7，核对 provider、端点和成功调用，只提交一次原始需求；每台共用一个 Gateway，未抓取 HTTP 正文。四路均已自然结束，框架与模型交付分列如下；不复用 TUI 114—119 证明新版本。

| 实际 TUI | 本片框架证据 | 模型交付结果 |
| --- | --- | --- |
| 120：持续 Goal、暂停/恢复与 Compact | 一个程序采样 960.000 秒、97 条，同一 Goal/任务续接，3 次 Compact，82 对原生工具，全部 54 个执行代次结束，程序退出零 | 采样主体通过；报告的间隔统计、两条间隔明细和内存单位错误，交付失败 |
| 121：并行普通订单 | 11 对原生工具完整，最终 30 行数据、汇总和哈希正确，任务自然结束 | 校验脚本仍报告不一致，模型却报告全部通过，交付失败 |
| 122：三名并行子代理 | 三个孩子各一代执行，实际重叠约 29.234 秒；父子共 15 对工具，源数据与合并 120 行正确，归属执行和锁自然收尾 | 数值、哈希、报告通过；父级提前读取源文件，违反先等全部孩子结束的原需求，整项交付失败 |
| 123：定时工作与原会话投递 | 一次 scheduler run、一个 handled wake、一个完成代次，6 对工具，原会话只收到一次最终回复，一次性 job 正常退休 | 20 行平方表、平方和 2870、哈希和首尾记录均正确，通过 |

120 的暂停发生于活动 claim 内：同一 claim 继续执行后自然结束，采样在暂停期间保持同一进程并继续追加；152.668 秒后通过实际 `/goal resume` 恢复原 Goal/任务。没有把暂停当成中断或停止资源。最终原生读取完整返回 CSV 全部 98 个物理行，与磁盘逐字一致；退出零从同一工具调用的完整归档核验，预览截断不当作原回执缺失。
实际最小/最大采样间隔为 9.991213 / 10.008800 秒，报告为 9.993425 / 10.008858 秒，另两条间隔明细抄错。生成程序将本平台按字节返回的最大驻留内存直接标为 KB，报告将实际增加的 48 KiB 写成 48 MB；这些是生成程序/模型统计错误，工具没有篡改原值。模型关于内存增长原因的推断没有证据，亦保留。
120 共 121 次成功物理模型请求、零重试；35 次进程状态查询、33 次行数/尾部查询、2 次进程等待。高频轮询和费用相关用量原账保留，不把任务自然结束等同执行高效，也不据请求数推断模型内因。
121 模型修正了初版金额与 Shell 计算错误，但遗留校验脚本仍比较错误常量、没有读取汇总文件。两次实际命令都返回不一致警告，模型修正汇总后未重跑，却报告全部通过；不把脚本退出码零当作内容验收通过。
122 父级完整读回三份已写好的源文件，随后孩子才完成；实际合并发生在全部孩子结束后。只有读取顺序违反原需求，不能写成提前合并或结果错误。重叠来自 canonical attempt 时间，并不证明三个系统进程或模型 HTTP 请求同时运行；18 次模型调用均成功。
123 首次执行在到期后约 25.669 秒，全部业务写入在到期后；前台 2 次、后台 4 次模型调用均成功。原生历史载体的外层正文为空不等于零模型输出，其后普通最终回复在原 TUI 可见。任务、run、attempt 和 claim 已结束，一次性 job 保留为 paused/one_shot_finished，不误判为残留运行。
四路测试者均未补需求、修产物或代跑生成程序；业务自然退出后，双机活动 claim 和 pending/processing 请求为空，历史 TUI 保留。本片所测路由链路收口，继续租约/恢复拆分；其它通道 owner 回退、异常 Goal 分路和活动任务 Gateway 重启恢复不由本轮证明，第 2 步整体仍未完成。系统网络不作为故障开关。

租约/恢复源码片新增 `test_background_claim_execution.py`：25 项合同先在迁移前实现通过，
覆盖成功/None/假值、精确身份与时钟、领取后终态和 unknown/不可读变化、子代理归属、忙碌车道、
中断/Compact/普通及模型错误分类、启动/停止/事实读取/结算异常的原传播范围。
真实心跳用受控回合保持执行中，释放前核对实际 renew、延长的到期时间、另一持有者不可抢占；
不能用 finish 后的 heartbeat_at 代替续租发生证据。迁移后补全 wake/policy/observation 的阻断参数组合，
恢复守卫另验每次查询、当前 checker 替换、日志指纹及不可读判据。旧测试替换点直接迁至新权威入口，
模块独立导入检查不许加载 runtime/网络后端。最终 324 项相关回归通过，包含后台来源、调度、恢复、
前后台共享车道与精确 claim 释放；独立源码复核未发现阻断性语义差异。以上为无模型的合同回归，
不替代真实模型验收。源码 `e1797a67d` 已发布并以同一 non-editable wheel 部署双机，
各 1,193 个包文件一致，默认入口和每台唯一 Gateway 同版；原环境、私有配置及回滚保留。
部署时两端均空闲，活动任务恢复须另行验证。线上 CI 列表为空，发布验收来自本地严格 gate。

本片 TUI 124—127 均为官方 MiniMax-M2.7，各只给一次原始中文需求；核对实际 profile、端点、
成功用量和原生历史，没有抓取请求正文或改日常默认模型。以下实际结果与自动回归分列：

| 实际 TUI | 框架及实际产物事实 | 交付和未收口边界 |
| --- | --- | --- |
| 124：十八分钟 Goal | 单次采样 74 条、约 1080.003 秒、单进程自然退出零；74 对工具、74 个执行代次和 Goal 自然收口 | 报告最小/最大间隔错误，交付失败；不能用状态 complete 代替报告核验 |
| 125：并行订单 | 30 行、三组统计、总订单 1605、金额 14625 和哈希正确；7 对工具、8 次成功模型调用，运行及锁收口 | 最终校验器漏查分组和行数，失败分支仍退出零，未读回全部文件；完整交付失败 |
| 126：定时与长子代理 | 原定时工作一次执行，父子交接和进程自然退出；50 批算术正确，记录前缀未被改写 | 末 20 批约 0.003 秒内补齐，记录仅约 870.157 秒；十五分钟和节奏要求失败。能力审批的稳定资源记录在后续代次被改为 DIRTY，修复及新版验收见下文，原失败不改写 |
| 127：另一会话普通任务 | 1000 条数字和平方，无缺失/重复，总和 500500、平方和 333833500；3 对工具、4 次成功模型调用，任务及锁收口 | 数据与原需求交付通过；跨任务并发只按真实时间关联，不扩称全部后台公平性 |

124 通过实际 `/exit` 关闭客户端，约 98.767 秒后从 CLI 重连同一 session；原 Gateway、Goal、
采样进程及数据前缀保持，未重发业务。另暂停目标约 181.664 秒时，当前 claim 正常结束，采样继续；
再经实际 `/goal resume` 恢复同一目标。根开发 Goal 始终 active，没有把这两个目标或三种控制混为一谈。
同一 claim 的两次运行中快照显示心跳推进约 540.152 秒，后次心跳跨过前次到期时间，证明真实续租，
不是借 finish 写时间戳。124 的完整 CSV 已返回模型，142 次成功物理模型调用及高频轮询单列，不能据此声称高效。
实际全部单调时钟间隔最小约 0.000245 秒、最大约 15.008010 秒；即使排除末尾近邻记录，
最小仍约 14.990755 秒，均不同于报告的 14.996411 / 15.005516 秒。测试者未修统计或补产物。

126 的原前台 claim 在定时到期前已结束，因此本例没有命中共享车道竞争；不能只凭同一会话先后执行宣称竞争通过。
其两条资源记录先由成功的能力审批确认 STABLE，后续正常新代次却被旧轮清理标为 DIRTY；
对应源码与迁移前一致，属于新发现的既有框架问题。没有持有锁不等于资源全部干净，未人工清账。
本例父子执行闭环与这项资源状态缺陷分列，不能将框架整项写成全过。
TUI 128 的唤醒到期入队后，现场仍观察到原前台 claim 持有；原 claim 释放后约 0.947 秒，
定时工作才以 previous_finished 接手，最后正常收口。此为同车道排队和串行接手的实际证据，
没有持久记录证明内部 acquire 曾返回 busy，该分支仍由合同测试覆盖，不据推测宣称真机命中。
128 的程序单次生成 12 批但仅持续约 220.075 秒，未满足四分钟；未全量读回原始批次，
哈希只有批次文件的前缀，完整交付仍失败，测试者未补做。

活动恢复的两次控制准备没有改变 Gateway：128 错把 request.workspace 对象当字符串，
129 错读早期尚未持久化的 conversation_runtime，把自己的 claim 当成其它工作而保守退出。
两次均未执行停启，分别记为观察者准备错误与恢复 NOT_EXERCISED，原业务照常完成、没有重投。
129 的 24 条交易公式、四期收入/支出、全季 2388/2592/-204 和哈希正确，12 对工具及执行链收口；
模型自行修复汇总后实际重跑校验成功。报告首两条混入表头、脚本未全文读回，完整交付失败。
有序活动恢复和资源状态修复的安装版复验继续见下文。系统网络始终不作为故障开关；
故障只能作用于明确归属本轮的应用进程或测试专用代理，不操作网卡、Wi-Fi、路由或系统代理。

资源换代的修复另记：新增 `test_runtime_db_stable_mutation_generation.py`，正常续轮、活动接管、
升级调和三项先在旧实现失败，均复现 STABLE 被误置 DIRTY；修复后通过。测试同时验证原 STABLE
记录逐字段不变、新轮能重新开始写入、旧轮仍无权限、未完成写入转 DIRTY、既有 DIRTY 证据不变。
生产只收窄原事务中的资源更新条件，不修改锁、代次 CAS、UNKNOWN 工具记录或历史数据库。
相关仓储、工具、能力申请及 Gateway 恢复合计 186 项通过，另 6 项文档检查通过；无全仓重跑。
该修复已发布并用同一 wheel 部署双机，每端 1,193 项包文件逐项一致；本地严格 gate 已通过，
线上 CI 列表为空，未作为验收来源。以下四路均从实际 TUI 发起一条原始中文需求，
使用官方 MiniMax-M2.7；配置来源和实际成功用量已核对，不宣称完成逐请求抓包。

| TUI | 实际框架证据 | 交付结果和覆盖限制 |
| --- | --- | --- |
| 130 本机活动恢复 | 首轮模型流已有输出、尚无工具操作时有序停启 Gateway；原请求、run 和会话保持，旧轮 UNKNOWN 经 `confirmed_noop` 进入第二代完成。5 对原生工具、3 个成功持久操作，执行锁清理 | 24 条记录、四期汇总、总数 4068 和哈希正确；模型主动切到 owner 根写文件，未遵守当前目录。恢复上下文的 cwd 正确，归为模型交付失败；不据零操作恢复推断未知副作用可自动重放 |
| 131 测试机能力申请 | 一次执行自然结束，3 次成功模型调用、2 对工具；没有子代理、能力申请、能力裁决或真实审批 | 模型只写脚本并口头询问执行权限；计划分支 NOT_EXERCISED、交付失败。不追加提示或由测试者代执行 |
| 132 测试机普通对照 | 5 次成功模型调用、6 对工具，2 个成功持久操作；run、attempt、claim 完成，零资源写入记录及执行锁 | 18 条记录公式、三组数量 75/183/291、金额 525/1281/2037、总数和哈希正确。脚本未全文读回、校验器失败退出语义不足，完整交付失败；独立核对不冒充模型自检 |
| 133 本机交互终端 | 测试 Goal 暂停后当前 claim 仍运行；随后实际中断、恢复，同一 run 从第一代进入第二代。原 PTY 的 STABLE 记录跨代全字段相同，第二代成功再次使用原终端；旧等待命令 UNKNOWN 整行保留。27 对工具、26 次官方成功调用，14 个持久操作成功，Goal/run/claim 完成，锁为零，原解释器自行退出 0 | 三批累计 30/100/210，间隔约 516.6625/152.5351 秒，数据正确。覆盖 cancelled 后续代；自然 done 续代由确定性回归覆盖，131 的能力裁决仍未命中。读回时序与模型自修另见下文 |

130 控制器按精确 PID 与出生标识确认原进程退出，才用原参数、完整环境和配置恢复唯一 Gateway；
不使用强杀或产品 restart 的强制清理分支。控制器自己的 11 项替身检查只算测试准备验证，
不计入产品真实验收。133 的人工审批等待单列，不把整段墙钟时长当作连续计算工作量；
真实控制只针对测试 Goal，开发 Goal 不受其 pause/resume 影响，历史 DIRTY 和 UNKNOWN 均不改写。

133 按原需求交付通过：同一 PTY 的非截断结果在写 JSON 前完整包含三批数值与实际时间戳，
JSON 的累计和间隔核对正确，随后模型输入 `exit()` 并读到退出零，最后又完整读取 CSV/JSON。
原文要求完整读回数据，没有限定必须先以文件工具重读 CSV；不额外添加文件读取顺序作为失败条件。
两次交互 Python 语法错误及一次未定义变量错误已由模型在原解释器内自行修复，原回执保留。
测试者仅批准精确操作和执行既定控制，没有代写、代运行或补充业务需求；测试 Goal 完成不等于开发 Goal 完成。

第 2 步四个结构切片的本轮框架验收收口，下一步进入公共命令合同。真实用例覆盖长任务、
普通并行对照、父子执行、定时唤醒、冷却续接、运行中续租、重连、排队、有序恢复与控制语义；
内部 acquire 的 busy 分支、自然 done 后 STABLE 换代和其它错误分路只按对应确定性回归记账。
131 原计划未命中，133 用同一底层资源协议补验取消后换代，不将两者描述为相同业务场景。
两端最后只读核对均为唯一 Gateway、无活动 claim、无待处理请求或活跃定时工作；历史记录原样保留。
这些结论不关闭模型履约失败，也不承诺通用 UNKNOWN 的 TUI 手工裁决能力已经实现。

## 必测模块

发布前 HTTP 取消回归覆盖 JSON/GET/SSE：响应头尚未返回时先中断再出现连接/清理异常，必须保持中断且仅请求一次；
未中断的对照必须原样抛错。实际 socket 等待用例与确定性竞态用例同时保留，不能靠多次重跑偶然通过。

长时间运行增量矩阵见 [设计与验收](docs/design/LONG_RUNNING_EXECUTION.md)。普通验收并行运行官方 MiniMax-M2.7
TUI，共用单 Gateway；只有用户明确要求的慢模型专项才另用单路对照，定向回放和真实通过分别记账。

| 模块 | 验证要点 |
|---|---|
| 配置与模型 | 服务商协议、密钥引用、上下文容量、会话选择、用户默认、子代理继承与显式覆盖 |
| 身份与工作区 | 多用户同 Gateway、家目录隔离、管理员显式越界、工具权限与真实路径 |
| 主子代理 | 创建、插话、停止、恢复、换代、结果落账、父级唤醒、重复及乱序事件 |
| 历史与压缩 | 未压缩历史完整性、Unicode JSONL、展示分页、长输出引用、压缩计数、模型切换 |
| 工具 | 参数校验、成功/失败状态、文件读写、搜索、补丁、命令/PTY、网络、MCP |
| 记忆与技能 | owner 隔离、自主记忆维护、人格确认、索引发现与按需读取；技能选代表场景 |
| TUI | 输入回显、换行、粘贴、滚轮、复制、完整展开、到底部、主子代理视角、Todo、活动状态 |
| 调度与交付 | 普通回合与目标模式、挂起唤醒、断线、后台交付和恢复；IM 无环境时标明未测 |

## 插件装卸拟议验收（待实施）

插件方案尚未落地，本节不是通过记录。先用合同/替身/回放验证代次、权限、撤销、清理与失败状态，
再用官方 MiniMax-M2.7 的实际 TUI 共用单 Gateway 并发验收：一路插件长任务、一路正常内置任务、一路插件管理。
对应 [合并实施计划](docs/design/MAINTAINABILITY_AND_JEV_REVIEW.md#下一轮结构整理顺序待实施) 的第 3—6 步：
命令解析、装卸链、TUI 使用和故障验收分别留证，全部通过才计首批插件可用；不能仅以补全或单次成功调用收口。
首批从显式本地包和只读示例开始；在线安装、更新/回退留后续专项，不以未实施功能阻塞后续结构拆分。
三路会话是角色安排，实际 TUI 编号与 tmux 名称须在启动后报告；每轮核对官方 provider/端点，保留装卸前后的 Gateway 进程身份。
核对未安装/已装停用/启用/卸载的有效能力与模型配置，卡死卸载时管理入口和无关任务可用、Gateway 不重启、插件不被重连复活。
版本切换、旧任务恢复、命令冲突、owner 隔离及清理失败按真实事实单列；报告实际 TUI 编号与 tmux 查看方式。
命令合同须覆盖 `/plugins` 管理与 `/plugins@ID` 调用、同源帮助/补全、停用时只读帮助、未知目标/参数、缺值、短开关组合、引号与跨平台路径；
`@` 不误触发文件补全，命令错误不转聊天，参数文本不作为 Shell 执行。此项仍为待实施矩阵。
详细矩阵见 [插件设计](docs/design/PLUGIN_LIFECYCLE.md#实施顺序与验收)，不得用现有工具测试替代这些尚未执行的验收。

合并计划第 10 步新增 [10 个简易插件与组合验收](docs/design/PLUGIN_SAMPLE_ACCEPTANCE.md)，仍为待实施：
先按 3/4/3 三批验证各包的实际功能、命令/帮助/配置、启停与卸载，再并发覆盖普通任务、插件长任务和管理操作。
提供工具或模型任务的插件还须由普通中文需求触发；纯展示插件用 TUI 操作验证，不强行增加模型调用。
用量等显示遥测不进入模型请求或调度判据；事件、目录和文件读取均守 owner/run 权限。
常规调用使用官方 MiniMax-M2.7；图片文字可用本地 OCR，需要模型视觉理解时用同一密钥引用的官方 MiniMax-M3。
每个包均覆盖故障与清理，全部卸载后对照核心能力和新会话请求基线；任一失败保留原证据，不以另一个样本通过抵消。
样本只用合成文件、测试页面和专属资源，结果由被测 my-agent 自行产出；日志、截图与个人配置不进仓库。
Audit/摄取不列入本轮新增验收；共享模块既有回归按改动影响保留，不能因此删除既有功能测试。
十步逐批验收的场景、证据及进入下一步条件见 [执行 Goal 与测试矩阵](docs/tasks/REFACTOR_PLUGIN_GOAL.md)。

## 重点定向回归入口

- runner 正常让出：`test_subagent_runner_result_state.py` 通过真实 manager 写回和读盘，联测六种结束原因、
  旧错误清理、显式失败、缺失/未知原因及状态冲突；`ok=False` 的正常让出不应填 `runner_error` 或最近错误。
  联合直属等待、恢复、结果载荷、能力授权、来源工作者和 Compact 共 237 项通过、1 项既有 xfail。
  真实 TUI 核对递归父级的 `PENDING / interrupted`、空错误字段、精确等待身份及结果触发的续跑；
  另用真实 `/stop` 验证取消不被清成普通等待，模型分工质量单独记录。
- 探针端点：`test_backends_base.py::test_probe_endpoint_matches_transport_request` 截获实际 HTTP 出口信封，
  比较能力诊断与请求 URL；覆盖 Messages、Chat、Responses 的代理前缀、版本后缀、完整接口、尾斜杠及成功/未证明能力共 36 组。
  替身只提供协议响应，不替换地址计算；与原生工具、请求作用域和 OAuth 相邻回归共 228 项通过。
  新版真实官方 MiniMax-M2.7 TUI 完成写程序、执行和读回；一次多余参数被拒后自行修正，原失败保留。
  后台会话未持久化完整探针结果，因此地址合同与真实工具链分别留证，不把替身信封称作真实抓包。
- 后台审批桥：`test_background_tool_approval.py` 联合 Gateway 代理控制、owner 权限模式、TUI 队列与后台活动测试。
  核对原始请求批准/拒绝、跨会话拒绝、claim 换轮失效、无接收方关闭式失败、缓存隔离；主审批不能扩权到子代理控制入口。
  Goal 暂停仍允许当前审批；回合中断取消等待，明确任务停止另验资源收回。真实验收从 TUI 启动 Goal，
  未批准前核对无执行，正常面板批准后读取原调用结果；拒绝及旧 PTY 续用分别留证。
  本片 428 项定向通过。真实 TUI 已分别证明暂停后批准原调用、跨 claim 读写同一 PTY 和拒绝不执行；
  程序仅启动一次、原终端正常退出，30 项实际输出与报告逐项一致。测试者没有执行任务程序或修改产物。
- 插话存储组合：联合 `test_runtime_guidance.py`、Gateway 控制与输入交付、主子恢复、Compact 和终态测试。
  故障注入直接指向提交批次组件或账本投影；保留提交后部分回执写入失败、确认重放、旧格式迁移和改绑半写入样例。
  新组件的独立导入不得加载聚合 Store 或运行执行器。实际 TUI 必须在同次短输入操作内确认完整回显并提交，
  保存提交时的 running claim、精确输入回执、模型原生输入和最终消费状态，区分忙碌插话与任务结束后的追问。
  本片定向 2,362 项通过；综合全仓 17,116 项通过、35 项 xfail、22 项跳过；该全仓结果早于本轮控制修正。
  旧 Goal 暂停后 PTY 继续不算资源停止失败，当前控制语义另行验收，不能由历史结果替代。
- PTY 生命周期补修定向覆盖 `test_pty_sessions.py`、`test_gateway_conversation_control.py`、
  `test_orchestration_cancel_subagents_tool.py` 及公共进程终止、线程取消和导入边界：
  跨模型回合资源停止、同会话不同任务、跨 owner/thread、精确 attempt、Popen 前后取消竞态、自然退出 PID 不再发信号。
  Goal 暂停/清除保留当前执行；有无 Goal 或目标 paused 时的 interrupt 都不能停止独立资源。
  真实 TUI 分开验证：暂停 Goal 后当前链及 PTY 继续、回合结束后没有 Goal 自动续跑；
  中断回合后独立 PTY 保留；明确 `/stop` 后所属资源停止，恢复原程序保留原始记录前缀。
  另一窗口同期采样用于确认隔离。不得由测试者执行采样脚本或补报告；旧暂停后静止的观测仅是旧实现记录。
  后台恢复还须验证策略、冻结快照和原生工具 Schema 均含获准的终端工具；显式配置或 owner/task 禁用仍有效。
  `test_background_main_agent_runtime.py` 联合进程/PTY 测试覆盖 Goal、子代理、定时和 Audit 唤醒目录，真实复验接续原未完成任务。
  当前两组定向分别为 444 项控制与 267 项目录/工具测试；真实子代理续采保留 19 条后采满 30 条，主代理 PTY 中断后继续并收尾。
  后者恢复轮走文件查询，未覆盖旧 PTY 句柄的后台读取；模型自设时限耗尽及报告错误均留为失败，详见 STATUS。

- 线程、消息、任务关联与 Audit 组合：核对绑定并发、Compact CAS、幂等追加、游标分页、终态不复活、
  命名工作修订及进度退休，并联测 Gateway、子代理和 Memory 的实际领域接口。
  最新定向 2,206 项通过、28 项既有 xfail、1 项 Linux 平台跳过；故障替身指向新组件，不保留旧 API。
  实际 TUI 已验暂停/压缩/重连续采、两路长命令并行、普通后续指令和用量；失败及未测范围见 STATUS。
  实际输入必须核对 canonical 用户消息与原生请求。tmux 批量文本使用括号粘贴，确认完整回显后提交；
  授权弹窗可能改变输入焦点，拟发送字符串不等于已接收。提交前任务已结束时只计后续指令，不计运行中插话。

- Goal、观察、唤醒与进度领域：联合目标编辑/预算/恢复、发布顺序、观察扫描、策略失败/退休/GC、
  插话与子代理回传测试；工具 mock 和故障 monkeypatch 直接指向所属领域，不能沿旧 Store 补导出。
  独立导入检查组件不加载 Store 或调度执行器；跨域旧账归档保持同一时间、保留期与原顺序。
  定向 1,236 项通过、28 项既有 xfail；新版双 TUI 验证约 98 秒暂停静止、压缩重连、原程序真实续采，
  并行分批生成各 2,000 条 CSV/JSONL 后程序核对全部编号、数值、中文、时间、样本与 SHA-256。
  报告误述取消原因、临时脚本目录错误与被拒绝的状态面读取分别留证，不冒充完整任务质量通过。

- 存储上下文与执行租约：联合索引、账本 GC、线程中断、后台运行、Goal 恢复与 Gateway 错误测试，
  验证原子领取/续租/终态、同任务恢复、坏账报告、惰性指纹缓存、动态能力读取与原路径。
  `initialize=False` 初始化及缺失线程/租约读回不能创建目录；组件独立导入不能加载组装入口或执行器。
  本片联合 1,064 项通过、4 项既有 xfail，追加只读打开合同一项通过；259 个原函数/方法中两处构造函数单独审阅，其余逻辑比较一致。
  新版双 TUI 验证约 95 秒暂停静止、压缩一代重连、原程序实际续采到 12 条，另一会话两路各 10 条、重叠 104.01 秒。
  租约终态、子结果唤醒、插话、目录隔离、原生投递去重与费用入账分别核对；报告的采样路叙述错误保留。

- Gateway 请求组件：联合会话上下文、Compact、前台历史、请求错误、Goal 恢复、模型选择和后台运行测试。
  历史提交、写前绑定与输入渲染的替身直接注入各职责模块，不给旧导入增加转发；错误字段和持久格式保持。
  `test_runtime_module_boundaries.py` 实际调用共享历史选择器后检查未加载 Gateway，另核对新组件不加载网络或执行器。
  本次 666 项定向通过，110 个定义/常量的搬移前后逻辑比较一致；该证据不替代新版真实 TUI 的暂停、
  压缩、重连、后台交付、插话和目录隔离验收。原始快照、差异与真实日志保留在仓库外。
  新版双 TUI 已验证暂停四条后约 111 秒静止、Compact 一代并重连、原程序实际续跑至十二条；
  另一会话两路各八条采样重叠 84.021 秒，插话与后续更新未改原 CSV。原生历史按精确 request/part 无重复。
  初次报告遗漏合计和时间心算错误保留；后续程序复核只修正部分数字，两处间隔范围仍错，不计完整任务质量通过。

- Gateway 流式边界：联合 streaming、verbose_progress、foreground_transcript、thinking_archive、
  main_activity 和恢复测试，验证迟到读取、审批缓存、事件顺序、插话、脱敏和思考归档；猴子补丁须指向
  实际组件，不通过旧请求模块导出。前后台超窗继续由现有真实 store/替身运行器验证携带、代次与取消。
  纯 carry 与流模块能独立加载，不引入执行器；真实多 TUI 仍须逐路核对模型源、输出、控制和原生账本。

- 配置为空时不得隐式选择 echo；需要本地后端的夹具必须显式配置。默认值、工作区列表、Goal revision
  和界面文案断言与当前合同同步，不能恢复已删除的兼容语义来迎合旧测试。
  `test_home_runtime_bootstrap.py` 与 `test_r103_run_reuse_no_split.py` 联合验证同任务复用 canonical run、
  新请求身份、归档终态和目录准备失败的 attempt 收口；不放宽终态断言。
  历史不可读与任务绑定冲突在唯一错误表分别登记，保留原唤醒、禁止猜身份或重放未知副作用。

- 后台交付边界：`test_background_owner_delivery_commit.py` 联合后台运行时与历史快照，验证
  未声明路线的 canonical 交付、声明但不可用时保留外发义务、整封/纯附件冻结、已发送只补本地、
  v1 载荷重投、commentary/final 去重、终态抑制和精确审计回执。独立导入不加载调度器或网络后端。
  实际 TUI 核对父子结果返回、Goal 暂停/恢复、重连后最终回复与原生历史身份；外部 IM 的失败重投
  仍由已有替身合同验证，不冒充真实 IM 发送通过。

- 后台执行边界：联合 `test_background_main_agent_runtime.py`、`test_background_history_snapshot.py`、
  `test_background_owner_delivery_commit.py`、`test_thread_model_selection.py`、`test_cli_resume_contract.py`，
  验证同片溢出重试、八次压缩公平让出、正常/取消/异常原生历史、模型冻结和技术续跑来源约束。
  独立导入检查执行模块和纯续跑判据不反向加载调度器。真实 TUI 并行覆盖父子接续、Goal 压缩恢复、
  执行中停止及新目录隔离，不能用原候选版本的通过结果代替这批执行器验收。
  本批五路真实 TUI 已留证：Goal 保留暂停前四条、压缩重连后补齐六条；受管后台进程停止后不再写入，
  续做保留前六条并补齐十二条。定时作业独立于前台 `/stop`，不能把中断等待当作取消定时任务；
  此类任务须经 TUI 工具显式清理。字段计算通过与模型写错时间、采样间隔失败分别记账。

- 后台准备边界：`test_background_context_runtime_errors.py`、`test_background_main_agent_runtime.py`、
  `test_background_owner_delivery_commit.py` 联合 Gateway 控制、上下文用量、TUI 模型统计回归。
  保持未压缩历史完整、detached 任务创建锚点与 lineage、读取失败不调用模型、展示统计不进入上下文。
  `test_runtime_module_boundaries.py` 在独立进程检查合同、策略、上下文和历史模块加载不引入调度或网络后端。
  真 TUI 增量复测子代理返回后的后台接续与 Goal/Compact；文件搬迁的单测不替代真实验收。

- 首次 Goal 目录：TUI 控制传输、HTTP 持久回执、Goal 初始化、普通请求目录和 store 绑定联合验证。
  覆盖模型菜单先建空线程、目录与 roots 同步、相对/缺失/外部/远程 owner 拒绝、已有目录不变、
  同 ID 改目录冲突及 v1/v2/v3 摘要防篡改。实际 TUI 分两路验证 owner 内目录执行与 owner 外拒绝，
  拒绝必须发生在创建 Goal 和调用模型前；不能先发送普通聊天替首次 Goal 补目录再计为通过。

- 仅思考响应：Chat/Messages 的流式与非流式不得因无正文丢弃有效思考、用量或隐藏重试；
  真空白仍报错。`test_native_tool_use_ir_messages_flow.py` 验证两次无工具续跑逐条保存、
  OpenAI 实际出站回放、下一工具轮和最终保存不重复；`test_response_decision_native_tool_use.py`
  验证坏工具修复不回放未执行工具。联合原超时探针、截断、插话及中断历史回归。
  真实抓包先检查仅思考响应是否漏入下一请求，再评价真实任务完成，不能仅凭缓存高称通过。

- 渠道失败提示：`test_tool_failure_channel_hint.py` 联合错误语义与工具执行回归，覆盖测试/编译非零、
  参数/状态/权限拒绝、取消、未知、真实网络不可用、重复回执和新回执覆盖旧失败。
  错误正文不能提升为控制码，缺 call_id 不猜新事件；关闭阈值和每工具一次不变。
  真实 TUI 核对失败码、下一次请求是否误加换渠道提示及实际排错进展；不能把提示过滤通过当作模型不再循环。

- 后台失败退避：`test_gateway_lane_retry.py`、`test_gateway_loops_resilience.py`、
  `test_background_main_wake_recall.py`、`test_model_unconfigured.py` 与会话模型选择联合验证。
  覆盖缺配置长时间不重跑、模型引用删除/恢复、精确旧会话改选、默认选择不串会话、零值冷却、
  远端拒绝不误判本地缺配置、同 owner 健康车道、跨 owner、短锁与有界回收。
  联合 `test_background_supply_backoff.py` 和 Goal 测试核对 scheduler 不提前关闭目标/消费 wake；
  真实执行错误和额度限制仍受原保护，不把原已暂停或受阻目标无条件激活。
  真 TUI 在未配置会话设置目标，再通过 /model 选模型，核对原目标恢复、唯一最终回复及原 wake；
  另一路正常任务并行，不能把手工改任务文件或替身模型当作真实恢复验收。

- 重启与持久回执：`test_tui_worker_paths.py` 验证已提交消息在 PID 暂不可见时仍读取原 terminal；
  没有终态沿既有超时返回，不再入队；未提交请求仍报告服务停止。真实 TUI 将重启与消息投递交错，
  区分队列提交、实际执行、模型 final 和前端展示，不把服务启动命令退出当作已经就绪。

- 旧计划续写与多用户路径：`test_task_progress_advisory.py` 验证精确旧账更新、缺省状态保留、跨会话、
  子代理/独立后台目标拒绝和无隐式重绑；`test_gateway_chat_conversation_context.py` 验证本地队列的
  自定义 owner、外部/未知来源、final/实时/历史路径一致。真实 TUI 用原会话追加验证笔记并索要完整路径，
  对照 native final、canonical public row 和终端画面；不能把宿主脱敏误记成模型漏答。
  路径样例必须真实含 owner/request 标识，分别覆盖绝对路径、Windows 路径和相对目录；
  仅用不含标识的示例不能检出第二层替换。外部来源不因该修复暴露完整宿主路径。

- 进度部分更新：`test_task_progress_coverage.py`、`test_task_progress_advisory.py` 与派工对账测试，
  覆盖只补备注/元数据、空状态、各规范状态、更正标记、新项默认及模型/展示一致；没有 ID 仍按参数错误返回。
  已完成项须先写入旧备注，再更新并读回新备注；只断言状态未变或空备注成功不算覆盖。
  原生 Schema 必须明确 ID 必填，不能为了部分更新把全部字段都标成可选；标题/状态仍允许按需更新。
  真实慢任务的计划状态和原工具回执并行核对，旧数据不推测重写，不以勾选进度替代产物验收。
- 显式采样：`test_provider_sampling.py` 联合三种 backend 测试，覆盖默认省略温度、显式零值/范围端点、
  单次摘要覆盖、工作片冻结与子代理继承。慢模型客户端对照严格串行，切换前检查原请求及服务端槽位退出；
  真实出站诊断只写私有测试目录，不记录认证头、不改请求协议，不把参数回放当完整 TUI 任务通过。
- 批次执行事实：`test_current_turn_execution.py`、`test_native_tool_use_ir_messages_flow.py`，覆盖
  只追加当前批次、Compact 轮号重置后的身份区分、未知副作用、批准来源、有界省略及全轮核验保留。
  连续请求逐字节保留此前缀和全部工具对，旧会话不强制清理。真实出站核对新增事实大小及实际任务进展。
- 代理树重复查询：`test_agent_tree_model_view.py` 联合工具重复观测回归，验证仅时钟/心跳变化继续计数，
  实际工具进展、终态和产物改变重新计数；原查询结果、权限和生命周期不改变，不用耗时判死。
  子代理查自己的子树时，从规范范围裁决排除自身查询活动；主代理显式查询该孩子仍保留其真实进展。
  正常模型并行验收与慢模型串行对照同时进行，不能把正常模型的轮询浪费漏记为慢模型专属问题。

- 子代理模型续派：`test_orchestration_background_dispatch.py`、`test_model_profiles.py`、
  `test_thread_model_selection.py` 及 worker/timeout 测试。覆盖 Gateway 无默认模型、父子异模型、
  child thread 改选后的恢复、并发显式注入和并行工具线程的依赖传递；旧捕获函数回放须能重现配置/连接不一致。
  真实验收区分普通父子交接与 coordinator 等待孙代理后的重新派工；没有真正产生孙代理的不计后者通过。

- 中断历史：`test_native_tool_use_ir_messages_flow.py`、`test_cli_run_conversation.py`、
  `test_gateway_chat_conversation_context.py`、`test_subagent_runtime_compact.py`、
  `test_background_main_agent_runtime.py`、`test_background_owner_delivery_commit.py` 联合验证
  原生调用/结果保留、未知副作用占位、空正文与异常不改成功、后台静默/外发失败仍留事实而不伪造送达、
  原请求幂等、同一 repair 补交。真实 TUI 用执行中 Esc 后继续，核对下一轮真实输入和已发生的工具事实；
  一路慢模型不派子代理，正常模型并行验证父/子与普通后续轮。历史旧缺口不按显示文字补造成功。

- 客户端计时：`test_gateway_client.py`、`test_gateway_admission_wait.py`、`test_tui_worker_paths.py`，
  覆盖时钟前跳/回拨、失联超时和活动租约续期。真实 TUI 可隔离替换客户端模块时钟注入跳变，
  不修改系统时钟、不影响 Gateway/模型计时；单独记录注入已发生、真实终态及任务产物，不能把替身当真实模型。
- 账号认证：`test_model_oauth.py`、`test_model_oauth_transport.py`、`test_tui_model_menu.py`，
  联合模型配置/共享目录/会话选择/原后端测试。覆盖跨 owner、冻结引用、刷新轮换、取消和退出竞态、
  私密参数保留/清除、重定向拒绝及协议复用。真实 TUI 的设备码确认另验；替身不作为实际账号权益证明。
- 用量增量：`test_model_call_ledger.py`、`test_tui_model_metrics.py`、`test_reproject_model_usage.py`，
  成功/异常/取消共用结算；累计容器重建换代，来源切换不重复算，旧账与缺报不得估算重写。
  真 TUI 中断后追加、Goal 后台交接、子代理及 Compact 必须按 provider 分项对账。
- 慢模型额外排队：模型配置与首事件估算定向测试，默认 0、按模型覆盖、无穷大/布尔/非法值拒绝。
  真实单槽并发等待、滚动输入、Esc 分开验；额外预算不能修复 schema 编译错误或输出截断。

- 工具重复恢复：`test_tool_guardrail_gate.py`、`test_tool_call_guardrail_runtime.py`，覆盖 300 次自身拒绝
  与 400 次成功调用的持续计数/提醒、不同归档引用同正文及相同预览不同尾部。
  不清计数、不挤掉原观测、真实失败/不同结果/实际写入及零阈值；拒绝经真实归档和 native 投影后仍有
  计数及换路说明。`test_tooling_filesystem.py` 验证行/字符非文本失败提示及无额外文件转换。
  真实 TUI 复验单文件动画与正常连续工具任务；没有触发重复门的真实任务只算正常链路验收。

- 模型资源与后台策展：`test_provider_request_scope.py`、`test_memory_curator_v2.py`，覆盖同端点前台
  优先、退出释放、pending/游标保留、pre_compact 屏障、request-local 预算、取消连接及旧请求未退出不重试。
  真机只开一路本地慢模型且不派子代理；官网正常模型可并行对照。缓存核对需同时查推理服务槽位日志，
  外部请求/代理别名和缓存容量不能从 TUI 百分比推断。
- 后台策展会话头与失败分类：`test_memory_curator_v2.py` 用真实 OpenAI/Anthropic 兼容后端加本地 HTTP
  回放，验证策展 run 自带非空会话头、值只由 owner_id + run_id 派生、同 run 重试同值、不同 run/owner
  不同值、run 结束 ContextVar 复位；去掉 `_execute` 的会话绑定时这三条用例必须失败。
  `test_curator_timeout_observability.py`、`test_curator_timeout_adaptive.py` 锁定供应商调用阶段的
  ValueError 归 `CURATOR_MODEL_FAILED`（结果、state.json、失败诊断一致），解析失败仍是
  `CURATOR_SCHEMA_INVALID`；失败诊断附脱敏后 ≤200 字正文，整条 warning ≤300 字符且可解析。

- 状态读取：`test_agent_tree_model_view.py`、`test_agent_tree_three_layer_status.py`、`test_orchestration_tools.py`，
  覆盖规范原状态、scope 裁决、恢复路径不外泄、实际报告与缺失报告、八节点直接可读及大树省略计数；
  与 `test_tool_context_reducer.py` 联合核对输出外置后仍保留状态和精确逻辑回读入口。
  真实 TUI 验证运行中查询、完成后交接和真实文件读取，不以最终 DONE 替代工具调用证据。
  无活动任务目录时验证当前会话过滤，显式主请求根验证 parent_id 子树；已有终态报告需实际读取。
- 思考预览：`test_tui_renderer.py` 覆盖流式折叠行数、接收字符数、无换行长段落和窄终端，
  与 `test_thinking_display_boundaries.py`、`test_tui_complete_detail.py` 联测；真实 TUI 需捕获多帧计数增长。

- 派工一致性：`test_orchestration_dispatch_state_contract.py`、`test_subagent_prompt_contract.py`、
  `test_subagent_role_templates.py`，覆盖启动/运行/终态混合快照不推导父级动作、角色正文隔离、冻结自定义角色、
  主代理保留自身分工与用户限制；`test_tool_context_reducer.py` 验证精简回执保留唯一动作建议。
  真实 TUI 分开记录父级独立工作、分层是否如实创建、活跃范围是否重复写、确实依赖结果时是否正常等待。

- 子代理交接：`test_subagent_registered_artifact_handoff.py`、`test_subagent_output_alignment.py`，
  覆盖自然/结构化结果、孙级身份、最新文件、删除、日志排除、账本链接拒绝、cwd 与相对/绝对路径一致、
  不从输出声明增权、不从内部同名文件隐式搬运。真实 TUI 另核对创建谱系与完成信封中的实际路径。
- 补丁交接：`test_artifact_registry.py`、`test_tools/test_filesystem_tools.py`，真实 handler 到归档再到自然收口，
  覆盖新增、修改、移动、删除、部分失败、同路径不同历史 artifact_id 与当前删除状态，保留权限和执行事实。

- 生命周期：`test_dispatch_liveness_and_revive.py`、`test_subagent_runner_result_state.py`、`test_direct_parent_lifecycle.py`。
- 宿主停止：`test_subagent_process_control.py`、`test_shell_orphan_kill.py`、`test_orchestration_cancel_subagents_tool.py`。
  受控进程验证另开 session 的写入者、忽略 TERM 的后代、独立兄弟保留和无句柄退出核对；
  未确认回执不得标记已终止或触发重派。它们不替代真实 TUI：还需在子代理长命令运行时暂停，
  观察文件保持不变，再从原会话恢复，分别核对原记录前缀和实际命令续跑，不由测试者补产物。
- 父子并行：`test_direct_parent_lifecycle.py`、`test_runtime_guidance.py`、`test_subagent_activity_diagnostics.py`、
  `test_runner_session_pool.py`，覆盖逐个完成、同时释放去重、模型答复/登记等待竞态、忙父级交接、
  慢流不误杀、阶段/审批诊断、旧 attempt、进度快照覆盖、通知重试和心跳回调失败；真实 TUI 组合另列。
- 退出与积压：`test_executor_exit_recovery.py`、`test_closeout_recovery_paging.py`，包含 exact attempt、慢执行存活、
  未知副作用封存、超过分页窗口、消费去重和重启游标；实际模型/故障注入仍需独立 TUI 证据。
- 历史：`test_conversation_store.py`、`test_background_history_snapshot.py`。
- 存储组合：`test_conversation_store.py` 另覆盖两个独立实例并发提交同一用量及累计快照，核对唯一行、首次时间和费用；
  联测 `test_conversation_context_usage.py` 的代次 CAS、`test_conversation_goal_tools.py` 的共享时钟/小数余量/重启，
  以及 `test_runtime_module_boundaries.py` 的领域独立导入。旧调用、getattr 与测试替身须一并迁移，不保留旧方法转发。
  本片真实 TUI 须核对暂停期间 Goal 秒数、恢复后的原程序续做、Compact 独立用量和主子账本归属；原始证据留仓库外。
- 目标：`test_conversation_goal_tools.py`、`test_goal_lifecycle_recovery.py`、
  `test_agent_goals.py`、`test_background_main_agent_runtime.py`、`test_gateway_conversation_control.py`、`test_run_audit_terminal.py`。
  中断增量另联测 `test_tui_input.py`、`test_tui_agent_navigation.py`、`test_r103_ledger_selfheal.py`：
  空白补全、前后台插话、Esc 与明确暂停分离、同任务换代、恢复总账及历史关闭事件保留。
  后台参数构造必须走到真实回执消费，不能仅断言邮箱写入；先后完成的历史目标不得误触发并行冲突迁移。
  子代理在 Goal 后台轮创建再回报时，持久 task ID 不需要伪造 user 消息；并测 active/complete 与混合普通请求，后者真实缺失仍报错。
  覆盖默认工具可见、无工具/无 Todo 的安全续跑、审批/暂停/错误边界、旧绑定显式迁移、
  命名目标的精确回合上下文、前后台共享时钟；普通模式不得因此自动续跑。
  另覆盖每代理一个未结束目标、父子计费与权限隔离、编辑版本冲突、暂停后保存不恢复、
  子 Goal 在同一 attempt 中跨轮与 Compact 续接、独立历史不覆盖；实际草稿键盘操作仍须 TUI 验收。
  当前真实 TUI 已覆盖主子保存、放弃、编辑中停止，以及旧版本冲突保留草稿；详情与未测组合见持续目标设计。
  `test_saved_goal_guidance_reaches_its_agent_and_can_cross_provider_boundary` 复现运行中改主目标被子代理误领，
  覆盖主/子消息隔离与提交模型、确认消费完整链路；共享 root task 不能授予父级邮箱。
- 模型：`test_model_profile_tool.py`（manage_models 工具）、`test_model_provider_management.py`、`test_provider_sampling.py`、`test_model_unconfigured.py`；
  未配置可进设置但不发请求，发布默认值为空，用户显式选择仍保留。
- TUI：`test_tui_interaction.py`、`test_tui_markdown.py`、`test_tui_pty.py`。
- 模型统计：`test_tui_model_metrics.py`，覆盖协议缓存分母、缺报、重放去重、明细裁剪、重试、主子隔离、重连和宽字符窄屏；独立压缩成功/失败均落账，绑定工作片的不重复结算；统计字段不得影响模型上下文。
- 开发检查：`test_contract_test_pyramid_gate.py`。

文件位于 `agent_py_agent/tests/`；改模块时补充对应边界用例，不以此短列表代替所有模块回归。

## 真实 TUI 记录

工具正文完整性：`test_tool_output_externalizer.py` 必须经过生产 `ToolExecutor` 与
`archive_tool_output_projection`，而不是仅手造完整 `ToolResult` 给 reducer；覆盖预览阈值以上的
完整文件、分页及继续游标、归档读取 JSON 和显式保留正文，同时保留大输出外置/脱敏回归。
慢模型复读验收沿原始任务和输入文件建立独立 owner/TUI，记录真实出站回执、重复调用、
产物与独立测试结果；不更改测试项目或用硬停计为通过，不并发占用慢模型。
`test_tools/test_shell_tool.py` 另从 Schema、规范执行入口及真实本地进程验证长命令，
与空输入、危险命令、owner 沙箱、超时和非零退出联测；不把旧长度拒绝当安全边界。

后台进程重复观测：`test_process_sessions.py` 回放 33 次 uptime 变化但状态/输出不变的等待，
并核对原始结果哈希、软提示频率、日志同尾增长和退出后重置；真实 TUI 单独记录模型是否采纳提示。

每次公布 tmux 名称；使用隔离测试用户和同一 Gateway。记录开始/结束、版本、供应商/接口、会话与请求身份、实际工具结果、最终产物、失败和未测边界。不写真实密钥或私人对话。

本轮慢模型只启用一路 TUI、不派子代理，优先验证长等待、流式、插话与停止；正常远端模型可多路并行。
本地缓存诊断同时核对界面最近一次比例、输入用量和推理服务实际预填充，不用延迟反推缓存，更不把缓存未命中当成上下文丢失。

验收分为启动/简单工具、连续多任务、多子代理、长上下文与慢模型组合。普通真实模型测试使用官方 MiniMax-M2.7；协议兼容测试按明确目标选择服务商，不静默改用户日常模型。

默认配置行为必须核对实际合并结果：旧安装若把完整默认 `system_prompt` 或工具延迟目录另存为显式
覆盖，仅升级 wheel 不会替换这些值。测试可在备份后移除测试配置中已确认是旧默认副本的字段，
不能直接覆盖用户定制提示。模型声明、界面 Goal、目标账本、任务绑定和最终工具结果分别取证。

## TUI 随 Gateway 升级原地切换与部署工具适配器重启（2026-09-25）

- 第一版（退出界面再 exec）真机验证时只核对了进程路径，漏掉两处：退出会闪回 shell；原命令行不带会话编号，`_setup_session` 会建新会话，界面回来是空对话。改为原地切换并用 `MY_AGENT_TUI_HANDOFF` 传会话与原始终端设置。
- `test_tui_upgrade_follow.py`：目标判定、七项空闲事实、只排到 UI 线程且两次复核、忙时撤回、exec 失败保留旧界面、会话与 termios 经环境变量传递、新进程暂存启动输出、未接管终端退出时补发复位序列、畸形或外来载荷忽略、Windows 只提示、Gateway 状态带 `runtime_prefix`。
- `test_cli_chat.py::TestChatCommandRuntime::test_in_place_handoff_keeps_session_and_loads_history_before_first_frame`：新进程沿用会话、就绪后同步读历史、跳过可见连接流程。
- 真实验收方法：在 tmux 里的 shell 中用新版 runtime 起一个空闲 TUI，再部署同一提交的下一版切 Gateway；每 0.1 秒记录进程路径变化与“正在切换”提示消失的时间，核对画面没出现退出横幅/shell 提示符、会话记录数不变、切换期间 send-keys 的字符出现在新界面输入框；最后退出 TUI，用 `stty -a` 核对 icanon/echo 已还原。
- 第二版真机（隔离 Gateway 127.0.0.1:8431、临时 home、两份同提交 runtime）首轮发现两处：新进程先把 stdout 换成缓冲再判断终端，误入 plain 模式卡在 `input()`，切换中键入的字符被它吞掉；启动器的启动页会清屏。已修并补回归（`test_in_place_handoff_detects_the_terminal_before_holding_output`、`test_boot_frame_is_skipped_during_an_in_place_handoff`）；同轮已确认：783 次采样全屏从未退出、无退出横幅、同一进程换到新 runtime。
- 部署工具（仓库外）：同机有 IM 适配器在跑时，先比对它实际加载的 agent_py_agent 模块在新旧安装间是否有变化，没变就不重启（IM 完全无感），变了才重启并核对同版。

## 未知执行轮的会话内恢复 `/recover`（2026-09-25）

- 真实触发：用户会话里模型在回合中执行 `my-agent gateway restart`，Gateway 自杀；启动恢复把该回合 run/attempt 记为 unknown，
  自动续跑报 `ACTIVE_TURN_OUTCOME_UNCERTAIN`，此后每条新消息续同一 active 工作任务都在 `create_attempt` 被拒，没有任何用户出口。
- `test_turn_recovery_control.py`：处置值只认结构化取值；`/recover` 只读列出未确认操作（已成功的不列）且不改库；
  unknown 挂载抛 `RuntimeRecoveryRequiredError`（`RUN_RECOVERY_REQUIRED`，仍是 `RuntimeConflictError`）且客户端文案指向 `/recover`；
  `/recover recorded` 后 attempt=recovered、run=created、事件带 operator 与处置、下一次 `create_attempt` 成功、再执行显示无需恢复；
  无 thread/无阻塞/非 unknown attempt 的阻塞分别返回无需恢复或 `RUN_RECOVERY_REJECTED`；Gateway 按已认证 scope 解析 thread 后分派；
  TUI 序列化与本地模式拒绝。
- 真实验收方法：部署后对卡住的会话先发 `/recover` 核对列出的工具与开始时间，再发一个处置值；随后发一条普通消息，
  核对该请求 done、运行库新 attempt 的 metadata 带 `recovered_from_attempt_id`，旧 attempt 的未确认操作没有被重做。
- 真实验收（2026-09-25，`62b3329cf`，双机 `runtime-step11z-cb3cb7a7`，用户批准对其卡住的会话执行）：经 Gateway `/control`
  以该会话身份发 `/recover`，只列出 1 条 `run_command`（执行中断，开始时间与卡住那一轮一致），运行库未变；外部事实核实为那次
  Gateway 确实已重启后发 `/recover recorded`，attempt=recovered、run=created、`attempt_recovered` 事件的 operator 与处置正确。
  用户 12:06 发下一条消息后：同一 agent run 开出 generation 2，metadata 带 `recovered_from_attempt_id`，旧 attempt 那条
  EXECUTING 操作在换代时转为 UNKNOWN、没有被重放；新一轮 12 条工具操作后 12:12 done，请求进入 done，期间 Gateway 进程号未变。

## 审计 `requests` 主题（2026-09-26）

- 背景：用户说“这种东西我希望以后是我的 my-agent 能帮我解决”——飞书没绑定管理员就发消息，全部 `MODEL_NOT_CONFIGURED`，当时靠开发者翻请求文件定位。
- `test_audit_requests_topic.py`：本人范围只看自己的请求，宿主写入的 `owner_id` 优先、旧记录按会话归属，同一请求两份去重，
  30 小时前的记录不进 24 小时窗口，输出不含 prompt/回复/用户文案；`all_owners` 未开许可被拒，开许可后含未归属记录与错误码计数，
  管理员附带密码已设与已绑定私聊（普通用户看不到）；`current_thread` 按会话过滤；不在 Gateway 回合里报告不可用；
  响应 `owner_id` 就是执行 owner 的规范编号。
- 真实验收方法：部署后在 TUI 里问 my-agent“我刚才在飞书发消息报错了，帮我查一下原因”，核对它调用 `audit_records`
  （topic=requests），在需要查飞书用户时先请你允许跨用户审计，再说出 `MODEL_NOT_CONFIGURED` 与“先在私聊发 /admin”的结论。
- 真实验收（2026-09-26，隔离 8432、真实模型、管理员已开跨用户审计）：第一轮暴露两处问题——未绑定飞书私聊的失败回复没带 `/admin`
  指引（执行 agent 是按用户隔离的，owner 字段被改写，原判定不成立），my-agent 只查本人范围后转去翻文件、结论只提 `/model`。
  修正后：失败回复带上 `/admin` 指引；my-agent 先查本人范围、按软提示改查 `all_owners`，两轮工具给出“2 条 MODEL_NOT_CONFIGURED、
  已设密码但未绑定、在飞书私聊发 /admin <密码>”。`test_admin_identity_gateway.py` 增加按用户隔离 agent 的指引用例，
  `test_audit_requests_topic.py` 增加软提示用例。

## Gateway 安全重启第一期（2026-09-26）

- 背景：用户要求代理能自己重启 Gateway 且 TUI/IM 不出事；此前代理在回合里执行 `gateway restart` 会切断自己，会话卡在 unknown。
- `test_restart_gate.py`：关口打开时准入并计数；关闭时工具停在领取前、重开后才开跑；等待中被中断则不准入，协调器返回
  not_started 的 `CANCELLED`（`action=gateway_restart_drain`）且不执行工具；执行中计数的等待可超时也可成功。
- `test_gateway_safe_restart.py`：同一目标的重复请求合并；排空完成后冷却拒绝、冷却 0 不限；同一会话 10 分钟 3 次后防循环、换会话或过窗放行；
  第一段等在飞回合、第二段等执行中工具，成功后关口保持关闭；第一段超时继续、第二段超时取消并重开关口；完成标记只在旧进程退出后消费一次，
  过期标记丢弃；续跑通知只写到数据根内的发起会话、按请求编号去重；`planned_restart` 恢复不加延迟、原因为 `gateway_safe_restart`、排在新请求前；
  服务循环排空返回重启报告、状态投影阶段；排空超时撤销请求并通知发起会话；`planned_restart` 分类为 stopped 不记失败；
  接班命令带 `--after-pid`、托管时返回 75 不拉进程；`/restart` 非管理员拒绝、管理员写入指向本进程的请求并合并重复。
- 第二批（同在 `test_gateway_safe_restart.py`）：排空时派发只给待处理请求写 `admission_wait_reason=gateway_restart_draining`、不认领；
  终端 `gateway restart` 在 Gateway 未运行时交回启动路径、在托管自己的工具进程里拒绝且不写请求、等到新进程号 running 返回 0、
  本请求被取消或冷却中返回 2；`test_gateway_commands.py` 的先停后起用例改为显式 `--force`；`test_tui_upgrade_follow.py` 核对
  排空阶段每次轮询都提示、cancelled/缺失不提示。
- 第三批（TUI 续跑边界与确认框作废）：`test_gateway_safe_restart.py::test_resumed_claim_writes_one_turn_resumed_boundary_before_new_output`
  从真实恢复标记出发（`planned_restart` 重排、接班认领同一请求号），核对 chunk 流只多出一条 `turn_resumed`、字段只有 t/kind/cause、
  排在续跑代次任何输出之前；普通请求不写，认领时已被停止的请求不执行也不写。`test_gateway_streaming.py` 核对边界先刷出缓冲进度、cause 去空白。
  `test_tui_runtime.py` 三组：旧确认框本地作废且不经 sink 写回；同轮同序号的新代工具卡用 `:resume1` 块号、新确认正常弹出并只写回一次；
  已终态的卡不重发终态（没有 `TERMINAL_*` 诊断）；旧回复、旧思考和进行中的 Compact 按 interrupted 冻结，参数临时行收起，续跑文本另起新块；
  提示文案只按 cause 选择，未知或空 cause 用通用提示，回合结束后再收到边界不发布。`test_tui_stateful.py` 状态机新增续跑规则，
  随机交错下块号仍唯一、旧卡只中断一次、新卡正常完成。`test_gateway_client.py` 用返回 False 的旧版 typed 消费者核对该行不显示、不报错；
  `test_gateway_verbose_progress.py` 核对 IM `/progress` 忽略它并照常前移游标。变异核对：新 TUI 用例在旧 adapter 上全部失败；
  去掉块号代次后缀、终态不移出未终态登记、去掉 Gateway 写入调用，各有用例失败。真实 TUI 验收未做：需在隔离 Gateway 上让回合停在确认框
  或长命令时安全重启，核对旧框关闭、提示出现、新确认能弹出、旧工具卡显示已中断。
- `test_gateway_restart_tool.py`：不在 Gateway 内拒绝且不写文件；Gateway 内立即返回 scheduled 并记录发起会话存储根；缺原因与冷却为 not_started；
  只注册给管理员主代理且可关闭；`test_user_config_owner_scope.py` 另核对普通用户与群看不到它。
- 真实验收方法：隔离 home 与 127.0.0.1:8432 的 Gateway 上，一次 prompt 让代理重启 Gateway，核对工具回执 scheduled、回合正常结束、
  Gateway 换了新进程号、发起会话收到“重启已完成”后没有再次重启；另开一个会话跑长命令时发 `/restart`，核对等它跑完才换进程、
  回合续跑完成、没有 unknown；重启期间发的消息在新进程里照常处理且只回复一次。
- 真实验收（2026-09-26，`07fa00fb3`，`runtime-step12b-3d81454b`，隔离 home、127.0.0.1:8432、管理员审批模式 full-access，真实模型）：
  A 轮一次 prompt“请安全重启一下 Gateway”：代理只调用一次 `restart_gateway`，本轮 13.8 秒 done；第一段等到发起回合自己结束，
  第二段无执行中工具，旧 10087 → 新 11073，停顿不到 1 秒；续跑唤醒写入 1 条并被处理，续跑回合不调工具、直接告诉用户重启完成，
  全程只有一次 `gateway_restart_requested`。B 轮会话在跑 45 秒命令时由另一会话发 `/restart`：第一段一直等到回合结束（52 秒）才换进程，
  回合在旧进程里 done（重排 0）。C2 轮第一段上限临时设 5 秒，回合先跑 20 秒写文件命令：第二段 `executing_tools=1`，等命令跑完才换进程，
  新进程立即续跑同一回合（重排 1、无 10 秒延迟）执行后两条命令并 done，文件里 step-one、step-two 各一次，副作用命令没有重跑。
  C 轮同样设置但第一条是只读命令（`sleep 20 && echo`）：只读命令不经过关口，进程在它跑到一半时退出，续跑时重跑了这条只读命令，结果正确，
  只是多花了时间。证据在 `~/.my-agent/releases/step12b-3d81454b/acceptance-safe-restart/`（不含模型目录，隔离 home 已删除）。

## 托管自停闸与聊天 `/model`（2026-09-25）

- 背景同上一节：模型在回合里重启了托管自己的 Gateway；飞书用户是另一个 owner，没有模型，飞书里发 `/model` 只得到“不支持的系统命令”。
- `test_gateway_host_guard.py`：Gateway 进程写入的托管进程号经 `_subprocess_text_env` 传给子进程，降权擦洗后仍保留；
  只有目标进程号与托管进程号完全相同才拒绝，缺失、坏值、别的 Gateway 都放行；`gateway stop`/`restart`/`start --force`
  拒绝时返回 2，且没有写停止请求、没有 kill、没有启动新进程；工具里停别的 Gateway 照常成功。conftest 清掉该变量，避免在托管工具里跑测试时串宿主。
- `test_model_text_control.py`：`/model`、`/model <编号>`、`/model default <编号>` 解析为结构化操作，多余正文无效；
  没有模型时返回“请管理员共享”的引导而不是不支持；管理员共享一个模型后 IM 用户看到它（不含管理员私有模型、密钥和接口地址），
  选中后本会话生效、新会话默认不变，再设默认后才变；越界编号拒绝；两个 IM 用户的会话选择互不影响；TUI 单独 `/model` 仍留给本地菜单。
- 真实验收方法：隔离 home 与 127.0.0.1:8431 的 Gateway 上，一次 prompt 让代理在对话里执行该 Gateway 的 `gateway restart`，
  核对工具结果为拒绝、回合正常结束、Gateway 进程号不变；再以 IM 身份经 Gateway `/ask` 发 `/model`、`/model 1` 和一条普通消息，
  核对列表、会话选择和普通消息使用所选共享模型完成。
- 真实验收（2026-09-25，`b3c86a697`，`runtime-step12a-13e1fc9e`，隔离 home、127.0.0.1:8431，管理员 full-access）：
  一次 prompt 让代理用 `run_command` 执行这台隔离 Gateway 的 `gateway restart`。工具记为 FAILED、退出码 2，拒绝文案原样回到回复里，
  回合 done/completed；Gateway 进程 12:23:16 启动后一直未变，日志没有任何停止事件。从普通终端执行同一 Gateway 的 `gateway stop` 照常成功。
  以飞书身份经 `/ask`（与适配器同一载荷与身份头）：共享前普通消息报 `MODEL_NOT_CONFIGURED`、文案引导发 `/model`，`/model` 提示请管理员共享；
  管理员经 `/client/models` 共享默认模型后，`/model` 列出 1 个带“管理员共享”的模型且不含接口地址，`/model 1` 选中，
  普通消息用该模型完成，再发 `/model` 显示当前会话模型。复制的模型目录随隔离 home 删除，证据在仓库外。

## IM 管理员身份与聊天内审批（2026-09-26 合入 main）

- 背景：管理员以前只有本机 local/main，飞书用户永远是自己的 owner，IM 客户端也无法确认工具。用户决定用管理员密码在飞书
  私聊里绑定管理员身份，并用密码批准工具。设计见 [IM 管理员身份](docs/design/ADMIN_CHANNEL_IDENTITY.md)。
- `test_admin_identity_store.py`：
  - 密码文件只有 scrypt 参数、盐和派生值；文件 0600、目录 0700；任何文件里都没有明文；重新设置会换盐。
  - 过短、首尾空白、含换行或制表符的密码被拒，不写文件。
  - 同一身份 10 分钟内错 5 次锁 10 分钟：锁定期内正确密码也拒绝，别的身份不受影响，到期后恢复，成功后计数清零；
    拒绝文案只有通用句子和剩余分钟数。窗口外的失败不累计。
  - 未设置或损坏的密码文件一律验证不通过；失败记录损坏时拒绝，不放行。
  - 绑定只按 `(channel, user_id)` 精确匹配，渠道名不分大小写、用户 ID 区分大小写；重复绑定不产生重复行；绑定文件损坏时
    查询 fail-closed、写入不覆盖。
  - CLI：两次输入不一致或密码过短时返回 2 且不写文件；status/list 输出不含散列、盐和明文；非 local/main 配置拒绝且不提示输入；
    命令组缺子命令时打印帮助，不落到默认聊天入口。
- `test_admin_identity_gateway.py`：
  - 绑定后，请求和控制作用域都解析为 local/main；群聊、缺私聊类型、其他用户、开关关闭、base owner 不是 local/main 时不变；
    解除绑定后恢复。
  - 真实 `handle_ask` 处理 `/admin <密码>`：回执 `command_text` 为 `/admin ******`，整个临时目录没有明文，不写请求队列；
    同一消息重投只重放原回执。
  - 错误密码、群聊（提示撤回并更换密码）、本机终端、开关关闭都被拒；`/admin status` 与 `/admin logout` 正常。
  - 服务端只对已绑定的管理员私聊、且执行 owner 为本机管理员时开启交互审批；显式声明能力的 TUI 不变。
  - `/progress` 的审批事件只有 `kind/tool/summary`，外部渠道收敛宿主路径；IM 渲染出 `/approve` 与 `/deny` 提示。
  - 真实 `BufferedChunkStreamWriter` 等待审批：错误密码不产生决定；正确的 `/approve` 经控制回执（正文为 `/approve ******`）
    让等待方得到 `approved`，再批准返回 `APPROVAL_NOT_PENDING`。`/deny` 不要密码，得到 `denied`；未绑定时 `/approve`
    返回 `ADMIN_IDENTITY_NOT_BOUND`，而且不校验密码。
  - 以下情形都拒绝，且不写决定文件：没有待决、同一回合两条待决（`APPROVAL_AMBIGUOUS`）、其他会话或其他用户、
    本次认领之前的旧执行事件。
- `test_admin_identity_gateway.py` 末尾两例（2026-09-26）：未绑定的管理员 IM 私聊遇到 `MODEL_NOT_CONFIGURED` 时回复追加
  `/admin <管理员密码>` 指引，其它错误码、群聊、开关关闭、未设密码、已绑定都不追加，且只看本私聊自己的绑定；`/model` 只在没有可选模型时追加。
- `test_admin_identity_clients.py`：
  - 适配器：`/admin`、`/approve`、`/deny` 不写持久入站记录，也不建回复 watcher，只直接 POST 一次并回复 Gateway 结果；
    Gateway 不可达时只回“服务暂时不可用”，不重试；普通消息照常入持久队列。临时目录里没有明文。
  - TUI 与终端：在写控制 outbox 与发 Gateway 之前本地拒绝；拒绝文案不含密码；这三条命令不写输入历史。
- 回归：本地严格 gate 共 54 个测试文件（3 个新增，加所涉模块既有测试与架构守卫）1468 passed、1 skipped；
  ruff、doc sync、strict code-size、diff check、clean package 均通过。
- 真实流程验收（2026-09-26，`0bbe68d55`，隔离 home、127.0.0.1:8432、真实模型；飞书侧以与适配器相同的 `/ask` 载荷与身份头模拟私聊/群聊）：
  私聊 `/admin status` 显示未绑定；群聊 `/admin <密码>` 返回 `ADMIN_IDENTITY_SCOPE_INVALID` 并提醒撤回；私聊绑定成功、status 显示绑定时间。
  注意：工作目录内 `write_file` 属于 mutating，默认确认模式本就不弹审批，不能用来测审批。改用 `restart_gateway`（dangerous）：
  `/progress` 出现 `permission_requested{tool: restart_gateway}`，`/approve <密码>` 后本轮 done、Gateway 换进程（85871 → 87163）；
  同样请求再发 `/deny`，工具未运行（`APPROVAL_REJECTED`）、进程号不变，代理没有重试。另一身份连错 5 次后锁 10 分钟，锁定期内正确密码也拒且文案不区分原因。
  绑定身份发 `/restart` 成功换进程（77505 → 85871），`/admin logout` 后 `/restart` 返回 `GATEWAY_RESTART_ADMIN_ONLY`。
  扫描隔离 home 全部 431 个文件，测试密码明文零命中；控制回执为 `/admin ******`、`/approve ******`。
  证据在 `~/.my-agent/releases/admin-identity-acceptance-20260926/`（隔离 home 与模型目录副本已删除）。真实飞书客户端上的验收待部署后由用户操作。

## 提交前严格 gate

- **线上 CI runner 与 bwrap（2026-09-25）**：Actions 重新启用后 Test 工作流自 7 月以来一直失败，根因是 ubuntu-24.04 runner 预装 bwrap 但 AppArmor 禁止非特权用户命名空间，sandbox 自检 `BWRAP_ISOLATION_FAILED`（setting up uid map: permission denied）→ 全部 `run_command` 用例按设计 fail-closed。两个工作流增加
  “Prepare bubblewrap sandbox on the runner”步骤：`sysctl kernel.apparmor_restrict_unprivileged_userns=0` 并复核自检；产品代码与测试都不绕过沙箱。线上 CI 仍不作为验收来源。
  放开后首轮 3.12 暴露 7 项失败并分类：goal 续跑 prompt 英文断言过时（改结构标记）、随包 bwrap 二进制名 `bwrap.linux-x86_64`（断言前缀）、
  原生 IR 窗口测试前提被余量外置抵消（该测试显式关掉 `tool_output_externalize_on_low_headroom`）、merged-/usr 下 `/bin`→`/usr/bin` 绕过根级目录排除（产品修，`_uninheritable_root_forms`），
  以及两项只在 runner 上出现、本机通过的用例（`test_decision_fault_matrix[dns]`、`test_host_command_stream` 断连取消）待各线复查。
  后续：dns 项由决策线修正（其 Mac 的 HTTP_PROXY 掩盖了解析路径，runner 才是对的）；断连取消项根因是 `execute_host_command` 在 attempt_executor 退出事实之后才写
  未启动回执，并发 `query_host_command` 在窗口里投影成 outcome_unknown。回执改到执行器登记仍为 running 时写入，回归
  `test_host_command_execution.py::test_unstarted_receipt_is_written_before_executor_exit_fact`（去掉修复即失败）。
  第二轮 CI 发现回执不能早于连接清理：`test_plugin_invocation.py::test_connection_cleanup_finishes_before_host_attempt_closes[denied]` 要求资源释放期间 attempt 仍 running；
  现按“ExitStack 资源清理 → 未启动回执 → attempt_executor 退出事实”的顺序写入，两条合同同时成立。
  Full Tests 另暴露 `test_shell_orphan_kill.py::test_foreground_timeout_cleans_group_after_leader_exits[False]`：宽限期内已证明消失的后代 PID 在最终核对前被复用，
  `os.kill` 探测重新成功而出生标识读不到，被记回 unresolved。终止回执改为记住已证明消失的进程实例（同号 PID 换出生标识才重新纳入），
  回归 `test_termination_receipt_keeps_proven_dead_pid_resolved_after_pid_reuse`。
  沙箱真跑之后 fast suite 单 job 实测 40–45 分钟，45 分钟预算在 8cd7d01d0 的运行里被顶满整体取消；test.yml 预算放到 60 分钟。
  结果（2026-09-25 13:3xZ，main `88631648e`，run 36137357520）：test (3.11) 41 分钟 success、test (3.12) 44 分钟 success——7 月以来首次全绿的 fast suite，
  且 run_command 用例真在 bwrap 里执行；test (3.10) 在 45 分钟预算处被取消（非用例失败，60 分钟预算已在 6db8ef403）。随后 Actions 被账单/额度挡住，
  新 run 3 秒内以 billing 注解失败；线上 CI 复验待用户处理 Billing 后进行，本地严格 gate 仍是唯一验收来源。

```bash
python3 -m pytest <直接相关测试文件> -q --tb=short
ruff check agent_py_agent scripts
python3 scripts/check_doc_sync.py
python3 scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json
git diff --check
python3 scripts/check_clean_package.py .
```

默认 focused tests。生产代码与测试代码累计增删约 10,000 行或明确另有要求时追加全仓 pytest；文档清理不算实施代码变动。线上 CI 未运行时如实说明，不替代本地严格 gate。

## 发布资料清理验证

注释与示例清理要比较生产 Python AST、默认配置值、协议与依赖标识。允许的人类展示字符串变化需单列；构建包检查 LICENSE/NOTICE、vendor 许可和不含秘密数据。历史重写须先备份、只改授权引用、带 lease 更新，验证发布树不变。

<!-- 媒体来源片 3adb61904 的既有记录；不代表当前 Compact 集成已验。 -->
## 真实开发长任务验收方法

用户明确要求长对话验收使用真实项目开发过程，禁止把重复生成的大行数当作真实任务通过依据。
当前选择让官网 MiniMax-M2.7 的 my-agent 在原生 TUI 中把 GitHub `sharkdp/fd` 从 Rust 复刻为 Python，
自行读源码、实现、运行测试、修复并提交项目产物。测试者只提交一次普通中文需求并观察，不能代写或补交产物。
记录自然产生的模型/工具回合、Compact、TUI 状态、CPU/RSS、退出/恢复与任务结果；未实际发生的长历史边界不计为通过。
下文合成一万/千万行记录只作为存储边界和缺陷复现，不代表此类真实开发工作负载。当前真实开发验收待完成。

## 官网真模型与TUI媒体验收

2026-09-23，独立候选线，专用测试机限制为 1 CPU / 2 GiB；官网直连，不通过中转，不使用假模型作为本轮验收。

- 官网 MiniMax-M2.7：100 独立用户身份各发送一次中文普通请求，100/100 terminal=done；处理槽峰值 50。
  同时一个原生 TUI 发问并正确回答 `5+6=11`。204.3 秒完成队列，44 个实拍终端帧未见“未同步/刷新失败”。
  cgroup 峰值 1047.1 MiB（含文件缓存），末次采样 Gateway RSS 313.8 MiB、TUI RSS 62.6 MiB。
  真实 `/status` 全部成功，但高峰 P95 3268 ms、最大 4897 ms；单核批量冷启动仍有排队和刷新延迟。
- 官网 MiniMax-M3：实际端点 `https://api.minimax.cn/anthropic/v1/messages`，现有私有 key 短请求确认返回 M3。
  原生 TUI `/attach` 添加 PNG，正确识别红圆、蓝方、绿三角及 `Q7N4`；终端 bracketed paste 拖入 MP4，
  正确识别红→蓝→绿和 1→2→3。测试提问未提供答案；未代模型执行视觉工具。
- 私有只读请求观察器确认真正外发 image/png 6484 字节与 video/mp4 5774 字节，SHA256 与素材一致；
  观察器调用原 HTTP 函数，不替换供应商、不改变请求/响应。模型工具轮为零。
- 真正 `/exit` 后重新启动同一会话，问图片和视频背景，M3 正确回答白色；请求再次带相同原件字节。
- macOS 隔离 Gateway + 原生 TUI，系统图片剪贴板经 Ctrl+V 成为附件，官网 M3 正确识别同图；原剪贴板完整恢复，
  本机隔离测试 TUI/Gateway 已退出。无改动用户日常模型/默认 Gateway。
- `input_media_max_bytes=16 MiB` 同时限制新输入和一次供应商请求的媒体展开；新近附件完整、超预算旧附件明确
  投影为归档引用，canonical refs 和原件不删除。owner 越界、符号链接、同长度内容变更、总量/数量超限均有合同验证。
- 相关组件矩阵当前为 1008 passed、1 skipped；跳过项仍为原 HTTP stop fixture 的 409，自行 skip 不计入通过。
  单测只验证协议/资源/输入边界，真实可用结论来自上述官网模型与原生 TUI。
- 无 checkpoint 的一万行历史：真实 TUI 续聊完成，自动压缩 generation=1 后正确回答 `4+4=8`。
  终态用时 410.81 秒；账本记录官网 M2.7 的 4 次供应商调用均 finished、0 retry，输入 294653 / 输出 1621 token。
  该用时不能算低延迟通过，也不能仅凭单次采样栈归因给 Compact 二分预算估算。

**未通过边界**：千万行浏览成功不等于千万行任意状态续聊成功。对 10,000,000 行、约 2.43 GB、
无 Compact byte checkpoint 的历史，隔离只读子进程在 384 MiB 地址空间上限下调用 `after_compact_report`
立即产生 `MemoryError`，还没有发起模型请求。`append_once` 的全量去重读取也需后续治理。
相关有界读取、分批 Compact 必须与另一开发线正在修改的 scope/checkpoint/CAS 合同合并验收。
本轮不声称无限时长、任意历史规模、100 个重工具或真实 IM 平台账号已通过。

证据保存在仓库外 `tui-real-media-20260923/`：`real-model/` 的 submissions/terminals/samples，
`media-*-tui.txt`、`media-provider-requests.jsonl`、`real-10k-history-*`、`uncompacted-10m-read.json`、本机截图粘贴验收。
旧假模型记录仍保留用于定位，不作为本轮通过依据。未推送、未替换用户默认环境。
