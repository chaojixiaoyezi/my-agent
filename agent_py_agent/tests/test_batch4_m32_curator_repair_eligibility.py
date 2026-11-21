from agent_py_agent.agent.memory_store.curator_models import CURATOR_REPLAY_BREAKER_FAILURE_COUNT
from agent_py_agent.tests.test_curator_timeout_adaptive import (
    _config,
    _conversation,
    _ScriptedBackend,
    _service,
    _valid_output,
)


def test_deferred_conflict_repair_leaves_input_eligible_at_breaker_floor(tmp_path):
    store, thread, messages = _conversation(tmp_path, 1)
    message_id = messages[0].message_id
    draft = _valid_output(thread.thread_id, [message_id])
    del draft["candidates"][0]["conflicts_with"]
    config = _config(max_retries=0, retention_scan_slice_ms=5000)
    service = _service(tmp_path, _ScriptedBackend([draft]), store, config=config)

    failures = [service.run(reason="admin") for _ in range(CURATOR_REPLAY_BREAKER_FAILURE_COUNT + 1)]
    assert service.state_store.load().per_thread_cursors.get(thread.thread_id) != message_id
    assert all("quarantined:message" not in warning for run in failures for warning in run.warnings)
    assert failures[0].failure_code == "CURATOR_REPAIR_BUDGET_DEFERRED"

    healthy = _ScriptedBackend([_valid_output(thread.thread_id, [message_id])])
    recovered = _service(tmp_path, healthy, store, config=config)
    assert recovered.run(reason="admin").processed_messages == 1
    assert len(healthy.prompts) == 1
    assert len(recovered.candidate_service.list()) == 1
