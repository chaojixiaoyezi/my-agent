# LLM: 调度策略是原 job 的显式配置，run 预留时冻结；只使用结构化时间/状态，不解析 prompt 或错误正文。
# 模块用途: 校验错过周期、排队期限和连续失败策略，并计算下一次可运行时间。
from __future__ import annotations

from .schedule import ScheduleValidationError

POLICY_DEFAULTS = {"misfire": "skip", "queue_timeout_seconds": 0,
                   "failure_pause_after": 0, "failure_backoff_seconds": 0,
                   "on_failure": None}


def _normalize_on_failure(value: object) -> dict | None:
    """规范化并校验策略中的 on_failure 失败调查与单次重试配置。"""
    if value is None:
        return None
    allowed_keys = frozenset({"investigate", "after_failures", "retry_once", "prompt"})
    if not isinstance(value, dict):
        raise ScheduleValidationError(
            "policy.on_failure 必须是对象（dict）或 null；例如 "
            '{"investigate": true, "after_failures": 1, "retry_once": false}'
        )
    unknown = sorted(set(value) - allowed_keys)
    if unknown:
        raise ScheduleValidationError(
            f"policy.on_failure 含不支持的字段: {unknown}；仅支持 {sorted(allowed_keys)}"
        )
    investigate = value.get("investigate", False)
    if type(investigate) is not bool:
        raise ScheduleValidationError("policy.on_failure.investigate 必须是布尔值 (true/false)")
    after_failures = value.get("after_failures", 1)
    if type(after_failures) is not int or after_failures < 1 or after_failures > 100:
        raise ScheduleValidationError("policy.on_failure.after_failures 必须是 1 到 100 之间的整数")
    retry_once = value.get("retry_once", False)
    if type(retry_once) is not bool:
        raise ScheduleValidationError("policy.on_failure.retry_once 必须是布尔值 (true/false)")
    prompt = value.get("prompt", "")
    if not isinstance(prompt, str):
        raise ScheduleValidationError("policy.on_failure.prompt 必须是字符串")
    if len(prompt) > 2000:
        raise ScheduleValidationError(
            f"policy.on_failure.prompt 长度不能超过 2000 字符（当前 {len(prompt)} 字符）"
        )
    return {
        "investigate": investigate,
        "after_failures": after_failures,
        "retry_once": retry_once,
        "prompt": prompt.strip(),
    }


# LLM: 缺失可选字段维持既有行为；未知字段、布尔数字与越界值拒绝，0 明确表示关闭限制。
# 函数用途: 规范化每项计划的运行策略，返回独立副本供原账本保存。
def normalize_policy(value: object) -> dict:
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - POLICY_DEFAULTS.keys():
        raise ScheduleValidationError("policy 含不支持的字段")
    result = {**POLICY_DEFAULTS, **value}
    result["on_failure"] = _normalize_on_failure(result.get("on_failure"))
    if result["misfire"] not in ("skip", "coalesce"):
        raise ScheduleValidationError("policy.misfire 必须是 skip 或 coalesce")
    for key in ("queue_timeout_seconds", "failure_pause_after", "failure_backoff_seconds"):
        number = result[key]
        if type(number) is not int or not 0 <= number <= 604800:
            raise ScheduleValidationError(f"policy.{key} 必须是 0 到 604800 的整数")
    return result


# LLM: 队列期限固定于原 run 预留时间；已实际开始的执行、等待孩子或重投冻结结果不受排队期限影响。
# 函数用途: 判断尚未开始的定时执行是否已经等候过久。
def queue_expired(run: dict, now: float) -> bool:
    deadline = float(run.get("queue_deadline_at") or 0)
    return bool(deadline and now >= deadline and not run.get("started_at"))


# LLM: 失败退避只延后未来新 run，不重放失败或 unknown 执行；只有明确 done 重置连续失败数，跳过不等于业务成功。
# 函数用途: 把同次终态写入转成计数、退避或暂停事实，保留手动暂停/删除状态。
def apply_run_policy(job: dict, run: dict, *, now: float) -> dict:
    if run.get("trigger") == "failure_investigation":
        return dict(job)
    updated = dict(job)
    status = run.get("status")
    if status == "done":
        updated.update(consecutive_failures=0, backoff_until=0.0)
        return updated
    if status not in {"failed", "unknown"}:
        return updated
    failures = int(job.get("consecutive_failures") or 0) + 1
    policy = normalize_policy(job.get("policy"))
    delay = min(604800, policy["failure_backoff_seconds"] * 2 ** min(failures - 1, 16))
    updated.update(consecutive_failures=failures, backoff_until=now + delay if delay else 0.0)
    if job.get("status") == "active" and policy["failure_pause_after"] and failures >= policy["failure_pause_after"]:
        updated.update(status="paused", paused_at=now, paused_reason="consecutive_failures")
    return updated
