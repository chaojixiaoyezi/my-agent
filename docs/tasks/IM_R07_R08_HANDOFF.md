# Workstream Handoff：R07/R08 监控控制

## 基本信息

- workstream：IM R07/R08，同线按文件独占
- branch：`codex/im-entry-update-assessment`
- worktree：任务已附属的共享修复工作树
- owner：监控控制子代理
- date：2026-10-14

## 本线目标

让创建者通过确定性 `/schedule` 删除自己的监控并停止准确执行；让绑定管理员在私人 IM 显式接管本机监控未来通知，同时查看、取消或重新核对原执行。保持原 owner/actor、原 job/run/claim、历史和产物，不迁移活进程或建立替代监控。

## 实际完成

- 命令和 `schedule` 工具共用 `scheduler/control.py`。新增 delete、execution、cancel、recover、takeover；delete 关闭未来并取消当前，cancel 保留未来，recover 只复核死亡证明及 waiting 收口。
- 删除按原创建人/owner 和 job 版本 CAS，保留 tombstone，重复删除可重试取消而不重新写删除事实。群成员、群内管理员、引用或昵称不能借创建人身份；未知 creator 拒绝写控制。
- 原 job/run 先持久写取消意图；queued/claimed 保存原 cancelled 历史及精确 pending IDs，running/waiting 留原 claim。TaskLink/RuntimeDB、必要输入、原资源整树的准备和清理均复用现有控制接口。
- 回执区分 cancel_requested 和 stopped。原主 claim、子执行轮中断、PTY 异步回收或资源未知时不能宣称退出。waiting 资源与原 claim 均确认后沿原收口；running 沿原执行者或死亡证明收口。
- 接管保存 `scheduler_notification_binding.v1` 到原 job、冻结到后续预留 run；包含准确来源线程、原三字段 binding、管理员 bound_at 代次、版本和 request_id。保留原 creator/source/execution，已预留 run 不改目标；群通知继续原 conversation_id。
- 每次外发核对原私人线程和管理员登录代次，注销/重绑失效，无任意 recipient 参数。
- Gateway 重启消费原 pending IDs，包括原 running 被启动恢复先归档成 unknown 的情况；继续整树取消并保留 unknown 业务历史，不重做任务。

## 改动文件

- 新增 `agent_py_agent/agent/scheduler/control.py`、`notification_binding.py`。
- 修改 `scheduler/tool.py`、`repository.py`、`execution.py`、`service.py`、`recovery.py`。
- 修改 `gateway_parts/schedule_control.py`、`schedule_diagnostics.py`、`command_catalog.py`。
- 新增 `agent_py_agent/tests/test_scheduler_control_lifecycle.py`（20 项）；修改模块文档 `docs/modules/gateway/02-progress.md`、`04-structure.md`。
- 新增 `docs/design/SCHEDULER_HUMAN_CONTROL.md`、本交接。
- 根负责的 `core.py` 已注入 `cancellation_handler`，为原 SchedulerService 提供准确取消回调；本线未改 core。

## 测试命令和结果

解释器与 Ruff 均使用项目 `.venv`。本线本地验收，不提交、不推送、不部署、不操作真实监控。

```bash
python -m pytest agent_py_agent/tests/test_scheduler_control_lifecycle.py -q --tb=short
python -m pytest agent_py_agent/tests/test_scheduler_control_lifecycle.py agent_py_agent/tests/test_schedule_control.py agent_py_agent/tests/test_scheduler_host_actor.py agent_py_agent/tests/test_scheduler_repository.py agent_py_agent/tests/test_scheduler_tool.py agent_py_agent/tests/test_scheduler_unknown_recovery.py agent_py_agent/tests/test_scheduler_execution.py agent_py_agent/tests/test_scheduler_im_l_integration.py agent_py_agent/tests/test_im_scheduler_security_matrix.py agent_py_agent/tests/test_scheduler_monitors.py agent_py_agent/tests/test_scheduler_service.py agent_py_agent/tests/test_scheduler_waiting_deadlock.py agent_py_agent/tests/test_scheduler_runtime.py agent_py_agent/tests/test_task_input_cancel_deadline.py agent_py_agent/tests/test_subagent_resource_stop.py agent_py_agent/tests/test_scheduler_corruption_report.py -q --tb=short
ruff check agent_py_agent/agent/scheduler agent_py_agent/agent/gateway_parts/schedule_control.py agent_py_agent/agent/gateway_parts/schedule_diagnostics.py agent_py_agent/agent/command_catalog.py agent_py_agent/tests/test_scheduler_control_lifecycle.py
python scripts/check_doc_sync.py
python scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json --report /tmp/im-r07-r08-code-size.md
git diff --check
```

结果：原 18 项新合同测试通过；非作者审计后追加 2 项坏 run 取消回归，当前 20 项新合同与相邻命令/损坏记录组合 26 项通过。16 文件 focused 组合 369 项通过（最终串行复验完成）。Ruff、doc sync、diff 检查通过；严格 code-size 在共同工作树最新结果 hard=0。本线没有跑全仓 pytest，也没有使用线上 CI 或真实 IM 作为本线验收来源。

曾出现一次验证竞态：369 个测试体通过，但并行 code-size 默认报告改动被跟踪的 CODE_SIZE_REPORT.md，触发测试 teardown 防污染守卫。随后只串行跑 focused 组合；后续 code-size 使用 /tmp 报告，不能将该竞态误报为产品用例通过或忽略守卫。

## 影响范围

原 scheduler 仓库、控制命令、工具和冻结投递。新增状态是原 job/run 的版本元数据与取消意图，原历史、TaskLink、RuntimeDB、输入与资源仍为各自唯一事实源。没有新配置、依赖、后台执行器或收件人表；显式控制动作不更改默认工具 ask 策略。

## 需要主线重点复查

- 原工具危险动作策略与确定性 slash 人类控制的分工；显式 ask 策略仍然生效。
- 取消锁序与无热 runner 情况；进程或 claim 身份不明保持 requested，不能写 stopped。
- 原运行归档为 unknown 后准确 pending ID 仍能追回子树，历史业务终态不能被取消补写覆盖。
- 未来通知接管仅 local/main、绑定管理员私人入口；管理员群身份及旧未知 creator 不授予接管。
- 旧 creator 迁移只使用已存在结构化迁移合同，不能用最新群 sender、引用作者或自然语言补人。

## 需要其他线协调

根补 `CODEBASE_TREE.md`、`TESTS.md`、总设计台账和 CLI_REFERENCE 的命令说明，统一严格 gate、原 registry 冒烟、打包与发行。根已完成 core 回调接线；本线未触碰共享配置或根台账。

## 剩余风险

没有真实 IM 收信、实际活进程整树退出、真实 Gateway 重启或默认安装迁移验收。合成出生身份死亡与原控制/资源替身只验证合同，不冒称生产任务已停止。损坏 TaskLink、未知 claim 或旧缺创建人保持拒绝/未确认，需要用户或管理员按原事实修复。

## 建议下一步

根先做非作者权限与取消审查，再串行组合 gate、原 registry 冒烟和隔离发行验收，最后按已授权范围合 main/发行。其他 agent 可并行只读审查 owner/actor、绑定代次与通知冻结；不要并行写同文件、默认报告或操作生产监控，尤其不能借“恢复”重开业务。

非作者审计补修：progress 线在隔离原仓库注入 running run 的 claim_epoch=-1，发现空 parsed list 曾误报 stopped。已在共用 control 聚合保留读取不完整的 cancel_requested，命令明确取消/停止尚未确认；原坏记录不改、不生成猜测取消目标，删除仍关闭未来、当前取消仍保留计划。delete/cancel 两项负向回归通过。建议下一步：非作者重跑原证据探针，再由根统一最终 focused/strict gate；可并行只读审查，不并行改默认报告或真实监控。
