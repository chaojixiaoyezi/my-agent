# Subagent Controlled Tools Overnight Plan

## Goal

把子代理能力申请、父级授权、受控 shell 网关、输出预算、任务 trash、回退报告和 E2E 观察链路串起来。实现必须保持 bundle 接口、不引入新依赖、不把危险命令直接交给子代理。

## Stages

1. Capability request/grant/gap fields
   - 扩展能力申请、授权、缺口字段，支持 tool/skill/MCP/shell/path/network/output budget 等信息。
   - 测试：生命周期写入、结构化 runner 输出解析、持久化兼容。

2. Parent routing and scoped grants
   - 父级路由时把申请里的范围信息带进 grant/gap，避免“泛泛授权”。
   - 测试：命中时 grant 带 scope，未命中时 gap 带可追踪原因和申请范围。

3. Shell gateway dry-run
   - 建立只判断不执行的 shell 网关，能回答“这条命令能不能跑、为什么、需要什么授权”。
   - 测试：允许命令、危险命令、越界 cwd、输出预算。

4. Shell gateway execute v1
   - 在授权范围内执行低风险命令，流式读取输出并截断，写审计和外置输出摘要。
   - 测试：成功执行、超时、输出截断、危险命令拒绝。

5. Task trash manager
   - 给任务目录提供 trash，替代子代理直接 rm；支持重建 trash 和 manifest。
   - 测试：移动文件、越界拒绝、trash 被删后重建。

6. E2E, fallback report, docs, push
   - 串 capability request -> scoped grant -> shell gateway -> trash -> fallback report。
   - 跑 focused tests、ruff、doc sync、strict guard、diff check；提交并推送远端。

7. Complex E2E matrix
   - 开始真实层级测试：主 -> 子 -> 孙 -> 孙孙，观察模板选择、日志、失败接管、权限申请和输出预算。

## Guardrails

- 子代理不能直接拿危险 shell；`rm` 走 task trash。
- 输出必须有预算，不能把大日志直接灌进上下文。
- 所有业务入口继续 bundle 化，例外必须进 allowlist。
- 每个阶段完成后本地提交；第 6 阶段完成后推远端，再开始第 7 阶段。
