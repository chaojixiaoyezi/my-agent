from __future__ import annotations

import shlex

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agent_py_agent.agent.command_arguments import (
    ArgumentSpec,
    CommandActionSpec,
    CommandArgumentError,
    lex_command_arguments,
)
from agent_py_agent.agent.command_binding import bind_command_arguments, render_action_help


@pytest.mark.parametrize(("raw", "expected"), [
    ('"C:\\new folder\\中文.txt"', ("C:\\new folder\\中文.txt",)),
    ('"C:\\new folder\\"', ("C:\\new folder\\",)),
    ('"\\\\server\\share\\a b"', ("\\\\server\\share\\a b",)),
    ('a" b"c "" \'\' "\'" \'"\'', ("a bc", "", "", "'", '"')),
    ('"$HOME/$(test)" #literal | > ; `id`', ("$HOME/$(test)", "#literal", "|", ">", ";", "`id`")),
    ('a\u3000b\t"c\u3000d"\n末尾', ("a", "b", "c\u3000d", "末尾")),
    (r"a\ b", ("a\\", "b")),
])
def test_literal_argument_grammar(raw, expected):
    tokens = lex_command_arguments(raw)
    assert tuple(token.value for token in tokens) == expected
    for token in tokens:
        assert tuple(item.value for item in lex_command_arguments(raw[token.start:token.end])) == (token.value,)


@given(st.lists(st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=45), max_size=8))
def test_completion_quoting_roundtrips_literal_values(values):
    source = " ".join(shlex.quote(value) for value in values)
    assert [token.value for token in lex_command_arguments(source)] == values


def test_unclosed_quote_is_error_but_partial_lexing_keeps_original_range():
    raw = 'run --path "中文 C:\\new folder '
    with pytest.raises(CommandArgumentError) as caught:
        lex_command_arguments(raw)
    assert caught.value.reason == "unclosed_quote"
    partial = lex_command_arguments(raw, partial=True)
    assert [token.value for token in partial] == ["run", "--path", "中文 C:\\new folder "]
    assert partial[-1].start == raw.index('"')
    assert partial[-1].end == len(raw) and partial[-1].open_quote == '"'


@pytest.fixture
def action():
    return CommandActionSpec("run", "参数合同用例", (
        ArgumentSpec("all", "全部", ("-a", "--all"), "boolean"),
        ArgumentSpec("brief", "简略", ("-b", "--brief"), "boolean"),
        ArgumentSpec("count", "数量", ("-n", "--count"), "integer", default=3),
        ArgumentSpec("tag", "标签", ("-t", "--tag"), multiple=True),
        ArgumentSpec("level", "等级", ("--level",), choices=("低", "高")),
        ArgumentSpec("files", "文件", required=True, multiple=True, path=True),
    ), kind="tool", target="example.run")


def test_binding_values_aliases_defaults_and_terminator(action):
    tokens = tuple(token.value for token in lex_command_arguments('-ab -n -20 --tag="a b" -t 中文 --level 高 -- "-x" "--" | $(literal)'))
    bound = bind_command_arguments(action, tokens)
    assert dict(bound.values) == {
        "all": True, "brief": True, "count": -20, "tag": ("a b", "中文"),
        "level": "高", "files": ("-x", "--", "|", "$(literal)"),
    }
    assert bound.options_ended and not bound.help_requested
    assert bind_command_arguments(action, ("one",)).values["count"] == 3
    with pytest.raises(TypeError):
        bound.values["all"] = False


@pytest.mark.parametrize(("tokens", "reason"), [
    (("--count",), "missing_value"),
    (("--count", "--all"), "missing_value"),
    (("-n20",), "unknown_option"),
    (("-an", "2"), "unknown_option"),
    (("--all=true",), "unexpected_value"),
    (("-n=2",), "short_option_value"),
    (("--level", "中"), "invalid_choice"),
    (("-n", "xx"), "invalid_type"),
    (("--tag", "--all"), "missing_value"),
    (("-a", "--all"), "duplicate_argument"),
    (("--missing",), "unknown_option"),
    (("--all",), "missing_argument"),
    (("--help", "--missing"), "unknown_option"),
])
def test_invalid_binding_is_structured_without_fallback(action, tokens, reason):
    with pytest.raises(CommandArgumentError) as caught:
        bind_command_arguments(action, tokens)
    assert caught.value.reason == reason


def test_help_skips_required_values_but_literal_help_is_data(action):
    assert bind_command_arguments(action, ("--help",)).help_requested
    data = bind_command_arguments(action, ("--", "--help"))
    assert not data.help_requested and data.values["files"] == ("--help",)
    with pytest.raises(CommandArgumentError) as caught:
        bind_command_arguments(action, tuple(token.value for token in lex_command_arguments('"-x"')))
    assert caught.value.reason == "unknown_option"


def test_partial_binding_reports_value_position_without_inventing_value(action):
    bound = bind_command_arguments(action, ("-a", "--count"), partial=True)
    assert bound.awaiting.name == "count" and bound.seen == {"all"}
    assert not bound.options_ended
    ended = bind_command_arguments(action, ("one", "--"), partial=True)
    assert ended.options_ended and ended.positional_count == 0


@pytest.mark.parametrize("make", [
    lambda: ArgumentSpec("x", "", ("--help",)),
    lambda: ArgumentSpec("x", "", value_type="boolean"),
    lambda: ArgumentSpec("x", "", ("-x",), "boolean", choices=(False,)),
    lambda: ArgumentSpec("x", "", required=True, default="x"),
    lambda: ArgumentSpec("x", "", value_type="integer", default=True),
    lambda: ArgumentSpec("x", "", value_type="number", default=float("nan")),
    lambda: ArgumentSpec("x", "", choices=("x",), default="y"),
    lambda: ArgumentSpec("x", "", multiple=True, default="x"),
    lambda: ArgumentSpec("x", "", options=["-x"]),
    lambda: CommandActionSpec("run", "", (ArgumentSpec("x", ""), ArgumentSpec("y", "", required=True))),
    lambda: CommandActionSpec("run", "", (ArgumentSpec("x", "", multiple=True), ArgumentSpec("y", ""))),
    lambda: CommandActionSpec("run", "", (ArgumentSpec("x", "", ("-x",)), ArgumentSpec("y", "", ("-x",)))),
    lambda: CommandActionSpec("run", "", (ArgumentSpec("x", "", ("-x", "-x")),)),
])
def test_invalid_schemas_rejected_before_publication(make):
    with pytest.raises(ValueError):
        make()


def test_help_derived_from_argument_declarations(action):
    help_text = render_action_help("/plugins@demo", action)
    assert "用法：/plugins@demo run" in help_text
    assert "-n, --count <count>" in help_text and "默认：3" in help_text
    assert "候选：低、高" in help_text and "<files...>" in help_text


def test_negative_number_outside_choices_reports_value_error():
    action = CommandActionSpec("run", "", (ArgumentSpec("count", "", ("-n",), "integer", choices=(1, 2)),))
    with pytest.raises(CommandArgumentError) as caught:
        bind_command_arguments(action, ("-n", "-3"))
    assert caught.value.reason == "invalid_choice"
