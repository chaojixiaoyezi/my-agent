"""W4：服务端观察任务不可回取，前台离开后观察流必须能继续显示。"""
import queue
import threading
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts.request_client import GatewayAskExecutionOptions
from agent_py_agent.cli.chat_parts.plain_state import ChatJob
from agent_py_agent.cli.chat_parts.tui_actions import _tui_attach_gateway_job
from agent_py_agent.cli.chat_parts.tui_input import restore_queued_prompts
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime, TuiTurnSummary


def _params(runtime, jobs):
    return SimpleNamespace(tui_runtime=runtime, jobs=jobs,
                           state_lock=threading.RLock(), pending_jobs_ref_for_enqueue=[0])


def _attach(params):
    _tui_attach_gateway_job(params, "observed", display_text="observed",
                            gateway_request_id="remote", client_message_id="input",
                            execution_options=GatewayAskExecutionOptions())


def _background(runtime, request_id):
    identity = f"bg-main:{request_id}"
    runtime.publish_background_transcript_events([{
        "schema": "background_transcript_event.v1", "seq": 1,
        "gateway_request_id": request_id, "request_id": identity,
        "block_id": f"{identity}:assistant", "kind": "assistant_completed",
        "phase": "completed", "payload": {"text": "后台继续输出"},
    }])
    return any(block.text == "后台继续输出" for block in runtime.store.snapshot().stable_blocks)


def test_restore_skips_observed_gateway_job():
    runtime, jobs = TuiRuntime("observed"), queue.Queue()
    params = _params(runtime, jobs)
    _attach(params)
    runtime.enqueue_prompt("draft", "draft", queued=True)
    jobs.put(ChatJob(user="draft", show_prompt=False, inject=[], prompt_files=[], request_id="draft"))
    params.pending_jobs_ref_for_enqueue[0] += 1
    assert not runtime.has_queued_prompt("remote")
    restored = restore_queued_prompts(jobs=jobs, runtime=runtime, state_lock=params.state_lock,
                                      pending_jobs_ref=params.pending_jobs_ref_for_enqueue,
                                      current_text="editing", current_cursor=3)
    assert restored.request_ids == ("draft",)
    assert restored.text == "draft\nediting"
    assert jobs.qsize() == 1 and jobs.queue[0].gateway_request_id == "remote"
    assert params.pending_jobs_ref_for_enqueue == [1]
    assert len(runtime.store.snapshot().queued_inputs) == 1
    runtime.restore_prompts(("remote",))
    assert len(runtime.store.snapshot().queued_inputs) == 1


def test_attach_failure_releases_observer_ownership():
    runtime = TuiRuntime("attach-failed")

    def reject(_job):
        raise RuntimeError("queue closed")

    params = _params(runtime, SimpleNamespace(put=reject))
    with pytest.raises(RuntimeError, match="queue closed"):
        _attach(params)
    assert params.pending_jobs_ref_for_enqueue == [0]
    assert "remote" not in runtime._owned_gateway_requests
    assert not runtime.store.snapshot().queued_inputs
    assert _background(runtime, "remote")


@pytest.mark.parametrize("summary", [TuiTurnSummary(), TuiTurnSummary(ok=False),
                                    TuiTurnSummary(ok=False, interrupted=True),
                                    TuiTurnSummary(ok=False, outcome_unknown=True)])
def test_released_request_renders_background_events(summary):
    runtime = TuiRuntime("release")
    runtime.register_gateway_request("remote")
    runtime.enqueue_prompt("remote", "observed", queued=True, observed=True)
    runtime.begin_turn("remote")
    runtime.complete_turn("remote", summary)
    assert "remote" not in runtime._owned_gateway_requests
    assert _background(runtime, "remote")


def test_restore_local_draft_releases_foreground_identity():
    runtime = TuiRuntime("restore-release")
    runtime.enqueue_prompt("draft", "draft", queued=True)
    runtime.register_gateway_request("draft")
    runtime.restore_prompts(("draft",))
    assert "draft" not in runtime._owned_gateway_requests
    assert _background(runtime, "draft")


def test_already_consumed_background_event_replays_when_foreground_fails():
    runtime = TuiRuntime("early-background")
    runtime.register_gateway_request("remote")
    runtime.begin_turn("remote")
    assert not _background(runtime, "remote")
    runtime.complete_turn("remote", TuiTurnSummary(ok=False, outcome_unknown=True))
    assert runtime.publish_background_transcript_events([]) == 0
    blocks = runtime.store.snapshot().stable_blocks
    assert sum(block.text == "后台继续输出" for block in blocks) == 1
    runtime.release_gateway_request("remote")
    assert sum(block.text == "后台继续输出" for block in runtime.store.snapshot().stable_blocks) == 1


def test_successful_foreground_does_not_replay_its_buffered_copy():
    runtime = TuiRuntime("success-background")
    runtime.register_gateway_request("remote")
    runtime.begin_turn("remote")
    assert not _background(runtime, "remote")
    runtime.complete_turn("remote", TuiTurnSummary(ok=True, response_text="后台继续输出"))
    assert sum(block.text == "后台继续输出" for block in runtime.store.snapshot().stable_blocks) == 1
