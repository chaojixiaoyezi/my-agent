# LLM: Hierarchy scope guards keep child planning from drifting into sibling domains.
# 模块用途: 判断层级调度是否越界、超过数量/深度或创建了错误领域的 child。

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..models import SubAgentTask
from ..role_templates import role_template_id_for_role
from .hierarchy_duplicate_domains import duplicate_child_domain_warnings
from .hierarchy_leaf_targets import LeafTargetDedupeRequest, duplicate_verified_leaf_target_warnings
from .hierarchy_scope_domains import domain_mismatch_reason, forbidden_child_scope_reason
from .hierarchy_write_policy import inherited_extra_write_roots
from .qa_role_contract import qa_role_identity_roles
from .repair_contract_identity import repair_contract_identity_from_context_packs

_IMPLEMENTATION_SCAN_MAX_NODES = 64
_ACTIVE_DUPLICATE_STATUSES = {"PLANNING", "RUNNING", "BLOCKED", "AWAITING_ACCEPTANCE"}


# LLM: schedule_block_reason keeps only red-line dispatch guards; workflow shape is left to LLM/templates.
# 函数用途: 判断本轮层级调度是否因深度、数量、空计划、越权写入或领域越界被阻断，不固定每层必须是 coordinator。
def schedule_block_reason(parent: SubAgentTask, request: Any) -> str:
    if not request.child_specs:
        return "no_child_specs"
    # LLM: max_depth=0 means unlimited; explicit positive values remain an override for tests/strict workflows.
    if request.max_depth > 0 and parent.depth + 1 > request.max_depth:
        return f"max_depth_exceeded:{request.max_depth}"
    if request.max_children > 0 and len(parent.child_ids) + len(request.child_specs) > request.max_children:
        return f"max_children_exceeded:{request.max_children}"
    drift_reason = _child_write_root_drift_reason(parent, request)
    if drift_reason:
        return drift_reason
    if scope_reason := forbidden_child_scope_reason(parent, request):
        return scope_reason
    return domain_mismatch_reason(parent, request)


# LLM: qa_phase_block_reason no longer enforces workflow order.
# 函数用途: QA 先后顺序交给父级/LLM 和 closeout；这里只保留兼容入口，不再阻断调度。
def qa_phase_block_reason(manager: Any, parent: SubAgentTask, request: Any) -> str:
    return ""


# LLM: active_duplicate_child_reason prevents recovery loops from spawning endless same-purpose QA repairs.
# 函数用途: 同父级已有活跃 QA/修复/验收类同名 child 时，阻断再次创建并提示复用或 takeover 现有 run。
def active_duplicate_child_reason(manager: Any, parent: SubAgentTask, request: Any) -> str:
    for spec in request.child_specs:
        if not _is_recovery_quality_like(spec):
            continue
        duplicate_id = _active_duplicate_child_id(manager, parent, spec)
        if duplicate_id:
            return f"active_duplicate_child:{duplicate_id};reuse_existing_run_or_takeover"
    return ""


# LLM: schedule_warnings reports soft coordination risks while allowing the parent LLM to decide.
# 函数用途: 返回重复文件目标等可审计风险；不在底层阻断调度，避免修复/协作写同一文件时卡死。
def schedule_warnings(manager: Any, parent: SubAgentTask, request: Any) -> list[str]:
    warnings: list[str] = []
    if qa_reason := _qa_before_implementation_reason(manager, parent, request):
        warnings.append(qa_reason)
    if mixed_reason := _mixed_coordinator_leaf_reason(request.child_specs):
        warnings.append(mixed_reason)
    warnings.extend(duplicate_verified_leaf_target_warnings(
        LeafTargetDedupeRequest(
            manager=manager,
            parent=parent,
            schedule_request=request,
            leaf_like=_is_leaf_like,
        )
    ))
    warnings.extend(duplicate_child_domain_warnings(manager, parent, request))
    return warnings


# LLM: _qa_before_implementation_reason reports empty-QA risk without blocking creation.
# 函数用途: 交付型父任务还没有 ready worker/leaf child 时给 warning，父级决定是否仍要先派 QA。
def _qa_before_implementation_reason(manager: Any, parent: SubAgentTask, request: Any) -> str:
    if not _parent_has_product_root(parent):
        return ""
    if not request.child_specs or not any(_is_qa_like(spec) for spec in request.child_specs):
        return ""
    if _ready_implementation_children(manager, parent):
        return ""
    return "qa_before_implementation_ready:create and finish worker/writer/leaf_worker before QA children"


# LLM: _parent_has_product_root uses explicit write roots as the stable signal for deliverable work.
# 函数用途: 判断父任务是否是有产物根的交付任务；没有产物根的研究/检查任务不受 QA 阶段门限制。
def _parent_has_product_root(parent: SubAgentTask) -> bool:
    return bool(inherited_extra_write_roots(parent))


# LLM: _is_qa_like checks explicit role/name identity, not broad inherited goal prose.
# 函数用途: 判断 child spec 是否是 tester、bug_finder 或 acceptor 这类 QA 角色。
def _is_qa_like(item: Any) -> bool:
    return bool(qa_role_identity_roles(role=str(getattr(item, "role", "")), agent_name=str(getattr(item, "agent_name", ""))))


# LLM: _active_duplicate_child_id scans siblings only, so unrelated branches can still run parallel repairs.
# 函数用途: 找到同父级、同名且仍活跃的 QA/修复类子任务；DONE/VERIFIED 任务不挡后续显式返工。
def _active_duplicate_child_id(manager: Any, parent: SubAgentTask, spec: Any) -> str:
    wanted_name = _normalized_agent_name(getattr(spec, "agent_name", ""))
    if not wanted_name:
        return ""
    # LLM: repair contracts move duplicate handling into the idempotency layer instead of blocking first.
    # 函数用途: 带 repair_contract 的修复任务按机器 scope 复用或新建，不被固定中文名字提前拦死。
    if repair_contract_identity_from_context_packs(getattr(spec, "context_packs", [])):
        return ""
    spec_is_repair = _is_repair_like(spec) or _identity_is_repair_worker(spec)
    for child_id in parent.child_ids:
        try:
            child = manager.load(child_id)
        except (FileNotFoundError, OSError, ValueError, TypeError):
            continue
        if not _is_active_child(child) or not _is_recovery_quality_like(child):
            continue
        if spec_is_repair and (_is_repair_like(child) or _identity_is_repair_worker(child)):
            return str(getattr(child, "id", "") or child_id)
        if _normalized_agent_name(getattr(child, "agent_name", "")) == wanted_name:
            return str(getattr(child, "id", "") or child_id)
    return ""


# LLM: _is_active_child treats blocked/running/awaiting-acceptance runs as recoverable work, not fresh slots.
# 函数用途: 判断已有子任务是否仍需要继续、接管或验收；完成的任务不算活跃重复。
def _is_active_child(item: Any) -> bool:
    status = str(getattr(item, "status", "") or "").upper()
    verification = str(getattr(item, "verification_status", "") or "").upper()
    if verification == "VERIFIED":
        return False
    return status in _ACTIVE_DUPLICATE_STATUSES


# LLM: _is_recovery_quality_like narrows duplicate blocking to structured QA/recovery roles.
# 函数用途: 只给 QA 模板角色或带 repair_contract 的任务做活跃同名去重，避免从 goal 自然语言猜。
def _is_recovery_quality_like(item: Any) -> bool:
    role = _template_role_identity(item)
    return bool(
        role in {"tester", "bug_finder", "acceptor"}
        or repair_contract_identity_from_context_packs(getattr(item, "context_packs", []))
        or _identity_is_repair_worker(item)
    )


# LLM: _is_repair_like checks structured repair contracts only.
# 函数用途: 判断任务是否带 repair_contract；同父级活跃 repair 只能继续或接管，不能靠换名无限新增。
def _is_repair_like(item: Any) -> bool:
    return bool(repair_contract_identity_from_context_packs(getattr(item, "context_packs", [])))


# LLM: _identity_is_repair_worker treats explicit repair role/name ids as recovery work.
# 函数用途: qa-repair-worker 这类结构化名字进入活跃去重；不从 goal 的“修复”自然语言猜。
def _identity_is_repair_worker(item: Any) -> bool:
    text = f"{getattr(item, 'role', '')} {getattr(item, 'agent_name', '')}".lower().replace("_", "-")
    return "repair" in text


# LLM: _normalized_agent_name compares model-selected role names without freezing user-facing Chinese prefixes.
# 函数用途: 去掉空白并小写化 agent_name；命名不同的 worker/QA 仍允许并行存在。
def _normalized_agent_name(value: Any) -> str:
    compact = re.sub(r"\s+", "", str(value or "").strip().lower())
    return re.sub(r"^(?:小+傻妞[-_:：]*)+", "", compact)


# LLM: _ready_implementation_children scans persisted descendants so QA can follow delegated production chains.
# 函数用途: 判断父节点子树里是否已有 worker/writer/leaf/coder 进入可验收或完成状态；否则不允许提前创建 QA。
def _ready_implementation_children(manager: Any, parent: SubAgentTask) -> list[Any]:
    ready: list[Any] = []
    queue = [str(item) for item in parent.child_ids if item]
    seen: set[str] = set()
    scanned = 0
    while queue and scanned < _IMPLEMENTATION_SCAN_MAX_NODES:
        child_id = queue.pop(0)
        if child_id in seen:
            continue
        seen.add(child_id)
        scanned += 1
        try:
            child = manager.load(child_id)
        except (FileNotFoundError, OSError, ValueError, TypeError):
            continue
        if _is_leaf_like(child) and _implementation_child_ready(child):
            ready.append(child)
        queue.extend(str(item) for item in getattr(child, "child_ids", []) or [] if str(item) not in seen)
    return ready


# LLM: _implementation_child_ready keeps QA creation behind an actual implementation checkpoint.
# 函数用途: worker/leaf 至少等待验收或已完成时，QA 子任务才有真实产物/证据可检查。
def _implementation_child_ready(child: Any) -> bool:
    status = str(getattr(child, "status", "") or "").upper()
    verification = str(getattr(child, "verification_status", "") or "").upper()
    return status in {"AWAITING_ACCEPTANCE", "DONE"} or verification in {"NEEDS_ACCEPTANCE", "VERIFIED"}


# LLM: _is_leaf_like detects implementation leaves from structured role/name ids.
# 函数用途: 判断 child spec 是否是执行/写作类叶子节点，用于 root 绕层创建保护。
def _is_leaf_like(item: Any) -> bool:
    text = f"{getattr(item, 'role', '')} {getattr(item, 'agent_name', '')}".lower()
    role = _template_role_identity(item)
    return role in {"worker", "writer", "researcher"} or any(token in text for token in {"leaf", "leaf_worker", "leaf-worker", "coder"})


# LLM: _mixed_coordinator_leaf_reason blocks one call from flattening a planned hierarchy.
# 函数用途: 同一次层级创建里不能既建 coordinator 又建 leaf，避免模型绕过“上层先创建下层领导”的职责链。
def _mixed_coordinator_leaf_reason(child_specs: list[Any]) -> str:
    has_coordinator = any(_child_has_role_token(spec, {"coordinator", "lead"}) for spec in child_specs)
    has_leaf = any(_child_has_role_token(spec, {"leaf", "leaf_worker", "leaf-worker"}) for spec in child_specs)
    return "mixed_coordinator_leaf_children" if has_coordinator and has_leaf else ""


# LLM: _child_write_root_drift_reason blocks model-invented sibling output paths before child runs exist.
# 函数用途: parent 已有权威产物根时，child 不能把 build 猜成 sibling 目录后继续落盘。
def _child_write_root_drift_reason(parent: SubAgentTask, request: Any) -> str:
    valid_roots = inherited_extra_write_roots(parent)
    if not valid_roots:
        return ""
    for spec in request.child_specs:
        invalid = _invalid_child_write_roots(spec, valid_roots, _internal_context_roots(parent))
        if invalid:
            return (
                "child_write_root_drift:"
                f"invalid_write_roots={invalid};"
                f"valid_inherited_write_roots={valid_roots};"
                "rewrite child goal with the exact inherited root"
            )
    return ""


# LLM: _invalid_child_write_roots compares structured child roots against inherited product roots literally.
# 函数用途: 找出 child extra_write_roots 里不在父级产物根下的本地路径；不从普通 goal 文本猜路径。
def _invalid_child_write_roots(spec: Any, valid_roots: list[str], internal_roots: list[str] | None = None) -> list[str]:
    invalid: list[str] = []
    for raw in getattr(spec, "extra_write_roots", []):
        text = str(raw or "").rstrip("/")
        if (
            text
            and not _is_under_any_write_root(text, valid_roots)
            and not _is_under_any_write_root(text, internal_roots or [])
            and text not in invalid
        ):
            invalid.append(text)
    return invalid


# LLM: _internal_context_roots prevents task/run workspace references from looking like product-root drift.
# 函数用途: child goal 可以提到父级 task_dir 或 agent workspace 作为上下文，但这些内部目录不能被当成用户产物根漂移。
def _internal_context_roots(parent: SubAgentTask) -> list[str]:
    roots: list[str] = []
    for field_name in ("task_dir", "task_workspace_dir", "agent_run_workspace_dir"):
        value = str(getattr(parent, field_name, "") or "").rstrip("/")
        if value and value not in roots:
            roots.append(value)
    return roots


# LLM: _is_under_any_write_root treats non-existing files/directories as path facts without touching disk.
# 函数用途: 判断候选路径是否等于或位于任一权威产物根下面。
def _is_under_any_write_root(candidate: str, roots: list[str]) -> bool:
    path = Path(candidate).expanduser().resolve(strict=False)
    for raw in roots:
        root = Path(str(raw)).expanduser().resolve(strict=False)
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


# LLM: _child_has_role_token keeps hierarchy role checks limited to explicit role/agent labels.
# 函数用途: 判断 child spec 是否属于 coordinator 或 leaf 类角色；不看 goal，避免“创建 leaf 的 coordinator”被误判为 leaf。
def _child_has_role_token(spec: Any, tokens: set[str]) -> bool:
    text = f"{spec.agent_name} {spec.role}".lower()
    return any(token in text for token in tokens)


# LLM: _template_role_identity maps role/name through the active template catalog.
# 函数用途: 用模板 id 判断角色类别，替代中文/英文自然语言职责词。
def _template_role_identity(item: Any) -> str:
    identity = f"{getattr(item, 'role', '')} {getattr(item, 'agent_name', '')}"
    return role_template_id_for_role(identity, fallback="")
