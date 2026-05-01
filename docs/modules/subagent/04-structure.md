# Subagent：结构树和详细说明

## 模块结构

```text
agent_py_agent/agent/
|-- subagent.py                         # 兼容入口
|-- subagents/                          # subagent 任务、manager、报告、runner、解析和渲染
|-- subagent_workflows/                 # workflow 模型、模板加载、路由、编译和验收规划
|   |-- builtin/                        # 内置 workflow 模板
|   |-- models.py                       # workflow / 任务 / 质量契约相关模型
|   |-- store.py                        # 模板加载和覆盖
|   |-- router.py                       # 根据目标选择 workflow
|   |-- compiler.py                     # 把 workflow 编译成 worker 派工规格
|   |-- planner.py                      # dry-run 规划门面
|   `-- acceptance.py                   # 父级验收计划
`-- agent_core/                         # 主循环、dispatch、runner prompt 等接入点
```

## 核心文件

- `agent_py_agent/agent/subagent_workflows/router.py`：回答“这个任务适合哪种 workflow”。
- `agent_py_agent/agent/subagent_workflows/compiler.py`：把抽象模板变成具体 worker 任务说明。
- `agent_py_agent/agent/subagent_workflows/acceptance.py`：生成父会话要检查什么。
- `agent_py_agent/agent/subagents/`：保存真实 subagent 管理、运行、报告和验收相关代码。
- `agent_py_agent/cli/subagents.py`：用户从 CLI 预览或操作 subagent 的入口。

## 数据流

1. 用户输入目标，例如“帮我做一个高质量文档交付”。
2. router 根据目标和配置选择 workflow。
3. store 加载内置或用户覆盖的模板。
4. compiler 生成 worker 派工规格，包含写入范围、证据要求和不能自验收的规则。
5. acceptance planner 生成父级验收清单。
6. runner 根据 execution context 调模型和工具，把 `RUNNER_RESULT.md`、`reports/runner_result.json`、`output.json` 写回任务目录。
7. `memory-resume` 在跨天恢复时用 archive/LocalStore 作为线索，最终推荐读取任务目录里的事实源，再由父级决定是否验收。
8. 目前 workflow dry-run CLI 可以展示计划；通用 workflow apply path 仍在推进中，LOG 专项 apply path 和 runner 恢复 scenario 已先行验证真实任务记录。

## 给初学编程学生的学习路径

1. 先看 `agent_py_agent/cli/subagents.py`，理解用户命令怎么进入程序。
2. 再看 `planner.py`，理解一个“规划结果”包含哪些部分。
3. 再看 `router.py`，学习如何把自然语言目标映射到模板。
4. 再看 `compiler.py`，学习模板怎样变成具体工作单。
5. 再看 `agent_py_agent/tests/test_scenario_gateway_resume.py::test_scenario_parent_subagent_cross_day_resume_uses_runner_task_facts`，理解 runner 写回后如何跨天恢复到任务事实源。
6. 最后看 `agent_py_agent/tests/test_subagent_workflow_*.py`，理解怎么证明路由、模板和编译没有坏。

## 当前第一版索引 / 待补齐

本页先解释主结构和学习路径。更细的类字段、状态机和通用 workflow apply 链路，需要等后续 apply path 落地后补齐。
