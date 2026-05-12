# LLM: Hierarchy scope guards keep child planning from drifting into sibling domains.
# 模块用途: 判断层级调度是否越界、超过数量/深度或创建了错误领域的 child。

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..models import SubAgentTask
from .base import _extract_write_dirs
from .hierarchy_domain_terms import (
    COORDINATION_ROLE_TOKENS,
    DOMAIN_STOPWORDS,
    FORBIDDEN_SCOPE_GENERIC_TERMS,
)
from .hierarchy_leaf_targets import LeafTargetDedupeRequest, duplicate_verified_leaf_target_warnings
from .hierarchy_write_policy import inherited_extra_write_roots
from .qa_role_contract import qa_role_identity_roles

_IMPLEMENTATION_SCAN_MAX_NODES = 64


# LLM: schedule_block_reason keeps only red-line dispatch guards; workflow shape is left to LLM/templates.
# 函数用途: 判断本轮层级调度是否因深度、数量、空计划、越权写入或领域越界被阻断，不固定每层必须是 coordinator。
def schedule_block_reason(parent: SubAgentTask, request: Any) -> str:
    if not request.child_specs:
        return "no_child_specs"
    if parent.depth + 1 > request.max_depth:
        return f"max_depth_exceeded:{request.max_depth}"
    if request.max_children > 0 and len(parent.child_ids) + len(request.child_specs) > request.max_children:
        return f"max_children_exceeded:{request.max_children}"
    mixed_reason = _mixed_coordinator_leaf_reason(request.child_specs)
    if mixed_reason:
        return mixed_reason
    drift_reason = _child_write_root_drift_reason(parent, request)
    if drift_reason:
        return drift_reason
    return _forbidden_child_scope_reason(parent, request) or _domain_mismatch_reason(parent, request)


# LLM: duplicate_child_domain_reason blocks repeated coordinator domains under the same parent.
# 函数用途: 阻止同一个父节点重复创建 checkout/quality 这类同域 coordinator，避免真实 E2E 扇出膨胀。
def duplicate_child_domain_reason(manager: Any, parent: SubAgentTask, request: Any) -> str:
    qa_phase_reason = _qa_before_implementation_reason(manager, parent, request)
    if qa_phase_reason:
        return qa_phase_reason
    seen_domains: list[set[str]] = []
    for child in _existing_coordination_children(manager, parent):
        seen_domains.append(_child_domain_tokens(child))
    for spec in request.child_specs:
        if not _is_coordination_like(spec):
            continue
        domains = _child_domain_tokens(spec)
        duplicate = _first_overlapping_domain(domains, seen_domains)
        if duplicate:
            return f"duplicate_child_domain:{duplicate}"
        if domains:
            seen_domains.append(domains)
    return ""


# LLM: schedule_warnings reports soft coordination risks while allowing the parent LLM to decide.
# 函数用途: 返回重复文件目标等可审计风险；不在底层阻断调度，避免修复/协作写同一文件时卡死。
def schedule_warnings(manager: Any, parent: SubAgentTask, request: Any) -> list[str]:
    return duplicate_verified_leaf_target_warnings(
        LeafTargetDedupeRequest(
            manager=manager,
            parent=parent,
            schedule_request=request,
            leaf_like=_is_leaf_like,
        )
    )


# LLM: _qa_before_implementation_reason keeps QA creation from becoming the only next layer.
# 函数用途: 交付型父任务还没有 worker/leaf child 时，阻断纯 tester/bug_finder/acceptor 批次，要求先创建实现节点。
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


# LLM: _existing_coordination_children reads only lightweight child task metadata.
# 函数用途: 获取当前父节点已存在的 coordinator/checker/tester 子任务；读取失败时跳过，避免破坏调度。
def _existing_coordination_children(manager: Any, parent: SubAgentTask) -> list[Any]:
    children: list[Any] = []
    for child_id in parent.child_ids:
        try:
            child = manager.load(child_id)
        except (FileNotFoundError, OSError, ValueError, TypeError):
            continue
        if _is_coordination_like(child):
            children.append(child)
    return children


# LLM: _is_coordination_like limits duplicate blocking to planner/reviewer style roles.
# 函数用途: 只给协调/测试/验收类节点做同域去重，避免多个同域 worker 被误挡。
def _is_coordination_like(item: Any) -> bool:
    text = f"{getattr(item, 'role', '')} {getattr(item, 'agent_name', '')}".lower()
    return any(token in text for token in COORDINATION_ROLE_TOKENS)


# LLM: _is_leaf_like detects implementation leaves without looking at broad goal prose.
# 函数用途: 判断 child spec 是否是执行/写作类叶子节点，用于 root 绕层创建保护。
def _is_leaf_like(item: Any) -> bool:
    text = f"{getattr(item, 'role', '')} {getattr(item, 'agent_name', '')}".lower()
    return any(token in text for token in {"leaf", "leaf_worker", "leaf-worker", "worker", "writer", "coder"})


# LLM: _child_domain_tokens extracts stable, human-named task domains from a child spec or task.
# 函数用途: 从 agent_name/role/goal 中提取 checkout、quality、catalog 等领域词，用于同父级去重。
def _child_domain_tokens(item: Any) -> set[str]:
    label_text = f"{getattr(item, 'agent_name', '')} {getattr(item, 'role', '')}".lower()
    label_tokens = _domain_tokens(label_text)
    if label_tokens:
        return label_tokens
    return _domain_tokens(_goal_domain_text(str(getattr(item, "goal", "")).lower()))


# LLM: _domain_tokens removes generic role/path words before duplicate-domain comparison.
# 函数用途: 把文本转换成领域词集合；优先使用 agent_name/role，避免共享路径导致误判。
def _domain_tokens(text: str) -> set[str]:
    tokens = re.findall(r"[a-z][a-z0-9]+", text)
    return {
        token for token in tokens
        if token not in DOMAIN_STOPWORDS and not _looks_generated_id_token(token)
    }


# LLM: _goal_domain_text strips inherited paths before goal fallback domain detection.
# 函数用途: duplicate-domain 兜底看 goal 时，去掉共享目录、文件名和继承块，避免 `/my-终端应用/...` 变成领域词。
def _goal_domain_text(text: str) -> str:
    head = re.split(r"\n\s*继承父级目标/边界|\n\s*父级必需文件/产物名|\n\s*父级禁止文件/反例名", text, maxsplit=1)[0]
    without_paths = re.sub(r"(?:~|/)[^\s，。；;、)）]+", " ", head)
    return re.sub(
        r"\b[A-Za-z0-9][A-Za-z0-9_.-]*\.(?:html?|css|js|json|md|py|txt|ya?ml)\b",
        " ",
        without_paths,
    )


# LLM: _looks_generated_id_token prevents run-id fragments from becoming business domains.
# 函数用途: 过滤 `dd1d90d8` 这类自动生成 id 片段，避免同父级不同 checker 被误判为同域重复。
def _looks_generated_id_token(token: str) -> bool:
    return any(char.isdigit() for char in token)


# LLM: _first_overlapping_domain keeps duplicate errors deterministic.
# 函数用途: 找出新任务领域和已有领域的第一个交集，返回稳定错误原因。
def _first_overlapping_domain(domains: set[str], seen_domains: list[set[str]]) -> str:
    for seen in seen_domains:
        overlap = sorted(domains & seen)
        if overlap:
            return overlap[0]
    return ""


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


# LLM: _invalid_child_write_roots compares model-proposed roots against inherited product roots literally.
# 函数用途: 找出 child goal/extra_write_roots 里不在父级产物根下的本地路径。
def _invalid_child_write_roots(spec: Any, valid_roots: list[str], internal_roots: list[str] | None = None) -> list[str]:
    invalid: list[str] = []
    for raw in [*getattr(spec, "extra_write_roots", []), *_extract_write_dirs(getattr(spec, "goal", ""))]:
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


# LLM: _forbidden_child_scope_reason enforces explicit sibling exclusions before bad children are persisted.
# 函数用途: parent goal 写明“不得创建 X”时，阻断包含 X 的下一层任务，避免错误领域 leaf 落盘。
def _forbidden_child_scope_reason(parent: SubAgentTask, request: Any) -> str:
    for term in _forbidden_scope_terms(parent):
        if _request_contains_scope_term(request.child_specs, term):
            return f"forbidden_child_scope:{term}"
    return ""


# LLM: _request_contains_scope_term keeps matching scoped to each requested child spec.
# 函数用途: 检查 child spec 自身文本是否包含被禁止的 sibling 领域词。
def _request_contains_scope_term(child_specs: list[Any], term: str) -> bool:
    return bool(term) and any(term in _child_scope_text(spec) for spec in child_specs)


# LLM: _forbidden_scope_terms extracts short machine-readable domain words from parent instructions.
# 函数用途: 从 parent goal 中提取不得创建的英文/标识符领域名，如 arithmetic/text。
def _forbidden_scope_terms(parent: SubAgentTask) -> list[str]:
    if int(parent.depth or 0) <= 0:
        return []
    text = str(parent.goal or "").lower()
    terms = re.findall(r"(?:不得|不能|不要)\s*创建\s*([a-zA-Z0-9_-]+)", text)
    return list(
        dict.fromkeys(
            term
            for raw in terms
            if (term := raw.strip("_-")) and term not in FORBIDDEN_SCOPE_GENERIC_TERMS
        )
    )


# LLM: _child_scope_text keeps forbidden-scope matching limited to the requested child spec.
# 函数用途: 合并 child 的 goal/agent_name/role，避免拿补全后的父级上下文误判。
def _child_scope_text(spec: Any) -> str:
    return f"{spec.goal} {spec.agent_name} {spec.role}".lower()


# LLM: _domain_mismatch_reason blocks child coordinators from drifting into sibling domains.
# 函数用途: parent 已经是 text/arithmetic 等单一领域时，阻断创建其它领域 child。
def _domain_mismatch_reason(parent: SubAgentTask, request: Any) -> str:
    parent_domains = _domain_terms(f"{parent.goal} {parent.agent_name} {parent.role}")
    if int(parent.depth or 0) <= 0 or not parent_domains:
        return ""
    return _first_domain_mismatch(parent_domains, request.child_specs)


# LLM: _first_domain_mismatch returns the first blocked sibling drift message.
# 函数用途: 按 child specs 顺序找第一个领域串线问题，保持错误信息稳定。
def _first_domain_mismatch(parent_domains: set[str], child_specs: list[Any]) -> str:
    for spec in child_specs:
        child_domains = _domain_terms(_child_scope_text(spec))
        if child_domains and parent_domains.isdisjoint(child_domains):
            return f"domain_mismatch:{','.join(sorted(parent_domains))}->{','.join(sorted(child_domains))}"
    return ""


# LLM: _domain_terms extracts stable task-domain words without treating every filename as a domain.
# 函数用途: 从 agent_name、leaf_worker_x、normalize_text 等命名里提取短领域词。
def _domain_terms(text: str) -> set[str]:
    lowered = str(text or "").lower()
    terms = set(re.findall(r"(?:leaf_worker|leaf-worker|leaf|lead)[_-]([a-z][a-z0-9_-]*)", lowered))
    if "arithmetic" in lowered:
        terms.add("arithmetic")
    if re.search(r"\btext\b|normalize_text", lowered):
        terms.add("text")
    generic = {"worker", "workers", "output", "outputs"}
    return {term.split("_")[0].split("-")[0] for term in terms if term and term not in generic}
