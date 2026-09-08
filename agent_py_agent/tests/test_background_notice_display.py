"""后台完成从 canonical transcript 投影到 TUI；实时正文与恢复历史共用消息身份。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from wcwidth import wcswidth


# LLM: 夹具使用与 Gateway 一致的 owner-scoped 会话库；只写临时 canonical 消息，不启动模型。
# 函数用途: 创建可用于本地轮询和冷 owner 恢复的真实消息源。
def _message_store(tmp_path):
    from agent_py_agent.agent.conversation.store import ConversationStore

    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread({
        "canonical_user_id": "local-agent", "channel": "chat",
        "channel_conversation_id": "session-1", "channel_user_id": "local-agent", "now": 1.0,
    })
    return store, thread


# LLM: 后台正文只通过 append_message 写入；metadata 决定是否公开，文本本身没有控制意义。
# 函数用途: 生成一条有稳定消息 ID 的后台最终回复。
def _append_background_message(store, thread, content="后台最终回复", **metadata):
    return store.append_message({
        "thread_id": thread.thread_id, "role": "assistant", "content": content, "now": 20.0,
        "metadata": {"background_delivery_reason": "root_subagents_terminal",
                     "assistant_part_id": "final", **metadata},
    })


def test_background_page_projects_canonical_message(tmp_path):
    from agent_py_agent.agent.conversation.message_stream import read_background_response_page

    store, thread = _message_store(tmp_path)
    message = _append_background_message(store, thread)
    rows, cursor, ok = read_background_response_page(store, thread.thread_id)
    assert ok and len(rows) == 1
    assert rows[0]["message_id"] == message.message_id
    assert rows[0]["notice_id"] == message.message_id
    assert rows[0]["schema_version"] == "background_message.v1"
    assert rows[0]["content"] == message.content
    assert cursor == store.message_byte_offset_after(thread.thread_id, message.message_id)
    assert not (store.root / "notices").exists()
    assert read_background_response_page(store, thread.thread_id, after=cursor) == ([], cursor, True)


def test_background_page_filters_roles_and_internal_audit(tmp_path):
    from agent_py_agent.agent.conversation.message_stream import read_background_response_page

    store, thread = _message_store(tmp_path)
    store.append_message({"thread_id": thread.thread_id, "role": "user", "content": "用户输入"})
    store.append_message({"thread_id": thread.thread_id, "role": "assistant", "content": "前台回复"})
    _append_background_message(store, thread, "过程回复", assistant_part_id="commentary:1")
    _append_background_message(store, thread, "内部审计", reason="audit_finding", task_id="task-audit")
    final = _append_background_message(store, thread, "公开汇报")
    rows, cursor, ok = read_background_response_page(store, thread.thread_id)
    assert ok and [row["message_id"] for row in rows] == [final.message_id]
    assert cursor == store.message_byte_offset_after(thread.thread_id, final.message_id)


def test_background_page_preserves_cursor_on_corruption(tmp_path):
    from agent_py_agent.agent.conversation.message_stream import read_background_response_page

    store, thread = _message_store(tmp_path)
    final = _append_background_message(store, thread)
    offset = store.message_byte_offset_after(thread.thread_id, final.message_id)
    with store._message_path(thread.thread_id).open("ab") as handle:
        handle.write(b"not-json\n")
    assert read_background_response_page(store, thread.thread_id, after=offset) == ([], offset, False)


def test_background_page_has_no_notice_file_dependency(tmp_path):
    from agent_py_agent.agent.conversation.message_stream import read_background_response_page

    store, thread = _message_store(tmp_path)
    _append_background_message(store, thread)
    (store.root / "notices").write_text("旧旁路不可写也不影响 canonical 读取")
    assert read_background_response_page(store, thread.thread_id)[2] is True


def test_tui_consumes_background_notices_and_publishes(tmp_path):
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
    from agent_py_agent.cli.chat_parts.tui_threading import _consume_background_notices

    store, thread = _message_store(tmp_path)
    message = _append_background_message(store, thread)
    agent = SimpleNamespace(conversation_store=store)
    runtime = TuiRuntime("session-1")
    seen = set()
    assert _consume_background_notices(agent, "session-1", runtime, [None], seen)
    assert _consume_background_notices(agent, "session-1", runtime, [None], seen)
    blocks = runtime.store.snapshot().stable_blocks
    assert [(block.role, block.text) for block in blocks] == [("assistant", message.content)]
    assert blocks[0].block_id == f"history:{thread.thread_id}:{message.message_id}:assistant"
    assert runtime.background_message_cursor == store.message_byte_offset_after(thread.thread_id, message.message_id)


def test_tui_skips_when_no_thread_or_no_session(tmp_path: Path) -> None:
    """无会话/无线程/无 notices 时静默跳过。"""
    from agent_py_agent.cli.chat_parts.tui_threading import _consume_background_notices

    class _Store:
        root = tmp_path / "conversations"

        def resolve_thread_report(self, **kwargs):
            return None, None

    class _Runtime:
        def publish_background_response(self, text, *, thread_id="", message_id=""):
            raise AssertionError("不应发布")

    class _Agent:
        conversation_store = _Store()

    _consume_background_notices(_Agent(), "", _Runtime(), [None], set())
    _consume_background_notices(_Agent(), "session-1", _Runtime(), [None], set())


def test_tui_thin_client_fetches_notices_via_http(tmp_path: Path) -> None:
    """Gateway 轻量客户端（无 conversation_store）走 HTTP /client/notices 分支。"""
    from agent_py_agent.cli.chat_parts.tui_threading import _consume_background_notices

    published: list[str] = []
    fetched: list[dict[str, object]] = []

    class _Agent:
        def request_background_notices(self, session_id, *, after, event_after, event_stream_id):
            fetched.append(
                {
                    "session_id": session_id,
                    "after": after,
                    "event_after": event_after,
                }
            )
            return {
                "ok": True,
                "cursor": 200,
                "transcript_events": [],
                "event_cursor": event_after,
                "event_stream_id": event_stream_id,
                "active_task_count": 2,
                "agent_activity": {
                    "schema_version": "conversation_agent_activity.v5",
                    "active_task_count": 2,
                    "compact_count": 3,
                    "active_task_projection_ok": True,
                    "subagents": [
                        {
                            "run_id": "child-1",
                            "name": "level-design",
                            "status": "RUNNING",
                            "description": "设计游戏关卡",
                            "attempts": 1,
                        }
                    ],
                    "hidden_subagent_count": 0,
                    "subagent_projection_ok": True,
                    "task_progress_items": [
                        {"id": "qa", "title": "整合测试", "status": "done"}
                    ],
                    "task_progress_generation_id": "generation-http",
                    "task_progress_plan_revision": 2,
                    "task_progress_projection_ok": True,
                },
                "notices": [
                    {
                        "schema_version": "background_message.v1",
                        "display_kind": "assistant_response",
                        "thread_id": "thread-http-1",
                        "message_id": "message-http-1",
                        "reason": "subagent_runner_finished",
                        "content": "HTTP 后台完成通知测试。",
                        "summary": "HTTP 后台完成通知测试。",
                        "created_at": 150.0,
                    }
                ],
            }

    class _Runtime:
        def update_background_activity(
            self,
            count,
            snapshot,
        ):
            task_progress = snapshot["task_progress"]
            published.append(
                f"active:{count}:{snapshot['compact_count']}:{snapshot['subagents'][0]['run_id']}:"
                f"{task_progress['items'][0]['id']}:{snapshot['hidden_subagent_count']}:"
                f"{snapshot['projection_ok']}:{snapshot['task_progress_projection_ok']}:"
                f"{bool(snapshot['main_activity'])}:{task_progress['generation_id']}:"
                f"{task_progress['plan_revision']}"
            )
            return True

        def publish_task_progress_snapshot(self, _items, **_kwargs):
            return True

        def publish_background_response(self, text, *, thread_id="", message_id=""):
            published.append(text)

    seen: set[tuple[str, str]] = set()
    runtime = _Runtime()
    assert _consume_background_notices(
        _Agent(), "session-http", runtime, [None], seen
    )
    assert len(fetched) == 1
    assert fetched[0]["after"] == 0.0
    assert fetched[0]["event_after"] == 0
    assert len(published) == 2
    assert published[0] == (
        "active:2:3:child-1:qa:0:True:True:False:generation-http:2"
    )
    assert published[1] == "HTTP 后台完成通知测试。"
    # 游标推进后不重复
    assert _consume_background_notices(
        _Agent(), "session-http", runtime, [None], seen
    )
    assert fetched[1]["after"] == 200


def test_tui_keeps_distinct_background_replies_with_same_timestamp() -> None:
    """同一 scheduler tick 的错误回执和最终交付都必须各显示一次。"""
    from agent_py_agent.cli.chat_parts.tui_threading import (
        _consume_background_notices,
    )

    published: list[str] = []
    calls = [
        [
            {
                "schema_version": "background_message.v1",
                "display_kind": "assistant_response",
                "thread_id": "thread-same-time",
                "message_id": "message-retry",
                "content": "工具绑定暂时失败。",
                "created_at": 150.0,
            }
        ],
        [
            {
                "schema_version": "background_message.v1",
                "display_kind": "assistant_response",
                "thread_id": "thread-same-time",
                "message_id": "message-retry",
                "content": "工具绑定暂时失败。",
                "created_at": 150.0,
            },
            {
                "schema_version": "background_message.v1",
                "display_kind": "assistant_response",
                "thread_id": "thread-same-time",
                "message_id": "message-final",
                "content": "任务完成，以下是最终交付。",
                "created_at": 150.0,
            },
        ],
    ]
    fetched_after: list[int] = []

    class _Agent:
        def request_background_notices(self, _session_id, *, after, event_after, event_stream_id):
            fetched_after.append(after)
            rows = calls.pop(0) if calls else []
            return {
                "ok": True,
                "cursor": 200 + len(fetched_after),
                "transcript_events": [],
                "event_cursor": event_after,
                "event_stream_id": event_stream_id,
                "active_task_count": 0,
                "agent_activity": {
                    "active_task_count": 0,
                    "active_task_projection_ok": True,
                    "subagent_projection_ok": True,
                    "task_progress_projection_ok": True,
                    "subagents": [],
                    "task_progress_items": [],
                },
                "notices": rows,
            }

    class _Runtime:
        def update_background_activity(self, *_args, **_kwargs):
            return False

        def publish_task_progress_snapshot(self, *_args, **_kwargs):
            return False

        def publish_background_response(self, text, *, thread_id="", message_id=""):
            del thread_id
            published.append(text)

    seen: set[tuple[str, str]] = set()
    runtime = _Runtime()
    assert _consume_background_notices(
        _Agent(), "session-same-time", runtime, [None], seen
    )
    assert _consume_background_notices(
        _Agent(), "session-same-time", runtime, [None], seen
    )

    assert fetched_after == [0, 201]
    assert published == [
        "工具绑定暂时失败。",
        "任务完成，以下是最终交付。",
    ]


def test_tui_notice_transport_failure_preserves_projection_and_reports_failure() -> None:
    """HTTP 断线不清空旧活动投影，并把失败交给监视线程退避。"""
    from agent_py_agent.cli.chat_parts.tui_threading import _consume_background_notices

    class _Agent:
        def request_background_notices(self, session_id, *, after, event_after, event_stream_id):
            return {
                "ok": False,
                "notices": [],
                "cursor": after,
                "transcript_events": [],
                "event_cursor": event_after,
                "event_stream_id": event_stream_id,
                "active_task_count": 0,
            }

    class _Runtime:
        def update_background_activity(self, *_args, **_kwargs):
            raise AssertionError("失败快照不得清空上一次真实活动")

    assert not _consume_background_notices(
        _Agent(),
        "session-http-failed",
        _Runtime(),
        [None],
        set(),
    )


def test_background_transcript_sink_reuses_free_code_diff_renderer() -> None:
    """后台 main 的过程、思考和 Update diff 进入同一 TUI block renderer。"""
    from agent_py_agent.agent.conversation.background_transcript import (
        BackgroundTranscriptSink,
        read_background_transcript_events,
    )
    from agent_py_agent.cli.chat_parts.tui_block_renderer import (
        TuiRenderContext,
        fragments_text,
        render_tui_snapshot,
    )
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime

    agent = SimpleNamespace()
    sink = BackgroundTranscriptSink(
        agent,
        thread_id="thread-rich-background",
        task_id="task-rich-background",
    )
    assert sink.write_thinking_delta("先核对现有渲染，再修改事件传输。") is True
    assert sink.write_thinking(
        "先核对现有渲染，再修改事件传输。",
        duration_seconds=2.5,
    ) is True
    sink.write_model("把后台工具结果接回现有正文渲染器：")
    sink.write_progress(
        {
            "round": 1,
            "call_index": 0,
            "tool": "edit_file",
            "phase": "started",
            "status": "执行中",
            "detail": "src/components/ui.tsx",
        }
    )
    sink.write_progress(
        {
            "round": 1,
            "call_index": 0,
            "tool": "edit_file",
            "phase": "finished",
            "status": "完成",
            "ok": True,
            "output": "更新成功",
            "display": {
                "kind": "diff",
                "path": "src/components/ui.tsx",
                "lines_added": 1,
                "lines_removed": 1,
                "hidden_lines": 0,
                "lines": [
                    {
                        "kind": "remove",
                        "old_line": 120,
                        "new_line": None,
                        "text": "return oldValue;",
                    },
                    {
                        "kind": "add",
                        "old_line": None,
                        "new_line": 120,
                        "text": "return newValue;",
                    },
                ],
            },
        }
    )
    sink.finish()

    page = read_background_transcript_events(
        agent,
        thread_id="thread-rich-background",
        after=0,
    )
    kinds = [row["kind"] for row in page["events"]]
    assert kinds == [
        "thinking_started",
        "thinking_delta",
        "thinking_completed",
        "assistant_completed",
        "tool_started",
        "tool_completed",
    ]
    assert page["cursor"] == 6
    runtime = TuiRuntime("session-rich-background")
    assert runtime.publish_background_transcript_events(page["events"]) == 6

    frame = render_tui_snapshot(
        runtime.store.snapshot(),
        TuiRenderContext(
            width=100,
            detailed_transcript=True,
            show_all=True,
        ),
    )
    transcript = "\n".join(fragments_text(line) for line in frame.transcript_lines)
    assert "先核对现有渲染，再修改事件传输。" in transcript
    assert "把后台工具结果接回现有正文渲染器" in transcript
    assert "Update(src/components/ui.tsx)" in transcript
    assert "Added 1 lines, removed 1 lines" in transcript
    assert "return oldValue;" in transcript
    assert "return newValue;" in transcript
    old_line = next(
        line for line in frame.transcript_lines if "return oldValue;" in fragments_text(line)
    )
    new_line = next(
        line for line in frame.transcript_lines if "return newValue;" in fragments_text(line)
    )
    assert any("class:tui-diff-remove" in fragment[0] for fragment in old_line)
    assert any("class:tui-diff-add" in fragment[0] for fragment in new_line)


def test_child_transcript_length_limit_notice_keeps_partial_body_and_no_control_event() -> None:
    from agent_py_agent.agent.conversation.background_transcript import BackgroundTranscriptSink

    for body in ("", "我会继续修改"):
        rows = []
        sink = BackgroundTranscriptSink(
            SimpleNamespace(), thread_id="thread-child-length", task_id="child-length",
            request_id="bg-agent:child-length:attempt-1",
            event_writer=lambda _agent, **event: rows.append(event),
        )
        sink.finish(final_text=body, publish_final=True, end_reason="max-tokens")
        assert rows[-1]["kind"] == "system_message"
        assert rows[-1]["block_id"] == "bg-agent:child-length:attempt-1:turn-end"
        assert "长度限制" in rows[-1]["payload"]["text"]
        assert [row["payload"]["text"] for row in rows if row["kind"] == "assistant_completed"] == ([body] if body else [])
        assert all(row["kind"] in {"system_message", "assistant_completed"} for row in rows)


def test_child_transcript_finish_publishes_canonical_reply_without_duplicate_commentary() -> None:
    from agent_py_agent.agent.conversation.background_transcript import (
        BACKGROUND_TRANSCRIPT_SCHEMA,
        BackgroundTranscriptSink,
    )

    rows: list[dict[str, object]] = []

    def append_event(_agent, **kwargs) -> None:
        rows.append(
            {
                "schema": BACKGROUND_TRANSCRIPT_SCHEMA,
                "seq": len(rows) + 1,
                **kwargs,
            }
        )

    request_id = "bg-agent:child-reply:attempt-2"
    sink = BackgroundTranscriptSink(
        SimpleNamespace(),
        thread_id="thread-child-reply",
        task_id="child-reply",
        request_id=request_id,
        event_writer=append_event,
    )
    sink.write_model("已经收到插话，我会继续等待研究员。")
    sink.finish(
        final_text="已经收到插话，我会继续等待研究员。",
        publish_final=True,
    )

    assert [row["kind"] for row in rows] == ["assistant_completed"]
    assert rows[0]["payload"] == {
        "text": "已经收到插话，我会继续等待研究员。",
        "process": False,
    }

    duplicate_rows: list[dict[str, object]] = []

    def append_duplicate(_agent, **kwargs) -> None:
        duplicate_rows.append(dict(kwargs))

    duplicate = BackgroundTranscriptSink(
        SimpleNamespace(),
        thread_id="thread-child-reply",
        task_id="child-reply",
        request_id="bg-agent:child-reply:attempt-3",
        event_writer=append_duplicate,
    )
    duplicate.write_model("先创建研究员。")
    duplicate.write_progress(
        {
            "round": 1,
            "call_index": 0,
            "tool": "create_subagents",
            "phase": "started",
        }
    )
    duplicate.finish(final_text="先创建研究员。", publish_final=True)

    assert [row["kind"] for row in duplicate_rows] == [
        "assistant_completed",
        "tool_started",
    ]


def test_background_tool_input_progress_is_transient_and_redacted() -> None:
    from agent_py_agent.agent.conversation.background_transcript import (
        BackgroundTranscriptSink,
        read_background_transcript_events,
    )

    agent = SimpleNamespace()
    sink = BackgroundTranscriptSink(
        agent,
        thread_id="thread-tool-input",
        task_id="task-tool-input",
    )
    assert sink.write_tool_input_progress(
        {
            "schema": "provider_tool_input_progress.v1",
            "phase": "started",
            "stream_index": 0,
            "tool": "write_file",
            "received_chars": 0,
            "partial_json": "secret body",
        }
    )
    assert sink.write_tool_input_progress(
        {
            "schema": "provider_tool_input_progress.v1",
            "phase": "streaming",
            "stream_index": 0,
            "tool": "write_file",
            "received_chars": 16_384,
        }
    )
    assert sink.write_tool_input_progress(
        {
            "schema": "provider_tool_input_progress.v1",
            "phase": "ready",
            "stream_index": 0,
            "tool": "write_file",
            "received_chars": 20_000,
        }
    )

    page = read_background_transcript_events(
        agent,
        thread_id="thread-tool-input",
        after=0,
    )
    assert [row["kind"] for row in page["events"]] == [
        "tool_input_started",
        "tool_input_progress",
        "tool_input_completed",
    ]
    serialized = json.dumps(page, ensure_ascii=False)
    assert "secret body" not in serialized
    assert "partial_json" not in serialized
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime

    runtime = TuiRuntime("session-tool-input")
    assert runtime.publish_background_transcript_events(page["events"]) == 3
    snapshot = runtime.store.snapshot()
    assert not any(block.role == "tool_input" for block in snapshot.active_blocks)
    assert snapshot.stable_blocks == ()
    assert snapshot.diagnostics == ()


def test_background_thinking_deltas_are_batched_before_tui_transport(monkeypatch) -> None:
    """逐 token 思考先合批，首片即时可见且完整终态仍能恢复全部正文。"""
    from agent_py_agent.agent.conversation import background_transcript

    now = [100.0]
    monkeypatch.setattr(background_transcript.time, "monotonic", lambda: now[0])
    agent = SimpleNamespace()
    sink = background_transcript.BackgroundTranscriptSink(
        agent,
        thread_id="thread-batched-thinking",
        task_id="task-batched-thinking",
    )

    assert sink.write_thinking_delta("首") is True
    for _index in range(100):
        assert sink.write_thinking_delta("片") is True
    first_page = background_transcript.read_background_transcript_events(
        agent,
        thread_id="thread-batched-thinking",
        after=0,
    )
    assert [row["kind"] for row in first_page["events"]] == [
        "thinking_started",
        "thinking_delta",
    ]
    assert first_page["events"][1]["payload"]["text"] == "首"

    now[0] += background_transcript.BACKGROUND_TRANSCRIPT_DELTA_FLUSH_SECONDS
    assert sink.write_thinking_delta("末") is True
    full_text = "首" + "片" * 100 + "末"
    assert sink.write_thinking(full_text, duration_seconds=3.0) is True
    final_page = background_transcript.read_background_transcript_events(
        agent,
        thread_id="thread-batched-thinking",
        after=0,
    )
    assert [row["kind"] for row in final_page["events"]] == [
        "thinking_started",
        "thinking_delta",
        "thinking_delta",
        "thinking_completed",
    ]
    assert final_page["events"][2]["payload"]["text"] == "片" * 100 + "末"
    assert final_page["events"][3]["payload"]["text"] == full_text


def test_child_transcript_publishes_consumed_input_only_after_provider_acceptance() -> None:
    """child sink 只在模型请求成功后发布 exact 用户消费回执。"""
    from agent_py_agent.agent.conversation.background_transcript import (
        BACKGROUND_TRANSCRIPT_SCHEMA,
        BackgroundTranscriptSink,
    )

    rows: list[dict[str, object]] = []

    def append_event(_agent, **kwargs) -> None:
        rows.append(
            {
                "schema": BACKGROUND_TRANSCRIPT_SCHEMA,
                "seq": len(rows) + 1,
                **kwargs,
            }
        )

    request_id = "bg-agent:child-a:attempt-a"
    sink = BackgroundTranscriptSink(
        SimpleNamespace(),
        thread_id="thread-child-a",
        task_id="child-a",
        request_id=request_id,
        event_writer=append_event,
    )

    sink.begin_active_turn_input(("agent-steer-1", "agent-steer-2"))
    assert rows == []
    sink.complete_active_turn_input(
        ("agent-steer-1", "agent-steer-2"),
        client_messages=(
            ("agent-steer-1", "先读两个核心文件"),
            ("agent-steer-2", "再写报告"),
        ),
    )

    assert len(rows) == 1
    assert rows[0]["kind"] == "active_turn_input_consumed"
    assert rows[0]["phase"] == "completed"
    assert rows[0]["block_id"] == f"{request_id}:active-input:1"
    assert rows[0]["payload"] == {
        "client_message_ids": ["agent-steer-1", "agent-steer-2"],
        "messages": [
            {
                "message_id": "agent-steer-1",
                "text": "先读两个核心文件",
                "truncated": False,
            },
            {
                "message_id": "agent-steer-2",
                "text": "再写报告",
                "truncated": False,
            },
        ],
    }


def test_background_reconnect_rebases_only_process_cursor(tmp_path: Path) -> None:
    """换流不重基canonical游标；新完成块追加后物理游标前进，模型正文和旧文件前缀不变。"""
    from agent_py_agent.agent.conversation.background_transcript import BackgroundTranscriptSink
    from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
    from agent_py_agent.agent.gateway_parts.http_handlers import read_gateway_client_notices
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
    from agent_py_agent.cli.chat_parts.tui_threading import _consume_background_notices

    store, thread = _message_store(tmp_path)
    final = _append_background_message(store, thread, "已保存的最终汇报")
    source = [SimpleNamespace(conversation_store=store)]
    sink = BackgroundTranscriptSink(source[0], thread_id=thread.thread_id, task_id="task-a")
    for index in range(5):
        sink.write_thinking(f"旧进程公开块 {index}")

    class Client:
        def request_background_notices(self, _session, *, after, event_after, event_stream_id):
            return read_gateway_client_notices(
                source[0], scope=GatewayControlScope("local-agent", "chat", "session-1"),
                after=after, event_after=event_after, event_stream_id=event_stream_id,
            )

    runtime, cursor, seen = TuiRuntime("session-1"), [0], set()
    assert _consume_background_notices(Client(), "session-1", runtime, [None], seen, cursor)
    old_stream = runtime.background_event_stream_id
    old_blocks = tuple(runtime.store.snapshot().stable_blocks)
    message_after = store.history_page_report(thread.thread_id).after
    assert cursor == [10] and runtime.background_message_cursor == message_after
    before = store._message_path(thread.thread_id).read_bytes()

    source[0] = SimpleNamespace(conversation_store=store)
    # 新 Agent 还没产生事件的第一次读取也要换代，不能沿用上一进程的大游标。
    assert _consume_background_notices(Client(), "session-1", runtime, [None], seen, cursor)
    assert cursor == [0] and runtime.background_event_stream_id != old_stream
    assert runtime.background_message_cursor == message_after
    assert tuple(runtime.store.snapshot().stable_blocks) == old_blocks
    restarted = BackgroundTranscriptSink(source[0], thread_id=thread.thread_id, task_id="task-a")
    restarted.write_thinking("重连后的公开块")
    assert _consume_background_notices(Client(), "session-1", runtime, [None], seen, cursor)
    blocks = tuple(runtime.store.snapshot().stable_blocks)
    assert blocks[:len(old_blocks)] == old_blocks
    assert blocks[-1].text == "重连后的公开块" and cursor == [2]
    assert runtime.background_message_cursor == store.history_page_report(thread.thread_id).after > message_after
    assert store._message_path(thread.thread_id).read_bytes().startswith(before)
    assert store.recent_messages(thread.thread_id, limit=0) == [final]
    assert _consume_background_notices(Client(), "session-1", runtime, [None], seen, cursor)
    assert tuple(runtime.store.snapshot().stable_blocks) == blocks


def test_background_new_stream_replays_even_when_sequence_is_already_larger():
    """换代按身份判定：新进程可能已经产生更多事件，不能靠数值倒退检测重启。"""
    from agent_py_agent.agent.conversation.background_transcript import (
        BackgroundTranscriptSink,
        read_background_transcript_events,
    )

    old_agent, new_agent = SimpleNamespace(), SimpleNamespace()
    old = BackgroundTranscriptSink(old_agent, thread_id="thread-a", task_id="task-a")
    old.write_thinking("旧消息")
    old_page = read_background_transcript_events(old_agent, thread_id="thread-a", after=0)
    new = BackgroundTranscriptSink(new_agent, thread_id="thread-a", task_id="task-a")
    for index in range(4):
        new.write_thinking(f"新消息 {index}")
    page = read_background_transcript_events(
        new_agent, thread_id="thread-a", after=old_page["cursor"], stream_id=old_page["stream_id"],
    )
    assert len(page["events"]) == 8
    assert [row["payload"]["text"] for row in page["events"] if row["kind"] == "thinking_completed"] == [
        f"新消息 {index}" for index in range(4)
    ]
    assert page["stream_id"] != old_page["stream_id"]


def test_background_stream_ack_requires_valid_page_and_successful_publish():
    """协议损坏、同流倒退或发布失败都不确认新身份；重试仍能消费同一页。"""
    from agent_py_agent.cli.chat_parts.tui_threading import (
        _consume_background_transcript_projection,
    )

    runtime = SimpleNamespace(background_event_stream_id="old")
    cursor = [9]
    for events, seq, stream in [(None, 1, "new"), ([], -1, "new"), ([], 1, None), ([], 1, "old")]:
        assert _consume_background_transcript_projection(
            runtime, events, seq, cursor, stream_id=stream,
        ) == (False, False)
        assert runtime.background_event_stream_id == "old" and cursor == [9]

    def reject(_events):
        raise ValueError("publish failed")

    runtime.publish_background_transcript_events = reject
    assert _consume_background_transcript_projection(
        runtime, [{"seq": 1}], 1, cursor, stream_id="new",
    ) == (False, False)
    assert runtime.background_event_stream_id == "old" and cursor == [9]
    runtime.publish_background_transcript_events = lambda _events: 1
    assert _consume_background_transcript_projection(
        runtime, [{"seq": 1}], 1, cursor, stream_id="new",
    ) == (True, True)
    assert runtime.background_event_stream_id == "new" and cursor == [1]


def test_gateway_notice_page_transports_background_event_cursor(tmp_path: Path) -> None:
    """独立event_after不覆盖canonical字节游标；旧客户端跳过display类型但仍前进物理位置。"""
    from agent_py_agent.agent.conversation.background_transcript import (
        BackgroundTranscriptSink,
    )
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
    from agent_py_agent.agent.gateway_parts.http_handlers import (
        read_gateway_client_notices,
    )

    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "local-agent",
            "channel": "chat",
            "channel_conversation_id": "session-rich-cursor",
            "channel_user_id": "local-agent",
            "now": 1.0,
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-rich-cursor",
            "goal": "后台显示测试",
            "now": 2.0,
        }
    )
    agent = SimpleNamespace(conversation_store=store)
    sink = BackgroundTranscriptSink(
        agent,
        thread_id=thread.thread_id,
        task_id="task-rich-cursor",
    )
    sink.write_model("准备调用工具。")
    sink.write_progress(
        {
            "round": 1,
            "call_index": 0,
            "tool": "run_command",
            "phase": "started",
            "detail": "npm test",
        }
    )

    first = read_gateway_client_notices(
        agent,
        scope=GatewayControlScope("local-agent", "chat", "session-rich-cursor"),
        after=0.0,
        event_after=0,
    )
    assert first["ok"] is True
    assert [row["kind"] for row in first["transcript_events"]] == [
        "assistant_completed",
        "tool_started",
    ]
    assert first["event_cursor"] == 2
    assert first["cursor"] == store.history_page_report(thread.thread_id).after > 0
    assert first["notices"] == []

    second = read_gateway_client_notices(
        agent,
        scope=GatewayControlScope("local-agent", "chat", "session-rich-cursor"),
        after=0.0,
        event_after=first["event_cursor"],
        event_stream_id=first["event_stream_id"],
    )
    assert second["transcript_events"] == []
    assert second["event_cursor"] == 2
    assert second["cursor"] == first["cursor"]


def test_tui_notice_loop_consumes_background_transcript_once() -> None:
    """薄客户端按 event_after 增量发布后台工具块，后续轮询不重复。"""
    from agent_py_agent.agent.conversation.background_transcript import (
        BackgroundTranscriptSink,
        read_background_transcript_events,
    )
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
    from agent_py_agent.cli.chat_parts.tui_threading import _consume_background_notices

    source = SimpleNamespace()
    sink = BackgroundTranscriptSink(
        source,
        thread_id="thread-http-rich",
        task_id="task-http-rich",
    )
    sink.write_model("运行定向测试：")
    sink.write_progress(
        {
            "round": 1,
            "call_index": 0,
            "tool": "run_command",
            "phase": "started",
            "detail": "pytest focused",
        }
    )
    sink.write_progress(
        {
            "round": 1,
            "call_index": 0,
            "tool": "run_command",
            "phase": "finished",
            "ok": True,
            "output": "2 passed",
        }
    )
    fetched: list[int] = []

    class _Agent:
        def request_background_notices(
            self,
            session_id,
            *,
            after,
            event_after,
            event_stream_id,
        ):
            del session_id, after
            fetched.append(event_after)
            page = read_background_transcript_events(
                source,
                thread_id="thread-http-rich",
                after=event_after,
                stream_id=event_stream_id,
            )
            return {
                "ok": True,
                "cursor": 0,
                "notices": [],
                "transcript_events": page["events"],
                "event_cursor": page["cursor"],
                "event_stream_id": page["stream_id"],
                "active_task_count": 0,
                "agent_activity": {
                    "active_task_count": 0,
                    "active_task_projection_ok": True,
                    "subagent_projection_ok": True,
                    "task_progress_projection_ok": True,
                    "subagents": [],
                    "task_progress_items": [],
                },
            }

    runtime = TuiRuntime("session-http-rich")
    event_cursor = [0]
    assert _consume_background_notices(
        _Agent(),
        "session-http-rich",
        runtime,
        [None],
        set(),
        event_cursor,
    )
    stable_count = len(runtime.store.snapshot().stable_blocks)
    assert fetched == [0]
    assert event_cursor == [3]
    assert stable_count == 2
    assert _consume_background_notices(
        _Agent(),
        "session-http-rich",
        runtime,
        [None],
        set(),
        event_cursor,
    )
    assert fetched == [0, 3]
    assert len(runtime.store.snapshot().stable_blocks) == stable_count


def test_background_transcript_lru_eviction_keeps_cursor_forward(monkeypatch) -> None:
    """纯展示线程被 LRU 淘汰后，同会话重现仍使用更大的全局事件序号。"""
    from agent_py_agent.agent.conversation import background_transcript

    monkeypatch.setattr(background_transcript, "BACKGROUND_TRANSCRIPT_MAX_THREADS", 2)
    agent = SimpleNamespace()
    first = background_transcript.BackgroundTranscriptSink(
        agent,
        thread_id="thread-1",
        task_id="task-1",
    )
    first.write_provider_retry(attempt=1, total=5, delay_seconds=1.0)
    first_page = background_transcript.read_background_transcript_events(
        agent,
        thread_id="thread-1",
        after=0,
    )
    old_cursor = first_page["cursor"]

    for index in (2, 3):
        sink = background_transcript.BackgroundTranscriptSink(
            agent,
            thread_id=f"thread-{index}",
            task_id=f"task-{index}",
        )
        sink.write_provider_retry(attempt=1, total=5, delay_seconds=1.0)

    resumed = background_transcript.BackgroundTranscriptSink(
        agent,
        thread_id="thread-1",
        task_id="task-1",
    )
    resumed.write_provider_retry(attempt=2, total=5, delay_seconds=2.0)
    resumed_page = background_transcript.read_background_transcript_events(
        agent,
        thread_id="thread-1",
        after=old_cursor,
        stream_id=first_page["stream_id"],
    )

    assert len(resumed_page["events"]) == 1
    assert resumed_page["events"][0]["seq"] > old_cursor


def test_background_transcript_projects_numeric_compact_events() -> None:
    """后台 Compact 只传冻结数字字段，并复用前台 Compact block 生命周期。"""
    from agent_py_agent.agent.conversation.background_transcript import (
        BackgroundTranscriptSink,
        read_background_transcript_events,
    )
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime

    agent = SimpleNamespace()
    sink = BackgroundTranscriptSink(
        agent,
        thread_id="thread-background-compact",
        task_id="task-background-compact",
    )
    assert sink.write_context_compaction(
        {
            "schema": "model_visible_context_compaction.v1",
            "generation": 1,
            "before_tokens": 118_000,
            "after_tokens": 32_000,
            "trigger_tokens": 115_200,
            "dropped_pairs": 20,
            "preserved_pairs": 5,
            "summary": "不得进入事件",
        }
    ) is True
    for phase, stage, percent in (
        ("started", "preparing", 5),
        ("progress", "summarizing", 52),
        ("completed", "completed", 100),
    ):
        assert sink.write_conversation_compact_progress(
            {
                "schema": "conversation_compaction_progress.v1",
                "generation": 2,
                "operation_id": "transcript:background-2",
                "source_kind": "conversation_transcript",
                "commit_authority": "conversation_thread",
                "phase": phase,
                "stage": stage,
                "percent": percent,
                "before_tokens": 90_000,
                "after_tokens": 30_000,
                "trigger_tokens": 80_000,
                "source_messages": 80,
                "summary": "不得进入事件",
            }
        ) is True

    page = read_background_transcript_events(
        agent,
        thread_id="thread-background-compact",
        after=0,
    )
    assert [row["kind"] for row in page["events"]] == [
        "context_window_compacted",
        "conversation_compaction_started",
        "conversation_compaction_progress",
        "conversation_compaction_completed",
    ]
    assert all("summary" not in row["payload"] for row in page["events"])
    runtime = TuiRuntime("session-background-compact")
    runtime.publish_background_transcript_events(page["events"])
    snapshot = runtime.store.snapshot()
    assert any(
        block.kind == "context_window_compacted" for block in snapshot.stable_blocks
    )
    assert not any(block.role == "compact" for block in snapshot.active_blocks)


def test_background_superseded_compact_candidate_leaves_no_failure_block() -> None:
    """后台 live 候选放弃后只结束动画，不冻结红色失败，也不占住下一次 Compact。"""
    from agent_py_agent.agent.conversation.background_transcript import (
        BackgroundTranscriptSink,
        read_background_transcript_events,
    )
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime

    agent = SimpleNamespace()
    sink = BackgroundTranscriptSink(
        agent,
        thread_id="thread-background-superseded",
        task_id="task-background-superseded",
    )
    base = {
        "schema": "conversation_compaction_progress.v1",
        "generation": 1,
        "operation_id": "live-tool:background-a",
        "source_kind": "active_turn_tool_archive",
        "commit_authority": "conversation_thread",
        "before_tokens": 118_000,
        "after_tokens": 109_000,
        "trigger_tokens": 115_200,
        "source_messages": 80,
    }
    assert sink.write_conversation_compact_progress(
        {**base, "phase": "started", "stage": "preparing", "percent": 5}
    )
    assert sink.write_conversation_compact_progress(
        {
            **base,
            "phase": "superseded",
            "stage": "candidate_discarded",
            "percent": 0,
        }
    )

    page = read_background_transcript_events(
        agent,
        thread_id="thread-background-superseded",
        after=0,
    )
    assert [row["kind"] for row in page["events"]] == [
        "conversation_compaction_started",
        "conversation_compaction_superseded",
    ]
    runtime = TuiRuntime("session-background-superseded")
    runtime.publish_background_transcript_events(page["events"])
    snapshot = runtime.store.snapshot()
    assert not any(block.role == "compact" for block in snapshot.active_blocks)
    assert not any(block.role == "compact" for block in snapshot.stable_blocks)


def test_tui_notice_loop_backs_off_and_resets_after_success(monkeypatch) -> None:
    """连续断线按 0.5/1/2 秒退避；成功后恢复 1 秒并重置退避。"""
    from agent_py_agent.cli.chat_parts import tui_threading

    outcomes = iter((False, False, False, True, False, False))
    waits: list[float] = []

    class _StopEvent:
        def is_set(self):
            return False

        def wait(self, delay):
            waits.append(delay)
            return len(waits) >= 6

    monkeypatch.setattr(
        tui_threading,
        "_consume_background_notices",
        lambda *_args, **_kwargs: next(outcomes),
    )

    tui_threading._background_notice_loop(
        _StopEvent(),
        object(),
        "session-backoff",
        object(),
        [None],
        foreground_running_ref=[True],
    )

    assert waits == [0.5, 1.0, 2.0, 1.0, 0.5, 1.0]


def test_tui_notice_loop_slows_healthy_idle_sessions(monkeypatch) -> None:
    """后台与前台都空闲时按五秒刷新，避免退出观察后的 tmux 窗口压垮 Gateway。"""
    from agent_py_agent.cli.chat_parts import tui_threading

    waits: list[float] = []

    class _StopEvent:
        def is_set(self):
            return False

        def wait(self, delay):
            waits.append(delay)
            return len(waits) >= 2

    monkeypatch.setattr(
        tui_threading,
        "_consume_background_notices",
        lambda *_args, **_kwargs: True,
    )

    tui_threading._background_notice_loop(
        _StopEvent(),
        object(),
        "session-idle",
        object(),
        [None],
        foreground_running_ref=[False],
    )

    assert waits == [5.0, 5.0]


def test_notice_loop_exposes_failed_refresh_without_changing_task(monkeypatch) -> None:
    from agent_py_agent.cli.chat_parts import tui_threading
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime

    runtime = TuiRuntime("refresh-health")
    runtime.update_background_activity(1)
    before = runtime.store.snapshot()
    outcomes = iter((False, False, True))
    observed = []
    redraws = []

    class StopEvent:
        def is_set(self):
            return False

        def wait(self, delay):
            snapshot = runtime.store.snapshot()
            observed.append((snapshot.background_sync_failed, delay))
            assert snapshot.active_blocks == before.active_blocks
            assert snapshot.status == before.status
            assert snapshot.pending_steers == before.pending_steers
            return len(observed) == 3

    monkeypatch.setattr(
        tui_threading, "_consume_background_notices", lambda *args: next(outcomes)
    )
    tui_threading._background_notice_loop(
        StopEvent(), object(), runtime.session_id, runtime,
        [SimpleNamespace(invalidate=lambda: redraws.append(True))],
    )

    assert observed == [(True, 0.5), (True, 1.0), (False, 1.0)]
    assert len(redraws) == 2


def test_tui_notice_loop_keeps_active_background_session_realtime(monkeypatch) -> None:
    """runtime 已接收 typed active count 时，即使前台空闲也继续每秒刷新。"""
    from agent_py_agent.cli.chat_parts import tui_threading

    waits: list[float] = []

    class _StopEvent:
        def is_set(self):
            return False

        def wait(self, delay):
            waits.append(delay)
            return len(waits) >= 2

    runtime = SimpleNamespace(has_active_background_task=lambda: True)
    monkeypatch.setattr(
        tui_threading,
        "_consume_background_notices",
        lambda *_args, **_kwargs: True,
    )

    tui_threading._background_notice_loop(
        _StopEvent(),
        object(),
        "session-active",
        runtime,
        [None],
        foreground_running_ref=[False],
    )

    assert waits == [1.0, 1.0]


def test_gateway_notice_snapshot_reports_canonical_active_task_count(tmp_path: Path) -> None:
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
    from agent_py_agent.agent.gateway_parts.http_handlers import (
        read_gateway_client_notices,
    )

    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "local-agent",
            "channel": "chat",
            "channel_conversation_id": "session-active",
            "channel_user_id": "local-agent",
            "now": 1.0,
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-active",
            "goal": "等待直属子代理完成",
            "now": 2.0,
        }
    )
    agent = SimpleNamespace(conversation_store=store)
    scope = GatewayControlScope("local-agent", "chat", "session-active")

    active = read_gateway_client_notices(agent, scope=scope, after=0.0)
    assert active["ok"] is True
    assert active["active_task_count"] == 1
    assert active["agent_activity"]["active_task_count"] == 1
    assert active["agent_activity"]["subagents"] == []

    store.update_task_status(
        {
            "task_id": "task-active",
            "status": "completed",
            "expected_status": "active",
        }
    )
    completed = read_gateway_client_notices(agent, scope=scope, after=0.0)
    assert completed["active_task_count"] == 0

    store.update_task_status(
        {
            "task_id": "task-active",
            "status": "active",
            "expected_status": "completed",
        }
    )
    store.update_task_status(
        {
            "task_id": "task-active",
            "status": "interrupted",
            "expected_status": "active",
        }
    )
    interrupted = read_gateway_client_notices(agent, scope=scope, after=0.0)
    assert interrupted["active_task_count"] == 0


def test_gateway_notice_snapshot_uses_resolved_owner_and_scope_identity(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.gateway_parts import http_handlers
    from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope

    owner_store = ConversationStore(tmp_path / "owner-conversations")
    owner_thread = owner_store.get_or_create_thread(
        {
            "canonical_user_id": "user-a",
            "channel": "tui-test",
            "channel_conversation_id": "session-owner",
            "channel_user_id": "user-a",
            "now": 1.0,
        }
    )
    owner_store.bind_task(
        {
            "thread_id": owner_thread.thread_id,
            "task_id": "task-owner",
            "goal": "owner 后台任务",
            "now": 2.0,
        }
    )
    base_store = ConversationStore(tmp_path / "base-conversations")
    base_agent = SimpleNamespace(conversation_store=base_store)
    owner_agent = SimpleNamespace(conversation_store=owner_store)
    scope = GatewayControlScope("user-a", "tui-test", "session-owner")
    resolved: list[object] = []

    def resolve(_base_agent, exact_scope):
        resolved.append(exact_scope)
        return owner_agent

    monkeypatch.setattr(http_handlers, "resolve_loaded_gateway_scope_agent", resolve)

    snapshot = http_handlers.read_gateway_client_notices(
        base_agent,
        scope=scope,
        after=0.0,
    )

    assert resolved == [scope]
    assert snapshot["ok"] is True
    assert snapshot["active_task_count"] == 1
    assert snapshot["agent_activity"]["active_task_count"] == 1


def test_gateway_notice_cold_owner_does_not_materialize_agent(monkeypatch) -> None:
    from agent_py_agent.agent.gateway_parts import http_handlers
    from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope

    calls: list[GatewayControlScope] = []

    def resolve(_base_agent, exact_scope):
        calls.append(exact_scope)
        return None

    monkeypatch.setattr(http_handlers, "resolve_loaded_gateway_scope_agent", resolve)

    snapshot = http_handlers.read_gateway_client_notices(
        SimpleNamespace(),
        scope=GatewayControlScope("cold-user", "local", "cold-session"),
        after=0.0,
        event_after=3000,
        event_stream_id="old-process",
    )

    assert len(calls) == 1
    assert snapshot["ok"] is True
    assert snapshot["owner_state"] == "cold"
    assert snapshot["active_task_count"] == 0
    assert snapshot["event_cursor"] == 3000
    assert snapshot["event_stream_id"] == "old-process"
    assert snapshot["transcript_events"] == []


def test_thin_client_sends_process_identity_separate_from_message_offset():
    from agent_py_agent.cli.chat_client_context import GatewayChatClientAgent

    requests = []

    def post(path, body, **kwargs):
        requests.append((path, body, kwargs))
        return 200, {"ok": True}

    client = SimpleNamespace(post_gateway_json=post)
    assert GatewayChatClientAgent.request_background_notices(
        client, "session-a", after=234, event_after=900, event_stream_id="process-a",
    ) == {"ok": True}
    path, body, kwargs = requests[0]
    assert path == "/client/notices" and kwargs == {"timeout": 2.0}
    assert body["after"] == 234 and body["event_after"] == 900
    assert body["event_stream_id"] == "process-a" and body["conversation_id"] == "session-a"


def test_gateway_notice_cold_owner_replays_exact_durable_final_without_agent(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from agent_py_agent.agent.conversation.background_transcript import BackgroundTranscriptSink
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.gateway_parts import http_handlers
    from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
    from agent_py_agent.agent.user_space.owner_resolver import (
        OwnerIdentity,
        resolve_owner_home,
    )

    owner = OwnerIdentity.provider_user("tui-test", "cold-user")
    owner_home = resolve_owner_home(tmp_path, owner).home_dir
    store_root = (
        owner_home
        / "workspace"
        / "runtime"
        / "workspaces"
        / "workspace-a"
        / "conversations"
    )
    store = ConversationStore(store_root)
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "cold-user",
            "channel": "tui-test",
            "channel_conversation_id": "cold-session",
            "channel_user_id": "cold-user",
            "now": 10.0,
        }
    )
    message = _append_background_message(store, thread, "冷 owner 的最终回复仍然可见。")
    store.goals_dir.rmdir()

    monkeypatch.setattr(
        http_handlers,
        "resolve_loaded_gateway_scope_agent",
        lambda _base_agent, _scope: None,
    )
    snapshot = http_handlers.read_gateway_client_notices(
        SimpleNamespace(home_paths=SimpleNamespace(root=tmp_path)),
        scope=GatewayControlScope(
            "cold-user",
            "tui-test",
            "cold-session",
            resolved_owner=owner,
        ),
        after=0.0,
    )

    assert snapshot["ok"] is True
    assert snapshot["owner_state"] == "cold"
    assert [row["content"] for row in snapshot["notices"]] == [
        "冷 owner 的最终回复仍然可见。"
    ]
    assert snapshot["cursor"] == store.message_byte_offset_after(thread.thread_id, message.message_id)
    assert not store.goals_dir.exists()

    foreground = store.append_message({
        "thread_id": thread.thread_id, "role": "assistant", "content": "冷用户的前台回复",
        "metadata": {"gateway_request_id": "gwreq-cold", "assistant_part_id": "final"},
    })
    for capable, expected in ((False, []), (True, [foreground.message_id])):
        page = http_handlers.read_gateway_client_notices(
            SimpleNamespace(home_paths=SimpleNamespace(root=tmp_path)),
            scope=GatewayControlScope("cold-user", "tui-test", "cold-session", resolved_owner=owner),
            after=snapshot["cursor"], display=http_handlers.NoticeDisplayCapabilities(foreground_messages=capable),
        )
        assert page["ok"] and [row["message_id"] for row in page["notices"]] == expected
        assert page["owner_state"] == "cold" and not store.goals_dir.exists()

    sink = BackgroundTranscriptSink(SimpleNamespace(conversation_store=store), thread_id=thread.thread_id, task_id="task-a")
    sink.write_thinking("重启前保存的完整过程")
    after = store.message_byte_offset_after(thread.thread_id, foreground.message_id)
    for capable in (False, True):
        page = http_handlers.read_gateway_client_notices(
            SimpleNamespace(home_paths=SimpleNamespace(root=tmp_path)),
            scope=GatewayControlScope("cold-user", "tui-test", "cold-session", resolved_owner=owner),
            after=after, display=http_handlers.NoticeDisplayCapabilities(display_checkpoints=capable),
        )
        assert page["ok"] and len(page["notices"]) == int(capable)
        assert page["cursor"] == store.history_page_report(thread.thread_id).after > after
        assert page["owner_state"] == "cold" and not store.goals_dir.exists()
        if capable:
            assert page["notices"][0]["display_kind"] == "process_event"
            assert page["notices"][0]["display_events"][0]["payload"]["text"] == "重启前保存的完整过程"


def test_runtime_keeps_multiple_background_notices_as_distinct_blocks() -> None:
    """同一 thread 连续后台回复不能因复用稳定 block id 而丢掉后者。"""
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime

    runtime = TuiRuntime("session-notices")
    runtime.publish_background_response("第一条", thread_id="thread-1", message_id="msg-1")
    runtime.publish_background_response("第二条", thread_id="thread-1", message_id="msg-2")

    snapshot = runtime.store.snapshot()
    notices = [block for block in snapshot.stable_blocks if block.role == "assistant"]
    assert [block.text for block in notices] == ["第一条", "第二条"]
    assert notices[0].block_id != notices[1].block_id


def test_runtime_background_activity_is_one_removable_animated_block() -> None:
    from agent_py_agent.cli.chat_parts.tui_block_renderer import (
        TuiRenderContext,
        fragments_text,
        render_tui_snapshot,
    )
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime

    runtime = TuiRuntime("session-working")
    children = [
        {
            "run_id": "child-engine",
            "name": "game-engine",
            "role": "worker",
            "status": "RUNNING",
            "description": "实现超级玛丽核心玩法",
            "attempts": 2,
            "context_tokens": 12345,
            "compact_count": 1,
            "created_at": 100.0,
            "ended_at": 0.0,
        },
        {
            "run_id": "child-levels",
            "name": "level-design",
            "role": "worker",
            "status": "DONE",
            "description": "设计前三个关卡",
            "attempts": 1,
            "context_tokens": 6789,
            "compact_count": 0,
            "created_at": 100.0,
            "ended_at": 145.0,
        },
    ]
    context_usage = {
        "schema": "model_visible_context_usage.v1",
        "estimated": True,
        "context_window_tokens": 128_000,
        "compact_trigger_tokens": 115_200,
        "current_tokens": 42_100,
        "prompt_tokens": 8_700,
        "messages_tokens": 20_000,
        "runtime_guidance_tokens": 400,
        "tool_schema_tokens": 13_000,
        "protocol": "native",
    }
    progress_items = [
        {"id": "core", "title": "游戏核心", "status": "done"},
        {"id": "qa", "title": "整合测试", "status": "in_progress"},
    ]
    assert runtime.update_background_activity(
        1,
        {
            "compact_count": 3,
            "main_activity": {"context_usage": context_usage},
            "subagents": children,
            "task_progress": {"items": progress_items},
        },
    ) is True
    assert runtime.update_background_activity(1, {"subagents": children}) is False
    snapshot = runtime.store.snapshot()
    active = [block for block in snapshot.active_blocks if block.role == "background"]
    assert len(active) == 1
    assert active[0].metadata["active_task_count"] == 1
    assert len(active[0].metadata["subagents"]) == 2
    assert snapshot.status.context_tokens == 42_100
    assert snapshot.status.context_usage is not None
    assert snapshot.status.context_usage.prompt_tokens == 8_700
    assert snapshot.status.compact_count == 3
    assert runtime.has_active_background_task() is True
    todo = next(block for block in snapshot.active_blocks if block.role == "todo")
    assert todo.metadata["items"] == progress_items
    assert runtime.needs_periodic_refresh() is True

    frame = render_tui_snapshot(
        snapshot,
        TuiRenderContext(
            width=100,
            spinner_index=2,
            now=150.0,
        ),
    )
    transcript = "\n".join(fragments_text(line) for line in frame.transcript_lines)
    rendered = "\n".join(fragments_text(line) for line in frame.agent_lines)
    assert frame.input_status_lines == ()
    assert "Working · main · 等待 1 个子代理 · 0:00" in transcript
    assert "main" not in rendered
    assert "game-engine · 运行中 · 实现超级玛丽核心玩法 · 0:50" in rendered
    assert "ctx 12.3k · compact 1" in rendered
    assert "重试" not in rendered  # 正常工作片计数不能被展示成失败重试次数。
    assert "level-design · 已完成 · 设计前三个关卡 · 0:45 · ctx 6.8k · compact 0" in rendered
    assert "模型已生成回复" not in rendered
    assert "尝试 1" not in rendered
    assert "/stop 停止后台任务" in fragments_text(frame.footer)

    changed_children = [dict(children[0], context_tokens=14000), children[1]]
    assert runtime.update_background_activity(
        1,
        {"subagents": changed_children},
    ) is True

    narrow = render_tui_snapshot(
        runtime.store.snapshot(),
        TuiRenderContext(width=52, spinner_index=0, now=151.0),
    )
    assert len(narrow.agent_lines) == 2
    assert all(max(0, wcswidth(fragments_text(line))) <= 52 for line in narrow.agent_lines)

    from agent_py_agent.cli.chat_parts.tui_threading import _publish_background_activity

    assert (
        _publish_background_activity(
            runtime,
            {
                "active_task_count": 0,
                "active_task_projection_ok": False,
                "subagent_projection_ok": True,
                "subagents": [],
            },
        )
        is False
    )
    assert any(
        block.role == "background" for block in runtime.store.snapshot().active_blocks
    )

    assert runtime.update_background_activity(0) is True
    assert runtime.has_active_background_task() is False
    assert not any(
        block.role == "background" for block in runtime.store.snapshot().active_blocks
    )
    assert runtime.publish_task_progress_snapshot(
        [
            {"id": "core", "title": "游戏核心", "status": "done"},
            {"id": "qa", "title": "整合测试", "status": "done"},
        ]
    ) is True
    final_todo = next(
        block
        for block in runtime.store.snapshot().active_blocks
        if block.role == "todo"
    )
    assert [item["status"] for item in final_todo.metadata["items"]] == [
        "done",
        "done",
    ]
    assert runtime.needs_periodic_refresh() is True
    assert runtime.needs_periodic_refresh() is False
    assert runtime.publish_task_progress_snapshot(None) is False
    assert runtime.publish_task_progress_snapshot([]) is True
    assert not any(
        block.role == "todo" for block in runtime.store.snapshot().active_blocks
    )


def test_long_main_activity_stays_on_one_terminal_line() -> None:
    """长 thinking 只能截断，不能把固定 Working 区撑成两行。"""
    from agent_py_agent.cli.chat_parts.tui_block_renderer import (
        TuiRenderContext,
        fragments_text,
        render_tui_snapshot,
    )
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime

    runtime = TuiRuntime("session-long-main-activity")
    assert runtime.update_background_activity(
        1,
        {
            "main_activity": {
                "phase": "thinking",
                "activity": (
                    "The user wants me to finalize the complete game and verify every file "
                    "before creating the final response"
                ),
                "started_at": 100.0,
            }
        },
    ) is True

    frame = render_tui_snapshot(
        runtime.store.snapshot(),
        TuiRenderContext(width=52, spinner_index=0, now=152.0),
    )
    working_lines = [
        line
        for line in frame.transcript_lines
        if "Working · main" in fragments_text(line)
    ]

    assert len(working_lines) == 1
    assert max(0, wcswidth(fragments_text(working_lines[0]))) <= 52


def test_main_activity_elapsed_never_falls_back_to_old_panel_start() -> None:
    """缺少当前主任务时钟时，底部面板年龄不能冒充本回合耗时。"""
    from agent_py_agent.cli.chat_parts.tui_block_renderer import (
        TuiRenderContext,
        fragments_text,
        render_tui_snapshot,
    )
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime

    runtime = TuiRuntime("session-main-clock")
    assert runtime.update_background_activity(
        1,
        {
            "main_activity": {},
            "subagents": [
                {
                    "run_id": "child-live",
                    "name": "worker",
                    "status": "RUNNING",
                    "created_at": 990.0,
                }
            ],
        },
    ) is True
    snapshot = runtime.store.snapshot()
    background = next(
        block for block in snapshot.active_blocks if block.role == "background"
    )
    background.metadata["started_at"] = 100.0

    frame = render_tui_snapshot(
        snapshot,
        TuiRenderContext(width=80, spinner_index=0, now=1_000.0),
    )
    rendered = "\n".join(
        fragments_text(line) for line in frame.transcript_lines
    )

    assert "Working · main · 等待 1 个子代理 · 0:00" in rendered
    assert "15:00" not in rendered
