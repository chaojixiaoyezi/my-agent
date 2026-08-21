# Subagent Real E2E Findings

旧的父验收历史记录已删除，避免继续误导实现。后续真实 E2E 只记录子代理执行、协作、状态树、证据 refs 和普通 closeout 发现的问题。

## 2026-08-21 `.7` Gateway 重启后的 unknown 父级热循环

- 现象：旧任务主 run/current attempt 已由崩溃恢复标为 `unknown`，多个未处理 child lifecycle observation
  每个 tick 都触发一次父级自动挂载；`create_attempt` 正确 fail-closed，但 Gateway 连续输出同一
  `RuntimeConflictError`，并可能让队首旧事件压住同线程新任务。
- 根因：执行权层已有 unknown 硬闸，事件消费层却没有读取同一结构化状态；observation 还只按 thread 合批，
  全局 limit 在过滤前切片。另一个缺口是显式 `recover_attempt_unknown` 只恢复 attempt，未复原 startup
  recovery 同时写成 unknown 的 run。
- 修正：新增仓储结构化 recovery block 投影；wake/observation/policy 在 unknown 时保留且零模型调用，
  observation 改按 thread+task 分批且 blocked 不占限额，claim 后再做一次竞态复核。人工恢复 current
  attempt 时同步把 run `unknown -> created`，只释放该 attempt 的执行权锁。
- 待真机证据：部署后以新日志偏移确认无重复 loop error，再从真实 TUI 单次发送原植物大战僵尸 prompt，
  观察递归 child 事件、最终汇报和受管 0.0.0.0:8080 服务；测试者不修改游戏文件。

## 2026-08-21 `.7` 窄交付授权被宽 forbidden 误伤，child 空状态看似挂死

- 场景与时间：`c2c0235` 部署后的单 Gateway / 真实 TUI 植物大战僵尸任务，请求
  `gwreq-1787341013-45962a241231475c9b837d7494ffbef5`。测试者只输入一次普通中文目标并观察；约 33 分钟
  时 4 个 child 中 2 个 `DONE`、1 个 `RUNNING`、1 个反复 grant 后回到 `PENDING`，最终游戏未交付。
- 现象：两个未收口 child 对已经列入 `allowed_write_roots` 的 task output 连续收到
  `WRITE_FORBIDDEN`；同一路径能力申请被自动 grant 两到三次仍无效。canonical state 的
  `current_tool/current_step/latest_summary` 始终为空，TUI 只能显示 `RUNNING/0%`，用户无法区分真实工作、
  权限循环和模型卡住。
- 根因：`write_boundary._forbidden_root_blocks_target` 只要发现宽 allowed root 与 `/root` forbidden 重合就
  一律拒绝，没有比较命中目标的规则具体程度；新增的窄 task output allow 因而永远输给祖先保护。另一个
  独立缺口是 `session_progress` 读取不存在的 `ToolResult.tool`，真实字段为 `tool_name`；runner stage trace
  虽有 typed 模型/工具边界，却只刷新 heartbeat，没有投影可见活动。
- 修正：对照 会话运行时 `FileSystemSandboxPolicy` 改成最具体命中条目优先、同层 deny 胜出；工具状态改读
  `tool_name`；模型请求和工具开始/结束把不含正文的有界短活动写入 canonical state。重复宿主生命周期的
  `raise_event` 模型工具同时删除，活动/阻塞/终态仍由内部 observation/wake 服务回传。
- 验证：本地 write-boundary、filesystem、subagent output、runner stage、kernel、工具注册/规格、
  conversation wake 与错误语义 focused 已通过。失败现场已原样保存到
  `/root/tui-parity-evidence/c2c0235-pvz/`，含 task、pane、request 和进程证据。
- 当前状态与风险：本地候选已修，相关 focused 和本地严格 gate 已通过，按用户约定未跑全仓 pytest。待推送、
  单 Gateway 重启部署后用全新 TUI 再输入一次同一
  prompt。通过必须看到 child 首次写入窄 output、无重复同路径 capability request、有非空活动状态、自然
  结束并由父级完成整合与受管 `0.0.0.0:8080`；不能只以单测或模型自述结案。
