# 真实链路验证辅助：当前状态

Live Lab 是开发验收辅助，不是生产调度器，也不能替被测代理补写业务产物。
历史逐次测试现场已移出发布资料；当前验收规则见 [TESTS](../../../TESTS.md)。

## 保留的验证职责

- 生成隔离的测试输入，保存结构化请求、工具结果、状态、输出与总结。
- 显式真实模型模式先验证一次实际响应，不以存在 API Key 代替模型可用性。
- 工具恢复验证同时检查有效产物与受保护的缺失输入，不能为通过测试制造缺失输入。
- 长文本压缩验证检查来源覆盖、压缩账本与后续恢复，不以模型声称“读完”作为通过证据。
- fixture 标记只用于测试层，不进入生产的状态、权限、任务归属或完成判定。

## 参数减量第 2 批（2026-09-27）

Live Lab 生成的测试配置不再写已删除的键：`session.py` 去掉 `runner_failure_policy`（重跑次数改用默认的 `runner_failure_retry_limit`），`main_agent_compact_stress.py` 去掉留空的 `max_tool_calls_per_round`（并入 `max_parallel_tool_calls`，留空仍是 8）。只影响测试配置的生成，不改验收判据。

## 发布清理

已移除旧批次稳定性脚本、机器现场配置和过时实验报告。常规测试以现有定向用例、脱敏回放及
真实 TUI 验收为主；删除旧脚本不等于相关能力已经通过当前版本验收。
`bad_weather` 已移除停用结果块协议的 `structured-repair` 调用，保留 Gateway 恢复与 runner 重试。
重试场景的已有收口断言失败仍保留，不能因删去失效场景就把整个 suite 记为通过。

参数减量第 1 批（分支 `claude/38-delete-dead-config`，2026-09-27）：`session.py` 生成的测试 YAML 不再写已删除的 `gateway_request_workers`（网关限流只剩
`gateway_user_inflight_limit` / `gateway_global_inflight_limit` 两层）。

真实 TUI 需公布 tmux 名称，多个用户共用一个 Gateway，并区分短启动、连续任务、
长上下文、多子代理、慢模型与故障恢复。开放问题以 [STATUS](../../../STATUS.md) 为准。
