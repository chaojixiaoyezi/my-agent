from __future__ import annotations


def test_optional_llm_task_title_uses_short_json_and_sanitizes() -> None:
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.run_task_workspace_writer import _preferred_task_name
    from agent_py_agent.agent.backends import ModelResponse

    class Backend:
        def generate_json(self, prompt, *, max_tokens=None, messages=None):
            assert len(prompt) < 2500
            assert max_tokens == 64
            return ModelResponse(text='{"title":"星桥 发布站"}', backend="fake")

    agent = SimpleNamespace(
        config=SimpleNamespace(workspace_task_llm_title_enabled=True, workspace_task_llm_title_input_chars=2000),
        backend=Backend(),
    )
    params = SimpleNamespace(task_attributes={})

    assert _preferred_task_name(agent, params, "请做一个很长的建站任务") == "星桥-发布站"


def test_optional_llm_task_title_failure_falls_back_to_deterministic_title() -> None:
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.run_task_workspace_writer import _preferred_task_name
    from agent_py_agent.agent.backends.errors import ProviderTimeoutError

    class Backend:
        def generate_json(self, prompt, *, max_tokens=None, messages=None):
            del prompt, max_tokens, messages
            raise ProviderTimeoutError("title request timed out")

    agent = SimpleNamespace(
        config=SimpleNamespace(workspace_task_llm_title_enabled=True, workspace_task_llm_title_input_chars=2000),
        backend=Backend(),
    )
    params = SimpleNamespace(task_attributes={})

    assert _preferred_task_name(agent, params, "请整理季度销售复盘") == "整理季度销售复盘"


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


def test_attach_run_task_workspace_context_defaults_contract_output_root(tmp_path):
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
    assert artifact["allowed_output_roots"] == [workspace["output_dir"]]
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
    assert f"relative_input_root: {tmp_path}" in injection
    # 稳而不管减负:注入段只留目录事实+一句定位;目录使用教学收编 lessons/workspace.md
    assert "输入目录不是交付目录" in injection


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
    assert updated.delivery_contract["task_workspace"]["user_requested_output_dir"] == str(user_dir)


def test_attach_run_task_workspace_context_resolves_relative_user_output_root_to_project(tmp_path):
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
    expected_root = str((repo / "lab_outputs" / "compact-stress").resolve(strict=False))
    expected_report = str((repo / "lab_outputs" / "compact-stress" / "report.md").resolve(strict=False))
    assert artifact["preferred_path"] == expected_report
    assert artifact["allowed_output_roots"] == [expected_root]
    assert workspace["output_dir"] != expected_root
    assert updated.delivery_contract["task_workspace"]["user_requested_output_dir"] == expected_root


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
    assert artifact["allowed_output_roots"] == [str(user_file.parent)]
    assert updated.delivery_contract["task_workspace"]["user_requested_output_dir"] == str(user_file.parent)


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
    assert artifact["allowed_output_roots"] == [r"C:\Users\alice\agent-output"]
    assert updated.delivery_contract["task_workspace"]["user_requested_output_dir"] == r"C:\Users\alice\agent-output"


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
    user_dir = str((Path.home() / "agent-output").resolve(strict=False))
    assert artifact["allowed_output_roots"] == [user_dir]
    assert updated.delivery_contract["task_workspace"]["user_requested_output_dir"] == user_dir


def test_attach_run_task_workspace_context_rewrites_relative_output_contract_to_task_output(tmp_path):
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
    assert artifact["preferred_path"] == str(Path(workspace["output_dir"]) / "项目分析报告.md")
    assert artifact["allowed_output_roots"] == [workspace["output_dir"]]


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
    assert "单次运行" in cli_section and "不会有任何回复" in cli_section

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


def test_relay_legacy_outputs_projected_on_reused_workspace(tmp_path):
    """R11a 接力倒退钉子:接力轮(timeline≥2)注入上轮子代理产出清单;
    首轮/无遗产不注入。纯事实投影零指令。"""
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.run_task_workspace_writer import _workspace_prompt_section

    root = tmp_path / "task"
    work = root / "work"
    agents = work / "agents" / "subagent-x1"
    agents.mkdir(parents=True)
    (work / "timeline.jsonl").write_text('{"run":1}\n{"run":2}\n', encoding="utf-8")
    (agents / "runner_response.md").write_text("深度分析" * 3000, encoding="utf-8")
    paths = SimpleNamespace(root=root, output_dir=root / "output", work_dir=work)

    section = _workspace_prompt_section(paths, primary_workspace_root=tmp_path)
    assert "- relay_legacy_outputs:" in section and "subagent-x1" in section, "接力轮必须看到遗产清单"

    (work / "timeline.jsonl").write_text('{"run":1}\n', encoding="utf-8")
    first_round = _workspace_prompt_section(paths, primary_workspace_root=tmp_path)
    assert "- relay_legacy_outputs:" not in first_round, "首轮不注入"
