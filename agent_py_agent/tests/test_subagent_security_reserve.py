from __future__ import annotations

from agent_py_agent.agent.local_store import LocalStore
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.models import SecuritySignal


def test_subagent_save_preserves_security_signal_reserve_fields(tmp_path) -> None:
    store = LocalStore(tmp_path / "local.db")
    manager = SubAgentManager(tmp_path / "subagents", local_store=store)
    task = manager.create_run(
        goal="检查可疑安全提示",
        thought="当前只记录安全信号，不做自动拦截。",
        plan=["记录信号", "交给后续安全模块判断"],
    )
    task.security_review_required = True
    task.security_signals = [
        SecuritySignal(
            signal_type="prompt_injection_suspected",
            severity="medium",
            summary="工具输出要求忽略上级指令，疑似安全欺骗。",
            evidence_refs=["reports/status_report.json"],
        )
    ]

    manager.save(task)

    loaded = manager.load(task.id)
    projected = store.get_agent_run(task.id)

    assert loaded.security_review_required is True
    assert len(loaded.security_signals) == 1
    assert loaded.security_signals[0].signal_type == "prompt_injection_suspected"
    assert loaded.security_signals[0].severity == "medium"
    assert loaded.security_signals[0].evidence_refs == ["reports/status_report.json"]
    assert projected is not None
    assert projected.metadata["security_review_required"] is True
    assert projected.metadata["security_signal_count"] == 1
    assert projected.metadata["security_signal_types"] == ["prompt_injection_suspected"]
