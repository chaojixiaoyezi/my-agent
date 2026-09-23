# 第 7 步恢复扫描交接

## 基本信息

- workstream：原收口恢复扫描的显式依赖。
- branch：`codex/closeout-recovery-step7`。
- worktree：沿用上一片的干净隔离工作树，从主线 `9bbfc0a01` 建新分支；原发布修复分支保留。
- owner：恢复扫描实施线；主线并行推进父终态通知内部依赖收窄。
- date：2026-09-23。

## 本线目标

去掉 `runtime_closeout.py` 恢复函数对完整 manager 的依赖，只提供其实际需要的运行账、task 读写、
task 列表和通知能力。保留原 WAL、事件分页、exact attempt、通知和清账顺序，不新增执行器或扫描器。

## 实际完成

- `restore_unpersisted_closeouts`／`_restore_closeout_event` 只接 repo/load/save；
  `advance_pending_closeout` 只接 repo/save/notify；`recover_pending_closeouts` 再接 list。
- `CloseoutNotifier` 是窄 Protocol，仅约定当前 task/result/payload 和 keyword-only attempt_id。
  恢复模块不再导入父通知实现，生产装配在原 capability sweep。
- 保持先还原 WAL 再消费事件；逐条推进保持 settle→notify→delivered→clear，失败不重跑业务。
  None 数据库模式仍可推进已有文件 WAL，已 delivered 只清账，坏结果不阻挡后项。
- 原监督函数在加入装配后达到 101 实现行，触发 100 行严格守卫；将既有回收计数移到真实摘要合并函数，
  只接记录和输出摘要，逐项诊断及原异常边界保持；没有压行或新增只转发 facade。
- 逐一迁移直接测试调用；测试 helper 只装配原 fixture 能力，不提供产品兼容入口。

## 改动文件

- `agent_py_agent/agent/subagents/services/runtime_closeout.py`：恢复入口、通知 Protocol 与边界注释。
- `agent_py_agent/agent/agent_core/orchestration/dispatch/capability_auto_sweep.py`：唯一生产装配、原回收摘要合并。
- `agent_py_agent/tests/test_closeout_recovery_dependencies.py`：无 manager／None 模式／持久恢复合同。
- `agent_py_agent/tests/test_closeout_recovery_paging.py`、`test_dispatch_liveness_and_revive.py`：原故障和分页调用迁移、监督装配回归。
- 设计、模块进度／结构、LLM_GUIDE、DESIGN_LEDGER、CODEBASE_TREE、TESTS、ROADMAP、COMPLETED 和本交接同步。

## 测试命令和结果

```bash
python3 -m pytest -o addopts='' \
  agent_py_agent/tests/test_closeout_recovery_dependencies.py \
  agent_py_agent/tests/test_closeout_recovery_paging.py \
  agent_py_agent/tests/test_dispatch_liveness_and_revive.py \
  agent_py_agent/tests/test_runner_commit_dependencies.py \
  agent_py_agent/tests/test_capability_auto_grant.py \
  agent_py_agent/tests/test_gateway_orphan_reconciler.py \
  agent_py_agent/tests/test_runner_timeout_policy.py \
  agent_py_agent/tests/test_subagent_coordination.py \
  agent_py_agent/tests/test_gateway_agent_control_service.py \
  agent_py_agent/tests/test_gateway_conversation_control.py -q --tb=short
ruff check agent_py_agent scripts
python3 scripts/check_import_boundaries.py
python3 scripts/check_doc_sync.py
python3 scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json
git diff --check HEAD
python3 scripts/check_clean_package.py .
```

最终 10 文件联合：**291 passed、1 项既有 skipped**（36.67 秒）。上述本地严格 gate 全部通过，
strict code-size 为 `blocked=False`，新增行隐私模式扫描无命中。未运行全仓 pytest，线上 CI 未作为验收来源。
只使用临时 fixture 文件及已有离线测试，没有读取私密配置或调用真实模型／Gateway／TUI。

## 影响范围

没有改变 canonical 目录、持久 schema、权限、分页游标、消费标记、调度周期或通知去重合同。
恢复只读取既有结果文件、补运行账与通知；不授予业务执行能力，也没有新队列或后台服务。

## 需要主线重点复查

- sweep 必须绑定真实 load/save/list 方法及当前父通知能力，不能给恢复重新传 manager 或通用大 context。
- 回调签名为 `notify_result(task, result, output_payload, *, attempt_id)`，本次身份不提前固化成某条 task。
- 确认 None 数据库、事件还原后只消费、旧 attempt 拒绝、标记失败和清账失败仍沿同一回归通过。

## 需要其他线协调

并行父通知片新增 `RunnerCompletionNotifier`，本片没有改 `runner_completion_wake.py`、
`runner_result_service.py`、`agent_control.py` 或 `store_wake_publication.py`。
集成后主线将 sweep、恢复测试 helper，以及清账失败用例中直接 `advance_pending_closeout` 的 legacy partial
替换为新 notifier 的 `.notify_result`；
`test_dispatch_liveness_and_revive.py` 原两处通知故障 monkeypatch 也由主线跟随新实现迁移。

## 剩余风险

- 尚未集成并行父通知新实现、构建 wheel 或执行同版原生 TUI；基线真实测试不覆盖本片。
- 文件损坏、硬断电、跨进程强杀、Windows 文件锁不在本片新增验收范围；发布半写语义仍按上一片边界。
- 没有扩展通用发布自动重试，无 key 幂等和无调用方重试的 prepared 发布仍不在本片承诺内。
- 未 push、部署或操作真实任务；本地通过不代表整步验收完成。

## 建议下一步

主线先集成本片与父通知小片，在唯一装配点和测试 helper 接上新 notifier，再跑组合 focused 与严格 gate。
其他 agent 可并行只读复核依赖与部署计划；同版发包前不要让两条实现线同时改写恢复或通知生产文件。
