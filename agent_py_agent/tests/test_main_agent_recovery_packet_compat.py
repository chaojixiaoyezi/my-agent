from __future__ import annotations

import json


# LLM: Recovery packet readers should accept both task and real-task envelopes before normalizing.
# 函数用途: 验证统一恢复入口不会因为旧双轨 schema_version 而拒绝同一类恢复包。
def test_real_task_recovery_reader_accepts_task_recovery_schema(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_real_task_recovery_resume import (
        recovery_delivery_contract_payload,
        recovery_packet_payload,
    )

    packet = tmp_path / "recovery_packet.json"
    packet.write_text(
        json.dumps(
            {
                "schema_version": "main-agent-task-recovery.v1",
                "case_id": "case-a",
                "status": "FAILED",
                "recommended_action": "repair_then_resume_same_case",
                "reason_codes": ["ARTIFACT_MISSING"],
                "refs": {"stdout_ref": "stdout.txt"},
                "acceptance": {"summary": {"failed": 1}},
            }
        ),
        encoding="utf-8",
    )

    payload = recovery_packet_payload(packet)
    delivery = recovery_delivery_contract_payload(packet, workspace=tmp_path)

    assert payload["case_id"] == "case-a"
    assert payload["schema_version"] == "main-agent-task-recovery.v1"
    assert delivery["packet_ref"] == "recovery_packet.json"
    assert delivery["schema_warning"]["code"] == "RECOVERY_PACKET_SCHEMA_COMPAT"
