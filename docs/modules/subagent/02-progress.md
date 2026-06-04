# Subagent Progress

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
- 子代理模板可以定义简短 persona、description、skills，但当前 runner 仍走现有执行链路。
- capability request/grant/gap 是可观察工作项，不是默认阻断任务的硬门。
- workflow mode 是显式配置能力，不应该替普通中文任务自动加限制。
