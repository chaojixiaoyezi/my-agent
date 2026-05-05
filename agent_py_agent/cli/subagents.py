from __future__ import annotations

"""LLM: implements CLI commands for subagent boards, due-checks, actions, routing, reviews, dispatch, and runner entrypoints.

给人看的解释：
这个文件只管用户在命令行里怎么操作子代理。
实际状态机和文件写入仍然交给 SimpleAgent/SubAgentManager，这里主要做参数转发和结果打印。
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
from agent_py_agent.cli._inspection import (
    cmd_subagent_context,
    cmd_subagents_due_check,
    cmd_subagents_probe,
)
from agent_py_agent.cli._review import cmd_subagents_acceptance, cmd_subagents_patches
