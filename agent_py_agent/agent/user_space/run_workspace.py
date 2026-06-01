# LLM: Run workspace helpers materialize per-task owner workspaces with output/work split.
# 模块用途: 为普通主代理 run 创建 output 交付区和 work 过程区、状态文件和时间线记录。

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from ..io import append_jsonl
from .home_layout import task_workspace_path


# LLM: RunWorkspacePaths is the stable handoff object for task-local outputs and runtime refs.
# 类用途: 保存一次主代理任务工作区的标准目录和元数据文件路径。
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


# LLM: EnsureRunWorkspaceRequest keeps run workspace creation bundle-based and future-proof.
# 类用途: 打包创建主代理任务工作区需要的 home、模板、任务、run 和请求字段。
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


# LLM: ensure_run_workspace is intentionally refs-only; it records where work belongs, not model/tool bodies.
# 函数用途: 创建或更新主代理任务工作区，并追加一条运行时间线事件。
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


# LLM: run_workspace_paths expands the task template into an output/work task folder.
# 函数用途: 根据 home、任务模板和任务名计算本次 run 的任务目录、交付目录和过程目录。
def run_workspace_paths(request: EnsureRunWorkspaceRequest) -> RunWorkspacePaths:
    root = task_workspace_path(
        request.home,
        request.template,
        date=_date_key(request.created_at),
        task_name=request.task_id or request.task_name or request.user_prompt,
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


# LLM: _write_task_yaml_if_missing preserves user edits to task metadata after first creation.
# 函数用途: 首次创建任务目录时写入轻量任务说明，后续运行不覆盖人工修改。
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


# LLM: _state_payload captures current run refs without storing long prompts or responses.
# 函数用途: 生成 state.json 的稳定字段，供 resume、doctor 和前端展示使用。
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


# LLM: _timeline_payload is append-only so future debugging can reconstruct task workspace creation.
# 函数用途: 生成 timeline.jsonl 的单条 refs-only 事件。
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


# LLM: _date_key accepts explicit ISO dates for tests and defaults to local calendar days for task folders.
# 函数用途: 归一化任务目录日期字段。
def _date_key(value: str | None) -> str:
    if not value:
        return date.today().isoformat()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return value[:10]


# LLM: _now_iso centralizes timeline timestamps.
# 函数用途: 返回 UTC ISO 时间字符串。
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# LLM: _yaml_escape keeps the tiny YAML seed readable without adding a YAML dependency.
# 函数用途: 转义任务元数据里的双引号。
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
