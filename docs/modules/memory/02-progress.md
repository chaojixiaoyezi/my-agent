# Memory Progress

## 2026-08-25 后台续片保留副作用操作终态

- 真机 `create_subagents` 的原始 tool artifact 已有 `tool_operation.status=succeeded`，但旧 index 没保存该
  nested typed fact；child 完成唤醒后的 carried record 只有 `ok=true`，按 fail-closed 核验被判 unverified，
  使 root 已回复却仍保持 active/Working。
- 当前 externalizer 对大小输出统一白名单保存 `tool_execution` 与 `tool_operation`，恢复时展开为现有
  operation verification 字段。`diagnostic/private` 等任意 envelope 字段不落 index；没有 operation 终态的
  旧记录仍保持 unverified，不用 `ok` 或输出正文补猜。
- externalizer、carried refs 与 background root completion 定向回归已证明成功 operation 跨片保持
  succeeded 且 task link 关闭；真 Gateway/TUI 复验待部署后执行。
- `4106025` 真机复验发现索引虽已有 operation，后台仍按 durable task id 查 foreground request 行。
  当前 externalizer 显式保存 `conversation_request_id`，child completion 信封传递同一 exact turn，
  carried refs 只按它恢复；字段落盘前的旧行仅在 `request_id` 同值时精确兼容。

## 2026-08-22 root active-turn 工具索引续接

- child lifecycle wake 现在复用 task `work/blobs/tool_outputs/index.jsonl` 的 typed 行，按 completion 信封的
  exact `conversation_request_id` 还原同一 active turn 已经执行过的工具；durable task id 只定位索引，
  不能当 turn id。小输出 `tool_call` 和大输出 `tool_output`
  共用 scoped call id 去重，后者只携带 artifact ref，不把正文整体塞回 prompt。
- r11 证明只恢复调用行仍不够：旧参数投影把 list 内 dict 丢掉，使 Todo/批量派工 `items=[]`；native 又
  不消费机械 tool-context。当前索引对 JSON 参数限深、限宽并递归脱敏，保留 items/covers 等结构关系；
  未知对象不 stringify。native 跨进程续跑把 carried 轨迹作为唯一有界 `CompactionSummary` 放回 IR，
  后续真实 UserTurn 保持在其后，不伪造 provider tool-use，也不重放副作用。
- 当前 Task Runtime State 另投影唯一 task_progress ledger 的 existing/open exact ids、完整 read 参数和
  `create_subagents.items[].covers` 精确绑定字段；代码不从标题或 goal 猜映射，也不自动合并同义清单。
- 该恢复只用于 root 后台工作片的执行连续性与 one-shot 去重，不改变 Compact generation、长期 Memory、
  child 私有 archive 或机器完成判断。r11 已证明原 objective/语言连续性；嵌套参数与 native handoff 的
  fresh r12 真机验证仍待严格 gate 和部署。

## 2026-08-22 运行中工具历史 Compact 统一账本

- 主代理、子代理和孙代理在同一运行 turn 内缩减 native 工具历史时，语义摘要会把
  ConversationThread 的上一代完整摘要与本次待回收工具往返合并成下一代替代摘要；
  不再只生成一份脱离会话代次的临时摘要。
- 持久、会话正文权威的真实 turn 必须在摘要成功后写 Compact checkpoint，并通过同一
  ConversationThread CAS 推进 generation；摘要为空、checkpoint 失败或 CAS 冲突时恢复
  原 native IR，并累计同一 Compact 失败熔断事实。
- raw archive、operation ledger、artifact registry 和真实文件仍是执行事实源；语义摘要
  只负责让下一模型轮知道已经做过什么、还缺什么，不能单独证明任务完成。
- `.7` 原样 Prompt 4 真机已证明 worker-3 在 119,295 tokens 提交 generation 1 并降到 36,586；但摘要模型
  普通续写最后工具动作。根因是 backend 将摘要 prompt 放在 history 最前。当前按 会话运行时 改为原任务 user
  在前、native history 居中、synthetic Compact user 指令最后；位置敏感 fake 回归会在顺序倒退时直接失败。

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
