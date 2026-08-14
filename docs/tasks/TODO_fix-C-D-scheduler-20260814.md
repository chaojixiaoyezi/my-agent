# 问题 C/D 修复任务书（2026-08-14，测试真机挖出）

> 测试期间 4 个实证产品问题：A（环境缺 tar，已修）/ B（INVALID_ARGUMENTS 误归
> UNKNOWN，已修 ceb7d582）/ C（attempt 卡 running 无自愈）/ D（重启丢 processing
> 请求）。本文档为 C/D 的修复任务书（证据 + 根因方向 + 验证方案），按序独立切片。

## 问题 C：solo 任务 attempt 卡 running 零推进且无自愈

### 真机证据（④类第3轮，req_1786673877744_59696_17）

- agent_runs: `agentrun-1786673878-546d5e77` status=created（30+ 分钟不变）
- agent_attempts: attempt-1786673878-5b4851a2 (gen1) + attempt-1786674436-1408fc3c (gen2)
  双 running，started_at 与 updated_at 差 0（记录零推进）
- state.json: status=RUNNING, progress=0.0, updated_at 停在 10:18（30 分钟不动）
- 8420 /status: requests.processing=0（**无执行线程**）——attempt 状态与执行事实脱节
- gateway 后台循环正常（每 2 分钟 wake-pending），但没驱动该 run
- **重启 gateway 后仍未自愈**（recover_interrupted_executions 未覆盖此形态，
  重启后无 recover 日志，attempt 保持 running）

### 根因（2026-08-14 代码链确认）

1. 执行线程异常退出（未捕获异常/线程死）时，attempt 状态机停留在 running——
   正常路径（done/failed 收口）没走到
2. **精确机制（关键）**：`_reclaim_orphaned_attempts`（conversation/runtime.py:3704，
   每 5 分钟 tick）已有孤儿回收，但 `reclaim_orphaned_attempt`（repository.py:1125）
   有 **fail-closed 副作用门**：attempt 有外部副作用 op（apply_patch/write_file 等）
   且无 effect_key（无法验证幂等）→ **拒自动判 failed，交人工**——第3轮 attempt
   正因有 apply_patch/write_file 操作被门拦住，回收永不执行 → 永久卡 running
3. 副作用门拦截是**静默**的（无事件/无标记）——卡死不可见，wake 循环不驱动
   created run，owner 无感知
4. recover_interrupted_executions（scheduler 域）只覆盖 scheduler runs，
   不覆盖 gateway 域 attempt

### 修复方向（真根因，保持安全）

- **拦截转可见**（治本最小改动）：reclaim 副作用门拦截时不得静默——把
  attempt 标记为可见待处理（runtime_events 写 orphan_reclaim_blocked 事件 +
  attempt 标 needs_input 或 run 标 needs_input），让 owner/wake 循环能发现、
  人工可处理；不再无声卡 running
- **执行心跳**（可选增强）：attempt 执行循环更新 last_progress_at，wake 循环
  对 running+心跳过期+无执行者的 attempt 触发 reclaim 扫描（兜底）
- **recover 覆盖扩展**：重启恢复时对 gateway 域 running attempt 且无 in-flight
  执行者的一律重开（而不是只认中断标记）

### 验证方案

- 单测：构造 running attempt + 心跳过期 → 调度循环终态化/重开
- 真机：复现卡死场景（kill 执行线程模拟）→ 看门狗在 N 分钟内救活/重开
- 全量 gate + 部署 1.10 + 复跑④类复刻任务

## 问题 D：gateway 重启丢 processing 队列请求

### 真机证据（④类第4轮，req_1786676643072_75125_0）

- POST /ask 返回 queued（req 文件进 processing/ 目录）
- 为恢复问题 C 重启 gateway 后：req 文件从 processing/ 消失，
  **不在 done/failed/pending 任何终态目录**——请求被静默丢弃
- 未重放（pending 无该 req）也未标失败（failed 无该 req）

### 根因方向（需 gateway 队列代码确认）

- gateway 启动时的 processing 目录清理逻辑：把 processing 中的文件视为
  「上次进程遗留」直接删除，没有「移到 pending 重放」或「标 failed」
- 重启时对 in-flight 请求的恢复策略缺失

### 修复方向（真根因）

- 启动恢复：processing/ 中无 .lock 或 lock 过期的请求 → 原子移回 pending/
  （重放，幂等键防双跑）；有活跃 lock 且进程死亡的 → 标 failed
  （TOOL_OPERATION_OUTCOME_UNKNOWN 语义）
- 与 R1-03 lease 感知/唤醒轮恢复衔接（不能与卡死 attempt 恢复打架）

### 验证方案

- 单测：构造 processing 队列 + 模拟重启 → 请求重放或标失败（绝不静默消失）
- 真机：发请求 → 立即重启 gateway → 请求最终有终态（done 或 failed）
- 全量 gate + 部署 1.10

## 顺序与边界

- C 先于 D（C 是 D 的触发场景之一；两者都涉及重启/恢复语义，分开切片防耦合）
- 每切片独立 commit + focused 测试 + 全量 gate + 部署 1.10 + 真机复验
- 修复期间 1.10 上遗留的测试数据（test-conv/test-trans/test-script/test-replicate
  用户）可清理（用户说过 1.10 测试数据不重要）
