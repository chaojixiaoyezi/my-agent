"""CLI 手动续跑（EXEC-33）focused 测试。

背景：非完成收口（UNKNOWN/blocked 等）后 CLI 退出 RC≠0，任务没有官方
恢复入口。
run --resume 从 task.yaml + run_workspace.json 持久事实源恢复同一
task/run/thread 链继续。本文件覆盖事实源加载与续跑调用契约。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.cli.resume_loop import _load_task_facts, run_manual_resume


class _FakeStore:
    def __init__(self):
        self.appends = []

    def get_or_create_thread(self, spec):
        return SimpleNamespace(thread_id="thread-" + str(spec.get("channel_conversation_id") or "t"))

    def append_message_once(self, spec, dedupe_key=""):
        self.appends.append(spec)
        return SimpleNamespace(
            message_id="m-" + str(len(self.appends)),
            thread_id=spec.get("thread_id", ""),
            metadata=dict(spec.get("metadata") or {}),
        )


class _FakeAgent:
    def __init__(self, tmp_path, workspace_root=""):
        self.home_paths = SimpleNamespace(
            owner_id="owner-1", owner_home_dir=str(tmp_path), root=str(tmp_path)
        )
        self.conversation_store = _FakeStore()
        self.subagents = SimpleNamespace(runtime_db=None)
        self.config = SimpleNamespace(cli_resume_max_rounds=8)
        self._current_run_task_workspace = workspace_root
        self.run_calls = []
        self.last_result = None

    def run(self, prompt, *, params, save=False, source="", delivery_contract=None,
            on_chunk=None, resume_context=False):
        self.run_calls.append(dict(prompt=prompt, params=params, save=save, source=source))
        return self.last_result


def _ok_result():
    return SimpleNamespace(runtime_status="ok", response="ok")


def _make_task_facts(tmp_path: Path) -> tuple[str, str]:
    """按真机目录结构创建 task.yaml + run_workspace.json。"""
    task_root = tmp_path / "tasks" / "2026-08-16" / "好-我有一个任务"
    work = task_root / "work"
    work.mkdir(parents=True)
    meta = {
        "task_id": "run-abc123",
        "run_id": "run-abc123",
        "request_id": "run-abc123",
        "task_title": "好-我有一个任务",
        "owner_id": "local/main",
        "owner_home": str(tmp_path),
        "source": "cli_run",
    }
    yaml_text = "\n".join(f'{k}: {json.dumps(v, ensure_ascii=False)}' for k, v in meta.items())
    (work / "task.yaml").write_text(yaml_text + "\n", encoding="utf-8")
    ws = {
        "task_root": str(task_root),
        "output_dir": str(task_root / "output"),
        "work_dir": str(work),
        "run_id": "run-abc123",
        "schema_version": "run_workspace.v1",
    }
    (work / "run_workspace.json").write_text(json.dumps(ws, ensure_ascii=False), encoding="utf-8")
    return str(task_root), "run-abc123"


def test_load_task_facts_from_dir(tmp_path):
    task_root, task_id = _make_task_facts(tmp_path)
    agent = _FakeAgent(tmp_path)
    facts = _load_task_facts(agent, task_root)
    assert facts is not None
    assert facts["task_id"] == task_id
    assert facts["run_workspace"]["task_root"] == task_root
    assert facts["run_workspace"]["output_dir"] == str(Path(task_root) / "output")


def test_load_task_facts_by_id_scans_tasks_root(tmp_path):
    task_root, task_id = _make_task_facts(tmp_path)
    agent = _FakeAgent(tmp_path)
    facts = _load_task_facts(agent, task_id)
    assert facts is not None and facts["task_id"] == task_id


def test_load_task_facts_missing_returns_none(tmp_path):
    agent = _FakeAgent(tmp_path)
    assert _load_task_facts(agent, "run-does-not-exist") is None


def test_manual_resume_runs_continuation_with_workspace(tmp_path):
    task_root, task_id = _make_task_facts(tmp_path)
    agent = _FakeAgent(tmp_path, workspace_root=task_root)
    agent.last_result = _ok_result()
    outcome = run_manual_resume(agent, task_ref=task_root)
    assert outcome.status == "completed"
    # 续跑轮(seq=1) 且 base_params 带 run_workspace
    assert len(agent.run_calls) == 1
    call = agent.run_calls[0]
    assert call["params"].task_attributes["run_workspace"]["task_root"] == task_root


class _LinkStore(_FakeStore):
    """带 task link 状态的最小 store（EXEC-35b 测试）。"""

    def __init__(self, link_status="completed"):
        super().__init__()
        self._status = link_status
        self.updated = None

    def load_task_link(self, task_id):
        if self._status is None:
            return None
        return SimpleNamespace(task_id=task_id, status=self._status)

    def update_task_status(self, request):
        self.updated = request
        return SimpleNamespace(task_id=request.get("task_id"), status=request.get("status"))


def test_manual_resume_reactivates_terminal_task_link(tmp_path):
    """EXEC-35b: 终态(completed)任务 link 在 resume 时被迁移回 active。"""
    task_root, task_id = _make_task_facts(tmp_path)
    agent = _FakeAgent(tmp_path, workspace_root=task_root)
    agent.conversation_store = _LinkStore(link_status="completed")
    agent.last_result = _ok_result()
    outcome = run_manual_resume(agent, task_ref=task_root)
    assert outcome.status == "completed"
    assert agent.conversation_store.updated == {"task_id": task_id, "status": "active"}


def test_manual_resume_keeps_active_link_untouched(tmp_path):
    """active 任务 link 不需要迁移。"""
    task_root, _ = _make_task_facts(tmp_path)
    agent = _FakeAgent(tmp_path, workspace_root=task_root)
    agent.conversation_store = _LinkStore(link_status="active")
    agent.last_result = _ok_result()
    run_manual_resume(agent, task_ref=task_root)
    assert agent.conversation_store.updated is None


from agent_py_agent.agent.user_space.run_workspace import (
    EnsureRunWorkspaceRequest,
    _activate_task_state,
)


def _state_request(task_id="t1", run_id="r1") -> EnsureRunWorkspaceRequest:
    return EnsureRunWorkspaceRequest(
        home="/tmp", template="", task_name="t", user_prompt="p",
        request_id=run_id, run_id=run_id, task_id=task_id,
        owner_id="o", owner_home="/tmp", source="cli_run",
    )


def test_activate_task_state_resets_terminal_fields(tmp_path):
    """EXEC-35c: 同 task 重激活清掉 finished_at/runtime_reason(终态投影不残留)。"""
    p = tmp_path / "state.json"
    import json

    p.write_text(json.dumps({
        "task_id": "t1", "primary_run_id": "r1", "status": "DONE",
        "progress": 1.0, "finished_at": "2026-08-16T11:00:00+00:00",
        "runtime_reason": "OPERATION_INCOMPLETE", "artifact_refs": ["keep-me"],
    }))
    _activate_task_state(p, _state_request())
    d = json.loads(p.read_text())
    assert "finished_at" not in d
    assert "runtime_reason" not in d
    assert d["status"] == "RUNNING"
    assert d["artifact_refs"] == ["keep-me"]  # 历史字段不丢


class _PolicyStore(_LinkStore):
    """带进度 policy 的最小 store（EXEC-35d 测试）。"""

    def __init__(self):
        super().__init__(link_status=None)
        self.disabled = []

    def list_progress_policies(self, *, enabled_only=False):
        return [
            SimpleNamespace(policy_id="p1", task_id="run-abc123"),
            SimpleNamespace(policy_id="p2", task_id="other-task"),
        ]

    def disable_progress_policy(self, policy_id):
        self.disabled.append(policy_id)
        return True


def test_manual_resume_disables_stale_progress_policies(tmp_path):
    """EXEC-35d: resume 停用该任务残留的进度 policy(接管旧执行者)。"""
    task_root, task_id = _make_task_facts(tmp_path)
    agent = _FakeAgent(tmp_path, workspace_root=task_root)
    agent.conversation_store = _PolicyStore()
    agent.last_result = _ok_result()
    outcome = run_manual_resume(agent, task_ref=task_root)
    assert outcome.status == "completed"
    assert agent.conversation_store.disabled == ["p1"]  # 只停本任务的


def test_next_seq_counts_continuation_history(tmp_path):
    """EXEC-35e: 历史已有 N 个续跑消息 → 下一个 seq=N+1。"""
    from agent_py_agent.cli.resume_loop import _next_manual_resume_seq

    task_root, task_id = _make_task_facts(tmp_path)
    agent = _FakeAgent(tmp_path, workspace_root=task_root)

    class _SeqStore(_FakeStore):
        def resolve_thread(self, *, channel, channel_conversation_id, channel_user_id):
            return SimpleNamespace(thread_id="thread-x")

        def recent_messages(self, thread_id, *, limit=20):
            return [
                SimpleNamespace(metadata={"continuation_seq": 1}),
                SimpleNamespace(metadata={"continuation_seq": 2}),
                SimpleNamespace(metadata={}),
            ]

    agent.conversation_store = _SeqStore()
    facts = _load_task_facts(agent, task_root)
    assert _next_manual_resume_seq(agent, facts) == 3


from types import SimpleNamespace as _NS


def _timeout_record():
    from agent_py_agent.agent.agent_core._tool_loop_service import (
        _mark_unknown_outcome_halt,
    )

    params = _NS(
        repeated_failure_halt=None,
        unknown_outcome_halt=None,
        tool_context=[],
    )
    record = _NS(
        call=_NS(tool_name="run_command"),
        result=_NS(
            ok=False,
            effect_outcome="unknown",
            error_code="TOOL_TIMEOUT",
            reported_error_code="TOOL_TIMEOUT",
            handler_executed=True,
        ),
        params=params,
    )
    _mark_unknown_outcome_halt(_NS(), record)
    return params


def test_timeout_without_termination_evidence_preserves_unknown_halt():
    """错误码不是进程已退出的证据；真正 UNKNOWN 与重启恢复保持一致。"""
    params = _timeout_record()
    assert params.unknown_outcome_halt == ("run_command", "TOOL_TIMEOUT", "unknown", True)
    assert not any("进程组已清理" in line for line in params.tool_context)


def test_other_unknown_still_sets_halt():
    """非超时的真未知仍保持人工闸(EXEC-27 安全语义不变)。"""
    from agent_py_agent.agent.agent_core._tool_loop_service import (
        _mark_unknown_outcome_halt,
    )

    params = _NS(repeated_failure_halt=None, unknown_outcome_halt=None, tool_context=[])
    record = _NS(
        call=_NS(tool_name="run_command"),
        result=_NS(ok=False, effect_outcome="unknown", error_code="EXECUTOR_DIED",
                   reported_error_code="EXECUTOR_DIED", handler_executed=True),
        params=params,
    )
    _mark_unknown_outcome_halt(_NS(), record)
    assert params.unknown_outcome_halt is not None


def test_manual_resume_repairs_channel_binding(tmp_path):
    """EXEC-35f: resume 把 channel 绑定修正回任务 thread（单一权威）。"""
    task_root, task_id = _make_task_facts(tmp_path)
    agent = _FakeAgent(tmp_path, workspace_root=task_root)

    class _ThreadStore(_FakeStore):
        def __init__(self):
            super().__init__()
            self.bound = None

        def thread_for_task(self, task_id):
            return SimpleNamespace(thread_id="thread-authoritative")

        def bind_channel(self, request):
            self.bound = request
            return SimpleNamespace(thread_id=request["thread_id"])

    agent.conversation_store = _ThreadStore()
    agent.last_result = _ok_result()
    run_manual_resume(agent, task_ref=task_root)
    bound = agent.conversation_store.bound
    assert bound is not None
    assert bound["thread_id"] == "thread-authoritative"
    assert bound["channel_conversation_id"] == "run-abc123"


def test_repeated_unknown_does_not_release_protection(tmp_path):
    """重复收到 UNKNOWN 不能解锁；同一轮只追加一次说明，避免污染缓存前缀。"""
    from agent_py_agent.agent.agent_core._tool_loop_service import (
        _mark_unknown_outcome_halt,
    )

    params = _NS(repeated_failure_halt=None, unknown_outcome_halt=None, tool_context=[])
    agent = _NS()
    for i in range(1, 5):
        record = _NS(
            call=_NS(tool_name="run_command"),
            result=_NS(ok=False, effect_outcome="unknown", error_code="EXECUTOR_DIED",
                       reported_error_code="EXECUTOR_DIED", handler_executed=True),
            params=params,
        )
        _mark_unknown_outcome_halt(agent, record)
    assert params.unknown_outcome_halt == ("run_command", "EXECUTOR_DIED", "unknown", True)
    assert len(params.tool_context) == 1
    assert not any("不再拦截" in line for line in params.tool_context)


def test_auto_resume_requires_active_goal(tmp_path):
    """EXEC-39: 无 active goal 时不自动续跑(_auto_resume_authorized=False)。"""
    from agent_py_agent.cli.resume_loop import _auto_resume_authorized

    class _NoGoalStore(_FakeStore):
        def load_goal(self, thread_id, *, goal_id="", task_id="", name=""):
            return None

    agent = _FakeAgent(tmp_path)
    agent.conversation_store = _NoGoalStore()
    runner = _NS(ctx=_NS(root_thread_id="t", root_task_id="task-1"))
    assert _auto_resume_authorized(agent, runner) is False


def test_auto_resume_with_active_goal(tmp_path):
    """有 active goal 时授权自动续跑(任务运行时 goal-driver 同款)。"""
    from agent_py_agent.cli.resume_loop import _auto_resume_authorized

    class _GoalStore(_FakeStore):
        def load_goal(self, thread_id, *, goal_id="", task_id="", name=""):
            return SimpleNamespace(status="active")

    agent = _FakeAgent(tmp_path)
    agent.conversation_store = _GoalStore()
    runner = _NS(ctx=_NS(root_thread_id="t", root_task_id="task-1"))
    assert _auto_resume_authorized(agent, runner) is True
