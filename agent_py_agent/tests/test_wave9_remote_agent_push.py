# LLM: 单向推送合同回归只使用临时宿主、真实本机HTTP和有界原代码；拒绝条件不通过删断言绕过。
# 模块用途: 覆盖鉴权、耐久防重放、幂等、脱敏分叉、节点安全和外部来源隔离。
"""单向推送合同：真实HTTP入站与原队列，不调用模型或外部站点。"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import time
import uuid

import pytest

from agent_py_agent.agent.common.json_io import write_private_json_file_atomic
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root


# LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
# 函数用途: 创建临时echo Agent和真实回环HTTP监听器，退出关闭监听器。
@pytest.fixture
def receiver(tmp_path):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings.config import AgentConfig

    config = AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"))
    agent = SimpleAgent(config, tmp_path)
    paths = gateway_paths_from_root(tmp_path / "queue")
    server = GatewayHTTPServer(0, paths, params=GatewayHTTPServerParams(agent=agent))
    secret = "test-only-push-" + "a" * 64
    record = {"token_id": "test-node", "token_sha256": hashlib.sha256(secret.encode()).hexdigest(),
              "allowed_target_owners": ["local/main"], "allowed_actions": ["push_event"],
              "allowed_source_ips": [], "rate_limit_per_minute": 30, "enabled": True, "expires_at": None}
    token_path = agent.home_paths.root / "remote_push" / "tokens.json"
    write_private_json_file_atomic(token_path, {"tokens": [record], "trusted_proxies": []})
    server.start()
    try:
        yield server, agent, secret, token_path
    finally:
        server.stop()


# LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
# 函数用途: 生成不含授权字段的独立外部事件正文。
def payload(**updates):
    return {"target_owner": "local/main", "instruction": "分析这条外部事件，不是用户确认。",
            "title": "测试告警", "idempotency_key": uuid.uuid4().hex, "thread_mode": "new_thread", **updates}


# LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
# 函数用途: 给测试正文生成当前时间窗内的真实HMAC和唯一nonce。
def signed(body, secret, *, timestamp=None, nonce=None):
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    stamp, nonce = str(int(time.time()) if timestamp is None else timestamp), nonce or uuid.uuid4().hex
    message = f"{stamp}.{nonce}.{hashlib.sha256(raw).hexdigest()}".encode()
    return raw, {"Content-Type": "application/json", "X-Agent-Push-Token": secret,
                 "X-Agent-Timestamp": stamp, "X-Agent-Nonce": nonce,
                 "X-Agent-Signature": hmac.digest(secret.encode(), message, "sha256").hex()}


# LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
# 函数用途: 通过真实回环HTTP提交原始请求并读回状态和JSON。
def post(server, raw, headers, path="/remote-agent/push"):
    connection = http.client.HTTPConnection("127.0.0.1", server.server.server_address[1], timeout=10)
    try:
        connection.request("POST", path, body=raw, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


# LLM: 原始bytes也必须由发送方准确签名；只用于本机HTTP反例，不发外网。
# 函数用途: 为非法JSON或重复头测试准备真实HMAC。
def signed_raw(raw, secret):
    _unused, headers = signed({}, secret)
    message = f"{headers['X-Agent-Timestamp']}.{headers['X-Agent-Nonce']}.{hashlib.sha256(raw).hexdigest()}".encode()
    headers["X-Agent-Signature"] = hmac.digest(secret.encode(), message, "sha256").hex()
    return raw, headers


# LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
# 函数用途: 验证nonfinite_json_is_rejected_before_enqueue的准确成功或拒绝合同，不降低原断言。
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_json_is_rejected_before_enqueue(receiver, value):
    server, _agent, secret, _path = receiver
    status, receipt = post(server, *signed(payload(context_payload={"reading": value}), secret))
    assert (status, receipt.get("error_code")) == (400, "REMOTE_PUSH_JSON_INVALID")
    assert not list(server.paths.inbox.glob("*.json"))


# LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
# 函数用途: 验证nonascii_signature_is_auth_failure_not_service_failure的准确成功或拒绝合同，不降低原断言。
def test_nonascii_signature_is_auth_failure_not_service_failure(receiver):
    server, _agent, secret, _path = receiver
    raw, headers = signed(payload(), secret)
    headers["X-Agent-Signature"] = chr(233) * 64
    status, receipt = post(server, raw, headers)
    assert (status, receipt.get("error_code")) == (401, "REMOTE_PUSH_SIGNATURE_INVALID")


# LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
# 函数用途: 验证host_push_authority_is_not_model_writable的准确成功或拒绝合同，不降低原断言。
def test_host_push_authority_is_not_model_writable(receiver):
    from agent_py_agent.agent.path_access_policy import PathAccessPolicy
    _server, agent, _secret, path = receiver
    policy = PathAccessPolicy.from_values(mode="full", agent_home_root=agent.home_paths.root)
    for name in (path.name, "nonces.json", "receipts.json", "endpoints.json"):
        assert not policy.check_write(path.parent / name).allowed


# LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
# 函数用途: 验证outbound_transport_exception_never_claims_not_started的准确成功或拒绝合同，不降低原断言。
def test_outbound_transport_exception_never_claims_not_started(receiver, monkeypatch):
    from agent_py_agent.agent.tooling import remote_agent_push_tool as module
    _server, agent, secret, _path = receiver
    module.save_remote_endpoint(agent, "remote", "https://example.com/remote-agent/push", secret)
    # LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
    # 函数用途: 模拟进入发送阶段后连接丢失，不能声明未开始。
    def fail_after_send(*_args):
        raise OSError("simulated transport loss after POST")
    monkeypatch.setattr(module, "send_remote_push", fail_after_send)
    result = agent.tools.tools["remote_agent_push"].execute({"endpoint_ref": "remote", **payload()})
    assert not result.ok and result.effect_outcome == "unknown"


# LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
# 函数用途: 验证nonce_is_durable_across_processes_and_expires的准确成功或拒绝合同，不降低原断言。
def test_nonce_is_durable_across_processes_and_expires(tmp_path):
    import subprocess
    import sys

    from agent_py_agent.agent.gateway_parts.remote_push_service import consume_push_nonce
    root = tmp_path / "remote_push"
    root.mkdir()
    consume_push_nonce(root, "node", "a" * 32, 1000)
    code = ("from pathlib import Path; import sys; "
            "from agent_py_agent.agent.gateway_parts.remote_push_service import consume_push_nonce,RemotePushError\n"
            "try: consume_push_nonce(Path(sys.argv[1]),'node','a'*32,float(sys.argv[2]))\n"
            "except RemotePushError as exc: print(exc.error_code)\n"
            "else: print('consumed')\n")
    for timestamp, expected in ((1001, "REMOTE_PUSH_NONCE_REPLAYED"), (1601, "consumed")):
        result = subprocess.run([sys.executable, "-c", code, str(root), str(timestamp)],
            capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == expected


# LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
# 函数用途: 验证prepared_receipt_recovers_without_duplicate_queue_or_context的准确成功或拒绝合同，不降低原断言。
def test_prepared_receipt_recovers_without_duplicate_queue_or_context(receiver, monkeypatch):
    from agent_py_agent.agent.gateway_parts import remote_push_service as module
    server, agent, secret, _path = receiver
    original = module.enqueue_remote_push
    # LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
    # 函数用途: 模拟原队列已提交但推送回执尚未确认的中断。
    def crash_after_commit(*args):
        original(*args)
        raise OSError("simulated post-queue crash")
    monkeypatch.setattr(module, "enqueue_remote_push", crash_after_commit)
    body = payload(context_snapshot=[{"role": "tool", "content": "准确旧证据"}])
    assert post(server, *signed(body, secret))[0] == 503
    monkeypatch.setattr(module, "enqueue_remote_push", original)
    status, receipt = post(server, *signed(body, secret))
    assert status == 202, receipt
    assert len(list(server.paths.inbox.glob("*.json"))) == 1
    assert len(agent.conversation_store.messages.recent(receipt["thread_id"], limit=0)) == 1


# LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
# 函数用途: 验证listener_restart_preserves_replay_and_business_receipt的准确成功或拒绝合同，不降低原断言。
def test_listener_restart_preserves_replay_and_business_receipt(receiver):
    server, agent, secret, _path = receiver
    body = payload()
    raw, headers = signed(body, secret)
    status, first = post(server, raw, headers)
    assert status == 202
    server.stop()
    replacement = GatewayHTTPServer(0, server.paths, params=GatewayHTTPServerParams(agent=agent))
    replacement.start()
    try:
        assert post(replacement, raw, headers)[0] == 409
        status, duplicate = post(replacement, *signed(body, secret))
        assert status == 202 and duplicate["duplicate"]
        assert duplicate["push_id"] == first["push_id"]
        assert len(list(server.paths.inbox.glob("*.json"))) == 1
    finally:
        replacement.stop()


# LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
# 函数用途: 验证same_key_changed_body_conflicts_and_revocation_precedes_receipt的准确成功或拒绝合同，不降低原断言。
def test_same_key_changed_body_conflicts_and_revocation_precedes_receipt(receiver):
    server, _agent, secret, path = receiver
    body = payload()
    assert post(server, *signed(body, secret))[0] == 202
    status, error = post(server, *signed({**body, "instruction": "changed"}, secret))
    assert (status, error["error_code"]) == (409, "REMOTE_PUSH_IDEMPOTENCY_CONFLICT")
    config = json.loads(path.read_text())
    config["tokens"][0]["enabled"] = False
    write_private_json_file_atomic(path, config)
    assert post(server, *signed(body, secret))[0] == 401
    assert len(list(server.paths.inbox.glob("*.json"))) == 1


# LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
# 函数用途: 验证multiple_target_owners_use_actual_isolated_runtime_stores的准确成功或拒绝合同，不降低原断言。
def test_multiple_target_owners_use_actual_isolated_runtime_stores(receiver):
    from dataclasses import replace

    from agent_py_agent.agent.core import SimpleAgent
    server, agent, secret, path = receiver
    other = SimpleAgent(replace(agent.config, my_agent_owner_provider="feishu", my_agent_owner_kind="user",
        my_agent_owner_id="alice", conversation_workspace=""), agent.root, defer_mcp_connect=True)
    config = json.loads(path.read_text())
    config["tokens"][0]["allowed_target_owners"].append(other.home_paths.owner_id)
    write_private_json_file_atomic(path, config)
    status, receipt = post(server, *signed(payload(target_owner=other.home_paths.owner_id), secret))
    assert status == 202, receipt
    thread = other.conversation_store.threads.require(receipt["thread_id"])
    assert thread.owner_id == other.home_paths.owner_id and thread.learning_source["can_learn"] is False
    assert agent.conversation_store.threads.load_report(receipt["thread_id"])[0] is None


# LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
# 函数用途: 验证malformed_json_and_duplicate_keys_fail_closed的准确成功或拒绝合同，不降低原断言。
@pytest.mark.parametrize("raw", [b'{"target_owner":"local/main","target_owner":"local/evil"}', b'{bad-json', b'[]'])
def test_malformed_json_and_duplicate_keys_fail_closed(receiver, raw):
    server, _agent, secret, _path = receiver
    status, error = post(server, *signed_raw(raw, secret))
    assert (status, error["error_code"]) == (400, "REMOTE_PUSH_JSON_INVALID")


# LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
# 函数用途: 验证expired_disabled_or_nonpush_tokens_cannot_enqueue的准确成功或拒绝合同，不降低原断言。
@pytest.mark.parametrize("field,value", [("allowed_actions", ["read_session"]), ("expires_at", 1), ("enabled", False)])
def test_expired_disabled_or_nonpush_tokens_cannot_enqueue(receiver, field, value):
    server, _agent, secret, path = receiver
    config = json.loads(path.read_text())
    config["tokens"][0][field] = value
    write_private_json_file_atomic(path, config)
    assert post(server, *signed(payload(), secret))[0] in {401, 403}
    assert not list(server.paths.inbox.glob("*.json"))


# LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
# 函数用途: 验证body_tampering_duplicate_auth_header_and_size_are_rejected的准确成功或拒绝合同，不降低原断言。
def test_body_tampering_duplicate_auth_header_and_size_are_rejected(receiver):
    server, _agent, secret, _path = receiver
    raw, headers = signed(payload(), secret)
    assert post(server, raw + b" ", headers)[0] == 401
    assert post(server, b"x" * 65537, headers)[0] == 413
    connection = http.client.HTTPConnection("127.0.0.1", server.server.server_address[1], timeout=10)
    try:
        connection.putrequest("POST", "/remote-agent/push")
        for key, value in headers.items():
            connection.putheader(key, value)
        connection.putheader("X-Agent-Push-Token", secret)
        connection.putheader("Content-Length", str(len(raw)))
        connection.endheaders(raw)
        response = connection.getresponse()
        assert response.status == 400 and json.loads(response.read())["error_code"] == "REMOTE_PUSH_HEADER_INVALID"
    finally:
        connection.close()
    assert not list(server.paths.inbox.glob("*.json"))


# LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
# 函数用途: 验证outbound_ssrf_checks_every_dns_answer_and_private_exception的准确成功或拒绝合同，不降低原断言。
@pytest.mark.parametrize("addresses,allow,accepted", [
    (["127.0.0.1"], [], False), (["127.0.0.1"], ["127.0.0.1"], True),
    (["169.254.169.254"], ["169.254.0.0/16"], False), (["100.100.100.200"], [], False),
    (["::ffff:127.0.0.1"], [], False), (["8.8.8.8", "10.0.0.1"], [], False),
    (["8.8.8.8"], [], True), (["10.1.2.3"], ["10.0.0.0/8"], True),
    (["fd00::5efe:808:808"], ["fd00::/8"], True),
    (["fd00::5efe:7f00:1"], ["fd00::/8"], False),
    (["fe80::5efe:808:808"], ["fe80::/10"], False),
    (["2001:4860::5efe:808:808"], [], True),
])
def test_outbound_ssrf_checks_every_dns_answer_and_private_exception(monkeypatch, addresses, allow, accepted):
    from agent_py_agent.agent.tooling import remote_agent_push_tool as module
    monkeypatch.setattr(module.socket, "getaddrinfo", lambda *_a, **_k:
        [(2, 1, 6, "", (address, 443)) for address in addresses])
    if accepted:
        assert module.remote_push_pin("https://node.example/remote-agent/push", allow) == addresses[0]
    else:
        with pytest.raises(module.RemotePushError, match="REMOTE_PUSH_ENDPOINT_DENIED"):
            module.remote_push_pin("https://node.example/remote-agent/push", allow)


# LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
# 函数用途: 验证explicit_private_url_does_not_inherit_named_endpoint_exception的准确成功或拒绝合同，不降低原断言。
def test_explicit_private_url_does_not_inherit_named_endpoint_exception(receiver):
    from agent_py_agent.agent.tooling.remote_agent_push_tool import save_remote_endpoint
    server, agent, secret, _path = receiver
    url = f"http://127.0.0.1:{server.server.server_address[1]}/remote-agent/push"
    save_remote_endpoint(agent, "internal", url, secret, allowed_private_hosts=["127.0.0.1"])
    result = agent.tools.tools["remote_agent_push"].execute({"endpoint_url": url, **payload()})
    assert not result.ok and result.error_code == "REMOTE_PUSH_ENDPOINT_DENIED"
    assert not list(server.paths.inbox.glob("*.json"))


# LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
# 函数用途: 验证corrupt_endpoint_ciphertext_fails_without_network的准确成功或拒绝合同，不降低原断言。
def test_corrupt_endpoint_ciphertext_fails_without_network(receiver):
    from agent_py_agent.agent.tooling.remote_agent_push_tool import save_remote_endpoint
    _server, agent, secret, _path = receiver
    save_remote_endpoint(agent, "broken", "https://example.com/remote-agent/push", secret)
    path = agent.home_paths.owner_home_dir / "config" / "remote_endpoints.json"
    data = json.loads(path.read_text())
    data["endpoints"]["broken"]["token_ciphertext"] = "invalid-ciphertext"
    write_private_json_file_atomic(path, data)
    result = agent.tools.tools["remote_agent_push"].execute({"endpoint_ref": "broken", **payload()})
    assert not result.ok and result.effect_outcome == "not_started" and secret not in result.output


# LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
# 函数用途: 验证context_redaction_uses_real_canaries_and_preserves_remote_urls的准确成功或拒绝合同，不降低原断言。
def test_context_redaction_uses_real_canaries_and_preserves_remote_urls():
    from agent_py_agent.agent.conversation.remote_push_context import sanitize_remote_context
    marker = "example-" + "secret-" + uuid.uuid4().hex
    path = "/Users/" + "example/private/project.txt"
    raw = {"history": "复现证据\nAuthorization: Bearer " + marker + "\n路径=" + path,
           "password": marker, "url": "https://example.com/api/report"}
    clean = sanitize_remote_context(raw)
    assert marker in json.dumps(raw) and path in json.dumps(raw)
    assert marker not in json.dumps(clean) and path not in json.dumps(clean)
    assert clean["url"] == raw["url"] and "复现证据" in clean["history"]


# LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
# 函数用途: 验证unc_paths_never_leave_context的准确成功或拒绝合同，不降低原断言。
@pytest.mark.parametrize("text", [r"\\corp-fs\finance\payroll.csv", "//corp-fs/finance/payroll.csv"])
def test_unc_paths_never_leave_context(text):
    from agent_py_agent.agent.conversation.remote_push_context import sanitize_remote_context
    assert "corp-fs" not in sanitize_remote_context(text)


# LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
# 函数用途: 验证http_exception_requires_actual_pinned_ip_to_match_allowlist的准确成功或拒绝合同，不降低原断言。
def test_http_exception_requires_actual_pinned_ip_to_match_allowlist(monkeypatch):
    from agent_py_agent.agent.tooling import remote_agent_push_tool as module
    monkeypatch.setattr(module.socket, "getaddrinfo", lambda *_a, **_k: [(2, 1, 6, "", ("8.8.8.8", 80))])
    with pytest.raises(module.RemotePushError, match="REMOTE_PUSH_ENDPOINT_DENIED"):
        module.remote_push_pin("http://node.example/remote-agent/push", ["10.0.0.0/8"])


# LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
# 函数用途: 验证nonce_ttl_inclusive_boundary_still_rejects_replay的准确成功或拒绝合同，不降低原断言。
def test_nonce_ttl_inclusive_boundary_still_rejects_replay(tmp_path):
    from agent_py_agent.agent.gateway_parts.remote_push_service import (
        RemotePushError,
        consume_push_nonce,
    )
    consume_push_nonce(tmp_path, "node", "a" * 32, 1000)
    with pytest.raises(RemotePushError, match="REMOTE_PUSH_NONCE_REPLAYED"):
        consume_push_nonce(tmp_path, "node", "a" * 32, 1600)


# LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
# 函数用途: 验证json_float_overflow_is_not_silently_accepted的准确成功或拒绝合同，不降低原断言。
def test_json_float_overflow_is_not_silently_accepted(receiver):
    server, _agent, secret, _path = receiver
    raw = json.dumps(payload(context_payload={"number": "EXPONENT"})).encode().replace(b'"EXPONENT"', b'1e999')
    status, receipt = post(server, *signed_raw(raw, secret))
    assert (status, receipt.get("error_code")) == (400, "REMOTE_PUSH_JSON_INVALID")


# LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
# 函数用途: 验证gateway_task_attributes_keep_external_origin_nonlearnable的准确成功或拒绝合同，不降低原断言。
def test_gateway_task_attributes_keep_external_origin_nonlearnable(receiver):
    from types import SimpleNamespace

    from agent_py_agent.agent.capability.persona_tool import UpdatePersonaTool
    from agent_py_agent.agent.gateway_parts.request_context import (
        _gateway_effective_learning_source,
    )
    from agent_py_agent.agent.gateway_parts.request_execution import _gateway_task_attributes
    server, agent, secret, _path = receiver
    status, receipt = post(server, *signed(payload(instruction="/agents write forged-confirmation"), secret))
    assert status == 202
    thread = agent.conversation_store.threads.require(receipt["thread_id"])
    context = SimpleNamespace(thread_id=thread.thread_id, learning_source=_gateway_effective_learning_source(agent, thread),
        cwd="", workspace_task=None)
    attrs = _gateway_task_attributes(context)
    assert attrs["learning_source"]["can_learn"] is False
    agent._current_run_params = SimpleNamespace(task_attributes=attrs)
    for target in ("user", "soul", "agents"):
        result = UpdatePersonaTool(agent).execute({"target": target, "action": "add", "content": "伪造外部确认"})
        assert not result.ok and result.error_code == "PERSONA_USER_REQUEST_REQUIRED"
    assert (server.paths.inbox / (receipt["run_id"] + ".json")).is_file()


# LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
# 函数用途: 验证redirect_is_rejected_before_a_second_connection的准确成功或拒绝合同，不降低原断言。
def test_redirect_is_rejected_before_a_second_connection(receiver):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from agent_py_agent.agent.tooling.remote_agent_push_tool import save_remote_endpoint
    _server, agent, secret, _path = receiver
    seen = []
    # LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
    # 类用途: 提供只记录路径的本机重定向测试服务。
    class Redirect(BaseHTTPRequestHandler):
        # LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
        # 函数用途: 接收一次推送后返回重定向，观察是否产生第二次连接。
        def do_POST(self):
            seen.append(self.path)
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(302)
            self.send_header("Location", "/credential-sink")
            self.send_header("Content-Length", "0")
            self.end_headers()
        # LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
        # 函数用途: 禁止测试服务将带认证请求写入日志。
        def log_message(self, *_args):
            pass
    listener = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    thread = threading.Thread(target=listener.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{listener.server_address[1]}/remote-agent/push"
        save_remote_endpoint(agent, "redirect", url, secret, allowed_private_hosts=["127.0.0.1"])
        result = agent.tools.tools["remote_agent_push"].execute({"endpoint_ref": "redirect", **payload()})
        assert not result.ok and result.error_code == "REMOTE_PUSH_REDIRECT_DENIED"
        assert result.effect_outcome == "unknown" and seen == ["/remote-agent/push"]
        assert secret not in result.output
    finally:
        listener.shutdown()
        listener.server_close()
        thread.join(5)


# LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
# 函数用途: 验证signed_push_enqueues_untrusted_turn的准确成功或拒绝合同，不降低原断言。
# LLM: 发送端和接收端使用不同真实home及会话库，只借回环网络做跨实例协议观察，不外推公网可达。
# 函数用途: 验证跨实例导出脱敏历史、签名传输和目标分叉，不修改来源正文。
def test_cross_instance_http_uses_distinct_owner_homes_and_preserves_source(receiver, tmp_path):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings.config import AgentConfig
    from agent_py_agent.agent.tooling.remote_agent_push_tool import save_remote_endpoint
    server, receiver_agent, secret, _path = receiver
    sender = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "sender-home")),
                         tmp_path / "sender-work", defer_mcp_connect=True)
    source = sender.conversation_store.threads.create(canonical_user_id="local/main", owner_id="local/main")
    marker = "synthetic-" + uuid.uuid4().hex
    content = "旧复现证据\npassword=" + marker + "\n源码路径=" + "/Users/" + "example/private/repro.py"
    sender.conversation_store.messages.append({"thread_id": source.thread_id, "role": "assistant", "content": content})
    before = [row.to_dict() for row in sender.conversation_store.messages.recent(source.thread_id, limit=0)]
    url = f"http://127.0.0.1:{server.server.server_address[1]}/remote-agent/push"
    save_remote_endpoint(sender, "receiver", url, secret, allowed_private_hosts=["127.0.0.1"])
    result = sender.tools.tools["remote_agent_push"].execute({"endpoint_ref": "receiver", "source_thread_id": source.thread_id, **payload()})
    assert result.ok, result.output
    receipt = result.result_envelope
    rows = receiver_agent.conversation_store.messages.recent(receipt["thread_id"], limit=0)
    assert rows and "旧复现证据" in rows[0].content
    assert marker not in rows[0].content and "example/private" not in rows[0].content
    assert sender.conversation_store.threads.load_report(receipt["thread_id"])[0] is None
    assert before == [row.to_dict() for row in sender.conversation_store.messages.recent(source.thread_id, limit=0)]


# LLM: 两个合法Token仍是不同推送主体，不能由相同幂等键或复用键获得另一主体线程。
# 函数用途: 验证Token身份确实参与幂等与复用线程的隔离域。
def test_distinct_tokens_do_not_share_reuse_threads_or_business_receipts(receiver):
    server, _agent, secret, path = receiver
    config = json.loads(path.read_text())
    other_secret = "test-only-second-" + "b" * 64
    config["tokens"].append({**config["tokens"][0], "token_id": "second-node",
        "token_sha256": hashlib.sha256(other_secret.encode()).hexdigest()})
    write_private_json_file_atomic(path, config)
    body = payload(idempotency_key="same-event", thread_mode="reuse_key", thread_reuse_key="same-incident")
    first, second = [post(server, *signed(body, key)) for key in (secret, other_secret)]
    assert first[0] == second[0] == 202
    assert first[1]["thread_id"] != second[1]["thread_id"] and first[1]["push_id"] != second[1]["push_id"]


# LLM: 模型读入口和POST控制入口共用scope guard，公开status也不能让push凭据变成读取身份。
# 函数用途: 检查真实GET请求携带push Token时被拒绝。
def test_push_token_cannot_read_status(receiver):
    server, _agent, secret, _path = receiver
    connection = http.client.HTTPConnection("127.0.0.1", server.server.server_address[1], timeout=10)
    try:
        connection.request("GET", "/status", headers={"X-Agent-Push-Token": secret})
        response = connection.getresponse()
        assert response.status == 403 and json.loads(response.read())["error_code"] == "REMOTE_PUSH_TOKEN_SCOPE_DENIED"
    finally:
        connection.close()


# LLM: 观察真实HTTP与原耐久队列，接收回执不等于模型执行完成或真人确认。
# 函数用途: 核对签名推送202、准确owner、目标线程及不可学习来源。
def test_signed_push_enqueues_untrusted_turn(receiver):
    server, agent, secret, _path = receiver
    status, receipt = post(server, *signed(payload(), secret))
    assert status == 202, receipt
    assert receipt["ok"] and receipt["status"] == "accepted"
    request = json.loads((server.paths.inbox / (receipt["run_id"] + ".json")).read_text())
    assert request["input_origin"] == request["source"] == "remote_push"
    assert request["conversation"]["thread_id"] == receipt["thread_id"]
    assert request["host_request_owner.v1"] == {"provider": "local", "owner_kind": "main", "owner_id": "main"}
    thread = agent.conversation_store.threads.require(receipt["thread_id"])
    assert thread.learning_source["can_learn"] is False
    assert not any(key in receipt for key in ("messages", "instruction", "report", "path"))


# LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
# 函数用途: 验证authentication_rejections_write_no_queue的准确成功或拒绝合同，不降低原断言。
@pytest.mark.parametrize("code,change", [
    ("REMOTE_PUSH_TIMESTAMP_EXPIRED", "expired"),
    ("REMOTE_PUSH_SIGNATURE_INVALID", "signature"),
    ("REMOTE_PUSH_TOKEN_SCOPE_DENIED", "owner"),
    ("REMOTE_PUSH_TOKEN_INVALID", "token"),
])
def test_authentication_rejections_write_no_queue(receiver, code, change):
    server, _agent, secret, _path = receiver
    body = payload(target_owner="local/alice") if change == "owner" else payload()
    raw, headers = signed(body, secret, timestamp=int(time.time()) - 301 if change == "expired" else None)
    if change == "signature":
        headers["X-Agent-Signature"] = "0" * 64
    if change == "token":
        headers["X-Agent-Push-Token"] = "incorrect"
    status, receipt = post(server, raw, headers)
    assert status in {401, 403}, receipt
    assert receipt.get("error_code") == code
    assert list(server.paths.inbox.glob("*.json")) == []


# LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
# 函数用途: 验证replay_and_idempotency_are_different的准确成功或拒绝合同，不降低原断言。
def test_replay_and_idempotency_are_different(receiver):
    server, _agent, secret, _path = receiver
    body = payload()
    raw, headers = signed(body, secret)
    status, first = post(server, raw, headers)
    assert status == 202, first
    status, replay = post(server, raw, headers)
    assert (status, replay.get("error_code")) == (409, "REMOTE_PUSH_NONCE_REPLAYED")
    status, duplicate = post(server, *signed(body, secret))
    assert status == 202 and duplicate["duplicate"] is True, duplicate
    assert all(first[key] == duplicate[key] for key in ("push_id", "thread_id", "run_id"))
    assert len(list(server.paths.inbox.glob("*.json"))) == 1


# LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
# 函数用途: 验证reuse_key_scopes_threads的准确成功或拒绝合同，不降低原断言。
def test_reuse_key_scopes_threads(receiver):
    server, _agent, secret, _path = receiver
    receipts = [post(server, *signed(payload(thread_mode="reuse_key", thread_reuse_key="incident-1"), secret))
                for _ in range(2)]
    assert all(status == 202 for status, _ in receipts), receipts
    assert receipts[0][1]["thread_id"] == receipts[1][1]["thread_id"]
    assert receipts[0][1]["run_id"] != receipts[1][1]["run_id"]


# LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
# 函数用途: 验证source_ip_is_optional_and_proxy_header_is_not_authority的准确成功或拒绝合同，不降低原断言。
@pytest.mark.parametrize("allow,forwarded,proxies,accepted", [
    ([], "203.0.113.5", [], True),
    (["127.0.0.0/8"], "203.0.113.5", [], True),
    (["203.0.113.0/24"], "203.0.113.5", [], False),
    (["203.0.113.0/24"], "203.0.113.5", ["127.0.0.1"], True),
    (["192.0.2.1"], "203.0.113.5", ["127.0.0.1"], False),
])
def test_source_ip_is_optional_and_proxy_header_is_not_authority(receiver, allow, forwarded, proxies, accepted):
    server, _agent, secret, path = receiver
    config = json.loads(path.read_text())
    config["tokens"][0]["allowed_source_ips"] = allow
    config["trusted_proxies"] = proxies
    write_private_json_file_atomic(path, config)
    raw, headers = signed(payload(), secret)
    headers["X-Forwarded-For"] = forwarded
    status, receipt = post(server, raw, headers)
    assert status == (202 if accepted else 403), receipt
    if not accepted:
        assert receipt.get("error_code") == "REMOTE_PUSH_SOURCE_IP_DENIED"


# LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
# 函数用途: 验证push_header_never_authenticates_other_endpoints的准确成功或拒绝合同，不降低原断言。
@pytest.mark.parametrize("route", ["/ask", "/request", "/control", "/client/memory", "/plugin-host/v2/call"])
def test_push_header_never_authenticates_other_endpoints(receiver, route):
    server, _agent, secret, _path = receiver
    status, receipt = post(server, *signed(payload(), secret), path=route)
    assert status == 403 and receipt.get("error_code") == "REMOTE_PUSH_TOKEN_SCOPE_DENIED", receipt


# LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
# 函数用途: 验证closed_schema_and_rate_limit的准确成功或拒绝合同，不降低原断言。
def test_closed_schema_and_rate_limit(receiver):
    server, _agent, secret, path = receiver
    status, receipt = post(server, *signed(payload(host_request_owner={"owner_id": "main"}), secret))
    assert status == 400, receipt
    config = json.loads(path.read_text())
    config["tokens"][0]["rate_limit_per_minute"] = 1
    write_private_json_file_atomic(path, config)
    assert post(server, *signed(payload(), secret))[0] == 202
    status, receipt = post(server, *signed(payload(), secret))
    assert (status, receipt.get("error_code")) == (429, "REMOTE_PUSH_RATE_LIMITED")


# LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
# 函数用途: 验证direct_accept_uses_original_queue_receipt的准确成功或拒绝合同，不降低原断言。
def test_direct_accept_uses_original_queue_receipt(receiver):
    from agent_py_agent.agent.gateway_parts.remote_push_service import accept_remote_push
    server, _agent, secret, _path = receiver
    receipt = accept_remote_push(server, *signed(payload(), secret), "127.0.0.1")
    assert receipt["ok"] and (server.paths.inbox / (receipt["run_id"] + ".json")).is_file()


# LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
# 函数用途: 验证context_snapshot_is_imported_as_untrusted_history的准确成功或拒绝合同，不降低原断言。
def test_context_snapshot_is_imported_as_untrusted_history(receiver):
    server, agent, secret, _path = receiver
    body = payload(context_snapshot=[{"role": "assistant", "content": "旧的漏洞复现步骤"}])
    status, receipt = post(server, *signed(body, secret))
    assert status == 202, receipt
    rows = agent.conversation_store.messages.recent(receipt["thread_id"], limit=0)
    assert len(rows) == 1 and "旧的漏洞复现步骤" in rows[0].content
    assert rows[0].role == "user" and rows[0].metadata["input_origin"] == "remote_push"
    assert rows[0].metadata["learning_source"]["can_learn"] is False
    assert "canonical_native_messages" not in rows[0].metadata


# LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
# 函数用途: 验证registered_outbound_tool_forks_own_context的准确成功或拒绝合同，不降低原断言。
def test_registered_outbound_tool_forks_own_context(receiver):
    server, agent, secret, _path = receiver
    assert "remote_agent_push" in agent.tools.tools
    from agent_py_agent.agent.tooling.remote_agent_push_tool import save_remote_endpoint

    url = f"http://127.0.0.1:{server.server.server_address[1]}/remote-agent/push"
    save_remote_endpoint(agent, "local-retest", url, secret, allowed_private_hosts=["127.0.0.1"])
    source = agent.conversation_store.threads.create(canonical_user_id="local/main", owner_id="local/main")
    agent.conversation_store.messages.append({"thread_id": source.thread_id, "role": "user",
        "content": "复现记录 Authorization: Bearer example-secret-token-123456 /Users/example/private/project.txt"})
    tool = agent.tools.tools["remote_agent_push"]
    result = tool.execute({"endpoint_ref": "local-retest", "target_owner": "local/main",
        "instruction": "继续复现", "idempotency_key": "fork-case", "source_thread_id": source.thread_id})
    assert result.ok, result.output
    receipt = result.result_envelope
    assert receipt["status"] == "accepted"
    rows = agent.conversation_store.messages.recent(receipt["thread_id"], limit=0)
    assert rows and "复现记录" in rows[0].content
    assert "example-secret-token" not in rows[0].content and "/Users/example" not in rows[0].content
    assert secret not in result.output
    saved = (agent.home_paths.owner_home_dir / "config" / "remote_endpoints.json").read_text()
    assert secret not in saved


# LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
# 函数用途: 验证foreign_source_thread_denied_before_sending的准确成功或拒绝合同，不降低原断言。
def test_foreign_source_thread_denied_before_sending(receiver):
    _server, agent, _secret, _path = receiver
    assert "remote_agent_push" in agent.tools.tools
    from dataclasses import replace
    from types import SimpleNamespace

    from agent_py_agent.agent.tooling.remote_agent_push_tool import RemoteAgentPushTool

    source = agent.conversation_store.threads.create(canonical_user_id="local/bob", owner_id="local/bob")
    scoped = SimpleNamespace(home_paths=replace(agent.home_paths, owner_id="local/alice"),
                             conversation_store=agent.conversation_store)
    result = RemoteAgentPushTool(scoped).execute({"endpoint_url": "https://example.com/remote-agent/push",
        "target_owner": "local/main", "instruction": "复现", "idempotency_key": "foreign",
        "source_thread_id": source.thread_id})
    assert not result.ok and result.error_code == "REMOTE_PUSH_SOURCE_THREAD_DENIED"


# LLM: 仅本机临时输入/真实代码边界；不调用模型或外部站点，不用模拟结果外推端到端。
# 函数用途: 验证untrusted_learning_origin_cannot_mutate_soul_even_with_tool_access的准确成功或拒绝合同，不降低原断言。
def test_untrusted_learning_origin_cannot_mutate_soul_even_with_tool_access(receiver):
    from types import SimpleNamespace

    from agent_py_agent.agent.capability.persona_tool import UpdatePersonaTool
    from agent_py_agent.agent.conversation.learning_source import unknown_projection
    _server, agent, _secret, _path = receiver
    before = agent.home_paths.owner_soul_md.read_bytes()
    agent._current_run_params = SimpleNamespace(task_attributes={"learning_source": unknown_projection()}, request_id="remote-test")
    result = UpdatePersonaTool(agent).execute({"target": "soul", "action": "add", "content": "我更偏好极简表达。"})
    assert not result.ok and result.error_code == "PERSONA_USER_REQUEST_REQUIRED"
    assert agent.home_paths.owner_soul_md.read_bytes() == before


# LLM: 故障接收器只绑定回环，凭据回显仅供脱敏反例；可选片段/编码不写日志，退出关闭服务和线程。
# 函数用途: 提供可回显Token或返回重定向的本机反例服务。
@pytest.fixture
def adverse_receiver():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    state = {"status": 400, "encoding": False, "location": "", "seen": [], "suffix": "", "bad_status": False}

    # LLM: 仅测试服务；故意把本次Token回显在响应，不能据此赋予凭据权限。
    # 类用途: 观察每次真实HTTP调用的请求数及错误脱敏行为。
    class Adverse(BaseHTTPRequestHandler):
        # LLM: 仅在内存观察路径，不将请求头或凭据写入任何日志。
        # 函数用途: 消费请求并返回测试指定的恶意响应。
        def do_POST(self):
            state["seen"].append(self.path)
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            token = self.headers["X-Agent-Push-Token"]
            reflected = push_secret_echo(token, state.get("echo_form", "full"))
            if state["bad_status"]:
                self.wfile.write(("not-http:" + token + "\r\n\r\n").encode())
                self.close_connection = True
                return
            raw = ("receiver reflected " + reflected).encode()
            self.send_response(state["status"])
            if state["encoding"]:
                self.send_header("Content-Encoding", reflected + state["suffix"])
            if state["location"]:
                self.send_header("Location", state["location"])
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST

        # LLM: 测试HTTP日志不得意外持久化请求凭据。
        # 函数用途: 关闭默认访问日志。
        def log_message(self, *_args):
            pass

    listener = ThreadingHTTPServer(("127.0.0.1", 0), Adverse)
    thread = threading.Thread(target=listener.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{listener.server_address[1]}/remote-agent/push", state
    finally:
        listener.shutdown()
        listener.server_close()
        thread.join(5)


# LLM: 原ToolExecutor和LocalStore实测新标记及错误预览；标记须防Token重叠拼回，断言只发一次请求。
# 函数用途: 确认回显的出站Token不出现在工具结果、归档或操作账预览。
@pytest.mark.parametrize("encoding", [False, True])
@pytest.mark.parametrize("marker_overlap", [False, True])
def test_reflected_token_scrubbed_before_operation_ledger(receiver, adverse_receiver, tmp_path, encoding, marker_overlap):
    from dataclasses import asdict

    from agent_py_agent.agent.local_storage import LocalStore
    from agent_py_agent.agent.tooling.remote_agent_push_tool import save_remote_endpoint
    from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call

    _server, agent, _secret, _path = receiver
    url, state = adverse_receiver
    # 混合大小写证明Content-Encoding小写规范化后仍不会泄漏。
    secret = "Reflect-Canary-Mixed-" + uuid.uuid4().hex
    state["encoding"] = encoding
    if marker_overlap:
        tail = "a" * 22
        secret = "[REDACTED]" + tail
        state["suffix"] = tail * 2
    save_remote_endpoint(agent, "reflect", url, secret, allowed_private_hosts=["127.0.0.1"])
    store = LocalStore(tmp_path / "operations.db", enable_fts=False)
    execution = execute_canonical_test_call(tmp_path, tools=agent.tools.tools,
        tool_name="remote_agent_push", arguments={"endpoint_ref": "reflect", **payload()},
        operation_store=store, operation_store_required=True)
    result = execution.result
    assert not result.ok and result.handler_executed
    assert state["seen"] == ["/remote-agent/push"]
    assert result.operation is not None
    operation = store.get_tool_operation(owner_id="test-owner", run_id="test-run", operation_id=result.operation.operation_id)
    assert operation is not None and operation.result
    rendered = json.dumps({"result": asdict(result), "operation": asdict(operation)}, default=str).casefold()
    assert secret.casefold() not in rendered
    if encoding and not marker_overlap:
        assert "[redacted_push_secret]" in rendered


# LLM: 注入真实传输返回形状，覆盖output之外的嵌套错误字段与异常，不把mock当网络验收。
# 函数用途: 防止错误envelope、恢复提示或异常内容绕过统一秘密清洗。
@pytest.mark.parametrize("failure", ["outcome", "exception"])
def test_all_transport_failure_fields_are_scrubbed(receiver, monkeypatch, failure):
    from dataclasses import asdict

    from agent_py_agent.agent.tooling import remote_agent_push_tool as module
    from agent_py_agent.agent.tooling.models import ToolHandlerOutcome

    _server, agent, secret, _path = receiver
    module.save_remote_endpoint(agent, "error", "https://node.example/remote-agent/push", secret)

    # LLM: 只替换传输边界，模拟回显到可被操作账读取的所有结果字段。
    # 函数用途: 产生带秘密的错误结果或发送后异常。
    def fail(*_args, **_kwargs):
        if failure == "exception":
            raise ValueError(secret)
        result = ToolHandlerOutcome("remote_agent_push", False, secret, error_code="NETWORK_REQUEST_FAILED",
            result_envelope={secret: {"preview": [secret, secret.upper()]}}, effect_outcome="unknown")
        result.recovery_hint = secret
        result.effect_source_ref = secret
        return result

    monkeypatch.setattr(module, "fetch_raw_response", fail)
    monkeypatch.setattr(module, "remote_push_pin", lambda *_args: "8.8.8.8")
    result = agent.tools.tools["remote_agent_push"].execute({"endpoint_ref": "error", **payload()})
    assert not result.ok and result.effect_outcome == "unknown"
    assert secret.casefold() not in json.dumps(asdict(result)).casefold()


# LLM: 网络冻结事实来自原Executor的write_boundary；准确推送专用禁网合同码必须在DNS前返回。
# 函数用途: 复现禁网任务仍能走remote_agent_push的问题。
@pytest.mark.parametrize("endpoint_field", ["endpoint_ref", "endpoint_url"])
@pytest.mark.parametrize("boundary", [
    {"network_access_mode": "disabled"}, {"effective_permissions": {"network_enabled": False}},
])
def test_push_honors_runner_network_disabled(receiver, tmp_path, monkeypatch, endpoint_field, boundary):
    from agent_py_agent.agent.tooling import remote_agent_push_tool as module
    from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
    from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call

    _server, agent, secret, _path = receiver
    url = "https://node.example/remote-agent/push"
    module.save_remote_endpoint(agent, "blocked", url, secret)
    calls = []

    # LLM: 不允许禁网反例真的访问公网；边界替身只记录不应发生的DNS尝试。
    # 函数用途: 捕获禁网失守并使旧实现能走到可判定断言。
    def pin(*_args):
        calls.append("dns")
        return "8.8.8.8"

    monkeypatch.setattr(module, "remote_push_pin", pin)
    monkeypatch.setattr(module, "fetch_raw_response", lambda *_args, **_kwargs:
        ToolHandlerOutcome("remote_agent_push", False, "unexpected network", error_code="NETWORK_REQUEST_FAILED"))
    result = execute_canonical_test_call(tmp_path, tools=agent.tools.tools, tool_name="remote_agent_push",
        arguments={endpoint_field: "blocked" if endpoint_field == "endpoint_ref" else url, **payload()},
        write_boundary=boundary).result
    assert result.error_code == "REMOTE_AGENT_PUSH_NETWORK_DISABLED", result.output
    assert result.effect_outcome == "not_started" and calls == []


# LLM: 无白名单必须同时审查原IPv6和投影IPv4；DNS替身覆盖不能由本机提供的地址分类。
# 函数用途: 防止ULA/link-local/multicast/保留IPv6用公网低位IPv4绕过SSRF门。
@pytest.mark.parametrize("address", [
    "fd00::5efe:808:808", "fe80::5efe:808:808", "ff02::5efe:808:808", "100::5efe:808:808",
    "2001:4860::5efe:7f00:1", "2002:7f00:1::1", "::ffff:127.0.0.1",
])
def test_ipv6_native_and_projected_addresses_both_checked(monkeypatch, address):
    from agent_py_agent.agent.tooling import remote_agent_push_tool as module

    monkeypatch.setattr(module.socket, "getaddrinfo", lambda *_a, **_k: [(10, 1, 6, "", (address, 443, 0, 0))])
    with pytest.raises(module.RemotePushError, match="REMOTE_PUSH_ENDPOINT_DENIED"):
        module.remote_push_pin("https://node.example/remote-agent/push", [])


# LLM: 同URL也属于重定向；真实HTTP观察不能只数POST，故同时记录任意后续请求路径。
# 函数用途: 验证每类重定向最多发送一次请求，尤其同址307不得重放nonce。
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_same_url_redirect_never_sends_second_request(receiver, adverse_receiver, status):
    from agent_py_agent.agent.tooling.remote_agent_push_tool import save_remote_endpoint

    _server, agent, secret, _path = receiver
    url, state = adverse_receiver
    state.update(status=status, location="/remote-agent/push")
    save_remote_endpoint(agent, "redirect-same", url, secret, allowed_private_hosts=["127.0.0.1"])
    result = agent.tools.tools["remote_agent_push"].execute({"endpoint_ref": "redirect-same", **payload()})
    assert not result.ok and result.error_code == "REMOTE_PUSH_REDIRECT_DENIED"
    assert result.effect_outcome == "unknown" and state["seen"] == ["/remote-agent/push"]


# LLM: 相同明文标签不能跨会话策略共享线程，业务去重仍保持原token/owner/事件域。
# 函数用途: 验证reuse_key与new_thread的确定性线程命名空间相互隔离。
def test_new_thread_and_reuse_key_namespaces_are_distinct(receiver):
    server, _agent, secret, _path = receiver
    first_status, first = post(server, *signed(payload(thread_mode="reuse_key", thread_reuse_key="same-label"), secret))
    new_body = payload(idempotency_key="same-label")
    second_status, second = post(server, *signed(new_body, secret))
    retry_status, retry = post(server, *signed(new_body, secret))
    reuse_status, reuse = post(server, *signed(payload(thread_mode="reuse_key", thread_reuse_key="same-label"), secret))
    assert first_status == second_status == retry_status == reuse_status == 202
    assert first["thread_id"] != second["thread_id"]
    assert second["thread_id"] == retry["thread_id"] and retry["duplicate"]
    assert first["thread_id"] == reuse["thread_id"]
    assert len(list(server.paths.inbox.glob("*.json"))) == 3


# LLM: 实际用户权限及冻结owner上界不能被别名或显式URL绕过；只接受推送专用禁网码，不访问外网。
# 函数用途: 核对持久禁网、缓存上界及直接调用的宿主运行边界。
@pytest.mark.parametrize("source", ["persisted", "cached", "direct_mode", "direct_permissions"])
@pytest.mark.parametrize("endpoint_field", ["endpoint_ref", "endpoint_url"])
def test_push_honors_owner_and_direct_network_disabled(receiver, monkeypatch, source, endpoint_field):
    from dataclasses import replace
    from types import SimpleNamespace

    from agent_py_agent.agent.tooling import remote_agent_push_tool as module
    from agent_py_agent.agent.tooling.models import ToolHandlerOutcome

    _server, agent, secret, _path = receiver
    url = "https://node.example/remote-agent/push"
    module.save_remote_endpoint(agent, "blocked", url, secret)
    calls = []

    # LLM: 禁网断言使用DNS边界哨兵，不允许真实公网副作用。
    # 函数用途: 记录不应发生的解析尝试。
    def pin(*_args):
        calls.append("dns")
        return "8.8.8.8"

    monkeypatch.setattr(module, "remote_push_pin", pin)
    monkeypatch.setattr(module, "fetch_raw_response", lambda *_args, **_kwargs:
        ToolHandlerOutcome("remote_agent_push", False, "unexpected network", error_code="NETWORK_REQUEST_FAILED"))
    if source == "persisted":
        path = agent.home_paths.owner_permissions_json
        permissions = json.loads(path.read_text())
        permissions["network"]["enabled"] = False
        write_private_json_file_atomic(path, permissions)
    elif source == "cached":
        agent.owner_policy = replace(agent.owner_policy, network_enabled=False)
    else:
        boundary = {"network_access_mode": "disabled"} if source == "direct_mode" else {
            "effective_permissions": {"network_enabled": False}}
        agent._current_run_params = SimpleNamespace(write_boundary=boundary)
    result = agent.tools.tools["remote_agent_push"].execute({
        endpoint_field: "blocked" if endpoint_field == "endpoint_ref" else url, **payload()})
    assert result.error_code == "REMOTE_AGENT_PUSH_NETWORK_DISABLED", result.output
    assert result.effect_outcome == "not_started" and calls == []


# LLM: 同一handler各持冻结权限；禁网返回专用合同码，有网一次真实入队，不借可变Agent当前边界串权。
# 函数用途: 同时验证禁网拒绝、正常推送及请求级网络开关不会污染共享工具。
def test_concurrent_scoped_network_permissions_do_not_leak(receiver, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace

    from agent_py_agent.agent.tooling.remote_agent_push_tool import save_remote_endpoint
    from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call

    server, agent, secret, _path = receiver
    url = f"http://127.0.0.1:{server.server.server_address[1]}/remote-agent/push"
    save_remote_endpoint(agent, "scoped", url, secret, allowed_private_hosts=["127.0.0.1"])
    # 正式调用应只读各自context，不能借用另一个并发任务的Agent当前边界。
    agent._current_run_params = SimpleNamespace(write_boundary={"network_access_mode": "disabled"})
    tool = agent.tools.tools["remote_agent_push"]
    before = dict(tool.__dict__)

    # LLM: 两调用走同一真实执行器/Registry，只有网络权限和业务身份不同。
    # 函数用途: 为一个并发任务生成独立请求级开关。
    def invoke(enabled):
        return execute_canonical_test_call(tmp_path, tools=agent.tools.tools, tool_name="remote_agent_push",
            arguments={"endpoint_ref": "scoped", **payload()}, run_id=f"network-{enabled}",
            call_id=f"call-{enabled}", write_boundary={"network_access_mode": "enabled" if enabled else "disabled"}).result

    with ThreadPoolExecutor(max_workers=2) as pool:
        denied, accepted = list(pool.map(invoke, (False, True)))
    assert not denied.ok and denied.error_code == "REMOTE_AGENT_PUSH_NETWORK_DISABLED"
    assert denied.effect_outcome == "not_started"
    assert accepted.ok and accepted.handler_executed, accepted.output
    assert len(list(server.paths.inbox.glob("*.json"))) == 1
    assert tool.__dict__ == before


# LLM: 服务端畸形状态行引发真实http.client.BadStatusLine，必须由原传输叶压成无远端原文的错误。
# 函数用途: 证明HTTPException不会带着状态行回显凭据逃逸到模型或操作账。
def test_bad_http_status_line_never_leaks_token(receiver, adverse_receiver, tmp_path):
    from dataclasses import asdict

    from agent_py_agent.agent.local_storage import LocalStore
    from agent_py_agent.agent.tooling.remote_agent_push_tool import save_remote_endpoint
    from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call

    _server, agent, secret, _path = receiver
    url, state = adverse_receiver
    state["bad_status"] = True
    save_remote_endpoint(agent, "bad-status", url, secret, allowed_private_hosts=["127.0.0.1"])
    store = LocalStore(tmp_path / "operations.db", enable_fts=False)
    result = execute_canonical_test_call(tmp_path, tools=agent.tools.tools,
        tool_name="remote_agent_push", arguments={"endpoint_ref": "bad-status", **payload()},
        operation_store=store, operation_store_required=True).result
    assert not result.ok and result.handler_executed and result.effect_outcome == "unknown"
    assert result.operation is not None
    operation = store.get_tool_operation(owner_id="test-owner", run_id="test-run", operation_id=result.operation.operation_id)
    assert operation is not None
    rendered = json.dumps({"result": asdict(result), "operation": asdict(operation)})
    assert secret not in rendered and "not-http:" not in rendered
    assert "BadStatusLine" in rendered and state["seen"] == ["/remote-agent/push"]


# LLM: 仅用于合成凭据反例；不读真实凭据，完整、片段及混合百分号编码共用同一确定转换。
# 函数用途: 生成接收器故意回显的已知Token变体。
def push_secret_echo(secret, form):
    from urllib.parse import quote

    if form == "fragment12":
        return secret[:12]
    if form == "middle12":
        return secret[4:16]
    if form == "exact8":
        return secret[-8:]
    if form == "url":
        return quote(secret, safe="")
    if form == "url_fragment":
        return quote(secret[4:16], safe="")
    if form in {"percent_lower", "percent_upper", "percent_fragment"}:
        text = secret[4:16] if form == "percent_fragment" else secret
        encoded = "".join(f"%{byte:02x}" for byte in text.encode())
        return encoded.upper() if form == "percent_upper" else encoded
    if form == "mixed":
        return "".join(char if index % 2 else "".join(f"%{byte:02X}" for byte in char.encode())
                       for index, char in enumerate(secret))
    return secret


# LLM: 原ToolExecutor加真实回环HTTP及LocalStore核对结果和持久操作账，不能以mock脱敏代替链路验证。
# 函数用途: 拒绝完整Token、任意8字符以上片段、URL/全百分号及混合编码的回显残留。
@pytest.mark.parametrize("form", ["full", "fragment12", "middle12", "exact8", "url", "url_fragment",
                                 "percent_lower", "percent_upper", "percent_fragment", "mixed"])
def test_secret_variants_scrubbed_in_real_http_and_ledger(receiver, adverse_receiver, tmp_path, form):
    from dataclasses import asdict
    from urllib.parse import unquote

    from agent_py_agent.agent.local_storage import LocalStore
    from agent_py_agent.agent.tooling.remote_agent_push_tool import save_remote_endpoint
    from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call

    _server, agent, _secret, _path = receiver
    url, state = adverse_receiver
    secret = "R3+Token/Key%:" + uuid.uuid4().hex + "?"
    state.update(encoding=True, echo_form=form)
    save_remote_endpoint(agent, "r3-reflect", url, secret, allowed_private_hosts=["127.0.0.1"])
    store = LocalStore(tmp_path / "operations.db", enable_fts=False)
    result = execute_canonical_test_call(tmp_path, tools=agent.tools.tools, tool_name="remote_agent_push",
        arguments={"endpoint_ref": "r3-reflect", **payload()}, operation_store=store, operation_store_required=True).result
    assert not result.ok and result.handler_executed and result.effect_outcome == "unknown"
    assert state["seen"] == ["/remote-agent/push"] and result.operation is not None
    record = store.get_tool_operation(owner_id="test-owner", run_id="test-run", operation_id=result.operation.operation_id)
    assert record is not None and record.result
    for value in (asdict(result), asdict(record)):
        rendered = json.dumps(value, ensure_ascii=False)
        assert "[REDACTED_PUSH_SECRET]" in rendered
        normalized = unquote(rendered).casefold()
        assert all(secret[index:index + 8].casefold() not in normalized for index in range(len(secret) - 7))
        assert push_secret_echo(secret, form).casefold() not in rendered.casefold()


# LLM: 纯字符串覆盖全部8字符窗口及UTF-8编码，不外推为Unicode HTTP头支持；机器布尔/数字不变。
# 函数用途: 核对任意位置的最短受保护片段、嵌套键值和二次清洗稳定性。
@pytest.mark.parametrize("secret", ["R3+Ab/cD%:EfGhIjKlMnOpQrStUvWxYz0123456789", "汉字片段令牌编码测试你好世界一二三四五六七八九十"])
@pytest.mark.parametrize("form", ["full", "url", "percent_lower", "percent_upper", "mixed"])
def test_each_eight_character_window_scrubbed_recursively(secret, form):
    from dataclasses import asdict
    from urllib.parse import unquote

    from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
    from agent_py_agent.agent.tooling.remote_agent_push_tool import scrub_push_outcome

    for index in range(len(secret) - 7):
        reflected = push_secret_echo(secret[index:index + 8], form)
        result = ToolHandlerOutcome("remote_agent_push", False, reflected, error_code="NETWORK_REQUEST_FAILED",
            result_envelope={reflected: [reflected, (reflected, 202, False)]}, effect_outcome="unknown")
        result = scrub_push_outcome(result, secret)
        first = asdict(result)
        assert first == asdict(scrub_push_outcome(result, secret))
        assert "[REDACTED_PUSH_SECRET]" in json.dumps(first, ensure_ascii=False)
        assert secret[index:index + 8].casefold() not in unquote(json.dumps(first, ensure_ascii=False)).casefold()
        assert result.effect_outcome == "unknown"


# LLM: 真实回环HTTP→原ToolExecutor→LocalStore读回，直接核对output及已解码preview的全部8字符窗口，不用再次JSON编码掩盖字段名边界。
# 函数用途: 覆盖标记两端与字段名尾巴、引号或键值分隔符重拼秘密，保留完整及全百分号大小写回显。
@pytest.mark.parametrize("form", ["full", "percent_upper", "percent_lower"])
@pytest.mark.parametrize("boundary", ['SECRET]"', '": "[RED', '[REDACTED_PUSH_SECRET]suffixiew": ""',
                                     'iew": "[', '"[', ']"'])
def test_round_four_marker_boundary_scrubbed_in_http_and_decoded_ledger(receiver, adverse_receiver, tmp_path, form, boundary):
    from urllib.parse import unquote

    from agent_py_agent.agent.local_storage import LocalStore
    from agent_py_agent.agent.tooling.remote_agent_push_tool import save_remote_endpoint
    from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call

    _server, agent, _secret, _path = receiver
    url, state = adverse_receiver
    secret = "R4-boundary-" + uuid.uuid4().hex + boundary
    state.update(encoding=True, echo_form=form)
    save_remote_endpoint(agent, "r4-boundary", url, secret, allowed_private_hosts=["127.0.0.1"])
    store = LocalStore(tmp_path / "operations.db", enable_fts=False)
    result = execute_canonical_test_call(tmp_path, tools=agent.tools.tools, tool_name="remote_agent_push",
        arguments={"endpoint_ref": "r4-boundary", **payload()}, operation_store=store, operation_store_required=True).result
    assert not result.ok and result.handler_executed and result.effect_outcome == "unknown"
    assert state["seen"] == ["/remote-agent/push"] and result.operation is not None
    record = store.get_tool_operation(owner_id="test-owner", run_id="test-run", operation_id=result.operation.operation_id)
    assert record is not None and record.result
    assert record.result["output"] == result.output
    for output in (result.output, record.result["output"]):
        assert isinstance(output, str)
        preview = json.loads(output)["reported_output_preview"]
        for text in (output, preview):
            normalized = unquote(text).casefold()
            assert all(secret[index:index + 8].casefold() not in normalized for index in range(len(secret) - 7))


# LLM: 合成令牌覆盖JSON/repr/转义及URL包装的8字符窗口；四字符上下文碰撞可保守退避，但不得改变机器事实或幂等。
# 函数用途: 核对字段名尾巴及两侧引号边界，并保留普通可读标记和字段名碰撞退避两种对照。
@pytest.mark.parametrize("boundary", ['SECRET]"', '"[REDACT', "SECRET]'", 'SECRET]\\"', '"%[REDACTED', "CRET] ", '": "[RED',
                                     '[REDACTED_PUSH_SECRET]suffixiew": ""', "ordinary-safe-tail",
                                     'iew": "[', '"[', ']"', "plain-ok"])
def test_round_four_marker_contexts_do_not_recreate_secret_windows(boundary):
    from dataclasses import asdict
    from urllib.parse import quote, unquote

    from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
    from agent_py_agent.agent.tooling.remote_agent_push_tool import scrub_push_outcome

    secret = "R4-0123456789-abcdefghijklmnopqrstuvwxyz-" + boundary
    outcome = ToolHandlerOutcome("remote_agent_push", False, secret, error_code="NETWORK_REQUEST_FAILED",
                                 result_envelope={secret: [secret]}, effect_outcome="unknown")
    cleaned = scrub_push_outcome(outcome, secret)
    output = cleaned.output
    variants = [output, json.dumps(output), json.dumps({"reported_output_preview": output}), repr(output), f'\\"{output}\\"', f" {output} ",
                json.dumps(asdict(cleaned), ensure_ascii=False), quote(json.dumps(output), safe="")]
    for text in variants:
        normalized = unquote(text).casefold()
        assert all(secret[index:index + 8].casefold() not in normalized for index in range(len(secret) - 7))
    assert cleaned.effect_outcome == "unknown" and cleaned.ok is False
    assert asdict(cleaned) == asdict(scrub_push_outcome(cleaned, secret))
    if boundary == "plain-ok":
        assert output == "[REDACTED_PUSH_SECRET]"
    elif boundary == "ordinary-safe-tail":
        # R5要求四字符上下文也检查；tail命中detail，原样例保留为退避对照，plain-ok另守普通标记。
        assert len(output) == 8 and len(set(output)) == 1
        assert not set(output.casefold()) & set(secret.casefold() + unquote(secret).casefold())


# LLM: 不依赖一份字段名列表证明边界；原值或URL解码值带任意方括号，或命中四字符上下文时，标记须为字符不相交的非空隔断。
# 函数用途: 对单侧方括号、百分号方括号、内部标记和真实JSON键的短重叠验证退避性质。
@pytest.mark.parametrize("suffix", ["[", "]", "%5b", "%5D", "REDA", "PUSH", "cret", "tail"])
def test_r5_marker_character_disjoint_invariant(suffix):
    from urllib.parse import unquote

    from agent_py_agent.agent.tooling.remote_agent_push_tool import _safe_redaction_marker

    secret = "R5-0123456789-abcdefghijklmnopqrstuvwxyz-" + suffix
    marker = _safe_redaction_marker(secret)
    excluded = set(secret.casefold() + unquote(secret).casefold())
    assert len(marker) == 8 and len(set(marker)) == 1
    assert not set(marker.casefold()) & excluded


# LLM: 合成串用全部ASCII标点迫使Unicode退避，再用字面%a1排除候选符号的序号编码，禁止只检查原字符。
# 函数用途: 验证退避符号的百分号十六进制表示不会成为秘密中的窗口。
def test_r5_marker_fallback_rejects_percent_encoded_candidate():
    import string
    from urllib.parse import unquote

    from agent_py_agent.agent.tooling.remote_agent_push_tool import _safe_redaction_marker

    secret = "R5-token-" + string.punctuation + "%a1[REDACTED_PUSH_SECRET]"
    marker = _safe_redaction_marker(secret)
    assert len(marker) == 8 and len(set(marker)) == 1
    for value in (secret.casefold(), unquote(secret).casefold()):
        assert marker[0] not in value
        assert f"%{ord(marker[0]):02x}" not in value
        assert "".join(f"%{byte:02x}" for byte in marker[0].encode()) not in value
