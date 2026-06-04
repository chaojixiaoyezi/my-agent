
from __future__ import annotations

"""CLI entrypoint for approved compact completion facts."""

import json
from typing import Any

from ..agent.memory_archive.compact_resume import (
    MemoryCompactResumeOptions,
    build_memory_compact_resume,
)
from ..agent.memory_archive.query import strip_sort_keys
from ..agent.memory_archive.runtime_fact_source import (
    ApprovedRuntimeFactSourceRequest,
    write_approved_runtime_fact_source,
)
from ..agent.agent_core.runtime.owner_roots import runtime_owner_root
from .common import make_agent


def cmd_memory_fact_write(args) -> int:
    agent = make_agent(args)
    root = runtime_owner_root(agent)
    compact_payload = _compact_payload_for_fact_write(root, args)
    fact_id = _completion_fact_id(args, compact_payload)
    payload = _memory_fact_write_payload(root, args, compact_payload, fact_id)
    if payload["ok"]:
        payload["fact_source_path"] = write_approved_runtime_fact_source(
            ApprovedRuntimeFactSourceRequest(
                root=root,
                fact_id=fact_id,
                goal=payload["goal"],
                next_actions=payload["next_actions"],
                acceptance=_args_list(args, "acceptance"),
                constraints=_args_list(args, "constraint"),
                latest_tests=_args_list(args, "latest_test"),
                source_apply_id=payload["source_apply_id"],
            )
        )
    _print_memory_fact_write(payload, json_output=getattr(args, "json", False))
    return 0 if payload["ok"] else 2


def _from_compact_arg(args) -> str:
    value = getattr(args, "from_compact", "")
    return value.strip() if isinstance(value, str) else ""


def _compact_payload_for_fact_write(root, args) -> dict[str, Any]:
    apply_ref = _from_compact_arg(args)
    if not apply_ref:
        return {}
    return build_memory_compact_resume(
        root,
        MemoryCompactResumeOptions(
            apply_ref=apply_ref,
            owner_type=getattr(args, "compact_owner_type", "main_agent") or "main_agent",
            owner_id=getattr(args, "compact_owner_id", "") or "",
            resume_mode="manual",
        ),
    )


def _completion_fact_id(args, compact_payload: dict[str, Any]) -> str:
    explicit = getattr(args, "fact_id", "") or ""
    if explicit:
        return explicit
    scope = _compact_scope(compact_payload)
    return next((str(scope.get(key) or "").strip() for key in ("request_id", "session_id", "task_id", "run_id") if scope.get(key)), "")


def _compact_scope(compact_payload: dict[str, Any]) -> dict[str, Any]:
    work_state = compact_payload.get("work_state", {}) if compact_payload else {}
    scope = work_state.get("scope", {}) if isinstance(work_state, dict) else {}
    return scope if isinstance(scope, dict) else {}


def _memory_fact_write_payload(root, args, compact_payload: dict[str, Any], fact_id: str) -> dict[str, Any]:
    missing = _fact_write_missing(args, fact_id)
    return {
        "ok": not missing,
        "workspace_root": str(root),
        "fact_id": fact_id,
        "source_apply_id": str(compact_payload.get("apply_id", "") or _from_compact_arg(args)),
        "goal": _fact_goal(args, compact_payload),
        "next_actions": _fact_next_actions(args, compact_payload),
        "acceptance": _args_list(args, "acceptance"),
        "constraints": _args_list(args, "constraint"),
        "latest_tests": _args_list(args, "latest_test"),
        "missing_required": missing,
        "fact_source_path": "",
        "next_commands": _fact_write_next_commands(args, fact_id),
    }


def _fact_write_missing(args, fact_id: str) -> list[str]:
    missing = [] if fact_id else ["fact_id"]
    if not any(_args_list(args, key) for key in ("acceptance", "constraint", "latest_test")):
        missing.append("facts")
    return missing


def _fact_goal(args, compact_payload: dict[str, Any]) -> str:
    explicit = getattr(args, "goal", "") or ""
    if explicit:
        return explicit
    return str(compact_payload.get("handoff", {}).get("goal", "") or "")


def _fact_next_actions(args, compact_payload: dict[str, Any]) -> list[str]:
    explicit = _args_list(args, "next_action")
    if explicit:
        return explicit
    next_step = str(compact_payload.get("handoff", {}).get("next_step", "") or "")
    return [next_step] if next_step else []


def _fact_write_next_commands(args, fact_id: str) -> list[str]:
    if not fact_id:
        return []
    command = f"my-agent memory-compact --apply --request-id {fact_id}"
    if getattr(args, "from_compact", ""):
        command += "  # or rerun the original compact scope if it used session/task/run filters"
    return [command, "my-agent memory-resume --from-compact <new_apply_id> --compact-resume-mode auto"]


def _args_list(args, name: str) -> list[str]:
    value = getattr(args, name, None)
    return [str(item).strip() for item in value or [] if str(item).strip()]


def _print_memory_fact_write(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(strip_sort_keys(payload), ensure_ascii=False, indent=2, sort_keys=True))
        return
    print("MY-AGENT MEMORY FACT WRITE")
    print(f"workspace={payload['workspace_root']}")
    print(f"ok={payload['ok']} fact_id={payload['fact_id'] or '-'}")
    if payload["missing_required"]:
        print("missing_required=" + json.dumps(payload["missing_required"], ensure_ascii=False))
        return
    print(f"fact_source={payload['fact_source_path']}")
    print("Next Commands")
    for command in payload["next_commands"]:
        print(f"- {command}")


__all__ = ["cmd_memory_fact_write"]
