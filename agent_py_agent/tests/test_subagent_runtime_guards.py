"""LLM: Runtime guard tests for stale runner attempts and factual closeout reports.

函数/模块用途: 验证超时旧线程不能继续调用工具，顶层工具上限收口必须基于 task.json 真实状态。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core._tool_loop_service import ToolLoopService
from agent_py_agent.agent.agent_core.subagent_attempt_guard import (
    stale_subagent_attempt_message,
    stale_subagent_attempt_result,
)
from agent_py_agent.agent.agent_core.subagent_dispatch_closeout import (
    DispatchCompletionRequest,
    subagent_dispatch_completion_response,
    subagent_dispatch_final_response_guard,
    subagent_dispatch_limit_response,
)
from agent_py_agent.agent.agent_core.tool_loop_completion import (
    ToolRoundCompletionRequest,
    completion_response_after_tool_round,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.subagents.manager import SubAgentManager


# LLM: _ExplodingBackend proves stale runner attempts stop before any new model request.
# 类用途: 测试超时旧线程时，如果工具循环还调用模型就立刻失败，避免真实环境继续烧请求。
class _ExplodingBackend:
    name = "exploding_backend"

    def generate(self, prompt: str, on_chunk=None):  # noqa: ARG002
        raise AssertionError("stale runner attempt must not call the model again")


# LLM: test_stale_attempt_guard_blocks_abandoned_runner_tools covers timeout-thread leakage.
# 函数用途: 模拟 runner attempt 被 timeout abandon 后，旧线程后续 write_file 调用应被工具入口拒绝。
def test_stale_attempt_guard_blocks_abandoned_runner_tools(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    task = manager.create_run(
        goal="写一个文件",
        thought="模拟超时旧线程",
        plan=["write"],
        allowed_tools=["write_file"],
    )
    prepared = manager.prepare_runner_attempt(task.id)
    manager.abandon_runner_attempt(task.id, prepared.runner_active_attempt_id, reason="timeout")
    agent = SimpleNamespace(
        subagents=manager,
        _current_subagent_run_id=task.id,
        _current_subagent_attempt_id=prepared.runner_active_attempt_id,
    )

    result = stale_subagent_attempt_result(agent, {"tool": "write_file", "path": "out.txt"})

    assert result is not None
    assert result.ok is False
    assert result.tool == "write_file"
    assert "已被废弃或超时" in result.output


# LLM: ToolLoopService must stop stale runner threads before the next model turn.
# 函数用途: runner timeout 后旧线程进入下一轮时，应本地收口并退出，不能继续向模型发请求或消耗工具轮。
def test_stale_attempt_guard_stops_tool_loop_before_next_model_call(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    task = manager.create_run(
        goal="写一个文件",
        thought="模拟超时旧线程",
        plan=["write"],
        allowed_tools=["write_file"],
    )
    prepared = manager.prepare_runner_attempt(task.id)
    manager.abandon_runner_attempt(task.id, prepared.runner_active_attempt_id, reason="timeout")
    agent = SimpleNamespace(
        config=SimpleNamespace(max_tool_rounds=0),
        backend=_ExplodingBackend(),
        subagents=manager,
        _current_subagent_run_id=task.id,
        _current_subagent_attempt_id=prepared.runner_active_attempt_id,
    )
    params = ToolLoopExecuteParams(
        user_prompt="继续写文件",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        granted_capabilities=None,
        write_boundary=None,
        task_attributes=None,
        request_id="",
        run_id="",
        task_id=task.id,
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
    )

    message = stale_subagent_attempt_message(agent)
    final_prompt, response, tool_rounds = ToolLoopService(agent).execute(params)

    assert message is not None
    assert final_prompt == ""
    assert tool_rounds == 0
    assert response is not None
    assert "旧 runner attempt 已停止" in response.text
    assert "父级接管" in response.text


# LLM: test_dispatch_limit_response_uses_persisted_task_state prevents false-positive final summaries.
# 函数用途: 顶层收口报告必须显示真实 TIMEOUT/BLOCKED 节点，不能把产物存在误说成全部完成。
def test_dispatch_limit_response_uses_persisted_task_state(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    root = manager.create_run(
        goal="root coordinator",
        thought="coordinate",
        plan=["split"],
        agent_name="stage7-root",
        role="coordinator",
    )
    root.status = "TIMEOUT"
    root.verification_status = "UNVERIFIED"
    manager.save(root)
    child = manager.create_run(
        goal="style worker",
        thought="write",
        plan=["write"],
        parent_id=root.id,
        root_id=root.id,
        depth=1,
        agent_name="小傻妞-style",
        role="leaf_worker",
    )
    child.status = "BLOCKED"
    child.verification_status = "FAILED"
    manager.save(child)
    agent = SimpleNamespace(subagents=manager, _current_subagent_run_id="")

    response = subagent_dispatch_limit_response(agent, backend="test")

    assert response is not None
    assert "尚未完整通过" in response.text
    assert root.id in response.text
    assert child.id in response.text
    assert "TIMEOUT/UNVERIFIED" in response.text
    assert "BLOCKED/FAILED" in response.text
    assert "不要把本轮说成完成" in response.text


# LLM: test_dispatch_closeout_treats_verified_takeover_source_as_resolved covers timeout recovery closeout.
# 函数用途: 旧 run 被 takeover 后，只要接管 run 已 DONE/VERIFIED，最终收口不应再把旧 run 当 blocker。
def test_dispatch_closeout_treats_verified_takeover_source_as_resolved(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    source = manager.create_run(goal="write page", thought="old worker", plan=["write"])
    replacement = manager.create_run(goal="take over write page", thought="replacement", plan=["write"])
    source.status = "TAKEN_OVER"
    source.verification_status = "UNVERIFIED"
    source.takeover_by = replacement.id
    replacement.status = "DONE"
    replacement.verification_status = "VERIFIED"
    manager.save(source)
    manager.save(replacement)
    agent = SimpleNamespace(subagents=manager, _current_subagent_run_id="")

    response = subagent_dispatch_limit_response(agent, backend="test")

    assert response is not None
    assert "未发现阻塞状态" in response.text
    assert "blocking_run_ids: (none)" in response.text
    assert "done_verified: 2" in response.text


# LLM: Verified repair runs should cover stale failed runs for the same concrete artifact.
# 函数用途: 修复小傻妞验收通过后，旧的失败 worker 不应继续让最终收口显示“链路未完成”。
def test_dispatch_closeout_treats_verified_repair_target_as_resolved(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    source = manager.create_run(goal="create index1.html", thought="old worker", plan=["write"])
    repair = manager.create_run(goal="repair index1.html", thought="repair worker", plan=["fix"])
    _write_output_json(source.output_json, {"artifacts": [{"path": "deliverables/index1.html"}]})
    _write_output_json(repair.output_json, {"artifacts": [{"path": "deliverables/index1.html"}]})
    source.status = "BLOCKED"
    source.verification_status = "UNVERIFIED"
    repair.status = "DONE"
    repair.verification_status = "VERIFIED"
    manager.save(source)
    manager.save(repair)
    agent = SimpleNamespace(subagents=manager, _current_subagent_run_id="")

    response = subagent_dispatch_limit_response(agent, backend="test")

    assert response is not None
    assert "未发现阻塞状态" in response.text
    assert "blocking_run_ids: (none)" in response.text
    assert "done_verified: 2" in response.text


# LLM: Broad repair output files_modified refs should resolve stale originals once all touched files are verified.
# 函数用途: 一个修复代理改了多个页面后，即使自身仍停在待验收，若页面已有 VERIFIED 覆盖，也不能拖住最终账本。
def test_dispatch_closeout_treats_multi_file_repair_as_resolved_by_verified_outputs(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    repair = manager.create_run(
        goal='修复 href="index.html" 改为 index1.html，并修复 index2.html',
        thought="repair",
        plan=["fix"],
    )
    page1 = manager.create_run(goal="verify index1.html", thought="done", plan=["verify"])
    page2 = manager.create_run(goal="verify index2.html", thought="done", plan=["verify"])
    repair.result = _subagent_result_json({
        "status": "AWAITING_ACCEPTANCE",
        "files_modified": [
            {"file": "/Users/example/project/deliverables/index1.html"},
            {"file": "/Users/example/project/deliverables/index2.html"},
        ],
    })
    _write_output_json(page1.output_json, {"artifacts": [{"path": "deliverables/index1.html"}]})
    _write_output_json(page2.output_json, {"artifacts": [{"path": "deliverables/index2.html"}]})
    repair.status = "AWAITING_ACCEPTANCE"
    repair.verification_status = "NEEDS_ACCEPTANCE"
    page1.status = page2.status = "DONE"
    page1.verification_status = page2.verification_status = "VERIFIED"
    manager.save(repair)
    manager.save(page1)
    manager.save(page2)
    agent = SimpleNamespace(subagents=manager, _current_subagent_run_id="")

    response = subagent_dispatch_limit_response(agent, backend="test")

    assert response is not None
    assert "blocking_run_ids: (none)" in response.text
    assert "done_verified: 3" in response.text


# LLM: Closeout target parsing must not mistake /Users paths for the English word "use".
# 函数用途: runner 只在 SUBAGENT_RESULT 写 artifact_path 时，也能从 /Users/.../index3.html 提取真实产物名。
def test_dispatch_closeout_reads_users_path_from_result_json(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    stale = manager.create_run(goal="repair all pages", thought="old repair", plan=["fix"])
    verified = manager.create_run(goal="verify index3.html", thought="done", plan=["verify"])
    stale.result = _subagent_result_json({
        "artifact_path": "/Users/example/my-claude-code/work/deliverables/index3.html",
    })
    _write_output_json(verified.output_json, {"artifacts": [{"path": "deliverables/index3.html"}]})
    stale.status = "AWAITING_ACCEPTANCE"
    stale.verification_status = "NEEDS_ACCEPTANCE"
    verified.status = "DONE"
    verified.verification_status = "VERIFIED"
    manager.save(stale)
    manager.save(verified)
    agent = SimpleNamespace(subagents=manager, _current_subagent_run_id="")

    response = subagent_dispatch_limit_response(agent, backend="test")

    assert response is not None
    assert "blocking_run_ids: (none)" in response.text


# LLM: Final closeout must scope reused workspaces to the current root turn.
# 函数用途: 旧 E2E run 留在同一 subagent_workspace 时，本轮只调度新 run，最终报告不能被旧失败节点污染。
def test_dispatch_closeout_ignores_unseen_historical_runs(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    old = manager.create_run(goal="old failed run", thought="old", plan=["old"])
    current = manager.create_run(goal="current page", thought="new", plan=["write"])
    old.status = "BLOCKED"
    old.verification_status = "FAILED"
    current.status = "DONE"
    current.verification_status = "VERIFIED"
    manager.save(old)
    manager.save(current)
    agent = SimpleNamespace(
        subagents=manager,
        _current_subagent_run_id="",
        _orchestration_run_ids_seen={current.id},
    )

    response = subagent_dispatch_limit_response(agent, backend="test")

    assert response is not None
    assert "blocking_run_ids: (none)" in response.text
    assert current.id in response.text
    assert old.id not in response.text


# LLM: Explicit quality-role requirements should prevent worker-only deterministic closeout.
# 函数用途: 用户明确要求测试子代理和验收子代理时，只有 worker DONE 不能直接完成。
def test_dispatch_completion_waits_for_explicit_quality_roles(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    worker = manager.create_run(goal="write three pages", thought="worker", plan=["write"])
    worker.status = "DONE"
    worker.verification_status = "VERIFIED"
    manager.save(worker)
    agent = SimpleNamespace(subagents=manager, _current_subagent_run_id="")

    response = subagent_dispatch_completion_response(
        DispatchCompletionRequest(
            agent=agent,
            params=_tool_loop_params(
                "请派小傻妞做页面，完成后必须继续创建测试子代理和验收子代理收口。"
            ),
            before_executed_count=0,
            backend="test",
        )
    )

    assert response is None


# LLM: Parent acceptance repair gets one model turn before deterministic closeout.
# 函数用途: 父级验收失败时先给 root 一次创建修复小傻妞的机会；同一阻塞重复出现才事实收口，避免无限等。
def test_dispatch_round_grants_one_parent_acceptance_repair_turn(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    task = manager.create_run(goal="deliver web artifact", thought="await repair", plan=["repair"])
    task.status = "AWAITING_ACCEPTANCE"
    task.verification_status = "NEEDS_ACCEPTANCE"
    manager.save(task)
    _write_json(task.reports_dir, "acceptance_review.json", {"decision": "REJECT", "ok": False})
    agent = SimpleNamespace(subagents=manager, _current_subagent_run_id="")
    params = _tool_loop_params("请安排小傻妞完成并验收。")

    first = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL dispatch_subagents]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    second = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL dispatch_subagents]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )

    assert first is None
    assert params.tool_context
    assert "parent_acceptance_repair_advice.suggested_tool_call" in params.tool_context[-1]
    assert second is not None
    assert "尚未完整通过" in second.text
    assert "不能按完成汇报" in second.text
    assert task.id in second.text


# LLM: Final model text must not claim completion when a requested acceptor role never ran.
# 函数用途: root 已经调度 worker/tester 但缺少用户明确要求的验收子代理时，最终回复守卫必须拦住口头完成。
def test_final_response_guard_blocks_missing_explicit_acceptor_role(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    coordinator = manager.create_run(
        goal="coordinate page",
        thought="coordinator",
        plan=["coordinate"],
        role="coordinator",
        attributes={"required_qa_roles": ["acceptor"]},
    )
    worker = manager.create_run(goal="write page", thought="worker", plan=["write"], role="worker")
    tester = manager.create_run(goal="test page", thought="tester", plan=["test"], role="tester")
    for task in (coordinator, worker, tester):
        task.status = "DONE"
        task.verification_status = "VERIFIED"
        manager.save(task)
    agent = SimpleNamespace(
        subagents=manager,
        _current_subagent_run_id="",
        _current_user_prompt="请安排小傻妞做页面，最后必须派验收子代理收口。",
        _orchestration_run_ids_seen={coordinator.id, worker.id, tester.id},
    )

    response = subagent_dispatch_final_response_guard(
        agent,
        ModelResponse(text="页面已经全部完成。", backend="test"),
        executed_tools=["dispatch_subagents"],
    )

    assert response is not None
    assert "缺少" in response.text
    assert "acceptor" in response.text
    assert "create_subagents" in response.text
    assert "页面已经全部完成" not in response.text


def _write_output_json(path: str, payload: dict[str, object]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


# LLM: _write_json writes compact report fixtures beside task runtime files.
# 函数用途: 给 runtime guard 测试写最小 JSON 报告，不重复路径创建样板。
def _write_json(root: str, filename: str, payload: dict[str, object]) -> None:
    path = Path(root) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _subagent_result_json(payload: dict[str, object]) -> str:
    return "[SUBAGENT_RESULT]\n" + json.dumps(payload, ensure_ascii=False) + "\n[/SUBAGENT_RESULT]"


def _tool_loop_params(prompt: str) -> ToolLoopExecuteParams:
    return ToolLoopExecuteParams(
        user_prompt=prompt,
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        granted_capabilities=None,
        write_boundary=None,
        task_attributes=None,
        request_id="req",
        run_id="run",
        task_id="task",
        one_shot_tool_calls=set(),
        executed_tools=["dispatch_subagents"],
        archive_tool_calls=[],
    )
