# LLM: C只接原缓存文件IO策略；keep/合并/编码/锁等待门外，原实例items与错误诊断不作耐久成功凭据。
# LLM: 缓存是可重建派生数据；读取可降级但清理和写回须完整读成功，跨实例撤回按文件指纹刷新。
# 模块用途: 缓存正文哈希向量，并在删除失败时保留可追踪的清理错误。
"""正文哈希向量缓存(派生索引,独立文件,绝不写入权威向量库)。

## 为什么要单独一个文件

语义召回的向量有两个完全不同的用途,生命周期也不一样:

1. **权威向量**(``memory_vectors.json``):按 entry_id 存的历史向量,带正文副本,
   供全库 ``VectorStore.search`` 语义召回使用。它由写入路径维护,删除事实时必须同步清掉。
2. **正文哈希缓存**(本文件):纯粹是"这段正文已经嵌过,别再花一次钱"的备忘录。
   键是正文哈希,值是向量,不含明文。

早先把 2 塞进 1 的文件里,造成两个真实缺陷(复审探针 P4/P6):
- 缓存项会进入 ``VectorStore.search`` 的全库线性扫描,把真实条目的 top_k 名额挤掉;
- 只做检索的进程持有启动时的旧快照,一次整文件写回就把**已删除事实的明文**重新写进
  ``memory_vectors.json``,违反 ``_jsonl_indexing`` 的清明文约定(隐私问题)。

拆成独立文件后,检索路径永远不重写 ``memory_vectors.json``。

## 键为什么是指纹而不是明文标识

键 = ``sha256(实现类名 + api_base + model)[:16]`` 指纹 + 正文哈希。用指纹而不是如实拼
``api_base``/``model``,是为了不把端点地址和模型名持久化到派生数据里;换端点或换模型时指纹自然改变。
正文哈希用完整 sha256 十六进制串,不进明文。

## 为什么没有配置开关

``memory_semantic_recall`` 已是语义召回总开关,缓存只是它的实现细节(去掉缓存检索结果逐字相同,
只是更贵)。参数中心正在减量,故不开新参数。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..common.global_write_commit import GlobalWriteCommitUnavailable, execute_global_write
from ..common.global_write_guard import GlobalWriteInWorkerError
from ..common.owner_write_errors import (
    LocalControlCoordinatorUnavailable,
    OwnerManagementIngressUnavailable,
)


@runtime_checkable
class EmbeddingIdentity(Protocol):
    """能提供缓存键指纹所需的嵌入身份(实现类/端点/模型)。"""

    def __call__(self) -> str: ...


# LLM: 缓存只是派生索引，读失败/损坏一律等价于"没有缓存"（退回现嵌），检索结果不因此改变。
# 类用途: 按正文哈希存取正文向量，与权威 memory_vectors.json 完全分离。
# LLM: C仅存策略，原items/read_stamp/last_error是同一派生缓存；native拒绝不能当耐久成功，后继get仍按原盘代刷新。
# 类用途: 复用原正文向量缓存，不用重建新实例规避失败。
class TextVectorCache:
    """正文哈希 → 向量 的独立缓存文件(JSON,dict 结构)。

    用法::

        cache = TextVectorCache(home / "memory_text_vectors.json")
        cache.put({key: vector})       # 只写本文件
        cache.get([key])               # 命中返回 {key: vector}
        cache.remove([key])            # 清理失效键
    """

    # LLM: 构造保留原宽松读取；get的快照指纹必须独立复核，写入/清理使本实例指纹失效。
    # 函数用途: 建立派生缓存，不把启动快照永久当成磁盘现状。
    # LLM: 仅增默认False策略，原宽松读取与指纹保持；不保存source或增加后台flush。
    # 函数用途: 绑定原缓存对象与本地IO策略，读取不授权后续写。
    def __init__(self, path: str | Path, *, coordinator_io: bool = False) -> None:
        self.coordinator_io = coordinator_io
        self._path = Path(path).expanduser()
        self._lock = threading.RLock()
        self._read_stamp = None
        # LLM: 写失败不能静默吞掉——调用方据此把真实错误反映到健康状态（否则缓存悄悄不生效）。
        # 每次写操作开头清空，写失败时留下异常。
        self.last_write_error: Exception | None = None
        # LLM: **构造宽松、写入严格**。构造时缓存文件读不了（权限/EIO/EMFILE）只意味着"这轮没有缓存"，
        #   绝不能把读错误抛给调用方——`add` 走过权威提交后抛异常会被当成"写入失败"而重试，
        #   而 `search_scoped` 抛异常会让整次检索失败，两者都违背"缓存是派生数据"的合同。
        #   写路径（_flush）反过来必须严格：读不出磁盘内容就不写，免得把整份缓存清空。
        self.last_read_error: Exception | None = None
        try:
            self._items: dict[str, list[float]] = self._load()
        except OSError as exc:
            self.last_read_error = exc
            self._items = {}

    # LLM: 读路径宽松，坏JSON/编码/IO或非对象根都记录故障；写路径必须经_load_for_write确认完整读取。
    #   逐键跳过坏值，保留好向量但记不完整错误，阻止写回丢值；完整读成功才清除旧错。
    # 函数用途: 读取当前派生缓存并保留诊断，缺失或读错均向召回返回空集合。
    def _load(self) -> dict[str, list[float]]:
        self.last_read_error = None
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            self.last_read_error = exc
            return {}
        if not isinstance(raw, dict):
            self.last_read_error = ValueError("cache root is not an object")
            return {}
        items: dict[str, list[float]] = {}
        for key, value in raw.items():
            if not isinstance(key, str) or not isinstance(value, list):
                self.last_read_error = ValueError("cache entry is not a vector")
                continue
            try:
                vector = [float(x) for x in value]
                if not all(math.isfinite(x) for x in vector):
                    raise ValueError("cache vector is not finite")
                items[key] = vector
            except (TypeError, ValueError, OverflowError) as exc:
                self.last_read_error = exc
                continue  # 好键仍可召回，但不完整快照禁止用于整文件写回。
        return items

    # LLM: 调用方持缓存文件锁；读路径可降级，任何软读错误在副作用之前升级为写失败。
    # 函数用途: 为put/remove/retain取得可安全修改的完整磁盘快照。
    def _load_for_write(self) -> dict[str, list[float]]:
        rows = self._load()
        if self.last_read_error is not None:
            raise self.last_read_error
        return rows

    # LLM: 跨进程写锁复用既有 locked_json_path sidecar（与追加方共用同一把锁）。
    #   **只把本次调用的增量写回**：put 只写本次 put 的键，remove/retain 只删本次删的键。
    #   整份内存快照叠回磁盘会把其他进程已经删掉的键重新写出来（X1 探针），必须按增量合并。
    #   `keep` 判据在**拿到跨进程文件锁之后**才算：ik 在实例锁内算完、落盘之前，别的进程/实例
    #   仍可能删掉事实（P5c）。放进文件锁内才真正把"复核 → 写入"这一段关上。
    # 函数用途: 在跨进程锁内按增量写入/删除，并在锁内复核待写键仍然有效。
    # LLM: C保原keep/增量合并/no-change与错误诊断；准确FD/目录出生和raw写进门，真实native先同对象穿出不返回成功。
    # 函数用途: 在原锁内合并本次缓存增量，拒绝不冒充耐久写入。
    def _flush(
        self,
        *,
        added: dict[str, list[float]] | None = None,
        removed: frozenset[str] = frozenset(),
        keep: Callable[[], set[str]] | None = None,
    ) -> None:
        added = added or {}
        self._read_stamp = None
        self.last_write_error = None
        try:
            from ..common.json_io import locked_json_path

            with locked_json_path(self._path, **({} if not self.coordinator_io else {"birth_commit": _coordinator_cache_write})):
                if keep is not None:
                    valid = keep()
                    added = {key: vector for key, vector in added.items() if key in valid}
                on_disk = self._load_for_write()
                before = dict(on_disk)
                for key in removed:
                    on_disk.pop(key, None)
                on_disk.update(added)
                # LLM: **内容没变就跳过整文件重写**。remove 的常态是"这些键盘上本来就没有"
                #   （大量删除请求命中不到），此时整文件原子替换纯属浪费：32 MB 缓存删一次约 1.6 秒。
                #   判据必须在文件锁内、用刚读出来的盘上内容比，不能凭本实例内存判断——
                #   另一个实例写的键本实例内存里没有、盘上有（P5d），只看内存会漏写。
                #   跳过写时**照常刷新 _items**，本实例内存视图与盘上保持一致。
                if on_disk == before:
                    self._items = on_disk
                    return
                if self.coordinator_io:
                    _coordinator_cache_write(lambda: self._path.parent.mkdir(parents=True, exist_ok=True))
                else:
                    self._path.parent.mkdir(parents=True, exist_ok=True)
                if not _write_atomic_unlocked(self._path, on_disk, **({} if not self.coordinator_io else {"coordinator_io": True})):
                    # 拿到了锁但 rename 失败（如磁盘满）：必须留下错误（W1），否则健康状态看不出来。
                    self.last_write_error = OSError("cache write failed")
                    self._items = on_disk
                    return
                self._items = on_disk
        except Exception as exc:
            # 拿不到跨进程锁时**不写盘**：此时磁盘内容不可信，写回会复活别的实例已删的键（X1b）。
            # 只记错误，内存视图仍按本次增量更新，供本轮后续读取使用。
            self.last_write_error = exc
            if self.coordinator_io and isinstance(exc, (GlobalWriteCommitUnavailable, GlobalWriteInWorkerError,
                        LocalControlCoordinatorUnavailable, OwnerManagementIngressUnavailable)):
                raise
            for key in removed:
                self._items.pop(key, None)
            self._items.update(added)

    def __len__(self) -> int:
        return len(self._items)

    # LLM: 在实例锁内用文件指纹发现其他实例的清理；不变时不解码JSON，读错时清掉旧快照并保留诊断。
    # 函数用途: 更新跨实例只读缓存，不把删除前的内存向量继续返回。
    def _refresh_if_changed(self) -> None:
        try:
            try:
                stat = self._path.stat()
                stamp = (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            except FileNotFoundError:
                stamp = ()
            if stamp != self._read_stamp or self.last_read_error is not None:
                self.last_read_error = None
                self._items = self._load()
                # 用读取之前的指纹：期间若发生原子替换，下次get仍能发现它。
                self._read_stamp = stamp if self.last_read_error is None else None
        except (OSError, UnicodeError) as exc:
            self.last_read_error = exc
            self._items = {}
            self._read_stamp = None

    # LLM: 先核对跨实例磁盘换代；缺失与坏值退回现嵌，正常get不做全量解码或复制整个缓存。
    # 函数用途: 只返回当前请求键的向量副本，不复用已被其他实例清掉的旧键。
    def get(self, keys: Iterable[str]) -> dict[str, list[float]]:
        with self._lock:
            self._refresh_if_changed()
            return {key: list(self._items[key]) for key in keys if isinstance(self._items.get(key), list)}

    # LLM: 写入只增补本文件的键，不触碰权威向量库；空输入直接返回，避免无意义 IO。
    #   `keep` 是调用方给的"这些键此刻仍然有效"的判据，在**同一把锁内**复核一遍再写：
    #   调用方在锁外做过一次复核，但复核之后、写入之前仍可能发生删除（P5b），
    #   这里把复核与写入收进同一临界区才真正关掉这个窗口。
    # 函数用途: 把本轮现嵌得到的向量按正文哈希写回缓存，写入前在锁内复核键仍然有效。
    def put(self, rows: dict[str, list[float]], *, keep: Callable[[], set[str]] | None = None) -> None:
        if not rows:
            return
        with self._lock:
            added = {
                key: [float(x) for x in vector]
                for key, vector in rows.items()
                if isinstance(key, str) and isinstance(vector, list)
            }
            if keep is not None:
                valid = keep()
                added = {key: vector for key, vector in added.items() if key in valid}
            if not added:
                return
            self._items.update(added)
            # 复核传进 _flush，在跨进程文件锁内再算一次（P5c）：实例锁内这次只是先用结果过滤，
            # 真正决定写不写的是拿锁之后那次。
            self._flush(added=added, keep=keep)

    # LLM: 删除**一律在文件锁内从磁盘上减掉请求的键**——不再要求"本实例内存里有这个键"。
    #   父代理与每个子代理 worker 各有自己的缓存实例，子代理删事实时本实例内存里往往没有那个键
    #   （P5d），只看内存就永远清不掉，孤儿会永久留在缓存文件里。
    #   盘上确实没有这些键时**不真的落盘**：由 _flush 在文件锁内比较改动前后的盘上内容、
    #   没变就跳过原子替换。32 MB 缓存整文件重写删一次约 1.6 秒，而"什么都没删"是常态
    #   （大量删除请求本来就命中不到）。
    # 函数用途: 移除一批失效正文哈希对应的缓存向量。
    def remove(self, keys: Iterable[str]) -> int:
        targets = frozenset(key for key in keys if isinstance(key, str))
        if not targets:
            return 0
        with self._lock:
            dropped = sum(1 for key in targets if key in self._items)
            for key in targets:
                self._items.pop(key, None)
            # 本实例内存里没有 ≠ 盘上没有：另一个实例（父代理 vs worker）可能写过这个键（P5d）。
            # 所以不能只看内存就跳过写盘；由 _flush 在文件锁内按磁盘现状决定是否需要真写。
            self._flush(removed=targets)
        return dropped

    # LLM: 回收孤儿键只针对本缓存文件；保留集合由调用方按当前 active 事实算出。
    # 函数用途: 丢弃不属于任何 active 记录的缓存项（换模型/迁移留下的旧键也一并清掉）。
    def retain(self, keep_keys: Iterable[str]) -> int:
        keep = {key for key in keep_keys if isinstance(key, str)}
        return self.retain_matching(lambda key: key in keep)

    # LLM: 维护进程没有 embedder，算不出完整键（缺指纹），只能按键里的**正文哈希**判定。
    #   键形如 `<指纹>:<正文哈希>`；正文哈希是完整 64 位十六进制。
    # 函数用途: 按"正文哈希仍在 active 事实中"回收孤儿键，供无 embedder 的调用方使用。
    def retain_content_hashes(self, keep_hashes: Iterable[str]) -> int:
        keep = {h for h in keep_hashes if isinstance(h, str)}
        return self.retain_matching(lambda key: _content_hash_of(key) in keep)

    # LLM: 回收在文件锁内按磁盘现状重算；原子写返回False必须记入last_write_error，删除恢复不能误报完整。
    # 函数用途: 丢弃磁盘与内存中不满足keep判据的键，并暴露真实落盘故障。
    # LLM: C回收判据与读/删/编码在Root外；保原items/错误事实，实际来源拒绝不吞成回收0成功。
    # 函数用途: 从原磁盘快照回收失效键，保持跨实例删除。
    def retain_matching(self, keep_key: Callable[[str], bool]) -> int:
        with self._lock:
            try:
                from ..common.json_io import locked_json_path

                self.last_write_error = None
                self._read_stamp = None
                with locked_json_path(self._path, **({} if not self.coordinator_io else {"birth_commit": _coordinator_cache_write})):
                    on_disk = self._load_for_write()
                    dropped = frozenset(key for key in on_disk if not keep_key(key))
                    if not dropped and on_disk == self._items:
                        return 0
                    for key in dropped:
                        on_disk.pop(key, None)
                    self._items = on_disk
                    if dropped:
                        _persist_reclaimed(self._path, on_disk, **({} if not self.coordinator_io else {"coordinator_io": True}))
                    return len(dropped)
            except Exception as exc:
                self.last_write_error = exc
                if self.coordinator_io and isinstance(exc, (GlobalWriteCommitUnavailable, GlobalWriteInWorkerError,
                        LocalControlCoordinatorUnavailable, OwnerManagementIngressUnavailable)):
                    raise
                return 0

    # LLM: 只读列键也复核同一文件指纹，避免另一个实例删除后诊断仍返回旧向量身份。
    # 函数用途: 返回缓存里所有键的快照。
    def keys(self) -> list[str]:
        with self._lock:
            self._refresh_if_changed()
            return list(self._items.keys())


# LLM: 键的正文哈希是冒号后的完整 SHA-256；维护回收只认这一段。
# 函数用途: 从缓存键里取出正文哈希部分（形状不符时返回整串，不会误配）。
def _content_hash_of(key: str) -> str:
    _, sep, digest = key.partition(":")
    return digest if sep else key


# LLM: 正文哈希能反推记忆是否存在、向量可近似还原语义，按记忆本体同一规则落盘：替换前临时文件就是 0600、目录 0700
#   （json_io.write_private_text_file_atomic_unlocked）。调用方负责持锁；失败返回 False，由调用方记 last_write_error。
# 函数用途: 不加锁地原子写出整份正文哈希缓存，成功返回 True。
# LLM: C先编码后固定raw末写；普通OSError仍原False，scope四类不是磁盘IO降级，默认原调用保持。
# 函数用途: 持原锁将完整缓存落盘，不把来源拒绝当普通写失败。
def _write_atomic_unlocked(path: Path, payload: dict[str, list[float]], *, coordinator_io: bool = False) -> bool:
    from ..common.json_io import write_private_text_file_atomic_unlocked

    try:
        if coordinator_io:
            raw = json.dumps(payload, ensure_ascii=False)
            _coordinator_cache_write(lambda: write_private_text_file_atomic_unlocked(
                path, raw, replace_once=True))
        else:
            write_private_text_file_atomic_unlocked(path, json.dumps(payload, ensure_ascii=False))
        return True
    except OSError:
        return False


# LLM: 指纹只取实现类/端点/模型三者哈希前 16 位，既不落端点明文，也能让换模型/换端点自然失效。
# 函数用途: 为给定 embedder 生成参与缓存键的稳定指纹。
def embedder_fingerprint(embedder: object) -> str:
    api_base = str(getattr(embedder, "api_base", "") or "")
    model = str(getattr(embedder, "model", "") or type(embedder).__name__)
    payload = f"{type(embedder).__name__}\x00{api_base}\x00{model}"
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:16]


# LLM: 缓存键 = 指纹 + 正文哈希，不含维度（维度守卫改由向量长度比对在读取点完成）。
# 函数用途: 拼出一条正文对应的缓存键。
def text_cache_key(fingerprint: str, text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
    return f"{fingerprint}:{digest}"


# LLM: 键的正文部分是完整 SHA-256 十六进制（64 位）；回收按它判定"这条键还属于某条 active 事实吗"。
# 函数用途: 算出一条索引文本对应的正文哈希（与 text_cache_key 内用的是同一个）。
def text_content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


# LLM: 检索与写入必须用同一段文本算键；此处把"正文 + keywords_en"的拼接口径收敛成唯一实现。
# 函数用途: 由记忆记录的正文与写路径关键词生成参与嵌入与缓存键的索引文本。
def index_text(content: str, attributes: object) -> str:
    attrs = attributes if isinstance(attributes, dict) else {}
    keywords = attrs.get("keywords_en") or []
    if isinstance(keywords, list):
        suffix = " ".join(str(item) for item in keywords)
    else:
        suffix = str(keywords)
    return content + (" " + suffix if suffix else "")


# LLM: 调用方已持缓存文件锁；复用原子写入并把False转换成错误，不隐藏删除清理欠账。
# 函数用途: 保存回收后的缓存，失败交回retain_matching记录。
# LLM: 原mkdir及raw写各固定末门，普通False仍原OSError；调用方原catch保普通清理诊断。
# 函数用途: 保存原回收缓存，C撤权不换新来源补写。
def _persist_reclaimed(path, on_disk, *, coordinator_io=False) -> None:
    if coordinator_io:
        _coordinator_cache_write(lambda: path.parent.mkdir(parents=True, exist_ok=True))
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    if not _write_atomic_unlocked(path, on_disk, **({} if not coordinator_io else {"coordinator_io": True})):
        raise OSError("cache reclaim write failed")


# LLM: 原缓存仅派生事实；当次scope每次重核后执行准确目录/FD出生或准备完的raw叶，绝不包keep/scan/embedding。
# 函数用途: 把C缓存本地副作用交给真实原同步末门，缺源拒绝而不换实例补写。
def _coordinator_cache_write(operation):
    return execute_global_write(operation, require_scope=True)
