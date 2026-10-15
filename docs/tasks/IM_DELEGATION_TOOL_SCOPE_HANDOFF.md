# IM 自动委派工具范围与能力等待修复

状态：本地源码切片，尚未统一提交、发行或真实环境复验。生产 `.5` 故障原证据保留。

## 真实失败与归层

原请求的父快照有 27 个可用工具，含 `schedule`；自动创建的 worker 实际只有默认 coding 的 22 个工具，原执行上下文也没有 `schedule`。
工具发现只读当前授权快照，孩子随后申请的未知 MCP 能力落成 `GAP`，没有创建新计划。原 HostActor、完整 topic、SessionTask 引用输入仍准确，尚未走到机器人 Get 或群授权门。
这首先是自动委派启动合同遗漏业务工具范围，不能仅归咎模型没有选中工具。

另一个独立合同缺口：原能力生命周期允许 `BLOCKED + OPEN` 等直属父裁决，但 IM 协调层把 `BLOCKED` 归为结束，直接生成失败回执，无法进入原 resolve 工具阶段。
真实这次申请已是 `GAP`，因此不能据“没有 resolve 调用”反推父模型忽略了一个仍 OPEN 的申请。

## 最小实现

- 只在 IM 自动派工的原 `create_subagents` 调用显式传入当前父快照的可用工具，经现有 `active_model_subagent_tools` 和 leaf 过滤。
- 原创建工具仍冻结 `direct_parent_tool_authority` 并二次交集，canonical 孩子范围不能因 coding 默认合并而越过父快照。既有 runner 会为 `run_command` 补齐同族 `process_session` 续接工具，仍受 owner 禁用和原每次调用门约束；这不是 runtime 工具名严格子集保证。手工子代理默认保持原样。
- owner 禁用、网络、命令沙箱、写根、HostActor、原请求、群人工授权和每次工具调用的资源门继续生效。可见工具不等于获准执行任意操作。
- 仅活跃或 BLOCKED 孩子的 OPEN 申请进入原直属父 resolve 投影。父模型仍须显式 grant/deny，原工具负责审计、恢复同一个孩子及唤醒。
- GAP/CLOSED 或其它终态不泛化成活跃；无可处理申请时明确汇总真实失败，不造新队列、不自动批准。
- 接单薄回执补结构化的协调角色和本片未观察孩子结果事实；继续由模型表达，无工具的回执轮不构成孩子缺能力证据。没有固定回复或关键词判断。

## 合同与参考

本仓库原权威：`im_delegation`、`create_policy`、`tool_grants`、`role_templates`、`capability_scope`、`capability_service` 和 `ResolveCapabilityRequestsTool`。
对照 OpenClaw 的 `conversation-tool-policy-pipeline.ts`：继承与当前策略逐层收窄；`subagent-spawn-child-plan.ts`：沙箱与工作路径在原创建处继承；没有另建执行器或授权账本。

## 验证与证据

先红：实际 factory 孩子缺 `schedule`、BLOCKED+OPEN 无 resolve 投影，两项均失败，保留 `delegation-tool-scope-before.xml`。
直接矩阵：真实 factory/执行上下文/registry；main、user、group；父工具收窄；命令依赖闭包；owner 禁用与网络关闭；手工默认；原 topic/人工 grant；BLOCKED+OPEN 的 fake 模型请求 seam 与真实 grant 恢复同孩子；GAP、CLOSED、取消及其它终态。
模型与 provider 均为替身，Store、RuntimeDB、创建服务和授权生命周期真实；重入复用原 child，不重复派工。未连接生产平台。
最终集中计数及文件 SHA 以外部 `delegation-source-frozen-handoff.json` 为准；开发中失败报告保留，不覆盖为成功。

## 边界与建议下一步

原真实 SessionTask 在后续 root failed attempt 前已写 done，此时间关系单独记录，本切片不改其结算职责。
原 `.5` 后来有独立最终失败回执，本轮没有把故障描述成丢最终回复；薄回执是否仍误述能力，留给 `.6` 单 prompt 真实验收观察。
建议根统一严格 gate、提交和 `.6` 同源双平台发行后，先验自动委派订阅→事件→取消与姓名；Linux/Mac 工件可并行，生产 Gateway、UI 与真实授权账仍由根独占。

## 统一门旧夹具校准

61文件首轮的15红来自两份原MagicMock创建用例，准确b3ff基线15/15同样失败；假对象的动态属性不构成宿主身份。仅将local测试上下文显式化并补裸Mock仍拒绝的阴性，保留原行为断言，不放宽生产actor门或删测试。最终gate及同版发行另记。
