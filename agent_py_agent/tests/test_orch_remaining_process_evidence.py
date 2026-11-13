"""复用原入口，验证隔离持久数据跨独立进程的授权与累计额度。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.runtime_context import set_current_subagent_context
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.tests.test_orch01_management_continuity import (
    Case,
    _agent,
    _bound,
    _child_messages,
    _foreground,
    _guide,
    _scheduled_params,
    _start_scheduled_run,
    case,
)


def _worker(payload):
    if payload["kind"] in {"management", "board"}:
        from agent_py_agent.agent.settings import AgentConfig

        agent = _agent(
            AgentConfig(model_backend="echo", my_agent_home=payload["home"], enable_subagents=True),
            Path(payload["workspace"]),
        )
        thread = agent.conversation_store.threads.load(payload["thread"])
        if payload["kind"] == "board":
            from agent_py_agent.agent.agent_core.orchestration.tools.handoff_board_tool import (
                ManageHandoffBoardTool,
            )
            from agent_py_agent.agent.tooling.models import ToolInvocationContext

            agent._current_run_params = RunParams(run_id=payload["child"], task_id=payload["child"])
            set_current_subagent_context(agent, run_id=payload["child"])
            tool = ManageHandoffBoardTool(agent)
            context = ToolInvocationContext(runtime_snapshot=agent.tools.runtime_snapshot())
            read = tool.execute_scoped(
                {
                    "action": "list_items",
                    "case_id": payload["case"],
                    "responsible_actor": payload["child"],
                },
                context,
            )
            assert read.ok, read.output
            rows = json.loads(read.output)["result"]["items"]
            row = next(row for row in rows if row["item_id"] == payload["item"])
            params = {
                "case_id": payload["case"],
                "item_id": row["item_id"],
                "frozen_input_version": row["frozen_input_version"],
            }
            ack = tool.execute_scoped({**params, "action": "record_ack"}, context)
            assert ack.ok, ack.output
            submitted = tool.execute_scoped(
                {**params, "action": "submit_handoff", "evidence_ids": ["isolated-receipt"]},
                context,
            )
            assert submitted.ok, submitted.output
            replay = tool.execute_scoped(
                {**params, "action": "submit_handoff", "evidence_ids": ["isolated-receipt"]},
                context,
            )
            assert replay.ok, replay.output
            return {
                "read_state": row["state"],
                "submitted": json.loads(submitted.output),
                "replay": json.loads(replay.output),
                "pid": os.getpid(),
            }
        item = Case(
            agent,
            agent.config,
            Path(payload["workspace"]),
            thread,
            payload["parent"],
            payload["child"],
            payload["job"],
            payload["run"],
            payload["claim"],
        )
        agent._current_run_params = _scheduled_params(item, agent)
        with _bound(item):
            result, body = _guide(agent, item.child_id, "after-process-restart")
        _foreground(agent, thread, "unrelated-process-task")
        routed, route = _guide(
            agent, item.child_id, "request-to-original-parent", operation="process-route"
        )
        return {
            "ok": result.ok,
            "resolution": body["scope_resolution"],
            "routed_ok": routed.ok,
            "route": route,
        }
    root = Path(payload["root"])
    manager = SubAgentManager(root / "subagents")
    repo = RuntimeRepository(root / "runtime.db")
    manager.runtime_db = repo
    parent = manager.load(payload["parent"])
    agent = SimpleNamespace(
        subagents=manager,
        runtime_db=repo,
        config=SimpleNamespace(
            enable_subagents=True, max_subagents=10, subagent_hierarchy_max_children_per_tool_call=0
        ),
        _current_run_params=RunParams(task_attributes=parent.attributes, host_channel_actor=None),
        _current_subagent_run_id=parent.id,
    )
    set_current_subagent_context(agent, run_id=parent.id, task_attributes=parent.attributes)
    if payload.get("ready"):
        Path(payload["ready"]).touch()
        assert sys.stdin.readline().strip() == "go"
    result = CreateSubagentsTool(agent).execute(
        {"items": [{"goal": "isolated child", "agent_name": "worker-1", "role": "worker"}]}
    )
    return {
        "ok": result.ok,
        "error": result.reported_error_code,
        "effect": result.effect_outcome,
        "payload": json.loads(result.output)
        if result.output.startswith("{")
        else {"raw": result.output},
        "pid": os.getpid(),
    }


def _spawn(payload):
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import json,sys; from agent_py_agent.tests.test_orch_remaining_process_evidence import _worker; "
            "print(json.dumps(_worker(json.loads(sys.argv[1]))))",
            json.dumps(payload),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _finish(process, text=None):
    stdout, stderr = process.communicate(text, timeout=45)
    assert process.returncode == 0, stderr
    return json.loads(stdout.strip().splitlines()[-1])


def test_authorized_parent_management_survives_independent_process(case):  # noqa: F811
    _start_scheduled_run(case)
    payload = {
        "kind": "management",
        "home": case.config.my_agent_home,
        "workspace": str(case.workspace),
        "thread": case.thread.thread_id,
        "parent": case.parent_task_id,
        "child": case.child_id,
        "job": case.job,
        "run": case.run_id,
        "claim": case.claim,
    }
    result = _finish(_spawn(payload))
    assert result["ok"], result
    assert result["resolution"]["principal_run_id"] == case.parent_task_id
    assert result["resolution"]["scheduler_run_id"] == case.run_id
    assert _child_messages(case) == ["after-process-restart"]
    pending = case.agent.conversation_store.guidance.pending("task", case.parent_task_id)
    assert len(pending) == 1
    assert pending[0].message.endswith("request-to-original-parent")
    assert result["route"]["route"]["route_id"]


def test_cumulative_creation_is_atomic_between_processes_and_survives_terminal(tmp_path):
    manager = SubAgentManager(tmp_path / "subagents")
    manager.runtime_db = RuntimeRepository(tmp_path / "runtime.db")
    parent = manager.create_run(goal="parent", thought="", plan=["work"], owner="test-owner")
    from agent_py_agent.agent.agent_core.orchestration.delegation_constraints import (
        StructuredDelegationConstraints,
    )

    parent.attributes["delegation_constraints"] = StructuredDelegationConstraints(
        max_cumulative_children=1
    ).to_dict()
    manager.save(parent)
    base = {"kind": "create", "root": str(tmp_path), "parent": parent.id}
    processes = [_spawn({**base, "ready": str(tmp_path / f"ready-{i}")}) for i in range(2)]
    try:
        deadline = time.monotonic() + 30
        while not all((tmp_path / f"ready-{i}").exists() for i in range(2)):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        for process in processes:
            process.stdin.write("go\n")
            process.stdin.flush()
        results = [_finish(process) for process in processes]
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
    assert sum(row["ok"] for row in results) == 1, results
    denied = next(row for row in results if not row["ok"])
    assert denied["error"] == "SUBAGENT_CUMULATIVE_LIMIT_EXCEEDED"
    assert denied["effect"] == "not_started"
    winner = next(row for row in results if row["ok"])
    child = manager.load(winner["payload"]["created_run_ids"][0])
    child.status = "COMPLETED"
    manager.save(child)
    restarted = _finish(_spawn(base))
    assert not restarted["ok"]
    assert restarted["error"] == "SUBAGENT_CUMULATIVE_LIMIT_EXCEEDED"
    assert restarted["effect"] == "not_started"


def test_native_board_recipient_reads_and_acks_in_separate_process(case):  # noqa: F811
    from agent_py_agent.agent.agent_core.orchestration.tools.handoff_board_tool import (
        ManageHandoffBoardTool,
    )
    from agent_py_agent.agent.tooling.models import ToolInvocationContext

    agent = case.agent
    _foreground(agent, case.thread, case.parent_task_id)
    tool = ManageHandoffBoardTool(agent)
    context = ToolInvocationContext(runtime_snapshot=agent.tools.runtime_snapshot())
    service = tool._resolve_service()
    board_case = service.store.open_case({"thread_id": case.thread.thread_id})
    pub = tool.execute_scoped(
        {
            "action": "publish_item",
            "case_id": board_case.case_id,
            "request_id": "independent-recipient",
            "responsible_actor": case.child_id,
            "frozen_input_version": "version-1",
        },
        context,
    )
    assert pub.ok, pub.output
    item = json.loads(pub.output)["result"]["item"]
    before = service.store.load_handoff_item(board_case.case_id, item["item_id"])
    forbidden = tool.execute_scoped(
        {
            "action": "record_ack",
            "case_id": board_case.case_id,
            "item_id": item["item_id"],
            "frozen_input_version": "version-1",
        },
        context,
    )
    assert not forbidden.ok
    assert service.store.load_handoff_item(board_case.case_id, item["item_id"]) == before
    result = _finish(
        _spawn(
            {
                "kind": "board",
                "home": case.config.my_agent_home,
                "workspace": str(case.workspace),
                "thread": case.thread.thread_id,
                "child": case.child_id,
                "case": board_case.case_id,
                "item": item["item_id"],
            }
        )
    )
    assert result["pid"] != os.getpid()
    assert result["read_state"] == "published"
    assert result["submitted"] == result["replay"]
    final = service.store.load_handoff_item(board_case.case_id, item["item_id"])
    assert final.state == "submitted"
    assert final.recipient_ack["actor_id"] == case.child_id
    assert final.recipient_ack["frozen_input_version"] == "version-1"
    assert final.acceptance_verdict is None
