# LLM: 维护状态只在原registry中，观察不改变发布/授权；质量只认宿主核验、版本绑定真实采用回数或显式人类结果；
#   模型自述adopted不单独加分也不伪造last_used_at，3/4/5阶梯负反馈按SQLite版本绑定事务裁决，三次负反馈立即回滚或退场封禁。
# 模块用途: 管理自学Skill试用、3/4/5阶梯淘汰与恢复、80/20探索推荐、有界游标维护与安全可恢复归档。
from __future__ import annotations

import logging
import math
import time
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime
from functools import lru_cache

from ..common.nofollow_fs import NoFollowPathError, list_names_beneath, read_text_beneath
from ..memory_store.retention_scan_budget import RetentionScanBudget
from .skill_learning_publish import (
    _MISSING_SENTINEL,
    CODE_EVIDENCE_INCOMPLETE,
    CODE_LIMIT_REACHED,
    CODE_NOT_LEARNED,
    CODE_USER_MODIFIED,
    SkillLearningGateError,
    _cfg_lookup,
    _historical_adoptions,
    _owned_record,
    _retire_third_strike_locked,
    _rollback_to_proven_stable_locked,
    _victim_rank_key,
)
from .skill_learning_store import (
    CODE_ARCHIVE_EXPIRED,
    LearnedSkill,
    SkillLearningEvent,
    SkillLearningRegistry,
    SkillLearningStore,
    SkillLearningStoreError,
    is_proven_stable_version,
)
from .skill_snapshot import skill_content_sha256
from .skill_usage_ledger import load_usage_evidence
from .skill_usage_receipts import (
    canonical_negative_key,
    cleanup_retired_aggregates,
    has_body_delivery_receipt,
    read_usage_aggregate,
    record_exact_negative,
)

# 中文说明: 试用或活跃Skill触发第1次负反馈前需达到的基础有效独立任务数。
BASE_ESTABLISH_USES_COUNT = 3
# 中文说明: 触发版本回滚或退场封禁的累计负反馈阈值。
MAX_STRIKE_COUNT = 3
# 中文说明: 学习反馈来源编号允许的最大字符长度。
MAX_IDENTIFIER_CHARS = 300
# 中文说明: 注册表内保留的展示级观察历史环形上限条数。
MAX_OBSERVATIONS_COUNT = 200
# 中文说明: 注册表内保留的晋升历史状态条数上限。
MAX_PROMOTION_HISTORY_COUNT = 100
# 中文说明: 维护到期时间字符串解析有界LRU缓存条目上限。
DEFAULT_TIMESTAMP_CACHE_COUNT = 256

_HOST_DELIVERY_KEYS_CTX: ContextVar[tuple[str, ...]] = ContextVar(
    "_sm_w2_host_delivery_keys",
    default=(),
)


@lru_cache(maxsize=DEFAULT_TIMESTAMP_CACHE_COUNT)
def _parse_iso_epoch_cached(raw: str) -> float:
    """将ISO时间戳字符串解析为Unix秒时间戳并仅缓存不可变字符串解析结果。"""
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def learning_reconcile_due(
    modified_at: float | str,
    now: float,
    *,
    interval_seconds: int,
) -> bool:
    """按实时流逝与可配置间隔判断维护是否到期；首次未执行时立即到期，绝不缓存到期布尔值。"""
    if int(interval_seconds) <= 0:
        return False
    if isinstance(modified_at, str):
        modified_ts = _parse_iso_epoch_cached(modified_at.strip())
    else:
        try:
            modified_ts = float(modified_at or 0.0)
        except (TypeError, ValueError):
            modified_ts = 0.0
    if not math.isfinite(modified_ts) or modified_ts <= 0.0:
        return True
    try:
        now_ts = float(now)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(now_ts):
        return False
    return (now_ts - modified_ts) >= float(interval_seconds)


# LLM: 由调用方宿主提供的来源事实，模型工具只构造model，human只来自已认证用户control；不从工具参数反序列化。
# 类用途: 汇总反馈身份与执行来源，并携带只由宿主填充的版本激活与打击纪元绑定标识。
@dataclass(frozen=True)
class LearningObservationSource:
    authority: str = 'model'
    job_id: str = ''
    receipt_id: str = ''
    actor_id: str = ''
    run_id: str = ''
    incarnation_id: str = ""
    activation_id: str = ""
    strike_epoch: int | None = None


def _valid_identifier(value: object) -> bool:
    return isinstance(value, str) and len(value) <= MAX_IDENTIFIER_CHARS and "\n" not in value and "\r" not in value


def _match_adoption_task(row: dict, task_id: str) -> bool:
    return (
        row.get("sha256") is not None
        and row.get("authority") == "model"
        and row.get("outcome") == "adopted"
        and (row.get("task_id") == task_id or row.get("root_task_id") == task_id)
    )


def _execute_third_strike_locked(
    store: SkillLearningStore,
    registry: SkillLearningRegistry,
    current: LearnedSkill,
    *,
    observations: list[dict] | None = None,
    last_negative_receipt: str = "",
    now: float,
) -> dict[str, object]:
    """在已持state_lock下幂等完成第三次负反馈：有已证实稳定前驱则回滚，否则立即退场并封禁名字与家族。"""
    stable = current.maintenance.get("previous_stable")
    if is_proven_stable_version(stable):
        snap_maint = dict(current.maintenance)
        if observations is not None:
            snap_maint["observations"] = observations[-MAX_OBSERVATIONS_COUNT:]
        hist = _historical_adoptions(replace(current, maintenance=snap_maint))
        reg2, rolled = _rollback_to_proven_stable_locked(
            store,
            registry,
            current,
            reason_code="third_strike_rollback",
            now=now,
        )
        rolled_maint = dict(rolled.maintenance)
        rolled_maint["historical_adoptions"] = hist
        rolled_maint.setdefault("baseline_at", float(now))
        if last_negative_receipt:
            rolled_maint["last_negative_receipt"] = last_negative_receipt
        final_rolled = replace(rolled, maintenance=rolled_maint)
        store.save_registry(replace(reg2, skills={**reg2.skills, current.name: final_rolled}))
        return {
            "skill_name": final_rolled.name,
            "sha256": final_rolled.sha256,
            "quality": "established",
            "state": "active",
            "action": "rolled_back",
            "hurt_count": 0,
            "uses_since_hurt": 0,
            "strike_epoch": int(rolled_maint.get("strike_epoch") or 0),
        }

    _retire_third_strike_locked(
        store,
        registry,
        current,
        reason_code="third_strike_retire",
        now=now,
    )
    return {
        "skill_name": current.name,
        "sha256": current.sha256,
        "quality": "unreliable",
        "state": "retired",
        "action": "retired",
        "hurt_count": MAX_STRIKE_COUNT,
        "uses_since_hurt": 0,
        "strike_epoch": int(current.maintenance.get("strike_epoch") or 0),
    }


def _apply_store_only_negative(
    maint: dict,
    *,
    neg_key_text: str,
    receipt_id: str,
    ts: float,
) -> tuple[bool, int, int]:
    """无RuntimeRepository的纯Store单测中按同款任务级去重与3/4/5阶梯推进负反馈。"""
    recorded_keys = [str(item) for item in maint.get("_store_negative_keys", []) if isinstance(item, str)]
    if maint.get("last_negative_receipt") == receipt_id and receipt_id:
        return False, int(maint.get("hurt_count") or 0), int(maint.get("strike_epoch") or 0)
    if neg_key_text and neg_key_text in recorded_keys:
        return False, int(maint.get("hurt_count") or 0), int(maint.get("strike_epoch") or 0)
    current_hurt = max(0, int(maint.get("hurt_count") or 0))
    if current_hurt >= MAX_STRIKE_COUNT:
        return False, current_hurt, max(0, int(maint.get("strike_epoch") or 0))
    new_hurt = current_hurt + 1
    new_epoch = max(0, int(maint.get("strike_epoch") or 0)) + 1
    if neg_key_text:
        recorded_keys.append(neg_key_text)
        maint["_store_negative_keys"] = recorded_keys[-50:]
    maint["hurt_count"] = new_hurt
    maint["strike_epoch"] = new_epoch
    maint["uses_since_hurt"] = 0
    maint["epoch_started_at"] = ts
    maint["last_negative_receipt"] = receipt_id
    return True, new_hurt, new_epoch


# LLM: 反馈绑定当前hash、incarnation_id/activation_id与独立任务；正文投递回执或自愿adopted均可归因。
#   正向结果绝不消减hurt_count；1/2次负反馈重置周期分别需要4/5次真实使用；3次负反馈立即回滚或退场封禁。
# 函数用途: 登记采用、宿主包验证或人类反馈，支持中断重启后的三次负反馈幂等续跑。
def observe_learning(store, name, *, sha256, task_id, outcome, source=None):
    source = source or LearningObservationSource()
    authority = source.authority
    job_id = source.job_id
    receipt_id = source.receipt_id
    actor_id = source.actor_id
    run_id = source.run_id
    if not all(_valid_identifier(v) for v in (task_id, job_id, receipt_id, actor_id, run_id, source.incarnation_id, source.activation_id)):
        raise ValueError('学习反馈来源编号无效')
    if source.strike_epoch is not None and (isinstance(source.strike_epoch, bool) or not isinstance(source.strike_epoch, int) or source.strike_epoch < 0):
        raise ValueError('学习反馈打击纪元无效')
    allowed = {'adopted', 'unknown'} if authority == 'model' else {'adopted', 'unknown', 'effective', 'ineffective', 'environment'} if authority in {'human', 'host'} else set()
    if outcome not in allowed or not task_id or (authority in {'human', 'host'} and not receipt_id) or (authority == 'human' and not actor_id):
        raise ValueError('学习反馈缺少准确来源或确认事实')

    ts = time.time()
    with store.state_lock():
        registry = store._prepare_clean_registry_locked(store.load_registry(), now=ts)
        current = _owned_record(store, registry, name)
        if current.sha256 != sha256:
            raise SkillLearningGateError('SKILL_LEARNING_REVISION_CONFLICT')

        data = dict(current.maintenance)
        cur_inc = str(data.get("incarnation_id") or "")
        cur_act = str(data.get("activation_id") or "")
        bound_record = bool(cur_inc and cur_act)
        if source.incarnation_id and source.incarnation_id != cur_inc:
            raise SkillLearningGateError('SKILL_LEARNING_REVISION_CONFLICT')
        if source.activation_id and source.activation_id != cur_act:
            raise SkillLearningGateError('SKILL_LEARNING_REVISION_CONFLICT')

        observations = list(data.get('observations', []))
        runtime_repo = store._runtime_repository()

        if bound_record and runtime_repo is not None and outcome == 'ineffective':
            existing_agg = read_usage_aggregate(
                runtime_repo,
                incarnation_id=cur_inc,
                activation_id=cur_act,
                content_sha256=sha256,
            )
            if existing_agg is not None and int(existing_agg.get("hurt_count") or 0) >= MAX_STRIKE_COUNT:
                return _execute_third_strike_locked(
                    store,
                    registry,
                    current,
                    observations=observations,
                    last_negative_receipt=receipt_id or str(data.get("last_negative_receipt") or ""),
                    now=ts,
                )

        adoption = next(
            (row for row in observations if row.get('sha256') == sha256 and _match_adoption_task(row, task_id)),
            None,
        )
        has_delivery = False
        if adoption is None and authority in {'human', 'host'} and outcome in {'effective', 'ineffective'}:
            if bound_record and runtime_repo is not None:
                has_delivery = has_body_delivery_receipt(
                    runtime_repo,
                    incarnation_id=cur_inc,
                    activation_id=cur_act,
                    content_sha256=sha256,
                    task_key=task_id,
                    root_task_id=task_id,
                    task_id=task_id,
                    request_id=task_id,
                )
                if not has_delivery and authority == 'host':
                    for cand_key in _HOST_DELIVERY_KEYS_CTX.get(()):
                        if cand_key and has_body_delivery_receipt(
                            runtime_repo,
                            incarnation_id=cur_inc,
                            activation_id=cur_act,
                            content_sha256=sha256,
                            task_key=cand_key,
                        ):
                            has_delivery = True
                            break
            if not has_delivery:
                raise ValueError('该任务没有此版本的结构化采用回执')

        if adoption:
            job_id = adoption.get('job_id') or job_id

        already_same_receipt = (
            authority in {'human', 'host'}
            and receipt_id
            and any(
                row.get('sha256') == sha256
                and row.get('authority') == authority
                and row.get('receipt_id') == receipt_id
                and row.get('outcome') == outcome
                for row in observations
            )
        )

        subject = job_id or task_id
        if outcome == 'ineffective' and authority in {'human', 'host'}:
            if bound_record:
                # 兼容仅含展示字段的内部键清洗，不留给Schema校验未知键。
                neg_key_text = canonical_negative_key(
                    root_task_id=task_id if task_id.startswith("task:") else "",
                    task_id="" if task_id.startswith(("task:", "request:", "receipt:")) else task_id,
                    request_id=task_id.removeprefix("request:") if task_id.startswith("request:") else "",
                    receipt_id=receipt_id,
                )
                if not neg_key_text:
                    neg_key_text = canonical_negative_key(task_id=task_id, receipt_id=receipt_id)

                accepted_strike = False
                new_hurt = int(data.get("hurt_count") or 0)
                new_epoch = int(data.get("strike_epoch") or 0)
                if already_same_receipt or (receipt_id and data.get("last_negative_receipt") == receipt_id):
                    accepted_strike = False
                elif runtime_repo is not None:
                    neg_res = record_exact_negative(
                        store,
                        incarnation_id=cur_inc,
                        activation_id=cur_act,
                        content_sha256=sha256,
                        root_task_id=task_id if task_id.startswith("task:") else "",
                        task_id="" if task_id.startswith(("task:", "request:", "receipt:")) else task_id,
                        request_id=task_id.removeprefix("request:") if task_id.startswith("request:") else "",
                        receipt_id=receipt_id,
                        at=ts,
                        runtime=runtime_repo,
                    )
                    status = str(neg_res.get("status") or "")
                    agg = neg_res.get("aggregate")
                    if isinstance(agg, dict):
                        new_hurt = int(agg["hurt_count"])
                        new_epoch = int(agg["strike_epoch"])
                        data["hurt_count"] = new_hurt
                        data["strike_epoch"] = new_epoch
                        data["uses_since_hurt"] = int(agg["uses_since_hurt"])
                        data["epoch_started_at"] = float(agg["epoch_started_at"])
                        data["usage_receipt_sequence"] = max(
                            int(data.get("usage_receipt_sequence") or 0),
                            int(agg["sequence"]),
                        )
                    if status == "already_retired" or new_hurt >= MAX_STRIKE_COUNT:
                        return _execute_third_strike_locked(
                            store,
                            registry,
                            current,
                            observations=observations,
                            last_negative_receipt=receipt_id,
                            now=ts,
                        )
                    accepted_strike = status == "accepted"
                elif _has_store_negative_key(store, name, cur_inc, cur_act, neg_key_text):
                    accepted_strike = False
                else:
                    accepted_strike, new_hurt, new_epoch = _apply_store_only_negative(
                        data,
                        neg_key_text=neg_key_text,
                        receipt_id=receipt_id,
                        ts=ts,
                    )

                if not any(
                    row.get("receipt_id") == receipt_id and row.get("sha256") == sha256 and row.get("authority") == authority
                    for row in observations
                ):
                    observations.append({
                        'subject': subject,
                        'task_id': task_id,
                        'job_id': job_id,
                        'sha256': sha256,
                        'authority': authority,
                        'outcome': outcome,
                        'receipt_id': receipt_id,
                        'actor_id': actor_id,
                        'run_id': run_id or task_id,
                        'root_task_id': task_id,
                        'at': ts,
                    })

                if accepted_strike and new_hurt >= MAX_STRIKE_COUNT:
                    return _execute_third_strike_locked(
                        store,
                        registry,
                        current,
                        observations=observations,
                        last_negative_receipt=receipt_id,
                        now=ts,
                    )
                if accepted_strike:
                    data["quality"] = "trial"
                    data["quality_basis"] = "negative_strike"
                    if data.get("state") not in {"disabled", "archived"}:
                        data["state"] = "trial"
                    data.pop("established_at", None)
                    data["last_negative_receipt"] = receipt_id
                    data.setdefault("baseline_at", ts)
            else:
                # 历史未绑定activation_id的遗留记录只追加展示历史，不写入可触发退场的版本级打击。
                observations.append({
                    'subject': subject,
                    'task_id': task_id,
                    'job_id': job_id,
                    'sha256': sha256,
                    'authority': authority,
                    'outcome': outcome,
                    'receipt_id': receipt_id,
                    'actor_id': actor_id,
                    'run_id': run_id or task_id,
                    'root_task_id': task_id,
                    'at': ts,
                })
                data.setdefault("baseline_at", ts)

            data["observations"] = observations[-MAX_OBSERVATIONS_COUNT:]
            internal_keys = dict(data)
            internal_keys.pop("_store_negative_keys", None)
            record = replace(current, maintenance=internal_keys)
            if "_store_negative_keys" in data:
                # 保持无SQLite测试中单次进程内存实例去重，避免向registry写入未知键触发Schema拒绝。
                _remember_store_negative_key(store, name, cur_inc, cur_act, data["_store_negative_keys"][-1])
            store.save_registry(replace(registry, skills={**registry.skills, name: record}))
            return {
                'skill_name': name,
                'sha256': sha256,
                'quality': record.maintenance.get('quality', 'trial'),
                'state': record.maintenance.get('state', 'trial'),
                'hurt_count': int(record.maintenance.get('hurt_count') or 0),
                'uses_since_hurt': int(record.maintenance.get('uses_since_hurt') or 0),
                'strike_epoch': int(record.maintenance.get('strike_epoch') or 0),
                'action': 'recorded' if not already_same_receipt else 'deduplicated',
            }

        # 非负向观察（adopted / effective / environment / unknown）：只增记显示环；正向反馈绝不抹除hurt_count。
        if authority == 'host':
            observations = [
                row for row in observations
                if not (
                    row.get('subject') == subject
                    and row.get('sha256') == sha256
                    and row.get('authority') == 'host'
                    and row.get('receipt_id') != receipt_id
                )
            ]
        dedup_key = (task_id if authority == 'model' else subject, sha256, authority)
        if not any((row['task_id'] if authority == 'model' else row['subject'], row['sha256'], row['authority']) == dedup_key for row in observations):
            observations.append({
                'subject': subject,
                'task_id': task_id,
                'job_id': job_id,
                'sha256': sha256,
                'authority': authority,
                'outcome': outcome,
                'receipt_id': receipt_id,
                'actor_id': actor_id,
                'run_id': run_id or task_id,
                'root_task_id': task_id,
                'at': ts,
            })

        data['observations'] = observations[-MAX_OBSERVATIONS_COUNT:]
        data.setdefault('baseline_at', ts)
        data['helped_count'] = _verified_counts(observations, sha256)[0]
        if bound_record:
            if outcome == 'effective':
                data['last_used_at'] = max(float(data.get('last_used_at') or 0.0), ts)
        record = replace(current, maintenance=data)
        store.save_registry(replace(registry, skills={**registry.skills, name: record}))
    return {
        'skill_name': name,
        'sha256': sha256,
        'quality': data.get('quality', 'trial'),
        'state': data.get('state', 'trial'),
        'hurt_count': int(data.get('hurt_count') or 0),
        'uses_since_hurt': int(data.get('uses_since_hurt') or 0),
        'strike_epoch': int(data.get('strike_epoch') or 0),
    }


# 仅作为无RuntimeRepository测试环境下的进程内负向任务去重辅助（有界LRU结构）。
_STORE_ONLY_NEG_KEYS: dict[tuple[str, str, str, str], list[str]] = {}


def _remember_store_negative_key(store: SkillLearningStore, name: str, inc: str, act: str, neg_key: str) -> None:
    cache_key = (str(store.directory), name, inc, act)
    if len(_STORE_ONLY_NEG_KEYS) >= DEFAULT_TIMESTAMP_CACHE_COUNT and cache_key not in _STORE_ONLY_NEG_KEYS:
        _STORE_ONLY_NEG_KEYS.pop(next(iter(_STORE_ONLY_NEG_KEYS)))
    bucket = _STORE_ONLY_NEG_KEYS.setdefault(cache_key, [])
    if neg_key not in bucket:
        bucket.append(neg_key)
        if len(bucket) > 50:
            del bucket[:-50]


def _has_store_negative_key(store: SkillLearningStore, name: str, inc: str, act: str, neg_key: str) -> bool:
    return neg_key in _STORE_ONLY_NEG_KEYS.get((str(store.directory), name, inc, act), ())


# LLM: 同一独立任务/job最多计一份结果，人类纠正优先宿主；模型声明和环境异常没有质量权重。
# 函数用途: 汇总当前方法版本可核验的正面与展示历史结果。
def _verified_counts(observations, sha256):
    facts = {}
    for authority in ('host', 'human'):
        for row in observations:
            if row.get('sha256') == sha256 and row.get('authority') == authority and row.get('outcome') in {'effective', 'ineffective'}:
                facts[row['subject']] = row['outcome']
    return sum(value == 'effective' for value in facts.values()), sum(value == 'ineffective' for value in facts.values())


def _resolve_control_policy(
    config_provider: Callable[[], object] | None,
    max_skills: int,
    evict_seconds: int,
) -> tuple[int, int]:
    """在所有store锁外解析人类控制动作的实时容量上限与归档宽限配置。"""
    if config_provider is None:
        return int(max_skills), int(evict_seconds)
    try:
        cfg = config_provider()
    except Exception as exc:
        raise SkillLearningGateError(
            CODE_EVIDENCE_INCOMPLETE,
            {"reason": "config_provider_failed", "error": type(exc).__name__},
        ) from exc
    if cfg is None:
        return int(max_skills), int(evict_seconds)
    eff_max = int(max_skills)
    if eff_max == 0:
        raw_max = _cfg_lookup(cfg, "self_learning_max_skills")
        if raw_max is _MISSING_SENTINEL:
            raw_max = _cfg_lookup(cfg, "max_learned_skills")
        if raw_max is not _MISSING_SENTINEL and raw_max is not None:
            eff_max = int(raw_max)
    eff_evict = int(evict_seconds)
    if eff_evict == 0:
        raw_evict = _cfg_lookup(cfg, "self_learning_evict_seconds")
        if raw_evict is not _MISSING_SENTINEL and raw_evict is not None:
            eff_evict = int(raw_evict)
    return eff_max, eff_evict


def _has_extra_dir_entries(root, rel_parts: tuple[str, ...]) -> tuple[bool, bool]:
    """检查目标Skill目录是否存在除SKILL.md以外的额外文件或符号链接；返回(exists_ok, has_extra_or_bad)。"""
    try:
        names = list_names_beneath(root, rel_parts)
    except NoFollowPathError:
        return False, True
    except OSError:
        return False, True
    if not names:
        return False, False
    if set(names) != {"SKILL.md"}:
        return True, True
    return True, False


def _select_cap_victim_locked(
    store: SkillLearningStore,
    registry: SkillLearningRegistry,
    managed: dict[str, LearnedSkill],
    *,
    protected: set[str],
    exclude_names: set[str] | None = None,
) -> str | None:
    """在已持state_lock内按统一R-2优先级选定一名可归档置换的在役托管Skill。"""
    if "*" in protected:
        return None
    excluded = exclude_names or set()
    eligible: list[tuple[tuple[object, ...], str]] = []
    for candidate_name, record in sorted(managed.items()):
        if candidate_name in excluded:
            continue
        maint = record.maintenance
        if (
            maint.get("pinned") is True
            or maint.get("state") in {"disabled", "archived"}
            or candidate_name in protected
            or f"owner:{candidate_name}" in protected
        ):
            continue
        eligible.append((_victim_rank_key(candidate_name, record), candidate_name))
    if not eligible:
        return None
    eligible.sort(key=lambda item: item[0])
    return eligible[0][1]


# LLM: 显式控制动作即使自动学习关闭也仍可由人类执行；恢复归档必须一次核验hash、额外文件、宽限期与容量上限。
# 函数用途: 执行用户显式pin/unpin/disable/enable/restore维护动作并持久化版本或安全迁移目录。
def control_learning(
    store,
    name,
    *,
    operation,
    max_skills=0,
    protected_ids=(),
    protection_complete=False,
    now=None,
    evict_seconds=0,
    config_provider=None,
):
    if operation not in {'pin', 'unpin', 'disable', 'enable', 'restore'}:
        raise ValueError('维护动作无效')
    eff_max, eff_evict = _resolve_control_policy(config_provider, max_skills, evict_seconds)
    ts = time.time() if now is None else float(now)
    protected = {str(item) for item in protected_ids}

    with store.state_lock():
        registry = store._prepare_clean_registry_locked(store.load_registry(), now=ts)
        current = registry.skills.get(name)
        if current is None or name in registry.blocked_names:
            raise SkillLearningGateError(CODE_NOT_LEARNED, {"skill_name": name})

        if operation == 'restore':
            if current.maintenance.get('state') != 'archived':
                raise ValueError('条目未归档')
            archived_at = float(current.maintenance.get('archived_at') or 0.0)
            if eff_evict > 0 and archived_at > 0.0 and (ts - archived_at) >= float(eff_evict):
                raise SkillLearningGateError(
                    CODE_ARCHIVE_EXPIRED,
                    {"skill_name": name, "archived_at": archived_at, "evict_seconds": eff_evict},
                )
            dir_ok, has_extra = _has_extra_dir_entries(store.directory, ("archived", name))
            if dir_ok and has_extra:
                handoff = replace(current, maintenance={**current.maintenance, "ownership": "user_modified"})
                store.save_registry(replace(registry, skills={**registry.skills, name: handoff}))
                raise SkillLearningGateError(
                    CODE_USER_MODIFIED,
                    {"skill_name": name, "state": "user_modified", "reason": "extra_files_in_archive"},
                )

            if eff_max > 0:
                try:
                    managed = store.managed_records(registry, include_archived=False)
                except SkillLearningStoreError as exc:
                    raise SkillLearningGateError(CODE_EVIDENCE_INCOMPLETE, exc.detail) from exc
                live = registry.live_count(managed_names=managed.keys())
                if live >= eff_max:
                    if not protection_complete:
                        raise SkillLearningGateError(
                            CODE_EVIDENCE_INCOMPLETE,
                            {"reason": "protection_incomplete", "limit": eff_max, "live": live},
                        )
                    victim = _select_cap_victim_locked(
                        store,
                        registry,
                        managed,
                        protected=protected,
                        exclude_names={name},
                    )
                    if victim is None:
                        raise SkillLearningGateError(
                            CODE_LIMIT_REACHED,
                            {"limit": eff_max, "live": live, "all_protected": True},
                        )
                    registry, _ = store._transition_archive_locked(
                        registry,
                        victim,
                        now=ts,
                        reason_code="cap_displace_for_restore",
                    )

            registry, next_record = store._transition_restore_locked(registry, name, now=ts)
            if store.read_version(name, next_record.revision) is None:
                path = store.skill_path(name, record=next_record)
                text = read_text_beneath(store.skills_root, path.relative_to(store.skills_root).parts)
                if text is not None:
                    store.save_version(name, next_record.revision, text)
            return {'skill_name': name, 'maintenance': dict(next_record.maintenance)}

        with ExitStack() as rollback:
            data = dict(current.maintenance)
            if operation in {'pin', 'unpin'}:
                data['pinned'] = operation == 'pin'
            elif operation == 'disable':
                data['state'] = 'disabled'
            elif operation == 'enable':
                if data.get('state') == 'archived':
                    raise ValueError('请显式restore归档条目')
                data['state'] = 'active' if data.get('quality') == 'established' else 'trial'
            next_record = replace(current, maintenance=data)
            if operation in {'disable', 'enable'}:
                path = store.skill_path(name, record=next_record)
                text = read_text_beneath(store.skills_root, path.relative_to(store.skills_root).parts)
                next_record = replace(next_record, revision=max(current.revision, current.version) + 1)
                store.save_version(name, next_record.revision, text)
            store.save_registry(replace(registry, skills={**registry.skills, name: next_record}))
            rollback.pop_all()
        return {'skill_name': name, 'maintenance': dict(next_record.maintenance)}


def _is_proven_never_used(record: LearnedSkill) -> bool:
    maint = record.maintenance
    if maint.get("ownership") == "unknown":
        return False
    return (
        maint.get("ever_used") is False
        and int(maint.get("independent_uses") or 0) == 0
        and float(maint.get("last_used_at") or 0.0) == 0.0
        and maint.get("legacy_last_activity_upper_bound") is None
        and maint.get("independent_uses_coverage", "exact") == "exact"
    )


# LLM: 推荐只是默认展示投影，不影响搜索/get授权；置顶优先、其余按80/20配额双向补足，未知历史绝不伪装新颖。
# 函数用途: 从同一registry确定性选出有界常用推荐集合，支持explore_percent=0保持原纯热度排序。
def recommended_learning(registry, limit=50, *, explore_percent=0) -> set[str]:
    eligible = [
        row for row in registry.skills.values()
        if row.layout != "owner"
        and row.name not in getattr(registry, "blocked_names", ())
        and row.maintenance.get("ownership") not in {"owner_layout", "user_modified"}
        and row.maintenance.get("state") not in {"disabled", "archived", "cooled"}
    ]
    if limit <= 0:
        return {row.name for row in eligible}

    def _legacy_sort_key(row: LearnedSkill) -> tuple[bool, float, str]:
        return (
            row.maintenance.get("quality") != "established",
            -float(row.maintenance.get("last_used_at") or 0.0),
            row.name,
        )

    pinned = [row for row in eligible if row.maintenance.get("pinned") is True]
    pinned.sort(key=_legacy_sort_key)
    selected = [row.name for row in pinned[:limit]]
    remaining = limit - len(selected)
    if remaining <= 0:
        return set(selected)

    unpinned = [row for row in eligible if row.maintenance.get("pinned") is not True]
    pct = max(0, min(100, int(explore_percent)))
    if pct == 0:
        unpinned.sort(key=_legacy_sort_key)
        return set(selected + [row.name for row in unpinned[:remaining]])

    explore_slots = min(remaining, math.ceil(remaining * pct / 100.0))
    proven_slots = remaining - explore_slots

    explore_pool = [row for row in unpinned if _is_proven_never_used(row)]
    proven_pool = [row for row in unpinned if not _is_proven_never_used(row)]

    def _explore_sort_key(row: LearnedSkill) -> tuple[float, float, int, str]:
        created_ts = _parse_iso_epoch_cached(row.created_at)
        baseline_ts = float(row.maintenance.get("baseline_at") or 0.0)
        return (-max(created_ts, baseline_ts), -created_ts, -int(row.revision or 0), row.name)

    def _proven_sort_key(row: LearnedSkill) -> tuple[int, float, int, str]:
        maint = row.maintenance
        return (
            0 if maint.get("quality") == "established" else 1,
            -float(maint.get("last_used_at") or 0.0),
            -int(maint.get("independent_uses") or 0),
            row.name,
        )

    explore_pool.sort(key=_explore_sort_key)
    proven_pool.sort(key=_proven_sort_key)

    picked_explore = explore_pool[:explore_slots]
    picked_proven = proven_pool[:proven_slots]
    left_explore = explore_pool[explore_slots:]
    left_proven = proven_pool[proven_slots:]

    picked = [row.name for row in picked_explore] + [row.name for row in picked_proven]
    shortfall = remaining - len(picked)
    if shortfall > 0:
        fill = left_proven[:shortfall]
        picked.extend(row.name for row in fill)
        shortfall -= len(fill)
    if shortfall > 0:
        picked.extend(row.name for row in left_explore[:shortfall])
    return set(selected + picked)


def _fold_skill_usage(
    store: SkillLearningStore,
    registry: SkillLearningRegistry,
    current: LearnedSkill,
    ev: dict | None,
    *,
    ts: float,
) -> tuple[SkillLearningRegistry, LearnedSkill, str]:
    """将单条Skill的UsageEvidence安全合入维护投影，返回(registry, updated_or_same, outcome_tag)。"""
    if current.layout == "owner" or current.maintenance.get("ownership") in {"owner_layout", "user_modified"}:
        return registry, current, "skipped"

    maint = dict(current.maintenance)
    if not maint.get("baseline_at"):
        maint["baseline_at"] = ts
    if "state" not in maint:
        maint["state"] = "trial"

    cur_inc = str(maint.get("incarnation_id") or "")
    cur_act = str(maint.get("activation_id") or "")
    cur_sha = str(current.sha256 or "").lower()
    bound_record = bool(cur_inc and cur_act and cur_sha)

    if isinstance(ev, dict):
        ev_ever = ev.get("ever_used")
        if ev_ever is True:
            maint["ever_used"] = True
        elif ev_ever is False:
            if maint.get("ever_used") is not True:
                maint["ever_used"] = False
        else:
            if maint.get("ever_used") is not True:
                maint["ever_used"] = None

        cov = str(ev.get("independent_uses_coverage") or "")
        if cov in {"exact", "lower_bound", "unknown"}:
            maint["independent_uses_coverage"] = cov

        last_real = ev.get("last_real_used_at")
        if isinstance(last_real, (int, float)) and float(last_real) > float(maint.get("last_used_at") or 0.0):
            maint["last_used_at"] = float(last_real)

        upper = ev.get("last_activity_at_upper_bound")
        if isinstance(upper, (int, float)) and float(upper) > 0.0:
            prev_upper = float(maint.get("legacy_last_activity_upper_bound") or 0.0)
            maint["legacy_last_activity_upper_bound"] = max(prev_upper, float(upper))

        ev_inc = str(ev.get("incarnation_id") or "")
        ev_act = str(ev.get("activation_id") or "")
        ev_sha = str(ev.get("content_sha256") or "").lower()
        identity_matches = (
            bound_record
            and (not ev_inc or ev_inc == cur_inc)
            and (not ev_act or ev_act == cur_act)
            and (not ev_sha or ev_sha == cur_sha)
        )
        if identity_matches:
            ev_seq_raw = ev.get("usage_receipt_sequence")
            ev_hurt_raw = ev.get("hurt_count")
            ev_epoch_raw = ev.get("strike_epoch")
            cur_seq = int(maint.get("usage_receipt_sequence") or 0)
            cur_hurt = int(maint.get("hurt_count") or 0)
            cur_epoch = int(maint.get("strike_epoch") or 0)

            if isinstance(ev.get("independent_uses"), int):
                maint["independent_uses"] = max(int(maint.get("independent_uses") or 0), int(ev["independent_uses"]))

            ev_seq = int(ev_seq_raw) if isinstance(ev_seq_raw, int) else None
            ev_hurt = int(ev_hurt_raw) if isinstance(ev_hurt_raw, int) else None
            ev_epoch = int(ev_epoch_raw) if isinstance(ev_epoch_raw, int) else cur_epoch
            should_sync = (ev_seq is None or ev_seq >= cur_seq) and (
                ev_epoch > cur_epoch or (ev_hurt is not None and ev_hurt >= cur_hurt) or ev_hurt is None
            )
            if should_sync:
                if ev_hurt is not None and ev_hurt >= cur_hurt:
                    maint["hurt_count"] = ev_hurt
                if ev_epoch >= cur_epoch:
                    maint["strike_epoch"] = ev_epoch
                    if isinstance(ev.get("uses_since_hurt"), int):
                        if ev_epoch > cur_epoch:
                            maint["uses_since_hurt"] = int(ev["uses_since_hurt"])
                        else:
                            maint["uses_since_hurt"] = max(int(maint.get("uses_since_hurt") or 0), int(ev["uses_since_hurt"]))
                if isinstance(ev.get("epoch_started_at"), (int, float)) and float(ev["epoch_started_at"]) > 0.0:
                    maint["epoch_started_at"] = float(ev["epoch_started_at"])
                if ev_seq is not None:
                    maint["usage_receipt_sequence"] = max(cur_seq, ev_seq)

    updated = replace(current, maintenance=maint) if maint != current.maintenance else current
    if bound_record and int(maint.get("hurt_count") or 0) >= MAX_STRIKE_COUNT:
        try:
            _owned_record(store, replace(registry, skills={**registry.skills, current.name: updated}), current.name)
        except SkillLearningGateError:
            maint["ownership"] = "user_modified"
            handed = replace(current, maintenance=maint)
            return replace(registry, skills={**registry.skills, current.name: handed}), handed, "changed"
        res = _execute_third_strike_locked(
            store,
            replace(registry, skills={**registry.skills, current.name: updated}),
            updated,
            now=ts,
        )
        fresh = store.load_registry()
        if res.get("action") == "retired":
            return fresh, updated, "retired"
        return fresh, fresh.skills.get(current.name, updated), "changed"

    hurt_now = max(0, int(maint.get("hurt_count") or 0))
    uses_now = max(0, int(maint.get("uses_since_hurt") or 0))
    if bound_record and hurt_now < MAX_STRIKE_COUNT and uses_now >= BASE_ESTABLISH_USES_COUNT + hurt_now:
        if maint.get("state") in {"trial", "active"} and maint.get("quality") != "established":
            maint["quality"] = "established"
            maint["quality_basis"] = "uses_threshold"
            maint["established_at"] = float(maint.get("established_at") or ts)
            if maint.get("state") == "trial":
                maint["promotion_history"] = [*maint.get("promotion_history", []), "active"][-MAX_PROMOTION_HISTORY_COUNT:]
            maint["state"] = "active"
            updated = replace(current, maintenance=maint)

    if updated is not current:
        return replace(registry, skills={**registry.skills, current.name: updated}), updated, "changed"
    return registry, current, "unchanged"


def _verify_single_ownership(store: SkillLearningStore, current: LearnedSkill) -> str:
    """只核验一条托管记录在磁盘上的真实所有权；任何读取异常标为unknown而不中断整批维护。"""
    if current.layout == "owner" or current.maintenance.get("ownership") == "owner_layout":
        return "owner_layout"
    if current.maintenance.get("ownership") == "user_modified":
        return "user_modified"
    is_archived = current.maintenance.get("state") == "archived"
    anchor = store.directory if is_archived else store.skills_root
    dir_parts = ("archived", current.name) if is_archived else ("learned", current.name)
    dir_exists, has_extra = _has_extra_dir_entries(anchor, dir_parts)
    if dir_exists and has_extra:
        return "user_modified"
    if not dir_exists:
        # 目录缺失或已清空且无未决操作时交还所有权、释放托管槽位；读取或链接异常仍保留未知状态。
        return "unknown" if has_extra else "user_modified"
    try:
        text = read_text_beneath(anchor, (*dir_parts, "SKILL.md"))
    except (NoFollowPathError, OSError, UnicodeDecodeError, ValueError):
        return "unknown"
    if text is None:
        return "user_modified"
    if skill_content_sha256(text) != current.sha256:
        return "user_modified"
    return "machine"


def _make_budget(cfg: object | None, *, batch_count: int, batch_bytes: int, batch_ms: int) -> tuple[RetentionScanBudget, int]:
    default_budget = RetentionScanBudget.default(cfg)
    eff_count = int(batch_count) if batch_count > 0 else default_budget.max_entries
    eff_bytes = int(batch_bytes) if batch_bytes > 0 else default_budget.max_bytes
    if batch_ms > 0:
        eff_ms = int(batch_ms)
        deadline = time.monotonic() + (eff_ms / 1000.0)
    else:
        deadline = default_budget.deadline
        eff_ms = max(1, int(round((deadline - time.monotonic()) * 1000.0)))
    return RetentionScanBudget(max_entries=eff_count, max_bytes=eff_bytes, deadline=deadline), eff_ms


def _checkpoint_cursor(
    store: SkillLearningStore,
    registry: SkillLearningRegistry,
    *,
    phase: str,
    after_name: str = "",
    after_entry: str = "",
    started_at: float,
) -> dict[str, object]:
    cursor: dict[str, object] = {
        "phase": phase,
        "started_at": float(started_at),
    }
    if after_name:
        cursor["after_name"] = after_name
    if after_entry:
        cursor["after_entry"] = after_entry
    store.save_registry(replace(registry, maintenance_cursor=cursor))
    return cursor


# LLM: 自动维护只读唯一registry并使用独立last_maintenance_at与游标；先合入使用证据、再核权、再执行独立7/15/15周期与50槽位淘汰、
#   再清理版本全文与已退役SQL回执；observe模式绝不做目录移动或删改，证据不完整与未知历史一律保守暂停清理。
# 函数用途: 按单批有界预算分阶段推进学习资产维护，支持任意中断位点续跑。
def maintain_learning(
    store,
    *,
    mode='observe',
    cool_seconds=0,
    archive_seconds=0,
    protected_ids=(),
    protection_complete=False,
    now=None,
    keep_versions=5,
    usage_loader=None,
    max_skills=0,
    evict_seconds=0,
    batch_count=0,
    batch_bytes=0,
    batch_ms=0,
    config_provider=None,
) -> dict[str, object]:
    if mode not in {'observe', 'autonomous'}:
        raise ValueError('维护策略无效')
    ts = time.time() if now is None else float(now)
    cfg = None
    if config_provider is not None:
        try:
            cfg = config_provider()
        except Exception as exc:
            logging.getLogger(__name__).warning("maintain_learning config_provider failed: %s", exc)
            cfg = None

    evidence: dict[str, object]
    loader = load_usage_evidence if usage_loader is None else usage_loader
    try:
        loaded = loader(store)
        evidence = dict(loaded) if isinstance(loaded, dict) else {}
    except Exception as exc:  # noqa: BLE001 - 证据读取故障时保持保守未知状态
        logging.getLogger(__name__).warning("maintain_learning usage_loader failed: %s", exc)
        evidence = {}

    ev_coverage = dict(evidence.get("coverage") or {}) if evidence else {
        "lifetime_summary": "unavailable",
        "delta_events": "unavailable",
        "version_attribution": "unavailable",
        "distinct_receipts": "unavailable",
    }
    ev_by_skill = dict(evidence.get("by_skill") or {})
    usage_sufficient = ev_coverage.get("lifetime_summary") == "complete"

    budget, budget_ms = _make_budget(cfg, batch_count=batch_count, batch_bytes=batch_bytes, batch_ms=batch_ms)
    protected = {str(item) for item in protected_ids}
    changed: list[str] = []
    evicted: list[str] = []
    would_cool: list[str] = []
    would_archive: list[str] = []
    would_evict: list[str] = []
    removed_versions = 0
    receipts_scanned = 0
    receipts_deleted = 0
    explicit_batch = (batch_count > 0 or batch_bytes > 0 or batch_ms > 0)
    did_any_work = False

    def _hit_budget(did_step: bool) -> bool:
        if not did_step:
            return False
        if explicit_batch:
            return budget.entries >= budget.max_entries or budget.exhausted()
        return budget.exhausted()

    def _result(*, backlog: bool, next_cursor: dict[str, object]) -> dict[str, object]:
        return {
            "changed": list(dict.fromkeys(changed)),
            "protection_complete": bool(protection_complete),
            "mode": mode,
            "removed_versions": removed_versions,
            "would_cool": list(dict.fromkeys(would_cool)),
            "would_archive": list(dict.fromkeys(would_archive)),
            "would_evict": list(dict.fromkeys(would_evict)),
            "evicted": list(dict.fromkeys(evicted)),
            "backlog": backlog,
            "cursor": dict(next_cursor),
            "coverage": {
                **ev_coverage,
                "protection": "complete" if protection_complete else "incomplete",
                "usage_sufficient": usage_sufficient,
            },
            "receipts_scanned": receipts_scanned,
            "receipts_deleted": receipts_deleted,
        }

    with store.state_lock():
        registry = store.load_registry()
        cursor = dict(registry.maintenance_cursor or {})
        phase = str(cursor.get("phase") or "recover")
        started_at = float(cursor.get("started_at") or ts)
        after_name = str(cursor.get("after_name") or "")
        after_entry = str(cursor.get("after_entry") or "")

        if mode == "observe":
            # observe模式绝不做目录搬迁、状态写坏或旧文件/回执清理；仅安全推演planned changes。
            for name, current in sorted(registry.skills.items()):
                maint = dict(current.maintenance)
                ev = ev_by_skill.get(name)
                ever_used = maint.get("ever_used")
                last_used = float(maint.get("last_used_at") or 0.0)
                if isinstance(ev, dict):
                    if ev.get("ever_used") is not None and ever_used is not True:
                        ever_used = ev.get("ever_used")
                    if isinstance(ev.get("last_real_used_at"), (int, float)):
                        last_used = max(last_used, float(ev["last_real_used_at"]))
                baseline = float(maint.get("baseline_at") or ts)
                restored = float(maint.get("restored_at") or 0.0)
                idle = ts - max(baseline, last_used, restored)
                is_prot = (
                    current.layout == "owner"
                    or maint.get("ownership") in {"owner_layout", "user_modified", "unknown"}
                    or maint.get("pinned") is True
                    or "*" in protected
                    or name in protected
                    or f"owner:{name}" in protected
                    or maint.get("state") == "disabled"
                )
                if is_prot or not usage_sufficient or ever_used is None:
                    continue
                state = maint.get("state", "trial")
                if state == "archived":
                    archived_at = float(maint.get("archived_at") or 0.0)
                    if evict_seconds > 0 and archived_at > 0.0 and (ts - archived_at) >= float(evict_seconds):
                        would_evict.append(name)
                elif archive_seconds > 0 and idle >= float(archive_seconds):
                    would_archive.append(name)
                elif cool_seconds > 0 and idle >= float(cool_seconds) and state in {"trial", "active"}:
                    would_cool.append(name)
            store.save_registry(replace(registry, last_maintenance_at=ts, maintenance_cursor={}))
            return _result(backlog=False, next_cursor={})

        # Phase 1: recover pending operations first
        if registry.pending_operation is not None or phase == "recover":
            registry, rec_outcome = store._recover_lifecycle_locked(
                registry,
                batch_count=budget.max_entries,
                batch_bytes=budget.max_bytes,
                batch_ms=budget_ms,
                now=ts,
            )
            if not rec_outcome.get("completed", True):
                next_cur = _checkpoint_cursor(
                    store,
                    registry,
                    phase="recover",
                    started_at=started_at,
                )
                return _result(backlog=True, next_cursor=next_cur)
            phase = "usage"
            after_name = ""
            after_entry = ""

        # Phase 2: usage folding & 3/4/5 promotion / crash recovery
        if phase == "usage":
            for name in sorted(registry.skills):
                if after_name and name <= after_name:
                    continue
                if _hit_budget(did_any_work):
                    next_cur = _checkpoint_cursor(
                        store,
                        registry,
                        phase="usage",
                        after_name=after_name,
                        started_at=started_at,
                    )
                    return _result(backlog=True, next_cursor=next_cur)
                current = registry.skills.get(name)
                if current is not None:
                    registry, _updated, outcome = _fold_skill_usage(
                        store,
                        registry,
                        current,
                        ev_by_skill.get(name) if isinstance(ev_by_skill.get(name), dict) else None,
                        ts=ts,
                    )
                    if outcome == "retired":
                        evicted.append(name)
                        changed.append(name)
                    elif outcome == "changed":
                        changed.append(name)
                after_name = name
                did_any_work = True
                budget.charge(entries=1, size=256)
            store.save_registry(registry)
            phase = "ownership"
            after_name = ""

        # Phase 3: per-skill disk ownership check
        if phase == "ownership":
            for name in sorted(registry.skills):
                if after_name and name <= after_name:
                    continue
                if _hit_budget(did_any_work):
                    next_cur = _checkpoint_cursor(
                        store,
                        registry,
                        phase="ownership",
                        after_name=after_name,
                        started_at=started_at,
                    )
                    return _result(backlog=True, next_cursor=next_cur)
                current = registry.skills.get(name)
                if current is not None:
                    owned_status = _verify_single_ownership(store, current)
                    inc_id = str(current.maintenance.get("incarnation_id") or "").strip()
                    act_id = str(current.maintenance.get("activation_id") or "").strip()
                    if current.layout != "owner" and owned_status == "machine" and (not inc_id or not act_id):
                        # 仅在写授权的维护路径内为仍属机器托管的遗留记录补齐唯一标识，不伪造历史精确计数
                        next_maint = dict(current.maintenance)
                        hurt_raw = next_maint.get("hurt_count")
                        epoch_raw = next_maint.get("strike_epoch")
                        has_prior_activity = bool(next_maint.get("ever_used") or next_maint.get("last_used_at"))
                        next_maint.update({
                            "ownership": owned_status,
                            "incarnation_id": inc_id or uuid.uuid4().hex,
                            "activation_id": act_id or uuid.uuid4().hex,
                            "independent_uses": 0,
                            "uses_since_hurt": 0,
                            "hurt_count": hurt_raw if isinstance(hurt_raw, int) and not isinstance(hurt_raw, bool) and hurt_raw >= 0 else 0,
                            "strike_epoch": epoch_raw if isinstance(epoch_raw, int) and not isinstance(epoch_raw, bool) and epoch_raw >= 0 else 0,
                            "epoch_started_at": float(next_maint.get("epoch_started_at") or started_at),
                            "independent_uses_coverage": "lower_bound" if has_prior_activity else "exact",
                        })
                        registry = replace(
                            registry,
                            skills={**registry.skills, name: replace(current, maintenance=next_maint)},
                        )
                        changed.append(name)
                    elif current.maintenance.get("ownership") != owned_status:
                        next_maint = {**current.maintenance, "ownership": owned_status}
                        registry = replace(
                            registry,
                            skills={**registry.skills, name: replace(current, maintenance=next_maint)},
                        )
                after_name = name
                did_any_work = True
                budget.charge(entries=1, size=256)
            store.save_registry(registry)
            phase = "lifecycle"
            after_name = ""

        # Phase 4: independent 7d/15d/15d idle transitions + max_skills cap displacement
        if phase == "lifecycle":
            for name in sorted(registry.skills):
                if after_name and name <= after_name:
                    continue
                if _hit_budget(did_any_work):
                    store.save_registry(registry)
                    next_cur = _checkpoint_cursor(
                        store,
                        registry,
                        phase="lifecycle",
                        after_name=after_name,
                        started_at=started_at,
                    )
                    return _result(backlog=True, next_cursor=next_cur)

                current = registry.skills.get(name)
                if current is not None:
                    maint = dict(current.maintenance)
                    if not maint.get("baseline_at"):
                        maint["baseline_at"] = ts
                        current = replace(current, maintenance=maint)
                        registry = replace(registry, skills={**registry.skills, name: current})

                    ownership = str(maint.get("ownership") or "machine")
                    is_protected = (
                        current.layout == "owner"
                        or ownership in {"owner_layout", "user_modified", "unknown"}
                        or maint.get("pinned") is True
                        or "*" in protected
                        or name in protected
                        or f"owner:{name}" in protected
                        or maint.get("state") == "disabled"
                    )
                    can_destruct = bool(
                        not is_protected
                        and protection_complete
                        and usage_sufficient
                        and maint.get("ever_used") is not None
                    )
                    last_activity = max(
                        float(maint.get("baseline_at") or 0.0),
                        float(maint.get("last_used_at") or 0.0),
                        float(maint.get("restored_at") or 0.0),
                    )
                    idle = ts - last_activity
                    state = str(maint.get("state") or "trial")

                    if can_destruct:
                        if state == "archived":
                            archived_at = float(maint.get("archived_at") or 0.0)
                            if evict_seconds > 0 and archived_at > 0.0 and (ts - archived_at) >= float(evict_seconds):
                                try:
                                    registry = store._transition_evict_locked(
                                        registry,
                                        name,
                                        now=ts,
                                        block=False,
                                        reason_code="idle_expired",
                                        cleanup_budget=(budget.max_entries, budget.max_bytes, budget_ms),
                                    )
                                    evicted.append(name)
                                    changed.append(name)
                                except SkillLearningStoreError as exc:
                                    logging.getLogger(__name__).warning("idle evict skipped for %s: %s", name, exc)
                                    registry = store.load_registry()
                        elif archive_seconds > 0 and idle >= float(archive_seconds):
                            try:
                                registry, _ = store._transition_archive_locked(
                                    registry,
                                    name,
                                    now=ts,
                                    reason_code="idle_archive",
                                )
                                changed.append(name)
                            except SkillLearningStoreError as exc:
                                logging.getLogger(__name__).warning("idle archive skipped for %s: %s", name, exc)
                                registry = store.load_registry()
                        elif cool_seconds > 0 and idle >= float(cool_seconds) and state in {"trial", "active"}:
                            maint.update(state="cooled", cooled_at=maint.get("cooled_at") or ts)
                            registry = replace(
                                registry,
                                skills={**registry.skills, name: replace(current, maintenance=maint)},
                            )
                            changed.append(name)

                after_name = name
                did_any_work = True
                budget.charge(entries=1, size=256)

            store.save_registry(registry)

            # Cap enforcement after idle lifecycle pass
            if max_skills > 0 and protection_complete and usage_sufficient:
                partial_managed: dict[str, LearnedSkill] = {}
                for cname, rec in sorted(registry.skills.items()):
                    if rec.layout == "owner" or rec.maintenance.get("state") == "archived":
                        continue
                    if rec.maintenance.get("ownership") in {"owner_layout", "user_modified", "unknown"}:
                        continue
                    if rec.maintenance.get("ever_used") is None:
                        continue
                    partial_managed[cname] = rec
                live_count = registry.live_count(
                    managed_names={
                        cname for cname, rec in registry.skills.items()
                        if rec.layout != "owner"
                        and rec.maintenance.get("state") != "archived"
                        and rec.maintenance.get("ownership") not in {"owner_layout", "user_modified"}
                    }
                )
                while live_count > max_skills:
                    if _hit_budget(did_any_work):
                        next_cur = _checkpoint_cursor(
                            store,
                            registry,
                            phase="lifecycle",
                            after_name=after_name,
                            started_at=started_at,
                        )
                        return _result(backlog=True, next_cursor=next_cur)
                    victim = _select_cap_victim_locked(store, registry, partial_managed, protected=protected)
                    if victim is None:
                        break
                    try:
                        registry, _ = store._transition_archive_locked(
                            registry,
                            victim,
                            now=ts,
                            reason_code="cap_enforce",
                        )
                        changed.append(victim)
                        partial_managed.pop(victim, None)
                        live_count -= 1
                        did_any_work = True
                        budget.charge(entries=1, size=256)
                    except SkillLearningStoreError:
                        partial_managed.pop(victim, None)
                        registry = store.load_registry()

            phase = "versions"
            after_name = ""

        # Phase 5: bounded version GC
        if phase == "versions":
            if protection_complete and keep_versions > 0:
                for name in sorted(registry.skills):
                    if after_name and name <= after_name:
                        continue
                    if _hit_budget(did_any_work):
                        next_cur = _checkpoint_cursor(
                            store,
                            registry,
                            phase="versions",
                            after_name=after_name,
                            started_at=started_at,
                        )
                        return _result(backlog=True, next_cursor=next_cur)
                    record = registry.skills.get(name)
                    if record is not None and not (
                        "*" in protected
                        or name in protected
                        or f"owner:{name}" in protected
                        or record.maintenance.get("pinned")
                        or record.maintenance.get("state") == "disabled"
                        or record.maintenance.get("ownership") in {"owner_layout", "user_modified", "unknown"}
                    ):
                        removed_versions += _collect_unreferenced_versions_for_skill(store, record, keep_versions)
                    after_name = name
                    did_any_work = True
                    budget.charge(entries=1, size=256)
            phase = "receipts"
            after_name = ""

        # Phase 6: bounded cleanup of retired SQLite exact receipts (use -> hurt -> aggregate anchor last)
        if phase == "receipts":
            runtime_repo = store._runtime_repository()
            if runtime_repo is not None:
                if _hit_budget(did_any_work):
                    next_cur = _checkpoint_cursor(
                        store,
                        registry,
                        phase="receipts",
                        after_entry=after_entry,
                        started_at=started_at,
                    )
                    return _result(backlog=True, next_cursor=next_cur)
                keep_pairs: set[tuple[str, str]] = set()
                for record in registry.skills.values():
                    inc = str(record.maintenance.get("incarnation_id") or "").strip()
                    act = str(record.maintenance.get("activation_id") or "").strip()
                    if inc and act:
                        keep_pairs.add((inc, act))
                rem_entries = max(1, budget.max_entries - budget.entries)
                cleanup_res = cleanup_retired_aggregates(
                    runtime_repo,
                    keep_pairs=keep_pairs,
                    after_entry=after_entry,
                    max_entries=rem_entries,
                    deadline=budget.deadline,
                )
                receipts_scanned += int(cleanup_res.get("scanned") or 0)
                receipts_deleted += int(cleanup_res.get("deleted") or 0)
                if not cleanup_res.get("complete", True):
                    next_cur = _checkpoint_cursor(
                        store,
                        registry,
                        phase="receipts",
                        after_entry=str(cleanup_res.get("after_entry") or ""),
                        started_at=started_at,
                    )
                    return _result(backlog=True, next_cursor=next_cur)

        # Phase 7: done -> clear cursor & advance last_maintenance_at
        store.save_registry(replace(registry, last_maintenance_at=ts, maintenance_cursor={}))

    if changed:
        store.append_event(SkillLearningEvent('maintained', reason=','.join(changed)[:200]))
    return _result(backlog=False, next_cursor={})


def _collect_unreferenced_versions_for_skill(store: SkillLearningStore, record: LearnedSkill, keep_versions: int) -> int:
    """清理单条Skill未被当前版本、已证实上一稳定版及最近N份修订引用的旧正文。"""
    if keep_versions <= 0:
        return 0
    ver_dir = store.versions_dir / record.name
    if not ver_dir.exists() or ver_dir.is_symlink():
        return 0
    paths = [path for path in ver_dir.glob('v*.md') if path.stem[1:].isdigit()]
    paths.sort(key=lambda path: int(path.stem[1:]))
    if any(path.is_symlink() for path in paths):
        return 0
    stable = record.maintenance.get('previous_stable')
    keep = {path.name for path in paths[-keep_versions:]} | {f'v{record.version}.md'}
    if isinstance(stable, dict) and stable.get('version') is not None:
        keep.add(f"v{stable.get('version')}.md")
    removed = 0
    for path in paths:
        if path.name not in keep:
            path.unlink()
            removed += 1
    return removed
