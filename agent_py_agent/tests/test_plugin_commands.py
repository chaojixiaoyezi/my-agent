from __future__ import annotations

from unittest.mock import Mock

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from agent_py_agent.agent.command_arguments import (
    ArgumentSpec,
    CommandActionSpec,
    CommandArgumentError,
)
from agent_py_agent.agent.plugin_commands import (
    PluginCommandSpec,
    parse_plugin_command,
    plugin_command_response,
)
from agent_py_agent.agent.plugin_completion import complete_plugin_command
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


def test_stopped_plugin_help_remains_static_and_business_is_not_authorized(plugin):
    disabled = PluginCommandSpec(plugin.plugin_id, plugin.summary, plugin.actions)
    parsed = parse_plugin_command("/plugins@Demo run --help", plugins=(disabled,))
    assert parsed.help_requested and not parsed.plugin.enabled
    assert not complete_plugin_command("/plugins@D", plugins=(disabled,))
    paths = Mock(side_effect=AssertionError("停用插件不扫描业务路径"))
    for text in ("/plugins@Demo ", "/plugins@Demo run "):
        assert {item.label for item in complete_plugin_command(text, plugins=(disabled,), paths=paths)} == {"-h", "--help"}
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
    ended = complete_plugin_command("/plugins@demo run -- ", plugins=(plugin,))
    assert {item.label for item in ended} == {"--label", "normal"}
    for item in ended:
        text = "/plugins@demo run -- " + item.text
        assert parse_plugin_command(text, plugins=(plugin,)).arguments.values["file"] == item.label


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
