
from __future__ import annotations

import json

from ..agent.conversation.store_guidance import normalize_guidance_target_type
from .common import make_agent


def cmd_guidance_send(args) -> int:
    agent = make_agent(args)
    target_type, target_id = _target_from_args(args)
    message = str(getattr(args, "message", "") or "").strip()
    if not target_type or not target_id:
        print("缺少目标：请传 --run-id / --thread-id / --task-id / --case-id，或 --target-type + --target-id。")
        return 2
    if not message:
        print("缺少提示内容。")
        return 2
    entry = agent.conversation_store.append_guidance(
        {
            "target_type": target_type,
            "target_id": target_id,
            "message": message,
            "sender": str(getattr(args, "sender", "") or "cli_user"),
            "priority": str(getattr(args, "priority", "") or "normal"),
            "delivery": str(getattr(args, "delivery", "") or "next_turn"),
        }
    )
    payload = {
        "ok": True,
        "guidance_id": entry.guidance_id,
        "target": {"type": entry.target_type, "id": entry.target_id},
        "delivery": entry.delivery,
        "message": entry.message,
    }
    if bool(getattr(args, "json", False)):
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"已追加提示 {entry.guidance_id} -> {entry.target_type}:{entry.target_id}")
    return 0


def _target_from_args(args) -> tuple[str, str]:
    direct_flags = (
        ("run_id", "agent_run"),
        ("thread_id", "thread"),
        ("task_id", "task"),
        ("case_id", "case"),
    )
    for attr, target_type in direct_flags:
        value = str(getattr(args, attr, "") or "").strip()
        if value:
            return target_type, value
    return (
        normalize_guidance_target_type(getattr(args, "target_type", "") or ""),
        str(getattr(args, "target_id", "") or "").strip(),
    )


__all__ = ["cmd_guidance_send"]
