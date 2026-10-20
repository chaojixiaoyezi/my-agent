# LLM: 真实薄CLI、回环HTTP、原C/lease/Root与准入，只有供应商使用既有fake；不接真实配置、模型或外部服务。
# 模块用途: 验本地普通输入经宿主冻结owner后进入Count2准入，防止filequeue绕过认证准备。
from dataclasses import replace
from types import SimpleNamespace

from agent_py_agent.agent.gateway_parts.io import read_json_file
from agent_py_agent.agent.gateway_parts.request_owner_binding import frozen_request_owner
from agent_py_agent.agent.gateway_parts.request_worker import _durable_request_owner
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.cli.chat_client_context import GatewayChatClientAgent
from agent_py_agent.cli.chat_parts.gateway_client import ChatRequestContent, submit_chat_request
from agent_py_agent.tests.test_gateway_c_http_wait import c_http


def test_real_thin_tui_local_ask_reaches_actual_count2_owner_admission(tmp_path, monkeypatch):
    with c_http(tmp_path, monkeypatch) as (
        agent,
        backend,
        gate,
        _control,
        _runtime,
        server,
        _close,
    ):
        client = GatewayChatClientAgent(
            SimpleNamespace(),
            replace(agent.config, gateway_port=server.server.server_address[1]),
            agent.root,
            [agent.root],
            agent.home_paths,
        )
        allocated = []
        request_id, _chunk, _response = submit_chat_request(
            gate.paths,
            ChatRequestContent(
                prompt="读取已有材料并说明结果",
                inject=[],
                prompt_files=[],
                save=True,
                show_prompt=False,
                resume_context=None,
                chat_session_id="local-transport",
            ),
            client,
            on_request_allocated=allocated.append,
        )
        payload = read_json_file(gate.paths.inbox / (request_id + ".json"))
        assert allocated == [request_id]
        assert frozen_request_owner(payload) == OwnerIdentity.local_main()
        assert _durable_request_owner(gate, payload) == OwnerIdentity.local_main()
        assert payload["metadata"]["gateway_admin_binding_version"] == "none"
        assert backend.calls == 0
        from agent_py_agent.agent.gateway_parts.multiprocess_admission import (
            session_scope_for_request,
        )
        from agent_py_agent.agent.gateway_parts.request_worker import AdmissionLimits

        decision = gate.admit(
            gate.paths.inbox / (request_id + ".json"),
            (OwnerIdentity.local_main(), session_scope_for_request(payload)),
            AdmissionLimits(8, 8),
        )
        assert decision["status"] == "admitted"


# LLM: 两Count共真实临时HTTP；Count2复用原C资源/短租约，Count0只用原Agent。全部本机临时路径，finally关闭监听器。
# 函数用途: 为同一客户端运输合同提供原两种宿主，不启动W、真实模型或飞书。
def _http_case(tmp_path, monkeypatch, count):
    from contextlib import contextmanager

    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts.http_service import (
        GatewayHTTPServer,
        GatewayHTTPServerParams,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths
    from agent_py_agent.agent.gateway_parts.queue_service import ensure_gateway_folders
    from agent_py_agent.agent.settings import AgentConfig

    @contextmanager
    def case():
        if count == 2:
            with c_http(tmp_path, monkeypatch) as values:
                yield values[0], values[2], values[5]
            return
        agent = SimpleAgent(
            AgentConfig(
                my_agent_home=str(tmp_path / "home"),
                model_backend="echo",
                gateway_per_user_owner_scoping=True,
            ),
            tmp_path / "workspace",
        )
        paths = gateway_paths(agent)
        ensure_gateway_folders(paths)
        server = GatewayHTTPServer(0, paths, params=GatewayHTTPServerParams(agent=agent))
        server.start()
        try:
            yield agent, None, server
        finally:
            server.stop()

    return case()


# LLM: 只投影本fixture既有宿主与端口；owner来自真实结构对象，不手填host_request_owner。
# 函数用途: 构造不带模型后端的原薄客户端。
def _client(agent, server, *, owner=None):
    return GatewayChatClientAgent(
        SimpleNamespace(),
        replace(agent.config, gateway_port=server.server.server_address[1]),
        agent.root,
        [agent.root],
        agent.home_paths,
        owner_identity=owner,
    )


# LLM: 捕获的是实际HTTP工作片已通过认证后的payload，调用仍委托原prepare/commit；不改变owner或执行权限。
# 函数用途: 记录所有输入在C/Count0原入队前是否已正确准备。
def _capture_prepared(monkeypatch):
    from agent_py_agent.agent.gateway_parts import http_handlers

    seen = []
    original = http_handlers._prepare_http_gateway_input

    def capture(server, scope, payload):
        result = original(server, scope, payload)
        seen.append(result)
        return result

    monkeypatch.setattr(http_handlers, "_prepare_http_gateway_input", capture)
    return seen


import pytest


@pytest.mark.parametrize("count", [0, 2])
@pytest.mark.parametrize(
    "owner",
    [
        OwnerIdentity.local_main(),
        OwnerIdentity.provider_user("local", "alice"),
        OwnerIdentity.provider_group("feishu", "group-test"),
    ],
)
def test_local_submit_both_counts_freeze_only_authenticated_owner(
    tmp_path, monkeypatch, count, owner
):
    from agent_py_agent.agent.gateway_parts.input_delivery_service import read_gateway_input_receipt

    with _http_case(tmp_path, monkeypatch, count) as (agent, _gate, server):
        seen = _capture_prepared(monkeypatch)
        client = _client(agent, server, owner=owner)
        result = submit_chat_request(
            server.paths,
            ChatRequestContent(
                prompt="同源材料",
                inject=["soft"],
                prompt_files=[],
                save=False,
                show_prompt=True,
                resume_context=False,
                chat_session_id="owner-test",
                client_message_id="same-message",
            ),
            client,
        )
        receipt = read_gateway_input_receipt(server.paths, result[0])
        assert receipt is not None
        assert frozen_request_owner(receipt.prepared_request) == owner
        assert len(seen) == 1 and seen[0]["include_prompt"] is True
        assert seen[0]["save"] is False and seen[0]["inject"] == ["soft"]
        assert seen[0]["resume_context"] is False
        again = submit_chat_request(
            server.paths,
            ChatRequestContent(
                prompt="同源材料",
                inject=["soft"],
                prompt_files=[],
                save=False,
                show_prompt=True,
                resume_context=False,
                chat_session_id="owner-test",
                client_message_id="same-message",
            ),
            client,
        )
        assert again[0] == result[0] and len(seen) == 1


@pytest.mark.parametrize("count", [0, 2])
@pytest.mark.parametrize("intent", ["queue", "auto"])
def test_delivery_intent_controls_active_route_through_existing_host_leaf(
    tmp_path, monkeypatch, count, intent
):
    from agent_py_agent.agent.gateway_parts import http_handlers
    from agent_py_agent.tests.test_gateway_c_http_wait import request

    with _http_case(tmp_path, monkeypatch, count) as (agent, _gate, server):
        called = []
        if count == 0:
            original = http_handlers._route_ask_to_active_turn
            monkeypatch.setattr(
                http_handlers,
                "_route_ask_to_active_turn",
                lambda *a, **kw: called.append(kw["resolved_owner"]) or None,
            )
        else:
            from agent_py_agent.agent.gateway_parts.multiprocess_input_receipt import (
                CoordinatorInputReceiptSource,
            )

            original = CoordinatorInputReceiptSource.route_fresh

            def route(self, receipt, scope, *, allow_active):
                called.append(allow_active)
                return original(self, receipt, scope, allow_active=allow_active)

            monkeypatch.setattr(CoordinatorInputReceiptSource, "route_fresh", route)
        code, body = request(
            server,
            "POST",
            "/ask",
            {
                "prompt": "普通输入",
                "conversation_id": "intent",
                "metadata": {"message_id": "intent-one"},
                "delivery_intent": intent,
            },
            headers={"X-User-Id": "local-agent", "X-Channel": "chat"},
        )
        assert code == 202 and body["input_state"] == "queued"
        if count == 0:
            assert called == ([] if intent == "queue" else [OwnerIdentity.local_main()])
        else:
            assert called == [intent != "queue"]


@pytest.mark.parametrize("count", [0, 2])
@pytest.mark.parametrize("intent", [None, False, [], {}, "", "future"])
def test_invalid_delivery_intent_has_no_host_preparation_or_queue_effect(
    tmp_path, monkeypatch, count, intent
):
    from agent_py_agent.tests.test_gateway_c_http_wait import request

    with _http_case(tmp_path, monkeypatch, count) as (_agent, _gate, server):
        seen = _capture_prepared(monkeypatch)
        before = {p: p.read_bytes() for p in server.paths.inbox.glob("*.json")}
        code, body = request(
            server, "POST", "/ask", {"prompt": "普通输入", "delivery_intent": intent}
        )
        assert code == 400 and body == {"error": "delivery_intent必须是auto或queue。"}
        assert (
            seen == [] and {p: p.read_bytes() for p in server.paths.inbox.glob("*.json")} == before
        )


@pytest.mark.parametrize("count", [0, 2])
def test_same_id_cannot_switch_queue_to_auto(tmp_path, monkeypatch, count):
    from agent_py_agent.tests.test_gateway_c_http_wait import request

    with _http_case(tmp_path, monkeypatch, count) as (_agent, _gate, server):
        body = {
            "prompt": "相同正文",
            "conversation_id": "intent-conflict",
            "metadata": {"message_id": "same-id"},
            "delivery_intent": "queue",
        }
        code, result = request(server, "POST", "/ask", body)
        assert code == 202
        before = {p: p.read_bytes() for p in server.paths.inbox.glob("*.json")}
        code, conflict = request(server, "POST", "/ask", {**body, "delivery_intent": "auto"})
        assert code == 409 and conflict["error_code"] == "IDEMPOTENCY_CONFLICT"
        assert conflict["request_id"] == result["request_id"]
        assert {p: p.read_bytes() for p in server.paths.inbox.glob("*.json")} == before


@pytest.mark.parametrize("status", [0, 503])
def test_callback_precedes_one_post_unknown_keeps_same_id_without_file_fallback(tmp_path, status):
    from agent_py_agent.agent.gateway_parts.input_identity import http_input_identity
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.gateway_parts.request_client import (
        GatewayAskParams,
        submit_gateway_ask,
    )

    paths = gateway_paths_from_root(tmp_path / "gateway")
    order = []
    expected, _ = http_input_identity(
        user_id="local-agent", channel="chat", conversation_id="callback", message_id="msg"
    )

    def send(body):
        assert order == [("allocated", expected)]
        assert body["metadata"]["message_id"] == "msg" and body["delivery_intent"] == "queue"
        assert "host_request_owner.v1" not in body and "route" not in body
        order.append(("post", expected))
        return status, {}

    from agent_py_agent.agent.gateway_parts.request_http_client import (
        GatewayAskSubmissionUnconfirmed,
    )

    with pytest.raises(GatewayAskSubmissionUnconfirmed) as captured:
        submit_gateway_ask(
            paths,
            params=GatewayAskParams(
                prompt="原始输入", chat_session_id="callback", client_message_id="msg"
            ),
            submit_request=send,
            on_request_allocated=lambda rid: order.append(("allocated", rid)),
        )
    assert captured.value.request_id == expected
    assert captured.value.error_code == "INPUT_DISPOSITION_UNCONFIRMED"
    assert order == [("allocated", expected), ("post", expected)]
    assert not list(paths.inbox.glob("*.json"))


def test_registration_error_or_missing_original_task_command_has_zero_post(tmp_path):
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.gateway_parts.request_client import (
        GatewayAskParams,
        submit_gateway_ask,
    )

    paths = gateway_paths_from_root(tmp_path / "gateway")
    posts = []

    def fail(_rid):
        raise RuntimeError("synthetic display rejection")

    with pytest.raises(RuntimeError, match="synthetic display"):
        submit_gateway_ask(
            paths,
            params=GatewayAskParams(prompt="普通输入"),
            submit_request=lambda b: posts.append(b),
            on_request_allocated=fail,
        )
    with pytest.raises(ValueError, match="原始输入"):
        submit_gateway_ask(
            paths,
            params=GatewayAskParams(
                prompt="任务正文", system_task={"kind": "audit_prepare", "attributes": {}}
            ),
            submit_request=lambda b: posts.append(b),
        )
    assert posts == [] and not list(paths.inbox.glob("*.json"))


@pytest.mark.parametrize(
    "raw",
    ["/audit 安全巡检 prepare 逐条检查来源", "/experiment observe skill_tool 10m 1 50000 整理资料"],
)
@pytest.mark.parametrize("count", [0, 2])
def test_prepared_plain_job_transports_original_task_slash_and_server_reparses(
    tmp_path, monkeypatch, raw, count
):
    import queue
    import threading

    from agent_py_agent.agent.conversation.control_commands import parse_conversation_task_command
    from agent_py_agent.agent.gateway_parts.input_delivery_service import read_gateway_input_receipt
    from agent_py_agent.cli.chat_parts.plain import _plain_enqueue_job
    from agent_py_agent.cli.chat_parts.plain_state import PlainEnqueueParams

    job = _plain_enqueue_job(PlainEnqueueParams(raw, queue.Queue(), threading.Lock(), [0], [], []))
    expected = parse_conversation_task_command(raw)
    assert expected.valid and job.user == expected.prompt
    assert job.original_prompt == raw and job.system_task == expected.to_request_payload()
    with _http_case(tmp_path, monkeypatch, count) as (agent, _gate, server):
        result = submit_chat_request(
            server.paths,
            ChatRequestContent(
                prompt=job.user,
                original_prompt=job.original_prompt,
                system_task=job.system_task,
                inject=[],
                prompt_files=[],
                save=True,
                show_prompt=False,
                resume_context=None,
                chat_session_id="task-slash",
            ),
            _client(agent, server),
        )
        stored = read_gateway_input_receipt(server.paths, result[0]).prepared_request
        assert stored["system_task"] == expected.to_request_payload()
        assert (
            stored["goal"] == expected.prompt
            and frozen_request_owner(stored) == OwnerIdentity.local_main()
        )


# LLM: Count2沿原已准入claim，Count0由实际HTTP准备后取得原claim/会话车道；不伪造host owner或活跃路由。
# 函数用途: 提供真正已有活跃前台的两种宿主，验证新排队输入不会被误当/btw。
def _active_case(tmp_path, monkeypatch, count):
    from contextlib import contextmanager

    from agent_py_agent.agent.gateway_parts.queue_service import claim_request
    from agent_py_agent.agent.gateway_parts.request_binding import GatewayTaskBindingWriter
    from agent_py_agent.tests.test_gateway_active_input_transport import foreground

    @contextmanager
    def case():
        if count == 2:
            with foreground(tmp_path, monkeypatch) as values:
                yield values[0], values[5], values[6]
            return
        with _http_case(tmp_path, monkeypatch, count) as (agent, _gate, server):
            original = submit_chat_request(
                server.paths,
                ChatRequestContent(
                    prompt="原活跃任务",
                    inject=[],
                    prompt_files=[],
                    save=True,
                    show_prompt=False,
                    resume_context=None,
                    chat_session_id="active-cid",
                ),
                _client(agent, server),
            )
            claim = claim_request(server.paths, server.paths.inbox / (original[0] + ".json"))
            assert claim is not None
            payload = read_json_file(claim.path)
            thread = agent.conversation_store.threads.get_or_create(payload["conversation"])
            GatewayTaskBindingWriter(
                claim.path, claim.request_id, execution_attempt_id=claim.execution_attempt_id
            ).bind_conversation_claim(thread.thread_id)
            assert agent.conversation_store.claims.acquire(
                {
                    "thread_id": thread.thread_id,
                    "task_id": "gateway:" + claim.request_id,
                    "reason": "gateway_foreground_turn",
                }
            )
            yield agent, server, claim

    return case()


@pytest.mark.parametrize("count", [0, 2])
@pytest.mark.parametrize("other_owner", [False, True])
def test_actual_active_turn_local_queue_never_appends_same_or_foreign_owner(
    tmp_path, monkeypatch, count, other_owner
):
    from agent_py_agent.agent.gateway_parts.input_delivery_service import read_gateway_input_receipt

    with _active_case(tmp_path, monkeypatch, count) as (agent, server, active):
        before = active.path.read_bytes()
        owner = (
            OwnerIdentity.provider_user("local", "another-user")
            if other_owner
            else OwnerIdentity.local_main()
        )
        result = submit_chat_request(
            server.paths,
            ChatRequestContent(
                prompt="新的独立输入",
                inject=[],
                prompt_files=[],
                save=True,
                show_prompt=False,
                resume_context=None,
                chat_session_id="active-cid",
            ),
            _client(agent, server, owner=owner),
        )
        receipt = read_gateway_input_receipt(server.paths, result[0])
        assert receipt.state == "queued" and receipt.transport is None
        assert (
            receipt.target_turn_id == "" and frozen_request_owner(receipt.prepared_request) == owner
        )
        assert (server.paths.inbox / (result[0] + ".json")).exists()
        assert active.path.read_bytes() == before
        assert agent.conversation_store.guidance.pending("request", active.request_id) == []


@pytest.mark.parametrize("count", [0, 2])
def test_existing_auto_still_finds_real_same_owner_active_turn(tmp_path, monkeypatch, count):
    from agent_py_agent.agent.gateway_parts.input_delivery_service import read_gateway_input_receipt
    from agent_py_agent.tests.test_gateway_c_http_wait import request

    with _active_case(tmp_path, monkeypatch, count) as (_agent, server, active):
        code, body = request(
            server,
            "POST",
            "/ask",
            {
                "prompt": "普通HTTP补充",
                "conversation_id": "active-cid",
                "metadata": {"message_id": "auto-active"},
            },
            headers={"X-User-Id": "local-agent", "X-Channel": "chat"},
        )
        assert code == 202
        receipt = read_gateway_input_receipt(server.paths, body["request_id"])
        assert receipt.state != "queued"
        assert not (server.paths.inbox / (body["request_id"] + ".json")).exists()
        if count == 2:
            assert receipt.transport["gateway_claim"]["request_id"] == active.request_id
        else:
            assert receipt.target_turn_id == active.request_id


@pytest.mark.parametrize("count", [0, 2])
@pytest.mark.parametrize("caller", ["standalone", "adapter"])
def test_other_formal_local_submitters_use_original_http_owner_gate(
    tmp_path, monkeypatch, count, caller
):
    from agent_py_agent.agent.gateway_parts.adapter import _submit_adapter_gateway_request
    from agent_py_agent.agent.gateway_parts.input_delivery_service import read_gateway_input_receipt
    from agent_py_agent.cli.gateway_client import _submit_gateway_ask

    with _http_case(tmp_path, monkeypatch, count) as (agent, _gate, server):
        agent.config = replace(agent.config, gateway_port=server.server.server_address[1])
        if caller == "standalone":
            result = _submit_gateway_ask(
                SimpleNamespace(
                    prompt="独立CLI输入",
                    inject=[],
                    prompt_file=[],
                    no_save=False,
                    show_prompt=False,
                    resume_context=None,
                ),
                agent,
                server.paths,
            )
            request_id = result.request_id
        else:
            result = _submit_adapter_gateway_request(agent, server.paths, {}, "文件适配器原输入")
            request_id = result[0]
        receipt = read_gateway_input_receipt(server.paths, request_id)
        assert (
            receipt.state == "queued"
            and frozen_request_owner(receipt.prepared_request) == OwnerIdentity.local_main()
        )
        assert receipt.client_message_id == receipt.prepared_request["metadata"]["message_id"]


@pytest.mark.parametrize("count", [0, 2])
def test_lost_response_reports_unconfirmed_without_changing_committed_host_request(
    tmp_path, monkeypatch, count
):
    from agent_py_agent.agent.gateway_parts import request_http_client
    from agent_py_agent.agent.gateway_parts.input_delivery_service import read_gateway_input_receipt
    from agent_py_agent.agent.gateway_parts.request_client import (
        GatewayAskParams,
        submit_gateway_ask,
    )
    from agent_py_agent.tests.test_gateway_c_http_wait import request

    with _http_case(tmp_path, monkeypatch, count) as (agent, _gate, server):
        client = _client(agent, server)
        calls = []

        def lost(body):
            calls.append(body)
            status, receipt = request_http_client.post_gateway_ask(client, body)
            assert status == 202
            assert read_gateway_input_receipt(server.paths, receipt["request_id"]).state == "queued"
            return 0, {}

        from agent_py_agent.agent.gateway_parts.request_http_client import (
            GatewayAskSubmissionUnconfirmed,
        )

        allocated = []
        with pytest.raises(GatewayAskSubmissionUnconfirmed) as captured:
            submit_gateway_ask(
                server.paths,
                params=GatewayAskParams(
                    prompt="响应丢失", chat_session_id="lost", client_message_id="lost-message"
                ),
                submit_request=lost,
                on_request_allocated=allocated.append,
            )
        request_id = captured.value.request_id
        path = server.paths.inbox / (request_id + ".json")
        before = path.read_bytes()
        code, status = request(
            server,
            "GET",
            "/input-status/" + request_id,
            headers={"X-User-Id": "local-agent", "X-Channel": "chat"},
        )
        # 这里只读证明宿主已提交事实仍在；生产客户端不自动GET、不取消、不重投。
        assert (
            code == 200 and status["request_id"] == request_id and status["input_state"] == "queued"
        )
        assert allocated == [request_id] and len(calls) == 1 and path.read_bytes() == before
        assert not (server.paths.terminal / (request_id + ".json")).exists()


@pytest.mark.parametrize(
    "raw",
    ["/audit 安全巡检 prepare 逐条检查来源", "/experiment observe skill_tool 10m 1 50000 整理资料"],
)
def test_actual_tui_prepared_job_keeps_raw_command_separate_from_display(
    tmp_path, monkeypatch, raw
):
    import queue
    import threading

    from agent_py_agent.agent.conversation.control_commands import parse_conversation_task_command
    from agent_py_agent.agent.gateway_parts.input_delivery_service import read_gateway_input_receipt
    from agent_py_agent.cli.chat_parts.tui_actions import _tui_enqueue_job
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
    from agent_py_agent.cli.chat_parts.tui_worker_paths import _submit_new_gateway_job

    with _http_case(tmp_path, monkeypatch, 2) as (agent, _gate, server):
        params = SimpleNamespace(
            runtime_inject=[],
            prompt_files=[],
            args=SimpleNamespace(no_save=False, resume_context=None),
            state_lock=threading.Lock(),
            is_running_ref=[False],
            pending_jobs_ref_for_enqueue=[0],
            tui_runtime=TuiRuntime("raw-task"),
            jobs=queue.Queue(),
            agent=_client(agent, server),
            paths=server.paths,
            current_session_id="raw-task",
        )
        _tui_enqueue_job(params, raw, display_text="仅供显示的摘要")
        job = params.jobs.get_nowait()
        assert job.original_prompt == raw and job.display_text == "仅供显示的摘要"
        result = _submit_new_gateway_job(params, job, [])
        expected = parse_conversation_task_command(raw)
        receipt = read_gateway_input_receipt(server.paths, result[0])
        assert receipt.prepared_request["goal"] == expected.prompt
        assert receipt.prepared_request["system_task"] == expected.to_request_payload()
        assert frozen_request_owner(receipt.prepared_request) == OwnerIdentity.local_main()


@pytest.mark.parametrize("count", [0, 2])
def test_bad_gateway_credential_never_falls_back_to_local_queue(tmp_path, monkeypatch, count):
    from agent_py_agent.agent.gateway_parts import request_http_client
    from agent_py_agent.agent.gateway_parts.client_credentials import GatewayClientCredentials

    with _http_case(tmp_path, monkeypatch, count) as (agent, _gate, server):
        before = {p: p.read_bytes() for p in server.paths.inbox.glob("*.json")}
        monkeypatch.setattr(
            request_http_client,
            "gateway_client_credentials",
            lambda _agent: GatewayClientCredentials(tmp_path, configured_token="synthetic-invalid"),
        )
        with pytest.raises(PermissionError, match="未授权"):
            submit_chat_request(
                server.paths,
                ChatRequestContent(
                    prompt="不受理输入",
                    inject=[],
                    prompt_files=[],
                    save=True,
                    show_prompt=False,
                    resume_context=None,
                    chat_session_id="bad-auth",
                ),
                _client(agent, server),
            )
        assert {p: p.read_bytes() for p in server.paths.inbox.glob("*.json")} == before


@pytest.mark.parametrize("damage", ["different_prompt", "different_task", "missing_raw"])
def test_mismatched_original_command_rejects_before_callback_and_post(tmp_path, damage):
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_task_command
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.gateway_parts.request_client import (
        GatewayAskParams,
        submit_gateway_ask,
    )

    command = parse_conversation_task_command("/audit 检查 prepare 检查来源")
    params = GatewayAskParams(
        prompt=command.prompt,
        system_task=command.to_request_payload(),
        original_prompt="/audit 检查 prepare 检查来源",
    )
    if damage == "different_prompt":
        params.prompt = "另一正文"
    elif damage == "different_task":
        params.system_task = {**params.system_task, "kind": "experiment"}
    else:
        params.original_prompt = None
    effects = []
    with pytest.raises(ValueError, match="原始输入"):
        submit_gateway_ask(
            gateway_paths_from_root(tmp_path / "gateway"),
            params=params,
            submit_request=lambda body: effects.append("post"),
            on_request_allocated=lambda rid: effects.append("allocated"),
        )
    assert effects == []


def test_auto_digest_omission_keeps_original_formula_and_identity_alias():
    import hashlib
    import json

    from agent_py_agent.agent.gateway_parts import http_handlers
    from agent_py_agent.agent.gateway_parts.input_identity import http_input_identity
    from agent_py_agent.agent.gateway_parts.request_client import GatewayAskExecutionOptions

    body = {"goal": "原正文", "metadata": {"message_id": "original"}, "conversation_id": "thread"}
    request_id, identity = http_input_identity(
        user_id="local-agent", channel="chat", conversation_id="thread", message_id="original"
    )
    original = {
        **identity,
        "goal": "原正文",
        "metadata": body["metadata"],
        "system_task": {},
        "workspace": {},
        "execution_options": GatewayAskExecutionOptions.from_payload(body).to_payload(),
    }
    expected = hashlib.sha256(
        json.dumps(original, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert http_handlers.http_input_identity is http_input_identity
    for value in ({}, {"delivery_intent": "auto"}):
        assert http_handlers._http_idempotent_request_identity(
            {**body, **value}, goal="原正文", user_id="local-agent", channel="chat"
        ) == (request_id, expected)


@pytest.mark.parametrize("caller", ["chat", "standalone", "adapter"])
def test_original_non_task_control_rejection_still_precedes_any_post(tmp_path, monkeypatch, caller):
    from agent_py_agent.agent.gateway_parts import request_http_client
    from agent_py_agent.agent.gateway_parts.adapter import _submit_adapter_gateway_request
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.cli.gateway_client import _submit_gateway_ask

    posts = []
    monkeypatch.setattr(request_http_client, "post_gateway_ask", lambda *args: posts.append(args))
    paths = gateway_paths_from_root(tmp_path / "gateway")
    agent = SimpleNamespace(root=tmp_path, workspace_roots=[tmp_path])
    with pytest.raises(ValueError, match="系统命令.*控制入口"):
        if caller == "chat":
            submit_chat_request(
                paths,
                ChatRequestContent(
                    prompt="/status",
                    inject=[],
                    prompt_files=[],
                    save=True,
                    show_prompt=False,
                    resume_context=None,
                ),
                agent,
                on_request_allocated=lambda rid: posts.append("allocated"),
            )
        elif caller == "standalone":
            _submit_gateway_ask(
                SimpleNamespace(
                    prompt="/status",
                    inject=[],
                    prompt_file=[],
                    no_save=False,
                    show_prompt=False,
                    resume_context=None,
                ),
                agent,
                paths,
            )
        else:
            _submit_adapter_gateway_request(agent, paths, {}, "/status")
    assert posts == [] and not list(paths.inbox.glob("*.json"))


def test_shared_http_body_keeps_all_execution_options_and_media_refs(tmp_path):
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.gateway_parts.request_client import (
        GatewayAskParams,
        submit_gateway_ask,
    )

    media = ({"schema_version": "input_media_ref.v1", "media_id": "synthetic-ref"},)
    seen = []
    params = GatewayAskParams(
        prompt="带附件请求",
        inject=["既有注入"],
        prompt_files=["既有ref"],
        input_media=media,
        save=False,
        include_prompt=True,
        resume_context=False,
        interactive_approvals=True,
        rich_transcript=True,
        workspace_root=str(tmp_path),
        workspace_roots=[str(tmp_path)],
    )
    from agent_py_agent.agent.gateway_parts.request_http_client import (
        GatewayAskSubmissionUnconfirmed,
    )

    with pytest.raises(GatewayAskSubmissionUnconfirmed) as captured:
        submit_gateway_ask(
            gateway_paths_from_root(tmp_path / "gateway"),
            params=params,
            submit_request=lambda body: seen.append(body) or (503, {}),
        )
    assert captured.value.request_id and len(seen) == 1
    body = seen[0]
    assert (
        body["input_media"] == list(media)
        and body["inject"] == ["既有注入"]
        and body["prompt_files"] == ["既有ref"]
    )
    assert (
        body["save"] is False and body["include_prompt"] is True and body["resume_context"] is False
    )
    assert body["client_capabilities"] == {"tool_approval": True, "rich_transcript": True}
    assert body["workspace"] == {"cwd": str(tmp_path.resolve()), "roots": [str(tmp_path.resolve())]}


@pytest.mark.parametrize("status", [404, 422])
def test_definite_http_rejection_is_synchronous_without_body_echo_or_queue_fallback(
    tmp_path, status
):
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.gateway_parts.request_client import (
        GatewayAskParams,
        submit_gateway_ask,
    )

    paths = gateway_paths_from_root(tmp_path / "gateway")
    with pytest.raises(ValueError, match="Gateway拒绝本次输入") as captured:
        submit_gateway_ask(
            paths,
            params=GatewayAskParams(prompt="普通请求"),
            submit_request=lambda _body: (status, {"error": "synthetic-only-private-body"}),
        )
    assert str(status) in str(captured.value) and "synthetic-only-private-body" not in str(
        captured.value
    )
    assert not list(paths.inbox.glob("*.json"))


@pytest.mark.parametrize("status", [200, 202])
def test_success_http_without_same_id_accepted_receipt_is_unconfirmed(tmp_path, status):
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.gateway_parts.request_client import (
        GatewayAskParams,
        submit_gateway_ask,
    )
    from agent_py_agent.agent.gateway_parts.request_http_client import (
        GatewayAskSubmissionUnconfirmed,
    )

    with pytest.raises(GatewayAskSubmissionUnconfirmed) as captured:
        submit_gateway_ask(
            gateway_paths_from_root(tmp_path / "gateway"),
            params=GatewayAskParams(prompt="普通输入"),
            submit_request=lambda body: (status, {}),
        )
    assert captured.value.error_code == "INPUT_DISPOSITION_UNCONFIRMED"


def test_confirmed_connection_refusal_is_synchronous_without_post_retry(tmp_path, monkeypatch):
    import urllib.error

    from agent_py_agent.agent.gateway_parts import request_http_client
    from agent_py_agent.agent.gateway_parts.client_credentials import GatewayClientCredentials

    calls = []
    monkeypatch.setattr(
        request_http_client,
        "gateway_client_credentials",
        lambda _agent: GatewayClientCredentials(tmp_path, configured_token="synthetic"),
    )

    def refused(*args, **kwargs):
        calls.append("connect")
        raise urllib.error.URLError(ConnectionRefusedError("synthetic"))

    monkeypatch.setattr(request_http_client.urllib.request, "urlopen", refused)
    with pytest.raises(ConnectionError, match="连接被拒绝.*请求未发送"):
        request_http_client.post_gateway_ask(
            SimpleNamespace(config=SimpleNamespace(gateway_port=1)),
            {"user_id": "local-agent", "channel": "chat", "metadata": {}},
        )
    assert calls == ["connect"]


def test_unconfirmed_tui_prepare_uses_original_finally_without_poll_or_host_terminal(
    tmp_path, monkeypatch
):
    import queue
    import threading

    from agent_py_agent.agent.gateway_parts import request_http_client
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.cli.chat_parts import gateway_client, tui_worker
    from agent_py_agent.cli.chat_parts.plain_state import ChatJob
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime

    runtime = TuiRuntime("unconfirmed-local")
    job = ChatJob(
        user="普通输入", show_prompt=False, inject=[], prompt_files=[], request_id="local-unknown"
    )
    jobs = queue.Queue()
    jobs.put(job)
    runtime.enqueue_prompt(job.request_id, job.user, queued=False)
    calls = []
    monkeypatch.setattr(
        request_http_client,
        "post_gateway_ask",
        lambda _agent, _body: calls.append("post") or (0, {}),
    )
    monkeypatch.setattr(
        gateway_client,
        "poll_gateway_chunks",
        lambda _request: pytest.fail("未确认输入不得进入无限续等"),
    )
    stop = threading.Event()
    summaries = []
    complete = runtime.complete_turn

    def finish(request_id, summary):
        summaries.append(summary)
        complete(request_id, summary)
        stop.set()

    monkeypatch.setattr(runtime, "complete_turn", finish)
    cfg = tui_worker.TuiWorkerConfig(
        jobs=jobs,
        state_lock=threading.Lock(),
        is_running_ref=[False],
        pending_jobs_ref=[1],
        running_prompt_ref=[""],
        running_request_id_ref=[""],
        running_started_at_ref=[0.0],
        agent=SimpleNamespace(root=tmp_path, workspace_roots=[tmp_path]),
        args=SimpleNamespace(),
        paths=gateway_paths_from_root(tmp_path / "gateway"),
        use_gateway=True,
        conversation_history=[],
        history_lock=threading.Lock(),
        build_history_context=lambda: "",
        assistant_outputs=[],
        last_token_estimate_ref=[0],
        stop_event=stop,
        current_session_id="unconfirmed-local",
        tui_runtime=runtime,
    )
    tui_worker._tui_worker_body(cfg)
    assert calls == ["post"] and len(summaries) == 1
    assert "受理未确认" in summaries[0].error and "请勿重发" in summaries[0].error
    assert (
        "这不表示宿主任务失败或未受理" in summaries[0].error and "gwreq-msg-" in summaries[0].error
    )
    assert (
        cfg.is_running_ref == [False]
        and cfg.pending_jobs_ref == [0]
        and cfg.running_request_id_ref == [""]
    )
    assert (
        job.gateway_request_id == ""
        and jobs.unfinished_tasks == 0
        and not runtime.store.snapshot().active_blocks
    )
    assert not list(cfg.paths.terminal.glob("*.json")) and not list(cfg.paths.inbox.glob("*.json"))
