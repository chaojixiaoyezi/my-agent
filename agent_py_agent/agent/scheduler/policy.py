# LLM: 调度策略是原 job 的显式配置，run 预留时冻结；只使用结构化时间/状态，不解析 prompt 或错误正文。
# 模块用途: 校验错过周期、排队期限和连续失败策略，并计算下一次可运行时间。
from __future__ import annotations

from .schedule import ScheduleValidationError

POLICY_DEFAULTS = {"misfire": "skip", "queue_timeout_seconds": 0,
                   "failure_pause_after": 0, "failure_backoff_seconds": 0}


# LLM: 缺失可选字段维持既有行为；未知字段、布尔数字与越界值拒绝，0 明确表示关闭限制。
# 函数用途: 规范化每项计划的运行策略，返回独立副本供原账本保存。
def normalize_policy(value: object) -> dict:
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - POLICY_DEFAULTS.keys():
        raise ScheduleValidationError("policy 含不支持的字段")
    result = {**POLICY_DEFAULTS, **value}
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
