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
    COMMON_KEYS,
    SAFETY_BOUNDARY,
    SAFETY_FREE,
    _descriptions_from_lines,
    applied_value,
    applied_value_with,
    classify_safety,
    common_parameters,
    parameter_registry,
    search_parameters,
)
from agent_py_agent.agent.settings.user_config_capability import (
    BOUNDARY_KEYS,
    TUNABLE_KEYS,
    is_credential_key,
    mask_value,
)

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


def test_file_header_block_is_not_the_first_key_description():
    """以空行结束的首段注释说明整份文件；前端目录生成器用同一规则，两边第一个键的说明一致。"""
    lines = ["# 某某配置文件", "# 说明：整份文件的用途", "", "# 第一个键自己的说明", "first: 1", "second: 2"]
    assert _descriptions_from_lines(lines) == {"first": "第一个键自己的说明", "second": ""}


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


def test_common_tier_is_a_closed_list_of_existing_non_boundary_parameters():
    """常用层级是封闭的产品决策名单：键必须存在、不能是安全边界项、不重复；登记表的 common 标记与名单一致。"""
    registry = parameter_registry()
    assert len(COMMON_KEYS) == len(set(COMMON_KEYS))
    missing = [key for key in COMMON_KEYS if key not in registry]
    assert not missing, f"常用名单里的键不存在：{missing}"
    boundary = [key for key in COMMON_KEYS if registry[key].safety != SAFETY_FREE or not registry[key].writable]
    assert not boundary, f"常用参数不能是安全边界项：{boundary}"
    assert {key for key, spec in registry.items() if spec.common} == set(COMMON_KEYS)
    assert [spec.key for spec in common_parameters()] == list(COMMON_KEYS)


def test_common_parameters_all_have_descriptions():
    """常用参数必须都有中文说明，也不能进空说明基线；新加常用参数时先在 agent_config.yaml 写说明（2026-09-27 起过渡名单已清空）。"""
    groups = json.loads(_BASELINE.read_text(encoding="utf-8"))["groups"]
    baseline = {key for group in groups for key in group["keys"]}
    assert not set(COMMON_KEYS) & baseline, "常用参数不能留在空说明基线里：先写说明，再从基线删掉"
    registry = parameter_registry()
    undocumented = sorted(key for key in COMMON_KEYS if not registry[key].description)
    assert not undocumented, f"这些常用参数没有中文说明：{undocumented}"


def test_reasoning_effort_applied_value_says_whether_the_model_supports_it():
    unsupported = SimpleNamespace(model_reasoning_effort="max", **_OPENCODE)
    value, rule = applied_value("model_reasoning_effort", unsupported)
    control = resolved_reasoning_control("auto", _OPENCODE["api_base"], "openai_compatible")
    assert value == describe_reasoning_effect("max", control) and "不改变请求" in value and "思考控制方式" in rule
    deepseek = SimpleNamespace(**{**_OPENCODE, "api_base": "https://api.deepseek.com/v1"}, model_reasoning_effort="max")
    assert applied_value("model_reasoning_effort", deepseek)[0] == "按推理强度档位发送：最高（发送 max）。"
    assert "不额外发送" in applied_value("model_reasoning_effort", SimpleNamespace(model_reasoning_effort="auto"))[0]
    # Responses 协议与发送同一换算：ChatGPT 订阅旧档案没有声明档位时，最高档实际发 high，关闭思考不发字段。
    chatgpt = {"model_backend": "openai_responses", "api_base": "https://chatgpt.com/backend-api/codex", "model_reasoning_control": "auto"}
    assert "实际发送 high" in applied_value("model_reasoning_effort", SimpleNamespace(**chatgpt, model_reasoning_effort="max"))[0]
    assert "不改变请求" in applied_value("model_reasoning_effort", SimpleNamespace(**chatgpt, model_reasoning_effort="off"))[0]
    declared = SimpleNamespace(**chatgpt, model_reasoning_effort="max", model_reasoning_levels=["low", "high", "max"])
    assert applied_value("model_reasoning_effort", declared)[0].endswith("（发送 max）。")


def test_extended_effort_applied_value_names_the_provider_effort():
    for level, levels, sent in [("xhigh", ["high", "xhigh"], "xhigh"), ("ultra", ["high", "max", "ultra"], "ultra"),
                               ("ultra", ["high", "max"], "max"), ("xhigh", [], "high")]:
        config = SimpleNamespace(model_backend="openai_responses", model_reasoning_control="effort", api_base="",
                                 model_reasoning_effort=level, model_reasoning_levels=levels)
        assert f"发送 {sent}" in applied_value("model_reasoning_effort", config)[0]


def test_applied_value_with_uses_the_new_value_without_touching_the_config():
    config = SimpleNamespace(max_tokens=16384, model_context_window_tokens=131072, model_reasoning_effort="auto", **_OPENCODE)
    assert applied_value_with("max_tokens", "65536", config)[0] == 32768 and config.max_tokens == 16384
    assert "不改变请求" in applied_value_with("model_reasoning_effort", "max", config)[0]
    assert config.model_reasoning_effort == "auto"
    assert applied_value_with("request_timeout", "300", config) is None
    assert applied_value_with("max_tokens", "很多", config) is None and applied_value_with("max_tokens", "1", None) is None


def test_applied_value_uses_the_single_output_cap_formula():
    """64K 在 128K 窗口模型上实际是 32768；窗口没填（空或 0）按 128000 兜底；没有派生规则的参数不给实际效果。"""
    value, rule = applied_value("max_tokens", SimpleNamespace(max_tokens=65536, model_context_window_tokens=131072))
    assert value == 32768 and "窗口" in rule
    assert applied_value("max_tokens", SimpleNamespace(max_tokens=65536, model_context_window_tokens=0))[0] == 32000
    assert applied_value("max_tokens", SimpleNamespace(max_tokens=65536, model_context_window_tokens=None))[0] == 32000
    assert applied_value("request_timeout", SimpleNamespace(request_timeout=300)) is None
    assert applied_value("max_tokens", None) is None


def test_registry_covers_every_config_field_with_yaml_descriptions():
    registry = parameter_registry()
    # P17：登记表覆盖三份配置；主配置部分仍必须与 AgentConfig 字段一一对应（capability 31 / runtime_guard 23
    # 由各自来源测试锁定），防止登记表悄悄丢字段。
    assert {key for key, spec in registry.items() if spec.source == "agent"} == {item.name for item in fields(AgentConfig)}
    assert "64K" in registry["max_tokens"].description
    assert registry["max_tokens"].value_type == "int" and registry["enable_self_learning"].value_type == "bool"
    fuse = registry["goal_continuation_idle_limit"]
    assert fuse.default == 3 and fuse.value_type == "int" and fuse.writable
    assert "0 = 不限" in fuse.description


def test_extra_sources_are_registered_with_yaml_descriptions():
    """P17：capability（dataclass 默认值）、runtime_guard 两份配置全部进登记表并带说明。"""
    from agent_py_agent.agent.capability.config import CapabilityConfig

    registry = parameter_registry()
    capability_keys = {item.name for item in fields(CapabilityConfig)} - {"config_warnings"}
    assert {key for key, spec in registry.items() if spec.source == "capability"} == capability_keys
    assert {key for key, spec in registry.items() if spec.source == "runtime_guard"} == {
        "repeat_fail_threshold", "readonly_no_progress_threshold", "repeated_success_hint_threshold",
        "terminal_block_enabled", "repeated_failure_halt_threshold", "hard_failure_halt_enabled",
        "hard_failure_halt_threshold", "tool_rate_window_seconds", "tool_rate_max_calls",
        "tool_circuit_failure_threshold", "tool_circuit_backoff_seconds", "tool_rate_max_records",
        "unknown_command_allowlist", "unknown_command_window_seconds", "unknown_command_max_calls",
        "background_max_tool_rounds", "background_max_tool_calls_per_round",
        "provider_transient_auto_resume_delays_seconds", "provider_transient_auto_resume_total_budget_seconds",
        "provider_supply_backoff_base_seconds",
        "provider_supply_backoff_max_seconds", "provider_transient_redispatch_limit",
        "main_agent_auto_resume_attempt_limit",
    }
    # 默认值/类型与 dataclass 或 YAML 一致；说明全部非空（新键不允许进空说明基线）。
    assert registry["capability_candidate_limit"].default == CapabilityConfig().capability_candidate_limit == 5
    assert registry["capability_candidate_limit"].value_type == "int"
    assert registry["decision_subagent_model_candidate_profile_ids"].value_type == "list"
    assert registry["repeat_fail_threshold"].default == 10 and registry["repeat_fail_threshold"].value_type == "int"
    assert registry["tool_circuit_backoff_seconds"].default == [1, 2, 4, 8, 16, 30]
    assert all(spec.description for key, spec in registry.items() if spec.source != "agent")


@pytest.mark.parametrize("key", [
    *sorted(BOUNDARY_KEYS), "api_base", "model_backend", "system_prompt", "prompt_files",
    "audit_enabled", "enable_tools", "enable_gateway_restart_tool", "additional_write_roots", "mcp_servers",
    "execution_mode", "computer_use_enabled", "computer_use_observation_enabled", "config_sources",
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


# 名字里带 token/prompt/path/owner/home/audit/command，但只是数量、上限、间隔或超时的参数（2026-09-27 前被误判为边界）
_NUMERIC_KNOBS = [
    "home_lesson_stale_caveat_days",
    "background_owner_workers", "background_threads_per_owner",
    "owner_agent_idle_seconds", "owner_agent_pool_max_agents",
    "owner_maintenance_scan_interval_seconds",
]
_CREDENTIALS = ["api_key", "gateway_auth_token", "feishu_app_secret",
                "feishu_verification_token", "feishu_encrypt_key", "qq_app_secret"]


@pytest.mark.parametrize("key", _NUMERIC_KNOBS)
def test_numeric_knobs_with_pointer_like_names_are_free_and_shown_in_full(key):
    spec = parameter_registry()[key]
    assert spec.value_type in {"int", "float"} and spec.safety != SAFETY_BOUNDARY and spec.writable and not spec.masked
    assert mask_value(key, 1600) == "1600"


def test_new_credential_spellings_hit_no_registered_parameter():
    # is_credential_key 同时决定边界分类：补写法后核对，登记表里脱敏的仍只有这 8 个真凭据，没有误伤普通参数
    assert {key for key, spec in parameter_registry().items() if spec.masked} == set(_CREDENTIALS)


@pytest.mark.parametrize("key", _CREDENTIALS)
def test_real_credentials_stay_masked_boundary_everywhere(key):
    spec = parameter_registry()[key]
    assert spec.masked and spec.safety == SAFETY_BOUNDARY and is_credential_key(key)
    assert mask_value(key, "FAKE-1234567890") == "FAK***"  # 原来 api_key、gateway_auth_token 等在回显里是明文


def test_credential_names_match_whole_trailing_segments_only():
    assert all(is_credential_key(key) for key in ("token", "new_service_api_key", "bot_token", "smtp_password", "x_cookies"))
    assert not any(is_credential_key(key) for key in ("max_tokens", "input_media_token_reserve", "token_budget",
                                                      "model_context_window_tokens", "keyboard_shortcuts", "api_keys_count"))
    # 2026-09-27 补的常见写法，同样只认完整末尾片段
    assert all(is_credential_key(key) for key in ("db_pass", "ftp_passwd", "mysql_pwd", "ssh_private_key", "aws_secret_key",
                                                  "aws_secret_access_key", "basic_auth", "auth", "pass"))
    assert not any(is_credential_key(key) for key in ("auth_enabled", "access_mode", "path_access_mode", "oauth", "bypass",
                                                      "compass", "model_auth_ref", "aws_access_key_id", "pwd_hint"))
    # 凭据名即使是数字类型也仍是边界（不因“数字不指向任何东西”被放开）
    assert classify_safety("bot_token", "int") == SAFETY_BOUNDARY and classify_safety("bot_token_limit", "int") != SAFETY_BOUNDARY


@pytest.mark.parametrize("key", [
    "additional_write_roots", "prompt_files", "system_prompt", "my_agent_home", "self_dev_worktree", "path_dangerous_roots",
    "my_agent_owner_provider", "my_agent_owner_kind", "my_agent_owner_id", "feishu_app_id", "feishu_app_secret",
    "feishu_verification_token", "feishu_encrypt_key", "qq_app_id", "qq_app_secret", "gateway_port",
    "feishu_personal_idle_lock_seconds", "cli_audit_cleanup_days", "mcp_servers",
])
def test_paths_write_scope_channel_credentials_and_owner_identity_stay_boundary(key):
    """收紧只放开数字类旋钮：路径、写入范围、飞书/QQ 凭据、owner 身份、访问锁与审计保留期仍是边界（飞书凭据另有显式放行）。"""
    spec = parameter_registry()[key]
    assert spec.safety == SAFETY_BOUNDARY
    assert spec.writable is (key in TUNABLE_KEYS and key not in BOUNDARY_KEYS)



def test_corrected_descriptions_say_what_the_code_does():
    """注释错位曾让 additional_write_roots 拿到 prompt_files 的说明；lease_stale 实际管审计来源采集 worker，不是 Gateway 租约。"""
    registry = parameter_registry()
    roots = registry["additional_write_roots"].description
    assert "可写根" in roots and "prompt" not in roots
    assert "prompt 文件" in registry["prompt_files"].description
    lease = registry["lease_stale_without_heartbeat_seconds"].description
    assert "审计来源采集 worker" in lease and "租约" in lease and "Gateway" not in lease
