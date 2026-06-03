"""Focused tests for background subagent dispatch helpers."""

from __future__ import annotations

from types import SimpleNamespace


def test_background_dispatch_project_root_is_repo_root():
    """后台 dispatch 子进程必须从仓库根启动，避免误导入旧安装版 CLI。"""
    import agent_py_agent.agent.agent_core.orchestration.background.dispatch as background_dispatch

    root = background_dispatch._project_root()

    assert (root / "agent_py_agent").is_dir()
    assert (root / "pyproject.toml").is_file()


def test_safe_agent_tree_error_is_structured(monkeypatch):
    """后台启动时树读取失败要返回结构化错误，而不是只给一条 warning。"""
    import agent_py_agent.agent.agent_core.orchestration.background.dispatch as background_dispatch

    def broken_tree(agent, params):
        del agent, params
        raise ValueError("broken tree index")

    monkeypatch.setattr(background_dispatch, "agent_tree_status_payload", broken_tree)

    payload = background_dispatch._safe_agent_tree(SimpleNamespace())

    assert payload["warnings"] == ["agent_tree_unavailable:ValueError"]
    error = payload["agent_tree_load_error"]
    assert error["context"] == "background_dispatch.agent_tree"
    assert error["category"] == "data_parse"
    assert "刷新代理树" in error["model_message"]


def test_start_background_dispatch_reports_mark_errors(monkeypatch):
    """后台启动状态写不进子代理账本时，父代理要能看到结构化错误。"""
    import agent_py_agent.agent.agent_core.orchestration.background.dispatch as background_dispatch

    agent = SimpleNamespace(
        config=SimpleNamespace(model_backend="minimax"),
        subagents=SimpleNamespace(load=lambda _run_id: (_ for _ in ()).throw(ValueError("state broken"))),
    )
    monkeypatch.setattr(
        background_dispatch,
        "_auto_start_dispatch_args",
        lambda _agent, _run_ids, background_launch_id="": (object(), object(), object()),
    )
    monkeypatch.setattr(
        background_dispatch,
        "_spawn_background_dispatch_process",
        lambda _agent, _request: SimpleNamespace(pid=12345),
    )
    monkeypatch.setattr(background_dispatch, "_safe_agent_tree", lambda _agent: {"schema_version": "tree.v1"})

    result = background_dispatch._start_background_dispatch(agent, ["child-broken"])

    assert result["status"] == "started"
    assert result["background_mark_errors"][0]["run_id"] == "child-broken"
    assert result["background_mark_errors"][0]["context"] == "background_dispatch.mark_start.load"
    assert result["background_mark_errors"][0]["category"] == "data_parse"


def test_start_background_dispatch_marks_channel_failure_on_immediate_process_exit(monkeypatch):
    """后台子进程一启动就退出时，任务不能继续伪装成 PLANNING。"""
    import agent_py_agent.agent.agent_core.orchestration.background.dispatch as background_dispatch

    task = SimpleNamespace(
        id="child-1",
        status="PLANNING",
        channel_status="OK",
        failure_type="",
        attributes={},
        result="",
        updated_at=0.0,
    )
    saved = {}

    class Manager:
        def load(self, run_id):
            assert run_id == "child-1"
            return task

        def save(self, next_task):
            saved["task"] = next_task

    agent = SimpleNamespace(
        config=SimpleNamespace(model_backend="minimax"),
        subagents=Manager(),
    )
    monkeypatch.setattr(
        background_dispatch,
        "_auto_start_dispatch_args",
        lambda _agent, _run_ids, background_launch_id="": (object(), object(), object()),
    )
    monkeypatch.setattr(
        background_dispatch,
        "_spawn_background_dispatch_process",
        lambda _agent, _request: SimpleNamespace(pid=12345, poll=lambda: 2),
    )
    monkeypatch.setattr(background_dispatch, "_safe_agent_tree", lambda _agent: {"schema_version": "tree.v1"})

    result = background_dispatch._start_background_dispatch(agent, ["child-1"])

    assert result["status"] == "failed"
    assert result["failure_type"] == "background_dispatch_startup"
    assert saved["task"].status == "CHANNEL_ERROR"
    assert saved["task"].channel_status == "BROKEN"
    assert saved["task"].failure_type == "background_dispatch_startup"
