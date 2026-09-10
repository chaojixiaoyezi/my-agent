from __future__ import annotations

"""统一候选证据审核、冲突裁决和正式晋升服务。"""

# LLM: 任何 Candidate 到 long_term/Persona/lesson/HOT 的正式写入都必须经过这个 Service。
# 模块用途: 核验消息/工具证据，执行保守自动策略，并在正式落点成功后标 promoted。

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from ..capability.persona_repository import (
    PersonaMutationRequest,
    PersonaRepository,
    persona_entry_id,
)
from ..common.json_io import read_jsonl_objects_report
from ..common.tool_output_paths import tool_output_index_paths_for_lookup
from .candidate_models import (
    MemoryCandidate,
    MemoryScope,
    host_promotion_mode,
    normalize_reference_list,
)
from .candidates import CandidateService
from .jsonl import JsonlMemory, MemoryRecord
from .lessons import HotRuleRepository, LessonRepository
from .operations import normalized_memory_content

_LONG_TERM_TARGET_TYPES = frozenset({"long_term_fact", "event", "project"})

# 写路径查重:表述变体(LCS 覆盖率)与长度差双闸,只合并"逐字近重复"的同一断言,
# 绝不把不同事实(加信息/换词义)并掉——漏并多存一条,误并丢信息,后者更糟。

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
    # 参数只供本次证据核对，不进入 Candidate/promotion_ref 或对模型回放。
    parameters: dict[str, object] = field(default_factory=dict, repr=False, compare=False)


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


# LLM: This adapter prefers the runtime gate ledger and uses the canonical owner tool-output
# index only for exact legacy call identities that predate operation_id in raw audit rows.
# 类用途: 按 owner/run/operation 或唯一的 run/call 归档核验工具成功终态。
class LocalStoreToolEvidenceVerifier:
    # LLM: owner_id is the immutable verification wall; owner_root is read-only and may expose
    # only canonical blobs/tool_outputs indexes for legacy evidence completion.
    # 函数用途: 初始化工具账本与 owner 私有归档的只读 Memory 适配器。
    def __init__(
        self,
        ledger_store: object,
        *,
        owner_id: str,
        owner_root: str | Path | None = None,
    ) -> None:
        self.ledger_store = ledger_store
        self.owner_id = str(owner_id or "").strip()
        self.owner_root = (
            Path(owner_root).expanduser().resolve(strict=False)
            if str(owner_root or "").strip()
            else None
        )

    # LLM: Candidate status/effect fields are advisory. Exact ledger evidence wins; the archive
    # fallback requires one unique run/call/tool identity plus typed succeeded operation facts.
    # 函数用途: 核验一条工具引用，仅真实成功终态通过。
    def verify(self, ref: dict[str, object]) -> VerifiedToolEvidence | None:
        owner_id = str(ref.get("owner_id") or "").strip()
        run_id = str(ref.get("run_id") or "").strip()
        operation_id = str(ref.get("operation_id") or "").strip()
        if not self.owner_id or owner_id != self.owner_id or not run_id:
            return None
        reader = getattr(self.ledger_store, "get_runtime_gate_ledger", None)
        record = None
        if operation_id and callable(reader):
            try:
                record = reader(run_id=run_id, operation_id=operation_id)
            except Exception:
                record = None
        if record is not None:
            return _verified_runtime_gate_record(self.owner_id, ref, record)
        return self._verify_owner_tool_archive(ref, run_id=run_id)

    # LLM: Legacy raw audit rows may omit operation_id even though the owner tool index retained
    # it. Resolve only a single exact invocation identity; ambiguity and malformed JSON fail closed.
    # 函数用途: 从 owner 工具输出索引补核验旧 run/call/tool 引用。
    def _verify_owner_tool_archive(
        self,
        ref: dict[str, object],
        *,
        run_id: str,
    ) -> VerifiedToolEvidence | None:
        if self.owner_root is None:
            return None
        call_id = str(ref.get("tool_call_id") or ref.get("call_id") or "").strip()
        tool_name = str(ref.get("tool_name") or ref.get("tool") or "").strip()
        claimed_operation_id = str(ref.get("operation_id") or "").strip()
        if not call_id or not tool_name:
            return None
        matches = _owner_tool_archive_matches(
            self.owner_root,
            run_id=run_id,
            call_id=call_id,
            tool_name=tool_name,
            operation_id=claimed_operation_id,
        )
        by_identity: dict[str, dict[str, object]] = {}
        for item in matches:
            identity = str(item.get("scoped_call_id") or f"{run_id}:{call_id}").strip()
            by_identity[identity] = item
        if len(by_identity) != 1:
            return None
        row = next(iter(by_identity.values()))
        operation = row.get("tool_operation")
        execution = row.get("tool_execution")
        if not isinstance(operation, dict) or not isinstance(execution, dict):
            return None
        resolved_operation_id = str(operation.get("operation_id") or "").strip()
        successful = (
            row.get("ok") is True
            and str(row.get("status") or "").strip().lower()
            in {"ok", "done", "succeeded"}
            and str(operation.get("status") or "").strip().lower() == "succeeded"
            and execution.get("handler_executed") is True
            and bool(resolved_operation_id)
        )
        parameters = row.get("parameters")
        return VerifiedToolEvidence(
            successful=successful,
            terminal_status="succeeded" if successful else "failed",
            canonical_ref={
                "owner_id": self.owner_id,
                "run_id": run_id,
                "operation_id": resolved_operation_id,
                "call_id": call_id,
                "tool": tool_name,
                "status": "succeeded" if successful else str(row.get("status") or ""),
                "source": "tool_output_index",
            },
            effect_outcome="confirmed" if successful else "unknown",
            parameters=dict(parameters) if isinstance(parameters, dict) else {},
        )


# LLM: The adapter explicitly migrates legacy ledger terminal label "done" to canonical
# ToolResult label "succeeded"; no other aliases or prose are accepted.
# 函数用途: 将一条 runtime gate 记录投影成 Memory 的规范工具证据。
def _verified_runtime_gate_record(
    owner_id: str,
    ref: dict[str, object],
    record: object,
) -> VerifiedToolEvidence:
    raw_status = str(getattr(record, "status", "") or "").strip().lower()
    successful = raw_status in {"succeeded", "done"}
    terminal_status = "succeeded" if successful else raw_status
    parameters = getattr(record, "parameters", {})
    canonical = {
        "owner_id": owner_id,
        "run_id": str(getattr(record, "run_id", "") or ""),
        "operation_id": str(getattr(record, "operation_id", "") or ""),
        "tool": str(getattr(record, "tool", "") or ""),
        "status": terminal_status,
    }
    call_id = str(ref.get("call_id") or ref.get("tool_call_id") or "").strip()
    if call_id:
        canonical["call_id"] = call_id
    effect_source_ref = str(ref.get("effect_source_ref") or "").strip()
    if effect_source_ref:
        canonical["effect_source_ref"] = effect_source_ref
    return VerifiedToolEvidence(
        successful=successful,
        terminal_status=terminal_status,
        canonical_ref=canonical,
        effect_outcome="confirmed" if successful else "unknown",
        parameters=dict(parameters) if isinstance(parameters, dict) else {},
    )


# LLM: Index lookup is owner-root bounded and returns metadata rows only; raw output bodies and
# physical artifact contents never enter Memory promotion verification.
# 函数用途: 读取 owner 内所有规范工具索引并筛选精确调用。
def _owner_tool_archive_matches(
    owner_root: Path,
    *,
    run_id: str,
    call_id: str,
    tool_name: str,
    operation_id: str,
) -> list[dict[str, object]]:
    matches: list[dict[str, object]] = []
    for path in tool_output_index_paths_for_lookup(owner_root):
        report = read_jsonl_objects_report(
            path,
            context="memory_promotion.tool_output_index",
        )
        if report.load_errors:
            continue
        for row in report.records:
            operation = row.get("tool_operation")
            row_operation_id = (
                str(operation.get("operation_id") or "").strip()
                if isinstance(operation, dict)
                else ""
            )
            if (
                str(row.get("run_id") or "").strip() == run_id
                and str(row.get("call_id") or "").strip() == call_id
                and str(row.get("tool") or "").strip() == tool_name
                and (not operation_id or row_operation_id == operation_id)
            ):
                matches.append(dict(row))
    return matches


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


# LLM: 正式目标提交与 MemoryPromotionService 主流程分离，保持主流程只做裁决编排。
# 类用途: 提供 long_term/Persona/lesson/HOT 四种正式落点的唯一提交实现。
class _PromotionCommitMixin:
    # LLM: 每个正式目标只有一个 repository；禁止在这里另写 Markdown/JSONL 旁路。
    # 函数用途: 提交目标并返回正式引用。
    def _commit_target(
        self,
        candidate: MemoryCandidate,
        *,
        reviewer: str,
        confirmed: bool,
    ) -> str:
        # 宿主确定性补全(与 _automatic_policy_block 同源):user_explicit 漏写 target 时按 long_term 落库。
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
    # 函数用途: 按 proposed_action 分发长期记忆提交。
    def _commit_long_term(self, candidate: MemoryCandidate) -> str:
        if candidate.candidate_type not in _LONG_TERM_TARGET_TYPES:
            raise ValueError("candidate type cannot be stored in long_term memory")
        if candidate.origin == "model_inferred":
            raise ValueError("model_inferred candidate cannot become a formal fact")
        _ensure_recallable_scope_for_add(candidate)
        existing = _same_subject_scope(self.long_term.all(), candidate)
        same_content = _same_content_in(existing, candidate)
        if same_content is not None and candidate.proposed_action == "add":
            return "memory/long_term/memory.jsonl#" + same_content.entry_id
        if candidate.proposed_action == "add":
            return self._commit_long_term_add(candidate, existing)
        return self._commit_long_term_existing(candidate, existing)

    # LLM: add 不得隐式转换为 replace；同 subject/scope 的冲突要求显式目标，不同主体分别保存。
    # 函数用途: 提交新增长期事实(add 分支)。
    def _commit_long_term_add(
        self,
        candidate: MemoryCandidate,
        existing: list[MemoryRecord],
    ) -> str:
        if existing:
            raise _PromotionConflict(
                "SUBJECT_SCOPE_CONFLICT_REQUIRES_REPLACE",
                tuple(item.entry_id for item in existing),
            )
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
        if not record:
            raise _PromotionConflict(
                "LONG_TERM_ADD_IDEMPOTENCY_IDENTITY_MISMATCH",
                _exact_content_entry_ids(self.long_term.all(), candidate),
            )
        committed = record[0]
        return "memory/long_term/memory.jsonl#" + committed.entry_id

    # LLM: replace/merge 必须精确 target_entry_id 且 target 在 same subject scope 内。
    # 函数用途: 提交对既有长期条目的 replace/merge/remove。
    def _commit_long_term_existing(
        self,
        candidate: MemoryCandidate,
        existing: list[MemoryRecord],
    ) -> str:
        target = candidate.target_entry_id
        if not target or target not in {item.entry_id for item in existing}:
            raise _PromotionConflict(
                "TARGET_ENTRY_NOT_IN_SAME_SUBJECT_SCOPE",
                tuple(item.entry_id for item in existing),
            )
        prior = next(item for item in existing if item.entry_id == target)
        action = candidate.proposed_action
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

    # LLM: USER facts require user_explicit evidence and SOUL alone requires current-turn
    # confirmation. AGENTS may be maintained autonomously but still uses the same owner path,
    # exact-entry CAS, quota, version history, and injection scanner.
    # 函数用途: 通过唯一 PersonaRepository 提交人格目标。
    def _commit_persona(
        self,
        candidate: MemoryCandidate,
        *,
        target: str,
        reviewer: str,
        confirmed: bool,
    ) -> str:
        if target in {"user", "soul"} and candidate.origin != "user_explicit":
            raise ValueError(f"{target.upper()} promotion requires user_explicit evidence")
        if target == "soul" and not confirmed:
            raise ValueError("SOUL promotion requires explicit user confirmation")
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
        if action == "add":
            adopted = self._adopt_existing_persona_tool_write(candidate, target=target)
            if adopted:
                return adopted
        quote = next(
            (str(ref.get("quote") or "") for ref in candidate.source_message_refs if str(ref.get("quote") or "")),
            "",
        )
        result = self.persona.mutate(
            PersonaMutationRequest(
                target=target,
                action=action,
                content=content,
                entry_id=candidate.target_entry_id,
                source_quote=quote,
                confirmed=confirmed or target in {"user", "agents"},
                source=f"memory-promotion:{reviewer}:{candidate.candidate_id}",
            )
        )
        entry_id = str(result.get("entry_id") or candidate.target_entry_id)
        return f"{target.upper()}.md#{entry_id or result.get('sha256', '')}"

    # LLM: When update_persona already committed the same deterministic entry in this turn, the
    # curator must adopt that exact entry instead of writing its paraphrase. Every check is typed:
    # verified tool, exact parameters, deterministic id, and current repository entry.
    # 函数用途: 识别已由 update_persona 正式写入的同一条人格事实并复用其引用。
    def _adopt_existing_persona_tool_write(
        self,
        candidate: MemoryCandidate,
        *,
        target: str,
    ) -> str:
        target_entry_id = str(candidate.target_entry_id or "").strip()
        if not target_entry_id or not candidate.source_tool_refs:
            return ""
        report = self.persona.list_entries(target)
        entries = report.get("entries")
        current_entries = entries if isinstance(entries, list) else []
        for ref in candidate.source_tool_refs:
            verified = self.tool_verifier.verify(ref)
            if verified is None or not _is_successful_persona_add(verified, target=target):
                continue
            direct_content = str(verified.parameters.get("content") or "").strip()
            if not direct_content or persona_entry_id(target, direct_content) != target_entry_id:
                continue
            if any(
                isinstance(entry, dict)
                and str(entry.get("entry_id") or "").strip() == target_entry_id
                and str(entry.get("content") or "").strip() == direct_content
                for entry in current_entries
            ):
                return f"{target.upper()}.md#{target_entry_id}"
        return ""


# LLM: Persona adoption trusts only canonical verifier output and exact structured parameters;
# candidate prose, model status words, and semantic similarity never authorize reuse.
# 函数用途: 判断核验后的工具证据是否为目标人格文件的成功 add。
def _is_successful_persona_add(
    verified: VerifiedToolEvidence,
    *,
    target: str,
) -> bool:
    canonical_tool = str(verified.canonical_ref.get("tool") or "").strip()
    action = str(verified.parameters.get("action") or "add").strip().lower()
    actual_target = str(verified.parameters.get("target") or "").strip().lower()
    return (
        verified.successful
        and verified.terminal_status == "succeeded"
        and verified.effect_outcome not in {"", "unknown", "not_started"}
        and canonical_tool == "update_persona"
        and action == "add"
        and actual_target == target
    )


# LLM: PromotionService 不解析聊天意图；只消费已审核 Candidate 和两个 typed verifier。
# 类用途: 唯一执行 long_term、Persona、lesson 与 HOT 正式晋升。
class MemoryPromotionService(_PromotionCommitMixin):
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

    # LLM: Automatic promotion covers every host-authorized non-SOUL target. Evidence, exact
    # mutation ids, conflicts, thresholds, temporary expiry and target repositories still fail closed.
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
        # 权限闸(automatic=True)：必须显式 auto_eligible；legacy 空值或任何未知值均在
        # evidence/status/target mutation 前 fail-closed，只有 approved + manual 路径可晋升。
        if automatic and str(candidate.promotion_mode or "").strip().lower() != "auto_eligible":
            return MemoryPromotionResult(
                candidate_id,
                False,
                candidate.status,
                "PROMOTION_MANUAL_REQUIRED",
            )
        evidence_code = self._verify_evidence(candidate)
        if evidence_code:
            return self._block_missing_evidence(candidate, reviewer, evidence_code)
        candidate = self._fill_default_target(candidate, reviewer)
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
            candidate = self._apply_automatic_policy(candidate, reviewer)
        if candidate.status != "approved":
            return MemoryPromotionResult(
                candidate_id,
                False,
                candidate.status,
                "REVIEW_REQUIRED",
            )
        try:
            promotion_ref = self._commit_with_conflict_handling(
                candidate, reviewer, confirmed
            )
        except _PromotionBlocked as exc:
            return MemoryPromotionResult(
                candidate.candidate_id,
                False,
                exc.status,
                exc.reason_code,
            )
        return self._finalize_promotion(candidate, reviewer, promotion_ref)

    # LLM: 宿主确定性补全仅修复 user_explicit 长期事实遗漏的 target；Persona、lesson、HOT
    # 必须由各自生产入口显式声明结构化 target，不能靠正文猜测，也不代表它们需要人工审核。
    # 函数用途: 空 target 的 user_explicit 长期事实候选补 default long_term。
    def _fill_default_target(
        self,
        candidate: MemoryCandidate,
        reviewer: str,
    ) -> MemoryCandidate:
        if str(candidate.promotion_target or "").strip().lower() in {"", "none"}:
            if (
                candidate.origin == "user_explicit"
                and candidate.candidate_type in _LONG_TERM_TARGET_TYPES
            ):
                candidate = self.candidates.transition(
                    candidate.candidate_id,
                    candidate.status,
                    reviewer=reviewer,
                    promotion_target="long_term",
                )
        return candidate

    # LLM: automatic 路径只做结构化放行,不做任何文本判断。
    # 函数用途: pending_review 候选按 conservative_v1 自动策略转 approved。
    def _apply_automatic_policy(
        self,
        candidate: MemoryCandidate,
        reviewer: str,
    ) -> MemoryCandidate:
        if candidate.status == "pending_review":
            candidate = self.candidates.transition(
                candidate.candidate_id,
                "approved",
                reviewer=reviewer,
                review_note="owner_autonomous_v1：结构化权威与安全门通过。",
            )
        return candidate

    # LLM: 冲突/门槛失败转稳定码,候选留在可复升状态,不崩溃。
    # 函数用途: 提交目标并把失败转 _PromotionBlocked 供主流程返回。
    def _commit_with_conflict_handling(
        self,
        candidate: MemoryCandidate,
        reviewer: str,
        confirmed: bool,
    ) -> str:
        try:
            return self._commit_target(candidate, reviewer=reviewer, confirmed=confirmed)
        except _PromotionConflict as exc:
            blocked = self.candidates.transition(
                candidate.candidate_id,
                "blocked_conflict",
                reviewer=reviewer,
                review_note=exc.reason_code,
                conflicts_with=list(exc.conflicts_with),
            )
            raise _PromotionBlocked(blocked.status, exc.reason_code) from None
        except ValueError as exc:
            # lesson/HOT 重复证据门槛不满足(lessons.py:_validate_lesson_candidate):
            # 自动路径保持原状态干净失败,不崩溃——候选留在 approved,证据凑齐后下次再晋升;
            # 稳定原因码供 curator 兜底与 CLI 显示。其他 target 的 ValueError 照常外抛。
            if str(candidate.promotion_target or "").strip().lower() in {"lesson", "hot"}:
                raise _PromotionBlocked(candidate.status, "PROMOTION_THRESHOLD_NOT_MET") from None
            raise

    # LLM: 总量闸:long_term 写成功后顺带治理(超上限按访问信号浓缩,user_explicit 不淘汰)。
    # 放在 promote 成功路径,任何写入口(remember/curator)都自动触发,无需单独 cron。
    # 函数用途: 晋升成功后治理、标记 promoted 并按需脱敏 remove 候选。
    def _finalize_promotion(
        self,
        candidate: MemoryCandidate,
        reviewer: str,
        promotion_ref: str,
    ) -> MemoryPromotionResult:
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


# LLM: Automatic policy consumes the shared host authority result plus exact mutation/conflict/scope
# facts. It never uses confidence, prose, elapsed time, or a model-authored approval claim.
# 函数用途: 返回空表示非 SOUL 候选可自主尝试正式写入，否则返回稳定阻塞码。
def _automatic_policy_block(candidate: MemoryCandidate) -> str:
    if host_promotion_mode(candidate) != "auto_eligible":
        return "AUTO_POLICY_AUTHORITY_REQUIRES_REVIEW"
    if candidate.proposed_action in {"replace", "remove", "merge"} and not candidate.target_entry_id:
        return "AUTO_POLICY_EXACT_TARGET_REQUIRED"
    if candidate.conflicts_with:
        return "AUTO_POLICY_CONFLICT_UNRESOLVED"
    scope = MemoryScope.from_value(candidate.scope)
    if scope.scope_type == "temporary" and not candidate.valid_until:
        return "AUTO_POLICY_TEMPORARY_REQUIRES_EXPIRY"
    return ""


# LLM: No review or autonomous path can manufacture formal user facts or SOUL authority.
# AGENTS is intentionally model-maintainable, while temporary/session instructions still cannot
# become a durable USER profile.
# 函数用途: 返回模型自主或人工路径都不能绕过的正式事实与 SOUL/USER 权威阻塞码。
def _absolute_authority_block(candidate: MemoryCandidate) -> str:
    if candidate.origin == "model_inferred" and candidate.promotion_target in {
        "long_term",
        "user",
        "soul",
    }:
        return "MODEL_INFERRED_FORMAL_AUTHORITY_FORBIDDEN"
    if (
        candidate.promotion_target in {"user", "soul"}
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


# LLM: 与 store 共用同一 normalized 合同，promotion 不复制第三套规范化。
# 函数用途: 在既有同 subject/scope 条目中按规范化正文找同文条目。
def _same_content_in(
    records: list[MemoryRecord],
    candidate: MemoryCandidate,
) -> MemoryRecord | None:
    normalized = normalized_memory_content(candidate.content)
    return next(
        (item for item in records if normalized_memory_content(item.content) == normalized),
        None,
    )


# LLM: 观察入口已拒绝脏值,但已落账的旧候选绕过入口仍会走到晋升边界,必须再校验一次。
# 函数用途: add 候选必须使用可召回的规范 scope,非法即 blocked_conflict,不落任何字节。
def _ensure_recallable_scope_for_add(candidate: MemoryCandidate) -> None:
    if candidate.proposed_action != "add":
        return
    try:
        MemoryScope.for_new_observation(candidate.scope)
    except ValueError as exc:
        raise _PromotionConflict(
            "SCOPE_NOT_RECALLABLE",
            tuple(),
        ) from exc


# LLM: 与写入侧共用 canonical 合同：project 的 project:/task: 前缀是同一身份的两种拼写，
# 同 scope 判定必须归一后比较，否则旧 task:<id> 账本与新 project:<id> 候选会生成第二条
# 正式事实。坏值退回字面相等（不扩大幂等合并范围，fail-closed）。
# 函数用途: 判断 long_term 条目 scope 与候选 scope 是否同一身份。
def _same_scope_identity(scope_type: str, scope_key: object, candidate_scope: MemoryScope) -> bool:
    from .scope_contract import canonical_scope_key

    if scope_type != candidate_scope.scope_type:
        return False
    try:
        return canonical_scope_key(scope_type, scope_key) == canonical_scope_key(
            candidate_scope.scope_type, candidate_scope.scope_key
        )
    except ValueError:
        return str(scope_key or "") == str(candidate_scope.scope_key or "")


# LLM: conflict 比较明确读取 subject_key + scope_type + scope_key；适用条件文字不参与身份猜测。
# 函数用途: 查找同主题同范围（canonical 等价）的 active 长期记忆。
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
            and _same_scope_identity(
                str(attributes.get("scope_type") or "legacy"),
                attributes.get("scope_key") or "legacy",
                scope,
            )
        ):
            result.append(record)
    return result


# LLM: 空 add 提交只能作为存储/晋升身份漂移的安全诊断；不得按正文返回另一个 scope 的假成功 ref。
# 函数用途: 为结构化冲突结果列出精确同正文旧条目 ID，不负责语义合并或选择替换目标。
def _exact_content_entry_ids(
    records: list[MemoryRecord],
    candidate: MemoryCandidate,
) -> tuple[str, ...]:
    normalized = normalized_memory_content(candidate.content)
    return tuple(
        item.entry_id
        for item in records
        if normalized_memory_content(item.content) == normalized and item.entry_id
    )


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


# LLM: 提交失败已落状态迁移的载体；只携带稳定 status 与原因码。
# 类用途: 让主流程把 blocked/门槛失败转成 MemoryPromotionResult，不吞回退路径。
class _PromotionBlocked(RuntimeError):
    # 函数用途: 构造一次已迁移状态的晋升阻断。
    def __init__(self, status: str, reason_code: str) -> None:
        self.status = status
        self.reason_code = reason_code
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
