# LLM: Recovery packet reader normalizes task and real-task envelopes before resume decisions.
# 模块用途: 统一读取恢复包 schema、case_id 和 refs，让旧双轨恢复包可以安全进入同一恢复链路。

from __future__ import annotations

import json
import logging
from pathlib import Path

TASK_RECOVERY_SCHEMA_VERSION = "main-agent-task-recovery.v1"
REAL_TASK_RECOVERY_SCHEMA_VERSION = "main-agent-real-task-recovery.v1"
SUPPORTED_RECOVERY_SCHEMA_VERSIONS = {
    TASK_RECOVERY_SCHEMA_VERSION,
    REAL_TASK_RECOVERY_SCHEMA_VERSION,
}
LOGGER = logging.getLogger(__name__)


# LLM: recovery_packet_payload reads supported recovery envelopes and returns structured invalid packets on failure.
# 函数用途: 兼容 task/real_task 两类 schema；不因旧包名不同直接中断恢复链路。
def recovery_packet_payload(
    packet_path: Path | None,
    *,
    expected_schema_version: str,
) -> dict[str, object]:
    if packet_path is None:
        return {}
    path = Path(packet_path).expanduser()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return invalid_recovery_packet(path, expected_schema_version, "RECOVERY_PACKET_INVALID_JSON", exc.msg)
    except OSError as exc:
        return invalid_recovery_packet(path, expected_schema_version, "RECOVERY_PACKET_UNREADABLE", str(exc))
    if not isinstance(payload, dict):
        return invalid_recovery_packet(path, expected_schema_version, "RECOVERY_PACKET_NOT_OBJECT", "recovery packet must be a JSON object")
    actual_schema = str(payload.get("schema_version") or "")
    if actual_schema not in SUPPORTED_RECOVERY_SCHEMA_VERSIONS:
        LOGGER.warning(
            "unsupported recovery packet schema_version: expected=%s actual=%s path=%s",
            expected_schema_version,
            actual_schema,
            path,
        )
        return invalid_recovery_packet(
            path,
            expected_schema_version,
            "RECOVERY_PACKET_UNSUPPORTED_SCHEMA",
            "unsupported recovery packet schema_version",
        )
    if actual_schema != expected_schema_version:
        payload = dict(payload)
        payload["schema_warning"] = {
            "code": "RECOVERY_PACKET_SCHEMA_COMPAT",
            "expected": expected_schema_version,
            "actual": actual_schema,
        }
    if not str(payload.get("case_id") or "").strip():
        return invalid_recovery_packet(path, expected_schema_version, "RECOVERY_PACKET_MISSING_CASE_ID", "recovery packet missing case_id")
    return payload


# LLM: invalid_recovery_packet keeps corrupt recovery inputs machine-readable for reconcile and delivery gates.
# 函数用途: 将坏 recovery_packet 归一成结构化 payload，避免 JSONDecodeError 或 schema ValueError 泄漏到上层。
def invalid_recovery_packet(
    path: Path,
    schema_version: str,
    code: str,
    message: str,
) -> dict[str, object]:
    return {
        "schema_version": schema_version,
        "case_id": "",
        "status": "invalid_recovery_packet",
        "recommended_action": "write_new_recovery_packet_or_restart_case",
        "reason_codes": [code],
        "packet_path": str(path),
        "findings": [
            {
                "code": code,
                "severity": "hard",
                "message": message,
                "packet_path": str(path),
            }
        ],
    }


def is_invalid_recovery_packet(payload: dict[str, object]) -> bool:
    return str(payload.get("status") or "") == "invalid_recovery_packet"


def first_recovery_reason_code(payload: dict[str, object]) -> str:
    codes = payload.get("reason_codes")
    return str(codes[0]) if isinstance(codes, list) and codes else "RECOVERY_PACKET_INVALID"


__all__ = [
    "REAL_TASK_RECOVERY_SCHEMA_VERSION",
    "SUPPORTED_RECOVERY_SCHEMA_VERSIONS",
    "TASK_RECOVERY_SCHEMA_VERSION",
    "first_recovery_reason_code",
    "invalid_recovery_packet",
    "is_invalid_recovery_packet",
    "recovery_packet_payload",
]
