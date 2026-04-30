# Memory：灵感碰撞 / 功能讨论记录

## 为什么想做

memory 模块要解决的是“长任务做久以后，重要事实散在聊天、临时摘要和任务目录里，下一轮很难可靠恢复”。它不是单纯把更多文字塞进 prompt，而是把记忆写入、路由、归档、诊断和恢复拆成可检查的工程链路。

## 讨论过什么

- 长期规则不能无限塞进常驻上下文，应该先走确定性 route，再读取权威文件。
- 压缩摘要只服务继续推理，不能替代事实源；事实源要回到任务目录、daily memory、hook/raw archive。
- `memory_archive_level`、hook retention、raw archive 要有配置诊断，不能悄悄丢信息。
- 用户 `--no-save` 时不能写冷归档，避免违背“不保存”的明确意图。

## 痛点

- 模型上下文会被截断，靠聊天历史“记住”不可靠。
- 规则和事实混在一起，后续很难判断哪条是权威依据。
- 旧 `memory.jsonl`、LocalStore、route index、raw archive 如果不同步，恢复任务会失真。
- 新手很难从一堆 memory 文件名里看懂“写入、索引、路由、归档”各自负责什么。

## 方向

第一版优先保证可见、可诊断、可回放：记忆仍写 JSONL，同时接 LocalStore 索引；长期规则通过 route index 找权威文件；raw archive 和 snapshot 用于恢复链路，不替代用户显式记忆。

## 相关旧文档

- [DESIGN_LEDGER.md](../../../DESIGN_LEDGER.md)
- [MEMORY_BACKLOG.md](../../../MEMORY_BACKLOG.md)
- [ARCHITECTURE_GUIDE.md](../../../ARCHITECTURE_GUIDE.md)
- [TESTS.md](../../../TESTS.md)
- [EVIDENCE.md](../../../EVIDENCE.md)

## 当前第一版索引 / 待补齐

本页先收拢 memory 的讨论入口。后续需要把 route index 样例、raw archive 样例、恢复失败样本和配置取舍按时间线补细。
