"""S-BG1 回归：后台主代理轮完成 → notices 文件 → TUI 显示。

真机实锤（s2-fast）：子代理完成后父代理在后台自动续跑汇总，但 TUI 无事件
驱动不刷新——用户看不到"后台已自动汇总"。修复：
1. gateway 在后台轮 report 产生后写 conversations/notices/{thread_id}.notices.jsonl
2. TUI 监视线程周期读取并以普通 assistant 消息显示已提交的后台最终回复
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace


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
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    published: list[str] = []

    class _Store:
        root = store_root

        def resolve_thread_report(self, **kwargs):
            return SimpleNamespace(thread_id=thread_id), None

    class _Runtime:
        def publish_background_response(self, text, *, thread_id=""):
            published.append(text)

    class _Agent:
        conversation_store = _Store()

    seen: set[float] = set()
    _consume_background_notices(_Agent(), "session-1", _Runtime(), [None], seen)
    assert len(published) == 1
    assert published[0] == "子代理已完成，后台自动汇总完成。"
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
        def request_background_notices(self, session_id, *, after):
            fetched.append({"session_id": session_id, "after": after})
            return {
                "ok": True,
                "cursor": 200.0,
                "active_task_count": 2,
                "agent_activity": {
                    "schema_version": "conversation_agent_activity.v2",
                    "active_task_count": 2,
                    "active_task_projection_ok": True,
                    "subagents": [
                        {
                            "run_id": "child-1",
                            "name": "level-design",
                            "status": "RUNNING",
                            "activity": "正在生成关卡",
                            "attempts": 1,
                        }
                    ],
                    "hidden_subagent_count": 0,
                    "subagent_projection_ok": True,
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
            *,
            main_activity,
            subagents,
            hidden_subagent_count,
            projection_ok,
        ):
            published.append(
                f"active:{count}:{subagents[0]['run_id']}:{hidden_subagent_count}:{projection_ok}:{bool(main_activity)}"
            )
            return True

        def publish_background_response(self, text, *, thread_id=""):
            published.append(text)

    seen: set[float] = set()
    _consume_background_notices(_Agent(), "session-http", _Runtime(), [None], seen)
    assert len(fetched) == 1
    assert fetched[0]["after"] == 0.0
    assert len(published) == 2
    assert published[0] == "active:2:child-1:0:True:False"
    assert published[1] == "HTTP 后台完成通知测试。"
    # 游标推进后不重复
    _consume_background_notices(_Agent(), "session-http", _Runtime(), [None], seen)
    assert fetched[1]["after"] == 150.0


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
            "activity": "正在使用 write_file",
            "attempts": 2,
            "token_count": 12345,
            "compact_count": 1,
            "created_at": 100.0,
            "ended_at": 0.0,
        },
        {
            "run_id": "child-levels",
            "name": "level-design",
            "role": "worker",
            "status": "DONE",
            "activity": "模型已生成回复",
            "attempts": 1,
            "token_count": 6789,
            "compact_count": 0,
            "created_at": 100.0,
            "ended_at": 145.0,
        },
    ]
    assert runtime.update_background_activity(1, subagents=children) is True
    assert runtime.update_background_activity(1, subagents=children) is False
    snapshot = runtime.store.snapshot()
    active = [block for block in snapshot.active_blocks if block.role == "background"]
    assert len(active) == 1
    assert active[0].metadata["active_task_count"] == 1
    assert len(active[0].metadata["subagents"]) == 2
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
    assert "game-engine · 运行中 · 正在使用 write_file · 0:50" in rendered
    assert "↓ 12.3k tokens · compact 1 · 重试 1 次" in rendered
    assert "level-design · 已完成 · 0:45 · ↓ 6.8k tokens · compact 0" in rendered
    assert "模型已生成回复" not in rendered
    assert "尝试 1" not in rendered
    assert "/stop to interrupt" in fragments_text(frame.footer)

    changed_children = [dict(children[0], activity="正在使用 run_command"), children[1]]
    assert runtime.update_background_activity(1, subagents=changed_children) is True

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
    assert not any(
        block.role == "background" for block in runtime.store.snapshot().active_blocks
    )
    assert runtime.needs_periodic_refresh() is False
