# LLM: 自动晋升须先核来源、权威、证据并完整快照CAS审核；原quota→policy→candidate→formal/index/mark及condense不拆。
# 模块用途: 唯一候选晋升算法，合格候选自动归档；C不缓存source、不将长业务或费用搬进Root。
from __future__ import annotations

"""统一候选证据审核、冲突裁决和正式晋升服务。"""

# LLM: 正式晋升仅经本服务；自动审核与人工审核共守原quota/policy/candidate锁、证据及精确目标CAS，AGENTS禁写。
# 模块用途: 核验候选后沿原正式仓库归档并标promoted，保留人工审核与已提交删除恢复。

import hashlib
import json
import logging
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol

from ..backends.bounded_call import BoundedCallExitUnconfirmedError
from ..capability.persona_repository import (
    PersonaMutationRequest,
    PersonaRepository,
    _mutate_persona_document,
    persona_entry_id,
)
from ..common.cache_freshness import cache_entry_trustworthy
from ..common.global_write_commit import GlobalWriteCommitUnavailable, execute_global_write
from ..common.global_write_guard import GlobalWriteInWorkerError
from ..common.json_io import (
    locked_json_path,
    read_json_object_report,
    write_json_file_atomic_unlocked,
)
from ..common.owner_write_errors import (
    LocalControlCoordinatorUnavailable,
    OwnerManagementIngressUnavailable,
)
from ..common.request_execution_ports import (
    RequestExecutionContextUnavailable,
    RequestExecutionOutcomeUncertainError,
)
from ..common.tool_index_stream import (
    InvalidToolIndexObject,
    iter_decoded_tool_index_objects,
    tool_index_prescreen_pattern,
)
from ..common.tool_output_paths import tool_output_index_paths_for_lookup
from ..conversation.learning_source import projection_is_learnable
from ..io.cursor_cache import CursorCache, file_stamp
from ..user_space.owner_policy import read_learning_exclusions
from ..user_space.owner_quota import OwnerQuotaAdmission
from .candidate_models import (
    MemoryCandidate,
    MemoryScope,
    host_promotion_mode,
    normalize_reference_list,
)
from .candidates import CandidateService, CandidateStateChangedError
from .jsonl import (
    JsonlMemory,
    MemoryLibraryWriteCapExceeded,
    MemoryRecord,
    MemoryVersionConflict,
)
from .learning_exclusions import excluded_candidate_ids
from .lessons import HotRuleRepository, LessonRepository, LessonRoutingPendingError
from .operations import normalized_memory_content

# LLM: working_agreement 第 1 期起与事实/事件/项目同走唯一长期库（JSONL），不再写 AGENTS。
_LONG_TERM_TARGET_TYPES = frozenset({"long_term_fact", "event", "project", "working_agreement"})

# 写路径查重:表述变体(LCS 覆盖率)与长度差双闸,只合并"逐字近重复"的同一断言,
# 绝不把不同事实(加信息/换词义)并掉——漏并多存一条,误并丢信息,后者更糟。

# 英文关键词最短 3 个字符：太短没有区分度；只收 ASCII 词面，用于 BM25 跨语言命中。
_EN_KEYWORD_MIN_LEN_CHARS = 3
# 每条记录最多保留 12 个英文关键词：关键词集合有界。
_EN_KEYWORD_MAX_COUNT = 12
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
    # LLM: 学习排除复核的会话绑定来源（conversation_store）；None 时提交路径不做名单复核，
    #   保持既有调用方行为，接线后提交在 policy 锁内复核（mc4-3103）。
    exclusion_store: object | None = None


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
    # LLM: C实际LocalStore连接出生native不得被宽catch变成缺证据，普通读取失败仍原fallback，不新增证据状态。
    # 函数用途: 核验原工具终态，保实际来源拒绝对象与原档案回退边界。
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
            except Exception as exc:
                if getattr(self.ledger_store, "coordinator_io", False) and isinstance(
                        exc, (GlobalWriteCommitUnavailable, GlobalWriteInWorkerError,
                                                 LocalControlCoordinatorUnavailable, OwnerManagementIngressUnavailable)):
                    raise
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
            _ToolArchiveMatch(run_id, call_id, tool_name, claimed_operation_id),
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


# LLM: 原四元匹配条件打包传递，归属仍由owner根与结构化字段证明，不从正文或字节预筛推断成功。
# 类用途: 保存记忆晋升要核对的run/call/tool/operation身份。
@dataclass(frozen=True)
class _ToolArchiveMatch:
    run_id: str
    call_id: str
    tool_name: str
    operation_id: str


# LLM: 共用唯一索引读取器，任一坏JSON/非对象/IO/UTF-8仍拒绝该文件；语法校验不构造无关历史对象。
# 函数用途: 按原owner边界和四元身份流式筛选工具证据，不保存整份索引。
def _owner_tool_archive_matches(owner_root: Path, query: _ToolArchiveMatch) -> list[dict[str, object]]:
    prescreen = next((pattern for value in (query.run_id, query.call_id, query.operation_id, query.tool_name)
                      if (pattern := tool_index_prescreen_pattern((value,))) is not None), None)
    matches: list[dict[str, object]] = []
    for path in tool_output_index_paths_for_lookup(owner_root):
        matches.extend(_archive_file_matches(path, query, prescreen))
    return matches


# LLM: 文件级拒绝必须在扫描结束后提交候选；异常只吞原read_jsonl_objects_report会报告的IO/UTF-8和对象坏行。
# 函数用途: 验证一个索引文件并返回精确身份匹配的记录，坏文件没有部分证据。
def _archive_file_matches(path: Path, query: _ToolArchiveMatch, prescreen: re.Pattern[bytes] | None) -> list[dict[str, object]]:
    try:
        lines = iter_decoded_tool_index_objects(path, prescreen, validate_objects=True)
        return [row for _, row in lines if _archive_record_matches(row, query)]
    except (OSError, UnicodeDecodeError, InvalidToolIndexObject):
        return []


# LLM: 对象合法性由全行校验保证；精确比较保持原str/or/strip及嵌套operation_id规则，重复项仍保留。
# 函数用途: 判断一条工具索引记录是否对应需要核验的旧工具调用。
def _archive_record_matches(row: dict[str, object], query: _ToolArchiveMatch) -> bool:
    if not isinstance(row, dict):
        return False
    operation = row.get("tool_operation")
    operation_id = str(operation.get("operation_id") or "").strip() if isinstance(operation, dict) else ""
    return (str(row.get("run_id") or "").strip() == query.run_id
            and str(row.get("call_id") or "").strip() == query.call_id
            and str(row.get("tool") or "").strip() == query.tool_name
            and (not query.operation_id or operation_id == query.operation_id))


# LLM: verifier 流式查一条真实消息并核验 thread/role/hash/quote，不把整段原话返回调用方。
# 类用途: 将 ConversationStore 接到 PromotionService 的 typed evidence contract。
class ConversationMessageEvidenceVerifier:
    # LLM: 缓存只保存最多256个文件/消息键、每键8份已核验短quote证明，不保留完整消息对象或正文。
    # 函数用途: 初始化当前owner只读消息核验器及可随时丢弃的有界证明缓存。
    def __init__(self, conversation_store: object) -> None:
        self.conversation_store = conversation_store
        self._proof_cache = CursorCache(256)

    # LLM: 命中仍逐次核对hash/role/quote；新quote、文件变更或无真实路径时回原Store，缓存不能授权另一消息。
    # 函数用途: 核验消息证据，稳定文件重复引用复用最小证明，不复制或缓存历史正文。
    def verify(self, ref: dict[str, object]) -> dict[str, object] | None:
        thread_id = str(ref.get("thread_id") or "").strip()
        message_id = str(ref.get("message_id") or "").strip()
        messages = getattr(self.conversation_store, 'messages', None)
        reader = getattr(messages, 'by_id_report', None)
        if not thread_id or not message_id or not callable(reader):
            return None
        key = self._file_key(thread_id, message_id)
        quote = str(ref.get("quote") or "").strip()
        cached = self._proof_cache.get(key) if key is not None else None
        reader_identity = getattr(reader, "__func__", reader)
        proofs = cached[1] if cached is not None and cached[0] is reader_identity else {}
        proof = proofs.get(quote)
        if proof is not None:
            return dict(proof) if _proof_matches(ref, proof) else None
        message, errors = reader(thread_id, message_id)
        if (errors or message is None or str(getattr(message, "thread_id", "")) != thread_id
                or str(getattr(message, "message_id", "")) != message_id):
            return None
        content = str(message.content or "")
        content_hash = "sha256:" + hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()
        claimed_hash = str(ref.get("content_hash") or "")
        if claimed_hash != content_hash or not quote or len(quote) > 300 or quote not in content:
            return None
        claimed_role = str(ref.get("role") or message.role)
        if claimed_role != str(message.role):
            return None
        proof = {
            "message_id": message_id,
            "thread_id": thread_id,
            "role": str(message.role),
            "channel": str(message.channel),
            "content_hash": content_hash,
            "quote": quote,
            "created_at": float(message.created_at),
        }
        if key is not None:
            if key != self._file_key(thread_id, message_id):
                return None
            proofs = {**proofs, quote: dict(proof)}
            if len(proofs) > 8:
                proofs.pop(next(iter(proofs)))
            self._proof_cache.put(key, (reader_identity, proofs))
        return proof

    # LLM: 只用当前Store canonical路径和五字段stamp；近期文件与没有真实文件路径的替身不缓存。
    # 函数用途: 按Store实例、根目录、线程和消息隔离证据证明，文件改写/替换后自然失效。
    def _file_key(self, thread_id: str, message_id: str):
        try:
            storage = self.conversation_store.storage
            path = storage.message_path(thread_id)
            stamp = file_stamp(path.stat())
            if cache_entry_trustworthy(stamp.mtime_ns):
                return id(self.conversation_store), str(storage.root), thread_id, message_id, stamp
        except (AttributeError, OSError, TypeError, ValueError):
            pass
        return None


# LLM: 只复用已由原消息核验的短标量证明，调用方当前声明仍须逐项匹配；不读正文、不写任何账本。
# 函数用途: 判断重复引用是否与缓存的同一hash、角色和短引文完全一致。
def _proof_matches(ref: dict, proof: dict) -> bool:
    return (str(ref.get("content_hash") or "") == proof["content_hash"]
            and str(ref.get("role") or proof["role"]) == proof["role"]
            and str(ref.get("quote") or "").strip() == proof["quote"])


# LLM: result 只说明机器决策和正式 ref，不包含候选或历史记忆正文。
# 类用途: 给 CLI、Curator 和测试返回统一晋升结果。
@dataclass(frozen=True)
class MemoryPromotionResult:
    candidate_id: str
    promoted: bool
    status: str
    reason_code: str
    promotion_ref: str = ""
    formal_memory_changed: bool = False

    # LLM: 结果只序列化决策码和正式ref；未新增提交时省略变更标志，保持旧消费者字段兼容。
    # 函数用途: 将晋升结果转换为 CLI/Curator 可消费字典。
    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        if not self.formal_memory_changed:
            payload.pop("formal_memory_changed")
        return payload


# LLM: 提交上下文显式携带外层 quota；不把准入藏在 service 的可变属性中，线程间不会串用。
# 类用途: 保存一次提交的审阅者、确认事实和已领取的配额准入。
@dataclass(frozen=True)
class _PromotionWriteContext:
    reviewer: str
    confirmed: bool
    admission: OwnerQuotaAdmission


# LLM: 正式目标写入处显式携带副作用事实；引用被复用不等于本轮写入。
# 类用途: 区分实际提交与仅采用已有正式引用。
@dataclass(frozen=True)
class _TargetCommit:
    reference: str
    changed: bool


# LLM: 四种目标共用唯一仓库，接收已经取得的外层配额准入。
# 类用途: 执行正式目标写入，不重复获取 owner quota 锁。
class _PromotionCommitMixin:
    # LLM: 每个正式目标只有一个repository，正式变更只取其锁内回执；引用复用不能猜成一次新写入。
    # 函数用途: 提交目标并返回正式引用及本次真实写入事实。
    def _commit_target(
        self,
        candidate: MemoryCandidate,
        context: _PromotionWriteContext,
    ) -> _TargetCommit:
        target = _promotion_target(candidate)
        if target == "long_term":
            return self._commit_long_term(candidate, context.admission)
        if target in {"user", "soul", "agents"}:
            return self._commit_persona(
                candidate,
                target,
                context,
            )
        if target == "lesson":
            lesson, changed = self.lessons.promote_with_receipt(
                candidate,
                min_occurrences=self.lesson_min_occurrences,
            )
            return _TargetCommit(lesson.path + "#" + lesson.lesson_id, changed)
        if target == "hot":
            lesson = self.lessons.get(candidate.target_entry_id)
            hot, changed = self.hot.promote_with_receipt(
                candidate,
                lesson=lesson,
                min_occurrences=self.hot_min_occurrences,
            )
            return _TargetCommit("memory-hot.md#" + hot.hot_id, changed)
        raise ValueError(f"candidate promotion_target {target!r} is not promotable")

    # LLM: subject 冲突只在同 scope 内；新时间不能覆盖，replace/remove 必须精确 target_entry_id。
    # 函数用途: 按 proposed_action 分发长期记忆提交。
    def _commit_long_term(self, candidate: MemoryCandidate, admission: OwnerQuotaAdmission) -> _TargetCommit:
        if candidate.candidate_type not in _LONG_TERM_TARGET_TYPES:
            raise ValueError("candidate type cannot be stored in long_term memory")
        if candidate.origin == "model_inferred" and candidate.candidate_type != "working_agreement":
            raise ValueError("model_inferred candidate cannot become a formal fact")
        _ensure_recallable_scope_for_add(candidate)
        existing = _same_subject_scope(self.long_term.all(), candidate)
        same_content = _same_content_in(existing, candidate)
        if same_content is not None and candidate.proposed_action == "add":
            return _TargetCommit("memory/long_term/memory.jsonl#" + same_content.entry_id, False)
        if candidate.proposed_action == "add":
            return _TargetCommit(self._commit_long_term_add(candidate, existing, admission), True)
        return _TargetCommit(self._commit_long_term_existing(candidate, existing, admission), True)

    # LLM: 保留原 add 调用接缝；独立提交原语仍共用已有准入、冲突判据和确定性 ID。
    # 函数用途: 委托正式仓库提交新增长期事实，不隐式改为替换。
    def _commit_long_term_add(self, candidate: MemoryCandidate, existing: list[MemoryRecord], admission: OwnerQuotaAdmission) -> str:
        return _commit_long_term_add(self, candidate, existing, admission)

    # LLM: replace/merge 必须精确 target_entry_id 且 target 在 same subject scope 内；提交前必须
    # 确认持久化的观察版本与目标一致；缺版本或版本变化按冲突阻断，不以墙钟或临提交版本补猜；
    # 读取与提交之间的并发由 apply_batch 的 expected_version CAS 兜底并同样转成冲突结果。
    # 函数用途: 提交对既有长期条目的 replace/merge/remove。
    def _commit_long_term_existing(
        self,
        candidate: MemoryCandidate,
        existing: list[MemoryRecord],
        admission: OwnerQuotaAdmission,
    ) -> str:
        target = candidate.target_entry_id
        if not target or target not in {item.entry_id for item in existing}:
            raise _PromotionConflict(
                "TARGET_ENTRY_NOT_IN_SAME_SUBJECT_SCOPE",
                tuple(item.entry_id for item in existing),
            )
        prior = next(item for item in existing if item.entry_id == target)
        stale_code = _stale_target_observation(candidate, prior)
        if stale_code:
            raise _PromotionConflict(stale_code, (target,))
        action = candidate.proposed_action
        if action in {"replace", "merge"}:
            try:
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
                            "expected_version": candidate.target_entry_version,
                        }
                    ], _admission=admission,
                )[0]
            except MemoryVersionConflict:
                raise _PromotionConflict("TARGET_ENTRY_CONCURRENT_WRITE", (target,)) from None
            return "memory/long_term/memory.jsonl#" + record.entry_id
        if action == "remove":
            try:
                self.long_term.apply_batch([{"action": "remove", "entry_id": target,
                    "source": f"candidate:{candidate.candidate_id}", "expected_version": candidate.target_entry_version}],
                    _admission=admission)
            except MemoryVersionConflict:
                raise _PromotionConflict("TARGET_ENTRY_CONCURRENT_WRITE", (target,)) from None
            return "memory/long_term/memory.jsonl#" + target + ":removed"
        raise ValueError("long_term promotion proposed_action must be add/replace/remove/merge")

    # LLM: USER仍核user_explicit，SOUL仍需原确认；AGENTS最终提交一律拒绝，只能由用户 /agents 预览确认。
    # confirmation. 第 0 期（v2）：promotion 的最终提交入口一律拒绝 agents——本方法对 agents
    # 直接抛出稳定的 _PromotionBlocked，任何到达提交层的路径都无法再写 AGENTS；用户直接要求
    # 写AGENTS走用户 /agents 预览确认，不经本服务。
    # 函数用途: 通过唯一 PersonaRepository 提交人格目标。
    def _commit_persona(
        self,
        candidate: MemoryCandidate,
        target: str,
        context: _PromotionWriteContext,
    ) -> _TargetCommit:
        reviewer, confirmed = context.reviewer, context.confirmed
        if target == "agents":
            raise _PromotionBlocked(candidate.status, "AGENTS_WRITE_USER_ONLY")
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
                return _TargetCommit(adopted, False)
        quote = next(
            (str(ref.get("quote") or "") for ref in candidate.source_message_refs if str(ref.get("quote") or "")),
            "",
        )
        result = _mutate_persona_document(self.persona, context.admission,
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
        return _TargetCommit(f"{target.upper()}.md#{entry_id or result.get('sha256', '')}", bool(result.get("changed")))

    # LLM: 保留原提交接缝，结构化事实验证独立于写入编排，避免四目标 mixin 继续膨胀。
    # 函数用途: 委托只读验证器复用本回合已正式提交的人格事实引用。
    def _adopt_existing_persona_tool_write(
        self,
        candidate: MemoryCandidate,
        *,
        target: str,
    ) -> str:
        return _adopt_existing_persona_tool_write(self, candidate, target=target)


# LLM: add 与写入编排分开，不隐式转 replace；同 subject/scope 冲突与空提交仍按原身份判据拒绝。
# 函数用途: 用已领取的准入向唯一长期仓库提交 add，返回确定性正式引用。
def _commit_long_term_add(
    service: MemoryPromotionService,
    candidate: MemoryCandidate,
    existing: list[MemoryRecord],
    admission: OwnerQuotaAdmission,
) -> str:
    if existing:
        raise _PromotionConflict(
            "SUBJECT_SCOPE_CONFLICT_REQUIRES_REPLACE",
            tuple(item.entry_id for item in existing),
        )
    entry_id = _long_term_entry_id(candidate.candidate_id)
    record = service.long_term.apply_batch(
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
        ], _admission=admission,
    )
    if not record:
        raise _PromotionConflict(
            "LONG_TERM_ADD_IDEMPOTENCY_IDENTITY_MISMATCH",
            _exact_content_entry_ids(service.long_term.all(), candidate),
        )
    committed = record[0]
    return "memory/long_term/memory.jsonl#" + committed.entry_id


# LLM: add与旧格式删除恢复必须共用精确ID派生，不能按内容或subject猜历史归属。
# 函数用途: 从候选稳定ID计算唯一正式记忆条目ID。
def _long_term_entry_id(candidate_id: str) -> str:
    return "memory-" + hashlib.sha256(candidate_id.encode("utf-8")).hexdigest()[:24]


# LLM: 已被 update_persona 提交的同一事实只认成功工具、精确参数、确定性 ID 与当前仓库，不写改述。
# 函数用途: 只读核对既有工具写入的人格条目，返回精确引用或空串。
def _adopt_existing_persona_tool_write(
    service: MemoryPromotionService,
    candidate: MemoryCandidate,
    *,
    target: str,
) -> str:
    target_entry_id = str(candidate.target_entry_id or "").strip()
    if not target_entry_id or not candidate.source_tool_refs:
        return ""
    report = service.persona.list_entries(target)
    entries = report.get("entries")
    current_entries = entries if isinstance(entries, list) else []
    for ref in candidate.source_tool_refs:
        verified = service.tool_verifier.verify(ref)
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


# LLM: 完整顺序为 quota→policy→正式目标；冲突候选提交在退出外锁后执行，不能 policy→quota。
# 函数用途: 在 policy 锁内复核名单后提交目标，失败按稳定码返回。
def _commit_with_exclusion_guard(
    service: MemoryPromotionService,
    candidate: MemoryCandidate,
    *,
    reviewer: str,
    confirmed: bool,
) -> tuple[_TargetCommit, MemoryCandidate]:
    try:
        return _commit_admitted(service, candidate, (reviewer, confirmed))
    except _PromotionConflict as exc:
        blocked = service.candidates.transition(candidate, "blocked_conflict",
            reviewer=reviewer, review_note=exc.reason_code, conflicts_with=list(exc.conflicts_with))
        raise _PromotionBlocked(blocked.status, exc.reason_code) from None
    except MemoryLibraryWriteCapExceeded as exc:
        # 写入硬帽拒绝（设计 §3.5.1）：候选保持原状态、不丢弃；结构化码原样上抛。
        raise _PromotionBlocked(candidate.status, exc.code) from None


# LLM: quota→policy→候选锁贯穿正式写入、promoted和remove脱敏，锁内回调用同一准入；解锁前不暴露半成功。
# 函数用途: 依目标的原配额策略领取准入，在名单锁内核对并写入正式目标。
def _commit_admitted(service: MemoryPromotionService, candidate: MemoryCandidate, review: tuple[str, bool]) -> tuple[_TargetCommit, MemoryCandidate]:
    enforcer = service.persona.quota_enforcer
    if _promotion_target(candidate) == "long_term" and service.long_term.quota_enforcer is not None:
        enforcer = service.long_term.quota_enforcer
    with enforcer.admission() as admission:
        with _learning_exclusion_scope(service), service.candidates.locked_snapshot(candidate):
            _ensure_not_excluded(service, candidate)
            context = _PromotionWriteContext(*review, admission)
            outcome = _commit_target_blocked(service, candidate, context)
            promoted = _complete_candidate_commit(service, candidate, context, outcome.reference)
            return outcome, promoted


# LLM: 调用方已正式提交并持quota/policy/候选锁；删除终态写入失败须保留本轮提交事实及清理责任。
# 函数用途: 正式写入后同步候选终态，失败时准确报告删除已提交而非吞掉异常。
def _complete_candidate_commit(service, candidate, context, promotion_ref):
    from ._jsonl_indexing import removal_cleanup_phase

    is_remove = candidate.proposed_action == 'remove' and _promotion_target(candidate) == 'long_term'
    with removal_cleanup_phase(formal_memory_changed=True, enabled=is_remove):
        return service.candidates.mark_promoted_admitted(candidate.candidate_id, context.admission,
            (context.reviewer, promotion_ref, ""))


# LLM: 准入与写入共用同一目标解析，空/none 的用户事实仍落 long_term，不能错领 disabled persona 配额。
# 函数用途: 统一规范化最终晋升目标，保持原用户事实默认落点。
def _promotion_target(candidate: MemoryCandidate) -> str:
    from .candidate_models import effective_promotion_target

    return effective_promotion_target(candidate.promotion_target, candidate.origin, candidate.candidate_type)


# LLM: C只把同原policy锁FD出生交活scope，锁等待和原名单读取仍Root外，默认未接线/Count0不变。
# 函数用途: 持有原policy锁覆盖候选/formal同步提交，不创建第二名单状态。
@contextmanager
def _learning_exclusion_scope(service: MemoryPromotionService) -> Iterator[None]:
    if service.exclusion_store is None:
        yield
        return
    options = {} if not service.coordinator_io else {"birth_commit": _coordinator_policy_birth}
    with locked_json_path(service.persona.owner_home / "memory_policy.json", **options):
        yield


# LLM: 名单不可读或来源命中都拒绝提交（fail-closed）；只读结构化 refs 与通道绑定。
# 函数用途: 在 policy 锁内复核候选来源是否命中学习排除名单。
def _ensure_not_excluded(service: MemoryPromotionService, candidate: MemoryCandidate) -> None:
    code = _learning_exclusion_block(service, candidate)
    if code:
        raise _PromotionBlocked(candidate.status, code)


# 函数用途: 读最新名单并判定一条候选是否命中；返回稳定原因码或空串。
def _learning_exclusion_block(
    service: MemoryPromotionService, candidate: MemoryCandidate
) -> str:
    if service.exclusion_store is None:
        return ""
    entries, error = read_learning_exclusions(
        SimpleNamespace(
            owner_memory_policy_json=service.persona.owner_home / "memory_policy.json"
        )
    )
    if error is not None:
        return "LEARNING_SOURCE_UNREADABLE"
    excluded = frozenset(entry.channel_conversation_id for entry in entries)
    if not excluded:
        return ""
    hits = excluded_candidate_ids(
        conversation_store=service.exclusion_store,
        rows=[candidate],
        excluded_conversation_ids=excluded,
    )
    return "LEARNING_SOURCE_EXCLUDED" if hits else ""


# LLM: 提交失败已落状态迁移的载体；只把冲突/门槛失败转稳定码，其他异常照常外抛。
# LLM: C的原OwnerManagementIngressUnavailable也是ValueError，须先同对象穿出；普通lesson/HOT门槛错误仍原包装。
# 函数用途: 提交目标并把失败转 _PromotionBlocked 供主流程返回。
def _commit_target_blocked(
    service: MemoryPromotionService,
    candidate: MemoryCandidate,
    context: _PromotionWriteContext,
) -> _TargetCommit:
    try:
        return service._commit_target(candidate, context)
    except ValueError as exc:
        if service.coordinator_io and isinstance(exc, OwnerManagementIngressUnavailable):
            raise
        # lesson/HOT 重复证据门槛不满足(lessons.py:_validate_lesson_candidate):
        # 自动路径保持原状态干净失败,不崩溃——候选留在 approved,证据凑齐后下次再晋升;
        # 稳定原因码供 curator 兜底与 CLI 显示。其他 target 的 ValueError 照常外抛。
        if str(candidate.promotion_target or "").strip().lower() in {"lesson", "hot"}:
            raise _PromotionBlocked(candidate.status, "PROMOTION_THRESHOLD_NOT_MET") from None
        raise


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


# LLM: 只抽出证据/状态变更前的纯判定，保持已晋升→自动权限→AGENTS→排除标记的优先级。
# 函数用途: 返回已有终态或结构化提前拒绝；None表示继续核验证据。
def _promotion_precheck(candidate: MemoryCandidate, automatic: bool) -> MemoryPromotionResult | None:
    if candidate.status == "promoted":
        return MemoryPromotionResult(candidate.candidate_id, True, "promoted", "ALREADY_PROMOTED", candidate.promotion_ref)
    code = _promotion_precheck_code(candidate, automatic)
    if code:
        return MemoryPromotionResult(candidate.candidate_id, False, candidate.status, code)
    return None


# LLM: 仅返回拒绝码，顺序仍为自动权限、AGENTS禁写、排除名单；无状态或存储副作用。
# 函数用途: 用平铺的提前返回保持各项拒绝原因的优先级。
def _promotion_precheck_code(candidate: MemoryCandidate, automatic: bool) -> str:
    if automatic and str(candidate.promotion_mode or "").strip().lower() != "auto_eligible":
        return "PROMOTION_MANUAL_REQUIRED"
    if str(candidate.promotion_target or "").strip().lower() == "agents":
        return "AUTO_AGENTS_WRITE_FORBIDDEN" if automatic else "AGENTS_WRITE_USER_ONLY"
    return str(candidate.promotion_exclusion or "").strip() if automatic else ""


# LLM: 不解析聊天意图；消费宿主来源及typed verifier，自动仅审核结构化合格快照，人工仍走原精确审核。
# 类用途: 唯一执行候选审核及long_term、Persona、lesson与HOT归档，保原安全门和仓库。
class MemoryPromotionService(_PromotionCommitMixin):
    # LLM: Construction accepts one authority bundle and one policy object, preventing partial
    # alternative wiring while preserving rejecting verifier defaults.
    # LLM: coordinator_io仅原policy锁出生/实际native透传策略，不保存source，不改review/promote/condense/费用或锁序。
    # 函数用途: 用真实领域对象装配原晋升服务，C每次末写仍靠当次原scope。
    def __init__(
        self,
        *,
        dependencies: MemoryPromotionDependencies,
        policy: MemoryPromotionPolicy | None = None,
        coordinator_io: bool = False,
    ) -> None:
        self.coordinator_io = coordinator_io
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
        self.exclusion_store = dependencies.exclusion_store
        self.lesson_min_occurrences = max(2, int(thresholds.lesson_min_occurrences))
        self.hot_min_occurrences = max(3, int(thresholds.hot_min_occurrences))

    # LLM: review只推进唯一CandidateService的结构化审核；最终写入仍重核证据、当前目标版本和候选CAS。
    # 函数用途: 人工审核精确候选；传完整快照时复用原CandidateService的锁内CAS，不允许确认期间换正文。
    def review(
        self,
        candidate_id: str | MemoryCandidate,
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

    # LLM: 第 0 期（设计 v2 §3.1）：memory promotion 的最终提交入口一律拒绝 agents 目标——
    # 自动、积压重试、人工晋升与审核后提交都算；候选保持原状态并带结构化原因码，不丢弃、
    # 不标失败。用户直接要求写 AGENTS 走 /agents 预览确认，不经本服务。证据、精确 mutation id、
    # 冲突、门槛、临时过期与目标仓库仍照常 fail-closed。
    #   正式提交沿原quota/policy/candidate锁序，自动审核仅批准通过全部门的精确快照，删除恢复只清既有墓碑。
    # 函数用途: 核验证据后提交；批次上下文延后治理，单条调用也在释放业务锁后收尾。
    def promote(
        self,
        candidate_id: str,
        *,
        reviewer: str = "memory-promotion-service",
        automatic: bool = False,
        confirmed: bool = False,
        expected_candidate: MemoryCandidate | None = None,
    ) -> MemoryPromotionResult:
        with promotion_batch(long_term=self.long_term, candidates=self.candidates, coordinator_io=self.coordinator_io):
            try:
                return self._promote_snapshot(candidate_id, reviewer=reviewer, automatic=automatic, confirmed=confirmed,
                                              expected_candidate=expected_candidate)
            except CandidateStateChangedError as exc:
                return MemoryPromotionResult(candidate_id, False, exc.status, "CANDIDATE_CHANGED_AFTER_VERIFICATION")

    # LLM: 各条仍走原完整审核/CAS；空批也偿还持久治理欠账，condense不持有policy/candidate或配额锁。
    # 函数用途: 成批自动晋升只读筛出的候选，整批只做一次总量治理。
    def promote_ready(self, *, limit: int = 32) -> tuple[MemoryPromotionResult, ...]:
        with promotion_batch(long_term=self.long_term, candidates=self.candidates, coordinator_io=self.coordinator_io):
            rows = self.candidates.readonly_rows(statuses={"pending_review", "approved"}, limit=limit)
            return tuple(self.promote(row.candidate_id, automatic=True) for row in rows)

    # LLM: 先核完整预期及证据/权威；自动策略通过才CAS审核pending，最终提交仍锁完整快照；清理错误不猜成功。
    # 函数用途: 合并默认落点与自动批准的预提交写入；精确恢复旧提交，正式写前登记耐久治理责任。
    def _promote_snapshot(
        self,
        candidate_id: str,
        *,
        reviewer: str,
        automatic: bool,
        confirmed: bool,
        expected_candidate: MemoryCandidate | None = None,
    ) -> MemoryPromotionResult:
        from .promotion_remove_recovery import recover_committed_update, recover_promotion_result

        candidate = self.candidates.get(candidate_id)
        if expected_candidate is not None and candidate != expected_candidate:
            return MemoryPromotionResult(candidate_id, False, candidate.status, "CANDIDATE_CHANGED_AFTER_VERIFICATION")
        recovery = recover_promotion_result(self, candidate, reviewer)
        if recovery is None:
            recovery = recover_committed_update(self, candidate, reviewer)
        if recovery is not None:
            return recovery
        early_result = _promotion_precheck(candidate, automatic)
        if early_result is not None:
            return early_result
        evidence_code = self._verify_evidence(candidate)
        if evidence_code:
            return self._block_missing_evidence(candidate, reviewer, evidence_code)
        if not automatic or candidate.status != "pending_review":
            candidate = self._fill_default_target(candidate, reviewer)
        effective = replace(candidate, promotion_target=_promotion_target(candidate))
        authority_code = _absolute_authority_block(effective)
        if authority_code:
            return MemoryPromotionResult(candidate_id, False, candidate.status, authority_code)
        if automatic:
            policy_code = _automatic_policy_block(effective)
            if policy_code:
                return MemoryPromotionResult(candidate_id, False, candidate.status, policy_code)
            candidate = self._apply_automatic_policy(candidate, reviewer)
        if candidate.status != "approved":
            return MemoryPromotionResult(candidate_id, False, candidate.status, "REVIEW_REQUIRED")
        if _promotion_target(candidate) == "long_term":
            _register_condense_debt(self.long_term, self.candidates, self.coordinator_io)
        return _commit_promotion_result(self, candidate, reviewer, confirmed)

    # LLM: 仅由已通过来源/证据/权威/安全门的自动分支调用；transition消费完整快照CAS，不复活拒绝或覆盖人工审核。
    # 函数用途: 在同一次耐久批准中补齐默认目标，批准必须先于任何正式写入。
    def _apply_automatic_policy(self, candidate: MemoryCandidate, reviewer: str) -> MemoryCandidate:
        if candidate.status != "pending_review":
            return candidate
        return self.candidates.transition(candidate, "approved", reviewer=reviewer,
            review_note="owner_autonomous_v1：结构化权威与安全门通过。", promotion_target=_promotion_target(candidate))

    # LLM: 与提交/恢复共用目标解析；持久补全使用完整快照CAS，不借用同状态transition空操作。
    # 函数用途: 为符合原默认政策的用户长期事实补上实际保存的落点。
    def _fill_default_target(
        self,
        candidate: MemoryCandidate,
        reviewer: str,
    ) -> MemoryCandidate:
        if (str(candidate.promotion_target or "").strip().lower() in {"", "none"}
                and _promotion_target(candidate) == "long_term"):
            return self.candidates.fill_long_term_target(candidate, reviewer)
        return candidate

    # LLM: 提交在 policy 锁内先复核名单（mc4-3103）：cleanup 的名单追加与晋升提交不能交错，
    #   复核与写入必须在同一临界区；未接线 exclusion_store 时退化为无复核提交。
    # 函数用途: 在 policy 锁内复核名单后提交目标。
    def _commit_with_conflict_handling(
        self,
        candidate: MemoryCandidate,
        reviewer: str,
        confirmed: bool,
    ) -> tuple[_TargetCommit, MemoryCandidate]:
        return _commit_with_exclusion_guard(
            self, candidate, reviewer=reviewer, confirmed=confirmed
        )

    # LLM: 回执只描述本条已提交事实；治理统一交批次末尾，不在这里逐条重扫或嵌套业务锁。
    # 函数用途: 返回准确晋升结果，condense欠账由外层原批次完成或耐久保留。
    def _finalize_promotion(
        self,
        candidate: MemoryCandidate,
        reviewer: str,
        outcome: _TargetCommit,
    ) -> MemoryPromotionResult:
        return MemoryPromotionResult(
            candidate.candidate_id,
            True,
            candidate.status,
            "PROMOTED",
            outcome.reference,
            formal_memory_changed=outcome.changed,
        )

    # LLM: user_explicit 逐条回查消息；工具事实及所有声明产物均逐条回查生产调用的宿主成功终态。
    # 函数用途: 返回空字符串表示证据通过，否则返回稳定阻塞码。
    def _verify_evidence(self, candidate: MemoryCandidate) -> str:
        if candidate.origin == "user_explicit":
            if not candidate.source_message_refs:
                return "USER_MESSAGE_EVIDENCE_MISSING"
            verified = [self.message_verifier.verify(ref) for ref in candidate.source_message_refs]
            if not all(verified) or not any(item and item.get("role") == "user" for item in verified):
                return "USER_MESSAGE_EVIDENCE_INVALID"
        from .evidence_admission import tool_evidence_failure

        return tool_evidence_failure(candidate, self.tool_verifier)

    # LLM: 证据失败CAS原快照进入blocked；终态只返回结果，旧失败不得覆盖并发已提交候选。
    # 函数用途: 持久化缺证据阻塞。
    def _block_missing_evidence(
        self,
        candidate: MemoryCandidate,
        reviewer: str,
        reason_code: str,
    ) -> MemoryPromotionResult:
        if candidate.status in {"pending_review", "approved"}:
            blocked = self.candidates.transition(
                candidate,
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


# LLM: 仅结构化判定自动审核资格；保AGENTS禁写、宿主来源/权威、精确目标、冲突及临时过期门和诊断优先级。
# 函数用途: 不合格返回原阻断码，全部通过返回空串；本函数不写候选或正式库。
def _automatic_policy_block(candidate: MemoryCandidate) -> str:
    if str(candidate.promotion_target or "").strip().lower() == "agents":
        return "AUTO_AGENTS_WRITE_FORBIDDEN"
    # 自动学习对旧空来源也保守关闭；人工审核不调用此策略，不能借空字段授予自动资格。
    source = candidate.learning_source if isinstance(candidate.learning_source, dict) else {}
    if not projection_is_learnable(source):
        return "AUTO_POLICY_LEARNING_SOURCE_UNVERIFIED"
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


# LLM: 人工审核也不能把模型推断制造成正式用户事实或SOUL权威；AGENTS禁写与会话/临时范围约束不可绕过。
# AGENTS is user-write-only: every automatic promotion path is blocked earlier in
# _automatic_policy_block (AUTO_AGENTS_WRITE_FORBIDDEN), while temporary/session instructions
# still cannot become a durable USER profile.
# 函数用途: 返回模型自主或人工路径都不能绕过的正式事实与 SOUL/USER 权威阻塞码。
def _absolute_authority_block(candidate: MemoryCandidate) -> str:
    if (
        candidate.origin == "model_inferred"
        and candidate.promotion_target in {"long_term", "user", "soul"}
        and candidate.candidate_type != "working_agreement"
    ):
        # working_agreement 的可信来源由学习来源门承担（§3.3 专门的审核规则）；
        # 事实/事件/项目仍要求 user_explicit/tool_verified。
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
        if len(low) < _EN_KEYWORD_MIN_LEN_CHARS or low in _EN_KEYWORD_STOPWORDS:
            continue
        if low not in kept:
            kept.append(low)
    return kept[:_EN_KEYWORD_MAX_COUNT]


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


# LLM: 正式覆盖只能使用宿主冻结的观察版本；旧缺字段候选保守阻断，墙钟先后不能授予覆盖权。
# 函数用途: 核对目标观察版本并返回稳定冲突码，后续写入继续使用同一版本CAS。
def _stale_target_observation(candidate: MemoryCandidate, prior: MemoryRecord) -> str:
    version = candidate.target_entry_version
    if type(version) is not int or version < 1:
        return "TARGET_ENTRY_OBSERVATION_VERSION_MISSING"
    if prior.version != version:
        return "TARGET_ENTRY_STALE_OBSERVATION"
    return ""


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
    attributes: dict[str, object] = {
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
    # 工作约定与子代理来源：来源投影和 run/task 关联进正式记录，撤回与审计可回溯标签。
    learning = candidate.learning_source if isinstance(candidate.learning_source, dict) else {}
    if learning:
        attributes["learning_source"] = dict(learning)
    if candidate.source_run_ids or candidate.source_task_ids:
        attributes["source_run_ids"] = list(candidate.source_run_ids)[:32]
        attributes["source_task_ids"] = list(candidate.source_task_ids)[:32]
    return attributes


# LLM: lesson 永不写进 long_term；这里只映射允许的事实种类。
# 函数用途: 选择长期记忆 kind。
def _long_term_kind(candidate_type: str) -> str:
    return {
        "long_term_fact": "fact",
        "event": "event",
        "project": "project",
        "working_agreement": "working_agreement",
    }[candidate_type]


# LLM: valid_until是唯一过期来源；转换和正式仓库非负约束共用，工具全批预检可在写入前调用。
# 函数用途: 将带时区ISO失效时间转为已经通过正式约束的epoch。
def _expiry_epoch(value: str) -> float:
    from .jsonl import _operation_expiry

    if not str(value or "").strip():
        return 0.0
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("candidate valid_until lacks timezone")
    return _operation_expiry(parsed.timestamp())


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


# LLM: 仅提交阶段捕获已分类失败，正文提交事实和派生修复责任不得丢失；使用已核验候选快照。
# 函数用途: 把提交与清理的不同终态转换为准确晋升回执。
def _commit_promotion_result(service, candidate, reviewer, confirmed):
    from ._jsonl_indexing import RemovalCleanupError

    candidate_id = candidate.candidate_id
    try:
        outcome, promoted = service._commit_with_conflict_handling(
            candidate, reviewer, confirmed
        )
    except LessonRoutingPendingError as exc:
        return MemoryPromotionResult(candidate_id, False, candidate.status, "LESSON_ROUTING_PENDING",
                                     exc.promotion_ref, formal_memory_changed=exc.formal_memory_changed)
    except RemovalCleanupError as exc:
        return MemoryPromotionResult(candidate_id, False, candidate.status, "REMOVE_CLEANUP_PENDING",
                                     formal_memory_changed=exc.formal_memory_changed)
    except _PromotionBlocked as exc:
        return MemoryPromotionResult(
            candidate.candidate_id,
            False,
            exc.status,
            exc.reason_code,
        )
    return service._finalize_promotion(promoted, reviewer, outcome)


# LLM: 只准确policy锁FD出生，原scope每次核当前来源；不保存callback或扩大权限。
# 函数用途: 为C名单锁出生调用原同步末门，等待仍门外。
def _coordinator_policy_birth(operation):
    return execute_global_write(operation, require_scope=True)


# LLM: 上下文只聚合本批现有仓库和欠账代次，不授运行权、不缓存quota或来源；退出未知时必须保留全部标记。
# 类用途: 保存一份正式库的待治理责任，condense只在全部候选业务锁释放后执行。
@dataclass(frozen=True)
class _CondenseDebt:
    memory: object
    marker: Path
    generation: str
    coordinator_io: bool


# LLM: 未知标记属于共享批次而非某层try；捕获内层异常不能恢复外层的治理资格。
# 类用途: 聚合本批欠账，并记住任何嵌套层出现过的执行未知。
@dataclass
class _PromotionBatchState:
    debts: dict = field(default_factory=dict)
    uncertain: bool = False


_PROMOTION_BATCH: ContextVar[_PromotionBatchState | None] = ContextVar("memory_promotion_batch", default=None)
_UNCERTAIN_PROMOTION_ERRORS = (
    RequestExecutionContextUnavailable, RequestExecutionOutcomeUncertainError, BoundedCallExitUnconfirmedError,
    GlobalWriteCommitUnavailable, GlobalWriteInWorkerError, LocalControlCoordinatorUnavailable, OwnerManagementIngressUnavailable,
)


# LLM: 显式上下文跨lambda保留原批次，嵌套不提前收尾；未知/取消不condense，普通失败仍保留原异常与耐久欠账。
# 函数用途: 嵌套共享不可逆未知状态；最外层在全部业务锁退出后才每库治理一次。
@contextmanager
def promotion_batch(*, long_term=None, candidates=None, coordinator_io: bool = False):
    active = _PROMOTION_BATCH.get()
    batch = active if active is not None else _PromotionBatchState()
    token = _PROMOTION_BATCH.set(batch) if active is None else None
    try:
        _resume_condense_debt(long_term, candidates, coordinator_io)
        yield
    except _UNCERTAIN_PROMOTION_ERRORS:
        batch.uncertain = True
        raise
    except BaseException as exc:
        if not isinstance(exc, Exception):
            batch.uncertain = True
        raise
    finally:
        if token is not None:
            _PROMOTION_BATCH.reset(token)
            if not batch.uncertain:
                _finish_condense_debts(batch.debts)


# LLM: 标记只绑定候选仓库同一memory根内的正式文件；任意路径或跨owner别名不能借此获得condense权限。
# 函数用途: 定位唯一pending_condense标记并返回可验证的相对正式路径。
def _condense_scope(memory, candidates) -> tuple[Path, str]:
    root = candidates.path.parent.resolve()
    formal = memory.path.resolve()
    relative = str(formal.relative_to(root))
    return root / "curator" / "pending_condense.json", relative


# LLM: 读取保持严格schema与formal身份；坏标记不能被当作空欠账覆盖，不从正文推断治理授权。
# 函数用途: 读取可跨重启恢复的治理责任，损坏时显式报错。
def _read_condense_marker(path: Path, formal: str) -> dict:
    report = read_json_object_report(path, context="memory.promotion.pending_condense")
    if report.load_error:
        raise RuntimeError("PROMOTION_PENDING_CONDENSE_CORRUPT")
    payload = report.payload
    through = payload.get("through")
    if payload and (set(payload) != {"schema", "formal", "pending", "generation", "through"}
                    or payload.get("schema") != "memory.pending_condense.v1"
                    or payload.get("formal") != formal or type(payload.get("pending")) is not bool
                    or not isinstance(payload.get("generation"), str) or not payload["generation"]
                    or (through is not None and (not isinstance(through, list) or len(through) != 5
                                                 or any(type(value) is not int for value in through)))):
        raise RuntimeError("PROMOTION_PENDING_CONDENSE_CORRUPT")
    return payload


# LLM: 标记写入沿原native末门，不持候选/政策/配额锁；每代正式写前落盘，未确认写入会阻断正式提交。
# 函数用途: 写入或完成同一份治理标记，不发出任何模型或网络请求。
def _write_condense_marker(path: Path, payload: dict, coordinator_io: bool) -> None:
    if coordinator_io:
        execute_global_write(lambda: write_json_file_atomic_unlocked(path, payload, sort_keys=True), require_scope=True)
    else:
        write_json_file_atomic_unlocked(path, payload, sort_keys=True)


# LLM: 本批同一原memory根只登记一次；新代先落耐久标记，旧批完成时不能清掉并发新代的欠账。
# 函数用途: 在第一条正式写入前登记批次condense责任，批准仍由原候选事务先行持久化。
def _register_condense_debt(memory, candidates, coordinator_io: bool) -> None:
    batch = _PROMOTION_BATCH.get()
    if batch is None:
        raise RuntimeError("promotion batch context is missing")
    debts = batch.debts
    path, formal = _condense_scope(memory, candidates)
    if str(path) in debts:
        _check_condense_formal(debts[str(path)], memory)
        return
    generation = uuid.uuid4().hex
    options = {"birth_commit": _coordinator_policy_birth} if coordinator_io else {}
    with locked_json_path(path, **options):
        current = _read_condense_marker(path, formal)
        _write_condense_marker(path, {"schema": "memory.pending_condense.v1", "formal": formal,
                                     "pending": True, "generation": generation, "through": current.get("through")}, coordinator_io)
    debts[str(path)] = _CondenseDebt(memory, path, generation, coordinator_io)


# LLM: 空批也检查当前owner唯一欠账；只恢复已声明的原仓库，缺正式依赖的测试替身不猜仓库或构造新服务。
# 函数用途: pending或正式版本晚于上次治理起点都要补跑，避免并发空批提前清账后丢掉迟到提交。
def _resume_condense_debt(memory, candidates, coordinator_io: bool) -> None:
    if memory is None or candidates is None:
        return
    path, formal = _condense_scope(memory, candidates)
    batch = _PROMOTION_BATCH.get()
    debts = batch.debts if batch is not None else {}
    if debts is not None and str(path) in debts:
        _check_condense_formal(debts[str(path)], memory)
        return
    current = _read_condense_marker(path, formal)
    if current and (current.get("pending") or current.get("through") != _condense_file_version(memory)):
        _register_condense_debt(memory, candidates, coordinator_io)


# LLM: 只确认condense开始前的原正式版本，绝不把收尾时的更晚提交宣称已覆盖；治理自身改写可多补一轮，不能少治理。
# 函数用途: 锁外每批每库治理一次，同代CAS记起点版本；失败或晚到正式写保留可恢复的版本欠账。
def _finish_condense_debts(debts: dict) -> None:
    for debt in debts.values():
        try:
            through = _condense_file_version(debt.memory)
            debt.memory.condense()
            options = {"birth_commit": _coordinator_policy_birth} if debt.coordinator_io else {}
            with locked_json_path(debt.marker, **options):
                formal = str(debt.memory.path.resolve().relative_to(debt.marker.parent.parent))
                current = _read_condense_marker(debt.marker, formal)
                if current.get("generation") == debt.generation and current.get("pending"):
                    _write_condense_marker(debt.marker, {**current, "pending": False, "through": through}, debt.coordinator_io)
        except _UNCERTAIN_PROMOTION_ERRORS:
            raise
        except Exception:
            logging.getLogger(__name__).warning("memory promotion pending_condense retained")


# LLM: 版本来自原正式文件五字段stat而非候选内容或另建权威；缺文件用None，权限和其它I/O错误不能伪装成空。
# 函数用途: 给耐久欠账绑定已覆盖版本的保守下界，检测旧批清账后的迟到正式提交。
def _condense_file_version(memory) -> list[int] | None:
    try:
        info = memory.path.stat()
    except FileNotFoundError:
        return None
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


# LLM: 一个原候选根只绑定唯一正式库；批内命中也须核规范路径，禁止跳过formal身份检查静默漏治理。
# 函数用途: 拒绝同一欠账标记混用不同正式文件的错误依赖绑定。
def _check_condense_formal(debt: _CondenseDebt, memory) -> None:
    if debt.memory.path.resolve() != memory.path.resolve():
        raise RuntimeError("PROMOTION_PENDING_CONDENSE_FORMAL_MISMATCH")
