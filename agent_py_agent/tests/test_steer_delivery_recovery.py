"""插话（TUI 运行中输入）遇到模型调用失败、以及后台回合收尾时不再丢失（2026-09-28）。

生产事实（只看结构化字段，见 TESTS.md）：一条插话随模型调用提交后，这次调用以 ProviderTransientError 失败。
旧规则下两层重试都因“有未确认插话”拒绝重发，尝试随即失败；新尝试又不重发“提交不明”的插话，Gateway 回执
永远停在 active_pending，TUI 一直轮询。定时任务（srun_*）没有 Gateway 请求文件，回合结束后也从不收尾。

锁定：
- 失败调用只把自己那批插话退回预留（批次记 rejected），守卫只看在途提交；下一次调用按新编号重新提交，确认后消费一次。
  拿过期调用编号退回是空操作；流中止换成合成回复时不退回。
- 墙钟超时后的物理重试同样重新提交，被放弃的旧调用不会被确认。
- 模型轮瞬断重试恢复：同一回合里模型只看到一次插话，并按它回复。
- 隔离 Gateway 端到端：TUI 客户端在模型调用进行中插话，带插话的调用瞬断后本轮重试成功，入口回执收成 consumed。
- 没有 Gateway 请求文件的后台目标：任务仍在运行时不收尾；任务终态后未消费的插话转成下一轮请求。
全部用假后端或直接账本操作，零网络（端到端只连进程内 127.0.0.1 临时端口）。
"""
from __future__ import annotations

import hashlib
import json
import socket
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import provider_transient_auto_resume as retry
from agent_py_agent.agent.agent_core import tool_model_generation as generation
from agent_py_agent.agent.agent_core._tool_loop_service import execute_tool_loop
from agent_py_agent.agent.agent_core.runtime.guidance import (
    acknowledge_injected_turn_input,
    active_turn_input_has_unconfirmed_delivery,
    restore_turn_input_after_failed_provider_call,
)
from agent_py_agent.agent.agent_core.tool_model_generation import (
    ModelGenerateParams,
    generate_model_response,
)
from agent_py_agent.agent.agent_core.tool_stream import (
    LongToolContentAbortPayload,
    LongToolContentStreamAbort,
)
from agent_py_agent.agent.backends import ModelResponse, ProviderTransientError
from agent_py_agent.agent.backends.base import EchoBackend
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import (
    GatewayAskParams,
    _process_gateway_requests,
    gateway_paths,
    submit_gateway_ask,
)
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.input_delivery_service import (
    _input_target_lifecycle,
    bind_gateway_input_active_locked,
    gateway_input_status_payload,
    gateway_input_transition,
    load_or_prepare_gateway_input_locked,
    read_gateway_input_receipt,
    reconcile_gateway_input_receipts,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.cli.chat_client_context import ActiveTurnInputDelivery, GatewayChatClientAgent
from agent_py_agent.cli.gateway_process import _build_gateway_auth_middleware
from agent_py_agent.tests.test_runtime_guidance import _tool_loop_params
from agent_py_agent.tests.test_tool_model_generation import _tool_loop_params as _generation_params

_TEXT = "改用已有资料直接收口"


# 函数用途: 按 TUI 插话的形状写一条带去重键、绑定精确回合的补充消息。
def _steer(store: ConversationStore, turn_id: str, key: str, target_type: str = "request"):
    return store.guidance.append_once(
        {
            "message": _TEXT,
            "sender": "local-agent",
            "target_type": target_type,
            "target_id": turn_id,
            "priority": "high",
            "delivery": "current_request",
            "metadata": {
                "kind": "active_turn_user_input",
                "record_in_transcript": True,
                "expected_turn_id": turn_id,
                "channel_message_id": key,
                "gateway_input_request_id": f"gwreq-msg-{key}",
            },
        },
        dedupe_key=key,
    )


# 函数用途: 按提交时间列出一个回合的模型提交批次：(调用编号, 状态, 插话编号列表)。
def _batches(store: ConversationStore, turn_id: str) -> list[tuple[str, str, list[str]]]:
    folder = store.storage.guidance_submission_batches_dir / hashlib.sha256(turn_id.encode("utf-8")).hexdigest()
    rows = sorted((json.loads(path.read_text(encoding="utf-8")) for path in folder.glob("*.json")),
                  key=lambda row: float(row.get("committed_at") or 0))
    return [(row["provider_call_id"], row["status"], [item["guidance_id"] for item in row["items"]]) for row in rows]


# 类用途: 第一次调用失败（直接抛瞬断，或卡住直到墙钟超时被放弃），之后的调用正常返回。
class _FailThenAcceptBackend:
    name = "fail-then-accept"

    def __init__(self, first: str) -> None:
        self.first = first
        self.calls = 0
        self.release = threading.Event()
        self.first_call_entered = threading.Event()

    # 函数用途: 按调用次数返回失败、迟到回复或正常回复；第一次调用真正进入时置位 first_call_entered。
    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        del prompt, on_chunk
        self.calls += 1
        if self.calls == 1:
            self.first_call_entered.set()
        if self.calls == 1 and self.first == "raise":
            raise ProviderTransientError("临时服务错误")
        if self.calls == 1:
            self.release.wait(10.0)
            return ModelResponse(text="被放弃的迟到回复", backend=self.name)
        return ModelResponse(text="accepted", backend=self.name)


# 函数用途: 组一次生成层请求：插话已认领并注入本尝试，等待随下一次模型调用提交。
def _generation_request(store: ConversationStore, backend: object, entry, timeout: float) -> ModelGenerateParams:
    assert store.guidance.claim_for_turn(entry, expected_turn_id="req-steer", attempt_id="attempt-1")
    agent = SimpleNamespace(backend=backend, conversation_store=store,
                            config=SimpleNamespace(request_timeout=timeout), _current_subagent_run_id="")
    params = replace(_generation_params(), request_id="req-steer", attempt_id="attempt-1",
                     live_archive_state={"_guidance_ack_ids": {entry.guidance_id},
                                         "_guidance_ack_entries": {entry.guidance_id: entry}})
    return ModelGenerateParams(agent=agent, params=params, prompt="continue", tool_rounds=0)


def test_failed_call_returns_its_steer_and_the_next_call_submits_it_again(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    entry = _steer(store, "req-steer", "steer-a")
    request = _generation_request(store, _FailThenAcceptBackend("raise"), entry, timeout=10)

    with pytest.raises(ProviderTransientError):
        generate_model_response(request)

    assert store.guidance.receipt("steer-a").status == "reserved"  # 退回预留，而不是“提交不明”
    assert not active_turn_input_has_unconfirmed_delivery(request.params.live_archive_state)
    assert [(status, ids) for _call, status, ids in _batches(store, "req-steer")] == [("rejected", [entry.guidance_id])]
    assert generate_model_response(request).text == "accepted"
    batches = _batches(store, "req-steer")
    receipt = store.guidance.receipt("steer-a")
    assert receipt.status == "submitted" and receipt.submission_id == batches[-1][0] != batches[0][0]
    host = SimpleNamespace(conversation_store=store)
    # 迟到的旧调用失败（编号已不是当前在途的那次）不能把新提交退回。
    assert restore_turn_input_after_failed_provider_call(host, request.params, provider_call_id=batches[0][0]) == 0
    assert store.guidance.receipt("steer-a").submission_id == batches[-1][0]
    assert acknowledge_injected_turn_input(host, request.params) == 1
    assert store.guidance.receipt("steer-a").status == "consumed"


def test_wall_timeout_retry_resubmits_the_steer_under_the_retry_call(tmp_path, monkeypatch):
    """第一次物理调用被墙钟放弃后，重试调用重新提交同一条插话。

    墙钟只对第一次调用生效，且只在假后端真正进入（插话已随这次调用提交）之后才开始计 0.05 秒：这样超时必然发生，
    又不会抢在提交之前；重试调用沿用配置的 10 秒，不会因满载下线程启动慢而被误判为第二次超时。
    """
    store = ConversationStore(tmp_path / "conversations")
    entry = _steer(store, "req-steer", "steer-a")
    backend = _FailThenAcceptBackend("block")
    request = _generation_request(store, backend, entry, timeout=10.0)
    original_wait = generation._wait_for_generation_result
    wall_timeouts: list[float] = []

    # 函数用途: 第一次物理调用等后端进入后按 0.05 秒墙钟等待；之后的调用原样使用配置超时（参数同原函数）。
    def wait_with_per_call_wall_clock(*call):
        *wait_args, timeout = call
        if not wall_timeouts:
            assert backend.first_call_entered.wait(timeout=10.0)
            timeout = 0.05
        wall_timeouts.append(timeout)
        return original_wait(*wait_args, timeout)

    monkeypatch.setattr(generation, "_wait_for_generation_result", wait_with_per_call_wall_clock)
    try:
        assert generate_model_response(request).text == "accepted"  # 物理重试不再被“有插话”拦下
    finally:
        backend.release.set()
    assert wall_timeouts == [0.05, 10.0]

    batches = _batches(store, "req-steer")
    assert backend.calls == 2 and [status for _call, status, _ids in batches] == ["rejected", "submitted"]
    assert store.guidance.receipt("steer-a").submission_id == batches[1][0]


def test_turn_retries_after_transient_failure_and_the_model_sees_the_steer_once(tmp_path, monkeypatch):
    monkeypatch.setattr(retry, "provider_transient_retry_delays", lambda _policy: (0.01,))
    monkeypatch.setattr(retry, "wait_interruptibly", lambda _delay: None)
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    _steer(agent.conversation_store, "req-1", "steer-b")
    wires: list[str] = []

    # 类用途: 第一次调用时插话已随请求发出，然后瞬断；重试时正常回复。
    class FlakyBackend:
        name = "flaky"

        # 函数用途: 记录每次出站内容，第一次抛瞬断。
        def generate(self, prompt: str, on_chunk=None, **kwargs):
            del on_chunk
            wires.append(json.dumps(kwargs.get("messages") or prompt, ensure_ascii=False))
            if len(wires) == 1:
                raise ProviderTransientError("临时服务错误")
            return ModelResponse(text="已按插话调整。", backend=self.name)

    agent.backend = FlakyBackend()
    params = _tool_loop_params()

    response = execute_tool_loop(agent, params).final_response

    assert response.text == "已按插话调整。"
    assert len(wires) == 2 and [wire.count(_TEXT) for wire in wires] == [1, 1]
    assert agent.conversation_store.guidance.receipt("steer-b").status == "consumed"
    assert [item["text"] for item in params.active_turn_user_inputs] == [_TEXT]
    assert [status for _call, status, _ids in _batches(agent.conversation_store, "req-1")] == ["rejected", "submitted"]


def test_stream_abort_becomes_a_reply_so_its_steer_is_not_returned(monkeypatch):
    returned: list[str] = []
    abort = LongToolContentStreamAbort(LongToolContentAbortPayload(tool="write_file", path="a.txt", chars=9, limit=8))

    # 函数用途: 模拟流被宿主中止（之后会换成合成回复并正常确认插话）。
    def aborted(*_args):
        raise abort

    monkeypatch.setattr(generation, "_generate_through_wall_guard", aborted)
    monkeypatch.setattr(generation, "_restore_failed_call_turn_input", lambda state: returned.append(state.call_id))
    with pytest.raises(LongToolContentStreamAbort):
        generation._generate_with_wall_timeout(SimpleNamespace(), SimpleNamespace(call_id="call-abort"))
    assert returned == []
    monkeypatch.setattr(generation, "_generate_through_wall_guard", lambda *_args: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(RuntimeError):
        generation._generate_with_wall_timeout(SimpleNamespace(), SimpleNamespace(call_id="call-failed"))
    assert returned == ["call-failed"]  # 普通失败才退回


# 函数用途: 建一个带独立 home 与 Gateway 目录的 echo Agent（不按用户分 owner），返回（Agent, Gateway 路径）。
def _gateway_agent(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), prompt_files=[],
                                    gateway_per_user_owner_scoping=False), tmp_path / "ws")
    paths = gateway_paths(agent)
    for folder in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses):
        folder.mkdir(parents=True, exist_ok=True)
    return agent, paths


# 函数用途: 取一个本机空闲端口给进程内 Gateway HTTP 服务。
def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# 类用途: 按 TUI 真实时序脚本化的模型：第一次调用进行中用户经 TUI 客户端插话；带上插话的调用瞬断；重试正常回复。
class _TuiSteerBackend:
    name = "tui-steer-script"

    def __init__(self, client: GatewayChatClientAgent, turn: list[str]) -> None:
        self.client, self.turn = client, turn
        self.wires: list[str] = []
        self.ingress: list[object] = []

    # 函数用途: 记录出站内容；第一次调用时发出插话，第二次调用瞬断。
    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        del on_chunk
        self.wires.append(json.dumps(kwargs.get("messages") or prompt, ensure_ascii=False))
        if len(self.wires) == 1:
            self.ingress.append(self.client.request_active_turn_input(
                "sess-steer", message=_TEXT, message_id="steer-e", expected_turn_id=self.turn[0]))
            return ModelResponse(text="第一轮回复", backend=self.name)
        if len(self.wires) == 2:
            raise ProviderTransientError("临时服务错误")
        return ModelResponse(text="已按插话调整。", backend=self.name)


def test_tui_steer_survives_a_transient_failure_through_an_isolated_gateway(tmp_path, monkeypatch):
    monkeypatch.setattr(retry, "provider_transient_retry_delays", lambda _policy: (0.01,))
    monkeypatch.setattr(retry, "wait_interruptibly", lambda _delay: None)
    agent, paths = _gateway_agent(tmp_path)
    port = _free_port()
    client = GatewayChatClientAgent(SimpleNamespace(), SimpleNamespace(gateway_port=port), tmp_path / "ws",
                                    [tmp_path / "ws"], SimpleNamespace())
    turn: list[str] = []
    backend = _TuiSteerBackend(client, turn)
    # Gateway 按会话选模另建后端实例（这里是 echo），所以在类上替换生成入口。
    monkeypatch.setattr(EchoBackend, "generate", lambda _self, prompt, *args, **kwargs: backend.generate(prompt, **kwargs))
    # 与生产 gateway run 相同的鉴权中间件：身份取自 TUI 客户端的请求头，而不是默认的 admin。
    server = GatewayHTTPServer(port, paths, params=GatewayHTTPServerParams(
        agent=agent, auth_middleware=_build_gateway_auth_middleware(agent.config)))
    server.start()
    try:
        request_id, _request_path, response_path = submit_gateway_ask(
            paths, params=GatewayAskParams(prompt="开始任务", save=False, chat_session_id="sess-steer", agent=agent))
        turn.append(request_id)
        _process_gateway_requests(agent, paths)
        status = client.request_active_turn_input_status(backend.ingress[0].request_id)
    finally:
        server.stop()

    response = json.loads(response_path.read_text(encoding="utf-8"))
    assert backend.ingress[0].delivery in {ActiveTurnInputDelivery.ACCEPTED, ActiveTurnInputDelivery.UNKNOWN}
    assert response["ok"] and response["response"] == "已按插话调整。"
    assert len(backend.wires) == 3 and [wire.count(_TEXT) for wire in backend.wires] == [0, 1, 1]
    assert status.delivery is ActiveTurnInputDelivery.ACCEPTED  # 入口回执收成 consumed，TUI 不再轮询
    assert read_gateway_input_receipt(paths, backend.ingress[0].request_id).state == "consumed"


# 函数用途: 建一个带 Gateway 目录的 echo Agent，并把一条插话登记成已绑定到后台任务的入口回执。
def _background_input(tmp_path, task_id: str):
    agent, paths = _gateway_agent(tmp_path)
    store = agent.conversation_store
    thread = store.threads.get_or_create({"canonical_user_id": "local-agent"})
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": task_id, "goal": "定时任务"})
    guidance = _steer(store, task_id, "steer-d", target_type="task")
    request_id = "gwreq-msg-steer-d"
    prepared = {"id": request_id, "request_id": request_id, "kind": "ask", "goal": _TEXT, "user_id": "local-agent",
                "metadata": {"client_input_digest": "digest-d", "expected_turn_id": task_id,
                             "gateway_input_request_id": request_id, "message_id": "steer-d"}}
    with gateway_input_transition(paths, request_id):
        receipt, _created = load_or_prepare_gateway_input_locked(
            paths, request_id=request_id, client_input_digest="digest-d", client_message_id="steer-d",
            guidance_dedupe_key="steer-d", prepared_request=prepared)
        bind_gateway_input_active_locked(paths, receipt, target_turn_id=task_id, guidance_dedupe_key="steer-d")
    assert guidance.guidance_id
    return agent, paths, request_id


def test_background_target_steer_waits_while_running_and_queues_after_the_task_ends(tmp_path):
    agent, paths, request_id = _background_input(tmp_path, "srun_test")
    store = agent.conversation_store

    reconcile_gateway_input_receipts(paths, agent)
    assert read_gateway_input_receipt(paths, request_id).state == "active_pending"  # 任务还在跑，不收尾
    assert store.guidance.receipt("steer-d").status == "pending"

    store.tasks.update_status({"task_id": "srun_test", "status": "completed"})
    reconcile_gateway_input_receipts(paths, agent)

    receipt = read_gateway_input_receipt(paths, request_id)
    assert receipt.state == "queued" and (paths.inbox / f"{request_id}.json").is_file()
    assert store.guidance.receipt("steer-d").status == "rejected"
    payload = gateway_input_status_payload(receipt)
    assert (payload["disposition"], payload["delivery_status"]) == ("queued", "accepted")  # TUI 接成下一轮
    store.storage.task_path("srun_broken").write_text("{", encoding="utf-8")
    # 没有任务记录或记录读不出来时不能当终态收尾。
    assert [_input_target_lifecycle(paths, agent, task) for task in ("srun_missing", "srun_broken")] == ["unknown"] * 2
