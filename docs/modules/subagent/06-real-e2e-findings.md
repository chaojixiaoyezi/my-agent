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
