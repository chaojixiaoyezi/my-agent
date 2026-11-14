from __future__ import annotations

"""Requirement provenance, review, and acceptance records (ORCH-05).

LLM: this module defines structural dataclasses and honesty invariants linking
requirements, input versions, verification evidence, independent reviews, and
acceptance verdicts. It never parses natural language to gate tool execution.
模块用途: 结构化记录需求绑定、输入版本、单测范围、独立评审与最终验收链条及失效校验。
"""

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

ObservationScope = Literal["module_only", "focused_integration", "full_candidate", "unverified"]
ReviewStatus = Literal["missing", "pending", "approved", "changes_requested", "stale"]
AcceptanceStatus = Literal["unaccepted", "accepted", "invalidated", "rejected"]
ReviewVerdict = Literal["pass", "changes_requested"]


# LLM: 需求溯源核心异常基类。
# 类用途: 溯源模块所有异常的通用基类。
class ProvenanceError(Exception):
    """Base error for requirement provenance operations."""


# LLM: 独立评审中作者自评被严格禁止（SELF_REVIEW_FORBIDDEN）。
# 类用途: 评审人与作者相同或为空时的拒绝异常。
class SelfReviewForbiddenError(ProvenanceError):
    """Raised when author attempts to perform independent review on their own work."""


# LLM: 观测范围诚实性违背（禁止在缺少集成测试或未经通过的独立评审时虚假升级到 full_candidate）。
# 类用途: 范围诚实性校验失败异常。
class ObservationScopeHonestyError(ProvenanceError):
    """Raised when attempting to claim full_candidate scope without integrated evidence and review."""


# LLM: 验收基于过期版本或失效证据时失败。
# 类用途: 验收输入版本与需求当前版本不匹配时的拒绝异常。
class StaleInputVersionError(ProvenanceError):
    """Raised when acceptance or verification links against a stale input version."""


# LLM: 需求溯源绑定记录（冻结不可变对象）。
# 类用途: 存储一个需求项在特定输入版本、单测范围、独立评审与验收状态的完整快照。
@dataclass(frozen=True)
class RequirementProvenanceRecord:
    requirement_id: str
    owner_ref: str
    task_id: str
    root: str
    input_version: str
    watched_paths: tuple[str, ...]
    verification_event_ids: tuple[int, ...] = ()
    observation_scope: ObservationScope = "unverified"
    review_status: ReviewStatus = "missing"
    acceptance_status: AcceptanceStatus = "unaccepted"
    invalidation_reason: str = ""
    author_principal_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    verification_invalidated_event_id: int = field(default=0, kw_only=True)

    # LLM: 转字典安全拷贝，便于序列化为 JSON。
    # 函数用途: 将需求溯源记录导出为基础数据字典。
    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["watched_paths"] = list(self.watched_paths)
        data["verification_event_ids"] = list(self.verification_event_ids)
        data["current_verification_event_ids"] = [identity for identity in self.verification_event_ids
                                                  if identity > self.verification_invalidated_event_id]
        return data

    # LLM: 从持久化字典构造不可变对象。
    # 函数用途: 反序列化需求溯源快照字典。
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RequirementProvenanceRecord:
        watched = tuple(str(p) for p in data.get("watched_paths", ()) if str(p).strip())
        event_ids_raw = data.get("verification_event_ids", ())
        event_ids: list[int] = []
        for raw_id in event_ids_raw:
            try:
                event_ids.append(int(raw_id))
            except (TypeError, ValueError):
                continue
        return cls(
            requirement_id=str(data.get("requirement_id") or "").strip(),
            owner_ref=str(data.get("owner_ref") or "").strip(),
            task_id=str(data.get("task_id") or "").strip(),
            root=str(data.get("root") or "").strip(),
            input_version=str(data.get("input_version") or "").strip(),
            watched_paths=watched,
            verification_event_ids=tuple(event_ids),
            verification_invalidated_event_id=int(data.get("verification_invalidated_event_id", 0)),
            observation_scope=data.get("observation_scope", "unverified"),  # type: ignore[arg-type]
            review_status=data.get("review_status", "missing"),  # type: ignore[arg-type]
            acceptance_status=data.get("acceptance_status", "unaccepted"),  # type: ignore[arg-type]
            invalidation_reason=str(data.get("invalidation_reason") or "").strip(),
            author_principal_id=str(data.get("author_principal_id") or "").strip(),
            created_at=str(data.get("created_at") or "").strip(),
            updated_at=str(data.get("updated_at") or "").strip(),
        )


# LLM: 独立评审记录（要求 reviewer != author，记录审查输入版本与结论）。
# 类用途: 保存由独立主体对某需求特定版本执行的评审结论及关联证据。
@dataclass(frozen=True)
class IndependentReviewRecord:
    review_id: str
    requirement_id: str
    reviewer_principal_id: str
    author_principal_id: str
    reviewed_input_version: str
    verdict: ReviewVerdict
    scope: str
    evidence_refs: tuple[str, ...] = ()
    created_at: str = ""
    owner_ref: str = ""

    # LLM: 转字典安全拷贝，便于序列化为 JSON。
    # 函数用途: 将独立评审记录导出为基础数据字典。
    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["evidence_refs"] = list(self.evidence_refs)
        return data

    # LLM: 从字典构造不可变独立评审对象。
    # 函数用途: 反序列化独立评审记录字典。
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IndependentReviewRecord:
        refs = tuple(str(r) for r in data.get("evidence_refs", ()) if str(r).strip())
        return cls(
            review_id=str(data.get("review_id") or "").strip(),
            requirement_id=str(data.get("requirement_id") or "").strip(),
            reviewer_principal_id=str(data.get("reviewer_principal_id") or "").strip(),
            author_principal_id=str(data.get("author_principal_id") or "").strip(),
            reviewed_input_version=str(data.get("reviewed_input_version") or "").strip(),
            verdict=data.get("verdict", "changes_requested"),  # type: ignore[arg-type]
            scope=str(data.get("scope") or "independent_review").strip(),
            evidence_refs=refs,
            created_at=str(data.get("created_at") or "").strip(),
            owner_ref=str(data.get("owner_ref") or "").strip(),
        )


# LLM: 阶段与最终验收事实记录。
# 类用途: 保存协调者或授权主体对需求项做出的阶段或最终验收决策事实。
@dataclass(frozen=True)
class AcceptanceRecord:
    acceptance_id: str
    requirement_id: str
    accepted_by_principal_id: str
    accepted_input_version: str
    verdict: Literal["accepted", "invalidated", "rejected", "changes_requested"] = "accepted"
    observation_scope: str = ""
    review_id: str = ""
    evidence_refs: tuple[str, ...] = ()
    created_at: str = ""
    owner_ref: str = ""

    # LLM: 转字典安全拷贝，便于序列化为 JSON。
    # 函数用途: 将验收记录导出为基础数据字典。
    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["evidence_refs"] = list(self.evidence_refs)
        return data

    # LLM: 从字典构造不可变验收记录。
    # 函数用途: 反序列化验收事实记录字典。
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AcceptanceRecord:
        refs = tuple(str(r) for r in data.get("evidence_refs", ()) if str(r).strip())
        return cls(
            acceptance_id=str(data.get("acceptance_id") or "").strip(),
            requirement_id=str(data.get("requirement_id") or "").strip(),
            accepted_by_principal_id=str(data.get("accepted_by_principal_id") or "").strip(),
            accepted_input_version=str(data.get("accepted_input_version") or "").strip(),
            verdict=data.get("verdict", "accepted"),  # type: ignore[arg-type]
            observation_scope=str(data.get("observation_scope") or "").strip(),
            review_id=str(data.get("review_id") or "").strip(),
            evidence_refs=refs,
            created_at=str(data.get("created_at") or "").strip(),
            owner_ref=str(data.get("owner_ref") or "").strip(),
        )


# LLM: 规范化单条路径，相对路径依据 root 展开，消除尾部斜杠与冗余分段。
# 函数用途: 对文件路径执行规范化解析。
def normalize_path(path: str | Path, root: str | Path = "") -> str:
    raw = str(path or "").strip()
    if not raw:
        return ""
    p = Path(raw).expanduser()
    if not p.is_absolute() and root:
        root_path = Path(root).expanduser().resolve(strict=False)
        p = root_path / p
    return str(p.resolve(strict=False)).replace("\\", "/")


# LLM: 规范化路径列表并去重排序，保持输出稳定有序。
# 函数用途: 将输入路径序列转换为标准化的不可变元组。
def normalize_paths(
    paths: list[str] | tuple[str, ...] | set[str],
    root: str | Path = "",
) -> tuple[str, ...]:
    cleaned = {normalize_path(p, root) for p in paths if str(p).strip()}
    return tuple(sorted(p for p in cleaned if p))


# LLM: 检查监控路径与被编辑路径是否相交（包含精确命中与目录层级包含关系）。
# 函数用途: 判断一个已修改文件是否落在某个监控路径或其子树内。
def path_intersects(watched_path: str, edited_path: str) -> bool:
    w = watched_path.strip().replace("\\", "/")
    e = edited_path.strip().replace("\\", "/")
    if not w or not e:
        return False
    if w == e:
        return True
    w_dir = w.rstrip("/") + "/"
    e_dir = e.rstrip("/") + "/"
    return e.startswith(w_dir) or w.startswith(e_dir)


# LLM: 独立评审防自评断言：评审人与作者绝对不能相同，且均不可为空。
# 函数用途: 校验独立评审主体的合法性，防止同人自评通过。
def validate_independent_review(reviewer_principal_id: str, author_principal_id: str) -> None:
    reviewer = reviewer_principal_id.strip()
    author = author_principal_id.strip()
    if not reviewer:
        raise SelfReviewForbiddenError("SELF_REVIEW_FORBIDDEN: reviewer_principal_id cannot be empty")
    if not author:
        raise SelfReviewForbiddenError("SELF_REVIEW_FORBIDDEN: author_principal_id cannot be empty")
    if reviewer == author:
        raise SelfReviewForbiddenError(
            f"SELF_REVIEW_FORBIDDEN: reviewer '{reviewer}' cannot be identical to author '{author}'"
        )


# LLM: 校验升级为 full_candidate 的前置条件：必须同时具备全量集成测试与匹配输入版本的通过评审。
# 函数用途: 阻断缺乏集成事实或未经他人复核时的范围虚标。
def validate_scope_upgrade(
    target_scope: str,
    *,
    has_integrated_evidence: bool,
    has_matching_approved_review: bool,
) -> None:
    if target_scope == "full_candidate":
        if not has_integrated_evidence:
            raise ObservationScopeHonestyError(
                "CANNOT_UPGRADE_TO_FULL_CANDIDATE_WITHOUT_INTEGRATED_EVIDENCE: "
                "full_candidate observation scope requires passing integrated evidence (scope='full')"
            )
        if not has_matching_approved_review:
            raise ObservationScopeHonestyError(
                "CANNOT_UPGRADE_TO_FULL_CANDIDATE_WITHOUT_APPROVED_REVIEW: "
                "full_candidate observation scope requires approved independent review matching input_version"
            )


# LLM: 汇总多条需求项的结构化闭环情况，生成软性建议与计数，不作为硬门阻断命令。
# 函数用途: 为协调代理或展示端提供需求溯源的客观统计与待办诊断。
def provenance_closeout_summary(records: list[RequirementProvenanceRecord]) -> dict[str, Any]:
    total = len(records)
    verified_count = sum(1 for r in records if r.observation_scope in ("module_only", "focused_integration", "full_candidate"))
    full_count = sum(1 for r in records if r.observation_scope == "full_candidate")
    approved_count = sum(1 for r in records if r.review_status == "approved")
    stale_review_count = sum(1 for r in records if r.review_status == "stale")
    accepted_count = sum(1 for r in records if r.acceptance_status == "accepted")
    rejected_count = sum(1 for r in records if r.acceptance_status == "rejected")
    invalidated_count = sum(1 for r in records if r.acceptance_status == "invalidated")

    advisory_notes: list[str] = []
    for r in records:
        if r.review_status == "stale":
            advisory_notes.append(f"需求 {r.requirement_id} 评审已过期（原因：{r.invalidation_reason or '路径变更'}），需重新独立评审。")
        elif r.review_status == "missing":
            advisory_notes.append(f"需求 {r.requirement_id} 尚未登记独立评审。")
        elif r.review_status == "changes_requested":
            advisory_notes.append(f"需求 {r.requirement_id} 独立评审提出返工要求，尚未通过。")

        if r.acceptance_status == "invalidated":
            advisory_notes.append(f"需求 {r.requirement_id} 验收已失效（原因：{r.invalidation_reason or '代码修改'}），需重新验收。")
        elif r.acceptance_status == "rejected":
            advisory_notes.append(f"需求 {r.requirement_id} 验收被驳回，需修改后重新提请验收。")
        elif r.acceptance_status == "unaccepted":
            advisory_notes.append(f"需求 {r.requirement_id} 尚未完成最终验收。")

        if r.observation_scope == "unverified":
            advisory_notes.append(f"需求 {r.requirement_id} 尚未关联任何有效验证事实。")
        elif r.observation_scope != "full_candidate":
            advisory_notes.append(f"需求 {r.requirement_id} 当前观测范围为 {r.observation_scope}，尚未完成全量候选集成验证。")

    all_ready = (
        total > 0
        and verified_count == total
        and approved_count == total
        and accepted_count == total
        and stale_review_count == 0
        and invalidated_count == 0
        and rejected_count == 0
    )

    return {
        "total_requirements": total,
        "verified_count": verified_count,
        "full_candidate_count": full_count,
        "approved_count": approved_count,
        "stale_review_count": stale_review_count,
        "accepted_count": accepted_count,
        "rejected_count": rejected_count,
        "invalidated_count": invalidated_count,
        "is_closeout_ready": all_ready,
        "advisory_notes": advisory_notes,
    }
