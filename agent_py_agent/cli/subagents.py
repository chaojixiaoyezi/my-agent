# LLM: CLI surface module; keep argparse/Typer wiring, stdout text, and service-call boundaries stable.
# 模块用途: 提供命令行入口或辅助函数，把用户命令转换成 agent 服务调用。

from __future__ import annotations

"""implements CLI commands for subagent boards, due-checks, actions, routing, reviews, dispatch, and runner entrypoints.

给人看的解释：
这个文件只管用户在命令行里怎么操作子代理。
实际状态机和文件写入仍然交给 SimpleAgent/SubAgentManager，这里主要做参数转发和结果打印。
父级验收计划入口也只导出只读 dry-run 命令，避免 CLI 聚合层隐式触发 tests 或状态写回。
"""

from agent_py_agent.cli._actions import (
    cmd_subagents_apply_actions,
    cmd_subagents_plan_actions,
    cmd_subagents_route_capabilities,
)
from agent_py_agent.cli._board import cmd_spawn, cmd_subagent_detail, cmd_subagents
from agent_py_agent.cli._dispatch import (
    cmd_subagent_run,
    cmd_subagents_dispatch,
    cmd_subagents_workflow_plan,
)

# LLM: hierarchy commands expose explicit child scheduling and refs-only recovery tree queries.
from agent_py_agent.cli._hierarchy import cmd_subagents_hierarchy, cmd_subagents_recovery_tree
from agent_py_agent.cli._inspection import (
    cmd_subagent_context,
    cmd_subagents_due_check,
    cmd_subagents_probe,
)

# LLM: leadership recovery command is a refs-only dry-run planner, not a mutating handoff apply path.
from agent_py_agent.cli._leadership import cmd_subagents_leadership_recovery_plan
from agent_py_agent.cli._memory_gate import cmd_subagents_memory_gate

# 函数用途: 汇总 review 类子命令入口；包含只读的父级验收计划 dry-run 查看命令。
# 函数用途: 汇总 review 类子命令入口，供 argparse 注册层统一导入。
from agent_py_agent.cli._review import (
    cmd_subagents_acceptance,
    cmd_subagents_acceptance_plan,
    cmd_subagents_patches,
    cmd_subagents_tests,
)
