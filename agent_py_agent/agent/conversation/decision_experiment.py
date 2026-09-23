# LLM: 只读原设置和原模型账判断实验资格；不开账、不读取候选、不调用模型。当前没有可靠输入上界/发送硬门，合格授权也须失败关闭。
# 模块用途: 在实验输入准备前检查关闭、撤销、范围、版本、期限与同进程账本，保留普通决策服务原路径。
from __future__ import annotations

import time

from ..contracts.model_call_budget import ModelCallBudgetError
from ..runtime_context import current_subagent_attempt_id


# LLM: 接入点开关和一般工具权限不是实验许可；当前返回值永不授予联网资格，缺少任一原身份不得回退猜测。
# 函数用途: 返回可追踪拒绝原因和固定剩余期限；调用方应据此跳过全部实验材料准备。
def experiment_admission(agent: object, params: object, settings: dict, *, point: str = "") -> tuple[str, float]:
    from .decision_service import _identity

    if not settings["effective"]["enabled"] or not settings["effective"]["experiment_enabled"]:
        return "experiment_disabled", 0.0
    authorization = settings["experiment_authorization"]
    if authorization is None:
        return "experiment_authorization_missing", 0.0
    if authorization["status"] != "active":
        return "experiment_revoked", 0.0
    if authorization["settings_revision"] != settings["revision"]:
        return "experiment_settings_changed", 0.0
    owner, thread, run, task = _identity(agent, params)
    current_attempt, supplied_attempt = current_subagent_attempt_id(agent), getattr(params, "attempt_id", "")
    if current_attempt and supplied_attempt and current_attempt != supplied_attempt:
        return "experiment_identity_changed", 0.0
    actual = dict(zip(("owner_ref", "thread_id", "run_id", "task_id"), (owner, thread, run, task)))
    actual.update(attempt_id=current_attempt or supplied_attempt, request_id=getattr(params, "request_id", ""))
    if any(authorization["binding"][key] != value for key, value in actual.items()):
        return "experiment_identity_changed", 0.0
    if point and point not in authorization["points"]:
        return "experiment_point_forbidden", 0.0
    if time.time() >= authorization["expires_at"]:
        return "experiment_expired", 0.0
    ledger = getattr(agent, "_model_call_ledger", None)
    if ledger is None or ledger.ledger_id != authorization["binding"]["ledger_id"]:
        return "experiment_ledger_changed", 0.0
    try:
        budget = ledger.input_budget_snapshot(authorization["authorization_id"])
    except ModelCallBudgetError:
        return "experiment_budget_missing", 0.0
    if budget["status"] != "active":
        return "experiment_budget_" + budget["status"], budget["deadline"]
    # 原 Jev 只有窗口字节门，无可靠 provider-input 上界；HTTP 观察者也不是发送准入门。
    return "input_bound_unavailable", budget["deadline"]
