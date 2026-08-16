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
