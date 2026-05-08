# LLM: CLI surface for writing user-approved runtime fact sources used by compact resume.
# 模块用途: 提供 memory-fact-write 命令，把显式验收/约束/测试写入 runtime_facts。

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
from .common import make_agent


# LLM: cmd_memory_fact_write writes approved compact completion facts without guessing missing fields.
# 函数用途: CLI 入口，把用户显式传入的验收、约束和测试条目写入 runtime_facts/<fact_id>/task.json。
def cmd_memory_fact_write(args) -> int:
    agent = make_agent(args)
    compact_payload = _compact_payload_for_fact_write(agent.root, args)
    fact_id = _completion_fact_id(args, compact_payload)
    payload = _memory_fact_write_payload(agent.root, args, compact_payload, fact_id)
    if payload["ok"]:
        payload["fact_source_path"] = write_approved_runtime_fact_source(
            ApprovedRuntimeFactSourceRequest(
                root=agent.root,
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


# LLM: _from_compact_arg accepts only a real CLI --from-compact string.
# 函数用途: 读取可选 compact apply 引用，避免 argparse/mock 默认值被当成真实 apply id。
def _from_compact_arg(args) -> str:
    value = getattr(args, "from_compact", "")
    return value.strip() if isinstance(value, str) else ""


# LLM: _compact_payload_for_fact_write reads compact handoff as context only and keeps writing explicit.
# 函数用途: 可选读取 --from-compact 的目标/下一步/范围；不会因为读取 handoff 自动补事实。
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


# LLM: _completion_fact_id ties manual fact writes to compact scope so future apply can find them.
# 函数用途: 选择 runtime_facts 目录名；优先用户 fact_id，其次 compact scope 中的 request/session/task/run id。
def _completion_fact_id(args, compact_payload: dict[str, Any]) -> str:
    explicit = getattr(args, "fact_id", "") or ""
    if explicit:
        return explicit
    scope = _compact_scope(compact_payload)
    return next((str(scope.get(key) or "").strip() for key in ("request_id", "session_id", "task_id", "run_id") if scope.get(key)), "")


# LLM: _compact_scope extracts the compact work_state scope without nesting CLI logic.
# 函数用途: 从 compact resume payload 读取 scope 字典，异常形态按空 scope 处理。
def _compact_scope(compact_payload: dict[str, Any]) -> dict[str, Any]:
    work_state = compact_payload.get("work_state", {}) if compact_payload else {}
    scope = work_state.get("scope", {}) if isinstance(work_state, dict) else {}
    return scope if isinstance(scope, dict) else {}


# LLM: _memory_fact_write_payload validates explicit fact input before any file write occurs.
# 函数用途: 生成写入结果 payload；缺 fact_id 或三类事实都为空时返回 ok=false。
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


# LLM: _fact_write_missing keeps manual fact writes useful for compact auto guard.
# 函数用途: 要求 fact_id 和至少一条验收/约束/测试，避免写入空 task.json 污染事实源。
def _fact_write_missing(args, fact_id: str) -> list[str]:
    missing = [] if fact_id else ["fact_id"]
    if not any(_args_list(args, key) for key in ("acceptance", "constraint", "latest_test")):
        missing.append("facts")
    return missing


# LLM: _fact_goal prefers explicit CLI goal and falls back to compact handoff goal.
# 函数用途: 为 task.json 写入可读目标；目标为空不阻断，因为补字段事实仍可独立使用。
def _fact_goal(args, compact_payload: dict[str, Any]) -> str:
    explicit = getattr(args, "goal", "") or ""
    if explicit:
        return explicit
    return str(compact_payload.get("handoff", {}).get("goal", "") or "")


# LLM: _fact_next_actions merges explicit CLI next actions with compact handoff next_step.
# 函数用途: 保存恢复后下一步线索；用户显式 --next-action 优先。
def _fact_next_actions(args, compact_payload: dict[str, Any]) -> list[str]:
    explicit = _args_list(args, "next_action")
    if explicit:
        return explicit
    next_step = str(compact_payload.get("handoff", {}).get("next_step", "") or "")
    return [next_step] if next_step else []


# LLM: _fact_write_next_commands tells users how to make the new fact source visible to compact apply.
# 函数用途: 输出下一步命令提示，强调用同一个 request/session/task/run scope 重新 compact apply。
def _fact_write_next_commands(args, fact_id: str) -> list[str]:
    if not fact_id:
        return []
    command = f"my-agent memory-compact --apply --request-id {fact_id}"
    if getattr(args, "from_compact", ""):
        command += "  # or rerun the original compact scope if it used session/task/run filters"
    return [command, "my-agent memory-resume --from-compact <new_apply_id> --compact-resume-mode auto"]


# LLM: _args_list normalizes argparse append values into clean text items.
# 函数用途: 读取 --acceptance/--constraint/--latest-test 等重复参数，过滤空字符串。
def _args_list(args, name: str) -> list[str]:
    value = getattr(args, name, None)
    return [str(item).strip() for item in value or [] if str(item).strip()]


# LLM: _print_memory_fact_write renders manual fact write results without hiding validation failures.
# 函数用途: 输出 fact_id、写入路径和下一步命令；JSON 模式供测试和未来 UI 读取。
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
