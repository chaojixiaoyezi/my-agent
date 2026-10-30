
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


# LLM: 仅声明恢复判断和记账依赖的路径、请求与案件字段，不提供运行实现；策略快照由运行时对象另行携带。
# 类用途: 为主代理自动恢复合同描述最小运行时结构。
class _RuntimeLike(Protocol):
    paths: dict[str, Path]
    request: object
    case: object
    workspace: Path


# LLM: 声明运行时、状态和恢复包引用，供恢复入口以结构化字段判断；不从最终文本推断是否失败。
# 类用途: 定义自动恢复函数所消费的主代理运行结果片结构。
class _BundleLike(Protocol):
    runtime: _RuntimeLike
    status: str
    recovery_packet_ref: str


# LLM: 不可变结果同时保留是否放行、已有尝试数、有效上限和原因码；构造本身不记账或启动恢复。
# 类用途: 向主代理执行循环返回可解释的自动恢复许可判断。
@dataclass(frozen=True)
class AutoResumeDecision:
    allowed: bool
    attempts: int
    max_attempts: int
    reason: str


# LLM: 读取账本和同一运行片的策略快照，逐项检查执行模式、显式恢复、失败态、恢复包、次数与剩余时间。
#   不增加尝试次数或启动恢复；首次解析策略时可将快照绑定到运行时对象。
# 函数用途: 为主代理执行循环决定本次失败能否自动恢复并给出拒绝原因。
def auto_resume_decision(bundle: _BundleLike) -> AutoResumeDecision:
    request = bundle.runtime.request
    max_attempts = auto_resume_limit(request, policy=_bundle_policy(bundle))
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


# LLM: 读取旧账本，将尝试数加一并追加当前恢复包引用，以同一策略快照记录有效上限；直接写文件，写失败向上传播。
# 函数用途: 在主代理发起一次自动恢复时持久记录实际尝试和恢复包链。
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
        "max_attempts": auto_resume_limit(bundle.runtime.request, policy=_bundle_policy(bundle)),
        "packet_refs": packet_refs,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return payload


# LLM: request 只能收紧宿主正上限，零不是关闭宿主保护；宿主显式0仍保留原不限次数语义。
# 函数用途: 计算主代理本次恢复请求与部署额度的交集。
def auto_resume_limit(request: object, *, policy: RuntimeGuardPolicy | None = None) -> int:
    configured_default = _configured_auto_resume_limit(policy=policy)
    value = getattr(request, "max_auto_recovery_attempts", None)
    if type(value) is not int or value < 0:
        return configured_default
    if configured_default > 0:
        return min(value, configured_default) if value else configured_default
    return value


# LLM: 请求超时预算减去向上取整的本轮耗时并下限为零；预算非法直接耗尽，耗时非法按预算全耗尽处理。
# 函数用途: 为恢复许可判断计算主代理还能用于自动恢复的秒数。
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


# LLM: 只在运行根路径下拼接固定账本文件名；要求调用方提供 root，不创建目录或文件。
# 函数用途: 统一自动恢复判断和记账所使用的持久账本位置。
def _ledger_path(paths: dict[str, Path]) -> Path:
    return paths["root"] / "auto_recovery_ledger.json"


# LLM: 读取账本 attempts 并转整数，缺字段按零；字段内容无法转整数时继续抛错，不伪造已用次数。
# 函数用途: 为恢复许可判断取得账本中累计的自动恢复尝试数。
def _ledger_attempts(path: Path) -> int:
    return int(_read_ledger(path).get("attempts", 0))


# LLM: 文件不可读、JSON 损坏或根值非对象时返回初始账本；对象中的字段原样保留，不在读取时修复或写回。
# 函数用途: 为恢复判断与尝试记账加载账本，或提供零尝试的初始结构。
def _read_ledger(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": SCHEMA_VERSION, "attempts": 0, "packet_refs": []}
    return payload if isinstance(payload, dict) else {"schema_version": SCHEMA_VERSION, "attempts": 0, "packet_refs": []}


# LLM: 字段默认唯一归 RuntimeGuardPolicy，读失败不能恢复旧 YAML 或静默关闭。
# 函数用途: 读取主代理自动恢复的部署上限。
def _configured_auto_resume_limit(*, policy: RuntimeGuardPolicy | None = None) -> int:
    return runtime_guard_int("main_agent_auto_resume_attempt_limit", DEFAULT_AUTO_RECOVERY_ATTEMPT_COUNT, policy=policy)


# LLM: 优先消费当前运行、代理或代理配置的冻结对象；没有快照时首次读取并绑定到运行时，判断与记账不得各自重读。
#   返回值必须是 RuntimeGuardPolicy，否则抛出部署策略错误，不以临时默认值绕过守卫。
# 函数用途: 为同一个主代理运行片取得唯一恢复策略快照。
def _bundle_policy(bundle: _BundleLike) -> RuntimeGuardPolicy:
    runtime = bundle.runtime
    policy = getattr(runtime, "runtime_guard_policy", None)
    if policy is None:
        agent = getattr(runtime, "agent", None)
        policy = getattr(agent, "runtime_guard_policy", None)
        if policy is None:
            policy = getattr(getattr(agent, "config", None), "runtime_guard_policy", None)
        if policy is None:
            from ..settings.runtime_guard_config import runtime_guard_policy

            policy = runtime_guard_policy()
        runtime.runtime_guard_policy = policy
    if not isinstance(policy, RuntimeGuardPolicy):
        from ..settings.deployment_policy import DeploymentPolicyError

        raise DeploymentPolicyError()
    return policy


__all__ = [
    "AutoResumeDecision",
    "DEFAULT_AUTO_RECOVERY_ATTEMPT_COUNT",
    "SCHEMA_VERSION",
    "auto_resume_decision",
    "auto_resume_limit",
    "auto_resume_remaining_timeout_seconds",
    "record_auto_resume_attempt",
]
