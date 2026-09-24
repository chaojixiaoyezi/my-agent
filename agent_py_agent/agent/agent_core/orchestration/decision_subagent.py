# LLM: 原创建批次只产生 host-owned pending advice；容量/工具未知不冒充可用，绝不修改有效模型。网络仍在 creation_guard 外。
# 模块用途: 为新孩子批量请求语义建议并复核来源，让原创建入口只在新 thread 保存待首轮验证的建议。
from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, is_dataclass
from types import SimpleNamespace

from ...backends import get_backend
from ...backends.decision_protocol import decision_json
from ...contracts.idempotency import operation_id
from ...conversation import decision_service
from ...conversation.decision_policy import connection_revision
from ...settings.decision_settings import execute_decision_settings_operation
from ...settings.model_profiles import (
    _resolved_profile,
    execute_model_profile_operation,
    model_profile_generation,
    model_profiles_path,
    read_model_profiles,
    selected_model_config,
)
from ...settings.model_provider_schema import ModelProfileGeneration
from ...settings.services.runtime_config_task import project_task_runtime_config_overlay
from ...settings.thread_model_selection import PendingSubagentModelAdvice
from ...tooling.models import ToolRuntimeSnapshot
from ..model.context_pressure import _known_shared_window_output_reserve

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


# LLM: prepared 是原规格，canonical 可指向随后提交的同一 SubAgentTask；reused 只能由原持久复用判定，准备身份不构成授权或完整容量证明。
# 类用途: 将原始输入、当前规格和真实准备对象绑定给一次模型建议。
@dataclass(frozen=True)
class SubagentModelInput:
    raw: dict
    prepared: object
    reused: bool = False
    canonical: object | None = None


# LLM: 快照只存公开候选、结构化用户范围和不可逆连接摘要，生命周期限于一次原创建批次；结果不是授权或可重放注册表。
# 类用途: 保存整批共用的决策阶段、逐项指纹、候选范围及版本。
@dataclass(frozen=True)
class SubagentModelDecision:
    stage: decision_service.DecisionStage
    params: object
    children: dict
    candidates: dict
    candidates_revision: str
    questions: dict
    state: dict
    candidate_profile_ids: tuple[str, ...] = ()
    settings_revision: tuple[int, int] = (0, 0)


# LLM: 指纹覆盖同一准备对象的身份、实际权限和输入引用，省略只用于展示的系统序号；它只拒绝旧建议，不能生成创建身份或授权。
# 函数用途: 对已有创建规格生成仅供本批次建议复核的摘要，不落盘或改变原幂等键。
def _child_fingerprint(child: SubagentModelInput) -> str:
    item = child.canonical or child.prepared
    fields = ("id", "parent_id", "root_id", "depth", "agent_thread_id", "subagent_session_id",
              "goal", "role", "allowed_tools", "allowed_skills", "allowed_write_roots", "forbidden_write_roots",
              "effective_permissions", "context_manifest", "context_packs", "attributes")
    value = {name: asdict(raw) if is_dataclass(raw := getattr(item, name, None)) else raw for name in fields}
    return hashlib.sha256(decision_json({"raw": child.raw, "prepared": value})).hexdigest()


# LLM: 原目录负责授权；启用准备先非阻塞冻结原目录代次再解析配置，不探针。未知代次可建议但不能在首请求采用。
#   用户填写了用途标签才带 usage_tags（语义参考，不是能力证明）；它进入候选版本摘要，改标签会使在途建议失效。
# 函数用途: 从原目录取公开候选和持久版本；只有首次启用准备可迁移旧目录，复核绝不写配置。
def _candidates(agent: object, *, deadline: float, candidate_profile_ids: tuple[str, ...] = (), initialize_generation: bool = False) -> dict:
    public = execute_model_profile_operation(agent, "list", {})
    result = {}
    allowed = set(candidate_profile_ids)
    for row in public["profiles"]:
        if time.monotonic() >= deadline:
            raise TimeoutError("子代理模型候选准备已到期。")
        if (row["id"] == "default" or allowed and row["id"] not in allowed
                or not row.get("available") or "agentic" not in row.get("available_for", [])):
            continue
        generation = model_profile_generation(agent, row["id"], initialize=initialize_generation)
        config = _resolved_profile(agent, read_model_profiles(model_profiles_path(agent.home_paths)), row["id"])
        result[row["id"]] = {
            "source_model_generation": generation.to_dict() if generation is not None else None,
            "revision": connection_revision(config),
            "model_name": config["model_name"],
            "model_backend": config["model_backend"],
            "context_window_tokens": config["model_context_window_tokens"],
            "capability": "agentic",
            "provider_tool_support": "unknown_until_original_runner_probe",
            **({"usage_tags": list(row["usage_tags"])} if row.get("usage_tags") else {}),
        }
    return result


# LLM: 完整 child 首请求投影尚未接入，现有 task/父工具快照不能证明输入；这里不包含输出预留，输出必须读取逐候选真实配置。
# 函数用途: 在共享首请求输入就绪前明确返回未知，使可选模型选择保留继承，不恢复 goal/schema 粗估。
def _input_budget(agent: object, params: object, child: SubagentModelInput) -> int | None:
    return None


# LLM: 只针对原入口已确认的新任务，逐候选复用模型解析、task overlay 与原出站 cap 判据；构造后端不探针、不读 OAuth token，不应用日志或改变宿主配置。
# 函数用途: 冻结每个候选的真实配置窗口、可证明输出上限及独立工具支持缺口；0 预留在候选侧表示未知。
def _candidate_request_limits(agent: object, child: SubagentModelInput, candidates: dict, *, deadline: float) -> dict:
    result = {}
    for profile_id in candidates:
        if time.monotonic() >= deadline:
            raise TimeoutError("子代理候选配置准备已到期。")
        config = project_task_runtime_config_overlay(
            selected_model_config(agent, profile_id=profile_id), child.canonical or child.prepared,
            workspace_root=agent.subagents.workspace_root,
        )
        backend = get_backend(config.model_backend, config)
        cap = _known_shared_window_output_reserve(SimpleNamespace(config=config, backend=backend))
        result[profile_id] = {
            "runtime_config_revision": connection_revision(asdict(config)),
            "model_name": config.model_name,
            "model_backend": config.model_backend,
            "context_window_tokens": int(config.model_context_window_tokens),
            "output_cap_tokens": cap or None,
            "output_cap_status": "known" if cap else "unknown",
            "output_cap_reason": "original_http_request_cap" if cap else "not_proven_by_original_provider_contract",
            "provider_tool_support": "unknown_until_original_runner_probe" if config.enable_tools else "not_required_tools_disabled",
        }
    return result


# LLM: 关闭/显式/dry-run/复用不建议；新孩子可有未知容量，但只能产生 pending。候选窗口/cap/probe 缺口如实提供，不能据此改模型。
# 函数用途: 在原创建锁内准备一批语义建议材料；首次请求仍须由未来真实 runner 验证。
def prepare_subagent_model_decision(agent: object, raw_params: dict, children: Callable[[], list[SubagentModelInput]]) -> SubagentModelDecision | None:
    if raw_params.get("dry_run") or getattr(agent, "home_paths", None) is None:
        return None
    params = getattr(agent, "_current_run_params", None)
    attrs = getattr(params, "task_attributes", {}) or {}
    thread_id = attrs.get("agent_thread_id") or attrs.get("conversation_thread_id") if isinstance(attrs, dict) else ""
    try:
        settings = execute_decision_settings_operation(agent, "read", {}, thread_id=thread_id, blocking=False)
        point_settings = settings["effective"]["points"]["subagent_model"]
        if point_settings["effective_mode"] == "off":
            return None
        candidate_profile_ids = tuple(point_settings["candidate_profile_ids"])
        eligible = {str(index): child for index, child in enumerate(children()) if child.raw.get("model") is None and not child.reused}
        if not eligible:
            return None
        stage = decision_service.begin_decision_stage(agent, params, operation_id=operation_id("create_subagents", {"params": raw_params}))
        if stage.error_code or "subagent_model" not in stage.enabled_points:
            return None
        candidates = _candidates(agent, deadline=stage.deadline, candidate_profile_ids=candidate_profile_ids, initialize_generation=True)
        if not candidates:
            return None
        questions, snapshots = {}, {}
        for key, child in eligible.items():
            limits = _candidate_request_limits(agent, child, candidates, deadline=stage.deadline)
            needed = _input_budget(agent, params, child)
            if child.canonical is None or not getattr(child.canonical, "id", ""):
                continue
            allowed = {ref: {**row, **limits[ref]} for ref, row in candidates.items()}
            fingerprint = _child_fingerprint(child)
            snapshots[key] = {"fingerprint": fingerprint, "candidates": list(allowed), "estimated_input_tokens": needed,
                              "candidate_request_limits": limits}
            questions[key] = {"type": "choice", "instructions": {"question": "为子任务建议一个候选模型；这是待验证建议，容量和工具支持尚未验证，不确定时保留原模型。部分候选带用户填写的用途标签 usage_tags，可用来判断与子任务的语义匹配，但不是能力或容量证明。", "goal": child.prepared.goal},
                "criteria": {**{ref: {field: value for field, value in row.items() if field not in {"revision", "runtime_config_revision", "source_model_generation"}}
                                for ref, row in allowed.items()}, **_RETAIN_CHOICES}}
        if not questions:
            return None
        revision = hashlib.sha256(decision_json(candidates)).hexdigest()
        state = {"children": snapshots, "selection_scope": "new_children_pending_validation"}
        decision_json({"state": state, "questions": questions})
        return SubagentModelDecision(stage, params, snapshots, candidates, revision, questions, state, candidate_profile_ids,
                                     (settings["revision"]["owner"], settings["revision"]["thread"]))
    except (InterruptedError, decision_service.ToolCancelled):
        raise
    except Exception:
        return None


# LLM: 目录候选在创建锁内准备，锁外网络前复读用户范围/版本；变化后不为旧候选发请求或接纳建议，读取失败保持原派工。
# 函数用途: 核对这批候选仍在用户当前设置范围内，不改变配置或创建任务。
def _candidate_scope_is_current(agent: object, prepared: SubagentModelDecision) -> bool:
    try:
        settings = execute_decision_settings_operation(agent, "read", {},
            thread_id=prepared.stage.thread_id, blocking=False)
        return (tuple(settings["effective"]["points"]["subagent_model"]["candidate_profile_ids"]) == prepared.candidate_profile_ids
                and (settings["revision"]["owner"], settings["revision"]["thread"]) == prepared.settings_revision)
    except (InterruptedError, decision_service.ToolCancelled):
        raise
    except Exception:
        return False


# LLM: 此函数是本模块唯一网络边界，调用方先释放 creation_guard；发网前复核本批范围，原服务仍拥有期限、取消、用量和配置复核。
# 函数用途: 对整批未显式选择的孩子请求一次建议，设置变化或普通错误沿原创建方案。
def decide_subagent_models(agent: object, prepared: SubagentModelDecision) -> decision_service.DecisionOutcome:
    if not _candidate_scope_is_current(agent, prepared):
        return decision_service.DecisionOutcome("off", "stale", reason="candidate_scope_changed")
    return decision_service.decide(agent, prepared.params, prepared.stage, point="subagent_model", state=prepared.state,
        questions=prepared.questions, candidates_revision=prepared.candidates_revision)


# LLM: 原创建锁内核验身份、设置/连接/配置及指纹；返回 init-only typed advice，不改 profile/task attrs，不持久化原响应或进程摘要。
# 函数用途: 将本批仍有效的建议交给同一准备对象，新 thread 只保存 pending；观察/错误/变更一律不授予采用资格。
def apply_subagent_model_decision(agent: object, prepared: SubagentModelDecision, outcome: decision_service.DecisionOutcome,
                                 children: list[SubagentModelInput]) -> dict[int, PendingSubagentModelAdvice]:
    proposals = {}
    if outcome.response is None or time.monotonic() >= prepared.stage.deadline:
        return proposals
    try:
        if not _candidate_scope_is_current(agent, prepared):
            return proposals
        current = _candidates(agent, deadline=min(prepared.stage.deadline, outcome.deadline),
                              candidate_profile_ids=prepared.candidate_profile_ids)
        if hashlib.sha256(decision_json(current)).hexdigest() != prepared.candidates_revision:
            return proposals
        if not decision_service.decision_outcome_is_current(agent, prepared.params, prepared.stage, outcome):
            return proposals
        mode = outcome.mode
        for answer in outcome.response.answers:
            if time.monotonic() >= outcome.deadline:
                return {}
            snapshot = prepared.children.get(answer.question_id)
            if snapshot is None:
                continue
            child = children[int(answer.question_id)]
            if child.raw.get("model") is not None or child.reused or _child_fingerprint(child) != snapshot["fingerprint"]:
                continue
            if _input_budget(agent, prepared.params, child) != snapshot["estimated_input_tokens"]:
                continue
            if _candidate_request_limits(agent, child, current, deadline=outcome.deadline) != snapshot["candidate_request_limits"]:
                continue
            selected = answer.value
            valid = not answer.error_code and answer.kind == "choice" and selected in snapshot["candidates"]
            if time.monotonic() >= outcome.deadline:
                return {}
            if outcome.may_apply and mode == "apply" and valid:
                stage = prepared.stage
                proposals[int(answer.question_id)] = PendingSubagentModelAdvice(
                    selected, stage.operation_id, stage.owner_ref, stage.thread_id, stage.run_id, stage.task_id,
                    *prepared.settings_revision, child.canonical.id, child.canonical.agent_thread_id,
                    source_model_generation=(ModelProfileGeneration.from_dict(current[selected]["source_model_generation"])
                                             if current[selected]["source_model_generation"] is not None else None),
                )
    except (InterruptedError, decision_service.ToolCancelled):
        raise
    except Exception:
        return {}
    return proposals
