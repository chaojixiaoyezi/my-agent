# LLM: 路径来自已授权owner；这里只保存偏好及投影限制，不授予工具权限，运行时接线归W5/W6。
# 模块用途: 维护12项内置插件的owner默认/线程覆盖，并提供只读运行限制投影。
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..common.json_io import (
    locked_json_path,
    read_json_object_report,
    write_private_json_file_atomic_unlocked,
)
from .errors import HostAPIError

BUILTIN_PLUGIN_IDS = ("persona", "agent-instructions", "time-context", "tool-bash", "tool-pwsh", "tool-fs",
                      "tool-fs-search", "tool-jobs", "tool-schedule", "local-skills", "goal-runner", "skill-recorder")
# 原跨平台run_command/PTY共用shell入口；不能返回并不存在的run_powershell来假装完成裁剪。
_TOOLS = {"tool-bash": ("run_command", "terminal_session"), "tool-pwsh": ("run_command", "terminal_session"),
    "tool-fs": ("read_file", "write_file", "edit_file", "apply_patch", "read_artifact"),
    "tool-fs-search": ("list_files", "find_files", "search_text"), "tool-jobs": ("process_session",),
    "tool-schedule": ("schedule",), "local-skills": ("skill_search", "skill_feedback"),
    "goal-runner": ("create_goal", "get_goal", "update_goal"), "skill-recorder": ("skill_summarize",)}


# LLM: 固定owner数据叶且拒已知链接；thread_id只是映射键，不参与文件路径拼接。
# 函数用途: 校验可信owner下的状态位置和可选线程编号。
def _path(owner_home, thread_id):
    home = Path(owner_home)
    path = home / "data" / "plugin_host" / "builtin_features.json"
    if not isinstance(thread_id, str) or thread_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", thread_id):
        raise HostAPIError("INVALID_SCHEMA")
    if any(p.is_symlink() for p in (home, home / "data", path.parent, path)):
        raise HostAPIError("INVALID_SCHEMA")
    return path


# LLM: 坏JSON/类型不退回全开；缺失文件只读返回默认，不创建文件或锁。
# 函数用途: 加载并校验唯一开关状态，不吞损坏。
def _state(path):
    report = read_json_object_report(path)
    data = report.payload if path.exists() else {"schema": "builtin_plugins_state.v1", "owner": {}, "threads": {}}
    if (report.load_error or data.get("schema") != "builtin_plugins_state.v1"
            or not isinstance(data.get("owner"), dict) or not isinstance(data.get("threads"), dict)):
        raise HostAPIError("CORRUPT_PLUGIN_STATE", 500)
    for switches in [data["owner"], *data["threads"].values()]:
        if not isinstance(switches, dict) or any(key not in BUILTIN_PLUGIN_IDS or type(value) is not bool for key, value in switches.items()):
            raise HostAPIError("CORRUPT_PLUGIN_STATE", 500)
    return data


# LLM: 只读owner默认加线程覆盖，不产生状态、工具或执行权限。
# 函数用途: 返回12项开关的有效值及当前作用域。
def list_builtin_plugins(owner_home, *, thread_id: str = "") -> dict[str, Any]:
    data = _state(_path(owner_home, thread_id))
    switches = {**data["owner"], **data["threads"].get(thread_id, {})} if thread_id else data["owner"]
    return {"thread_id": thread_id, "plugins": [{"plugin_id": key, "enabled": switches.get(key, True)} for key in BUILTIN_PLUGIN_IDS]}


# LLM: 固定data/plugin_host/builtin_features.json原锁下读改写；损坏、链接或无效输入不覆盖。
# 函数用途: 保存一个owner默认或线程级内置插件开关。
def toggle_builtin_plugin(owner_home, *, plugin_id: str, enabled: bool, thread_id: str = "") -> dict[str, Any]:
    if not isinstance(plugin_id, str) or plugin_id not in BUILTIN_PLUGIN_IDS or type(enabled) is not bool:
        raise HostAPIError("INVALID_SCHEMA")
    path = _path(owner_home, thread_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked_json_path(path):
        data = _state(path)
        switches = data["threads"].setdefault(thread_id, {}) if thread_id else data["owner"]
        switches[plugin_id] = enabled
        write_private_json_file_atomic_unlocked(path, data)
    return list_builtin_plugins(owner_home, thread_id=thread_id)


# LLM: 纯读取投影可供turn裁剪；未知或未接线消费者不得据此宣称已实施运行限制。
# 函数用途: 将禁用插件转换成工具名称和四类提示词/技能跳过标志。
def resolve_builtin_plugin_restrictions(owner_home, *, thread_id: str = "") -> dict[str, Any]:
    disabled = [row["plugin_id"] for row in list_builtin_plugins(owner_home, thread_id=thread_id)["plugins"] if not row["enabled"]]
    return {"disabled_plugin_ids": disabled,
        "disabled_tool_names": sorted({tool for plugin in disabled for tool in _TOOLS.get(plugin, ())}),
        "skip_persona": "persona" in disabled, "skip_agent_instructions": "agent-instructions" in disabled,
        "skip_time_context": "time-context" in disabled, "skip_local_skills": "local-skills" in disabled}
