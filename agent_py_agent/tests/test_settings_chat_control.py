"""聊天 `/settings`：在 TUI 与 IM 里查找、查看、修改、恢复默认、回滚参数（参数中心，2026-09-27）。

背景：用户几乎不用命令行，并希望 my-agent 与自己都能改更多参数、改错能回滚。本测试锁定：解析拒绝式校验且 set 的值保留原样、
命令目录走 Gateway、TUI 文本还原与本地模式拒绝、Gateway 分派不落入 steer/stop、只有管理员可用（非管理员看不到任何参数）、
读写走参数中心并记账、边界项拒绝、普通异常不承诺“没有改动”。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.command_catalog import match_conversation_command
from agent_py_agent.agent.conversation.control_commands import (
    ConversationControlCommand,
    parse_conversation_control,
)
from agent_py_agent.agent.gateway_parts import settings_control_service as module
from agent_py_agent.agent.settings.config import load_config
from agent_py_agent.agent.settings.parameter_registry import (
    COMMON_KEYS,
    LOADER_METADATA_KEYS,
    MANAGED_ELSEWHERE_KEYS,
    listed_parameters,
    parameter_registry,
    search_parameters,
)
from agent_py_agent.cli.chat_parts import control_runtime

_ADMIN = SimpleNamespace(owner_provider="local", owner_kind="main", owner_id="main")
_FEISHU_USER = SimpleNamespace(owner_provider="feishu", owner_kind="users", owner_id="ou_x")


def _settings(text: str) -> ConversationControlCommand:
    command = parse_conversation_control(text, reject_unknown_slash=True)
    assert command is not None and command.kind == "settings"
    return command


# 假配置等于“Gateway 按 user_config 夹具启动”：运行值与用户配置一致（request_timeout 300，其余默认）。
def _run(monkeypatch, path, text: str, *, home=_ADMIN):
    monkeypatch.setattr(module, "_scoped_home", lambda _agent, _scope: home)
    agent = SimpleNamespace(config=SimpleNamespace(config_path=str(path), max_tokens=65536, request_timeout=300))
    return module.execute_settings_control(agent, _settings(text), None)


@pytest.fixture
def user_config(tmp_path):
    path = tmp_path / "desktop.yaml"
    path.write_text('agent_name: "myagent"\nrequest_timeout: 300\n', encoding="utf-8")
    return path


@pytest.mark.parametrize("text,operation,valid", [
    ("/settings", "overview", True), ("/settings help", "help", True), ("/settings search 输出 上限", "search", True),
    ("/settings show MAX_TOKENS", "show", True), ("/settings show ../x", "show", False),
    ("/settings set max_tokens 32768", "set", True), ("/settings set max_tokens", "set", False),
    ("/settings set agent_name 我的 助手", "set", True), ("/settings reset max_tokens", "reset", True),
    ("/settings history", "history", True), ("/settings history bad-key", "history", False),
    ("/settings revert a1b2c3", "revert", True), ("/settings revert a1b", "revert", False),
    ("/settings delete max_tokens", "unknown", False), ("/settings all", "all", True), ("/settings ALL", "all", True),
    ("/settings all max_tokens", "all", False),
])
def test_parser_validates_keys_ids_and_keeps_set_values(text, operation, valid):
    command = _settings(text)
    assert (command.operation, command.valid) == (operation, valid)
    assert "/settings set" in command.usage
    if text == "/settings set agent_name 我的 助手":
        assert command.value == "set agent_name 我的 助手"


def test_catalog_routes_through_gateway_and_tui_round_trips_the_text():
    assert match_conversation_command("/settings set max_tokens 32768") is not None
    command = _settings("/settings SET Max_Tokens 32768")
    text = control_runtime._command_text(command)
    assert text == "/settings set max_tokens 32768" and _settings(text) == command
    assert match_conversation_command("/settings all") is not None
    everything = _settings("/settings ALL")
    assert control_runtime._command_text(everything) == "/settings all" and _settings("/settings all") == everything


def test_local_tui_mode_refuses_instead_of_falling_into_stop():
    execution = SimpleNamespace(state=SimpleNamespace(request_id="", running=True))
    result = control_runtime._execute_local_control(execution, _settings("/settings"))
    assert result.kind == "settings" and result.ok is False and "Gateway" in result.message


def test_gateway_dispatch_reaches_the_settings_service(monkeypatch):
    from agent_py_agent.agent.gateway_parts import control_service

    seen = []
    monkeypatch.setattr(module, "execute_settings_control",
                        lambda agent, command, scope: seen.append(command.operation) or "handled")
    assert control_service.execute_gateway_conversation_control(None, None, _settings("/settings"), None) == "handled"
    assert seen == ["overview"]


def test_only_admins_can_see_or_change_parameters(monkeypatch, user_config):
    refused = _run(monkeypatch, user_config, "/settings show max_tokens", home=_FEISHU_USER)
    assert refused.ok is False and "管理员" in refused.message and "65536" not in refused.message
    blank = SimpleNamespace(owner_provider="", owner_kind="", owner_id="")
    assert _run(monkeypatch, user_config, "/settings", home=blank).ok is False


def test_admin_can_find_change_review_and_revert(monkeypatch, user_config):
    overview = _run(monkeypatch, user_config, "/settings")
    assert overview.ok and "request_timeout = 300" in overview.message and "/restart" in overview.message
    found = _run(monkeypatch, user_config, "/settings search max_tokens")
    assert found.ok and found.message.splitlines()[1].startswith("- max_tokens［可改］当前 65536")
    shown = _run(monkeypatch, user_config, "/settings show max_tokens")
    assert "64K" in shown.message and "未覆盖" in shown.message and "可以修改" in shown.message
    changed = _run(monkeypatch, user_config, "/settings set max_tokens 32768")
    assert changed.ok and "已把 max_tokens 改为 32768" in changed.message and "/restart" in changed.message
    assert load_config(user_config).max_tokens == 32768
    history = _run(monkeypatch, user_config, "/settings history")
    change_id = history.message.splitlines()[1].split()[1]
    assert "max_tokens set" in history.message and "（chat）" in history.message
    reverted = _run(monkeypatch, user_config, f"/settings revert {change_id}")
    assert reverted.ok and "max_tokens:" not in user_config.read_text(encoding="utf-8")


# P18 真实验收：internal 经真实调度入口（会统一带上 capability_path）也要能查到常数，不能被兜底吞成“读不到”。
def test_internal_lists_code_constants_through_the_real_dispatch(monkeypatch, user_config):
    found = _run(monkeypatch, user_config, "/settings internal TOOL_PREVIEW_MAX_LINE_COUNT")
    assert found.ok, found.message
    assert "TOOL_PREVIEW_MAX_LINE_COUNT" in found.message and "只读" in found.message


# P18 验收后续（2026-10-02）：capability 文件里的覆盖要算进 show 的“用户配置里”，
# runtime_guard 的 show 说法与 set 被拒一致，回执“原来/现在是 默认值”不再多一个空格。
def _agent_with_capability(tmp_path, cap_content: str | None):
    path = tmp_path / "desktop.yaml"
    path.write_text('agent_name: "myagent"\n', encoding="utf-8")
    cap_path = tmp_path / "config" / "capability_config.yaml"
    if cap_content is not None:
        cap_path.parent.mkdir(parents=True)
        cap_path.write_text(cap_content, encoding="utf-8")
    return SimpleNamespace(root=str(tmp_path), config=SimpleNamespace(config_path=str(path)),
                           capability_config_path=str(cap_path))


def test_show_counts_capability_file_as_user_override(monkeypatch, tmp_path):
    """capability 键改过之后，show 的“用户配置里”要算上 capability 文件里的覆盖（P18 观察项）。"""
    monkeypatch.setattr(module, "_scoped_home", lambda _agent, _scope: _ADMIN)
    agent = _agent_with_capability(tmp_path, "capability_candidate_limit: 7\n")
    shown = module.execute_settings_control(agent, _settings("/settings show capability_candidate_limit"), None)
    assert shown.ok, shown.message
    assert "当前运行值：7" in shown.message
    assert "用户配置里：7" in shown.message
    assert "未覆盖" not in shown.message


def test_show_runtime_guard_wording_matches_the_set_refusal(monkeypatch, tmp_path):
    """runtime_guard 没有用户覆盖层：show 的说法与 set 被拒时一致（P18 观察项）。"""
    monkeypatch.setattr(module, "_scoped_home", lambda _agent, _scope: _ADMIN)
    agent = SimpleNamespace(root=str(tmp_path),
                            config=SimpleNamespace(config_path=str(tmp_path / "desktop.yaml")))
    shown = module.execute_settings_control(agent, _settings("/settings show repeat_fail_threshold"), None)
    assert shown.ok, shown.message
    assert "不能在这里修改：属于 runtime_guard 配置" in shown.message
    assert "只能查看和搜索" in shown.message
    assert "宿主入口或配置文件里改" not in shown.message
    refused = module.execute_settings_control(agent, _settings("/settings set repeat_fail_threshold 5"), None)
    assert refused.ok is False and "只能查看和搜索" in refused.message


def test_set_and_revert_receipts_have_no_double_space_before_default(monkeypatch, tmp_path):
    """回执“原来 是默认值”“现在是 默认值”中间多了一个空格（P18 观察项），已修。"""
    monkeypatch.setattr(module, "_scoped_home", lambda _agent, _scope: _ADMIN)
    path = tmp_path / "desktop.yaml"
    path.write_text('agent_name: "myagent"\n', encoding="utf-8")
    agent = SimpleNamespace(root=str(tmp_path),
                            config=SimpleNamespace(config_path=str(path), request_timeout=240))
    changed = module.execute_settings_control(agent, _settings("/settings set dynamic_timeout_min 40"), None)
    assert changed.ok and "（原来是默认值）" in changed.message, changed.message
    assert "原来 是默认值" not in changed.message
    history = module.execute_settings_control(agent, _settings("/settings history dynamic_timeout_min"), None)
    change_id = history.message.splitlines()[1].split()[1]
    reverted = module.execute_settings_control(agent, _settings(f"/settings revert {change_id}"), None)
    assert reverted.ok and "现在是默认值" in reverted.message, reverted.message
    assert "现在是 默认值" not in reverted.message


def test_show_reports_the_applied_output_cap_for_the_default_model(monkeypatch, user_config):
    monkeypatch.setattr(module, "_scoped_home", lambda _agent, _scope: _ADMIN)
    agent = SimpleNamespace(config=SimpleNamespace(
        config_path=str(user_config), max_tokens=65536, model_context_window_tokens=131072, request_timeout=300))
    shown = module.execute_settings_control(agent, _settings("/settings show max_tokens"), None)
    assert shown.ok and "当前运行值：65536" in shown.message and "实际效果：32768" in shown.message
    assert "/model" in shown.message
    plain = module.execute_settings_control(agent, _settings("/settings show request_timeout"), None)
    assert plain.ok and "实际效果" not in plain.message


def test_boundary_and_unexpected_failures_are_reported_honestly(monkeypatch, user_config):
    boundary = _run(monkeypatch, user_config, "/settings set api_base http://evil.example")
    assert boundary.ok is False and "安全边界" in boundary.message
    shown = _run(monkeypatch, user_config, "/settings show system_prompt")
    assert "不能在这里修改" in shown.message

    def _broken(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(module, "set_parameter", _broken)
    failed = _run(monkeypatch, user_config, "/settings set max_tokens 1024")
    assert failed.ok is False and "没能完整确认" in failed.message and "没有改动" not in failed.message


def test_set_and_show_tell_the_reasoning_effect_on_the_default_model(monkeypatch, user_config):
    """opencode.ai 不在已确认的推理参数名单里：/settings set 与 show 都如实说“不改变请求”，而不是只说写入成功。"""
    monkeypatch.setattr(module, "_scoped_home", lambda _agent, _scope: _ADMIN)
    agent = SimpleNamespace(config=SimpleNamespace(
        config_path=str(user_config), max_tokens=65536, model_context_window_tokens=131072,
        api_base="https://opencode.ai/zen/v1", model_backend="openai_compatible"))
    changed = module.execute_settings_control(agent, _settings("/settings set model_reasoning_effort max"), None)
    assert changed.ok and "已把 model_reasoning_effort 改为 max" in changed.message
    assert "按新值在默认模型上的实际效果：当前模型不支持调节" in changed.message and "/model" in changed.message
    shown = module.execute_settings_control(agent, _settings("/settings show model_reasoning_effort"), None)
    assert "实际效果：不额外发送推理参数" in shown.message  # 运行中的配置仍是 auto，重启后才按新值
    capped = module.execute_settings_control(agent, _settings("/settings set max_tokens 32768"), None)
    assert "按新值在默认模型上的实际效果：32768" in capped.message
    plain = module.execute_settings_control(agent, _settings("/settings set request_timeout 200"), None)
    assert plain.ok and "实际效果" not in plain.message
    reset = module.execute_settings_control(agent, _settings("/settings reset model_reasoning_effort"), None)
    assert reset.ok and "按新值在默认模型上的实际效果：不额外发送推理参数" in reset.message
    history = module.execute_settings_control(agent, _settings("/settings history model_reasoning_effort"), None)
    change_id = history.message.splitlines()[1].split()[1]
    reverted = module.execute_settings_control(agent, _settings(f"/settings revert {change_id}"), None)
    assert reverted.ok and "现在是 max" in reverted.message and "实际效果：当前模型不支持调节" in reverted.message


def test_default_view_lists_only_the_common_parameters(monkeypatch, user_config):
    """用户说“把用户当傻瓜”：/settings 默认只列常用参数，说清总数，全部参数在 /settings all。"""
    user_config.write_text('request_timeout: 300\ndynamic_timeout_min: 40\n', encoding="utf-8")
    shown = _run(monkeypatch, user_config, "/settings")
    listed = [line[2:].split(" = ", 1)[0] for line in shown.message.splitlines() if line.startswith("- ")]
    assert shown.ok and listed == list(COMMON_KEYS)
    assert "- request_timeout = 300（改过，默认 240）：HTTP 基础超时秒数" in shown.message
    assert "dynamic_timeout_min" not in shown.message and "另外你还改过 1 个其它参数" in shown.message
    assert f"一共 {len(listed_parameters())} 项参数" in shown.message and "/settings all" in shown.message
    assert "- memory_compact_auto_trigger_percent = 90：" in shown.message
    # 布尔值按配置文件写法显示，不能因为 False 是假值就显示成“（空）”。
    assert "- enable_self_learning = false：自学习（默认关闭）" in shown.message
    assert "- enable_subagents = true：" in shown.message


def test_default_view_marks_a_change_that_waits_for_restart(monkeypatch, user_config):
    monkeypatch.setattr(module, "_scoped_home", lambda _agent, _scope: _ADMIN)
    agent = SimpleNamespace(config=SimpleNamespace(config_path=str(user_config), request_timeout=240))
    shown = module.execute_settings_control(agent, _settings("/settings"), None)
    assert "- request_timeout = 240（改过，默认 240）（已改成 300，发 /restart 后生效）" in shown.message


def test_all_view_lists_every_parameter_with_changes_and_boundaries(monkeypatch, user_config):
    assert _run(monkeypatch, user_config, "/settings set dynamic_timeout_min 40").ok
    shown = _run(monkeypatch, user_config, "/settings all")
    lines = shown.message.splitlines()
    assert shown.ok and len([line for line in lines if line.startswith("- ")]) >= len(listed_parameters())
    assert str(user_config) in shown.message and "最近修改：" in shown.message and "与默认值不同的参数：2 个" in shown.message
    assert "- request_timeout = 300［改过］" in lines and "- dynamic_timeout_min = 30［改过］" in lines
    assert any(line.startswith("- api_base = ") and line.endswith("［安全边界］") for line in lines)
    headers = [line for line in lines if line.startswith("【")]
    assert any(line.startswith("【模型请求】") for line in headers) and headers[-1].startswith("【其它】")


def test_loader_metadata_is_hidden_from_lists_and_search_but_show_still_works(monkeypatch, user_config):
    """配置路径、来源、分层、告警是加载器元数据，不是参数：列表、计数、搜索都不出现；字段仍在，show 仍能看。"""
    from agent_py_agent.agent.settings.user_config_capability import capability_summary

    registry = parameter_registry()
    assert set(registry) >= LOADER_METADATA_KEYS and not LOADER_METADATA_KEYS & set(listed_parameters())
    assert len(listed_parameters()) == len(registry) - len(LOADER_METADATA_KEYS) - len(MANAGED_ELSEWHERE_KEYS)
    summary = capability_summary()
    assert summary["writable_count"] + summary["boundary_count"] == len(listed_parameters())
    assert not LOADER_METADATA_KEYS & {spec.key for spec in search_parameters("config", limit=0)}
    lines = _run(monkeypatch, user_config, "/settings all").message.splitlines()
    assert not [line for line in lines if any(line.startswith(f"- {key} = ") for key in LOADER_METADATA_KEYS)]
    found = _run(monkeypatch, user_config, "/settings search config_warnings").message
    assert not any(f"- {key}［" in found for key in LOADER_METADATA_KEYS)
    detail = _run(monkeypatch, user_config, "/settings show config_warnings")
    assert detail.ok and detail.message.startswith("config_warnings（") and "不能在这里修改" in detail.message


def test_managed_elsewhere_values_are_hidden_from_lists_and_search_but_show_still_works(monkeypatch, user_config):
    """/model 登录生成的 OAuth 运行引用由模型菜单写入、请勿手填：列表和搜索不出现，show 仍能查看且仍是边界。"""
    registry = parameter_registry()
    assert {"model_auth_ref"} == MANAGED_ELSEWHERE_KEYS and set(registry) >= MANAGED_ELSEWHERE_KEYS
    assert not MANAGED_ELSEWHERE_KEYS & set(listed_parameters())
    assert not MANAGED_ELSEWHERE_KEYS & {spec.key for spec in search_parameters("model", limit=0)}
    lines = _run(monkeypatch, user_config, "/settings all").message.splitlines()
    assert not [line for line in lines if line.startswith("- model_auth_ref = ")]
    detail = _run(monkeypatch, user_config, "/settings show model_auth_ref")
    assert detail.ok and detail.message.startswith("model_auth_ref（") and "不能在这里修改" in detail.message
