from __future__ import annotations

"""统一候选证据审核、冲突裁决和正式晋升服务。"""

# LLM: 任何 Candidate 到 long_term/Persona/lesson/HOT 的正式写入都必须经过这个 Service。
# 模块用途: 核验消息/工具证据，执行保守自动策略，并在正式落点成功后标 promoted。

import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Protocol

from ..capability.persona_repository import PersonaMutationRequest, PersonaRepository
from .candidate_models import MemoryCandidate, MemoryScope, normalize_reference_list
from .candidates import CandidateService
from .jsonl import JsonlMemory, MemoryRecord
from .lessons import HotRuleRepository, LessonRepository

_LONG_TERM_TARGET_TYPES = frozenset({"long_term_fact", "event", "project"})

# 写路径查重:表述变体(LCS 覆盖率)与长度差双闸,只合并"逐字近重复"的同一断言,
# 绝不把不同事实(加信息/换词义)并掉——漏并多存一条,误并丢信息,后者更糟。
_MERGE_CONTENT_SIMILARITY = 0.90
_MERGE_MAX_LENGTH_RATIO = 0.20

# 英文关键词抽取:只收 ASCII 词面(结构化),不做词义判断;用于 BM25 臂跨语言命中。
_EN_KEYWORD_MIN_LEN = 3
_EN_KEYWORD_MAX = 12
_EN_KEYWORD_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "from", "that", "this", "have", "has",
        "was", "were", "will", "would", "should", "could", "about", "into",
    }
)


# LLM: 工具证据结果由 Tool Agent 权威接口提供；Memory 不读取模型文字或自建工具成功状态机。
# 类用途: 表示一次 operation/call 的已核验终态和 canonical ref。
@dataclass(frozen=True)
class VerifiedToolEvidence:
    successful: bool
    terminal_status: str
    canonical_ref: dict[str, object]
    effect_outcome: str = ""


# LLM: Protocol 是 Memory 对工具模块的唯一依赖面，具体 ledger/状态字段仍归 Tool Agent。
# 类用途: 由工具运行层按 operation_id/call_id 核验正式成功终态。
class ToolEvidenceVerifier(Protocol):
    # LLM: 实现必须查询 Tools 权威账本并返回 typed 终态，不能信任候选自填的 status。
    # 函数用途: 核验一条工具证据引用是否对应真实成功终态。
    def verify(self, ref: dict[str, object]) -> VerifiedToolEvidence | None: ...


# LLM: 消息核验必须回到 ConversationStore；候选内保存的 role/hash/quote 不是自证。
# 类用途: 由 owner ConversationStore 精确核验 user_explicit 来源。
class MessageEvidenceVerifier(Protocol):
    # LLM: 实现必须查询当前 owner ConversationStore 并核对 message/thread/role/hash/quote。
    # 函数用途: 核验一条 user_explicit 消息引用。
    def verify(self, ref: dict[str, object]) -> dict[str, object] | None: ...


# LLM: 缺少 Tool Agent verifier 时 tool_verified 自动/手动晋升都 fail closed。
# 类用途: 默认工具证据核验器。
class RejectingToolEvidenceVerifier:
    # LLM: 没有正式 Tools 适配器时固定拒绝，不能降级信任模型声明。
    # 函数用途: 对无法核验的工具引用返回未通过。
    def verify(self, ref: dict[str, object]) -> VerifiedToolEvidence | None:
        del ref
        return None


# LLM: 缺少 ConversationStore 时 user_explicit 不得靠候选自称升级为事实。
# 类用途: 默认消息证据核验器。
class RejectingMessageEvidenceVerifier:
    # LLM: 没有 ConversationStore 适配器时固定拒绝，不能用候选字段自证 user_explicit。
    # 函数用途: 对无法核验的消息引用返回未通过。
    def verify(self, ref: dict[str, object]) -> dict[str, object] | None:
        del ref
        return None


# LLM: The dependency object lists every canonical authority used by promotion and replaces a
# wide constructor; it contains no alternate persistence callbacks.
# 类用途: 汇总 Candidate、长期记忆、Persona、lesson、HOT 与证据核验器。
@dataclass(frozen=True)
class MemoryPromotionDependencies:
    candidates: CandidateService
    long_term: JsonlMemory
    persona: PersonaRepository
    lessons: LessonRepository
    hot: HotRuleRepository
    message_verifier: MessageEvidenceVerifier | None = None
    tool_verifier: ToolEvidenceVerifier | None = None


# LLM: Threshold policy is immutable and separate from repositories so tests/config cannot mix
# a lesson threshold into HOT behavior accidentally.
# 类用途: 保存 lesson/HOT 重复证据门槛。
@dataclass(frozen=True)
class MemoryPromotionPolicy:
    lesson_min_occurrences: int = 2
    hot_min_occurrences: int = 3


# LLM: This adapter reads the Tool Agent's authoritative runtime gate ledger; it does not infer success from archive prose.
# 类用途: 按 owner/run/operation 精确核验工具成功终态，并返回可保存的最小规范引用。
class LocalStoreToolEvidenceVerifier:
    # LLM: owner_id 是不可变核验边界，runtime gate ledger 由执行层每轮工具执行后机器写入。
    # 函数用途: 初始化 runtime gate ledger 的只读 Memory 适配器。
    def __init__(self, ledger_store: object, *, owner_id: str) -> None:
        self.ledger_store = ledger_store
        self.owner_id = str(owner_id or "").strip()

    # LLM: Claimed status/effect fields in the candidate are advisory; the runtime gate ledger is authoritative.
    # 函数用途: 查一条工具执行观测记录，只有执行层判定 succeeded 时才通过。
    def verify(self, ref: dict[str, object]) -> VerifiedToolEvidence | None:
        owner_id = str(ref.get("owner_id") or "").strip()
        run_id = str(ref.get("run_id") or "").strip()
        operation_id = str(ref.get("operation_id") or "").strip()
        reader = getattr(self.ledger_store, "get_runtime_gate_ledger", None)
        if (
            not callable(reader)
            or not self.owner_id
            or owner_id != self.owner_id
            or not run_id
            or not operation_id
        ):
            return None
        try:
            record = reader(run_id=run_id, operation_id=operation_id)
        except Exception:
            return None
        if record is None:
            return None
        successful = str(record.status or "").strip().lower() == "succeeded"
        effect_outcome = "confirmed" if successful else "unknown"
        canonical = {
            "owner_id": self.owner_id,
            "run_id": str(record.run_id),
            "operation_id": str(record.operation_id),
            "tool": str(record.tool),
            "status": str(record.status),
        }
        call_id = str(ref.get("call_id") or "").strip()
        if call_id:
            canonical["call_id"] = call_id
        effect_source_ref = str(ref.get("effect_source_ref") or "").strip()
        if effect_source_ref:
            canonical["effect_source_ref"] = effect_source_ref
        return VerifiedToolEvidence(
            successful=successful,
            terminal_status=str(record.status or ""),
            canonical_ref=canonical,
            effect_outcome=effect_outcome,
        )


# LLM: verifier 流式查一条真实消息并核验 thread/role/hash/quote，不把整段原话返回调用方。
# 类用途: 将 ConversationStore 接到 PromotionService 的 typed evidence contract。
class ConversationMessageEvidenceVerifier:
    # LLM: 构造器只保存 ConversationStore 只读接口，不复制其文件布局或消息状态机。
    # 函数用途: 初始化当前 owner 消息证据核验器。
    def __init__(self, conversation_store: object) -> None:
        self.conversation_store = conversation_store

    # LLM: 候选 claim 必须与权威消息的 thread/role/hash/quote 全部匹配，且只返回最小证据字段。
    # 函数用途: 核验一条 user_explicit 引用并返回规范证据。
    def verify(self, ref: dict[str, object]) -> dict[str, object] | None:
        thread_id = str(ref.get("thread_id") or "").strip()
        message_id = str(ref.get("message_id") or "").strip()
        reader = getattr(self.conversation_store, "message_by_id_report", None)
        if not thread_id or not message_id or not callable(reader):
            return None
        message, errors = reader(thread_id, message_id)
        if errors or message is None:
            return None
        content = str(message.content or "")
        content_hash = "sha256:" + hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()
        claimed_hash = str(ref.get("content_hash") or "")
        quote = str(ref.get("quote") or "").strip()
        if claimed_hash != content_hash or not quote or len(quote) > 300 or quote not in content:
            return None
        claimed_role = str(ref.get("role") or message.role)
        if claimed_role != str(message.role):
            return None
        return {
            "message_id": message_id,
            "thread_id": thread_id,
            "role": str(message.role),
            "channel": str(message.channel),
            "content_hash": content_hash,
            "quote": quote,
            "created_at": float(message.created_at),
        }


# LLM: result 只说明机器决策和正式 ref，不包含候选或历史记忆正文。
# 类用途: 给 CLI、Curator 和测试返回统一晋升结果。
@dataclass(frozen=True)
class MemoryPromotionResult:
    candidate_id: str
    promoted: bool
    status: str
    reason_code: str
    promotion_ref: str = ""

    # LLM: 结果只序列化决策码和正式 ref，不暴露候选或 Persona 正文。
    # 函数用途: 将晋升结果转换为 CLI/Curator 可消费字典。
    def to_dict(self) -> dict[str, object]:
        return asdict(self)


# LLM: PromotionService 不解析聊天意图；只消费已审核 Candidate 和两个 typed verifier。
# 类用途: 唯一执行 long_term、Persona、lesson 与 HOT 正式晋升。
class MemoryPromotionService:
    # LLM: Construction accepts one authority bundle and one policy object, preventing partial
    # alternative wiring while preserving rejecting verifier defaults.
    # 函数用途: 初始化唯一 Candidate 晋升服务。
    def __init__(
        self,
        *,
        dependencies: MemoryPromotionDependencies,
        policy: MemoryPromotionPolicy | None = None,
    ) -> None:
        thresholds = policy or MemoryPromotionPolicy()
        self.candidates = dependencies.candidates
        self.long_term = dependencies.long_term
        self.persona = dependencies.persona
        self.lessons = dependencies.lessons
        self.hot = dependencies.hot
        self.message_verifier = (
            dependencies.message_verifier or RejectingMessageEvidenceVerifier()
        )
        self.tool_verifier = dependencies.tool_verifier or RejectingToolEvidenceVerifier()
        self.lesson_min_occurrences = max(2, int(thresholds.lesson_min_occurrences))
        self.hot_min_occurrences = max(3, int(thresholds.hot_min_occurrences))

    # LLM: review 只推进统一 CandidateService 状态，不创建独立 decision ledger。
    # 函数用途: 管理员审核通过或拒绝一条精确 candidate_id。
    def review(
        self,
        candidate_id: str,
        *,
        approved: bool,
        reviewer: str,
        note: str = "",
        proposed_action: str | None = None,
        target_entry_id: str | None = None,
        promotion_target: str | None = None,
    ) -> MemoryCandidate:
        target = "approved" if approved else "rejected"
        return self.candidates.transition(
            candidate_id,
            target,
            reviewer=reviewer,
            review_note=note,
            proposed_action=proposed_action,
            target_entry_id=target_entry_id,
            promotion_target=promotion_target,
        )

    # LLM: automatic 只允许无冲突 user_explicit/tool_verified 的新长期事实；其他目标都必须先人工审核。
    # 函数用途: 核验证据并提交一个候选的正式落点。
    def promote(
        self,
        candidate_id: str,
        *,
        reviewer: str = "memory-promotion-service",
        automatic: bool = False,
        confirmed: bool = False,
    ) -> MemoryPromotionResult:
        candidate = self.candidates.get(candidate_id)
        if candidate.status == "promoted":
            return MemoryPromotionResult(
                candidate_id,
                True,
                "promoted",
                "ALREADY_PROMOTED",
                candidate.promotion_ref,
            )
        evidence_code = self._verify_evidence(candidate)
        if evidence_code:
            return self._block_missing_evidence(candidate, reviewer, evidence_code)
        # 宿主确定性补全:模型/来源偶发漏写 promotion_target 时,对"用户明确要求记住的长期事实/事件候选"
        # (user_explicit + long_term_fact/event + 空 target)补成 long_term——这是用户显式表达,符合
        # 自主晋升语义;model_inferred/Persona/lesson/HOT 一律不补(必须人工审核)。纯结构化规则。
        if str(candidate.promotion_target or "").strip().lower() in {"", "none"}:
            if (
                candidate.origin == "user_explicit"
                and candidate.candidate_type in _LONG_TERM_TARGET_TYPES
            ):
                candidate = self.candidates.transition(
                    candidate_id,
                    candidate.status,
                    reviewer=reviewer,
                    promotion_target="long_term",
                )
        authority_code = _absolute_authority_block(candidate)
        if authority_code:
            return MemoryPromotionResult(
                candidate_id,
                False,
                candidate.status,
                authority_code,
            )
        if automatic:
            policy_code = _automatic_policy_block(candidate)
            if policy_code:
                return MemoryPromotionResult(
                    candidate_id,
                    False,
                    candidate.status,
                    policy_code,
                )
            if candidate.status == "pending_review":
                candidate = self.candidates.transition(
                    candidate_id,
                    "approved",
                    reviewer=reviewer,
                    review_note="conservative_v1 自动策略：精确证据、无替换、仅长期事实。",
                )
        if candidate.status != "approved":
            return MemoryPromotionResult(
                candidate_id,
                False,
                candidate.status,
                "REVIEW_REQUIRED",
            )
        try:
            promotion_ref = self._commit_target(candidate, reviewer=reviewer, confirmed=confirmed)
        except _PromotionConflict as exc:
            blocked = self.candidates.transition(
                candidate.candidate_id,
                "blocked_conflict",
                reviewer=reviewer,
                review_note=exc.reason_code,
                conflicts_with=list(exc.conflicts_with),
            )
            return MemoryPromotionResult(
                candidate.candidate_id,
                False,
                blocked.status,
                exc.reason_code,
            )
        except ValueError as exc:
            # lesson/HOT 重复证据门槛不满足(lessons.py:_validate_lesson_candidate):
            # 自动路径保持原状态干净失败,不崩溃——候选留在 approved,证据凑齐后下次再晋升;
            # 稳定原因码供 curator 兜底与 CLI 显示。其他 target 的 ValueError 照常外抛。
            if str(candidate.promotion_target or "").strip().lower() in {"lesson", "hot"}:
                return MemoryPromotionResult(
                    candidate.candidate_id,
                    False,
                    candidate.status,
                    "PROMOTION_THRESHOLD_NOT_MET",
                )
            raise
        # 总量闸:long_term 写成功后顺带治理(超上限按访问信号浓缩,user_explicit 不淘汰)。
        # 放在 promote 成功路径,任何写入口(remember/curator)都自动触发,无需单独 cron。
        try:
            if str(candidate.promotion_target or "").strip().lower() in {"", "none", "long_term"}:
                self.long_term.condense()
        except Exception:
            pass  # 治理失败不影响本次晋升本身
        promoted = self.candidates.mark_promoted(
            candidate.candidate_id,
            reviewer=reviewer,
            promotion_ref=promotion_ref,
        )
        if candidate.proposed_action == "remove":
            self.candidates.redact_content(
                content_hashes={candidate.content_hash},
                reviewer=reviewer,
            )
            promoted = self.candidates.get(candidate.candidate_id)
        return MemoryPromotionResult(
            candidate.candidate_id,
            True,
            promoted.status,
            "PROMOTED",
            promotion_ref,
        )

    # LLM: user_explicit 必须逐条回查 ConversationStore；tool_verified 必须逐条回查 Tool Agent 终态。
    # 函数用途: 返回空字符串表示证据通过，否则返回稳定阻塞码。
    def _verify_evidence(self, candidate: MemoryCandidate) -> str:
        if candidate.origin == "user_explicit":
            if not candidate.source_message_refs:
                return "USER_MESSAGE_EVIDENCE_MISSING"
            verified = [self.message_verifier.verify(ref) for ref in candidate.source_message_refs]
            if not any(item and item.get("role") == "user" for item in verified):
                return "USER_MESSAGE_EVIDENCE_INVALID"
        if candidate.origin == "tool_verified":
            if not candidate.source_tool_refs:
                return "TOOL_EVIDENCE_MISSING"
            verified_tools = [self.tool_verifier.verify(ref) for ref in candidate.source_tool_refs]
            if not verified_tools or not all(
                item is not None
                and item.successful
                and item.terminal_status == "succeeded"
                and item.effect_outcome not in {"unknown", "not_started"}
                for item in verified_tools
            ):
                return "TOOL_EVIDENCE_NOT_SUCCEEDED"
        return ""

    # LLM: 证据失败从 pending/approved 进入统一 blocked 状态；终态只返回结果不改写历史。
    # 函数用途: 持久化缺证据阻塞。
    def _block_missing_evidence(
        self,
        candidate: MemoryCandidate,
        reviewer: str,
        reason_code: str,
    ) -> MemoryPromotionResult:
        if candidate.status in {"pending_review", "approved"}:
            blocked = self.candidates.transition(
                candidate.candidate_id,
                "blocked_missing_evidence",
                reviewer=reviewer,
                review_note=reason_code,
            )
            status = blocked.status
        else:
            status = candidate.status
        return MemoryPromotionResult(
            candidate.candidate_id,
            False,
            status,
            reason_code,
        )

    # LLM: 每个正式目标只有一个 repository；禁止在这里另写 Markdown/JSONL 旁路。
    # 函数用途: 提交目标并返回正式引用。
    def _commit_target(
        self,
        candidate: MemoryCandidate,
        *,
        reviewer: str,
        confirmed: bool,
    ) -> str:
        # 宿主确定性补全(与 _automatic_policy_block 同源):user_explicit 长期事实候选漏写 target
        # 时按 long_term 落库——策略检查已放行,落库必须用同一语义,否则 'none' 不可落。
        target = str(candidate.promotion_target or "").strip().lower()
        if target in {"", "none"}:
            if candidate.origin == "user_explicit" and candidate.candidate_type in _LONG_TERM_TARGET_TYPES:
                target = "long_term"
        if target == "long_term":
            return self._commit_long_term(candidate)
        if target in {"user", "soul", "agents"}:
            return self._commit_persona(
                candidate,
                target=target,
                reviewer=reviewer,
                confirmed=confirmed,
            )
        if target == "lesson":
            lesson = self.lessons.promote(
                candidate,
                min_occurrences=self.lesson_min_occurrences,
            )
            return lesson.path + "#" + lesson.lesson_id
        if target == "hot":
            lesson = self.lessons.get(candidate.target_entry_id)
            hot = self.hot.promote(
                candidate,
                lesson=lesson,
                min_occurrences=self.hot_min_occurrences,
            )
            return "memory-hot.md#" + hot.hot_id
        raise ValueError(f"candidate promotion_target {target!r} is not promotable")

    # LLM: subject 冲突只在同 scope 内；新时间不能覆盖，replace/remove 必须精确 target_entry_id。
    # 函数用途: 提交长期记忆 add/replace/remove/merge。
    def _commit_long_term(self, candidate: MemoryCandidate) -> str:
        if candidate.candidate_type not in _LONG_TERM_TARGET_TYPES:
            raise ValueError("candidate type cannot be stored in long_term memory")
        if candidate.origin == "model_inferred":
            raise ValueError("model_inferred candidate cannot become a formal fact")
        existing = _same_subject_scope(self.long_term.all(), candidate)
        same_content = next(
            (item for item in existing if item.content.strip() == candidate.content.strip()),
            None,
        )
        if same_content is not None and candidate.proposed_action == "add":
            return "memory/long_term/memory.jsonl#" + same_content.entry_id
        action = candidate.proposed_action
        if action == "add":
            if existing:
                raise _PromotionConflict(
                    "SUBJECT_SCOPE_CONFLICT_REQUIRES_REPLACE",
                    tuple(item.entry_id for item in existing),
                )
            # 写路径查重:同 scope 内"逐字近重复"的旧断言合入旧条目,不新增(治重复记录膨胀)。
            # 与 subject 冲突不同——跨 subject 的表述变体不是矛盾,而是同一事实的重复记录。
            near = _near_duplicate_in_scope(self.long_term.all(), candidate)
            if near is not None:
                record = self.long_term.apply_batch(
                    [
                        {
                            "action": "replace",
                            "entry_id": near.entry_id,
                            "content": candidate.content,
                            "kind": _long_term_kind(candidate.candidate_type),
                            "tags": _merged_tags(near, candidate),
                            "attributes": _long_term_attributes(candidate),
                            "source": f"candidate:{candidate.candidate_id}",
                            "expires_at": _expiry_epoch(candidate.valid_until),
                            "expected_version": near.version,
                        }
                    ]
                )[0]
                return "memory/long_term/memory.jsonl#" + record.entry_id
            entry_id = "memory-" + hashlib.sha256(
                candidate.candidate_id.encode("utf-8")
            ).hexdigest()[:24]
            record = self.long_term.apply_batch(
                [
                    {
                        "action": "add",
                        "entry_id": entry_id,
                        "role": "user" if candidate.origin == "user_explicit" else "system",
                        "content": candidate.content,
                        "kind": _long_term_kind(candidate.candidate_type),
                        "tags": [candidate.candidate_type, candidate.origin],
                        "attributes": _long_term_attributes(candidate),
                        "source": f"candidate:{candidate.candidate_id}",
                        "expires_at": _expiry_epoch(candidate.valid_until),
                    }
                ]
            )
            committed = record[0] if record else next(
                item for item in self.long_term.all() if item.entry_id == entry_id
            )
            return "memory/long_term/memory.jsonl#" + committed.entry_id
        target = candidate.target_entry_id
        if not target or target not in {item.entry_id for item in existing}:
            raise _PromotionConflict(
                "TARGET_ENTRY_NOT_IN_SAME_SUBJECT_SCOPE",
                tuple(item.entry_id for item in existing),
            )
        prior = next(item for item in existing if item.entry_id == target)
        if action in {"replace", "merge"}:
            record = self.long_term.apply_batch(
                [
                    {
                        "action": "replace",
                        "entry_id": target,
                        "content": candidate.content,
                        "kind": _long_term_kind(candidate.candidate_type),
                        "tags": [candidate.candidate_type, candidate.origin],
                        "attributes": _long_term_attributes(candidate),
                        "source": f"candidate:{candidate.candidate_id}",
                        "expires_at": _expiry_epoch(candidate.valid_until),
                        "expected_version": prior.version,
                    }
                ]
            )[0]
            return "memory/long_term/memory.jsonl#" + record.entry_id
        if action == "remove":
            self.long_term.remove(
                target,
                source=f"candidate:{candidate.candidate_id}",
                expected_version=prior.version,
            )
            return "memory/long_term/memory.jsonl#" + target + ":removed"
        raise ValueError("long_term promotion proposed_action must be add/replace/remove/merge")

    # LLM: USER 只允许 user_explicit；SOUL/AGENTS 还必须带本次显式 confirmed，不接受后台自动确认。
    # 函数用途: 通过唯一 PersonaRepository 提交人格目标。
    def _commit_persona(
        self,
        candidate: MemoryCandidate,
        *,
        target: str,
        reviewer: str,
        confirmed: bool,
    ) -> str:
        if candidate.origin != "user_explicit":
            raise ValueError("Persona promotion requires user_explicit evidence")
        if target in {"soul", "agents"} and not confirmed:
            raise ValueError("SOUL/AGENTS promotion requires explicit user confirmation")
        if target == "user" and candidate.candidate_type not in {
            "user_profile",
            "user_preference",
        }:
            raise ValueError("USER promotion requires profile/preference candidate")
        if target == "soul" and candidate.candidate_type != "soul_change":
            raise ValueError("SOUL promotion requires soul_change candidate")
        if target == "agents" and candidate.candidate_type != "working_agreement":
            raise ValueError("AGENTS promotion requires working_agreement candidate")
        action = candidate.proposed_action
        if action == "merge":
            action = "replace"
        if action not in {"add", "replace", "remove"}:
            raise ValueError("Persona promotion action is unsupported")
        scope = MemoryScope.from_value(candidate.scope)
        content = _scoped_persona_content(candidate.content, scope) if action != "remove" else ""
        quote = next(
            (
                str(ref.get("quote") or "")
                for ref in candidate.source_message_refs
                if str(ref.get("quote") or "")
            ),
            "",
        )
        result = self.persona.mutate(
            PersonaMutationRequest(
                target=target,
                action=action,
                content=content,
                entry_id=candidate.target_entry_id,
                source_quote=quote,
                confirmed=confirmed or target == "user",
                source=f"memory-promotion:{reviewer}:{candidate.candidate_id}",
            )
        )
        entry_id = str(result.get("entry_id") or candidate.target_entry_id)
        return f"{target.upper()}.md#{entry_id or result.get('sha256', '')}"


# LLM: automatic policy 是固定机器条件；不使用 confidence 阈值或“看起来像事实”的文本判断。
# 函数用途: 返回空表示可自动晋升，否则返回稳定原因码。
def _automatic_policy_block(candidate: MemoryCandidate) -> str:
    # lesson 专线:model_inferred/subagent_lesson 的 lesson 候选允许自动晋升——重复与证据门槛
    # 在 lessons.py:_validate_lesson_candidate(非 user_explicit 必须 occurrence>=2 且证据组>=2)
    # 于 _commit_target 落库时强制,自动路径走到那里天然生效,这里只放行目标类型、不重复判断;
    # 事实/Persona/其他 target 仍必须人工(绝对权威块与 Persona 闸在 _absolute_authority_block)。
    if (
        candidate.candidate_type == "lesson"
        and str(candidate.promotion_target or "").strip().lower() == "lesson"
    ):
        if candidate.proposed_action != "add" or candidate.target_entry_id:
            return "AUTO_POLICY_MUTATION_REQUIRES_REVIEW"
        if candidate.conflicts_with:
            return "AUTO_POLICY_CONFLICT_REQUIRES_REVIEW"
        scope = MemoryScope.from_value(candidate.scope)
        if scope.scope_type == "temporary" and not candidate.valid_until:
            return "AUTO_POLICY_TEMPORARY_REQUIRES_EXPIRY"
        return ""
    if candidate.origin not in {"user_explicit", "tool_verified"}:
        return "AUTO_POLICY_ORIGIN_REQUIRES_REVIEW"
    # 宿主确定性补全:模型/来源偶发把 promotion_target 写成 none/空,但对 user_explicit 的
    # long_term_fact/event/project(用户明确要求记住的长期事实)按 long_term 处理——这是用户显式
    # 表达,符合自主晋升语义。model_inferred/Persona/lesson/HOT 不在此列(必须人工)。
    target = str(candidate.promotion_target or "").strip().lower()
    if target in {"", "none"}:
        if candidate.origin == "user_explicit" and candidate.candidate_type in _LONG_TERM_TARGET_TYPES:
            target = "long_term"
    if target != "long_term":
        return "AUTO_POLICY_TARGET_REQUIRES_REVIEW"
    if candidate.candidate_type not in _LONG_TERM_TARGET_TYPES:
        return "AUTO_POLICY_TYPE_REQUIRES_REVIEW"
    if candidate.proposed_action != "add" or candidate.target_entry_id:
        return "AUTO_POLICY_MUTATION_REQUIRES_REVIEW"
    if candidate.conflicts_with:
        return "AUTO_POLICY_CONFLICT_REQUIRES_REVIEW"
    scope = MemoryScope.from_value(candidate.scope)
    if scope.scope_type == "temporary" and not candidate.valid_until:
        return "AUTO_POLICY_TEMPORARY_REQUIRES_EXPIRY"
    return ""


# LLM: Human review may accept an inference as a lesson, but it cannot manufacture user fact or
# Persona authority, and temporary/session instructions can never become a durable USER profile.
# 函数用途: 返回任何审核或后台来源都不能绕过的事实与 Persona 权威阻塞码。
def _absolute_authority_block(candidate: MemoryCandidate) -> str:
    if candidate.origin == "model_inferred" and candidate.promotion_target in {
        "long_term",
        "user",
        "soul",
        "agents",
    }:
        return "MODEL_INFERRED_FORMAL_AUTHORITY_FORBIDDEN"
    if (
        candidate.promotion_target in {"user", "soul", "agents"}
        and candidate.origin != "user_explicit"
    ):
        return "PERSONA_USER_EXPLICIT_REQUIRED"
    scope = MemoryScope.from_value(candidate.scope)
    if candidate.promotion_target == "user" and scope.scope_type in {
        "session",
        "temporary",
    }:
        return "TEMPORARY_USER_PROFILE_FORBIDDEN"
    return ""


# LLM: 查重是字符级确定性算法(LCS 覆盖率+长度差),不解析词义,不依赖 embedding 端点。
# 函数用途: 归一化正文后计算两段文字的 LCS 覆盖率。
def _text_similarity(a: str, b: str) -> float:
    ta = "".join(str(a or "").lower().split())
    tb = "".join(str(b or "").lower().split())
    if not ta or not tb:
        return 0.0
    if ta == tb:
        return 1.0
    if abs(len(ta) - len(tb)) / max(len(ta), len(tb)) > _MERGE_MAX_LENGTH_RATIO:
        return 0.0
    lcs = _lcs_length(ta, tb)
    return 2.0 * lcs / (len(ta) + len(tb))


def _lcs_length(a: str, b: str) -> int:
    if not a or not b:
        return 0
    if len(a) > len(b):
        a, b = b, a
    prev = [0] * (len(b) + 1)
    for row in range(len(a)):
        cur = [0] * (len(b) + 1)
        ach = a[row]
        for col in range(len(b)):
            if ach == b[col]:
                cur[col + 1] = prev[col] + 1
            else:
                cur[col + 1] = prev[col + 1] if prev[col + 1] >= cur[col] else cur[col]
        prev = cur
    return prev[-1]


# LLM: 查重范围限定同 scope;跨 scope 近重复是不同语境的记忆,不得合并。
# 函数用途: 在同 scope active 记录里找与候选内容"逐字近重复"的条目。
def _near_duplicate_in_scope(
    records: list[MemoryRecord],
    candidate: MemoryCandidate,
) -> MemoryRecord | None:
    scope = MemoryScope.from_value(candidate.scope)
    best: MemoryRecord | None = None
    best_score = 0.0
    for record in records:
        attributes = record.attributes if isinstance(record.attributes, dict) else {}
        if str(attributes.get("scope_type") or "legacy") != scope.scope_type:
            continue
        if str(attributes.get("scope_key") or "legacy") != scope.scope_key:
            continue
        score = _text_similarity(candidate.content, record.content)
        if score > best_score:
            best, best_score = record, score
    if best is None or best_score < _MERGE_CONTENT_SIMILARITY:
        return None
    return best


# LLM: 合并时保留旧条目身份(subject_key/entry_id 不动),正文用新表述,证据与标签追加去重。
# 函数用途: 把候选证据并入被查重命中的旧条目 tags。
def _merged_tags(prior: MemoryRecord, candidate: MemoryCandidate) -> list[str]:
    merged = [str(tag) for tag in (prior.tags or [])]
    for tag in (candidate.candidate_type, candidate.origin):
        if tag and tag not in merged:
            merged.append(tag)
    return merged[:12]


# LLM: 英文关键词只做词面抽取(ASCII 词/小写/停用词过滤),不做词义或翻译判断。
# 函数用途: 从正文抽取英文关键词,供 BM25 臂跨语言命中中文记忆。
def _english_keywords(text: str) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]+", str(text or ""))
    kept: list[str] = []
    for word in words:
        low = word.lower()
        if len(low) < _EN_KEYWORD_MIN_LEN or low in _EN_KEYWORD_STOPWORDS:
            continue
        if low not in kept:
            kept.append(low)
    return kept[:_EN_KEYWORD_MAX]


# LLM: 中文关键词来自稳定 subject_key 的机械拆段(与 lessons routing 同法),不读正文。
# 函数用途: 从 subject_key 生成中文关键词段。
def _cn_keywords(subject_key: str) -> list[str]:
    parts = [part for part in re.split(r"[^一-鿿A-Za-z0-9]+", str(subject_key or "")) if len(part) >= 2]
    return list(dict.fromkeys(parts))[:12]


# LLM: conflict 比较明确读取 subject_key + scope_type + scope_key；适用条件文字不参与身份猜测。
# 函数用途: 查找同主题同范围的 active 长期记忆。
def _same_subject_scope(
    records: list[MemoryRecord],
    candidate: MemoryCandidate,
) -> list[MemoryRecord]:
    scope = MemoryScope.from_value(candidate.scope)
    result: list[MemoryRecord] = []
    for record in records:
        attributes = record.attributes if isinstance(record.attributes, dict) else {}
        if (
            str(attributes.get("subject_key") or "") == candidate.subject_key
            and str(attributes.get("scope_type") or "legacy") == scope.scope_type
            and str(attributes.get("scope_key") or "legacy") == scope.scope_key
        ):
            result.append(record)
    return result


# LLM: 长期事实 attributes 只保存短结构化 provenance/scope，不复制完整用户原话或工具输出。
# 函数用途: 构造 long_term 记录元数据。
def _long_term_attributes(candidate: MemoryCandidate) -> dict[str, object]:
    scope = MemoryScope.from_value(candidate.scope)
    return {
        "candidate_id": candidate.candidate_id,
        "candidate_type": candidate.candidate_type,
        "origin": candidate.origin,
        "subject_key": candidate.subject_key,
        "scope_type": scope.scope_type,
        "scope_key": scope.scope_key,
        "applies_when": scope.applies_when,
        "excludes_when": scope.excludes_when,
        "valid_from": candidate.valid_from,
        "valid_until": candidate.valid_until,
        "keywords_en": _english_keywords(candidate.content),
        "keywords_cn": _cn_keywords(candidate.subject_key),
        "evidence_refs": normalize_reference_list(
            [
                *candidate.source_message_refs,
                *candidate.source_tool_refs,
                *candidate.source_artifact_refs,
                *candidate.evidence_refs,
            ]
        )[:32],
    }


# LLM: lesson 永不写进 long_term；这里只映射允许的事实种类。
# 函数用途: 选择长期记忆 kind。
def _long_term_kind(candidate_type: str) -> str:
    return {"long_term_fact": "fact", "event": "event", "project": "project"}[candidate_type]


# LLM: valid_until 是唯一过期来源，缺失不从“临时”等正文词推断。
# 函数用途: 将 ISO 失效时间转为 JsonlMemory epoch。
def _expiry_epoch(value: str) -> float:
    if not str(value or "").strip():
        return 0.0
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("candidate valid_until lacks timezone")
    return parsed.timestamp()


# LLM: Persona 行显式携带 typed scope，避免 macOS/Linux 等不同环境事实变成无范围冲突文本。
# 函数用途: 生成 USER/SOUL/AGENTS 中自包含的一行。
def _scoped_persona_content(content: str, scope: MemoryScope) -> str:
    condition = f"；适用：{scope.applies_when}" if scope.applies_when else ""
    exclusion = f"；不适用：{scope.excludes_when}" if scope.excludes_when else ""
    normalized = " ".join(content.split())
    return f"[{scope.scope_type}:{scope.scope_key}] {normalized}{condition}{exclusion}"


# LLM: 冲突异常只携带稳定 entry IDs 和原因码，不把旧正文回显到 candidate review。
# 类用途: 触发 blocked_conflict 状态迁移。
class _PromotionConflict(RuntimeError):
    # LLM: conflicts_with 只接收精确正式 entry ID，reason_code 保持稳定供状态机消费。
    # 函数用途: 构造一次不含正文的晋升冲突。
    def __init__(self, reason_code: str, conflicts_with: tuple[str, ...]) -> None:
        self.reason_code = reason_code
        self.conflicts_with = conflicts_with
        super().__init__(reason_code)


__all__ = [
    "ConversationMessageEvidenceVerifier",
    "LocalStoreToolEvidenceVerifier",
    "MemoryPromotionDependencies",
    "MemoryPromotionPolicy",
    "MemoryPromotionResult",
    "MemoryPromotionService",
    "MessageEvidenceVerifier",
    "RejectingMessageEvidenceVerifier",
    "RejectingToolEvidenceVerifier",
    "ToolEvidenceVerifier",
    "VerifiedToolEvidence",
]
