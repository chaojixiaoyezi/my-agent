
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from ..io import append_jsonl
from .home_layout import task_workspace_path


@dataclass(frozen=True)
class RunWorkspacePaths:
    root: Path
    work_dir: Path
    output_dir: Path
    runtime_dir: Path
    agents_dir: Path
    logs_dir: Path
    collab_dir: Path
    collab_blackboard_md: Path
    collab_messages_jsonl: Path
    collab_findings_jsonl: Path
    collab_evidence_packets_dir: Path
    artifacts_dir: Path
    artifact_manifest_json: Path
    compact_dir: Path
    summaries_dir: Path
    task_yaml: Path
    state_json: Path
    timeline_jsonl: Path


@dataclass(frozen=True)
class EnsureRunWorkspaceRequest:
    home: str | Path
    template: str
    task_name: str
    user_prompt: str
    request_id: str = ""
    run_id: str = ""
    task_id: str = ""
    owner_id: str = ""
    owner_home: str = ""
    source: str = "run"
    created_at: str | None = None


def ensure_run_workspace(request: EnsureRunWorkspaceRequest) -> RunWorkspacePaths:
    paths = run_workspace_paths(request)
    for directory in (
        paths.root,
        paths.work_dir,
        paths.output_dir,
        paths.runtime_dir,
        paths.agents_dir,
        paths.logs_dir,
        paths.collab_dir,
        paths.collab_evidence_packets_dir,
        paths.artifacts_dir,
        paths.compact_dir,
        paths.summaries_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    _write_task_yaml_if_missing(paths.task_yaml, request)
    _write_seed_file(paths.collab_blackboard_md, "# Blackboard\n\n")
    _write_seed_file(paths.collab_messages_jsonl, "")
    _write_seed_file(paths.collab_findings_jsonl, "")
    _write_seed_json(paths.artifact_manifest_json, _artifact_manifest_payload(request))
    paths.state_json.write_text(
        json.dumps(_state_payload(request), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    append_jsonl(paths.timeline_jsonl, _timeline_payload(request), sort_keys=True)
    return paths


def run_workspace_paths(request: EnsureRunWorkspaceRequest) -> RunWorkspacePaths:
    root = task_workspace_path(
        request.home,
        request.template,
        date=_date_key(request.created_at),
        task_name=_workspace_task_name(request),
    )
    work = root / "work"
    return RunWorkspacePaths(
        root=root,
        work_dir=work,
        output_dir=root / "output",
        runtime_dir=work / "runtime",
        agents_dir=work / "agents",
        logs_dir=work / "logs",
        collab_dir=work / "collab",
        collab_blackboard_md=work / "collab" / "blackboard.md",
        collab_messages_jsonl=work / "collab" / "messages.jsonl",
        collab_findings_jsonl=work / "collab" / "findings.jsonl",
        collab_evidence_packets_dir=work / "collab" / "evidence_packets",
        artifacts_dir=work / "refs" / "artifacts",
        artifact_manifest_json=work / "refs" / "artifacts" / "manifest.json",
        compact_dir=work / "compact",
        summaries_dir=work / "summaries",
        task_yaml=work / "task.yaml",
        state_json=work / "state.json",
        timeline_jsonl=work / "timeline.jsonl",
    )


def _write_task_yaml_if_missing(path: Path, request: EnsureRunWorkspaceRequest) -> None:
    if path.exists():
        return
    task_id = request.task_id or request.task_name or "task"
    text = (
        f'task_id: "{_yaml_escape(task_id)}"\n'
        f'request_id: "{_yaml_escape(request.request_id)}"\n'
        f'run_id: "{_yaml_escape(request.run_id)}"\n'
        f'owner_id: "{_yaml_escape(request.owner_id)}"\n'
        f'owner_home: "{_yaml_escape(request.owner_home)}"\n'
        f'source: "{_yaml_escape(request.source)}"\n'
    )
    path.write_text(text, encoding="utf-8")


def _state_payload(request: EnsureRunWorkspaceRequest) -> dict[str, object]:
    return {
        "version": 1,
        "request_id": request.request_id,
        "run_id": request.run_id,
        "task_id": request.task_id,
        "owner_id": request.owner_id,
        "owner_home": request.owner_home,
        "task_name": request.task_name,
        "source": request.source,
        "updated_at": _now_iso(),
        "reserved": {},
    }


def _artifact_manifest_payload(request: EnsureRunWorkspaceRequest) -> dict[str, object]:
    return {
        "version": 1,
        "request_id": request.request_id,
        "run_id": request.run_id,
        "task_id": request.task_id,
        "owner_id": request.owner_id,
        "owner_home": request.owner_home,
        "artifacts": [],
        "updated_at": _now_iso(),
    }


def _timeline_payload(request: EnsureRunWorkspaceRequest) -> dict[str, object]:
    return {
        "event_type": "run_workspace_saved",
        "request_id": request.request_id,
        "run_id": request.run_id,
        "task_id": request.task_id,
        "owner_id": request.owner_id,
        "owner_home": request.owner_home,
        "source": request.source,
        "created_at": _now_iso(),
    }


def _date_key(value: str | None) -> str:
    if not value:
        return date.today().isoformat()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return value[:10]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _workspace_task_name(request: EnsureRunWorkspaceRequest) -> str:
    candidates = (request.task_id, request.task_name, request.user_prompt)
    for value in candidates:
        text = str(value or "").strip()
        if text and not _looks_like_machine_id(text):
            return text
    return str(request.user_prompt or request.task_name or request.task_id or request.run_id or request.request_id or "task")


def _looks_like_machine_id(value: str) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    machine_prefixes = (
        "run-",
        "gw-",
        "req-",
        "session-",
        "thread-",
        "subagent-",
        "capreq-",
        "capreq_",
        "auto-compact",
    )
    if text.startswith(machine_prefixes):
        return True
    return bool(re.fullmatch(r"(run|gw|req|task|session|thread)[_-]?[0-9a-f]{6,}", text))


def _yaml_escape(value: object) -> str:
    return str(value or "").replace('"', '\\"')


def _write_seed_file(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def _write_seed_json(path: Path, payload: dict[str, object]) -> None:
    if path.exists():
        return
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


__all__ = ["EnsureRunWorkspaceRequest", "RunWorkspacePaths", "ensure_run_workspace", "run_workspace_paths"]
