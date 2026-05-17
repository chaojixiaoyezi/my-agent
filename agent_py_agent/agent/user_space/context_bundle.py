# LLM: Main-agent context bundles give every root run a structured refs-only handoff.
# 模块用途: 为主代理每轮运行生成 context bundle v1，避免模型从自然语言里猜路径、任务和恢复入口。

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


# LLM: MainContextBundleRequest is the only input bundle for root context assembly.
# 类用途: 打包生成主代理 context bundle 所需的任务、路径、记忆和保存边界字段。
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
    allowed_tools: tuple[str, ...] = ()
    granted_capabilities: tuple[str, ...] = ()
    tool_specs: tuple[object, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    owner_type: str = "main_agent"
    owner_id: str = "root"
    root_run_id: str = ""
    parent_run_id: str = ""
    created_at: str | None = None


# LLM: MainContextBundleResult returns both prompt text and persisted refs without extra lookups.
# 类用途: 保存 context bundle 的 prompt 注入文本、JSON 路径、Markdown 路径和原始 payload。
@dataclass(frozen=True)
class MainContextBundleResult:
    prompt_section: str
    json_path: str = ""
    markdown_path: str = ""
    bundle: dict[str, object] | None = None


# LLM: build_main_context_bundle keeps root context deterministic and independent of prompt wording.
# 函数用途: 生成主代理 context bundle；save=True 时落盘，save=False 时只返回临时 prompt 注入。
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


# LLM: latest_main_context_bundle_path gives compact/resume a refs-first root run card when one exists.
# 函数用途: 返回最近一次保存的主代理 context bundle；没有家目录或没有文件时返回空字符串。
def latest_main_context_bundle_path(home_paths: Any | None) -> str:
    if home_paths is None:
        return ""
    base = Path(home_paths.memory_archive_dir) / "snapshots" / "context_bundles"
    if not base.exists():
        return ""
    candidates = _latest_bundle_candidates(base)
    return str(candidates[0]) if candidates else ""


# LLM: _bundle_payload is refs-only so large artifacts and raw tool outputs stay outside prompts.
# 函数用途: 生成机器可读的主代理上下文结构，只记录路径、范围和计数，不内联大正文。
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
        "reserved": {},
    }
    payload.update(main_context_contract_sections(request, home_paths))
    return payload


# LLM: _identity_payload names the owner without mixing in subagent-local identity.
# 函数用途: 生成主代理身份字段，后续子代理复用内核时可替换 owner_type/owner_id。
def _identity_payload(request: MainContextBundleRequest) -> dict[str, object]:
    return {
        "owner_type": request.owner_type or "main_agent",
        "owner_id": request.owner_id or "root",
        "root_run_id": request.root_run_id or request.run_id,
        "parent_run_id": request.parent_run_id,
        "source": request.source or "run",
        "reserved": {},
    }


# LLM: _scope_payload keeps request/run/task ids machine-readable for recovery and UI.
# 函数用途: 生成本轮运行范围字段，避免模型从自然语言摘要里猜 request_id/run_id/task_id。
def _scope_payload(request: MainContextBundleRequest) -> dict[str, object]:
    return {
        "request_id": request.request_id,
        "run_id": request.run_id,
        "task_id": request.task_id,
        "context_scope": request.context_scope or "default",
        "reserved": {},
    }


# LLM: _workspace_refs records stable folders and leaves body reading to explicit tools.
# 函数用途: 生成主工作区、my-agent 家目录、任务空间和产物空间的 refs。
def _workspace_refs(request: MainContextBundleRequest, home_paths: Any | None) -> dict[str, object]:
    root = str(Path(request.root).resolve())
    if home_paths is None:
        return {"primary_workspace_root": root, "reserved": {}}
    return {
        "primary_workspace_root": root,
        "my_agent_home": str(Path(home_paths.root).resolve()),
        "workspace_tasks_root": str(Path(home_paths.workspace_tasks_dir).resolve()),
        "memory_root": str(Path(home_paths.memory_dir).resolve()),
        "artifacts_root": str((Path(home_paths.memory_archive_dir) / "artifacts").resolve()),
        "reserved": {},
    }


# LLM: _task_payload preserves only a short task preview and structured attributes.
# 函数用途: 生成任务字段；长 prompt 不复制进 bundle，避免把 context bundle 变成第二份聊天记录。
def _task_payload(request: MainContextBundleRequest) -> dict[str, object]:
    return {
        "user_prompt_preview": _preview(request.user_prompt, max_chars=240),
        "attributes": dict(request.task_attributes or {}),
        "reserved": {},
    }


# LLM: _memory_refs tells the model where memory lives without dumping all memory content.
# 函数用途: 生成长期记忆、按天流水、路由候选和关键记忆入口的 refs。
def _memory_refs(request: MainContextBundleRequest, home_paths: Any | None) -> dict[str, object]:
    payload: dict[str, object] = {
        "related_memory_count": int(request.memory_count),
        "routed_required_read_paths": list(request.routed_required_read_paths),
        "routed_candidate_paths": list(request.routed_candidate_paths),
        "reserved": {},
    }
    if home_paths is not None:
        payload.update(
            {
                "daily_memory_root": str(Path(home_paths.memory_daily_dir).resolve()),
                "key_memory_path": str(Path(home_paths.memory_md).resolve()),
                "lessons_root": str(Path(home_paths.memory_lessons_dir).resolve()),
                "indexes_root": str(Path(home_paths.memory_indexes_dir).resolve()),
            }
        )
    return payload


# LLM: _recovery_refs exposes compact/snapshot locations for future resume without auto-reading bodies.
# 函数用途: 生成恢复相关 refs，包括 compact apply、snapshot 和本轮是否注入自动恢复上下文。
def _recovery_refs(request: MainContextBundleRequest, home_paths: Any | None) -> dict[str, object]:
    payload: dict[str, object] = {
        "resume_context_injected": bool(request.resume_context_injected),
        "reserved": {},
    }
    if home_paths is not None:
        archive = Path(home_paths.memory_archive_dir)
        payload.update(
            {
                "compact_applies_root": str((archive / "compact_applies").resolve()),
                "snapshots_root": str((archive / "snapshots").resolve()),
                "tokens_root": str((archive / "tokens").resolve()),
            }
        )
    return payload


# LLM: _tooling_payload records tool-context shape without duplicating tool catalogs.
# 函数用途: 记录当前已有运行时注入数量和工具入口类型，便于调试 prompt 来源。
def _tooling_payload(request: MainContextBundleRequest) -> dict[str, object]:
    return {
        "runtime_injection_count_before_bundle": int(request.runtime_injection_count),
        "tool_gateway": "default_tool_registry",
        "reserved": {},
    }


# LLM: _write_bundle_files persists JSON and Markdown mirrors under memory_archive snapshots.
# 函数用途: 将 context bundle 写到可恢复目录；不覆盖旧 bundle，只额外更新 latest 副本。
def _write_bundle_files(
    request: MainContextBundleRequest,
    bundle: dict[str, object],
) -> tuple[Path, Path]:
    home_paths = request.home_paths
    base = Path(home_paths.memory_archive_dir) / "snapshots" / "context_bundles" / date.today().isoformat()
    base.mkdir(parents=True, exist_ok=True)
    stem = _bundle_stem(request)
    json_path = base / f"{stem}.json"
    markdown_path = base / f"{stem}.md"
    json_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(render_markdown_bundle(bundle, json_path=str(json_path)), encoding="utf-8")
    _write_latest_copies(base, json_path, markdown_path)
    return json_path, markdown_path


# LLM: _write_latest_copies gives humans and resume tools a stable pointer to the newest bundle.
# 函数用途: 在同一天目录下更新 latest JSON/Markdown 副本；不删除历史版本。
def _write_latest_copies(base: Path, json_path: Path, markdown_path: Path) -> None:
    (base / "latest_context_bundle.json").write_text(json_path.read_text(encoding="utf-8"), encoding="utf-8")
    (base / "latest_context_bundle.md").write_text(markdown_path.read_text(encoding="utf-8"), encoding="utf-8")


# LLM: _rewrite_bundle_files refreshes persisted self-check/prompt-budget after the final path is known.
# 函数用途: 更新 JSON、Markdown 和 latest 副本，确保落盘 bundle 与返回 payload 一致。
def _rewrite_bundle_files(json_path: Path, markdown_path: Path, bundle: dict[str, object]) -> None:
    json_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(render_markdown_bundle(bundle, json_path=str(json_path)), encoding="utf-8")
    _write_latest_copies(json_path.parent, json_path, markdown_path)


# LLM: _latest_bundle_candidates prefers explicit latest mirrors, then falls back to timestamped JSON files.
# 函数用途: 查找最新 context bundle JSON，供 CLI compact apply 自动带上主代理任务卡。
def _latest_bundle_candidates(base: Path) -> list[Path]:
    latest = [path for path in base.glob("*/latest_context_bundle.json") if path.is_file()]
    regular = [
        path
        for path in base.glob("*/*.json")
        if path.is_file() and not path.name.startswith("latest_context_bundle")
    ]
    return sorted([*latest, *regular], key=lambda item: (_safe_mtime(item), str(item)), reverse=True)


# LLM: _bundle_stem keeps filenames literal-safe without hiding useful run ids.
# 函数用途: 用 request/run/task 字段生成安全文件名，字段为空时回退时间戳。
def _bundle_stem(request: MainContextBundleRequest) -> str:
    raw = request.request_id or request.run_id or request.task_id or f"context-{time_module.time_ns()}"
    return safe_task_slug(raw, max_chars=120)


# LLM: _preview trims prompt previews so context bundles do not duplicate full user messages.
# 函数用途: 生成短预览；多余正文仍以原始 run archive 为准。
def _preview(value: object, *, max_chars: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars].rstrip()}..."


# LLM: _now_iso centralizes context bundle timestamps.
# 函数用途: 返回 UTC ISO 时间字符串，供 bundle 创建时间使用。
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# LLM: _safe_mtime makes latest lookup robust when files disappear during diagnostics.
# 函数用途: 读取修改时间；文件不可访问时返回 0。
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
