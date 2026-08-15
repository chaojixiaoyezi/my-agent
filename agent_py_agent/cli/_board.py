

from __future__ import annotations

import json
import time
from dataclasses import asdict, is_dataclass

from ..agent.agent_core.subagent import SpawnSubagentsParams
from ..agent.common.display_width import display_width, truncate_display
from ..agent.startup_recovery import is_recent_board_item
from ..agent.subagents.models import SubAgentBoardOptions
from .common import make_agent
from .shared_progress import (
    format_shared_progress_lines,
    format_takeover_view_lines,
    shared_progress_for_board,
)

_BOARD_GOAL_PREVIEW_CHARS = 180


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
    limit = _subagent_config_int(agent, args, "limit", "subagent_cli_default_limit")
    board = agent.subagents.board.write_board(
        options=SubAgentBoardOptions(
            recent_limit=limit,
            status=args.status or "",
            owner=args.owner or "",
            root_id=args.root_id or "",
        ),
    )
    current_hot = _current_hot_items(board.hot_list)
    items = _visible_board_items(args, board, current_hot)
    print("SUBAGENT BOARD")
    historical_hot = max(0, len(board.hot_list) - len(current_hot))
    print(f"total={board.summary.get('total', 0)} hot={len(current_hot)} historical_hot={historical_hot}")
    print("current_summary=" + json.dumps(_visible_summary([*current_hot, *board.recent[:limit]]), ensure_ascii=False, sort_keys=True))
    print("history_summary=" + json.dumps(board.summary, ensure_ascii=False, sort_keys=True))
    panels = shared_progress_for_board(agent, board, purpose="subagents_board")
    _print_shared_progress(panels)
    _print_takeover_view(panels)
    if not items:
        print("没有匹配的子代理记录。")
        return 0
    for item in items[:limit]:
        flags = ",".join(item.risk_flags) if item.risk_flags else "ok"
        print(
            f"- {item.id} status={item.status} verify={item.verification_status} "
            f"channel={item.channel_status} depth={item.depth} "
            f"owner={item.owner or 'none'} final={item.final_owner or 'none'} "
            f"evidence={item.evidence_count} requests={item.open_request_count} "
            f"gaps={item.open_gap_count} flags={flags} :: {_goal_preview(item.goal)}"
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


def _current_hot_items(items: list) -> list:
    now = time.time()
    return [item for item in items if is_recent_board_item(item, now=now)]


def _visible_board_items(args, board, current_hot: list) -> list:
    if getattr(args, "all", False) or getattr(args, "status", None) or getattr(args, "owner", None) or getattr(args, "root_id", None):
        return board.items if getattr(args, "all", False) else board.hot_list or board.recent
    return current_hot or board.recent


def _visible_summary(items: list) -> dict:
    by_status: dict[str, int] = {}
    for item in items:
        status = str(getattr(item, "status", "") or "UNKNOWN")
        by_status[status] = by_status.get(status, 0) + 1
    return {"visible_total": len(items), "by_status": by_status}


def _goal_preview(value: object) -> str:
    text = str(value or "").replace("\n", " ").strip()
    # 预算按显示列宽 + 字形簇边界截断(审计 #22):CJK 预览不再忽长忽短,emoji/组合字形不被切碎
    if display_width(text) <= _BOARD_GOAL_PREVIEW_CHARS:
        return text
    return truncate_display(text, _BOARD_GOAL_PREVIEW_CHARS).rstrip() + "..."


def cmd_subagent_detail(args) -> int:

    agent = make_agent(args)
    task = agent.subagents.load(args.run_id)
    print(
        json.dumps(
            _task_jsonable(task), ensure_ascii=False, indent=2,
        )
    )
    return 0
