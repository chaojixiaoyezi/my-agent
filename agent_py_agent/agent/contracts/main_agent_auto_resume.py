
# LLM: 主代理恢复预算只读宿主部署策略，请求只能收紧；台账仍按原流程记录真实尝试次数。
# 模块用途: 判断并记录主代理失败后的有界自动恢复。
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..settings.runtime_guard_config import RuntimeGuardPolicy, runtime_guard_int
from .state_machine import normalize_status

SCHEMA_VERSION = "main-agent-auto-resume-ledger.v1"
# 主代理自动恢复最多尝试的次数；超过后不再自动重试，避免故障循环消耗资源。
DEFAULT_AUTO_RECOVERY_ATTEMPT_COUNT = RuntimeGuardPolicy().main_agent_auto_resume_attempt_limit


class _RuntimeLike(Protocol):
    paths: dict[str, Path]
    request: object
    case: object
    workspace: Path


class _BundleLike(Protocol):
    runtime: _RuntimeLike
    status: str
    recovery_packet_ref: str


@dataclass(frozen=True)
class AutoResumeDecision:
    allowed: bool
    attempts: int
    max_attempts: int
    reason: str


def auto_resume_decision(bundle: _BundleLike) -> AutoResumeDecision:
    request = bundle.runtime.request
    max_attempts = auto_resume_limit(request)
    attempts = _ledger_attempts(_ledger_path(bundle.runtime.paths))
    if not bool(getattr(request, "execute", False)):
        return AutoResumeDecision(False, attempts, max_attempts, "not_execute_mode")
    if getattr(request, "recovery_packet_path", None) is not None and not bool(
        getattr(request, "auto_recovery_active", False)
    ):
        return AutoResumeDecision(False, attempts, max_attempts, "explicit_resume")
    if normalize_status(bundle.status) != "FAILED":
        return AutoResumeDecision(False, attempts, max_attempts, "status_not_failed")
    if not str(bundle.recovery_packet_ref or "").strip():
        return AutoResumeDecision(False, attempts, max_attempts, "missing_recovery_packet")
    if max_attempts > 0 and attempts >= max_attempts:
        return AutoResumeDecision(False, attempts, max_attempts, "attempts_exhausted")
    if auto_resume_remaining_timeout_seconds(bundle) <= 0:
        return AutoResumeDecision(False, attempts, max_attempts, "timeout_budget_exhausted")
    return AutoResumeDecision(True, attempts, max_attempts, "allowed")


def record_auto_resume_attempt(bundle: _BundleLike) -> dict[str, object]:
    path = _ledger_path(bundle.runtime.paths)
    payload = _read_ledger(path)
    attempts = int(payload.get("attempts", 0)) + 1
    packet_refs = [str(item) for item in payload.get("packet_refs", []) if str(item)]
    packet_refs.append(str(bundle.recovery_packet_ref))
    payload = {
        "schema_version": SCHEMA_VERSION,
        "case_id": str(getattr(bundle.runtime.case, "case_id", "") or ""),
        "attempts": attempts,
        "max_attempts": auto_resume_limit(bundle.runtime.request),
        "packet_refs": packet_refs,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return payload


# LLM: request 只能收紧宿主正上限，零不是关闭宿主保护；宿主显式0仍保留原不限次数语义。
# 函数用途: 计算主代理本次恢复请求与部署额度的交集。
def auto_resume_limit(request: object) -> int:
    configured_default = _configured_auto_resume_limit()
    value = getattr(request, "max_auto_recovery_attempts", None)
    if type(value) is not int or value < 0:
        return configured_default
    if configured_default > 0:
        return min(value, configured_default) if value else configured_default
    return value


def auto_resume_remaining_timeout_seconds(bundle: _BundleLike) -> int:
    try:
        current_budget = max(0, int(getattr(bundle.runtime.request, "task_timeout_seconds", 0)))
    except (TypeError, ValueError):
        return 0
    try:
        spent = max(0, math.ceil(float(getattr(bundle, "duration_seconds", 0.0))))
    except (TypeError, ValueError):
        spent = current_budget
    return max(0, current_budget - spent)


def _ledger_path(paths: dict[str, Path]) -> Path:
    return paths["root"] / "auto_recovery_ledger.json"


def _ledger_attempts(path: Path) -> int:
    return int(_read_ledger(path).get("attempts", 0))


def _read_ledger(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": SCHEMA_VERSION, "attempts": 0, "packet_refs": []}
    return payload if isinstance(payload, dict) else {"schema_version": SCHEMA_VERSION, "attempts": 0, "packet_refs": []}


# LLM: 字段默认唯一归 RuntimeGuardPolicy，读失败不能恢复旧 YAML 或静默关闭。
# 函数用途: 读取主代理自动恢复的部署上限。
def _configured_auto_resume_limit() -> int:
    return runtime_guard_int("main_agent_auto_resume_attempt_limit", DEFAULT_AUTO_RECOVERY_ATTEMPT_COUNT)


__all__ = [
    "AutoResumeDecision",
    "DEFAULT_AUTO_RECOVERY_ATTEMPT_COUNT",
    "SCHEMA_VERSION",
    "auto_resume_decision",
    "auto_resume_limit",
    "auto_resume_remaining_timeout_seconds",
    "record_auto_resume_attempt",
]
