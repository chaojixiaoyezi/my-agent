# LLM: 保留原页面生产、Executor、归档、配置与 worker；只替换网络和模型供应商，不让展示建议伪装成正式来源。
# 模块用途: 离线验证外部材料提示的真实接线、独立默认、用户/agent 设置和超时取消边界。
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import threading
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model.call_runtime import model_call_ledger
from agent_py_agent.agent.agent_core.tool_call_archive_record import archive_tool_output_projection
from agent_py_agent.agent.agent_core.tool_context import external_material_order as module
from agent_py_agent.agent.backends.typesafe_decision import TypesafeDecisionBackend
from agent_py_agent.agent.backends.typesafe_decision_wire import parse_typesafe_response
from agent_py_agent.agent.runtime_context import (
    restore_current_subagent_context,
    set_current_subagent_context,
)
from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.agent.settings.decision_settings import (
    execute_decision_settings_operation as settings,
)
from agent_py_agent.agent.settings.decision_settings_schema import decision_field_scopes
from agent_py_agent.agent.tooling import web_fetch_runtime
from agent_py_agent.agent.tooling.cancellation import ToolCancelled
from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool
from agent_py_agent.agent.tooling.web import WebFetchTool
from agent_py_agent.cli.chat_parts import tui_decision_menu
from agent_py_agent.tests._tool_runtime_harness import (
    execute_canonical_test_call,
    runtime_snapshot_for_tools,
)
from agent_py_agent.tests.test_decision_external_material_order import install
from agent_py_agent.tests.test_decision_external_material_order import prepared as prepared
from agent_py_agent.tests.test_decision_model_profiles import decision
from agent_py_agent.tests.test_decision_settings import host_at, patch
from agent_py_agent.tests.test_tui_decision_menu import (
    Gateway,
    choose,
    open_scope,
    press,
    running,
    visible,
)


# LLM: 本 helper 只造 HTTP 响应字节，保留真实抓取的解析、网络范围校验、归档和 canonical 转换。
# 函数用途: 为两页提供有界内存响应，不开端口、不访问真实网络。
def install_pages(monkeypatch):
    responses = []
    for index in (1, 2):
        body = io.BytesIO(f"<html><title>来源 {index}</title><body>原文 {index}</body></html>".encode())
        responses.append(SimpleNamespace(status=200, headers={"Content-Type": "text/html; charset=utf-8"},
                                         read=body.read, close=body.close))
    queue = iter(responses)
    monkeypatch.setattr(web_fetch_runtime, "_send_pinned", lambda *_args, **_kwargs: (SimpleNamespace(close=lambda: None), next(queue)))


def test_real_extract_executor_archive_sources_reach_advisor_without_new_storage(prepared, monkeypatch):
    host, old_record, _ = prepared
    host.config.tool_output_externalize_min_chars = 10_000_000
    host.config.tool_output_preview_chars = 128
    tool = WebFetchTool(max_chars=512, timeout=2, resolver=lambda _host: ("93.184.216.34",), artifact_root=host.root / "pages")
    params = replace(old_record.params, tool_runtime_snapshot=runtime_snapshot_for_tools({"web_fetch": tool}, run_id="run-1"))
    install_pages(monkeypatch)
    execution = execute_canonical_test_call(
        host.root, tools={"web_fetch": tool}, tool_name="web_fetch",
        arguments={"mode": "extract", "urls": ["https://example.test/one", "https://example.test/two"]},
        run_id="run-1", call_id="fetch-1", attempt_id="attempt-1",
        output_archiver=lambda call, outcome: archive_tool_output_projection(host, params, call, outcome),
    )
    assert execution.result.ok, execution.result.to_dict()
    record = replace(old_record, params=params, call=execution.call, result=execution.result)
    archive = record.result.metadata["archive_output_record"]
    assert archive["output_externalized"] is True
    pages = record.result.metadata["handler_details"]["pages"]
    for page in pages:
        assert hashlib.sha256(Path(page["artifact_ref"]).read_bytes()).hexdigest() == page["content_hash"]
    before_files = {str(path): path.read_bytes() for path in host.root.rglob("*") if path.is_file()}
    before_result, before_archive = deepcopy(record.result.to_dict()), deepcopy(archive)
    calls, _ = install(monkeypatch)
    assert "2 → 1" in module.external_material_order_hint(host, record, archive)
    assert len(calls) == 1
    assert [row["content_hash"] for row in calls[0][2]["state"]["pages"]] == [page["content_hash"] for page in pages]
    assert before_result == record.result.to_dict() and before_archive == archive
    assert {str(path): path.read_bytes() for path in host.root.rglob("*") if path.is_file()} == before_files


# LLM: 决策连接、owner/thread 设置、有界 worker 和用量仍走生产服务；唯一 fake 是供应商的同步 decide。
# 函数用途: 返回稳定优先级或指定异常，验证真实服务如何区分可选故障与用户停止。
def install_provider(monkeypatch, *, error=None, cancel=None):
    calls = []
    def invoke(backend, request, *, deadline):
        calls.append((request, deadline))
        if cancel:
            cancel.cancel("user-stop")
            raise ToolCancelled("user-stop")
        if error:
            raise error
        payload = request.payload(backend.model_name)
        answers = {}
        for key, spec in payload["questions"].items():
            choice = "later" if key == "page_1" else "first"
            answers[key] = {"type": "choice", "choice": choice, "confidence": 1.0,
                            "probabilities": {option: float(option == choice) for option in spec["criteria"]}}
        return parse_typesafe_response(request, backend.model_name, {
            "model": "fixture-decision", "answers": answers, "usage": {"input_tokens": 29}})
    monkeypatch.setattr(TypesafeDecisionBackend, "decide", invoke)
    return calls


# LLM: 当前会话来自真实 ConversationStore，决策配置只经原 CAS 服务；参数对象保持原 ToolLoopExecuteParams。
# 函数用途: 把 canonical 批次放入可调用原决策服务的临时 owner/thread，不写任何正式用户配置。
def configured(prepared, tmp_path, mode):
    _, record, archive = prepared
    host = host_at(tmp_path)
    key, _ = decision(host)
    thread = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
    patch(host, {"enabled": True, "profile_id": key, f"points.{module._POINT}.mode": mode})
    params = replace(record.params, task_attributes={"agent_thread_id": thread.thread_id})
    return host, replace(record, params=params), archive


@pytest.mark.parametrize("mode", ["off", "observe", "apply"])
def test_original_service_worker_binding_and_usage(prepared, tmp_path, monkeypatch, mode):
    host, record, archive = configured(prepared, tmp_path, mode)
    calls = install_provider(monkeypatch)
    hint = module.external_material_order_hint(host, record, archive)
    assert bool(hint) is (mode == "apply")
    assert len(calls) == (0 if mode == "off" else 1)
    if calls:
        binding = calls[0][0].binding
        assert binding.point == module._POINT and binding.run_id == record.call.run_id
        assert binding.thread_id == record.params.task_attributes["agent_thread_id"]
        assert binding.source_refs == (archive["scoped_call_id"],)
        ledger = model_call_ledger(host).records()
        assert len(ledger) == 1 and ledger[0].metadata["purpose"] == "decision"
        assert ledger[0].metadata["thread_id"] == binding.thread_id


def test_optional_provider_timeout_preserves_original_and_does_not_cancel_user(prepared, tmp_path, monkeypatch):
    host, record, archive = configured(prepared, tmp_path, "apply")
    calls = install_provider(monkeypatch, error=TimeoutError("optional decision exhausted"))
    before = record.result.to_dict()
    assert module.external_material_order_hint(host, record, archive) == ""
    assert len(calls) == 1 and not record.params.cancellation_token.cancelled
    assert before == record.result.to_dict()


def test_real_service_propagates_explicit_user_cancellation(prepared, tmp_path, monkeypatch):
    host, record, archive = configured(prepared, tmp_path, "apply")
    install_provider(monkeypatch, cancel=record.params.cancellation_token)
    with pytest.raises((ToolCancelled, InterruptedError)):
        module.external_material_order_hint(host, record, archive)


def test_real_optional_worker_deadline_does_not_cancel_host(prepared, tmp_path, monkeypatch):
    host, record, archive = configured(prepared, tmp_path, "apply")
    patch(host, {f"points.{module._POINT}.timeout_seconds": 0.1, "stage_timeout_seconds": 0.5})
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def wait_for_deadline(_backend, _request, *, deadline):
        assert deadline > 0
        entered.set()
        try:
            release.wait(1)
            raise TimeoutError("provider finished after the optional deadline")
        finally:
            finished.set()
    monkeypatch.setattr(TypesafeDecisionBackend, "decide", wait_for_deadline)
    try:
        assert module.external_material_order_hint(host, record, archive) == ""
        assert entered.is_set() and not record.params.cancellation_token.cancelled
    finally:
        release.set()
        assert finished.wait(1)


def test_config_defaults_and_original_agent_setting_tool_share_thread_override(tmp_path):
    config = load_config(Path(__file__).parents[1] / "config" / "agent_config.yaml")
    defaults = AgentConfig()
    for suffix in ("mode", "timeout_seconds", "profile_id"):
        name = f"decision_{module._POINT}_{suffix}"
        assert getattr(config, name) == getattr(defaults, name) == ("off" if suffix == "mode" else None)
        assert decision_field_scopes()[f"points.{module._POINT}.{suffix}"] == ["owner", "thread"]
    host = host_at(tmp_path)
    key, _ = decision(host)
    thread = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
    patch(host, {"enabled": True, f"points.{module._POINT}.mode": "observe"})
    previous = set_current_subagent_context(host, run_id="current-run", task_attributes={"agent_thread_id": thread.thread_id})
    try:
        current = settings(host, "read", {"scope": "thread"}, thread_id=thread.thread_id)
        outcome = UserConfigTool(host).execute({"action": "decision_patch", "scope": "thread",
            "expected_revision": current["revision"], "changes": {
                f"points.{module._POINT}.mode": "apply", f"points.{module._POINT}.timeout_seconds": 0.75,
                f"points.{module._POINT}.profile_id": key}})
        assert outcome.ok, outcome.output
        view = settings(host, "read", {"scope": "thread"}, thread_id=thread.thread_id)
        point = view["effective"]["points"][module._POINT]
        assert point["mode"] == "apply" and point["timeout_seconds"] == 0.75 and point["profile_id"] == key
        assert settings(host, "read", {})["effective"]["points"][module._POINT]["mode"] == "observe"
        assert not hasattr(host, "_model_call_ledger")
    finally:
        restore_current_subagent_context(host, previous)


def test_original_tui_can_edit_external_material_thread_mode(tmp_path):
    async def scenario():
        gateway = Gateway(tmp_path)
        async with running(tmp_path, gateway) as ui:
            await open_scope(ui, thread=True)
            # 实验能力是新的会话通用字段，接入点菜单跟在五个通用字段之后。
            await choose(ui, 5)
            view = settings(gateway.host, "read", {"scope": "thread"}, thread_id=gateway.thread.thread_id)
            points = [key for key in tui_decision_menu._POINTS if f"points.{key}.mode" in tui_decision_menu._fields(view)]
            assert "外部材料阅读优先级" in visible(ui.app)
            await choose(ui, points.index(module._POINT))
            await choose(ui, 0)
            await press(ui, b"\x1b[B\x1b[B\r")
            result = settings(gateway.host, "read", {"scope": "thread"}, thread_id=gateway.thread.thread_id)
            assert result["overrides"]["thread"][f"points.{module._POINT}.mode"] == "apply"
            assert not any(name in {"decision_probe", "select", "set_default"} for name, _payload in gateway.calls)
    asyncio.run(scenario())
