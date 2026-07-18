from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.action_protocol import RunScope, ToolCallEnvelope
from agent_py_agent.agent.tooling.models import ToolExecutionResult
from agent_py_agent.agent.verification.repository import (
    VerificationContext,
    VerificationEvidenceRepository,
)
from agent_py_agent.agent.verification.runtime import record_tool_verification


def _agent(tmp_path: Path, *, owner: str = "owner-a") -> tuple[SimpleNamespace, Path, Path]:
    project = tmp_path / f"project-{owner}"
    owner_home = tmp_path / owner
    (project / ".git").mkdir(parents=True)
    (project / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\naddopts = '-q'\n",
        encoding="utf-8",
    )
    params = SimpleNamespace(
        context_scope="default",
        task_id="child-task",
        task_attributes={
            "conversation_thread_id": "thread-1",
            "conversation_task_id": "root-task",
        },
    )
    agent = SimpleNamespace(
        root=project,
        home_paths=SimpleNamespace(
            owner_id=f"providers/feishu/users/{owner}",
            owner_home_dir=str(owner_home),
        ),
        _current_run_params=params,
        _current_run_task_workspace=str(project),
        _current_request_id="request-1",
    )
    return agent, project, owner_home


def _envelope(tool: str, payload: dict[str, object]) -> ToolCallEnvelope:
    return ToolCallEnvelope(
        call_id="call-1",
        source="model_tool_call",
        tool_name=tool,
        input=payload,
        scope=RunScope(task_id="child-task", root_task_id="root-task", run_id="child-run"),
    )


def test_runtime_records_child_test_under_structured_root_task(tmp_path: Path):
    agent, project, owner_home = _agent(tmp_path)
    result = ToolExecutionResult(
        "run_command",
        True,
        "24 passed",
        result_envelope={
            "process": {"status": "exited", "return_code": 0, "command_succeeded": True},
        },
    )

    record_tool_verification(
        agent,
        _envelope("run_command", {"command": "pytest -q", "working_dir": str(project)}),
        result,
    )

    fact = result.result_envelope["verification_evidence"]
    repository = VerificationEvidenceRepository(owner_home)
    context = VerificationContext("providers/feishu/users/owner-a", "thread-1", "root-task")
    assert (fact["status"], fact["scope"]) == ("passed", "full")
    assert repository.status(context, root=project)["status"] == "passed"


def test_successful_file_write_makes_same_root_task_evidence_stale(tmp_path: Path):
    agent, project, owner_home = _agent(tmp_path)
    test_result = ToolExecutionResult(
        "run_command",
        True,
        "24 passed",
        result_envelope={"process": {"status": "exited", "return_code": 0}},
    )
    record_tool_verification(
        agent,
        _envelope("run_command", {"command": "pytest", "working_dir": str(project)}),
        test_result,
    )
    changed = project / "app.py"
    changed.write_text("print('changed')\n", encoding="utf-8")
    write_result = ToolExecutionResult(
        "edit_file",
        True,
        "edited",
        result_envelope={"path": str(changed), "target_path": str(changed)},
    )

    record_tool_verification(
        agent,
        _envelope("edit_file", {"path": str(changed)}),
        write_result,
    )

    repository = VerificationEvidenceRepository(owner_home)
    context = VerificationContext("providers/feishu/users/owner-a", "thread-1", "root-task")
    assert write_result.result_envelope["verification_state"][0]["status"] == "stale"
    assert repository.status(context, root=project)["status"] == "stale"


def test_failed_write_and_arbitrary_command_do_not_change_evidence(tmp_path: Path):
    agent, project, owner_home = _agent(tmp_path)
    arbitrary = ToolExecutionResult(
        "run_command",
        True,
        "hello",
        result_envelope={"process": {"status": "exited", "return_code": 0}},
    )
    failed_write = ToolExecutionResult("edit_file", False, "missing", error_code="PATH_NOT_FOUND")

    record_tool_verification(
        agent,
        _envelope("run_command", {"command": "echo hello", "working_dir": str(project)}),
        arbitrary,
    )
    record_tool_verification(
        agent,
        _envelope("edit_file", {"path": str(project / "missing.py")}),
        failed_write,
    )

    repository = VerificationEvidenceRepository(owner_home)
    context = VerificationContext("providers/feishu/users/owner-a", "thread-1", "root-task")
    assert "verification_evidence" not in arbitrary.result_envelope
    assert "verification_state" not in failed_write.result_envelope
    assert repository.status(context, root=project)["status"] == "unverified"
