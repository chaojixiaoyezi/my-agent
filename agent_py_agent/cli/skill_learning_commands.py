# LLM: `skills learned` 是用户查看、回滚、删除自动总结 Skill 的唯一入口：只读写 SkillLearningStore（登记表、账本、
#   版本、removed 归档）与 owner 的 skills/learned/ 目录，不构造 Agent、不调模型；revert/remove 走
#   skill_learning_publish 的同一闸门并把事件记进账本。owner 与 home 按配置解析，规则与 skills proposals 相同。
#   不要把这些处理器暴露成模型工具。同步检查 skill_proposal_commands.add_skill_subcommands 与 test_skill_learning_cli.py。
# 模块用途: 注册 `my-agent skills learned list|show|revert|remove`，输出中文说明或 --json 机器字段。
from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..agent.capability.skill_learning_publish import (
    CODE_NOT_LEARNED,
    SkillLearningGateError,
    remove_learned_skill,
    revert_learned_skill,
)
from ..agent.capability.skill_learning_store import (
    EVENT_REMOVED,
    LearnedSkill,
    SkillLearningStore,
    SkillLearningStoreError,
)
from ..agent.capability.skill_snapshot import skill_content_sha256
from ..agent.settings import load_config
from ..agent.settings.services.runtime_config_env import apply_runtime_config_environment
from ..agent.user_space.home_layout import home_paths
from ..agent.user_space.home_root import configured_home_root
from ..agent.user_space.owner_resolver import (
    home_paths_with_owner,
    owner_identity_from_config,
    resolve_owner_home,
)

_STATE_LABELS = {"active": "生效中", "user_modified": "已被你修改（不再自动更新）", "missing": "文件已不存在"}
_NAME_HELP = "自动总结的 Skill 名（可从 list 获得）"


# LLM: 上下文只携带当前 owner 的存储与展示用配置事实；开关状态只用于展示，不决定能否回滚或删除。
# 类用途: 保存一次 skills learned 命令解析出的 owner 存储与配置。
@dataclass(frozen=True)
class _LearnedCommandContext:
    store: SkillLearningStore
    owner_id: str
    config: object


# LLM: 挂在 `skills` 命令树下；只提供查看、回滚、删除，不提供任何直接写入或编辑 Skill 的命令。
# 函数用途: 注册 skills learned 子命令树。
def add_learned_skill_subcommands(areas: argparse._SubParsersAction) -> None:
    learned = areas.add_parser(
        "learned",
        help="查看、回滚或删除自学习自动总结的 Skill",
        description="自动总结的 Skill 发布在当前 owner 的 skills/learned/，登记与账本在 data/skill_learning/。",
    )
    actions = learned.add_subparsers(dest="skills_learned_action", required=True)
    listing = actions.add_parser("list", help="列出自动总结的 Skill、待处理请求数和今日调用次数")
    listing.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    listing.set_defaults(func=cmd_skills_learned_list)
    for name, help_text, handler in (
        ("show", "查看一个自动总结 Skill 的登记信息和最近账本事件", cmd_skills_learned_show),
        ("revert", "退回上一个版本；只有第 1 版时等同删除；你改过的 Skill 不能回滚", cmd_skills_learned_revert),
        ("remove", "删除（移到 data/skill_learning/removed/ 归档），以后不再自动生成同名 Skill", cmd_skills_learned_remove),
    ):
        parser = actions.add_parser(name, help=help_text)
        parser.add_argument("name", help=_NAME_HELP)
        parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
        parser.set_defaults(func=handler)


# LLM: 只读列出，不创建目录。
# 函数用途: 处理 `skills learned list`。
def cmd_skills_learned_list(args: Any) -> int:
    return _run(args, "skills learned list", _list_payload)


# LLM: 只读查看；名字不在登记表时返回 SKILL_LEARNING_NOT_LEARNED。
# 函数用途: 处理 `skills learned show`。
def cmd_skills_learned_show(args: Any) -> int:
    return _run(args, "skills learned show", _show_payload)


# LLM: 用户显式回滚；成功后事件写入账本。
# 函数用途: 处理 `skills learned revert`。
def cmd_skills_learned_revert(args: Any) -> int:
    return _run(args, "skills learned revert", _revert_payload)


# LLM: 用户显式删除；成功后事件写入账本，名字进入 blocked_names。
# 函数用途: 处理 `skills learned remove`。
def cmd_skills_learned_remove(args: Any) -> int:
    return _run(args, "skills learned remove", _remove_payload)


# LLM: 统一错误边界：闸门与存储错误保留其结果码，其它异常转成有界诊断码；退出码只由 ok 决定。
# 函数用途: 解析 owner、执行一个子命令并输出结果。
def _run(args: Any, command: str, operation: Callable[[_LearnedCommandContext, Any], dict[str, object]]) -> int:
    try:
        payload = operation(_owner_context(args), args)
    except SkillLearningGateError as exc:
        payload = {"ok": False, "error_code": exc.code, "detail": exc.detail}
    except SkillLearningStoreError as exc:
        payload = {"ok": False, "error_code": exc.code, "detail": {}}
    except Exception as exc:  # noqa: BLE001 - CLI 必须返回有界诊断而不是堆栈。
        payload = {"ok": False, "error_code": "SKILL_LEARNING_CLI_" + type(exc).__name__.upper(), "detail": {}}
    payload = {"command": command, **payload}
    _print_payload(payload, json_output=bool(getattr(args, "json", False)))
    return 0 if payload.get("ok") else 1


# LLM: 与 skills proposals 同一 owner 解析：配置层→home 根→可信 owner→owner 路径投影；不初始化 Agent、不创建目录。
# 函数用途: 按配置找到当前 owner 的自学存储。
def _owner_context(args: Any) -> _LearnedCommandContext:
    config = apply_runtime_config_environment(load_config(args.config))
    base = home_paths(configured_home_root(config))
    owner = resolve_owner_home(base.root, owner_identity_from_config(config))
    scoped = home_paths_with_owner(base, owner)
    return _LearnedCommandContext(SkillLearningStore.for_home(scoped), owner.owner_id, config)


# LLM: 状态只由登记 hash 与磁盘文件事实决定；今日调用数按 UTC 日期，与服务端计数同口径。
# 函数用途: 构造 list 的输出。
def _list_payload(context: _LearnedCommandContext, args: Any) -> dict[str, object]:
    store = context.store
    registry = store.load_registry()
    today = datetime.now(timezone.utc).date().isoformat()
    return {
        "ok": True,
        "owner_id": context.owner_id,
        "self_learning_enabled": bool(getattr(context.config, "enable_self_learning", False)),
        "learned_root": str(store.learned_root),
        "pending_requests": len(store.pending_requests()),
        "calls_today": registry.daily_calls if registry.daily_date == today else 0,
        "daily_limit": int(getattr(context.config, "self_learning_daily_limit", 0) or 0),
        "blocked_names": sorted(set(registry.blocked_names)),
        "count": len(registry.skills),
        "skills": [_skill_row(store, registry.skills[name]) for name in sorted(registry.skills)],
    }


# LLM: 返回登记信息与最近 20 条账本事件；账本只含结构化字段与有界 reason。
# 函数用途: 构造 show 的输出。
def _show_payload(context: _LearnedCommandContext, args: Any) -> dict[str, object]:
    record = context.store.load_registry().skills.get(args.name)
    if record is None:
        raise SkillLearningGateError(CODE_NOT_LEARNED, {"skill_name": args.name})
    return {"ok": True, "skill": _skill_row(context.store, record), "events": context.store.events(args.name, 20)}


# LLM: 回滚与删除共用闸门；成功后把事件写进账本再输出。
# 函数用途: 构造 revert 的输出。
def _revert_payload(context: _LearnedCommandContext, args: Any) -> dict[str, object]:
    event = revert_learned_skill(context.store, args.name)
    context.store.append_event(event)
    return {"ok": True, "code": event.event, "skill_name": event.skill_name, "version": event.version}


# LLM: 删除成功后事件写进账本。
# 函数用途: 构造 remove 的输出。
def _remove_payload(context: _LearnedCommandContext, args: Any) -> dict[str, object]:
    event = remove_learned_skill(context.store, args.name)
    context.store.append_event(event)
    return {"ok": True, "code": event.event, "skill_name": event.skill_name, "version": event.version}


# LLM: 行内容是登记记录加路径与状态；状态 active/user_modified/missing 只由文件事实推导。
# 函数用途: 生成一个自学 Skill 的展示行。
def _skill_row(store: SkillLearningStore, record: LearnedSkill) -> dict[str, object]:
    path = store.skill_path(record.name)
    if path.is_symlink() or not path.is_file():
        state = "missing"
    else:
        state = "active" if skill_content_sha256(path.read_text(encoding="utf-8")) == record.sha256 else "user_modified"
    return {**record.to_record(), "path": str(path), "state": state}


# LLM: --json 输出稳定机器字段；普通输出只给人看，不作为任何机器判断依据。
# 函数用途: 打印命令结果。
def _print_payload(payload: dict[str, object], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print("\n".join(_human_lines(payload)))


# LLM: 只做展示分派，不改变结果。
# 函数用途: 按命令结果选择中文展示内容。
def _human_lines(payload: dict[str, object]) -> list[str]:
    if not payload.get("ok"):
        detail = json.dumps(payload.get("detail") or {}, ensure_ascii=False, sort_keys=True)
        return [f"操作未完成：{payload.get('error_code')}", f"细节：{detail}"]
    if "skills" in payload:
        return _list_lines(payload)
    if "skill" in payload:
        return _show_lines(payload)
    if payload.get("code") == EVENT_REMOVED:
        return [f"已删除 {payload.get('skill_name')}，归档在 data/skill_learning/removed/，以后不再自动生成同名 Skill。"]
    return [f"已把 {payload.get('skill_name')} 退回到第 {payload.get('version')} 版。"]


# LLM: 每个 Skill 一行摘要，完整事件用 show 查看。
# 函数用途: 生成列表的中文展示。
def _list_lines(payload: dict[str, object]) -> list[str]:
    switch = "开启" if payload.get("self_learning_enabled") else "关闭"
    limit = payload.get("daily_limit") or "不限"
    lines = [
        f"自动总结的 Skill（owner {payload.get('owner_id')}，共 {payload.get('count')} 个；自学习开关：{switch}）",
        f"待处理请求 {payload.get('pending_requests')} 条；今日总结调用 {payload.get('calls_today')}/{limit} 次",
    ]
    for item in payload.get("skills") or []:
        state = _STATE_LABELS.get(item["state"], item["state"])
        lines.append(f"- {item['name']} 第 {item['version']} 版 [{state}] 更新于 {item['updated_at']}")
        lines.append(f"  {item['path']}")
    return lines


# LLM: 展示登记事实与最近事件；事件只显示结构化字段与有界 reason。
# 函数用途: 生成单个 Skill 的中文详情。
def _show_lines(payload: dict[str, object]) -> list[str]:
    item = payload["skill"]
    lines = [
        f"{item['name']}：第 {item['version']} 版，{_STATE_LABELS.get(item['state'], item['state'])}",
        f"路径：{item['path']}",
        f"创建于 {item['created_at']}，更新于 {item['updated_at']}",
        f"来源运行：{'、'.join(item['source_run_ids']) or '无'}",
        "最近事件：",
    ]
    for event in payload.get("events") or []:
        code = f" {event.get('code')}" if event.get("code") else ""
        lines.append(f"- {event.get('at')} {event.get('event')} 第 {event.get('version')} 版{code} {event.get('reason') or ''}")
    return lines


__all__ = [
    "add_learned_skill_subcommands",
    "cmd_skills_learned_list",
    "cmd_skills_learned_remove",
    "cmd_skills_learned_revert",
    "cmd_skills_learned_show",
]
