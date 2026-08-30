"""Embedding 抽象 + 本地确定性 embedder + 可选语义端点(Phase 2)。

综合三家:学 claw memory/embedding.py(LocalHashingEmbedder 纯 Python 特征哈希 + cosine)
+ 通道运行时 可插拔/可降级(配了 provider 才启用语义,否则降级词面)。

自建优先:
- ``LocalHashingEmbedder``:纯 Python、零依赖、确定性——signed feature hashing,默认/兜底/测试。
- ``OpenAICompatibleEmbedder``:调 ``/embeddings`` 端点拿真语义向量。注意——这**不是引入库**,
  只是个 stdlib ``urllib`` 写的 HTTP 客户端(my-agent 自建);密钥由调用方注入的 resolver 解析。
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
_EMBED_TIMEOUT = 30.0


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


def _column_mean(vecs: list[list[float]]) -> list[float]:
    """逐维均值(各列平均),即文档集的"通用方向"向量。"""
    n = len(vecs)
    dim = len(vecs[0])
    return [sum(vecs[i][j] for i in range(n)) / n for j in range(dim)]


def _subtract_mean(vec: list[float], mean: list[float]) -> list[float]:
    """vec 减 mean 后 L2 归一(按 mean 维度,防长度不一)。"""
    return l2_normalize([vec[i] - mean[i] for i in range(len(mean))])


def mean_center(
    query_vec: list[float], doc_vecs: list[list[float]]
) -> tuple[list[float], list[list[float]]]:
    """减文档集均值向量,去 embedding 的"通用方向偏置"(某些文档和所有查询都高相似→干扰召回),
    提升语义召回区分度(实测真实 embedding 换词查询召回 @3→@1,无关项 cosine 转负被过滤)。
    <2 文档时均值无意义,原样返回。

    检索经典的 all-but-the-top / mean-centering:纯本地、零依赖、对任何 embedder 通用。
    """
    if len(doc_vecs) < 2:
        return query_vec, doc_vecs
    mean = _column_mean(doc_vecs)
    return _subtract_mean(query_vec, mean), [_subtract_mean(v, mean) for v in doc_vecs]


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


def _one_vector(v: object) -> list[float]:
    """单条返回向量 → L2 归一;非 list / 含非数值都抛 EmbeddingError(供调用方降级,不裸崩)。"""
    if not isinstance(v, list):
        raise EmbeddingError(f"embedding 响应含非向量元素:{type(v).__name__}")
    try:
        return l2_normalize([float(x) for x in v])
    except (TypeError, ValueError) as exc:
        raise EmbeddingError(f"embedding 响应含非数值向量:{type(exc).__name__}") from exc


def _parse_vectors(raw: object, expected: int) -> list[list[float]]:
    """把端点返回的向量数组解析+L2归一;数量/形状不符一律抛 EmbeddingError(供降级),不让坏响应裸崩。

    数量守卫(len 必须等于请求条数)堵住"批量少返回→静默错位";逐条非 list/非数值也抛错而非裸异常——
    兑现"端点异常→EmbeddingError→调用方降级 BM25"的契约(此前解析在 try 外,坏 200 响应会裸崩)。
    """
    if not isinstance(raw, list) or len(raw) != expected:
        got = len(raw) if isinstance(raw, list) else type(raw).__name__
        raise EmbeddingError(f"embedding 响应向量数不符:期望 {expected} 得 {got}")
    return [_one_vector(v) for v in raw]


class OpenAICompatibleEmbedder:
    """调 OpenAI 兼容 ``/embeddings`` 端点的语义 embedder(生产路径)。

    POST {api_base}/embeddings {model, input:[...]} → {data:[{embedding:[...]}]}。
    **零外部库**:用 stdlib ``urllib`` 自建客户端。失败抛 ``EmbeddingError``,调用方降级 BM25。
    """

    def __init__(self, *, api_base: str, model: str, api_key: str = "", dim: int = DEFAULT_EMBED_DIM) -> None:
        self._api_base = api_base.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._dim = int(dim)
        self._timeout = _EMBED_TIMEOUT

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
                data = json.loads(resp.read().decode("utf-8")).get("data")
        except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as exc:
            raise EmbeddingError(f"embedding 端点调用失败:{type(exc).__name__}") from exc
        rows = data if isinstance(data, list) else []
        return _parse_vectors([item.get("embedding") if isinstance(item, dict) else None for item in rows], len(texts))


class MiniMaxEmbedder:
    """MiniMax 原生 ``/embeddings`` 端点的语义 embedder(国际站 ``api.minimaxi.com/v1``)。

    ⚠️ MiniMax **非 OpenAI 兼容**:请求体是 ``{model, texts:[...], type}``(字段叫 ``texts`` 不是
    ``input``),响应是 ``{vectors:[[...]], base_resp:{status_code}}``(不是 OpenAI 的
    ``data[].embedding``)。故单独适配,同 ``EmbeddingProvider`` 协议、可直接替换。零外部库(stdlib
    ``urllib`` 自建)。失败/``status_code`` 非 0 抛 ``EmbeddingError``,调用方降级 BM25,绝不让检索崩。
    ``type`` 固定 ``"db"``(存储语义);查询侧 MiniMax 推荐 ``"query"``,留作后续按 kind 细分的优化。
    embo-01 维度 1536。真机已验:Bearer key 直连、无需 GroupId(国际站比国内站简单)。
    """

    def __init__(self, *, api_base: str, model: str = "embo-01", api_key: str = "", dim: int = 1536) -> None:
        self._api_base = api_base.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._dim = int(dim)
        self._timeout = _EMBED_TIMEOUT

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        body = json.dumps({"model": self._model, "texts": list(texts), "type": "db"}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        req = urllib.request.Request(f"{self._api_base}/embeddings", data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as exc:
            raise EmbeddingError(f"MiniMax embedding 端点调用失败:{type(exc).__name__}") from exc
        if (payload.get("base_resp") or {}).get("status_code") not in (0, None):
            raise EmbeddingError(f"MiniMax embedding 返回错误:{(payload.get('base_resp') or {}).get('status_msg')}")
        return _parse_vectors(payload.get("vectors"), len(texts))  # 守卫:数量/形状不符→EmbeddingError


# LLM: embedding 不拥有密钥存储；只调用上层注入的 resolver，异常不得泄露
# source 或密钥正文。
# 函数用途: 把配置中的密钥引用解析成调用 embedding 端点所需的值。
def _resolve_api_key(source: str, secret_resolver: Any) -> str:
    """经调用方注入的 secret_resolver 解析密钥；失败或无解析器时返回空。"""
    if not source or not callable(secret_resolver):
        return ""
    try:
        return str(secret_resolver(source))
    except Exception:
        return ""


def build_embedder(config: dict[str, Any] | None, *, secret_resolver: Any = None) -> EmbeddingProvider | None:
    """按配置造 embedder。返回 None = 未配置 → 上层走纯 BM25 词面召回。

    provider: ``local``(确定性本地,无需端点)| ``openai_compatible``(配 api_base/model/
    api_key_source)。密钥经 secret_resolver 解析(by-ref/env,不落明文)。配置非法/缺字段 → None。
    """
    if not isinstance(config, dict):
        return None
    provider = str(config.get("provider") or "").strip().lower()
    dim = int(config.get("dim") or DEFAULT_EMBED_DIM)
    if provider == "local":
        return LocalHashingEmbedder(dim=dim)
    if provider not in ("openai_compatible", "openai", "http", "minimax"):
        return None
    api_base = str(config.get("api_base") or "").strip()
    model = str(config.get("model") or "").strip()
    if not api_base or not model:
        return None
    api_key = _resolve_api_key(str(config.get("api_key_source") or ""), secret_resolver)
    if provider == "minimax":  # MiniMax 原生协议(texts/type/vectors),非 OpenAI 兼容
        return MiniMaxEmbedder(api_base=api_base, model=model, api_key=api_key, dim=int(config.get("dim") or 1536))
    return OpenAICompatibleEmbedder(api_base=api_base, model=model, api_key=api_key, dim=dim)
