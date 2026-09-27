"""创建子代理时对显式输入路径做可见性预检（2026-09-27 真实缺口 C）。

真机：Full Access 管理员父代理派 4 个只读子代理读 owner home 外的 git 工作树，子代理没有 Full Access，
读取全部 PATH_OWNER_SCOPE_BLOCKED。本文件钉住：
1. 只检查调用方显式给出的结构化输入（input_refs / input_files / required_read_paths /
   context_manifest.required_read_paths），goal 正文里的路径一律不参与；
2. 可见性用子代理工具实际使用的同一条判定链，读不到就整批拒绝（not_started），并给出大白话修复提示；
3. 预检结论与子代理真实 read_file 的裁决一致。
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.agent_core.orchestration.create_read_scope import (
    INPUT_PATH_NOT_VISIBLE_ERROR_CODE,
    INPUT_PATH_NOT_VISIBLE_HINT,
    child_read_scope,
)
from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.runtime_context import (
    restore_current_subagent_context,
    set_current_subagent_context,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests.test_agent.backends import _TestNativeBackend

_BLOCKED = "PATH_OWNER_SCOPE_BLOCKED"


def _agent(tmp_path: Path, monkeypatch, *, full_access: bool = True) -> tuple[SimpleAgent, Path]:
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    extra = {"access_mode": "full-access"} if full_access else {}
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs", **extra), project)
    owner_home = Path(str(agent.home_paths.owner_home_dir)).resolve()
    owner_home.mkdir(parents=True, exist_ok=True)
    return agent, owner_home


def _outside_worktree(tmp_path: Path) -> Path:
    worktree = tmp_path / "my-agent-worktrees" / "my-agent-self" / "docs"
    worktree.mkdir(parents=True, exist_ok=True)
    target = worktree / "notes.md"
    target.write_text("墙外资料", encoding="utf-8")
    return target


def _create(agent: SimpleAgent, params: dict):
    return CreateSubagentsTool(agent).execute({**params, "defer_start": True})


def test_full_access_parent_cannot_hand_child_an_outside_worktree(tmp_path, monkeypatch):
    agent, _owner_home = _agent(tmp_path, monkeypatch)
    outside = _outside_worktree(tmp_path)

    result = _create(agent, {"goal": "只读调研 my-agent-self", "input_refs": [str(outside)]})

    assert result.ok is False
    assert result.reported_error_code == INPUT_PATH_NOT_VISIBLE_ERROR_CODE
    assert result.effect_outcome == "not_started"
    payload = json.loads(result.output)
    assert payload["error"].startswith(INPUT_PATH_NOT_VISIBLE_HINT)
    assert "本批没有创建任何子代理" in payload["error"]
    child = payload["invisible_inputs"][0]
    assert child["index"] == 0
    assert child["invisible_refs"] == [
        {"ref": str(outside), "resolved_path": str(outside.resolve()), "reason_code": _BLOCKED}
    ]
    assert str(Path(str(agent.home_paths.owner_home_dir)).resolve()) in child["visible_roots"]
    assert payload["next_action"]["retry_tool"] == "create_subagents"
    assert agent.subagents.list_runs() == []


def test_inputs_inside_workspace_or_shared_are_created_normally(tmp_path, monkeypatch):
    agent, owner_home = _agent(tmp_path, monkeypatch)
    inside = owner_home / "projects" / "spec.md"
    shared = owner_home.parents[2] / "shared" / "skills" / "guide.md"

    result = _create(
        agent,
        {
            "goal": "阅读工作区里的规格",
            "input_refs": [str(inside), "notes/relative.md"],
            "required_read_paths": [str(shared)],
            "context_manifest": {"required_read_paths": [str(inside)]},
        },
    )

    assert result.ok is True, result.output
    assert len(agent.subagents.list_runs()) == 1


def test_batch_with_one_invisible_item_creates_nothing(tmp_path, monkeypatch):
    agent, owner_home = _agent(tmp_path, monkeypatch)
    outside = _outside_worktree(tmp_path)
    other_owner = owner_home.parent.parent / "feishu" / "ou_other" / "private.md"

    result = _create(
        agent,
        {
            "items": [
                {"goal": "读工作区", "input_refs": [str(owner_home / "a.md")]},
                {"goal": "读墙外工作树", "input_refs": [str(outside)]},
                {"goal": "读别人的家", "input_files": [str(other_owner)]},
                {"goal": "清单里要求读墙外", "context_manifest": {"required_read_paths": [str(outside)]}},
            ]
        },
    )

    assert result.ok is False
    assert result.reported_error_code == INPUT_PATH_NOT_VISIBLE_ERROR_CODE
    children = json.loads(result.output)["invisible_inputs"]
    assert [child["index"] for child in children] == [1, 2, 3]
    assert children[0]["invisible_refs"][0]["reason_code"] == _BLOCKED
    assert children[1]["invisible_refs"][0]["reason_code"] == "PATH_CROSS_OWNER_BLOCKED"
    assert children[2]["invisible_refs"][0]["ref"] == str(outside)
    assert agent.subagents.list_runs() == []


def test_paths_only_mentioned_in_goal_text_are_never_prechecked(tmp_path, monkeypatch):
    agent, _owner_home = _agent(tmp_path, monkeypatch)
    outside = _outside_worktree(tmp_path)
    goal = f"请阅读 {outside} 并总结"

    single = _create(agent, {"goal": goal})
    batch = _create(agent, {"items": [{"goal": goal}, {"goal": f"对照 {outside} 写结论"}]})

    assert single.ok is True, single.output
    assert batch.ok is True, batch.output
    assert len(agent.subagents.list_runs()) == 3


def test_nested_child_creation_uses_the_same_precheck(tmp_path, monkeypatch):
    agent, owner_home = _agent(tmp_path, monkeypatch, full_access=False)
    coordinator = agent.subagents.create_run(goal="协调调研", thought="", plan=[], role="coordinator")
    outside = _outside_worktree(tmp_path)
    previous = set_current_subagent_context(agent, run_id=coordinator.id)
    try:
        rejected = _create(agent, {"goal": "孙代理读墙外", "input_refs": [str(outside)]})
        accepted = _create(agent, {"goal": "孙代理读工作区", "input_refs": [str(owner_home / "b.md")]})
    finally:
        restore_current_subagent_context(agent, previous)

    assert rejected.ok is False
    assert rejected.reported_error_code == INPUT_PATH_NOT_VISIBLE_ERROR_CODE
    assert json.loads(rejected.output)["invisible_inputs"][0]["invisible_refs"][0]["reason_code"] == _BLOCKED
    assert accepted.ok is True, accepted.output
    assert [task.parent_id for task in agent.subagents.list_runs() if task.id != coordinator.id] == [coordinator.id]


class _ReadEachPathBackend(_TestNativeBackend):
    """测试用后端：逐个路径调用 read_file，读完后自然结束。"""

    name = "read_each_path_backend"

    def __init__(self, paths: list[str]):
        self.paths = list(paths)
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        self.calls += 1
        if self.calls > len(self.paths):
            return ModelResponse(text="已读取完毕。", backend=self.name)
        return ModelResponse(
            text="",
            backend=self.name,
            tool_use_blocks=[
                {"id": f"read-{self.calls}", "name": "read_file", "input": {"path": self.paths[self.calls - 1]}}
            ],
        )


def test_precheck_matches_the_childs_real_read_file_decision(tmp_path, monkeypatch):
    agent, owner_home = _agent(tmp_path, monkeypatch)
    outside = _outside_worktree(tmp_path)
    inside = owner_home / "projects" / "spec.md"
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_text("工作区资料", encoding="utf-8")
    project_file = tmp_path / "project" / "README.md"
    project_file.write_text("父代理项目", encoding="utf-8")
    paths = [str(inside), str(outside), str(project_file)]
    task = agent.subagents.create_run(goal="逐个读取资料", thought="", plan=["read_file"], allowed_tools=["read_file"])
    scope = child_read_scope(agent, _params_like(task))
    predicted_blocked = {path for path in paths if not scope.check(path)[1].allowed}

    agent.backend = _ReadEachPathBackend(paths)
    agent.run_subagent(task.id, dry_run=False, probe=False)

    failures = agent.subagents.load(task.id).attributes["tool_failure_ledger"]["failures"]
    actually_blocked = {item["target"] for item in failures if item["tool"] == "read_file"}
    assert predicted_blocked == {str(outside), str(project_file)}
    # 预检"看不到"⇔ 子代理真实 read_file 失败；工作区内的文件两边都可读。
    # 注：父项目目录是子代理的网关根，路径门放行后由 handler 按 owner 墙拒绝，运行时报码是路径笔误提示的
    # TOOL_INVALID_ARGUMENTS 而非 PATH_OWNER_SCOPE_BLOCKED；预检回执给出的是底层裁决码。
    assert actually_blocked == predicted_blocked


# 函数用途: 用已创建子代理的真实创建字段重建一份创建参数，保证预检与该子代理的运行边界同源。
def _params_like(task):
    from agent_py_agent.agent.subagents.services.base import CreateRunParams

    return CreateRunParams(
        goal=task.goal,
        thought=task.thought,
        plan=list(task.plan),
        role=task.role,
        allowed_tools=list(task.allowed_tools),
        extra_write_roots=[root for root in task.allowed_write_roots if root != task.task_dir],
        attributes=dict(task.attributes),
    )


def test_declared_parent_workspace_is_granted_to_child_and_precheck_agrees(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.authority import (
        CONVERSATION_EXECUTION_CWD_ATTR,
        CONVERSATION_RUNTIME_WORKSPACE_ROOTS_ATTR,
    )

    agent, _owner_home = _agent(tmp_path, monkeypatch)
    project = tmp_path / "project"
    source = project / "src" / "main.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("print('ok')", encoding="utf-8")
    outside = _outside_worktree(tmp_path)
    # 宿主为本轮会话下发的结构化工作目录：子代理按原规则继承为墙外已授权根。
    agent._current_run_params = SimpleNamespace(
        task_attributes={
            CONVERSATION_EXECUTION_CWD_ATTR: str(project),
            CONVERSATION_RUNTIME_WORKSPACE_ROOTS_ATTR: [str(project)],
        }
    )

    rejected = _create(agent, {"goal": "读项目和墙外", "input_refs": [str(source), str(outside)]})
    created = _create(agent, {"goal": "读项目", "input_refs": [str(source), "src/main.py"]})

    assert rejected.ok is False
    refs = json.loads(rejected.output)["invisible_inputs"][0]["invisible_refs"]
    assert [item["ref"] for item in refs] == [str(outside)]
    assert created.ok is True, created.output
    task = agent.subagents.list_runs()[-1]
    agent._current_run_params = None
    agent.backend = _ReadEachPathBackend([str(source), "src/main.py", str(outside)])
    agent.run_subagent(task.id, dry_run=False, probe=False)
    failures = agent.subagents.load(task.id).attributes["tool_failure_ledger"]["failures"]
    assert [(item["target"], item["error_code"]) for item in failures] == [(str(outside), _BLOCKED)]
