# Subagent Progress

## 2026-06-06 主链路小跳转清理

- 删除只服务单一调用点的 facade/helper 文件，把能力请求解析、action rescue 渲染、runner
  guidance 注入、runner tool 过滤和 root task policy 折回当前权威模块。
- `runner_context` 现在直接构造执行上下文、写入边界、runtime guidance 和 runner allowed
  tools；角色模板相关判断留在 `role_templates`。
- 这轮清理不新增工具、不新增硬门，只减少跨文件跳转和旧入口。

## 2026-06-06 状态精确化

- 子代理运行、恢复、tree、closeout 统一按当前协议状态判断；`COMPLETED`、`SUCCESS`、`ERROR`
  等旧标签不再隐式兼容成 `DONE` 或 `FAILED`。
- 恢复状态机不再把 `PLANNED`、`QUEUED`、`WAIT_CHILD` 旧别名提升成当前协议状态；旧状态进入
  `manual_review`，避免跨版本残留污染当前 run。
- 缺少结构化 `failure_type` 时，恢复快照不再从 `runner_last_error` 或自由文本错误里猜恢复码；
  工具错误文本分类只保留在工具结果诊断层，不能替代任务状态事实。
- 恢复 mode 只认当前显式枚举，例如 `rerun_from_continue_packet`、`rerun_from_checkpoint`、
  `takeover_from_continue_packet`、`takeover_from_checkpoint`；不再用 `rerun_*` / `takeover_*`
  前缀把未知旧值提升成自动重跑或接管。
- capability 等待状态只认当前协议 `PENDING_CAPABILITY_REQUEST`，不再把 `NEEDS_TOOL`、
  `WAITING_FOR_TOOL` 等旧/模糊状态别名自动升级成能力申请。
- 工具结果没有显式 `error_code` / `error_type` 时，机器错误码统一是 `UNKNOWN_ERROR`；
  日志里的错误正文可以给模型看，但不能反推出结构化错误码、任务状态或验收结论。
- `DONE` 仍是唯一已完成状态；`FAILED`、`TIMEOUT`、`CHANNEL_ERROR`、`BLOCKED`
  是可恢复/阻塞状态，恢复器和 strategy 只扫描这些结构化状态。
- `output.json`、checkpoint 和 QA payload 只读结构化字段、`ok` 布尔、blockers 和 refs；
  summary、角色描述、旧状态词只作为展示或软上下文。

## 2026-06-04 收敛

- 删除旧 manager mixin 和过渡转发文件，`SubAgentManager` 现在直接拥有初始化、基础生命周期和工单路径。
- `SubAgentBoardService`、`SubAgentPatchService`、`SubAgentHierarchyService` 直接作为当前服务入口，不再保留单独转发文件。
- 子代理状态继续以 task-local canonical state 为权威；owner projection 和 global index 只做查找。
- `cancel_subagents` 是父代理处理卡住下级的控制面：可取消、废弃 attempt、记录审计，再由父代理接管或汇总。
- `inspect_agent_tree` 重复查看只给紧凑提示和直接摘要；需要等待时用 `wait` 登记下次查看间隔，不把轮询做成硬门。
- `create_subagents` 不再因为已经有活跃子代理就默认拒绝第二批；父代理可以先派一批，后面按需要继续派。
- 子代理可以写 task workspace 里的协作产物；最终交付由主代理汇总到 `output/` 或用户指定目录。

## 运行约定

- 子代理没有长期个人记忆，只保留 task-local 状态、事件、artifact refs、compact 和候选经验。
- 子代理模板可以定义简短 persona、description、skills 和机器能力字段；当前 runner 仍走现有执行链路。
- `role` 是模板选择字段，`agent_name` 只是展示名。创建任务时会把模板能力快照写入
  `attributes.role_template`，后续调度、恢复、timeout 和 runner prompt 只读这个快照或模板字段，
  不从显示名或普通中文/英文描述里猜角色。
- capability request/grant/gap 是可观察工作项，不是默认阻断任务的硬门。
- workflow mode 是显式配置能力，不应该替普通中文任务自动加限制。
