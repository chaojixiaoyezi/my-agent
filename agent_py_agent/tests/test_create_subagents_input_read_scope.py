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

import pytest

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
    # 父项目目录与墙外工作树一样，运行时也报 PATH_OWNER_SCOPE_BLOCKED + authorization（2026-09-28 修：
    # 原先这里会退化成"路径笔误"的 TOOL_INVALID_ARGUMENTS —— 根内路径永远不该被判成拼写错误）。
    assert actually_blocked == predicted_blocked


# 函数用途: 取出一次 read_file 失败回执的结构化字段，供真墙/真拼写两类用例共用。
# 账本条目只有 tool/call_id/error_code/target/message 五个字段（结构化回执本来就没有 failure_stage，
# 它只出现在渲染后的回执文本头里），所以断言一律只看 error_code / target / suspected_path_typo。
def _read_failure(agent, task_id: str) -> dict:
    failures = agent.subagents.load(task_id).attributes["tool_failure_ledger"]["failures"]
    return next(item for item in failures if item["tool"] == "read_file")


def test_blocked_path_inside_a_known_root_reports_the_permission_code(tmp_path, monkeypatch):
    """根内路径被 owner 墙拒绝时，必须报权限码，不能退化成"路径拼写错误"（TOOL_INVALID_ARGUMENTS）。

    这是本次改动的核心场景。夹具要点（两面都必须成立，否则用例区分不了新旧实现）：
      · 目标落在某个 workspace_root（outside）之下 → 命中新加的"根内不报拼写"判断；
      · 同时拼写建议**确实指向另一个路径**（projects/alpha/x.md）→ 旧实现会真的抛参数错误。
    用工具层直接构造：子代理端到端造不出这个组合（未授权路径一律先被墙拦）。
    """
    from agent_py_agent.agent.path_recovery_hints import suggest_workspace_typo_target
    from agent_py_agent.agent.tooling._filesystem_read import (
        FileSystemTool,
        PathAccessError,
        filesystem_access_options,
    )

    owner = tmp_path / "owner"
    owner.mkdir()
    project_root = tmp_path / "projects" / "alpha"
    project_root.mkdir(parents=True)
    outside = tmp_path / "beta"
    outside.mkdir()
    target = outside / "alpha" / "x.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("墙外资料", encoding="utf-8")

    tool = FileSystemTool(
        outside,
        workspace_roots=[outside, owner, project_root],
        access_options=filesystem_access_options(owner_scope_root=str(owner)),
    )
    # 前提一：目标确实在某个 workspace_root 之下，且被 owner 墙拒绝。
    assert tool.check_path_access(target).allowed is False
    assert any(target.is_relative_to(root) for root in tool.workspace_roots)
    # 前提二：建议是另一个路径（所以旧实现会把它当拼写提示，真的报成参数错误）。
    suggestion = suggest_workspace_typo_target(str(target), tool.workspace_roots)
    assert suggestion == str(project_root / "x.md")

    with pytest.raises(PathAccessError) as excinfo:
        tool.resolve_path(str(target))
    assert excinfo.value.access_code == _BLOCKED


def test_path_typo_outside_every_known_root_keeps_the_spelling_hint(tmp_path, monkeypatch):
    """真拼写：前缀写错、建议目标确实不同，才保留拼写提示 + 参数错误。

    端到端这一层现在造不出"能到达拼写分支"的子代理环境（子代理的 workspace_roots 只认已授权写根，
    未授权路径一律先被 owner 墙拦成 PATH_OWNER_SCOPE_BLOCKED）。因此这里钉的是
    resolve_path 里拼写分支本身的语义（唯一能真实执行到它的入口）。
    """
    from agent_py_agent.agent.path_recovery_hints import suggest_workspace_typo_target
    from agent_py_agent.agent.tooling._filesystem_read import _workspace_typo_error

    root = (tmp_path / "project").resolve()
    root.mkdir(parents=True, exist_ok=True)
    typo = tmp_path / "old" / root.name / "README.md"
    # 路径里含根名但层级错位：建议目标确实不同于原路径 → 有纠正价值，保留提示。
    assert suggest_workspace_typo_target(str(typo), [root]) == str(root / "README.md")
    hint = _workspace_typo_error(str(typo), root, [root])
    assert "suspected_path_typo=true" in hint
    assert str(typo) in hint and str(root / "README.md") in hint


def test_uncorrectable_suggestion_falls_back_to_the_permission_code(tmp_path, monkeypatch):
    """建议路径等于请求路径时没有纠正价值：入口不再给拼写提示，按真实权限结论上报权限码。"""
    from agent_py_agent.agent.path_recovery_hints import suggest_workspace_typo_target
    from agent_py_agent.agent.tooling._filesystem_read import _workspace_typo_error

    root = (tmp_path / "project").resolve()
    inside = root / "notes.md"
    # 路径本身就在这个根之下：旧实现给出的"建议"就是它自己，只会诱导调用方原地重试。
    assert suggest_workspace_typo_target(str(inside), [root]) == ""
    # 建议为空 ⇒ 拼写分支不成立；resolve_path 只能落到 PathAccessError（权限码），不再退化成参数错误。
    assert _workspace_typo_error(str(inside), root, [root]) == ""

    # 端到端：一个既不在 owner 墙内、也拿不到可纠正建议的路径，回执必须是权限码且不含拼写提示。
    agent, _owner_home = _agent(tmp_path, monkeypatch)
    unreachable = tmp_path / "elsewhere" / "notes.md"
    task = agent.subagents.create_run(goal="读取墙外文件", thought="", plan=["read_file"], allowed_tools=["read_file"])

    agent.backend = _ReadEachPathBackend([str(unreachable)])
    agent.run_subagent(task.id, dry_run=False, probe=False)

    failure = _read_failure(agent, task.id)
    assert "suspected_path_typo" not in str(failure)
    assert failure["error_code"] == _BLOCKED


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
