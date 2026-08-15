from __future__ import annotations

"""Memory v2 统一 retention Service 与旧 home 入口委托层。"""

# LLM: 所有 plan/apply 调用必须进入 MemoryRetentionService；旧 home-retention 只能作为同服务 CLI alias。
# 模块用途: 组合 typed policy 扫描、锁内重规划、执行重验证，以及正式长期记忆硬删除。

from datetime import datetime, timezone
from pathlib import Path

from ..common.json_io import locked_json_path
from .candidates import CandidateService
from .jsonl import JsonlMemory, MemoryRecord
from .retention_apply import execute_retention_plan, record_retention_report
from .retention_models import (
    RETENTION_PLAN_SCHEMA_VERSION,
    RETENTION_SCHEMA_VERSION,
    MemoryRetentionAction,
    MemoryRetentionError,
    MemoryRetentionPolicy,
    MemoryRetentionReport,
)
from .retention_scan import build_retention_plan


# LLM: Service 持有 owner home、唯一 CandidateService 和可选 JsonlMemory；它不解析自然语言删除范围。
# 类用途: 为管理员 CLI 和 Gateway owner maintenance 提供同一 retention 计划/执行主链。
class MemoryRetentionService:
    # LLM: 构造器只登记当前 owner 的规范根和正式仓库；conversation roots 仍由其独立 retention 合同管理。
    # 函数用途: 初始化统一 Memory Retention 计划与执行服务。
    def __init__(
        self,
        *,
        home_paths: object,
        candidates: CandidateService,
        long_term: JsonlMemory | None = None,
        conversation_roots: tuple[str | Path, ...] = (),
    ) -> None:
        self.home = home_paths
        self.candidates = candidates
        self.long_term = long_term
        self.conversation_roots = tuple(
            Path(path).expanduser().resolve(strict=False) for path in conversation_roots
        )

    # LLM: dry-run 只调用只读 scanner，不创建 lock、trash、audit 或任何目标文件。
    # 函数用途: 返回当前 owner 的 retention 清理计划。
    def plan(self, *, now: datetime | None = None) -> MemoryRetentionReport:
        return build_retention_plan(
            home=self.home,
            candidates=self.candidates,
            conversation_roots=self.conversation_roots,
            now=_normalize_now(now),
        )

    # LLM: apply 在 owner retention 锁内重新 plan；策略损坏、legal hold 或任一扫描错误时不执行任何动作。
    # 函数用途: 应用当前时刻重新验证出的 retention 计划。
    def apply(self, *, now: datetime | None = None) -> MemoryRetentionReport:
        current = _normalize_now(now)
        lock_anchor = Path(self.home.owner_trash_dir) / ".memory-retention-v2"
        with locked_json_path(lock_anchor):
            plan = self.plan(now=current)
            if plan.errors or plan.legal_hold:
                stopped = MemoryRetentionReport(
                    applied=False,
                    actions=(),
                    errors=plan.errors,
                    legal_hold=plan.legal_hold,
                    policy_fingerprint=plan.policy_fingerprint,
                )
                record_retention_report(home=self.home, report=stopped, now=current)
                return stopped
            return execute_retention_plan(
                home=self.home,
                candidates=self.candidates,
                plan=plan,
                now=current,
                allowed_external_roots=self.conversation_roots,
            )

    # LLM: 正式长期记忆删除只按稳定 entry_id/version 调 JsonlMemory 权威链；不得重写 ConversationStore。
    # 函数用途: 硬删除一条正式长期记忆并触发正文、索引、候选 redaction、tombstone 和 ops 清理。
    def hard_delete_long_term(
        self,
        entry_id: str,
        *,
        expected_version: int,
        source: str = "memory-retention-admin",
    ) -> MemoryRecord:
        if self.long_term is None:
            raise RuntimeError("long-term repository is required for hard delete")
        target = str(entry_id or "").strip()
        if not target:
            raise ValueError("entry_id is required")
        return self.long_term.remove(
            target,
            source=str(source or "memory-retention-admin"),
            expected_version=int(expected_version),
        )


# LLM: 旧函数名只委托唯一 v2 Service，不保留旧 policy key、scanner 或 executor。
# 函数用途: 兼容现有 home doctor/maintenance 的 dry-run 调用。
def plan_owner_retention(
    home: object,
    *,
    now: datetime | None = None,
) -> MemoryRetentionReport:
    service = MemoryRetentionService(
        home_paths=home,
        candidates=CandidateService(home.owner_memory_candidates_jsonl),
    )
    return service.plan(now=now)


# LLM: 旧 apply 函数同样委托 v2 Service；它不是另一套运行路径。
# 函数用途: 兼容 Gateway owner maintenance 和旧 home-retention CLI。
def apply_owner_retention(
    home: object,
    *,
    now: datetime | None = None,
) -> MemoryRetentionReport:
    service = MemoryRetentionService(
        home_paths=home,
        candidates=CandidateService(home.owner_memory_candidates_jsonl),
    )
    return service.apply(now=now)


# LLM: naive datetime 仅供测试并按 UTC 解释，避免本地时区让保留 cutoff 漂移。
# 函数用途: 规范 plan/apply 共用当前时间。
def _normalize_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


OwnerRetentionPlan = MemoryRetentionReport
RetentionAction = MemoryRetentionAction

__all__ = [
    "RETENTION_PLAN_SCHEMA_VERSION",
    "RETENTION_SCHEMA_VERSION",
    "MemoryRetentionAction",
    "MemoryRetentionError",
    "MemoryRetentionPolicy",
    "MemoryRetentionReport",
    "MemoryRetentionService",
    "OwnerRetentionPlan",
    "RetentionAction",
    "apply_owner_retention",
    "plan_owner_retention",
]
