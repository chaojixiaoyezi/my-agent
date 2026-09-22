# LLM: 本模块只在原创建准备与物化之间提供模型建议；不创建任务、不拥有去重表，网络必须由调用方放在 creation_guard 外。
# 模块用途: 为一批尚未创建且未显式选模型的子代理准备授权候选，并在原创建入口复核后绑定建议。
from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, is_dataclass

from ...backends.decision_protocol import decision_json
from ...backends.tool_schema import tool_model_specs_to_anthropic_tools
from ...contracts.idempotency import operation_id
from ...conversation import decision_service
from ...conversation.decision_policy import connection_revision
from ...memory_archive import estimate_tokens
from ...model_guidance import provider_system_instruction
from ...settings.decision_settings import execute_decision_settings_operation
from ...settings.model_profiles import (
    _resolved_profile,
    execute_model_profile_operation,
    model_profiles_path,
    read_model_profiles,
)
from ...tooling.models import ToolRuntimeSnapshot

_MODEL_REF = "host_model_profile.v1"
_SELECTION = "host_model_decision.v1"
_CURRENT_TOOLS: ContextVar[ToolRuntimeSnapshot | None] = ContextVar("subagent_decision_tools", default=None)
_RETAIN_CHOICES = {
    "retain_original": "沿用原模型，不改变子代理创建方案。",
    "need_data": "现有客观资料不足，沿用原模型。",
    "not_needed": "无需额外模型选择，沿用原模型。",
    "no_match": "候选没有适合项，沿用原模型。",
    "abstain": "无法可靠判断，沿用原模型。",
}


# LLM: 只引用 ToolExecutor 传入的原冻结快照，不保存到共享 Agent/RunParams，也不构造第二目录；退出必须恢复原上下文。
# 函数用途: 在当前派工调用内传递真实工具 schema，使根和递归选择都能读取同一工作片事实。
@contextmanager
def bind_subagent_decision_tools(snapshot: ToolRuntimeSnapshot):
    token = _CURRENT_TOOLS.set(snapshot if isinstance(snapshot, ToolRuntimeSnapshot) else None)
    try:
        yield
    finally:
        _CURRENT_TOOLS.reset(token)


# LLM: prepared 是原 CreateRunParams/HierarchyChildSpec，不引入第二份任务 schema；reused 只能由原持久复用函数判定。
# 类用途: 指向一个已通过原校验的创建规格和对应原始输入。
@dataclass(frozen=True)
class SubagentModelInput:
    raw: dict
    prepared: object
    reused: bool = False
    canonical: object | None = None


# LLM: 快照只存公开候选和不可逆连接摘要，生命周期限于一次原创建批次；结果不是授权或可重放注册表。
# 类用途: 保存整批共用的决策阶段、逐项指纹及候选版本。
@dataclass(frozen=True)
class SubagentModelDecision:
    stage: decision_service.DecisionStage
    params: object
    children: dict
    candidates: dict
    candidates_revision: str
    questions: dict
    state: dict


# LLM: 指纹覆盖实际准备的任务、权限和输入引用，省略只用于展示的系统序号；不能用目标文本生成新的创建身份。
# 函数用途: 对已有创建规格生成仅供本批次建议复核的摘要，不落盘或改变原幂等键。
def _child_fingerprint(child: SubagentModelInput) -> str:
    item = child.canonical or child.prepared
    fields = ("goal", "role", "allowed_tools", "allowed_skills", "context_manifest", "context_packs", "attributes")
    value = {name: asdict(raw) if is_dataclass(raw := getattr(item, name, None)) else raw for name in fields}
    return hashlib.sha256(decision_json({"raw": child.raw, "prepared": value})).hexdigest()


# LLM: 原目录负责 owner/shared 和 agentic 用途授权；不探针、不上传凭据，不把未缓存能力误判为不能执行原工具。
# 函数用途: 从原公开目录取生成模型和连接摘要；真实工具支持保持未知，由原 child runner 按原协议探测。
def _candidates(agent: object, *, deadline: float) -> dict:
    public = execute_model_profile_operation(agent, "list", {})
    data = read_model_profiles(model_profiles_path(agent.home_paths))
    result = {}
    for row in public["profiles"]:
        if time.monotonic() >= deadline:
            raise TimeoutError("子代理模型候选准备已到期。")
        if row["id"] == "default" or not row.get("available") or "agentic" not in row.get("available_for", []):
            continue
        config = _resolved_profile(agent, data, row["id"])
        result[row["id"]] = {
            "revision": connection_revision(config),
            "model_name": config["model_name"],
            "model_backend": config["model_backend"],
            "context_window_tokens": config["model_context_window_tokens"],
            "capability": "agentic",
            "provider_tool_support": "unknown_until_original_runner_probe",
        }
    return result


# LLM: 工具只读当前 ToolInvocationContext 原快照，RunParams 不拥有工具协议；原 system 片段不是完整 child prompt 证明。
# 函数用途: 对纯内联子任务估算现有输入与显式输出预算，资料需后续读取时返回未知并保留原模型。
def _input_budget(agent: object, params: object, child: SubagentModelInput) -> int | None:
    prepared = child.canonical or child.prepared
    manifest = getattr(prepared, "context_manifest", {}) or {}
    if is_dataclass(manifest):
        manifest = asdict(manifest)
    if any(manifest.get(key) for key in ("required_read_paths", "hint_read_paths", "task_pack_refs", "role_pack", "quality_contract_ref")):
        return None
    packs = getattr(prepared, "context_packs", []) or []
    if any(pack.get("path") or pack.get("ref") for pack in packs):
        return None
    output = getattr(agent.config, "max_tokens", None)
    if type(output) is not int or output <= 0:
        return None
    tools = []
    if getattr(agent.config, "enable_tools", False):
        snapshot = _CURRENT_TOOLS.get()
        if snapshot is None:
            return None
        # 保守计入当前父工作片的全部已授权 schema；最终子工具范围仍由原创建权限规则决定。
        tools = tool_model_specs_to_anthropic_tools(list(snapshot.specs))
    materials = {name: getattr(prepared, name, None) for name in ("goal", "description", "thought", "plan", "context_packs")}
    instruction = provider_system_instruction(getattr(agent, "backend", None))
    decision_json({"materials": materials, "instruction": instruction, "tools": tools})
    # 这里仍是创建前估算；执行时原完整 prompt/上下文门继续独立核验，不能把模型窗口大小当任务需求。
    return estimate_tokens(materials) + estimate_tokens(instruction) + estimate_tokens(tools) + output


# LLM: 关闭、显式模型、dry-run 和原复用均不构造新操作；阶段在候选准备前冻结，整批最多一次网络请求。
# 函数用途: 在原创建锁内准备只读建议快照，任何可选准备失败都返回原创建路径。
def prepare_subagent_model_decision(agent: object, raw_params: dict, children: Callable[[], list[SubagentModelInput]]) -> SubagentModelDecision | None:
    if raw_params.get("dry_run") or getattr(agent, "home_paths", None) is None:
        return None
    params = getattr(agent, "_current_run_params", None)
    attrs = getattr(params, "task_attributes", {}) or {}
    thread_id = attrs.get("agent_thread_id") or attrs.get("conversation_thread_id") if isinstance(attrs, dict) else ""
    try:
        settings = execute_decision_settings_operation(agent, "read", {}, thread_id=thread_id, blocking=False)
        if settings["effective"]["points"]["subagent_model"]["effective_mode"] == "off":
            return None
        eligible = {str(index): child for index, child in enumerate(children()) if child.raw.get("model") is None and not child.reused}
        if not eligible:
            return None
        stage = decision_service.begin_decision_stage(agent, params, operation_id=operation_id("create_subagents", {"params": raw_params}))
        if stage.error_code or "subagent_model" not in stage.enabled_points:
            return None
        candidates = _candidates(agent, deadline=stage.deadline)
        questions, snapshots = {}, {}
        for key, child in eligible.items():
            needed = _input_budget(agent, params, child)
            if needed is None:
                child.prepared.attributes[_SELECTION] = {"status": "retained", "reason": "capacity_unknown"}
                continue
            allowed = {ref: value for ref, value in candidates.items() if value["context_window_tokens"] >= needed}
            if not allowed:
                child.prepared.attributes[_SELECTION] = {"status": "retained", "reason": "no_compatible_candidate"}
                continue
            fingerprint = _child_fingerprint(child)
            snapshots[key] = {"fingerprint": fingerprint, "candidates": list(allowed), "estimated_required_tokens": needed}
            questions[key] = {"type": "choice", "instructions": {"question": "为这个子任务选择已授权的执行模型；不确定时保留原模型。", "goal": child.prepared.goal},
                "criteria": {**{ref: {field: value for field, value in row.items() if field != "revision"} for ref, row in allowed.items()}, **_RETAIN_CHOICES}}
        if not questions:
            return None
        revision = hashlib.sha256(decision_json(candidates)).hexdigest()
        state = {"children": snapshots, "selection_scope": "new_children_only"}
        decision_json({"state": state, "questions": questions})
        return SubagentModelDecision(stage, params, snapshots, candidates, revision, questions, state)
    except (InterruptedError, decision_service.ToolCancelled):
        raise
    except Exception:
        return None


# LLM: 此函数是本模块唯一网络边界，调用方必须先释放 manager.creation_guard；原服务负责取消、预算和原账本。
# 函数用途: 对整批未显式选择的孩子请求一次建议，错误原样保留为可选失败。
def decide_subagent_models(agent: object, prepared: SubagentModelDecision) -> decision_service.DecisionOutcome:
    return decision_service.decide(agent, prepared.params, prepared.stage, point="subagent_model", state=prepared.state,
        questions=prepared.questions, candidates_revision=prepared.candidates_revision)


# LLM: 原创建锁内重新校验身份/配置/候选和逐项指纹；只改新规格的宿主模型引用，不改用户 model 或原持久任务。
# 函数用途: 将仍有效的逐项建议交给原创建流程；错误题、观察模式和变更后的候选保持继承。
def apply_subagent_model_decision(agent: object, prepared: SubagentModelDecision, outcome: decision_service.DecisionOutcome,
                                 children: list[SubagentModelInput]) -> None:
    if outcome.response is None or time.monotonic() >= prepared.stage.deadline:
        return
    try:
        current = _candidates(agent, deadline=min(prepared.stage.deadline, outcome.deadline))
        if hashlib.sha256(decision_json(current)).hexdigest() != prepared.candidates_revision:
            return
        if not decision_service.decision_outcome_is_current(agent, prepared.params, prepared.stage, outcome):
            return
        mode = outcome.mode
        for answer in outcome.response.answers:
            if time.monotonic() >= outcome.deadline:
                return
            snapshot = prepared.children.get(answer.question_id)
            if snapshot is None:
                continue
            child = children[int(answer.question_id)]
            if child.raw.get("model") is not None or child.reused or _child_fingerprint(child) != snapshot["fingerprint"]:
                continue
            if _input_budget(agent, prepared.params, child) != snapshot["estimated_required_tokens"]:
                continue
            selected = answer.value
            valid = not answer.error_code and answer.kind == "choice" and selected in snapshot["candidates"]
            if time.monotonic() >= outcome.deadline:
                return
            if outcome.may_apply and mode == "apply" and valid:
                child.prepared.attributes[_MODEL_REF] = {"profile_id": selected}
            child.prepared.attributes[_SELECTION] = {"operation_id": prepared.stage.operation_id,
                "candidates_revision": prepared.candidates_revision, "mode": mode, "status": "applied" if outcome.may_apply and mode == "apply" and valid else "retained",
                "reason": answer.error_code or (selected if isinstance(selected, str) else "invalid_answer")}
    except (InterruptedError, decision_service.ToolCancelled):
        raise
    except Exception:
        return
