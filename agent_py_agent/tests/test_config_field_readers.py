"""每个 AgentConfig 字段都必须有读取方：配置必须真的生效（参数中心，2026-09-27）。

背景：vision_* 六项、lsp_servers、scheduler_mode、extensions_dir、continuation_reminder_seconds、task_max_grandchildren
在功能移除或改造后仍留在配置里，my-agent 据此误判能力（例如“看图额度只有 1024”），用户改了也不生效。
判据只认结构化事实：字段名以属性访问或字符串键出现在 settings/config.py 之外的产品代码里，或者由决策设置映射
`decision_config_fields()` 按字段名读取；不按前后缀猜动态拼接。
"""
from __future__ import annotations

import re
from dataclasses import fields
from pathlib import Path

from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.settings.decision_settings_defaults import decision_config_fields

_PACKAGE = Path(__file__).resolve().parents[1]
_DEFINITION = _PACKAGE / "agent" / "settings" / "config.py"


_REFERENCE = re.compile(r"\.([A-Za-z_]\w*)|[\"']([A-Za-z_]\w*)[\"']")


def _referenced_names() -> set[str]:
    names: set[str] = set()
    for path in _PACKAGE.rglob("*.py"):
        if "tests" in path.relative_to(_PACKAGE).parts or path == _DEFINITION:
            continue
        for attribute, key in _REFERENCE.findall(path.read_text(encoding="utf-8", errors="ignore")):
            names.add(attribute or key)
    return names


def test_every_config_field_has_a_reader():
    referenced = _referenced_names()
    mapped = {name for domain, name in decision_config_fields().values() if domain in {"agent", "memory"}}
    unread = [item.name for item in fields(AgentConfig) if item.name not in referenced | mapped]
    assert unread == [], f"这些配置项没有任何读取方，改了也不生效：删除它们，或让读取方真正使用它们：{unread}"
