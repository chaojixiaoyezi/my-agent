from __future__ import annotations

"""LLM: 本模块提供 JSONL 记忆事实流水，并可选同步索引到 LocalStore 方便搜索。

新手说明:
记忆现在采用“双轨落盘”：
- JSONL 仍然是原始记忆流水，每行一条记录，方便直接打开查看。
- 可选 LocalStore 会把同一条记忆索引到 SQLite + FTS5，方便更快检索。

这样做的好处是：简单文件还在，后续要做本地检索、同步、迁移或审计时，
也已经有结构化账本可以接。
"""

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..io import append_jsonl

if TYPE_CHECKING:
    from ..local_store import LocalSearchResult, LocalStore


@dataclass
class MemoryRecord:
    """LLM: 表示一条已经准备写入 JSONL 的记忆事实。

    新手说明:
    MemoryRecord 是最小记忆单位。它记录“谁说的、说了什么、属于哪类、有哪些标签、什么时候创建”。

    字段说明:
    role: 记忆来源角色，例如 user、assistant、tool、system。
    content: 记忆正文。
    kind: 记忆类型，默认 dialogue；也可以是 rule、summary、note 等上层定义。
    tags: 标签列表，用于粗分类和后续检索。
    created_at: Unix 时间戳；为 0 时写 JSON 前会自动补当前时间。
    """

    role: str
    content: str
    kind: str = "dialogue"
    tags: list[str] | None = None
    created_at: float = 0.0

    def to_json(self) -> str:
        """LLM: 把当前记忆转成一行 UTF-8 JSON 字符串。

        新手说明:
        JSONL 文件是一行一个 JSON。这个方法负责把 MemoryRecord 变成可以直接追加到文件的一行文本。

        参数说明:
        这个方法没有输入参数，只读取当前 record 字段。

        返回说明:
        返回 JSON 字符串，不包含换行符。

        副作用说明:
        如果 created_at 还是 0，会把它改成当前时间；这个方法本身不写文件。
        """

        if not self.created_at:
            self.created_at = time.time()
        return json.dumps(asdict(self), ensure_ascii=False)


class JsonlMemory:
    """LLM: JSONL-backed memory store with optional LocalStore indexing and search fallback.

    新手说明:
    JSONL 是事实流水，LocalStore 是检索索引。
    即使 SQLite 或 FTS5 出问题，JSONL 记忆也不应该因此写不进去。

    字段说明:
    path: JSONL 记忆文件路径。
    local_store: 可选 LocalStore；有它时 add/index_all/search 可以同步索引和优先搜索索引。
    """

    def __init__(self, path: str | Path, local_store: LocalStore | None = None):
        """LLM: 初始化 JSONL 记忆文件位置，并确保父目录存在。

        新手说明:
        创建 JsonlMemory 时只准备文件路径和可选索引对象，不会读取全部记忆。

        参数说明:
        path: JSONL 文件路径。
        local_store: 可选 LocalStore，用于索引和搜索；为空时仍可正常写 JSONL。

        副作用说明:
        会创建 path 的父目录；不会创建 LocalStore，也不会调用模型。
        """
        self.path = Path(path)
        self.local_store = local_store
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def add(
        self,
        role: str,
        content: str,
        *,
        kind: str = "dialogue",
        tags: list[str] | None = None,
    ) -> MemoryRecord:
        """LLM: 追加一条记忆到 JSONL，并尽力同步索引到 LocalStore。

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
        会追加写入 JSONL 文件；如果 local_store 存在，会尝试 upsert 一条索引记录。
        """

        record = MemoryRecord(
            role=role,
            content=content,
            kind=kind,
            tags=tags or [],
            created_at=time.time(),
        )
        append_jsonl(self.path, asdict(record))
        self._try_index_record(record)
        return record

    def all(self) -> list[MemoryRecord]:
        """LLM: 从 JSONL 文件读取全部记忆记录。

        新手说明:
        这个方法适合小规模本地记忆。文件不存在时返回空列表；空行会被跳过。

        参数说明:
        这个方法没有输入参数。

        返回说明:
        返回 MemoryRecord 列表，顺序按 JSONL 文件中的行顺序。

        异常说明:
        如果某一行不是合法 JSON，目前会由 json.loads 抛错；后续如需容错可加 read audit。
        """

        if not self.path.exists():
            return []
        records: list[MemoryRecord] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            records.append(MemoryRecord(**obj))
        return records

    def search(self, query: str, top_k: int = 5) -> list[MemoryRecord]:
        """LLM: 搜索记忆，优先使用 LocalStore，失败或无命中时退回 JSONL 关键词搜索。

        新手说明:
        有 LocalStore 时优先走 SQLite/FTS5。
        没有索引、索引为空或索引临时失败时，退回 JSONL 关键词检索。

        参数说明:
        query: 搜索文本。
        top_k: 最多返回多少条结果。

        返回说明:
        返回 MemoryRecord 列表。LocalStore 有命中时返回索引结果，否则返回 JSONL fallback 结果。
        """

        indexed = self._search_local_store(query, top_k)
        if indexed:
            return indexed
        return self._search_jsonl(query, top_k)

    def index_all(self) -> int:
        """LLM: 把现有 JSONL 记忆补写到 LocalStore 索引。

        新手说明:
        这个命令适合第一次升级到 SQLite/FTS5 后运行一次。
        如果没有 local_store，就什么都不做并返回 0。

        参数说明:
        这个方法没有输入参数。

        返回说明:
        返回成功尝试索引的记录数量。

        副作用说明:
        会调用 LocalStore.upsert_record；不会改写 JSONL。
        """

        if not self.local_store:
            return 0
        count = 0
        for record in self.all():
            self._index_record(record)
            count += 1
        return count

    def _search_jsonl(self, query: str, top_k: int = 5) -> list[MemoryRecord]:
        """LLM: 使用简单关键词评分在 JSONL 事实流水中搜索。

        新手说明:
        这是 LocalStore 不可用时的兜底搜索。它不是高级搜索，只按词包含和整句包含打分。

        参数说明:
        query: 搜索文本；为空时返回按时间排序的前 top_k 条。
        top_k: 最多返回多少条。

        返回说明:
        返回按分数和创建时间倒序排列的 MemoryRecord 列表。
        """
        query_terms = {term.lower() for term in query.split() if term.strip()}
        scored: list[tuple[int, float, MemoryRecord]] = []
        for rec in self.all():
            text = rec.content.lower()
            score = sum(1 for term in query_terms if term in text)
            if query and query.lower() in text:
                score += 3
            if score > 0 or not query_terms:
                scored.append((score, rec.created_at, rec))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [record for _, _, record in scored[:top_k]]

    def _search_local_store(self, query: str, top_k: int) -> list[MemoryRecord]:
        """LLM: 尝试通过 LocalStore 搜索 memory source_type。

        新手说明:
        LocalStore 搜索更快、更结构化，但它只是索引层。失败时应该安静退回 JSONL 搜索。

        参数说明:
        query: 搜索文本。
        top_k: 最多返回多少条。

        返回说明:
        返回从 LocalStore hit 还原出的 MemoryRecord 列表；没有 local_store 或搜索失败时返回空列表。
        """
        if not self.local_store:
            return []
        try:
            hits = self.local_store.search(query, limit=top_k, source_type="memory")
        except Exception:
            return []
        return [self._memory_from_hit(hit) for hit in hits]

    def _try_index_record(self, record: MemoryRecord) -> None:
        """LLM: 尽力索引一条 memory record，索引失败不影响主写入。

        新手说明:
        写记忆时最重要的是 JSONL 成功。索引只是加速搜索的副本，坏了不能拖垮 chat/runner。

        参数说明:
        record: 已经写入或准备写入的 MemoryRecord。

        返回说明:
        没有返回值。
        """
        if not self.local_store:
            return
        try:
            self._index_record(record)
        except Exception:
            # 记忆 JSONL 是主流水，索引失败不能让 chat/runner 主链路中断。
            return

    def _index_record(self, record: MemoryRecord) -> None:
        """LLM: 把一条 MemoryRecord 写入 LocalStore 索引。

        新手说明:
        这里把记忆转换成 LocalStore 统一记录格式：source_type、source_id、title、content、metadata。

        参数说明:
        record: 要索引的 MemoryRecord。

        返回说明:
        没有返回值；local_store 为空时直接返回。

        副作用说明:
        会调用 local_store.upsert_record。
        """
        if not self.local_store:
            return
        self.local_store.upsert_record(
            source_type="memory",
            source_id=self._source_id(record),
            title=f"{record.kind}:{record.role}",
            content=record.content,
            metadata={
                "role": record.role,
                "kind": record.kind,
                "tags": record.tags or [],
                "created_at": record.created_at,
            },
        )

    def _source_id(self, record: MemoryRecord) -> str:
        """LLM: 为 memory record 生成稳定的 LocalStore source_id。

        新手说明:
        LocalStore 需要一个 id 来判断同一条记录。这里用时间、role、kind 和内容 hash 组合。

        参数说明:
        record: 要生成 id 的 MemoryRecord。

        返回说明:
        返回字符串，形如 `<created_at>:<role>:<kind>:<digest>`。
        """
        payload = json.dumps(asdict(record), ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        return f"{record.created_at:.6f}:{record.role}:{record.kind}:{digest}"

    def _memory_from_hit(self, hit: LocalSearchResult) -> MemoryRecord:
        """LLM: 把 LocalStore search hit 还原成 MemoryRecord。

        新手说明:
        搜索索引返回的是 LocalSearchResult，不是 MemoryRecord。这个函数负责把 metadata 里的 role、
        kind、tags、created_at 取回来，重新拼成记忆记录。

        参数说明:
        hit: LocalStore.search 返回的单条命中。

        返回说明:
        返回 MemoryRecord。metadata 缺字段时使用保守默认值。
        """
        metadata = hit.metadata
        tags = metadata.get("tags")
        return MemoryRecord(
            role=str(metadata.get("role") or "unknown"),
            content=hit.content,
            kind=str(metadata.get("kind") or "dialogue"),
            tags=tags if isinstance(tags, list) else [],
            created_at=float(metadata.get("created_at") or hit.created_at),
        )
