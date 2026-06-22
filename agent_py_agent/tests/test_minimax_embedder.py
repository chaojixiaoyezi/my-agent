"""MiniMax 原生 embedding 适配器单测(#1 P2):请求构造 + 响应解析 + 选择逻辑。

MiniMax 非 OpenAI 兼容(请求 texts/type、响应 vectors/base_resp)。这里用真打 curl 验过的**真实响应
形状**喂进适配器,确定性地测我的解析/构造逻辑(不上网、不计费、不抖动);真机端到端见
test_minimax_embedder_real.py(gated)。
"""

from __future__ import annotations

import json
import urllib.request

from agent_py_agent.agent.retrieval.embedding import EmbeddingError, MiniMaxEmbedder


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


def test_build_memory_embedder_picks_minimax_for_embo_model() -> None:
    from agent_py_agent.agent.core import _build_memory_embedder
    from agent_py_agent.agent.settings.config import AgentConfig

    emb = _build_memory_embedder(
        AgentConfig(
            memory_semantic_recall=True,
            memory_embedding_model="embo-01",
            memory_embedding_api_base="https://api.minimaxi.com/v1",
            api_key="k",
        )
    )
    assert isinstance(emb, MiniMaxEmbedder) and emb.dim == 1536  # embo* → MiniMax 适配器,1536 维


def test_build_memory_embedder_non_embo_stays_openai_compatible() -> None:
    from agent_py_agent.agent.core import _build_memory_embedder
    from agent_py_agent.agent.retrieval.embedding import OpenAICompatibleEmbedder
    from agent_py_agent.agent.settings.config import AgentConfig

    emb = _build_memory_embedder(
        AgentConfig(
            memory_semantic_recall=True,
            memory_embedding_model="text-embedding-3-small",
            memory_embedding_api_base="https://api.openai.com/v1",
            api_key="k",
        )
    )
    assert isinstance(emb, OpenAICompatibleEmbedder)  # 非 embo 仍走 OpenAI 兼容,不误判


def test_build_embedder_factory_minimax_provider() -> None:
    from agent_py_agent.agent.retrieval.embedding import build_embedder

    emb = build_embedder({"provider": "minimax", "api_base": "https://api.minimaxi.com/v1", "model": "embo-01"})
    assert isinstance(emb, MiniMaxEmbedder) and emb.dim == 1536  # 工厂按 provider=minimax 造原生适配器
    assert build_embedder({"provider": "minimax", "model": "embo-01"}) is None  # 缺 api_base → None(不半配)
