from __future__ import annotations


def test_memory_write_requires_scope_and_validation_evidence() -> None:
    from agent_py_agent.agent.contracts.offline_memory_skill_contract import (
        validate_memory_skill_contract,
    )

    result = validate_memory_skill_contract(
        {
            "memory_writes": [
                {
                    "memory_ref": "memory://lessons/one",
                    "scope": {},
                    "validation": {"ok": False, "evidence_refs": []},
                }
            ],
            "memory_uses": [],
            "skill_manifests": [],
            "tool_manifest": {"visible_tools": []},
        }
    )

    assert result.ok is False
    assert result.error_codes == ("MEMORY_SCOPE_MISSING", "MEMORY_VALIDATION_MISSING")


def test_memory_write_rejects_secret_fields_recursively() -> None:
    from agent_py_agent.agent.contracts.offline_memory_skill_contract import (
        validate_memory_skill_contract,
    )

    result = validate_memory_skill_contract(
        {
            "memory_writes": [
                {
                    "memory_ref": "memory://facts/secret",
                    "scope": {"namespace": "project", "owner_type": "main_agent"},
                    "validation": {"ok": True, "evidence_refs": ["artifact://check"]},
                    "payload": {"service": {"api_key": "sk-demo"}},
                }
            ],
            "memory_uses": [],
            "skill_manifests": [],
            "tool_manifest": {"visible_tools": []},
        }
    )

    assert result.ok is False
    assert result.error_codes == ("MEMORY_SECRET_FIELD",)
    assert result.findings[0]["field_path"] == "payload.service.api_key"


def test_stale_memory_cannot_be_used_as_current_fact() -> None:
    from agent_py_agent.agent.contracts.offline_memory_skill_contract import (
        validate_memory_skill_contract,
    )

    result = validate_memory_skill_contract(
        {
            "memory_records": [
                {"memory_ref": "memory://facts/old", "is_stale": True},
            ],
            "memory_uses": [
                {"memory_ref": "memory://facts/old", "usage": "current_fact"},
            ],
            "memory_writes": [],
            "skill_manifests": [],
            "tool_manifest": {"visible_tools": []},
        }
    )

    assert result.ok is False
    assert result.error_codes == ("MEMORY_FACT_STALE",)


def test_memory_retrieval_rejects_missing_expected_and_irrelevant_refs() -> None:
    from agent_py_agent.agent.contracts.offline_memory_skill_contract import (
        validate_memory_skill_contract,
    )

    result = validate_memory_skill_contract(
        {
            "memory_writes": [],
            "memory_uses": [],
            "memory_retrievals": [
                {
                    "query_ref": "query://task",
                    "selected_refs": ["memory://old", "memory://noise"],
                    "expected_refs": ["memory://current"],
                    "irrelevant_refs": ["memory://noise"],
                }
            ],
            "skill_manifests": [],
            "tool_manifest": {"visible_tools": []},
        }
    )

    assert result.ok is False
    assert result.error_codes == ("MEMORY_RECALL_EXPECTED_MISSING", "MEMORY_RECALL_IRRELEVANT_SELECTED")


def test_skill_manifest_requires_trigger_and_input_contract() -> None:
    from agent_py_agent.agent.contracts.offline_memory_skill_contract import (
        validate_memory_skill_contract,
    )

    result = validate_memory_skill_contract(
        {
            "memory_writes": [],
            "memory_uses": [],
            "skill_manifests": [
                {
                    "skill_id": "skill.report",
                    "trigger": {},
                    "input_contract": {},
                    "allowed_tools": ["read_file"],
                    "safety_scan": {"ok": True, "finding_codes": []},
                }
            ],
            "tool_manifest": {"visible_tools": ["read_file"]},
        }
    )

    assert result.ok is False
    assert result.error_codes == ("SKILL_TRIGGER_MISSING", "SKILL_INPUT_CONTRACT_MISSING")


def test_skill_manifest_rejects_safety_findings_and_unknown_tools() -> None:
    from agent_py_agent.agent.contracts.offline_memory_skill_contract import (
        validate_memory_skill_contract,
    )

    result = validate_memory_skill_contract(
        {
            "memory_writes": [],
            "memory_uses": [],
            "skill_manifests": [
                {
                    "skill_id": "skill.deploy",
                    "trigger": {"task_types": ["deployment"]},
                    "input_contract": {"required_fields": ["workspace_ref"]},
                    "allowed_tools": ["raw_shell"],
                    "safety_scan": {"ok": False, "finding_codes": ["DANGEROUS_TOOL"]},
                }
            ],
            "tool_manifest": {"visible_tools": ["read_file"]},
        }
    )

    assert result.ok is False
    assert result.error_codes == ("SKILL_SAFETY_FINDING", "SKILL_TOOL_NOT_VISIBLE")


def test_valid_memory_and_skill_contract_passes() -> None:
    from agent_py_agent.agent.contracts.offline_memory_skill_contract import (
        validate_memory_skill_contract,
    )

    result = validate_memory_skill_contract(
        {
            "memory_writes": [
                {
                    "memory_ref": "memory://lessons/ok",
                    "scope": {"namespace": "project", "owner_type": "main_agent"},
                    "validation": {"ok": True, "evidence_refs": ["artifact://report"]},
                    "payload": {"lesson_code": "prefer_replay_before_real_e2e"},
                }
            ],
            "memory_records": [{"memory_ref": "memory://lessons/ok", "is_stale": False}],
            "memory_uses": [{"memory_ref": "memory://lessons/ok", "usage": "historical_lesson"}],
            "skill_manifests": [
                {
                    "skill_id": "skill.report",
                    "trigger": {"task_types": ["report"]},
                    "input_contract": {"required_fields": ["artifact_ref"]},
                    "allowed_tools": ["read_file"],
                    "safety_scan": {"ok": True, "finding_codes": []},
                }
            ],
            "tool_manifest": {"visible_tools": ["read_file"]},
        }
    )

    assert result.ok is True
    assert result.error_codes == ()
