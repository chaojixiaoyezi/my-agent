# LLM: CLI surface module; keep argparse/Typer wiring, stdout text, and service-call boundaries stable.
# 模块用途: 提供命令行入口或辅助函数，把用户命令转换成 agent 服务调用。


from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass

from ..agent.agent_core.subagent_params import SpawnSubagentsParams
from ..agent.subagent import filter_board_items
from ..agent.subagents.models import SubAgentBoardOptions
from .common import make_agent
from .shared_progress import (
    format_acceptance_next_action_lines,
    format_acceptance_plan_lines,
    format_shared_progress_lines,
    format_takeover_view_lines,
    shared_progress_for_board,
)


# LLM: _print_takeover_view keeps board takeover guidance visible and refs-only.
# 函数用途: 在 subagents 看板里展示可接管 run 的 packet/read_order，不读取 artifact 正文。
def _print_takeover_view(panels: list[dict]) -> None:
    print("Takeover View")
    for line in format_takeover_view_lines(panels):
        print(line)


# LLM: _print_acceptance_plan keeps parent validation guidance visible but non-mutating.
# 函数用途: 在 subagents 看板中展示父级验收 dry-run 决策；不执行 tests、不写 task。
def _print_acceptance_plan(panels: list[dict]) -> None:
    print("Acceptance Plan")
    for line in format_acceptance_plan_lines(panels):
        print(line)


# LLM: _print_acceptance_next_action keeps advisory commands visible but never executes them.
# 函数用途: 在 subagents 看板中展示父级验收下一动作建议；只打印命令和 refs，不写状态。
def _print_acceptance_next_action(panels: list[dict]) -> None:
    print("Acceptance Next Action")
    for line in format_acceptance_next_action_lines(panels):
        print(line)


# LLM: _task_jsonable 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _task_jsonable(task):
    if is_dataclass(task):
        return asdict(task)
    return getattr(task, "__dict__", {"value": str(task)})


# LLM: cmd_spawn 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
def cmd_spawn(args) -> int:

    agent = make_agent(args)
    tasks = agent.spawn_subagents(
        params=SpawnSubagentsParams(
            goal=args.goal,
            count=_subagent_config_int(agent, args, "count", "subagent_spawn_default_count"),
            role=getattr(args, "role", "worker"),
            agent_name=getattr(args, "agent_name", ""),
        )
    )
    for task in tasks:
        print(json.dumps(_task_jsonable(task), ensure_ascii=False))
    return 0


# LLM: cmd_subagents 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
def cmd_subagents(args) -> int:

    agent = make_agent(args)
    board = agent.subagents.write_board(
        options=SubAgentBoardOptions(recent_limit=_subagent_config_int(agent, args, "limit", "subagent_cli_default_limit")),
    )
    items = filter_board_items(
        board.items if args.all else board.hot_list or board.recent,
        status=args.status or "",
        owner=args.owner or "",
        root_id=args.root_id or "",
    )
    print("SUBAGENT BOARD")
    print(f"total={board.summary.get('total', 0)} hot={len(board.hot_list)}")
    print("summary=" + json.dumps(board.summary, ensure_ascii=False, sort_keys=True))
    panels = shared_progress_for_board(agent, board, purpose="subagents_board")
    _print_shared_progress(panels)
    _print_takeover_view(panels)
    _print_acceptance_plan(panels)
    _print_acceptance_next_action(panels)
    if not items:
        print("没有匹配的子代理记录。")
        return 0
    limit = _subagent_config_int(agent, args, "limit", "subagent_cli_default_limit")
    for item in items[:limit]:
        flags = ",".join(item.risk_flags) if item.risk_flags else "ok"
        print(
            f"- {item.id} status={item.status} verify={item.verification_status} "
            f"channel={item.channel_status} depth={item.depth} "
            f"owner={item.owner or 'none'} final={item.final_owner or 'none'} "
            f"evidence={item.evidence_count} requests={item.open_request_count} "
            f"gaps={item.open_gap_count} flags={flags} :: {item.goal}"
        )
    print(f"\n已写入: {agent.subagents.workspace / 'subagent_board.json'}")
    print(f"已写入: {agent.subagents.workspace / 'SUBAGENT_BOARD.md'}")
    return 0


# LLM: _print_shared_progress keeps board output refs-only and compact.
# 函数用途: 在子代理看板里展示共享进度面板摘要和 failure handoff 计数。
def _print_shared_progress(panels: list[dict]) -> None:
    print("Shared Progress")
    for line in format_shared_progress_lines(panels):
        print(line)


# LLM: _subagent_config_int resolves optional subagent CLI defaults from AgentConfig.
# 函数用途: 子代理 CLI 没有显式传数量/条数时，统一读取 agent_config.yaml。
def _subagent_config_int(agent, args, arg_name: str, config_name: str) -> int:
    value = getattr(args, arg_name, None)
    if value is not None:
        return int(value)
    return int(getattr(agent.config, config_name, 0) or 0)


# LLM: cmd_subagent_detail 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
def cmd_subagent_detail(args) -> int:

    agent = make_agent(args)
    task = agent.subagents.load(args.run_id)
    print(
        json.dumps(
            _task_jsonable(task), ensure_ascii=False, indent=2,
        )
    )
    return 0
