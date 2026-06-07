from __future__ import annotations


def test_parent_cannot_complete_if_required_child_failed() -> None:
    from agent_py_agent.agent.contracts.offline_subagent_contract import validate_subagent_contract

    result = validate_subagent_contract(
        {
            "parent": {"run_id": "parent", "status": "VERIFYING", "required_child_run_ids": ["child-a"]},
            "children": [
                {
                    "run_id": "child-a",
                    "parent_run_id": "parent",
                    "status": "FAILED",
                    "artifact_refs": ["artifact://child-a/report"],
                    "acceptance": {"ok": False},
                }
            ],
        }
    )

    assert result.ok is False
    assert result.error_codes == ("CHILD_NOT_SUCCESSFUL",)


def test_child_success_requires_artifacts_and_acceptance() -> None:
    from agent_py_agent.agent.contracts.offline_subagent_contract import validate_subagent_contract

    result = validate_subagent_contract(
        {
            "parent": {"run_id": "parent", "status": "VERIFYING", "required_child_run_ids": ["child-a"]},
            "children": [
                {
                    "run_id": "child-a",
                    "parent_run_id": "parent",
                    "status": "DONE",
                    "artifact_refs": [],
                    "acceptance": {"ok": False, "evidence_refs": []},
                }
            ],
        }
    )

    assert result.ok is False
    assert result.error_codes == ("CHILD_ARTIFACT_MISSING", "CHILD_ACCEPTANCE_MISSING")


def test_child_timeout_blocks_parent_with_specific_code() -> None:
    from agent_py_agent.agent.contracts.offline_subagent_contract import validate_subagent_contract

    result = validate_subagent_contract(
        {
            "parent": {"run_id": "parent", "status": "VERIFYING", "required_child_run_ids": ["child-timeout"]},
            "children": [
                {
                    "run_id": "child-timeout",
                    "parent_run_id": "parent",
                    "status": "TIMEOUT",
                    "artifact_refs": [],
                    "acceptance": {"ok": False, "evidence_refs": []},
                }
            ],
        }
    )

    assert result.ok is False
    assert result.error_codes == ("CHILD_TIMEOUT",)


def test_child_timed_out_alias_is_not_a_current_timeout_status() -> None:
    from agent_py_agent.agent.contracts.offline_subagent_contract import validate_subagent_contract

    result = validate_subagent_contract(
        {
            "parent": {"run_id": "parent", "status": "VERIFYING", "required_child_run_ids": ["child-timeout"]},
            "children": [
                {
                    "run_id": "child-timeout",
                    "parent_run_id": "parent",
                    "status": "TIMED_OUT",
                    "artifact_refs": [],
                    "acceptance": {"ok": False, "evidence_refs": []},
                }
            ],
        }
    )

    assert result.ok is False
    assert result.error_codes == ("CHILD_NOT_SUCCESSFUL",)


def test_explicit_subagent_depth_and_child_limits_are_enforced() -> None:
    from agent_py_agent.agent.contracts.offline_subagent_contract import validate_subagent_contract

    result = validate_subagent_contract(
        {
            "limits": {"max_depth": 1, "max_children": 1},
            "parent": {"run_id": "parent", "status": "RUNNING", "required_child_run_ids": ["child-a", "child-b"]},
            "children": [
                {"run_id": "child-a", "parent_run_id": "parent", "depth": 1, "status": "RUNNING"},
                {"run_id": "child-b", "parent_run_id": "parent", "depth": 2, "status": "RUNNING"},
            ],
        }
    )

    assert result.ok is False
    assert result.error_codes == ("SUBAGENT_DEPTH_EXCEEDED", "SUBAGENT_CHILD_LIMIT_EXCEEDED")


def test_child_result_export_must_be_refs_only() -> None:
    from agent_py_agent.agent.contracts.offline_subagent_contract import validate_subagent_contract

    result = validate_subagent_contract(
        {
            "parent": {"run_id": "parent", "status": "RUNNING", "required_child_run_ids": ["child-a"]},
            "children": [
                {
                    "run_id": "child-a",
                    "parent_run_id": "parent",
                    "status": "DONE",
                    "artifact_refs": ["artifact://child-a/report"],
                    "acceptance": {"ok": True, "evidence_refs": ["evidence://child-a"]},
                    "export": {"kind": "full_context", "refs": []},
                }
            ],
        }
    )

    assert result.ok is False
    assert result.error_codes == ("CHILD_CONTEXT_EXPORT_NOT_REFS_ONLY",)


def test_valid_subagent_parent_closeout_passes() -> None:
    from agent_py_agent.agent.contracts.offline_subagent_contract import validate_subagent_contract

    result = validate_subagent_contract(
        {
            "limits": {"max_depth": 2, "max_children": 2},
            "parent": {"run_id": "parent", "status": "VERIFYING", "required_child_run_ids": ["child-a"]},
            "children": [
                {
                    "run_id": "child-a",
                    "parent_run_id": "parent",
                    "depth": 1,
                    "status": "DONE",
                    "artifact_refs": ["artifact://child-a/report"],
                    "acceptance": {"ok": True, "evidence_refs": ["evidence://child-a"]},
                    "export": {"kind": "refs_only", "refs": ["artifact://child-a/report"]},
                }
            ],
        }
    )

    assert result.ok is True
    assert result.error_codes == ()
