"""原设置→可选决策服务→本地 HTTP→原账本/显示的组合验收，不调用真实供应商。"""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model.call_runtime import model_call_summary
from agent_py_agent.agent.conversation import decision_service
from agent_py_agent.agent.gateway_parts.io import locked_file_transition
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.tests.test_decision_model_profiles import decision as add_decision
from agent_py_agent.tests.test_decision_protocol import questions, response
from agent_py_agent.tests.test_decision_settings import host_at, patch


# LLM: 只在本机随机端口提供自有协议样本，全部线程和等待在 finally 回收；不访问真实服务商。
# 函数用途: 用真实 HTTP 字节验证整条调用链，允许测试控制服务错误及在途延迟。
@pytest.fixture
def server():
    state = SimpleNamespace(status=200, body=None, requests=[], entered=threading.Event(), release=threading.Event(),
                            block=False)

    # LLM: 测试服务器只接收有限自有请求并返回固定协议，无命令执行或持久业务副作用。
    # 类用途: 让正式传输栈实际进行连接、发送、读取与取消。
    class Handler(BaseHTTPRequestHandler):
        # LLM: 正文只留内存断言，认证头不打印；网络阻塞通过有界 Event 等待控制；错误体只在非 200 时使用。
        # 函数用途: 返回成功或指定 HTTP 错误（可带服务商错误体），以验证服务故障不会进入主模型失败路径。
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.requests.append((self.path, body))
            state.entered.set()
            if state.block:
                state.release.wait(3)
            encoded = json.dumps(state.body if state.body is not None and state.status != 200 else response()).encode()
            try:
                self.send_response(state.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass

        # LLM: 不将测试身份头或路径写入日志；只在断言失败时显示受控数据。
        # 函数用途: 关闭标准 HTTP 请求日志。
        def log_message(self, *_args):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{http.server_port}"
    worker = threading.Thread(target=lambda: http.serve_forever(poll_interval=0.01), daemon=True)
    worker.start()
    try:
        yield state
    finally:
        state.release.set()
        http.shutdown()
        http.server_close()
        worker.join(2)


# LLM: 只通过原配置入口创建临时 owner/thread/模型，开启决策仍不访问网络。
# 函数用途: 返回可用于真实本地 HTTP 的宿主、调用范围和共用阶段。
def configured(tmp_path, server, *, mode="apply", timeout=1.0):
    host = host_at(tmp_path)
    thread = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
    key, _ = add_decision(host, api_base=server.url)
    patch(host, {"enabled": True, "profile_id": key, "timeout_seconds": timeout,
                 "points.subagent_model.mode": mode}, thread_id=thread.thread_id)
    params = SimpleNamespace(request_id="req-decision", run_id="run-decision", task_id="task-decision",
        task_attributes={"conversation_thread_id": thread.thread_id}, live_archive_state={})
    params.tui_runtime = TuiRuntime("decision-http")
    params.effective_on_chunk = params.tui_runtime.begin_turn(params.request_id)
    stage = decision_service.begin_decision_stage(host, params, operation_id="batch-1")
    return host, params, thread, stage


# LLM: 结构化材料为协议层测试数据，不能代表真实模型对业务需求的判断质量。
# 函数用途: 向同一阶段提交一次多题决策，不执行返回的任何建议。
def decide(host, params, stage):
    return decision_service.decide(host, params, stage, point="subagent_model", state={"需求": "整理资料"},
        questions=questions(), candidates_revision="candidates-1", source_refs=("request:1",))


@pytest.mark.parametrize("mode", ["apply", "observe"])
def test_configured_http_decision_reaches_original_usage_and_display(tmp_path, server, mode):
    host, params, thread, stage = configured(tmp_path, server, mode=mode)
    result = decide(host, params, stage)
    assert result.status == "success" and result.mode == mode
    assert result.may_apply is (mode == "apply")
    assert result.response.answers[0].value == "model-a"
    assert len(server.requests) == 1
    path, body = server.requests[0]
    assert path == "/v1/systemone" and set(body) == {"model", "state", "questions"}
    assert host.config.model_name == "deployment-model" and host.backend.name == "deployment"
    summary = model_call_summary(host, request_id=params.request_id, run_id=params.run_id)
    assert summary["purpose_breakdown"]["decision"]["physical_model_attempt_count"] == 1
    assert summary["provider_http_attempt_count"] == 1
    host.conversation_store.model_usage.append_snapshot_once({"thread_id": thread.thread_id,
        "request_id": params.request_id, "run_id": params.run_id, "task_id": params.task_id,
        "source": "test", "model_calls": summary})
    metrics = params.tui_runtime.store.snapshot().status.model_metrics
    assert metrics["decision_input_tokens"] == 120 and metrics["decision_input_complete"]
    assert metrics["input_tokens"] == 120 and metrics["output_tokens"] == 30
    assert metrics["model_rounds"] == 0


def test_off_does_not_send_or_create_call_ledger(tmp_path, server):
    host, params, thread, stage = configured(tmp_path, server)
    patch(host, {"enabled": False}, thread_id=thread.thread_id)
    outcome = decide(host, params, stage)
    assert outcome.status == "off" and not outcome.may_apply
    assert not server.requests and not hasattr(host, "_model_call_ledger")
    assert host.config.model_name == "deployment-model"


@pytest.mark.parametrize("status", [401, 500])
def test_http_error_returns_original_plan_marker_and_cools_connection(tmp_path, server, status):
    server.status = status
    host, params, _, stage = configured(tmp_path, server)
    first = decide(host, params, stage)
    assert first.status in {"error", "configuration_required"} and not first.may_apply
    again = decide(host, params, stage)
    assert again.status in {"cooldown", "configuration_required"} and not again.may_apply
    assert len(server.requests) == 1
    assert host.config.model_name == "deployment-model"
    assert model_call_summary(host, request_id=params.request_id)["status_counts"]["failed"] == 1
    metrics = params.tui_runtime.store.snapshot().status.model_metrics
    assert metrics["decision_call_count"] == 1 and metrics["decision_input_tokens"] is None


def test_slow_http_is_bounded_and_late_result_cannot_be_applied(tmp_path, server):
    server.block = True
    host, params, _, stage = configured(tmp_path, server, timeout=0.12)
    start = time.monotonic()
    result = decide(host, params, stage)
    elapsed = time.monotonic() - start
    assert result.status == "deadline" and not result.may_apply
    assert elapsed < 0.8 and server.entered.is_set()
    server.release.set()
    assert model_call_summary(host, request_id=params.request_id)["status_counts"]["timed_out"] == 1


def test_disabling_while_http_waits_rejects_late_suggestion(tmp_path, server):
    server.block = True
    host, params, thread, stage = configured(tmp_path, server, timeout=2)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(decide, host, params, stage)
        assert server.entered.wait(1)
        patch(host, {"enabled": False}, thread_id=thread.thread_id)
        result = future.result(timeout=0.8)
        assert result.status in {"off", "stale"} and not result.may_apply
        server.release.set()
    assert host.config.model_name == "deployment-model" and len(server.requests) == 1


def test_usage_refresh_does_not_wait_for_thread_write_lock(tmp_path, server):
    server.block = True
    host, params, thread, stage = configured(tmp_path, server)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(decide, host, params, stage)
        assert server.entered.wait(1)
        with locked_file_transition(host.conversation_store.threads.storage.thread_path(thread.thread_id)):
            server.release.set()
            # 线程写锁被占用时不等待：设置按已提交版本无锁读取，建议照常形成，采用前仍复核版本。
            result = future.result(timeout=0.8)
            assert result.status == "success" and result.may_apply
            metrics = params.tui_runtime.store.snapshot().status.model_metrics
            assert metrics["decision_input_tokens"] == 120
