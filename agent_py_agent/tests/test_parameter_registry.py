"""参数中心登记表（2026-09-27）：每个 AgentConfig 字段一条，说明取自随包 YAML 注释，安全等级结构化判定。

锁定：登记表覆盖全部字段；凭据、权限、身份、路径、外部地址、会运行代码的设置、执行权威链与内部元数据不可由模型改；
普通节奏/预算/上限参数可改；显式白名单（飞书凭据）可改但脱敏；搜索先按键名再按说明。
说明先取键正上方注释、没有再取行尾注释（引号里的 # 不算）；说明为空的参数只能留在带原因的基线名单里。
推理强度登记了派生规则：当前模型不支持调节时如实说“不改变请求”，修改回执按新值给出同一结果。
"""
from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.reasoning_control import (
    describe_reasoning_effect,
    resolved_reasoning_control,
)
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.settings.config_io import yaml_trailing_comment
from agent_py_agent.agent.settings.parameter_registry import (
    SAFETY_BOUNDARY,
    _descriptions_from_lines,
    applied_value,
    applied_value_with,
    parameter_registry,
    search_parameters,
)
from agent_py_agent.agent.settings.user_config_capability import BOUNDARY_KEYS, TUNABLE_KEYS

_BASELINE = Path(__file__).parent / "fixtures" / "parameter_description_baseline.json"
_OPENCODE = {"api_base": "https://opencode.ai/zen/v1", "model_backend": "openai_compatible", "model_reasoning_control": "auto"}


@pytest.mark.parametrize("line,comment", [
    ("a: 1  # 说明", "说明"), ('url: "http://x/#frag"  # 地址', "地址"), ("s: 'it''s # 不是注释'", ""),
    ('q: "# 引号里"', ""), ("n: 3", ""), ("e: 4  #", ""), ("  nested: 5  # 缩进行也能取", "缩进行也能取"),
])
def test_trailing_comment_uses_the_loader_quote_rule(line, comment):
    assert yaml_trailing_comment(line) == comment


def test_descriptions_prefer_the_comment_above_then_the_trailing_comment():
    lines = ["# 上方说明", "a: 1  # 行尾说明", "b: 2  # 行尾说明 b", 'c: "x # 不是注释"  # 真说明', "d: 'y'", "",
             "# 被空行隔开的注释", "", "e: 3", "f: 4  #", "a: 5  # 同名键只取第一次"]
    assert _descriptions_from_lines(lines) == {"a": "上方说明", "b": "行尾说明 b", "c": "真说明", "d": "", "e": "", "f": ""}


def test_empty_descriptions_only_come_from_the_reasoned_baseline():
    """名单外新增空说明失败；名单里的参数有了说明或已删除也失败，逼着名单只减不增。"""
    groups = json.loads(_BASELINE.read_text(encoding="utf-8"))["groups"]
    assert all(group["reason"].strip() and group["keys"] for group in groups)
    listed = [key for group in groups for key in group["keys"]]
    assert len(listed) == len(set(listed)), "基线名单里有重复的键"
    registry = parameter_registry()
    empty = {key for key, spec in registry.items() if not spec.description}
    added = sorted(empty - set(listed))
    assert not added, f"这些参数没有中文说明，请在随包 agent_config.yaml 该键正上方（或行尾）写一句：{added}"
    stale = sorted(set(listed) - empty)
    assert not stale, f"这些参数已有说明或已不存在，请从 {_BASELINE.name} 删掉：{stale}"
    assert registry["decision_timeout_seconds"].description == "前台单次等待；必须为有限正秒数"


def test_reasoning_effort_applied_value_says_whether_the_model_supports_it():
    unsupported = SimpleNamespace(model_reasoning_effort="max", **_OPENCODE)
    value, rule = applied_value("model_reasoning_effort", unsupported)
    control = resolved_reasoning_control("auto", _OPENCODE["api_base"], "openai_compatible")
    assert value == describe_reasoning_effect("max", control) and "不改变请求" in value and "思考控制方式" in rule
    deepseek = SimpleNamespace(**{**_OPENCODE, "api_base": "https://api.deepseek.com/v1"}, model_reasoning_effort="max")
    assert applied_value("model_reasoning_effort", deepseek)[0] == "按推理强度档位发送：最高。"
    assert "不额外发送" in applied_value("model_reasoning_effort", SimpleNamespace(model_reasoning_effort="auto"))[0]


def test_applied_value_with_uses_the_new_value_without_touching_the_config():
    config = SimpleNamespace(max_tokens=16384, model_context_window_tokens=131072, model_reasoning_effort="auto", **_OPENCODE)
    assert applied_value_with("max_tokens", "65536", config)[0] == 32768 and config.max_tokens == 16384
    assert "不改变请求" in applied_value_with("model_reasoning_effort", "max", config)[0]
    assert config.model_reasoning_effort == "auto"
    assert applied_value_with("request_timeout", "300", config) is None
    assert applied_value_with("max_tokens", "很多", config) is None and applied_value_with("max_tokens", "1", None) is None


def test_applied_value_uses_the_single_output_cap_formula():
    """64K 在 128K 窗口模型上实际是 32768；窗口未知时等于配置值；没有派生规则的参数不给实际使用值。"""
    value, rule = applied_value("max_tokens", SimpleNamespace(max_tokens=65536, model_context_window_tokens=131072))
    assert value == 32768 and "窗口" in rule
    assert applied_value("max_tokens", SimpleNamespace(max_tokens=65536, model_context_window_tokens=0))[0] == 65536
    assert applied_value("request_timeout", SimpleNamespace(request_timeout=300)) is None
    assert applied_value("max_tokens", None) is None


def test_registry_covers_every_config_field_with_yaml_descriptions():
    registry = parameter_registry()
    assert set(registry) == {item.name for item in fields(AgentConfig)}
    assert "64K" in registry["max_tokens"].description
    assert registry["max_tokens"].value_type == "int" and registry["enable_self_learning"].value_type == "bool"


@pytest.mark.parametrize("key", [
    *sorted(BOUNDARY_KEYS), "api_base", "model_backend", "memory_embedding_api_base", "system_prompt", "prompt_files",
    "audit_enabled", "enable_tools", "enable_gateway_restart_tool", "additional_write_roots", "mcp_servers",
    "execution_mode", "computer_use_enabled", "concurrency_lock_enabled", "result_check_execute_tests", "config_sources",
    "self_dev_worktree", "protect_running_runtime",
])
def test_security_relevant_keys_are_never_model_writable(key):
    spec = parameter_registry()[key]
    assert spec.safety == SAFETY_BOUNDARY and spec.writable is False


@pytest.mark.parametrize("key", ["max_tokens", "request_timeout", "memory_compact_auto_trigger_percent",
                                 "enable_self_learning", "tool_read_max_chars", "agent_name"])
def test_ordinary_parameters_are_model_writable(key):
    assert parameter_registry()[key].writable is True


def test_secret_like_keys_are_writable_only_when_explicitly_listed_and_always_masked():
    for spec in parameter_registry().values():
        if spec.masked:
            assert spec.writable is (spec.key in TUNABLE_KEYS), spec.key
    secret = parameter_registry()["feishu_app_secret"]
    assert secret.masked and secret.writable


def test_search_prefers_key_matches_then_descriptions():
    keys = [spec.key for spec in search_parameters("max_tokens", limit=5)]
    assert keys[0] == "max_tokens"
    assert any(spec.key == "max_tokens" for spec in search_parameters("64K"))


def test_applied_value_with_hands_the_rule_the_loaded_type(monkeypatch):
    """回执里的新值是文本；派生规则拿到的必须是重启后加载出来的类型（整数就是 int），不能靠规则自己容忍字符串。"""
    from agent_py_agent.agent.settings import parameter_registry as registry

    monkeypatch.setitem(registry._APPLIED_RULES, "max_tokens", (lambda config: type(config.max_tokens).__name__, "类型"))
    assert applied_value_with("max_tokens", "65536", SimpleNamespace(max_tokens=1)) == ("int", "类型")
