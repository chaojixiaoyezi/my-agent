from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.tooling.runtime_contracts import (
    ToolCall,
    ToolResult,
    ToolSuccessFacts,
)
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


def _call(tool: str, arguments: dict[str, object]) -> ToolCall:
    return ToolCall(
        call_id="call-1",
        tool_name=tool,
        arguments=arguments,
        source_protocol="native",
        schema_hash="sha256:" + "0" * 64,
        run_id="child-run",
        turn_id="turn-1",
        attempt_id="attempt-1",
    )


def _success(call: ToolCall, output: str, handler_metadata: dict[str, object]) -> ToolResult:
    return ToolResult.succeeded(
        call,
        output,
        facts=ToolSuccessFacts(metadata={"handler_details": handler_metadata}),
    )


def test_runtime_records_child_test_under_structured_root_task(tmp_path: Path):
    agent, project, owner_home = _agent(tmp_path)
    call = _call("run_command", {"command": "pytest -q", "working_dir": str(project)})
    result = _success(
        call,
        "24 passed",
        {
            "process": {"status": "exited", "return_code": 0, "command_succeeded": True},
        },
    )

    result = record_tool_verification(agent, call, result)

    fact = result.metadata["handler_details"]["verification_evidence"]
    repository = VerificationEvidenceRepository(owner_home)
    context = VerificationContext("providers/feishu/users/owner-a", "thread-1", "root-task")
    assert (fact["status"], fact["scope"]) == ("passed", "full")
    assert repository.status(context, root=project)["status"] == "passed"


def test_successful_file_write_makes_same_root_task_evidence_stale(tmp_path: Path):
    agent, project, owner_home = _agent(tmp_path)
    test_call = _call("run_command", {"command": "pytest", "working_dir": str(project)})
    test_result = _success(
        test_call,
        "24 passed",
        {"process": {"status": "exited", "return_code": 0}},
    )
    test_result = record_tool_verification(agent, test_call, test_result)
    changed = project / "app.py"
    changed.write_text("print('changed')\n", encoding="utf-8")
    write_call = _call("edit_file", {"path": str(changed)})
    write_result = _success(
        write_call,
        "edited",
        {"path": str(changed), "target_path": str(changed)},
    )

    write_result = record_tool_verification(agent, write_call, write_result)

    repository = VerificationEvidenceRepository(owner_home)
    context = VerificationContext("providers/feishu/users/owner-a", "thread-1", "root-task")
    state = write_result.metadata["handler_details"]["verification_state"][0]
    assert state["status"] == "stale"
    assert state["last_verification_id"] == test_result.metadata["handler_details"][
        "verification_evidence"
    ]["id"]
    assert state["last_verification_status"] == "passed"
    assert repository.status(context, root=project)["status"] == "stale"


def test_failed_write_and_arbitrary_command_do_not_change_evidence(tmp_path: Path):
    agent, project, owner_home = _agent(tmp_path)
    arbitrary_call = _call("run_command", {"command": "echo hello", "working_dir": str(project)})
    arbitrary = _success(
        arbitrary_call,
        "hello",
        {"process": {"status": "exited", "return_code": 0}},
    )
    failed_call = _call("edit_file", {"path": str(project / "missing.py")})
    failed_write = ToolResult.failed(
        failed_call,
        "missing",
        error_code="PATH_NOT_FOUND",
        failure_stage="execution",
    )

    arbitrary = record_tool_verification(agent, arbitrary_call, arbitrary)
    failed_write = record_tool_verification(agent, failed_call, failed_write)

    repository = VerificationEvidenceRepository(owner_home)
    context = VerificationContext("providers/feishu/users/owner-a", "thread-1", "root-task")
    assert "verification_evidence" not in arbitrary.metadata.get("handler_details", {})
    assert "verification_state" not in failed_write.metadata.get("handler_details", {})
    assert repository.status(context, root=project)["status"] == "unverified"
