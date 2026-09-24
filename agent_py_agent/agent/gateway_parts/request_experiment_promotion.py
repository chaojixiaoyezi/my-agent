# LLM: 自动晋升只在用户用 /experiment apply 建立的授权内、回合正常收尾时由 Gateway 宿主执行；Jev 回答或模型工具都进不来。
#   证据规则（decision_experiment_evaluation）不通过就只写 skipped 回执；通过后在精确回合转换锁内复读设置、核对授权
#   仍是本请求那份、active、未到期、含 apply、设置 revision 与授权时一致、点仍为 off，再经原设置服务 patch 的完整 CAS 写
#   points.skill_tool.mode=apply（thread 范围）。任何冲突或用户后改都跳过、绝不覆盖；到期或撤销不回滚已晋升设置，reset 恢复继承。
#   回执权威是本请求记录的 experiment_records.promotion：先写 promoting 再改设置，已有任何回执即不再尝试（重放幂等），
#   崩溃遗留的 promoting 保持原样表示结果不确定，绝不自动重试或反向恢复。
# 模块用途: 在用户显式 apply 授权内，把满足证据规则的 skill_tool 建议一次性写成本会话设置并留下结构化回执。
from __future__ import annotations

import time

from ..conversation.decision_experiment_evaluation import SKILL_TOOL_PROPOSAL
from ..conversation.decision_policy import decision_owner_ref
from ..settings.decision_settings import execute_decision_settings_operation
from ..settings.decision_settings_schema import DecisionSettingsConflict
from ..settings.model_provider_schema import ModelProfileError
from .request_experiment import EXPERIMENT_GRANT_KEY
from .request_experiment_records import (
    request_experiment_evaluation,
    run_in_turn,
    update_experiment_records,
)

_PROMOTION_SCHEMA = "gateway_decision_experiment_promotion.v1"


# LLM: 回执只含授权编号、目标字段、状态/原因码、证据摘要（记录编号与计数）和前后值/版本，不含正文、凭据或模型回答。
# 函数用途: 生成一份晋升回执。
def _receipt(grant: dict, evaluation: dict, outcome: tuple[str, str], values: dict | None = None) -> dict:
    status, reason = outcome
    return {"schema": _PROMOTION_SCHEMA, "status": status, "reason": reason, "authorization_id": grant["authorization_id"],
            "point": SKILL_TOOL_PROPOSAL["point"], "field": SKILL_TOOL_PROPOSAL["field"],
            "scope": SKILL_TOOL_PROPOSAL["scope"], "to": SKILL_TOOL_PROPOSAL["to"],
            "evaluation": {"status": evaluation["status"], "reasons": list(evaluation["reasons"]),
                           "sample_count": evaluation["sample_count"], "comparable_count": evaluation["comparable_count"],
                           "record_ids": [sample["record_id"] for sample in evaluation["samples"]]},
            "before": (values or {}).get("before"), "after": (values or {}).get("after")}


# LLM: 只投影原设置读回里的目标字段：线程覆盖是否存在及其值、有效模式与两层 revision；不复制其它设置。
# 函数用途: 记录晋升目标字段在某一时刻的值，供回执展示前后差异。
def _field_value(view: dict) -> dict:
    overrides = view["overrides"]["thread"]
    field = SKILL_TOOL_PROPOSAL["field"]
    return {"thread_override_present": field in overrides, "thread_override": overrides.get(field),
            "effective_mode": view["effective"]["points"][SKILL_TOOL_PROPOSAL["point"]]["effective_mode"],
            "revision": dict(view["revision"])}


# LLM: 在转换锁内按顺序核对：仍是本请求的那份授权、身份一致、active、含 apply、未到期、revision 与授权时一致、
#   能力仍开启、点有效模式仍为 off。任何一项不符返回原因码（用户后改优先），空串表示可以提交。
# 函数用途: 判断当前设置下是否还能执行这次自动晋升。
def _promotion_blocker(context: object, view: dict, grant: dict) -> str:
    authorization = view["experiment_authorization"]
    if authorization is None or authorization["authorization_id"] != grant["authorization_id"]:
        return "authorization_replaced"
    if (authorization["binding"]["request_id"] != context.request_id
            or authorization["binding"]["owner_ref"] != decision_owner_ref(context.agent)):
        return "identity_changed"
    if authorization["status"] != "active":
        return "authorization_revoked"
    if "apply" not in authorization["operations"]:
        return "apply_not_authorized"
    if time.time() >= authorization["expires_at"]:
        return "authorization_expired"
    if authorization["settings_revision"] != view["revision"]:
        return "settings_changed"
    if not view["effective"]["enabled"] or not view["effective"]["experiment_enabled"]:
        return "experiment_disabled"
    if view["effective"]["points"][SKILL_TOOL_PROPOSAL["point"]]["effective_mode"] != SKILL_TOOL_PROPOSAL["from"]:
        return "point_not_off"
    return ""


# LLM: create=True 只在还没有任何晋升回执时写入；create=False 只替换同一授权的 promoting 标记。返回是否真正写入。
# 函数用途: 在本请求记录里写入或收尾一份晋升回执。
def _write_receipt(context: object, receipt: dict, *, create: bool) -> bool:
    written = False

    # LLM: 纯比较与替换，保留条目列表；任何不符都返回 None 表示不改。
    # 函数用途: 生成写入回执后的实验记录块。
    def change(block: dict) -> dict | None:
        nonlocal written
        previous = block.get("promotion")
        pending = (isinstance(previous, dict) and previous.get("status") == "promoting"
                   and previous.get("authorization_id") == receipt["authorization_id"])
        if (previous is not None) if create else not pending:
            return None
        written = True
        return {**block, "promotion": receipt}

    update_experiment_records(context, change)
    return written


# LLM: 唯一写设置的地方：原设置服务 patch、thread 范围、expected_revision 取锁内刚读到的版本（完整 CAS）。
#   冲突=没有写入（skipped）；字段/身份校验失败也在写入前；其它异常可能已写入，记 uncertain，不重试不恢复。
# 函数用途: 按原设置 CAS 把 skill_tool 改为 apply，并返回对应回执。
def _apply(context: object, view: dict, grant: dict, evaluation: dict) -> dict:
    before = {"before": _field_value(view)}
    try:
        result = execute_decision_settings_operation(
            context.agent, "patch", {"scope": "thread", "expected_revision": view["revision"],
                                     "changes": {SKILL_TOOL_PROPOSAL["field"]: SKILL_TOOL_PROPOSAL["to"]}},
            thread_id=grant["thread_id"])
    except DecisionSettingsConflict:
        return _receipt(grant, evaluation, ("skipped", "settings_conflict"), before)
    except ModelProfileError:
        return _receipt(grant, evaluation, ("skipped", "promotion_rejected"), before)
    except Exception:  # noqa: BLE001 写入可能已发生，只记不确定
        return _receipt(grant, evaluation, ("uncertain", "settings_write_uncertain"), before)
    return _receipt(grant, evaluation, ("applied", ""), {"before": _field_value(result["before"]),
                                                         "after": _field_value(result)})


# LLM: 调用方持有转换锁。规则不通过或前提不符只写 skipped；通过时先写 promoting（已有回执即放弃），再 CAS 改设置，最后收尾回执。
# 函数用途: 在一次回合收尾中至多执行一次自动晋升，并返回最终回执（未执行时为 None）。
def _promote_once(context: object, grant: dict, evaluation: dict) -> dict | None:
    if evaluation["status"] != "proposal":
        receipt = _receipt(grant, evaluation, ("skipped", "evaluation_keep_observing"))
        return receipt if _write_receipt(context, receipt, create=True) else None
    view = execute_decision_settings_operation(context.agent, "read", {"scope": "thread"}, thread_id=grant["thread_id"])
    blocker = _promotion_blocker(context, view, grant)
    if blocker:
        receipt = _receipt(grant, evaluation, ("skipped", blocker), {"before": _field_value(view)})
        return receipt if _write_receipt(context, receipt, create=True) else None
    if not _write_receipt(context, _receipt(grant, evaluation, ("promoting", "")), create=True):
        return None
    receipt = _apply(context, view, grant, evaluation)
    _write_receipt(context, receipt, create=False)
    return receipt


# LLM: 只在本请求 apply 授权回执存在时由回合收尾调用；已有晋升回执直接返回（零写入）。证据评估在锁外只读完成，
#   锁内再按设置事实核对；回合关闭/停止时转换锁抛中断，由调用方跳过。
# 函数用途: 回合结束时检查证据并在授权内自动晋升 skill_tool 设置。
def promote_skill_tool_if_ready(context: object) -> dict | None:
    grant = context.request[EXPERIMENT_GRANT_KEY]
    block = context.request.get("experiment_records")
    if isinstance(block, dict) and block.get("promotion") is not None:
        return None
    evaluation = request_experiment_evaluation(context, thread_id=grant["thread_id"])
    return run_in_turn(context, "experiment_promotion", lambda: _promote_once(context, grant, evaluation))


__all__ = ["promote_skill_tool_if_ready"]
