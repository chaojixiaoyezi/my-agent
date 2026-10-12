"""后台真实尝试退出后，只释放精确代次的模型提交前预留。"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.conversation.background_execution import _run_background_model_attempt
from agent_py_agent.agent.conversation.store import ConversationStore


@pytest.mark.parametrize("submitted", [False, True])
def test_background_failure_releases_only_preprovider_reservation(tmp_path, submitted):
    store = ConversationStore(tmp_path / "conversations")
    entry = store.guidance.append_once({
        "target_type": "task", "target_id": "task-a", "message": "合成插话",
        "metadata": {"expected_turn_id": "turn-a"},
    }, dedupe_key="input-a")
    binding = {"invocation_run_id": "turn-a", "task_id": "task-a", "run_id": "run-a",
               "agent_run_id": "agent-run-a", "attempt_id": "attempt-a"}

    # LLM: 模拟 core 在模型前发布身份，使用真实预留/提交账；退出后由被测后台宿主收尾，不代它改回执。
    # 函数用途: 在明确提交边界的两侧抛出相同中断，验证宿主不能把未知提交重放。
    def run(_prompt, *, params):
        params.conversation_task_binding_callback.bind_runtime_authority(binding)
        assert store.guidance.claim_for_turn(entry, expected_turn_id="turn-a", attempt_id="attempt-a")
        if submitted:
            store.guidance.submissions.mark_submitted(
                "turn-a", [entry], attempt_id="attempt-a", provider_call_id="provider-a",
            )
        raise InterruptedError("合成中断")

    agent = SimpleNamespace(conversation_store=store, run=run)
    with pytest.raises(InterruptedError, match="合成中断"):
        _run_background_model_attempt(agent, "合成需求", RunParams(request_id="turn-a", task_id="task-a"))
    receipt = store.guidance.receipt("input-a")
    assert receipt.status == ("submitted" if submitted else "pending")
    assert store.guidance.claim_for_turn(
        receipt.entry, expected_turn_id="turn-a", attempt_id="attempt-b",
    ) is (not submitted)


@pytest.mark.parametrize("successor", ["pending", "reserved", "submitted", "consumed"])
def test_rejected_old_batch_cannot_repair_over_released_successor(tmp_path, successor):
    store = ConversationStore(tmp_path / "conversations")
    entry = store.guidance.append_once({
        "target_type": "task", "target_id": "task-a", "message": "合成插话",
        "metadata": {"expected_turn_id": "turn-a"},
    }, dedupe_key="input-a")
    assert store.guidance.claim_for_turn(entry, expected_turn_id="turn-a", attempt_id="attempt-a")
    store.guidance.submissions.mark_submitted("turn-a", [entry], attempt_id="attempt-a", provider_call_id="call-a")
    store.guidance.submissions.restore_for_retry("turn-a", [entry], attempt_id="attempt-a", provider_call_id="call-a")
    assert store.guidance.recovery.release_reserved("turn-a", dead_attempt_id="attempt-a")["released"] == 1
    if successor != "pending":
        assert store.guidance.claim_for_turn(entry, expected_turn_id="turn-a", attempt_id="attempt-b")
    if successor in {"submitted", "consumed"}:
        store.guidance.submissions.mark_submitted("turn-a", [entry], attempt_id="attempt-b", provider_call_id="call-b")
    if successor == "consumed":
        store.guidance.acknowledgements.consume_submitted("turn-a", [entry], provider_call_id="call-b")
    with store.guidance.ledger.turn_guard("turn-a"):
        assert store.guidance.submissions.repair_locked("turn-a") == 0
    assert store.guidance.receipt("input-a").status == successor
