# LLM: Compact resume CLI rendering stays separate from archive search/resume command orchestration.
# 模块用途: 输出 memory-resume --from-compact 的人类视图和 JSON，避免 CLI 命令文件继续膨胀。

from __future__ import annotations

import json
from typing import Any

from ..agent.memory_archive.query import strip_sort_keys


# LLM: print_memory_resume_from_compact renders compact-specific resume output without hiding guard status.
# 函数用途: 输出 compact resume 的 apply id、状态、handoff、continue packet、补全提示和推荐路径。
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


# LLM: _print_compact_handoff shows the resume package fields that matter before continuing work.
# 函数用途: 输出 compact resume 的目标、阶段、验收、约束、测试和 action guard 摘要。
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


# LLM: _print_compact_continue_packet shows the stable go/no-go packet without expanding artifact bodies.
# 函数用途: 输出继续工作包的模式、是否可继续和工具执行边界，方便用户判断恢复后能否继续。
def _print_compact_continue_packet(packet: dict[str, Any]) -> None:
    if not packet:
        return
    print("Continue Packet")
    print(
        f"- ready={packet.get('ready_to_continue')} mode={packet.get('continue_mode') or '-'} "
        f"tools={packet.get('automatic_tool_execution') or 'none'}"
    )
    print(f"- consistency_status={packet.get('consistency_status') or '-'}")


# LLM: _print_compact_completion_prompt makes blocked resume actionable without writing facts automatically.
# 函数用途: 当 compact resume 缺工作状态字段时，输出可复制补全模板、建议命令和缺失字段列表。
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


# LLM: _print_named_items keeps compact handoff subsections compact and stable for CLI users.
# 函数用途: 输出一个命名列表，空列表明确显示 none。
def _print_named_items(title: str, items: list[str]) -> None:
    print(title)
    for item in items or ["none"]:
        print(f"- {item}")


__all__ = ["print_memory_resume_from_compact"]
