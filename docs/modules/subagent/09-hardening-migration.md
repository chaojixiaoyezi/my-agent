# Subagent Hardening Migration

中文说明：这份文档记录 2026-05-15 开始的子代理硬化迁移。目标不是继续加一堆流程限制，而是把子代理做成“不同记忆、不同权限、但能力接近主代理的执行者”。默认本地模式下，子代理、协调者、测试者、验收者都应该能读、能写、能汇报、能在任务目录内工作；角色只追加职责，不应该把代理变成没手没脚的空模板。

## Reference Lessons

- 会话运行时: 子代理创建和执行尽量走结构化参数、角色应用、agent path 和事件流。不要让下级从自然语言摘要里猜工具名、路径和父子关系。
- 通道运行时: 子代理有 session/run registry、日志、send/kill/info 等控制面；父子权限和可见性靠运行登记和命令边界，不靠大段提示词碰运气。
- 长期助手: delegate 子任务是干净的新对话，有自己的 task id、terminal/cache 和 sandbox；父级只拿 summary/ref，大输出外置，避免父级上下文越来越重。

## Current Migration Principles

- 子代理默认像主代理：除记忆和权限边界不同外，基础读写、报告、任务目录工作能力保持可用。
- 用户可见子代理配置少于 10 个；调度细节、上下文长度、工作流微参数由系统内部策略处理。
- root 不走 `capability_request`，因为 root 没有上级；下级仍可向父级申请工具、skill、MCP、网络或 shell 能力。
- 路径、工具名、文件合同走结构化 `task_packet` / `context_bundle` / typed envelope；提示词只解释职责，不作为唯一事实来源。
- 工作完成后再派 QA/验收；是否提前派测试、找茬、验收由 LLM 根据任务依赖决定，不能写死成所有任务一开始就创建一堆空转角色。
- 安全底线保留：不能越权写别人的目录，不能删除系统目录，不能自毁 my-agent 关键文件，不能无限创建/恢复。

## 24 Steps

1. 精简默认配置，只保留少量用户能理解的子代理入口。
2. 保留旧配置字段兼容，但不再作为新功能扩展入口。
3. 所有内置和用户模板都叠加基础读写/报告工具，避免空模板。
4. root runner 上下文隐藏 `capability_request`，下级仍保留申请通道。
5. 给 context bundle 增加 `subagent_task_packet.v1`，固定 role、goal、文件合同、写入合同和工具合同。
6. 让 runner prompt 明确优先读 `context_bundle.task_packet`，不要从摘要里重新猜路径。
7. 标准 JSON 工具调用也做工具名/参数名别名归一，例如 `write/file_path` -> `write_file/path`。
8. 冲突参数显式报错，不静默猜测，例如 `path` 和 `file_path` 不一致时拒绝。
9. 把 create/schedule/dispatch 的工具输出继续 typed envelope 化，父级只读 run ids 和 refs。
10. 将 parent planner、coordinator planner 和 runner 继续隔离，planner 不读产品正文。
11. 将 QA/tester/acceptor 的创建策略改成“work 完成后按依赖触发”为主。
12. 允许 LLM 决定 QA 覆盖范围：一个 QA 可以检查多个 worker，不强制一一对应。
13. 强化 direct child 进度摘要，父级先看状态和 refs，不直接读正文。
14. 优化 takeover packet，确保原 run 挂死后新 run 接管同一任务目录和 artifacts。
15. 优化 leadership recovery，coordinator 挂掉后新 leader 能接住原孩子。
16. 批量失败时做 bounded batch recovery，不把 3 个失败扩成 30 个新代理。
17. packet 损坏/缺失/过期时降级读 checkpoint、summary 和 context bundle。
18. no-progress fuse 只阻止死循环，不阻止正常重试和人工/父级纠偏。
19. 子代理 compact 只读 task-local refs，不读取主代理 SOUL/USER/memory。
20. 多次 compact 后继续从 progress/continue packet 接任务，避免重复写同一部分。
21. 真实 E2E 使用小白自然语言任务，不在普通任务提示词里塞 dispatch/run_id 等术语。
22. 真实 E2E 必须由主代理派第一层，后续由下级继续派下级；测试者只观察日志和产物。
23. 把发现的问题持续追加到 `06-real-e2e-findings.md`，记录问题、修法、是否解决。
24. 每轮提交前跑 focused tests、ruff、strict code-size；推远端前按“无 CI 时本地最严格测试”执行。

## 15 Test Groups

1. 单 worker 自然语言任务：能读写、能汇报、能验收。
2. 3 worker 并行：不抢同一文件，不误用同一 runner_instruction。
3. 5 worker 并行：父级只看 refs 和状态，不读产品正文。
4. 1 主、2 子、4 孙：层级创建由上级逐层完成。
5. 1 主、4 子、16 孙、48 孙孙：压力测试层级、命名、refs 和日志。
6. 单个 worker 超时：优先 packet-first 续跑或 takeover。
7. coordinator 超时：新 leader 接管下级。
8. 多个 worker 同时失败：批量恢复不无限扩容。
9. `latest_continue_packet.json` 损坏：降级 checkpoint/summary。
10. 连续失败：触发 no-progress fuse，不死循环。
11. 购物网站 E2E：worker 完成后 QA、修复、验收闭环。
12. 高端家具单文件 HTML：普通小白提示词不含专业术语。
13. 多次主代理 compact：能自动接住原任务状态。
14. 多次子代理 compact：只读 task-local refs，不污染主 memory。
15. 工具调用漂移：`write/file_path`、XML-ish、缺结束标记、长 content 分块都能恢复或给出可执行纠偏。

## First Slice Implemented

- 默认配置已经只暴露 8 个子代理相关用户项。
- 旧子代理微参数进入 `HIDDEN_COMPAT_CONFIG_FIELDS` 兼容层：老配置仍能被代码识别，但默认样例和新开发规范不再鼓励用户调这些细碎开关。
- 角色模板/契约改成职责叠加，所有角色都有基础读写、汇报和能力申请工具；root 执行上下文会隐藏能力申请工具。
- `context_bundle` 增加 `task_packet`，runner prompt 明确优先按结构化任务包执行。
- 标准 JSON 工具调用增加别名归一，减少模型把工具名或路径字段写错造成的硬失败。

## Step 9 Dispatch Envelope

- 中文说明：`create_subagents` 和 `schedule_child_subagents` 已经有 `typed_envelope`；本轮把 `dispatch_subagents` 也补成 `subagent_dispatch` typed envelope。
- envelope 只放稳定控制字段：`dry_run`、`summary`、`actionable_run_ids`、`recovery_run_ids`、`dispatch_json`、`dispatch_md`、`record_count` 和 scope。父级要继续推进或恢复时读这些字段，不从自然语言 `message` 里猜 run id。
- 旧报告里 `summary` 可能是字符串；新桥接会把它包成 `{"text": "..."}`，保持兼容。
- 这一步的目的不是增加流程，而是减少“模型把摘要当工具/把路径说错/把 run id 读漏”的机会。结构化字段是事实来源，自然语言只负责让人看懂。
