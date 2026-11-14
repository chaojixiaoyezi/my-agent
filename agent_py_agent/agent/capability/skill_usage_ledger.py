# LLM: 使用台账只保存身份与INT64计数，写失败只告警；无历史起点证明时读取不得补造lifetime完整性。
# 模块用途: 统一记录 owner 的人工检索、读取、宿主注入与采用事实。
from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Sequence
from dataclasses import MISSING, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..common.nofollow_fs import (
    open_directory_beneath,
    read_text_beneath,
    unlink_file_beneath,
    write_text_atomic_beneath,
)
from ..local_storage.owner_read_index import (
    _INT64_MAX,
    USAGE_EVENTS,
    OwnerReadIndex,
    OwnerReadUnavailable,
    require_complete,
)
from ..user_space.owner_quota import OwnerQuotaChange
from .skill_learning_store import SkillLearningStore, SkillLearningStoreError
from .skill_usage_receipts import (
    canonical_task_key,
    read_usage_aggregate,
    record_exact_use,
)

MAX_USAGE_LEDGER_EVENTS = 2000
KEEP_USAGE_LEDGER_EVENTS = 1000
# 中文说明：单条使用事件 delta 返回数组的协议最大长度（条）。
MAX_USAGE_DELTA_ROWS_COUNT = 2000
_REAL_USE_EVENTS = frozenset({"manual_get", "host_injected"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTEXT_DEFAULTS = {
    "request_id": "",
    "thread_id": "",
    "task_id": "",
    "chars": 0,
    "body_chars": MISSING,
    "content_sha256": "",
    "task_key": "",
    "incarnation_id": "",
    "activation_id": "",
    "strike_epoch": MISSING,
}


# LLM: Store传params，旧调用方的显式关键字覆盖同名项；归一后复用批量口校验/事务，记录异常只warning。
# 函数用途: 记录一次使用事实，并在真实 learned 技能正文投递时同步写入精确回执与再激活。
def record_skill_usage_event(
    agent_or_store, *, params: dict | None = None,
    event=MISSING, skill_id=MISSING, source=MISSING, kind=MISSING,
    request_id=MISSING, thread_id=MISSING, task_id=MISSING, chars=MISSING,
    body_chars=MISSING, content_sha256=MISSING, task_key=MISSING,
    incarnation_id=MISSING, activation_id=MISSING, strike_epoch=MISSING,
) -> None:
    try:
        values = dict(params or {})
        overrides = dict(
            event=event, skill_id=skill_id, source=source, kind=kind,
            request_id=request_id, thread_id=thread_id, task_id=task_id, chars=chars,
            body_chars=body_chars, content_sha256=content_sha256, task_key=task_key,
            incarnation_id=incarnation_id, activation_id=activation_id, strike_epoch=strike_epoch,
        )
        values.update({key: value for key, value in overrides.items() if value is not MISSING})
        row = _usage_row(agent_or_store, values)
        store = _usage_store(agent_or_store)
        _record_usage_rows(store, [row])
    except Exception:  # noqa: BLE001 - 附带统计不能改变原工具/宿主的成功或失败。
        logging.getLogger(__name__).warning("SKILL_USAGE_RECORD_UNAVAILABLE")


# LLM: 单条与整批复用同一事件字典校验，再拿owner配额→state锁；空批不创建目录，故障只warning且不写半批。
# 函数用途: 为一次搜索的全部命中只追加并保存一次使用台账。
def record_skill_usage_events(store: SkillLearningStore, events: Sequence[dict[str, object]]) -> None:
    try:
        rows = [_usage_row(store, event) for event in events]
        if rows:
            _record_usage_rows(_usage_store(store), rows)
    except Exception:  # noqa: BLE001 - 附带批量统计不能改变原工具结果。
        logging.getLogger(__name__).warning("SKILL_USAGE_RECORD_UNAVAILABLE")


# LLM: 仅接收本地已校验事件；同一锁内按 quota->state->runtime 顺序持久化 JSONL、SQL 精确回执与同步再激活。
# 函数用途: 让单条与批量记账共用同一个持久事务。
def _record_usage_rows(store, events: list[dict]) -> None:
    _check_usage_directory(store)
    with store.state_lock() as admission:
        rows = _ledger_rows(store)
        summary = _read_summary(store, rows)
        # 去重只覆盖原有界明细窗口，不另建终身增长索引；重复投递连文件也不重写。
        seen = {row['source_key'] for row in rows if row.get('source_key')}
        added = False
        for row in events:
            key = row.get('source_key')
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            added = True
            row["sequence"] = summary["last_sequence"] + 1
            _add_event(summary, row)
            rows.append(row)
            if len(rows) > MAX_USAGE_LEDGER_EVENTS:
                rows = rows[-KEEP_USAGE_LEDGER_EVENTS:]
        if added:
            _save_usage(store, admission, rows, summary)

        # 阶段去重不吞真实正文投递；原精确回执按任务/版本幂等。
        _record_exact_and_reheat_locked(store, events)

# LLM: 生命周期权威仍是原summary；未知不能转成空计数，原writer不依赖此读取口。
# 函数用途: 保持完整成功时的原字典，失败时抛带coverage的异常。
def load_skill_usage_summary(store_or_home) -> dict:
    report = load_skill_usage_summary_report(store_or_home)
    if report['coverage']['status'] != 'complete':
        logging.getLogger(__name__).warning("SKILL_USAGE_SUMMARY_UNAVAILABLE")
    return require_complete(report)['summary']


# LLM: 只绑定Store原owner根，不从ledger父目录猜归属；构造不落盘。
# 函数用途: 为同一Store复用纯派生读索引。
def _usage_read_index(store):
    index = getattr(store, '_usage_read_index', None)
    if index is None:
        index = OwnerReadIndex(store.skills_root.parent)
        store._usage_read_index = index
    return index


# LLM: 明细不超过原writer保留上限，不扩大预算或重建丢失历史。
# 函数用途: 固定view分页取得供原replay校验的有界最近明细。
def _indexed_usage_rows(index, view):
    first = require_complete(index.query(view, limit=1000))
    total = first['total_count']
    if total > MAX_USAGE_LEDGER_EVENTS:
        raise OwnerReadUnavailable({'status': 'partial', 'reason': 'usage_detail_budget',
                                    'source': first['coverage']})
    if total == 0 and view.get('through', 0) > 0:
        raise OwnerReadUnavailable({'status': 'unavailable', 'reason': 'missing_usage_projection',
                                    'source': first['coverage']})
    rows = first['rows']
    while len(rows) < total:
        page = require_complete(index.query(view, limit=min(1000, total - len(rows)), offset=len(rows)))
        if not page['rows']:
            raise OwnerReadUnavailable({'status': 'unavailable', 'reason': 'missing_usage_projection',
                                        'source': page['coverage']})
        rows.extend(page['rows'])
    return rows


# LLM: summary为lifetime基线；无summary的无序号legacy明细不能证明起点，保持partial且不补号反证完整或反写事实。
# 函数用途: 返回使用汇总及独立的来源、生命周期和明细覆盖状态。
def load_skill_usage_summary_report(store_or_home) -> dict:
    sources = {}
    try:
        store = _usage_store(store_or_home)
        index = _usage_read_index(store)
        parts = store.directory.relative_to(store.skills_root.parent).parts
        summary_view = index.advance('usage', (*parts, 'usage_summary.json'))
        sources['summary'] = summary_view
        projected = index.query(summary_view, limit=1)
        sources['summary'] = projected['coverage']
        require_complete(projected)
        present = bool(projected['rows'])
        if not present and projected['coverage'].get('reason') != 'source_absent':
            raise ValueError('missing summary projection')
        summary = projected['rows'][0] if present else _empty_summary()
        detail_view = index.advance('usage', (*parts, 'usage_ledger.jsonl'))
        sources['ledger'] = detail_view
        rows = _indexed_usage_rows(index, detail_view)
        if not present and any('sequence' not in row for row in rows):
            raise OwnerReadUnavailable({
                'status': 'partial', 'reason': 'legacy_history_start_unknown',
                'scope': 'lifetime_summary',
                'detail_history': {'status': 'unknown', 'reason': 'legacy_history_start_unknown'},
            })
        numbered = next(((position, row['sequence']) for position, row in enumerate(rows)
                         if 'sequence' in row), None)
        if not present and numbered is not None and numbered[1] != numbered[0] + 1:
            raise OwnerReadUnavailable({'status': 'unavailable', 'reason': 'missing_lifetime_summary'})
        if (not isinstance(summary, dict) or not isinstance(summary.get('totals'), dict)
                or not isinstance(summary.get('by_skill'), dict)
                or not isinstance(summary.get('daily_history'), dict)):
            raise ValueError('invalid usage summary')
        _summary_sequence(summary)
        _normalize_by_skill(summary)
        _replay_usage_rows(summary, rows)
        _prune_daily(summary)
        # 两来源均在全部处理结束后重新校验，替换或删除不能被早先读取掩盖。
        for name, view in (('summary', summary_view), ('ledger', detail_view)):
            check = index.query(view, limit=0)
            sources[name] = check['coverage']
            require_complete(check)
        sequences = [row['sequence'] for row in rows]
        complete_history = ((not sequences and summary['last_sequence'] == 0)
                            or bool(sequences) and sequences[0] == 1
                            and sequences[-1] == summary['last_sequence']
                            and all(right == left + 1 for left, right in zip(sequences, sequences[1:])))
        return {'summary': summary, 'coverage': {
            'status': 'complete', 'reason': 'summary_and_retained_replay',
            'scope': 'lifetime_summary', 'sources': sources,
            'detail_history': {'status': 'complete' if complete_history else 'partial',
                               'reason': 'covered' if complete_history else 'retained_or_missing_detail'},
        }}
    except OwnerReadUnavailable as error:
        return {'summary': None, 'coverage': {
            'status': error.coverage.get('status', 'unavailable'),
            'reason': error.coverage.get('reason', 'usage_read_unavailable'),
            'failure': error.coverage, 'sources': sources,
            **{key: error.coverage[key] for key in ('scope', 'detail_history') if key in error.coverage},
        }}
    except Exception as error:  # noqa: BLE001 - 读取保留未知，不修复规范来源。
        return {'summary': None, 'coverage': {
            'status': 'unavailable',
            'reason': 'io_error' if isinstance(error, OSError) else 'invalid_usage_state',
            'sources': sources,
        }}


# LLM: Agent 优先取当前 owner 的 home_paths 并保留 runtime_db 绑定；不能用 cwd、全局 home 决定归属。
# 函数用途: 兼容真实 Agent、Store、HomePaths 和 owner_home 路径，不创建新存储权威。
def _usage_store(value) -> SkillLearningStore:
    repo = getattr(value, "runtime_db", None) or getattr(getattr(value, "subagents", None), "runtime_db", None)
    attached = getattr(value, "skill_learning_store", None)
    if isinstance(value, SkillLearningStore):
        return value
    provider = (lambda r=repo: r) if repo is not None else None
    if isinstance(attached, SkillLearningStore):
        if provider is None or getattr(attached, "_usage_runtime_provider", None) is not None:
            return attached
        return SkillLearningStore(attached.directory, attached.skills_root, usage_runtime_provider=provider)
    home = getattr(value, "home_paths", value)
    if hasattr(home, "owner_skill_learning_dir") and hasattr(home, "owner_home_dir"):
        return SkillLearningStore.for_home(home, usage_runtime_provider=provider)
    root = Path(getattr(home, "owner_home_dir", home))
    return SkillLearningStore(root / "data" / "skill_learning", root / "skills", usage_runtime_provider=provider)


def _is_real_body_delivery_row(row: dict) -> bool:
    if row.get("kind") == "capability_package":
        return False
    if row.get("event") not in _REAL_USE_EVENTS:
        return False
    if "body_chars" in row:
        return isinstance(row["body_chars"], int) and row["body_chars"] > 0
    return isinstance(row.get("chars"), int) and row["chars"] > 0


def _iso_to_timestamp(value: object) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if value > 0 else 0.0
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip())
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, parsed.timestamp())
        except ValueError:
            return 0.0
    return 0.0


def _learned_skill_name(kind: str, skill_id: str) -> str:
    if kind == "capability_package" or skill_id.startswith("capability:"):
        return ""
    cleaned = skill_id.removeprefix("skill:")
    if cleaned.startswith("builtin:"):
        return ""
    if cleaned.startswith("owner:"):
        return cleaned.removeprefix("owner:")
    return cleaned


def learning_usage_binding(agent: object, entry: object) -> dict[str, object]:
    """从快照条目或受管登记表提取版本绑定字段，供人工读取与会话带回使用回执对齐。"""
    result: dict[str, object] = {}
    sha = str(getattr(entry, "content_sha256", "") or "").strip().lower()
    if _SHA256_RE.fullmatch(sha):
        result["content_sha256"] = sha
    asset_fields = getattr(entry, "asset_fields", None)
    binding = asset_fields.get("_learning_usage_binding") if isinstance(asset_fields, dict) else None
    if not isinstance(binding, dict):
        metadata = getattr(entry, "metadata", None)
        binding = metadata.get("_learning_usage_binding") if isinstance(metadata, dict) else None
    if isinstance(binding, dict):
        inc = str(binding.get("incarnation_id") or "").strip()
        act = str(binding.get("activation_id") or "").strip()
        epoch = binding.get("strike_epoch")
        binding_sha = str(binding.get("content_sha256") or "").strip().lower()
        if binding_sha and not _SHA256_RE.fullmatch(binding_sha):
            binding_sha = ""
        if inc and act and (not sha or not binding_sha or binding_sha == sha):
            if not sha and binding_sha:
                result["content_sha256"] = binding_sha
            result["incarnation_id"] = inc
            result["activation_id"] = act
            if type(epoch) is int and epoch >= 0:
                result["strike_epoch"] = epoch
            return result
    # 历史版本恢复会清空已冻结条目的私有绑定；缺失绑定时保持不可用，不重新绑定到当前激活态。
    if getattr(entry, "frozen_path", "") and isinstance(asset_fields, dict):
        return result
    source = str(getattr(entry, "source", "") or "")
    stable_id = str(getattr(entry, "stable_id", "") or "")
    name = str(getattr(entry, "name", "") or "").strip()
    if not name or (source != "user" and not stable_id.startswith("owner:")):
        return result
    home = getattr(agent, "home_paths", None)
    if home is None or not hasattr(home, "owner_skill_learning_dir"):
        return result
    try:
        store = _usage_store(agent)
        if not store.registry_path.exists():
            return result
        record = store.load_registry().skills.get(name)
        if record is None or record.layout == "owner":
            return result
        if sha and str(record.sha256 or "").lower() != sha:
            return result
        inc = str(record.maintenance.get("incarnation_id") or "").strip()
        act = str(record.maintenance.get("activation_id") or "").strip()
        if inc and act:
            result["incarnation_id"] = inc
            result["activation_id"] = act
            epoch = record.maintenance.get("strike_epoch")
            if type(epoch) is int and epoch >= 0:
                result["strike_epoch"] = epoch
            if not result.get("content_sha256") and _SHA256_RE.fullmatch(str(record.sha256 or "").lower()):
                result["content_sha256"] = str(record.sha256).lower()
    except Exception:  # noqa: BLE001 - 统计绑定提取失败不阻塞业务工具返回
        return result
    return result


def _record_exact_and_reheat_locked(store: SkillLearningStore, events: list[dict]) -> None:
    real_events = [row for row in events if _is_real_body_delivery_row(row)]
    if not real_events:
        return

    runtime_repo = store._runtime_repository()
    exact_aggregates: dict[tuple[str, str, str], dict] = {}
    if runtime_repo is not None:
        for row in real_events:
            if (
                isinstance(row.get("body_chars"), int)
                and row["body_chars"] > 0
                and row.get("incarnation_id")
                and row.get("activation_id")
                and _SHA256_RE.fullmatch(str(row.get("content_sha256") or ""))
                and row.get("task_key")
            ):
                receipt_event = {**row, "at": _iso_to_timestamp(row["at"])}
                result = record_exact_use(store, receipt_event, runtime=runtime_repo)
                agg = result.get("aggregate")
                if isinstance(agg, dict):
                    key = (
                        str(agg.get("incarnation_id") or ""),
                        str(agg.get("activation_id") or ""),
                        str(agg.get("content_sha256") or ""),
                    )
                    exact_aggregates[key] = agg

    if not store.registry_path.exists():
        return
    registry = store.load_registry()
    dirty = False
    for row in real_events:
        name = _learned_skill_name(str(row.get("kind") or "skill"), str(row.get("skill_id") or ""))
        if not name:
            continue
        current = registry.skills.get(name)
        if current is None or current.layout == "owner" or name in registry.blocked_names:
            continue
        if current.maintenance.get("ownership") == "user_modified":
            continue
        ts = _iso_to_timestamp(row["at"]) or datetime.now(timezone.utc).timestamp()
        state = str(current.maintenance.get("state") or "trial")
        if state == "archived":
            try:
                registry, current = store._transition_restore_locked(registry, name, now=ts)
            except (SkillLearningStoreError, OSError, ValueError):
                continue
        maint = dict(current.maintenance)
        updated_here = False
        if maint.get("state") == "cooled":
            maint["state"] = "active" if maint.get("quality") == "established" else "trial"
            maint["restored_at"] = ts
            maint.pop("cooled_at", None)
            updated_here = True

        inc = str(maint.get("incarnation_id") or "")
        act = str(maint.get("activation_id") or "")
        sha = str(current.sha256 or "").lower()
        row_inc = str(row.get("incarnation_id") or "")
        row_act = str(row.get("activation_id") or "")
        row_sha = str(row.get("content_sha256") or "").lower()
        version_matches = (not row_inc and not row_act) or (row_inc == inc and row_act == act and (not row_sha or row_sha == sha))
        if version_matches:
            prev_last = float(maint.get("last_used_at") or 0.0)
            if ts > prev_last:
                maint["last_used_at"] = ts
                updated_here = True
            agg = exact_aggregates.get((inc, act, sha))
            if agg is not None:
                ind_uses = int(agg["independent_uses"])
                uses_hurt = int(agg["uses_since_hurt"])
                hurt_cnt = int(agg["hurt_count"])
                maint["independent_uses"] = max(int(maint.get("independent_uses") or 0), ind_uses)
                maint["uses_since_hurt"] = uses_hurt
                maint["hurt_count"] = hurt_cnt
                maint["strike_epoch"] = int(agg["strike_epoch"])
                maint["epoch_started_at"] = float(agg["epoch_started_at"])
                maint["usage_receipt_sequence"] = max(int(maint.get("usage_receipt_sequence") or 0), int(agg["sequence"]))
                if ind_uses > 0:
                    maint["ever_used"] = True
                maint["independent_uses_coverage"] = str(agg["coverage"])
                if hurt_cnt < 3 and uses_hurt >= 3 + hurt_cnt and maint.get("state") in {"trial", "active"} and maint.get("quality") != "established":
                    maint["quality"] = "established"
                    maint["quality_basis"] = "uses_threshold"
                    maint["established_at"] = float(maint.get("established_at") or ts)
                    if maint.get("state") == "trial":
                        maint["promotion_history"] = [*maint.get("promotion_history", []), "active"][-100:]
                    maint["state"] = "active"
                updated_here = True
            elif row.get("body_chars") is not None and int(row.get("body_chars") or 0) > 0:
                maint["ever_used"] = True
                updated_here = True
        if updated_here:
            registry = replace(
                registry,
                skills={**registry.skills, name: replace(current, maintenance=maint)},
            )
            dirty = True
    if dirty:
        store.save_registry(registry)


def load_usage_evidence(store_or_home, *, after_sequence: int = 0) -> dict[str, object]:
    """聚合学习使用账本、索引汇总与 SQLite 版本绑定精确回执，返回 UsageEvidence 契约字典。"""
    if type(after_sequence) is not int or after_sequence < 0:
        raise ValueError("after_sequence must be a non-negative integer")
    store = _usage_store(store_or_home)
    warnings: list[str] = []

    def _warn(code: str) -> None:
        if code not in warnings:
            warnings.append(code)

    registry = None
    managed: dict = {}
    if store.registry_path.exists():
        try:
            registry = store.load_registry()
            try:
                managed = store.managed_records(registry, include_archived=True)
            except SkillLearningStoreError:
                _warn("registry_partial")
                for skill_name, rec in sorted(registry.skills.items()):
                    if rec.layout == "owner" or rec.maintenance.get("ownership") in {"owner_layout", "user_modified", "unknown"}:
                        continue
                    managed[skill_name] = rec
        except Exception:  # noqa: BLE001 - 注册表不可读时保持保守降级，不中断读路径
            _warn("registry_unavailable")

    summary: dict | None = None
    summary_present = False
    rows: list[dict] = []
    lifetime_status = "unavailable"
    delta_status = "unavailable"
    through = 0
    detail_complete = False

    try:
        index = _usage_read_index(store)
        parts = store.directory.relative_to(store.skills_root.parent).parts
        summary_view = index.advance("usage", (*parts, "usage_summary.json"))
        projected = index.query(summary_view, limit=1)
        require_complete(projected)
        summary_present = bool(projected["rows"])
        if not summary_present and projected["coverage"].get("reason") != "source_absent":
            raise ValueError("missing summary projection")
        summary = projected["rows"][0] if summary_present else _empty_summary()
        detail_view = index.advance("usage", (*parts, "usage_ledger.jsonl"))
        rows = _indexed_usage_rows(index, detail_view)
        if not summary_present and any("sequence" not in row for row in rows):
            raise OwnerReadUnavailable({"status": "partial", "reason": "legacy_history_start_unknown"})
        numbered = next(((idx, row["sequence"]) for idx, row in enumerate(rows) if "sequence" in row), None)
        if not summary_present and numbered is not None and numbered[1] != numbered[0] + 1:
            raise OwnerReadUnavailable({"status": "unavailable", "reason": "missing_lifetime_summary"})
        if (
            not isinstance(summary, dict)
            or not isinstance(summary.get("totals"), dict)
            or not isinstance(summary.get("by_skill"), dict)
            or not isinstance(summary.get("daily_history"), dict)
        ):
            raise ValueError("invalid usage summary")
        _summary_sequence(summary)
        _normalize_by_skill(summary)
        _replay_usage_rows(summary, rows)
        _prune_daily(summary)
        for view in (summary_view, detail_view):
            check = index.query(view, limit=0)
            require_complete(check)
        through = int(summary["last_sequence"])
        lifetime_status = "complete"
        sequences = [int(row["sequence"]) for row in rows if "sequence" in row]
        detail_complete = (
            (not sequences and through == 0)
            or (
                bool(sequences)
                and sequences[0] == 1
                and sequences[-1] == through
                and all(right == left + 1 for left, right in zip(sequences, sequences[1:]))
            )
        )
        if (
            detail_complete
            or through == 0
            or (sequences and sequences[0] <= after_sequence + 1)
            or (after_sequence >= through > 0)
        ):
            delta_status = "complete"
        else:
            delta_status = "partial"
            _warn("usage_ledger_trimmed")
    except OwnerReadUnavailable as err:
        cov_status = err.coverage.get("status", "unavailable")
        lifetime_status = "partial" if cov_status == "partial" else "unavailable"
        delta_status = "partial" if cov_status == "partial" else "unavailable"
        _warn(str(err.coverage.get("reason") or "usage_read_unavailable"))
        summary = None
        rows = []
    except Exception:  # noqa: BLE001 - 使用统计不可用时保留保守未知状态
        lifetime_status = "unavailable"
        delta_status = "unavailable"
        _warn("usage_read_unavailable")
        summary = None
        rows = []

    runtime_repo = None
    distinct_status = "unavailable"
    try:
        runtime_repo = store._runtime_repository()
    except Exception:  # noqa: BLE001
        runtime_repo = None
        _warn("runtime_repository_unavailable")

    aggregates_by_triple: dict[tuple[str, str, str], dict] = {}
    if runtime_repo is None:
        _warn("runtime_repository_unavailable")
    else:
        try:
            with runtime_repo.transaction() as _conn:
                _conn.execute("SELECT 1").fetchone()
            for _name, rec in managed.items():
                inc = str(rec.maintenance.get("incarnation_id") or "")
                act = str(rec.maintenance.get("activation_id") or "")
                sha = str(rec.sha256 or "").lower()
                if inc and act and _SHA256_RE.fullmatch(sha):
                    agg = read_usage_aggregate(
                        runtime_repo,
                        incarnation_id=inc,
                        activation_id=act,
                        content_sha256=sha,
                    )
                    if agg is not None:
                        aggregates_by_triple[(inc, act, sha)] = agg
            distinct_status = "complete"
        except Exception:  # noqa: BLE001
            distinct_status = "unavailable"
            _warn("usage_receipts_unavailable")

    # 按 learned 技能聚合 JSONL 行，严格剔除能力包事件与 builtin 技能。
    skill_names: set[str] = set(managed.keys())
    rows_by_skill: dict[str, list[dict]] = {}
    summary_by_skill: dict[str, dict] = {}
    legacy_skills: set[str] = set()
    has_missing_version_binding = any(
        not rec.maintenance.get("incarnation_id") or not rec.maintenance.get("activation_id")
        for rec in managed.values()
    )

    if summary is not None:
        for raw_skill_id, counts in summary["by_skill"].items():
            if not isinstance(counts, dict) or counts.get("kind") == "capability_package":
                continue
            name = _learned_skill_name(str(counts.get("kind") or "skill"), str(raw_skill_id))
            if not name:
                continue
            skill_names.add(name)
            existing_counts = summary_by_skill.get(name)
            if existing_counts is None:
                summary_by_skill[name] = dict(counts)
            else:
                for ev in USAGE_EVENTS:
                    existing_counts[ev] = int(existing_counts.get(ev, 0)) + int(counts.get(ev, 0))
                existing_counts["effective_uses"] = (
                    int(existing_counts.get("manual_get", 0))
                    + int(existing_counts.get("host_injected", 0))
                )
                if str(counts.get("last_used_at") or "") > str(existing_counts.get("last_used_at") or ""):
                    existing_counts["last_used_at"] = counts.get("last_used_at", "")

    for row in rows:
        if row.get("kind") == "capability_package":
            continue
        name = _learned_skill_name(str(row.get("kind") or "skill"), str(row.get("skill_id") or ""))
        if not name:
            continue
        skill_names.add(name)
        rows_by_skill.setdefault(name, []).append(row)
        if _is_real_body_delivery_row(row):
            if (
                not row.get("incarnation_id")
                or not row.get("activation_id")
                or not _SHA256_RE.fullmatch(str(row.get("content_sha256") or ""))
            ):
                legacy_skills.add(name)

    by_skill_out: dict[str, dict] = {}
    for name in sorted(skill_names):
        rec = managed.get(name)
        maint = dict(rec.maintenance) if rec is not None else {}
        inc = str(maint.get("incarnation_id") or "")
        act = str(maint.get("activation_id") or "")
        sha = str(rec.sha256 or "").lower() if rec is not None else ""
        agg = aggregates_by_triple.get((inc, act, sha)) if (inc and act and sha) else None

        skill_rows = rows_by_skill.get(name, [])
        real_rows = [row for row in skill_rows if _is_real_body_delivery_row(row)]
        zero_body_real_events = sum(
            1
            for row in skill_rows
            if row.get("event") in _REAL_USE_EVENTS
            and "body_chars" in row
            and int(row.get("body_chars") or 0) == 0
        )

        sum_counts = summary_by_skill.get(name, {})
        sum_effective = int(sum_counts.get("manual_get", 0)) + int(sum_counts.get("host_injected", 0))
        if detail_complete:
            lifetime_events = len(real_rows)
        else:
            lifetime_events = max(len(real_rows), max(0, sum_effective - zero_body_real_events))

        real_row_ts = max((_iso_to_timestamp(r.get("at")) for r in real_rows), default=0.0)
        agg_ts = float(agg["last_real_used_at"]) if agg is not None else 0.0
        last_real_used_at = max(real_row_ts, agg_ts) or None

        sum_ts = _iso_to_timestamp(sum_counts.get("last_used_at")) if sum_effective > 0 else 0.0
        rec_ts = float(maint.get("last_used_at") or 0.0) if (inc and act) else 0.0
        upper_bound = max(last_real_used_at or 0.0, sum_ts, rec_ts) or None

        ind_uses = int(agg["independent_uses"]) if agg is not None else 0
        uses_since_hurt = int(agg["uses_since_hurt"]) if agg is not None else 0
        hurt_count = int(agg["hurt_count"]) if agg is not None else (
            int(maint["hurt_count"]) if isinstance(maint.get("hurt_count"), int) else None
        )
        strike_epoch = int(agg["strike_epoch"]) if agg is not None else max(0, int(maint.get("strike_epoch") or 0))
        epoch_started_at = (
            float(agg["epoch_started_at"])
            if agg is not None
            else (float(maint["epoch_started_at"]) if maint.get("epoch_started_at") else None)
        )
        seq_val = int(agg["sequence"]) if agg is not None else (
            int(maint["usage_receipt_sequence"])
            if isinstance(maint.get("usage_receipt_sequence"), int)
            else None
        )

        # 保守计算 ever_used 与 independent_uses_coverage，绝不把未知当成未使用。
        has_any_positive = (ind_uses > 0) or (lifetime_events > 0) or (last_real_used_at is not None)
        if has_any_positive:
            ever_used: bool | None = True
        elif lifetime_status == "complete":
            ever_used = False
        else:
            ever_used = None

        if distinct_status == "complete" and inc and act and sha:
            if agg is not None:
                ind_cov = str(agg["coverage"])
            elif name in legacy_skills and ever_used:
                ind_cov = "lower_bound"
            else:
                ind_cov = "exact"
        elif has_any_positive:
            ind_cov = "lower_bound"
        else:
            ind_cov = "unknown"

        delta_rows = [
            row for row in skill_rows if int(row.get("sequence") or 0) > after_sequence
        ][:MAX_USAGE_DELTA_ROWS_COUNT]

        entry_doc: dict[str, object] = {
            "ever_used": ever_used,
            "lifetime_use_events": max(0, lifetime_events),
            "last_real_used_at": last_real_used_at,
            "last_activity_at_upper_bound": upper_bound,
            "independent_uses": max(0, ind_uses),
            "independent_uses_coverage": ind_cov,
            "uses_since_hurt": max(0, uses_since_hurt),
            "activation_id": act,
            "strike_epoch": max(0, strike_epoch),
            "rows": delta_rows,
            "hurt_count": hurt_count,
            "epoch_started_at": epoch_started_at,
            "incarnation_id": inc,
            "content_sha256": sha if _SHA256_RE.fullmatch(sha) else "",
            "usage_receipt_sequence": seq_val,
        }
        by_skill_out[name] = entry_doc

    if (
        distinct_status == "complete"
        and not legacy_skills
        and not has_missing_version_binding
        and registry is not None
    ):
        version_attribution = "complete"
    else:
        version_attribution = "legacy_unknown"

    return {
        "coverage": {
            "lifetime_summary": lifetime_status,
            "delta_events": delta_status,
            "version_attribution": version_attribution,
            "distinct_receipts": distinct_status,
        },
        "through": through,
        "by_skill": by_skill_out,
        "warnings": warnings,
    }


# LLM: 先验事件和种类为字符串再判断成员，chars限制非负INT64；保留省略与None差别，不落query、正文或异常文本。
# 函数用途: 生成一行有 UTC 日期和有界关联标识的使用记录。
def _usage_row(agent, params: dict) -> dict:
    event, skill_id, source = params["event"], params["skill_id"], params["source"]
    kind = params.get("kind", "skill")
    if (not isinstance(event, str) or event not in USAGE_EVENTS
            or not isinstance(kind, str) or kind not in {"skill", "capability_package"} or not skill_id):
        raise ValueError("invalid usage identity")
    context = {key: value for key, value in params.items() if key not in {"event", "skill_id", "source", "kind"}}
    evidence = {key: context.pop(key) for key in ('source_key', 'version_ref', 'receipt_ref', 'producer') if key in context}
    if any(type(value) is not str or len(value) > 512 for value in evidence.values()):
        raise ValueError('invalid usage evidence')
    if evidence and (len(evidence.get('source_key', '')) != 64 or not evidence.get('version_ref') or not evidence.get('receipt_ref')):
        raise ValueError('incomplete usage evidence')
    if event in {'visible', 'selected', 'executed'} and not evidence:
        raise ValueError('method stage requires original source evidence')
    if set(context) - set(_CONTEXT_DEFAULTS):
        raise ValueError("invalid usage context")
    run_params = getattr(agent, "_current_run_params", None)
    attrs = getattr(run_params, "task_attributes", None) or {}
    values = {
        key: context.get(key, getattr(run_params, key, default))
        for key, default in _CONTEXT_DEFAULTS.items()
    }
    if "thread_id" not in context:
        values["thread_id"] = attrs.get("conversation_thread_id", "")

    raw_body_chars = values.pop("body_chars")
    has_body_chars = raw_body_chars is not MISSING and raw_body_chars is not None
    if has_body_chars and (type(raw_body_chars) is not int or not 0 <= raw_body_chars <= _INT64_MAX):
        raise ValueError("invalid usage body_chars")

    raw_strike_epoch = values.pop("strike_epoch")
    has_strike_epoch = raw_strike_epoch is not MISSING and raw_strike_epoch is not None
    if has_strike_epoch and (type(raw_strike_epoch) is not int or not 0 <= raw_strike_epoch <= _INT64_MAX):
        raise ValueError("invalid usage strike_epoch")

    chars = values.pop("chars")
    if "chars" not in context and has_body_chars:
        chars = raw_body_chars
    if type(chars) is not int or not 0 <= chars <= _INT64_MAX:
        raise ValueError("invalid usage chars")

    raw_sha = str(values.pop("content_sha256") or "").strip().lower()[:128]
    raw_task_key = str(values.pop("task_key") or "").strip()[:512]
    raw_inc = str(values.pop("incarnation_id") or "").strip()[:512]
    raw_act = str(values.pop("activation_id") or "").strip()[:512]

    req_id = str(values.get("request_id") or "")[:512]
    thr_id = str(values.get("thread_id") or "")[:512]
    tsk_id = str(values.get("task_id") or "")[:512]
    resolved_task_key = canonical_task_key(task_key=raw_task_key, task_id=tsk_id, request_id=req_id)

    at = datetime.now(timezone.utc).isoformat()
    row: dict[str, object] = {
        "at": at,
        "date": at[:10],
        "event": event,
        "skill_id": str(skill_id)[:512],
        "kind": kind,
        "source": str(source)[:128],
        "chars": chars,
        "request_id": req_id,
        "thread_id": thr_id,
        "task_id": tsk_id,
    }
    if has_body_chars:
        row["body_chars"] = raw_body_chars
    if has_strike_epoch:
        row["strike_epoch"] = raw_strike_epoch
    if raw_sha:
        row["content_sha256"] = raw_sha
    if resolved_task_key:
        row["task_key"] = resolved_task_key
    if raw_inc:
        row["incarnation_id"] = raw_inc
    if raw_act:
        row["activation_id"] = raw_act
    row.update(evidence)
    return row


# LLM: 文件路径固定在原 Store 目录内，读写统一 no-follow；坏 JSON 不按空账覆盖。
# 函数用途: 安全读取一份可选统计文件。
def _read_usage_text(store, name: str) -> str | None:
    root = store.skills_root.parent
    parts = (store.directory / name).relative_to(root).parts
    return read_text_beneath(root, parts)


# LLM: 原 Store 锁会以最近祖先为锚，先从 owner 根核受管父链，避免既有目录链接把锁文件建到其它 owner。
# 函数用途: 在进入原配额/状态锁前拒绝不安全目录；正常首次缺失仍交原 Store 创建。
def _check_usage_directory(store) -> None:
    root = store.skills_root.parent
    try:
        descriptor = open_directory_beneath(root, store.directory.relative_to(root).parts)
    except FileNotFoundError:
        return
    os.close(descriptor)


# LLM: JSONL 边界仅物理 LF，Unicode NEL/分隔符仍是合法标识内容；损坏行拒绝而非静默丢账。
# 函数用途: 读取完整有界事件账本，供追加或缺失汇总时重建。
def _ledger_rows(store) -> list[dict]:
    text = _read_usage_text(store, "usage_ledger.jsonl") or ""
    rows = [json.loads(line) for line in text.split("\n") if line.strip()]
    if any(not isinstance(row, dict) or row.get("event") not in USAGE_EVENTS for row in rows):
        raise ValueError("invalid usage ledger")
    return rows


# LLM: 四类事实分别计数；有效使用只算正文读取与实际宿主投递，不把检索/采用混进去。
# 函数用途: 返回新的空计数，避免跨 owner 共享可变对象。
def _empty_counts() -> dict:
    return {**dict.fromkeys(USAGE_EVENTS[:4], 0), "effective_uses": 0}


# LLM: 日期映射与生命周期计数附带同一事件水位，明细裁剪不能重置累计总数；构造不落盘。
# 函数用途: 创建一个 owner 的空使用汇总。
def _empty_summary() -> dict:
    return {"totals": _empty_counts(), "by_skill": {}, "daily_history": {}, "last_sequence": 0}


# LLM: 生命周期计数取原汇总；缺失汇总且legacy无序号时拒绝写入恢复，不通过一次追加为残余历史补造完整证明。
# 函数用途: 加载汇总并检查基本结构，拒绝损坏状态被新事件覆盖。
def _read_summary(store, rows: list | None = None) -> dict:
    text = _read_usage_text(store, "usage_summary.json")
    rows = _ledger_rows(store) if rows is None else rows
    if text is None and any('sequence' not in row for row in rows):
        raise ValueError('legacy_history_start_unknown')
    summary = json.loads(text) if text is not None else _empty_summary()
    if (not isinstance(summary, dict) or not isinstance(summary.get("totals"), dict)
            or not isinstance(summary.get("by_skill"), dict) or not isinstance(summary.get("daily_history"), dict)):
        raise ValueError("invalid usage summary")
    _summary_sequence(summary)
    _normalize_by_skill(summary)
    _replay_usage_rows(summary, rows)
    return summary


# LLM: 仅规范化统计聚合键，不改变快照、pin、授权身份或原始明细里的调用者stable_id。
# 函数用途: 去掉能力包和普通Skill的引用包装前缀，避免同一方法被拆开计数。
def _canonical_skill_id(kind: str, skill_id: str) -> str:
    if kind == "capability_package" or skill_id.startswith("capability:"):
        skill_id = skill_id.removeprefix("capability:")
    return skill_id.removeprefix("skill:")


# LLM: 只对各身份四类事件求和并重算有效使用；最近元数据来自时间最新的旧行，生命周期与每日历史原样保留。
# 函数用途: 在内存中合并旧摘要的前缀别名，下次正常写入时保存规范键。
def _normalize_by_skill(summary: dict) -> None:
    merged = {}
    for skill_id, counts in summary["by_skill"].items():
        canonical = _canonical_skill_id(counts.get("kind", "skill"), skill_id)
        if not canonical:
            raise ValueError("invalid usage skill identity")
        target = merged.setdefault(canonical, _empty_counts())
        for event in USAGE_EVENTS:
            value = counts.get(event, 0)
            if type(value) is not int or value < 0:
                raise ValueError("invalid usage count")
            if event in USAGE_EVENTS[:4] or event in counts or value:
                target[event] = target.get(event, 0) + value
        target["effective_uses"] = target["manual_get"] + target["host_injected"]
        if counts.get("last_used_at", "") >= target.get("last_used_at", ""):
            target.update({key: counts[key] for key in ("last_used_at", "last_source", "kind") if key in counts})
    summary["by_skill"] = merged


# LLM: 每事件恰增四类中的一项，旧汇总可由四项之和恢复水位；不把effective_uses再加一次。
# 函数用途: 校验生命周期计数并恢复唯一汇总序号。
def _summary_sequence(summary: dict) -> None:
    counts = [summary["totals"].get(event, 0) for event in USAGE_EVENTS]
    if any(type(count) is not int or count < 0 for count in counts):
        raise ValueError("invalid usage totals")
    total = sum(counts)
    sequence = summary.get("last_sequence", total)
    if type(sequence) is not int or sequence != total:
        raise ValueError("invalid usage watermark")
    summary["last_sequence"] = sequence


# LLM: 明细必须单调；旧回滚丢失且已计入汇总的缺口仅告警不捏造行，水位后的缺号仍拒绝；兼容无序号前缀。
# 函数用途: 给旧行补内存序号，只补记水位后连续事件，避免旧领先汇总毒化后续记账。
def _replay_usage_rows(summary: dict, rows: list[dict]) -> None:
    numbered = next(((index, row["sequence"]) for index, row in enumerate(rows) if "sequence" in row), None)
    if numbered is not None and (type(numbered[1]) is not int or numbered[1] < 1):
        raise ValueError("invalid usage sequence")
    base = max(summary["last_sequence"] - len(rows), 0) if numbered is None else numbered[1] - numbered[0] - 1
    if base < 0:
        raise ValueError("invalid legacy usage prefix")
    previous = base
    for row in rows:
        sequence = row.setdefault("sequence", previous + 1)
        if type(sequence) is not int or sequence <= previous:
            raise ValueError("usage sequence is not increasing")
        if sequence != previous + 1:
            if sequence > summary["last_sequence"] + 1:
                raise ValueError("usage watermark has a gap")
            logging.getLogger(__name__).warning("SKILL_USAGE_DETAIL_GAP")
        if sequence > summary["last_sequence"]:
            if sequence != summary["last_sequence"] + 1:
                raise ValueError("usage watermark has a gap")
            _add_event(summary, row)
        previous = sequence
    if previous < summary["last_sequence"]:
        logging.getLogger(__name__).warning("SKILL_USAGE_DETAIL_GAP")


# LLM: 全量和单技能计数共用同一口径，旧有效使用字段按事实重新计算，不另加一次。
# 函数用途: 为指定事件增加计数并更新有效使用总数。
def _increment(counts: dict, event: str) -> None:
    for key in USAGE_EVENTS:
        value = counts.get(key, 0)
        if type(value) is not int or value < 0:
            raise ValueError("invalid usage count")
        if key in USAGE_EVENTS[:4]:
            counts.setdefault(key, 0)
    counts[event] = counts.get(event, 0) + 1
    counts["effective_uses"] = counts["manual_get"] + counts["host_injected"]


# LLM: 仅保留UTC最近30个日历日的日期映射，不把最近30个非空日当作30天，不保留未来日期。
# 函数用途: 清理三十天之外的每日历史并按日期排序。
def _prune_daily(summary: dict) -> None:
    today = datetime.now(timezone.utc).date()
    oldest = (today - timedelta(days=29)).isoformat()
    summary["daily_history"] = {day: counts for day, counts in sorted(summary["daily_history"].items())
                                if oldest <= day <= today.isoformat()}


# LLM: 只对已核连续序号累加四类计数与UTC日期映射；by_skill统一规范身份，原始明细仍保留调用者身份以供追溯。
# 函数用途: 将一条事实累加到生命周期、单技能和每日汇总。
def _add_event(summary: dict, row: dict) -> None:
    event = row["event"]
    _increment(summary["totals"], event)
    skill_id = _canonical_skill_id(row["kind"], row["skill_id"])
    if not skill_id:
        raise ValueError("invalid usage skill identity")
    skill = summary["by_skill"].setdefault(skill_id, _empty_counts())
    _increment(skill, event)
    skill.update(last_used_at=row["at"], last_source=row["source"], kind=row["kind"])
    daily = summary["daily_history"].setdefault(row["date"], dict.fromkeys(USAGE_EVENTS[:4], 0))
    daily[event] = daily.get(event, 0) + 1
    summary["last_sequence"] = row["sequence"]
    _prune_daily(summary)


# LLM: 原锁内只联合核额一次，不对每个文件另扫owner目录；正常明细先写而回滚汇总先恢复，耐久事件由水位补记。
# 函数用途: 保存有界账本及生命周期汇总，不写任何方法正文。
def _save_usage(store, admission, rows: list, summary: dict) -> None:
    texts = {"usage_ledger.jsonl": "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
             "usage_summary.json": json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n"}
    previous = {name: _read_usage_text(store, name) for name in texts}
    changes = []
    for name, text in texts.items():
        path = store.directory / name
        changes.append(OwnerQuotaChange(path, len(text.encode("utf-8"))))
    admission.check(changes)
    try:
        _write_usage_pair(store, texts)
    except OSError:
        _write_usage_pair(store, dict(reversed(previous.items())))
        raise


# LLM: 调用方持原锁与配额，None仅用于撤销本次创建的文件；所有写/删仍从本owner根逐段nofollow。
# 函数用途: 应用或恢复一对使用文件，不读取或复制任何Skill正文。
def _write_usage_pair(store, texts: dict) -> None:
    root = store.skills_root.parent
    for name, text in texts.items():
        parts = (store.directory / name).relative_to(root).parts
        if text is None:
            unlink_file_beneath(root, parts)
        else:
            write_text_atomic_beneath(root, parts, text)
