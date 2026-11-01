"""正式配置读口消费W0诊断DTO；只用临时文件和合成秘密标记。"""
from argparse import Namespace
from dataclasses import asdict

import pytest

from agent_py_agent.agent.settings import config_io
from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.agent.settings.config_diagnostics import ConfigParseDiagnostic
from agent_py_agent.cli.config_cmd import cmd_config_get, cmd_config_set

MARKER = "synthetic-private-config-marker"


@pytest.mark.parametrize("line", [f"feishu_app_secret {MARKER}", f"  feishu_app_secret: {MARKER}"])
def test_loader_reports_second_line_with_source_without_secret(tmp_path, line):
    path = tmp_path / "broken.yaml"
    original = f"agent_name: fixture\n{line}\nchat_history_max_turns: 9\n".encode()
    path.write_bytes(original)
    config = load_config(path)
    assert config.config_warnings, "语法错误不能被静默跳过"
    diagnostics = getattr(config, "config_parse_diagnostics", [])
    assert len(diagnostics) == 1 and isinstance(diagnostics[0], ConfigParseDiagnostic)
    fact = diagnostics[0]
    assert fact.source == str(path.resolve()) and fact.line == 2 and fact.column >= 1
    assert fact.severity == "error" and fact.value_redacted is True
    assert fact.code in {"CONFIG_PARSE_MISSING_COLON", "CONFIG_PARSE_INDENTATION"}
    assert config.agent_name == "fixture" and config.chat_history_max_turns == 9
    assert MARKER not in repr(config.config_warnings) + repr(asdict(fact))
    assert any(str(path) in warning and ":2:" in warning for warning in config.config_warnings)
    assert path.read_bytes() == original


@pytest.mark.parametrize("text,expected", [
    ('\ufeff# 中文注释\r\nagent_name: "鲸#鲸" # 行尾说明\r\n\r\n', {"agent_name": "鲸#鲸"}),
    ('items:\n- "a#b"\n  - 2\nnext: true\n', {"items": ["a#b", 2], "next": True}),
    ("items: [1, 'a#b']\nmap: {'a': [1, 2]}\n", {"items": [1, "a#b"], "map": {"a": [1, 2]}}),
    ('agent_name: ""\nurl: "https://example.invalid/#anchor"\n',
     {"agent_name": "", "url": "https://example.invalid/#anchor"}),
    ("# only comments\n   # indented comment\n \n", {}),
    ("agent_name: 'it''s#ok' # comment\n", {"agent_name": "it''s#ok"}),
    (r'agent_name: "a\"#b" # comment' + "\n", {"agent_name": r'a\"#b'}),
])
def test_existing_legal_subset_and_legacy_dict_api(text, expected):
    diagnostics = []
    assert config_io.parse_simple_yaml_text(text) == expected
    assert config_io.parse_simple_yaml_text(text, source="fixture", diagnostics=diagnostics) == expected
    assert diagnostics == []


@pytest.mark.parametrize("bad,code", [
    ("missing-colon", "CONFIG_PARSE_MISSING_COLON"),
    ("  chat_history_max_turns: 7", "CONFIG_PARSE_INDENTATION"),
    ("\tchat_history_max_turns: 7", "CONFIG_PARSE_INDENTATION"),
    ("- orphan", "CONFIG_PARSE_ORPHAN_LIST"),
    (": no-key", "CONFIG_PARSE_EMPTY_KEY"),
    ('agent_name: "unterminated', "CONFIG_PARSE_SCALAR"),
    ("items: [1, 2}", "CONFIG_PARSE_SCALAR"),
])
def test_parser_emits_typed_bounded_structure_facts(bad, code):
    diagnostics = []
    data = config_io.parse_simple_yaml_text(f"# prelude\n{bad}\nvalid: 3\n",
                                          source="synthetic.yaml", diagnostics=diagnostics)
    assert data["valid"] == 3
    assert len(diagnostics) == 1
    fact = diagnostics[0]
    assert isinstance(fact, ConfigParseDiagnostic)
    assert (fact.source, fact.line, fact.code) == ("synthetic.yaml", 2, code)
    assert fact.value_redacted is True and fact.message


def test_diagnostic_collection_has_a_fixed_bound():
    diagnostics = []
    config_io.parse_simple_yaml_text((f"not-mapping {MARKER}\n" * 200),
                                     source="oversized.yaml", diagnostics=diagnostics)
    assert 1 <= len(diagnostics) <= 32
    assert diagnostics[-1].code == "CONFIG_PARSE_DIAGNOSTICS_TRUNCATED"
    assert MARKER not in repr(diagnostics)


def test_unknown_keys_are_not_syntax_errors_and_runtime_diagnostics_cannot_be_injected(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text('unknown_setting: 1\nconfig_parse_diagnostics: ["forged"]\n')
    config = load_config(path)
    assert config.config_parse_diagnostics == []
    assert any("unknown config key" in message for message in config.config_warnings)
    assert "config_parse_diagnostics" not in config.config_sources


def test_cli_distinguishes_raw_from_effective_and_reports_location(tmp_path, capsys):
    path = tmp_path / "config.yaml"
    path.write_text(f"chat_history_max_turns: not-an-integer\nfeishu_app_secret {MARKER}\n")
    assert cmd_config_get(Namespace(config=str(path), key="chat_history_max_turns")) == 0
    output = capsys.readouterr()
    text = output.out + output.err
    assert "raw" in text and "effective" in text
    assert "not-an-integer" in text and str(AgentConfig().chat_history_max_turns) in text
    assert "CONFIG_PARSE_MISSING_COLON" in text and f"{path}:2:" in text
    assert MARKER not in text


def test_cli_secret_raw_and_effective_values_are_both_masked(tmp_path, capsys):
    path = tmp_path / "config.yaml"
    path.write_text(f'feishu_app_secret: "{MARKER}"\nbroken-line {MARKER}\n')
    assert cmd_config_get(Namespace(config=str(path), key="feishu_app_secret")) == 0
    output = capsys.readouterr()
    assert MARKER not in output.out + output.err
    assert "raw" in output.out and "effective" in output.out


def test_cli_set_surfaces_existing_parse_warning_without_rewriting_bad_line(tmp_path, capsys):
    path = tmp_path / "config.yaml"
    original = f'\ufeff# keep\r\nfeishu_app_secret {MARKER}\r\nfeishu_app_id: "old"\r\n'.encode()
    path.write_bytes(original)
    assert cmd_config_set(Namespace(config=str(path), key="feishu_app_id", value="new-app")) == 0
    output = capsys.readouterr()
    assert "CONFIG_PARSE_MISSING_COLON" in output.out + output.err
    assert MARKER not in output.out + output.err
    assert path.read_bytes().startswith(f'\ufeff# keep\r\nfeishu_app_secret {MARKER}\r\n'.encode())


def test_load_file_supplies_the_same_actual_source(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("agent_name: fixture\nbad-line\n")
    diagnostics = []
    assert config_io.load_simple_yaml(path, diagnostics=diagnostics) == {"agent_name": "fixture"}
    assert diagnostics[0].source == str(path.resolve()) and diagnostics[0].line == 2


def test_missing_file_and_unreadable_file_are_not_silent_defaults(tmp_path, monkeypatch):
    missing = tmp_path / "missing.yaml"
    with pytest.raises(FileNotFoundError):
        load_config(missing)
    path = tmp_path / "unreadable.yaml"
    path.write_text("agent_name: fixture\n")

    def denied(*_args, **_kwargs):
        raise PermissionError("synthetic read failure")

    monkeypatch.setattr(type(path), "read_text", denied)
    with pytest.raises(PermissionError):
        load_config(path)
