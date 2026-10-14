# N01 群监控与宿主发起人修复交接

## 基本信息

- workstream：N01 scheduler_recovery_audit
- worktree：managed main-release-gate-fix-20261012
- 基线：5142efb0a
- 状态：产品源已冻结；整合线统一严格验收、提交 main 和发布。

## 已完成

修复群助手创建监控成功、随后本人 list 为零及准确编号被拒的问题。Gateway 前台认证 scope、后台主根与子孙创建现在共用专有 HostChannelActor；creator/create/list/get/history/status/update/pause/resume/run_now/delete 读取同一个宿主来源，不把模型 attributes 同名键当身份。

原 ThreadTaskLink 的 actor 只从 typed keyword 首次保存；原 SubAgentTask 保存子孙专有字段并在原序列化往返。恢复按准确 parent/root/owner/thread 核对，不增加权限或新的身份账。已有根不能换人；同本人从新入口继续原根时允许新请求编号，孩子继续保留原根创建出处。普通用户、管理员和 full_access 都不能借其它群成员的监控或审批身份。

关闭两条独立审查确认的 P1：旧无 actor 主根加模型属性指向另一成员 work 线程不能借身份；私人审批不能使用 scope.rootB/currentA 或 scope.rootA/typedB 的混合身份。缺证明、坏记录、身份冲突和归属不匹配各有准确稳定错误码；本 owner 没有编号与已有但无权操作分开。

首次委派尚无 TaskLink 时复用准确原 SessionTask 和 immutable delegated 来源，必须同时匹配原 session_task_id、发送方、目标及 owner。普通派活无这套证明时返回 None，非身份业务继续；敏感操作仍拒绝。真实后台首次唤醒的 task_id 为空，装配沿原 session_task 引用恢复，不修改 claim 身份。

定时 run 的 creator 与首次冻结 origin_bindings 统一身份：后台开关关闭后群内新发言人不替换原布置者；新 isolated IM 线程复用原 execution_origin schema，原 SchedulerService 在新 TaskLink 上保存同源 typed actor。没有新增凭据下发、批准或网络/文件权限。

补升级边界：原 queued isolated 线程只有旧三字段 metadata 时，新来源不能被误当成身份冲突。迁移持原 TaskLink transition 锁及原 repository mutation scope（quota→store），在同一账本事务内核对 queued、未开工及冻结身份完全相同，再在原 ThreadStore 锁核对旧三字段/owner/home/无绑定才追加已知来源；保留所有可变用户设置。claimed、已 started、wrong run 和跨 owner 不改线程，claim/start 不能插入复核与写来源之间，不重写 scheduler store。

## 主要文件

- conversation/host_actor.py：专有冻结值、准确主根/委派恢复及谱系冲突检查。
- gateway_parts/request_execution.py、conversation/runtime.py：前台/后台原参数装配；runtime 的一处接线由 A 线实现。
- conversation/models.py、store_tasks.py、task_promotion.py：主根原账与首次 typed bind。
- subagents/model_task.py、services/base.py、services/hierarchy/context.py、scheduler.py，以及 orchestration/create_policy.py、subagent/run_flow.py：原孩子创建、序列化与逐片继承。
- scheduler/tool.py、execution_policy.py、execution.py、service.py：统一创建人来源、原冻结定时执行及新 TaskLink。
- conversation/im_tool_approval.py：准确原审批 scope 与本人私人接收者。

## 测试与独立审查

使用临时原 Repository、ConversationStore、TaskLink、SessionTask、创建服务和真实 ToolExecutor。模型/runner/运输由替身承接，没有真实飞书外发或读取生产业务正文。

```bash
python -m pytest agent_py_agent/tests/test_scheduler_host_actor.py \
  agent_py_agent/tests/test_im_tool_approval.py \
  agent_py_agent/tests/test_im_delegation_runtime.py \
  agent_py_agent/tests/test_scheduler_im_l_integration.py \
  agent_py_agent/tests/test_scheduler_delivery_policy.py -q --tb=short
```

结果：89 项通过。包括真实空 task_id 委派首片、两种执行模式乘后台开关四组真实 claim→后台参数→监控/审批→孩子创建、同群不同人、旧伪造字段、主子孙序列化、准确 root 冲突以及同本人新入口续原任务。

原子代理、决策准备、RuntimeDB、能力选择与调度/后台投递十文件回归 297 项通过，无失败、错误或跳过。仓库外证据为 actor-full-final-core.xml 与 actor-full-scheduler-subagent-regression.xml。

独立审查在最终冻结版本复跑原两个文件 56 项全部通过；两条 P1 负向探针原断言保留，攻击路径全部明确拒绝且 store/root 字节不变。证据为 my-agent-N01-independent-final.xml 和 my-agent-N01-independent-probes-final.json，均在仓库外。Ruff 本片和 git diff --check 通过；最终 strict code-size hard=0，报告为仓库外 N01_CODE_SIZE_FINAL.md。主线还需运行最终联合 focused tests、doc sync 与 clean package。

窄旧 queued 迁移完成后，test_scheduler_host_actor.py、test_scheduler_im_l_integration.py、test_scheduler_execution.py 与 test_scheduler_service.py 四文件 113 项通过，无失败或跳过（仓库外 N01_QUEUED_MIGRATION_FINAL.xml）。新正例验证来源恢复及 cwd/模型/其它 metadata 保留，四负例验证线程字节不变；strict size 再跑仍 hard=0（N01_CODE_SIZE_POST_MIGRATION.md）。非作者补审本次窄迁移，先前 P1 探针结论仍保持。

非作者随后确认 queued 重读与线程写之间还能直接 claim 的并发窗口，已把这两步纳入原 repository store 事务；真实并行 claim 在来源写时保持等待，写时状态为 queued，释放后才成为 claimed，未开工。与之相反的已 claimed 情况不写来源。另恢复合法 local 未晋升请求 actor=None，未给假夹具造 IM 身份。最终六相关文件 141 项通过（N01_ATOMIC_MIGRATION_LOCAL_FINAL.xml），涵盖上轮联合 gate 的 test_scheduler_tool.py 11 项原失败及两个新增永久合同。

测试夹具修正：旧 TaskLink 字节不变断言继续保留，只在模拟旧 asdict 载荷时省略新增 None 身份键。委派夹具使用真实后台 RunParams 并保留宿主当前参数，工具循环是投影；不再用缺身份的投影替换宿主事实。一个原本通过的 decision prepared-root 单例因新增无 actor 父 owner 比较被误伤，旧基线对照后撤回这项多余门；原局部 unmanaged 权限与准备对象合同保持不变。

## 参考与边界

先只读 Hermes 模块索引、gateway/session_context 和 delegate scoped registry，以及 free-code sessionIngress：采用宿主作用域与 canonical 身份锚的通用模式，不用全局最新成员、模型说明或 os.environ 兜底。已有 owner/run/claim/attempt 仍守执行权，本字段只证明发起人。

旧主群根完全缺失冻结证明时，不能从新的群绑定升级成任何人；非身份后台工作继续，敏感管理给明确拒绝。此前已开工的 TaskLink 不被静默改写。旧 isolated 线程只有准确未开工 run 的冻结证明才迁移来源，不能凭最新发言人补身份。

## 建议下一步

整合线先完成联合严格 gate，再由非作者复验原 P1 和真实测试包；随后沿用户授权提交远端 main、打包。A/N02 可并行做各自最终文档和独立审查，生产源保持冻结，任何新增修复须重跑直接相关入口。不要操作现网监控或补造生产身份记录。
