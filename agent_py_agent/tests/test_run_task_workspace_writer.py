from __future__ import annotations


def test_attach_run_task_workspace_context_defaults_contract_output_root(tmp_path):
    from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
        attach_run_task_workspace_context,
    )
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.config import AgentConfig
    from agent_py_agent.agent.core import SimpleAgent

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


def test_attach_run_task_workspace_context_preserves_user_requested_output_root(tmp_path):
    from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
        attach_run_task_workspace_context,
    )
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.config import AgentConfig
    from agent_py_agent.agent.core import SimpleAgent

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
