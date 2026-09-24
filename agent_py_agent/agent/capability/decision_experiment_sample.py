# LLM: 只在实验调用真正经过原账预留和结算（DecisionOutcome.experiment 有值）后构造记录；全是结构化事实：
#   身份 refs、配置版本、基线（点关闭时实际展示的工具名集合）、候选（Jev 短名单/延迟名单的宿主投影）与原账结算视图。
#   不含用户正文、题目、候选说明、模型回答、凭据或端点；realized 留空，由 Gateway 在回合结束时按工具账补写。
#   调用方：decision_recommendation._experiment_observe；写入方：gateway_parts.request_experiment_records。
# 模块用途: 为一次只观察的 skill_tool 实验生成可落进请求记录的有界对照条目。
from __future__ import annotations

import hashlib
import json

from ..conversation.decision_experiment_evaluation import EXPERIMENT_RECORD_SCHEMA
from ..runtime_context import current_subagent_attempt_id
from ..settings.decision_experiment_schema import experiment_task_id

# 基线名称只供核对，截到 64 个（与能力观测同一口径）；完整集合另以数量与摘要表达。
_BASELINE_NAME_LIMIT = 64
# 候选短名单/延迟名单要逐个判断实际调用的工具是否被保留，需尽量完整；256 覆盖常见工具规模，超出时显式标记截断。
_CANDIDATE_NAME_LIMIT = 256
_SETTLEMENT_FIELDS = ("outcome", "status", "reserved_http_requests", "max_http_requests", "charged_input_tokens",
                      "provider_input_tokens", "estimated_input_tokens", "input_bound_tokens", "max_input_tokens",
                      "unknown_usage_calls", "input_bound_kind", "input_bound_ratio", "input_bound_warning")


# LLM: 名称先排序再截断，截断与否单独返回；只接受已由原快照校验过的字符串集合。
# 函数用途: 把一组工具名变成有界、稳定排序的列表和截断标记。
def _names(values: object, limit: int) -> tuple[list[str], bool]:
    names = sorted(values or ())
    return names[:limit], len(names) > limit


# LLM: 摘要覆盖完整排序集合，截断的名单仍可用它比对是否同一基线；紧凑 JSON 保证跨进程稳定。
# 函数用途: 生成工具名集合的 sha256 摘要。
def _digest(names: list[str]) -> str:
    return hashlib.sha256(json.dumps(names, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


# LLM: 基线就是点关闭时原路径实际交给模型的工具：沿原 Registry 的 model_visible_specs 读同一冻结快照，不另算展示规则。
# 函数用途: 记录实验对照的基线（数量、名称摘要和有界名称列表）。
def _baseline(agent, params, snapshot) -> dict:
    visible = agent.tools.model_visible_specs(runtime_snapshot=snapshot, allowed_tools=params.allowed_tools)
    names = sorted({spec.name for spec in visible})
    listed, truncated = _names(names, _BASELINE_NAME_LIMIT)
    return {"variant": "point_off", "presented_count": len(names), "presented_digest": _digest(names),
            "presented_names": listed, "names_truncated": truncated}


# LLM: 候选只取宿主对 Jev 回答的原投影结果（短名单/延迟名单与 Skill 计数）；未投影时只留结构化原因，不补猜名单。
# 函数用途: 记录实验对照的候选（若按 apply 会展示和收起哪些工具）。
def _candidate(projected, reason: str) -> dict:
    if projected is None:
        return {"status": "retained" if reason.startswith("retained:") else "unavailable", "reason": reason}
    tools = projected.tool_snapshot
    shortlist, shortlist_cut = _names(tools.presentation_shortlist_names, _CANDIDATE_NAME_LIMIT)
    deferred, deferred_cut = _names(tools.presentation_deferred_names, _CANDIDATE_NAME_LIMIT)
    return {"status": "projected", "reason": "", "tool_projection": tools.presentation_shortlist_names is not None,
            "shortlist_count": len(tools.presentation_shortlist_names or ()), "shortlist_names": shortlist,
            "deferred_count": len(tools.presentation_deferred_names or ()), "deferred_names": deferred,
            "names_truncated": shortlist_cut or deferred_cut,
            "selected_skill_count": len(projected.selected_skill_ids or ())}


# LLM: 身份全部来自决策阶段与宿主参数，task 与授权/预留同一投影；call_id 指向原模型调用账中的那条记录。
# 函数用途: 生成实验记录的可追溯身份引用。
def _refs(agent, params, *, stage, settlement: dict) -> dict:
    return {"owner_ref": stage.owner_ref, "thread_id": stage.thread_id, "run_id": stage.run_id,
            "task_id": experiment_task_id(stage.task_id, stage.run_id),
            "attempt_id": current_subagent_attempt_id(agent) or str(getattr(params, "attempt_id", "") or ""),
            "request_id": str(getattr(params, "request_id", "") or ""), "operation_id": stage.operation_id,
            "call_id": str(settlement.get("call_id") or "")}


# LLM: outcome.experiment 为 None（未进入原账）时调用方不得调用本函数；结算字段是原账返回视图的白名单投影，不重读账本。
#   返回的条目 status=observed、realized=None；record_id 取原调用编号，写入方据此去重，同一次调用只落一条。
# 函数用途: 组装一次只观察实验的完整对照记录（身份、配置、基线、候选、结算）。
def experiment_sample_record(agent, params, *, stage, outcome, snapshot, candidates_revision: str,
                             question_count: int, candidate, candidate_reason: str) -> dict:
    facts = outcome.experiment
    settlement = facts["settlement"]
    return {
        "schema": EXPERIMENT_RECORD_SCHEMA, "record_id": str(settlement.get("call_id") or ""), "status": "observed",
        "point": "skill_tool", "variant": "observe", "authorization_id": facts["authorization_id"],
        "refs": _refs(agent, params, stage=stage, settlement=settlement),
        "config": {"settings_revision": dict(facts["settings_revision"]), "policy_revision": facts["policy_revision"],
                   "connection_revision": facts["connection_revision"], "candidates_revision": candidates_revision,
                   "input_bound_policy": facts["input_bound_policy"]},
        "decision": {"status": outcome.status, "reason": outcome.reason, "question_count": question_count},
        "baseline": _baseline(agent, params, snapshot),
        "candidate": _candidate(candidate, candidate_reason),
        "settlement": {key: settlement.get(key) for key in _SETTLEMENT_FIELDS},
        "realized": None,
    }


__all__ = ["experiment_sample_record"]
