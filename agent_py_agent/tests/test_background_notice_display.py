"""S-BG1 回归：任一后台主代理车道完成 → notices 文件 → TUI 显示。

真机实锤（s2-fast）：子代理完成后父代理在后台自动续跑汇总，但 TUI 无事件
驱动不刷新——用户看不到"后台已自动汇总"。修复：
1. gateway 在 child wake、observation、due policy 任一后台轮 report 产生后写 notices
2. TUI 监视线程周期读取并以普通 assistant 消息显示已提交的后台最终回复
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from wcwidth import wcswidth


def test_gateway_records_background_notice(tmp_path: Path) -> None:
    """后台轮 report 产生 → notices 文件按 thread_id 落一行 JSON。"""
    from agent_py_agent.agent.conversation.runtime import _record_background_notice

    store_root = tmp_path / "conversations"
    store = SimpleNamespace(root=store_root)
    report = SimpleNamespace(
        thread_id="thread-bg-1",
        reason="subagent_runner_finished",
        response="子代理 A/B/C 已完成：t1=1-5 t2=1-5 t3=1-5",
        created_at=1787200000.0,
        delivery_status="sent",
    )
    _record_background_notice(store, report)

    notices_path = store_root / "notices" / "thread-bg-1.notices.jsonl"
    assert notices_path.exists()
    rows = [json.loads(line) for line in notices_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["thread_id"] == "thread-bg-1"
    assert rows[0]["reason"] == "subagent_runner_finished"
    assert "子代理" in rows[0]["summary"]
    assert rows[0]["schema_version"] == "background_notice.v2"
    assert rows[0]["display_kind"] == "assistant_response"
    assert rows[0]["content"] == report.response


def test_gateway_background_notice_carries_final_task_progress(tmp_path: Path) -> None:
    import hashlib

    from agent_py_agent.agent.conversation.runtime import _record_background_notice
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.task_progress import (
        with_task_progress_display_plan,
        write_task_progress,
    )

    owner_root = tmp_path / "owner"
    store = ConversationStore(tmp_path / "conversations")
    task_path = str(tmp_path / "project" / "bbb")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "local-agent",
            "channel": "chat",
            "channel_conversation_id": "session-progress",
            "channel_user_id": "local-agent",
            "now": 1.0,
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-progress",
            "task_path": task_path,
            "goal": "完成游戏",
            "now": 2.0,
        }
    )
    ledger_id = f"task-path:{hashlib.sha256(task_path.encode('utf-8')).hexdigest()[:16]}"
    write_task_progress(
        owner_root,
        ledger_id,
        with_task_progress_display_plan(
            {"items": [{"id": "qa", "title": "整合测试", "status": "done"}]},
            generation_id="turn-final",
            item_ids=["qa"],
        ),
    )
    report = SimpleNamespace(
        thread_id=thread.thread_id,
        task_id="task-progress",
        reason="subagent_runner_finished",
        response="游戏已完成。",
        created_at=100.0,
        delivery_status="sent",
    )

    _record_background_notice(
        store,
        report,
        agent=SimpleNamespace(
            home_paths=SimpleNamespace(owner_home_dir=owner_root)
        ),
    )

    notice_path = store.root / "notices" / f"{thread.thread_id}.notices.jsonl"
    row = json.loads(notice_path.read_text(encoding="utf-8").splitlines()[0])
    assert row["task_progress_items"] == [
        {"id": "qa", "title": "整合测试", "status": "done"}
    ]
    assert row["task_progress_generation_id"] == "turn-final"
    assert row["task_progress_plan_revision"] == 1


def test_gateway_skips_internal_or_empty_background_notice(tmp_path: Path) -> None:
    """被 delivery contract 抑制或没有正文的后台轮不进入用户 transcript。"""
    from agent_py_agent.agent.conversation.runtime import _record_background_notice

    store_root = tmp_path / "conversations"
    store = SimpleNamespace(root=store_root)
    for delivery_status, response in (("suppressed", "内部整合"), ("sent", "")):
        _record_background_notice(
            store,
            SimpleNamespace(
                thread_id="thread-bg-hidden",
                reason="subagent_runner_finished",
                response=response,
                created_at=1787200000.0,
                delivery_status=delivery_status,
            ),
        )

    assert not (store_root / "notices").exists()


def test_gateway_notice_write_failure_is_silent(tmp_path: Path) -> None:
    """notices 写入失败不抛（显示增强不能影响后台轮主流程）。"""
    from agent_py_agent.agent.conversation.runtime import _record_background_notice

    store = SimpleNamespace(root=tmp_path / "conversations")
    report = SimpleNamespace(
        thread_id="t", reason="r", response="x", created_at=1.0, delivery_status="sent"
    )
    # 覆盖 notices 目录为文件（mkdir 失败路径）
    bad_root = tmp_path / "bad"
    bad_root.mkdir()
    (bad_root / "notices").write_text("not-a-dir", encoding="utf-8")
    _record_background_notice(SimpleNamespace(root=bad_root), report)  # 不抛即通过


def test_tui_consumes_background_notices_and_publishes(tmp_path: Path) -> None:
    """TUI 监视：v2 notices 新行 → 普通助手回复 + 去重。"""
    from agent_py_agent.cli.chat_parts.tui_threading import _consume_background_notices

    store_root = tmp_path / "conversations"
    thread_id = "thread-tui-1"
    notices_dir = store_root / "notices"
    notices_dir.mkdir(parents=True)
    (notices_dir / f"{thread_id}.notices.jsonl").write_text(
        json.dumps(
            {
                "schema_version": "background_notice.v2",
                "display_kind": "assistant_response",
                "thread_id": thread_id,
                "reason": "subagent_runner_finished",
                "content": "子代理已完成，后台自动汇总完成。",
                "summary": "子代理已完成，后台自动汇总完成。",
                "created_at": 100.0,
                "task_progress_items": [
                    {"id": "qa", "title": "整合测试", "status": "done"}
                ],
                "task_progress_generation_id": "generation-final",
                "task_progress_plan_revision": 4,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    published: list[str] = []
    progress_snapshots: list[tuple[list[dict[str, object]], str, int]] = []

    class _Store:
        root = store_root

        def resolve_thread_report(self, **kwargs):
            return SimpleNamespace(thread_id=thread_id), None

    class _Runtime:
        def publish_task_progress_snapshot(
            self,
            items,
            *,
            generation_id="",
            plan_revision=0,
        ):
            progress_snapshots.append(
                (list(items), str(generation_id), int(plan_revision))
            )

        def publish_background_response(self, text, *, thread_id=""):
            published.append(text)

    class _Agent:
        conversation_store = _Store()

    seen: set[float] = set()
    _consume_background_notices(_Agent(), "session-1", _Runtime(), [None], seen)
    assert len(published) == 1
    assert published[0] == "子代理已完成，后台自动汇总完成。"
    assert progress_snapshots == [
        (
            [{"id": "qa", "title": "整合测试", "status": "done"}],
            "generation-final",
            4,
        )
    ]
    # 第二次消费同文件：已 seen，不重复发布
    _consume_background_notices(_Agent(), "session-1", _Runtime(), [None], seen)
    assert len(published) == 1


def test_tui_skips_when_no_thread_or_no_session(tmp_path: Path) -> None:
    """无会话/无线程/无 notices 时静默跳过。"""
    from agent_py_agent.cli.chat_parts.tui_threading import _consume_background_notices

    class _Store:
        root = tmp_path / "conversations"

        def resolve_thread_report(self, **kwargs):
            return None, None

    class _Runtime:
        def publish_background_notice(self, text, *, thread_id=""):
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
        def request_background_notices(self, session_id, *, after, event_after):
            fetched.append(
                {
                    "session_id": session_id,
                    "after": after,
                    "event_after": event_after,
                }
            )
            return {
                "ok": True,
                "cursor": 200.0,
                "transcript_events": [],
                "event_cursor": event_after,
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
                        "schema_version": "background_notice.v2",
                        "display_kind": "assistant_response",
                        "thread_id": "thread-http-1",
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

        def publish_background_response(self, text, *, thread_id=""):
            published.append(text)

    seen: set[float] = set()
    assert _consume_background_notices(
        _Agent(), "session-http", _Runtime(), [None], seen
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
        _Agent(), "session-http", _Runtime(), [None], seen
    )
    assert fetched[1]["after"] == 150.0


def test_tui_notice_transport_failure_preserves_projection_and_reports_failure() -> None:
    """HTTP 断线不清空旧活动投影，并把失败交给监视线程退避。"""
    from agent_py_agent.cli.chat_parts.tui_threading import _consume_background_notices

    class _Agent:
        def request_background_notices(self, session_id, *, after, event_after):
            return {
                "ok": False,
                "notices": [],
                "cursor": after,
                "transcript_events": [],
                "event_cursor": event_after,
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
    sink.complete_active_turn_input(("agent-steer-1", "agent-steer-2"))

    assert len(rows) == 1
    assert rows[0]["kind"] == "active_turn_input_consumed"
    assert rows[0]["phase"] == "completed"
    assert rows[0]["block_id"] == f"{request_id}:active-input:1"
    assert rows[0]["payload"] == {
        "client_message_ids": ["agent-steer-1", "agent-steer-2"]
    }


def test_gateway_notice_page_transports_background_event_cursor(tmp_path: Path) -> None:
    """独立 event_after 游标不会与最终通知 created_at 游标互相覆盖。"""
    from agent_py_agent.agent.conversation.background_transcript import (
        BackgroundTranscriptSink,
    )
    from agent_py_agent.agent.conversation.store import ConversationStore
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
        scope=SimpleNamespace(conversation_id="session-rich-cursor"),
        after=0.0,
        event_after=0,
    )
    assert first["ok"] is True
    assert [row["kind"] for row in first["transcript_events"]] == [
        "assistant_completed",
        "tool_started",
    ]
    assert first["event_cursor"] == 2
    assert first["cursor"] == 0.0

    second = read_gateway_client_notices(
        agent,
        scope=SimpleNamespace(conversation_id="session-rich-cursor"),
        after=0.0,
        event_after=first["event_cursor"],
    )
    assert second["transcript_events"] == []
    assert second["event_cursor"] == 2


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
        ):
            del session_id, after
            fetched.append(event_after)
            page = read_background_transcript_events(
                source,
                thread_id="thread-http-rich",
                after=event_after,
            )
            return {
                "ok": True,
                "cursor": 0.0,
                "notices": [],
                "transcript_events": page["events"],
                "event_cursor": page["cursor"],
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
    scope = SimpleNamespace(conversation_id="session-active")

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


def test_runtime_keeps_multiple_background_notices_as_distinct_blocks() -> None:
    """同一 thread 连续后台回复不能因复用稳定 block id 而丢掉后者。"""
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime

    runtime = TuiRuntime("session-notices")
    runtime.publish_background_response("第一条", thread_id="thread-1")
    runtime.publish_background_response("第二条", thread_id="thread-1")

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
    assert "ctx 12.3k · compact 1 · 重试 1 次" in rendered
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
