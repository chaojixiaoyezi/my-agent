
from __future__ import annotations

import json
from typing import Any

from ..agent.memory_archive.query import strip_sort_keys


def print_memory_resume_from_compact(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(strip_sort_keys(payload), ensure_ascii=False, indent=2, sort_keys=True))
        return
    print("MY-AGENT MEMORY RESUME FROM COMPACT")
    print(f"workspace={payload['workspace_root']}")
    print(f"apply_id={payload['apply_id']}")
    print(f"plan_id={payload['plan_id'] or '-'}")
    print(f"consistency_status={payload['consistency_report']['status']}")
    print(f"action_guard={payload['action_guard']['status']}")
    _print_compact_handoff(payload.get("handoff", {}))
    _print_compact_continue_packet(payload.get("continue_packet", {}))
    _print_compact_completion_prompt(payload.get("completion_prompt", {}))
    print("Recommended Reads")
    for path in payload["recommended_read_paths"] or ["none"]:
        print(f"- {path}")
    print("Next Actions")
    for action in payload["next_actions"]:
        print(f"- {action}")


def _print_compact_handoff(handoff: dict[str, Any]) -> None:
    if not handoff:
        return
    print("Handoff")
    print(f"- goal: {handoff.get('goal') or 'unknown'}")
    print(f"- current_phase: {handoff.get('current_phase') or 'unknown'}")
    print(f"- next_step: {handoff.get('next_step') or 'unknown'}")
    print(f"- missing_fields: {json.dumps(handoff.get('missing_fields', []), ensure_ascii=False)}")
    _print_named_items("Acceptance", handoff.get("acceptance", {}).get("items", []))
    _print_named_items("Constraints", handoff.get("constraints", {}).get("items", []))
    _print_named_items("Latest Tests", handoff.get("latest_tests", {}).get("items", []))


def _print_compact_continue_packet(packet: dict[str, Any]) -> None:
    if not packet:
        return
    print("Continue Packet")
    print(
        f"- ready={packet.get('ready_to_continue')} mode={packet.get('continue_mode') or '-'} "
        f"tools={packet.get('automatic_tool_execution') or 'none'}"
    )
    print(f"- consistency_status={packet.get('consistency_status') or '-'}")


def _print_compact_completion_prompt(completion: dict[str, Any]) -> None:
    if completion.get("status") != "needs_user_input":
        return
    print("Completion Prompt")
    print("- missing_fields=" + json.dumps(completion.get("missing_fields", []), ensure_ascii=False))
    template = str(completion.get("prompt_template") or "")
    if template:
        print(template)
    commands = completion.get("suggested_commands", [])
    if commands:
        print("Suggested Commands")
        for command in commands:
            print(f"- {command}")


def _print_named_items(title: str, items: list[str]) -> None:
    print(title)
    for item in items or ["none"]:
        print(f"- {item}")


__all__ = ["print_memory_resume_from_compact"]
