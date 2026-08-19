from __future__ import annotations

"""Gateway 正式后台主链使用的唯一 Memory Curator 服务。"""

# LLM: Every trigger becomes a durable reason in one state store and every run uses one
# no-tools backend, one evidence validator, and one recoverable batch committer.
# 模块用途: 调度增量策展、严格模型提取、整批提交、失败审计与保守自动晋升。

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ..common.json_io import read_json_object_report
from .candidate_models import CandidateObservation, MemoryScope, utc_now_iso
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
    validate_curator_state,
)
from .curator_run_log import CuratorRunLog, CuratorRunRecord
from .curator_state import (
    CuratorLeaseLostError,
    CuratorStateCorruptError,
    CuratorSuccessCommit,
    MemoryCuratorStateStore,
    _sha256_file,
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


# LLM: 触发/调度/失败结果与执行链分离:Lifecycle 只做持久化触发、lease 获取与结果构造,
# 不直接读写 Daily/Candidate。
# 类用途: 提供 Curator 触发登记、到期调度与失败结果构造。
class _CuratorLifecycleMixin:
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
        try:
            state = self.state_store.load()
        except CuratorStateCorruptError as exc:
            return self._corrupt_failure("interval", exc)
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
        except CuratorStateCorruptError as exc:
            # 崩溃恢复阶段也读 state: 损坏同样隔离留证, 不进无落账的 unleased 路径。
            return self._corrupt_failure(reason, exc)
        except Exception as exc:
            return self._unleased_failure(reason, _failure_code(exc))
        try:
            acquired = self.state_store.acquire(
                reason=reason,
                config_revision=self.config.revision(),
                lease_seconds=_lease_seconds(self.config),
                now=now,
            )
        except CuratorStateCorruptError as exc:
            # 损坏 state 拒绝接管: 不 acquire、不消费、不自动重跑, 隔离留证转人工。
            return self._corrupt_failure(reason, exc)
        if acquired is None:
            return self._result(status="busy", reason=reason)
        context = _run_context(reason, *acquired)
        try:
            return self._execute(context)
        except Exception as exc:
            return self._commit_failure(context, exc)

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
        except CuratorStateCorruptError as exc:
            # 失败落账阶段发现 state 损坏: 同样隔离留证, 不推进任何事实。
            return self._corrupt_failure(context.reason, exc)
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

    # LLM: 损坏 state 的失败路径与普通失败分离: 独立错误码 CURATOR_STATE_CORRUPT(不伪装
    # CURATOR_SCHEMA_INVALID), 原子 quarantine 原文件留不可变副本, run_log 落幂等审计。
    # 哨兵存在后不再重复隔离; quarantine/哨兵写失败转 CURATOR_STATE_QUARANTINE_FAILED
    # 保持可观测, 每 tick 单次尝试, 绝不循环重试或伪造自愈成功。
    # 函数用途: 返回损坏隔离后的稳定失败结果并落无正文审计。
    def _corrupt_failure(self, reason: str, exc: CuratorStateCorruptError) -> CuratorRunResult:
        failure_code = "CURATOR_STATE_CORRUPT"
        finished_at = utc_now_iso()
        # 隔离失败退避: 同 error_class 在冷却窗口内复用上次失败审计(不新增行),
        # 期满才再试——防 quarantine 长期失败时 run_log 无限刷屏。
        if not self.state_store.quarantine_backoff_eligible(exc.error_class, now=finished_at):
            return self._quarantine_backoff_reuse(reason, exc, finished_at)
        quarantine: dict[str, object] | None = None
        try:
            quarantine = self.state_store.quarantine_corrupt(
                error_class=exc.error_class,
                now=finished_at,
            )
        except Exception:
            failure_code = "CURATOR_STATE_QUARANTINE_FAILED"
        if failure_code == "CURATOR_STATE_QUARANTINE_FAILED":
            # 隔离写失败: 每次尝试都是独立失败事件。哨兵可能已由本次尝试半写成功,
            # 不能用哨兵时间/派生 run_id(会与后续成功隔离的幂等记录碰撞); 用当前时刻
            # + 一次性 run_id, 失败可观测且每次可见, 不伪造幂等。
            run_id = "memory-curator-corrupt-" + uuid.uuid4().hex
            record_time = finished_at
            quarantine = {}
            try:
                # 记录退避事实(含本次一次性 run_id): 冷却窗口内复用本审计, 不新增行。
                self.state_store.record_quarantine_backoff(
                    exc.error_class, now=finished_at, run_id=run_id
                )
            except Exception:  # noqa: BLE001 退避留痕失败不掩盖隔离失败本身
                pass
        else:
            if quarantine is None:
                try:
                    quarantine = self.state_store.corrupt_evidence() or {}
                except Exception:
                    quarantine = {}
            run_id = _corrupt_run_id(quarantine)
            # 审计时间戳用隔离时间(首次)或哨兵时间(重复 run): record_id 稳定, 幂等合并不刷屏。
            record_time = str(quarantine.get("quarantined_at") or finished_at)
        warnings = [
            f"state_quarantined:{path}"
            for path in [str(quarantine.get("quarantine_path") or "")]
            if path
        ]
        record = CuratorRunRecord(
            run_id=run_id,
            lease_id="",
            status="failed",
            reason=reason,
            provider=self.provider_name,
            model=self.model_name,
            started_at=record_time,
            finished_at=record_time,
            failure_code=failure_code,
            warnings=tuple(warnings),
        )
        try:
            self.run_log.append(record)
        except Exception:
            failure_code = "CURATOR_RUN_AUDIT_FAILED"
        return CuratorRunResult(
            run_id=run_id,
            status="failed",
            reason=reason,
            provider=self.provider_name,
            model=self.model_name,
            failure_code=failure_code,
        )

    # LLM: 退避窗口内不重复尝试隔离, 复用上一次失败审计: 同 run_id/时间 → append
    # 幂等合并不新增行(record_id 稳定), 失败仍可观测(上次审计在), 不伪造自愈。
    # 函数用途: 隔离失败冷却期内返回与上次一致的可观测失败结果。
    def _quarantine_backoff_reuse(
        self,
        reason: str,
        exc: CuratorStateCorruptError,
        finished_at: str,
    ) -> CuratorRunResult:
        marker = self.state_store.quarantine_backoff_evidence()
        run_id = str(marker.get("run_id") or "").strip()
        if not run_id:
            run_id = "memory-curator-corrupt-" + uuid.uuid4().hex
        record_time = str(marker.get("last_attempt_at") or finished_at)
        record = CuratorRunRecord(
            run_id=run_id,
            lease_id="",
            status="failed",
            reason=reason,
            provider=self.provider_name,
            model=self.model_name,
            started_at=record_time,
            finished_at=record_time,
            failure_code="CURATOR_STATE_QUARANTINE_FAILED",
            warnings=(),
        )
        failure_code = "CURATOR_STATE_QUARANTINE_FAILED"
        try:
            self.run_log.append(record)  # 幂等: 同 record_id 合并不新增行
        except Exception:
            failure_code = "CURATOR_RUN_AUDIT_FAILED"
        return CuratorRunResult(
            run_id=run_id,
            status="failed",
            reason=reason,
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


# LLM: 提取/提交/晋升链与触发调度分离:Execution 只消费已持 lease 的上下文,把输入快照、
# 模型提取、整批事务和提交后晋升收拢在一条可审计路径上。
# 类用途: 提供 Curator 的持 lease 执行链。
class _CuratorExecutionMixin:
    # LLM: Extraction/evidence validation 全部在事务收到任何权威内容前完成。
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

    # LLM: 升级自愈:每次持 lease 提炼前幂等应用 Memory v2 迁移,失败只记 warning 不阻断。
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

    # LLM: Formal memory 走固定子预算;due 检查省略它,实际 run 包含它而不动游标契约。
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
                # 正式记忆是增强输入非主链依赖:读取失败降级为空并记入错误,不阻断提炼。
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

    # LLM: committer 最后写 state;自动晋升只在事务成功后开始,不能回溯失效其游标。
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
        return self._post_commit_report(
            context, batch, prepared, committed, extra_warnings
        )

    # LLM: 自动晋升在事务成功后执行,失败与已成功事务隔离且保持可重试。
    # 函数用途: 提交成功后执行自动晋升并构造成功结果。
    def _post_commit_report(
        self,
        context: _RunContext,
        batch: CuratorInputBatch,
        prepared: _PreparedOutputs,
        committed: CuratorBatchCommit,
        extra_warnings: tuple[str, ...],
    ) -> CuratorRunResult:
        # 权限合同:提交后的自动晋升同样只消费宿主已标 auto_eligible 的候选
        # (与 _pending_promotable_candidate_ids 同判据,不重复走 selector 而直接过滤);
        # lesson/hot/subagent/model_inferred 的 manual_required 候选永不回调晋升。
        promotable = [
            item.candidate_id
            for item in committed.candidates
            if str(getattr(item, "promotion_mode", "") or "").strip().lower()
            == "auto_eligible"
        ]
        promoted, promotion_warnings = self._promote_eligible(promotable)
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

    # LLM: 晋升只消费已提交候选 ID;失败与已成功事务隔离且保持可重试。
    # 函数用途: 执行保守自动晋升并返回数量和稳定警告码。
    def _pending_promotable_candidate_ids(self, *, limit: int = 32) -> list[str]:
        """已落库、待晋升的合格候选 ID(纯结构化:只读 pending_review 状态;失败静默返空)。
        LLM: 只取宿主已标 auto_eligible 的 user_explicit/tool_verified long_term 候选;
        lesson/hot/subagent/model_inferred 一律 manual_required,永不出现在选择器。
        """
        try:
            rows = self.candidate_service.list(statuses={"pending_review"}, limit=limit)
            ids: list[str] = []
            for row in rows:
                origin = str(getattr(row, "origin", "") or "").strip().lower()
                target = str(getattr(row, "promotion_target", "") or "").strip().lower()
                mode = str(getattr(row, "promotion_mode", "") or "").strip().lower()
                # 权限闸:非 auto_eligible(含 legacy 归一 manual_required)一律不选。
                if mode != "auto_eligible":
                    continue
                # user_explicit/tool_verified:target 为空或 long_term 都算可晋升候选——
                # promote 入口会把 target=none 的 user_explicit 长期事实补成 long_term
                # (模型偶发漏写 target,宿主确定性补全)。model_inferred/Persona 一律排除。
                if origin in {"user_explicit", "tool_verified"} and target in {"", "none", "long_term"}:
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


# LLM: This is the only background Memory extraction service used by Gateway maintenance, CLI,
# compact, task completion, and lifecycle signals.
# 类用途: 执行一个 owner 的增量 Daily/Candidate 策展闭环。
class MemoryCuratorService(_CuratorExecutionMixin, _CuratorLifecycleMixin):
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

    # LLM: Status exposes configuration and durable health only; no prompt, response, formal
    # memory, candidate body, or user content is returned.
    # 函数用途: 为 CLI/doctor 返回 Curator 当前配置与 state。
    def recover(self) -> dict[str, object]:
        """人工恢复通道(CLI memory curator recover): 凭权威审计链重建 state 解除隔离。

        审计 seq1477 门槛2: 恢复须结构化/幂等/可审计, 不能由普通文本或「删哨兵」
        单步触发。校验链(全部通过才执行任何写):
          1. 隔离副本存在且 sha256 与哨兵一致(隔离完整性/防篡改)
          2. run_log 存在成功审计 → 恢复点 = 最后一条成功 cursor_after(权威连续
             账本; 损坏 run 被 fail-closed 从不 commit, 不可能越界推进成功链)
          3. 副本可解析时其游标与最后成功审计等值(双向证明); 不等 → 拒绝转人工
        全部通过 → 重建 state(游标=权威点, 清不可信 lease, 保留可解析副本的
        pending/last_* 字段) → 先原子写 state 再删哨兵 → 恢复事件以
        recovered_rollback/recovery 形态落 run_log 审计。
        任一校验失败 → 拒绝执行(refused + 结构化原因), 不伪造自愈成功。
        """
        evidence = self.state_store.corrupt_evidence()
        if evidence is None:
            return {"status": "noop", "reason": "no_quarantine_marker"}
        # 1. 隔离副本完整性: 存在 + hash 与哨兵一致
        quarantine_path = self.state_store.path.parent / str(
            evidence.get("quarantine_path") or ""
        )
        if not quarantine_path.is_file():
            return {
                "status": "refused",
                "reason": "quarantine_copy_missing",
                "evidence": _recover_evidence_summary(evidence),
            }
        if _sha256_file(quarantine_path) != str(evidence.get("original_sha256") or ""):
            return {
                "status": "refused",
                "reason": "quarantine_copy_mismatch",
                "evidence": _recover_evidence_summary(evidence),
            }
        # 2. 权威审计链: 成功记录按时间升序, 恢复点 = 最后一条 cursor_after
        succeeded = [r for r in self.run_log.list() if r.status == "succeeded"]
        if not succeeded:
            return {
                "status": "refused",
                "reason": "no_authoritative_audit",
                "evidence": _recover_evidence_summary(evidence),
            }
        last_success = succeeded[-1]
        authoritative = last_success.cursor_after
        if not isinstance(authoritative, dict) or not authoritative:
            return {
                "status": "refused",
                "reason": "no_authoritative_cursor",
                "evidence": _recover_evidence_summary(evidence),
            }
        # 3. 副本可解析时: 游标与审计链双向等值(未分裂/未越界)
        copy_payload: dict[str, object] | None = None
        try:
            report = read_json_object_report(
                quarantine_path, context="memory_curator.quarantine_copy"
            )
            if not report.load_error:
                copy_payload = report.payload
        except Exception:  # noqa: BLE001 副本解析失败不阻断(走纯审计重建)
            copy_payload = None
        if isinstance(copy_payload, dict):
            copy_cursors = copy_payload.get("per_thread_cursors")
            auth_cursors = authoritative.get("per_thread_cursors")
            if isinstance(copy_cursors, dict) and copy_cursors != auth_cursors:
                return {
                    "status": "refused",
                    "reason": "cursor_mismatch_with_audit",
                    "evidence": _recover_evidence_summary(evidence),
                }
            copy_audit = copy_payload.get("last_processed_audit_event_id")
            auth_audit = authoritative.get("last_audit_event_id")
            if (
                copy_audit is not None
                and str(copy_audit or "") != str(auth_audit or "")
            ):
                return {
                    "status": "refused",
                    "reason": "audit_event_id_mismatch",
                    "evidence": _recover_evidence_summary(evidence),
                }
        # 4. 重建 state: 游标/审计 = 权威链; 副本可解析时保留 pending/last_* 字段
        try:
            if isinstance(copy_payload, dict):
                restored = MemoryCuratorState.from_dict(copy_payload)
            else:
                restored = MemoryCuratorState()
            restored = replace(
                restored,
                last_processed_message_id=str(
                    authoritative.get("last_processed_message_id") or ""
                ),
                last_processed_audit_event_id=str(
                    authoritative.get("last_audit_event_id") or ""
                ),
                per_thread_cursors=dict(authoritative.get("per_thread_cursors") or {}),
                active_lease={},  # 损坏前 lease 不可信, 绝不接管
                last_failure_at="",
                last_failure_code="",
                last_committed_run_id=last_success.run_id,
            )
            if not isinstance(copy_payload, dict):
                # 副本不可解析(unreadable): 计数从权威审计链累计(恢复点不丢账)。
                restored = replace(
                    restored,
                    processed_count=sum(
                        int(r.processed_messages or 0) + int(r.processed_audit_events or 0)
                        for r in succeeded
                    ),
                    candidate_count=sum(int(r.candidates or 0) for r in succeeded),
                    daily_event_count=sum(int(r.daily_events or 0) for r in succeeded),
                )
            validate_curator_state(restored)
        except (TypeError, ValueError):
            return {
                "status": "refused",
                "reason": "restored_state_invalid",
                "evidence": _recover_evidence_summary(evidence),
            }
        # 5. 原子落回 + 删哨兵(最后一步), 然后写恢复审计
        recovered_at = utc_now_iso()
        try:
            self.state_store.restore_after_recovery(
                restored, sentinel_evidence=evidence
            )
        except CuratorStateCorruptError as exc:
            return {
                "status": "refused",
                "reason": str(exc.error_class or "restore_failed"),
                "evidence": _recover_evidence_summary(evidence),
            }
        audit_ok = True
        try:
            self.run_log.append(
                CuratorRunRecord(
                    run_id="memory-curator-recover-" + uuid.uuid4().hex,
                    lease_id="",
                    status="recovered_rollback",
                    phase="recovery",
                    reason="admin",
                    provider=self.provider_name,
                    model=self.model_name,
                    started_at=recovered_at,
                    finished_at=recovered_at,
                    cursor_after=dict(authoritative),
                    recovery={
                        "kind": "manual_from_quarantine",
                        "sentinel_sha256": str(evidence.get("original_sha256") or ""),
                        "quarantine_path": str(evidence.get("quarantine_path") or ""),
                        "error_class": str(evidence.get("error_class") or ""),
                        "restored_from_run_id": last_success.run_id,
                        "restored_at": recovered_at,
                    },
                )
            )
        except Exception:  # noqa: BLE001 审计写失败不阻断已完成的恢复, 如实标注
            audit_ok = False
        return {
            "status": "recovered",
            "reason": "restored_from_authoritative_audit",
            "restored_from_run_id": last_success.run_id,
            "audit_recorded": audit_ok,
            "evidence": _recover_evidence_summary(evidence),
        }

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
    # 自主晋升语义;不补 model_inferred/Persona/lesson 等(那些必须人工)。同时按定版合同给
    # 每条提炼候选确定性赋 promotion_mode(合格 user/tool 新 long_term fact/event/project→
    # auto_eligible,其余一律 manual_required)。纯结构化规则,不猜正文,模型 Schema 不暴露该字段。
    candidates = tuple(
        _fill_candidate_promotion_authority(item) for item in extraction.candidates
    )
    return _PreparedOutputs(daily, candidates, cursor, tuple(extraction.warnings))


# LLM: 宿主只根据验证后的结构化来源、证据、动作、目标、范围和冲突赋权；正文与 confidence
# 不参与权限判断，model_inferred/Persona/lesson/HOT 永远不能获得自动权限。
# 函数用途: 返回 promotion_target 补全及 fail-closed promotion_mode 赋权后的候选 observation。
def _fill_candidate_promotion_authority(item: object) -> object:
    target = str(getattr(item, "promotion_target", "") or "").strip().lower()
    origin = str(getattr(item, "origin", "") or "").strip().lower()
    ctype = str(getattr(item, "candidate_type", "") or "").strip().lower()
    if not target and origin == "user_explicit" and ctype in {"long_term_fact", "event"}:
        item = replace(item, promotion_target="long_term")
        target = "long_term"
    scope = MemoryScope.from_value(getattr(item, "scope", None))
    evidence_complete = (
        origin == "user_explicit"
        and bool(getattr(item, "source_message_refs", ()))
    ) or (
        origin == "tool_verified"
        and bool(getattr(item, "source_tool_refs", ()))
    )
    temporary_has_expiry = (
        scope.scope_type != "temporary"
        or bool(str(getattr(item, "valid_until", "") or "").strip())
    )
    # 权限：只有证据完整、范围可落、无冲突的 user/tool 新长期事实才可进入自动路径；
    # Promotion 仍会重新核验真实 message/tool evidence、冲突和 expiry。
    auto_eligible = (
        origin in {"user_explicit", "tool_verified"}
        and ctype in {"long_term_fact", "event", "project"}
        and bool(str(getattr(item, "subject_key", "") or "").strip())
        and str(getattr(item, "proposed_action", "") or "").strip().lower() == "add"
        and target == "long_term"
        and not str(getattr(item, "target_entry_id", "") or "").strip()
        and not tuple(getattr(item, "conflicts_with", ()) or ())
        and evidence_complete
        and temporary_has_expiry
    )
    if auto_eligible:
        return replace(item, promotion_mode="auto_eligible")
    return replace(item, promotion_mode="manual_required")


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


# LLM: 损坏 run_id 由原文件 sha256 派生, 首次(算文件)与重复(读哨兵)取值一致,
# 保证同一损坏的重复维护 tick 落幂等审计, 不伪造自愈成功。
# 函数用途: 从隔离元数据派生稳定损坏 run_id。
# LLM: 恢复拒绝原因只带结构化证据标识(路径/hash 前缀/分类), 不复制损坏正文。
# 函数用途: 生成恢复校验失败时返回的哨兵证据摘要。
def _recover_evidence_summary(evidence: dict[str, object]) -> dict[str, object]:
    return {
        "quarantine_path": str(evidence.get("quarantine_path") or ""),
        "original_sha256": str(evidence.get("original_sha256") or "")[:16],
        "error_class": str(evidence.get("error_class") or ""),
        "quarantined_at": str(evidence.get("quarantined_at") or ""),
    }


def _corrupt_run_id(quarantine: dict[str, object]) -> str:
    digest = str(quarantine.get("original_sha256") or "")
    if len(digest) < 16:
        return "memory-curator-corrupt-" + uuid.uuid4().hex
    return "memory-curator-corrupt-" + digest[:16]


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
    if isinstance(exc, CuratorStateCorruptError):
        return "CURATOR_STATE_CORRUPT"
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
