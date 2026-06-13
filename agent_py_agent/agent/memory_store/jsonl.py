
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
    from ..local_storage import LocalSearchResult, LocalStore


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
    # 开放结构化扩展位(P5-2):上层语义字段挂这里,例如教训记忆的
    # trigger_conditions(结构化触发条件,决策端按字段匹配提权,不解析正文)。
    # 旧 JSONL 行没有此键,读取时按空 dict 兼容;空 dict 不写入 JSON 行(省字节)。
    attributes: dict | None = None

    def to_json(self) -> str:
        """把当前记忆转成一行 UTF-8 JSON 字符串。

        新手说明:
        JSONL 文件是一行一个 JSON。这个方法负责把 MemoryRecord 变成可以直接追加到文件的一行文本。

        这个方法没有输入参数，只读取当前 record 字段。

        返回说明:
        返回 JSON 字符串，不包含换行符。

        副作用说明:
        如果 created_at 还是 0，会把它改成当前时间；这个方法本身不写文件。"""

        if not self.created_at:
            self.created_at = time.time()
        return json.dumps(_record_payload(self), ensure_ascii=False)


class JsonlMemory(JsonlMemoryIndexMixin):
    """JSONL-backed memory store with optional LocalStore indexing.

    新手说明:
    JSONL 是事实流水，LocalStore 是检索索引。
    即使 SQLite 或 FTS5 出问题，JSONL 记忆也不应该因此写不进去。

    字段说明:
    path: JSONL 记忆文件路径。
    local_store: 可选 LocalStore；有它时 add/index_all/search 可以同步索引和优先搜索索引。"""

    def __init__(
        self,
        path: str | Path,
        local_store: LocalStore | None = None,
        daily_mirror_dir: str | Path | None = None,
    ):
        """初始化 JSONL 记忆文件位置，并确保父目录存在。

        新手说明:
        创建 JsonlMemory 时只准备文件路径和可选索引对象，不会读取全部记忆。

        path: JSONL 文件路径。
        local_store: 可选 LocalStore，用于索引和搜索；为空时仍可正常写 JSONL。

        副作用说明:
        会创建 path 的父目录；不会创建 LocalStore，也不会调用模型。"""
        self.path = Path(path)
        self.local_store = local_store
        self.daily_mirror_dirs = _daily_mirror_dirs(daily_mirror_dir)
        self.daily_mirror_dir = self.daily_mirror_dirs[0] if self.daily_mirror_dirs else None
        self.path.parent.mkdir(parents=True, exist_ok=True)

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
        需要带结构化扩展字段（attributes，如教训的 trigger_conditions）时，
        自行构造 MemoryRecord 走 add_record。

        role: 记忆来源角色，例如 user 或 assistant。
        content: 记忆正文。
        kind: 记忆类型，默认 dialogue。
        tags: 可选标签列表；None 会变成空列表。

        返回说明:
        返回刚写入的 MemoryRecord。

        副作用说明:
        会追加写入 JSONL 文件；如果 local_store 存在，会尝试 upsert 一条索引记录。"""

        return self.add_record(
            MemoryRecord(
                role=role,
                content=content,
                kind=kind,
                tags=tags or [],
                created_at=time.time(),
            )
        )

    # LLM: 记忆写入的底层唯一落盘口(add 是它的便捷封装)。接受完整 MemoryRecord,
    #   attributes 等扩展字段(P5-2 trigger_conditions)由调用方在 record 上携带。
    #   副作用:JSONL 追加 + daily mirror + LocalStore 索引(索引失败不打断)。
    # 函数用途: 想写带结构化扩展字段的记忆时,构造好 MemoryRecord 从这里进。
    def add_record(self, record: MemoryRecord) -> MemoryRecord:
        if not record.created_at:
            record.created_at = time.time()
        append_jsonl(self.path, _record_payload(record))
        self._append_daily_mirror(record)
        self._try_index_record(record)
        return record

    def _append_daily_mirror(self, record: MemoryRecord) -> None:
        if not self.daily_mirror_dirs:
            return
        for daily_dir in self.daily_mirror_dirs:
            path = daily_dir / f"{date.fromtimestamp(record.created_at).isoformat()}.jsonl"
            append_jsonl(path, _record_payload(record))

    def all(self) -> list[MemoryRecord]:
        """从 JSONL 文件读取全部记忆记录。

        新手说明:
        这个方法适合小规模本地记忆。文件不存在时返回空列表；空行会被跳过。

        这个方法没有输入参数。

        返回说明:
        返回当前 owner 长期记忆文件的 MemoryRecord 列表；home daily mirror 由 search() 补充检索或 CLI 直接读取。

        异常说明:
        如果某一行不是合法 JSON，目前会由 json.loads 抛错；后续如需容错可加 read audit。"""

        return _dedupe_memory_records(
            [
                *self._read_memory_file(self.path),
            ]
        )

    def search(self, query: str, top_k: int = 5) -> list[MemoryRecord]:
        """搜索记忆，优先使用 LocalStore 索引，再读取 JSONL 正式源。

        新手说明:
        有 LocalStore 时优先走 SQLite/FTS5。
        没有索引、索引为空或索引临时失败时，继续从 owner JSONL / daily mirror 检索。

        query: 搜索文本。
        top_k: 最多返回多少条结果。

        返回说明:
        返回 MemoryRecord 列表。LocalStore 是检索索引，JSONL 是正式记忆源。"""

        records, _load_errors = self.search_report(query, top_k=top_k)
        return records

    def search_report(self, query: str, top_k: int = 5) -> tuple[list[MemoryRecord], list[dict]]:
        """搜索记忆并保留可恢复的索引读取错误。

        LocalStore 只是检索索引，不是记忆事实源。索引坏了时继续从当前 owner JSONL / daily
        mirror 检索，但把错误报告给调用方，避免上层误判为“没有记忆”。
        """

        indexed, load_errors = self._search_local_store_report(query, top_k)
        if len(indexed) >= top_k:
            return indexed[:top_k], load_errors
        source_records = _merge_search_result_groups(
            (
                self._search_jsonl(query, top_k),
                self._search_daily_mirror(query, top_k),
            ),
            top_k=top_k,
        )
        return _merge_search_result_groups((indexed, source_records), top_k=top_k), load_errors

    def index_all(self) -> int:
        """把现有 JSONL 记忆补写到 LocalStore 索引。

        新手说明:
        这个命令适合第一次升级到 SQLite/FTS5 后运行一次。
        如果没有 local_store，就什么都不做并返回 0。

        这个方法没有输入参数。

        返回说明:
        返回成功尝试索引的 owner 长期记忆记录数量；home daily mirror 暂不混入本地索引重建。

        副作用说明:
        会调用 LocalStore.upsert_record；不会改写 JSONL。"""

        if not self.local_store:
            return 0
        count = 0
        for record in self._read_memory_file(self.path):
            self._index_record(record)
            count += 1
        return count

    def _daily_mirror_files(self) -> list[Path]:
        files: list[Path] = []
        for daily_dir in self.daily_mirror_dirs:
            if daily_dir.exists():
                files.extend(path for path in daily_dir.glob("*.jsonl") if path.is_file())
        return sorted(set(files))

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

    def _search_daily_mirror(self, query: str, top_k: int) -> list[MemoryRecord]:
        records: list[MemoryRecord] = []
        for path in self._daily_mirror_files():
            records.extend(self._read_memory_file(path))
        return _search_memory_records(records, query, top_k)

# 函数用途: MemoryRecord → JSONL 行字典;attributes 为空时不写该键,旧行格式不变。
def _record_payload(record: MemoryRecord) -> dict:
    payload = asdict(record)
    if not payload.get("attributes"):
        payload.pop("attributes", None)
    return payload


def _memory_record_key(record: MemoryRecord) -> tuple[str, str, str, float]:
    return (record.role, record.kind, record.content, float(record.created_at or 0.0))


def _search_memory_records(records: list[MemoryRecord], query: str, top_k: int) -> list[MemoryRecord]:
    query_terms = {term.lower() for term in query.split() if term.strip()}
    scored: list[tuple[int, float, MemoryRecord]] = []
    for record in records:
        score = _memory_search_score(record, query, query_terms)
        if score > 0 or not query_terms:
            scored.append((score, record.created_at, record))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [record for _, _, record in scored[:top_k]]


def _memory_search_score(record: MemoryRecord, query: str, query_terms: set[str]) -> int:
    text = record.content.lower()
    score = sum(1 for term in query_terms if term in text)
    if query and query.lower() in text:
        score += 3
    return score


def _merge_search_result_groups(groups: tuple[list[MemoryRecord], ...], *, top_k: int) -> list[MemoryRecord]:
    records: list[MemoryRecord] = []
    seen: set[tuple[str, str, str, float]] = set()
    for group in groups:
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
