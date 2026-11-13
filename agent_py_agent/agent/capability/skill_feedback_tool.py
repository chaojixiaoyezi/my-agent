# LLM: status 只读本 owner 原登记与当前快照，不暴露 pending_learning_proposals；
#   adopted/remove/restore/ineffective 均须核实当前主会话 claim（拒绝子代理/隔离域）与版本绑定，不绕过人工或宿主约束。
# 模块用途: 为主会话提供自治技能状态查询、任务采用标记、静默无效负反馈（通道 A）以及显式移除/恢复控制工具。
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from functools import partial
from importlib import import_module
from inspect import signature
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import runtime_context
from ..runtime_db.execution_mode import ExecutionMode, resolve_execution_mode
from ..runtime_db.schema import runtime_db_path
from ..settings.live_switches import refresh_live_switches
from ..tooling.models import (
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ResourceScopePolicy,
    ToolHandlerOutcome,
    ToolModelSpec,
    ToolRuntimePolicy,
)
from ..user_space.owner_quota import OwnerQuotaExceeded, OwnerQuotaUnavailable
from .skill_learning_maintenance import (
    _HOST_DELIVERY_KEYS_CTX,
    MAX_STRIKE_COUNT,
    LearningObservationSource,
    control_learning,
    observe_learning,
)
from .skill_learning_protection import learning_protection_for_agent
from .skill_learning_publish import (
    CODE_LIMIT_REACHED,
    CODE_NOT_LEARNED,
    CODE_USER_MODIFIED,
    SkillLearningGateError,
)
from .skill_learning_report import apply_learned_skill_action, learned_skill_row
from .skill_learning_store import (
    CODE_ARCHIVE_EXPIRED,
    CODE_EVIDENCE_INCOMPLETE,
    SkillLearningStore,
    SkillLearningStoreError,
)
from .skill_search_tool import _snapshot_for
from .skill_usage_ledger import learning_usage_binding
from .skill_usage_receipts import candidate_delivery_task_keys, read_usage_aggregate

if TYPE_CHECKING:
    from ..runtime_db.repository import RuntimeRepository

_RESTRICTED_SCOPES = frozenset({"task_local", "control_plane", "isolated"})
_MUTATING_ACTIONS = frozenset({"adopted", "remove", "restore", "ineffective"})
_ALL_ACTIONS = ("status", "adopted", "remove", "restore", "ineffective")
_NON_OWNER_PREFIXES = ("builtin:", "workspace:", "shared:")


# LLM: 只表达纯参数形状错误；错误信息固定且不回显非预期输入正文。
# 类用途: 区分输入校验拒绝与权限或存储故障。
class _FeedbackArgumentsError(ValueError):
    pass


# LLM: 区分执行身份/claim 不足与参数或权限越界，保证全部拒绝均在写前收口（not_started）。
# 类用途: 在主会话 claim 缺失或失效时中止变更操作。
class _FeedbackClaimError(PermissionError):
    pass


# LLM: 统一校验唯一 action 集合及必填 skill_id，禁止未识别键或隐式操作别名透传到存储层。
# 函数用途: 在取得任何 claim 或触碰存储前校验工具参数。
def _feedback_operation(params: object) -> str:
    if not isinstance(params, dict) or set(params) - {"action", "skill_id"}:
        raise _FeedbackArgumentsError("参数须为对象，仅支持action、skill_id。")
    operation = params.get("action", "status")
    if operation not in _ALL_ACTIONS:
        raise _FeedbackArgumentsError(
            "动作必须为 status / adopted / remove / restore / ineffective。"
        )
    if not isinstance(params.get("skill_id", ""), str):
        raise _FeedbackArgumentsError("skill_id 必须为字符串。")
    if operation in _MUTATING_ACTIONS and not params.get("skill_id", "").strip():
        raise _FeedbackArgumentsError(f"action={operation} 时必须提供 skill_id")
    return operation


def _strip_owner_prefix(skill_id: str) -> str:
    text = skill_id.strip()
    if text.startswith("owner:"):
        return text[len("owner:") :]
    return text


def _non_owner_prefix(skill_id: str) -> str:
    text = skill_id.strip()
    for prefix in _NON_OWNER_PREFIXES:
        if text.startswith(prefix):
            return prefix[:-1]
    return ""


def _refresh_agent_config(agent: object) -> object:
    cfg = getattr(agent, "config", None)
    if cfg is not None:
        try:
            refresh_live_switches(cfg)
        except OSError:
            pass
    return cfg


def _is_child_or_restricted_context(
    agent: object,
    *,
    scope: object | None = None,
    context: object | None = None,
) -> bool:
    if runtime_context.current_subagent_run_id(agent):
        return True
    current = getattr(agent, "_current_run_params", None)
    if str(getattr(current, "context_scope", "") or "") in _RESTRICTED_SCOPES:
        return True
    attrs = getattr(current, "task_attributes", None)
    if isinstance(attrs, dict) and attrs.get("agent_run_id"):
        return True
    if scope is not None and str(getattr(scope, "agent_kind", "") or "") != "main":
        return True
    if context is not None:
        if bool(getattr(context, "child", False)):
            return True
        if str(getattr(context, "context_scope", "") or "") in _RESTRICTED_SCOPES:
            return True
        if str(getattr(context, "subagent_run_id", "") or ""):
            return True
    return False


def _agent_usage_runtime_provider(agent: object):
    home = getattr(agent, "home_paths", None)
    owner_home_str = str(getattr(home, "owner_home_dir", "") or "").strip()
    subagents = getattr(agent, "subagents", None)
    explicit = getattr(subagents, "execution_mode", None) if subagents is not None else None
    if explicit in (None, ""):
        explicit = getattr(getattr(agent, "config", None), "execution_mode", None)
    mode = resolve_execution_mode(explicit, has_home_dir=bool(owner_home_str))
    if mode == ExecutionMode.LOCAL_UNMANAGED:
        return lambda: None

    base_repo = getattr(agent, "runtime_db", None) or getattr(subagents, "runtime_db", None)
    if base_repo is not None:
        return lambda repo=base_repo: repo

    if not owner_home_str:
        return lambda: None
    expected = runtime_db_path(Path(owner_home_str)).expanduser().resolve(strict=False)
    cached: list[RuntimeRepository] = []

    def _provider() -> RuntimeRepository | None:
        if cached:
            return cached[0]
        if os.path.islink(expected):
            raise SkillLearningStoreError(CODE_EVIDENCE_INCOMPLETE, "runtime repository path is symlink")
        if not expected.is_file():
            return None
        from ..runtime_db.repository import RuntimeRepository

        repo = RuntimeRepository(expected, existing_only=True)
        cached.append(repo)
        return repo

    return _provider


def _store_for(agent: object) -> SkillLearningStore:
    home_paths = getattr(agent, "home_paths", None)
    if home_paths is None:
        raise SkillLearningStoreError(CODE_EVIDENCE_INCOMPLETE, "owner home_paths unavailable")
    return SkillLearningStore.for_home(
        home_paths,
        usage_runtime_provider=_agent_usage_runtime_provider(agent),
    )


def _as_non_neg_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


def _as_non_neg_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and float(value) >= 0.0:
        return float(value)
    return None


def _config_non_neg_int(cfg: object, key: str, default: int) -> int:
    raw = getattr(cfg, key, default)
    parsed = _as_non_neg_int(raw)
    return parsed if parsed is not None else default


def _build_coverage(status: str, reason: str, *, as_of: float | str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"status": status, "reason": reason}
    if isinstance(as_of, str) and as_of:
        out["as_of"] = as_of
    elif as_of is not None and not isinstance(as_of, bool):
        out["as_of"] = datetime.fromtimestamp(float(as_of), timezone.utc).isoformat()
    return out


def _lifecycle_from_record(
    store: SkillLearningStore,
    record: Any,
    cfg: object,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    import time

    ts = time.time() if now is None else float(now)
    maint = dict(getattr(record, "maintenance", {}) or {})
    layout = str(getattr(record, "layout", "learned") or "learned")
    ownership = str(maint.get("ownership") or "")
    raw_state = str(maint.get("state") or "trial")
    if layout == "owner" or ownership == "user_modified":
        state = "user_owned"
    elif raw_state in {"trial", "active", "cooled", "archived", "disabled", "retired"}:
        state = raw_state
    else:
        state = "unknown"

    cur_inc = str(maint.get("incarnation_id") or "")
    cur_act = str(maint.get("activation_id") or "")
    sha256 = str(getattr(record, "sha256", "") or "")
    bound = bool(cur_inc and cur_act and sha256)

    agg: dict[str, Any] | None = None
    repo_error = ""
    if bound:
        try:
            runtime_repo = store._runtime_repository()
            if runtime_repo is not None:
                agg = read_usage_aggregate(
                    runtime_repo,
                    incarnation_id=cur_inc,
                    activation_id=cur_act,
                    content_sha256=sha256,
                )
        except Exception as exc:  # noqa: BLE001 - 只读投影遇到运行库异常时降级为部分已知，不伪造零计数。
            repo_error = type(exc).__name__

    if state == "user_owned":
        uses_since_hurt: int | None = None
        hurt_count: int | None = None
        required_uses: int | None = None
        ind_cov = "unknown"
        ind_uses = None
        coverage = _build_coverage("complete", "user_owned_exempt", as_of=ts)
    elif repo_error:
        uses_since_hurt = None
        hurt_count = None
        required_uses = None
        ind_cov = "unknown"
        ind_uses = _as_non_neg_int(maint.get("independent_uses"))
        coverage = _build_coverage("partial", f"runtime_usage_unreadable:{repo_error}", as_of=ts)
    elif agg is not None:
        hurt_count = _as_non_neg_int(agg.get("hurt_count"))
        uses_since_hurt = _as_non_neg_int(agg.get("uses_since_hurt"))
        required_uses = (MAX_STRIKE_COUNT + hurt_count) if hurt_count is not None else None
        raw_ind_cov = str(agg.get("coverage") or "exact")
        ind_cov = raw_ind_cov if raw_ind_cov in {"exact", "lower_bound", "unknown"} else "unknown"
        ind_uses = _as_non_neg_int(agg.get("independent_uses"))
        cov_status = "complete" if ind_cov == "exact" else "partial"
        coverage = _build_coverage(cov_status, "runtime_usage_aggregate", as_of=ts)
    elif bound:
        hurt_count = _as_non_neg_int(maint.get("hurt_count"))
        if hurt_count is None:
            hurt_count = 0
        uses_since_hurt = _as_non_neg_int(maint.get("uses_since_hurt"))
        if uses_since_hurt is None:
            uses_since_hurt = 0
        required_uses = MAX_STRIKE_COUNT + hurt_count
        raw_ind_cov = str(maint.get("independent_uses_coverage") or "exact")
        ind_cov = raw_ind_cov if raw_ind_cov in {"exact", "lower_bound", "unknown"} else "exact"
        ind_uses = _as_non_neg_int(maint.get("independent_uses"))
        cov_status = "complete" if ind_cov == "exact" else "partial"
        coverage = _build_coverage(cov_status, "registry_maintenance_facts", as_of=ts)
    else:
        uses_since_hurt = None
        hurt_count = None
        required_uses = None
        ind_cov = "unknown"
        ind_uses = _as_non_neg_int(maint.get("independent_uses"))
        coverage = _build_coverage("partial", "unbound_legacy_record", as_of=ts)

    cool_seconds = _config_non_neg_int(cfg, "self_learning_cool_seconds", 604800)
    archive_seconds = _config_non_neg_int(cfg, "self_learning_archive_seconds", 1296000)
    evict_seconds = _config_non_neg_int(cfg, "self_learning_evict_seconds", 1296000)
    last_activity = (
        _as_non_neg_float(maint.get("last_used_at"))
        or _as_non_neg_float(maint.get("baseline_at"))
        or 0.0
    )
    archived_at = _as_non_neg_float(maint.get("archived_at")) or 0.0
    pinned = bool(maint.get("pinned"))

    cool_in: float | None = None
    archive_in: float | None = None
    evict_in: float | None = None
    timer_reason = ""
    if state == "user_owned":
        timer_reason = "user_owned_exempt"
    elif pinned:
        timer_reason = "pinned_no_eviction"
    elif state in {"trial", "active"}:
        if cool_seconds > 0 and last_activity > 0.0:
            cool_in = max(0.0, float(cool_seconds) - max(0.0, ts - last_activity))
            timer_reason = "idle_cool_countdown"
        else:
            timer_reason = "cool_timer_disabled_or_no_baseline"
    elif state == "cooled":
        if archive_seconds > 0 and last_activity > 0.0:
            archive_in = max(0.0, float(archive_seconds) - max(0.0, ts - last_activity))
            timer_reason = "cooled_archive_countdown"
        else:
            timer_reason = "archive_timer_disabled_or_no_baseline"
    elif state == "archived":
        if evict_seconds > 0 and archived_at > 0.0:
            evict_in = max(0.0, float(evict_seconds) - max(0.0, ts - archived_at))
            timer_reason = "archived_evict_countdown"
        else:
            timer_reason = "evict_timer_disabled_or_no_archived_at"
    else:
        timer_reason = f"state_{state}"

    return {
        "state": state,
        "quality": str(maint.get("quality") or "") or None,
        "independent_uses": ind_uses,
        "uses_since_hurt": uses_since_hurt,
        "required_uses": required_uses,
        "hurt_count": hurt_count,
        "retire_at_hurt_count": MAX_STRIKE_COUNT,
        "independent_uses_coverage": ind_cov,
        "cool_in_seconds": cool_in,
        "archive_in_seconds": archive_in,
        "evict_in_seconds": evict_in,
        "timer_reason": timer_reason,
        "version": _as_non_neg_int(getattr(record, "version", None)),
        "coverage": coverage,
    }


def _non_learned_lifecycle() -> dict[str, Any]:
    return {
        "state": "user_owned",
        "quality": None,
        "independent_uses": None,
        "uses_since_hurt": None,
        "required_uses": None,
        "hurt_count": None,
        "retire_at_hurt_count": MAX_STRIKE_COUNT,
        "independent_uses_coverage": "unknown",
        "cool_in_seconds": None,
        "archive_in_seconds": None,
        "evict_in_seconds": None,
        "timer_reason": "non_learned_source",
        "version": None,
        "coverage": _build_coverage("unavailable", "non_learned_snapshot_source"),
    }


def _feedback_response(
    *,
    ok: bool,
    action: str,
    code: str,
    reason: str,
    side_effect_status: str,
    next_action: str,
    skill_id: str | None = None,
    lifecycle: dict[str, Any] | None = None,
    manual_path: str | None = None,
    archived_skills: list[dict[str, Any]] | None = None,
    legacy_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": bool(ok),
        "action": action,
        "code": code,
        "reason": reason,
        "side_effect_status": side_effect_status,
        "next_action": next_action,
    }
    if skill_id is not None:
        payload["skill_id"] = skill_id
    if lifecycle is not None:
        payload["lifecycle"] = lifecycle
    if manual_path is not None:
        payload["manual_path"] = manual_path
    if archived_skills is not None:
        payload["archived_skills"] = archived_skills
    if legacy_status is not None:
        payload["legacy_status"] = legacy_status
    return payload


def _error_outcome(
    *,
    action: str,
    error_code: str,
    response_code: str,
    reason: str,
    effect_outcome: str,
    side_effect_status: str,
    next_action: str,
    skill_id: str | None = None,
    manual_path: str | None = None,
    lifecycle: dict[str, Any] | None = None,
    output_text: str | None = None,
) -> ToolHandlerOutcome:
    envelope = _feedback_response(
        ok=False,
        action=action,
        code=response_code,
        reason=reason,
        side_effect_status=side_effect_status,
        next_action=next_action,
        skill_id=skill_id,
        lifecycle=lifecycle,
        manual_path=manual_path,
    )
    rendered = output_text if output_text is not None else json.dumps(envelope, ensure_ascii=False)
    return ToolHandlerOutcome(
        "skill_feedback",
        False,
        rendered,
        error_code=error_code,
        effect_outcome=effect_outcome,
        result_envelope={"skill_feedback": envelope},
    )


# LLM: 原登记仍是自学所有权权威；磁盘状态复用原报告推导，返回值不得暴露本机路径。
# 函数用途: 返回一个已发布技能的公开版本与维护状态。
def _registered_status(store: SkillLearningStore, record: Any) -> dict[str, Any]:
    row = learned_skill_row(store, record)
    skill = {key: value for key, value in row.items() if key != "path"}
    return {
        "ok": True,
        "skill_id": f"owner:{record.name}",
        "name": record.name,
        "sha256": record.sha256,
        "status": row["state"],
        "skill": skill,
        "maintenance": record.maintenance,
    }


# LLM: 可选 Store 方法优先，否则只借公开模块接口；模块缺失保持兼容，不新建第二使用账。
# 函数用途: 取得附带统计的同 owner 读写函数，不在此执行记账。
def _usage_interface(store: SkillLearningStore, method: str, function: str):
    callback = getattr(store, method, None)
    if callback is not None:
        return callback
    module_name = f"{__package__}.skill_usage_ledger"
    try:
        module = import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            return None
        raise
    return partial(getattr(module, function), store)


# LLM: 原报告可选摘要失败时明确未知；只读公共读口，不自动修复或确认内容。
# 函数用途: 给反馈状态附加原使用台账摘要，兼容 Store 和模块级接口。
def _with_usage_summary(store: SkillLearningStore, result: dict[str, Any]) -> dict[str, Any]:
    try:
        reader = _usage_interface(store, "usage_summary", "load_skill_usage_summary")
        if reader is not None:
            summary = reader() if callable(reader) else reader
            if isinstance(summary, dict):
                result["skill_usage"] = summary
                if "daily_history" in summary and "daily_history" not in result:
                    result["daily_history"] = summary["daily_history"]
    except Exception:  # noqa: BLE001 - 辅助统计失败不影响原状态查询。
        result["skill_usage_warning"] = "SKILL_USAGE_UNAVAILABLE"
    return result


# LLM: 新增队列回填参数时必须显式 refill=False；旧无参数接口本身只读，按签名选择，不靠异常重做。
# 函数用途: 计算热队列请求数且不触发任何恢复写入。
def _pending_request_count(store: SkillLearningStore) -> int:
    reader = store.pending_requests
    if "refill" in signature(reader).parameters:
        return len(reader(refill=False))
    return len(reader())


# LLM: V2-4 已退役 S1 人工确认提案列表；status 只读本 owner 原登记、使用摘要、已归档技能与实时策略。
# 函数用途: 返回本身份自学习技能的只读全景摘要，不创建存储目录、不消费队列、不确认内容。
def _feedback_overview(agent: object, store: SkillLearningStore, registry: Any) -> ToolHandlerOutcome:
    cfg = _refresh_agent_config(agent)
    skills = [_registered_status(store, registry.skills[name]) for name in sorted(registry.skills)]
    archived_skills = [
        {
            "skill_id": f"owner:{name}",
            "lifecycle": _lifecycle_from_record(store, registry.skills[name], cfg),
        }
        for name in sorted(registry.skills)
        if registry.skills[name].maintenance.get("state") == "archived"
    ]
    mode = str(getattr(cfg, "self_learning_maintenance_mode", "autonomous") or "autonomous").strip().lower()
    legacy = {
        "ok": True,
        "self_learning_enabled": bool(getattr(cfg, "enable_self_learning", False)),
        "self_learning_maintenance_mode": mode,
        "learned_skills_count": len(skills),
        "learned_skills": [
            {
                "name": row["name"],
                "version": row["skill"]["version"],
                "status": row["status"],
                "sha256": row["sha256"],
            }
            for row in skills
        ],
        "archived_skills_count": len(archived_skills),
        "pending_requests_count": _pending_request_count(store),
        "daily_summarization_calls": {
            "owner_date": registry.daily_date,
            "owner_calls": registry.daily_calls,
            "owner_daily_limit": int(getattr(cfg, "self_learning_daily_limit", 0) or 0),
            "note": "本身份后台自动总结 LLM 调用次数（非 skill_search 工具调用次数）",
        },
    }
    _with_usage_summary(store, legacy)
    envelope = _feedback_response(
        ok=True,
        action="status",
        code="OK",
        reason=f"已读取本身份 {len(skills)} 个已登记自学 Skill（其中归档 {len(archived_skills)} 个，维护模式 {mode}）。",
        side_effect_status="not_started",
        next_action="如需查看单个技能的打击计数与冷却/归档倒计时，请带 skill_id 再次调用 status。",
        archived_skills=archived_skills,
        legacy_status=legacy,
    )
    return ToolHandlerOutcome(
        "skill_feedback",
        True,
        json.dumps(legacy, ensure_ascii=False),
        effect_outcome="",
        result_envelope={"skill_feedback": envelope},
    )


def _feedback_status(agent: object, store: SkillLearningStore, reference: str) -> ToolHandlerOutcome:
    registry = store.load_registry()
    cleaned = reference.strip()
    if not cleaned:
        return _feedback_overview(agent, store, registry)

    cfg = _refresh_agent_config(agent)
    prefix = _non_owner_prefix(cleaned)
    name = _strip_owner_prefix(cleaned)
    if not prefix and name in registry.skills:
        record = registry.skills[name]
        legacy = _registered_status(store, record)
        lifecycle = _lifecycle_from_record(store, record, cfg)
        envelope = _feedback_response(
            ok=True,
            action="status",
            code="OK",
            reason=f"已读取 owner:{name} 当前生命周期状态（{lifecycle['state']}）。",
            side_effect_status="not_started",
            next_action="若本技能步骤在当前任务无效/误导请调用 action='ineffective'；若需归档移除或恢复可调用 action='remove'/'restore'。",
            skill_id=f"owner:{name}",
            lifecycle=lifecycle,
            manual_path=None,
            legacy_status=legacy,
        )
        return ToolHandlerOutcome(
            "skill_feedback",
            True,
            json.dumps(legacy, ensure_ascii=False),
            effect_outcome="",
            result_envelope={"skill_feedback": envelope},
        )

    snapshot = _snapshot_for(agent)
    entry = snapshot.resolve(cleaned) if snapshot is not None else None
    if entry is not None and (entry.source != "owner" or entry.name not in registry.skills):
        legacy_non_learned = {
            "ok": True,
            "skill_id": entry.stable_id,
            "name": entry.name,
            "source": entry.source,
            "status": "active_builtin_or_shared",
            "note": "该技能为内置或共享技能，非本身份自学习生成",
        }
        envelope = _feedback_response(
            ok=True,
            action="status",
            code="OK",
            reason="该技能属于内置、工作区或共享来源，不受自学生命周期管理。",
            side_effect_status="not_started",
            next_action="非自学习技能不支持 skill_feedback 写入操作。",
            skill_id=entry.stable_id,
            lifecycle=_non_learned_lifecycle(),
            manual_path=None,
            legacy_status=legacy_non_learned,
        )
        return ToolHandlerOutcome(
            "skill_feedback",
            True,
            json.dumps(legacy_non_learned, ensure_ascii=False),
            effect_outcome="",
            result_envelope={"skill_feedback": envelope},
        )

    return _error_outcome(
        action="status",
        error_code="SKILL_LEARNING_NOT_LEARNED",
        response_code="SKILL_LEARNING_NOT_LEARNED",
        reason=f"未在本身份登记表或当前可用方法快照中找到技能 {cleaned}。",
        effect_outcome="not_started",
        side_effect_status="not_started",
        next_action="调用 skill_feedback(action='status') 查看当前全部已登记的自学技能列表。",
        skill_id=cleaned,
    )


def _resolve_verified_usage_binding(
    agent: object,
    entry: object | None,
    record: Any,
    *,
    action: str,
) -> tuple[dict[str, object], ToolHandlerOutcome | None]:
    """核对运行快照绑定（incarnation/activation/strike_epoch）与当前登记记录一致。"""
    maint = dict(getattr(record, "maintenance", {}) or {})
    rec_inc = str(maint.get("incarnation_id") or "")
    rec_act = str(maint.get("activation_id") or "")
    raw_epoch = maint.get("strike_epoch")
    epoch_val = raw_epoch if isinstance(raw_epoch, int) and not isinstance(raw_epoch, bool) and raw_epoch >= 0 else None

    if entry is None:
        binding: dict[str, object] = {}
        if rec_inc and rec_act:
            binding["incarnation_id"] = rec_inc
            binding["activation_id"] = rec_act
        if epoch_val is not None:
            binding["strike_epoch"] = epoch_val
        sha = str(getattr(record, "sha256", "") or "").strip().lower()
        if sha:
            binding["content_sha256"] = sha
        return binding, None

    binding = learning_usage_binding(agent, entry)
    if rec_inc and rec_act:
        bind_inc = str(binding.get("incarnation_id") or "")
        bind_act = str(binding.get("activation_id") or "")
        if bind_inc != rec_inc or bind_act != rec_act:
            return {}, _error_outcome(
                action=action,
                error_code="SKILL_LEARNING_REVISION_CONFLICT",
                response_code="SKILL_LEARNING_REVISION_CONFLICT",
                reason=f"技能 owner:{record.name} 当前快照的激活绑定已过期或来自历史快照，未将反馈归因到当前版本。",
                effect_outcome="not_started",
                side_effect_status="not_started",
                next_action="请调用 skill_search 重新加载当前版本正文后再试。",
                skill_id=f"owner:{record.name}",
            )
    return dict(binding), None


def _record_adopted_usage(
    store: SkillLearningStore,
    entry: object,
    scope: object,
    binding: dict[str, object] | None = None,
) -> None:
    try:
        recorder = _usage_interface(store, "record_skill_usage", "record_skill_usage_event")
        if callable(recorder):
            kwargs: dict[str, object] = {
                "event": "adopted",
                "skill_id": entry.stable_id,
                "source": entry.source,
                "task_id": scope.root_task_id,
            }
            for key in ("content_sha256", "incarnation_id", "activation_id", "strike_epoch"):
                if key in (binding or {}) and binding[key] not in ("", None):
                    kwargs[key] = binding[key]
            try:
                sig = signature(recorder)
                accepts_var_kw = any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values())
                if not accepts_var_kw:
                    kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
            except (TypeError, ValueError):
                pass
            recorder(**kwargs)
    except Exception:  # noqa: BLE001 - 辅助统计故障不能掩盖已落账的采用声明。
        logging.getLogger(__name__).warning("SKILL_USAGE_RECORD_UNAVAILABLE")


def _main_write_claim(agent: object):
    from ..conversation.tool_approval_scope import (
        ToolApprovalScopeUnavailable,
        tool_approval_scope,
        tool_approval_scope_is_current,
    )

    if _is_child_or_restricted_context(agent):
        raise PermissionError("子代理或隔离运行上下文禁止调用 skill_feedback。")
    current = getattr(agent, "_current_run_params", None)
    run_id = str(getattr(current, "run_id", "") or getattr(current, "task_id", "") or "").strip()
    if not run_id:
        raise _FeedbackClaimError("当前缺少主会话运行身份，未写入学习记录。")
    try:
        scope = tool_approval_scope(agent, run_id)
    except (ToolApprovalScopeUnavailable, ValueError) as exc:
        raise _FeedbackClaimError("当前主会话任务身份不可用，未写入学习记录。") from exc
    if _is_child_or_restricted_context(agent, scope=scope):
        raise PermissionError("仅主会话允许修改自学技能状态，子代理禁止调用。")
    if not tool_approval_scope_is_current(agent, scope):
        raise _FeedbackClaimError("当前主会话任务 claim 已失效或不是最新尝试，未写入学习记录。")
    attributes = getattr(current, "task_attributes", None)
    return current, scope, attributes if isinstance(attributes, dict) else {}


def _safe_owner_manual_path(agent: object, candidate: Path | str | None) -> str | None:
    if not candidate:
        return None
    home_paths = getattr(agent, "home_paths", None)
    owner_home_str = str(getattr(home_paths, "owner_home_dir", "") or "").strip()
    if not owner_home_str:
        return None
    try:
        owner_root = Path(owner_home_str).expanduser().resolve(strict=False)
        target = Path(candidate).expanduser().resolve(strict=False)
        if target.is_relative_to(owner_root):
            return str(candidate)
    except (OSError, ValueError):
        return None
    return None


def _adopt_feedback(agent: object, store: SkillLearningStore, reference: str) -> ToolHandlerOutcome:
    current, scope, attributes = _main_write_claim(agent)
    cfg = _refresh_agent_config(agent)
    if getattr(cfg, "enable_self_learning", False) is not True:
        return _error_outcome(
            action="adopted",
            error_code="SKILL_FEEDBACK_UNAVAILABLE",
            response_code="SELF_LEARNING_DISABLED",
            reason="当前身份未开启自主学习（enable_self_learning=False），已拒绝登记模型采用声明。",
            effect_outcome="not_started",
            side_effect_status="not_started",
            next_action="如需启用自主学习请通过 user_config 开启 enable_self_learning，或直接按当前技能正文继续执行。",
            skill_id=reference,
        )
    snapshot = _snapshot_for(agent)
    entry = snapshot.resolve(reference) if snapshot is not None else None
    if entry is None or entry.source != "owner" or not entry.enabled:
        return _error_outcome(
            action="adopted",
            error_code="SKILL_FEEDBACK_UNAVAILABLE",
            response_code="SKILL_NOT_AUTHORIZED_IN_SNAPSHOT",
            reason=f"技能 {reference} 不在当前主会话已授权的本身份可用快照中，未写入采用记录。",
            effect_outcome="not_started",
            side_effect_status="not_started",
            next_action="先调用 skill_search 检索并加载本身份可用技能，再传入准确的 skill_id。",
            skill_id=reference,
        )
    registry = store.load_registry()
    record = registry.skills.get(entry.name)
    if record is None or record.layout == "owner":
        return _error_outcome(
            action="adopted",
            error_code="CANNOT_REMOVE_NON_LEARNED_SKILL",
            response_code="NOT_A_LEARNED_SKILL",
            reason=f"技能 {entry.stable_id} 不是自主学习发布的托管技能，无需登记 adopted。",
            effect_outcome="not_started",
            side_effect_status="not_started",
            next_action="只有本身份自学习发布的 Skill（owner:<name>）需要登记采用。",
            skill_id=entry.stable_id,
        )
    if record.sha256 != entry.content_sha256:
        return _error_outcome(
            action="adopted",
            error_code="SKILL_LEARNING_REVISION_CONFLICT",
            response_code="SKILL_LEARNING_REVISION_CONFLICT",
            reason=f"技能 {entry.stable_id} 磁盘版本哈希已变化，当前快照与最新登记版本不一致，未登记 adopted。",
            effect_outcome="not_started",
            side_effect_status="not_started",
            next_action="请调用 skill_search 重新加载当前版本正文后再试。",
            skill_id=entry.stable_id,
        )
    binding, binding_err = _resolve_verified_usage_binding(agent, entry, record, action="adopted")
    if binding_err is not None:
        return binding_err

    maint = dict(record.maintenance)
    raw_epoch = maint.get("strike_epoch")
    fallback_epoch = raw_epoch if isinstance(raw_epoch, int) and not isinstance(raw_epoch, bool) and raw_epoch >= 0 else None
    bind_epoch = binding.get("strike_epoch")
    epoch_val = bind_epoch if isinstance(bind_epoch, int) and not isinstance(bind_epoch, bool) and bind_epoch >= 0 else fallback_epoch

    observe_learning(
        store,
        entry.name,
        sha256=entry.content_sha256,
        task_id=scope.root_task_id,
        outcome="adopted",
        source=LearningObservationSource(
            run_id=scope.run_id,
            job_id=str(attributes.get("scheduler_job_id") or ""),
            incarnation_id=str(binding.get("incarnation_id") or maint.get("incarnation_id") or ""),
            activation_id=str(binding.get("activation_id") or maint.get("activation_id") or ""),
            strike_epoch=epoch_val,
        ),
    )
    _record_adopted_usage(store, entry, scope, binding)
    updated_record = store.load_registry().skills.get(entry.name, record)
    lifecycle = _lifecycle_from_record(store, updated_record, cfg)
    envelope = _feedback_response(
        ok=True,
        action="adopted",
        code="OK",
        reason=f"已为当前主会话任务登记采用 {entry.stable_id}（模型声明不直接算作有效性加分）。",
        side_effect_status="committed",
        next_action="继续执行任务；若后续发现该技能步骤在当前任务失效或误导，请调用 action='ineffective' 静默记分。",
        skill_id=entry.stable_id,
        lifecycle=lifecycle,
    )
    return ToolHandlerOutcome(
        "skill_feedback",
        True,
        json.dumps(envelope, ensure_ascii=False),
        effect_outcome="",
        result_envelope={"skill_feedback": envelope},
    )


def _remove_feedback(agent: object, store: SkillLearningStore, reference: str) -> ToolHandlerOutcome:
    _main_write_claim(agent)
    cfg = _refresh_agent_config(agent)
    cleaned = reference.strip()
    prefix = _non_owner_prefix(cleaned)
    snapshot = _snapshot_for(agent)
    entry = snapshot.resolve(cleaned) if snapshot is not None else None
    if prefix:
        manual = _safe_owner_manual_path(agent, getattr(entry, "path", None)) if prefix == "workspace" and entry else None
        return _error_outcome(
            action="remove",
            error_code="CANNOT_REMOVE_NON_LEARNED_SKILL",
            response_code="CANNOT_REMOVE_NON_LEARNED_SKILL",
            reason=f"技能 {cleaned} 属于 {prefix} 非自学习来源，工具不能移除。",
            effect_outcome="not_started",
            side_effect_status="not_started",
            next_action=(
                f"如确需删除本用户本地文件，请由用户手动查看处理：{manual}"
                if manual
                else "内置或共享技能由部署/管理员维护，不能通过 skill_feedback 移除。"
            ),
            skill_id=cleaned,
            manual_path=manual,
        )

    name = _strip_owner_prefix(cleaned)
    registry = store.load_registry()
    record = registry.skills.get(name)
    if record is None:
        if entry is not None:
            manual = _safe_owner_manual_path(agent, getattr(entry, "path", None)) if entry.source in {"owner", "workspace"} else None
            return _error_outcome(
                action="remove",
                error_code="CANNOT_REMOVE_NON_LEARNED_SKILL",
                response_code="CANNOT_REMOVE_NON_LEARNED_SKILL",
                reason=f"技能 {cleaned} 为非自学习来源（{entry.source}），不能通过 skill_feedback 移除。",
                effect_outcome="not_started",
                side_effect_status="not_started",
                next_action=(
                    f"如确需删除本地个人/工作区技能文件，请由用户按路径手动处理：{manual}"
                    if manual
                    else "内置或共享技能不能通过 skill_feedback 移除。"
                ),
                skill_id=cleaned,
                manual_path=manual,
            )
        local_skill = store.skills_root / name / "SKILL.md"
        if os.path.lexists(local_skill):
            manual = _safe_owner_manual_path(agent, local_skill)
            return _error_outcome(
                action="remove",
                error_code="CANNOT_REMOVE_NON_LEARNED_SKILL",
                response_code="CANNOT_REMOVE_NON_LEARNED_SKILL",
                reason=f"技能 {cleaned} 为用户自建本地技能，不属于自主学习托管资产，工具拒绝删除。",
                effect_outcome="not_started",
                side_effect_status="not_started",
                next_action=(
                    f"若确认要删除，请由用户按路径手动处理：{manual}"
                    if manual
                    else "请由用户检查本地技能目录后手动处理。"
                ),
                skill_id=cleaned,
                manual_path=manual,
            )
        return _error_outcome(
            action="remove",
            error_code="SKILL_LEARNING_NOT_LEARNED",
            response_code="SKILL_LEARNING_NOT_LEARNED",
            reason=f"未在本身份自学习登记表中找到 {cleaned}，未执行任何移除操作。",
            effect_outcome="not_started",
            side_effect_status="not_started",
            next_action="先调用 skill_feedback(action='status') 确认准确的技能 ID 与状态。",
            skill_id=cleaned,
        )

    managed = store.managed_records(registry, include_archived=True)
    if record.layout == "owner" or record.maintenance.get("ownership") in {"owner_layout", "user_modified"} or name not in managed:
        manual = _safe_owner_manual_path(agent, store.skill_path(name, record=record))
        return _error_outcome(
            action="remove",
            error_code="CANNOT_REMOVE_NON_LEARNED_SKILL",
            response_code="CANNOT_REMOVE_NON_LEARNED_SKILL",
            reason=f"技能 owner:{name} 为用户所有或已被用户手动修改，所有权属于用户本人，工具不能代删。",
            effect_outcome="not_started",
            side_effect_status="not_started",
            next_action=(
                f"如确需移除，请提示用户按该文件路径手动删除或改名：{manual}"
                if manual
                else "请由用户前往自己的技能目录手动处理。"
            ),
            skill_id=f"owner:{name}",
            manual_path=manual,
        )

    result = apply_learned_skill_action(store, name, "remove")
    updated = store.load_registry().skills.get(name, record)
    lifecycle = _lifecycle_from_record(store, updated, cfg)
    if result.get("committed") and result.get("ledger_appended") is False:
        return _error_outcome(
            action="remove",
            error_code="SKILL_FEEDBACK_UNAVAILABLE",
            response_code="LEDGER_APPEND_FAILED",
            reason=(
                f"自学技能 owner:{name} 已经完成归档移除并加入屏蔽名单，"
                f"但事件台账追加未确认成功（{result.get('ledger_error') or 'write_failed'}）。"
            ),
            effect_outcome="unknown",
            side_effect_status="partial",
            next_action=f"调用 skill_feedback(action='status', skill_id='owner:{name}') 核对归档状态，不要重复 Blind retry remove。",
            skill_id=f"owner:{name}",
            lifecycle=lifecycle,
        )

    envelope = _feedback_response(
        ok=True,
        action="remove",
        code="OK",
        reason=f"已将自学技能 owner:{name} 归档移除，并加入阻止自动重建名单。",
        side_effect_status="committed",
        next_action="该技能已移出在役列表并加入阻止重建名单；可调用 skill_feedback(action='status') 确认当前状态。",
        skill_id=f"owner:{name}",
        lifecycle=lifecycle,
    )
    return ToolHandlerOutcome(
        "skill_feedback",
        True,
        json.dumps(envelope, ensure_ascii=False),
        effect_outcome="",
        result_envelope={"skill_feedback": envelope},
    )


def _restore_feedback(agent: object, store: SkillLearningStore, reference: str) -> ToolHandlerOutcome:
    _main_write_claim(agent)
    cfg = _refresh_agent_config(agent)
    cleaned = reference.strip()
    if _non_owner_prefix(cleaned):
        return _error_outcome(
            action="restore",
            error_code="CANNOT_REMOVE_NON_LEARNED_SKILL",
            response_code="CANNOT_REMOVE_NON_LEARNED_SKILL",
            reason=f"技能 {cleaned} 为非自学习来源，不支持 restore。",
            effect_outcome="not_started",
            side_effect_status="not_started",
            next_action="仅已归档的自学 Skill（owner:<name>）可执行 restore。",
            skill_id=cleaned,
        )

    name = _strip_owner_prefix(cleaned)
    protection = learning_protection_for_agent(agent)
    control_learning(
        store,
        name,
        operation="restore",
        protected_ids=protection.get("protected_ids", ()),
        protection_complete=bool(protection.get("protection_complete", False)),
        config_provider=lambda: _refresh_agent_config(agent),
    )
    updated = store.load_registry().skills.get(name)
    lifecycle = _lifecycle_from_record(store, updated, cfg) if updated is not None else None
    envelope = _feedback_response(
        ok=True,
        action="restore",
        code="OK",
        reason=f"已将已归档的自学技能 owner:{name} 恢复为在役可用状态（{lifecycle['state'] if lifecycle else 'trial'}）。",
        side_effect_status="committed",
        next_action="需要时可用 skill_search 检索使用该技能。",
        skill_id=f"owner:{name}",
        lifecycle=lifecycle,
    )
    return ToolHandlerOutcome(
        "skill_feedback",
        True,
        json.dumps(envelope, ensure_ascii=False),
        effect_outcome="",
        result_envelope={"skill_feedback": envelope},
    )


def _ineffective_feedback(agent: object, store: SkillLearningStore, reference: str) -> ToolHandlerOutcome:
    current, scope, attributes = _main_write_claim(agent)
    cfg = _refresh_agent_config(agent)
    cleaned = reference.strip()
    prefix = _non_owner_prefix(cleaned)
    if prefix:
        return _error_outcome(
            action="ineffective",
            error_code="SKILL_FEEDBACK_UNAVAILABLE",
            response_code="NON_LEARNED_SKILL",
            reason=f"技能 {cleaned} 为 {prefix} 静态来源，不记入自学习负向淘汰计数。",
            effect_outcome="not_started",
            side_effect_status="not_started",
            next_action="仅对当前任务实际加载过的本身份自学 Skill（owner:<name>）调用 ineffective。",
            skill_id=cleaned,
        )

    name = _strip_owner_prefix(cleaned)
    registry = store.load_registry()
    record = registry.skills.get(name)
    if record is None:
        return _error_outcome(
            action="ineffective",
            error_code="SKILL_LEARNING_NOT_LEARNED",
            response_code="SKILL_LEARNING_NOT_LEARNED",
            reason=f"技能 {cleaned} 不在本身份已登记的自学记录中，未记录负向打击。",
            effect_outcome="not_started",
            side_effect_status="not_started",
            next_action="调用 skill_feedback(action='status') 查看已登记的自学技能列表。",
            skill_id=cleaned,
        )
    if record.layout == "owner" or record.maintenance.get("ownership") == "user_modified":
        return _error_outcome(
            action="ineffective",
            error_code="CANNOT_REMOVE_NON_LEARNED_SKILL",
            response_code="USER_OWNED_SKILL_EXEMPT",
            reason=f"技能 owner:{name} 为用户自建或用户已修改技能，不参与自主淘汰记分。",
            effect_outcome="not_started",
            side_effect_status="not_started",
            next_action="无需对用户自有技能执行 ineffective。",
            skill_id=f"owner:{name}",
        )

    snapshot = _snapshot_for(agent)
    entry = snapshot.resolve(cleaned) if snapshot is not None else None
    if entry is not None:
        if entry.source != "owner":
            return _error_outcome(
                action="ineffective",
                error_code="SKILL_FEEDBACK_UNAVAILABLE",
                response_code="NON_LEARNED_SKILL",
                reason=f"当前快照中 {cleaned} 指向 {entry.source} 来源，未记录自学负反馈。",
                effect_outcome="not_started",
                side_effect_status="not_started",
                next_action="仅针对 owner:<name> 自学 Skill 提交 ineffective。",
                skill_id=cleaned,
            )
        if entry.content_sha256 and entry.content_sha256 != record.sha256:
            return _error_outcome(
                action="ineffective",
                error_code="SKILL_LEARNING_REVISION_CONFLICT",
                response_code="SKILL_LEARNING_REVISION_CONFLICT",
                reason=f"技能 owner:{name} 当前运行快照内容哈希与已登记版本不一致，拒绝将旧版本负反馈归因到新版本。",
                effect_outcome="not_started",
                side_effect_status="not_started",
                next_action="调用 skill_feedback(action='status', skill_id=...) 查看最新已登记版本。",
                skill_id=f"owner:{name}",
            )

    binding, binding_err = _resolve_verified_usage_binding(agent, entry, record, action="ineffective")
    if binding_err is not None:
        return binding_err

    request_id = str(getattr(current, "request_id", "") or "").strip()
    task_id = str(getattr(current, "task_id", "") or "").strip()
    root_task_id = str(getattr(scope, "root_task_id", "") or "").strip()
    canonical_task = root_task_id or task_id or (f"request:{request_id}" if request_id else str(scope.run_id or ""))
    receipt_id = f"channel-a:{canonical_task}:{record.sha256[:16]}"
    delivery_keys = tuple(
        candidate_delivery_task_keys(
            root_task_id=root_task_id,
            task_id=task_id,
            request_id=request_id,
        )
    )
    maint = dict(record.maintenance)
    prev_hurt = int(maint.get("hurt_count") or 0)
    raw_epoch = maint.get("strike_epoch")
    fallback_epoch = raw_epoch if isinstance(raw_epoch, int) and not isinstance(raw_epoch, bool) and raw_epoch >= 0 else None
    bind_epoch = binding.get("strike_epoch")
    epoch_val = bind_epoch if isinstance(bind_epoch, int) and not isinstance(bind_epoch, bool) and bind_epoch >= 0 else fallback_epoch

    token = _HOST_DELIVERY_KEYS_CTX.set(delivery_keys)
    try:
        obs_result = observe_learning(
            store,
            record.name,
            sha256=record.sha256,
            task_id=canonical_task,
            outcome="ineffective",
            source=LearningObservationSource(
                authority="host",
                run_id=scope.run_id,
                receipt_id=receipt_id,
                job_id=str(attributes.get("scheduler_job_id") or ""),
                incarnation_id=str(binding.get("incarnation_id") or maint.get("incarnation_id") or ""),
                activation_id=str(binding.get("activation_id") or maint.get("activation_id") or ""),
                strike_epoch=epoch_val,
            ),
        )
    finally:
        _HOST_DELIVERY_KEYS_CTX.reset(token)

    after_registry = store.load_registry()
    after_record = after_registry.skills.get(record.name)
    outcome_action = str(obs_result.get("action") or "recorded")
    new_hurt = int(obs_result.get("hurt_count") or 0)
    if after_record is not None:
        lifecycle = _lifecycle_from_record(store, after_record, cfg)
    else:
        lifecycle = {
            "state": "retired",
            "quality": "unreliable",
            "independent_uses": None,
            "uses_since_hurt": 0,
            "required_uses": None,
            "hurt_count": MAX_STRIKE_COUNT,
            "retire_at_hurt_count": MAX_STRIKE_COUNT,
            "independent_uses_coverage": "unknown",
            "cool_in_seconds": None,
            "archive_in_seconds": None,
            "evict_in_seconds": None,
            "timer_reason": "retired_on_third_strike",
            "version": None,
            "coverage": _build_coverage("complete", "third_strike_retired"),
        }

    deduplicated = outcome_action == "deduplicated" or (
        outcome_action == "recorded" and bool(maint.get("incarnation_id")) and new_hurt <= prev_hurt
    )
    if outcome_action == "retired":
        reason = f"自学技能 owner:{name} 累计达到第 {MAX_STRIKE_COUNT} 次任务负向反馈，已退场并封禁同名自动生成。"
        code = "RETIRED"
    elif outcome_action == "rolled_back":
        reason = f"自学技能 owner:{name} 累计第 {MAX_STRIKE_COUNT} 次任务负向反馈，已自动回滚至已证实稳定的上一版本。"
        code = "ROLLED_BACK"
    elif deduplicated:
        reason = f"当前任务已存在 owner:{name} 本版本的负向反馈记录，本次静默去重（hurt_count={new_hurt}）。"
        code = "DEDUPLICATED"
    else:
        reason = (
            f"已静默记录 owner:{name} 本次任务无效反馈（hurt_count={new_hurt}/{MAX_STRIKE_COUNT}，"
            f"需连续 {MAX_STRIKE_COUNT + new_hurt} 次独立有效使用才可重新晋升）。"
        )
        code = "OK"

    envelope = _feedback_response(
        ok=True,
        action="ineffective",
        code=code,
        reason=reason,
        side_effect_status="committed",
        next_action="无需向用户弹窗确认，请直接换用其他可行方案继续完成当前任务。",
        skill_id=f"owner:{name}",
        lifecycle=lifecycle,
    )
    return ToolHandlerOutcome(
        "skill_feedback",
        True,
        json.dumps(envelope, ensure_ascii=False),
        effect_outcome="",
        result_envelope={"skill_feedback": envelope},
    )


def _map_store_or_gate_error(action: str, skill_id: str, exc: Exception) -> ToolHandlerOutcome:
    raw_code = str(getattr(exc, "code", "") or "")
    detail = getattr(exc, "detail", None)
    if raw_code == CODE_ARCHIVE_EXPIRED:
        return _error_outcome(
            action=action,
            error_code="SKILL_LEARNING_ARCHIVE_EXPIRED",
            response_code="SKILL_LEARNING_ARCHIVE_EXPIRED",
            reason=f"已归档技能 {skill_id} 已超出保留宽限期（self_learning_evict_seconds），不可恢复。",
            effect_outcome="not_started",
            side_effect_status="not_started",
            next_action="向用户说明该归档已过期，不再重复调用 restore。",
            skill_id=skill_id,
        )
    if raw_code == CODE_LIMIT_REACHED:
        return _error_outcome(
            action=action,
            error_code="SKILL_LEARNING_LIMIT_REACHED",
            response_code="SKILL_LEARNING_LIMIT_REACHED",
            reason=f"恢复 {skill_id} 将超出在役自学技能容量上限且其余技能均受保护，未执行恢复。",
            effect_outcome="not_started",
            side_effect_status="not_started",
            next_action="提示用户先移除或取消置顶其他自学技能，或提高 self_learning_max_skills 后再试。",
            skill_id=skill_id,
        )
    if raw_code == CODE_USER_MODIFIED:
        return _error_outcome(
            action=action,
            error_code="SKILL_LEARNING_USER_MODIFIED",
            response_code="SKILL_LEARNING_USER_MODIFIED",
            reason=f"技能 {skill_id} 已被用户手动修改或含额外文件，工具不再覆盖或恢复。",
            effect_outcome="not_started",
            side_effect_status="not_started",
            next_action="请提示用户直接在对应技能目录查看和手工处理。",
            skill_id=skill_id,
        )
    if raw_code == CODE_NOT_LEARNED:
        return _error_outcome(
            action=action,
            error_code="SKILL_LEARNING_NOT_LEARNED",
            response_code="SKILL_LEARNING_NOT_LEARNED",
            reason=f"技能 {skill_id} 不在当前自学习有效托管记录中，未变更任何状态。",
            effect_outcome="not_started",
            side_effect_status="not_started",
            next_action="调用 skill_feedback(action='status') 查看当前已登记的自学技能列表。",
            skill_id=skill_id,
        )
    if raw_code == CODE_EVIDENCE_INCOMPLETE:
        reason = "学习所有权、使用回执或活跃保护名单证据不完整，未执行变更。"
        if isinstance(detail, dict) and detail.get("reason"):
            reason = f"学习所有权或保护证据不完整（{detail['reason']}），未执行变更。"
        return _error_outcome(
            action=action,
            error_code="SKILL_LEARNING_EVIDENCE_INCOMPLETE",
            response_code="SKILL_LEARNING_EVIDENCE_INCOMPLETE",
            reason=reason,
            effect_outcome="not_started",
            side_effect_status="not_started",
            next_action="检查存储目录与运行库状态后重试，不按不完整证据强制执行。",
            skill_id=skill_id,
        )
    if raw_code == "SKILL_LEARNING_REVISION_CONFLICT":
        return _error_outcome(
            action=action,
            error_code="SKILL_LEARNING_REVISION_CONFLICT",
            response_code="SKILL_LEARNING_REVISION_CONFLICT",
            reason=f"技能 {skill_id} 版本哈希或绑定代次已变更，未执行状态更新。",
            effect_outcome="not_started",
            side_effect_status="not_started",
            next_action="调用 skill_feedback(action='status') 确认最新版本号。",
            skill_id=skill_id,
        )
    return _error_outcome(
        action=action,
        error_code="SKILL_FEEDBACK_UNAVAILABLE",
        response_code=raw_code or "SKILL_FEEDBACK_UNAVAILABLE",
        reason="无法核验学习技能存储或约束条件，未执行变更。",
        effect_outcome="not_started",
        side_effect_status="not_started",
        next_action="调用 skill_feedback(action='status') 确认当前存储与状态后重试。",
        skill_id=skill_id,
    )


# LLM: actor/run 只读宿主执行上下文；status 只读本身份生命周期与使用摘要，写动作全部校验主会话 claim 并结构化收口。
# 类用途: 提供主会话唯一入口，允许模型查询状态、声明采用、通道 A 静默标记失效，以及受控移除/恢复自学 Skill。
class SkillFeedbackTool(BaseTool):
    model_spec = ToolModelSpec(
        name="skill_feedback",
        description=(
            "主会话专用自学 Skill 反馈与维护工具（子代理/隔离上下文禁止调用）。"
            "action=status 查看本身份已登记自学 Skill 的当前状态、打击计数与淘汰倒计时（或传 skill_id 查看单个技能）；"
            "action=adopted 登记当前主会话任务实际采用了快照中的自学 Skill（不直接算作有效性加分）；"
            "action=ineffective（通道 A）在主会话中当已加载的自学 Skill 步骤失败、无效或误导当前任务时，"
            "由主模型静默调用以记录本任务去重负向打击（无需向用户弹窗确认，不接受未在当前任务送达/采用的技能）；"
            "action=remove 归档移除本身份自学 Skill（对非自学习技能拒绝并返回 CANNOT_REMOVE_NON_LEARNED_SKILL）；"
            "action=restore 在宽限与容量允许时恢复已归档的自学 Skill。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["status", "adopted", "remove", "restore", "ineffective"],
                    "default": "status",
                    "description": (
                        "status 只读查看状态；adopted 标记采用本任务已授权自学 Skill；"
                        "ineffective 通道 A 静默记录当前任务已送达自学 Skill 失效/误导（不询问用户）；"
                        "remove 归档移除自学 Skill；restore 恢复已归档自学 Skill。"
                    ),
                },
                "skill_id": {
                    "type": "string",
                    "description": "目标技能编号（例如 owner:foo 或 foo）。除 status 外均必填且非空。",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    )
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy(
            "read_only",
            by_parameter=(
                (
                    "action",
                    (
                        ("status", "read_only"),
                        ("adopted", "mutating"),
                        ("remove", "mutating"),
                        ("restore", "mutating"),
                        ("ineffective", "mutating"),
                    ),
                ),
            ),
        ),
        idempotency_policy=IdempotencyPolicy("operation"),
        resource_scopes=ResourceScopePolicy(
            mode="from_arguments",
            parameter_names=("skill_id",),
            parameter_kinds={"skill_id": "logical"},
            resource_domains={"skill_id": "skill_learning"},
        ),
    )

    def __init__(self, agent: object):
        self.agent = agent

    # LLM: 纯参数与运行域预检发生在任何 claim 与存储操作前，确保无效参数或子代理拦截绝不产生副作用。
    # 函数用途: 执行前校验工具入参与主会话权限边界。
    def validate_invocation(self, params: object, context: object) -> ToolHandlerOutcome | None:
        action = str(params.get("action", "status")) if isinstance(params, dict) else "status"
        skill_id = str(params.get("skill_id", "")).strip() if isinstance(params, dict) and isinstance(params.get("skill_id"), str) else None
        try:
            _feedback_operation(params)
        except _FeedbackArgumentsError as exc:
            return _error_outcome(
                action=action if action in _ALL_ACTIONS else "status",
                error_code="SKILL_FEEDBACK_UNAVAILABLE",
                response_code="INVALID_ARGUMENTS",
                reason=str(exc),
                effect_outcome="not_started",
                side_effect_status="not_started",
                next_action="传入合法 JSON 对象，仅允许 action（status/adopted/remove/restore/ineffective）与 skill_id。",
                skill_id=skill_id,
                output_text=str(exc),
            )
        if _is_child_or_restricted_context(self.agent, context=context):
            return _error_outcome(
                action=action,
                error_code="TOOL_PERMISSION_DENIED",
                response_code="CHILD_CONTEXT_FORBIDDEN",
                reason="仅主会话允许调用 skill_feedback，子代理或隔离运行上下文禁止查看或修改学习反馈。",
                effect_outcome="not_started",
                side_effect_status="not_started",
                next_action="如对方法有效性有疑问，请在结果中向父代理或主会话汇报，勿在子任务直接调用 skill_feedback。",
                skill_id=skill_id,
            )
        return None

    # LLM: status 只读本 owner 状态，四个写动作均须主会话 claim 与前置校验；所有异常按结构化错误码和准确副作用状态返回。
    # 函数用途: 调度执行 skill_feedback 工具并生成符合 FeedbackResponse 契约的回执。
    def execute(self, params: object) -> ToolHandlerOutcome:
        raw_action = str(params.get("action", "status")) if isinstance(params, dict) else "status"
        raw_skill_id = (
            params.get("skill_id", "").strip()
            if isinstance(params, dict) and isinstance(params.get("skill_id"), str)
            else None
        )
        try:
            operation = _feedback_operation(params)
        except _FeedbackArgumentsError as exc:
            return _error_outcome(
                action=raw_action if raw_action in _ALL_ACTIONS else "status",
                error_code="SKILL_FEEDBACK_UNAVAILABLE",
                response_code="INVALID_ARGUMENTS",
                reason=str(exc),
                effect_outcome="not_started",
                side_effect_status="not_started",
                next_action="传入合法 JSON 对象，仅允许 action 与 skill_id。",
                skill_id=raw_skill_id,
                output_text=str(exc),
            )

        if _is_child_or_restricted_context(self.agent):
            return _error_outcome(
                action=operation,
                error_code="TOOL_PERMISSION_DENIED",
                response_code="CHILD_CONTEXT_FORBIDDEN",
                reason="仅主会话允许调用 skill_feedback，子代理或隔离运行上下文禁止调用。",
                effect_outcome="not_started",
                side_effect_status="not_started",
                next_action="在主会话中执行 skill_feedback。",
                skill_id=raw_skill_id,
            )

        reference = str((params or {}).get("skill_id") or "").strip() if isinstance(params, dict) else ""
        try:
            store = _store_for(self.agent)
            if operation == "status":
                return _feedback_status(self.agent, store, reference)
            if operation == "adopted":
                return _adopt_feedback(self.agent, store, reference)
            if operation == "remove":
                return _remove_feedback(self.agent, store, reference)
            if operation == "restore":
                return _restore_feedback(self.agent, store, reference)
            return _ineffective_feedback(self.agent, store, reference)
        except PermissionError as exc:
            if isinstance(exc, _FeedbackClaimError):
                return _error_outcome(
                    action=operation,
                    error_code="SKILL_FEEDBACK_UNAVAILABLE",
                    response_code="CLAIM_UNAVAILABLE",
                    reason=str(exc),
                    effect_outcome="not_started",
                    side_effect_status="not_started",
                    next_action="仅在当前活跃主会话任务中执行写操作；若任务已结束请勿重试。",
                    skill_id=reference or None,
                )
            return _error_outcome(
                action=operation,
                error_code="TOOL_PERMISSION_DENIED",
                response_code="CHILD_CONTEXT_FORBIDDEN",
                reason=str(exc),
                effect_outcome="not_started",
                side_effect_status="not_started",
                next_action="仅主会话可调用 skill_feedback。",
                skill_id=reference or None,
            )
        except (SkillLearningGateError, SkillLearningStoreError) as exc:
            return _map_store_or_gate_error(operation, reference, exc)
        except (OwnerQuotaExceeded, OwnerQuotaUnavailable):
            return _error_outcome(
                action=operation,
                error_code="SKILL_FEEDBACK_UNAVAILABLE",
                response_code="OWNER_QUOTA_UNAVAILABLE",
                reason="当前身份配额已满或不可用，未执行学习状态变更。",
                effect_outcome="not_started",
                side_effect_status="not_started",
                next_action="清理磁盘空间或检查配额后重试。",
                skill_id=reference or None,
            )
        except ValueError as exc:
            msg = str(exc)
            if "该任务没有此版本的结构化采用回执" in msg:
                reason = f"当前主会话任务未检测到技能 {reference} 本版本的结构化正文送达或采用回执，未记录负向打击。"
                next_act = "仅对当前任务已加载过正文（skill_search get / 宿主注入 / 已 adopted）的自学技能调用 ineffective。"
            elif "条目未归档" in msg:
                reason = f"技能 {reference} 当前未处于归档状态，无需执行 restore。"
                next_act = "调用 skill_feedback(action='status', skill_id=...) 查看该技能实际状态。"
            else:
                reason = "无法核验当前方法版本、任务来源或存储条件，未执行变更。"
                next_act = "调用 skill_feedback(action='status') 查看当前可用技能与版本状态。"
            return _error_outcome(
                action=operation,
                error_code="SKILL_FEEDBACK_UNAVAILABLE",
                response_code="PRECONDITION_NOT_MET",
                reason=reason,
                effect_outcome="not_started",
                side_effect_status="not_started",
                next_action=next_act,
                skill_id=reference or None,
            )
        except (OSError, TypeError):
            logging.getLogger(__name__).warning("SKILL_FEEDBACK_FAILED", exc_info=True)
            return _error_outcome(
                action=operation,
                error_code="SKILL_FEEDBACK_UNAVAILABLE",
                response_code="SKILL_FEEDBACK_UNAVAILABLE",
                reason="学习存储当前不可读或写入异常，已停止本次操作。",
                effect_outcome="not_started",
                side_effect_status="not_started",
                next_action="先调用 skill_feedback(action='status') 核对状态。",
                skill_id=reference or None,
            )
