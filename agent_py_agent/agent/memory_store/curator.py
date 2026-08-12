from __future__ import annotations

"""Gateway 正式后台主链使用的唯一 Memory Curator 服务。"""

# LLM: Every trigger becomes a durable reason in one state store and every run uses one
# no-tools backend, one evidence validator, and one recoverable batch committer.
# 模块用途: 调度增量策展、严格模型提取、整批提交、失败审计与保守自动晋升。

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .candidate_models import CandidateObservation, utc_now_iso
from .candidates import CandidateService
from .curator_backend import (
    CuratorModelTimeoutError,
    adaptive_timeout_seconds,
    extract_with_retries,
)
from .curator_commit import (
    CuratorBatchCommit,
    CuratorBatchCommitError,
    CuratorBatchCommitter,
    CuratorCommitRecoveryError,
)
from .curator_formal import FormalMemorySource
from .curator_inputs import (
    CuratorInputBatch,
    CuratorToolReferenceSource,
    collect_curator_inputs,
)
from .curator_models import (
    CuratorExtraction,
    CuratorRunResult,
    MemoryCuratorConfig,
    MemoryCuratorState,
)
from .curator_run_log import CuratorRunLog, CuratorRunRecord
from .curator_state import (
    CuratorLeaseLostError,
    CuratorSuccessCommit,
    MemoryCuratorStateStore,
)
from .curator_validation import CuratorCursorAdvance, next_cursors, validate_extraction
from .daily import DailyMemoryEvent, DailyMemoryStore

_RUN_STATUSES = frozenset({"succeeded", "failed", "busy", "disabled", "not_due"})
_REASON_PRIORITY = (
    "pre_compact",
    "session_close",
    "reset",
    "task_complete",
    "admin",
    "daily_finalize",
    "turn_threshold",
    "interval",
    "migration",
)


# LLM: Input authority damage is distinct from provider/schema failure and always blocks cursor
# advancement.
# 类用途: 表示 ConversationStore、audit 或正式记忆输入无法安全读取。
class CuratorInputError(RuntimeError):
    pass


# LLM: Runtime identity is immutable for a service instance and appears in every result/audit;
# model switches therefore remain explicit without changing schema.
# 类用途: 保存 Curator 实际 provider、model、owner 与时区。
@dataclass(frozen=True)
class MemoryCuratorIdentity:
    provider: str = ""
    model: str = ""
    owner_id: str = ""
    timezone_name: str = ""


# LLM: A dependency bundle replaces a wide constructor and makes the no-tools boundary visible:
# there is no ToolRegistry, file tool, persona tool, remember tool, or Skill installer field.
# 类用途: 汇总 Curator 所需的只读输入、规范仓库和可选正式记忆投影。
@dataclass(frozen=True)
class MemoryCuratorDependencies:
    backend: object
    conversation_store: object
    audit_dir: Path
    state_store: MemoryCuratorStateStore
    daily_store: DailyMemoryStore
    candidate_service: CandidateService
    run_log: CuratorRunLog
    identity: MemoryCuratorIdentity = MemoryCuratorIdentity()
    formal_memory_source: FormalMemorySource | None = None
    tool_reference_source: CuratorToolReferenceSource | None = None
    promotion_callback: Callable[[str], object] | None = None
    # 升级自愈:每次持 lease 执行前自动应用 Memory v2 迁移(幂等),失败不阻断提炼。
    migration_service: object | None = None


# LLM: Run context binds one acquired lease to its pre-run cursor snapshot; helpers cannot use a
# later mutable state accidentally.
# 类用途: 保存一次 Curator lease 运行的宿主身份和旧状态。
@dataclass(frozen=True)
class _RunContext:
    reason: str
    run_id: str
    lease_id: str
    started_at: str
    state_before: MemoryCuratorState
    recovery: dict[str, object]


# LLM: This is the only background Memory extraction service used by Gateway maintenance, CLI,
# compact, task completion, and lifecycle signals.
# 类用途: 执行一个 owner 的增量 Daily/Candidate 策展闭环。
class MemoryCuratorService:
    # LLM: Construction wires existing canonical services and creates only a transaction
    # coordinator; it starts no thread, daemon, or second scheduler.
    # 函数用途: 初始化唯一 Curator 服务及其可恢复提交器。
    def __init__(
        self,
        *,
        config: MemoryCuratorConfig,
        dependencies: MemoryCuratorDependencies,
    ) -> None:
        self.config = config
        self.dependencies = dependencies
        self.backend = dependencies.backend
        self.conversation_store = dependencies.conversation_store
        self.audit_dir = Path(dependencies.audit_dir)
        self.state_store = dependencies.state_store
        self.daily_store = dependencies.daily_store
        self.candidate_service = dependencies.candidate_service
        self.run_log = dependencies.run_log
        self.runs_dir = self.run_log.runs_dir
        self.identity = dependencies.identity
        self.provider_name = self.identity.provider or str(
            getattr(self.backend, "name", "") or ""
        )
        self.model_name = self.identity.model
        self.owner_id = self.identity.owner_id.strip()
        self.timezone_name = self.identity.timezone_name.strip()
        self.formal_memory_source = dependencies.formal_memory_source
        self.tool_reference_source = dependencies.tool_reference_source
        self.promotion_callback = dependencies.promotion_callback
        self.migration_service = dependencies.migration_service
        self.committer = CuratorBatchCommitter(
            candidate_service=self.candidate_service,
            daily_store=self.daily_store,
            state_store=self.state_store,
            run_log=self.run_log,
        )

    # LLM: Triggers persist only a reason; compact/task/channel paths never call the model or
    # write Daily/long-term synchronously.
    # 函数用途: 幂等登记一次统一后台策展请求。
    def request(self, reason: str) -> dict[str, object]:
        state = self.state_store.request(reason)
        return {
            "requested": True,
            "reason": reason,
            "pending_reasons": list(state.pending_reasons),
            "requested_at": state.pending_requested_at,
        }

    # LLM: Due selection uses durable state and incremental counts only; it never infers trigger
    # intent from conversation text.
    # 函数用途: 在 Gateway owner maintenance tick 中运行一个到期批次。
    def run_if_due(self, *, now: datetime | None = None) -> CuratorRunResult:
        if not self.config.enabled:
            return self._result(status="disabled", reason="interval")
        state = self.state_store.load()
        current = _localized_now(now, self.timezone_name)
        pending = _pending_reason(state.pending_reasons)
        if pending:
            return self._run_pending_when_due(state, pending, current)
        batch = self._collect(state, include_formal=False)
        if batch.load_errors:
            return self.run(reason="interval", now=current)
        if batch.user_turns() >= self.config.turn_threshold:
            return self.run(reason="turn_threshold", now=current)
        if _interval_due(batch, state, current, self.config.interval_seconds):
            return self.run(reason="interval", now=current)
        if _daily_finalize_due(state, current, self.config.daily_finalize_hour):
            return self.run(reason="daily_finalize", now=current)
        # 框架兜底:无新消息但存在【已落库、待晋升的合格候选】时,也触发晋升——否则候选落库后
        # 若 curator 不再 run(无新消息),pending_review 的 user_explicit 候选永远不晋升
        # (真机:longdoc 长文档提炼10条候选后 pending=[] 停摆,晋升不触发)。
        # 纯结构化判据:只读候选账本里 pending_review 的候选数,不解析正文。
        pending_promotable = self._pending_promotable_candidate_ids(limit=32)
        if pending_promotable:
            promoted, _warnings = self._promote_eligible(pending_promotable)
            if promoted:
                return self._result(status="succeeded", reason="promote_backlog")
        return self._result(status="not_due", reason="interval")

    # LLM: Recovery runs before lease acquisition; an unexpired prior lease remains busy, while
    # an expired partial transaction is rolled back before any new input is processed.
    # 函数用途: 显式或自动执行一次 Curator 并返回无正文运行摘要。
    def run(
        self,
        *,
        reason: str,
        force: bool = False,
        now: datetime | None = None,
    ) -> CuratorRunResult:
        if not self.config.enabled and not force:
            return self._result(status="disabled", reason=reason)
        try:
            self.committer.recover_incomplete(now=now)
        except Exception as exc:
            return self._unleased_failure(reason, _failure_code(exc))
        acquired = self.state_store.acquire(
            reason=reason,
            config_revision=self.config.revision(),
            lease_seconds=_lease_seconds(self.config),
            now=now,
        )
        if acquired is None:
            return self._result(status="busy", reason=reason)
        context = _run_context(reason, *acquired)
        try:
            return self._execute(context)
        except Exception as exc:
            return self._commit_failure(context, exc)

    # LLM: Status exposes configuration and durable health only; no prompt, response, formal
    # memory, candidate body, or user content is returned.
    # 函数用途: 为 CLI/doctor 返回 Curator 当前配置与 state。
    def status(self) -> dict[str, object]:
        state = self.state_store.load()
        return {
            "enabled": self.config.enabled,
            "provider": self.provider_name,
            "model": self.model_name,
            "config": {
                "interval_seconds": self.config.interval_seconds,
                "turn_threshold": self.config.turn_threshold,
                "batch_message_limit": self.config.batch_message_limit,
                "max_input_chars": self.config.max_input_chars,
                "timeout_seconds": self.config.timeout_seconds,
                "max_retries": self.config.max_retries,
                "daily_finalize_hour": self.config.daily_finalize_hour,
                "auto_promotion_policy": self.config.auto_promotion_policy,
            },
            "state": state.to_dict(),
        }

    # LLM: Cooldown applies only to a durable failed pending request; it bounds provider retries
    # without deleting the reason or skipping input.
    # 函数用途: 判断 pending reason 现在执行还是等待失败退避。
    def _run_pending_when_due(
        self,
        state: MemoryCuratorState,
        pending: str,
        current: datetime,
    ) -> CuratorRunResult:
        last_failure = _parse_time(state.last_failure_at)
        retry_after = max(30, min(300, self.config.interval_seconds))
        if (
            state.last_failure_code
            and last_failure is not None
            and (current - last_failure).total_seconds() < retry_after
        ):
            return self._result(status="not_due", reason=pending)
        return self.run(reason=pending, now=current)

    # LLM: Extraction and evidence validation complete before the transaction receives any
    # canonical target content.
    # 函数用途: 执行一个已持 lease 的提取、整批提交与提交后晋升。
    def _execute(self, context: _RunContext) -> CuratorRunResult:
        migration_warnings = self._preflight_migration()
        batch = self._collect(context.state_before, include_formal=True)
        if batch.load_errors:
            raise CuratorInputError(_load_error_code(batch.load_errors))
        if not batch.messages and not batch.audit_events:
            return self._commit_batch(
                context, batch, None, extra_warnings=migration_warnings
            )
        extraction = extract_with_retries(self.backend, self.config, batch)
        validated = validate_extraction(extraction, batch, owner_id=self.owner_id)
        return self._commit_batch(
            context, batch, validated, extra_warnings=migration_warnings
        )

    # LLM: 升级自愈:legacy(v1 前)数据会在正式记忆读取时破坏读路径,所以必须在每次持 lease
    # 提炼前自动迁移;apply 幂等(marker guard),失败只记 warning 不阻断提炼(读路径已容错,
    # 下轮再试)。这是升级/部署/迁移永不因旧数据瘫痪的机制层兜底,不是一次性手工操作。
    # 函数用途: 每次执行前应用 Memory v2 迁移,返回可展示的迁移 warning。
    def _preflight_migration(self) -> tuple[str, ...]:
        if self.migration_service is None:
            return ()
        try:
            report = self.migration_service.apply()
        except Exception as exc:
            return (f"memory_migration_failed:{_failure_code(exc)}",)
        if report.applied:
            return (
                f"memory_migration_applied:candidates={report.migrated_candidates},"
                f"daily={report.migrated_daily_events}",
            )
        return ()

    # LLM: The batch committer writes state last; automatic promotion starts only after that
    # transaction succeeds and cannot retroactively invalidate its cursor.
    # 函数用途: 构造 Daily/Candidate、游标、run audit 并整批提交。
    def _commit_batch(
        self,
        context: _RunContext,
        batch: CuratorInputBatch,
        extraction: CuratorExtraction | None,
        *,
        extra_warnings: tuple[str, ...] = (),
    ) -> CuratorRunResult:
        prepared = _prepare_outputs(context, batch, extraction)
        finished_at = utc_now_iso()
        run_record = _successful_run_record(
            context,
            prepared,
            provider=self.provider_name,
            model=self.model_name,
            finished_at=finished_at,
        )
        state_commit = _success_commit(
            context,
            prepared,
            finished_at=finished_at,
            finalize_date=_finalize_date(
                context.reason,
                self.timezone_name,
                started_at=context.started_at,
            ),
        )
        try:
            committed = self.committer.commit(
                CuratorBatchCommit(
                    run_record=run_record,
                    state_commit=state_commit,
                    daily_events=prepared.daily_events,
                    candidate_observations=prepared.candidates,
                )
            )
        except CuratorCommitRecoveryError:
            raise
        except Exception as exc:
            raise CuratorBatchCommitError("curator batch commit failed") from exc
        promoted, promotion_warnings = self._promote_eligible(
            [item.candidate_id for item in committed.candidates]
        )
        warnings = (
            *prepared.warnings,
            *extra_warnings,
            *(
                f"formal_memory_read_skipped:{item.get('code', 'UNKNOWN')}"
                for item in batch.formal_memory_errors
            ),
            *promotion_warnings,
        )
        if promoted:
            warnings = (*warnings, f"auto_promoted:{promoted}")
        return CuratorRunResult(
            run_id=context.run_id,
            status="succeeded",
            reason=context.reason,
            provider=self.provider_name,
            model=self.model_name,
            processed_messages=prepared.cursor.processed_messages,
            processed_audit_events=prepared.cursor.processed_audit_events,
            daily_events=len(committed.daily_events),
            candidates=len(committed.candidates),
            warnings=warnings,
        )

    # LLM: Formal memory has its own fixed sub-budget; due checks omit it and actual runs include
    # it without reducing the exact ConversationStore/audit cursor contract.
    # 函数用途: 收集一次有界增量输入快照。
    def _collect(
        self,
        state: MemoryCuratorState,
        *,
        include_formal: bool,
    ) -> CuratorInputBatch:
        formal = ()
        formal_memory_errors: list[dict[str, object]] = []
        if include_formal and self.formal_memory_source is not None:
            try:
                formal = self.formal_memory_source.read(
                    max_items=32,
                    max_chars=min(6_000, max(1_000, self.config.max_input_chars // 4)),
                )
            except Exception as exc:
                # 正式记忆(lessons/hot/long_term)是增强输入而非主链依赖:读取失败降级为空
                # 并记入可降级错误,不阻断本次提炼(legacy/损坏文件的正式迁移由
                # migration_service 自愈,读路径对单文件异常保持容错)。
                formal_memory_errors.append(
                    {"code": _failure_code(exc), "detail": str(exc)[:200]}
                )
        formal_chars = len(
            json.dumps([item.to_model() for item in formal], ensure_ascii=False)
        )
        input_config = replace(
            self.config,
            max_input_chars=max(
                2_000,
                self.config.max_input_chars - 7_000 - formal_chars,
            ),
        )
        batch = collect_curator_inputs(
            conversation_store=self.conversation_store,
            audit_dir=self.audit_dir,
            state=state,
            config=input_config,
            formal_memories=formal,
            tool_reference_source=self.tool_reference_source,
        )
        if formal_memory_errors:
            batch = replace(batch, formal_memory_errors=tuple(formal_memory_errors))
        return batch

    # LLM: Promotion consumes only committed candidate IDs through the unique PromotionService;
    # failure is isolated from the already-successful Curator transaction and remains retryable.
    # 函数用途: 执行保守自动晋升并返回数量和稳定警告码。
    def _pending_promotable_candidate_ids(self, *, limit: int = 32) -> list[str]:
        """返回已落库、待晋升的合格候选 ID(纯结构化:只读 pending_review 状态,不解析正文)。

        LLM: 用于 run_if_due 无新消息时的晋升兜底;取 user_explicit/tool_verified 且
        promotion_target=long_term 的候选,以及 lesson 类型候选(promote 会自动再校验,
        这里只是候选集)。lesson 专线与事实线并列:model_inferred/subagent_lesson 的
        lesson 候选由 _automatic_policy_block 放行,重复/证据门槛在落库时强制。
        失败静默返回空,绝不外抛。
        """
        try:
            rows = self.candidate_service.list(statuses={"pending_review"}, limit=limit)
            ids: list[str] = []
            for row in rows:
                origin = str(getattr(row, "origin", "") or "").strip().lower()
                target = str(getattr(row, "promotion_target", "") or "").strip().lower()
                candidate_type = str(getattr(row, "candidate_type", "") or "").strip().lower()
                # user_explicit/tool_verified:target 为空或 long_term 都算可晋升候选——
                # promote 入口会把 target=none 的 user_explicit 长期事实补成 long_term
                # (模型偶发漏写 target,宿主确定性补全)。model_inferred/Persona 一律排除。
                if origin in {"user_explicit", "tool_verified"} and target in {"", "none", "long_term"}:
                    ids.append(str(getattr(row, "candidate_id", "") or ""))
                    continue
                # lesson 专线:模型/子代理推断的 lesson 候选(重复出现+多证据组才过落库门槛)。
                if candidate_type == "lesson" and target == "lesson":
                    ids.append(str(getattr(row, "candidate_id", "") or ""))
            return ids
        except Exception:
            return []

    def _promote_eligible(self, candidate_ids: list[str]) -> tuple[int, tuple[str, ...]]:
        if self.config.auto_promotion_policy == "manual_only" or self.promotion_callback is None:
            return 0, ()
        promoted = 0
        warnings: list[str] = []
        for candidate_id in candidate_ids:
            try:
                result = self.promotion_callback(candidate_id)
            except Exception:
                warnings.append("CURATOR_AUTO_PROMOTION_FAILED")
                continue
            if bool(getattr(result, "promoted", False)):
                promoted += 1
        return promoted, tuple(dict.fromkeys(warnings))

    # LLM: Failure audit is appended before releasing the lease; old cursors and pending reasons
    # remain, making a later retry safe.
    # 函数用途: 记录一次持 lease 运行失败并返回稳定结果。
    def _commit_failure(
        self,
        context: _RunContext,
        exc: BaseException,
    ) -> CuratorRunResult:
        failure_code = _failure_code(exc)
        finished_at = utc_now_iso()
        try:
            self.run_log.append(
                _failed_run_record(
                    context,
                    provider=self.provider_name,
                    model=self.model_name,
                    finished_at=finished_at,
                    failure_code=failure_code,
                )
            )
        except Exception:
            failure_code = "CURATOR_RUN_AUDIT_FAILED"
        try:
            self.state_store.commit_failure(
                lease_id=context.lease_id,
                failure_code=failure_code,
                now=finished_at,
            )
        except CuratorLeaseLostError:
            pass
        return CuratorRunResult(
            run_id=context.run_id,
            status="failed",
            reason=context.reason,
            provider=self.provider_name,
            model=self.model_name,
            failure_code=failure_code,
        )

    # LLM: A recovery error before lease acquisition cannot safely mutate state or fabricate a
    # run ID; the pending durable request remains available for operator repair/retry.
    # 函数用途: 返回未取得 lease 时的稳定失败结果。
    def _unleased_failure(self, reason: str, failure_code: str) -> CuratorRunResult:
        return CuratorRunResult(
            run_id="",
            status="failed",
            reason=reason,
            provider=self.provider_name,
            model=self.model_name,
            failure_code=failure_code,
        )

    # LLM: Simple non-running results share one enum validation and never synthesize a run ID.
    # 函数用途: 构造 disabled、busy 或 not_due 结果。
    def _result(self, *, status: str, reason: str) -> CuratorRunResult:
        if status not in _RUN_STATUSES:
            raise ValueError("unsupported memory curator result status")
        return CuratorRunResult(
            run_id="",
            status=status,
            reason=reason,
            provider=self.provider_name,
            model=self.model_name,
        )


# LLM: Prepared outputs bind host-created Daily records, canonical candidate observations, exact
# cursor advancement, and non-content warnings before transaction construction.
# 类用途: 保存一次验证后、尚未落盘的 Curator 批次。
@dataclass(frozen=True)
class _PreparedOutputs:
    daily_events: tuple[DailyMemoryEvent, ...]
    candidates: tuple[CandidateObservation, ...]
    cursor: CuratorCursorAdvance
    warnings: tuple[str, ...]


# LLM: Empty runs use the same output object and transaction, proving admin/finalize requests
# release their lease without a second no-work commit path.
# 函数用途: 将验证后的 extraction 转成宿主持有的提交对象。
def _prepare_outputs(
    context: _RunContext,
    batch: CuratorInputBatch,
    extraction: CuratorExtraction | None,
) -> _PreparedOutputs:
    if extraction is None:
        cursor = CuratorCursorAdvance(
            dict(context.state_before.per_thread_cursors),
            context.state_before.last_processed_audit_event_id,
            0,
            0,
        )
        return _PreparedOutputs((), (), cursor, ("no_new_experience",))
    cursor = next_cursors(
        context.state_before.per_thread_cursors,
        context.state_before.last_processed_audit_event_id,
        batch,
        extraction,
    )
    extracted_at = utc_now_iso()
    daily = tuple(
        draft.to_daily_event(curator_run_id=context.run_id, extracted_at=extracted_at)
        for draft in extraction.daily_events
    )
    # 宿主确定性补全:模型偶发漏写 promotion_target 时,对"用户明确要求记住的长期事实/事件候选"
    # (user_explicit + long_term_fact/event + 空 target)补成 long_term——这是用户显式表达,符合
    # 自主晋升语义;不补 model_inferred/Persona/lesson 等(那些必须人工)。纯结构化规则,不猜正文。
    candidates = tuple(
        _fill_candidate_promotion_target(item) for item in extraction.candidates
    )
    return _PreparedOutputs(daily, candidates, cursor, tuple(extraction.warnings))


# LLM: 仅对 user_explicit 的 long_term_fact/event 候选补全漏写的 promotion_target=long_term；
# model_inferred/Persona/lesson/HOT 一律不补(必须人工审核),不扩大自动晋升面。
# 函数用途: 返回 promotion_target 补全后的候选 observation(纯结构化规则,不解析正文)。
def _fill_candidate_promotion_target(item: object) -> object:
    target = str(getattr(item, "promotion_target", "") or "").strip().lower()
    if target:
        return item
    origin = str(getattr(item, "origin", "") or "").strip().lower()
    ctype = str(getattr(item, "candidate_type", "") or "").strip().lower()
    if origin == "user_explicit" and ctype in {"long_term_fact", "event"}:
        from dataclasses import replace

        return replace(item, promotion_target="long_term")
    return item


# LLM: Lease recovery metadata is copied from the host-generated lease only and never inferred
# from provider output or timestamps.
# 函数用途: 构造一次已获取 lease 的运行上下文。
def _run_context(
    reason: str,
    state: MemoryCuratorState,
    lease: dict[str, object],
) -> _RunContext:
    recovery = lease.get("recovery") if isinstance(lease.get("recovery"), dict) else {}
    return _RunContext(
        reason=reason,
        run_id=str(lease["run_id"]),
        lease_id=str(lease["lease_id"]),
        started_at=str(lease["acquired_at"]),
        state_before=state,
        recovery=dict(recovery),
    )


# LLM: Successful audit includes counts/cursors and recovery IDs only; extracted content remains
# solely in Daily/Candidate authorities.
# 函数用途: 构造整批事务内的最终成功 run audit。
def _successful_run_record(
    context: _RunContext,
    prepared: _PreparedOutputs,
    *,
    provider: str,
    model: str,
    finished_at: str,
) -> CuratorRunRecord:
    return CuratorRunRecord(
        run_id=context.run_id,
        lease_id=context.lease_id,
        status="succeeded",
        reason=context.reason,
        provider=provider,
        model=model,
        started_at=context.started_at,
        finished_at=finished_at,
        processed_messages=prepared.cursor.processed_messages,
        processed_audit_events=prepared.cursor.processed_audit_events,
        daily_events=len(prepared.daily_events),
        candidates=len(prepared.candidates),
        warnings=prepared.warnings,
        cursor_before=_cursor_payload(
            context.state_before.per_thread_cursors,
            context.state_before.last_processed_audit_event_id,
        ),
        cursor_after=_cursor_payload(
            prepared.cursor.per_thread_cursors,
            prepared.cursor.last_audit_event_id,
        ),
        recovery=context.recovery,
    )


# LLM: Failure audit preserves the acquired lease and recovery provenance but never serializes
# exception text that may contain provider payload or user data.
# 函数用途: 构造失败 run audit。
def _failed_run_record(
    context: _RunContext,
    *,
    provider: str,
    model: str,
    finished_at: str,
    failure_code: str,
) -> CuratorRunRecord:
    return CuratorRunRecord(
        run_id=context.run_id,
        lease_id=context.lease_id,
        status="failed",
        reason=context.reason,
        provider=provider,
        model=model,
        started_at=context.started_at,
        finished_at=finished_at,
        failure_code=failure_code,
        cursor_before=_cursor_payload(
            context.state_before.per_thread_cursors,
            context.state_before.last_processed_audit_event_id,
        ),
        recovery=context.recovery,
    )


# LLM: State commit count/cursor values come from the same prepared object used to build the run
# audit, eliminating split-brain bookkeeping.
# 函数用途: 构造事务最后写入的成功 state 变化。
def _success_commit(
    context: _RunContext,
    prepared: _PreparedOutputs,
    *,
    finished_at: str,
    finalize_date: str,
) -> CuratorSuccessCommit:
    return CuratorSuccessCommit(
        lease_id=context.lease_id,
        run_id=context.run_id,
        reason=context.reason,
        per_thread_cursors=prepared.cursor.per_thread_cursors,
        last_processed_audit_event_id=prepared.cursor.last_audit_event_id,
        processed_messages=prepared.cursor.processed_messages,
        processed_audit_events=prepared.cursor.processed_audit_events,
        candidate_count=len(prepared.candidates),
        daily_event_count=len(prepared.daily_events),
        last_daily_finalize_date=finalize_date,
        committed_at=finished_at,
    )


# LLM: Cursor payload is a no-content audit projection and not a second cursor authority.
# 函数用途: 构造 run audit 的前后游标字段。
def _cursor_payload(cursors: dict[str, str], audit_event_id: str) -> dict[str, object]:
    return {
        "per_thread_cursors": dict(cursors),
        "last_audit_event_id": str(audit_event_id or ""),
    }


# LLM: Failure taxonomy contains stable codes only; provider exception text is never persisted.
# 函数用途: 将 Curator 异常分类为恢复和运维使用的错误码。
def _failure_code(exc: BaseException) -> str:
    if isinstance(exc, CuratorModelTimeoutError):
        return "CURATOR_MODEL_TIMEOUT"
    if isinstance(exc, CuratorCommitRecoveryError):
        return "CURATOR_COMMIT_RECOVERY_FAILED"
    if isinstance(exc, CuratorBatchCommitError):
        return "CURATOR_COMMIT_FAILED"
    if isinstance(exc, CuratorInputError):
        value = str(exc or "")
        return value if value.startswith("CURATOR_") else "CURATOR_INPUT_INVALID"
    if isinstance(exc, CuratorLeaseLostError):
        return "CURATOR_LEASE_LOST"
    if isinstance(exc, (ValueError, TypeError, json.JSONDecodeError)):
        value = str(exc or "")
        return value if value.startswith("CURATOR_") else "CURATOR_SCHEMA_INVALID"
    return "CURATOR_MODEL_FAILED"


# LLM: Input load errors retain only their existing stable code and never include damaged row
# content in state or run audit.
# 函数用途: 汇总增量输入读取失败码。
def _load_error_code(errors: tuple[dict[str, object], ...]) -> str:
    for error in errors:
        code = str(error.get("error_code") or "")
        if code:
            return "CURATOR_INPUT_" + code
    return "CURATOR_INPUT_READ_FAILED"


# LLM: Pending priority is one fixed host order shared by every trigger source, not array timing
# or natural-language importance.
# 函数用途: 选择下一次运行的最高优先级 reason。
def _pending_reason(reasons: list[str]) -> str:
    return next((reason for reason in _REASON_PRIORITY if reason in reasons), "")


# LLM: Interval due requires actual unprocessed message/audit input and uses a timezone-aware
# durable success timestamp.
# 函数用途: 判断增量输入是否达到时间触发条件。
def _interval_due(
    batch: CuratorInputBatch,
    state: MemoryCuratorState,
    now: datetime,
    interval_seconds: int,
) -> bool:
    if not batch.messages and not batch.audit_events:
        return False
    last_success = _parse_time(state.last_success_at)
    return last_success is None or (now - last_success).total_seconds() >= interval_seconds


# LLM: Daily finalize uses structured hour/date state in the owner timezone and does not inspect
# conversation words.
# 函数用途: 判断今日每日收尾是否到期。
def _daily_finalize_due(
    state: MemoryCuratorState,
    now: datetime,
    finalize_hour: int,
) -> bool:
    return now.hour >= finalize_hour and state.last_daily_finalize_date != now.date().isoformat()


# LLM: Finalize date is written only by a successful daily_finalize transaction and derives from the acquired run clock.
# 函数用途: 按本次 lease 的结构化开始时间生成每日收尾日期，测试/重放不读取另一时钟。
def _finalize_date(reason: str, timezone_name: str, *, started_at: str) -> str:
    if reason != "daily_finalize":
        return ""
    current = _parse_time(started_at)
    if current is None:
        raise ValueError("curator lease acquired_at is invalid")
    return _localized_now(current, timezone_name).date().isoformat()


# LLM: Lease covers all bounded retries plus commit/recovery margin and has one lower bound.
# 函数用途: 计算一次 Curator lease 时长。
def _lease_seconds(config: MemoryCuratorConfig) -> int:
    # 模型调用超时按输入规模自适应放大(adaptive_timeout_seconds),lease 必须覆盖
    # 最坏情况(输入达 max_input_chars 上限)的两次尝试加提交缓冲——否则长文提炼时
    # lease 先于模型调用过期,运行中 batch 会被误判 busy/丢失。
    worst_case_timeout = adaptive_timeout_seconds(
        config.timeout_seconds, config.max_input_chars
    )
    return worst_case_timeout * (config.max_retries + 1) + 90


# LLM: Owner timezone affects scheduling/date sharding only and never fact truth or ledger order.
# 函数用途: 将当前或测试时间转换到 owner 时区。
def _localized_now(value: datetime | None, timezone_name: str) -> datetime:
    current = value or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if not timezone_name:
        return current.astimezone()
    return current.astimezone(ZoneInfo(timezone_name))


# LLM: Bad/naive durable timestamps are treated as absent, never as a future time that silently
# disables maintenance.
# 函数用途: 解析 Curator state 中的带时区 ISO 时间。
def _parse_time(value: str) -> datetime | None:
    if not str(value or "").strip():
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


__all__ = [
    "CuratorInputError",
    "CuratorModelTimeoutError",
    "MemoryCuratorDependencies",
    "MemoryCuratorIdentity",
    "MemoryCuratorService",
]
