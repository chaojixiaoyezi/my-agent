# Memory：结构树和详细说明

## 模块结构

```text
agent_py_agent/agent/
|-- memory.py                         # 旧兼容入口，真实存储已拆到 memory_store/
|-- memory_settings.py                # 旧兼容入口，真实配置解析在 settings/memory.py
|-- settings/memory.py                # memory 配置、默认值、warning、安全归一化和参数边界
|-- memory_store/                     # 长期记忆 JSONL 事实流水，可选同步索引到 LocalStore
|-- memory_routing/                   # route index、匹配、required/candidate path、read receipt
`-- memory_archive/                   # hook snapshot、raw archive、留存、token 估算

agent_py_agent/cli/
|-- memory_commands.py                # memory-route / memory-doctor 等可见诊断命令
`-- memory_archive_commands.py        # memory archive 相关命令
```

## 核心文件

- `memory_store/jsonl.py`：读写长期记忆 JSONL，是最朴素的事实落盘层；LocalStore 只是索引，不替代 JSONL。
- `settings/memory.py`：解析配置，处理非法值回退和 warning；会原地更新 AgentConfig-like 对象。
- `memory_routing/loader.py`：读取 route index。
- `memory_routing/models.py`：定义 route、match、path resolution、read receipt 等票据结构。
- `memory_routing/matcher.py`：根据用户输入匹配可能需要读取的长期规则，并区分 required/candidate path。
- `memory_routing/context.py`：把命中的规则变成运行时可注入的上下文片段。
- `memory_archive/storage.py`：保存 raw archive 和 hook snapshot。
- `memory_archive/runtime.py`：把 run turn 的元数据写入归档。
- `cli/memory_commands.py`：给用户和开发者看 route/doctor 结果。

## 数据流

1. 用户对话或命令触发记忆写入，基础事实先落到 JSONL。
2. LocalStore 可以为旧 memory 补建索引，让搜索和 timeline 能看到它。
3. 当新任务需要规则时，memory routing 根据 query 匹配 route index。
4. 匹配到的 authority path 会被安全读取成上下文片段。
5. 长任务或压缩前，archive 写 raw event / snapshot，方便后续恢复和审计。
6. doctor 命令检查配置、route index、hook/raw 目录和潜在 warning。

## 给初学编程学生的学习路径

1. 先看 `agent_py_agent/cli/memory_commands.py`，理解用户如何运行 `memory-route` 和 `memory-doctor`。
2. 再看 `agent_py_agent/agent/settings/memory.py`，学习配置如何设置默认值、warning 和安全回退。
3. 再看 `agent_py_agent/agent/memory_store/jsonl.py`，理解 JSONL 事实流水和 LocalStore 索引的区别。
4. 再看 `memory_routing/models.py`，认识 route、match、required/candidate path 和 read receipt 的数据形状。
5. 再看 `memory_routing/matcher.py`，理解关键词和别名如何命中规则。
6. 再看 `memory_archive/models.py` 和 `storage.py`，理解归档保存什么。
7. 最后看 `agent_py_agent/tests/test_memory_*.py`，用测试反推每一层必须保证的行为。

## 当前第一版索引 / 待补齐

本页先讲主结构和阅读路径。后续需要补真实 route index 样例、raw archive 样例、doctor 输出样例、LocalStore 命中样例和恢复链路图。
