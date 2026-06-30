from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.subagent.expected_outputs_seed import (
    seed_declared_expected_outputs,
)
from agent_py_agent.agent.task_progress import read_task_progress, write_task_progress


class _FakeTask:
    def __init__(self, attrs: dict) -> None:
        self.attributes = attrs
        self.task_workspace_dir = ""
        self.output_dir = ""
        self.allowed_write_roots = []
        self.locked_files = []


def _agent(tmp_path, task):
    return SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=str(tmp_path)),
        subagents=SimpleNamespace(load=lambda run_id: task),
        root=str(tmp_path),
    )


def test_seed_from_parent_output_files(tmp_path):
    """父代理声明了 output_files → 播种成 expected_outputs,核对门即可生效。"""
    task = _FakeTask({"output_files": ["/home/u/data/ip_top10.txt"]})
    agent = _agent(tmp_path, task)

    assert seed_declared_expected_outputs(agent, "subagent-1") is True
    patterns = [e["pattern"] for e in read_task_progress(tmp_path, "subagent-1").get("expected_outputs") or []]
    assert "/home/u/data/ip_top10.txt" in patterns


def test_no_output_files_no_seed(tmp_path):
    """没声明 output_files 的任务零影响,不播种。"""
    agent = _agent(tmp_path, _FakeTask({}))
    assert seed_declared_expected_outputs(agent, "subagent-2") is False
    assert not (read_task_progress(tmp_path, "subagent-2").get("expected_outputs"))


def test_does_not_override_existing_declaration(tmp_path):
    """子代理自己/已有 expected_outputs 声明不被覆盖。"""
    write_task_progress(tmp_path, "subagent-3", {"expected_outputs": [{"pattern": "report.md", "min_count": 1}]})
    agent = _agent(tmp_path, _FakeTask({"output_files": ["/home/u/other.txt"]}))

    assert seed_declared_expected_outputs(agent, "subagent-3") is False
    patterns = [e["pattern"] for e in read_task_progress(tmp_path, "subagent-3").get("expected_outputs")]
    assert patterns == ["report.md"]


def test_filters_non_file_refs(tmp_path):
    """artifact:// / URL 这类非本地文件 ref 被过滤,不会播成永远缺失的对账项。"""
    agent = _agent(tmp_path, _FakeTask({"output_files": ["artifact://x/y", "https://example.com/z"]}))
    assert seed_declared_expected_outputs(agent, "subagent-4") is False
