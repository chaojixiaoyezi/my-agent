from agent_py_agent.agent.action_protocol import (
    CompactContinuePacketEnvelope,
    decode_action_envelope,
)
from agent_py_agent.agent.memory_archive.compact_continue_packet import (
    CompactContinuePacketRequest,
    build_compact_continue_packet,
)


def test_compact_continue_packet_contains_typed_recovery_envelope():
    packet = build_compact_continue_packet(
        CompactContinuePacketRequest(
            metadata={"apply_id": "apply-1", "plan_id": "plan-1"},
            work_state={
                "goal": "finish task",
                "phase": "repair",
                "next_step": "run tests",
                "acceptance": {"items": ["tests pass"]},
                "constraints": {"items": ["do not delete"]},
                "latest_tests": {"status": "passing", "items": ["focused tests"]},
                "missing_fields": [],
            },
            consistency={"status": "ok"},
            action_guard={
                "status": "allowed",
                "mode": "auto",
                "allowed_to_continue": True,
                "allowed_next_action": "continue_after_guard",
                "owner": {"owner_type": "main_agent", "owner_id": "root"},
            },
            handoff={},
            recommended_read_paths=["compact_context.md", "work_state_snapshot.json"],
            next_actions=["continue"],
            subagent_owner_refs={},
            main_context_bundle={},
        )
    )

    envelope = decode_action_envelope(packet["typed_envelope"])

    assert isinstance(envelope, CompactContinuePacketEnvelope)
    assert envelope.operation_id == "compact_continue_packet:compact-continue-apply-1"
    assert envelope.apply_id == "apply-1"
    assert envelope.ready_to_continue is True
    assert [item.path for item in envelope.path_refs] == [
        "compact_context.md",
        "work_state_snapshot.json",
    ]
