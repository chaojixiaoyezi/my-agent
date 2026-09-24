from __future__ import annotations

from dataclasses import replace
from unittest.mock import Mock

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from agent_py_agent.agent.command_arguments import (
    ArgumentSpec,
    CommandActionSpec,
    CommandArgumentError,
)
from agent_py_agent.agent.command_binding import render_action_help
from agent_py_agent.agent.plugin_commands import (
    PluginCommandSpec,
    parse_plugin_command,
    plugin_command_response,
    render_plugin_use_card,
)
from agent_py_agent.agent.plugin_completion import complete_plugin_command
from agent_py_agent.cli.chat_parts.slash_commands import CHAT_HELP_TEXT
from agent_py_agent.cli.chat_parts.tui_input import TuiInputCompleter


@pytest.mark.parametrize(("text", "ok", "reason", "snippet"), [
    ("/plugins", True, None, "插件业务入口"),
    ("/PLUGINS\u3000help install", True, None, "用法：/plugins install <source>"),
    ("/plugins install --help", True, None, "默认停用"),
    ("/plugins help --help", True, None, "用法：/plugins help [action]"),
    ('/plugins install "中文 a.whl"', False, "not_implemented", "本次没有执行操作"),
    ("/plugins list -e", False, "not_implemented", "尚未开放"),
    ("/plugins install", False, "missing_argument", "用法：/plugins install <source>"),
    ("/plugins list --unknown", False, "unknown_option", "用法：/plugins list"),
    ("/plugins help missing", False, "unknown_action", "未声明"),
    ("/plugins missing", False, "unknown_action", "未声明"),
    ('/plugins install "未闭合', False, "unclosed_quote", "引号尚未闭合"),
    ("/plugins@", False, "invalid_plugin_id", "插件 ID"),
    ("/plugins@../../bad", False, "invalid_plugin_id", "插件 ID"),
    ("/plugins@demo run", False, "unknown_plugin", "当前目录没有"),
])
def test_public_response_classifies_help_and_errors(text, ok, reason, snippet):
    result = plugin_command_response(text)
    assert result["kind"] == "plugin_command" and result["ok"] is ok
    assert result.get("reason") == reason and snippet in result["message"]
    if not ok:
        assert result["error_code"] in {"INVALID_COMMAND_ARGUMENTS", "UNKNOWN_PLUGIN", "PLUGIN_COMMAND_UNAVAILABLE"}


@pytest.fixture
def plugin():
    return PluginCommandSpec("Demo", "说明", (
        CommandActionSpec("run", "运行", (
            ArgumentSpec("count", "条数", ("-n", "--count"), "integer", required=True),
            ArgumentSpec("level", "等级", ("--level",), choices=("低", "高")),
            ArgumentSpec("path", "路径", ("--path",), path=True),
            ArgumentSpec("files", "文件", multiple=True),
        ), kind="tool", target="demo.run"),
    ), enabled=True, default_action="run")


def test_host_description_selects_target_without_executing_it(plugin):
    plain = parse_plugin_command('/plugins@Demo -n 2 --path "C:\\new folder\\" -- -x', plugins=(plugin,))
    assert plain.plugin is plugin and plain.action.target == "demo.run"
    assert dict(plain.arguments.values) == {"count": 2, "path": "C:\\new folder\\", "files": ("-x",)}
    assert not plain.help_requested
    assert parse_plugin_command("/plugins@Demo", plugins=(plugin,)).help_requested
    assert parse_plugin_command("/plugins@Demo --help", plugins=(plugin,)).help_requested
    assert parse_plugin_command("/plugins@Demo run --help", plugins=(plugin,)).help_requested
    with pytest.raises(CommandArgumentError) as caught:
        parse_plugin_command("/plugins@demo", plugins=(plugin,))
    assert caught.value.reason == "unknown_plugin"


def test_use_card_reads_action_and_settings_declarations_without_private_values(plugin):
    card = render_plugin_use_card(plugin, {
        "properties": {"token": {"type": "string"}, "mode": {"type": "string"}},
        "required": ["token"],
    })
    assert "/plugins@Demo run" in card and "普通中文示例：" in card
    assert render_action_help("/plugins@Demo", plugin.actions[0]).splitlines()[1] in card
    assert card.count("请用 Demo 插件") == 2
    assert "必填设置：token" in card and "可配置项：token、mode" in card
    assert "使用已启用插件的动作" in CHAT_HELP_TEXT
    assert "插件使用入口（尚未开放）" not in CHAT_HELP_TEXT


def test_use_card_does_not_invent_slash_action_for_tool_only_plugin():
    plugin = PluginCommandSpec("only-tools", "提供工具", (), enabled=True, package_version="1.0")
    card = render_plugin_use_card(plugin, {"type": "object", "properties": {}, "required": []})
    assert "未声明显式命令" in card and "/plugins@only-tools" not in card
    assert card.count("请用 only-tools 插件") == 2


def test_stopped_plugin_help_remains_static_and_business_is_not_authorized(plugin):
    disabled = PluginCommandSpec(plugin.plugin_id, plugin.summary, plugin.actions)
    parsed = parse_plugin_command("/plugins@Demo run --help", plugins=(disabled,))
    assert parsed.help_requested and not parsed.plugin.enabled
    assert not complete_plugin_command("/plugins@D", plugins=(disabled,))
    paths = Mock(side_effect=AssertionError("停用插件不扫描业务路径"))
    for text in ("/plugins@Demo ", "/plugins@Demo run "):
        assert {item.label for item in complete_plugin_command(text, plugins=(disabled,), paths=paths, requested=True)} == {"-h", "--help"}
    assert complete_plugin_command("/plugins@Demo run --path ", plugins=(disabled,), paths=paths) == ()
    paths.assert_not_called()


def test_default_action_first_positional_can_complete_without_action_name():
    plugin = PluginCommandSpec("demo", "默认位置参数", (
        CommandActionSpec("run", "查看", (ArgumentSpec("name", "名称", choices=("alpha", "beta")),)),
    ), enabled=True, default_action="run")
    result = complete_plugin_command("/plugins@demo al", plugins=(plugin,))
    assert [item.label for item in result] == ["alpha"]
    assert parse_plugin_command("/plugins@demo alpha", plugins=(plugin,)).arguments.values["name"] == "alpha"


def test_default_positional_candidate_cannot_shadow_an_explicit_action():
    plugin = PluginCommandSpec("demo", "", (
        CommandActionSpec("run", "默认动作", (ArgumentSpec("name", "位置值", choices=("status", "run", "alpha")),)),
        CommandActionSpec("status", "状态动作", (ArgumentSpec("id", "编号", required=True),)),
    ), enabled=True, default_action="run")
    choices = complete_plugin_command("/plugins@demo sta", plugins=(plugin,))
    assert len(choices) == 1 and choices[0].label == "status" and choices[0].summary == "状态动作"
    same_name = complete_plugin_command("/plugins@demo ru", plugins=(plugin,))
    assert len(same_name) == 1 and same_name[0].summary == "默认动作"
    explicit = parse_plugin_command("/plugins@demo run status", plugins=(plugin,))
    assert explicit.action.name == "run" and explicit.arguments.values["name"] == "status"


@pytest.mark.parametrize("text", ["请解释 /plugins@demo", "/plugins/readme.md", "/goal read --help"])
def test_non_plugin_input_is_untouched(text):
    assert parse_plugin_command(text) is None
    assert plugin_command_response(text) is None


@pytest.mark.parametrize(("text", "expected"), [
    ("/plugins ", {"help", "-h", "--help"}),
    ("/plugins help in", {"info", "install"}),
    ("/plugins list --e", {"--enabled"}),
    ("/plugins list -e --e", set()),
    ("/plugins list -- --", set()),
    ('/plugins help "in', {"info", "install"}),
])
def test_completion_follows_declared_actions_and_binding_state(text, expected):
    paths = Mock(side_effect=AssertionError("非路径参数不能扫描目录"))
    result = complete_plugin_command(text, paths=paths)
    assert {item.label for item in result} == expected
    paths.assert_not_called()


def test_plugin_value_and_namespace_completion_share_host_description(plugin):
    assert [item.text for item in complete_plugin_command("/plugins@D", plugins=(plugin,))] == ["/plugins@Demo"]
    assert {item.label for item in complete_plugin_command("/plugins@Demo run --level ", plugins=(plugin,))} == {"低", "高"}
    result = complete_plugin_command("/plugins@Demo run --level=高", plugins=(plugin,))
    assert len(result) == 1 and result[0].label == "--level=高"
    text = "/plugins@Demo -n 1 "
    assert "--count" not in {item.label for item in complete_plugin_command(text, plugins=(plugin,))}
    assert complete_plugin_command("/plugins@Demo --help ", plugins=(plugin,)) == ()


def test_unfinished_quote_path_completion_preserves_prefix_and_roundtrips():
    text = '/plugins install "中文 '
    paths = Mock(return_value=[("中文 路径/a.whl", False)])
    candidates = complete_plugin_command(text, paths=paths)
    paths.assert_called_once_with("中文 ")
    assert len(candidates) == 1
    item = candidates[0]
    completed = text[:item.start] + item.text
    assert completed.startswith("/plugins install ")
    assert parse_plugin_command(completed).arguments.values["source"] == "中文 路径/a.whl"
    assert item.append_space


def test_value_completion_never_changes_a_literal_into_an_option():
    plugin = PluginCommandSpec("demo", "", (
        CommandActionSpec("run", "", (
            ArgumentSpec("label", "标签", ("--label",), choices=("-x", "normal")),
            ArgumentSpec("file", "文件", choices=("--label", "normal")),
        )),
    ), enabled=True)
    separated = complete_plugin_command("/plugins@demo run --label ", plugins=(plugin,))
    assert {item.label for item in separated} == {"normal"}
    inline = complete_plugin_command("/plugins@demo run --label=", plugins=(plugin,))
    assert {item.label for item in inline} == {"--label=-x", "--label=normal"}
    ended = complete_plugin_command("/plugins@demo run -- ", plugins=(plugin,), requested=True)
    assert {item.label for item in ended} == {"--label", "normal"}
    for item in ended:
        text = "/plugins@demo run -- " + item.text
        assert parse_plugin_command(text, plugins=(plugin,)).arguments.values["file"] == item.label


@pytest.mark.parametrize("text", ["/plugins", "/plugins help", "/plugins help install", "/plugins help install ", "/plugins list -e ", "/plugins list -e", "/plugins list --help"])
def test_complete_management_input_does_not_get_optional_flags_inserted_by_enter(tmp_path, text):
    completer = TuiInputCompleter(tmp_path)
    assert list(completer.get_completions(Document(text), CompleteEvent())) == []
    if text.endswith(" "):
        explicit = list(completer.get_completions(Document(text), CompleteEvent(completion_requested=True)))
        assert {item.text for item in explicit} >= {"-h", "--help"}
        assert all(item.enter_action == "apply" for item in explicit)


def test_apply_completion_then_enter_submits_the_original_help_target(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from prompt_toolkit.buffer import Buffer, CompletionState

    from agent_py_agent.cli.chat_parts import tui_keybindings
    from agent_py_agent.cli.chat_parts.tui_input import apply_selected_completion

    completer = TuiInputCompleter(tmp_path)
    buffer = Buffer()
    buffer.set_document(Document("/plugins help ins"))
    items = list(completer.get_completions(buffer.document, CompleteEvent()))
    assert len(items) == 1
    buffer.complete_state = CompletionState(buffer.document, items, complete_index=0)
    apply_selected_completion(buffer)
    assert buffer.text == "/plugins help install "
    assert list(completer.get_completions(buffer.document, CompleteEvent())) == []
    submitted = []
    monkeypatch.setattr(tui_keybindings, "_submit_input_area", lambda _event, params: submitted.append(params.input_area.buffer.text))
    tui_keybindings._handle_enter_keybinding(SimpleNamespace(), SimpleNamespace(input_area=SimpleNamespace(buffer=buffer)))
    assert submitted == ["/plugins help install "]


def test_real_tui_completion_only_fills_declared_path_and_never_submits(tmp_path, monkeypatch):
    from agent_py_agent.cli.chat_parts import tui_input

    paths = Mock(return_value=[("中文 a.whl", False)])
    monkeypatch.setattr(tui_input, "_path_candidates", paths)
    completer = TuiInputCompleter(tmp_path)
    text = '/plugins install "中文 '
    result = list(completer.get_completions(Document(text), CompleteEvent()))
    assert result and all(item.enter_action == "apply" for item in result)
    paths.assert_called_once_with(completer.workspace, "中文 ")
    paths.reset_mock()
    assert list(completer.get_completions(Document("/plugins@unknown @file"), CompleteEvent())) == []
    assert list(completer.get_completions(Document("/plugins help install", cursor_position=17), CompleteEvent())) == []
    paths.assert_not_called()


def test_use_card_gives_no_spoken_example_for_display_only_actions(plugin):
    # 真实 TUI：卡片曾建议"请用插件打开面板"，模型无从调用，只能回答没有这个能力
    display = replace(plugin.actions[0], kind="display", target="line", summary="打开或关闭活动面板")
    card = render_plugin_use_card(replace(plugin, actions=(display,)), {})
    assert "普通中文示例：" not in card and "请用 Demo 插件" not in card
    assert "普通中文请求不会打开面板" in card and "/plugins@Demo run" in card
