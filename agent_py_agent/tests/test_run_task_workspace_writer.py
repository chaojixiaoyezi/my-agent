from __future__ import annotations

import pytest


@pytest.mark.parametrize("folder", ["runs", "tasks", "projects/custom"])
def test_terminal_archive_scope_uses_owner_home_not_business_folder_name(tmp_path, folder):
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.run_task_workspace_writer import _workspace_is_owner_scoped

    owner = tmp_path / "owner"
    archive = owner / folder / "run-a"
    archive.mkdir(parents=True)
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=owner))
    assert _workspace_is_owner_scoped(agent, archive)
    assert not _workspace_is_owner_scoped(agent, tmp_path / "other-owner" / "run-a")
    assert not _workspace_is_owner_scoped(agent, owner)
    assert not _workspace_is_owner_scoped(agent, owner / ".." / "other-owner")


@pytest.mark.parametrize("target_part", ["date", "archive", "work", "state"])
def test_terminal_archive_scope_rejects_links_at_every_owner_relative_level(tmp_path, target_part):
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.run_task_workspace_writer import _workspace_is_owner_scoped

    owner = tmp_path / "owner"
    archive = owner / "runs" / "date" / "run-a"
    target = {
        "date": archive.parent, "archive": archive, "work": archive / "work",
        "state": archive / "work/state.json",
    }[target_part]
    target.parent.mkdir(parents=True)
    destination = owner / "linked-target"
    if target_part == "state":
        destination.write_text("{}")
    else:
        destination.mkdir()
    target.symlink_to(destination, target_is_directory=target_part != "state")
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=owner))
    assert not _workspace_is_owner_scoped(agent, archive)


def test_tool_output_archive_root_stays_fixed_when_turn_is_promoted(tmp_path) -> None:
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
        TOOL_OUTPUT_ARCHIVE_ROOT_ATTR,
        current_run_tool_output_archive_root,
    )

    owner = tmp_path / "owners" / "local" / "main"
    task = owner / "tasks" / "2026-08-29" / "demo"
    attrs: dict[str, object] = {}
    agent = SimpleNamespace(
        root=owner,
        home_paths=SimpleNamespace(owner_home_dir=owner),
        subagents=None,
    )
    params = SimpleNamespace(task_attributes=attrs, context_scope="default", run_id="run-a")

    first = current_run_tool_output_archive_root(agent, params)
    attrs["run_workspace"] = {
        "task_root": str(task),
        "output_dir": str(task / "output"),
        "work_dir": str(task / "work"),
    }
    second = current_run_tool_output_archive_root(agent, params)

    assert first == owner.resolve(strict=False)
    assert second == first
    assert attrs[TOOL_OUTPUT_ARCHIVE_ROOT_ATTR] == str(first)


def test_runtime_archive_uses_identity_not_user_directory_template(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.run_task_workspace_writer import _run_workspace_request
    from agent_py_agent.agent.user_space.run_workspace import ensure_run_workspace

    owner = tmp_path / "owner"
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=owner, owner_runs_dir=owner / "runs"),
        config=SimpleNamespace(workspace_task_path_template="tasks/{date}/{task_slug}"),
    )
    params = SimpleNamespace(task_id="request-one", run_id="request-one", task_attributes={})
    first = _run_workspace_request(agent, params, "整理季度销售复盘")
    same = _run_workspace_request(agent, params, "修改上个月的另一个项目")
    assert first.template == same.template
    root = ensure_run_workspace(first).root
    assert root.is_relative_to(owner / "runs")
    assert not (owner / "tasks").exists()
    assert (root / "work" / "state.json").is_file()
    params.task_id = "request-two"
    assert _run_workspace_request(agent, params, first.user_prompt).template != first.template


def test_runtime_archive_requires_structured_identity(tmp_path):
    from types import SimpleNamespace

    import pytest

    from agent_py_agent.agent.agent_core.run_task_workspace_writer import _run_workspace_request

    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=tmp_path))
    with pytest.raises(ValueError, match="explicit runtime identity"):
        _run_workspace_request(agent, SimpleNamespace(task_attributes={}), "任务名字不能冒充运行身份")


def test_task_local_workspace_root_prefers_agent_run_workspace_over_parent_task_root(tmp_path):
    from pathlib import Path
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
        current_run_task_work_dir,
        current_run_task_workspace_root,
    )

    parent_root = tmp_path / "tasks" / "parent"
    agent_root = parent_root / "work" / "agents" / "child-1"
    params = SimpleNamespace(
        context_scope="task_local",
        task_attributes={
            "run_workspace": {
                "task_root": str(parent_root),
                "output_dir": str(parent_root / "output"),
                "work_dir": str(parent_root / "work"),
            },
            "agent_run_workspace_dir": str(agent_root),
        },
        delivery_contract={
            "task_workspace": {
                "task_root": str(parent_root),
                "output_dir": str(parent_root / "output"),
                "work_dir": str(parent_root / "work"),
            }
        },
    )

    agent = SimpleNamespace(_current_run_task_workspace=str(parent_root))

    assert current_run_task_workspace_root(agent, params) == agent_root.resolve(strict=False)
    assert current_run_task_work_dir(agent, params) == agent_root.resolve(strict=False)
    assert current_run_task_workspace_root(agent, params) != Path(parent_root).resolve(strict=False)


def test_subagent_run_id_workspace_root_prefers_loaded_agent_workspace_even_without_task_local_scope(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
        current_run_task_work_dir,
        current_run_task_workspace_root,
    )

    parent_root = tmp_path / "tasks" / "parent"
    agent_root = parent_root / "work" / "agents" / "subagent-1"

    class Subagents:
        def load(self, run_id):
            assert run_id == "subagent-1"
            return SimpleNamespace(agent_run_workspace_dir=str(agent_root))

    agent = SimpleNamespace(subagents=Subagents(), _current_run_task_workspace=str(parent_root))
    params = SimpleNamespace(
        run_id="subagent-1",
        context_scope="default",
        task_attributes={
            "run_workspace": {
                "task_root": str(parent_root),
                "output_dir": str(parent_root / "output"),
                "work_dir": str(parent_root / "work"),
            }
        },
        delivery_contract={
            "task_workspace": {
                "task_root": str(parent_root),
                "output_dir": str(parent_root / "output"),
                "work_dir": str(parent_root / "work"),
            }
        },
    )

    assert current_run_task_workspace_root(agent, params) == agent_root.resolve(strict=False)
    assert current_run_task_work_dir(agent, params) == agent_root.resolve(strict=False)


def test_attach_runtime_context_does_not_invent_contract_output_permission(tmp_path):
    from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
        attach_run_task_workspace_context,
    )
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    params = RunParams(
        save=True,
        request_id="req-1",
        run_id="run-1",
        task_id="task-1",
        delivery_contract={
            "schema_version": "delivery_contract.v1",
            "artifacts": [{"artifact_id": "report", "kind": "md", "required": True}],
        },
    )

    updated = attach_run_task_workspace_context(agent, params, "写一份报告")

    workspace = updated.task_attributes["run_workspace"]
    artifact = updated.delivery_contract["artifacts"][0]
    assert artifact == params.delivery_contract["artifacts"][0]
    assert updated.delivery_contract["task_workspace"]["output_dir"] == workspace["output_dir"]
    assert updated.delivery_contract["task_workspace"]["work_dir"] == workspace["work_dir"]


def test_ordinary_conversation_chat_does_not_precreate_task_workspace(tmp_path):
    from agent_py_agent.agent.agent_core._runtime_params import ArchiveRunParams
    from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
        attach_run_task_workspace_context,
        write_run_task_workspace_if_needed,
    )
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.backends import ModelResponse
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    home = tmp_path / "home"
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(home)), tmp_path)
    params = RunParams(
        request_id="chat-1",
        run_id="chat-1",
        task_id="chat-1",
        task_attributes={"conversation_thread_id": "thread-1"},
    )

    updated = attach_run_task_workspace_context(agent, params, "今天天气怎么样")
    archived = write_run_task_workspace_if_needed(
        agent,
        ArchiveRunParams(
            do_save=True,
            user_prompt="今天天气怎么样",
            final_response=ModelResponse(text="晴", backend="echo"),
            archive_tool_calls=[],
            run_request_id="chat-1",
            run_id="chat-1",
            task_id="chat-1",
            source="gateway",
            task_attributes=updated.task_attributes,
        ),
    )

    assert updated is params
    assert "run_workspace" not in updated.task_attributes
    assert archived == ""
    assert not list(agent.home_paths.owner_tasks_dir.rglob("work"))


def test_attach_run_task_workspace_context_no_save_still_creates_task_workspace(tmp_path):
    from pathlib import Path

    from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
        attach_run_task_workspace_context,
    )
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    params = RunParams(
        save=False,
        request_id="req-nosave",
        run_id="run-nosave",
        task_id="task-nosave",
    )

    updated = attach_run_task_workspace_context(agent, params, "临时诊断但仍然需要任务工作目录")

    workspace = updated.task_attributes["run_workspace"]
    assert workspace["task_root"]
    assert Path(workspace["output_dir"]).name == "output"
    assert Path(workspace["work_dir"]).name == "work"
    injection = "\n".join(updated.inject)
    assert "# Current Task Workspace" in injection
    assert f"cwd: {agent.home_paths.owner_home_dir}" in injection
    # 运行定位不再强制分开输入与交付；修改既有文件也是合法的家目录工作。
    assert "续作保留原文件位置" in injection
    assert "输入目录不是交付目录" not in injection


def test_gateway_task_workspace_prompt_uses_validated_client_cwd(tmp_path) -> None:
    from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
        attach_run_task_workspace_context,
    )
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    service_root = tmp_path / "service"
    client_root = tmp_path / "client-project"
    client_root.mkdir()
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
        service_root,
    )
    params = RunParams(
        request_id="gw-client-cwd",
        run_id="gw-client-cwd",
        task_id="gw-client-cwd",
        task_attributes={
            "conversation_thread_id": "thread-client-cwd",
            "conversation_task_id": "gw-client-cwd",
            "conversation_execution_cwd": str(client_root),
            "conversation_runtime_workspace_roots": [str(client_root)],
        },
        delivery_contract={"schema_version": "delivery_contract.v1", "artifacts": []},
    )

    updated = attach_run_task_workspace_context(agent, params, "在 bbb 目录创建游戏")

    injection = "\n".join(updated.inject)
    assert f"cwd: {client_root.resolve()}" in injection
    assert updated.delivery_contract["task_workspace"]["source_workspace_root"] == str(
        client_root.resolve()
    )


def test_finish_run_workspace_rejects_a_different_run_identity(tmp_path):
    import json

    from agent_py_agent.agent.user_space.run_workspace import (
        EnsureRunWorkspaceRequest,
        FinishRunWorkspaceRequest,
        ensure_run_workspace,
        finish_run_workspace,
    )

    paths = ensure_run_workspace(
        EnsureRunWorkspaceRequest(
            home=tmp_path,
            template="tasks/{date}/{task_name}",
            task_name="身份测试",
            user_prompt="验证终态身份",
            request_id="request-one",
            run_id="run-one",
        )
    )

    finished = finish_run_workspace(
        FinishRunWorkspaceRequest(
            root=paths.root,
            request_id="request-two",
            run_id="run-two",
            status="DONE",
        )
    )

    state = json.loads(paths.state_json.read_text(encoding="utf-8"))
    assert finished is None
    assert state["status"] == "RUNNING"
    assert isinstance(state["updated_at"], float)
    assert "finished_at" not in state


def test_remove_unmodified_run_workspace_preserves_any_user_material(tmp_path):
    from agent_py_agent.agent.user_space.run_workspace import (
        EnsureRunWorkspaceRequest,
        ensure_run_workspace,
        remove_unmodified_run_workspace,
    )

    request = EnsureRunWorkspaceRequest(
        home=tmp_path,
        template="tasks/{date}/{task_name}",
        task_name="临时空壳",
        user_prompt="建立任务目录",
        request_id="request-placeholder",
        run_id="request-placeholder",
        task_id="request-placeholder",
    )
    untouched = ensure_run_workspace(request)

    assert remove_unmodified_run_workspace(
        untouched.root,
        owner_home=tmp_path,
        task_id="request-placeholder",
    ) is True
    assert not untouched.root.exists()

    preserved = ensure_run_workspace(request)
    user_file = preserved.root / "notes.txt"
    user_file.write_text("用户内容", encoding="utf-8")

    assert remove_unmodified_run_workspace(
        preserved.root,
        owner_home=tmp_path,
        task_id="request-placeholder",
    ) is False
    assert user_file.read_text(encoding="utf-8") == "用户内容"


def test_finish_run_workspace_is_idempotent_and_appends_one_terminal_event(tmp_path):
    import json

    from agent_py_agent.agent.user_space.run_workspace import (
        EnsureRunWorkspaceRequest,
        FinishRunWorkspaceRequest,
        ensure_run_workspace,
        finish_run_workspace,
    )

    paths = ensure_run_workspace(
        EnsureRunWorkspaceRequest(
            home=tmp_path,
            template="tasks/{date}/{task_name}",
            task_name="幂等终态",
            user_prompt="验证重复收尾",
            request_id="request-one",
            run_id="run-one",
        )
    )
    request = FinishRunWorkspaceRequest(
        root=paths.root,
        request_id="request-one",
        run_id="run-one",
        status="BLOCKED",
        runtime_status="context_overflow",
        runtime_reason="context_overflow",
    )

    assert finish_run_workspace(request) == paths
    assert finish_run_workspace(request) == paths

    state = json.loads(paths.state_json.read_text(encoding="utf-8"))
    events = [
        json.loads(line)
        for line in paths.timeline_jsonl.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert state["status"] == "BLOCKED"
    assert state["runtime_status"] == "context_overflow"
    assert isinstance(state["updated_at"], float)
    assert [event["event_type"] for event in events].count("run_workspace_finished") == 1


def test_terminal_workspace_status_uses_only_typed_runtime_facts():
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
        _terminal_workspace_status,
    )

    params = SimpleNamespace(task_attributes={})
    for status in ('needs_user_input', 'approval_required'):
        assert _terminal_workspace_status(params, SimpleNamespace(runtime_status=status)) == 'BLOCKED'
    assert _terminal_workspace_status(params, SimpleNamespace(runtime_status="ok")) == "DONE"
    assert (
        _terminal_workspace_status(
            params,
            SimpleNamespace(runtime_status="cancelled", runtime_reason="INTERRUPTED"),
        )
        == "CANCELLED"
    )
    assert (
        _terminal_workspace_status(
            params,
            SimpleNamespace(runtime_status="context_overflow", runtime_reason="context_overflow"),
        )
        == "BLOCKED"
    )
    assert (
        _terminal_workspace_status(
            params,
            SimpleNamespace(runtime_status="ok", runtime_reason="background_dispatch"),
        )
        == ""
    )
    goal_params = SimpleNamespace(task_attributes={"thread_goal_id": "goal-one"})
    assert (
        _terminal_workspace_status(
            goal_params,
            SimpleNamespace(runtime_status="unfinished", runtime_reason="TOOL_ROUND_LIMIT_REACHED"),
        )
        == ""
    )


def test_attach_run_task_workspace_context_preserves_user_requested_output_root(tmp_path):
    from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
        attach_run_task_workspace_context,
    )
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    user_dir = tmp_path / "user-output"
    params = RunParams(
        save=True,
        request_id="req-1",
        run_id="run-1",
        task_id="task-1",
        delivery_contract={
            "schema_version": "delivery_contract.v1",
            "artifacts": [{"artifact_id": "report", "kind": "md", "allowed_output_roots": [str(user_dir)]}],
        },
    )

    updated = attach_run_task_workspace_context(agent, params, "写一份报告到指定目录")

    artifact = updated.delivery_contract["artifacts"][0]
    assert artifact["allowed_output_roots"] == [str(user_dir)]
    assert updated.delivery_contract["task_workspace"]["output_dir"] != str(user_dir)
    assert "user_requested_output_dir" not in updated.delivery_contract["task_workspace"]


def test_attach_runtime_context_preserves_relative_contract_paths(tmp_path):
    from pathlib import Path

    from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
        attach_run_task_workspace_context,
    )
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    repo = tmp_path / "repo"
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), repo)
    params = RunParams(
        save=True,
        request_id="req-1",
        run_id="run-1",
        task_id="task-1",
        delivery_contract={
            "schema_version": "delivery_contract.v1",
            "artifacts": [
                {
                    "artifact_id": "report",
                    "kind": "md",
                    "preferred_path": "lab_outputs/compact-stress/report.md",
                    "allowed_output_roots": ["lab_outputs/compact-stress"],
                }
            ],
        },
    )

    updated = attach_run_task_workspace_context(agent, params, "报告写到 lab_outputs/compact-stress/report.md")

    workspace = updated.task_attributes["run_workspace"]
    artifact = updated.delivery_contract["artifacts"][0]
    assert artifact == params.delivery_contract["artifacts"][0]
    assert artifact["preferred_path"] == "lab_outputs/compact-stress/report.md"
    assert artifact["allowed_output_roots"] == ["lab_outputs/compact-stress"]
    assert workspace["output_dir"] not in artifact["allowed_output_roots"]
    assert "user_requested_output_dir" not in updated.delivery_contract["task_workspace"]


def test_attach_run_task_workspace_context_preserves_user_requested_absolute_artifact_path(tmp_path):
    from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
        attach_run_task_workspace_context,
    )
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    user_file = tmp_path / "requested-output" / "final_report.md"
    params = RunParams(
        save=True,
        request_id="req-1",
        run_id="run-1",
        task_id="task-1",
        delivery_contract={
            "schema_version": "delivery_contract.v1",
            "artifacts": [{"artifact_id": "report", "kind": "md", "path": str(user_file)}],
        },
    )

    updated = attach_run_task_workspace_context(agent, params, "报告写到指定绝对路径")

    artifact = updated.delivery_contract["artifacts"][0]
    assert artifact["path"] == str(user_file)
    assert "allowed_output_roots" not in artifact
    assert "user_requested_output_dir" not in updated.delivery_contract["task_workspace"]


def test_attach_run_task_workspace_context_preserves_windows_absolute_artifact_path(tmp_path):
    from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
        attach_run_task_workspace_context,
    )
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    user_file = r"C:\Users\alice\agent-output\final_report.md"
    params = RunParams(
        save=True,
        request_id="req-1",
        run_id="run-1",
        task_id="task-1",
        delivery_contract={
            "schema_version": "delivery_contract.v1",
            "artifacts": [{"artifact_id": "report", "kind": "md", "path": user_file}],
        },
    )

    updated = attach_run_task_workspace_context(agent, params, "报告写到 Windows 绝对路径")

    artifact = updated.delivery_contract["artifacts"][0]
    assert artifact["path"] == user_file
    assert "allowed_output_roots" not in artifact
    assert "user_requested_output_dir" not in updated.delivery_contract["task_workspace"]


def test_attach_run_task_workspace_context_preserves_home_artifact_path(tmp_path):
    from pathlib import Path

    from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
        attach_run_task_workspace_context,
    )
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    user_file = "~/agent-output/final_report.md"
    params = RunParams(
        save=True,
        request_id="req-1",
        run_id="run-1",
        task_id="task-1",
        delivery_contract={
            "schema_version": "delivery_contract.v1",
            "artifacts": [{"artifact_id": "report", "kind": "md", "path": user_file}],
        },
    )

    updated = attach_run_task_workspace_context(agent, params, "报告写到家目录底下")

    artifact = updated.delivery_contract["artifacts"][0]
    assert artifact["path"] == user_file
    assert "allowed_output_roots" not in artifact
    assert "user_requested_output_dir" not in updated.delivery_contract["task_workspace"]


def test_attach_runtime_context_preserves_literal_output_prefix_contract(tmp_path):
    from pathlib import Path

    from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
        attach_run_task_workspace_context,
    )
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    params = RunParams(
        save=True,
        request_id="req-1",
        run_id="run-1",
        task_id="task-1",
        delivery_contract={
            "schema_version": "delivery_contract.v1",
            "artifacts": [
                {
                    "artifact_id": "report",
                    "kind": "md",
                    "preferred_path": "./output/项目分析报告.md",
                    "allowed_output_roots": ["./output"],
                }
            ],
        },
    )

    updated = attach_run_task_workspace_context(agent, params, "报告放到你这次任务自己的 output 目录里")

    workspace = updated.task_attributes["run_workspace"]
    artifact = updated.delivery_contract["artifacts"][0]
    assert artifact["preferred_path"] == "./output/项目分析报告.md"
    assert artifact["allowed_output_roots"] == ["./output"]
    assert workspace["output_dir"] not in artifact["allowed_output_roots"]


def test_single_shot_environment_fact_only_for_cli_run(tmp_path):
    """R7c 实锤钉子(缺陷③):cli_run 单次运行必须注入"请示无人应答"环境事实;
    gateway(有 guidance 渠道)不注入,避免误导可交互形态。"""
    from dataclasses import replace

    from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
        attach_run_task_workspace_context,
    )
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    base = RunParams(save=True, request_id="req-ss", run_id="run-ss", task_id="task-ss")

    cli = attach_run_task_workspace_context(agent, replace(base, source="cli_run"), "做一个任务")
    cli_section = next(item for item in cli.inject if "# Current Task Workspace" in str(item))
    assert "单次运行" in cli_section and "不会收到应答" in cli_section

    gateway = attach_run_task_workspace_context(agent, replace(base, source="gateway"), "做一个任务")
    gateway_section = next(item for item in gateway.inject if "# Current Task Workspace" in str(item))
    assert "单次运行" not in gateway_section, "gateway 有 guidance 补发渠道,不得注入单次事实"
    assert f"- cwd: {tmp_path}" in gateway_section
    assert "task_root:" not in gateway_section
    assert str(gateway.task_attributes["run_workspace"]["task_root"]) not in gateway_section


def test_same_prompt_relay_reuses_task_workspace(tmp_path):
    """R8 接力实锤钉子:同 prompt 的新 run(三个机器 ID 全新)必须复用同一任务
    目录接续,而不是开 -run-<ns> 新目录把接力变重做;不同 prompt 不得误复用。"""
    from agent_py_agent.agent.user_space.run_workspace import (
        EnsureRunWorkspaceRequest,
        ensure_run_workspace,
    )

    def request(prompt, rid):
        return EnsureRunWorkspaceRequest(
            home=tmp_path / "home",
            template="tasks/{date}/{task_slug}",
            task_name=prompt,
            user_prompt=prompt,
            request_id=f"req-{rid}",
            run_id=f"run-{rid}",
            task_id=f"run-{rid}",
        )

    first = ensure_run_workspace(request("帮我做一份周榜任务,共 24 周。", "1781000000001"))
    relay = ensure_run_workspace(request("帮我做一份周榜任务,共 24 周。", "1781000000002"))
    other = ensure_run_workspace(request("另一个完全不同的任务。", "1781000000003"))

    assert relay.root == first.root, "同 prompt 接力必须复用同一任务目录"
    assert other.root != first.root, "不同 prompt 不得误复用"


def test_runtime_cwd_prompt_does_not_scan_old_child_outputs(tmp_path, monkeypatch):
    from pathlib import Path

    from agent_py_agent.agent.agent_core.run_task_workspace_writer import _workspace_prompt_section

    def reject_scan(*args, **kwargs):
        raise AssertionError("目录说明不应扫描旧运行资料")

    monkeypatch.setattr(Path, "glob", reject_scan)
    section = _workspace_prompt_section(primary_workspace_root=tmp_path)
    assert f"cwd: {tmp_path}" in section
    assert "普通目录" in section
    assert "relay_legacy_outputs" not in section
    assert "最终交付物写这里" not in section


@pytest.mark.parametrize('business_task', [False, True])
def test_only_bound_business_task_exposes_existing_delivery_directory(tmp_path, business_task):
    import json
    from pathlib import Path

    from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
        attach_run_task_workspace_context,
    )
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(tmp_path / 'home')), tmp_path / 'work')
    owner = agent.home_paths.owner_id
    params = RunParams(request_id='delivery-guidance', run_id='delivery-guidance', task_id='delivery-guidance',
        source='gateway', delivery_contract=({'artifacts': [], 'reply': {'format': 'plain'}} if business_task else None),
        business_context=(BusinessExecutionContext(owner, 'space-a', None,
            'delivery-guidance', 1, None, (), (), 'business-context.v1') if business_task else None),
        task_attributes={'conversation_thread_id': 'thread-a', 'conversation_task_id': 'delivery-guidance',
                         'conversation_execution_cwd': str(tmp_path / 'work')})
    updated = attach_run_task_workspace_context(agent, params, '创建交付文件')
    prompt = '\n'.join(updated.inject)
    output = (updated.delivery_contract['task_workspace']['output_dir']
              if updated.delivery_contract else None)
    assert (f'本任务已声明交付目录: {output}' in prompt) is business_task
    assert f'cwd: {tmp_path / "work"}' in prompt
    assert updated.task_attributes['conversation_execution_cwd'] == str(tmp_path / 'work')
    if business_task:
        assert '不会自动成为已登记交付物' in prompt
        assert '/tasks/' in output and '/runs/' not in output
        assert updated.delivery_contract['artifacts'] == []
        assert updated.delivery_contract['reply'] == {'format': 'plain'}
        from agent_py_agent.tests.test_tool_gateway_contract import _execute, _registry
        registry = _registry(agent.home_paths.owner_home_dir)
        written = _execute(registry, 'write_file', {'path': str(Path(output) / 'result.json'), 'content': '{"total":4200}'},
            write_boundary={'allowed_write_roots': [str(agent.home_paths.owner_home_dir)],
                            'forbidden_write_roots': [str(agent.home_paths.owner_home_dir / 'runs')]})
        assert written.ok, written.output
        agent.config.workspace_task_path_template = 'tasks/changed/{task_slug}'
        retried = attach_run_task_workspace_context(agent, params, '创建交付文件')
        assert retried.delivery_contract['task_workspace']['output_dir'] == output
        frozen = json.loads((Path(
            updated.task_attributes['run_workspace']['task_root']) / 'work/run_workspace.json').read_text())
        assert frozen['delivery_contract']['task_workspace']['output_dir'] == output


def test_delivery_collection_keeps_unregistered_and_working_files_out(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core._finalization_service import _structured_delivery_artifacts
    from agent_py_agent.agent.artifacts.registry import ArtifactRegistration, register_artifact

    output = tmp_path / 'output'
    output.mkdir()
    deliverable = output / 'result.json'
    deliverable.write_text('{"total":4530}')
    working = tmp_path / 'working.json'
    working.write_text('{"draft":true}')
    ready = register_artifact(ArtifactRegistration(workspace_root=tmp_path, path=deliverable,
        run_id='run-a', task_id='task-a', created_by_tool='write_file')).to_dict()
    other = register_artifact(ArtifactRegistration(workspace_root=tmp_path, path=working,
        run_id='run-a', task_id='task-a', created_by_tool='write_file')).to_dict()
    ctx = SimpleNamespace(task_attributes={'run_workspace': {'output_dir': str(tmp_path / 'runs/output'),
                                                               }},
                          delivery_contract={'task_workspace': {'output_dir': str(output)}},
                          archive_tool_calls=[{'artifact_registry_refs': [ready, other]}])
    assert [row['path'] for row in _structured_delivery_artifacts(ctx)] == [str(deliverable)]
    ctx.archive_tool_calls = [{'tool': 'run_command', 'output': str(deliverable)}]
    assert _structured_delivery_artifacts(ctx) == []


@pytest.mark.parametrize('bad', [None, False, 7, {}, [], [''], ''])
def test_delivery_contract_bad_optional_paths_do_not_crash_finalization(bad):
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core._finalization_service import _declared_delivery_targets
    from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
        _delivery_output_dir,
    )

    contract = {'task_workspace': {'output_dir': bad},
                'artifacts': [{'allowed_output_roots': bad, 'path': bad}]}
    assert _delivery_output_dir(SimpleNamespace(business_context=object()), contract) is None
    assert _declared_delivery_targets({}, contract) == ((), frozenset())
    assert _declared_delivery_targets({}, {'artifacts': bad}) == ((), frozenset())


def _foreground_output_case(tmp_path):
    from types import SimpleNamespace

    owner = tmp_path / "owners/local/main"
    owner.mkdir(parents=True)
    link = SimpleNamespace(task_id="request-a", thread_id="thread-a", status="active", created_at=1704067200.0)
    thread = SimpleNamespace(owner_id="local/main", metadata={}, last_user_request_id="request-a", last_user_message_id="msg-a")
    store = SimpleNamespace(tasks=SimpleNamespace(link_report=lambda thread_id, task_id: (link, None)),
                            threads=SimpleNamespace(load=lambda thread_id: thread))
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=owner, owner_id="local/main", root=tmp_path),
        conversation_store=store, config=SimpleNamespace(workspace_task_path_template="tasks/{date}/{task_slug}"))
    params = SimpleNamespace(task_attributes={"conversation_task_id":"request-a", "conversation_thread_id":"thread-a"},
                             request_id="request-a", delivery_contract=None, business_context=None)
    return agent, params, link, thread


def test_foreground_output_uses_original_link_birth_and_preserves_explicit_contract(tmp_path):
    from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
        _host_delivery_contract,
    )

    agent, params, link, thread = _foreground_output_case(tmp_path)
    result = _host_delivery_contract(agent, params)
    output = agent.home_paths.owner_home_dir / "tasks/2024-01-01/request-a/output"
    assert result["task_workspace"]["output_dir"] == str(output)
    assert output.is_dir()
    assert _host_delivery_contract(agent, params) == result
    params.delivery_contract = {"task_workspace":{"output_dir":"/explicit/project/output"}}
    assert _host_delivery_contract(agent, params) == params.delivery_contract


@pytest.mark.parametrize("mismatch", ["owner", "request", "terminal", "background", "missing_birth"])
def test_foreground_output_rejects_unbound_identity_without_creation(tmp_path, mismatch):
    from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
        _host_delivery_contract,
    )

    agent, params, link, thread = _foreground_output_case(tmp_path)
    if mismatch == "owner":
        thread.owner_id = "local/other"
    if mismatch == "request":
        thread.last_user_request_id = "other"
    if mismatch == "terminal":
        link.status = "completed"
    if mismatch == "background":
        thread.metadata = {"background_agent": True}
    if mismatch == "missing_birth":
        link.created_at = 0
    assert _host_delivery_contract(agent, params) is None
    assert not (agent.home_paths.owner_home_dir / "tasks").exists()


def test_foreground_output_rejects_control_template_and_child_link(tmp_path):
    from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
        _host_delivery_contract,
    )

    agent, params, link, thread = _foreground_output_case(tmp_path)
    agent.config.workspace_task_path_template = "runs/{date}/{task_slug}"
    with pytest.raises(ValueError):
        _host_delivery_contract(agent, params)
    assert not (agent.home_paths.owner_home_dir / "runs").exists()
    agent.config.workspace_task_path_template = "tasks/{date}/{task_slug}"
    outside = tmp_path / "outside"
    outside.mkdir()
    (agent.home_paths.owner_home_dir / "tasks").symlink_to(outside, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        _host_delivery_contract(agent, params)
    assert not list(outside.iterdir())


def test_task_promotion_publishes_same_delivery_contract_to_loop(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
    from agent_py_agent.agent.conversation import task_promotion

    contract = {"task_workspace":{"output_dir":"/owner/tasks/task-a/output"}}
    agent = SimpleNamespace(_current_run_params=SimpleNamespace(task_attributes={"conversation_task_id":"task-a"}, delivery_contract=contract))
    projection = ToolLoopExecuteParams(user_prompt="做个文件", memories=[], runtime_injections=[], prompt_files=[],
        tool_catalog_section="", tool_recommendations_section="", tool_context=[], effective_on_chunk=None,
        allowed_tools=None, write_boundary=None, task_attributes={}, request_id="req", run_id="req", task_id="req",
        one_shot_tool_calls=set(), executed_tools=[], archive_tool_calls=[])
    monkeypatch.setattr(task_promotion, "promote_current_conversation_task", lambda agent: object())
    task_promotion.promote_conversation_task_for_run(agent, projection)
    assert projection.delivery_contract is contract
    assert projection.task_attributes == agent._current_run_params.task_attributes


def test_child_uses_same_parent_task_output_without_changing_cwd(tmp_path):
    from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
        _host_delivery_contract,
    )

    agent, params, link, thread = _foreground_output_case(tmp_path)
    parent = _host_delivery_contract(agent, params)
    agent._current_subagent_run_id = "child-run"
    params.context_scope = "task_local"
    params.request_id = "child-attempt"
    params.task_attributes["conversation_execution_cwd"] = str(tmp_path / "explicit-project")
    child = _host_delivery_contract(agent, params)
    assert child == parent
    assert params.task_attributes["conversation_execution_cwd"] == str(tmp_path / "explicit-project")


def test_foreground_materialization_freezes_host_output_in_manifest_and_params(tmp_path):
    import json
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
        materialize_promoted_task_workspace,
    )
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams

    agent, values, link, thread = _foreground_output_case(tmp_path)
    agent.root = agent.home_paths.owner_home_dir
    agent.tools = SimpleNamespace(workspace_root=tmp_path)
    params = RunParams(request_id="request-a", run_id="request-a", task_id="request-a",
        source="gateway", task_attributes=values.task_attributes)
    root = materialize_promoted_task_workspace(agent, params, "制作文件")
    manifest = json.loads((root / "work/run_workspace.json").read_text())
    expected = str(agent.root / "tasks/2024-01-01/request-a/output")
    assert params.delivery_contract["task_workspace"]["output_dir"] == expected
    assert manifest["delivery_contract"]["task_workspace"]["output_dir"] == expected
    assert params.task_attributes["run_workspace"]["task_root"] == str(root)
    assert params.task_attributes["run_workspace"]["output_dir"] == expected
    assert agent.tools.workspace_root == tmp_path


def _ordinary_session_output_case(tmp_path):
    from types import SimpleNamespace

    owner = tmp_path / 'owners/local/main'
    owner.mkdir(parents=True)
    task = SimpleNamespace(task_id='stask-a', sender_thread_id='sender', target_thread_id='executor',
        created_at=1704067200.0, status='accepted', metadata={}, conversation_request_id='stask-a')
    threads = {'sender': SimpleNamespace(owner_id='local/main'),
               'executor': SimpleNamespace(owner_id='local/main')}
    store = SimpleNamespace(session_tasks=SimpleNamespace(load=lambda key: task if key == task.task_id else None),
        threads=SimpleNamespace(load=threads.get), tasks=SimpleNamespace(link_report=lambda *args: (None, None)))
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=owner, owner_id='local/main'),
        config=SimpleNamespace(workspace_task_path_template='tasks/{date}/{task_slug}'), conversation_store=store)
    params = SimpleNamespace(source='background_main_agent', run_id='stask-a', task_id='stask-a', request_id='stask-a',
        business_context=None, delivery_contract=None, task_attributes={
            'conversation_session_task_id': 'stask-a', 'conversation_task_id': 'stask-a',
            'conversation_thread_id': 'executor', 'conversation_execution_cwd': str(tmp_path / 'explicit-project')})
    return agent, params, task, threads


def test_ordinary_session_uses_original_birth_and_narrow_output_without_changing_cwd(tmp_path):
    from agent_py_agent.agent.agent_core.run_task_workspace_writer import _host_delivery_contract

    agent, params, task, threads = _ordinary_session_output_case(tmp_path)
    expected = str(agent.home_paths.owner_home_dir / 'tasks/2024-01-01/stask-a/output')
    assert _host_delivery_contract(agent, params)['task_workspace']['output_dir'] == expected
    assert params.task_attributes['conversation_execution_cwd'] == str(tmp_path / 'explicit-project')
    assert not (agent.home_paths.owner_home_dir / 'tasks').exists()
    params.task_attributes.pop('conversation_task_id')
    assert _host_delivery_contract(agent, params)['task_workspace']['output_dir'] == expected
    params.delivery_contract = {'task_workspace': {'output_dir': str(tmp_path / 'explicit-output')}}
    assert _host_delivery_contract(agent, params) == params.delivery_contract


@pytest.mark.parametrize('mismatch', ['owner', 'sender_owner', 'target', 'run', 'task', 'request', 'attribute', 'cancelled', 'bound_request'])
def test_ordinary_session_output_rejects_wrong_owner_target_or_exact_executor(tmp_path, mismatch):
    from agent_py_agent.agent.agent_core.run_task_workspace_writer import _host_delivery_contract

    agent, params, task, threads = _ordinary_session_output_case(tmp_path)
    if mismatch == 'owner':
        threads['executor'].owner_id = 'local/other'
    elif mismatch == 'sender_owner':
        threads['sender'].owner_id = 'local/other'
    elif mismatch == 'target':
        params.task_attributes['conversation_thread_id'] = 'sender'
    elif mismatch in {'run', 'task', 'request'}:
        setattr(params, mismatch + '_id', 'stask-other')
    elif mismatch == 'attribute':
        params.task_attributes['conversation_task_id'] = 'stask-other'
    elif mismatch == 'cancelled':
        task.status = 'cancelled'
    else:
        task.conversation_request_id = 'other-request'
    with pytest.raises(PermissionError, match='SESSION_TASK_WORKSPACE_IDENTITY_MISMATCH'):
        _host_delivery_contract(agent, params)
    assert not (agent.home_paths.owner_home_dir / 'tasks').exists()


@pytest.mark.parametrize('mismatch', [None, 'root_request', 'root', 'thread', 'attempt', 'terminal', 'transcript', 'origin_request'])
def test_ordinary_session_child_reuses_only_canonical_parent_output(tmp_path, mismatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.run_task_workspace_writer import _host_delivery_contract

    agent, params, task, threads = _ordinary_session_output_case(tmp_path)
    params.source, params.context_scope = 'subagent', 'task_local'
    params.run_id, params.request_id, params.attempt_id = 'child-a', 'attempt-a', 'attempt-a'
    params.task_attributes.pop('conversation_session_task_id')
    params.task_attributes['conversation_request_id'] = params.request_id
    record = SimpleNamespace(id='child-a', root_id=task.task_id, status='RUNNING', runner_active_attempt_id='attempt-a',
        attributes={'conversation_request_id': task.task_id, 'conversation_task_id': task.task_id,
                    'conversation_thread_id': task.target_thread_id})
    agent._current_subagent_run_id = record.id
    agent.subagents = SimpleNamespace(load=lambda run_id: record if run_id == record.id else None)
    def require_authority(request):
        if request.attempt_id != 'attempt-a' or record.status == 'CANCELLED':
            raise PermissionError('EXECUTION_AUTHORITY_UNAVAILABLE')
    agent.tools = SimpleNamespace(operation_store=SimpleNamespace(require_authority=require_authority))
    if mismatch == 'root_request':
        params.task_attributes['conversation_request_id'] = task.task_id
    elif mismatch == 'root':
        record.root_id = 'stask-other'
    elif mismatch == 'thread':
        record.attributes['conversation_thread_id'] = 'other-thread'
    elif mismatch == 'attempt':
        params.attempt_id = 'old-attempt'
    elif mismatch == 'terminal':
        record.status = 'CANCELLED'
    elif mismatch == 'transcript':
        params.task_attributes['conversation_request_id'] = 'other-attempt'
    elif mismatch == 'origin_request':
        record.attributes['conversation_request_id'] = 'other-task'
    if mismatch not in {None, 'root_request'}:
        with pytest.raises(PermissionError):
            _host_delivery_contract(agent, params)
    else:
        result = _host_delivery_contract(agent, params)
        assert result['task_workspace']['output_dir'] == str(agent.home_paths.owner_home_dir / 'tasks/2024-01-01/stask-a/output')
    assert not (agent.home_paths.owner_home_dir / 'tasks').exists()


@pytest.mark.parametrize('bound_before_context', [False, True])
def test_ordinary_session_output_contract_materializes_and_inherits_exact_root(tmp_path, bound_before_context):
    from agent_py_agent.agent.agent_core.orchestration.create_constraints import (
        delegated_product_write_roots,
    )
    from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
        attach_run_task_workspace_context,
    )
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(tmp_path/'home')), tmp_path/'project')
    sender = agent.conversation_store.threads.create(owner_id=agent.home_paths.owner_id, canonical_user_id='local-agent')
    target = agent.conversation_store.threads.create(owner_id=agent.home_paths.owner_id, canonical_user_id='local-agent')
    task = agent.conversation_store.session_tasks.create(SessionTaskDraft(sender.thread_id, target.thread_id, '普通委派'), now=1704067200.0)
    if bound_before_context:
        agent.conversation_store.session_tasks.bind_turn(task.task_id, turn_id=task.task_id)
    owner = agent.home_paths.owner_home_dir
    attrs = {'conversation_thread_id': target.thread_id, 'conversation_task_id': task.task_id,
        'conversation_session_task_id': task.task_id, 'conversation_execution_cwd': str(owner),
        'conversation_runtime_workspace_roots': [str(owner)], 'conversation_execution_workspace_explicit': True}
    if not bound_before_context:
        # 原首次wake尚未晋升ThreadTaskLink，只携带原SessionTask ID；不替宿主补字段。
        attrs.pop('conversation_task_id')
    params = RunParams(source='background_main_agent', run_id=task.task_id, task_id=task.task_id,
        request_id=task.task_id, task_attributes=attrs)
    attached = attach_run_task_workspace_context(agent, params, '制作文件')
    output = owner / 'tasks/2024-01-01' / task.task_id / 'output'
    assert attached.delivery_contract['task_workspace']['output_dir'] == str(output)
    from agent_py_agent.agent.agent_core._finalization_service import _declared_delivery_targets
    if bound_before_context:
        assert attached.task_attributes['run_workspace']['output_dir'] == str(output)
        # 工作晋升后无工具表达阶段也保留同一个不可执行的宿主声明。
        roots, exact = _declared_delivery_targets(attached.task_attributes, None)
        assert roots == (output,) and not exact
    else:
        assert 'run_workspace' not in attached.task_attributes  # 原预绑定阶段仍不提前物化内部运行目录。
    assert attached.task_attributes['conversation_execution_cwd'] == str(owner)
    assert output.is_dir()
    agent._current_run_params = attached
    roots = delegated_product_write_roots(agent)
    assert str(output) in roots and str(owner/'tasks') not in roots
    assert agent.conversation_store.session_tasks.load(task.task_id).status == ('accepted' if bound_before_context else 'queued')
    from agent_py_agent.agent.agent_core.tool_runtime_ledger import (
        write_boundary_with_runtime_ledger,
    )
    from agent_py_agent.tests.test_tool_gateway_contract import _execute, _registry

    registry = _registry(owner)
    registry.owner_scope_root = str(owner)
    agent.tools = registry
    child = RunParams(source='subagent_run_model_turn', context_scope='task_local', task_id=task.task_id,
        task_attributes=dict(attached.task_attributes), write_boundary={'allowed_write_roots': list(roots)})
    boundary = write_boundary_with_runtime_ledger(agent, child)
    own = _execute(registry, 'write_file', {'path': str(output/'own.csv'), 'content': 'label,value\nalpha,7\n'}, write_boundary=boundary)
    assert own.ok, own.output
    foreign = owner/'tasks/2024-01-01/stask-other/output/no.csv'
    denied = _execute(registry, 'write_file', {'path': str(foreign), 'content': 'foreign'}, write_boundary=boundary)
    assert not denied.ok and not foreign.exists()


def test_session_child_output_uses_real_execution_fence_not_lagging_runner_projection(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.run_task_workspace_writer import _host_delivery_contract
    from agent_py_agent.agent.runtime_db.managed_operation_store import (
        AuthorityContextMissing,
        ManagedOperationStore,
    )
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository

    agent, params, task, threads = _ordinary_session_output_case(tmp_path)
    repo = RuntimeRepository(tmp_path / 'runtime.db')
    native = repo.record_run_creation(owner_id='local/main', conversation_task_id=task.task_id,
        thread_id=task.target_thread_id, run_id='child-a', role='assistant')
    params.source, params.context_scope = 'subagent_run_model_turn', 'task_local'
    params.run_id, params.request_id, params.attempt_id = 'child-a', native['attempt_id'], native['attempt_id']
    params.task_attributes.pop('conversation_session_task_id')
    params.task_attributes['conversation_request_id'] = params.request_id
    record = SimpleNamespace(id='child-a', root_id=task.task_id, status='PENDING', runner_active_attempt_id='',
        attributes={'conversation_request_id': task.task_id, 'conversation_task_id': task.task_id,
                    'conversation_thread_id': task.target_thread_id})
    agent._current_subagent_run_id = record.id
    agent.subagents = SimpleNamespace(load=lambda run_id: record if run_id == record.id else None)
    agent.tools = SimpleNamespace(operation_store=ManagedOperationStore(repo))
    expected = _host_delivery_contract(agent, params)
    assert expected['task_workspace']['output_dir'].endswith('/tasks/2024-01-01/stask-a/output')
    params.attempt_id = 'other-child-attempt'
    params.request_id = params.attempt_id
    params.task_attributes['conversation_request_id'] = params.attempt_id
    with pytest.raises(AuthorityContextMissing):
        _host_delivery_contract(agent, params)
    params.attempt_id = native['attempt_id']
    params.request_id = params.attempt_id
    params.task_attributes['conversation_request_id'] = params.attempt_id
    assert repo.settle_agent_attempt(agent_run_id=native['agent_run_id'], attempt_id=native['attempt_id'])['settled']
    with pytest.raises(AuthorityContextMissing):
        _host_delivery_contract(agent, params)
    assert not (agent.home_paths.owner_home_dir / 'tasks').exists()
