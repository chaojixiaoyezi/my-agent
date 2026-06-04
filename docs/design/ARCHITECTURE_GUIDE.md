# Architecture Guide

当前原则：先让一条主链路稳定、可审计、好排查。不要为历史入口、历史目录或猜测式恢复继续加旁路。

## Main Chain

```text
CLI / Gateway
  -> SimpleAgent
  -> prompt + model round
  -> tool loop
  -> orchestration tools when needed
  -> SubAgentManager and services
  -> task/output + task/work + owner home indexes
  -> closeout / report
```

## Where Code Belongs

- 主代理运行时：`agent/agent_core/`
- 子代理：`agent/subagents/`
- gateway：`agent/gateway_parts/`
- CLI：`cli/`
- owner home / task workspace：`agent/user_space/`
- memory / compact / artifact refs：`agent/memory_store/` 和 `agent/memory_archive/`
- LocalStore：`agent/local_storage/`
- contracts：`agent/contracts/`
- tool registry / execution：`agent/tooling/`
- config：`agent/settings/`

## Design Rules

- 新功能接已有主链路，不新增平行事实源。
- 新状态必须有权威写入位置；projection/index 只能做查找。
- 新参数优先进入现有 config 或明确 Request/Options/Params，不偷偷写死一套默认值。
- 新工具必须和现有工具语义明显不同；能用现有工具表达就不要加工具。
- 普通中文任务不应该被额外硬门拦住；安全边界、危险路径、显式权限仍然保留。
- 文档必须描述当前代码。删除旧入口时，同步删除对应文档和测试。

## Reference Style

参考 长期助手 / 模型助手 Code / 会话运行时 / 通道运行时 时，优先复刻这些底层思路：

- 单一清晰入口比多层转发更容易排查。
- 状态机和持久化边界要明确。
- 子代理应该专注任务，不拥有复杂长期人格或长期记忆。
- 等待/查看子代理进度应该是后台提醒，不是硬门和忙轮询。
- 本地系统不应成为瓶颈；慢只能主要来自模型或外部服务。
