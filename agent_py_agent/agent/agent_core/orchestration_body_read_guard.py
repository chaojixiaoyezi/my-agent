# LLM: Delegating body-read guard keeps parent agents refs-only until QA acceptance finishes.
# 模块用途: 父级已派出子代理时，阻止其在验收完成前主动读取产物正文或大 artifact 正文。

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..subagents.services.qa_role_contract import qa_role_identity_roles
from ..tools import ToolExecutionResult
from .orchestration_delegation_intent import (
    prompt_requests_refs_only_delegation,
    user_authorized_parent_body_read,
)
from .orchestration_shell_body_read import ShellBodyReadPathRequest, shell_body_read_paths
from .runner_context import current_subagent_run_id

_BODY_READ_TOOLS = {"read_file", "read_artifact", "run_command"}
_ORCHESTRATION_ARTIFACT_PREFIXES = (
    "dispatch_subagents-",
    "subagent_board-",
    "subagents_due_check-",
    "subagents_plan_actions-",
    "subagents_apply_actions-",
)
_METADATA_FILE_NAMES = {
    "ACCEPTANCE.md",
    "CONTEXT_BUNDLE.md",
    "HANDOFF.md",
    "STATUS.md",
    "TAKEOVER_READINESS.md",
    "acceptance_review.json",
    "checkpoint.json",
    "compaction_ledger.jsonl",
    "context_bundle.json",
    "failing_tests.json",
    "failure_handoff.json",
    "final_report.md",
    "latest_continue_packet.json",
    "latest_metadata.json",
    "latest_summary.md",
    "output.json",
    "parent_acceptance_auto_followup.json",
    "progress.md",
    "runner_result.json",
    "session_compact_ledger.jsonl",
    "status_report.json",
    "subagent_board.json",
    "subagent_dispatch_report.json",
    "summary.md",
    "takeover_readiness.json",
    "task.md",
    "task.json",
    "test_execution.json",
    "timeline.jsonl",
}
_MAX_DESCENDANT_SCAN = 128


# LLM: DelegatingBodyReadGuardRequest bundles tool-loop state for parent body-read policy checks.
# 类用途: 保存当前 agent 和即将执行的工具 payload，后续增加豁免字段时不扩散函数签名。
@dataclass(frozen=True)
class DelegatingBodyReadGuardRequest:
    agent: object
    payload: object
    user_prompt: str = ""


# LLM: _TopLevelDelegationParent mirrors a CLI root that has child runs but no current subagent id.
# 类用途: 给顶层 root 复用现有 descendants/acceptor 判断；只保存 child_ids，不落盘不持久化。
@dataclass(frozen=True)
class _TopLevelDelegationParent:
    id: str
    child_ids: list[str]
    role: str = "root"
    agent_name: str = "root"
    task_dir: str = ""


# LLM: maybe_block_delegating_body_read is the tool-loop entry point for refs-only delegation.
# 函数用途: 当前 runner 已委托下级且 acceptor 未完成时，阻断 read_file/read_artifact 读取正文。
def maybe_block_delegating_body_read(request: DelegatingBodyReadGuardRequest) -> ToolExecutionResult | None:
    if not isinstance(request.payload, dict):
        return None
    tool = str(request.payload.get("tool") or "")
    if tool not in _BODY_READ_TOOLS:
        return None
    parent = _current_parent_task(request.agent)
    if parent is None:
        parent = _top_level_delegation_parent(request.agent, request.user_prompt)
    if parent is None or not _has_delegated_children(parent):
        return None
    if user_authorized_parent_body_read(request.user_prompt):
        return None
    if _has_completed_acceptor(request.agent, parent):
        return None
    if tool == "run_command":
        if not _is_shell_body_read_command(request.agent, parent, request.payload):
            return None
        return ToolExecutionResult(tool, False, _blocked_message(tool, parent))
    if tool == "read_file" and _is_runtime_metadata_read(request.agent, parent, request.payload):
        return None
    if tool == "read_file" and _is_current_task_local_read(request.agent, parent, request.payload):
        return None
    if tool == "read_artifact" and _is_orchestration_artifact_read(request.payload):
        return None
    return ToolExecutionResult(tool, False, _blocked_message(tool, parent))


# LLM: _current_parent_task resolves the active runner task without assuming manager internals.
# 函数用途: 读取当前 subagent runner 的任务记录；失败时放行，避免普通聊天工具被误挡。
def _current_parent_task(agent: object):
    run_id = current_subagent_run_id(agent)
    if not run_id:
        return None
    try:
        return agent.subagents.load(run_id)
    except (AttributeError, FileNotFoundError, OSError, KeyError, TypeError, ValueError):
        return None


# LLM: _top_level_delegation_parent protects normal CLI roots when the user explicitly asks for refs-only delegation.
# 函数用途: root 不是子代理 run 时，从 manager.list_runs 构造一个临时父节点，让验收前读正文保护仍生效。
def _top_level_delegation_parent(agent: object, prompt: str) -> _TopLevelDelegationParent | None:
    if not prompt_requests_refs_only_delegation(prompt):
        return None
    tasks = _top_level_child_tasks(agent)
    child_ids = [str(getattr(task, "id", "") or "") for task in tasks if str(getattr(task, "id", "") or "").strip()]
    if not child_ids:
        return None
    return _TopLevelDelegationParent(id="top-level-root", child_ids=child_ids)


# LLM: _top_level_child_tasks reads only the subagent manager's bounded task list.
# 函数用途: 顶层 root 没有 parent task 时，获取当前 subagent workspace 中的 run 列表；失败即放行，避免普通工具误伤。
def _top_level_child_tasks(agent: object) -> list[object]:
    try:
        items = agent.subagents.list_runs()
    except (AttributeError, FileNotFoundError, OSError, KeyError, TypeError, ValueError):
        return []
    if not isinstance(items, list):
        return []
    return items[:_MAX_DESCENDANT_SCAN]


# LLM: _has_delegated_children is the cheap signal that this runner is a parent/coordinator now.
# 函数用途: 有 child_ids 才进入委托期读正文保护；普通 leaf worker 仍可读写自己负责的文件。
def _has_delegated_children(parent: object) -> bool:
    return bool([item for item in getattr(parent, "child_ids", []) if str(item or "").strip()])


# LLM: _has_completed_acceptor unlocks final parent inspection only after a real acceptor finished.
# 函数用途: 广度扫描父级子树，发现 VERIFIED/DONE acceptor 后允许父级做最后读正文验收。
def _has_completed_acceptor(agent: object, parent: object) -> bool:
    for task in _descendants(agent, parent):
        if not _is_acceptor(task):
            continue
        if _is_completed_for_acceptance(task):
            return True
    return False


# LLM: _descendants walks persisted child ids and avoids broad workspace scans.
# 函数用途: 只按 task.child_ids 读取有限后代，缺失节点跳过，避免 guard 自己扩大 IO。
def _descendants(agent: object, parent: object) -> list[object]:
    queue = [str(item) for item in getattr(parent, "child_ids", []) if str(item or "").strip()]
    seen: set[str] = set()
    result: list[object] = []
    while queue and len(seen) < _MAX_DESCENDANT_SCAN:
        run_id = queue.pop(0)
        if run_id in seen:
            continue
        seen.add(run_id)
        try:
            task = agent.subagents.load(run_id)
        except (AttributeError, FileNotFoundError, OSError, KeyError, TypeError, ValueError):
            continue
        result.append(task)
        queue.extend(str(item) for item in getattr(task, "child_ids", []) if str(item or "").strip())
    return result


# LLM: _is_acceptor uses the same role identity helper as QA scheduling.
# 函数用途: 通过 role/agent_name 识别验收子代理，兼容用户中文命名和模板角色。
def _is_acceptor(task: object) -> bool:
    roles = qa_role_identity_roles(
        role=str(getattr(task, "role", "") or ""),
        agent_name=str(getattr(task, "agent_name", "") or ""),
    )
    return "acceptor" in roles


# LLM: _is_completed_for_acceptance keeps the unlock conservative and auditable.
# 函数用途: acceptor 必须 DONE/COMPLETED 或 VERIFIED，不能仅因 PLANNING/RUNNING 存在而放行。
def _is_completed_for_acceptance(task: object) -> bool:
    status = str(getattr(task, "status", "") or "").upper()
    verification = str(getattr(task, "verification_status", "") or "").upper()
    return status in {"DONE", "COMPLETED"} or verification == "VERIFIED"


# LLM: _is_runtime_metadata_read allows parents to inspect coordination state without product bodies.
# 函数用途: 委托期 read_file 只允许读取 subagent runtime 元数据文件，不允许读 deliverables/业务正文。
def _is_runtime_metadata_read(agent: object, parent: object, payload: dict[str, Any]) -> bool:
    path = _payload_path(agent, payload)
    if path is None or path.name not in _METADATA_FILE_NAMES:
        return False
    return (
        _looks_like_runtime_path(path)
        or _is_current_task_metadata_path(parent, path)
        or _is_descendant_task_metadata_path(agent, parent, path)
    )


# LLM: _is_orchestration_artifact_read lets parents read small refs/status artifacts while blocking product bodies.
# 函数用途: 委托期允许 dispatch/subagent_board 等编排摘要 artifact，避免父级恢复时只能盲目派工。
def _is_orchestration_artifact_read(payload: dict[str, Any]) -> bool:
    raw = str(payload.get("artifact_ref") or payload.get("ref") or "").strip()
    if not raw:
        return False
    name = Path(raw).name
    return any(name.startswith(prefix) for prefix in _ORCHESTRATION_ARTIFACT_PREFIXES)


# LLM: _payload_path normalizes read_file path enough for policy checks but does not require file existence.
# 函数用途: 支持绝对路径和相对 workspace root 路径；解析失败时保守返回 None。
def _payload_path(agent: object, payload: dict[str, Any]) -> Path | None:
    raw = str(payload.get("path") or payload.get("file") or "").strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        root = Path(str(getattr(agent, "root", "") or ".")).expanduser()
        candidate = root / candidate
    try:
        return candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        return None


# LLM: _is_shell_body_read_command detects shell reads that would bypass read_file/read_artifact guards.
# 函数用途: 父级委托期间，如果 run_command 用 cat/tail/head/sed/rg 等读取产物正文，也按正文读取处理。
def _is_shell_body_read_command(agent: object, parent: object, payload: dict[str, Any]) -> bool:
    command = str(payload.get("command") or payload.get("cmd") or "").strip()
    if not command:
        return False
    paths = shell_body_read_paths(
        ShellBodyReadPathRequest(command=command, root=str(getattr(agent, "root", "") or "."))
    )
    return any(_shell_path_requires_body_read_block(agent, parent, path) for path in paths)


# LLM: _shell_path_requires_body_read_block separates metadata/control-plane reads from product bodies.
# 函数用途: shell 正文命令读取业务产物时阻断；读取 runtime 元数据或当前 task-local 文件时放行。
def _shell_path_requires_body_read_block(agent: object, parent: object, path: Path) -> bool:
    if path.name in _METADATA_FILE_NAMES and (
        _looks_like_runtime_path(path)
        or _is_current_task_metadata_path(parent, path)
        or _is_descendant_task_metadata_path(agent, parent, path)
    ):
        return False
    if _is_current_task_local_path(parent, path):
        return False
    return True


# LLM: _looks_like_runtime_path keeps runtime metadata readable without importing workspace adapters.
# 函数用途: 用路径片段识别 .my_agent_runtime/subagents 或 tasks/.../agents 元数据区。
def _looks_like_runtime_path(path: Path) -> bool:
    parts = tuple(path.parts)
    if ".my_agent_runtime" in parts and "subagents" in parts:
        return True
    if "_runtime" in parts and "subagents" in parts:
        return True
    if "tasks" in parts and "agents" in parts:
        return True
    return False


# LLM: _is_current_task_metadata_path recognizes the active legacy task directory as runtime metadata.
# 函数用途: 允许父级读取自己 task_dir 下的 output/status/runner 元数据，但不放行业务产物正文。
def _is_current_task_metadata_path(parent: object, path: Path) -> bool:
    task_dir = str(getattr(parent, "task_dir", "") or "").strip()
    if not task_dir:
        return False
    try:
        path.relative_to(Path(task_dir).expanduser().resolve(strict=False))
    except ValueError:
        return False
    return True


# LLM: _is_current_task_local_read lets a runner inspect files in its own assigned workspace.
# 函数用途: 允许 worker/coordinator 读取自己 task_dir 下的正文；refs-only 保护仍阻止父级读取下级或外部产物。
def _is_current_task_local_read(agent: object, parent: object, payload: dict[str, Any]) -> bool:
    path = _payload_path(agent, payload)
    if path is None:
        return False
    return _is_current_task_local_path(parent, path)


# LLM: _is_current_task_local_path is shared by direct read_file and guarded shell reads.
# 函数用途: 判断路径是否属于当前 runner 自己的 task_dir；这类读取不属于父级偷看下级产物。
def _is_current_task_local_path(parent: object, path: Path) -> bool:
    task_dir = str(getattr(parent, "task_dir", "") or "").strip()
    if not task_dir:
        return False
    try:
        path.relative_to(Path(task_dir).expanduser().resolve(strict=False))
    except ValueError:
        return False
    return True


# LLM: _is_descendant_task_metadata_path lets a delegating parent read child status packets without product bodies.
# 函数用途: 允许父级读取已委托子代理 task_dir 下的 output/status/runner 元数据，避免恢复时被 refs-only 卡死。
def _is_descendant_task_metadata_path(agent: object, parent: object, path: Path) -> bool:
    for task in _descendants(agent, parent):
        task_dir = str(getattr(task, "task_dir", "") or "").strip()
        if not task_dir:
            continue
        try:
            path.relative_to(Path(task_dir).expanduser().resolve(strict=False))
        except ValueError:
            continue
        return True
    return False


# LLM: _blocked_message teaches the model the recovery route instead of making it ask the user.
# 函数用途: 返回机器可读阻断原因和推荐工具，让父级自己派 QA/验收/修复，不打扰用户。
def _blocked_message(tool: str, parent: object) -> str:
    return (
        "delegating_body_read_blocked=true "
        f"tool={tool} parent_run_id={getattr(parent, 'id', '')}。"
        "父级已经派出子代理，验收子代理完成前保持 refs-only："
        "不要主动 read_file/read_artifact/run_command 读取产物正文或大 artifact 正文。"
        "请先使用 subagent_board 或 dispatch_subagents 的 direct_children 摘要查看状态；"
        "如缺 tester/bug_finder/acceptor，请调用 schedule_child_subagents 创建质量子代理；"
        "如已有失败 refs，请创建 scoped repair worker；"
        "只有 acceptor 完成验收后，父级才进入最终读正文核查阶段。"
    )
