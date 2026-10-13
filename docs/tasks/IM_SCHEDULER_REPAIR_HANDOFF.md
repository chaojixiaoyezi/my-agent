# IM 调度查询与恢复修复交接

## 基本信息

- workstream: scheduler-recovery-audit / M04–M09
- branch: codex/im-entry-update-assessment
- 基线: 84a63ea87
- date: 2026-10-13
- 状态: 本地实现和定向验收完成；由主线整合严格 gate、Linux 实测、提交与打包。

## 本线目标

让原 owner 的监控查询不依赖可写锁，旧运行可准确结算并在历史里查到，坏运行不会被报成健康；旧群监控仅凭可信冻结来源恢复准确创建人。

## 实际完成

- 原 `store.json` 完整只读快照：common no-follow 读取，前后签名与完整长度核对，竞争最多三次；查询不创建锁、不取写 quota，不等待写方。写方原 quota→flock/CAS 与原子替换不变。
- 稳定结构化故障：忙碌、不可读、坏内容、schema 不匹配、owner 不匹配；异常上保存安全 details/errno，坏 JSON 有行列。
- `SchedulerService` 冷启动和每次入队前调用同源恢复。只有租约到期且进程死亡可证实才恢复；claimed 未开工可重新排队，running 已开工只记 unknown。
- 正常 claim、waiting、领取发现死亡与周期恢复共用原终态状态变更：移出活动账、更新 job、追加 history。history 已提交但 store 替换失败时保留同身份/epoch 原终态，不覆盖成 unknown。
- 旧版误留活动账本的合法 unknown 迁移进原历史，保留原原因；无原原因记结果未知，不冒称已证实死亡。不重放任务。
- 健康投影包含 run 解析错误，不能损坏报告 invalid 而 `/status` healthy。
- 旧 group 创建人迁移只从计划线程的不可变 execution_origin 取证，核对 source/thread/owner/operation；不使用群的最新 actor、不回落管理员。缺证据写 unmanaged 原因；只提供准确 ID 诊断，不授予当前 actor 控制权。

## 改动文件

- `agent_py_agent/agent/scheduler/repository.py`：无写锁现读、稳定错误载荷、同源终态提交、run 健康。
- `agent_py_agent/agent/scheduler/service.py`：启动与 tick 恢复接线，开始前终态逻辑拆到恢复模块。
- `agent_py_agent/agent/scheduler/store_snapshot.py`：受管普通文件的有界完整读取，无写入副作用。
- `agent_py_agent/agent/scheduler/recovery.py`：启动准备、开始前失败、死亡/旧终态结算；只写原 scheduler 账。
- `agent_py_agent/agent/scheduler/creator_migration.py`：准确来源创建人迁移与未归属 ID 查询。
- `agent_py_agent/tests/test_scheduler_repair.py`：11 项新回归（参数化后 11 项），先红后绿。
- `agent_py_agent/tests/test_scheduler_unknown_recovery.py`：unknown 应在历史中，不继续留活动账。
- `agent_py_agent/tests/test_scheduler_scan_costs.py`：保留缓存/扫描守卫，更新无读写锁和坏 run 健康口径。
- `docs/design/SCHEDULER_EXECUTION_POLICIES.md`：当前行为、诊断码、恢复与旧创建人边界。

## 测试命令和结果

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD:$PWD/agent_py_agent" .venv/bin/python -m pytest \
  agent_py_agent/tests/test_scheduler*.py agent_py_agent/tests/test_schedule_control.py \
  -q --tb=short --junitxml=/tmp/scheduler-repair-focused.xml

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD:$PWD/agent_py_agent" .venv/bin/python -m pytest \
  agent_py_agent/tests/test_scheduler_repair.py agent_py_agent/tests/test_scheduler_unknown_recovery.py \
  -q --tb=short --junitxml=/tmp/scheduler-repair-last.xml

.venv/bin/ruff check agent_py_agent/agent/scheduler \
  agent_py_agent/tests/test_scheduler_repair.py agent_py_agent/tests/test_scheduler_unknown_recovery.py \
  agent_py_agent/tests/test_scheduler_scan_costs.py

.venv/bin/python scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json \
  --report /tmp/scheduler-repair-code-size.md
```

结果：

- 旧基线的新缺陷断言先失败 8 项，覆盖锁依赖、恢复未接线、unknown 留活动账、健康误报、错误类别与创建人迁移。
- 完整定向调度组合 363 项通过、0 失败、0 跳过，58.8 秒。
- 最后一处旧 unknown 原因保留修正后，21 项恢复/新回归再次通过。
- 本线 Ruff、AST 语法与 scoped `git diff --check` 通过。
- 本线 code-size 无新阻断；检查时唯一全仓阻断为并行 Cloud `_classified_error` nesting，不在本线修改。报告单独写 `/tmp`，不重复覆盖主线生成报告。
- 未运行全仓 pytest；未触碰生产数据、服务、会话正文或安装。

## 影响范围

所有 owner 的 scheduler 读查询与原 claim 终态；group 的旧缺创建人记录会在服务冷启动尝试迁移。准确 creator 和活跃 runner 不改归属。停用/删除保留已开工 running/waiting，正常下一周期仍服从原失败策略。

## 需要主线重点复查

- 无写锁快照仅适用于写方原子替换的 JSON 权威，不能据此移除变更 CAS、quota 或 history 写锁。
- 租约过期不等于死亡；缺 PID/指纹、存活相同指纹、权限无法核实都不自动结算。
- 已开工 unknown 不能重新入队。新自然周期和旧 run 重放必须继续区分。
- history/store 两次提交之间失败时，只采纳准确同 owner/run/job/thread/epoch 的终态证据。
- 冷 query 仓库不偷偷写迁移。旧无 immutable origin 的群监控必须明确不可管理，不能按当前发言人或最新群绑定补 creator。

## 需要其他线协调

- `/schedule` 使用异常的 `error_code`、`details`、`errno`；详情中没有路径或正文。错误码：`SCHEDULER_STORE_BUSY`、`SCHEDULER_STORE_UNREADABLE`、`SCHEDULER_STORE_INVALID_CONTENT`、`SCHEDULER_STORE_SCHEMA_MISMATCH`、`SCHEDULER_STORE_OWNER_MISMATCH`。
- `unmanaged_monitor_ids(repository)` 返回未归属准确 ID；主线按既有用户/管理员范围决定提示，不展示别人任务名字。普通工具与 slash 控制继续按准确 creator 过滤。
- 主线统一补 CODEBASE_TREE、DESIGN_LEDGER、TESTS 等导航，独立恢复模块没有新的配置或第三方依赖。
- 主线生成报告 `CODE_SIZE_REPORT.md` 曾被一次尺寸检查写入；按主线统一生成/恢复，不将中间报告当发布验收。

## 剩余风险

Linux 真 Gateway SIGKILL/重启、实际 IM 用户入口和发行包尚需集成验证。上述 363 项来自隔离临时账本、假进程状态、fake LLM 或 Echo，不是生产故障现场已恢复的证明。未读取或迁移用户现网任务，也没有对任意旧无证据监控提供新的强制接管入口。

## 建议下一步

主线先冻结产品源码，组合命令诊断与 scheduler 回归，再在隔离 Linux 的单 Gateway 上验证 running 中止、活 runner 保留、旧 unknown 历史、群 creator 和新自然周期；确认完整严格 gate 后再推 main、打包。展示与安装可并行，恢复/身份链由主线统一守住，避免重复执行与跨 owner 授权。


## Linux 联合验收追加：缓存夹具修正

Linux 联合原结果为 524 项、1 失败、0 错误、0 跳过（436.3 秒）。唯一失败是 `test_scheduler_scan_costs.py::test_fact_cache_invalidated_when_scheduler_store_changes`；Mac 联合未失败。

旧 `84a63ea87` 的该测试在 Linux 同样失败；被测 `owner_wake_discovery.py` 新旧 SHA256 完全一致。原单例失败后使用 PDB 查看事实，不在失败前加采样或延迟：文件的当前元数据摘要等于缓存条目的摘要，缓存命中为 none；同一时刻无缓存判定却为 hard，任务时间已过期。这证明是同尺寸原地覆盖未改变 Linux 文件元数据签名，非时区或新调度恢复回归。完整测试元数据证据保存在测试者临时 `/tmp/scheduler-cache-linux-evidence.json`，没有业务正文。

该夹具原用 `Path.write_text` 原地覆盖，实际调度仓库唯一写方使用临时 JSON 文件的原子替换。现在只将夹具换为同一个 `write_json_file_atomic_unlocked`，保留所有发现结果断言；不加 sleep、skip、强改时间戳，也不修改 owner 缓存产品逻辑。粗粒度同尺寸原地违规改写仍由既有 TTL 边界兜底，生产原子替换按 inode 变化立即失效。

修正后 Linux 与 Mac 完整 `test_scheduler_scan_costs.py` 各 45 项通过、0 失败、0 跳过；结果分别在测试机 `/tmp/scheduler-scan-linux-fixed.xml` 与本机 `/tmp/scheduler-scan-mac-fixed.xml`。联合其它 523 项原已通过，生产代码与安装部署未改变。
