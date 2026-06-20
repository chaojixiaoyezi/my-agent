"""Embedding 抽象 + 本地确定性 embedder + 可选语义端点(Phase 2)。

综合三家:学 claw memory/embedding.py(LocalHashingEmbedder 纯 Python 特征哈希 + cosine)
+ 通道运行时 可插拔/可降级(配了 provider 才启用语义,否则降级词面)。

自建优先:
- ``LocalHashingEmbedder``:纯 Python、零依赖、确定性——signed feature hashing,默认/兜底/测试。
- ``OpenAICompatibleEmbedder``:调 ``/embeddings`` 端点拿真语义向量。注意——这**不是引入库**,
  只是个 stdlib ``urllib`` 写的 HTTP 客户端(my-agent 自建);密钥经 Phase 1 SecretStore by-ref 解析。
  端点抖动抛 ``EmbeddingError``,调用方降级到 BM25,绝不让检索崩。
"""

from __future__ import annotations

import hashlib
import json
import math
import urllib.error
import urllib.request
from typing import Any, Protocol, runtime_checkable

from agent_py_agent.agent.retrieval.lexical import tokenize

DEFAULT_EMBED_DIM = 256


@runtime_checkable
class EmbeddingProvider(Protocol):
    """把文本批量编码成定长向量。真实后端(OpenAI 兼容 /embeddings 等)实现同协议即可替换。"""

    @property
    def dim(self) -> int: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class EmbeddingError(RuntimeError):
    """embedding 端点不可用;调用方据此降级到 BM25。"""


def l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


def cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度;两边都已 L2 归一时就是点积。长度不等取较短。"""
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    return sum(a[i] * b[i] for i in range(n))


def _hash_bucket(token: str, dim: int) -> tuple[int, float]:
    """稳定哈希(sha1,非进程内 salted hash)→ (桶下标, 符号),signed feature hashing。"""
    digest = hashlib.sha1(token.encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:4], "big") % dim
    sign = 1.0 if digest[4] & 1 else -1.0
    return bucket, sign


def _features(text: str) -> list[str]:
    """特征:词/CJK-bigram(复用 lexical.tokenize)+ 字符 trigram(捕模糊/形近)。"""
    low = text.lower()
    feats = list(tokenize(low))
    compact = "".join(low.split())
    feats += [compact[i : i + 3] for i in range(len(compact) - 2)]
    return feats or ["∅"]


class LocalHashingEmbedder:
    """确定性本地 embedder(默认/兜底/测试):特征哈希 → 定长向量,L2 归一。零依赖、可复现。"""

    def __init__(self, dim: int = DEFAULT_EMBED_DIM) -> None:
        self._dim = max(8, int(dim))

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        for token in _features(text):
            bucket, sign = _hash_bucket(token, self._dim)
            vec[bucket] += sign
        return l2_normalize(vec)


class OpenAICompatibleEmbedder:
    """调 OpenAI 兼容 ``/embeddings`` 端点的语义 embedder(生产路径)。

    POST {api_base}/embeddings {model, input:[...]} → {data:[{embedding:[...]}]}。
    **零外部库**:用 stdlib ``urllib`` 自建客户端。失败抛 ``EmbeddingError``,调用方降级 BM25。
    """

    def __init__(
        self,
        *,
        api_base: str,
        model: str,
        api_key: str = "",
        dim: int = DEFAULT_EMBED_DIM,
        timeout: float = 30.0,
        normalize: bool = True,
    ) -> None:
        self._api_base = api_base.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._dim = int(dim)
        self._timeout = timeout
        self._normalize = normalize

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        body = json.dumps({"model": self._model, "input": list(texts)}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        req = urllib.request.Request(f"{self._api_base}/embeddings", data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode("utf-8")).get("data") or []
        except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as exc:
            raise EmbeddingError(f"embedding 端点调用失败:{type(exc).__name__}") from exc
        vectors = [list(map(float, item.get("embedding") or [])) for item in data]
        if self._normalize:
            vectors = [l2_normalize(v) for v in vectors]
        return vectors


def build_embedder(config: dict[str, Any] | None, *, secret_resolver: Any = None) -> EmbeddingProvider | None:
    """按配置造 embedder。返回 None = 未配置 → 上层走纯 BM25 词面召回。

    provider: ``local``(确定性本地,无需端点)| ``openai_compatible``(配 api_base/model/
    api_key_source)。密钥经 secret_resolver 解析(接 Phase 1 SecretStore.resolve_source,by-ref/env,
    不落明文)。配置非法/缺字段 → None。
    """
    if not isinstance(config, dict):
        return None
    provider = str(config.get("provider") or "").strip().lower()
    dim = int(config.get("dim") or DEFAULT_EMBED_DIM)
    if provider == "local":
        return LocalHashingEmbedder(dim=dim)
    if provider in ("openai_compatible", "openai", "http"):
        api_base = str(config.get("api_base") or "").strip()
        model = str(config.get("model") or "").strip()
        if not api_base or not model:
            return None
        api_key = ""
        source = str(config.get("api_key_source") or "")
        if source and callable(secret_resolver):
            try:
                api_key = str(secret_resolver(source))
            except Exception:
                api_key = ""
        return OpenAICompatibleEmbedder(api_base=api_base, model=model, api_key=api_key, dim=dim)
    return None
