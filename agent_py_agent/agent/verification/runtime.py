from __future__ import annotations

"""Connect passive verification evidence to the common tool-call seam.

LLM: this module consumes structured tool payload/result facts only.  Failures
to write advisory evidence must never change the underlying tool result.
模块用途: 在所有主代理和子代理共用的工具出口记录测试，并在文件修改后让旧测试过期。
"""

import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..action_protocol import ToolCallEnvelope
from ..agent_core.run_task_workspace_writer import current_run_task_workspace_root
from ..agent_core.runner.context import current_subagent_run_id, current_task_attributes
from ..agent_core.runtime.owner_roots import runtime_owner_root
from ..agent_core.runtime.task_identity import durable_task_id
from ..tooling.models import ToolExecutionResult
from ..tooling.write_boundary import WRITE_TOOL_NAMES, declared_write_paths
from .project_facts import classify_verification_command, project_facts_for
from .repository import VerificationContext, VerificationEvidence, VerificationEvidenceRepository


# LLM: this is the only runtime integration point for the evidence ledger.  Do
# not add per-IM or per-tool prompt patches elsewhere.
# 函数用途: 根据一次真实工具结果，记录测试证据或让旧证据过期。
def record_tool_verification(
    agent: object,
    payload: object,
    result: ToolExecutionResult,
) -> None:
    try:
        tool_payload, scope = _payload_and_scope(payload)
        repository = VerificationEvidenceRepository(runtime_owner_root(agent))
        context = _verification_context(agent, scope=scope)
        if result.tool == "run_command":
            _record_command(repository, context, agent=agent, payload=tool_payload, result=result)
        if result.ok and result.tool in WRITE_TOOL_NAMES:
            _mark_writes(repository, context, agent=agent, payload=tool_payload, result=result)
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError, sqlite3.Error):
        return


def _record_command(
    repository: VerificationEvidenceRepository,
    context: VerificationContext,
    *,
    agent: object,
    payload: dict[str, object],
    result: ToolExecutionResult,
) -> None:
    process = result.result_envelope.get("process")
    if not isinstance(process, dict) or process.get("status") != "exited":
        return
    command = str(payload.get("command") or "").strip()
    if not command:
        return
    try:
        exit_code = int(process.get("return_code"))
    except (TypeError, ValueError):
        return
    cwd = _command_cwd(agent, payload)
    classified = classify_verification_command(
        command,
        cwd=cwd,
        exit_code=exit_code,
        output=result.output,
    )
    if classified is None:
        return
    row = repository.record(
        context,
        VerificationEvidence(**asdict(classified)),
    )
    result.result_envelope["verification_evidence"] = _public_evidence(row)


def _mark_writes(
    repository: VerificationEvidenceRepository,
    context: VerificationContext,
    *,
    agent: object,
    payload: dict[str, object],
    result: ToolExecutionResult,
) -> None:
    paths = _structured_changed_paths(agent, payload, result)
    by_root: dict[str, list[str]] = {}
    for path in paths:
        facts = project_facts_for(Path(path).parent)
        if facts is not None:
            by_root.setdefault(str(facts.root), []).append(path)
    states: list[dict[str, Any]] = []
    for root, changed_paths in sorted(by_root.items()):
        repository.mark_edited(context, root=root, paths=changed_paths)
        states.append(repository.status(context, root=root))
    if states:
        result.result_envelope["verification_state"] = [
            {
                "status": state["status"],
                "root": state["root"],
                "changed_paths": state["changed_paths"],
            }
            for state in states
        ]


def _verification_context(
    agent: object,
    *,
    scope: dict[str, object],
) -> VerificationContext:
    current = getattr(agent, "_current_run_params", None)
    attrs = getattr(current, "task_attributes", None) if current is not None else None
    if not isinstance(attrs, dict):
        attrs = current_task_attributes(agent) or {}
    thread_id = str(attrs.get("conversation_thread_id") or "").strip()
    task_id = str(scope.get("root_task_id") or scope.get("task_id") or "").strip()
    task_id = task_id or (durable_task_id(current) if current is not None else "")
    task_id = task_id or current_subagent_run_id(agent) or str(getattr(agent, "_current_request_id", "") or "default")
    home_paths = getattr(agent, "home_paths", None)
    owner_ref = str(getattr(home_paths, "owner_id", "") or runtime_owner_root(agent).name or "local")
    return VerificationContext(owner_ref=owner_ref, thread_id=thread_id or "local", task_id=task_id)


def _payload_and_scope(payload: object) -> tuple[dict[str, object], dict[str, object]]:
    if isinstance(payload, ToolCallEnvelope):
        return dict(payload.input), payload.scope.to_dict()
    if isinstance(payload, dict):
        scope = payload.get("scope")
        return dict(payload), dict(scope) if isinstance(scope, dict) else {}
    return {}, {}


def _command_cwd(agent: object, payload: dict[str, object]) -> Path:
    raw = str(payload.get("working_dir") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve(strict=False)
    task_root = current_run_task_workspace_root(agent, getattr(agent, "_current_run_params", None))
    return task_root or Path(getattr(agent, "root", ".")).expanduser().resolve(strict=False)


def _structured_changed_paths(
    agent: object,
    payload: dict[str, object],
    result: ToolExecutionResult,
) -> list[str]:
    envelope = result.result_envelope if isinstance(result.result_envelope, dict) else {}
    raw_paths: list[object] = []
    raw_paths.extend(envelope.get(key) for key in ("path", "target_path", "output_path"))
    files_modified = envelope.get("files_modified")
    if isinstance(files_modified, list):
        raw_paths.extend(files_modified)
    if not any(str(path or "").strip() for path in raw_paths):
        raw_paths.extend(declared_write_paths(result.tool, payload))
    root = Path(getattr(agent, "root", ".")).expanduser().resolve(strict=False)
    task_root = current_run_task_workspace_root(agent, getattr(agent, "_current_run_params", None))
    resolved: list[str] = []
    for raw in raw_paths:
        text = str(raw or "").strip()
        if not text:
            continue
        path = Path(text).expanduser()
        if not path.is_absolute():
            base = task_root if task_root is not None and text.replace("\\", "/").startswith(("output/", "work/")) else root
            path = base / path
        normalized = str(path.resolve(strict=False))
        if normalized not in resolved:
            resolved.append(normalized)
    return resolved


def _public_evidence(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "status": row["status"],
        "kind": row["kind"],
        "scope": row["scope"],
        "canonical_command": row["canonical_command"],
        "exit_code": row["exit_code"],
        "root": row["root"],
        "created_at": row["created_at"],
    }


__all__ = ["record_tool_verification"]
