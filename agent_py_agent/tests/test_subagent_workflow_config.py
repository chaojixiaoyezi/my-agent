from __future__ import annotations

from agent_py_agent.agent.config import load_config


def _write_config(tmp_path, lines: list[str]):
    config_path = tmp_path / "agent_config.yaml"
    config_path.write_text("\n".join(lines), encoding="utf-8")
    return config_path


def test_subagent_workflow_config_defaults(tmp_path):
    config = load_config(_write_config(tmp_path, []))

    assert config.subagent_workflow_mode == "auto"
    assert config.subagent_builtin_workflows is True
    assert config.subagent_user_workflow_dirs == [".agent/workflows/user"]
    assert config.subagent_workflow_review_rounds == 1
    assert config.subagent_workflow_config_warnings == []


def test_subagent_workflow_config_accepts_manual_mode(tmp_path):
    config = load_config(
        _write_config(
            tmp_path,
            [
                "subagent_workflow_mode: manual",
                "subagent_builtin_workflows: false",
                "subagent_user_workflow_dirs:",
                "  - .agent/workflows/team",
                "  - workflows/local",
            ],
        )
    )

    assert config.subagent_workflow_mode == "manual"
    assert config.subagent_builtin_workflows is False
    assert config.subagent_user_workflow_dirs == [".agent/workflows/team", "workflows/local"]
    assert config.subagent_workflow_config_warnings == []


def test_subagent_workflow_config_accepts_off_mode(tmp_path):
    config = load_config(
        _write_config(
            tmp_path,
            [
                "subagent_workflow_mode: off",
            ],
        )
    )

    assert config.subagent_workflow_mode == "off"
    assert config.subagent_workflow_config_warnings == []


def test_subagent_workflow_config_invalid_mode_falls_back_to_auto(tmp_path):
    config = load_config(
        _write_config(
            tmp_path,
            [
                "subagent_workflow_mode: always",
            ],
        )
    )

    assert config.subagent_workflow_mode == "auto"
    assert config.subagent_workflow_config_warnings == [
        {
            "field_name": "subagent_workflow_mode",
            "raw_value": "always",
            "fallback_value": "auto",
            "reason": "expected one of ['auto', 'manual', 'off']",
        }
    ]


def test_subagent_workflow_config_invalid_user_dirs_falls_back_to_default(tmp_path):
    config = load_config(
        _write_config(
            tmp_path,
            [
                "subagent_user_workflow_dirs: .agent/workflows/user",
            ],
        )
    )

    assert config.subagent_user_workflow_dirs == [".agent/workflows/user"]
    assert config.subagent_workflow_config_warnings == [
        {
            "field_name": "subagent_user_workflow_dirs",
            "raw_value": ".agent/workflows/user",
            "fallback_value": [".agent/workflows/user"],
            "reason": "expected a list of non-empty strings",
        }
    ]
