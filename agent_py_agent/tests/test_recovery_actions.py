from __future__ import annotations

import ast
from pathlib import Path

import pytest


def test_recovery_action_registry_accepts_enum_and_known_values() -> None:
    from agent_py_agent.agent.contracts.recovery import (
        RecoveryAction,
        known_recovery_action_values,
        recovery_action_value,
    )

    assert RecoveryAction.CLOSEOUT.value in known_recovery_action_values()
    assert recovery_action_value(RecoveryAction.REPAIR) == "repair"
    assert recovery_action_value("repair_structured_checkpoint_json") == "repair_structured_checkpoint_json"


def test_recovery_action_registry_uses_single_action_names() -> None:
    from agent_py_agent.agent.contracts.error_taxonomy import ERROR_CONTRACTS
    from agent_py_agent.agent.contracts.recovery import RECOVERY_ACTIONS

    assert all("_or_" not in action for action in RECOVERY_ACTIONS)
    assert "request_approval_or_stop" not in RECOVERY_ACTIONS
    assert "change_strategy_or_stop" not in RECOVERY_ACTIONS
    assert "repair_or_request_capability" not in RECOVERY_ACTIONS

    taxonomy_actions = {contract.recommended_action for contract in ERROR_CONTRACTS.values()}
    assert taxonomy_actions.issubset(set(RECOVERY_ACTIONS))
    assert all("_or_" not in action for action in taxonomy_actions)


def test_recovery_action_registry_rejects_unknown_strings() -> None:
    from agent_py_agent.agent.contracts.recovery import recovery_action_value

    with pytest.raises(ValueError, match="unknown recovery action"):
        recovery_action_value("invented_action")


def test_literal_recommended_actions_use_unified_vocabulary() -> None:
    from agent_py_agent.agent.contracts.recovery import RECOVERY_ACTIONS

    allowed = set(RECOVERY_ACTIONS)
    assert _unknown_literal_recommended_actions(allowed) == []


def _unknown_literal_recommended_actions(allowed: set[str]) -> list[tuple[str, int, str]]:
    roots = (
        Path("agent_py_agent/agent/contracts"),
        Path("agent_py_agent/agent/agent_core"),
        Path("agent_py_agent/agent/tooling"),
    )
    unknown: list[tuple[str, int, str]] = []
    for root in roots:
        for path in root.rglob("*.py"):
            unknown.extend(_unknown_literal_recommended_actions_in_file(path, allowed))
    return unknown


def _unknown_literal_recommended_actions_in_file(path: Path, allowed: set[str]) -> list[tuple[str, int, str]]:
    unknown: list[tuple[str, int, str]] = []
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        value = _literal_recommended_action(node)
        if value and value not in allowed:
            unknown.append((str(path), node.lineno, value))
    return unknown


def _literal_recommended_action(node: ast.AST) -> str:
    if not isinstance(node, ast.Call):
        if isinstance(node, ast.Dict):
            return _literal_dict_recommended_action(node)
        return ""
    for keyword in node.keywords:
        if keyword.arg != "recommended_action":
            continue
        return _constant_string(keyword.value)
    return ""


def _literal_dict_recommended_action(node: ast.Dict) -> str:
    for key, value in zip(node.keys, node.values, strict=False):
        if not isinstance(key, ast.Constant) or key.value != "recommended_action":
            continue
        return _constant_string(value)
    return ""


def _constant_string(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""
