"""原AG服务+真实outbox组件；HTTP只替换运输和已认证夹具，无模型或真实IM。"""
import json
import threading
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.auth.models import Permission, Role
from agent_py_agent.agent.capability import agents_write_service
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.gateway_parts import http_handlers
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.cli.chat_parts.control_runtime import _gateway_control_result_from_body
from agent_py_agent.cli.chat_parts.tui_control_delivery import (
    TuiControlOperationEntry,
    TuiControlOperationReconciler,
)
from agent_py_agent.tests.test_agents_controls import host
from agent_py_agent.tests.test_agents_write_service import source


# LLM: 沿原已认证HTTP夹具和真实AG服务，只替换发送管道；不造MP/source或runtime authority。
# 函数用途: 让单测直接读原控制响应，并把原字典交给真实客户端解析器。
@pytest.fixture
def transport(host, source, tmp_path, monkeypatch):  # noqa: F811
    replies = []
    handler = SimpleNamespace(
        _auth_middleware=object(),
        _send_json=lambda status, body: replies.append((status, body)),
    )
    monkeypatch.setattr(http_handlers, "_request_identity", lambda handler: ("fixture-user", Permission(Role.ADMIN, "fixture-user", ["chat"])))
    server = SimpleNamespace(agent=host, paths=gateway_paths_from_root(tmp_path / "gateway"))

    # LLM: scope/message_id来自当前原夹具入口；AG新授权仍仅由生产HTTP issuer形成。
    # 函数用途: 通过真实公开HTTP控制分派执行一次测试命令，不绕原nonce服务。
    def send(text, message_id):
        scope = GatewayControlScope(
            "fixture-user", "chat", "conversation", {"message_id": message_id},
            resolved_owner=OwnerIdentity.local_main(),
        )
        http_handlers._handle_persistent_control_operation(
            handler, server, command=parse_conversation_control(text),
            command_text=text, scope=scope,
        )
        code, body = replies[-1]
        assert code in {200, 202}
        assert body["status"] == "control"
        return _gateway_control_result_from_body("agents", body)

    return send


# LLM: stopEvent阻止异步runner，只直接调用原_reconcile_one；所有nonce/来源/解析/outbox是真正产品函数。
# 函数用途: 建立可确定观察一次对账的真实TUI组件，保持原终态门不替换。
def reconciler(tmp_path, submit, completions):
    stop = threading.Event()
    stop.set()
    return TuiControlOperationReconciler(
        path=tmp_path / "tui-outbox.json", submit=submit,
        status=lambda operation_id: pytest.fail("本次已经收口，不应需要GET"),
        on_restore=lambda entry: None,
        on_complete=lambda entry, result: completions.append(result),
        on_terminal_unknown=lambda entry, result: pytest.fail("已返回业务结果，不是运输未知"),
        on_conflict=lambda entry: pytest.fail("合法新输入不应身份冲突"),
        on_error=lambda error: pytest.fail(type(error).__name__),
        stop_event=stop, initial_delay=0.05, maximum_delay=0.05,
    )


@pytest.mark.parametrize("text,domain_state", [("/agents", "loaded"), ("/agents reset", "preview")])
def test_original_agents_http_result_finishes_real_outbox(transport, source, tmp_path, text, domain_state):  # noqa: F811
    completions = []
    observed = []

    def submit(entry):
        result = transport(entry.command_text, entry.message_id)
        assert result.ok
        assert result.details["state"] == domain_state
        observed.append(result)
        return result

    r = reconciler(tmp_path, submit, completions)
    entry = TuiControlOperationEntry("control-original", text, "agents")
    r.enqueue(entry)
    r._reconcile_one(entry)
    assert source.default_path.read_text() == "原始用户约定\n"
    assert len(observed) == 1
    assert completions == observed, "原合法AG响应必须结束运输并进入原显示callback"
    assert json.loads(r.path.read_text())["entries"] == {}
    assert observed[0].control_state == "completed"
    assert observed[0].operation_id.startswith("gwctl-msg-")


def test_confirm_response_loss_replays_original_outer_result(transport, source, tmp_path, monkeypatch):  # noqa: F811
    preview = transport('/agents write "已由原确认写入"', "preview")
    assert preview.details["state"] == "preview"
    confirm_text = preview.details["confirm_command"]
    completions = []
    actual_writes = []
    original_write = source.write_text

    # LLM: 计数器仅包原A写函数，权限/同FD/CAS及返回都保持原调用；不补写任何业务数据。
    # 函数用途: 核对丢HTTP响应后的相同控制ID不会再次写入正文。
    def tracked_write(content, *, authorization, context):
        actual_writes.append(authorization.request_id)
        return original_write(content, authorization=authorization, context=context)

    monkeypatch.setattr(source, "write_text", tracked_write)
    submissions = []

    def submit(entry):
        result = transport(entry.command_text, entry.message_id)
        submissions.append(result)
        if len(submissions) == 1:
            assert result.ok and result.details["state"] == "completed"
            raise OSError("simulated lost transport reply after original durable effect")
        return result

    r = reconciler(tmp_path, submit, completions)
    entry = TuiControlOperationEntry("control-confirm", confirm_text, "agents")
    r.enqueue(entry)
    r._reconcile_one(entry)
    assert completions == []
    assert source.default_path.read_text() == "已由原确认写入"
    assert actual_writes == ["control-confirm"]
    pending = r._read_entries()["control-confirm"]
    assert pending.message_id == entry.message_id and pending.command_text == confirm_text
    r._reconcile_one(pending)
    assert actual_writes == ["control-confirm"]
    assert len(completions) == 1, "运输重试必须读回原成功result，不能只返回nonce重放失败"
    assert completions[0].ok and completions[0].details["state"] == "completed"
    assert json.loads(r.path.read_text())["entries"] == {}
    assert submissions[0].operation_id == submissions[1].operation_id


def test_consumed_unknown_remains_domain_unknown_and_finishes_transport(transport, source, tmp_path, monkeypatch):  # noqa: F811
    preview = transport('/agents write "原未知副作用"', "preview-unknown")
    original_save = agents_write_service.save_record

    # LLM: 故障仅落在原nonce完成回执；副作用和消费记录仍由原service/A执行，保旧unknown不重放合同。
    # 函数用途: 重放原真实receipt故障，让最终写入发生但业务结果保持未知。
    def fail_completed(home, record):
        if record["state"] == "completed":
            raise OSError("original nonce completion receipt fault")
        return original_save(home, record)

    monkeypatch.setattr(agents_write_service, "save_record", fail_completed)
    completions = []
    observed = []

    def submit(entry):
        result = transport(entry.command_text, entry.message_id)
        assert not result.ok and result.details["state"] == "consumed_unknown"
        assert result.delivery_status == "accepted"
        observed.append(result)
        return result

    r = reconciler(tmp_path, submit, completions)
    entry = TuiControlOperationEntry("control-unknown", preview.details["confirm_command"], "agents")
    r.enqueue(entry)
    r._reconcile_one(entry)
    assert source.default_path.read_text() == "原未知副作用"
    assert completions == observed, "内层unknown已返回不应令客户端重发同一副作用"
    assert json.loads(r.path.read_text())["entries"] == {}
    assert not completions[0].ok and completions[0].details["state"] == "consumed_unknown"
    assert completions[0].control_state == "completed"
