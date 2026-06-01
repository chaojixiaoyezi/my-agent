from __future__ import annotations

import pytest


def test_recovery_action_enum_values_keep_legacy_constants() -> None:
    from agent_py_agent.agent.contracts.recovery_actions import (
        ACTION_CLOSEOUT,
        ACTION_REPAIR,
        RecoveryAction,
        recovery_action_value,
    )

    assert RecoveryAction.CLOSEOUT.value == ACTION_CLOSEOUT
    assert recovery_action_value(RecoveryAction.REPAIR) == ACTION_REPAIR


def test_recovery_action_registry_includes_error_taxonomy_repairs() -> None:
    from agent_py_agent.agent.contracts.recovery_actions import (
        known_recovery_action_values,
        recovery_action_value,
    )

    assert "repair_structured_checkpoint_json" in known_recovery_action_values()
    assert recovery_action_value("repair_structured_checkpoint_json") == "repair_structured_checkpoint_json"


def test_recovery_action_registry_rejects_unknown_strings() -> None:
    from agent_py_agent.agent.contracts.recovery_actions import recovery_action_value

    with pytest.raises(ValueError, match="unknown recovery action"):
        recovery_action_value("invented_action")
