"""没有入口回执的插话在收口时内容不能丢（3a 10-03 定，step17h）。

背景：带入口回执（gateway_input_request_id）的插话在回合收口时，入口对账会把它排成“备用下一轮”，内容不丢。
	/steer 控制命令如果 scope 里没有入口请求号（IM 多是这种），收口时只被拒收、没有下一轮，内容就丢了（9b 复审发现的老问题，
	不是 38 那批引入的）。这里按两级处理，判据只看结构化字段（回执级 closeout_replay 标记 + 回执/scope 里的会话身份），不读正文：
	1. 回执里有可重放的结构化输入（会话、渠道、会话 ID、用户身份、插话去重键）→ 经同一个结构化入口排成“备用下一轮”；
	2. 拿不到可重放的输入 → 不改任何状态地放弃重放，给该会话留一条带原因码的宿主提示“你刚才补充的话没有被处理，请重新发送”。
	两种收口（续跑上限收口、普通回合结束）都经 settle_gateway_inputs_for_turn，所以同一个分流同时覆盖两条路。
	有入口回执的旧路径行为不变（仍由入口对账排备用下一轮，计数仍记 backup_turns）。
"""
from __future__ import annotations

import json
from dataclasses import replace

from agent_py_agent.agent.conversation.host_notices import (
    host_notice,
    host_notices_from,
    pending_host_notices,
    queue_host_notice,
    take_host_notices,
    with_host_notice_lines,
)
from agent_py_agent.agent.conversation.turn_resume_notice import (
    STEER_CLOSEOUT_UNAVAILABLE_NOTICE,
    TURN_RESUME_LIMIT_BACKUP_NOTICE,
)
from agent_py_agent.agent.gateway_parts import daemon_metadata, read_json_file
from agent_py_agent.agent.gateway_parts.input_delivery_service import (
    _settle_one_steer_closeout,
    read_gateway_input_receipt,
    settle_gateway_inputs_for_turn,
)
from agent_py_agent.agent.gateway_parts.recovery import (
    MAX_UNPLANNED_RESUME_COUNT,
)
from agent_py_agent.agent.gateway_parts.steer_closeout_replay import (
    STEER_CLOSEOUT_NOTICE_CODE,
    STEER_CLOSEOUT_NOTICE_SOURCE,
    steer_replay_input,
)
from agent_py_agent.tests.test_shutdown_turn_resume import _echo_agent, _gateway, _processing
from agent_py_agent.tests.test_turn_resume_limit import (
    _REQUEST,
    _marker,
    _restart,
    _terminal_response,
)

_STEER = "另外把 todo.txt 的行数也告诉我。"


# 函数用途: 按 IM /steer 的真实形状登记一条“没有入口回执”的插话：元数据带会话与渠道身份、不带 gateway_input_request_id；
#   返回 (插话条目, 去重键, 会话编号)。
def _steer_without_receipt(agent, key: str, *, thread_id: str = "", with_identity: bool = True):
    store = agent.conversation_store
    owner = agent.home_paths.owner_id
    metadata = {
        "kind": "active_turn_user_input",
        "record_in_transcript": True,
        "expected_turn_id": _REQUEST,
        "channel_message_id": key,
    }
    if with_identity:
        metadata.update({
            "thread_id": thread_id or store.threads.get_or_create({"canonical_user_id": owner}).thread_id,
            "channel": "feishu",
            "conversation_id": f"conv-{key}",
        })
    entry = store.guidance.append_once({
        "message": _STEER, "sender": owner, "target_type": "request", "target_id": _REQUEST, "priority": "high",
        "delivery": "current_request", "metadata": metadata,
    }, dedupe_key=key)
    return entry, key, str(metadata.get("thread_id") or "")


# 函数用途: 直接调 Gateway 的收口入口（两种收口共用这一处），返回它写下的结构化计数。
def _settle(paths, agent, turn_id: str = _REQUEST) -> dict:
    return settle_gateway_inputs_for_turn(paths, target_turn_id=turn_id, conversation_store=agent.conversation_store)


# 函数用途: 读出一条已排队重放的 request_id 对应的入口回执，供断言“排了且只排了一次”。
def _queued_replay_receipts(paths) -> list[dict]:
    queued = []
    for path in sorted((paths.root / "input_receipts").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("state") == "queued" and str(payload.get("request_id") or "").startswith("steer-replay-"):
            queued.append(payload)
    return queued


# 函数用途: 读出某会话当前还没取走的宿主提示；会话编号空时返回空表（没有会话就没有提示）。
def _pending_notices(store, thread_id: str) -> list[object]:
    if not str(thread_id or "").strip():
        return []
    return list(pending_host_notices(store, thread_id))


def test_replayable_steer_without_receipt_takes_the_backup_turn(tmp_path):
    """没有入口回执、但回执里会话身份齐全的插话：收口时经同一个结构化入口排成备用下一轮，内容出现一次。"""
    agent = _echo_agent(tmp_path)
    paths = _gateway(agent)
    _entry, key, thread_id = _steer_without_receipt(agent, "steer-nr-replay")

    summary = _settle(paths, agent)

    assert summary["steer_replay_queued"] == 1 and summary["steer_replay_unavailable"] == 0
    assert agent.conversation_store.guidance.receipt(key).status == "rejected"
    queued = _queued_replay_receipts(paths)
    assert len(queued) == 1, "只排一轮"
    request_id = queued[0]["request_id"]
    assert (paths.inbox / f"{request_id}.json").is_file(), "备用下一轮的请求真的落进了入口队列"
    prepared = json.loads((paths.inbox / f"{request_id}.json").read_text(encoding="utf-8"))
    assert prepared["goal"] == _STEER, "重放的是同一段补充内容"
    assert prepared["metadata"]["thread_id"] == thread_id
    assert prepared["metadata"]["channel"] == "feishu" and prepared["metadata"]["conversation_id"] == "conv-steer-nr-replay"
    assert "expected_turn_id" not in prepared["metadata"], "重放请求不绑在已经收口的旧回合上"


def test_replayable_steer_replays_once_even_if_settlement_runs_twice(tmp_path):
    """同一个回合收口跑两遍（重试/补交）也只排一轮：回执记了终值，第二遍不再当待办。"""
    agent = _echo_agent(tmp_path)
    paths = _gateway(agent)
    _entry, key, _thread = _steer_without_receipt(agent, "steer-nr-once")

    first = _settle(paths, agent)
    second = _settle(paths, agent)

    assert (first["steer_replay_queued"], second["steer_replay_queued"]) == (1, 0)
    assert len(_queued_replay_receipts(paths)) == 1, "内容只出现一次，不重复"
    assert agent.conversation_store.guidance.receipt(key).migration.get("closeout_replay") == "replay_queued"


def test_replay_request_id_is_derived_from_the_steer_so_replay_is_idempotent(tmp_path):
    """重放的请求号必须由插话身份推出（同一插话永远同一个号）：即使这条插话第二次以“待收口重放”的身份回到收口
    （标记被重置、或另一条路径重新提交），入口也只认这一个请求，排不出第二条请求——同一条补充不会在队列里出现两遍。"""
    agent = _echo_agent(tmp_path)
    paths = _gateway(agent)
    _entry, key, _thread = _steer_without_receipt(agent, "steer-nr-idem")
    store = agent.conversation_store
    replay = steer_replay_input(store.guidance.receipt(key))
    assert replay is not None
    assert replay.identity() == steer_replay_input(store.guidance.receipt(key)).identity(), "同一插话的请求号稳定"

    first = _settle(paths, agent)

    # 把回执按“又被收口拒收一次”重放回待办（模拟重跑），再让收口处理同一个插话的第二遍。
    receipt = store.guidance.receipt(key)
    reset = replace(receipt, migration={**dict(receipt.migration), "closeout_replay": "pending"})
    store.storage.guidance_dedupe_path(key).write_text(json.dumps(reset.to_dict()), encoding="utf-8")
    assert store.guidance.receipt(key).migration.get("closeout_replay") == "pending"
    second = _settle(paths, agent)

    assert first["steer_replay_queued"] == 1
    assert len(_queued_replay_receipts(paths)) == 1, "同一插话在入口只有一个请求，不会排两轮"
    assert len(list(paths.inbox.glob("steer-replay-*.json"))) == 1
    assert replay.identity()[0] == _queued_replay_receipts(paths)[0]["request_id"]
    # 第二遍命中同一个请求：不新建入口项，但内容仍在队列里，所以这个计数按“内容已在队列”记为 1。
    assert second["steer_replay_queued"] == 1, "第二次命中同一个请求，内容仍在队列里"
    # be 2026-10-03 复审窗口：enqueue 已经成功、mark_closeout_replay 还没落盘就重启，重跑收口时回执仍是 pending。
    # 这时内容其实已经在队列里，第二遍绝不能算成“排不了”——否则会给用户发“请重新发送”，用户一重发就是双份。
    assert second["steer_replay_unavailable"] == 0, "已排队的插话不能被算成重放不了"
    assert [n for n in _pending_notices(store, _thread) if getattr(n, "code", "") == STEER_CLOSEOUT_NOTICE_CODE] == [], \
        "已经排进下一轮，不应再提示用户重发"


def test_consumed_replay_receipt_counts_as_not_queued(tmp_path):
    """入口回执已经是 consumed（这条输入早被消费）时不算排上：queue_gateway_input_locked 不排队、原样返回，
    按 state=="queued" 判断正好把它排除在外，不会把一条已消费的插话当成“已在队列里”。"""
    agent = _echo_agent(tmp_path)
    paths = _gateway(agent)
    store = agent.conversation_store
    owner = agent.home_paths.owner_id
    thread_id = store.threads.get_or_create({"canonical_user_id": owner}).thread_id
    _entry, key, _thread = _steer_without_receipt(agent, "steer-nr-consumed", thread_id=thread_id)
    receipt = store.guidance.receipt(key)
    # 先用真实收口入口排一次，再把它改成 consumed：重放请求号与内容指纹都由生产代码推出，必然合法。
    first = _settle(paths, agent)
    assert first["steer_replay_queued"] == 1
    request_id = _queued_replay_receipts(paths)[0]["request_id"]
    receipt_path = paths.root / "input_receipts" / f"{request_id}.json"
    queued = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt_path.write_text(json.dumps({**queued, "state": "consumed"}), encoding="utf-8")

    # 把插话回执放回待办，模拟重跑收口；此时入口那条已是 consumed，不能算“内容已在队列里”。
    reset = replace(receipt, migration={**dict(receipt.migration), "closeout_replay": "pending"})
    store.storage.guidance_dedupe_path(key).write_text(json.dumps(reset.to_dict()), encoding="utf-8")

    summary = {"errors": 0, "steer_replay_queued": 0, "steer_replay_unavailable": 0}
    _settle_one_steer_closeout(paths, store, store.guidance.receipt(key), summary)

    assert summary["steer_replay_queued"] == 0, "已消费的输入不是“内容已在队列里”"
    assert summary["steer_replay_unavailable"] == 1
    assert _queued_replay_receipts(paths) == []
    assert store.guidance.receipt(key).migration.get("closeout_replay") == "replay_unavailable"


def test_steer_without_any_session_identity_gets_the_resend_notice(tmp_path):
    """回执里连会话都定位不到（重放不了）：不起备用下一轮，不给任何会话发提示，只回标“重放不了”等人工看账。"""
    agent = _echo_agent(tmp_path)
    paths = _gateway(agent)
    store = agent.conversation_store
    _entry, key, _missing = _steer_without_receipt(agent, "steer-nr-noid", with_identity=False)
    assert steer_replay_input(store.guidance.receipt(key)) is None, "缺会话身份的插话不算可重放"

    summary = _settle(paths, agent)

    assert summary["steer_replay_queued"] == 0
    assert summary["steer_replay_unavailable"] == 1
    assert _queued_replay_receipts(paths) == []
    assert store.guidance.receipt(key).migration.get("closeout_replay") == "replay_unavailable"


def test_unreplayable_steer_tells_the_user_to_resend_on_both_surfaces(tmp_path):
    """重放不了但会话定位得到（IM 实况）：不留任何轮次，给该会话一条带原因码的宿主提示；TUI 与 IM 都从这张表取文案。"""
    agent = _echo_agent(tmp_path)
    paths = _gateway(agent)
    store = agent.conversation_store
    owner = agent.home_paths.owner_id
    thread_id = store.threads.get_or_create({"canonical_user_id": owner}).thread_id
    entry, key, _thread = _steer_without_receipt(agent, "steer-nr-nokey", thread_id=thread_id)
    receipt = store.guidance.receipt(key)
    # 收口时回执里没有可用的去重键（排不出幂等请求），但会话身份还在（能告诉用户是谁）：按结构化事实算重放不了。
    assert steer_replay_input(replace(receipt, dedupe_key="")) is None

    summary = {"errors": 0, "steer_replay_queued": 0, "steer_replay_unavailable": 0}
    _settle_one_steer_closeout(paths, store, replace(receipt, dedupe_key=""), summary)

    assert (summary["steer_replay_queued"], summary["steer_replay_unavailable"]) == (0, 1)
    assert _queued_replay_receipts(paths) == []
    notices = pending_host_notices(store, thread_id)
    assert len(notices) == 1
    notice = notices[0]
    assert (notice.source, notice.code, notice.text) == (
        STEER_CLOSEOUT_NOTICE_SOURCE, STEER_CLOSEOUT_NOTICE_CODE, STEER_CLOSEOUT_UNAVAILABLE_NOTICE)
    assert notice.text == "你刚才补充的话没有被处理，请重新发送。"
    # IM 正文前的提示行与 TUI 灰色系统行共用同一张提示表渲染（按编号取走再渲染，模拟真实回复出口）。
    taken = take_host_notices(store, thread_id, [notice.notice_id])
    assert [row.notice_id for row in taken] == [notice.notice_id], "按编号能取到这条提示"
    assert with_host_notice_lines("正文", taken) == "【提示】你刚才补充的话没有被处理，请重新发送。\n\n正文"
    assert with_host_notice_lines("正文", (notice.to_dict(),)) == with_host_notice_lines("正文", taken)


def test_taken_host_notices_still_render(tmp_path):
    """回归：take_host_notices 返回的是 HostNotice（真实回复出口就是这样取的），渲染必须认得对象，
    否则提示取走后在 IM/TUI 文案里整条消失——用户什么都看不到，也不报错。"""
    agent = _echo_agent(tmp_path)
    store = agent.conversation_store
    owner = agent.home_paths.owner_id
    thread_id = store.threads.get_or_create({"canonical_user_id": owner}).thread_id
    notice = host_notice(STEER_CLOSEOUT_NOTICE_SOURCE, STEER_CLOSEOUT_NOTICE_CODE, STEER_CLOSEOUT_UNAVAILABLE_NOTICE)
    assert queue_host_notice(store, thread_id, notice)

    taken = take_host_notices(store, thread_id, [notice.notice_id])

    assert host_notices_from(taken) == (notice,)
    assert with_host_notice_lines("正文", taken) == f"【提示】{STEER_CLOSEOUT_UNAVAILABLE_NOTICE}\n\n正文"


def test_steer_with_identity_and_receipt_keeps_the_old_path(tmp_path, monkeypatch):
    """有入口回执的旧路径行为不变：仍由入口对账排成备用下一轮，计数记 backup_turns，不走新的两分支。"""
    agent = _echo_agent(tmp_path)
    paths = _gateway(agent)
    monkeypatch.setattr(daemon_metadata, "process_identity_is_live", lambda _identity: False)
    store = agent.conversation_store
    owner = agent.home_paths.owner_id
    thread_id = store.threads.get_or_create({"canonical_user_id": owner}).thread_id
    from agent_py_agent.agent.gateway_parts.input_delivery_service import (
        bind_gateway_input_active_locked,
        gateway_input_transition,
        load_or_prepare_gateway_input_locked,
    )
    input_id = "gwreq-msg-steer-nr-old"
    store.guidance.append_once({
        "message": _STEER, "sender": owner, "target_type": "request", "target_id": _REQUEST, "priority": "high",
        "delivery": "current_request",
        "metadata": {"kind": "active_turn_user_input", "record_in_transcript": True, "expected_turn_id": _REQUEST,
                     "channel_message_id": "steer-nr-old", "gateway_input_request_id": input_id, "thread_id": thread_id},
    }, dedupe_key="steer-nr-old")
    prepared = {"id": input_id, "request_id": input_id, "kind": "ask", "goal": _STEER, "user_id": owner,
                "metadata": {"client_input_digest": "digest-old", "expected_turn_id": _REQUEST,
                             "gateway_input_request_id": input_id, "message_id": "steer-nr-old"}}
    with gateway_input_transition(paths, input_id):
        receipt, _created = load_or_prepare_gateway_input_locked(
            paths, request_id=input_id, client_input_digest="digest-old", client_message_id="steer-nr-old",
            guidance_dedupe_key="steer-nr-old", prepared_request=prepared)
        bind_gateway_input_active_locked(paths, receipt, target_turn_id=_REQUEST, guidance_dedupe_key="steer-nr-old")
    _processing(paths, _REQUEST, active_turn_recovery=_marker(MAX_UNPLANNED_RESUME_COUNT))

    assert _restart(paths, agent) == ({"requeued": 0, "failed": 1}, [])

    response = _terminal_response(paths)
    assert response["user_error"] == TURN_RESUME_LIMIT_BACKUP_NOTICE
    assert response["guidance_settlement"]["backup_turns"] == 1
    assert response["guidance_settlement"]["steer_replay_queued"] == 0
    assert read_gateway_input_receipt(paths, input_id).state == "queued", "旧路径仍由入口对账排队"
    assert agent.conversation_store.guidance.receipt("steer-nr-old").migration.get("closeout_replay") is None
