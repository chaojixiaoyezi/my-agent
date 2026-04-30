# Live Lab：开发推进记录

## 已完成

- `scripts/live_agent_lab.py` 已作为 Live Lab 薄入口。
- `scripts/live_lab/` 已拆出 CLI、runner、cases、constants 和 log-analysis replay。
- log-analysis replay 可离线跑 SecurityAlertV1 fixture，产出 case、route、evidence、first response report、forensic package 和 replay summary。
- replay 已有 no-finding negative fixture，以及 detector/evidence/report stage 失败模拟测试。
- `TESTS.md` 已记录 Live Lab 和 offline replay 的运行入口。

## 解决的问题

- 把“真实链路能不能跑”从临时手工命令变成可重复 suite。
- LOG replay 不依赖真实 LLM，也能验证 ingest、detector、case、route、evidence、report 的闭环。
- 失败 stage、error_type、error_message 等字段让父会话和 reviewer 能知道哪里坏了。
- Live Lab 产物目录让验收不只看终端输出，还能检查真实文件。

## 下一步

- 增加更多 suite：gateway ask 长链路、subagent runner 长链路、memory 恢复链路。
- 给每个 suite 建固定输出 schema 和人类可读 summary。
- 把 Live Lab 结果接入 acceptance/evidence 记录模板。
- 如果新增 case 或改变产物路径，同步更新本文件和 `04-structure.md`。

## 已跑测试

- 历史记录显示 `agent_py_agent/tests/test_live_lab_log_analysis_replay.py` 已覆盖正向 replay、no-finding、evidence/report failure gates。
- 历史验收记录显示 `python scripts\live_agent_lab.py --suite log-analysis ...` 曾返回 `LIVE_LAB_PASS`。
- 同步门 focused 验收：`python -m pytest agent_py_agent\tests\test_doc_sync.py` -> `5 passed`。
- 同步门手工检查：`python scripts\check_doc_sync.py` -> `DOC_SYNC_PASS`。
- 父会话全量回归：`python -m pytest` -> `241 passed`。
- 空白检查：`git diff --check` -> passed。

## 未跑测试

- 本文档第一版没有重新启动可见 Terminal 或真实 API Live Lab。
- 暂未新增 gateway/memory/subagent 的 Live Lab suite。

## 风险

- Live Lab 很容易被误解成普通单测；它更像“可见演练”，成本和依赖可能更高。
- 当前 log-analysis replay 较成熟，其它模块 suite 还需要补。
- 如果 suite 输出路径变化但文档没同步，新手会按旧路径找不到产物。
