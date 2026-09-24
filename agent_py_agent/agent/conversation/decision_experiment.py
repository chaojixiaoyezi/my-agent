# LLM: 只读原设置和原模型账判断实验资格；不开账、不调用模型。只有 v2 信封记录已接受的经验上界口径才可能放行，
# 放行也只是进入“先算上界、再预留、传输层许可复核”的链路，不是发送许可本身。
# 模块用途: 在实验输入准备前及发送前检查关闭、撤销、范围、版本、期限、账本代次与上界口径，保留普通决策服务原路径。
from __future__ import annotations

import time
from dataclasses import dataclass

from ..contracts.model_call_budget import ModelCallBudgetError
from ..runtime_context import current_subagent_attempt_id
from ..settings.decision_experiment_schema import (
    EMPIRICAL_INPUT_BOUND_POLICY,
    EXPERIMENT_SCHEMA,
    experiment_task_id,
)
from ..settings.decision_settings import execute_decision_settings_operation
from ..settings.decision_settings_projection import decision_profile
from ..settings.decision_settings_schema import POINT_RUNTIME_SCOPES
from ..settings.model_profiles import model_profiles_path, read_model_profiles
from ..settings.model_provider_schema import ModelProfileError


# LLM: 冻结同一次复读得到的设置、点行、策略版本和已授权连接；只在本次实验路由内使用，不跨调用缓存。
# 类用途: 让实验判断、复核和发送许可共用一次准入后的原配置事实。
@dataclass(frozen=True)
class ExperimentRoute:
    settings: dict
    row: dict
    revision: str
    config: dict


# LLM: 接入点开关和一般工具权限不是实验许可；缺少任一原身份不得回退猜测，v1 或未知口径永不放行。
# 函数用途: 返回可追踪拒绝原因（空串表示准入通过）和原账预算期限；调用方据此决定是否准备实验材料。
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
    actual = dict(zip(("owner_ref", "thread_id", "run_id", "task_id"), (owner, thread, run, experiment_task_id(task, run))))
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
    if authorization["schema"] != EXPERIMENT_SCHEMA:
        return "input_bound_policy_missing", budget["deadline"]
    if authorization["input_bound_policy"] != EMPIRICAL_INPUT_BOUND_POLICY:
        return "input_bound_policy_unsupported", budget["deadline"]
    return "", budget["deadline"]


# LLM: 实验只在该点普通模式为 off 时运行，与 observe/apply 的普通建议互斥；范围仍按原点登记过滤。
# 函数用途: 从已准入的授权中列出本阶段可以准备实验材料的接入点。
def experiment_stage_points(settings: dict, scope: str) -> tuple[str, ...]:
    points = settings["effective"]["points"]
    return tuple(point for point in settings["experiment_authorization"]["points"]
                 if POINT_RUNTIME_SCOPES.get(point) == scope and points[point]["effective_mode"] == "off")


# LLM: 每次都非阻塞复读原设置并重跑准入；普通模式重新开启、配置失效或未授权点都返回原因，不构造后端或读取候选。
# 函数用途: 为 decide、在途复核和发送许可提供同一套实验路由事实。
def experiment_route(agent: object, params: object, *, thread_id: str, point: str) -> tuple[str, ExperimentRoute | None]:
    from .decision_service import _policy_revision

    settings = execute_decision_settings_operation(agent, "read", {}, thread_id=thread_id, blocking=False)
    reason, _deadline = experiment_admission(agent, params, settings, point=point)
    row = settings["effective"]["points"][point]
    if not reason and row["effective_mode"] != "off":
        reason = "experiment_point_mode_not_off"
    if reason:
        return reason, None
    try:
        config = decision_profile(agent, read_model_profiles(model_profiles_path(agent.home_paths)), row["profile_id"])
    except ModelProfileError:
        return "configuration_unavailable", None
    return "", ExperimentRoute(settings, row, _policy_revision(settings), config)


# LLM: 与普通 _stale 分开：普通路径把 off 视为关闭，实验路径要求 off；连接版本按当前 profile 重算，旧快照不能放行。
# 函数用途: 判断冻结的实验请求在发送前后是否仍对应当前授权、策略和连接，返回失效原因或空串。
def experiment_current(agent: object, params: object, *, thread_id: str, point: str, revision: str,
                       connection: str) -> str:
    from .decision_policy import connection_revision

    reason, route = experiment_route(agent, params, thread_id=thread_id, point=point)
    if reason:
        return reason
    if route.revision != revision:
        return "policy_changed"
    return "" if connection_revision(route.config) == connection else "connection_changed"
