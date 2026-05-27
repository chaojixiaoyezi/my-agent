from __future__ import annotations

import json
from types import SimpleNamespace


# LLM: config default keeps main task recovery budget out of hidden code constants.
# 函数用途: 验证请求未显式传值时，自动恢复次数从统一运行门配置读取。
def test_auto_resume_limit_reads_runtime_guard_config(tmp_path, monkeypatch):
    from agent_py_agent.agent.contracts import main_agent_auto_resume
    from agent_py_agent.agent.settings import runtime_guard_config

    config = tmp_path / "runtime_guard_config.yaml"
    config.write_text("main_agent_auto_resume_attempt_limit: 7\n", encoding="utf-8")
    monkeypatch.setattr(runtime_guard_config, "DEFAULT_RUNTIME_GUARD_CONFIG_PATH", config)

    assert main_agent_auto_resume.auto_resume_limit(SimpleNamespace()) == 7


# LLM: explicit request budget wins over shared config so callers can narrow or uncap one run.
# 函数用途: 验证 request.max_auto_recovery_attempts 是结构化覆盖值，不被默认配置覆盖。
def test_auto_resume_limit_request_override_wins(tmp_path, monkeypatch):
    from agent_py_agent.agent.contracts import main_agent_auto_resume
    from agent_py_agent.agent.settings import runtime_guard_config

    config = tmp_path / "runtime_guard_config.yaml"
    config.write_text("main_agent_auto_resume_attempt_limit: 7\n", encoding="utf-8")
    monkeypatch.setattr(runtime_guard_config, "DEFAULT_RUNTIME_GUARD_CONFIG_PATH", config)

    assert main_agent_auto_resume.auto_resume_limit(SimpleNamespace(max_auto_recovery_attempts=2)) == 2


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
