"""插话对账按 guidance 的目标类型结算，不让同名运输请求盖过持久任务。"""

from __future__ import annotations

import pytest

from agent_py_agent.agent.gateway_parts.input_delivery_service import (
    bind_gateway_input_active_locked,
    gateway_input_transition,
    load_or_prepare_gateway_input_locked,
    read_gateway_input_receipt,
    reconcile_gateway_input_receipts,
    settle_gateway_inputs_for_turn,
)
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.tests.test_steer_delivery_recovery import _background_input


# LLM: 用真实 guidance 和入口账本构造两个不同身份，不把预期路由写成 mock 返回值。
# 函数用途: 在临时 Gateway 里登记一条已有精确投递目标的输入，不执行模型或网络请求。
def _add_input(agent, paths, *, target_type, target_id, turn_id, key):
    request_id = f"gwreq-msg-{key}"
    metadata = {
        "expected_turn_id": turn_id, "channel_message_id": key,
        "gateway_input_request_id": request_id, "client_input_digest": key,
    }
    agent.conversation_store.guidance.append_once({
        "target_type": target_type, "target_id": target_id,
        "message": "补充测试要求", "sender": "local-agent", "metadata": metadata,
    }, dedupe_key=key)
    prepared = {
        "id": request_id, "request_id": request_id, "kind": "ask",
        "goal": "补充测试要求", "user_id": "local-agent", "metadata": metadata,
    }
    with gateway_input_transition(paths, request_id):
        receipt, _ = load_or_prepare_gateway_input_locked(
            paths, request_id=request_id, client_input_digest=key,
            client_message_id=key, guidance_dedupe_key=key, prepared_request=prepared,
        )
        bind_gateway_input_active_locked(
            paths, receipt, target_turn_id=turn_id, guidance_dedupe_key=key,
        )
    return request_id


@pytest.mark.parametrize("folder", ["terminal", "processing"])
def test_active_task_guidance_survives_same_named_closed_transport(tmp_path, folder):
    agent, paths, input_id = _background_input(tmp_path, "gwreq-original")
    write_json_file_atomic(getattr(paths, folder) / "gwreq-original.json", {
        "id": "gwreq-original", "status": "failed", "turn_phase": "closed",
    })

    summary = reconcile_gateway_input_receipts(paths, agent)

    assert read_gateway_input_receipt(paths, input_id).state == "active_pending"
    assert agent.conversation_store.guidance.receipt("steer-d").status == "pending"
    assert not (paths.inbox / f"{input_id}.json").exists()
    assert summary["queued"] == 0


def test_terminal_task_guidance_settles_despite_same_named_open_transport(tmp_path):
    agent, paths, input_id = _background_input(tmp_path, "gwreq-original")
    agent.conversation_store.tasks.update_status({
        "task_id": "gwreq-original", "status": "completed",
    })
    write_json_file_atomic(paths.processing / "gwreq-original.json", {
        "id": "gwreq-original", "status": "processing", "turn_phase": "open",
    })

    reconcile_gateway_input_receipts(paths, agent)

    assert read_gateway_input_receipt(paths, input_id).state == "queued"
    assert agent.conversation_store.guidance.receipt("steer-d").status == "rejected"


@pytest.mark.parametrize("task_fact", ["missing", "corrupt"])
def test_closed_transport_cannot_settle_unreadable_task_guidance(tmp_path, task_fact):
    agent, paths, input_id = _background_input(tmp_path, "gwreq-original")
    task_path = agent.conversation_store.storage.task_path("gwreq-original")
    if task_fact == "missing":
        task_path.unlink()
    else:
        task_path.write_text("{", encoding="utf-8")
    write_json_file_atomic(paths.terminal / "gwreq-original.json", {
        "id": "gwreq-original", "status": "failed", "turn_phase": "closed",
    })

    reconcile_gateway_input_receipts(paths, agent)

    assert read_gateway_input_receipt(paths, input_id).state == "active_pending"
    assert agent.conversation_store.guidance.receipt("steer-d").status == "pending"
    assert not (paths.inbox / f"{input_id}.json").exists()


def test_task_target_is_read_from_original_guidance_not_expected_transport_id(tmp_path):
    agent, paths, _ = _background_input(tmp_path, "persistent-task")
    input_id = _add_input(
        agent, paths, target_type="task", target_id="persistent-task",
        turn_id="gwreq-later", key="different-transport",
    )
    write_json_file_atomic(paths.terminal / "gwreq-later.json", {
        "id": "gwreq-later", "status": "failed", "turn_phase": "closed",
    })

    reconcile_gateway_input_receipts(paths, agent)

    assert read_gateway_input_receipt(paths, input_id).state == "active_pending"
    assert agent.conversation_store.guidance.receipt("different-transport").status == "pending"


@pytest.mark.parametrize("settlement", ["periodic", "transport_closeout"])
def test_request_settlement_does_not_reject_live_task_mailbox(tmp_path, settlement):
    agent, paths, task_input = _background_input(tmp_path, "gwreq-original")
    request_input = _add_input(
        agent, paths, target_type="request", target_id="gwreq-original",
        turn_id="gwreq-original", key="request-only",
    )
    write_json_file_atomic(paths.terminal / "gwreq-original.json", {
        "id": "gwreq-original", "status": "failed", "turn_phase": "closed",
    })

    if settlement == "periodic":
        reconcile_gateway_input_receipts(paths, agent)
    else:
        settle_gateway_inputs_for_turn(
            paths, target_turn_id="gwreq-original", conversation_store=agent.conversation_store,
        )

    assert read_gateway_input_receipt(paths, request_input).state == "queued"
    assert read_gateway_input_receipt(paths, task_input).state == "active_pending"
    assert agent.conversation_store.guidance.receipt("request-only").status == "rejected"
    assert agent.conversation_store.guidance.receipt("steer-d").status == "pending"


@pytest.mark.parametrize("resume_limit", [False, True])
def test_terminalizer_guidance_closeout_preserves_live_task_mailbox(tmp_path, resume_limit):
    from agent_py_agent.agent.gateway_parts.recovery import _settle_turn_guidance

    agent, paths, _ = _background_input(tmp_path, "gwreq-original")
    _add_input(agent, paths, target_type="request", target_id="gwreq-original",
               turn_id="gwreq-original", key="request-only")

    _settle_turn_guidance(
        agent.conversation_store, "gwreq-original",
        {"error_code": "TURN_RESUME_LIMIT_EXCEEDED"} if resume_limit else {},
    )

    assert agent.conversation_store.guidance.receipt("steer-d").status == "pending"
    assert agent.conversation_store.guidance.receipt("request-only").status == "rejected"
