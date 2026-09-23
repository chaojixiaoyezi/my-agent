# LLM: 只由已鉴权且已获用户明确授权的宿主控制 API 调用 authorize；HostCommandIdentity 只是原来源，工具一般执行权不授予实验。
# 模块用途: 将一次有限、只观察的用户许可保存到原会话设置及原模型账；没有调度器、网络、自动应用或恢复策略。
from __future__ import annotations

import time
import uuid
from dataclasses import asdict

from ..agent_core.model.call_runtime import model_call_ledger
from ..contracts.model_call_budget import ModelCallBudgetError, ModelCallInputBudget
from ..runtime_context import current_subagent_attempt_id
from ..runtime_db.host_commands import HostCommandIdentity
from .decision_experiment_schema import (
    EXPERIMENT_SCHEMA,
    experiment_identifier,
    experiment_points,
    experiment_positive_count,
    validate_experiment_authorization,
)
from .decision_settings_schema import (
    DecisionSettingsAccessError,
    DecisionSettingsConflict,
    positive_seconds,
)
from .model_provider_schema import ModelProfileError


# LLM: 宿主须先完成明确用户授权和原 operation 幂等；HostCommandIdentity 仅证明字段形状/归属，不证明用户确认或持久提交。
# 函数用途: 为未来宿主控制入口保存一次有限许可原语；当前没有产品授权入口，普通模型不注册它，保存也不授予联网资格。
def authorize_decision_experiment(context: object, params: object, *, source: HostCommandIdentity,
                                  expected_revision: dict, points: list[str], duration_seconds: float,
                                  max_http_requests: int, max_input_tokens: int) -> dict:
    from .decision_settings import _check_revision, execute_decision_settings_operation

    started, issued = time.monotonic(), time.time()
    binding = _host_experiment_binding(context, params, source)
    duration = positive_seconds(duration_seconds)
    allowed_points = experiment_points(points)
    maximum_http, maximum_input = experiment_positive_count(max_http_requests), experiment_positive_count(max_input_tokens)
    view = execute_decision_settings_operation(context, "read", {"scope": "thread"}, thread_id=binding["thread_id"])
    if not view["effective"]["enabled"] or not view["effective"]["experiment_enabled"]:
        raise DecisionSettingsAccessError("实验能力关闭，不能建立实验许可。")
    _check_revision({"expected_revision": expected_revision}, {"revision": view["revision"]["owner"]},
                    {"revision": view["revision"]["thread"]})
    existing = view["experiment_authorization"]
    if existing is not None and existing["source"]["operation_id"] == source.operation_id:
        raise DecisionSettingsConflict("原授权操作不得重新发放额度，请读取原回执。")
    ledger = model_call_ledger(context)
    binding["ledger_id"] = ledger.ledger_id
    authorization = validate_experiment_authorization({
        "schema": EXPERIMENT_SCHEMA, "authorization_id": uuid.uuid4().hex, "status": "active", "scope": "thread",
        "binding": binding, "source": {**asdict(source), "operation_id": source.operation_id},
        "points": allowed_points, "operations": ["observe"], "duration_seconds": duration,
        "issued_at": issued, "expires_at": issued + duration, "max_http_requests": maximum_http,
        "max_input_tokens": maximum_input,
        "settings_revision": {"owner": expected_revision["owner"], "thread": expected_revision["thread"] + 1},
    })
    limits = ModelCallInputBudget(authorization["authorization_id"], deadline=started + duration,
                                 max_http_requests=maximum_http, max_input_tokens=maximum_input, **binding)
    ledger.begin_input_budget(limits)
    try:
        report = execute_decision_settings_operation(context, "experiment_authorize",
            {"scope": "thread", "expected_revision": expected_revision}, thread_id=binding["thread_id"], host_authorization=authorization)
    except BaseException:
        revoke_original_experiment_budget(context, authorization)
        raise
    if existing is not None:
        revoke_original_experiment_budget(context, existing)
    return report


# LLM: 只校验宿主传入的原身份，不推断授权；runner 与 params 有冲突就拒绝，不能借普通自然语言或被测材料选目标。
# 函数用途: 取得明确用户控制来源与当前原任务的绑定，拒绝跨 owner/thread 或缺失 attempt/request。
def _host_experiment_binding(context: object, params: object, source: HostCommandIdentity) -> dict:
    from ..conversation.decision_service import _identity

    if type(source) is not HostCommandIdentity or source.owner_id != context.home_paths.owner_id:
        raise DecisionSettingsAccessError("实验授权需要当前用户的宿主显式控制来源。")
    owner, thread, run, task = _identity(context, params)
    if source.thread_id != thread:
        raise DecisionSettingsAccessError("实验授权来源不属于当前会话。")
    current = current_subagent_attempt_id(context)
    supplied = getattr(params, "attempt_id", "")
    if current and supplied and current != supplied:
        raise DecisionSettingsAccessError("实验授权的运行尝试冲突。")
    binding = dict(zip(("owner_ref", "thread_id", "run_id", "task_id"), (owner, thread, run, task)))
    binding.update(attempt_id=current or supplied, request_id=getattr(params, "request_id", ""))
    return {key: experiment_identifier(value) for key, value in binding.items()}


# LLM: 原 owner/thread 锁及完整 CAS 已由调用方持有；普通 set/unset 不触碰本信封，撤销不会生成新许可或延长期限。
# 函数用途: 在原设置事务内一次性保存或撤销完整授权，保持普通覆盖不变。
def experiment_settings_transition(current: dict, operation: str, payload: dict, authorization: dict | None, view: dict) -> dict:
    if operation == "experiment_authorize":
        authorization = validate_experiment_authorization(authorization)
        if authorization is None or not view["effective"]["enabled"] or not view["effective"]["experiment_enabled"]:
            raise DecisionSettingsAccessError("实验能力关闭或没有宿主许可。")
        expected = {"owner": view["revision"]["owner"], "thread": current["revision"] + 1}
        if authorization["binding"]["thread_id"] != view["thread_id"] or authorization["settings_revision"] != expected:
            raise DecisionSettingsAccessError("实验授权目标或设置版本不符。")
        if authorization["status"] != "active" or time.time() >= authorization["expires_at"]:
            raise DecisionSettingsAccessError("实验授权已经失效。")
    else:
        authorization = current["experiment_authorization"]
        if authorization is None or payload.get("authorization_id") != authorization["authorization_id"]:
            raise DecisionSettingsConflict("待撤销实验许可已变化，请读取当前许可。")
        authorization = {**authorization, "status": "revoked"}
    return {**current, "revision": current["revision"] + 1, "experiment_authorization": authorization}


# LLM: 设置是撤销权威，原账只关闭同代内存预留；缺账/已重启时不创建新账，更不把恢复失败改报许可仍可用。
# 函数用途: 保存撤销或新授权后停止旧预算的未来预留，保留未知和已发生用量。
def revoke_original_experiment_budget(context: object, authorization: dict | None) -> None:
    ledger = getattr(context, "_model_call_ledger", None)
    if authorization is None or ledger is None or ledger.ledger_id != authorization["binding"]["ledger_id"]:
        return
    try:
        ledger.revoke_input_budget(authorization["authorization_id"])
    except ModelCallBudgetError:
        pass
