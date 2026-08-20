"""S-BG1 回归：后台主代理轮完成 → notices 文件 → TUI 显示。

真机实锤（s2-fast）：子代理完成后父代理在后台自动续跑汇总，但 TUI 无事件
驱动不刷新——用户看不到"后台已自动汇总"。修复：
1. gateway 在后台轮 report 产生后写 conversations/notices/{thread_id}.notices.jsonl
2. TUI 监视线程周期读取并 publish_background_notice（system_message 显示）
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
    assert rows[0]["schema_version"] == "background_notice.v1"


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
    """TUI 监视：notices 新行 → publish_background_notice 被调 + 去重。"""
    from agent_py_agent.cli.chat_parts.tui_threading import _consume_background_notices

    store_root = tmp_path / "conversations"
    thread_id = "thread-tui-1"
    notices_dir = store_root / "notices"
    notices_dir.mkdir(parents=True)
    (notices_dir / f"{thread_id}.notices.jsonl").write_text(
        json.dumps(
            {
                "schema_version": "background_notice.v1",
                "thread_id": thread_id,
                "reason": "subagent_runner_finished",
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
        def publish_background_notice(self, text, *, thread_id=""):
            published.append(text)

    class _Agent:
        conversation_store = _Store()

    seen: set[float] = set()
    _consume_background_notices(_Agent(), "session-1", _Runtime(), [None], seen)
    assert len(published) == 1
    assert "后台自动完成" in published[0]
    assert "子代理已完成" in published[0]
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
