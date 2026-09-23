# 第 7 步唤醒配对发布交接

## 基本信息

- workstream：第 7 步稳定 key 的唤醒／观察半写恢复。
- branch：`codex/wake-publication-step7`。
- worktree：独立隔离工作树；基线 `c37ca0c90`，不包含主线之后的真实 TUI 文档提交。
- owner：唤醒发布实施线；主线继续负责结果提交依赖收窄、集成和真实验收。
- date：2026-09-23。

## 本线目标

原 wake 已写出、dedupe 回执替换失败时，runner WAL 重试会生成第二条通知；观察的随机无链兜底又会掩盖部分发布。
本片只关闭稳定 key 发布的文件一致性缺口，保留原队列、观察账、文件锁和 runner WAL 恢复入口。

## 实际完成

- 原 dedupe 文件升级为 v2：先冻结完整 signal／可选 observation 与身份，按固定 ID 安装原文件后确认 published。
- prepared 重试始终恢复同一份原内容，即使原 wake 已 handled；观察只有固定 ID 不存在才追加。
- 默认通用策略保持 pending 合并、published／handled 后新一代；runner 完成显式保留 handled，由同一发布锁裁决。
- 删除完成通知的前置回执旁路和发布异常时的随机无链观察；失败沿原通知错误和 WAL 重试传播。
- 查询只读原子快照，不取写锁、不创建锁文件或触发恢复；旧 v1 只在写入口从原已发布内容显式迁移。
- 损坏、身份冲突、v1 原观察缺失均报错并保留原账，不用重试的新正文补造旧事实。
- 未新增配置、依赖、队列或通用恢复服务；这是现有持久发布合同的修复。

## 改动文件

- `agent_py_agent/agent/conversation/store_wake_publication.py`：原 dedupe 发布／恢复／迁移与纯读回执；只接收原 storage。
- `agent_py_agent/agent/conversation/store_wakes.py`：组装配对发布、返回原冻结观察，保留消费和投递冻结职责。
- `agent_py_agent/agent/subagents/runner_completion_wake.py`：精确完成显式 `retain_handled=True`，不以前置查询绕开恢复。
- `agent_py_agent/tests/test_wake_publication_recovery.py`：32 项故障、并发、重放、Goal 与迁移验证。
- `agent_py_agent/tests/test_dispatch_liveness_and_revive.py`：保留两项既有红灯的真实文件替换故障事实及单通知断言。
- `agent_py_agent/tests/test_conversation_wake_events.py`：原 wake-before-observation 断言跟随真实追加入口。
- `docs/design/closeout_state_machine.md`、`DESIGN_LEDGER.md`、`LLM_GUIDE.md`、`CODEBASE_TREE.md`、
  `TESTS.md`、`docs/ROADMAP.md`、`docs/COMPLETED.md`、子代理进度／结构文档和本交接：同步合同、职责及验收边界。

## 测试命令和结果

修前仅运行两项 `test_closeout_wake_receipt_half_write_does_not_duplicate`，均失败（`2 != 1`）；
新顺序先预留，注入点移到 wake 安装后的最终回执替换，不删除原断言。

```bash
python3 -m pytest -o addopts='' \
  agent_py_agent/tests/test_wake_publication_recovery.py \
  agent_py_agent/tests/test_dispatch_liveness_and_revive.py \
  agent_py_agent/tests/test_conversation_wake_events.py \
  agent_py_agent/tests/test_closeout_recovery_paging.py \
  agent_py_agent/tests/test_direct_parent_lifecycle.py \
  agent_py_agent/tests/test_subagent_runner_result_state.py \
  agent_py_agent/tests/test_service_window_semantics.py \
  agent_py_agent/tests/test_conversation_store.py \
  agent_py_agent/tests/test_conversation_goal_tools.py \
  agent_py_agent/tests/test_goal_lifecycle_recovery.py \
  agent_py_agent/tests/test_observation_route.py -q --tb=short
ruff check agent_py_agent scripts
python3 scripts/check_doc_sync.py
python3 scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json
git diff --check
python3 scripts/check_clean_package.py .
```

定向结果：**261 passed、1 项既有 skipped**（10.58 秒），未运行全仓 pytest。
本地严格 gate 全部通过，strict code-size 为 `blocked=False`；新增行隐私扫描无命中。
初次 Ruff 只报新测试的 import 排序、clean-package 只报两个新文件尚未暂存，修正并暂存后复跑均通过。
线上 CI、wheel 安装与真实 TUI 不作为本片验收来源。
所有文件故障注入都只写 pytest 临时目录，没有读取私密配置或操作真实任务。

## 影响范围

稳定 key 发布使用原 dedupe 路径，因此不迁移 Store 目录或创建第二套状态。
原 WAL→RuntimeDB→父通知→已交付标记→清 WAL 顺序不变；只有父通知内部从先信号后回执改为先冻结配对再安装。
消费仍先写 handled 再删除 pending，合法的 owner_delivery 冻结保留；线程活动更新仍由原 WakeStore 完成。
v1 已完整发布记录按原 signal／observation 迁移，迁移标记留在原文件；旧版缺失的观察无法可靠反推时明确报错。

## 需要主线重点复查

- 完成通知不能恢复前置 `delivery_receipt` 提前返回，否则会绕过 prepared 配对恢复；永久去重权威在原发布锁。
- 通用 Goal 必须保持默认策略；不能把所有 handled 改成永久去重。
- 新 schema 与旧 schema 的交接必须单版部署；旧运行代码不理解 prepared，不能和新代码同时写同一数据目录。
- 集成主线依赖收窄时，`test_dispatch_liveness_and_revive.py` 两处旧 `record_pending_closeout(manager, ...)`
  调用由主线按其新签名调整；本片没有抢改该依赖接口。

## 需要其他线协调

主线新 TUI 与台账提交独立保留，不因本片故障回归通过改写其源码版本和运行结果。
能力申请旧入口 `runner_completion_wake.py::notify_parent_on_capability_request` 先写观察再 raise_signal，
异常只记 `capability_request_notify_error`；此入口本身没有重试。已向主线明确上报，未为它预建通用恢复器。

## 剩余风险

- prepared 后调用方不重试时不会自动安装；runner 由原 WAL 重试，通用调用没有新增宿主扫描承诺。
- 无 key 调用不承诺重试幂等；通用 published／handled 后的新调用会开启下一代，不能当永久去重使用。
- 观察账已有坏行时不自动截断或清空；保留 prepared 和错误，需要沿明确的账本修复流程处理。
- published 快照表示该次配对已完整安装；不自动为外部删除或损坏的观察重新造账。
- 离线注入完整文件操作前／后异常和多 Store 同进程并发；未做跨进程强杀、真实硬断电、Windows 文件锁或新包原生 TUI 验收。
- JSONL 复用原 flush append，未改成 fsync 或原子替换；字节级截断行明确报错并保留 prepared。
  单文件原子替换也复用现有 IO，不额外承诺断电持久性。未 push、部署、运行真实任务或调用真实模型。

## 建议下一步

主线先审阅并集成本片，再与依赖收窄片跑组合 focused／严格 gate；只有同一版发布后才能做原生 TUI 恢复验收。
其他 agent 可并行审阅原文件发布合同和迁移边界，避免同时改写本片通知文件或用旧运行包证明新代码通过。
