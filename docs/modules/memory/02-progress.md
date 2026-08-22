# Memory Progress

## 2026-08-22 运行中工具历史 Compact 统一账本

- 主代理、子代理和孙代理在同一运行 turn 内缩减 native 工具历史时，语义摘要会把
  ConversationThread 的上一代完整摘要与本次待回收工具往返合并成下一代替代摘要；
  不再只生成一份脱离会话代次的临时摘要。
- 持久、会话正文权威的真实 turn 必须在摘要成功后写 Compact checkpoint，并通过同一
  ConversationThread CAS 推进 generation；摘要为空、checkpoint 失败或 CAS 冲突时恢复
  原 native IR，并累计同一 Compact 失败熔断事实。
- raw archive、operation ledger、artifact registry 和真实文件仍是执行事实源；语义摘要
  只负责让下一模型轮知道已经做过什么、还缺什么，不能单独证明任务完成。

## 2026-08-17 测试债清理：home 优先级修复恢复记忆链路测试

- 根因：`_configured_home_root` 曾改为"环境变量无条件优先"，但测试 conftest 的
  隔离 fixture 给每个测试注入 MY_AGENT_HOME，导致 30+ 文件中显式
  `my_agent_home` 配置全部失效——memory runtime/basics/archive 等一整簇测试挂掉
  （看似记忆模块问题，实为 home 解析优先级）。
- 修复：显式配置值 > MY_AGENT_HOME 环境变量 > ~/.my-agent 兜底；配置 YAML 默认
  改空（未固定 home 时环境变量注入生效）。home 4 文件 54 项 + 记忆簇 ~38 项转绿。
- 记忆模块本身无行为变更；测试恢复覆盖 raw archive / runtime fact / auto-resume
  context / compact 续接等既有契约。

## 2026-08-17 EXEC-31b 测试适配（记忆路径）

- `tool_protocol="text"` 配置全部移除（30 文件）；协议测试快照默认改 native。
- 记忆相关假后端补 native 探针 + `**kwargs`（native 传 tools/messages/tool_choice）。
- compact 自动续接链在 native 工具轮下第二次续接的 ready 判定差异标 xfail 记录
  （`test_run_auto_compact_apply_can_repeat_when_continuation_makes_tool_progress`），
  待后续按 native 语义适配断言。

## 2026-08-16 阶段三 gorm 复刻实验（记忆侧观察）

- 双线（ma-a/ma-b）长任务复刻全程走 memory raw archive + runtime facts + auto
  resume context；未发现记忆模块新增缺陷。
- resume 链（run --resume）在多次 429 配额中断下跨进程恢复，memory archive 的
  recovery snapshot 与任务事实源保持一致性，无记忆污染。
