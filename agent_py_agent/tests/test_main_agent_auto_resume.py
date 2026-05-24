from __future__ import annotations

import json
from types import SimpleNamespace


# LLM: zero max_auto_recovery_attempts means unlimited auto-resume attempts.
# 函数用途: 验证自动恢复预算里显式 0 不会被解释成禁用恢复。
def test_auto_resume_zero_limit_is_unlimited(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_auto_resume import auto_resume_decision

    (tmp_path / "auto_recovery_ledger.json").write_text(
        json.dumps({"attempts": 99, "packet_refs": ["old.json"]}),
        encoding="utf-8",
    )
    request = SimpleNamespace(
        execute=True,
        max_auto_recovery_attempts=0,
        recovery_packet_path=None,
        auto_recovery_active=False,
        task_timeout_seconds=600,
    )
    runtime = SimpleNamespace(paths={"root": tmp_path}, request=request, case=SimpleNamespace(case_id="case-1"))
    bundle = SimpleNamespace(
        runtime=runtime,
        status="FAILED",
        recovery_packet_ref="recovery_packet.json",
        duration_seconds=5,
    )

    decision = auto_resume_decision(bundle)

    assert decision.allowed is True
    assert decision.max_attempts == 0
