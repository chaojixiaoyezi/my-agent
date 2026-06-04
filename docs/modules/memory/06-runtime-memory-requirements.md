# Runtime Memory Requirements

## 必须

- 主代理长期记忆写当前 owner `memory/long_term/memory.jsonl`。
- 每日工作记忆写当前 owner `memory/daily/YYYY-MM-DD.jsonl`。
- raw turn/tool/gateway 审计写当前 owner `audit/YYYY-MM-DD.jsonl`。
- 大工具输出写当前 owner `blobs/tool_outputs/`。
- task workspace 固定为 `tasks/<date>/<task-slug>/{output,work}/`。
- 子代理状态固定为 task-local `work/agents/<run_id>/canonical_state.json`。
- 所有索引记录都要能追到正文事实路径。

## 禁止

- 普通运行扫描 repo `data/*` 当事实源。
- 子代理自动提升长期记忆。
- 用模型自然语言猜写入根、验收状态或测试结果。
- 新业务字段塞入通用扩展槽。
- 用“还能跑”的旁路吞掉损坏状态；损坏要显式记录错误并指向修复入口。

## 可选维护

- retention 只在显式维护命令中执行。
- index rebuild 只重建轻量索引，不改正文事实。
- backup/export 只作为维护动作，不插入普通任务链路。
