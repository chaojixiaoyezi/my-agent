# LLM: Memory store module; keep JSONL storage and indexing formats stable.
# 模块用途: 提供底层记忆 JSONL 存储、索引和读取能力。

from __future__ import annotations

"""本模块提供 JSONL 记忆事实流水，并可选同步索引到 LocalStore 方便搜索。

新手说明:
记忆现在采用'双轨落盘'：
- JSONL 仍然是原始记忆流水，每行一条记录，方便直接打开查看。
- 可选 LocalStore 会把同一条记忆索引到 SQLite + FTS5，方便更快检索。

这样做的好处是：简单文件还在，后续要做本地检索、同步、迁移或审计时，
也已经有结构化账本可以接。
"""

import json
import time
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from ..io import append_jsonl
from ._jsonl_indexing import JsonlMemoryIndexMixin

if TYPE_CHECKING:
    from ..local_store import LocalSearchResult, LocalStore


# LLM: memory store 以 JSONL 记录和本地索引作为事实来源；修改 MemoryRecord 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 MemoryRecord 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass
class MemoryRecord:
    """表示一条已经准备写入 JSONL 的记忆事实。

    新手说明:
    MemoryRecord 是最小记忆单位。它记录'谁说的、说了什么、属于哪类、有哪些标签、什么时候创建'。

    字段说明:
    role: 记忆来源角色，例如 user、assistant、tool、system。
    content: 记忆正文。
    kind: 记忆类型，默认 dialogue；也可以是 rule、summary、note 等上层定义。
    tags: 标签列表，用于粗分类和后续检索。
    created_at: Unix 时间戳；为 0 时写 JSON 前会自动补当前时间。"""

    role: str
    content: str
    kind: str = "dialogue"
    tags: list[str] | None = None
    created_at: float = 0.0

    # LLM: memory store 以 JSONL 记录和本地索引作为事实来源；修改 to_json 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 把 to json 对应对象转换成字典、JSON 或文本形态，供持久化和输出层复用。
    def to_json(self) -> str:
        """把当前记忆转成一行 UTF-8 JSON 字符串。

        新手说明:
        JSONL 文件是一行一个 JSON。这个方法负责把 MemoryRecord 变成可以直接追加到文件的一行文本。

        参数说明:
        这个方法没有输入参数，只读取当前 record 字段。

        返回说明:
        返回 JSON 字符串，不包含换行符。

        副作用说明:
        如果 created_at 还是 0，会把它改成当前时间；这个方法本身不写文件。"""

        if not self.created_at:
            self.created_at = time.time()
        return json.dumps(asdict(self), ensure_ascii=False)


# LLM: memory store 以 JSONL 记录和本地索引作为事实来源；修改 JsonlMemory 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 封装 JsonlMemory 的状态和协作方法，作为当前模块对外复用的领域对象。
class JsonlMemory(JsonlMemoryIndexMixin):
    """JSONL-backed memory store with optional LocalStore indexing and search fallback.

    新手说明:
    JSONL 是事实流水，LocalStore 是检索索引。
    即使 SQLite 或 FTS5 出问题，JSONL 记忆也不应该因此写不进去。

    字段说明:
    path: JSONL 记忆文件路径。
    local_store: 可选 LocalStore；有它时 add/index_all/search 可以同步索引和优先搜索索引。"""

    # LLM: memory store 以 JSONL 记录和本地索引作为事实来源；修改 __init__ 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 初始化实例依赖、路径或缓存状态，为同一对象的后续方法提供共享上下文。
    def __init__(
        self,
        path: str | Path,
        local_store: LocalStore | None = None,
        daily_mirror_dir: str | Path | None = None,
        fallback_read_paths: tuple[str | Path, ...] | list[str | Path] | None = None,
    ):
        """初始化 JSONL 记忆文件位置，并确保父目录存在。

        新手说明:
        创建 JsonlMemory 时只准备文件路径和可选索引对象，不会读取全部记忆。

        参数说明:
        path: JSONL 文件路径。
        local_store: 可选 LocalStore，用于索引和搜索；为空时仍可正常写 JSONL。

        副作用说明:
        会创建 path 的父目录；不会创建 LocalStore，也不会调用模型。"""
        self.path = Path(path)
        self.local_store = local_store
        self.daily_mirror_dirs = _daily_mirror_dirs(daily_mirror_dir)
        self.daily_mirror_dir = self.daily_mirror_dirs[0] if self.daily_mirror_dirs else None
        self.fallback_read_paths = _fallback_read_paths(fallback_read_paths, primary=self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # LLM: memory store 以 JSONL 记录和本地索引作为事实来源；修改 add 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 add 在当前模块中的核心转换或协调步骤，衔接 memory store 以 JSONL 记录和本地索引作为事实来源。
    def add(
        self,
        role: str,
        content: str,
        *,
        kind: str = "dialogue",
        tags: list[str] | None = None,
    ) -> MemoryRecord:
        """追加一条记忆到 JSONL，并尽力同步索引到 LocalStore。

        新手说明:
        这是写入记忆的主入口。先构造 MemoryRecord，再写入 JSONL，最后尝试写索引。
        索引失败不会让记忆写入失败，因为 JSONL 才是主事实流水。

        参数说明:
        role: 记忆来源角色，例如 user 或 assistant。
        content: 记忆正文。
        kind: 记忆类型，默认 dialogue。
        tags: 可选标签列表；None 会变成空列表。

        返回说明:
        返回刚写入的 MemoryRecord。

        副作用说明:
        会追加写入 JSONL 文件；如果 local_store 存在，会尝试 upsert 一条索引记录。"""

        record = MemoryRecord(
            role=role,
            content=content,
            kind=kind,
            tags=tags or [],
            created_at=time.time(),
        )
        append_jsonl(self.path, asdict(record))
        self._append_daily_mirror(record)
        self._try_index_record(record)
        return record

    # LLM: daily mirror keeps the future ~/my-agent/memory/daily ledger populated while legacy memory_path stays readable.
    # 函数用途: 把同一条记忆追加到按天分片的 home memory JSONL；未配置时保持旧行为。
    def _append_daily_mirror(self, record: MemoryRecord) -> None:
        if not self.daily_mirror_dirs:
            return
        for daily_dir in self.daily_mirror_dirs:
            path = daily_dir / f"{date.fromtimestamp(record.created_at).isoformat()}.jsonl"
            append_jsonl(path, asdict(record))

    # LLM: memory store 以 JSONL 记录和本地索引作为事实来源；修改 all 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 读取 all 需要的文件、记录或配置，并整理成调用方可直接使用的结果。
    def all(self) -> list[MemoryRecord]:
        """从 JSONL 文件读取全部记忆记录。

        新手说明:
        这个方法适合小规模本地记忆。文件不存在时返回空列表；空行会被跳过。

        参数说明:
        这个方法没有输入参数。

        返回说明:
        返回旧 memory_path 的 MemoryRecord 列表；home daily mirror 由 search() 补充检索或 CLI 直接读取。

        异常说明:
        如果某一行不是合法 JSON，目前会由 json.loads 抛错；后续如需容错可加 read audit。"""

        return _dedupe_memory_records(
            [
                *self._read_memory_file(self.path),
                *self._read_fallback_memory_files(),
            ]
        )

    # LLM: memory store 以 JSONL 记录和本地索引作为事实来源；修改 search 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 search 在当前模块中的核心转换或协调步骤，衔接 memory store 以 JSONL 记录和本地索引作为事实来源。
    def search(self, query: str, top_k: int = 5) -> list[MemoryRecord]:
        """搜索记忆，优先使用 LocalStore，失败或无命中时退回 JSONL 关键词搜索。

        新手说明:
        有 LocalStore 时优先走 SQLite/FTS5。
        没有索引、索引为空或索引临时失败时，退回 JSONL 关键词检索。

        参数说明:
        query: 搜索文本。
        top_k: 最多返回多少条结果。

        返回说明:
        返回 MemoryRecord 列表。LocalStore 有命中时返回索引结果，否则返回 JSONL fallback 结果。"""

        indexed = self._search_local_store(query, top_k)
        if len(indexed) >= top_k:
            return indexed[:top_k]
        fallback = _merge_search_results(
            self._search_jsonl(query, top_k),
            self._search_fallback_memory(query, top_k),
            self._search_daily_mirror(query, top_k),
            top_k,
        )
        return _merge_search_results(indexed, fallback, top_k)

    # LLM: memory store 以 JSONL 记录和本地索引作为事实来源；修改 index_all 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 写入或登记 index all 相关记录，集中处理目标路径、格式化和状态更新。
    def index_all(self) -> int:
        """把现有 JSONL 记忆补写到 LocalStore 索引。

        新手说明:
        这个命令适合第一次升级到 SQLite/FTS5 后运行一次。
        如果没有 local_store，就什么都不做并返回 0。

        参数说明:
        这个方法没有输入参数。

        返回说明:
        返回成功尝试索引的旧 memory_path 记录数量；home daily mirror 暂不混入本地索引重建。

        副作用说明:
        会调用 LocalStore.upsert_record；不会改写 JSONL。"""

        if not self.local_store:
            return 0
        count = 0
        for record in self._read_memory_file(self.path):
            self._index_record(record)
            count += 1
        return count

    # LLM: _daily_mirror_files exposes daily mirror reads without changing the append path.
    # 函数用途: 返回按天镜像 JSONL 文件列表；未配置或目录不存在时返回空列表。
    def _daily_mirror_files(self) -> list[Path]:
        files: list[Path] = []
        for daily_dir in self.daily_mirror_dirs:
            if daily_dir.exists():
                files.extend(path for path in daily_dir.glob("*.jsonl") if path.is_file())
        return sorted(set(files))

    # LLM: _read_memory_file is shared by legacy memory_path and daily mirror reads.
    # 函数用途: 读取一个 JSONL 文件并转换成 MemoryRecord；文件不存在时返回空列表。
    def _read_memory_file(self, path: Path) -> list[MemoryRecord]:
        if not path.exists():
            return []
        records: list[MemoryRecord] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            records.append(MemoryRecord(**obj))
        return records

    # LLM: _search_daily_mirror lets recall see home daily records without changing legacy all()/index_all semantics.
    # 函数用途: 在 home daily mirror 中做轻量关键词检索，作为旧 memory_path 搜索补充。
    def _search_daily_mirror(self, query: str, top_k: int) -> list[MemoryRecord]:
        records: list[MemoryRecord] = []
        for path in self._daily_mirror_files():
            records.extend(self._read_memory_file(path))
        return _search_memory_records(records, query, top_k)

    # LLM: fallback_read_paths lets owner-home memory become primary without losing old legacy memory_path records.
    # 函数用途: 读取旧 memory_path 等兼容记忆文件，只作为 owner memory 的补充读源，不再作为新写入目标。
    def _read_fallback_memory_files(self) -> list[MemoryRecord]:
        records: list[MemoryRecord] = []
        for path in self.fallback_read_paths:
            records.extend(self._read_memory_file(path))
        return records

    # LLM: fallback search keeps migrated owner memory compatible with older global JSONL memories.
    # 函数用途: 在兼容读源中做轻量关键词搜索，保证迁移后旧记忆仍可召回。
    def _search_fallback_memory(self, query: str, top_k: int) -> list[MemoryRecord]:
        return _search_memory_records(self._read_fallback_memory_files(), query, top_k)


# LLM: _memory_record_key dedupes legacy memory and daily mirror copies without relying on line numbers.
# 函数用途: 生成记忆记录去重 key，避免同一条写入同时从两个文件返回。
def _memory_record_key(record: MemoryRecord) -> tuple[str, str, str, float]:
    return (record.role, record.kind, record.content, float(record.created_at or 0.0))


# LLM: _search_memory_records mirrors JSONL fallback scoring for non-legacy record lists.
# 函数用途: 对已加载的 MemoryRecord 列表做关键词评分，供 daily mirror 搜索复用。
def _search_memory_records(records: list[MemoryRecord], query: str, top_k: int) -> list[MemoryRecord]:
    query_terms = {term.lower() for term in query.split() if term.strip()}
    scored: list[tuple[int, float, MemoryRecord]] = []
    for record in records:
        score = _memory_search_score(record, query, query_terms)
        if score > 0 or not query_terms:
            scored.append((score, record.created_at, record))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [record for _, _, record in scored[:top_k]]


# LLM: _memory_search_score keeps daily mirror search behavior aligned with JsonlMemoryIndexMixin.
# 函数用途: 计算单条记忆和查询文本的简单关键词命中分数。
def _memory_search_score(record: MemoryRecord, query: str, query_terms: set[str]) -> int:
    text = record.content.lower()
    score = sum(1 for term in query_terms if term in text)
    if query and query.lower() in text:
        score += 3
    return score


# LLM: _merge_search_results preserves LocalStore priority while filling gaps from JSONL/daily facts.
# 函数用途: 合并索引搜索和 JSONL fallback 结果，并按 MemoryRecord 语义去重。
def _merge_search_results(*groups: list[MemoryRecord] | int) -> list[MemoryRecord]:
    top_k = int(groups[-1])
    record_groups = [group for group in groups[:-1] if isinstance(group, list)]
    records: list[MemoryRecord] = []
    seen: set[tuple[str, str, str, float]] = set()
    for group in record_groups:
        _append_unique_records(records, seen, group, top_k=top_k)
        if len(records) >= top_k:
            return records
    return records


def _append_unique_records(
    records: list[MemoryRecord],
    seen: set[tuple[str, str, str, float]],
    group: list[MemoryRecord],
    *,
    top_k: int,
) -> None:
    for record in group:
        key = _memory_record_key(record)
        if key in seen:
            continue
        seen.add(key)
        records.append(record)
        if len(records) >= top_k:
            return


def _dedupe_memory_records(records: list[MemoryRecord]) -> list[MemoryRecord]:
    deduped: list[MemoryRecord] = []
    seen: set[tuple[str, str, str, float]] = set()
    for record in records:
        key = _memory_record_key(record)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(record)
    return deduped


def _daily_mirror_dirs(value: object) -> tuple[Path, ...]:
    if value is None:
        return ()
    raw_items = value if isinstance(value, (list, tuple, set)) else (value,)
    dirs: list[Path] = []
    for item in raw_items:
        path = Path(item)
        if path not in dirs:
            dirs.append(path)
    return tuple(dirs)


def _fallback_read_paths(value: object, *, primary: Path) -> tuple[Path, ...]:
    if not value:
        return ()
    raw_items = value if isinstance(value, (list, tuple, set)) else (value,)
    paths: list[Path] = []
    for item in raw_items:
        path = Path(item)
        if path == primary or path in paths:
            continue
        paths.append(path)
    return tuple(paths)
