# Codebase Tree

这份文档做两件事：
- 在目录树上直接给关键文件加一句“这是干嘛的”，方便你扫一眼就知道位置。
- 在后面的详细说明里把职责再展开，方便后续继续扩展工具、记忆和循环智能体能力。

## Tree

```text
simple-python-agent-v0.3/                      # 项目根目录，放代码、说明文档和验证记录
|-- agent_py_agent/                            # Python 包目录，核心代码主要都在这里
|   |-- __main__.py                            # CLI 入口，负责 run/chat/记忆命令
|   |-- README.md                              # 包级说明文档
|   |-- agent/                                 # 智能体核心模块目录
|   |   |-- __init__.py                        # 包初始化文件
|   |   |-- backend.py                         # 模型后端适配层，负责对接 echo / OpenAI 兼容 / Anthropic 兼容接口
|   |   |-- config.py                          # 配置结构和简化 YAML 加载器
|   |   |-- core.py                            # 智能体主调度器，把 prompt、记忆、后端和工具循环串起来
|   |   |-- memory.py                          # 本地 JSONL 记忆系统，负责写入和检索历史内容
|   |   |-- prompting.py                       # prompt 拼装器，负责把人格、记忆、工具信息和用户任务合成最终上下文
|   |   |-- subagent.py                        # 子任务记录模块，用来拆分任务并落盘
|   |   `-- tools.py                           # 工具注册、工具元数据、工具检索和工具执行入口
|   |-- config/                                # 配置目录
|   |   `-- agent_config.yaml                  # 运行配置文件，控制模型、记忆、工具和检索参数
|   |-- data/                                  # 运行时数据目录
|   |   |-- memory.jsonl                       # 长期记忆文件
|   |   `-- subagents/                         # 子任务记录输出目录
|   |-- extensions/                            # 预留扩展目录
|   |-- prompts/                               # prompt 规则文件目录
|   |   `-- default.md                         # 默认动态 prompt 规则
|   `-- tests/                                 # 本地测试目录
|       |-- run_tests.py                       # 一键冒烟测试入口
|       |-- test_agent.py                      # 核心 agent 行为测试
|       |-- test_backends.py                   # 后端适配测试
|       `-- test_tools.py                      # 工具目录、工具调用和工具能力测试
|-- .gitignore                                 # Git 忽略规则
|-- ACCEPTANCE.md                              # 验收记录
|-- CODEBASE_TREE.md                           # 当前这份目录树说明
|-- EVIDENCE.md                                # 过程证据记录
|-- RESULT.md                                  # 结果记录
|-- RUNLOG.md                                  # 运行日志说明
|-- SKILL_SPARK.yaml                           # 项目任务描述
|-- SPEC.md                                    # 原始需求规格
|-- STATUS.md                                  # 当前阶段状态说明
|-- TESTS.md                                   # 测试说明
|-- TEST_CHECKLIST.md                          # 测试检查清单
`-- validation/                                # 验证输出目录
```

## 关键文件说明

### `agent_py_agent/agent/tools.py`

这是本轮最重要的基础设施文件，负责四块能力：
- 注册有哪些工具可用。
- 保存每个工具的元数据。
  例如类别、关键词、适用场景、不适用场景、参数说明和示例。
- 生成两层工具提示。
  一层是常驻的工具目录，一层是按当前任务筛出来的少量候选详情。
- 执行工具调用。

这次额外预留了混合检索框架：
- 当前真正生效的是关键词检索。
- 向量检索接口已经留好，后续接 embedding 时不用重写核心流程。

### `agent_py_agent/agent/core.py`

这是智能体主循环，也就是“真正驱动程序跑起来”的地方。

它现在的工具流程是：
1. 先根据用户问题检索相关记忆。
2. 生成工具目录。
3. 再根据当前任务挑出最相关的几个工具详情。
4. 把这些内容一起拼进 prompt。
5. 如果模型发出 `[TOOL_CALL]`，就执行工具并把结果回填给模型继续推理。

简单说，`core.py` 负责把“会想”变成“会做”。

### `agent_py_agent/agent/prompting.py`

这个文件负责 prompt 的装配顺序。

当前结构已经从“全量工具手册”升级成：
- `# Tools`
  常驻工具目录，让模型始终知道手里有哪些工具。
- `# Recommended Tools`
  只放当前任务更相关的少量工具详情，减少误判。
- `# Tool Transcript`
  放工具调用记录和执行结果，让模型能在下一轮接着推理。

这样做的目的就是：
- 比全量注入更省 prompt
- 比只给工具名更不容易选错工具

### `agent_py_agent/agent/config.py`

这个文件定义项目的配置总表，并提供一个轻量 YAML 读取器。

本轮新增了与工具检索相关的配置项：
- `tool_catalog_limit`
  控制目录层最多展示多少工具。
- `tool_retrieval_limit`
  控制每次最多注入多少个高相关工具详情。
- `tool_vector_search_enabled`
  向量检索预留开关，当前默认关闭。

### `agent_py_agent/config/agent_config.yaml`

这是给人改的配置文件，不是给代码看的结构定义。

你后续调工具策略时，优先会改这里：
- 工具返回长度限制
- 工具详情注入数量
- 是否打开向量检索开关

### `agent_py_agent/tests/test_tools.py`

这个测试文件重点验证：
- 工具目录和候选工具详情是否真的出现在 prompt 里。
- 工具调用循环能不能正常执行。
- 写文件、追加文件、抓网页、测接口这些基础能力有没有回归。

## 跨平台兼容性

当前代码已经按“尽量兼容 Windows / Linux / macOS”收口，主要依据是：
- 路径统一使用 `pathlib.Path`
- 网络请求统一用标准库 `urllib`
- 工具系统没有依赖 PowerShell、cmd 或 macOS 专用命令
- 配置与数据文件统一按 UTF-8 读写

这意味着：
- 在三大平台上都能直接跑纯 Python 主链路
- 后续如果要加命令执行类工具，需要继续保持这一层跨平台约束
