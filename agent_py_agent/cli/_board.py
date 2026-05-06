
from __future__ import annotations

import json

from ..agent.subagent import filter_board_items
from .common import make_agent


def cmd_spawn(args) -> int:

    agent = make_agent(args)
    tasks = agent.spawn_subagents(args.goal, args.count)
    for task in tasks:
        print(json.dumps(task.__dict__, ensure_ascii=False))
    return 0


def cmd_subagents(args) -> int:

    agent = make_agent(args)
    board = agent.subagents.write_board(recent_limit=args.limit)
    items = filter_board_items(
        board.items if args.all else board.hot_list or board.recent,
        status=args.status or "",
        owner=args.owner or "",
        root_id=args.root_id or "",
    )
    print("SUBAGENT BOARD")
    print(f"total={board.summary.get('total', 0)} hot={len(board.hot_list)}")
    print("summary=" + json.dumps(board.summary, ensure_ascii=False, sort_keys=True))
    if not items:
        print("没有匹配的子代理记录。")
        return 0
    for item in items[: args.limit]:
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


def cmd_subagent_detail(args) -> int:

    agent = make_agent(args)
    task = agent.subagents.load(args.run_id)
    print(
        json.dumps(
            task.__dict__, ensure_ascii=False, indent=2, default=lambda value: value.__dict__
        )
    )
    return 0
