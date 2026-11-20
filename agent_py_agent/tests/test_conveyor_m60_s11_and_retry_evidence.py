from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.channels import (
    DeliveryContext,
    DeliveryReceipt,
    ReplyEnvelope,
)
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.conversation.technical_continuation import (
    USER_INPUT_YIELD_WAKE_REASON,
    park_for_user_input,
    recover_user_input_yield,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.queue_service import claim_request, ensure_gateway_folders
from agent_py_agent.agent.scheduler.effect_scope import run_scheduler_delivery
from agent_py_agent.agent.scheduler.execution import target_outcome_for_run
from agent_py_agent.agent.scheduler.repository import (
    SchedulerJobCreateRequest,
    SchedulerRepository,
    SchedulerRunFinish,
)
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.support.multiprocess_processing_cases import setup_case
from agent_py_agent.tests.test_w3_s11_user_input_yield import _scheduled_case


@pytest.mark.parametrize("phase", ["processing", "admitted_pending"])
def test_admitted_user_input_yields_in_another_process(tmp_path, phase):
    store = ConversationStore(tmp_path / "conversation")
    thread = store.threads.get_or_create({
        "canonical_user_id": "local-agent", "channel": "synthetic",
        "channel_conversation_id": "test-chat", "channel_user_id": "local-cli",
    })
    if phase == "admitted_pending":
        paths, _, _, _, active_path, _, _ = setup_case(tmp_path, local=True)
        ensure_gateway_folders(paths)
        assert active_path.parent == paths.inbox
    else:
        paths = gateway_paths_from_root(tmp_path / "gateway")
        ensure_gateway_folders(paths)
        request = paths.inbox / "request-one.json"
        request.write_text(json.dumps({
            "id": request.stem, "prompt": "continue",
            "conversation": {"thread_id": thread.thread_id},
            "host_request_owner.v1": asdict(OwnerIdentity.local_main()),
        }), encoding="utf-8")
        claim = claim_request(paths, request)
        assert claim is not None
        active_path = claim.path

    program = """
import sys
from pathlib import Path
from types import SimpleNamespace
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.conversation.technical_continuation import yields_to_user_input
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

root, thread_id = Path(sys.argv[1]), sys.argv[2]
agent = SimpleNamespace(root=root, home_paths=SimpleNamespace(identity=OwnerIdentity.local_main()),
    config=SimpleNamespace(technical_cutoff_continuation_enabled=True, gateway_workspace="gateway"),
    conversation_store=ConversationStore(root / "conversation"))
params = SimpleNamespace(source="background_main_agent", task_attributes={
    "conversation_thread_id": thread_id, "conversation_task_id": "srun_test", "scheduler_run_id": "srun_test"})
print("yield" if yields_to_user_input(agent, params) else "continue")
"""
    def probe():
        result = subprocess.run(
            [sys.executable, "-c", program, str(tmp_path), thread.thread_id],
            capture_output=True, text=True, check=True, timeout=30,
        )
        return result.stdout.strip()

    assert probe() == "yield"
    active_path.replace(paths.done / active_path.name)
    assert probe() == "continue"


def test_parked_predecessor_recovers_one_successor(tmp_path, monkeypatch):
    case = _scheduled_case(tmp_path, monkeypatch, tool_rounds=0)
    case.store.tasks.bind({
        "thread_id": case.thread.thread_id, "task_id": case.run_id, "goal": "monitor", "now": 1000,
    })
    params = SimpleNamespace(task_attributes={
        "conversation_thread_id": case.thread.thread_id, "conversation_task_id": case.run_id,
        "background_wake_signal_id": case.signal.wake_signal_id,
    })
    assert park_for_user_input(case.agent, params)
    assert case.store.wakes.pending() == [case.signal]

    case.scheduler.tick(now=1001)
    assert not case.calls
    [successor] = case.store.wakes.pending()
    assert successor.reason == USER_INPUT_YIELD_WAKE_REASON
    assert successor.metadata["yielded_wake_signal_id"] == case.signal.wake_signal_id
    assert case.agent.scheduler_repository.get_active_run(case.run_id)["status"] == "waiting"

    reopened = ConversationStore(case.store.storage.root)
    assert recover_user_input_yield(reopened, case.signal)
    assert reopened.wakes.pending() == [successor]
    reopened.wakes.mark_handled(successor.wake_signal_id, now=1002)
    assert recover_user_input_yield(reopened, case.signal)
    assert not reopened.wakes.pending()


def _retry_repository(tmp_path):
    repo = SchedulerRepository(tmp_path, owner_provider="local", owner_kind="main", owner_id="local/main")
    job, _ = repo.create_job(SchedulerJobCreateRequest(
        name="retry", prompt="run", thread_id="thread",
        schedule={"kind": "every", "every_seconds": 300, "anchor_at": 1000},
        policy={"on_failure": {"retry_once": True}}, now=1000,
    ))
    first = repo.reserve_manual_run(job["job_id"], now=1001)
    return repo, job, _start_run(repo, first["run_id"], 1001)


def _start_run(repo, run_id, now):
    claim = repo.claim_run(run_id, lease_seconds=30, now=now)
    repo.mark_run_running(run_id, claim["claim_id"], now=now)
    return repo.get_active_run(run_id)


def test_scheduler_retry_reuses_completed_operation(tmp_path):
    repo, job, run = _retry_repository(tmp_path)
    executed = []

    def delivery(run):
        lease = repo.begin_event_effect(run, "operation-one", kind="delivery", capture_receipt=True)
        if lease and "replayed_receipt" in lease:
            return lease["replayed_receipt"]
        executed.append(run["run_id"])
        receipt = {"delivery_status": "sent", "receipt_id": "delivery-one"}
        repo.finish_event_effect(lease, receipt=receipt)
        return receipt

    original = delivery(run)
    terminal = repo.finish_run(run["run_id"], run["claim_id"], SchedulerRunFinish(status="failed", now=1002))
    retry_id = repo.get_job(job["job_id"])["failure_streak"]["retry_run_id"]
    assert retry_id
    assert delivery(_start_run(repo, retry_id, 1063)) == original
    assert executed == [run["run_id"]]
    assert terminal["effect_receipts"]["delivery:operation-one"] == original
    assert terminal["idempotency_keys"] == ["delivery:operation-one"]
    assert terminal["failure_stage"] == "execution"
    assert terminal["reexecutable"] is True


def test_scheduler_delivery_recovers_receipt_across_new_request_ids(tmp_path):
    repo, job, run = _retry_repository(tmp_path)
    agent = SimpleNamespace(scheduler_repository=repo)
    attempts = []
    envelope = ReplyEnvelope(content="complete")

    def deliver(candidate):
        context = DeliveryContext(
            channel="test", target="recipient", mode="proactive", task_id=candidate["run_id"],
            idempotency_key=f"request:{candidate['run_id']}",
        )

        def send():
            attempts.append(context.idempotency_key)
            return DeliveryReceipt(context.channel, context.target, envelope.content,
                                   delivery_status="sent", receipt_id="original-receipt")

        return run_scheduler_delivery(agent, context, envelope, send)

    expected = deliver(run)
    repo.finish_run(run["run_id"], run["claim_id"], SchedulerRunFinish(status="failed", now=1002))
    repo = SchedulerRepository(tmp_path, owner_provider="local", owner_kind="main", owner_id="local/main")
    agent.scheduler_repository = repo
    retry_id = repo.get_job(job["job_id"])["failure_streak"]["retry_run_id"]
    assert deliver(_start_run(repo, retry_id, 1063)) == expected
    assert attempts == [f"request:{run['run_id']}"]


def test_unsettled_effect_blocks_retry(tmp_path):
    repo, job, run = _retry_repository(tmp_path)
    repo.begin_event_effect(run, "unfinished", kind="delivery", capture_receipt=True)
    terminal = repo.finish_run(run["run_id"], run["claim_id"], SchedulerRunFinish(status="failed", now=1002))
    assert terminal["reexecutable"] is False
    assert not repo.get_job(job["job_id"])["failure_streak"]["retry_run_id"]


def test_postcheck_retry_carries_original_target_result(tmp_path):
    repo, job, run = _retry_repository(tmp_path)
    outcome = {
        "schema": "scheduler.target-outcome.v1", "run_id": run["run_id"],
        "status": "succeeded", "response": '{"generation": 1}', "error_code": "",
    }
    repo.record_rule_result(run["run_id"], run["claim_id"], {"generation": 1}, target_outcome=outcome)
    terminal = repo.finish_run(run["run_id"], run["claim_id"], SchedulerRunFinish(
        status="failed", now=1002, postcheck_observation={"status": "failed"},
    ))
    assert terminal["failure_stage"] == "postcheck"
    assert terminal["effect_receipts"]["target:execution"] == {
        "run_id": run["run_id"], "status": "succeeded",
    }
    retry_id = repo.get_job(job["job_id"])["failure_streak"]["retry_run_id"]
    retry = _start_run(repo, retry_id, 1063)
    assert target_outcome_for_run(retry) == outcome
    assert retry["structured_result"] == {"generation": 1}
