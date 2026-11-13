# LLM: 自动总结 Skill 的只读报告与用户触发的回滚/删除，CLI `skills learned` 与聊天 `/skills learned` 共用这一处：
#   状态只由登记 hash 与磁盘文件事实推导（active/user_modified/missing）；回滚与删除走 skill_learning_publish 的同一闸门，
#   成功后把事件写进账本。不构造 Agent、不调模型，不要暴露成模型工具。
#   改动须同步 cli/skill_learning_commands.py、gateway_parts/skill_control_service.py 与 test_skill_learning_integration.py、
#   test_skill_chat_control.py。
# 模块用途: 统一自学Skill状态、学习队列/每日总结历史与独立正文使用统计，以及用户要求的回滚和删除。
from __future__ import annotations

from datetime import datetime, timezone

from ..common.nofollow_fs import NoFollowPathError, read_text_beneath
from ..user_space.owner_quota import OwnerQuotaExceeded, OwnerQuotaUnavailable
from .skill_learning_publish import (
    CODE_NOT_LEARNED,
    SkillLearningGateError,
    remove_learned_skill,
    revert_learned_skill,
)
from .skill_learning_store import (
    CODE_EVIDENCE_INCOMPLETE,
    LearnedSkill,
    SkillLearningStore,
    SkillLearningStoreError,
)
from .skill_snapshot import skill_content_sha256
from .skill_usage_ledger import load_skill_usage_summary


# LLM: 行内容是登记记录加路径与状态；状态 active/user_modified/missing/unreadable 只由文件事实推导，传已知 record 免重复读表。只读。
# 函数用途: 生成一个自动总结 Skill 的展示行。
def learned_skill_row(store: SkillLearningStore, record: LearnedSkill) -> dict[str, object]:
    path = store.skill_path(record.name, record=record)
    if record.maintenance.get("state") in {"archived","disabled","cooled"}:
        state = record.maintenance["state"]
    elif path.is_symlink() or not path.is_file():
        state = "missing"
    else:
        try:
            text = read_text_beneath(store.skills_root, path.relative_to(store.skills_root).parts)
        except (OSError, ValueError, UnicodeError, NoFollowPathError):
            state = "unreadable"
        else:
            if text is None:
                state = "missing"
            else:
                state = "active" if skill_content_sha256(text) == record.sha256 else "user_modified"
    return {**record.to_record(), "path": str(path), "state": state}


# LLM: 只读投影，不触发溢出回填；明确区分经验证在役槽位数与覆盖率状态，不把证据不全静默伪装为 0。
# 函数用途: 汇总学习状态、候选/缓冲积压、30日调用历史和Skill实际使用台账。
def learned_skills_report(store: SkillLearningStore, config: object) -> dict[str, object]:
    registry = store.load_registry()
    today = datetime.now(timezone.utc).date().isoformat()
    managed_count: int | None
    coverage: dict[str, object]
    try:
        managed = store.managed_records(registry, include_archived=False)
        managed_count = registry.live_count(managed_names=managed.keys())
        coverage = {"status": "complete", "managed": "complete"}
    except SkillLearningStoreError as exc:
        if exc.code != CODE_EVIDENCE_INCOMPLETE:
            raise
        managed_count = None
        coverage = {
            "status": "unavailable",
            "managed": "unavailable",
            "code": exc.code,
            "detail": dict(exc.detail),
        }
    return {
        "self_learning_enabled": bool(getattr(config, "enable_self_learning", False)),
        "learned_root": str(store.learned_root),
        "pending_requests": len(store.pending_requests(refill=False)),
        "overflow_requests": len(store.overflow_requests()),
        "pending_proposals": _pending_proposal_count(store),
        "calls_today": registry.daily_calls if registry.daily_date == today else 0,
        "daily_history": dict(registry.daily_history),
        "skill_usage": load_skill_usage_summary(store),
        "daily_limit": int(getattr(config, "self_learning_daily_limit", 0) or 0),
        "blocked_names": sorted(set(registry.blocked_names)),
        "count": len(registry.skills),
        "managed_count": managed_count,
        "coverage": coverage,
        "skills": [learned_skill_row(store, registry.skills[name]) for name in sorted(registry.skills)],
    }


# LLM: 活跃和溢出候选均来自同一canonical读取入口；只统计仍pending的项，不把superseded当待确认。
# 函数用途: 展示全部可确认候选积压，不进行回填、确认或发布。
def _pending_proposal_count(store: SkillLearningStore) -> int:
    from .skill_learning_proposals import list_learning_proposals

    return sum(row.get("status") == "pending" for row in list_learning_proposals(store, include_overflow=True))


# LLM: 名字先在登记表里精确查找，不在表里就抛 CODE_NOT_LEARNED，不用它拼路径；事件只含结构化字段与有界 reason。只读。
# 函数用途: 返回一个自动总结 Skill 的登记信息与最近事件。
def learned_skill_detail(store: SkillLearningStore, name: str, *, limit: int = 20) -> dict[str, object]:
    record = store.load_registry().skills.get(name)
    if record is None:
        raise SkillLearningGateError(CODE_NOT_LEARNED, {"skill_name": name})
    return {"skill": learned_skill_row(store, record), "events": store.events(name, limit)}


# LLM: action 只能是 revert 或 remove，都走发布闸门（在登记表里找不到或被用户改过时拒绝）；
#   主变更提交后若仅账本追加失败，保留已提交事实并如实上报 ledger_appended=False，绝不吞掉已提交状态。
# 函数用途: 执行用户要求的回滚或删除，返回结果码、名字、版本及提交回执。
def apply_learned_skill_action(store: SkillLearningStore, name: str, action: str) -> dict[str, object]:
    handlers = {"revert": revert_learned_skill, "remove": remove_learned_skill}
    event = handlers[action](store, name)
    ledger_appended = True
    ledger_error = ""
    try:
        store.append_event(event)
    except (OSError, SkillLearningStoreError, OwnerQuotaExceeded, OwnerQuotaUnavailable) as exc:
        ledger_appended = False
        ledger_error = type(exc).__name__
    result: dict[str, object] = {
        "code": event.event,
        "skill_name": event.skill_name,
        "version": event.version,
        "sha256": event.sha256,
        "committed": True,
        "outcome": "committed",
        "ledger_appended": ledger_appended,
    }
    if ledger_error:
        result["ledger_error"] = ledger_error
    return result


__all__ = [
    "apply_learned_skill_action",
    "learned_skill_detail",
    "learned_skill_row",
    "learned_skills_report",
]
