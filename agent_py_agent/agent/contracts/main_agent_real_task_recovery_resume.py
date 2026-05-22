# LLM: Real task recovery resume helpers keep continuation anchored to packet refs.
# 模块用途: 读取 recovery_packet.json，生成续跑 attempt 路径和交付合同字段，不解析 stdout 自然语言。

from __future__ import annotations

from pathlib import Path

from .main_agent_real_task_recovery_packet import SCHEMA_VERSION
from .main_agent_recovery_packet_reader import (
    first_recovery_reason_code,
    is_invalid_recovery_packet,
)
from .main_agent_recovery_packet_reader import (
    recovery_packet_payload as _read_recovery_packet_payload,
)


# LLM: recovery_packet_payload validates the packet schema before any resume run starts.
# 函数用途: 读取恢复包 JSON，确认版本和 case_id；坏包直接抛错，避免续跑错任务。
def recovery_packet_payload(packet_path: Path | None) -> dict[str, object]:
    return _read_recovery_packet_payload(packet_path, expected_schema_version=SCHEMA_VERSION)


# LLM: recovery_case_id returns the structured case identity for selecting one suite task.
# 函数用途: 从恢复包的 case_id 字段选择续跑任务，不从命令、日志或 prompt 猜。
def recovery_case_id(packet_path: Path | None) -> str:
    payload = recovery_packet_payload(packet_path)
    return str(payload.get("case_id") or "").strip()


# LLM: case_ids_for_recovery_request keeps resume selection tied to the packet case_id.
# 函数用途: 续跑时从 recovery_packet.case_id 选择任务；显式 case_ids 不匹配则拒绝。
def case_ids_for_recovery_request(
    requested_case_ids: tuple[str, ...],
    packet_path: Path | None,
) -> tuple[str, ...]:
    payload = recovery_packet_payload(packet_path)
    if is_invalid_recovery_packet(payload):
        code = first_recovery_reason_code(payload)
        raise ValueError(code)
    resume_case_id = recovery_case_id(packet_path)
    if not resume_case_id:
        return requested_case_ids
    if requested_case_ids and resume_case_id not in requested_case_ids:
        raise ValueError("recovery_packet_path case_id does not match requested case_ids")
    return (resume_case_id,)


# LLM: recovery_delivery_contract_payload embeds bounded recovery facts into delivery_contract.
# 函数用途: 把恢复包作为结构化交付合同的一部分传给运行循环，保留 refs 和 finding codes。
def recovery_delivery_contract_payload(
    packet_path: Path | None,
    *,
    workspace: Path,
) -> dict[str, object]:
    if packet_path is None:
        return {}
    path = Path(packet_path).expanduser().resolve()
    payload = recovery_packet_payload(path)
    if is_invalid_recovery_packet(payload):
        return {
            "schema_version": payload.get("schema_version"),
            "status": payload.get("status"),
            "recommended_action": payload.get("recommended_action"),
            "reason_codes": list(payload.get("reason_codes") or []),
            "packet_ref": _rel(path, workspace),
            "findings": list(payload.get("findings") or []),
        }
    result = {
        "schema_version": payload.get("schema_version"),
        "case_id": payload.get("case_id"),
        "status": payload.get("status"),
        "recommended_action": payload.get("recommended_action"),
        "reason_codes": list(payload.get("reason_codes") or []),
        "packet_ref": _rel(path, workspace),
        "refs": dict(payload.get("refs") or {}),
        "acceptance": dict(payload.get("acceptance") or {}),
    }
    if isinstance(payload.get("schema_warning"), dict):
        result["schema_warning"] = dict(payload["schema_warning"])
    return result


# LLM: resume_attempt_paths writes continuation logs under a fresh attempt folder.
# 函数用途: 续跑时 stdout/stderr/command/验收报告不覆盖上一轮失败证据。
def resume_attempt_paths(paths: dict[str, Path], packet_path: Path | None) -> dict[str, Path]:
    if packet_path is None:
        return paths
    attempt_root = paths["root"] / "resumes" / _next_attempt_id(paths["root"] / "resumes")
    resumed = dict(paths)
    resumed.update(
        {
            "config": attempt_root / "config.yaml",
            "command": attempt_root / "command.json",
            "delivery_contract": attempt_root / "delivery_contract.json",
            "stdout": attempt_root / "stdout.txt",
            "stderr": attempt_root / "stderr.txt",
            "acceptance_report": attempt_root / "acceptance_report.json",
            "recovery_packet": attempt_root / "recovery_packet.json",
            "events": attempt_root / "events.jsonl",
        }
    )
    return resumed


# LLM: _next_attempt_id derives a stable monotonic attempt folder from existing dirs.
# 函数用途: 生成 attempt-001/002 这类路径，让多次续跑不会互相覆盖。
def _next_attempt_id(root: Path) -> str:
    try:
        used = [int(path.name.removeprefix("attempt-")) for path in root.glob("attempt-*")]
    except OSError:
        used = []
    return f"attempt-{(max(used) if used else 0) + 1:03d}"


# LLM: _rel keeps refs portable inside the chosen real-task workspace.
# 函数用途: 把恢复包绝对路径转成相对工作区引用，外部路径才保留绝对值。
def _rel(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


__all__ = [
    "case_ids_for_recovery_request",
    "recovery_case_id",
    "recovery_delivery_contract_payload",
    "recovery_packet_payload",
    "resume_attempt_paths",
]
