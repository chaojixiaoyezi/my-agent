# LLM: 自动总结 Skill 的只读报告与用户触发的回滚/删除，CLI `skills learned` 与聊天 `/skills learned` 共用这一处：
#   状态只由登记 hash 与磁盘文件事实推导（active/user_modified/missing）；回滚与删除走 skill_learning_publish 的同一闸门，
#   成功后把事件写进账本。不构造 Agent、不调模型，不要暴露成模型工具。
#   改动须同步 cli/skill_learning_commands.py、gateway_parts/skill_control_service.py 与 test_skill_learning_integration.py、
#   test_skill_chat_control.py。
# 模块用途: 统一“自动总结的 Skill 现在是什么状态、最近发生了什么”的事实，以及用户要求的回滚与删除。
from __future__ import annotations

from datetime import datetime, timezone

from .skill_learning_publish import (
    CODE_NOT_LEARNED,
    SkillLearningGateError,
    remove_learned_skill,
    revert_learned_skill,
)
from .skill_learning_store import LearnedSkill, SkillLearningStore
from .skill_snapshot import skill_content_sha256


# LLM: 行内容是登记记录加路径与状态；状态 active/user_modified/missing 只由文件事实推导。只读。
# 函数用途: 生成一个自动总结 Skill 的展示行。
def learned_skill_row(store: SkillLearningStore, record: LearnedSkill) -> dict[str, object]:
    path = store.skill_path(record.name)
    if path.is_symlink() or not path.is_file():
        state = "missing"
    else:
        state = "active" if skill_content_sha256(path.read_text(encoding="utf-8")) == record.sha256 else "user_modified"
    return {**record.to_record(), "path": str(path), "state": state}


# LLM: 只读列出，不创建目录；今日调用数按 UTC 日期，与服务端计数同口径；开关状态只作展示。
# 函数用途: 汇总自动总结 Skill 的列表、待处理请求数、今日调用次数与被屏蔽的名字。
def learned_skills_report(store: SkillLearningStore, config: object) -> dict[str, object]:
    registry = store.load_registry()
    today = datetime.now(timezone.utc).date().isoformat()
    return {
        "self_learning_enabled": bool(getattr(config, "enable_self_learning", False)),
        "learned_root": str(store.learned_root),
        "pending_requests": len(store.pending_requests()),
        "calls_today": registry.daily_calls if registry.daily_date == today else 0,
        "daily_limit": int(getattr(config, "self_learning_daily_limit", 0) or 0),
        "blocked_names": sorted(set(registry.blocked_names)),
        "count": len(registry.skills),
        "skills": [learned_skill_row(store, registry.skills[name]) for name in sorted(registry.skills)],
    }


# LLM: 名字先在登记表里精确查找，不在表里就抛 CODE_NOT_LEARNED，不用它拼路径；事件只含结构化字段与有界 reason。只读。
# 函数用途: 返回一个自动总结 Skill 的登记信息与最近事件。
def learned_skill_detail(store: SkillLearningStore, name: str, *, limit: int = 20) -> dict[str, object]:
    record = store.load_registry().skills.get(name)
    if record is None:
        raise SkillLearningGateError(CODE_NOT_LEARNED, {"skill_name": name})
    return {"skill": learned_skill_row(store, record), "events": store.events(name, limit)}


# LLM: action 只能是 revert 或 remove，都走发布闸门（在登记表里找不到或被用户改过时拒绝），成功后把事件写进账本。
#   会改 skills/learned/ 与账本（写文件副作用）。
# 函数用途: 执行用户要求的回滚或删除，返回结果码、名字和版本。
def apply_learned_skill_action(store: SkillLearningStore, name: str, action: str) -> dict[str, object]:
    handlers = {"revert": revert_learned_skill, "remove": remove_learned_skill}
    event = handlers[action](store, name)
    store.append_event(event)
    return {"code": event.event, "skill_name": event.skill_name, "version": event.version}


__all__ = [
    "apply_learned_skill_action",
    "learned_skill_detail",
    "learned_skill_row",
    "learned_skills_report",
]
