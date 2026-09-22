"""原宿主命令审批接续合同；假用户决定与假工具，不计真实 TUI 验收。"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest

from agent_py_agent.agent.contracts.tool_approval import ToolApprovalDecision
from agent_py_agent.agent.runtime_db.host_command_execution import (
    execute_host_command,
    query_host_command,
)
from agent_py_agent.agent.tooling.cancellation import CancellationToken
from agent_py_agent.agent.tooling.models import ApprovalPolicy, ToolHandlerOutcome
from agent_py_agent.tests.test_host_command_execution import case


# LLM: 测试使用原请求、执行器和操作账，只把管理工具的免确认改为明确询问，不绕过宿主审批接线。
# 函数用途: 构造必须批准才能增加调用计数的同一工具。
def approval_case(tmp_path):
    repo, request, tool, prepare = case(tmp_path)
    tool.runtime_policy = replace(tool.runtime_policy, approval_policy=ApprovalPolicy("always"))
    return repo, request, tool, prepare


def test_approve_resumes_exact_pending_call_and_persists_single_execution(tmp_path):
    repo, request, tool, prepare = approval_case(tmp_path)
    seen = []
    def approve(payload, **kwargs):
        assert tool.calls == 0 and query_host_command(repo, request)["state"] == "running"
        assert payload["binding"]["operation_id"] == request.operation_id
        assert payload["binding"]["args_hash"] == "sha256:" + request.input_digest
        assert all(option["decision"] != "approved_session" for option in payload["options"])
        seen.append(payload)
        return ToolApprovalDecision(payload["permission_id"], "approved")
    result = execute_host_command(repo, request, prepare, request_permission=approve)
    assert result["state"] == "succeeded" and tool.calls == 1, result
    replay = execute_host_command(repo, request, prepare, request_permission=lambda *_: pytest.fail("不得重复询问"))
    assert replay["state"] == "succeeded" and tool.calls == 1 and len(seen) == 1
    with repo._runtime_connection() as conn:
        assert conn.execute("SELECT count(*) FROM tool_operations").fetchone()[0] == 1


@pytest.mark.parametrize("decision,expected", [("denied", "APPROVAL_REJECTED"), ("cancelled", "CANCELLED"),
                                            ("unavailable", "APPROVAL_REQUIRED"), ("approved_session", "APPROVAL_REQUIRED")])
def test_denial_cancel_unavailable_and_unsupported_session_never_execute(tmp_path, decision, expected):
    repo, request, tool, prepare = approval_case(tmp_path)
    result = execute_host_command(repo, request, prepare, request_permission=lambda value, **_: {
        "permission_id": value["permission_id"], "decision": decision,
    })
    assert result["error_code"] == expected and tool.calls == 0, result
    replay = execute_host_command(repo, request, prepare, request_permission=lambda *_: pytest.fail("终态不能再询问"))
    assert replay == result
    with repo._runtime_connection() as conn:
        assert conn.execute("SELECT count(*) FROM tool_operations").fetchone()[0] == 0


@pytest.mark.parametrize("consumer", [None, lambda value, **_: {"permission_id": "other", "decision": "approved"},
                                     lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disconnected"))])
def test_missing_broken_or_mismatched_consumer_keeps_unstarted_result(tmp_path, consumer):
    repo, request, tool, prepare = approval_case(tmp_path)
    result = execute_host_command(repo, request, prepare, request_permission=consumer)
    assert result["state"] == "approval_required" and tool.calls == 0, result


def test_duplicate_while_approval_waits_observes_original_attempt_without_new_prompt(tmp_path):
    repo, request, tool, prepare = approval_case(tmp_path)
    entered, release = Event(), Event()
    def approve(value, **kwargs):
        entered.set()
        assert release.wait(5)
        return {"permission_id": value["permission_id"], "decision": "approved"}
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(execute_host_command, repo, request, prepare, request_permission=approve)
        try:
            assert entered.wait(5)
            same = execute_host_command(repo, request, prepare, request_permission=lambda *_: pytest.fail("重复审批"))
            assert same["state"] == "running" and tool.calls == 0
        finally:
            release.set()
        assert waiting.result(timeout=5)["state"] == "succeeded"
    assert tool.calls == 1


def test_late_approval_cannot_override_cancellation_or_original_prehandler_gate(tmp_path):
    repo, request, tool, prepare = approval_case(tmp_path)
    token = CancellationToken()
    def prepared(binding):
        return replace(prepare(binding), cancellation_token=token)
    def cancel(value, **kwargs):
        token.cancel()
        return {"permission_id": value["permission_id"], "decision": "approved"}
    result = execute_host_command(repo, request, prepared, request_permission=cancel)
    assert result["error_code"] == "CANCELLED" and tool.calls == 0, result
    second = replace(request, request_id="second")
    def denied(binding):
        call = replace(prepare(binding).call, operation_id=second.operation_id)
        return replace(prepare(binding), call=call, pre_handler_gate=lambda _: ToolHandlerOutcome(
            call.tool_name, False, "固定执行权已经撤销", error_code="PLUGIN_REVOKED", effect_outcome="not_started"))
    result = execute_host_command(repo, second, denied, request_permission=lambda value, **_: {
        "permission_id": value["permission_id"], "decision": "approved",
    })
    assert result["error_code"] == "PLUGIN_REVOKED" and tool.calls == 0, result


def test_approval_transport_error_after_cancellation_remains_cancelled(tmp_path):
    repo, request, tool, prepare = approval_case(tmp_path)
    token = CancellationToken()
    def broken(value, **kwargs):
        token.cancel()
        raise OSError("fixture approval disconnected")
    result = execute_host_command(repo, request, lambda binding: replace(prepare(binding), cancellation_token=token),
                                  request_permission=broken)
    assert result["error_code"] == "CANCELLED" and tool.calls == 0, result
