from __future__ import annotations


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
