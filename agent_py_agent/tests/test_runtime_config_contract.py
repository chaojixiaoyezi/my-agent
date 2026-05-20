from __future__ import annotations


# LLM: Runtime config validation should fail fast when required machine fields are absent.
# 函数用途: 验证 workspace_root/artifact_dir/tool_timeout/max_steps 缺失时返回结构化错误码。
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


# LLM: Runtime config validation should reject invalid scalar types before runtime starts.
# 函数用途: 验证数字字段类型错误或非正数会被配置合同拦截。
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


# LLM: Dangerous config combinations must be surfaced as machine findings, not hidden in docs.
# 函数用途: 验证根目录工作区、shell 开关和无审批高危动作组合都会被 doctor 合同发现。
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
