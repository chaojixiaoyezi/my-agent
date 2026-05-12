# LLM: QA role coverage checks stay isolated from generic acceptance findings.
# 模块用途: 校验用户点名的 tester、bug_finder、acceptor 是否由真实后代 run 覆盖。

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..reports import AcceptanceReviewFinding
from ..role_contracts import normalize_subagent_role

_ROLE_COVERAGE_MARKERS = {
    "tester": ("tester", "测试子代理", "测试代理", "测试角色", "测试员"),
    "bug_finder": (
        "bug_finder",
        "bug-finder",
        "bug finder",
        "bugfinder",
        "找错子代理",
        "找茬子代理",
        "找错代理",
        "找茬代理",
        "找错角色",
        "找茬角色",
        "找错",
        "找茬",
    ),
    "acceptor": ("acceptor", "验收子代理", "验收代理", "验收角色", "验收员", "由acceptor", "由 acceptor"),
}
_ROLE_COVERAGE_ORDER = ("tester", "bug_finder", "acceptor")
_ROLE_SCAN_MAX_NODES = 64
_ROLE_SCAN_MAX_BYTES = 65536


# LLM: required_role_coverage_finding prevents coordinators from claiming QA roles that were never spawned.
# 函数用途: 用户明确要求 tester/bug_finder/acceptor 时，验收必须看到真实后代 run 角色。
def required_role_coverage_finding(task, created_at: float) -> AcceptanceReviewFinding:
    required = _required_role_coverage(task)
    if not required:
        return AcceptanceReviewFinding(
            name="required_role_coverage",
            ok=True,
            severity="P1",
            message="当前任务未声明必须覆盖特定 QA 角色。",
            evidence_path=task.output_json,
            created_at=created_at,
        )
    present = _descendant_roles(task)
    missing = [role for role in required if role not in present]
    return AcceptanceReviewFinding(
        name="required_role_coverage",
        ok=not missing,
        severity="P0",
        message=_role_coverage_message(required, present, missing),
        evidence_path=task.output_json,
        created_at=created_at,
    )


# LLM: _required_role_coverage reads only explicit role words from task contracts.
# 函数用途: 从 goal/acceptance 文本提取用户点名的 QA 角色；leaf 不承接父级角色覆盖检查。
def _required_role_coverage(task) -> list[str]:
    if _task_is_leaf(task):
        return []
    text = _task_contract_text(task)
    return [role for role in _ROLE_COVERAGE_ORDER if _role_marker_present(text, role)]


# LLM: _task_contract_text combines stable task fields while avoiding output self-claims.
# 函数用途: 只从任务目标和验收条件判断“要求过什么”，不相信 runner 总结里自称已完成。
def _task_contract_text(task) -> str:
    values = [str(getattr(task, "goal", "") or "")]
    values.extend(str(item) for item in getattr(task, "acceptance_checks", []) or [])
    return " ".join(values).lower()


# LLM: _task_is_leaf mirrors child-spawn checks so inherited parent wording does not punish leaf workers.
# 函数用途: leaf_worker 或名字带 leaf 的任务不继续要求下级 QA 角色，避免父级继承文本误伤。
def _task_is_leaf(task) -> bool:
    role = str(getattr(task, "role", "") or "").lower()
    agent_name = str(getattr(task, "agent_name", "") or "").lower()
    return role == "leaf_worker" or "leaf" in agent_name


# LLM: _role_marker_present keeps role detection explicit and ordered.
# 函数用途: 检查文本是否点名某个内置 QA 角色，避免把普通“验收条件”当成 acceptor 角色要求。
def _role_marker_present(text: str, role: str) -> bool:
    markers = _ROLE_COVERAGE_MARKERS.get(role, ())
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in markers)


# LLM: _descendant_roles scans persisted child task.json files, not model summaries.
# 函数用途: 沿真实 child_ids 广度优先读取少量后代 run，汇总实际落盘角色。
def _descendant_roles(task) -> set[str]:
    queue = _string_list(getattr(task, "child_ids", []))
    workspace = _child_workspace(task)
    roles: set[str] = set()
    if not queue or workspace is None:
        return roles
    seen: set[str] = set()
    scan = _RoleScanState(workspace=workspace, seen=seen, roles=roles, queue=queue)
    scanned = 0
    while queue and scanned < _ROLE_SCAN_MAX_NODES:
        run_id = queue.pop(0)
        scanned += _scan_role_record(scan, run_id)
    return roles


# LLM: _RoleScanState bundles breadth-first role scan state so helpers take stable request objects.
# 类用途: 保存角色覆盖扫描的 workspace、seen、roles、queue，避免散乱参数和后续扩展破坏调用点。
@dataclass
class _RoleScanState:
    workspace: Path
    seen: set[str]
    roles: set[str]
    queue: list[str]


# LLM: _scan_role_record keeps the breadth-first loop flat and bounded.
# 函数用途: 读取一个 child run 的角色和 child_ids；已扫描或读取失败时返回 0。
def _scan_role_record(scan: _RoleScanState, run_id: str) -> int:
    if run_id in scan.seen:
        return 0
    scan.seen.add(run_id)
    record = _read_child_task_record(scan.workspace, run_id)
    if not record:
        return 0
    scan.roles.update(_roles_from_record(record))
    scan.queue.extend(child_id for child_id in _string_list(record.get("child_ids", [])) if child_id not in scan.seen)
    return 1


# LLM: _child_workspace derives sibling run dirs from the current task_dir.
# 函数用途: 通过当前 run 的 task_dir 找到同一个 subagents workspace；失败时保守返回 None。
def _child_workspace(task) -> Path | None:
    try:
        task_dir = Path(str(getattr(task, "task_dir", "") or "")).expanduser()
    except OSError:
        return None
    if not str(task_dir):
        return None
    return task_dir.parent


# LLM: _read_child_task_record is a bounded exact-path JSON read for role coverage.
# 函数用途: 只打开 child_id/task.json，限制文件大小，避免 glob 扫描和大文件读取。
def _read_child_task_record(workspace: Path, run_id: str) -> dict[str, object]:
    path = workspace / run_id / "task.json"
    try:
        if path.stat().st_size > _ROLE_SCAN_MAX_BYTES:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


# LLM: _roles_from_record trusts persisted identity fields, not inherited goal prose.
# 函数用途: 从后代 task.json 的 role/agent_name 恢复实际 QA 角色，避免父级继承文本伪装成真实角色。
def _roles_from_record(record: dict[str, object]) -> set[str]:
    roles: set[str] = set()
    normalized = normalize_subagent_role(str(record.get("role") or ""))
    if normalized in _ROLE_COVERAGE_ORDER:
        roles.add(normalized)
    text = str(record.get("agent_name") or "").lower()
    for role in _ROLE_COVERAGE_ORDER:
        if _role_marker_present(text, role):
            roles.add(role)
    return roles


# LLM: _role_coverage_message gives parent recovery enough facts to re-dispatch missing roles.
# 函数用途: 输出缺哪些角色、已发现哪些角色，方便上级下一轮补派 tester/bug_finder/acceptor。
def _role_coverage_message(required: list[str], present: set[str], missing: list[str]) -> str:
    if not missing:
        return f"已覆盖用户点名 QA 角色: {', '.join(required)}。"
    found = ", ".join(sorted(present)) if present else "无"
    return f"缺少用户点名 QA 角色: {', '.join(missing)}；已发现角色: {found}。"


# LLM: _string_list mirrors generic acceptance normalization without importing the larger module.
# 函数用途: 将 child_ids 等字段归一成字符串列表，保持角色覆盖扫描轻量。
def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item not in (None, "")]
    return [str(value)]
