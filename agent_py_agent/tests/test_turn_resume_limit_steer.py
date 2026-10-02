"""续跑上限收口时用户插话的终态（3a 10-02 定，step17h）。

原则：用户的插话不能悄悄悬着。续跑上限收口（TURN_RESUME_LIMIT_EXCEEDED，只在启动恢复里产生，提交插话的进程已证明死亡）时，
每条插话都要有终态和用户看得到的说明，内容不丢、也不送两遍。判断只看结构化事实：回执状态、确认批次（答复记录）、
会话历史里有没有这条插话的去重键。
- 没被取走（pending）：拒收，入口回执把它排成“备用下一轮”马上跑；提示说“你补充的话会作为新的一轮马上处理”。
- 已被续跑回合取走、送进主调用、进程随即被杀（submitted、没有确认批次、历史里没有）：同上，拒收并起备用下一轮。
- 已写进历史（submitted 但历史里已有它的去重键）：收成 consumed（settle_reason=recorded_in_transcript），不再起备用下一轮；
  提示说“你补充的话已记在会话里，发‘继续’会一起处理”。
- 不是续跑上限的收口（例如卡死超时）不动已提交的插话，行为不变。
"""
from __future__ import annotations

import json

import pytest

from agent_py_agent.agent.conversation.store_guidance_acknowledgements import transcript_dedupe_key
from agent_py_agent.agent.conversation.turn_resume_notice import (
    TURN_RESUME_LIMIT_BACKUP_NOTICE,
    TURN_RESUME_LIMIT_NOTICE,
    TURN_RESUME_LIMIT_RECORDED_NOTICE,
    turn_resume_limit_notice,
)
from agent_py_agent.agent.gateway_parts import daemon_metadata, read_json_file
from agent_py_agent.agent.gateway_parts.input_delivery_service import (
    bind_gateway_input_active_locked,
    gateway_input_transition,
    load_or_prepare_gateway_input_locked,
    read_gateway_input_receipt,
)
from agent_py_agent.agent.gateway_parts.recovery import (
    MAX_UNPLANNED_RESUME_COUNT,
    _terminal_gateway_request_payload,
    recover_gateway_processing_requests_report,
)
from agent_py_agent.tests.test_shutdown_turn_resume import _echo_agent, _gateway, _processing
from agent_py_agent.tests.test_turn_resume_limit import _REQUEST, _marker, _restart, _terminal_response

_STEER = "另外把 todo.txt 的行数也告诉我。"


# 函数用途: 按 TUI 插话的形状登记一条插话：guidance 回执（带会话、去重键和入口请求号）加一张绑定到被测回合的入口回执，
#   入口回执里存着“备用下一轮”要排的完整请求。返回 (guidance 条目, 入口请求号, 会话编号)。
def _steer(agent, paths, key: str):
    store = agent.conversation_store
    owner = agent.home_paths.owner_id
    thread_id = store.threads.get_or_create({"canonical_user_id": owner}).thread_id
    input_id = f"gwreq-msg-{key}"
    entry = store.guidance.append_once({
        "message": _STEER, "sender": owner, "target_type": "request", "target_id": _REQUEST, "priority": "high",
        "delivery": "current_request",
        "metadata": {"kind": "active_turn_user_input", "record_in_transcript": True, "expected_turn_id": _REQUEST,
                     "channel_message_id": key, "gateway_input_request_id": input_id, "thread_id": thread_id},
    }, dedupe_key=key)
    prepared = {"id": input_id, "request_id": input_id, "kind": "ask", "goal": _STEER, "user_id": owner,
                "metadata": {"client_input_digest": f"digest-{key}", "expected_turn_id": _REQUEST,
                             "gateway_input_request_id": input_id, "message_id": key}}
    with gateway_input_transition(paths, input_id):
        receipt, _created = load_or_prepare_gateway_input_locked(
            paths, request_id=input_id, client_input_digest=f"digest-{key}", client_message_id=key,
            guidance_dedupe_key=key, prepared_request=prepared)
        bind_gateway_input_active_locked(paths, receipt, target_turn_id=_REQUEST, guidance_dedupe_key=key)
    return entry, input_id, thread_id


# 函数用途: 模拟最后一代续跑回合把插话取走、送进主调用（回执 submitted，有提交批次、没有确认批次），随后进程被杀。
def _taken_by_killed_generation(agent, entry):
    store = agent.conversation_store
    assert store.guidance.claim_for_turn(entry, expected_turn_id=_REQUEST, attempt_id="attempt-gen4")
    store.guidance.submissions.mark_submitted(_REQUEST, [entry], attempt_id="attempt-gen4", provider_call_id="model-call:gen4")
    assert store.guidance.receipt(entry.metadata["dedupe_key"]).status == "submitted"


def _settlement(response: dict) -> dict:
    return {k: response["guidance_settlement"][k] for k in ("dead_submissions", "recorded_in_transcript", "backup_turns")}


@pytest.mark.parametrize("taken", [True, False], ids=["taken_and_killed", "not_taken"])
def test_steer_at_the_limit_runs_as_a_backup_turn_and_the_notice_says_so(tmp_path, monkeypatch, taken):
    agent = _echo_agent(tmp_path)
    paths = _gateway(agent)
    monkeypatch.setattr(daemon_metadata, "process_identity_is_live", lambda _identity: False)
    entry, input_id, _thread = _steer(agent, paths, "steer-k")
    if taken:
        _taken_by_killed_generation(agent, entry)
    _processing(paths, _REQUEST, active_turn_recovery=_marker(MAX_UNPLANNED_RESUME_COUNT))

    assert _restart(paths, agent) == ({"requeued": 0, "failed": 1}, [])

    response = _terminal_response(paths)
    assert (response["error_code"], response["user_error"], response["error"]) == (
        "TURN_RESUME_LIMIT_EXCEEDED", TURN_RESUME_LIMIT_BACKUP_NOTICE, TURN_RESUME_LIMIT_BACKUP_NOTICE)
    assert _settlement(response) == {"dead_submissions": int(taken), "recorded_in_transcript": 0, "backup_turns": 1}
    receipt = agent.conversation_store.guidance.receipt("steer-k")
    assert receipt.status == "rejected", "拒收，不悄悄悬着"
    assert receipt.migration.get("dead_submission") == (
        {"submission_id": "model-call:gen4", "attempt_id": "attempt-gen4"} if taken else None)
    assert read_gateway_input_receipt(paths, input_id).state == "queued"
    backup = read_json_file(paths.inbox / f"{input_id}.json")
    assert backup["goal"] == _STEER and "active_turn_recovery" not in backup, "内容原样作为新的一轮，计数从 0 开始"


def test_steer_already_in_history_is_settled_once_without_a_second_delivery(tmp_path, monkeypatch):
    agent = _echo_agent(tmp_path)
    paths = _gateway(agent)
    monkeypatch.setattr(daemon_metadata, "process_identity_is_live", lambda _identity: False)
    entry, input_id, thread_id = _steer(agent, paths, "steer-r")
    _taken_by_killed_generation(agent, entry)
    agent.conversation_store.messages.append_once(
        {"thread_id": thread_id, "role": "user", "content": _STEER,
         "metadata": {"kind": "active_turn_user_input", "guidance_id": entry.guidance_id}},
        dedupe_key=transcript_dedupe_key(entry.guidance_id))
    _processing(paths, _REQUEST, active_turn_recovery=_marker(MAX_UNPLANNED_RESUME_COUNT))

    assert _restart(paths, agent) == ({"requeued": 0, "failed": 1}, [])

    response = _terminal_response(paths)
    assert (response["user_error"], response["error"]) == (TURN_RESUME_LIMIT_RECORDED_NOTICE, TURN_RESUME_LIMIT_RECORDED_NOTICE)
    assert _settlement(response) == {"dead_submissions": 1, "recorded_in_transcript": 1, "backup_turns": 0}
    receipt = agent.conversation_store.guidance.receipt("steer-r")
    assert (receipt.status, receipt.migration.get("settle_reason")) == ("consumed", "recorded_in_transcript")
    assert read_gateway_input_receipt(paths, input_id).state == "consumed"
    assert not (paths.inbox / f"{input_id}.json").exists(), "已在历史里，不再起备用下一轮，模型不会看到两遍"


def test_crash_after_sealing_the_limit_still_settles_the_steer_and_fixes_the_notice(tmp_path, monkeypatch):
    """上限答复已封存进热记录、插话还没结算时进程又挂了：启动补交时照样结算（同一分流），归档前把提示改成对应句子。"""
    agent = _echo_agent(tmp_path)
    paths = _gateway(agent)
    monkeypatch.setattr(daemon_metadata, "process_identity_is_live", lambda _identity: False)
    entry, input_id, _thread = _steer(agent, paths, "steer-s")
    _taken_by_killed_generation(agent, entry)
    path = _processing(paths, _REQUEST, active_turn_recovery=_marker(MAX_UNPLANNED_RESUME_COUNT))
    sealed = {"id": _REQUEST, "kind": "ask", "ok": False, "status": "failed", "created_at": 0,
              "error_code": "TURN_RESUME_LIMIT_EXCEEDED", "user_error": TURN_RESUME_LIMIT_NOTICE, "error": TURN_RESUME_LIMIT_NOTICE}
    path.write_text(json.dumps(_terminal_gateway_request_payload(_REQUEST, read_json_file(path), sealed)), encoding="utf-8")

    report = recover_gateway_processing_requests_report(paths, startup=True, agent=agent)

    assert (report.summary["archived"], report.load_errors) == (1, [])
    response = _terminal_response(paths)
    assert (response["user_error"], response["error"]) == (TURN_RESUME_LIMIT_BACKUP_NOTICE, TURN_RESUME_LIMIT_BACKUP_NOTICE)
    assert _settlement(response) == {"dead_submissions": 1, "recorded_in_transcript": 0, "backup_turns": 1}
    assert agent.conversation_store.guidance.receipt("steer-s").status == "rejected"
    assert read_gateway_input_receipt(paths, input_id).state == "queued" and (paths.inbox / f"{input_id}.json").is_file()


def test_limit_without_steer_keeps_the_original_sentence(tmp_path, monkeypatch):
    agent = _echo_agent(tmp_path)
    paths = _gateway(agent)
    monkeypatch.setattr(daemon_metadata, "process_identity_is_live", lambda _identity: False)
    _processing(paths, _REQUEST, active_turn_recovery=_marker(MAX_UNPLANNED_RESUME_COUNT))

    assert _restart(paths, agent) == ({"requeued": 0, "failed": 1}, [])

    response = _terminal_response(paths)
    assert response["user_error"] == TURN_RESUME_LIMIT_NOTICE
    assert _settlement(response) == {"dead_submissions": 0, "recorded_in_transcript": 0, "backup_turns": 0}


def test_other_closeouts_leave_submitted_steer_alone(tmp_path):
    """卡死超时等非上限收口时进程未必死了，已提交的插话结果未知：照旧不动，不起备用下一轮。"""
    agent = _echo_agent(tmp_path)
    paths = _gateway(agent)
    entry, input_id, _thread = _steer(agent, paths, "steer-t")
    _taken_by_killed_generation(agent, entry)
    _processing(paths, _REQUEST, processing_failure_count=1)

    report = recover_gateway_processing_requests_report(paths, startup=False, max_attempts=2, timeout_seconds=1, agent=agent)

    assert (report.summary["failed"], _terminal_response(paths)["error_code"]) == (1, "GATEWAY_PROCESSING_TIMEOUT")
    assert "guidance_settlement" not in _terminal_response(paths)
    assert agent.conversation_store.guidance.receipt("steer-t").status == "submitted"
    assert read_gateway_input_receipt(paths, input_id).state == "terminal_unknown"
    assert not (paths.inbox / f"{input_id}.json").exists()


@pytest.mark.parametrize("backup,recorded,expected", [
    (0, 0, TURN_RESUME_LIMIT_NOTICE), (1, 0, TURN_RESUME_LIMIT_BACKUP_NOTICE),
    (0, 2, TURN_RESUME_LIMIT_RECORDED_NOTICE), (1, 1, TURN_RESUME_LIMIT_BACKUP_NOTICE)])
def test_notice_is_chosen_from_settlement_counts(backup, recorded, expected):
    assert turn_resume_limit_notice(backup_turns=backup, recorded_in_transcript=recorded) == expected
    assert TURN_RESUME_LIMIT_BACKUP_NOTICE == "这一轮被打断太多次，已停止自动续跑；你补充的话会作为新的一轮马上处理。"
