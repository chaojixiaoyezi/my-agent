from types import SimpleNamespace

from agent_py_agent.agent.agent_core.orchestration.dispatch import capability_auto_sweep
from agent_py_agent.agent.conversation import runtime
from agent_py_agent.agent.conversation.models import WakeSignal


def test_regular_wake_never_runs_child_synchronously(monkeypatch):
    started = []
    monkeypatch.setattr(capability_auto_sweep, "auto_start_stalled_orphans",
                        lambda agent: started.append(agent) or {"started": 1})
    monkeypatch.setattr(capability_auto_sweep, "_redispatch_stalled_subagents",
                        lambda agent: (_ for _ in ()).throw(AssertionError("synchronous runner blocked parent lane")))
    agent = SimpleNamespace(subagents=object())
    result = capability_auto_sweep.auto_capability_sweep(agent, SimpleNamespace(metadata={}, source_agent_id=""))
    assert started == [agent]
    assert result["redispatched"] == 1


def test_terminal_observation_fallback_retires_only_matching_ids():
    handled = []
    events = [SimpleNamespace(observation_id="one", root_task_id="root", thread_id="thread")]
    store = SimpleNamespace(threads=SimpleNamespace(require=lambda _: SimpleNamespace(owner_id="owner")),
                            tasks=SimpleNamespace(load_report=lambda _: (SimpleNamespace(thread_id="thread", status="completed"), None)),
                            observations=SimpleNamespace(recent_report=lambda *a, **k: (events, []),
                                                         mark_handled=lambda *a, **k: handled.append((a, k))))
    runtime._retire_terminal_background_source(store, {"thread_id": "thread", "task_id": "root", "wake_signal": {
        "kind": "observation_fallback", "root_task_id": "root", "observation_ids": ["one", "foreign"]}})
    assert handled == [(("thread", ["one"]), {})]


def test_real_claim_uses_fresh_clock_not_tick_snapshot(monkeypatch):
    from dataclasses import replace

    from agent_py_agent.agent.conversation import background_claim
    from agent_py_agent.tests.test_background_claim_execution import _claim_fixture

    case = _claim_fixture(monkeypatch)
    dependencies = runtime._background_claim_dependencies(case.scheduler)
    assert "claim_clock" in dependencies.__dataclass_fields__
    dependencies = replace(dependencies, claim_clock=lambda: 900.0)
    background_claim.run_claimed(dependencies, case.request)
    assert case.records["acquire"][0]["now"] == 900.0
