# LLM: 协作账本的唯一读写入口：case 是 JSON 快照，请求/证据/参与者/决策是只追加 JSONL（同 id 取最后一行）。
#   追加由 io.jsonl 的文件锁串行；“读当前快照→改→写回”的状态更新另由按 case 的更新锁串行（_case_update_lock），
#   否则晚写入的旧快照会覆盖别人刚写的状态。改动须同步 test_collaboration_store*.py 与 test_collaboration_concurrency.py。
# 模块用途: 保存多代理协作的 case、请求、证据、参与者、决策和能力登记，供协作工具与主代理汇总读取。
"""Collaboration store for cases, requests, evidence, participants,
decisions, and agent capabilities.
"""

from __future__ import annotations

import copy
import json
import sqlite3
import threading
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..common.json_io import (
    append_private_jsonl_records,
    locked_json_path,
    write_json_file_atomic_unlocked,
    write_private_jsonl_records,
)
from ..common.nofollow_fs import ensure_private_dir, read_bytes_beneath
from ..common.opaque_id import validate_opaque_id
from ..gateway_parts.io import read_json_file, write_json_file_atomic
from ..runtime_errors import runtime_error_report
from .coverage import case_response_coverage
from .history_index import CollaborationHistoryIndex, file_signature
from .identity import agent_identity_keys, request_update_targets
from .models import (
    AGENT_CAPABILITY_STATUS_AVAILABLE,
    AgentCapability,
    CaseDecision,
    CaseParticipant,
    CollaborationCase,
    CollaborationRequest,
    EvidencePacket,
    HandoffBoardItem,
    new_case_id,
    new_decision_id,
    new_evidence_id,
    new_request_id,
)
from .request_status import (
    CollaborationRequestStatus,
    case_overview_row,
    case_status_for_update,
    case_status_protocol_metadata,
    case_status_text,
    case_window_status,
    evidence_sources_by_request,
    is_blocked_request_status,
    is_completed_request_status,
    is_declined_request_status,
    is_terminal_status,
    missing_responder_agent_ids_by_request,
    pending_request_row,
    request_has_required_evidence,
    request_is_effectively_timed_out,
    request_response_status,
    request_status_for_update,
    request_status_protocol_metadata,
    unavailable_target_agent_ids_by_request,
)
from .store_common import dict_items, now, read_jsonl_report, strings
from .store_common import now as current_time

# 协作单文件进程内缓存的最大条目数。
_CASE_FILE_CACHE_MAX_ENTRIES_COUNT = 512
_CASE_FILE_CACHE_LOCK = threading.RLock()
_CASE_FILE_CACHE: OrderedDict[tuple[str, int, int, int, int, int], CollaborationCase] = OrderedDict()


# LLM: 版本仅供_all_cases只读投影；ctime识别等长恢复mtime的改写，status更新仍锁内读canonical，不复用缓存授权。
# 函数用途: 生成路径/设备/inode/大小/mtime/ctime协作文件键，stat失败回原读取。
def _case_file_key(path: Path):
    try:
        info = path.stat()
    except OSError:
        return None
    return str(path), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


# LLM: 只记忆成功且读前后稳定的case；entities/metadata均深拷贝，不把浅replace或空记录当独立快照。
# 函数用途: 复用未变化协作JSON的建模结果，保持原空值与转换异常语义。
def _read_cached_case(path: Path) -> CollaborationCase | None:
    key = _case_file_key(path)
    with _CASE_FILE_CACHE_LOCK:
        cached = _CASE_FILE_CACHE.get(key) if key is not None else None
        if cached is not None:
            _CASE_FILE_CACHE.move_to_end(key)
    if cached is not None:
        return copy.deepcopy(cached)
    data = read_json_file(path)
    if not data:
        return None
    case = CollaborationCase.from_dict(data)
    if key is not None and _case_file_key(path) == key:
        snapshot = copy.deepcopy(case)
        with _CASE_FILE_CACHE_LOCK:
            _CASE_FILE_CACHE[key] = snapshot
            _CASE_FILE_CACHE.move_to_end(key)
            while len(_CASE_FILE_CACHE) > _CASE_FILE_CACHE_MAX_ENTRIES_COUNT:
                _CASE_FILE_CACHE.popitem(last=False)
    return case


# ---------------------------------------------------------------------------
# shared layout
# ---------------------------------------------------------------------------

# LLM: 目录布局与按 case 的更新锁都在这里定义，子类只能经这些路径读写，不另造位置或第二把状态锁。
# 类用途: 协作账本的公共目录布局和共享工具，所有协作 store 子类都继承它。
class CollaborationBaseStore:
    """Shared directory layout for case/request/evidence ledgers."""

    # LLM: 原目录和账本仍保留；旧 case 在后台固定批次迁移，构造不读取整份旧 JSON 名册。
    # 函数用途: 初始化协作存储及按行索引的唯一生产维护入口。
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.cases_dir = self.root / "cases"
        self.requests_dir = self.root / "requests"
        self.evidence_dir = self.root / "evidence"
        self.participants_dir = self.root / "participants"
        self.decisions_dir = self.root / "decisions"
        self.handoff_items_dir = self.root / "handoff_items"
        self.capabilities_path = self.root / "agent_capabilities.json"
        self._ensure_dirs()
        self.history_index = CollaborationHistoryIndex(self.root)
        self._history_worker_lock = threading.Lock()
        self._history_worker = None
        self._history_maintenance_report = {}
        self._start_history_maintenance()

    def _ensure_dirs(self) -> None:
        for path in (
            self.cases_dir,
            self.requests_dir,
            self.evidence_dir,
            self.participants_dir,
            self.decisions_dir,
        ):
            ensure_private_dir(path)

    # LLM: 持久 dirty 先于规范发表；规范写失败不清占位，投影成功与指纹一起原子提交。
    # 函数用途: 保留原 case JSON 写入口，并增量同步 case 状态路由。
    def _write_case(self, case: CollaborationCase) -> None:
        self._prepare_history_write(case.case_id)
        self.history_index.mark_dirty(case.case_id, published=self._case_path(case.case_id).exists())
        write_json_file_atomic(self._case_path(case.case_id), case.to_dict())
        self.history_index.commit_event(case.case_id, self._history_signature(case.case_id), case=case)

    # LLM: 精确三文件 stat 是一致性门；不用目录/正文标题推断业务身份。
    # 函数用途: 记录一个 case 的规范版本，用于点查识别外部改写或未结提交。
    def _history_signature(self, case_id):
        return json.dumps([file_signature(path) for path in (self._case_path(case_id),
                          self._requests_path(case_id), self._evidence_path(case_id))])

    # LLM: 持原 case 锁复核 ID、版本及 JSONL 错误；先过滤原始行归属，再由 SQL 按本 case 请求 ID 折叠最后快照。
    # 函数用途: 迁移或恢复一个精确 case，保留有效行和坏账事实。
    def _sync_history_case(self, case_id, *, already_locked=False):
        case_id = validate_opaque_id(case_id, kind="case_id")
        if not already_locked:
            with self._case_update_lock(case_id):
                return self._sync_history_case(case_id, already_locked=True)
        before = self._history_signature(case_id)
        data = read_bytes_beneath(self.root, ("cases", f"{case_id}.json"), require_dir_fd=True)
        if data is None:
            if self.history_index.discard_unpublished(case_id):
                return
            raise KeyError(f"collaboration canonical case unavailable: {case_id}")
        case = CollaborationCase.from_dict(json.loads(data))
        if case.case_id != case_id:
            raise ValueError("collaboration canonical case identity mismatch")
        requests, request_errors = self.case_request_history_report(case_id)
        evidence, evidence_errors = self.case_evidence_report(case_id)
        requests, misplaced_requests = self._owned_history_rows(case_id, requests, "requests")
        evidence, misplaced_evidence = self._owned_history_rows(case_id, evidence, "evidence")
        after = self._history_signature(case_id)
        if before != after:
            raise RuntimeError("collaboration canonical files changed during index recovery")
        self.history_index.sync_case(case, requests, evidence,
                                     [*request_errors, *evidence_errors, *misplaced_requests, *misplaced_evidence], after)

    # LLM: 规范文件所属 case 决定边界；冲突行单列错误，不跨 case 投影，也不丢其它有效行。
    # 函数用途: 从迁移/修复批次中分离归属错误的可解析行。
    @staticmethod
    def _owned_history_rows(case_id, rows, kind):
        valid, errors = [], []
        for record_index, row in enumerate(rows, 1):
            if row.case_id == case_id:
                valid.append(row)
            else:
                error = runtime_error_report(ValueError("collaboration case identity mismatch"),
                                             context=f"collaboration.{kind}.read")
                error.update(case_id=case_id, claimed_case_id=row.case_id, record_index=record_index)
                errors.append(error)
        return valid, errors

    # LLM: 原 case 锁内先核验旧来源，避免增量写将此前未索引的有效行/坏账盖成新的健康指纹；不重入文件锁。
    # 函数用途: 仅在该 case 来源变化或尚未迁移时精确恢复，再允许写新事件。
    def _prepare_history_write(self, case_id):
        current = self._history_signature(case_id)
        previous = self.history_index.case_signature(case_id)
        if previous != current and self._case_path(case_id).exists():
            self._sync_history_case(case_id, already_locked=True)

    # LLM: 手动/后台每轮各消费一页精确 dirty 和稳定库存；失败标记保留，不以成功数推断完整覆盖。
    # 函数用途: 固定批次修复/迁移 case 并返回真实覆盖和错误。
    def backfill_history_index(self):
        errors, repaired = self._recover_history_cases()
        loaded = 0
        for seq, case_id in self.history_index.inventory_batch():
            try:
                self._sync_history_case(case_id)
                self.history_index.complete_inventory(seq)
                loaded += 1
            except Exception as exc:  # noqa: BLE001 保留覆盖未知，绝不回退全树 pending 扫描
                error = runtime_error_report(exc, context="collaboration.history.migration")
                error["case_id"] = case_id
                errors.append(error)
        self.history_index.complete_inventory(None)
        return {**self.history_index.coverage(), "loaded": loaded, "repaired": repaired, "errors": errors}

    # LLM: 只用于已在停写/同 case 锁约束下完成并稳定的外部修复；不改规范文件、不自动发现任意手工写。
    #   标记耐久化后由正常读前恢复或后台固定页消费，调用成功不等于重建已经完成。
    # 函数用途: 将一个明确修复完的冷 case 交回生产索引恢复队列。
    def invalidate_history_case(self, case_id: str) -> None:
        case_id = validate_opaque_id(case_id, kind="case_id")
        if not self._case_path(case_id).exists() and self.history_index.case_signature(case_id) is None:
            raise KeyError(f"collaboration case unknown: {case_id}")
        with self._case_update_lock(case_id):
            self.history_index.mark_dirty(case_id, published=True)
        self._start_history_maintenance()

    # LLM: 每实例至多一个后台迁移，健康库立即返回；启动不是覆盖完成证明。
    # 函数用途: 自动调度旧 case 的生产迁移，不在 hot miss 中同步枚举。
    def _start_history_maintenance(self):
        if self.history_index.coverage()["complete"]:
            return
        with self._history_worker_lock:
            if self._history_worker is not None and self._history_worker.is_alive():
                return
            self._history_worker = threading.Thread(target=self._maintain_history, name="collaboration-history", daemon=True)
            self._history_worker.start()

    # LLM: 首批建立库存后按真实剩余数限定轮数，遇到坏账立即停该轮，不无限重试或伪报覆盖。
    # 函数用途: 后台逐页收敛旧 case 投影。
    def _maintain_history(self):
        try:
            report = self.backfill_history_index()
            for _ in range((report["remaining"] + 63) // 64 + 1):
                if report["complete"] or report["errors"]:
                    break
                report = self.backfill_history_index()
            self._history_maintenance_report = report
        except Exception as exc:  # noqa: BLE001 未完成覆盖继续显式拒绝健康空集
            self._history_maintenance_report = {"complete": False, "errors": [runtime_error_report(exc, context="collaboration.history.migration")]}

    # LLM: 每轮最多修一页已知 dirty；同一原 case 锁内核对源，失败保持标记，不触发目录发现。
    # 函数用途: 供读前和后台共用的精确恢复入口。
    def _recover_history_cases(self):
        errors, repaired = [], 0
        for case_id in self.history_index.dirty_cases():
            try:
                self._sync_history_case(case_id)
                repaired += 1
            except Exception as exc:  # noqa: BLE001 精确恢复失败继续保留 dirty
                error = runtime_error_report(exc, context="collaboration.history.recovery")
                error["case_id"] = case_id
                errors.append(error)
        return errors, repaired

    # LLM: 正常 pending 先消费固定页精确恢复，再请求仍未完成的后台维护；热缺失不全树扫描。
    # 函数用途: 在读取候选前恢复已知失败或外部修复通知。
    def _prepare_history_read(self):
        errors, _ = self._recover_history_cases()
        self._start_history_maintenance()
        return errors

    def _case_path(self, case_id: str) -> Path:
        return self.cases_dir / f"{case_id}.json"

    # LLM: 同一 case 的“读当前快照→改→写回”必须串行：2026-09-27 全仓并发测试里，一个很早读到 open 的请求更新最后才写入，
    #   把已完成的请求写回 open（同理会丢掉别人刚写的改派目标、摘要）。锁文件 cases/<case_id>.update.lock 与 JSONL/JSON
    #   自己的写锁不同名，临界区里照常调用 append_jsonl、write_json_file_atomic 不会自锁；同一线程不能嵌套取这把锁。
    #   调用方必须先用 load_case 确认 case 存在，再取锁，避免按外部传入的 case_id 在别处建锁文件。
    # 函数用途: 给一个 case 的状态更新加进程内与跨进程排他锁，保证每次更新都基于上一次的结果。
    @contextmanager
    def _case_update_lock(self, case_id: str):
        with locked_json_path(self.cases_dir / f"{case_id}.update"):
            yield

    def _requests_path(self, case_id: str) -> Path:
        return self.requests_dir / f"{case_id}.jsonl"

    def _evidence_path(self, case_id: str) -> Path:
        return self.evidence_dir / f"{case_id}.jsonl"

    def _participants_path(self, case_id: str) -> Path:
        return self.participants_dir / f"{case_id}.jsonl"

    def _decisions_path(self, case_id: str) -> Path:
        return self.decisions_dir / f"{case_id}.jsonl"

    def _handoff_items_path(self, case_id: str) -> Path:
        case_id = validate_opaque_id(case_id, kind="case_id")
        return self.handoff_items_dir / f"{case_id}.jsonl"


# ---------------------------------------------------------------------------
# evidence records
# ---------------------------------------------------------------------------

class CollaborationEvidenceStore(CollaborationBaseStore):
    # LLM: 参与人账本按 case 追加，落盘走私有追加（0600/0700）；case 各目录由构造器按 0700 建（pdp）。
    # 函数用途: 登记一名参与人并返回归一化后的记录。
    def add_participant(self, request: dict) -> CaseParticipant:
        participant = CaseParticipant(
            case_id=str(request.get("case_id") or ""),
            agent_id=str(request.get("agent_id") or ""),
            role=str(request.get("role") or ""),
            status=str(request.get("status") or "requested"),
            requested_capabilities=strings(request.get("requested_capabilities")),
            joined_at=current_time(request.get("now")),
            metadata=request.get("metadata") or {},
        )
        # 私有追加：0600/0700，存量宽权限协作账下次写入即收紧；内容逐字节不变。
        append_private_jsonl_records(self._participants_path(participant.case_id), [participant.to_dict()], sort_keys=True)
        return participant

    # LLM: 证据格式及私有追加不变；同一原 case 锁内先标 dirty，再发表规范证据和精确回应投影。
    # 函数用途: 提交一份证据包并落进该 case 的证据账。
    def submit_evidence(self, request: dict) -> EvidencePacket:
        case_id = str(request.get("case_id") or "")
        self.load_case(case_id)
        evidence = EvidencePacket(
            evidence_id=new_evidence_id(),
            case_id=case_id,
            request_id=str(request.get("request_id") or ""),
            source_agent_id=str(request.get("source_agent_id") or ""),
            matched=bool(request.get("matched")),
            summary=str(request.get("summary") or ""),
            evidence_refs=strings(request.get("evidence_refs")),
            queried_scopes=strings(request.get("queried_scopes")),
            used_query_hints=strings(request.get("used_query_hints")),
            miss_reason=str(request.get("miss_reason") or ""),
            response_facts=dict_items(request.get("response_facts")),
            followup_suggestions=dict_items(request.get("followup_suggestions")),
            query_actions=dict_items(request.get("query_actions")),
            confidence=float(request.get("confidence") or 0.0),
            limitations=strings(request.get("limitations")),
            created_at=current_time(request.get("now")),
            metadata=request.get("metadata") or {},
        )
        # 私有追加：0600/0700，存量宽权限协作账下次写入即收紧；内容逐字节不变。
        with self._case_update_lock(case_id):
            self._prepare_history_write(case_id)
            self.history_index.mark_dirty(case_id, published=True)
            append_private_jsonl_records(self._evidence_path(case_id), [evidence.to_dict()], sort_keys=True)
            self.history_index.commit_event(case_id, self._history_signature(case_id), evidence=evidence)
        return evidence

    def case_evidence(self, case_id: str) -> list[EvidencePacket]:
        evidence, _load_errors = self.case_evidence_report(case_id)
        return evidence

    def case_evidence_report(self, case_id: str) -> tuple[list[EvidencePacket], list[dict[str, Any]]]:
        report = read_jsonl_report(self._evidence_path(case_id), context="collaboration.evidence.read")
        return [EvidencePacket.from_dict(row) for row in report.rows], report.load_errors

    def case_participants(self, case_id: str) -> list[CaseParticipant]:
        participants, _load_errors = self.case_participants_report(case_id)
        return participants

    def case_participants_report(self, case_id: str) -> tuple[list[CaseParticipant], list[dict[str, Any]]]:
        report = read_jsonl_report(self._participants_path(case_id), context="collaboration.participants.read")
        return [CaseParticipant.from_dict(row) for row in report.rows], report.load_errors

    # LLM: 裁决记录追加写私有账本（0600/0700）；只记录结构化字段，不解析自然语言。
    # 函数用途: 把一条 case 裁决追加进裁决账。
    def append_decision(self, decision: CaseDecision) -> CaseDecision:
        # 私有追加：0600/0700，存量宽权限协作账下次写入即收紧；内容逐字节不变。
        append_private_jsonl_records(self._decisions_path(decision.case_id), [decision.to_dict()], sort_keys=True)
        return decision

    def case_decisions(self, case_id: str) -> list[CaseDecision]:
        decisions, _load_errors = self.case_decisions_report(case_id)
        return decisions

    def case_decisions_report(self, case_id: str) -> tuple[list[CaseDecision], list[dict[str, Any]]]:
        report = read_jsonl_report(self._decisions_path(case_id), context="collaboration.decisions.read")
        return [CaseDecision.from_dict(row) for row in report.rows], report.load_errors


# ---------------------------------------------------------------------------
# agent capabilities
# ---------------------------------------------------------------------------

def _capability_roster_load_error(path: Path, exc: BaseException) -> dict[str, Any]:
    report = runtime_error_report(exc, context="collaboration.agent_capabilities.read")
    report["path"] = str(path)
    return report


class CollaborationCapabilityStore(CollaborationEvidenceStore):
    # LLM: 每次只 upsert 一个身份及其能力，旧整体 JSON 仅作迁移来源，不再受 1 MiB 整体读写门槛限制。
    # 函数用途: 保持原登记签名，以按行原子事务持久化一名代理。
    def register_agent(self, capability: AgentCapability) -> AgentCapability:
        self.history_index.upsert_agent(capability)
        return capability

    def agent_capabilities(self) -> list[AgentCapability]:
        capabilities, _load_error = self.agent_capabilities_report()
        return capabilities

    # LLM: 旧名册流式迁移错误与 SQL 错误均显式报告，不把部分迁移解释为健康空名册。
    # 函数用途: 返回全部历史能力身份及准确读错误。
    def agent_capabilities_report(self) -> tuple[list[AgentCapability], dict[str, Any] | None]:
        try:
            return self.history_index.agents(), None
        except (OSError, UnicodeError, ValueError, sqlite3.Error) as exc:
            return [], _capability_roster_load_error(self.capabilities_path, exc)

    def agent_identity_keys(self, values: object = ()) -> set[str]:
        return agent_identity_keys(values)

    def match_agents(
        self,
        *,
        required_capabilities: list[str] | tuple[str, ...] | None = None,
        exclude_agent_id: str = "",
        limit: int = 20,
    ) -> list[AgentCapability]:
        matches, _load_error = self.match_agents_report(
            required_capabilities=required_capabilities,
            exclude_agent_id=exclude_agent_id,
            limit=limit,
        )
        return matches

    # LLM: 有限候选只读独立活跃投影，精确能力/排除/排序及 limit<=0 合同保持，错误不吞成健康无匹配。
    # 函数用途: 不加载整份历史名册地挑选协作候选。
    def match_agents_report(
        self,
        *,
        required_capabilities: list[str] | tuple[str, ...] | None = None,
        exclude_agent_id: str = "",
        limit: int = 20,
    ) -> tuple[list[AgentCapability], dict[str, Any] | None]:
        required = {str(item) for item in (required_capabilities or []) if str(item or "").strip()}
        try:
            return self.history_index.matching_agents(required, exclude_agent_id, limit), None
        except (OSError, UnicodeError, ValueError, sqlite3.Error) as exc:
            return [], _capability_roster_load_error(self.capabilities_path, exc)

    def _matches(self, capability: AgentCapability, required: set[str], exclude_agent_id: str) -> bool:
        if capability.agent_id == exclude_agent_id:
            return False
        if capability.status != AGENT_CAPABILITY_STATUS_AVAILABLE:
            return False
        return not required or required.issubset(set(capability.capabilities))


# ---------------------------------------------------------------------------
# collaboration cases
# ---------------------------------------------------------------------------

# LLM: 协作 case 的创建、持久化、列表缓存与成员绑定继承自能力存储基类；改动时保持 case_id/thread_id/task_id 账本契约与 test_collaboration_store.py / test_store_scan_indexes.py 一致。
# 类用途: 管理多代理协作 case 的落盘记录、状态流转与按物理文件指纹复用的读取缓存。
class CollaborationCaseStore(CollaborationCapabilityStore):
    # LLM: 新 ID 的首次规范发表也持原 case 更新锁，与后续增量写和恢复采用同一边界。
    # 函数用途: 创建一个协作 case 并发表规范文件及索引。
    def open_case(self, request: dict) -> CollaborationCase:
        current = current_time(request.get("now"))
        case = CollaborationCase(
            case_id=new_case_id(),
            thread_id=str(request.get("thread_id") or ""),
            task_id=str(request.get("task_id") or ""),
            title=str(request.get("title") or ""),
            summary=str(request.get("summary") or ""),
            priority=str(request.get("priority") or "normal"),
            created_by=str(request.get("created_by") or ""),
            required_capabilities=strings(request.get("required_capabilities")),
            entities=request.get("entities") or {},
            created_at=current,
            updated_at=current,
            metadata=request.get("metadata") or {},
        )
        with self._case_update_lock(case.case_id):
            self._write_case(case)
        if case.created_by:
            self.add_participant({
                "case_id": case.case_id,
                "agent_id": case.created_by,
                "role": "creator",
                "status": "joined",
                "now": current,
            })
        return case

    def load_case(self, case_id: str) -> CollaborationCase:
        data = read_json_file(self._case_path(case_id))
        if not data:
            raise KeyError(f"unknown collaboration case: {case_id}")
        return CollaborationCase.from_dict(data)

    def list_cases(self, *, status: str | None = None) -> list[CollaborationCase]:
        cases = [case for case in self._all_cases() if status is None or case.status == status]
        cases.sort(key=lambda item: item.created_at)
        return cases

    # LLM: 锁内重读 case 再改写，保证基于最新快照；非法状态文本保留当前状态并记协议错误元数据。只写 case JSON。
    # 函数用途: 更新协作 case 的状态与元数据，供 record_case_status 和协作工具关闭/重开 case 时调用。
    def update_case_status(
        self,
        case_id: str,
        *,
        status: str,
        now: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> CollaborationCase:
        self.load_case(case_id)
        with self._case_update_lock(case_id):
            return self._update_case_status_locked(case_id, status=status, now=now, metadata=metadata)

    # LLM: 只能在 _case_update_lock 内调用；重读 case、按协议归一状态、合并元数据后原子写回 case JSON。
    # 函数用途: update_case_status 的锁内实现，拆开是为了让加锁范围一眼可见。
    def _update_case_status_locked(
        self,
        case_id: str,
        *,
        status: str,
        now: float | None,
        metadata: dict[str, Any] | None,
    ) -> CollaborationCase:
        case = self.load_case(case_id)
        next_metadata = dict(case.metadata)
        next_metadata.pop("raw_case_status", None)
        next_metadata.pop("case_status_protocol_error", None)
        if metadata:
            next_metadata.update(metadata)
        raw_status = str(status or case.status)
        next_metadata.update(case_status_protocol_metadata(raw_status))
        updated = replace(
            case,
            status=case_status_for_update(raw_status, case.status),
            updated_at=now if now is not None else __import__("time").time(),
            metadata=next_metadata,
        )
        self._write_case(updated)
        return updated

    def record_case_status(self, request: dict) -> CollaborationCase:
        case_id = str(request.get("case_id") or "")
        status_text = str(request.get("status") or "").strip()
        if not status_text:
            raise ValueError("status_required")
        kwargs = {
            "summary": request.get("summary", ""),
            "decision_type": request.get("decision_type", ""),
            "evidence_ids": request.get("evidence_ids") or [],
            "actor_agent_id": request.get("actor_agent_id", ""),
            "metadata": request.get("metadata") or {},
            "now": request.get("now"),
        }
        case = self.load_case(case_id)
        canonical_status = case_status_for_update(status_text, case.status)
        protocol_metadata = case_status_protocol_metadata(status_text)
        kwargs["metadata"] = {**kwargs["metadata"], **protocol_metadata}
        self._validate_terminal_case_status(case_id, canonical_status, kwargs)
        updated = self.update_case_status(
            case_id, status=canonical_status, now=kwargs.get("now"), metadata=protocol_metadata,
        )
        self._append_status_decision(case_id, canonical_status, kwargs)
        return updated

    # LLM: 原glob成员/排序不变，每次仍观察文件增删；只读投影缓存不参与任何状态更新锁或业务决定。
    # 函数用途: 枚举所有协作case，复用未变化文件的独立模型快照。
    def _all_cases(self) -> list[CollaborationCase]:
        rows: list[CollaborationCase] = []
        if not self.cases_dir.exists():
            return rows
        for path in sorted(self.cases_dir.glob("*.json")):
            case = _read_cached_case(path)
            if case is not None:
                rows.append(case)
        return rows

    def _validate_terminal_case_status(self, case_id: str, status_text: str, kwargs: dict[str, Any]) -> None:
        summary = str(kwargs.get("summary") or "").strip()
        decision_type = str(kwargs.get("decision_type") or "").strip()
        if is_terminal_status(status_text) and not (summary or decision_type or self.case_decisions(case_id)):
            raise ValueError("summary_or_decision_required")

    def _append_status_decision(self, case_id: str, status_text: str, kwargs: dict[str, Any]) -> None:
        summary = str(kwargs.get("summary") or "").strip()
        decision_type = str(kwargs.get("decision_type") or "").strip()
        if not (summary or decision_type):
            return
        self.append_decision(
            CaseDecision(
                decision_id=new_decision_id(),
                case_id=case_id,
                decision_type=decision_type or "case_status_update",
                summary=summary,
                evidence_ids=strings(kwargs.get("evidence_ids")),
                created_at=current_time(kwargs.get("now")),
                metadata={
                    "actor_agent_id": str(kwargs.get("actor_agent_id") or ""),
                    "status": status_text,
                    **(kwargs.get("metadata") or {}),
                },
            )
        )


# ---------------------------------------------------------------------------
# collaboration requests
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _CoveredStatusRequest:
    request: CollaborationRequest
    targets: tuple[str, ...]
    status_text: str
    metadata: dict[str, Any]
    kwargs: dict[str, Any]


@dataclass(frozen=True)
class _RequestTargetResolution:
    targets: tuple[str, ...]
    load_error: dict[str, Any] | None = None


class CollaborationRequestStore(CollaborationCaseStore):
    # LLM: 原 case 锁同时串行请求/证据/状态发表；先耐久 dirty，原 JSONL 成功后才更新目标路由。
    # 函数用途: 发起一条协作请求并登记进请求账。
    def request_collaboration(self, request_data: dict) -> CollaborationRequest:
        case_id = str(request_data.get("case_id") or "")
        case = self.load_case(case_id)
        required = strings(request_data.get("required_capabilities"))
        request_params = {
            "target_agent_ids": request_data.get("target_agent_ids") or [],
            "requester_agent_id": request_data.get("requester_agent_id", ""),
        }
        target_resolution = self._request_targets_report(required, request_params)
        current = current_time(request_data.get("now"))
        metadata = dict(request_data.get("metadata") or {})
        if target_resolution.load_error:
            metadata["capability_roster_load_error"] = target_resolution.load_error
        request = CollaborationRequest(
            request_id=new_request_id(),
            case_id=case.case_id,
            requester_agent_id=str(request_data.get("requester_agent_id") or ""),
            target_agent_ids=target_resolution.targets,
            required_capabilities=required,
            question=str(request_data.get("question") or ""),
            entities=request_data.get("entities") or case.entities,
            problem_statement=str(request_data.get("problem_statement") or ""),
            observed_facts=dict_items(request_data.get("observed_facts")),
            query_intent=request_data.get("query_intent") or {},
            query_hints=dict_items(request_data.get("query_hints")),
            routing_requirements=request_data.get("routing_requirements") or {},
            response_contract=request_data.get("response_contract") or {},
            context_refs=strings(request_data.get("context_refs")),
            priority=str(request_data.get("priority") or case.priority),
            deadline_at=float(request_data.get("deadline_at") or 0.0),
            created_at=current,
            updated_at=current,
            metadata=metadata,
        )
        # 私有追加：0600/0700，存量宽权限协作账下次写入即收紧；内容逐字节不变。
        with self._case_update_lock(case_id):
            self._prepare_history_write(case_id)
            self.history_index.mark_dirty(case_id, published=True)
            append_private_jsonl_records(self._requests_path(case_id), [request.to_dict()], sort_keys=True)
            self.history_index.commit_event(case_id, self._history_signature(case_id), request=request)
            self._add_request_participants(case_id, request, current)
        return request

    def case_requests(self, case_id: str) -> list[CollaborationRequest]:
        requests, _load_errors = self.case_requests_report(case_id)
        return requests

    def case_requests_report(self, case_id: str) -> tuple[list[CollaborationRequest], list[dict[str, Any]]]:
        requests: dict[str, CollaborationRequest] = {}
        report = read_jsonl_report(self._requests_path(case_id), context="collaboration.requests.read")
        for row in report.rows:
            request = CollaborationRequest.from_dict(row)
            if request.request_id:
                requests[request.request_id] = request
        return list(requests.values()), report.load_errors

    def case_request_history(self, case_id: str) -> list[CollaborationRequest]:
        requests, _load_errors = self.case_request_history_report(case_id)
        return requests

    def case_request_history_report(self, case_id: str) -> tuple[list[CollaborationRequest], list[dict[str, Any]]]:
        report = read_jsonl_report(self._requests_path(case_id), context="collaboration.requests.read")
        return [CollaborationRequest.from_dict(row) for row in report.rows], report.load_errors

    def _request_targets_report(
        self, required: tuple[str, ...], kwargs: dict[str, Any]
    ) -> _RequestTargetResolution:
        targets = strings(kwargs.get("target_agent_ids"))
        if targets:
            return _RequestTargetResolution(targets)
        matches, load_error = self.match_agents_report(
            required_capabilities=required,
            exclude_agent_id=str(kwargs.get("requester_agent_id") or ""),
            limit=20,
        )
        return _RequestTargetResolution(
            tuple(item.agent_id for item in matches),
            load_error,
        )

    def _add_request_participants(self, case_id: str, request: CollaborationRequest, current: float) -> None:
        for agent_id in request.target_agent_ids:
            self.add_participant({
                "case_id": case_id,
                "agent_id": agent_id,
                "role": "responder",
                "status": "requested",
                "requested_capabilities": request.required_capabilities,
                "now": current,
            })


class CollaborationRequestStatusStore(CollaborationRequestStore):
    # LLM: 请求状态更新 = 锁内读该请求最新一行→算新快照→追加一行；非法状态文本保留读到的当前状态。
    #   副作用：追加请求行、改派时追加参与者、追加一条状态决策。锁见 _case_update_lock。
    # 函数用途: 协作请求的完成、拒绝、阻塞、改派等状态变化都从这里写入账本。
    def update_request_status(self, request_data: dict) -> CollaborationRequest:
        case_id = str(request_data.get("case_id") or "")
        self.load_case(case_id)
        with self._case_update_lock(case_id):
            return self._update_request_status_locked(case_id, request_data)

    # LLM: 原 case 锁内保持原校验顺序；dirty 先于规范追加，最后快照与新目标路由一起发表。
    # 函数用途: update_request_status 的锁内实现，拆开是为了让加锁范围一眼可见。
    def _update_request_status_locked(self, case_id: str, request_data: dict) -> CollaborationRequest:
        request_id = str(request_data.get("request_id") or "")
        request = self._request_by_id(case_id, request_id)
        status_text = str(request_data.get("status") or "").strip()
        if not status_text:
            raise ValueError("status_required")
        kwargs = {
            "summary": request_data.get("summary", ""),
            "actor_agent_id": request_data.get("actor_agent_id", ""),
            "target_agent_ids": request_data.get("target_agent_ids") or [],
            "now": request_data.get("now"),
            "metadata": request_data.get("metadata") or {},
        }
        updated = self._updated_request_snapshot(request, status_text, kwargs)
        # 私有追加：0600/0700，存量宽权限协作账下次写入即收紧；内容逐字节不变。
        self._prepare_history_write(case_id)
        self.history_index.mark_dirty(case_id, published=True)
        append_private_jsonl_records(self._requests_path(case_id), [updated.to_dict()], sort_keys=True)
        self.history_index.commit_event(case_id, self._history_signature(case_id), request=updated)
        self._add_rerouted_participants(case_id, request, updated, kwargs)
        self._append_request_status_decision(case_id, updated, kwargs)
        return updated

    def _request_by_id(self, case_id: str, request_id: str) -> CollaborationRequest:
        request_id_text = str(request_id or "").strip()
        if not request_id_text:
            raise ValueError("request_id_required")
        requests = {item.request_id: item for item in self.case_requests(case_id)}
        request = requests.get(request_id_text)
        if request is None:
            raise KeyError(f"unknown collaboration request: {request_id_text}")
        return request

    def _updated_request_snapshot(
        self, request: CollaborationRequest, status_text: str, kwargs: dict[str, Any]
    ) -> CollaborationRequest:
        current = current_time(kwargs.get("now"))
        metadata = self._next_request_metadata(request, status_text, kwargs)
        canonical_status = request_status_for_update(status_text, request.status)
        targets = request_update_targets(
            explicit_targets=kwargs.get("target_agent_ids"),
            metadata=kwargs.get("metadata") or {},
        )
        status = self._covered_status(
            _CoveredStatusRequest(request, targets, canonical_status, metadata, kwargs)
        )
        return replace(
            request,
            target_agent_ids=targets or request.target_agent_ids,
            status=status,
            updated_at=current,
            metadata=metadata,
        )

    def _next_request_metadata(
        self, request: CollaborationRequest, status_text: str, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        metadata = {**request.metadata, **(kwargs.get("metadata") or {})}
        metadata.pop("raw_request_status", None)
        metadata.pop("request_status_protocol_error", None)
        metadata.update(request_status_protocol_metadata(status_text))
        summary = str(kwargs.get("summary") or "").strip()
        if kwargs.get("actor_agent_id"):
            metadata["last_actor_agent_id"] = str(kwargs.get("actor_agent_id") or "")
        if summary:
            metadata["status_summary"] = summary
        targets = request_update_targets(
            explicit_targets=kwargs.get("target_agent_ids"),
            metadata=kwargs.get("metadata") or {},
        )
        if targets and tuple(request.target_agent_ids) != targets:
            metadata.setdefault("original_target_agent_ids", list(request.target_agent_ids))
            metadata.update({"rerouted_from": list(request.target_agent_ids), "rerouted_to": list(targets)})
        return metadata

    def _covered_status(self, request_data: _CoveredStatusRequest) -> str:
        request = request_data.request
        status_text = request_data.status_text
        if not (is_completed_request_status(status_text) and len(request.target_agent_ids) > 1):
            return status_text
        sources = evidence_sources_by_request(
            self.case_evidence(request.case_id),
            identity_keys=self.agent_identity_keys,
        ).get(request.request_id, set())
        if request_has_required_evidence(request, sources, target_identity_keys=self.agent_identity_keys):
            return status_text
        request_data.metadata["partial_completion_by"] = str(
            request_data.kwargs.get("actor_agent_id") or ""
        )
        request_data.metadata["partial_completion_summary"] = str(
            request_data.kwargs.get("summary") or ""
        )
        return CollaborationRequestStatus.PENDING.value

    def _add_rerouted_participants(
        self, case_id: str, old: CollaborationRequest, updated: CollaborationRequest, kwargs: dict[str, Any]
    ) -> None:
        for agent_id in updated.target_agent_ids:
            if agent_id not in old.target_agent_ids:
                self.add_participant({
                    "case_id": case_id,
                    "agent_id": agent_id,
                    "role": "responder",
                    "status": "requested",
                    "requested_capabilities": old.required_capabilities,
                    "now": kwargs.get("now"),
                    "metadata": {"request_id": old.request_id, "rerouted": True},
                })

    def _append_request_status_decision(
        self, case_id: str, request: CollaborationRequest, kwargs: dict[str, Any]
    ) -> None:
        summary = (
            str(kwargs.get("summary") or "").strip()
            or f"request {request.request_id} status={request.status}"
        )
        self.append_decision(
            CaseDecision(
                decision_id=new_decision_id(),
                case_id=case_id,
                decision_type="collaboration_request_status_update",
                summary=summary,
                created_at=current_time(kwargs.get("now")),
                metadata={
                    "request_id": request.request_id,
                    "request_status": request.status,
                    "actor_agent_id": str(kwargs.get("actor_agent_id") or ""),
                    **(kwargs.get("metadata") or {}),
                },
            )
        )


class CollaborationPendingRequestStore(CollaborationRequestStatusStore):
    def pending_requests_for_agent(
        self, *, agent_id: str, agent_name: str = "", agent_role: str = "", limit: int = 10
    ) -> list[dict[str, Any]]:
        rows, _load_errors = self.pending_requests_for_agent_report(
            agent_id=agent_id, agent_name=agent_name, agent_role=agent_role, limit=limit,
        )
        return rows

    # LLM: 保持冻结签名、精确身份和排序；limit 下推有限点查，覆盖/坏账通过既有 load_errors 返回。
    # 函数用途: 返回指定代理的待回应请求，不枚举关闭历史。
    def pending_requests_for_agent_report(
        self, *, agent_id: str, agent_name: str = "", agent_role: str = "", limit: int = 10
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        identities = self.agent_identity_keys((agent_id, agent_name, agent_role))
        rows, load_errors = (
            self._pending_request_rows_for_identities_report(identities, limit=limit) if identities else ([], [])
        )
        rows.sort(key=lambda item: (str(item.get("priority") or ""), float(item.get("created_at") or 0.0)))
        return (rows if limit <= 0 else rows[:limit]), load_errors

    # LLM: 正常查询在一个复用连接内按目标分页；只对版本变化的候选 case 精确恢复，不作全树回退。
    # 函数用途: 读取热投影，复核候选规范指纹，显式报告中断提交或未完成迁移。
    def _pending_request_rows_for_identities_report(
        self, identities: set[str], *, limit: int = 0
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        try:
            recovery_errors = self._prepare_history_read()
            for _ in range(2):
                candidates, errors = self.history_index.pending_rows(identities, limit)
                changed = {row[0] for row in candidates if self._history_signature(row[0]) != row[4]}
                if not changed:
                    rows = [pending_request_row(CollaborationCase.from_dict(json.loads(row[2])),
                            CollaborationRequest.from_dict(json.loads(row[3]))) for row in candidates]
                    return rows, [*recovery_errors, *errors]
                for case_id in sorted(changed):
                    self._sync_history_case(case_id)
            raise RuntimeError("collaboration canonical candidates changed repeatedly")
        except Exception as exc:  # noqa: BLE001 索引错误不伪装健康空集，也不回退全树读取
            return [], [runtime_error_report(exc, context="collaboration.pending.index")]


# ---------------------------------------------------------------------------
# public collaboration store
# ---------------------------------------------------------------------------

class _CaseSnapshot:
    def __init__(self, store: CollaborationStore, case_id: str):
        self.case = store.load_case(case_id)
        self.requests, request_errors = store.case_requests_report(case_id)
        request_history, _request_history_errors = store.case_request_history_report(case_id)
        self.request_history_count = len(request_history)
        self.evidence, evidence_errors = store.case_evidence_report(case_id)
        self.participants, participant_errors = store.case_participants_report(case_id)
        self.decisions, decision_errors = store.case_decisions_report(case_id)
        self.load_errors = [
            *request_errors,
            *evidence_errors,
            *participant_errors,
            *decision_errors,
        ]
        self.current = now()
        self.identity_keys = store.agent_identity_keys
        self.evidence_sources_by_request = evidence_sources_by_request(
            self.evidence, identity_keys=self.identity_keys,
        )
        self.has_required_evidence = self._required_evidence_index()

    @classmethod
    def from_store(cls, store: CollaborationStore, case_id: str) -> _CaseSnapshot:
        return cls(store, case_id)

    def _required_evidence_index(self) -> dict[str, bool]:
        return {
            item.request_id: request_has_required_evidence(
                item,
                self.evidence_sources_by_request.get(item.request_id, set()),
                target_identity_keys=self.identity_keys,
            )
            for item in self.requests
            if item.request_id
        }


@dataclass(frozen=True)
class _CollectionResultInput:
    snapshot: _CaseSnapshot
    groups: dict[str, list[Any]]
    coverage: dict[str, Any]
    missing: dict[str, list[str]]
    unavailable: dict[str, list[str]]
    ready: bool


def _request_groups(snapshot: _CaseSnapshot) -> dict[str, list[Any]]:
    return {
        "pending": [item for item in snapshot.requests if _is_pending(item, snapshot)],
        "blocked": [item for item in snapshot.requests if is_blocked_request_status(item.status)],
        "timed_out": [item for item in snapshot.requests if _is_timed_out(item, snapshot)],
        "completed": [item for item in snapshot.requests if _is_completed(item, snapshot)],
        "declined": [item for item in snapshot.requests if is_declined_request_status(item.status)],
    }


def _status_payload(snapshot: _CaseSnapshot, groups: dict[str, list[Any]]) -> dict[str, Any]:
    ready = bool(
        groups["blocked"]
        or groups["timed_out"]
        or (not groups["pending"] and (groups["completed"] or snapshot.evidence))
    )
    coverage = case_response_coverage(
        snapshot.requests,
        snapshot.evidence_sources_by_request,
        target_identity_keys=snapshot.identity_keys,
    )
    missing = missing_responder_agent_ids_by_request(
        snapshot.requests,
        snapshot.evidence_sources_by_request,
        target_identity_keys=snapshot.identity_keys,
    )
    unavailable = unavailable_target_agent_ids_by_request(snapshot.requests)
    return {
        "case": snapshot.case.to_dict(),
        "case_window": _case_window(snapshot, groups, ready),
        "collection_result": _collection_result(
            _CollectionResultInput(snapshot, groups, coverage, missing, unavailable, ready)
        ),
        "request_count": len(snapshot.requests),
        "request_history_count": snapshot.request_history_count,
        "evidence_count": len(snapshot.evidence),
        "participant_count": len(snapshot.participants),
        "decision_count": len(snapshot.decisions),
        "pending_request_count": len(groups["pending"]),
        "blocked_request_count": len(groups["blocked"]),
        "timed_out_request_count": len(groups["timed_out"]),
        "completed_request_count": len(groups["completed"]),
        "declined_request_count": len(groups["declined"]),
        "missing_evidence_request_ids": _missing_evidence_request_ids(snapshot),
        "timed_out_request_ids": [item.request_id for item in groups["timed_out"] if item.request_id],
        "missing_responder_agent_ids_by_request": missing,
        "unavailable_target_agent_ids_by_request": unavailable,
        "response_coverage": coverage,
        "ready_for_main_agent": ready,
        "requires_main_agent": ready,
        "load_errors": snapshot.load_errors,
        **_bounded_case_rows(snapshot, groups),
    }


def _case_window(
    snapshot: _CaseSnapshot, groups: dict[str, list[Any]], ready: bool
) -> dict[str, Any]:
    window_status = case_window_status(snapshot.case.status)
    next_action = (
        "上报上级并带上已回/未回/不可达清单。" if ready
        else "继续等待响应，或按需要补发协作请求。"
    )
    return {
        "status": window_status,
        "is_open": window_status == "open",
        "ready_to_report": ready,
        "pending_request_count": len(groups["pending"]),
        "next_action_zh": next_action,
    }


def _collection_result(request: _CollectionResultInput) -> dict[str, Any]:
    return {
        "status": "ready_to_report" if request.ready else "collecting",
        "responded_target_count": int(request.coverage.get("responded_target_count") or 0),
        "missing_target_count": int(request.coverage.get("missing_target_count") or 0),
        "unavailable_target_count": int(request.coverage.get("unavailable_target_count") or 0),
        "evidence_count": len(request.snapshot.evidence),
        "missing_responder_agent_ids_by_request": request.missing,
        "unavailable_target_agent_ids_by_request": request.unavailable,
        "deadline_expired_request_ids": [
            item.request_id for item in request.groups["timed_out"] if item.request_id
        ],
        "next_action_zh": (
            "带部分结果继续推进；不要等所有响应者都回复。"
            if request.ready
            else "到 deadline 再收回；响应者缺席只记为未回复，不阻塞主流程。"
        ),
    }


def _bounded_case_rows(snapshot: _CaseSnapshot, groups: dict[str, list[Any]]) -> dict[str, Any]:
    return {
        "requests": [_request_row(snapshot, item) for item in snapshot.requests[-10:]],
        "pending_requests": [_request_row(snapshot, item) for item in groups["pending"][-10:]],
        "blocked_requests": [_request_row(snapshot, item) for item in groups["blocked"][-10:]],
        "timed_out_requests": [_request_row(snapshot, item) for item in groups["timed_out"][-10:]],
        "completed_requests": [_request_row(snapshot, item) for item in groups["completed"][-10:]],
        "declined_requests": [_request_row(snapshot, item) for item in groups["declined"][-10:]],
        "evidence": [item.to_dict() for item in snapshot.evidence[-10:]],
        "participants": [item.to_dict() for item in snapshot.participants[-20:]],
        "decisions": [item.to_dict() for item in snapshot.decisions[-10:]],
    }


def _request_row(snapshot: _CaseSnapshot, item: Any) -> dict[str, Any]:
    row = item.to_dict()
    row["response_status"] = request_response_status(
        item,
        now=snapshot.current,
        has_required_evidence=snapshot.has_required_evidence.get(item.request_id, False),
    )
    row["has_response"] = row["response_status"] == "responded"
    row["deadline_expired"] = _is_timed_out(item, snapshot)
    return row


def _is_pending(item: Any, snapshot: _CaseSnapshot) -> bool:
    return not (
        is_blocked_request_status(item.status)
        or _is_timed_out(item, snapshot)
        or is_declined_request_status(item.status)
        or snapshot.has_required_evidence.get(item.request_id, False)
    )


def _is_timed_out(item: Any, snapshot: _CaseSnapshot) -> bool:
    return request_is_effectively_timed_out(
        item,
        now=snapshot.current,
        has_required_evidence=snapshot.has_required_evidence.get(item.request_id, False),
    )


def _is_completed(item: Any, snapshot: _CaseSnapshot) -> bool:
    return (
        not is_blocked_request_status(item.status)
        and not is_declined_request_status(item.status)
        and snapshot.has_required_evidence.get(item.request_id, False)
    )


def _missing_evidence_request_ids(snapshot: _CaseSnapshot) -> list[str]:
    return [
        item.request_id
        for item in snapshot.requests
        if item.request_id and not snapshot.has_required_evidence.get(item.request_id, False)
    ]


def _sum_status(statuses: list[dict[str, Any]], key: str) -> int:
    return sum(int(status.get(key) or 0) for status in statuses)


def _case_window_status(status: dict[str, Any]) -> str:
    return case_window_status(case_status_text(status))


def _missing_count(statuses: list[dict[str, Any]]) -> int:
    return sum(
        len(ids)
        for ids in (status.get("missing_evidence_request_ids") for status in statuses)
        if isinstance(ids, list)
    )


def _overview_blocker(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": row["case_id"],
        "title": row["title"],
        "reason": "collaboration_ready_for_main_agent",
        "blocked_request_count": row["blocked_request_count"],
        "timed_out_request_count": row.get("timed_out_request_count", 0),
        "missing_evidence_request_count": row["missing_evidence_request_count"],
    }


def _readiness(blockers: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "ready": not blockers,
        "blocker_count": len(blockers),
        "blockers": blockers[-20:],
        "next_action": (
            "run_background_main_agent_or_review_ready_cases"
            if blockers
            else "no_collaboration_blockers"
        ),
    }


class CollaborationStore(CollaborationPendingRequestStore):
    """Public collaboration store — the single entry point."""

    def overview(self) -> dict[str, Any]:
        statuses = [self.case_status(case.case_id) for case in self.list_cases()]
        ready_cases = [
            case_overview_row(status)
            for status in statuses
            if bool(status.get("ready_for_main_agent"))
        ]
        blockers = [_overview_blocker(row) for row in ready_cases]
        return {
            "schema_version": "collaboration_overview.v1",
            "case_count": len(statuses),
            "open_case_count": sum(
                1 for status in statuses if _case_window_status(status) == "open"
            ),
            "closed_case_count": sum(
                1 for status in statuses if _case_window_status(status) == "closed"
            ),
            "ready_case_count": len(ready_cases),
            "request_count": _sum_status(statuses, "request_count"),
            "pending_request_count": _sum_status(statuses, "pending_request_count"),
            "blocked_request_count": _sum_status(statuses, "blocked_request_count"),
            "timed_out_request_count": _sum_status(statuses, "timed_out_request_count"),
            "completed_request_count": _sum_status(statuses, "completed_request_count"),
            "declined_request_count": _sum_status(statuses, "declined_request_count"),
            "missing_evidence_request_count": _missing_count(statuses),
            "evidence_count": _sum_status(statuses, "evidence_count"),
            "participant_count": _sum_status(statuses, "participant_count"),
            "decision_count": _sum_status(statuses, "decision_count"),
            "ready_cases": ready_cases[-20:],
            "readiness": _readiness(blockers),
        }

    def case_status(self, case_id: str) -> dict[str, Any]:
        snapshot = _CaseSnapshot.from_store(self, case_id)
        groups = _request_groups(snapshot)
        return _status_payload(snapshot, groups)

    # LLM: 按 case 获取交接板独占文件锁，串行化读-核验-追加临界区。
    # 函数用途: 返回针对单个 case 交接项的上下文锁。
    @contextmanager
    def handoff_case_lock(self, case_id: str):
        if not case_id or not isinstance(case_id, str) or "/" in case_id or "\\" in case_id or ".." in case_id:
            raise ValueError("invalid collaboration case_id")
        clean_case_id = validate_opaque_id(case_id, kind="case_id")
        ensure_private_dir(self.handoff_items_dir)
        lock_path = self.handoff_items_dir / f"{clean_case_id}.lock"
        with locked_json_path(lock_path):
            yield

    # LLM: 维护活跃（含非 closed 状态）case 索引集合 .active_cases.json。
    # 函数用途: 在文件锁保护下原子更新活跃 case 索引表。
    def _update_active_cases_index(self, case_id: str, *, is_active: bool) -> None:
        ensure_private_dir(self.handoff_items_dir)
        idx_path = self.handoff_items_dir / ".active_cases.json"
        with locked_json_path(idx_path):
            active_set: set[str] = set()
            if idx_path.exists():
                try:
                    data = json.loads(idx_path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        active_set = set(data.get("active_cases", []))
                    elif isinstance(data, list):
                        active_set = set(data)
                except Exception:
                    active_set = set()
            else:
                for p in self.handoff_items_dir.glob("*.jsonl"):
                    cid = p.stem
                    items = self.case_handoff_items(cid)
                    if any(it.state != "closed" for it in items):
                        active_set.add(cid)
            if is_active:
                active_set.add(case_id)
            else:
                active_set.discard(case_id)
            write_json_file_atomic_unlocked(idx_path, {"active_cases": sorted(active_set)})

    # LLM: 交接项状态变更是只追加 JSONL（同 item_id 取最后一行）；写入由 locked_json_path / append_private_jsonl_records 串行。
    # 函数用途: 将交接项的最新状态快照追加到所属 case 的交接账本并更新活跃索引。
    def append_handoff_item(self, item: HandoffBoardItem) -> None:
        ensure_private_dir(self.handoff_items_dir)
        path = self._handoff_items_path(item.case_id)
        append_private_jsonl_records(path, [item.to_dict()], sort_keys=True)
        if item.state != "closed":
            self._update_active_cases_index(item.case_id, is_active=True)
        else:
            items = self.case_handoff_items(item.case_id)
            has_active = any(it.state != "closed" for it in items)
            self._update_active_cases_index(item.case_id, is_active=has_active)

    # LLM: 原子重写指定 case 的交接项账本（归档清理时只保留非 closed 项，空则删除文件）。
    # 函数用途: 重写 case 交接账本并同步更新活跃索引。
    def rewrite_case_handoff_items(self, case_id: str, items: list[HandoffBoardItem]) -> None:
        path = self._handoff_items_path(case_id)
        if not items:
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass
        else:
            ensure_private_dir(self.handoff_items_dir)
            records = [item.to_dict() for item in items]
            write_private_jsonl_records(path, records, sort_keys=True)
        has_active = any(it.state != "closed" for it in items)
        self._update_active_cases_index(case_id, is_active=has_active)

    # LLM: 读 case 的交接项快照；同 item_id 只保留最后一条，保持插入次序。
    # 函数用途: 获取指定 case 下所有交接项的最新状态列表。
    def case_handoff_items(self, case_id: str) -> list[HandoffBoardItem]:
        items, _ = self.case_handoff_items_report(case_id)
        return items

    # LLM: 读交接项 JSONL 账本并返回解析结果与错误报告。
    # 函数用途: 返回交接项实体列表与格式异常报告。
    def case_handoff_items_report(
        self,
        case_id: str,
    ) -> tuple[list[HandoffBoardItem], list[dict[str, Any]]]:
        items: dict[str, HandoffBoardItem] = {}
        path = self._handoff_items_path(case_id)
        report = read_jsonl_report(path, context="collaboration.handoff_items.read")
        for row in report.rows:
            item = HandoffBoardItem.from_dict(row)
            if item.item_id:
                items[item.item_id] = item
        return list(items.values()), report.load_errors

    # LLM: 按 case_id 与 item_id 查询最新交接项实体。
    # 函数用途: 返回单条交接项的最新快照或 None。
    def load_handoff_item(self, case_id: str, item_id: str) -> HandoffBoardItem | None:
        items = self.case_handoff_items(case_id)
        for item in items:
            if item.item_id == item_id:
                return item
        return None

    # LLM: 遍历所有 case 的交接项，支持按 state 和 responsible_actor 过滤及早期上限截止。
    # 函数用途: 跨 case 检索交接项列表，非 closed 状态优先使用活跃索引。
    def list_all_handoff_items(
        self,
        *,
        state: str | None = None,
        responsible_actor: str | None = None,
        limit: int | None = None,
    ) -> list[HandoffBoardItem]:
        results: list[HandoffBoardItem] = []
        if not self.handoff_items_dir.exists():
            return results

        idx_path = self.handoff_items_dir / ".active_cases.json"
        is_non_closed_query = state is not None and state != "closed"

        case_ids: list[str] | None = None
        if is_non_closed_query and idx_path.exists():
            try:
                data = json.loads(idx_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    case_ids = sorted(data.get("active_cases", []))
                elif isinstance(data, list):
                    case_ids = sorted(data)
            except Exception:
                case_ids = None

        if case_ids is not None:
            for case_id in case_ids:
                items = self.case_handoff_items(case_id)
                for item in items:
                    if state is not None and item.state != state:
                        continue
                    if responsible_actor is not None and item.responsible_actor != responsible_actor:
                        continue
                    results.append(item)
                    if limit is not None and len(results) >= limit:
                        return results
            return results

        for path in sorted(self.handoff_items_dir.glob("*.jsonl")):
            case_id = path.stem
            items = self.case_handoff_items(case_id)
            for item in items:
                if state is not None and item.state != state:
                    continue
                if responsible_actor is not None and item.responsible_actor != responsible_actor:
                    continue
                results.append(item)
                if limit is not None and len(results) >= limit:
                    return results
        return results
