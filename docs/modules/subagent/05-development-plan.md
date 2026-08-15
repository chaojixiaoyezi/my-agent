# Subagent Development Plan

## 方向

- 保持一个 `SubAgentManager` root manager。
- 新能力优先进入现有 service 子包。
- 子代理专注短周期任务，不拥有长期个人记忆。
- 父代理通过 tree、rollup、refs、cancel/takeover 管理下级。
- 等待子代理进度使用 `wait` policy，不忙轮询。

## 优先级

1. 真实主代理和子代理链路稳定完工。
2. 子代理状态、失败原因、未完成原因可追踪。
3. compact 后能接着做，不重复读旧报告。
4. owner home 路径保持清晰，不回到 repo data。
5. 删除旧入口、旧文档、旧测试，不新增同义工具。
