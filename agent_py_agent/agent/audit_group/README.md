# 审计组：只读观察与受限黑盒调用（AG1）

状态：领域服务、原生工具和定向合同回归已落地；**当前宿主原生执行未通过，不能按整体上线验收通过处理**。本模块不接管 Plugin Host v2 运输、Web 双机器人或会话生命周期。

## 入口与归属

- `access_policy.py`：`AuditGroupError`、`require_audit_group_member`、准确 owner 解析、实时停用/工具策略检查。
- `observation_service.py`：`AuditGroupObservationService(root_dir)`，提供 `list_owners`、`summarize_assets`、`list_alerts`、`posture_overview`。
- `blackbox_invoke_service.py`：`AuditGroupBlackboxInvokeService(root_dir).invoke_asset(caller_owner, *, source_owner, asset_type, asset_id, expected_revision=None, inputs=None, rule_id="", timeout_s=30.0)`。
- `readonly_store.py`、`owner_catalog.py`、`observation_projection.py`、`safe_projection.py`：有界 no-follow 读取、白名单投影、覆盖与脱敏。
- `source_snapshot.py`、`snapshot_guard.py`：同源依赖闭包、原锁只读协调、双读一致性与版本指纹。
- `blackbox_execution.py`、`isolated_unit.py`、`caller_controls.py`：受限执行单元、总期限、原治理 authority 与调用者磁盘配额。
- `tooling/audit_group_tools.py`：`audit_group_query` 和 `audit_group_invoke_asset`。工具构造接真实 caller，参数不允许 `caller_owner`、`as_user` 或 `installation_owner`。

工具查询动作是 `owners`、`assets`、`alerts`、`posture`。调用工具有写入结果/启动进程的能力，沿原危险动作审批合同；只读工具不提供任何执行入口。注册时不借插件安装者身份，冻结后的工具执行仍实时核验资格。

## 账号与授权

`LocalUser` 新字段兼容旧记录：`in_audit_group=False`、固定 `allowed_channels=("web",)`、固定 `im_binding_allowed=False`，以及资格更新时间/修改者。公开视图不增加密码散列泄露。

`LocalUserLifecycle.set_audit_group_membership` 只接受 `local/main` 管理受管本地用户。沿操作锁→用户锁、CAS、原子账号保存与同文件回执执行；重放先于 CAS。显式 `idempotency_key` 会绑定意图，不同意图不能复用；省略时每次是新操作。创建账号可用 `in_audit_group=True`；保留旧 `quotas` 兼容别名和 `actor_owner` 宿主上下文，未知创建选项拒绝。

新 provider 绑定、已有 provider 映射解析及飞书认证映射都拒绝受管 `local/<uid>` 的非 Web 使用。管理员和原生飞书个人账号不因此变成受管本地账号。成员资格不能授给原生飞书账号。禁用账号优先于审计组标记。

服务的 `caller_owner` 是**宿主已核验的有效 owner**，不是一个可由插件请求任选的代理身份。运输适配器仍必须绑定真实 actor/effective owner；领域方法的 Python 参数本身不签发身份。

## 只读投影

- owner 清单只来自原本地账号库与原 provider 索引；不按任意目录发现账号。目录沿 `owner_resolver` 规范布局，普通本地用户为 `owners/local/<uid>`。
- `AssetSummary` 只有任务约定的 14 个字段，不返回代码、技能正文、工作流步骤、监控 Prompt、凭据引用、数据源配置或宿主绝对路径。修订只接受非负整数、技能内容摘要或监控双修订。
- schema 仅保留有界 JSON Schema 关键字；动态键、默认值、示例和描述都经过安全投影，不开放 `$ref` 外部读取。
- 监控状态合并原 `data/scheduler/store.json` 与绑定；修订是 `binding_revision:job_version`。暂停/删除的 job 不能被旧绑定复活，简介不回落 `job.prompt`。
- 告警当前只投影原 Scheduler 的 `failed` / `unknown` 运行终态，不公开 `response`、`structured_result`、`error_message` 或 job 快照。**这不是完整的业务安全告警仓库**。`since` 必须是带时区 ISO 时间，按实际时刻比较。
- 态势是已登记 owner、资产及监控运行异常的聚合计数，**未接入园区/物理设备数据源**。
- 缺失存储可为空；损坏、权限/链接错误、未完成资产事务或预算超限必须保留 `partial` 与 `source_errors`，不能伪装为完整空清单。

分页游标只含筛选指纹与偏移，不是跨多次请求的不可变数据快照；数据在分页期间变更可能影响条目位置。

## 支持的执行子集

| 类型 | 当前允许的合同 | 明确拒绝的情况 |
| --- | --- | --- |
| script | Python 3，明确结构化输出字段，输入 JSON，强制平台沙箱 | 非 Python 解释器、携带数据源/凭据句柄、非结构化输出或原生沙箱未就绪 |
| skill | `skill_meta.json` 中有结构化 `io_contract.outputs`，且 `metadata.blackbox` 恰为 `{"executor":"script","script_id":"同源脚本ID"}` | 普通规程型/无黑盒合同技能、任意来源或模型上下文注入；返回 `AUDIT_GROUP_SKILL_BLACKBOX_UNSUPPORTED` |
| workflow | 同源冻结闭包内的脚本和上述结构化技能；原结构化条件及输入映射 | 缺依赖、来源前缀越界、无可执行单元的步骤或不支持的模式；不运行通用 LLM/提示词步骤 |
| monitor | 显式选择一条启用规则，确定性的 `scripts` / `workflows` `run` 动作，按冻结条件单次复核 | LLM、任意工具/通知动作、规则歧义或未满足的依赖条件；不注册计划 |

不将技能 `SKILL.md` 正文放进调用者主头像 Prompt。工作流结果只保存安全回执，不构造含 `workflow_snapshot` 的原生 `WorkflowRun`。监控不写原调度 run、checkpoint、运行次数、下次时间或通知状态。`datasource` 不是可调用类型。

这不是对任意现有脚本/技能/工作流的无条件兼容：不能证明安全的合同明确拒绝，不能降级到宿主直接执行。

## 隔离、配额与发表

1. 先查真实成员与调用者治理 authority：`data/managed_user_governance/<uid>/governance_policy.json`。用户可编辑的治理镜像不授权。原自建资产开关不因使用他人资产被解释成创建许可；实际子单元仍核对应工具禁用策略。
2. 冻结原件及同源传递依赖，检查 enabled 与期望修订。原账号、维护状态或管理员控制停用都拒绝执行。依赖绝不回落到 caller、shared 或全局同名资产。
3. 只打开已有原资产/Scheduler 锁，不创建源锁。锁出生/消失/更换 inode 均冲突；锁内双读全部依赖，拒绝已观察到的撕裂版本。该协调依赖正常写者遵守原锁/发布协议，**不保证对忽略协议的恶意直接文件写入作完整 ABA 检测**。
4. 由调用者原 `OwnerQuotaEnforcer` 为匿名缓冲预留占位；源主人不计费。代码只经匿名临时描述符送入子进程，不进入命名源码文件、argv、环境或公开回执。bootstrap 读完后关闭 fd 0，不再保留输入配置。
5. 子进程 `cwd` 是调用者 workspace，但**无任何文件写根**，禁网、只读根和读取 allowlist 由原平台沙箱强制。stdout/stderr 用有界管道采集；不公开原 stderr。不能用直接写 workspace 或 staging 绕过输出净化、配额与复核。
6. 每个执行单元前和执行后核成员、治理及源快照。这里只承诺检查点撤权和不发表拒绝的结果，**不提供在途进程的实时撤权取消**。
7. 只保留合同输出字段；统一脱敏、抹除可精确识别的源私密原文并清理绝对路径，再次按调用者实时磁盘配额原子保存 `data/audit_group_exec/<invocation_id>/result.json`。当前不发布任意文件/二进制 Artifact，`generated_artifacts` 为空；占位在 finally 删除。

本实现不是信息流/恶意编码检测器。源资产代码必须作为来源主人显式提供的受信执行定义；字符串净化不能证明能阻止恶意源脚本将自身正文任意编码后送出。进程读取/写入隔离、并发预留与发表、撤权交错都仍需在支持的原生宿主复验，不能用 mock 编排通过代替内核证明。

## 当前验证与集成门

新增测试：`tests/test_audit_group_observation_and_invoke.py`。测试分清四类编排（仅替换隔离单元）、实际文件/配额/治理/源状态检查、沙箱拒绝安全关闭，以及不替换执行单元的真实原生测试。

当前真实测试在原沙箱 `require_ready` 返回 `SANDBOX_NETWORK_ISOLATION_UNAVAILABLE`；服务将其转换为 `503 AUDIT_GROUP_SANDBOX_UNAVAILABLE`。**真实原生脚本执行与完整跨 owner 黑盒端到端尚未验证成功**，没有增加 skip/xfail 或 unsafe fallback。

Plugin Host v2 和 Web/IM 真实入口、双机器人裁剪、UI、跨机器、Linux 原生内核与生产部署不在本次验证结论内。集成前须修复/核验原沙箱准备条件，并由集成者协调更新与新 Web-only 合同冲突的旧认证测试；本任务没有修改未授权旧测试来制造全绿。
