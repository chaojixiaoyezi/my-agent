"""I4（第 8 条②）：被宿主停机打断的回合，重启后统一自动续跑，用户看得出来。

规则：宿主停机准入拒绝（ModelCallAdmissionClosedError，含显式原因链）打断的 Gateway 回合不写失败终态、不收口插话、不记审计，
原样留在 processing，和随进程消失的回合走同一条重启恢复：recovery 按死进程重排（cause gateway_restart / gateway_safe_restart），
续跑时写 turn_resumed 边界（TUI 显示），最终结果 channel_delivery.host_notices 最前面补同一句提示（IM 渲染在回复正文前）。
停机前用户已发过停止的照常按取消收尾；普通失败照旧写 failed 终态。
"""
from __future__ import annotations

import itertools
import json
import re
import time
import uuid
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.adapter.manager import _reply_text_with_host_notices
from agent_py_agent.agent.agent_core.model.call_runtime import (
    MODEL_CALL_INTERRUPTED_ERROR_CODE,
    settle_open_model_calls_for_shutdown,
)
from agent_py_agent.agent.backends import gateway_helpers, http
from agent_py_agent.agent.contracts import model_call_ledger
from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallAdmissionClosedError,
    ModelCallAdmissionClosure,
)
from agent_py_agent.agent.conversation import store_claims, turn_resume_notice
from agent_py_agent.agent.conversation.turn_resume_notice import turn_resumed_notice_text
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import (
    read_json_file,
    recover_gateway_processing_requests,
    request_execution,
)
from agent_py_agent.agent.gateway_parts.http_handlers import _public_result
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.gateway_parts.request_worker import _finish_claimed_gateway_request
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.cli.chat_parts import tui_runtime
from agent_py_agent.tests.test_gateway_safe_restart import _agent as _echo_agent

_CLOSURE = ModelCallAdmissionClosure("HostShutdownInterrupted", MODEL_CALL_INTERRUPTED_ERROR_CODE)
_MARKER = {"schema_version": "gateway_restart_resume.v1", "error_code": "MODEL_CALL_ADMISSION_CLOSED",
           "reason_code": MODEL_CALL_INTERRUPTED_ERROR_CODE}


def _gateway(agent):
    paths = gateway_paths(agent)
    for folder in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses):
        folder.mkdir(parents=True, exist_ok=True)
    return paths


def _processing(paths, request_id: str, **extra):
    path = paths.processing / f"{request_id}.json"
    path.write_text(json.dumps({"id": request_id, "kind": "ask", "prompt": "停机前在跑", "attempts": 1,
                                "status": "processing", "turn_phase": "open", "lease_started_at": time.time() - 10,
                                "execution_attempt_id": f"attempt-{uuid.uuid4().hex}", **extra}), encoding="utf-8")
    return path


# 函数用途: 模拟接班进程认领续跑请求：从 inbox 取回，记成 processing 并换新的执行编号。
def _claim_again(paths, path):
    pending = paths.inbox / path.name
    payload = {**read_json_file(pending), "status": "processing", "execution_attempt_id": f"attempt-{uuid.uuid4().hex}"}
    path.write_text(json.dumps(payload), encoding="utf-8")
    pending.unlink()


def _refused(wrapped: bool = False):
    def run(_context):
        error = ModelCallAdmissionClosedError(_CLOSURE)
        if wrapped:
            raise RuntimeError("执行器包了一层") from error
        raise error

    return run


def _done_result(text: str):
    return SimpleNamespace(
        channel_delivery={"content": text}, response=text, backend="test", used_memories=0, tool_rounds=0, prompt="",
        prompt_token_estimate=0, runtime_injection_token_estimate=0, turn_token_estimate=0, cumulative_token_estimate=0,
        memory_resume_context_injected=False, memory_resume_context_query="", memory_resume_context_matches=0,
        memory_resume_context_token_estimate=0, memory_resume_context_error="")


def _spy_closeout(monkeypatch):
    calls = {"guidance": [], "audit": []}
    monkeypatch.setattr(request_execution, "_settle_pending_gateway_guidance",
                        lambda _agent, request_id, failure=None: calls["guidance"].append(request_id))
    monkeypatch.setattr(request_execution, "_complete_gateway_request_audit",
                        lambda _agent, context, _path, _response: calls["audit"].append(context["request_id"]))
    return calls


def _left_in_processing(paths, request_id: str) -> bool:
    return ((paths.processing / f"{request_id}.json").exists() and not (paths.terminal / f"{request_id}.json").exists()
            and not (paths.failed / f"{request_id}.json").exists() and not (paths.responses / f"{request_id}.json").exists())


@pytest.mark.parametrize("wrapped", [False, True])
def test_admission_refused_turn_is_left_for_restart_recovery(tmp_path, monkeypatch, wrapped):
    agent = _echo_agent(tmp_path)
    paths = _gateway(agent)
    path = _processing(paths, "gwreq-refused")
    calls = _spy_closeout(monkeypatch)
    monkeypatch.setattr(request_execution, "_run_gateway_ask", _refused(wrapped))

    response = request_execution._handle_gateway_request(agent, path)
    _finish_claimed_gateway_request(paths, path, "gwreq-refused", response)

    assert response["restart_resume"] == _MARKER
    assert calls == {"guidance": [], "audit": []}, "和进程消失一样：不收口插话、不记审计"
    assert _left_in_processing(paths, "gwreq-refused"), "不写终态，原样留给重启恢复"


@pytest.mark.parametrize("case", ["ordinary_failure", "user_stopped"])
def test_ordinary_failures_and_user_stops_still_close_the_turn(tmp_path, monkeypatch, case):
    agent = _echo_agent(tmp_path)
    paths = _gateway(agent)
    path = _processing(paths, "gwreq-closed")
    calls = _spy_closeout(monkeypatch)

    def failing(_context):
        raise RuntimeError("普通失败")

    def stopped_then_refused(_context):  # 执行中用户发了停止，随后宿主停机、下一次模型调用被拒
        path.write_text(json.dumps({**json.loads(path.read_text(encoding="utf-8")), "cancel_requested": True}), encoding="utf-8")
        raise ModelCallAdmissionClosedError(_CLOSURE)

    monkeypatch.setattr(request_execution, "_run_gateway_ask", failing if case == "ordinary_failure" else stopped_then_refused)
    response = request_execution._handle_gateway_request(agent, path)
    _finish_claimed_gateway_request(paths, path, "gwreq-closed", response)

    assert "restart_resume" not in response and calls["audit"] == ["gwreq-closed"]
    assert (paths.terminal / "gwreq-closed.json").exists() and not path.exists(), "照常写终态"
    expected = ("failed", False) if case == "ordinary_failure" else ("interrupted", True)
    assert (response["status"], response["ok"]) == expected, "普通失败照旧 failed；停机前用户已发停止，以用户为准按取消收尾"


@pytest.mark.parametrize("planned,cause", [(False, "gateway_restart"), (True, "gateway_safe_restart")])
def test_refused_turn_resumes_after_restart_with_the_same_notice_on_tui_and_im(tmp_path, monkeypatch, planned, cause):
    agent = _echo_agent(tmp_path, gateway_processing_timeout_seconds=1)
    paths = _gateway(agent)
    request_id = "gwreq-resume"
    path = _processing(paths, request_id)
    monkeypatch.setattr(request_execution, "_run_gateway_ask", _refused())
    _finish_claimed_gateway_request(paths, path, request_id, request_execution._handle_gateway_request(agent, path))

    recovered = recover_gateway_processing_requests(
        paths, startup=True, max_attempts=2, timeout_seconds=1, agent=agent, planned_restart=planned)
    assert recovered["requeued"] == 1 and read_json_file(paths.inbox / path.name)["active_turn_recovery"]["cause"] == cause
    _claim_again(paths, path)
    monkeypatch.setattr(request_execution, "_run_gateway_ask", lambda _context: _done_result("续跑完成"))
    response = request_execution._handle_gateway_request(agent, path)
    chunks = [json.loads(line) for line in (paths.processing / f"{request_id}.chunks.jsonl").read_text().splitlines()]
    _finish_claimed_gateway_request(paths, path, request_id, response)

    notice = response["channel_delivery"]["host_notices"][0]
    assert response["ok"] and (notice["source"], notice["code"], notice["text"]) == (
        "gateway_turn_resume", cause, turn_resumed_notice_text(cause))
    assert [row["cause"] for row in chunks if row["kind"] == "turn_resumed"] == [cause], "TUI 在续跑边界显示"
    public = _public_result(read_json_file(paths.terminal / f"{request_id}.json")["terminal_response"])
    assert _reply_text_with_host_notices(public).startswith("【提示】" + turn_resumed_notice_text(cause)), "IM 回复正文前显示"


def test_fresh_turns_carry_no_resume_notice(tmp_path, monkeypatch):
    agent = _echo_agent(tmp_path)
    paths = _gateway(agent)
    path = _processing(paths, "gwreq-fresh")
    monkeypatch.setattr(request_execution, "_run_gateway_ask", lambda _context: _done_result("一次做完"))
    response = request_execution._handle_gateway_request(agent, path)
    assert response["ok"] and "host_notices" not in response["channel_delivery"]


def test_tui_and_im_share_one_notice_table():
    assert tui_runtime.turn_resumed_notice_text is turn_resume_notice.turn_resumed_notice_text
    assert turn_resumed_notice_text("gateway_restart") == "Gateway 重启打断了这一轮，已自动续跑。"
    assert turn_resumed_notice_text("新原因") == turn_resume_notice.TURN_RESUMED_FALLBACK_NOTICE


# ---------------------------------------------------------------- 真实链路：真实 SimpleAgent + Gateway 入口，只替换供应商传输


def _reply(payload: dict, text: str | None, call: tuple[str, dict] | None, seq: int) -> dict:
    message: dict = {"role": "assistant", "content": text}
    finish = "stop"
    if call is not None:
        message = {"role": "assistant", "content": None, "tool_calls": [{
            "id": f"call_sr_{seq}", "type": "function",
            "function": {"name": call[0], "arguments": json.dumps(call[1], ensure_ascii=False)}}]}
        finish = "tool_calls"
    return {"id": f"chatcmpl-sr-{seq}", "object": "chat.completion", "created": int(time.time()),
            "model": str(payload.get("model") or ""), "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 20, "total_tokens": 1020}}


# 类用途: 供应商假线路：第一次主调用途中宿主停机结清（关门），响应照常带着 list_files 回来；重启后的调用直接收尾。
class _ShutdownWire:
    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self.main_calls = 0

    def __call__(self, request) -> dict:
        payload = json.loads(json.dumps(request.payload))
        seq = next(self._ids)
        gateway_helpers._emit_provider_attempt({"attempt_id": f"sr-{seq}", "status": "started"})
        names = [str((row.get("function") or {}).get("name") or "") for row in payload.get("tools") or []]
        if names == ["my_agent_capability_probe"]:
            nonce = re.search(r"nonce ([0-9a-f]+)", json.dumps(payload, ensure_ascii=False))
            return _reply(payload, None, ("my_agent_capability_probe", {"nonce": nonce.group(1) if nonce else ""}), seq)
        self.main_calls += 1
        if self.main_calls == 1:
            settle_open_model_calls_for_shutdown()  # Gateway 停机结清恰好发生在这次调用在途时
            return _reply(payload, None, ("list_files", {"path": "."}), seq)
        return _reply(payload, "SR-DONE 续跑完成。", None, seq)


def _real_agent(tmp_path, *, plugin_events_enabled: bool = False) -> SimpleAgent:
    return SimpleAgent(AgentConfig(
        model_backend="openai_compatible", api_base="http://127.0.0.1:9/v1", model_name="sr-scripted",
        api_key="sr-fake-key-not-a-credential", stream_enabled=False, model_context_window_tokens=200_000,
        enable_tools=True, memory_path="memory.jsonl", my_agent_home=str(tmp_path / "home"),
        gateway_workspace="gateway", orphan_supervision_interval_seconds=0, memory_curator_enabled=False,
        enable_self_learning=False,
        # B5×B7：总开关关着时装配点不接收紧征询；本 helper 默认关，需要征询的用例显式打开。
        plugin_events_enabled=plugin_events_enabled,
    ), tmp_path / "root")


def test_real_chain_refused_turn_resumes_after_restart(tmp_path, monkeypatch):
    wire = _ShutdownWire()
    monkeypatch.setattr(http, "post_json", wire)
    agent = _real_agent(tmp_path)
    paths = _gateway(agent)
    owner = agent.home_paths.owner_id
    request_id = "gwreq-real-shutdown"
    path = _processing(paths, request_id, prompt="SR-WORK 看一下当前目录", conversation={
        "canonical_user_id": owner, "channel": "chat", "channel_conversation_id": "sr-session", "channel_user_id": owner})

    first = request_execution._handle_gateway_request(agent, path)
    _finish_claimed_gateway_request(paths, path, request_id, first, conversation_store=agent.conversation_store)
    assert first["restart_resume"] == _MARKER and wire.main_calls == 1, "第二次模型调用被准入拒绝，没有发出"
    assert _left_in_processing(paths, request_id)

    # 重启：新进程有自己的准入表和新的 agent，启动恢复按死进程（这里是没有进程身份）把请求重排；
    # 旧回合留下的会话车道占用也按“持有进程已死”立即接管（同一测试进程里只能这样模拟，生产里旧进程确实已退出）。
    monkeypatch.setattr(model_call_ledger, "_ADMISSION_REGISTRY", model_call_ledger._ModelCallAdmissionRegistry())
    monkeypatch.setattr(store_claims, "process_identity_is_live", lambda _identity: False)
    successor = _real_agent(tmp_path)
    recovered = recover_gateway_processing_requests(
        paths, startup=True, max_attempts=2, timeout_seconds=900, agent=successor, planned_restart=False)
    assert recovered["requeued"] == 1
    _claim_again(paths, path)
    second = request_execution._handle_gateway_request(successor, path)

    assert second["ok"] and "SR-DONE" in second["response"]
    assert second["channel_delivery"]["host_notices"][0]["code"] == "gateway_restart"
    assert wire.main_calls == 2, "续跑只多发一次模型调用"
    runtime = read_json_file(path)["runtime_authority"]
    main_run = successor.subagents.runtime_db.main_agent_run_for_task(runtime["task_id"])
    assert main_run["status"] == "done", "被拒那次记的 failed 不挡续跑：同一主 run 开新 attempt 跑完"
