
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from ..common.json_io import (
    locked_json_path,
    write_json_file_atomic,
    write_json_file_atomic_unlocked,
)
from ..contracts.state_machine import REGISTERED_STATES, VERIFICATION_STATES
from ..io import append_jsonl
from .home_layout import task_workspace_path
from .task_title import (
    collapse_dashes,
    concise_task_title,
    looks_like_machine_id,
    prompt_fingerprint,
    workspace_slug_char,
)

_RUN_WORKSPACE_FINISH_STATUSES = frozenset(
    {"DONE", "FAILED", "CANCELLED", "ABANDONED", "TIMEOUT", "CHANNEL_ERROR", "BLOCKED"}
)


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
    summaries_dir: Path
    task_yaml: Path
    workspace_json: Path
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


# LLM: 该请求只携带 host 已裁决的结构化终态与精确 workspace 身份，不能放入模型正文或推断字段。
# 类用途: 描述一次 standalone 工作区收尾要核对的身份、状态和审计时间。
@dataclass(frozen=True)
class FinishRunWorkspaceRequest:
    root: str | Path
    request_id: str = ""
    run_id: str = ""
    task_id: str = ""
    status: str = "DONE"
    verification_status: str = "UNVERIFIED"
    runtime_status: str = "ok"
    runtime_reason: str = ""
    runtime_source: str = ""
    finished_at: str | None = None


# LLM: 终态规范化值只在一次 finish 调用内部流转，不是新的持久 schema 或第二状态源。
# 类用途: 把已经校验的状态、验证状态、终结运行 ID 和时间作为一个整体传给锁内写入逻辑。
@dataclass(frozen=True)
class _RunWorkspaceTerminal:
    status: str
    verification: str
    run_id: str
    finished_at: str


def ensure_run_workspace(request: EnsureRunWorkspaceRequest) -> RunWorkspacePaths:
    return activate_run_workspace(run_workspace_paths(request).root, request)


# LLM: 这是 standalone 主运行工作区的唯一终态写入口；只接受结构化运行结果和精确
# request/run/task 身份，不解析模型正文，也不扫描 output/ 猜“是否完成”。
# 函数用途: 在一次顶层运行真正返回时原子结束 state.json，并给 timeline 留一条可审计终态。
def finish_run_workspace(request: FinishRunWorkspaceRequest) -> RunWorkspacePaths | None:
    status = str(request.status or "").strip().upper()
    verification = str(request.verification_status or "").strip().upper()
    if status not in REGISTERED_STATES or status not in _RUN_WORKSPACE_FINISH_STATUSES:
        raise ValueError(f"run workspace finish status is invalid: {status}")
    if verification not in VERIFICATION_STATES:
        raise ValueError(f"run workspace verification status is invalid: {verification}")
    paths = _run_workspace_paths_for_root(Path(request.root).expanduser().resolve(strict=False))
    identity = _workspace_identity(paths.root)
    terminal = _RunWorkspaceTerminal(
        status=status,
        verification=verification,
        run_id=str(request.run_id or request.request_id or request.task_id or "").strip(),
        finished_at=str(request.finished_at or _now_iso()),
    )
    if not identity or not _finish_identity_matches(identity, request):
        return None
    changed = False
    with locked_json_path(paths.state_json):
        current = _read_json_object(paths.state_json)
        if not current or not _finish_state_identity_matches(current, request):
            return None
        if (
            str(current.get("status") or "").strip().upper() == terminal.status
            and str(current.get("terminal_run_id") or "").strip() == terminal.run_id
            and str(current.get("finished_at") or "").strip()
        ):
            return paths
        updated = _finished_state_payload(current, request, terminal)
        write_json_file_atomic_unlocked(paths.state_json, updated)
        changed = True
    if changed:
        append_jsonl(
            paths.timeline_jsonl,
            _finish_timeline_payload(
                request,
                terminal.status,
                terminal.verification,
                terminal.finished_at,
            ),
            sort_keys=True,
        )
    return paths


# LLM: 终态 payload 保留已有 artifact/blocker 等任务事实，只覆盖 host 有权裁决的运行字段。
# 函数用途: 生成要原子写入 state.json 的最终状态；成功运行同时把进度设为 100%。
def _finished_state_payload(
    current: dict[str, object],
    request: FinishRunWorkspaceRequest,
    terminal: _RunWorkspaceTerminal,
) -> dict[str, object]:
    updated = {
        **current,
        "status": terminal.status,
        "verification_status": terminal.verification,
        "current_step": "",
        "runtime_status": str(request.runtime_status or ""),
        "runtime_reason": str(request.runtime_reason or ""),
        "runtime_source": str(request.runtime_source or ""),
        "terminal_run_id": terminal.run_id,
        "finished_at": terminal.finished_at,
        "updated_at": terminal.finished_at,
    }
    if terminal.status == "DONE":
        updated["progress"] = 1.0
    return updated


def activate_run_workspace(
    root: str | Path,
    request: EnsureRunWorkspaceRequest,
) -> RunWorkspacePaths:
    """Project the current execution onto an existing or newly resolved task directory.

    Conversation task links remain the per-execution lifecycle authority.  The
    files directly under ``work/`` describe whichever execution currently owns
    that reusable project directory, while ``timeline.jsonl`` preserves prior
    activations.
    """

    paths = _run_workspace_paths_for_root(Path(root).expanduser().resolve(strict=False))
    previous_identity = _workspace_identity(paths.root)
    same_activation = _same_run_activation(previous_identity, request)
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
        paths.summaries_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    _write_task_yaml(paths.task_yaml, request)
    write_json_file_atomic(
        paths.workspace_json,
        _workspace_identity_payload(request, paths),
    )
    _write_seed_file(paths.collab_blackboard_md, "# Blackboard\n\n")
    _write_seed_file(paths.collab_messages_jsonl, "")
    _write_seed_file(paths.collab_findings_jsonl, "")
    _activate_artifact_manifest(paths.artifact_manifest_json, request)
    _activate_task_state(paths.state_json, request)
    if not same_activation:
        append_jsonl(paths.timeline_jsonl, _timeline_payload(request), sort_keys=True)
    return paths


def run_workspace_paths(request: EnsureRunWorkspaceRequest) -> RunWorkspacePaths:
    return _run_workspace_paths_for_root(_resolve_run_workspace_root(request))


def _run_workspace_paths_for_root(root: Path) -> RunWorkspacePaths:
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
        summaries_dir=work / "summaries",
        task_yaml=work / "task.yaml",
        workspace_json=work / "run_workspace.json",
        state_json=work / "state.json",
        timeline_jsonl=work / "timeline.jsonl",
    )


def _write_task_yaml(path: Path, request: EnsureRunWorkspaceRequest) -> None:
    # task_id 是机器身份，task_title 才是人类标题；两者不能再互相兜底。
    task_id = str(request.task_id or request.run_id or request.request_id or "").strip()
    text = (
        f'task_id: "{_yaml_escape(task_id)}"\n'
        f'task_title: "{_yaml_escape(_workspace_task_name(request))}"\n'
        f'request_id: "{_yaml_escape(request.request_id)}"\n'
        f'run_id: "{_yaml_escape(request.run_id)}"\n'
        f'owner_id: "{_yaml_escape(request.owner_id)}"\n'
        f'owner_home: "{_yaml_escape(request.owner_home)}"\n'
        f'source: "{_yaml_escape(request.source)}"\n'
    )
    path.write_text(text, encoding="utf-8")


def _workspace_identity_payload(request: EnsureRunWorkspaceRequest, paths: RunWorkspacePaths) -> dict[str, object]:
    return {
        "schema_version": "run_workspace.v1",
        "request_id": request.request_id,
        "run_id": request.run_id,
        "task_id": request.task_id,
        "task_root": str(paths.root),
        "output_dir": str(paths.output_dir),
        "work_dir": str(paths.work_dir),
        "task_title": _workspace_task_name(request),
        "prompt_fingerprint": prompt_fingerprint(request.user_prompt),
        "owner_id": request.owner_id,
        "owner_home": request.owner_home,
        "task_name": request.task_name,
        "source": request.source,
        "updated_at": _now_iso(),
    }


def _task_state_payload(request: EnsureRunWorkspaceRequest) -> dict[str, object]:
    run_id = str(request.run_id or request.request_id or request.task_id or "").strip()
    return {
        "version": 1,
        "task_id": request.task_id or run_id,
        "primary_run_id": run_id,
        "status": "RUNNING",
        "verification_status": "",
        "progress": 0.0,
        "current_step": "",
        "latest_summary": "",
        "blockers": [],
        "artifact_refs": [],
        "evidence_refs": [],
        "child_run_ids": [],
        "updated_at": _now_iso(),
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


# LLM: timeline 终态记录必须与 state.json 使用同一请求事实，不得二次推导或改写状态。
# 函数用途: 把已核验的工作区终态整理成一条追加式审计事件。
def _finish_timeline_payload(
    request: FinishRunWorkspaceRequest,
    status: str,
    verification_status: str,
    finished_at: str,
) -> dict[str, object]:
    return {
        "event_type": "run_workspace_finished",
        "request_id": request.request_id,
        "run_id": request.run_id,
        "task_id": request.task_id,
        "status": status,
        "verification_status": verification_status,
        "runtime_status": request.runtime_status,
        "runtime_reason": request.runtime_reason,
        "runtime_source": request.runtime_source,
        "created_at": finished_at,
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
    # LLM: task_id/request_id/run_id 只参与身份与撞名后缀，绝不能成为目录标题候选。
    # 人类: 先用明确任务名，没有就从用户原话提取；两者都空才退到 task。
    task_name = str(request.task_name or "").strip()
    if task_name and not looks_like_machine_id(task_name):
        return concise_task_title(task_name)
    return concise_task_title(str(request.user_prompt or "task"))


def _resolve_run_workspace_root(request: EnsureRunWorkspaceRequest) -> Path:
    base = task_workspace_path(
        request.home,
        request.template,
        date=_date_key(request.created_at),
        task_name=_workspace_task_name(request),
    )
    if _workspace_matches_request(base, request):
        return base
    suffix = safe_workspace_suffix(request)
    if not suffix:
        return _next_available_workspace(base)
    candidate = base.with_name(f"{base.name}-{suffix}")
    if _workspace_matches_request(candidate, request):
        return candidate
    return _next_available_workspace(candidate)


# LLM: 任务目录复用判定(R8 接力实锤修复:此前只按 request_id/run_id/task_id
#   三个机器 ID 匹配,新 run 三 ID 全新永不命中,同 prompt 接力被迫开
#   "-run-<ns>" 新目录——接力变重做,原 progress/产物/expected_outputs 声明全部
#   失效,违背配置注释"同一 prompt 会复用同一个任务目录"的承诺)。补
#   prompt_fingerprint 匹配:逐字相同的 prompt 即同一任务(resume 语义),复用
#   同目录接续;fingerprint 已随 _workspace_identity_payload 落盘多轮,旧目录
#   天然可比。机器 ID 匹配保留在前(同 run 重入最强证据)。
# 函数用途: 判断"这个已存在的任务目录就是本次请求要的那个吗"。
def _workspace_matches_request(root: Path, request: EnsureRunWorkspaceRequest) -> bool:
    if not root.exists():
        return True
    state = _workspace_identity(root)
    if not state:
        return False
    request_values = _identity_values(request)
    state_values = {
        "request_id": str(state.get("request_id") or "").strip(),
        "run_id": str(state.get("run_id") or "").strip(),
        "task_id": str(state.get("task_id") or "").strip(),
    }
    for key, value in request_values.items():
        if value and state_values.get(key) == value:
            return True
    request_fingerprint = prompt_fingerprint(request.user_prompt)
    state_fingerprint = str(state.get("prompt_fingerprint") or "").strip()
    return bool(request_fingerprint and state_fingerprint and request_fingerprint == state_fingerprint)


def _workspace_identity(root: Path) -> dict[str, object]:
    return _read_json_object(root / "work" / "run_workspace.json")


def _read_json_object(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _identity_values(request: EnsureRunWorkspaceRequest) -> dict[str, str]:
    return {
        "request_id": str(request.request_id or "").strip(),
        "run_id": str(request.run_id or "").strip(),
        "task_id": str(request.task_id or "").strip(),
    }


# LLM: finish identity 只来自 typed request；空值会被调用方忽略，但不能用路径名补齐身份。
# 函数用途: 规范化本次收尾请求里的 request、run、task 三种标识。
def _finish_identity_values(request: FinishRunWorkspaceRequest) -> dict[str, str]:
    return {
        "request_id": str(request.request_id or "").strip(),
        "run_id": str(request.run_id or "").strip(),
        "task_id": str(request.task_id or "").strip(),
    }


# LLM: workspace identity 必须逐项等于所有非空入参；不允许任意一个命中就放行。
# 函数用途: 防止一次运行把另一个任务目录错误写成终态。
def _finish_identity_matches(
    identity: dict[str, object],
    request: FinishRunWorkspaceRequest,
) -> bool:
    incoming = {key: value for key, value in _finish_identity_values(request).items() if value}
    return bool(incoming) and all(
        str(identity.get(key) or "").strip() == value for key, value in incoming.items()
    )


# LLM: 锁内 state 还要再核对 canonical task/run，避免 identity 检查后被并发激活替换。
# 函数用途: 在真正写 state.json 前做最后一次当前执行身份确认。
def _finish_state_identity_matches(
    state: dict[str, object],
    request: FinishRunWorkspaceRequest,
) -> bool:
    expected_task_id = str(request.task_id or request.run_id or request.request_id or "").strip()
    expected_run_id = str(request.run_id or request.request_id or request.task_id or "").strip()
    state_task_id = str(state.get("task_id") or "").strip()
    state_run_id = str(state.get("primary_run_id") or "").strip()
    return bool(
        expected_task_id
        and expected_run_id
        and (not state_task_id or state_task_id == expected_task_id)
        and (not state_run_id or state_run_id == expected_run_id)
    )


def _same_run_activation(
    state: dict[str, object],
    request: EnsureRunWorkspaceRequest,
) -> bool:
    incoming = _identity_values(request)
    present = {key: value for key, value in incoming.items() if value}
    return bool(present) and all(str(state.get(key) or "").strip() == value for key, value in present.items())


def safe_workspace_suffix(request: EnsureRunWorkspaceRequest) -> str:
    source = str(request.run_id or request.request_id or request.task_id or "").strip()
    if not source:
        return ""
    return collapse_dashes("".join(workspace_slug_char(char) for char in source.lower())).strip("-_")[:24].strip("-_")


def _next_available_workspace(root: Path) -> Path:
    if not root.exists():
        return root
    for index in range(2, 1000):
        candidate = root.with_name(f"{root.name}-{index}")
        if not candidate.exists():
            return candidate
    return root.with_name(f"{root.name}-{datetime.now(timezone.utc).strftime('%H%M%S%f')}")


def _yaml_escape(value: object) -> str:
    return str(value or "").replace('"', '\\"')


def _write_seed_file(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def _activate_task_state(path: Path, request: EnsureRunWorkspaceRequest) -> None:
    """Keep the single workspace projection aligned with its current execution."""

    incoming = _task_state_payload(request)
    if not path.exists():
        write_json_file_atomic(path, incoming)
        return
    current = _read_json_object(path)
    if not current:
        # An unreadable projection is evidence worth preserving for doctor/query
        # commands; do not silently erase it while activating a new run.
        return
    current_task_id = str(current.get("task_id") or "").strip()
    incoming_task_id = str(incoming.get("task_id") or "").strip()
    if current_task_id == incoming_task_id:
        current.update(
            {
                "task_id": incoming["task_id"],
                "primary_run_id": incoming["primary_run_id"],
                "status": incoming["status"],
                "updated_at": incoming["updated_at"],
            }
        )
        write_json_file_atomic(path, current)
        return
    write_json_file_atomic(path, incoming)


def _activate_artifact_manifest(path: Path, request: EnsureRunWorkspaceRequest) -> None:
    """Move workspace-level artifact metadata to the current execution identity."""

    incoming = _artifact_manifest_payload(request)
    if not path.exists():
        write_json_file_atomic(path, incoming)
        return
    current = _read_json_object(path)
    if not current:
        return
    if _same_run_activation(current, request):
        return
    incoming["artifacts"] = list(current.get("artifacts") or [])
    write_json_file_atomic(path, incoming)


__all__ = [
    "EnsureRunWorkspaceRequest",
    "FinishRunWorkspaceRequest",
    "RunWorkspacePaths",
    "activate_run_workspace",
    "ensure_run_workspace",
    "finish_run_workspace",
    "run_workspace_paths",
]
