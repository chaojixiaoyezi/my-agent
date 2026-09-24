"""模型用途标签：用户显式填写的开放小写标识符，只进目录与决策候选材料，不进运行时模型配置；只做本地合同验收。"""

import asyncio
import time

import pytest

from agent_py_agent.agent.settings.model_profiles import (
    execute_model_profile_operation as op,
)
from agent_py_agent.agent.settings.model_profiles import (
    model_profiles_path,
    read_model_profiles,
    selected_model_config,
)
from agent_py_agent.agent.settings.model_provider_schema import ModelProfileError, resolved_model
from agent_py_agent.tests.test_decision_model_profiles import decision
from agent_py_agent.tests.test_model_profiles import Host, add


def test_usage_tags_normalize_persist_and_reach_the_public_list(tmp_path):
    host = Host(tmp_path)
    tagged, _ = add(host, usage_tags="vision, long_document ，vision")
    plain, _ = add(host, model_name="MiniMax-M3")
    stored = read_model_profiles(model_profiles_path(host.home_paths))["profiles"]
    assert stored[tagged]["usage_tags"] == ["long_document", "vision"], "去重、排序、全角逗号也能分隔"
    assert "usage_tags" not in stored[plain], "没填就不写键"
    rows = {row["id"]: row for row in op(host, "list", {})["profiles"]}
    assert rows[tagged]["usage_tags"] == ["long_document", "vision"]
    assert "usage_tags" not in rows[plain]


@pytest.mark.parametrize("tags", ["Vision", "long-document", "1st", "x" * 41, ["ok", 3], {"tag": "ok"},
                                  ",".join(f"t{index}" for index in range(17))])
def test_bad_usage_tags_are_rejected_and_nothing_is_saved(tmp_path, tags):
    host = Host(tmp_path)
    with pytest.raises(ModelProfileError):
        add(host, usage_tags=tags)
    assert not model_profiles_path(host.home_paths).exists()


def test_decision_models_do_not_take_usage_tags(tmp_path):
    host = Host(tmp_path)
    with pytest.raises(ModelProfileError):
        decision(host, usage_tags="low_cost")
    assert not model_profiles_path(host.home_paths).exists()


def test_usage_tags_stay_out_of_the_runtime_model_config(tmp_path):
    host = Host(tmp_path)
    key, _ = add(host, usage_tags="low_cost")
    assert "usage_tags" not in resolved_model(read_model_profiles(model_profiles_path(host.home_paths)), key)
    op(host, "set_default", {"profile_id": key})
    config = selected_model_config(host)
    assert config.model_name == "MiniMax-M2.7" and not hasattr(config, "usage_tags")


def test_decision_candidates_carry_usage_tags_only_when_filled(tmp_path):
    from agent_py_agent.agent import gateway_model_observation
    from agent_py_agent.agent.agent_core.orchestration import decision_subagent

    host = Host(tmp_path)
    tagged, _ = add(host, usage_tags="long_document")
    plain, _ = add(host, model_name="MiniMax-M3")
    main = gateway_model_observation._candidates(host, time.monotonic() + 5)
    assert main[tagged]["usage_tags"] == ["long_document"] and "usage_tags" not in main[plain]
    child = decision_subagent._candidates(host, deadline=time.monotonic() + 5)
    assert child[tagged]["usage_tags"] == ["long_document"] and "usage_tags" not in child[plain]


def test_model_form_keeps_usage_tags_when_editing_other_fields(tmp_path, monkeypatch):
    from agent_py_agent.cli.chat_parts import tui_model_menu, tui_provider_menu

    host = Host(tmp_path)
    key, result = add(host, usage_tags="low_cost, long_document")
    row = next(row for row in result["profiles"] if row["id"] == key)
    sent = []

    async def dialog(*args, **_kwargs):
        _app, title, body = args[:3]
        return body.current_value if title == "新增模型 · 选择接口" else True

    async def request(*args):
        _app, agent, _session, operation, payload = args
        sent.append((operation, payload))
        return op(agent, operation, payload)

    monkeypatch.setattr(tui_model_menu, "_dialog", dialog)
    monkeypatch.setattr(tui_provider_menu, "_dialog", dialog)
    monkeypatch.setattr(tui_provider_menu, "_request", request)
    asyncio.run(tui_provider_menu._edit_model(None, host, "test", row["provider_id"], row))
    assert [action for action, _ in sent] == ["save_model"]
    assert sent[0][1]["profile"]["usage_tags"] == "long_document, low_cost"
    saved = read_model_profiles(model_profiles_path(host.home_paths))["profiles"][key]
    assert saved["usage_tags"] == ["long_document", "low_cost"]
