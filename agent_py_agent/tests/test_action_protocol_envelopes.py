from agent_py_agent.agent.action_protocol import (
    ACTION_PROTOCOL_SCHEMA_VERSION,
    ArtifactRef,
    EvidenceRef,
    PathRef,
    RunScope,
    SubagentResultEnvelope,
    SubagentScheduleEnvelope,
    ToolCallEnvelope,
    ToolCallEnvelopePayloadRequest,
    ToolCallResultEnvelope,
    decode_action_envelope,
    path_refs_from_subagent_refs,
    subagent_schedule_envelope_from_payload,
    tool_call_envelope_from_payload,
)


def test_tool_call_envelope_round_trips_with_scope_and_reserved_fields():
    scope = RunScope(
        request_id="req-1",
        session_id="sess-1",
        task_id="task-1",
        run_id="run-1",
        owner_type="subagent_run",
        owner_id="run-1",
        reserved={"future": "ok"},
    )
    envelope = ToolCallEnvelope(
        call_id="call-1",
        source="legacy_text_protocol",
        tool="read_file",
        args={"path": "README.md"},
        scope=scope,
        reserved={"parser": "tool_block"},
    )

    payload = envelope.to_dict()
    decoded = decode_action_envelope(payload)

    assert payload["schema_version"] == ACTION_PROTOCOL_SCHEMA_VERSION
    assert payload["kind"] == "tool_call"
    assert payload["operation_id"] == "tool_call:call-1"
    assert payload["scope"]["reserved"] == {"future": "ok"}
    assert payload["reserved"] == {"parser": "tool_block"}
    assert isinstance(decoded, ToolCallEnvelope)
    assert decoded.call_id == "call-1"
    assert decoded.operation_id == "tool_call:call-1"
    assert decoded.scope.owner_type == "subagent_run"
    assert decoded.args == {"path": "README.md"}


def test_tool_call_envelope_from_payload_separates_tool_name_from_args():
    envelope = tool_call_envelope_from_payload(
        ToolCallEnvelopePayloadRequest(
            payload={"tool": "write_file", "path": "out.txt", "content": "hello"},
            call_id="call-write",
            source="legacy_text_protocol",
        )
    )

    assert envelope.tool == "write_file"
    assert envelope.args == {"path": "out.txt", "content": "hello"}
    assert envelope.call_id == "call-write"
    assert envelope.operation_id == "tool_call:call-write"
    assert envelope.source == "legacy_text_protocol"


def test_tool_call_result_and_subagent_result_envelopes_are_decodeable():
    scope = RunScope(task_id="task-1", run_id="run-1")
    result = ToolCallResultEnvelope(
        call_id="call-1",
        tool="read_file",
        ok=True,
        output_ref="memory_archive/artifacts/tool-call-1.txt",
        scope=scope,
    )
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

    decoded_result = decode_action_envelope(result.to_dict())
    decoded_subagent = decode_action_envelope(subagent.to_dict())

    assert isinstance(decoded_result, ToolCallResultEnvelope)
    assert decoded_result.operation_id == "tool_call_result:call-1"
    assert decoded_result.output_ref == "memory_archive/artifacts/tool-call-1.txt"
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
                    "role": "leaf_worker",
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
