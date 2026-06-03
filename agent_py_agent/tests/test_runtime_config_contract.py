from __future__ import annotations


def test_runtime_config_contract_rejects_missing_required_fields() -> None:
    from agent_py_agent.agent.contracts.runtime_config_contract import validate_runtime_config

    result = validate_runtime_config({})

    assert result.ok is False
    assert result.error_codes == (
        "CONFIG_WORKSPACE_ROOT_MISSING",
        "CONFIG_ARTIFACT_DIR_MISSING",
        "CONFIG_TOOL_TIMEOUT_MISSING",
        "CONFIG_MAX_STEPS_MISSING",
    )


def test_runtime_config_contract_rejects_invalid_types() -> None:
    from agent_py_agent.agent.contracts.runtime_config_contract import validate_runtime_config

    result = validate_runtime_config(
        {
            "workspace_root": "/tmp/workspace",
            "artifact_dir": "artifacts",
            "tool_timeout": "slow",
            "max_steps": 0,
        }
    )

    assert result.ok is False
    assert result.error_codes == ("CONFIG_TOOL_TIMEOUT_INVALID", "CONFIG_MAX_STEPS_INVALID")


def test_runtime_config_contract_reports_dangerous_combinations() -> None:
    from agent_py_agent.agent.contracts.runtime_config_contract import validate_runtime_config

    result = validate_runtime_config(
        {
            "workspace_root": "/",
            "artifact_dir": "artifacts",
            "tool_timeout": 30,
            "max_steps": 20,
            "allow_shell": True,
            "allow_dangerous_actions": True,
            "approval_required": False,
        }
    )

    assert result.ok is False
    assert result.error_codes == (
        "CONFIG_WORKSPACE_ROOT_DANGEROUS",
        "CONFIG_ALLOW_SHELL_ENABLED",
        "CONFIG_DANGEROUS_ACTIONS_WITHOUT_APPROVAL",
    )
