# LLM: W6回归使用临时Owner与既有原账；纯合同测试不冒充真实模型、部署或外部SSE验收。
# 模块用途: 守住扩展事件、真实用量、请求控制、附件及门户动作接线。
import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.plugin_host import action_specs, composition, requests, streams
from agent_py_agent.agent.plugin_host.contracts import HostDomainPorts, decode_call
from agent_py_agent.agent.plugin_host.conversations import workspace_changes
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.plugin_host.schemas import validate_request_event
from agent_py_agent.tests.test_plugin_host_v2_requests import public_case as public_case


# LLM: 仅造公开事件信封，不把测试数据当作真实模型输出。
# 函数用途: 为闭合合同回归提供统一事件头。
def event(kind, data):
    return {"event_id": "event-one", "sequence": 1, "kind": kind,
            "created_at": "2026-10-27T08:00:00Z", "data": data}


# LLM: 规范终态样本只用于纯投影测试，不伪装真实执行。
# 函数用途: 构造带准确请求身份的终态样本。
def terminal_row(**extra):
    return {"id": "plugin-request-one", "status": "done", "completed_at": 1793088000,
            "terminal_response": {"id": "plugin-request-one", "ok": True}, **extra}


@pytest.mark.parametrize("elapsed", [None, 0, 125])
# LLM: 同时测HTTP共用合同及缓冲，不能只改单一路径。
# 函数用途: 保证可空耗时完整进入SSE帧。
def test_elapsed_ms_survives_both_public_event_validators(elapsed):
    row = event("tool_progress", {"call_id": "call-one", "tool_name": "read_file", "status": "ended",
                                 "summary": "读取完成", "elapsed_ms": elapsed})
    assert validate_request_event(row) == row
    buffer = streams.RequestEventBuffer()
    assert buffer.push(row, "cursor-one")
    assert json.loads(buffer.take(now=1, check_current=lambda: None).split(b"data: ")[1]) == row


@pytest.mark.parametrize("value", [-1, True, 1.5, "1"])
# LLM: 数值不能接受布尔、负数和字符串。
# 函数用途: 固定工具耗时的严格类型边界。
def test_elapsed_ms_rejects_invalid_numbers(value):
    row = event("tool_progress", {"call_id": "call-one", "tool_name": "read_file", "status": "ended",
                                 "summary": "读取完成", "elapsed_ms": value})
    with pytest.raises(HostAPIError):
        validate_request_event(row)


@pytest.mark.parametrize("extended", [False, True])
# LLM: 新合同必须兼容旧两字段，不靠删除旧断言造绿。
# 函数用途: 同测旧终态和扩展用量的可运输性。
def test_terminal_usage_remains_backward_compatible(extended):
    data = requests._terminal_view(terminal_row())
    if extended:
        data["usage"].update(cached_tokens=3, cache_read_tokens=3, cache_write_tokens=None,
                             total_tokens=15, elapsed_seconds=0.5, tokens_per_second=10.0)
    row = event("terminal", data)
    assert validate_request_event(row) == row
    assert streams.RequestEventBuffer().push(row, "cursor-one")


@pytest.mark.parametrize("source", ["usage", "result", "terminal_response"])
# LLM: 仅检查同一原请求记录中真实字段的投影，不推算供应商用量。
# 函数用途: 固定三种已有计量来源的读取合同。
def test_terminal_reads_real_usage_from_original_request(source):
    metrics = {"input_tokens": 12, "output_tokens": 6, "cached_tokens": 4,
               "elapsed_seconds": 1.5, "tokens_per_second": 4.0}
    row = terminal_row()
    if source == "usage":
        row[source] = metrics
    elif source == "result":
        row[source] = {"usage": metrics}
    else:
        row[source]["usage"] = metrics
    assert requests._terminal_view(row)["usage"] == metrics


# LLM: 零是已记录值，不能被falsey回退覆盖。
# 函数用途: 固定源优先级并保留真实零值。
def test_terminal_usage_prefers_request_over_other_sources_and_keeps_zero():
    row = terminal_row(usage={"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
                              "elapsed_seconds": 0, "tokens_per_second": 0},
                       result={"usage": {"input_tokens": 999}})
    assert requests._terminal_view(row)["usage"] == row["usage"]


PORTAL_ACTIONS = {
    "conversations.threads.settings.get": ("read", {"read"}),
    "conversations.threads.settings.update": ("write", {"threads.manage"}),
    "conversations.threads.packs.get": ("read", {"read"}),
    "conversations.threads.packs.set": ("write", {"threads.manage"}),
    "conversations.threads.model.set": ("write", {"threads.manage"}),
    "conversations.threads.permission_mode.set": ("write", {"threads.manage"}),
    "conversations.threads.usage.get": ("read", {"read"}),
    "conversations.threads.compact": ("execute", {"chat", "threads.manage"}),
    "conversations.threads.delete": ("write", {"threads.manage"}),
    "conversations.requests.interject": ("execute", {"chat"}),
    "subagents.list": ("read", {"tasks.read"}), "subagents.get": ("read", {"tasks.read"}),
    "subagents.steer": ("execute", {"tasks.control"}), "subagents.stop": ("execute", {"tasks.control"}),
    "models.providers.list": ("read", {"read"}), "models.providers.save": ("write", {"write"}),
    "models.providers.delete": ("write", {"write"}), "models.profiles.delete": ("write", {"write"}),
    "plugins.builtin.list": ("read", {"read"}), "plugins.builtin.toggle": ("write", {"write"}),
    "workspace.files.list": ("read", {"read"}), "workspace.files.read": ("read", {"read"}),
    "workspace.files.upload": ("write", {"write"}), "workspace.files.download": ("read", {"read"}),
    "workspace.git.diff": ("read", {"read"}), "workspace.git.inspect": ("read", {"read"}),
    "processes.background.stop": ("execute", {"tasks.control"}),
    "user_space.identity.resolve": ("read", {"read"}), "capability_packages.list": ("read", {"read"}),
}


@pytest.mark.parametrize("name", PORTAL_ACTIONS)
# LLM: 新动作逐项核名称/能力，不放宽旧目录测试。
# 函数用途: 确认29个门户入口有闭合静态合同。
def test_all_portal_actions_have_exact_capability_contract(name):
    assert name in action_specs.action_specs()
    spec = action_specs.action_spec(name)
    assert (spec.mode, spec.capabilities) == PORTAL_ACTIONS[name]
    assert spec.payload_schema["additionalProperties"] is False


# LLM: 只验证公开解码，不把schema通过当作附件消费证明。
# 函数用途: 固定附件与线程设置可选字段的入站合同。
def test_message_attachment_schema_and_create_fields_are_accepted():
    payload = {"thread_id": "thread-one", "text": "hello", "context_paths": ["src/main.py"],
               "input_media": [{"sha256": "a" * 64, "path": "/fixture/media", "media_type": "image/png"}]}
    row = decode_call(json.dumps({"schema": "plugin_host_call.v2", "action": "conversations.messages.send",
                                  "idempotency_key": "fixture-key-00000001", "payload": payload}).encode())
    assert tuple(row.payload["context_paths"]) == ("src/main.py",)
    created = action_specs.action_spec("conversations.threads.create").validate_payload({
        "model_profile_id": "fixture-model", "reasoning_effort": "high", "permission_mode": "ask",
        "preferred_pack_ids": ["pack-one"], "pack_ids": ["pack-one"]})
    assert created["model_profile_id"] == "fixture-model"


# LLM: 模式来自显式枚举，不从正文推断。
# 函数用途: 验证插话别名、默认值和非法模式拒绝。
def test_interject_schema_checks_mode_and_message_alias():
    assert "conversations.requests.interject" in action_specs.action_specs()
    spec = action_specs.action_spec("conversations.requests.interject")
    assert spec.validate_payload({"thread_id": "thread-one", "message": "补充"})["mode"] == "steer"
    with pytest.raises(HostAPIError):
        spec.validate_payload({"thread_id": "thread-one", "text": "补充", "mode": "all"})


# LLM: 缺业务后端不能由通用观察口伪装成功。
# 函数用途: 守住兄弟模块尚未合入时的未就绪状态。
def test_missing_sibling_handler_is_unavailable_not_an_observation_fallback():
    assert "models.providers.list" in action_specs.action_specs()
    ports = HostDomainPorts(observations=SimpleNamespace(call=lambda _: pytest.fail("wrong domain")))
    assert composition.domain_name("models.providers.list") == "portal_v2"
    assert composition.domain_ready("models.providers.list", ports) is False


# LLM: 只读投影不可经可变别名回写原账。
# 函数用途: 验证用量结果与来源记录互相独立。
def test_terminal_does_not_mutate_original_usage():
    row = terminal_row(usage={"input_tokens": 12, "output_tokens": 6})
    before = deepcopy(row)
    result = requests._terminal_view(row)
    result["usage"]["input_tokens"] = 1
    assert row == before


# LLM: 所有入站仍用真实激活、issuer、MAC及原input/queue；只替换不涉及模型的执行准备端口。
# 函数用途: 为新接线回归建立一条真实Owner请求，不运行模型或访问真实用户资料。
def queued_case(case):
    from agent_py_agent.agent.gateway_parts.plugin_request_source import PluginRequestPorts
    from agent_py_agent.tests.test_plugin_host_v2_requests import _public_call

    _, domain, _, paths, _, home, *_ = case
    domain.request_ports = PluginRequestPorts(lambda _: paths, lambda a, c, e, p: {
        **p, "owner_id": home.owner_id, "conversation": {"thread_id": a.call.payload["thread_id"]}})
    thread = _public_call(case, "threads.create", {}, "fixture-create-00001").data["thread_id"]
    sent = _public_call(case, "messages.send", {"thread_id": thread, "text": "hello"}, "fixture-send-0000001")
    return thread, sent.data["request_id"]


# LLM: 全部领域调用使用原解码和真实发行器，不手造授权DTO。
# 函数用途: 生成当前真实Owner的测试调用。
def authorized_call(case, action, payload, key=None):
    comp, _, _, _, source, *_ = case
    value = {"schema": "plugin_host_call.v2", "action": action, "payload": payload}
    if key:
        value["idempotency_key"] = key
    return comp.issuer.authorize(source, decode_call(json.dumps(value).encode()))


@pytest.mark.parametrize("mode,delivery", [("steer", "interrupt"), ("interrupt", "interrupt"), ("queue", "next_turn")])
# LLM: 实际临时原队列与guidance均落账；不运行真实模型。
# 函数用途: 验证三种插话模式及准确回合关联。
def test_interject_uses_original_guidance_and_records_running_request(public_case, mode, delivery):
    from agent_py_agent.agent.gateway_parts.queue_service import claim_request

    thread, request_id = queued_case(public_case)
    comp, _, store, paths, *_ = public_case
    claim = claim_request(paths, paths.inbox / (request_id + ".json"))
    assert claim is not None
    call = authorized_call(public_case, "conversations.requests.interject", {
        "thread_id": thread, "request_id": request_id, "message": "补充这条线索", "mode": mode})
    result = composition.dispatch_call(call, comp.ports)
    assert result.data["accepted"] is True
    entry, = store.guidance.pending(target_type="thread", target_id=thread)
    assert entry.delivery == delivery and entry.message == "补充这条线索"
    assert entry.metadata.get("expected_turn_id", "") == ("" if mode == "queue" else request_id)
    assert json.loads(claim.path.read_bytes())["interjections"][0]["guidance_id"] == entry.guidance_id


# LLM: 拒绝错误线程必须先于任何guidance写入。
# 函数用途: 验证原请求归属不是仅靠传入thread_id声称。
def test_interject_cannot_cross_thread_or_leak_to_a_completed_request(public_case):
    from agent_py_agent.tests.test_plugin_host_v2_requests import _public_call

    thread, request_id = queued_case(public_case)
    other = _public_call(public_case, "threads.create", {}, "fixture-create-00002").data["thread_id"]
    comp, _, store, *_ = public_case
    call = authorized_call(public_case, "conversations.requests.interject", {
        "thread_id": other, "request_id": request_id, "text": "cannot cross"})
    with pytest.raises(HostAPIError) as failure:
        composition.dispatch_call(call, comp.ports)
    assert failure.value.code == "RESOURCE_NOT_FOUND"
    assert not store.guidance.pending(target_type="thread", target_id=thread)


# LLM: 原writer已提交事件在终态前读取，MAC重连不重复；这里只测进程内真实接线。
# 函数用途: 验证过程事件和准确续读顺序。
def test_live_commentary_and_tools_arrive_before_terminal_and_resume(public_case):
    from agent_py_agent.agent.gateway_parts.queue_service import claim_request
    from agent_py_agent.agent.gateway_parts.stream_writer import write_chunk_event

    thread, request_id = queued_case(public_case)
    _, domain, _, paths, *_ = public_case
    claim = claim_request(paths, paths.inbox / (request_id + ".json"))
    chunk = claim.path.with_name(request_id + ".chunks.jsonl")
    write_chunk_event(chunk, {"kind": "assistant_commentary", "text": "开始核对"})
    write_chunk_event(chunk, {"kind": "tool_progress", "progress": {"call_id": "call-one", "tool_name": "read_file",
        "phase": "started", "elapsed_ms": 0}, "text": "读取"})
    call = authorized_call(public_case, "conversations.requests.stream", {"request_id": request_id})
    iterator = domain.stream(call)
    first = next(iterator)
    assert first["kind"] == "assistant_delta" and first["data"]["part"] == "commentary"
    assert first["data"]["text"] == "开始核对"
    iterator.close()
    resumed = domain.stream(authorized_call(public_case, "conversations.requests.stream", {
        "request_id": request_id, "event_cursor": first["event_id"]}))
    second = next(resumed)
    assert second["sequence"] == first["sequence"] + 1
    assert second["kind"] == "tool_progress" and second["data"]["call_id"] == "call-one"
    assert second["data"]["elapsed_ms"] == 0
    resumed.close()


@pytest.mark.parametrize("phase,expected", [("started", "started"), ("finished", "ended"),
                                           ("deferred", "ended"), ("interrupted", "ended")])
# LLM: 调用真正的原工具事件序列化器，时钟受控；不执行工具或模型，不把展示ID当控制令牌。
# 函数用途: 守住原生tool/phase/round/call_index与中文status的真实接缝。
def test_native_tool_producer_fields_are_projected_without_legacy_arguments(monkeypatch, phase, expected):
    from agent_py_agent.agent.agent_core.tool_loop import round_execution

    monkeypatch.setattr(round_execution, "time", SimpleNamespace(monotonic=lambda: 12.0))
    native = round_execution._structured_tool_progress(round_execution.ToolProgressEvent(
        request=SimpleNamespace(tool_rounds=2), idx=1, call=SimpleNamespace(tool_name="read_file"),
        phase=phase, status="中文展示状态", started_at=11.5), "read_file", "")
    row = {"kind": "tool_progress", "progress": native, "text": "private-legacy-arguments"}
    projected = streams._progress_event(row, "fixture-request-one", 0)
    assert projected is not None
    kind, data, _ = projected
    assert kind == "tool_progress" and data["tool_name"] == "read_file"
    assert data["status"] == expected and data["elapsed_ms"] == 500
    assert data["summary"] == "中文展示状态"
    assert data["call_id"].startswith("tool-")
    assert streams._progress_event(row, "fixture-request-one", 999)[1]["call_id"] == data["call_id"]
    assert streams._progress_event(row, "fixture-request-two", 0)[1]["call_id"] != data["call_id"]
    row["progress"] = {**native, "call_index": 2}
    assert streams._progress_event(row, "fixture-request-one", 0)[1]["call_id"] != data["call_id"]
    assert "private-legacy-arguments" not in json.dumps(data)
    validate_request_event(event(kind, data))


# LLM: 原序列化器、Gateway writer、原chunk、发行器及stream全走真接线；仅工具事件输入受控，没有执行真实工具/模型。
# 函数用途: 验证原生工具开始与完成在请求终态前成对送达且不泄漏旧文本参数。
def test_native_tool_writer_reaches_stream_before_terminal(public_case):
    from agent_py_agent.agent.agent_core.tool_loop.round_execution import (
        ToolProgressEvent,
        _structured_tool_progress,
    )
    from agent_py_agent.agent.gateway_parts.queue_service import claim_request
    from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter

    _, request_id = queued_case(public_case)
    _, domain, _, paths, *_ = public_case
    claim = claim_request(paths, paths.inbox / (request_id + ".json"))
    writer = BufferedChunkStreamWriter(claim.path.with_name(request_id + ".chunks.jsonl"), rich_transcript=True)
    for phase in ("started", "finished"):
        native = _structured_tool_progress(ToolProgressEvent(
            request=SimpleNamespace(tool_rounds=2), idx=1, call=SimpleNamespace(tool_name="read_file"),
            phase=phase, status="原生展示状态"), "read_file", "")
        writer.write_progress(native, "private-legacy-arguments")
    iterator = domain.stream(authorized_call(public_case, "conversations.requests.stream", {"request_id": request_id}))
    first, second = next(iterator), next(iterator)
    iterator.close()
    assert first["kind"] == second["kind"] == "tool_progress"
    assert first["data"]["status"] == "started" and second["data"]["status"] == "ended"
    assert first["data"]["call_id"] == second["data"]["call_id"]
    assert second["sequence"] == first["sequence"] + 1
    assert "private-legacy-arguments" not in json.dumps([first, second])
    assert claim.path.is_file() and not (paths.done / claim.path.name).exists()


# LLM: 文件/媒体来自临时Owner；引用与原输入指纹一同保存，不涉及真实用户资料。
# 函数用途: 验证受理后修改文件不会改变已冻结正文。
def test_frozen_workspace_context_and_media_are_in_canonical_input(public_case):
    from agent_py_agent.agent.conversation.input_media import (
        import_input_media_bytes,
        owner_input_media_root,
    )
    from agent_py_agent.tests.test_plugin_host_v2_requests import _public_call

    thread, _ = queued_case(public_case)
    _, _, store, paths, _, home, *_ = public_case
    root = home.workspace_dir
    root.mkdir(parents=True, exist_ok=True)
    file = root / "fixture.txt"
    file.write_text("frozen-file-context")
    store.threads.update_atomic(thread, lambda value: replace(value, **workspace_changes(value, root)))
    media = import_input_media_bytes(b"fixture-image", owner_input_media_root(home.home_dir))
    result = _public_call(public_case, "messages.send", {"thread_id": thread, "text": "带附件", "context_paths": ["fixture.txt"],
        "input_media": [media]}, "fixture-attach-00001")
    raw = json.loads((paths.inbox / (result.data["request_id"] + ".json")).read_bytes())
    assert raw["input_payload"]["context_paths"] == [str(file)]
    assert raw["input_payload"]["input_media"] == [media]
    assert raw["input_media"] == [media]
    file.write_text("changed-after-submit")
    assert "frozen-file-context" in raw["goal"] and "changed-after-submit" not in raw["goal"]


# LLM: 穿越和符号链接必须拒绝，不能丢附件后仍返回文本成功。
# 函数用途: 覆盖附件工作根与nofollow反例。
def test_workspace_attachment_cannot_escape_or_follow_symlinks(public_case, tmp_path):
    from agent_py_agent.tests.test_plugin_host_v2_requests import _public_call

    thread, _ = queued_case(public_case)
    _, _, store, _, _, home, *_ = public_case
    home.workspace_dir.mkdir(parents=True, exist_ok=True)
    store.threads.update_atomic(thread, lambda value: replace(value, **workspace_changes(value, home.workspace_dir)))
    (home.workspace_dir / "escape.txt").symlink_to(tmp_path / "private.txt")
    for index, name in enumerate(("../private.txt", "escape.txt")):
        with pytest.raises(HostAPIError):
            _public_call(public_case, "messages.send", {"thread_id": thread, "text": "refuse", "context_paths": [name]},
                         "fixture-escape-0000" + str(index))


# LLM: 使用原命名中断注册表，不把取消标记当作物理停止；规范终态仍未知。
# 函数用途: 验证运行中取消确实向准确线程立旗。
def test_running_cancel_signals_the_exact_registered_request(public_case):
    from agent_py_agent.agent.concurrency.interrupt import is_interrupted, register_interruptible
    from agent_py_agent.agent.conversation.control_commands import (
        conversation_request_interrupt_name,
    )
    from agent_py_agent.agent.gateway_parts.plugin_request_source import PluginRequestPorts
    from agent_py_agent.agent.gateway_parts.queue_service import claim_request
    from agent_py_agent.agent.plugin_host.gateway_runtime import GatewayPluginRequests
    from agent_py_agent.tests.test_plugin_host_v2_requests import _public_call

    _, request_id = queued_case(public_case)
    _, domain, store, paths, *_ = public_case
    claim_request(paths, paths.inbox / (request_id + ".json"))
    runtime = GatewayPluginRequests(SimpleNamespace(server=SimpleNamespace(agent=None), store_for=lambda _: store))
    runtime.conversation = domain
    domain.request_ports = PluginRequestPorts(lambda _: paths, domain.request_ports.prepare_request, runtime.cancel)
    revision = _public_call(public_case, "requests.poll", {"request_id": request_id}).data["request"]["revision"]
    with register_interruptible(conversation_request_interrupt_name(request_id)):
        assert not is_interrupted()
        result = _public_call(public_case, "requests.cancel", {"request_id": request_id, "expected_revision": revision},
                              "fixture-cancel-00001")
        assert is_interrupted()
        assert result.data["state"] == "cancelling" and result.data["terminal"] is None


@pytest.mark.parametrize("field", ["model_metrics", "model_context_usage"])
# LLM: 仅同请求快照可作单请求用量，不能借线程累计值补数。
# 函数用途: 覆盖原计量/上下文快照中的标准token及速率字段。
def test_request_scoped_metric_snapshot_is_not_lost(field):
    row = terminal_row()
    row["terminal_response"][field] = {"input_tokens": 10, "output_tokens": 5,
        "cache_read_input_tokens": 2, "elapsed_seconds": 0.5, "output_tps": 10.0}
    usage = requests._terminal_view(row)["usage"]
    assert usage["input_tokens"] == 10 and usage["output_tokens"] == 5
    assert usage["cached_tokens"] == 2 and usage["tokens_per_second"] == 10.0


# LLM: chat本身不赋予附件文件读取能力。
# 函数用途: 验证新增附件参数仍受独立read能力约束。
def test_attachment_capability_is_not_implicitly_granted():
    from agent_py_agent.agent.plugin_host.authorization import _check_capabilities

    call = decode_call(json.dumps({"schema": "plugin_host_call.v2", "action": "conversations.messages.send",
        "idempotency_key": "fixture-key-00000001", "payload": {"thread_id": "thread-one", "text": "read",
        "context_paths": ["fixture.txt"]}}).encode())
    with pytest.raises(HostAPIError) as failure:
        _check_capabilities(call, frozenset({"chat"}), own=True)
    assert failure.value.code == "CAPABILITY_DENIED"


@pytest.fixture
# LLM: 真实安装、授权和原控制账均用临时Owner，不替换issuer。
# 函数用途: 为门户接缝测试建立有写能力的独立宿主。
def portal_case(tmp_path):
    from agent_py_agent.agent import plugin_host_api as api
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.gateway_parts.queue_service import ensure_gateway_folders
    from agent_py_agent.agent.plugin_host.conversations import ConversationDomain
    from agent_py_agent.agent.plugin_host.gateway_composition import GatewayPortalDomain
    from agent_py_agent.tests.test_plugin_host_v2_grants import active_grant

    caps = ["read", "write", "chat", "threads.manage"]
    home, install, entry, ref = active_grant(tmp_path, caps)
    api.set_host_api_base("http://127.0.0.1:8420")
    token = api.issue_host_api_env(ref, entry.manifest.plugin_id)[api.HOST_API_TOKEN_ENV]
    host = composition.HostComposition(capability_policy=lambda: frozenset(caps))
    store = ConversationStore(home.data_dir / "conversations")
    paths = gateway_paths_from_root(home.data_dir / "gateway")
    ensure_gateway_folders(paths)
    conversation = ConversationDomain(lambda _: store, host.cursors, default_thread=lambda *_: False)
    resources = SimpleNamespace(store_for=lambda _: store, paths_for=lambda _: paths, server=SimpleNamespace(agent=None))
    conversation.portal_v2 = GatewayPortalDomain(resources, conversation)
    host.ports = HostDomainPorts(conversation=conversation)
    try:
        yield host, conversation, store, paths, host.issuer.ingress(token), home, install, entry
    finally:
        host.close()
        api.set_host_api_base(None)


# LLM: 仅替代未合入的兄弟函数，幂等与Owner使用真实原服务；不外推W4集成完成。
# 函数用途: 验证惰性分派及同key只执行一次。
def test_lazy_provider_backend_uses_owner_and_original_write_receipt(portal_case, monkeypatch):
    import sys
    from types import ModuleType

    name = "agent_py_agent.agent.plugin_host.model_settings_v2_handlers"
    module = ModuleType(name)
    calls = []

    # LLM: 桩只返回公开字段，不读写任何服务商配置。
    # 函数用途: 观察实际调用的Owner与执行次数。
    def save(home, **payload):
        calls.append((home.owner_id, payload))
        return {"provider_id": payload["provider_id"], "revision": len(calls)}

    module.save_provider = save
    monkeypatch.setitem(sys.modules, name, module)
    host, _, _, _, _, home, *_ = portal_case
    call = authorized_call(portal_case, "models.providers.save", {
        "provider_id": "fixture-provider", "api_base": "https://fixture.invalid/v1", "models": []}, "fixture-provider-001")
    first = composition.dispatch_call(call, host.ports)
    second = composition.dispatch_call(call, host.ports)
    assert first.data == second.data
    assert len(calls) == 1 and calls[0][0] == home.owner_id


# LLM: 合并事件校验器不能丢掉原稳定错误码约束。
# 函数用途: 防止原异常文字冒充公开终态码。
def test_stream_terminal_rejects_lowercase_error_codes():
    data = requests._terminal_view(terminal_row())
    data["error_code"] = "not_public"
    with pytest.raises(HostAPIError):
        streams.RequestEventBuffer().push(event("terminal", data), "cursor-one")


# LLM: 使用明确假凭据，检查真实原控制账，不处理任何真实秘密。
# 函数用途: 验证秘密只参与摘要而不额外落入操作回执。
def test_provider_secret_is_not_copied_into_control_receipts(portal_case, monkeypatch):
    import sys
    from types import ModuleType

    name = "agent_py_agent.agent.plugin_host.model_settings_v2_handlers"
    module = ModuleType(name)
    module.save_provider = lambda home, **data: {"provider_id": data["provider_id"]}
    monkeypatch.setitem(sys.modules, name, module)
    host, _, _, paths, *_ = portal_case
    call = authorized_call(portal_case, "models.providers.save", {"provider_id": "fixture-provider",
        "api_base": "https://fixture.invalid/v1", "models": [], "api_key": "fixture-not-a-real-secret"}, "fixture-secret-00001")
    assert composition.dispatch_call(call, host.ports).data["provider_id"] == "fixture-provider"
    assert all("fixture-not-a-real-secret" not in path.read_text() for path in paths.root.rglob("*.json"))


# LLM: Unicode分片按实发字节推进，当前行替换必须让旧MAC位置失效。
# 函数用途: 同测多字节无损续读与已读分片的来源绑定。
def test_partial_multibyte_progress_replays_every_byte_and_rejects_replacement(public_case):
    from agent_py_agent.agent.gateway_parts.stream_writer import write_chunk_event

    _, request_id = queued_case(public_case)
    _, _, _, paths, *_ = public_case
    chunk = paths.processing / (request_id + ".chunks.jsonl")
    text = "核对线索" * 4000
    write_chunk_event(chunk, {"kind": "assistant_commentary", "text": text})
    facts = {"request_id": request_id, "terminal": None}
    position, pieces = {}, []
    while True:
        value, position, more = streams.request_progress(paths, facts, position)
        if value:
            assert value[1]["offset_bytes"] == len("".join(pieces).encode())
            pieces.append(value[1]["text"])
        if not more:
            break
    assert "".join(pieces) == text
    _, first_position, _ = streams.request_progress(paths, facts, {})
    chunk.write_text(json.dumps({"kind": "assistant_commentary", "text": "不相同" * 4000}) + "\n")
    with pytest.raises(HostAPIError) as failure:
        streams.request_progress(paths, facts, first_position)
    assert failure.value.code == "CURSOR_EXPIRED"


# LLM: 规范终态后的坏尾不能被解释成仍在等待writer。
# 函数用途: 验证损坏进度尾部显式失败而不无限轮询。
def test_canonical_terminal_with_a_partial_progress_line_fails_instead_of_waiting_forever(public_case):
    _, request_id = queued_case(public_case)
    _, _, _, paths, *_ = public_case
    (paths.processing / (request_id + ".chunks.jsonl")).write_bytes(b'{"kind":')
    with pytest.raises(HostAPIError) as failure:
        streams.request_progress(paths, {"request_id": request_id, "terminal": {"status": "succeeded"}}, {})
    assert failure.value.code == "STORE_UNAVAILABLE"


# LLM: 使用真实Gateway准备接缝；显示能力不是工具/网络授权。
# 函数用途: 验证原producer确实开启公开typed事件出口。
def test_gateway_message_preparation_enables_the_public_typed_stream(public_case):
    from agent_py_agent.agent.gateway_parts.plugin_request_source import PluginRequestPorts
    from agent_py_agent.agent.plugin_host.gateway_runtime import GatewayPluginRequests
    from agent_py_agent.tests.test_plugin_host_v2_requests import _public_call

    thread, _ = queued_case(public_case)
    _, domain, store, paths, *_ = public_case
    runtime = GatewayPluginRequests(SimpleNamespace(paths_for=lambda _: paths, store_for=lambda _: store))
    runtime.conversation = domain
    domain.request_ports = PluginRequestPorts(lambda _: paths, runtime.prepare)
    result = _public_call(public_case, "messages.send", {"thread_id": thread, "text": "stream"}, "fixture-rich-0000001")
    row = json.loads((paths.inbox / (result.data["request_id"] + ".json")).read_bytes())
    assert row["client_capabilities"]["rich_transcript"] is True


# LLM: 排队状态也要写入同一关联记录，不能让原限额只约束运行态。
# 函数用途: 覆盖第64条插话后的明确拒绝。
def test_pending_queue_interjections_share_the_request_limit(public_case):
    from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic

    thread, request_id = queued_case(public_case)
    host, _, _, paths, *_ = public_case
    path = paths.inbox / (request_id + ".json")
    update_json_file_atomic(path, lambda row: {**row, "interjections": [
        {"guidance_id": "fixture-" + str(i), "mode": "queue"} for i in range(63)]})
    call = authorized_call(public_case, "conversations.requests.interject", {
        "thread_id": thread, "request_id": request_id, "text": "queue", "mode": "queue"})
    assert composition.dispatch_call(call, host.ports).data["accepted"] is True
    assert len(json.loads(path.read_bytes())["interjections"]) == 64
    with pytest.raises(HostAPIError) as failure:
        composition.dispatch_call(call, host.ports)
    assert failure.value.code == "PAYLOAD_TOO_LARGE"


# LLM: 只在临时日志造大样本，不扫描真实Owner；每次内存窗口保持有界。
# 函数用途: 验证长日志越过旧整文件上限后仍能推进到真实事件。
def test_progress_log_larger_than_four_megabytes_is_read_in_bounded_windows(public_case):
    _, request_id = queued_case(public_case)
    _, _, _, paths, *_ = public_case
    path = paths.processing / (request_id + ".chunks.jsonl")
    ignored = json.dumps({"kind": "ignored", "data": "x" * 65536}).encode() + b"\n"
    row = {"kind": "tool_progress", "progress": {"call_id": "last-call", "tool_name": "read_file", "phase": "finished"}, "text": "done"}
    path.write_bytes(ignored * 65 + json.dumps(row).encode() + b"\n")
    position = {}
    for _ in range(10):
        value, position, more = streams.request_progress(paths, {"request_id": request_id, "terminal": None}, position)
        if value:
            assert value[0] == "tool_progress" and value[1]["call_id"] == "last-call"
            return
        assert more
    pytest.fail("reader did not advance through the bounded windows")
