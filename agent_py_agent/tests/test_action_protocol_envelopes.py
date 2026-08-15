from agent_py_agent.agent.action_protocol import (
    ArtifactRef,
    EvidenceRef,
    PathRef,
    RunScope,
    SubagentResultEnvelope,
    SubagentScheduleEnvelope,
    decode_action_envelope,
    path_refs_from_subagent_refs,
    subagent_schedule_envelope_from_payload,
)


def test_subagent_result_envelope_is_decodeable():
    scope = RunScope(task_id="task-1", run_id="run-1")
    subagent = SubagentResultEnvelope(
        result_id="result-1",
        run_id="run-1",
        status="DONE",
        summary="human display only",
        actual_tools=["read_file"],
        artifact_refs=[ArtifactRef(artifact_id="art-1", path="out.txt")],
        evidence_refs=[
            EvidenceRef(
                evidence_id="ev-1",
                claim="file exists",
                evidence_refs=["report.json"],
                artifact_refs=["out.txt"],
            )
        ],
        scope=scope,
    )

    decoded_subagent = decode_action_envelope(subagent.to_dict())

    assert isinstance(decoded_subagent, SubagentResultEnvelope)
    assert decoded_subagent.operation_id == "subagent_result:result-1"
    assert decoded_subagent.actual_tools == ["read_file"]
    assert decoded_subagent.artifact_refs[0].artifact_id == "art-1"
    assert decoded_subagent.evidence_refs[0].claim == "file exists"


def test_subagent_result_path_refs_are_structured_not_summary_inferred():
    refs = path_refs_from_subagent_refs(
        artifact_refs=[ArtifactRef(artifact_id="art-1", path="out.txt", kind="file")],
        evidence_refs=[
            EvidenceRef(
                evidence_id="ev-1",
                claim="report exists",
                evidence_refs=["reports/check.json"],
                artifact_refs=["out.txt"],
            )
        ],
        owner_run_id="run-1",
    )
    subagent = SubagentResultEnvelope(
        result_id="result-1",
        run_id="run-1",
        status="DONE",
        summary="display text mentions ignored.txt but that is not a ref",
        path_refs=refs,
    )

    decoded = decode_action_envelope(subagent.to_dict())

    assert isinstance(decoded, SubagentResultEnvelope)
    assert all(isinstance(item, PathRef) for item in decoded.path_refs)
    assert [item.path for item in decoded.path_refs] == ["out.txt", "reports/check.json"]


def test_subagent_schedule_envelope_unifies_create_and_child_schedule_payloads():
    envelope = subagent_schedule_envelope_from_payload(
        {
            "parent_run_id": "parent-1",
            "root_id": "root-1",
            "created_run_ids": ["child-1"],
            "planned_count": 1,
            "items": [
                {
                    "run_id": "child-1",
                    "parent_id": "parent-1",
                    "root_id": "root-1",
                    "depth": 2,
                    "role": "worker",
                    "agent_name": "小小傻妞-leaf",
                    "goal": "write file",
                }
            ],
        },
        tool="schedule_child_subagents",
    )

    decoded = decode_action_envelope(envelope.to_dict())

    assert isinstance(decoded, SubagentScheduleEnvelope)
    assert decoded.kind == "subagent_schedule"
    assert decoded.operation_id == "subagent_schedule:schedule_child_subagents:child-1"
    assert decoded.tool == "schedule_child_subagents"
    assert decoded.created_run_ids == ["child-1"]
    assert decoded.items[0]["agent_name"] == "小小傻妞-leaf"


def test_subagent_schedule_envelope_carries_reuse_dispatch_and_state_contract():
    envelope = subagent_schedule_envelope_from_payload(
        {
            "parent_run_id": "parent-1",
            "root_id": "root-1",
            "created_run_ids": ["child-new"],
            "reused_run_ids": ["child-old"],
            "dispatch_run_ids": ["child-new"],
            "next_action": {"tool": "dispatch_subagents", "params": {"run_ids": ["child-new"]}},
            "current_turn_run_state": {
                "dispatchable_run_ids": ["child-new"],
                "verified_run_ids": ["child-old"],
                "next_action": "continue_dispatch_unfinished_run_ids",
            },
            "planned_count": 2,
            "schedule_lifecycle": {
                "requested_count": 2,
                "accepted_run_ids": ["child-new"],
                "running_run_ids": [],
                "failed_run_ids": [],
                "acceptance_status": "accepted",
                "counts": {"recorded": 2, "accepted": 1, "running": 0, "failed": 0},
            },
        },
        tool="create_subagents",
    )

    decoded = decode_action_envelope(envelope.to_dict())

    assert isinstance(decoded, SubagentScheduleEnvelope)
    assert decoded.created_run_ids == ["child-new"]
    assert decoded.reused_run_ids == ["child-old"]
    assert decoded.dispatch_run_ids == ["child-new"]
    assert decoded.next_action["tool"] == "dispatch_subagents"
    assert decoded.current_turn_run_state["dispatchable_run_ids"] == ["child-new"]
    assert decoded.current_turn_run_state["verified_run_ids"] == ["child-old"]
    assert decoded.accepted_run_ids == ["child-new"]
    assert decoded.running_run_ids == []
    assert decoded.acceptance_status == "accepted"
    assert decoded.lifecycle_counts["accepted"] == 1
