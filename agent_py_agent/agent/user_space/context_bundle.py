
from __future__ import annotations

import json
import time as time_module
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .context_bundle_contracts import (
    finalize_context_bundle_contracts,
    main_context_contract_sections,
)
from .context_bundle_rendering import render_markdown_bundle, render_prompt_section
from .home_layout import safe_task_slug


@dataclass(frozen=True)
class MainContextBundleRequest:
    root: str | Path
    home_paths: Any | None
    user_prompt: str
    request_id: str = ""
    run_id: str = ""
    task_id: str = ""
    source: str = "run"
    context_scope: str = "default"
    save: bool = True
    memory_count: int = 0
    runtime_injection_count: int = 0
    routed_required_read_paths: tuple[str, ...] = ()
    routed_candidate_paths: tuple[str, ...] = ()
    resume_context_injected: bool = False
    task_attributes: dict | None = None
    workspace_roots: tuple[str, ...] = ()
    write_boundary: dict | None = None
    tool_runtime_snapshot: object | None = None
    tool_runtime_errors: tuple[dict[str, object], ...] = ()
    artifact_refs: tuple[str, ...] = ()
    owner_type: str = "main_agent"
    owner_id: str = "root"
    root_run_id: str = ""
    parent_run_id: str = ""
    created_at: str | None = None


@dataclass(frozen=True)
class MainContextBundleResult:
    prompt_section: str
    json_path: str = ""
    markdown_path: str = ""
    bundle: dict[str, object] | None = None


def build_main_context_bundle(request: MainContextBundleRequest) -> MainContextBundleResult:
    bundle = _bundle_payload(request)
    prompt_section = render_prompt_section(bundle, json_path="")
    bundle = finalize_context_bundle_contracts(bundle, prompt_section)
    prompt_section = render_prompt_section(bundle, json_path="")
    bundle = finalize_context_bundle_contracts(bundle, prompt_section)
    if not request.save or request.home_paths is None:
        return MainContextBundleResult(prompt_section=prompt_section, bundle=bundle)
    json_path, markdown_path = _write_bundle_files(request, bundle)
    prompt_section = render_prompt_section(bundle, json_path=str(json_path))
    bundle = finalize_context_bundle_contracts(bundle, prompt_section)
    _rewrite_bundle_files(json_path, markdown_path, bundle)
    prompt_section = render_prompt_section(bundle, json_path=str(json_path))
    return MainContextBundleResult(
        prompt_section=prompt_section,
        json_path=str(json_path),
        markdown_path=str(markdown_path),
        bundle=bundle,
    )


def latest_main_context_bundle_path(home_paths: Any | None) -> str:
    if home_paths is None:
        return ""
    base = _memory_archive_root(home_paths) / "snapshots" / "context_bundles"
    if not base.exists():
        return ""
    candidates = _latest_bundle_candidates(base)
    return str(candidates[0]) if candidates else ""


def _bundle_payload(request: MainContextBundleRequest) -> dict[str, object]:
    home_paths = request.home_paths
    created_at = request.created_at or _now_iso()
    payload = {
        "schema": "main_context_bundle.v1",
        "version": 1,
        "created_at": created_at,
        "identity": _identity_payload(request),
        "scope": _scope_payload(request),
        "workspace_refs": _workspace_refs(request, home_paths),
        "task": _task_payload(request),
        "memory_refs": _memory_refs(request, home_paths),
        "recovery_refs": _recovery_refs(request, home_paths),
        "tooling": _tooling_payload(request),
        "next_action": {
            "expected": "build_prompt_and_run_tool_loop",
            "explanation": "Use refs first, read bodies only when needed.",
        },
    }
    payload.update(main_context_contract_sections(request, home_paths))
    return payload


def _identity_payload(request: MainContextBundleRequest) -> dict[str, object]:
    return {
        "owner_type": request.owner_type or "main_agent",
        "owner_id": request.owner_id or "root",
        "root_run_id": request.root_run_id or request.run_id,
        "parent_run_id": request.parent_run_id,
        "source": request.source or "run",
    }


def _scope_payload(request: MainContextBundleRequest) -> dict[str, object]:
    return {
        "request_id": request.request_id,
        "run_id": request.run_id,
        "task_id": request.task_id,
        "context_scope": request.context_scope or "default",
    }


def _workspace_refs(request: MainContextBundleRequest, home_paths: Any | None) -> dict[str, object]:
    root = str(Path(request.root).resolve())
    if home_paths is None:
        return {"primary_workspace_root": root}
    return {
        "primary_workspace_root": root,
        "my_agent_home": str(Path(home_paths.root).resolve()),
        "owner_id": str(getattr(home_paths, "owner_id", "") or ""),
        "owner_home": _resolved_home_attr(home_paths, "owner_home_dir"),
        "owner_tasks_root": _resolved_home_attr(home_paths, "owner_tasks_dir"),
        "owner_memory_root": _resolved_home_attr(home_paths, "owner_memory_dir"),
        "owner_artifacts_root": _resolved_home_attr(home_paths, "owner_artifacts_dir"),
    }


def _task_payload(request: MainContextBundleRequest) -> dict[str, object]:
    return {
        "user_prompt_preview": _preview(request.user_prompt, max_chars=240),
        "attributes": dict(request.task_attributes or {}),
    }


def _memory_refs(request: MainContextBundleRequest, home_paths: Any | None) -> dict[str, object]:
    payload: dict[str, object] = {
        "related_memory_count": int(request.memory_count),
        "routed_required_read_paths": list(request.routed_required_read_paths),
        "routed_candidate_paths": list(request.routed_candidate_paths),
    }
    if home_paths is not None:
        payload.update(
            {
                "owner_daily_memory_root": _resolved_home_attr(home_paths, "owner_memory_daily_dir"),
                "owner_key_memory_path": _resolved_home_attr(home_paths, "owner_memory_md"),
                "owner_lessons_root": _resolved_home_attr(home_paths, "owner_memory_lessons_dir"),
                "owner_indexes_root": _resolved_home_attr(home_paths, "owner_memory_indexes_dir"),
            }
        )
    return payload


def _resolved_home_attr(home_paths: Any, attr: str) -> str:
    raw = getattr(home_paths, attr, None)
    return str(Path(raw).resolve()) if raw else ""


def _recovery_refs(request: MainContextBundleRequest, home_paths: Any | None) -> dict[str, object]:
    payload: dict[str, object] = {
        "resume_context_injected": bool(request.resume_context_injected),
    }
    if home_paths is not None:
        archive = _memory_archive_root(home_paths)
        payload.update(
            {
                "compact_applies_root": str((archive / "compact_applies").resolve()),
                "snapshots_root": str((archive / "snapshots").resolve()),
                "tokens_root": str((archive / "tokens").resolve()),
            }
        )
    return payload


def _tooling_payload(request: MainContextBundleRequest) -> dict[str, object]:
    return {
        "runtime_injection_count_before_bundle": int(request.runtime_injection_count),
        "tool_gateway": "default_tool_registry",
    }


def _write_bundle_files(
    request: MainContextBundleRequest,
    bundle: dict[str, object],
) -> tuple[Path, Path]:
    home_paths = request.home_paths
    base = _memory_archive_root(home_paths) / "snapshots" / "context_bundles" / date.today().isoformat()
    base.mkdir(parents=True, exist_ok=True)
    stem = _bundle_stem(request)
    json_path = base / f"{stem}.json"
    markdown_path = base / f"{stem}.md"
    json_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(render_markdown_bundle(bundle, json_path=str(json_path)), encoding="utf-8")
    _write_latest_copies(base, json_path, markdown_path)
    return json_path, markdown_path


def _write_latest_copies(base: Path, json_path: Path, markdown_path: Path) -> None:
    (base / "latest_context_bundle.json").write_text(json_path.read_text(encoding="utf-8"), encoding="utf-8")
    (base / "latest_context_bundle.md").write_text(markdown_path.read_text(encoding="utf-8"), encoding="utf-8")


def _rewrite_bundle_files(json_path: Path, markdown_path: Path, bundle: dict[str, object]) -> None:
    json_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(render_markdown_bundle(bundle, json_path=str(json_path)), encoding="utf-8")
    _write_latest_copies(json_path.parent, json_path, markdown_path)


def _memory_archive_root(home_paths: Any) -> Path:
    owner_home = getattr(home_paths, "owner_home_dir", None)
    if owner_home:
        return Path(owner_home) / "memory_archive"
    return Path(home_paths.memory_archive_dir)


def _latest_bundle_candidates(base: Path) -> list[Path]:
    latest = [path for path in base.glob("*/latest_context_bundle.json") if path.is_file()]
    regular = [
        path
        for path in base.glob("*/*.json")
        if path.is_file() and not path.name.startswith("latest_context_bundle")
    ]
    return sorted([*latest, *regular], key=lambda item: (_safe_mtime(item), str(item)), reverse=True)


def _bundle_stem(request: MainContextBundleRequest) -> str:
    raw = request.request_id or request.run_id or request.task_id or f"context-{time_module.time_ns()}"
    return safe_task_slug(raw, max_chars=120)


def _preview(value: object, *, max_chars: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars].rstrip()}..."


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


__all__ = [
    "MainContextBundleRequest",
    "MainContextBundleResult",
    "build_main_context_bundle",
    "latest_main_context_bundle_path",
]
