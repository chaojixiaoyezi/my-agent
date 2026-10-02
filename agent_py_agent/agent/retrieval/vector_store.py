"""暴力 cosine 向量库(Phase 2,纯 Python,零外部依赖)。

参考实现 用 Milvus 做向量库;但 my-agent 面向个人/小团队,记忆量级是百~千条——**暴力遍历算
cosine 完全够用,不必引 Milvus/faiss**(用户原则:能自建就自建)。向量持久化到 JSON,
进程内加载后线性扫描求 top-k。量级真涨到十万级再考虑换 ANN 后端(接口不变,可平滑替换)。

P14 快照格式(my-agent.memory-vectors.v2):空间身份、实际维度、代次和全部向量放在**同一个文件**里,一次原子替换
整体生效;写入在正式跨进程锁(locked_json_path)内"读最新快照 → 裁决 → 整份写回",读取发现文件换代就重载,
所以不会出现"B 的身份认证 A 的向量"。
"""

# LLM: 本模块是记忆向量投影的唯一读写实现；快照格式、身份裁决和锁协议改动要同步 jsonl 的语义召回/重建、
#   error_taxonomy 里的 VECTOR_* 原因码，以及 test_vector_identity、test_vector_store_robustness。
# 模块用途: 本地 JSON 向量库：单文件原子快照、跨进程一致、按空间身份与实际维度拒绝混用。

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agent_py_agent.agent.common.json_io import locked_json_path, write_text_file_atomic_unlocked
from agent_py_agent.agent.retrieval.embedding import cosine

# 快照文件 schema 版本；不是这个版本的 JSON 对象按 P14 之前的旧格式（整份就是 id→条目、没有身份）读入。
VECTOR_STORE_SCHEMA_VERSION = "my-agent.memory-vectors.v2"


# LLM: 向量库的结构化失败统一带 reason 码（登记在 error_taxonomy），调用方按码记健康状态，不解析异常文本。
# 类用途: 向量库失败的基类，携带原因码，例如重建未完成 VECTOR_REBUILD_INCOMPLETE。
class VectorStoreError(Exception):
    # LLM: reason 是机器判断的唯一依据，消息文本只给人看。
    # 函数用途: 记下原因码。
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# LLM: 向量空间不可信不是普通异常：调用方要按 reason 降级（语义召回退回关键词），不能吞成"没结果"。
# 类用途: 携带空间失效原因码：VECTOR_META_MISSING / VECTOR_IDENTITY_MISMATCH / VECTOR_DIMENSION_MISMATCH /
#   VECTOR_STORE_UNREADABLE。
class VectorIdentityError(VectorStoreError):
    pass


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


# LLM: 指纹 = (inode, 大小, 纳秒 mtime)；原子替换每次都换新文件，读者靠它发现别的实例或进程已经换代。
# 函数用途: 从 stat 结果取快照文件指纹。
def _file_stamp(st: os.stat_result) -> tuple[int, int, int]:
    return st.st_ino, st.st_size, st.st_mtime_ns


# LLM: 身份按 {str: str} 整体相等比较；值统一转字符串，避免 JSON 往返改类型造成假失配。空值表示不管理身份。
# 函数用途: 规范调用方或快照头里的身份字典，空值返回 None。
def _normalized_identity(identity: object) -> dict[str, str] | None:
    if not isinstance(identity, dict) or not identity:
        return None
    return {str(key): str(value) for key, value in identity.items()}


# LLM: 只认 v2 快照头；其它合法 JSON 对象按旧格式读入（没有头 = 没有身份）。坏 JSON、非对象、items 不是字典都算
#   读不了，不猜内容。返回 (快照头或 None, 条目, 是否可读)。
# 函数用途: 把快照文件字节解析成头、条目和可读标记。
def _parse_snapshot(raw: bytes) -> tuple[dict[str, object] | None, dict[str, dict[str, object]], bool]:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, {}, False
    if not isinstance(data, dict):
        return None, {}, False
    if data.get("schema_version") != VECTOR_STORE_SCHEMA_VERSION:
        return None, data, True
    items = data.get("items")
    if not isinstance(items, dict):
        return None, {}, False
    header = {key: data.get(key) for key in ("schema_version", "identity", "dim", "generation", "written_at")}
    return header, items, True


# LLM: 维度以实际向量为准（P14 第 5 条）：同一批写入必须等长且非空，否则抛 VECTOR_DIMENSION_MISMATCH；空批返回 None。
# 函数用途: 校验一批 (id, 向量, 正文, 元数据) 的向量维度一致并返回该维度。
def _rows_dim(rows: list[tuple[str, list[float], str, dict[str, object]]]) -> int | None:
    lengths = {len(vector) for _id, vector, _text, _metadata in rows}
    if not lengths:
        return None
    if 0 in lengths or len(lengths) > 1:
        raise VectorIdentityError("VECTOR_DIMENSION_MISMATCH")
    return lengths.pop()


# LLM: 条目形状固定为 {vector, text, metadata}，读侧 search 与 jsonl 二次核验都按这三个键取值；改形状要同步两边。
# 函数用途: 把一行写入数据变成落盘条目（向量、正文、元数据各自复制，不与调用方共享可变对象）。
def _item(vector: list[float], text: str, metadata: dict[str, object] | None) -> dict[str, object]:
    return {"vector": list(vector), "text": text, "metadata": dict(metadata or {})}


# LLM: 向量库只是可重建的派生投影。身份、维度、代次与全部向量同在一个快照文件里；写入一律在 locked_json_path 内
#   先读最新快照再整份原子替换，读取按文件指纹发现换代就重载。identity=None 是不管理身份的通用模式（测试与通用检索），
#   生产记忆语义通道必须带身份（core._memory_semantic_channel 保证）。
# 类用途: id → (向量, 文本, 元数据) 的暴力 cosine 库，单文件原子快照、跨实例跨进程一致。
class VectorStore:
    """id → (向量, 文本, 元数据) 的暴力 cosine 库。JSON 持久化,原子写。

    用法::

        vs = VectorStore(home / "memory_vectors.json", identity=space_identity)
        vs.upsert("mem-1", embedder.embed(["..."])[0], text="...", metadata={...})
        hits = vs.search(query_vec, top_k=5)

    带 identity 时(生产记忆):快照头记录生成这批向量的空间身份与实际维度;身份缺失/不一致、维度不符或文件读不了,
    读写都抛 VectorIdentityError(调用方退回关键词);空库可被当前身份接管。identity 为 None 时不管理身份,
    允许混维度(检索时跳过长度不符的向量),写入会清掉快照头里的身份担保。
    """

    # LLM: 构造只读文件、不写盘；身份在实例生命周期内固定。
    # 函数用途: 绑定快照路径与当前空间身份，并加载一次现有快照。
    def __init__(self, path: str | Path, identity: dict[str, object] | None = None) -> None:
        self._path = Path(path).expanduser()
        self._lock = threading.RLock()  # 同实例的读写在进程内串行；跨实例、跨进程的写由 locked_json_path 串行
        self._identity = _normalized_identity(identity)
        self._header: dict[str, object] | None = None
        self._items: dict[str, dict[str, object]] = {}
        self._readable = True
        self._stamp: tuple[int, int, int] | None = None
        self._reload()

    # LLM: 同一个文件句柄取指纹和内容，二者必然同代；文件不存在是确定的空库，其它读错误记为读不了。
    # 函数用途: 从磁盘重新加载快照到实例缓存。
    def _reload(self) -> None:
        try:
            with self._path.open("rb") as handle:
                stamp = _file_stamp(os.fstat(handle.fileno()))
                raw = handle.read()
        except FileNotFoundError:
            self._header, self._items, self._readable, self._stamp = None, {}, True, None
            return
        except OSError:
            self._header, self._items, self._readable, self._stamp = None, {}, False, None
            return
        self._header, self._items, self._readable = _parse_snapshot(raw)
        self._stamp = stamp

    # LLM: 每次读写前先比文件指纹，别的实例或进程换代后立即重载；没变就用缓存（只花一次 stat）。调用方须持 self._lock。
    # 函数用途: 需要时重载快照，保证接下来看到的是最新一代。
    def _refresh(self) -> None:
        try:
            stamp: tuple[int, int, int] | None = _file_stamp(self._path.stat())
        except OSError:
            stamp = None
        if stamp != self._stamp or not self._readable:
            self._reload()

    # LLM: 维度只认快照头里写下的整数（来自实际向量）；布尔值等非整数一律当没有，交给 META_MISSING 裁决。
    # 函数用途: 返回快照头记录的实际维度；没有头或值不是整数返回 None。
    def _stored_dim(self) -> int | None:
        dim = (self._header or {}).get("dim")
        return dim if isinstance(dim, int) and not isinstance(dim, bool) else None

    # LLM: 空间裁决只看结构化事实：读不了 → VECTOR_STORE_UNREADABLE；空库可被接管；有向量但头里没身份或没维度 →
    #   VECTOR_META_MISSING；身份不同 → VECTOR_IDENTITY_MISMATCH。不管理身份时恒可用。调用方须已刷新快照。
    # 函数用途: 返回当前快照对本实例身份的裁决原因码，空串表示可用。
    def _identity_reason(self) -> str:
        if self._identity is None:
            return ""
        if not self._readable:
            return "VECTOR_STORE_UNREADABLE"
        if not self._items:
            return ""
        stored = _normalized_identity((self._header or {}).get("identity"))
        if stored is None or self._stored_dim() is None:
            return "VECTOR_META_MISSING"
        return "" if stored == self._identity else "VECTOR_IDENTITY_MISMATCH"

    # LLM: 增量写入的裁决：快照身份可用、新向量与快照维度一致（空库按这批向量定维度）；不管理身份时不检查。
    # 函数用途: 校验本次写入能否进入当前快照，返回写入后快照的维度。
    def _writable_dim(self, rows: list[tuple[str, list[float], str, dict[str, object]]]) -> int | None:
        if self._identity is None:
            return None
        reason = self._identity_reason()
        if reason:
            raise VectorIdentityError(reason)
        dim = _rows_dim(rows)
        if self._items and dim != self._stored_dim():
            raise VectorIdentityError("VECTOR_DIMENSION_MISMATCH")
        return dim

    # LLM: 调用方必须已持有 self._lock 与 locked_json_path。代次每次新生成；落盘后在锁内 stat 记下新指纹，
    #   本实例不必重读整份文件。副作用：原子替换快照文件（目录不存在会创建）。
    # 函数用途: 把一份完整快照原子写盘，并更新实例缓存。
    def _write_snapshot(self, items: dict[str, dict[str, object]], identity: dict[str, str] | None, dim: int | None) -> None:
        header: dict[str, object] = {
            "schema_version": VECTOR_STORE_SCHEMA_VERSION,
            "identity": identity,
            "dim": dim if items else None,
            "generation": uuid.uuid4().hex,
            "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        write_text_file_atomic_unlocked(self._path, json.dumps({**header, "items": items}, ensure_ascii=False))
        self._header, self._items, self._readable = header, items, True
        self._stamp = _file_stamp(self._path.stat())

    # LLM: 只读汇总，与身份裁决用同一次刷新；读不了时条目数为 None（未知），不用 0 冒充。
    # 函数用途: 返回条目数和快照头，供状态预览使用。
    def summary(self) -> dict[str, object]:
        with self._lock:
            self._refresh()
            return {
                "vector_count": len(self._items) if self._readable else None,
                "meta": dict(self._header) if self._header else None,
            }

    # LLM: 对外的身份状态投影；读路径（语义召回前置检查、状态预览）使用，不写盘。
    # 函数用途: 返回 (是否可用, 原因码)；不管理身份时恒可用。
    def identity_status(self) -> tuple[bool, str]:
        with self._lock:
            self._refresh()
            reason = self._identity_reason()
        return not reason, reason

    # LLM: 先刷新再计数，别的实例或进程换代后数字立即跟上；读不了时为 0，需要区分"未知"的地方用 summary()。
    # 函数用途: 返回当前快照的条目数。
    def __len__(self) -> int:
        with self._lock:
            self._refresh()
            return len(self._items)

    # LLM: 单条写入与批量写入共用同一条裁决和落盘路径；空 id 直接拒绝。
    # 函数用途: 写入或覆盖一条向量。
    def upsert(self, id: str, vector: list[float], *, text: str = "", metadata: dict[str, object] | None = None) -> None:
        if not id:
            raise ValueError("vector id required")
        self.upsert_many([(id, vector, text, dict(metadata or {}))])

    # LLM: 增量写在跨进程锁内"刷新 → 裁决 → 整份写回"，裁决失败抛 VectorIdentityError 且不落盘。不管理身份的写入
    #   会把快照头身份置空（它无法为新向量担保），之后带身份的读者会得到 VECTOR_META_MISSING。空 id 的行跳过。
    # 函数用途: 批量写入或覆盖向量，只落盘一次。
    def upsert_many(self, rows: list[tuple[str, list[float], str, dict[str, object]]]) -> None:
        """批量 upsert(只 flush 一次,省 IO)。rows: [(id, vector, text, metadata), ...]。"""
        rows = [row for row in rows if row[0]]
        if not rows:
            return
        with self._lock, locked_json_path(self._path):
            self._refresh()
            dim = self._writable_dim(rows)
            items = dict(self._items)
            items.update({id: _item(vector, text, metadata) for id, vector, text, metadata in rows})
            self._write_snapshot(items, self._identity, dim)

    # LLM: 重建专用：用完整新快照整体替换旧库，身份取本实例、维度取这批向量，不看旧快照——这正是管理员修复失配的
    #   入口。与增量写共用同一把跨进程锁；维度不一致抛 VECTOR_DIMENSION_MISMATCH，旧库原样保留。
    # 函数用途: 原子替换整个向量库，返回写入条数。
    def replace_all(self, rows: list[tuple[str, list[float], str, dict[str, object]]]) -> int:
        rows = [row for row in rows if row[0]]
        dim = _rows_dim(rows) if self._identity is not None else None
        items = {id: _item(vector, text, metadata) for id, vector, text, metadata in rows}
        with self._lock, locked_json_path(self._path):
            self._write_snapshot(items, self._identity, dim)
        return len(items)

    # LLM: 删除只去掉精确 id，保留快照头的身份与维度（删向量不引入不可信数据），也不做身份裁决——硬删除的隐私清理
    #   不能被身份失配挡住。id 不存在返回 False 且不写盘。
    # 函数用途: 删除一条向量并原子落盘。
    def remove(self, id: str) -> bool:
        with self._lock, locked_json_path(self._path):
            self._refresh()
            if id not in self._items:
                return False
            items = {key: value for key, value in self._items.items() if key != id}
            identity = _normalized_identity((self._header or {}).get("identity"))
            self._write_snapshot(items, identity, self._stored_dim())
            return True

    # LLM: 带身份时身份与 query 维度在同一份快照上裁决，失配抛 VectorIdentityError（调用方退回关键词）；不带身份时沿用
    #   跳过长度不符向量的旧行为。锁内只取快照，打分在锁外。只读，不写盘。
    # 函数用途: 线性扫描当前快照，返回 top_k 个最相似的向量命中。
    def search(self, query_vector: list[float], *, top_k: int = 5, min_score: float = 0.0) -> list[VectorHit]:
        """线性扫描求 top_k(按 cosine 降序,过滤 < min_score)。

        带身份时:身份与维度在**同一份快照**上裁决——身份不可用或 query 维度与快照维度不同,抛 VectorIdentityError,
        不在别人的向量空间里打分。
        维度守卫:跳过长度与 query 不一致的向量(换了 embedding 模型/版本后,旧维度向量留在库里——
        若不跳,cosine 会截到较短一方做比较 = 静默垃圾分、错召回。宁可跳过等其被同 id 重嵌覆盖)。
        单条坏向量(非数值)也跳过,不连累整库语义召回。
        """
        qlen = len(query_vector)
        with self._lock:
            self._refresh()
            reason = self._identity_reason()
            if not reason and self._identity is not None and self._items and qlen != self._stored_dim():
                reason = "VECTOR_DIMENSION_MISMATCH"
            snapshot = list(self._items.items())  # 锁内取快照,打分在锁外(写入总是换新字典,不会改到这份)
        if reason:
            raise VectorIdentityError(reason)
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

    # LLM: 先刷新再列 id；保留期与硬删除核对靠它确认向量是否真被清掉，只读。
    # 函数用途: 返回当前快照的全部向量 id。
    def ids(self) -> list[str]:
        with self._lock:
            self._refresh()
            return list(self._items.keys())
