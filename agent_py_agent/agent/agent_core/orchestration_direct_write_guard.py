# LLM: Delegate-only direct-write guard keeps root/parent agents from bypassing assigned workers.
# 模块用途: 当用户明确要求通过子代理完成时，阻止 root/父级直接写业务产物，要求改走 worker/takeover/repair。

from __future__ import annotations

"""Guard direct product writes when the current user asked for delegated execution."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..tools import ToolExecutionResult
from .orchestration_delegation_intent import prompt_requests_refs_only_delegation

_DIRECT_WRITE_TOOLS = {"write_file", "append_file", "replace_in_file"}
_SHELL_TOOL = "run_command"
_RUNTIME_MARKERS = {"_runtime", ".my_agent_runtime", "subagents", "tasks", "agents"}
_RUNTIME_FILE_NAMES = {
    "ACTION_RECEIPTS.md",
    "BUGS.md",
    "BUILD_REPORT.md",
    "CONTEXT_BUNDLE.md",
    "HANDOFF.md",
    "STATUS.md",
    "WORK_LOG.md",
    "output.json",
    "task.json",
}
_WRITE_COMMAND_PATTERNS = (
    r"(^|[;&|]\s*)(cat|printf|echo)\b[\s\S]*(>|>>)",
    r"(^|[;&|]\s*)tee\b",
    r"\bwrite_text\s*\(",
    r"\bopen\s*\([^)]*,\s*['\"][wa]",
    r"(^|[;&|]\s*)touch\s+",
    r"(^|[;&|]\s*)cp\s+",
    r"(^|[;&|]\s*)mv\s+",
)
_DELEGATE_ONLY_PATTERNS = (
    r"只能.{0,16}(通过|让|由).{0,12}(子代理|subagent|worker|builder)",
    r"必须.{0,16}(通过|让|由).{0,12}(子代理|subagent|worker|builder)",
    r"不能.{0,12}(自己|直接).{0,12}(写|实现|修改|创建|write|implement|create)",
    r"不要.{0,12}(自己|直接).{0,12}(写|实现|修改|创建|write|implement|create)",
    r"root.{0,24}(must not|cannot|do not).{0,24}(write|implement|create)",
    r"delegate[-_ ]only",
)
_PRODUCT_WRITER_ROLE_TOKENS = (
    "worker",
    "writer",
    "coder",
    "builder",
    "implementer",
)
_NON_PRODUCT_WRITER_ROLE_TOKENS = (
    "accept",
    "bug",
    "coordinator",
    "critic",
    "lead",
    "manager",
    "planner",
    "qa",
    "review",
    "root",
    "test",
    "verifier",
    "验收",
    "协调",
    "测试",
    "找茬",
)
_REPORT_WRITE_SUFFIXES = (".md", ".txt", ".json", ".jsonl")
_REPORT_WRITE_NAME_MARKERS = (
    "acceptance",
    "audit",
    "bug",
    "check",
    "finding",
    "handoff",
    "report",
    "review",
    "status",
    "summary",
    "test",
    "验收",
    "报告",
    "测试",
)


# LLM: DelegateOnlyDirectWriteGuardRequest bundles tool-loop data for direct-write policy checks.
# 类用途: 保存即将执行的工具 payload 和当前用户 prompt，避免工具循环直接知道正则细节。
@dataclass(frozen=True)
class DelegateOnlyDirectWriteGuardRequest:
    agent: object
    payload: object
    user_prompt: str = ""


# LLM: maybe_block_delegate_only_direct_write is a tool-loop preflight for delegated task boundaries.
# 函数用途: 用户明确要求通过子代理完成时，阻断 root/父级直接写文件或用 shell 生成产物。
def maybe_block_delegate_only_direct_write(
    request: DelegateOnlyDirectWriteGuardRequest,
) -> ToolExecutionResult | None:
    if not isinstance(request.payload, dict):
        return None
    if not _user_requested_delegate_only(request.user_prompt):
        return None
    if _current_runner_can_write_product(request.agent):
        return None
    tool = str(request.payload.get("tool") or "").strip()
    if tool in _DIRECT_WRITE_TOOLS and not _is_allowed_non_product_write(request.agent, request.payload):
        return _blocked_result(tool)
    if tool == _SHELL_TOOL and _shell_command_writes_product(request.payload):
        return _blocked_result(tool)
    return None


# LLM: _user_requested_delegate_only detects explicit current-run delegation constraints.
# 函数用途: 只在用户明确说不能由 root 直接写/必须通过子代理时触发，避免普通任务误伤。
def _user_requested_delegate_only(prompt: str) -> bool:
    text = " ".join(str(prompt or "").lower().split())
    if not text:
        return False
    if prompt_requests_refs_only_delegation(text):
        return True
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in _DELEGATE_ONLY_PATTERNS)


# LLM: _current_runner_can_write_product keeps delegate-only guard scoped to the outer root, not active subagents.
# 函数用途: 只要已经进入某个 subagent runner，上层派工约束就不再按角色剥夺它的基础写入能力；真实路径仍由 write boundary 守住。
def _current_runner_can_write_product(agent: object) -> bool:
    run_id = str(getattr(agent, "_current_subagent_run_id", "") or "").strip()
    if not run_id:
        return False
    task = _load_current_runner_task(agent, run_id)
    return task is not None


# LLM: _load_current_runner_task tolerates fake agents and partial managers in tests and recovery paths.
# 函数用途: 尝试读取当前 runner 的 task 元数据；失败时返回 None，让 guard 保守按父级处理。
def _load_current_runner_task(agent: object, run_id: str) -> object | None:
    subagents = getattr(agent, "subagents", None)
    load = getattr(subagents, "load", None)
    if not callable(load):
        return None
    try:
        return load(run_id)
    except Exception:
        return None


# LLM: _runner_identity_text normalizes role/name fields for direct-writer role checks.
# 函数用途: 合并 role 和 agent_name，兼容 leaf_worker、小小傻妞-worker 等自然命名。
def _runner_identity_text(task: object | None) -> str:
    if task is None:
        return ""
    role = str(getattr(task, "role", "") or "")
    name = str(getattr(task, "agent_name", "") or "")
    return f"{role} {name}".lower().replace("-", "_")


# LLM: _is_runtime_write lets agents still write their own coordination reports.
# 函数用途: 允许写 task/runtime 元数据或报告，不允许写 deliverables 这类最终业务产物。
def _is_runtime_write(agent: object, payload: dict[str, Any]) -> bool:
    path = _payload_path(agent, payload)
    if path is None:
        return False
    if path.name in _RUNTIME_FILE_NAMES and _looks_like_runtime_path(path):
        return True
    return _looks_like_runtime_path(path) and "deliverables" not in path.parts


# LLM: _is_allowed_non_product_write separates report artifacts from business deliverables.
# 函数用途: 委托模式下允许 root/QA/coordinator 写报告类交接文件，但继续阻止它们写 index.html 等业务正文。
def _is_allowed_non_product_write(agent: object, payload: dict[str, Any]) -> bool:
    return _is_runtime_write(agent, payload) or _is_report_artifact_write(agent, payload)


# LLM: _is_report_artifact_write lets reviewer roles leave visible evidence without editing product bodies.
# 函数用途: 识别 test_report/acceptance_report/status 等小型交接文件；它们是验收证据，不是 worker 负责的业务产物。
def _is_report_artifact_write(agent: object, payload: dict[str, Any]) -> bool:
    path = _payload_path(agent, payload)
    if path is None:
        return False
    name = path.name.lower()
    if not name.endswith(_REPORT_WRITE_SUFFIXES):
        return False
    return any(marker in name for marker in _REPORT_WRITE_NAME_MARKERS)


# LLM: _shell_command_writes_product flags shell forms that can create or mutate product files.
# 函数用途: 拦截 cat/echo/tee/python write_text/cp/mv/touch 等直接产物写入；ls/find/mkdir 等观察和建目录不拦。
def _shell_command_writes_product(payload: dict[str, Any]) -> bool:
    command = str(payload.get("command") or "").strip()
    if not command:
        return False
    return any(re.search(pattern, command, flags=re.IGNORECASE) for pattern in _WRITE_COMMAND_PATTERNS)


# LLM: _payload_path resolves write paths enough to distinguish runtime refs from products.
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


# LLM: _looks_like_runtime_path detects my-agent runtime areas without importing workspace adapters.
# 函数用途: 用路径片段识别 subagent/task/agent 元数据区，给协调报告留下写入通道。
def _looks_like_runtime_path(path: Path) -> bool:
    parts = set(path.parts)
    return bool(_RUNTIME_MARKERS.intersection(parts)) and "subagents" in parts


# LLM: _blocked_result teaches the model the correct delegated recovery route.
# 函数用途: 工具层拒绝直接写后，明确建议继续派 worker/takeover/repair，而不是询问用户。
def _blocked_result(tool: str) -> ToolExecutionResult:
    return ToolExecutionResult(
        tool,
        False,
        (
            "delegated_direct_write_blocked=true。"
            "用户已要求 root/父级通过子代理完成，不能直接写业务产物。"
            "请改用 create_subagents、schedule_child_subagents 或 dispatch_subagents："
            "有可恢复 run 时优先 packet/takeover/repair worker；"
            "没有产物且原 worker 超时时，可以创建新的 worker，但 root 仍不能亲自写最终 deliverables。"
        ),
    )
