# S06/S07/S08 云错误与后台恢复交接

日期：2026-10-14。独立工作树 credential-storage-s21 延续开发；已交凭据改动不在本切片补丁中。根已发布第一批 `c0cabbb21`；本线不提交、不推送、不部署或调用真实飞书。

## 已完成

- S06：HTTP/CLI 共用准确数值分类和阶段，90202/90215/90213 分开；明确拒绝不再 UNKNOWN，陌生动作/供应商码仍失败关闭。
- S07：原操作账正式支持同 root 当前活跃 attempt 的只读核对接管，不迁移原执行身份；CAS 新核对代数和 holder/epoch，拒绝旧 attempt、取消、错 owner/root/tool/args。managed 原账补齐同 run 等价 UNKNOWN 屏障，不许换调用编号盲追加；未来 run 独立。
- 云更新只做一次授权准确目标读回，证明目标状态已满足后原操作结算；追加没有可靠目的位置继续 UNKNOWN，不猜已写/未写。原资料与分析不重做。
- S08：沿 owner 原 raw event 写 no-save 最小工具审计，不写参数、单元格、秘密或模型输出。记录实际 actor、thread/task/run/job/rule、声明动作、原操作、阶段及 HTTP/API 数值。

完整合同见 [FEISHU_CLOUD_RECOVERY_AUDIT](../design/FEISHU_CLOUD_RECOVERY_AUDIT.md)。根需同步总台账、CODEBASE_TREE 与 TESTS 导航。

## 补丁与文件范围

- `/tmp/cloud-s06-s08-first.patch` 是已先交的 S06/S08 七文件分片。
- 最终交付另给 S06/S08 相对 first 的小增量及 S07 相对 `/tmp/cloud-s06-s08-snapshot` 的增量，避免整树凭据 diff。
- S07：`local_storage/tool_operations.py`、`runtime_db/managed_operation_store.py`、新增 `operation_reconciliation_authority.py`、`tooling/tool_operation_coordinator.py`、`feishu_cloud_tool.py`、新增 `feishu_cloud_recovery.py`、两份新回归和本设计/交接。
- S06/S08：`adapter/feishu_cloud.py`、`feishu_cli.py`、`contracts/error_taxonomy.py`、`agent_core/runtime/live_archive.py`、新增 `minimal_tool_audit.py` 及两份新测试。

## 根整合重点

- 新 `ToolOperationReconciliationClaimRequest` 字段只为显式当前恢复身份；旧同 attempt 请求可保留，但不准跨 attempt 猜调用者。
- 新 `ToolOperationRecord.reconciliation_only` 是原账只读限制的投影。只读接管不允许 reopen，原 holder/generation 不能结算新核对 claim。
- 同 run 等价 UNKNOWN 只阻断相同 tool/args/namespace；独立未来 run 不替代、清空或重放旧操作。
- 目标值一致只能证明更新要求的目标状态满足，不证明唯一写入者。没有证明原追加目的位置时仍需原操作核对，绝不自动重发 append。
- 所有新网络读取受既有核验开关及原 owner/网络/身份/资源门约束；没有新增秘密保存路径或配置开关。
- 不把 no-save 最小审计扩成全文工具归档；原完整归档仍按 save 和既有脱敏政策。

## 验证

新云错误矩阵、三 owner 审计、真实 Registry 更新/追加恢复、跨 attempt 权威矩阵，以及原操作幂等/managed、定时 waiting、no-save 原回归均已验证。十文件组合全部测试断言通过，但并行尺寸检查改写 CODE_SIZE_REPORT.md 触发一项 teardown 守卫；恢复报告并单独重跑该项通过，最新最小审计六项亦通过。局部 Ruff、diff 与 strict size hard=0 通过。doc sync 在本线旧基线树提示已复制凭据 gateway 代码缺配套根文档，属于第一批整合文档，不在此增量中修；根应在当前main整合树重跑。未以线上 CI 或真实租户作为本线验收来源。

## 建议下一步

根先审查跨 attempt 只读接管的 holder/generation/current epoch 和原身份保留，再串行应用两份增量并跑 focused/strict gate。供应商错误与 no-save 审计可由其它 agent 并行只读审阅；原 operation 状态、资源锁和恢复结算必须由同一负责人收口。真实租户验收需要授权且保留未知追加禁止重发边界。
