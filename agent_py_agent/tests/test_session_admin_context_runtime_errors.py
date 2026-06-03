from __future__ import annotations

from pathlib import Path


class _MockConfig:
    def __init__(self, session_workspace: Path, user_id: str = "admin") -> None:
        self.session_workspace = str(session_workspace)
        self.user_id = user_id


class _BrokenRegistry:
    def __init__(self, store: object) -> None:
        self._store = store

    def query_tasks(self, **kwargs: object) -> list[dict]:
        raise ValueError("task registry json is broken")


def _patch_broken_task_registry(monkeypatch) -> object:
    import agent_py_agent.agent.task_registry as task_registry_module

    monkeypatch.setattr(task_registry_module, "TaskRegistry", _BrokenRegistry)
    return object()


def test_context_sync_reports_task_registry_failure(tmp_path: Path, monkeypatch) -> None:
    from agent_py_agent.agent.session.context_sync import (
        SessionContextSync,
        format_context_for_channel,
    )
    from agent_py_agent.agent.session.cross_channel import CrossChannelSession

    cc = CrossChannelSession(_MockConfig(tmp_path / "sessions"))
    cc.bind_session("sess_123", "chat", "admin")
    store = _patch_broken_task_registry(monkeypatch)

    context = SessionContextSync(cc, task_registry_store=store).sync_to_channel(
        "sess_123",
        "feishu",
    )

    assert context["tasks"] == []
    assert context["load_errors"]
    assert context["load_errors"][0]["context"] == "session.context_sync.tasks"
    assert "task registry json is broken" in context["load_errors"][0]["message"]

    formatted = format_context_for_channel(context, "feishu")
    assert "读取警告" in formatted
    assert "session.context_sync.tasks" in formatted


def test_admin_get_all_tasks_report_preserves_registry_error(tmp_path: Path, monkeypatch) -> None:
    from agent_py_agent.agent.session.admin_query import AdminCrossChannelQuery
    from agent_py_agent.agent.session.cross_channel import CrossChannelSession
    from agent_py_agent.agent.session.manager import SessionManager

    config = _MockConfig(tmp_path / "sessions")
    store = _patch_broken_task_registry(monkeypatch)
    query = AdminCrossChannelQuery(CrossChannelSession(config), SessionManager(config), store)

    tasks, load_errors = query.get_all_tasks_report("admin")

    assert tasks == []
    assert load_errors
    assert load_errors[0]["context"] == "session.admin.tasks"


def test_admin_recent_activity_report_keeps_good_items_and_task_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.session.admin_query import AdminCrossChannelQuery
    from agent_py_agent.agent.session.cross_channel import CrossChannelSession
    from agent_py_agent.agent.session.manager import SessionManager

    config = _MockConfig(tmp_path / "sessions")
    cc = CrossChannelSession(config)
    sm = SessionManager(config)
    session = sm.create_session("admin", "chat")
    cc.bind_session(session.session_id, "chat", "admin")
    store = _patch_broken_task_registry(monkeypatch)
    query = AdminCrossChannelQuery(cc, sm, store)

    timeline, load_errors = query.get_recent_activity_report("admin", limit=10)

    assert any(item["type"] == "session_update" for item in timeline)
    assert load_errors
    assert load_errors[0]["context"] == "session.admin.recent_activity.tasks"


def test_admin_channel_summary_reports_task_registry_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.session.admin_query import AdminCrossChannelQuery
    from agent_py_agent.agent.session.cross_channel import CrossChannelSession
    from agent_py_agent.agent.session.manager import SessionManager

    config = _MockConfig(tmp_path / "sessions")
    cc = CrossChannelSession(config)
    sm = SessionManager(config)
    session = sm.create_session("admin", "chat")
    cc.bind_session(session.session_id, "chat", "admin")
    store = _patch_broken_task_registry(monkeypatch)
    query = AdminCrossChannelQuery(cc, sm, store)

    summary = query.get_channel_summary("admin", "chat")

    assert summary["active_tasks"] == []
    assert summary["load_errors"]
    assert summary["load_errors"][0]["context"] == "session.admin.channel_summary.tasks"
