

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass

from ..agent.agent_core.subagent import SpawnSubagentsParams
from ..agent.subagents.models import SubAgentBoardOptions
from .common import make_agent
from .shared_progress import (
    format_shared_progress_lines,
    format_takeover_view_lines,
    shared_progress_for_board,
)


def _print_takeover_view(panels: list[dict]) -> None:
    print("Takeover View")
    for line in format_takeover_view_lines(panels):
        print(line)


def _task_jsonable(task):
    if is_dataclass(task):
        return asdict(task)
    return getattr(task, "__dict__", {"value": str(task)})


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


def cmd_subagents(args) -> int:

    agent = make_agent(args)
    board = agent.subagents.board.write_board(
        options=SubAgentBoardOptions(
            recent_limit=_subagent_config_int(agent, args, "limit", "subagent_cli_default_limit"),
            status=args.status or "",
            owner=args.owner or "",
            root_id=args.root_id or "",
        ),
    )
    items = board.items if args.all else board.hot_list or board.recent
    print("SUBAGENT BOARD")
    print(f"total={board.summary.get('total', 0)} hot={len(board.hot_list)}")
    print("summary=" + json.dumps(board.summary, ensure_ascii=False, sort_keys=True))
    panels = shared_progress_for_board(agent, board, purpose="subagents_board")
    _print_shared_progress(panels)
    _print_takeover_view(panels)
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


def _print_shared_progress(panels: list[dict]) -> None:
    print("Shared Progress")
    for line in format_shared_progress_lines(panels):
        print(line)


def _subagent_config_int(agent, args, arg_name: str, config_name: str) -> int:
    value = getattr(args, arg_name, None)
    if value is not None:
        return int(value)
    return int(getattr(agent.config, config_name, 0) or 0)


def cmd_subagent_detail(args) -> int:

    agent = make_agent(args)
    task = agent.subagents.load(args.run_id)
    print(
        json.dumps(
            _task_jsonable(task), ensure_ascii=False, indent=2,
        )
    )
    return 0
