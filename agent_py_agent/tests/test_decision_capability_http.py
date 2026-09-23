"""临时 owner→能力消费者→原生 localhost HTTP→实际 prompt/schema；不调用真实供应商。"""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model.call_runtime import model_call_ledger, model_call_summary
from agent_py_agent.agent.capability.decision_recommendation import (
    CapabilityPresentation,
    recommend_capabilities,
)
from agent_py_agent.tests.test_decision_capability_consumer import model_input
from agent_py_agent.tests.test_decision_capability_consumer import surface as surface  # noqa: F401
from agent_py_agent.tests.test_decision_model_profiles import decision
from agent_py_agent.tests.test_decision_settings import patch
from agent_py_agent.tests.test_tool_presentation_projection import (
    prepared as tool_surface,  # noqa: F401
)
from agent_py_agent.tests.test_tool_presentation_projection import search


# LLM: 回答必须取本次 HTTP 的动态问题和候选编号，不能拿固定协议题替代实际消费者；不执行任何建议。
# 函数用途: 从收到的合法候选中选择一个工具和一个 Skill，并返回完整原生逐题分布。
def native_answer(payload):
    desired = {"presentation_optional_a", "workspace:method-001"}
    answers = {}
    for key, question in payload["questions"].items():
        choice = "include" if question["instructions"]["candidate"]["ref"] in desired else "not_needed"
        assert choice in question["criteria"]
        answers[key] = {"type": "choice", "choice": choice, "confidence": 1.0,
                        "probabilities": {candidate: float(candidate == choice) for candidate in question["criteria"]}}
    return {"model": "localhost-decision", "answers": answers, "usage": {"input_tokens": 41}}


# LLM: 随机 localhost 端口只接本测试的有限请求；延迟由有界 Event 控制，退出时释放并回收所有服务线程。
# 函数用途: 提供真实 socket/HTTP 字节和可控错误，不替换决定后端、worker或传输函数。
@pytest.fixture
def capability_http():
    state = SimpleNamespace(status=200, block=False, entered=threading.Event(), release=threading.Event(),
                            finished=threading.Event(), requests=[], failures=[])

    # LLM: 不打印认证头或服务器日志，结果只在内存中供断言；本服务没有外部访问或持久写入。
    # 类用途: 根据本次动态问题生成供应商样本，并允许控制在途请求的返回时间。
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                size = int(self.headers["Content-Length"])
                assert 0 < size <= 262144
                body = json.loads(self.rfile.read(size))
                state.requests.append((self.path, body))
                encoded = json.dumps(native_answer(body)).encode()
                state.entered.set()
                if state.block:
                    assert state.release.wait(3), "测试未及时释放阻塞服务器"
                self.send_response(state.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except BaseException as exc:
                state.failures.append(exc)
                state.entered.set()
            finally:
                state.finished.set()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = False
    state.url = f"http://127.0.0.1:{server.server_port}"
    worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    worker.start()
    try:
        yield state
    finally:
        state.release.set()
        server.shutdown()
        server.server_close()
        worker.join(2)
        assert not worker.is_alive() and not state.failures


# LLM: 只经原模型目录和字段 patch 指向本地测试服务，保持原消费者 fixture 的真实快照与 runner 身份。
# 函数用途: 给当前测试宿主绑定一个 localhost 原生决策配置，不碰日常 owner。
def configure(surface, http, *, timeout=1.5):
    key, _ = decision(surface.host, api_base=http.url)
    patch(surface.host, {"profile_id": key, "timeout_seconds": timeout})
    return model_input(surface, CapabilityPresentation(surface.snapshot))[:2]


# LLM: 主入口不被 mock，原 settings/backend/worker/账本依次参与，只有最终主模型输入由原 builder/schema投影读取。
# 函数用途: 调用真实能力消费者一次，并返回其已核对的本工作片展示结果。
def recommend(surface):
    return recommend_capabilities(surface.host, surface.params, surface.snapshot, surface.contract)


def test_local_http_success_reduces_actual_prompt_and_schema_and_preserves_search(surface, capability_http):
    baseline = configure(surface, capability_http)
    result = recommend(surface)
    assert result.finding == "skill_tool_decision:apply:applied"
    prompt, schema, _loop = model_input(surface, result)
    assert len(prompt.encode()) < len(baseline[0].encode())
    assert len(schema.encode()) < len(baseline[1].encode())
    assert "workspace:method-001" in prompt and "method-059" not in prompt
    assert surface.first.model_spec.name in schema and surface.second.model_spec.name not in schema
    assert result.tool_snapshot.runtimes is surface.snapshot.runtimes
    assert result.tool_snapshot.available_tool_names == surface.snapshot.available_tool_names
    _report, loaded = search(surface.host.tools, result.tool_snapshot, surface.second.model_spec.name)
    assert surface.second.model_spec.name in loaded
    assert len(capability_http.requests) == 1
    path, payload = capability_http.requests[0]
    assert path == "/v1/systemone" and set(payload) == {"model", "state", "questions"}
    assert "candidates" not in payload["state"] and len(payload["questions"]) > 60
    assert payload["state"]["policy"]["context_policy"] == "progressive"
    records = model_call_ledger(surface.host).records()
    assert len(records) == 1 and records[0].metadata["purpose"] == "decision"
    summary = model_call_summary(surface.host, request_id=surface.params.request_id)
    assert summary["provider_http_attempt_count"] == 1
    assert surface.host.backend.model_name == "original-main"


def test_local_http_timeout_preserves_exact_original_input_and_late_answer_cannot_apply(surface, capability_http):
    capability_http.block = True
    baseline = configure(surface, capability_http, timeout=0.3)
    started = time.monotonic()
    try:
        result = recommend(surface)
        assert time.monotonic() - started < 1.2
        assert capability_http.entered.is_set() and len(capability_http.requests) == 1
        assert result.tool_snapshot is surface.snapshot and result.selected_skill_ids is None
        assert result.finding.endswith(":deadline"), result.finding
        assert model_input(surface, result)[:2] == baseline
        records = model_call_ledger(surface.host).records()
        assert len(records) == 1 and records[0].status == "timed_out"
    finally:
        capability_http.release.set()
    assert capability_http.finished.wait(1)
    assert model_call_ledger(surface.host).records()[0].status == "timed_out"
    assert model_input(surface, result)[:2] == baseline


@pytest.mark.parametrize("changes", [{"enabled": False}, {"points.skill_tool.context_policy": "metadata"}])
def test_local_http_inflight_configuration_change_rejects_old_selection_before_server_release(surface, capability_http, changes):
    capability_http.block = True
    baseline = configure(surface, capability_http, timeout=2)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(recommend, surface)
        try:
            assert capability_http.entered.wait(1)
            patch(surface.host, changes)
            result = future.result(timeout=0.8)
            assert not capability_http.release.is_set()
            assert result.tool_snapshot is surface.snapshot and result.selected_skill_ids is None
            assert not result.finding.endswith(":applied")
            assert model_input(surface, result)[:2] == baseline
        finally:
            capability_http.release.set()
    assert capability_http.finished.wait(1) and len(capability_http.requests) == 1
    assert model_input(surface, result)[:2] == baseline


@pytest.mark.parametrize("status", [401, 500])
def test_local_http_errors_keep_actual_original_input_and_do_not_retry_provider(surface, capability_http, status):
    capability_http.status = status
    baseline = configure(surface, capability_http)
    first = recommend(surface)
    again = recommend(surface)
    for result in (first, again):
        assert result.tool_snapshot is surface.snapshot and result.selected_skill_ids is None
        assert model_input(surface, result)[:2] == baseline
        assert not result.finding.endswith(":applied")
    assert len(capability_http.requests) == 1
    assert model_call_summary(surface.host, request_id=surface.params.request_id)["status_counts"]["failed"] == 1
    assert surface.host.backend.model_name == "original-main"
