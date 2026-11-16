# LLM: C仅存coordinator_io策略；原锁/reload/身份/编码门外，准确FD出生及固定快照raw末写逐次消费原scope。
# LLM: 向量身份和正式记录删除分别守原合同；损坏库不能被当作空库报告删除成功，保留唯一原子存储。
# 模块用途: 保存并检索正式向量，为精确删除提供可确认的清理结果。
"""暴力 cosine 向量库(Phase 2,纯 Python,零外部依赖)。

参考实现 用 Milvus 做向量库;但 my-agent 面向个人/小团队,记忆量级是百~千条——**暴力遍历算
cosine 完全够用,不必引 Milvus/faiss**(用户原则:能自建就自建)。向量持久化到 JSON,
进程内加载后线性扫描求 top-k。量级真涨到十万级再考虑换 ANN 后端(接口不变,可平滑替换)。

P14 快照格式(my-agent.memory-vectors.v2):空间身份、实际维度、代次和全部向量放在**同一个文件**里,一次原子替换
整体生效;写入在正式跨进程锁(locked_json_path)内"读最新快照 → 裁决 → 整份写回",读取发现文件换代就重载,
所以不会出现"B 的身份认证 A 的向量"。

D-12 追加记录:快照不小于 _VECTOR_APPEND_MIN_BASE_BYTES 时,只新增 id 的写入不再整份重写,而是在同一个文件末尾追加
一行 "\\n" + {schema_version: my-agent.memory-vectors.v2.append, generation: 快照代次, items: {...}} 并 fsync;
其它实例发现 inode 未变、长度增长时只读新增字节。覆盖已有 id、删除、身份或维度变化、文件权限不私有,或追加段将超过
快照字节数的 _VECTOR_APPEND_MAX_TAIL_PERCENT% 时,仍整份原子重写(顺带压实),所以被删或被覆盖的旧正文不会残留。
末条不完整是崩溃残尾,读者不读入,下一次追加先截掉。旧版本按整份 json.loads 读带追加段的文件会失败,得到
VECTOR_STORE_UNREADABLE(语义召回退回关键词、写入和删除被拒绝),不会静默丢向量;压实或重建后旧版本又能正常读取。
"""

# LLM: 本模块是记忆向量投影的唯一读写实现；快照格式、身份裁决和锁协议改动要同步 jsonl 的语义召回/重建、
#   error_taxonomy 里的 VECTOR_* 原因码，以及 test_vector_identity、test_vector_store_robustness。
# 模块用途: 本地 JSON 向量库：单文件原子快照、跨进程一致、按空间身份与实际维度拒绝混用。

from __future__ import annotations

import json
import math
import os
import stat
import threading
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agent_py_agent.agent.common.json_io import (
    locked_json_path,
    write_private_text_file_atomic_unlocked,
)
from agent_py_agent.agent.retrieval.embedding import cosine

from ..common.global_write_commit import execute_global_write

# 快照文件 schema 版本；不是这个版本的 JSON 对象按 P14 之前的旧格式（整份就是 id→条目、没有身份）读入。
VECTOR_STORE_SCHEMA_VERSION = "my-agent.memory-vectors.v2"
# 快照之后追加记录的 schema 版本；每条记录带所属快照的 generation，换代后的旧记录不会被套到新快照上。
VECTOR_STORE_APPEND_SCHEMA_VERSION = "my-agent.memory-vectors.v2.append"
# 快照小于这个字节数时整份重写本来就便宜，继续保持单个 JSON 文档（旧版本照常读）；达到后新增 id 才走追加。
_VECTOR_APPEND_MIN_BASE_BYTES = 1024 * 1024
# 追加段累计将超过快照字节数的这个百分比时整份压实一次，限定冷启动多读的字节和压实摊销。
_VECTOR_APPEND_MAX_TAIL_PERCENT = 25


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


# LLM: 向量在装载/插入时已经转换并校验为有限float元组；查询阶段只核长度，不反复转换全库。
# 函数用途: 对已验证向量计算相似度，坏向量或维度不符直接跳过。
def _safe_cosine(query_vector: list[float], qlen: int, vec: object) -> float | None:
    """对一条存储向量算 cosine;不可比则返回 None(不是 list / 维度不一致 / 含非数值都跳过)。

    维度守卫是关键:换 embedding 模型后维度会变(如本地 256 → MiniMax embo-01 的 1536),
    旧维度向量若参与比较会被 cosine 截断成垃圾分,静默错召回——宁可跳过。
    """
    if not isinstance(vec, tuple) or len(vec) != qlen:
        return None
    return cosine(query_vector, vec)


# LLM: 数值转换只在读/写边界发生；非有限值或坏形状不进入打分，坏旧条目原载荷仍保留以免重写丢数据。
# 函数用途: 把可用向量转换为不可变浮点元组。
def _validated_vector(vector: object) -> tuple[float, ...] | None:
    if not isinstance(vector, (list, tuple)):
        return None
    try:
        values = tuple(float(value) for value in vector)
        return values if values and all(math.isfinite(value) for value in values) else None
    except (TypeError, ValueError, OverflowError):
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
#   读不了，不猜内容。返回 (快照头或 None, 条目, 是否可读)。接受 bytes 或 memoryview，切片解码不复制整段字节。
# 函数用途: 把快照文件字节解析成头、条目和可读标记。
def _parse_snapshot(raw: bytes | memoryview) -> tuple[dict[str, object] | None, dict[str, dict[str, object]], bool]:
    try:
        data = json.loads(str(raw, "utf-8"))
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


# LLM: 装载边界把合法向量转换成float元组，坏向量留原值供兼容跳过；快照条目与追加记录条目共用这一处。
# 函数用途: 原地预校验一批已解析条目的向量并返回同一个字典。
def _validated_items(items: dict[str, dict[str, object]]) -> dict[str, dict[str, object]]:
    for item in items.values():
        if isinstance(item, dict) and (vector := _validated_vector(item.get("vector"))) is not None:
            item["vector"] = vector
    return items


# LLM: data[start:] 必须由 "\n" 开头的记录组成；空行跳过。末条解析失败是崩溃或并发写到一半的残尾，不算已提交、
#   不推进位置；中间坏记录、schema 或 generation 不符都返回 None（调用方整份重读或判读不了，不猜内容）。
# 函数用途: 解析快照之后的追加记录，返回 (已提交末尾偏移, 按顺序合并的条目)。
def _tail_records(generation: object, data: bytes, start: int) -> tuple[int, dict[str, dict[str, object]]] | None:
    rows: dict[str, dict[str, object]] = {}
    consumed = position = start
    while position < len(data):
        if data[position] != ord("\n"):
            return None
        stop = data.find(b"\n", position + 1)
        last = stop < 0
        stop = len(data) if last else stop
        body = data[position + 1:stop]
        if body.strip():
            try:
                record = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                if last:
                    break
                return None
            items = record.get("items") if isinstance(record, dict) else None
            if (not isinstance(items, dict) or record.get("schema_version") != VECTOR_STORE_APPEND_SCHEMA_VERSION
                    or not isinstance(generation, str) or record.get("generation") != generation):
                return None
            rows.update(_validated_items(items))
        consumed = position = stop
    return consumed, rows


# LLM: 第一行是单行 v2 快照时其后只能是追加记录；旧格式、多行文件和无换行文件仍整份 json.loads，行为与原来一致。
#   追加位置 (快照字节数, 已提交末尾) 只对单行 v2 快照给出，其它格式为 None（下一次写入整份重写成单行快照）。
# 函数用途: 把整个文件字节解析成头、条目、可读标记和追加位置。
def _parse_store(raw: bytes) -> tuple[dict[str, object] | None, dict[str, dict[str, object]], bool, tuple[int, int] | None]:
    newline = raw.find(b"\n")
    if newline >= 0:
        header, items, _readable = _parse_snapshot(memoryview(raw)[:newline])
        if header is not None:
            parsed = _tail_records(header.get("generation"), raw, newline)
            if parsed is None:
                return None, {}, False, None
            items = _validated_items(items)
            items.update(parsed[1])
            return header, items, True, (newline, parsed[0])
    header, items, readable = _parse_snapshot(raw)
    tail = (len(raw), len(raw)) if header is not None and newline < 0 else None
    return header, _validated_items(items), readable, tail


# LLM: 调用方持 self._lock 与 locked_json_path；不跟随符号链接，打开后先核对文件仍是刷新时那一代（指纹一致），
#   有崩溃残尾先截回已提交末尾，再写一条完整记录并 fsync。写入失败尽力截回原末尾再抛出，调用方不发布实例状态。
# 函数用途: 在已提交末尾追加一条记录，返回追加后的文件指纹。
def _append_record_unlocked(path: Path, expected: tuple[int, int, int] | None, end: int, payload: bytes) -> tuple[int, int, int]:
    descriptor = os.open(path, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    try:
        current = _file_stamp(os.fstat(descriptor))
        if expected is None or current != expected or current[1] < end:
            raise OSError("vector snapshot changed before append")
        try:
            if current[1] > end:
                os.ftruncate(descriptor, end)
            os.lseek(descriptor, end, os.SEEK_SET)
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short write while appending vector record")
                view = view[written:]
            os.fsync(descriptor)
        except BaseException:
            try:
                os.ftruncate(descriptor, end)
            except OSError:
                pass
            raise
        return _file_stamp(os.fstat(descriptor))
    finally:
        os.close(descriptor)


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
# LLM: 插入前验证有限数值，内存只存不可变float元组；JSON仍自然编码为数组。
# 函数用途: 校验并复制一行向量数据，拒绝坏向量。
def _item(vector: list[float], text: str, metadata: dict[str, object] | None) -> dict[str, object]:
    validated = _validated_vector(vector)
    if validated is None:
        raise VectorIdentityError("VECTOR_DIMENSION_MISMATCH")
    return {"vector": validated, "text": text, "metadata": dict(metadata or {})}


# LLM: 向量库只是可重建的派生投影。身份、维度、代次与全部向量同在一个快照文件里；写入一律在 locked_json_path 内
#   先读最新快照再整份原子替换（大快照只新增 id 时改为同文件追加一条记录，见模块说明），读取按文件指纹发现换代就重载、
#   只变长就只读新增字节。identity=None 是不管理身份的通用模式（测试与通用检索），
#   生产记忆语义通道必须带身份（core._memory_semantic_channel 保证）。
# 类用途: id → (向量, 文本, 元数据) 的暴力 cosine 库，单文件原子快照、跨实例跨进程一致。
# LLM: C只保存IO策略且沿原RLock/同path文件锁，无source/第二投影；身份、维度和原缓存发布保持。
# 类用途: 管理唯一原向量快照，C末写仍与原实例算法共用。
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
    # LLM: coordinator_io默认False仅纯策略字段，原读取/身份不变，不在构造保存活来源。
    # 函数用途: 绑定原向量快照和IO策略，启动只读现有文件。
    def __init__(self, path: str | Path, identity: dict[str, object] | None = None, *, coordinator_io: bool = False) -> None:
        self.coordinator_io = coordinator_io
        self._path = Path(path).expanduser()
        self._lock = threading.RLock()  # 同实例的读写在进程内串行；跨实例、跨进程的写由 locked_json_path 串行
        self._identity = _normalized_identity(identity)
        self._header: dict[str, object] | None = None
        self._items: dict[str, dict[str, object]] = {}
        self._readable = True
        self._stamp: tuple[int, int, int] | None = None
        self._tail: tuple[int, int] | None = None  # (快照字节数, 已提交末尾)；只有单行 v2 快照才可追加与增量读
        self._reload()

    # LLM: 默认原locked_json_path调用形状；C只交准确FD出生，锁等待与所有向量裁决仍在Root外。
    # 函数用途: 取得本快照原跨进程锁，不保存或复用请求来源。
    def _locked_snapshot(self):
        if self.coordinator_io:
            return locked_json_path(self._path, birth_commit=_coordinator_vector_write)
        return locked_json_path(self._path)

    # LLM: 同一个文件句柄取指纹和内容，二者必然同代；文件不存在是确定的空库，其它读错误记为读不了。
    # LLM: 装载边界把合法向量转换成float元组，坏向量留原值供兼容跳过，避免查询重复数值转换。
    # LLM: 同一 inode 只是变长时先试增量读（只读新增字节）；增量读不成立才回到同一句柄整份重读。
    # 函数用途: 从同一FD装载向量快照并预校验向量。
    def _reload(self) -> None:
        try:
            with self._path.open("rb") as handle:
                stamp = _file_stamp(os.fstat(handle.fileno()))
                if self._read_appended(handle, stamp):
                    return
                handle.seek(0)
                raw = handle.read(stamp[1])  # 只读指纹记录的长度，并发追加的新字节留给下一次增量读
        except FileNotFoundError:
            self._header, self._items, self._readable, self._stamp, self._tail = None, {}, True, None, None
            return
        except OSError:
            self._header, self._items, self._readable, self._stamp, self._tail = None, {}, False, None, None
            return
        self._header, self._items, self._readable, self._tail = _parse_store(raw)
        self._stamp = stamp

    # LLM: 只在本实例已装载可读的单行 v2 快照、文件 inode 不变且比已提交末尾更长时成立；新增字节必须至少含一条
    #   本代 generation 的完整记录，否则（被换代后 inode 复用、只剩残尾、被截短、只改了 mtime）交给整份重读。
    #   条目按写时复制换新字典，锁外打分的快照不受影响。
    # 函数用途: 只读取并应用已提交末尾之后的追加记录，成功返回 True。
    def _read_appended(self, handle, stamp: tuple[int, int, int]) -> bool:
        tail, known = self._tail, self._stamp
        if (tail is None or known is None or not self._readable or self._header is None
                or stamp[0] != known[0] or stamp[1] <= tail[1]):
            return False
        handle.seek(tail[1])
        parsed = _tail_records(self._header.get("generation"), handle.read(stamp[1] - tail[1]), 0)
        if parsed is None or not parsed[1]:
            return False
        self._items = {**self._items, **parsed[1]}
        self._tail, self._stamp = (tail[0], tail[1] + parsed[0]), stamp
        return True

    # LLM: 每次读写前先比文件指纹，别的实例或进程换代后立即重载；没变就用缓存（只花一次 stat）。调用方须持 self._lock。
    # 函数用途: 需要时重载快照，保证接下来看到的是最新一代。
    def _refresh(self) -> None:
        try:
            stamp: tuple[int, int, int] | None = _file_stamp(self._path.stat())
        except OSError:
            stamp = None
        if stamp != self._stamp or (not self._readable and self._stamp is None):
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
    # LLM: C编码/代次在Root外，准确原raw末replace_once进门；真正写成功后才原缓存/指纹发布。
    # 函数用途: 写同一个完整向量快照并在成功后更新实例事实。
    def _write_snapshot(self, items: dict[str, dict[str, object]], identity: dict[str, str] | None, dim: int | None) -> None:
        header: dict[str, object] = {
            "schema_version": VECTOR_STORE_SCHEMA_VERSION,
            "identity": identity,
            "dim": dim if items else None,
            "generation": uuid.uuid4().hex,
            "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if self.coordinator_io:
            raw = json.dumps({**header, "items": items}, ensure_ascii=False)
            _coordinator_vector_write(lambda: write_private_text_file_atomic_unlocked(
                self._path, raw, replace_once=True))
        else:
            write_private_text_file_atomic_unlocked(self._path, json.dumps({**header, "items": items}, ensure_ascii=False))
        self._header, self._items, self._readable = header, items, True
        self._stamp = _file_stamp(self._path.stat())
        self._tail = (self._stamp[1], self._stamp[1])  # 整份写出的是单行快照，没有追加段

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

    # LLM: 增量写在跨进程锁内"刷新 → 裁决 → 写回"，裁决失败抛 VectorIdentityError 且不落盘。不管理身份的写入
    #   会把快照头身份置空（它无法为新向量担保），之后带身份的读者会得到 VECTOR_META_MISSING。空 id 的行跳过。
    # 函数用途: 批量写入或覆盖向量，只落盘一次。
    # LLM: C仅锁FD出生进原scope，等待/reload/身份/维度/合并仍Root外，原行过滤和一次写保持。
    # LLM: 全是新 id 且快照头不变时只追加一条记录（_append_payload 定条件）；其余情况仍整份写回并压实追加段。
    # 函数用途: 批量写原向量，保唯一快照与原跨实例一致性。
    def upsert_many(self, rows: list[tuple[str, list[float], str, dict[str, object]]]) -> None:
        """批量 upsert(只 flush 一次,省 IO)。rows: [(id, vector, text, metadata), ...]。"""
        rows = [row for row in rows if row[0]]
        if not rows:
            return
        with self._lock, self._locked_snapshot():
            self._refresh()
            dim = self._writable_dim(rows)
            added = {id: _item(vector, text, metadata) for id, vector, text, metadata in rows}
            payload = self._append_payload(added, dim)
            if payload is not None:
                self._append_items(added, payload)
                return
            items = dict(self._items)
            items.update(added)
            self._write_snapshot(items, self._identity, dim)

    # LLM: 追加与整份写回必须得到同一份逻辑快照：只接受全新 id（覆盖会让旧正文留在盘上，删除另走整份写回），
    #   快照头的身份和维度与整份写回将写出的完全一致，文件是刷新时那一代、未跟随符号链接且权限已私有（否则整份
    #   写回顺带收紧到 0600）。编码在 C 门外完成，追加段将超出预算时返回 None 改走整份压实。调用方须持两把锁并已刷新。
    # 函数用途: 判断本次写入能否只追加一条记录，能则返回编码好的记录字节。
    def _append_payload(self, added: dict[str, dict[str, object]], dim: int | None) -> bytes | None:
        tail, header = self._tail, self._header or {}
        generation = header.get("generation")
        if (tail is None or not self._readable or not isinstance(generation, str) or not generation
                or tail[0] < _VECTOR_APPEND_MIN_BASE_BYTES
                or _normalized_identity(header.get("identity")) != self._identity or self._stored_dim() != dim
                or any(key in self._items for key in added)):
            return None
        try:
            info = os.lstat(self._path)
        except OSError:
            return None
        if (not stat.S_ISREG(info.st_mode) or _file_stamp(info) != self._stamp
                or (os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077)):
            return None
        record = {"schema_version": VECTOR_STORE_APPEND_SCHEMA_VERSION, "generation": generation, "items": added}
        payload = ("\n" + json.dumps(record, ensure_ascii=False)).encode("utf-8")
        if (tail[1] - tail[0]) + len(payload) > tail[0] * _VECTOR_APPEND_MAX_TAIL_PERCENT // 100:
            return None
        return payload

    # LLM: 调用方须持 self._lock 与 locked_json_path；C 模式只把准确的追加末写交给原门。写成功后才写时复制发布条目、
    #   追加位置和新指纹，本实例不重读文件；失败时实例状态原样不动。
    # 函数用途: 把新向量作为一条追加记录落盘并更新实例缓存。
    def _append_items(self, added: dict[str, dict[str, object]], payload: bytes) -> None:
        base_size, end = self._tail
        expected = self._stamp

        # 函数用途: 执行一次准确的追加末写。
        def append() -> tuple[int, int, int]:
            return _append_record_unlocked(self._path, expected, end, payload)

        stamp = _coordinator_vector_write(append) if self.coordinator_io else append()
        self._items = {**self._items, **added}
        self._tail, self._stamp = (base_size, end + len(payload)), stamp

    # LLM: 重建专用：用完整新快照整体替换旧库，身份取本实例、维度取这批向量，不看旧快照——这正是管理员修复失配的
    #   入口。与增量写共用同一把跨进程锁；维度不一致抛 VECTOR_DIMENSION_MISMATCH，旧库原样保留。
    # 函数用途: 原子替换整个向量库，返回写入条数。
    # LLM: C只接准确FD出生与原末writer，完整维度/内容准备Root外，原重建身份算法不变。
    # 函数用途: 替换原向量快照，不另造恢复库或跨请求来源。
    def replace_all(self, rows: list[tuple[str, list[float], str, dict[str, object]]]) -> int:
        rows = [row for row in rows if row[0]]
        dim = _rows_dim(rows) if self._identity is not None else None
        items = {id: _item(vector, text, metadata) for id, vector, text, metadata in rows}
        with self._lock, self._locked_snapshot():
            self._write_snapshot(items, self._identity, dim)
        return len(items)

    # LLM: 删除只去掉精确 id，保留快照头的身份与维度（删向量不引入不可信数据），也不做身份裁决——硬删除的隐私清理
    #   不能被身份失配挡住。坏文件必须报告清理未确认，不能当成不存在；精确 id 不存在才返回 False 且不写盘。
    # 函数用途: 删除一条向量并原子落盘。
    # LLM: C保持原坏库拒绝/不存在不写；锁等待、刷新和裁决Root外，失败不发布删除成功。
    # 函数用途: 删除精确向量并确认原快照落盘，保原身份。
    def remove(self, id: str) -> bool:
        return id in self.remove_many([id])

    # LLM: 精确ID集合共用原删除裁决；坏库不能当空库，身份失配不阻挡隐私清理，实际有变化才一次落盘。
    # 函数用途: 批量删除派生向量并返回真实删除ID，避免每条全快照重写。
    def remove_many(self, ids: Iterable[str]) -> set[str]:
        wanted = set(ids)
        with self._lock, self._locked_snapshot():
            self._refresh()
            if not self._readable:
                raise VectorIdentityError("VECTOR_STORE_UNREADABLE")
            removed = wanted.intersection(self._items)
            if not removed:
                return set()
            items = {key: value for key, value in self._items.items() if key not in removed}
            identity = _normalized_identity((self._header or {}).get("identity"))
            self._write_snapshot(items, identity, self._stored_dim())
            return removed

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

    # LLM: 召回复用入口（S3，用户 10-02 拍板）：带身份且身份可用（档案、协议、模型、端点摘要都对上）才返回，否则空字典；
    #   不管理身份的通用模式（identity=None）无从核对是不是同一个向量空间，一律不复用。
    #   只交出向量和“嵌入时用的文本”，能否复用由调用方按文本完全一致裁决，维度再由检索器按本轮 query 长度核一次。只读。
    # LLM: 内部元组不外泄可变引用；对外仍给list以保持HybridRetriever及既有调用合同。
    # 函数用途: 返回已验证向量的列表副本及对应文本。
    def stored_vectors(self, ids: Iterable[str]) -> dict[str, tuple[list[float], str]]:
        wanted = set(ids)
        with self._lock:
            self._refresh()
            if self._identity is None or self._identity_reason():
                return {}
            snapshot = {key: self._items[key] for key in wanted if key in self._items}
        return {key: (list(item["vector"]), str(item.get("text", ""))) for key, item in snapshot.items()
                if isinstance(item, dict) and isinstance(item.get("vector"), tuple)}

    # LLM: 先刷新再列 id；保留期与硬删除核对靠它确认向量是否真被清掉，只读。
    # 函数用途: 返回当前快照的全部向量 id。
    def ids(self) -> list[str]:
        with self._lock:
            self._refresh()
            return list(self._items.keys())


# LLM: 只消费当前global scope，缺源沿原GlobalWriteCommitUnavailable，不缓存来源或把向量算法放入末门。
# 函数用途: 为本地向量快照的准确出生和raw末写调用原同步写门。
def _coordinator_vector_write(operation):
    return execute_global_write(operation, require_scope=True)
