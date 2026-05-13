# LLM: Run workspace helpers materialize per-task owner workspaces under ~/.my-agent without replacing legacy storage.
# 模块用途: 为普通主代理 run 创建 outputs/runtime/agents 分区、状态文件和时间线记录。

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
    outputs_dir: Path
    runtime_dir: Path
    agents_dir: Path
    logs_dir: Path
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
    source: str = "run"
    created_at: str | None = None


# LLM: ensure_run_workspace is intentionally refs-only; it records where work belongs, not model/tool bodies.
# 函数用途: 创建或更新主代理任务工作区，并追加一条运行时间线事件。
def ensure_run_workspace(request: EnsureRunWorkspaceRequest) -> RunWorkspacePaths:
    paths = run_workspace_paths(request)
    for directory in (paths.root, paths.outputs_dir, paths.runtime_dir, paths.agents_dir, paths.logs_dir):
        directory.mkdir(parents=True, exist_ok=True)
    _write_task_yaml_if_missing(paths.task_yaml, request)
    paths.state_json.write_text(
        json.dumps(_state_payload(request), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    append_jsonl(paths.timeline_jsonl, _timeline_payload(request), sort_keys=True)
    return paths


# LLM: run_workspace_paths expands the admin-configured task template into the owner home.
# 函数用途: 根据 home、任务模板和任务名计算本次 run 的任务目录和标准子目录。
def run_workspace_paths(request: EnsureRunWorkspaceRequest) -> RunWorkspacePaths:
    root = task_workspace_path(
        request.home,
        request.template,
        date=_date_key(request.created_at),
        task_name=request.task_id or request.task_name or request.user_prompt,
    )
    return RunWorkspacePaths(
        root=root,
        outputs_dir=root / "outputs",
        runtime_dir=root / "runtime",
        agents_dir=root / "agents",
        logs_dir=root / "logs",
        task_yaml=root / "task.yaml",
        state_json=root / "state.json",
        timeline_jsonl=root / "timeline.jsonl",
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
        "task_name": request.task_name,
        "source": request.source,
        "updated_at": _now_iso(),
        "reserved": {},
    }


# LLM: _timeline_payload is append-only so future debugging can reconstruct task workspace creation.
# 函数用途: 生成 timeline.jsonl 的单条 refs-only 事件。
def _timeline_payload(request: EnsureRunWorkspaceRequest) -> dict[str, object]:
    return {
        "event_type": "run_workspace_saved",
        "request_id": request.request_id,
        "run_id": request.run_id,
        "task_id": request.task_id,
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


__all__ = ["EnsureRunWorkspaceRequest", "RunWorkspacePaths", "ensure_run_workspace", "run_workspace_paths"]
