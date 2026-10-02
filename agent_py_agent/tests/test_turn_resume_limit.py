"""续跑次数上限（I4 续，3a 10-02 定）：同一个 Gateway 用户回合因非计划重启最多自动续跑 MAX_UNPLANNED_RESUME_COUNT 次。

锁定：
- 计数存在请求记录的 active_turn_recovery.unplanned_resume_count，经真实认领（queue_service.claim_request）原样保留，跨重启累计；
- 用完后再被打断：请求收成 failed（TURN_RESUME_LIMIT_EXCEEDED，恢复动作 request_user_input），TUI（user_error）、
  IM 公开结果和管理员完整结果（error）显示 turn_resume_notice 表里同一句话；
- 安全重启接班和服务内租约过期不计，也不清零；坏计数按数据损坏 fail-closed，不当 0 重置；
- 用户发“继续”是新回合，不受上限影响（真实链路：连续被停机打断到上限后，同一会话的新请求照常跑完）。
"""
from __future__ import annotations

import itertools
import json
import re
import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model.call_runtime import settle_open_model_calls_for_shutdown
from agent_py_agent.agent.backends import gateway_helpers, http
from agent_py_agent.agent.contracts import model_call_ledger
from agent_py_agent.agent.contracts.error_taxonomy import error_contract
from agent_py_agent.agent.conversation import store_claims
from agent_py_agent.agent.conversation.turn_resume_notice import TURN_RESUME_LIMIT_NOTICE
from agent_py_agent.agent.gateway_parts import daemon_metadata, read_json_file, request_execution
from agent_py_agent.agent.gateway_parts.http_handlers import _public_result
from agent_py_agent.agent.gateway_parts.queue_service import claim_request
from agent_py_agent.agent.gateway_parts.recovery import (
    MAX_UNPLANNED_RESUME_COUNT,
    recover_gateway_processing_requests_report,
)
from agent_py_agent.agent.gateway_parts.request_worker import _finish_claimed_gateway_request
from agent_py_agent.cli.chat_parts.tui_worker_paths import _gateway_outcome
from agent_py_agent.tests.test_shutdown_turn_resume import (
    _MARKER,
    _echo_agent,
    _gateway,
    _left_in_processing,
    _processing,
    _real_agent,
    _reply,
)

_REQUEST = "gwreq-resume-limit"


def _marker(count: object, cause: str = "gateway_restart") -> dict:
    return {"schema_version": "gateway_active_turn_recovery.v1", "request_id": _REQUEST, "dead_execution_attempt_id": "",
            "requeued_at": 1.0, "cause": cause, "unplanned_resume_count": count}


# 函数用途: 模拟一次 Gateway 启动恢复（旧进程已死），只返回重排/失败数，再附带恢复报告里的错误。
def _restart(paths, agent, *, planned: bool = False):
    report = recover_gateway_processing_requests_report(
        paths, startup=True, max_attempts=2, timeout_seconds=900, agent=agent, planned_restart=planned)
    return {"requeued": report.summary["requeued"], "failed": report.summary["failed"]}, report.load_errors


# 函数用途: 用真实认领把 inbox 里的请求取回 processing（整条记录原样保留，换新的执行编号和进程身份）。
def _claim(paths, request_id: str = _REQUEST):
    assert claim_request(paths, paths.inbox / f"{request_id}.json") is not None


def _pending_marker(paths, request_id: str = _REQUEST) -> dict:
    return read_json_file(paths.inbox / f"{request_id}.json")["active_turn_recovery"]


def _terminal_response(paths, request_id: str = _REQUEST) -> dict:
    return read_json_file(paths.terminal / f"{request_id}.json")["terminal_response"]


def test_unplanned_restarts_resume_up_to_the_limit_then_stop(tmp_path, monkeypatch):
    agent = _echo_agent(tmp_path)
    paths = _gateway(agent)
    _processing(paths, _REQUEST)
    monkeypatch.setattr(daemon_metadata, "process_identity_is_live", lambda _identity: False)  # 每次启动时旧进程都已退出

    for expected in range(1, MAX_UNPLANNED_RESUME_COUNT + 1):
        assert _restart(paths, agent) == ({"requeued": 1, "failed": 0}, [])
        assert _pending_marker(paths)["unplanned_resume_count"] == expected, "计数跨重启累计"
        _claim(paths)
    assert _restart(paths, agent) == ({"requeued": 0, "failed": 1}, []), "第 4 次被打断：不再续跑"

    response = _terminal_response(paths)
    assert (response["ok"], response["status"], response["error_code"]) == (False, "failed", "TURN_RESUME_LIMIT_EXCEEDED")
    assert (response["unplanned_resume_count"], response["max_unplanned_resume_count"]) == (3, 3)
    assert not (paths.processing / f"{_REQUEST}.json").exists() and (paths.responses / f"{_REQUEST}.json").exists()


def test_safe_restarts_and_lease_expiry_neither_count_nor_reset(tmp_path, monkeypatch):
    agent = _echo_agent(tmp_path)
    paths = _gateway(agent)
    _processing(paths, _REQUEST, active_turn_recovery=_marker(MAX_UNPLANNED_RESUME_COUNT))
    monkeypatch.setattr(daemon_metadata, "process_identity_is_live", lambda _identity: False)

    assert _restart(paths, agent, planned=True) == ({"requeued": 1, "failed": 0}, []), "安全重启接班照常续跑"
    assert (_pending_marker(paths)["cause"], _pending_marker(paths)["unplanned_resume_count"]) == (
        "gateway_safe_restart", MAX_UNPLANNED_RESUME_COUNT)
    _claim(paths)
    processing = paths.processing / f"{_REQUEST}.json"
    payload = read_json_file(processing)
    processing.write_text(json.dumps({**payload, "lease_started_at": time.time() - 20, "lease_heartbeat_at": time.time() - 20}),
                          encoding="utf-8")
    report = recover_gateway_processing_requests_report(paths, startup=False, max_attempts=2, timeout_seconds=1, agent=agent)
    assert (report.summary["requeued"], report.summary["failed"]) == (1, 0), "服务内租约过期走自己的卡死预算"
    pending = read_json_file(paths.inbox / f"{_REQUEST}.json")
    assert (pending["processing_failure_count"], pending["active_turn_recovery"]["cause"]) == (1, "processing_lease_expired")
    assert pending["active_turn_recovery"]["unplanned_resume_count"] == MAX_UNPLANNED_RESUME_COUNT
    _claim(paths)

    assert _restart(paths, agent) == ({"requeued": 0, "failed": 1}, []), "两种都没把用掉的次数清零"
    assert _terminal_response(paths)["error_code"] == "TURN_RESUME_LIMIT_EXCEEDED"


def test_limit_notice_is_the_same_sentence_on_tui_and_im(tmp_path, monkeypatch):
    agent = _echo_agent(tmp_path)
    paths = _gateway(agent)
    _processing(paths, _REQUEST, active_turn_recovery=_marker(MAX_UNPLANNED_RESUME_COUNT))
    assert _restart(paths, agent) == ({"requeued": 0, "failed": 1}, [])

    response = _terminal_response(paths)
    assert TURN_RESUME_LIMIT_NOTICE == "这一轮被打断太多次，已停止自动续跑；发‘继续’可以接着做。"
    _text, _, summary = _gateway_outcome(SimpleNamespace(job=SimpleNamespace(show_prompt=False)), response)
    assert summary.error == TURN_RESUME_LIMIT_NOTICE, "TUI 显示 user_error"
    assert _public_result(response)["error"] == TURN_RESUME_LIMIT_NOTICE, "IM 公开结果显示 error（取自 user_error）"
    assert response["error"] == TURN_RESUME_LIMIT_NOTICE, "管理员拿完整结果，IM 显示 error"
    chunks = [json.loads(line) for line in (paths.processing / f"{_REQUEST}.chunks.jsonl").read_text().splitlines()]
    aborted = [{key: row[key] for key in ("error_code", "status")} for row in chunks if row.get("kind") == "request_aborted"]
    assert aborted == [{"error_code": "TURN_RESUME_LIMIT_EXCEEDED", "status": "failed"}], "流消费者也收到终态"


def test_limit_code_is_registered_as_waiting_for_the_user():
    contract = error_contract("TURN_RESUME_LIMIT_EXCEEDED")
    assert (contract.code, contract.recommended_action, contract.retryable) == (
        "TURN_RESUME_LIMIT_EXCEEDED", "request_user_input", False)


@pytest.mark.parametrize("bad", ["3", -1, True, 2.5], ids=["string", "negative", "bool", "float"])
def test_corrupted_resume_count_fails_closed(tmp_path, bad):
    agent = _echo_agent(tmp_path)
    paths = _gateway(agent)
    _processing(paths, _REQUEST, active_turn_recovery=_marker(bad))

    summary, errors = _restart(paths, agent)

    assert summary == {"requeued": 0, "failed": 0}, "不当 0 重置、也不擅自收口"
    assert [error["error_type"] for error in errors] == ["DataCorruptionError"]
    assert _left_in_processing(paths, _REQUEST)


# ---------------------------------------------------------------- 真实链路：真实 SimpleAgent + Gateway 入口，只替换供应商传输


# 类用途: 供应商假线路：停机模式下每次主调用途中都宿主停机结清（关门）；关掉停机模式后直接收尾。
class _CrashLoopWire:
    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self.main_calls = 0
        self.shutdown_every_call = True

    def __call__(self, request) -> dict:
        payload = json.loads(json.dumps(request.payload))
        seq = next(self._ids)
        gateway_helpers._emit_provider_attempt({"attempt_id": f"rl-{seq}", "status": "started"})
        names = [str((row.get("function") or {}).get("name") or "") for row in payload.get("tools") or []]
        if names == ["my_agent_capability_probe"]:
            nonce = re.search(r"nonce ([0-9a-f]+)", json.dumps(payload, ensure_ascii=False))
            return _reply(payload, None, ("my_agent_capability_probe", {"nonce": nonce.group(1) if nonce else ""}), seq)
        self.main_calls += 1
        if self.shutdown_every_call:
            settle_open_model_calls_for_shutdown()
            return _reply(payload, None, ("list_files", {"path": "."}), seq)
        return _reply(payload, "RL-DONE 接着做完了。", None, seq)


# 函数用途: 模拟一次非计划重启：新进程有自己的准入表和新的 agent；旧进程已死，请求和会话车道都按死进程接管。
def _restart_process(monkeypatch, tmp_path, paths):
    monkeypatch.setattr(model_call_ledger, "_ADMISSION_REGISTRY", model_call_ledger._ModelCallAdmissionRegistry())
    successor = _real_agent(tmp_path)
    return successor, _restart(paths, successor)


def test_real_chain_user_continue_after_the_limit_is_a_new_turn(tmp_path, monkeypatch):
    wire = _CrashLoopWire()
    monkeypatch.setattr(http, "post_json", wire)
    monkeypatch.setattr(daemon_metadata, "process_identity_is_live", lambda _identity: False)
    monkeypatch.setattr(store_claims, "process_identity_is_live", lambda _identity: False)
    agent = _real_agent(tmp_path)
    paths = _gateway(agent)
    owner = agent.home_paths.owner_id
    conversation = {"canonical_user_id": owner, "channel": "chat", "channel_conversation_id": "rl-session",
                    "channel_user_id": owner}
    path = _processing(paths, _REQUEST, prompt="RL-WORK 看一下当前目录", conversation=conversation)

    for resumes in range(MAX_UNPLANNED_RESUME_COUNT + 1):
        response = request_execution._handle_gateway_request(agent, path)
        _finish_claimed_gateway_request(paths, path, _REQUEST, response, conversation_store=agent.conversation_store)
        assert response["restart_resume"] == _MARKER and _left_in_processing(paths, _REQUEST), f"第 {resumes + 1} 次被停机打断"
        agent, (summary, errors) = _restart_process(monkeypatch, tmp_path, paths)
        if resumes < MAX_UNPLANNED_RESUME_COUNT:
            assert (summary, errors) == ({"requeued": 1, "failed": 0}, [])
            _claim(paths)
    assert (summary, errors) == ({"requeued": 0, "failed": 1}, []), "续跑 3 次后第 4 次被打断：停止自动续跑"
    assert _terminal_response(paths)["user_error"] == TURN_RESUME_LIMIT_NOTICE
    assert wire.main_calls == MAX_UNPLANNED_RESUME_COUNT + 1, "每一代只发出一次主调用，下一次都被准入拒绝"

    wire.shutdown_every_call = False  # 用户发“继续”：同一会话的新请求，没有续跑标记，不受上限影响
    continued = _processing(paths, "gwreq-user-continue", prompt="继续", conversation=conversation)
    response = request_execution._handle_gateway_request(agent, continued)

    assert response["ok"] and "RL-DONE" in response["response"]
    assert "restart_resume" not in response and "host_notices" not in response["channel_delivery"]
