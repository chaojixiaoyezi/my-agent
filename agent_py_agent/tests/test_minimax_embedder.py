"""MiniMax 原生 embedding 适配器单测(#1 P2):请求构造 + 响应解析 + 选择逻辑。

MiniMax 非 OpenAI 兼容(请求 texts/type、响应 vectors/base_resp)。这里用真打 curl 验过的**真实响应
形状**喂进适配器,确定性地测我的解析/构造逻辑(不上网、不计费、不抖动);真机端到端见
test_minimax_embedder_real.py(gated)。
"""

from __future__ import annotations

import json
import urllib.request
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.retrieval.embedding import EmbeddingError, MiniMaxEmbedder
from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._b = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a) -> bool:
        return False


def test_minimax_builds_native_request_and_parses_vectors(monkeypatch) -> None:
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["auth"] = req.headers.get("Authorization")
        return _FakeResp({"vectors": [[3.0, 4.0]], "base_resp": {"status_code": 0, "status_msg": "success"}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    emb = MiniMaxEmbedder(api_base="https://api.minimaxi.com/v1", model="embo-01", api_key="k")
    out = emb.embed(["你好"])

    assert captured["url"] == "https://api.minimaxi.com/v1/embeddings"
    assert captured["body"] == {"model": "embo-01", "texts": ["你好"], "type": "db"}  # MiniMax 原生字段
    assert captured["auth"] == "Bearer k"
    assert len(out) == 1 and abs(out[0][0] - 0.6) < 1e-6 and abs(out[0][1] - 0.8) < 1e-6  # (3,4) L2 归一→(0.6,0.8)


def test_minimax_status_error_raises_embedding_error(monkeypatch) -> None:
    def fake_urlopen(req, timeout=None):
        return _FakeResp({"base_resp": {"status_code": 1004, "status_msg": "auth failed"}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    emb = MiniMaxEmbedder(api_base="https://x/v1", model="embo-01")
    try:
        emb.embed(["x"])
        raise AssertionError("status_code 非 0 应抛 EmbeddingError")
    except EmbeddingError as exc:
        assert "auth failed" in str(exc)  # 把 MiniMax 的错误透出,调用方据此降级 BM25


def test_minimax_empty_input_is_noop() -> None:
    assert MiniMaxEmbedder(api_base="https://x/v1", model="embo-01").embed([]) == []


def _stub(payload: dict, monkeypatch) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResp(payload))


def test_minimax_partial_count_mismatch_raises(monkeypatch) -> None:
    _stub({"vectors": [[1.0, 0.0]], "base_resp": {"status_code": 0}}, monkeypatch)  # 发2回1
    try:
        MiniMaxEmbedder(api_base="https://x/v1", model="embo-01").embed(["a", "b"])
        raise AssertionError("数量不符应抛 EmbeddingError(防批量静默错位)")
    except EmbeddingError:
        pass


def test_minimax_nonnumeric_or_missing_vectors_raise(monkeypatch) -> None:
    for bad in ({"vectors": [["a", "b"]], "base_resp": {"status_code": 0}}, {"base_resp": {"status_code": 0}}):
        _stub(bad, monkeypatch)
        try:
            MiniMaxEmbedder(api_base="https://x/v1", model="embo-01").embed(["a"])
            raise AssertionError("非数值/缺 vectors 应抛 EmbeddingError 而非裸崩")
        except EmbeddingError:
            pass


def test_openai_compatible_malformed_or_partial_raise(monkeypatch) -> None:
    from agent_py_agent.agent.retrieval.embedding import OpenAICompatibleEmbedder

    emb = OpenAICompatibleEmbedder(api_base="https://x/v1", model="m")
    _stub({"data": [{"embedding": ["x"]}]}, monkeypatch)  # 非数值
    try:
        emb.embed(["a"])
        raise AssertionError("非数值 embedding 应抛 EmbeddingError")
    except EmbeddingError:
        pass
    _stub({"data": [{"embedding": [1.0, 0.0]}]}, monkeypatch)  # 发2回1
    try:
        emb.embed(["a", "b"])
        raise AssertionError("数量不符应抛 EmbeddingError")
    except EmbeddingError:
        pass


def test_openai_compatible_real_http_roundtrip() -> None:
    """真实本地 HTTP 往返(不 monkeypatch urlopen):验 OpenAICompatibleEmbedder 真能打 OpenAI 兼容
    /embeddings(等价于指向本地 llama-server)——请求路径/体/鉴权头 + 响应解析全链路真走一遍。"""
    import http.server
    import threading

    from agent_py_agent.agent.retrieval.embedding import OpenAICompatibleEmbedder

    captured: dict = {}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            captured.update(path=self.path, input=body["input"], auth=self.headers.get("Authorization"))
            out = json.dumps({"data": [{"embedding": [1.0, 0.0]} for _ in body["input"]]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a) -> None:
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        emb = OpenAICompatibleEmbedder(api_base=f"http://127.0.0.1:{srv.server_address[1]}/v1", model="m", api_key="k")
        out = emb.embed(["hello", "world"])
    finally:
        srv.shutdown()

    assert captured["path"] == "/v1/embeddings"  # 真实请求路径
    assert captured["input"] == ["hello", "world"] and captured["auth"] == "Bearer k"  # 请求体 + 鉴权头
    assert len(out) == 2 and len(out[0]) == 2  # 响应解析 + 归一,N 进 N 出


def test_minimax_multi_text_keeps_order_and_count(monkeypatch) -> None:
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["texts"] = json.loads(req.data.decode("utf-8"))["texts"]
        # 按请求顺序返回各自向量(N 进 N 出,逐条对齐)
        return _FakeResp({"vectors": [[1.0, 0.0], [0.0, 2.0], [0.0, 0.0, 0.0]], "base_resp": {"status_code": 0}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    out = MiniMaxEmbedder(api_base="https://x/v1", model="embo-01").embed(["a", "b", "c"])
    assert captured["texts"] == ["a", "b", "c"]  # 批量原样送
    assert len(out) == 3  # N 进 N 出,不串位
    assert abs(out[0][0] - 1.0) < 1e-9 and abs(out[1][1] - 1.0) < 1e-9  # 各自归一,顺序对齐


def test_minimax_http_error_degrades_to_embedding_error(monkeypatch) -> None:
    import urllib.error

    def rate_limited(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", rate_limited)
    try:
        MiniMaxEmbedder(api_base="https://x/v1", model="embo-01").embed(["x"])
        raise AssertionError("429/5xx 应抛 EmbeddingError(供上层降级 BM25)")
    except EmbeddingError:
        pass  # 限流/服务端错误 → 降级而非崩


def test_minimax_network_error_degrades_not_crash(monkeypatch) -> None:
    def boom(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    try:
        MiniMaxEmbedder(api_base="https://x/v1", model="embo-01").embed(["x"])
        raise AssertionError("网络错误应抛 EmbeddingError(供上层降级)")
    except EmbeddingError:
        pass


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    home = SimpleNamespace(root=tmp_path, config_dir=tmp_path / "config", owner_provider="local",
                           owner_kind="main", owner_id="main")
    path = tmp_path / "desktop.yaml"
    path.write_text('agent_name: "minimax-test"\n', encoding="utf-8")
    config = load_config(path)
    return SimpleNamespace(home_paths=home, config=config)


def _add(host, **values):
    profile_id = str(uuid4())
    profile = {"model_name": "text-embedding-3-small", "model_backend": "openai_compatible",
               "api_base": "https://emb.example.test/v1", "api_key": "fake-emb-credential",
               "model_context_window_tokens": 32768, "capability": "embedding", **values}
    execute_model_profile_operation(host, "add", {"profile_id": profile_id, "profile": profile})
    return profile_id


def _agent(host, **cfg):
    return SimpleNamespace(home_paths=host.home_paths, config=AgentConfig(**cfg))


def test_build_memory_embedder_picks_minimax_for_embo_model(host) -> None:
    from agent_py_agent.agent.core import _build_memory_embedder

    profile_id = _add(host, model_name="embo-01", api_base="https://api.minimaxi.com/v1")
    emb = _build_memory_embedder(_agent(host, memory_semantic_recall=True, embedding_model_profile=profile_id))
    assert isinstance(emb, MiniMaxEmbedder) and emb.dim == 1536  # embo* → MiniMax 适配器,1536 维


def test_build_memory_embedder_non_embo_stays_openai_compatible(host) -> None:
    from agent_py_agent.agent.core import _build_memory_embedder
    from agent_py_agent.agent.retrieval.embedding import OpenAICompatibleEmbedder

    profile_id = _add(host, model_name="text-embedding-3-small", api_base="https://api.openai.com/v1")
    emb = _build_memory_embedder(_agent(host, memory_semantic_recall=True, embedding_model_profile=profile_id))
    assert isinstance(emb, OpenAICompatibleEmbedder)  # 非 embo 仍走 OpenAI 兼容,不误判


def test_new_embedding_fields_survive_normalize(monkeypatch) -> None:
    """新配置字段经 normalize_agent_config(yaml→config 的真实归一管线)不被丢/不被改——

    否则用户在 yaml 里写了开关也静默不生效(归一若有字段白名单会吞掉新字段)。
    """
    from agent_py_agent.agent.settings.normalize import normalize_agent_config

    raw = {
        "memory_semantic_recall": True,
        "embedding_model_profile": "prof-abc-123",
        "gateway_per_user_owner_scoping": True,
    }
    normalized, _warnings = normalize_agent_config(raw)
    for key, value in raw.items():
        assert normalized.get(key) == value, f"normalize 丢了/改了新字段 {key}"
