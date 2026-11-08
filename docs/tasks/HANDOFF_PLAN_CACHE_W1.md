# Workstream Handoff

## 基本信息

- workstream：PLAN-CACHE-W1（前台逐轮预载写回线程）
- branch：`plan-cache-w1`
- worktree：`/private/tmp/wt-plan-cache-w1`
- owner：c1 [SOL]
- date：2026-11-07
- base：`bfafa4ca7ec084570136abaadd0f62f9af352d34`
- 代码提交：`e5c7438dbc6a7e6b42ce1fb72da5e19838d632d1`；本轮接续时已存在，未改写其历史。

## 本线目标

前台本轮因创作或显式任务预载的工具，下一轮仍从原线程的已加载集合恢复，避免工具表随提示词翻转，导致工具表之后的历史缓存前缀失效。

## 实际完成

- `_initial_loop_loaded_tool_names` 沿本轮快照与 allowed 交集记录前台新增预载；本轮返回集合的成员保持原样。
- `persist_turn_preloaded_tools` 复用 `record_session_discovered_tools` 的线程 CAS；按 `sorted(fresh)` 追加，正常重复回合不写盘。
- 范围门与 `mount_thread_asset_tools` 一致：`task_local / isolated / control_plane`、`agent_thread_id`、子代理上下文不写回；后台唤醒轮不增加这次持久化调用。
- 不授权、不执行、不改 allowed；无新增配置、读取上限、输出上限或压缩阈值。
- 现有生产补丁和新增测试的运行逻辑与任务书一致。当前 ci-venv Ruff 要求两处长导入使用多行排版；产品注释移近关联语句，产品总计仍为 +18/-1。9 项测试断言保持原样。

## 改动文件

- `agent_py_agent/agent/agent_core/runtime/loop_support.py`：+6/-1（含导入排版）。
- `agent_py_agent/agent/conversation/tool_discovery.py`：+12（同一范围门和原 CAS，注释随语句保留）。
- `agent_py_agent/tests/test_cache_sticky_turn_preloads.py`：9 项回归。
- `docs/tasks/HANDOFF_PLAN_CACHE_W1.md`：本交接。

产品代码合计 +18/-1，只有白名单内的两个生产文件。共享的 `CODEBASE_TREE.md`、`DESIGN_LEDGER.md`、`TESTS.md` 由集成人统一更新，本片不改。

## 测试命令和结果

解释器为指定 ci-venv 的 Python 3.12.13，平台 macOS 26.4 arm64；实际导入源来自本工作树。HOME 与 MY_AGENT_HOME 均为 `/private/tmp/my-agent-home-cache-w1`，不读取真实私人空间。生产和测试源文件在本轮门禁期间保持不变。

```bash
bash /private/tmp/wt-plan-cache-w1.gate.sh
```

- 并发完整门禁见 `gate-current.log`；本轮独立门禁与复跑记录见下方“独立验收记录”，不将运行中状态写成通过。
- 旧红绿记录：基线 8 failed / 1 passed；补丁后 9 passed。原记录保留，本轮未把缺函数的异常冒称全部都是行为断言失败。
- 消费方由指定 `rg -l` 动态重算为 63 份，含新增测试；与既有清单一致。
- 指定 ci-venv 缺 Pillow。按规格剔除规则核对，本片消费方中直接导入 PIL 的文件为 0，因此实际剔除清单为空；没有修改 ci-venv 或跳过本片测试。
- 仓外补充运行探针通过：两个受控并发预载保留名称并集与无关 metadata；32 次正常重复调用零 CAS；缺失线程不补造；模拟存储写入异常保持原对象传播；allowed 交集和当前轮集合不变；完整清理后 `fd_delta == 0`。结果：`/private/tmp/my-agent-home-cache-w1/invariant-proof.json`。
- 6.1-sol 对给定的精确生产片段独立只读静态复核，未发现确定 P0/P1；该复核不等于独立运行完整仓库或门禁。

统一验收脚本的规格目录尚未拆出。隔离 HOME 已有的脚本经逐字核验，等于审计结果 `opus-cache-hit-audit-result.md` 第 8.4 节原件，SHA-256：`4321bebbec2fc1e26cb5366f3af18956183f7b7f11b606ff15df68bcd29baa39`，未修改脚本或原发布资料。

本地提交之后执行下列入口，读取的是已提交分支而非未提交工作区：

```bash
HOME=/private/tmp/my-agent-home-cache-w1 MY_AGENT_HOME=/private/tmp/my-agent-home-cache-w1 \
PYTHONDONTWRITEBYTECODE=1 TMPDIR=/private/tmp/my-agent-home-cache-w1 \
/Users/xiaoyezi/.my-agent/releases/claude-tools/ci-venv-312/bin/python \
/private/tmp/my-agent-home-cache-w1/opus-cache-kit/run_review.py post \
  --ref plan-cache-w1 --only W1 --repo /private/tmp/wt-plan-cache-w1 \
  --home /private/tmp/my-agent-home-cache-w1 \
  --work /private/tmp/my-agent-home-cache-w1/post-review-current
```

- 上述为原执行者准备的提交后入口；本轮独立导出使用不同的 `post-review-codex` 目录，避免覆盖并发验收快照。最终结果按实际日志记录。
- 完整本地门禁日志：`/private/tmp/my-agent-home-cache-w1/gate-current.log`。
- 源码、消费方与 gate 脚本核对：`/private/tmp/my-agent-home-cache-w1/consumers-verified.json`。

## 影响范围

只延续现有粘性展示语义，与 tool_search 加载共享一份线程集合。压缩提交时仍沿原 active-set 选择收缩；没有修改执行许可、前后台路由、取消或恢复状态机。

## 需要主线重点复查

统一 W1 探针须同时证明：第三轮工具表与第二轮末次逐字节一致；第三轮首调模拟命中率至少 0.90；线程持久包含 scripts/workflows。真实供应商缓存、账单和线上账号未验证，不能从本地模拟器结果外推。

## 需要其他线协调

W2 可在独立工作树并行，避免并写本片两个生产文件。集成人负责共享台账、最终集成分支及不带 `--only` 的组合统一验收。

## 剩余风险

- 前台已加载集合预期单调增长到下一次压缩挑选活跃集，这是既有 tool_search 粘性语义。
- 线程不存在时沿原入口静默跳过；其它写入异常原样传播，不另设恢复或吞错路径。
- CAS 合并保留最新线程与其它字段。正常重复回合已验证不写盘；不承诺两个并发同名预载在各自锁外读取 fresh 后只发生一次 CAS。
- 不把局部静态复核、FD 探针或缓存模拟器结果当作真实模型、真实 TUI、线上或跨平台验收。
- 未 push、合并、部署或重启服务；没有改主检出。

## 建议下一步

先核对本地完整 gate 和提交后 W1 探针，再与 W2 集成到 `plan-cache-int`，运行不带 `--only` 的统一复核。W2 与只读复核可以并行，两个生产文件保持单写者，真实供应商命中与账单另在最终版本上验证。

## 独立验收记录

- 首次完整门禁日志：`/private/tmp/my-agent-home-cache-w1/gate.log`。结果为 2268 passed、20 xfailed、1 failed；唯一失败是金样 `vertical_slice_gp6` 在场景自带的 100 秒总时限触发超时。没有调整场景时限、金样断言或测试范围，正在以独立基线导出核对。
- 首次门禁同时发现两处导入排版与未暂存交接文件；导入已按当前 Ruff 修正，交接纳入暂存后重新检查 clean-package。
- 预期修复的缓存回归仍为基线 8 failed / 1 passed → 新文件 9 passed。
- 后续完整门禁和提交后独立 W1 复核必须取得实际退出码与探针事实，再将本节补齐。独立复核工作目录为 `/private/tmp/my-agent-home-cache-w1/post-review-codex`，日志为 `/private/tmp/my-agent-home-cache-w1/post-review-codex.log`。
