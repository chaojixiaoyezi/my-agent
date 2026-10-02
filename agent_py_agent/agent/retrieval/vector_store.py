"""暴力 cosine 向量库(Phase 2,纯 Python,零外部依赖)。

参考实现 用 Milvus 做向量库;但 my-agent 面向个人/小团队,记忆量级是百~千条——**暴力遍历算
cosine 完全够用,不必引 Milvus/faiss**(用户原则:能自建就自建)。向量持久化到 JSON,
进程内加载后线性扫描求 top-k。量级真涨到十万级再考虑换 ANN 后端(接口不变,可平滑替换)。
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agent_py_agent.agent.retrieval.embedding import cosine


# LLM: 向量库身份不匹配不是普通异常：调用方要按结构化 reason 降级（语义召回退回关键词），不能吞成"没结果"。
# 类用途: 携带 P14 的身份失效原因码（VECTOR_META_MISSING / VECTOR_IDENTITY_MISMATCH）。
class VectorIdentityError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# LLM: 元数据 schema 固定；换模型后旧 meta 与当前身份不一致，向量库视为不存在，避免静默混用两个向量空间。
VECTOR_META_SCHEMA_VERSION = "my-agent.memory-vectors-meta.v1"


def _safe_cosine(query_vector: list[float], qlen: int, vec: object) -> float | None:
    """对一条存储向量算 cosine;不可比则返回 None(不是 list / 维度不一致 / 含非数值都跳过)。

    维度守卫是关键:换 embedding 模型后维度会变(如本地 256 → MiniMax embo-01 的 1536),
    旧维度向量若参与比较会被 cosine 截断成垃圾分,静默错召回——宁可跳过。
    """
    if not isinstance(vec, list) or len(vec) != qlen:
        return None
    try:
        return cosine(query_vector, [float(x) for x in vec])
    except (TypeError, ValueError):
        return None


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

    P14: 传入 identity（档案编号/服务商/模型名/维度）时，向量库旁边写 memory_vectors.meta.json，
    记录生成这批向量的嵌入身份；身份缺失或不一致时调用方必须把已有向量当不存在（退回关键词），
    不静默混用两个向量空间。identity 为 None 时保持旧行为（不管理元数据）。
    """

    def __init__(self, path: str | Path, identity: dict[str, object] | None = None) -> None:
        self._path = Path(path).expanduser()
        self._lock = threading.RLock()  # 同 owner 共享一个 store 时序列化写,保证文件落盘=最新快照
        self._items: dict[str, dict[str, object]] = self._load()
        self._identity = dict(identity) if identity else None
        self._meta_path = self._path.with_name(self._path.name + ".meta.json")

    def _load(self) -> dict[str, dict[str, object]]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    # LLM: 元数据独立于向量文件存读；meta 损坏按"没有元数据"处理（调用方退回关键词），不猜测。
    # 函数用途: 读取旁边的身份元数据文件，损坏或缺失返回 None。
    def _load_meta(self) -> dict[str, object] | None:
        try:
            data = json.loads(self._meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    # LLM: 首次写入或重建时落盘身份；只写当前 identity 与时间，不含密钥或向量正文。
    # 函数用途: 按当前身份原子写入元数据文件。
    def _write_meta(self) -> None:
        if self._identity is None:
            return
        meta = {
            "schema_version": VECTOR_META_SCHEMA_VERSION,
            "profile_id": str(self._identity.get("profile_id", "") or ""),
            "provider": str(self._identity.get("provider", "") or ""),
            "model_name": str(self._identity.get("model_name", "") or ""),
            "dim": int(self._identity.get("dim", 0) or 0),
            "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        tmp = self._meta_path.with_name(f"{self._meta_path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._meta_path)

    # LLM: 一致性只比结构化身份字段：档案编号、模型名、维度必比；服务商两端都非空才比（旧数据无服务商不算冲突）。
    # 函数用途: 判定当前身份与元数据是否一致，返回 (是否可用, 原因码)；不管理身份时恒可用。
    def identity_status(self) -> tuple[bool, str]:
        if self._identity is None:
            return True, ""
        meta = self._load_meta()
        if meta is None:
            return False, "VECTOR_META_MISSING"
        expected = {
            "profile_id": str(self._identity.get("profile_id", "") or ""),
            "model_name": str(self._identity.get("model_name", "") or ""),
            "dim": int(self._identity.get("dim", 0) or 0),
        }
        for key, value in expected.items():
            if str(meta.get(key, "") or "") != str(value):
                return False, "VECTOR_IDENTITY_MISMATCH"
        provider = str(self._identity.get("provider", "") or "")
        if provider and str(meta.get("provider", "") or "") != provider:
            return False, "VECTOR_IDENTITY_MISMATCH"
        return True, ""

    # LLM: 写入前必须保证身份已就位；meta 缺失且库里有向量=旧文件无元数据，拒绝混写；
    #   meta 缺失但库为空=首次写入，直接落身份；meta 不一致就抛错，调用方捕获后降级。
    # 函数用途: 校验并确保元数据与当前身份一致，否则拒绝写入。
    def _ensure_identity_writable(self) -> None:
        if self._identity is None:
            return
        meta = self._load_meta()
        if meta is None:
            if self._items:
                raise VectorIdentityError("VECTOR_META_MISSING")
            self._write_meta()
            return
        ok, reason = self.identity_status()
        if not ok:
            raise VectorIdentityError(reason)

    # LLM: 重建入口专用：清空向量并落新身份；identity 为 None 时只清空不写 meta（旧行为）。
    # 函数用途: 把向量库重置为空并写入当前身份元数据。
    def reset(self) -> None:
        with self._lock:
            self._items = {}
            self._flush()
            if self._identity is not None:
                self._write_meta()

    # LLM: 只读投影，不触发网络或校验；供预览入口显示当前元数据。
    # 函数用途: 返回元数据字典或 None。
    def meta(self) -> dict[str, object] | None:
        return self._load_meta()

    def _flush(self) -> None:
        # 并发健壮(同 owner 共享一个 store 时可能并发写):① 唯一 tmp 名,避免两次 flush 写同一 tmp 互相截断;
        # ② 先做 GIL 原子快照再 dumps,避免 json 迭代途中被并发 upsert 改 dict 大小而崩。
        self._path.parent.mkdir(parents=True, exist_ok=True)
        snapshot = dict(self._items)
        tmp = self._path.with_name(f"{self._path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._path)  # 原子替换

    def __len__(self) -> int:
        return len(self._items)

    def upsert(self, id: str, vector: list[float], *, text: str = "", metadata: dict[str, object] | None = None) -> None:
        if not id:
            raise ValueError("vector id required")
        with self._lock:  # 写串行:防慢 flush 用旧快照赢得 replace 竞争而丢最新更新
            self._ensure_identity_writable()
            self._items[id] = {"vector": list(vector), "text": text, "metadata": dict(metadata or {})}
            self._flush()

    def upsert_many(self, rows: list[tuple[str, list[float], str, dict[str, object]]]) -> None:
        """批量 upsert(只 flush 一次,省 IO)。rows: [(id, vector, text, metadata), ...]。"""
        with self._lock:
            self._ensure_identity_writable()
            self._apply_rows(rows)
            self._flush()

    def _apply_rows(self, rows: list[tuple[str, list[float], str, dict[str, object]]]) -> None:
        for id, vector, text, metadata in rows:
            if not id:
                continue
            self._items[id] = {"vector": list(vector), "text": text, "metadata": dict(metadata or {})}

    def remove(self, id: str) -> bool:
        with self._lock:
            if id not in self._items:
                return False
            del self._items[id]
            self._flush()
            return True

    def search(self, query_vector: list[float], *, top_k: int = 5, min_score: float = 0.0) -> list[VectorHit]:
        """线性扫描求 top_k(按 cosine 降序,过滤 < min_score)。

        维度守卫:跳过长度与 query 不一致的向量(换了 embedding 模型/版本后,旧维度向量留在库里——
        若不跳,cosine 会截到较短一方做比较 = 静默垃圾分、错召回。宁可跳过等其被同 id 重嵌覆盖)。
        单条坏向量(非数值)也跳过,不连累整库语义召回。
        """
        qlen = len(query_vector)
        with self._lock:
            snapshot = list(self._items.items())  # 锁内取快照,打分在锁外(避免迭代时被并发写改大小)
        scored: list[VectorHit] = []
        for id, item in snapshot:
            score = _safe_cosine(query_vector, qlen, item.get("vector"))
            if score is None or score < min_score:
                continue
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
