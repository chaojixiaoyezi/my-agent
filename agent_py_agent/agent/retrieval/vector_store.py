"""暴力 cosine 向量库(Phase 2,纯 Python,零外部依赖)。

claw 用 Milvus 做向量库;但 my-agent 面向个人/小团队,记忆量级是百~千条——**暴力遍历算
cosine 完全够用,不必引 Milvus/faiss**(用户原则:能自建就自建)。向量持久化到 JSON,
进程内加载后线性扫描求 top-k。量级真涨到十万级再考虑换 ANN 后端(接口不变,可平滑替换)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from agent_py_agent.agent.retrieval.embedding import cosine


@dataclass(frozen=True)
class VectorHit:
    id: str
    score: float
    text: str
    metadata: dict[str, object]


class VectorStore:
    """id → (向量, 文本, 元数据) 的暴力 cosine 库。JSON 持久化,原子写。

    用法::

        vs = VectorStore(home / "memory_vectors.json")
        vs.upsert("mem-1", embedder.embed(["..."])[0], text="...", metadata={...})
        hits = vs.search(query_vec, top_k=5)
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path).expanduser()
        self._items: dict[str, dict[str, object]] = self._load()

    def _load(self) -> dict[str, dict[str, object]]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(self._items, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._path)  # 原子替换

    def __len__(self) -> int:
        return len(self._items)

    def upsert(self, id: str, vector: list[float], *, text: str = "", metadata: dict[str, object] | None = None) -> None:
        if not id:
            raise ValueError("vector id required")
        self._items[id] = {"vector": list(vector), "text": text, "metadata": dict(metadata or {})}
        self._flush()

    def upsert_many(self, rows: list[tuple[str, list[float], str, dict[str, object]]]) -> None:
        """批量 upsert(只 flush 一次,省 IO)。rows: [(id, vector, text, metadata), ...]。"""
        for id, vector, text, metadata in rows:
            if not id:
                continue
            self._items[id] = {"vector": list(vector), "text": text, "metadata": dict(metadata or {})}
        self._flush()

    def remove(self, id: str) -> bool:
        if id not in self._items:
            return False
        del self._items[id]
        self._flush()
        return True

    def search(self, query_vector: list[float], *, top_k: int = 5, min_score: float = 0.0) -> list[VectorHit]:
        """线性扫描求 top_k(按 cosine 降序,过滤 < min_score)。"""
        scored: list[VectorHit] = []
        for id, item in self._items.items():
            vec = item.get("vector")
            if not isinstance(vec, list):
                continue
            score = cosine(query_vector, [float(x) for x in vec])
            if score >= min_score:
                scored.append(
                    VectorHit(
                        id=id,
                        score=score,
                        text=str(item.get("text", "")),
                        metadata=dict(item.get("metadata", {}) if isinstance(item.get("metadata"), dict) else {}),
                    )
                )
        scored.sort(key=lambda h: (-h.score, h.id))
        return scored[: max(0, top_k)]

    def ids(self) -> list[str]:
        return list(self._items.keys())
