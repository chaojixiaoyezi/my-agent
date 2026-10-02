"""参数减量第 3 批 E 组（2026-09-27）：内部参数降为读取点旁的具名常量，值不变。

锁定：14 个配置键从 AgentConfig 删除，写在 YAML 里只告警并忽略、不转值；常量等于原默认值；真实装配的工具注册表
用的就是这些常量；给模型看的目录分页提示和截断标记不再指向已删的配置键。加载器元数据从 /settings 列表隐藏另见
test_settings_chat_control。
"""
from __future__ import annotations

from dataclasses import fields

from agent_py_agent.agent.settings.config import AgentConfig, load_config

_E_KEYS = {
    "background_context_max_string_chars": "5", "background_context_max_list_items": "1",
    "background_context_max_dict_items": "2", "background_context_max_depth": "2",
    "conversation_context_recent_limit": "3", "background_pending_wake_prompt_limit": "4",
    "conversation_terminal_tool_fold_max_chars": "1500", "tool_catalog_limit": "5", "tool_catalog_offset": "2",
    "tool_catalog_entry_max_chars": "50", "tool_detail_max_chars": "60", "contract_status_max_scan_files": "7",
    "contract_status_max_report_bytes": "900", "contract_status_recent_findings_limit": "1",
}


def test_e_group_keys_are_gone_and_only_warn(tmp_path):
    assert not {item.name for item in fields(AgentConfig)} & set(_E_KEYS)
    path = tmp_path / "agent_config.yaml"
    path.write_text("".join(f"{key}: {value}\n" for key, value in _E_KEYS.items()), encoding="utf-8")

    config = load_config(path)

    for key in _E_KEYS:
        assert f"unknown config key: {key!r}; ignored" in config.config_warnings
        assert not hasattr(config, key)


def test_e_group_constants_keep_the_old_defaults():
    from agent_py_agent.agent import core
    from agent_py_agent.agent.contracts import contract_status
    from agent_py_agent.agent.conversation import (
        background_context,
        context_budget,
        tool_context_window,
    )

    budget = context_budget.DEFAULT_BACKGROUND_CONTEXT_BUDGET
    assert (budget.max_string_chars, budget.max_list_items, budget.max_dict_items, budget.max_depth) == (1200, 20, 80, 6)
    assert background_context.CONVERSATION_CONTEXT_RECENT_LIMIT_COUNT == 20
    assert background_context.BACKGROUND_PENDING_WAKE_PROMPT_LIMIT_COUNT == 20
    assert tool_context_window._TERMINAL_TOOL_FOLD_MAX_CHARS == 6_000
    assert (core.TOOL_CATALOG_LIMIT_COUNT, core.TOOL_DETAIL_MAX_CHARS) == (80, 4_000)
    assert (contract_status.CONTRACT_STATUS_RECENT_FINDINGS_LIMIT, contract_status.CONTRACT_STATUS_MAX_SCAN_FILES,
            contract_status.CONTRACT_STATUS_MAX_REPORT_BYTES) == (20, 1_000, 2_000_000)


def test_the_assembled_registry_uses_the_catalog_constants(tmp_path):
    from agent_py_agent.agent.core import SimpleAgent

    registry = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path).tools

    assert (registry.catalog_limit, registry.catalog_offset, registry.catalog_entry_max_chars,
            registry.tool_detail_max_chars) == (80, 0, 700, 4_000)


def test_model_facing_texts_no_longer_name_the_deleted_keys():
    from agent_py_agent.agent.tooling.models import ToolModelSpec
    from agent_py_agent.agent.tooling.registry import CatalogRenderConfig, _catalog_page_notice

    config = CatalogRenderConfig(mode="compact", offset=0, limit=80, categories=[], include_examples=False,
                                 entry_max_chars=700, show_truncated_notice=True, detail_max_chars=4_000)
    notice = _catalog_page_notice(config, total=90, returned=80)
    assert "list_tools" in notice and "tool_catalog_" not in notice
    spec = ToolModelSpec(name="demo", description="很长的说明" * 200, input_schema={"type": "object", "properties": {}})
    rendered = [spec.render_catalog_entry(max_chars=40), spec.render_recommended_entry(max_chars=40),
                spec.render_detail_entry(max_chars=40)]
    assert all("截断" in text and "_max_chars" not in text for text in rendered)
