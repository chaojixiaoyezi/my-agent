"""宿主提示（host notices，2026-09-27）：待送达的提示存在会话线程上，同一会话下一条前台回复提交时取走（提交即已读）。

锁定：
- 存取：清洗与限长、同来源替换、最多 5 条、按编号取走、按来源清除、坏条目丢弃；线程记录往返，旧记录为空。
- 模型看不到：上下文包（完整与精简）不含待送达提示；最终消息正文不拼提示；模型收到的输入与模型可见历史投影都没有提示原文。
- Gateway：用户消息落账后、模型执行前在流里发布 host_notice；正常回复提交时按编号取走，写进最终元数据与
  channel_delivery（/result 白名单放行，/progress 不转发）；下一轮不重复；回合失败不取走。
- 显示：飞书在同一条回复正文前加“【提示】”；TUI 发起窗口与同会话窗口画成灰色系统行；历史回放排在用户消息之后、
  回复之前，有快照的回合不重复。
- 智能程度检测：结论排进发起检测的会话，/effort 看过、或重新开始检测时清掉。
全部用 echo 后端或假传输，零网络，不读真实配置。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.adapter.manager import (
    _GatewayReplyPollClient,
    _reply_text_with_host_notices,
)
from agent_py_agent.agent.backends.base import EchoBackend
from agent_py_agent.agent.conversation.background_context import _minimal_context_bundle
from agent_py_agent.agent.conversation.background_transcript import (
    read_background_transcript_events,
)
from agent_py_agent.agent.conversation.history_display import conversation_history_display_events
from agent_py_agent.agent.conversation.host_notices import (
    HOST_NOTICE_LIMIT_COUNT,
    HostNotice,
    clear_host_notices,
    host_notice,
    host_notices_from,
    pending_host_notices,
    queue_host_notice,
    take_host_notices,
    with_host_notice_lines,
)
from agent_py_agent.agent.conversation.models import ConversationThread
from agent_py_agent.agent.conversation.native_history import provider_history_messages_from_rows
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import (
    GatewayAskParams,
    _process_gateway_requests,
    gateway_paths,
    submit_gateway_ask,
)
from agent_py_agent.agent.gateway_parts.http_handlers import (
    _public_result,
    _read_public_progress_events,
)
from agent_py_agent.agent.gateway_parts.request_history import GatewayAssistantTurn
from agent_py_agent.agent.gateway_parts.stream_writer import write_host_notice_events
from agent_py_agent.agent.settings import reasoning_probe as probe
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.cli.chat_parts.tui_block_renderer import TuiRenderContext, _render_block
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime, TuiTurnEventAdapter
from agent_py_agent.tests.test_gateway_foreground_transcript import _foreground
from agent_py_agent.tests.test_reasoning_probe import _DEEPSEEK, _Wire
from agent_py_agent.tests.test_reasoning_probe import _agent as _probe_agent
from agent_py_agent.tests.test_reasoning_probe import _run as _effort

_SESSION = "notice-session"
_TEXT = "智能程度检测：支持按档位调节"


# 函数用途: 建一个带 Gateway 目录的 echo Agent（不按用户分 owner，读写同一个会话存储）。
def _gateway_agent(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), prompt_files=[],
                                    gateway_per_user_owner_scoping=False), tmp_path / "ws")
    paths = gateway_paths(agent)
    for path in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses):
        path.mkdir(parents=True, exist_ok=True)
    return agent, paths


# 函数用途: 以同一聊天会话提交并处理一条 Gateway 请求，返回（请求号, 响应, chunk 流事件）。
def _ask(tmp_path, agent, paths, prompt: str):
    request_id, _request_path, response_path = submit_gateway_ask(
        paths, params=GatewayAskParams(prompt=prompt, save=False, chat_session_id=_SESSION, agent=agent))
    _process_gateway_requests(agent, paths)
    chunks = next(tmp_path.rglob(f"{request_id}.chunks.jsonl"), None)
    rows = [json.loads(line) for line in chunks.read_text(encoding="utf-8").splitlines()] if chunks else []
    return request_id, json.loads(response_path.read_text(encoding="utf-8")), rows


# 函数用途: 取出聊天会话对应的线程编号。
def _thread_id(agent) -> str:
    return agent.conversation_store.threads.resolve(
        channel="chat", channel_conversation_id=_SESSION, channel_user_id="local-agent").thread_id


# 函数用途: 读出一个线程的全部消息记录。
def _rows(store, thread_id: str) -> list:
    return list(store.messages.page_after_offset_report(thread_id, after=0)[0])


def test_notices_are_cleaned_replaced_capped_taken_and_cleared(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread_id = store.threads.get_or_create({"canonical_user_id": "owner-a"}).thread_id
    first = host_notice("probe", "supported", "结论\x07一\n二")
    assert first.text == "结论 一 二" and len(first.notice_id) == 12
    assert queue_host_notice(store, thread_id, first)
    newer = host_notice("probe", "unsupported", "新结论")
    assert queue_host_notice(store, thread_id, newer) and pending_host_notices(store, thread_id) == (newer,)  # 同来源替换
    for index in range(6):
        queue_host_notice(store, thread_id, host_notice(f"src-{index}", "c", f"提示{index}"))
    pending = pending_host_notices(store, thread_id)
    assert len(pending) == HOST_NOTICE_LIMIT_COUNT and [notice.text for notice in pending] == [f"提示{i}" for i in range(1, 6)]
    assert take_host_notices(store, thread_id, [pending[0].notice_id, "missing"]) == (pending[0],)
    assert pending_host_notices(store, thread_id) == pending[1:]
    assert clear_host_notices(store, thread_id, "src-5") and pending_host_notices(store, thread_id) == pending[1:4]
    assert not queue_host_notice(store, "no-such-thread", first) and take_host_notices(store, "no-such-thread", ["x"]) == ()
    assert take_host_notices(store, thread_id, []) == () and pending_host_notices(store, thread_id) == pending[1:4]
    assert host_notice("a", "b", "长" * 900).text == "长" * 500
    rows = [{"notice_id": "", "source": "s", "code": "c", "text": "t"}, "bad", {"notice_id": "i", "source": "s", "code": "", "text": "t"}]
    assert host_notices_from(rows) == (HostNotice("i", "s", "", "t"),)


def test_thread_record_roundtrips_and_model_context_never_contains_notices(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread_id = store.threads.get_or_create({"canonical_user_id": "owner-a"}).thread_id
    assert queue_host_notice(store, thread_id, host_notice("probe", "supported", "只给用户的提示"))
    loaded = store.threads.load(thread_id)
    assert loaded.pending_host_notices[0]["text"] == "只给用户的提示"
    assert ConversationThread.from_dict(loaded.to_dict()).pending_host_notices == loaded.pending_host_notices
    broken = {**loaded.to_dict(), "pending_host_notices": [{"notice_id": 1}, "x"]}
    assert ConversationThread.from_dict(broken).pending_host_notices == ()
    old = {key: value for key, value in loaded.to_dict().items() if key != "pending_host_notices"}
    assert ConversationThread.from_dict(old).pending_host_notices == ()  # 旧记录
    for bundle in (store.context_bundle(thread_id), _minimal_context_bundle(loaded)):
        assert "pending_host_notices" not in bundle["thread"] and "只给用户的提示" not in json.dumps(bundle, ensure_ascii=False)


def test_gateway_turn_shows_notice_first_commits_it_once_and_keeps_it_from_the_model(tmp_path, monkeypatch):
    agent, paths = _gateway_agent(tmp_path)
    seen: list[str] = []
    original = EchoBackend.generate

    def spy(self, prompt, *args, **kwargs):
        seen.append(json.dumps([prompt, args, kwargs], ensure_ascii=False, default=str))
        return original(self, prompt, *args, **kwargs)

    monkeypatch.setattr(EchoBackend, "generate", spy)
    _ask(tmp_path, agent, paths, "第一句")
    store, thread_id = agent.conversation_store, _thread_id(agent)
    notice = host_notice("reasoning_probe", "supported", _TEXT)
    assert queue_host_notice(store, thread_id, notice)
    _request_id, response, chunks = _ask(tmp_path, agent, paths, "第二句")
    kinds = [row.get("kind") for row in chunks]
    assert kinds.count("host_notice") == 1 and chunks[kinds.index("host_notice")]["notice"] == notice.to_dict()
    assert not {"model_delta", "assistant_final", "tool_progress", "assistant_commentary"} & set(kinds[:kinds.index("host_notice")])
    assert response["ok"] and response["channel_delivery"]["host_notices"] == [notice.to_dict()]
    assert _public_result(response)["channel_delivery"]["host_notices"] == [notice.to_dict()]
    assert pending_host_notices(store, thread_id) == ()  # 提交即已读
    rows = _rows(store, thread_id)
    final = [row for row in rows if row.role == "assistant"][-1]
    assert final.metadata["host_notices"] == [notice.to_dict()] and _TEXT not in final.content
    assert seen and all(_TEXT not in item for item in seen)  # 模型这一轮看不到
    assert _TEXT not in json.dumps(provider_history_messages_from_rows(rows), ensure_ascii=False)
    events = [(event["kind"], event["payload"].get("text")) for event in conversation_history_display_events(rows)]
    at = events.index(("system_message", _TEXT))
    assert events[at - 1] == ("user_message", "第二句") and events.count(("system_message", _TEXT)) == 1
    _third_id, third, third_chunks = _ask(tmp_path, agent, paths, "第三句")
    assert "host_notices" not in third["channel_delivery"] and all(row.get("kind") != "host_notice" for row in third_chunks)
    assert all(_TEXT not in item for item in seen)  # 下一轮模型同样看不到


def test_failed_turn_keeps_the_notice_for_the_next_reply(tmp_path, monkeypatch):
    agent, paths = _gateway_agent(tmp_path)
    _ask(tmp_path, agent, paths, "第一句")
    store, thread_id = agent.conversation_store, _thread_id(agent)
    notice = host_notice("reasoning_probe", "supported", _TEXT)
    queue_host_notice(store, thread_id, notice)

    def broken(self, prompt, *args, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(EchoBackend, "generate", broken)
    _request_id, response, _chunks = _ask(tmp_path, agent, paths, "第二句")
    assert response["ok"] is False and pending_host_notices(store, thread_id) == (notice,)


def test_progress_does_not_forward_notices_and_im_puts_them_before_the_body(tmp_path, monkeypatch):
    chunk = tmp_path / "req.chunks.jsonl"
    chunk.write_text(json.dumps({"kind": "host_notice", "notice": HostNotice("n1", "p", "c", _TEXT).to_dict()}) + "\n",
                     encoding="utf-8")
    assert _read_public_progress_events(chunk, 0) == ([], 1)  # /progress 不转发，飞书只从最终回复拿
    body = {"ok": True, "response": "模型回复", "channel_delivery": {"host_notices": [HostNotice("n1", "p", "c", _TEXT).to_dict()]}}
    assert _reply_text_with_host_notices(body) == f"【提示】{_TEXT}\n\n模型回复"
    assert _reply_text_with_host_notices({"ok": True, "response": "模型回复"}) == "模型回复"
    assert with_host_notice_lines("", body["channel_delivery"]["host_notices"]) == f"【提示】{_TEXT}"

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(body).encode("utf-8")

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: _Response())
    pending = SimpleNamespace(request_id="req", user_id="u", channel="feishu")
    assert _GatewayReplyPollClient(8420).poll_response(pending, 0.0) == f"【提示】{_TEXT}\n\n模型回复"


def test_tui_turn_and_same_conversation_windows_show_one_gray_system_line(tmp_path):
    runtime = TuiRuntime("session")
    adapter = TuiTurnEventAdapter(runtime, "req-1")
    notice = HostNotice("n1", "reasoning_probe", "supported", _TEXT)
    assert adapter.on_gateway_event({"kind": "host_notice", "notice": notice.to_dict()})
    adapter.on_gateway_event({"kind": "host_notice", "notice": notice.to_dict()})  # 重放同一事件不多出一行
    assert not adapter.on_gateway_event({"kind": "host_notice", "notice": {"text": "没有编号"}})
    blocks = [block for block in runtime.store.snapshot().stable_blocks if block.text == _TEXT]
    assert len(blocks) == 1 and blocks[0].role == "system"
    assert adapter.on_gateway_event({"kind": "host_notice", "notice": HostNotice("n2", "other", "c", "另一条").to_dict()})
    assert [block.text for block in runtime.store.snapshot().stable_blocks if block.role == "system"][-2:] == [_TEXT, "另一条"]
    assert _render_block(blocks[0], TuiRenderContext(width=120))[0][0] == ("class:tui-muted", f"◇ {_TEXT}")  # 灰色系统行
    agent, writer, store, thread, _request = _foreground(tmp_path)
    write_host_notice_events(writer, [notice])
    live = read_background_transcript_events(agent, thread_id=thread.thread_id, after=0)["events"]
    assert [row["payload"]["text"] for row in live if row["kind"] == "system_message"] == [_TEXT]
    meta = {"gateway_request_id": "gwreq-live", "conversation_request_id": "request-a", "assistant_part_id": "final"}
    turn = GatewayAssistantTurn(end_reason="completed", display_snapshot=writer.prepare_display_history(),
                                host_notices=(notice.to_dict(),))
    store.messages.append({"thread_id": thread.thread_id, "role": "assistant", "content": "回复", "metadata": {**meta, **turn.metadata()}})
    replay = conversation_history_display_events(_rows(store, thread.thread_id))
    assert [event["payload"].get("text") for event in replay].count(_TEXT) == 1  # 快照里已有，不按元数据重复


@pytest.fixture
def _sync_probe(monkeypatch):
    _Wire(_DEEPSEEK).install(monkeypatch)
    monkeypatch.setattr(probe, "_spawn", lambda target: target())


def test_probe_verdict_is_queued_to_the_origin_session_and_cleared_when_seen(tmp_path, monkeypatch, _sync_probe):
    agent, _profile_id = _probe_agent(tmp_path)
    _effort(agent, "/effort high")  # 自动检测，同步跑完
    store = agent.conversation_store
    thread_id = store.threads.resolve(channel="feishu", channel_conversation_id="c-1", channel_user_id="u-1").thread_id
    pending = pending_host_notices(store, thread_id)
    assert len(pending) == 1 and pending[0].source == "reasoning_probe" and pending[0].code == "max_above_low"
    assert "relay-model" in pending[0].text and "支持按档位调节" in pending[0].text and "/effort revert" in pending[0].text
    _effort(agent, "/effort")  # 在这里看到了结论
    assert pending_host_notices(store, thread_id) == ()
    _effort(agent, "/effort probe")
    assert len(pending_host_notices(store, thread_id)) == 1
    monkeypatch.setattr(probe, "_spawn", lambda target: None)
    _effort(agent, "/effort probe")  # 重新检测：旧结论作废
    assert pending_host_notices(store, thread_id) == ()
