"""家目录文件墙、普通目录语义与稳定整理指南的回归；真实 TUI 另行验收。"""

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.tool_runtime_ledger import write_boundary_with_runtime_ledger
from agent_py_agent.agent.prompting_parts.builder import PromptBuilder, ToolSections
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests.test_tool_gateway_contract import _execute, _registry


# LLM: 测试复用生产 Tool Gateway 和权限合并入口，不把可写结论伪造为 handler 结果。
# 函数用途: 创建同 owner 主/子代理文件场景；只在 pytest 临时目录写入测试资料。
def _home_case(tmp_path, scope):
    owner = tmp_path / "owners" / "alice"
    first = owner / "tasks" / "old-a"
    second = owner / "tasks" / "old-b"
    private = owner / "runs" / "today" / "runtime-1"
    for folder in (first, second, private / "work", private / "output"):
        folder.mkdir(parents=True, exist_ok=True)
    registry = _registry(owner)
    registry.owner_scope_root = str(owner)
    home = SimpleNamespace(owner_home_dir=owner, owner_runs_dir=owner / "runs")
    agent = SimpleNamespace(
        tools=registry, home_paths=home,
        subagents=SimpleNamespace(owner_scope_root=str(owner)),
        config=SimpleNamespace(access_mode="workspace-write"),
    )
    params = SimpleNamespace(
        context_scope=scope, source="gateway", run_id="",
        task_attributes={
            "conversation_thread_id": "thread-a",
            "conversation_task_id": "runtime-1",
            "conversation_execution_cwd": str(first),
            "conversation_runtime_workspace_roots": [str(first)],
            # 旧版本留下的重写元数据不能改变任何参数或产品路径。
            "conversation_rebase_from_task_root": str(second),
            "run_workspace": {"task_root": str(private)},
        },
        write_boundary={"execution_cwd": str(first), "allowed_write_roots": [str(first)]},
    )
    return registry, write_boundary_with_runtime_ledger(agent, params), owner, first, second, private


@pytest.mark.parametrize("scope", ["conversation", "task_local"])
@pytest.mark.parametrize("path_mode", ["absolute", "sibling-relative"])
def test_main_and_child_read_edit_old_work_without_switching_runtime(tmp_path, scope, path_mode):
    registry, boundary, owner, first, second, private = _home_case(tmp_path, scope)
    target = second / "report.md"
    content = f"Reference: {first}/code.py\n"
    path = str(target) if path_mode == "absolute" else "../old-b/report.md"
    written = _execute(registry, "write_file", {"path": path, "content": content}, write_boundary=boundary)
    assert written.ok, written.output
    assert target.read_text() == content
    read = _execute(registry, "read_file", {"path": path}, write_boundary=boundary)
    assert read.ok and content.strip() in read.output
    edited = _execute(registry, "edit_file", {
        "path": path, "old_string": "Reference:", "new_string": "Verified:",
    }, write_boundary=boundary)
    assert edited.ok, edited.output
    assert target.read_text() == content.replace("Reference:", "Verified:")
    assert boundary["allowed_write_roots"] == [str(owner.resolve())]
    assert boundary["execution_cwd"] == str(first)
    assert boundary["task_root"] == str(private)
    assert not (private / "report.md").exists()


@pytest.mark.parametrize("scope", ["conversation", "task_local"])
def test_home_scope_keeps_other_owner_and_host_state_protected(tmp_path, scope):
    registry, boundary, owner, first, second, private = _home_case(tmp_path, scope)
    other = tmp_path / "owners" / "bob"
    other.mkdir()
    secret = other / "private.md"
    secret.write_text("other owner content\n")
    (first / "outside-link").symlink_to(other, target_is_directory=True)
    for path in (str(secret), "../../../../owners/bob/private.md", "outside-link/private.md"):
        read = _execute(registry, "read_file", {"path": path}, write_boundary=boundary)
        write = _execute(registry, "write_file", {"path": path, "content": "changed"}, write_boundary=boundary)
        assert not read.ok and not write.ok
    assert secret.read_text() == "other owner content\n"
    control = private / "work" / "state.json"
    control.write_text('{"status":"active"}')
    denied = _execute(registry, "write_file", {"path": str(control), "content": "changed"}, write_boundary=boundary)
    assert not denied.ok
    assert control.read_text() == '{"status":"active"}'


@pytest.mark.parametrize("name", ["output", "work", "workspace", "tasks"])
def test_business_directory_names_are_not_magic_aliases(tmp_path, name):
    registry, boundary, _, first, _, private = _home_case(tmp_path, "conversation")
    path = f"{name}/file.txt"
    result = _execute(registry, "write_file", {"path": path, "content": "user file"}, write_boundary=boundary)
    assert result.ok, result.output
    assert (first / path).read_text() == "user file"
    assert not (private / path).exists()


def test_exact_worker_does_not_gain_whole_home(tmp_path):
    registry, boundary, owner, first, second, _ = _home_case(tmp_path, "task_local")
    params = SimpleNamespace(
        context_scope="task_local", run_id="", task_attributes={"run_workspace": {"task_root": str(first)}},
        write_boundary={"read_scope_mode": "exact", "allowed_write_roots": [str(first)]},
    )
    agent = SimpleNamespace(tools=registry, subagents=SimpleNamespace(owner_scope_root=str(owner)))
    exact = write_boundary_with_runtime_ledger(agent, params)
    assert exact["allowed_write_roots"] == [str(first.resolve())]
    denied = _execute(registry, "write_file", {"path": str(second / "no.txt"), "content": "no"}, write_boundary=exact)
    assert not denied.ok


def test_directory_guide_is_shared_cache_stable_and_can_be_disabled(tmp_path):
    home = SimpleNamespace(owner_home_dir=tmp_path / "alice", owner_kind="user")
    config = AgentConfig(prompt_files=[], workspace_task_path_template="tasks/{date}/{task_slug}")
    builder = PromptBuilder(config, tmp_path, home_paths=home)
    first = builder.build(user_prompt="整理季度材料", tools=ToolSections(native_tool_use=True))
    child = builder.build(user_prompt="继续上月的脚本", context_scope="task_local", tools=ToolSections(native_tool_use=True))
    for rendered in (first, child):
        assert "# 家目录与整理约定" in rendered
        assert "tasks/{date}/{task_slug}" in rendered
        assert "不用判断长期项目/短期任务状态" in rendered
        assert "主题或对象＋成果或动作" in rendered
        assert "不要截取用户原话的开头" in rendered
        assert "用户明确给了目录名则沿用" in rendered
        assert "不为美化名称搬动旧项目" in rendered
        assert "保留原件与来源" in rendered
        assert "SOUL.md" in rendered and "memory-hot.md" in rendered
    first_guide = str(first).split("# 家目录与整理约定", 1)[1].split("#", 1)[0]
    child_guide = str(child).split("# 家目录与整理约定", 1)[1].split("#", 1)[0]
    assert first_guide == child_guide
    config.home_context_enabled = False
    assert "# 家目录与整理约定" not in builder.build(user_prompt="任意任务")


def test_child_output_refs_keep_old_absolute_paths_and_resolve_sibling_relative_paths(tmp_path):
    from copy import deepcopy

    from agent_py_agent.agent.agent_core.orchestration.create_policy import (
        normalize_create_output_params,
    )

    home = tmp_path / "alice"
    cwd = home / "tasks" / "current"
    previous_file = home / "tasks" / "previous" / "report.md"
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=home),
        _current_run_params=SimpleNamespace(task_attributes={
            "conversation_execution_cwd": str(cwd),
            "run_workspace": {"task_root": str(home / "runs" / "runtime-1")},
        }),
    )
    original = {
        "goal": f"核对 {previous_file} 和 ../sibling/notes.md",
        "output_files": [str(previous_file), "../sibling/notes.md", "output/summary.md"],
        "attributes": {"output_refs": ["work/check.md", "https://example.invalid/artifact"]},
    }
    before = deepcopy(original)
    actual = normalize_create_output_params(original, agent)

    assert original == before
    assert actual["goal"] == before["goal"]
    assert actual["output_files"] == [
        str(previous_file), str(home / "tasks" / "sibling" / "notes.md"), str(cwd / "output" / "summary.md"),
    ]
    assert actual["attributes"]["output_refs"] == [str(cwd / "work" / "check.md"), "https://example.invalid/artifact"]


def test_ordinary_grandchild_uses_owner_home_even_when_parent_has_legacy_task_root(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration.create_constraints import (
        resolved_extra_write_roots,
    )

    home = tmp_path / "alice"
    parent = SimpleNamespace(
        id="child-parent", attributes={}, task_dir=str(home / "runs" / "private"),
        allowed_write_roots=[str(home / "tasks" / "old-task")],
    )
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=home),
        _current_run_params=SimpleNamespace(run_id=parent.id),
        subagents=SimpleNamespace(load=lambda run_id: parent if run_id == parent.id else None),
    )
    assert resolved_extra_write_roots(agent, {}, "继续核对上个月的项目") == [str(home)]
    assert resolved_extra_write_roots(agent, {"_exact_allowed_tools": True}, "核对来源") == []


def test_runtime_lookup_keeps_new_runs_and_old_task_archives(tmp_path):
    from agent_py_agent.agent.common.tool_output_paths import tool_output_roots_for_lookup
    from agent_py_agent.agent.user_space.home_runtime_query import _task_state_files

    expected_states = []
    expected_indexes = [tmp_path / "blobs" / "tool_outputs"]
    for namespace in ("runs", "tasks"):
        work = tmp_path / namespace / "2026-09-05" / "saved-id" / "work"
        archive = work / "blobs" / "tool_outputs"
        archive.mkdir(parents=True)
        state = work / "state.json"
        state.write_text('{"status":"DONE"}')
        expected_states.append(state)
        expected_indexes.append(archive)
    home = SimpleNamespace(owner_runs_dir=tmp_path / "runs", owner_tasks_dir=tmp_path / "tasks")
    assert set(_task_state_files(home, None)) == set(expected_states)
    assert set(tool_output_roots_for_lookup(tmp_path)) == set(expected_indexes)
