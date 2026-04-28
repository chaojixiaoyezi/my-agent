from __future__ import annotations

"""本地记忆模块。

当前实现比较朴素，但很好懂：
- 每条记忆存成一行 JSON
- 全部放在一个 `.jsonl` 文件里
- 检索时做简单关键词匹配

它不是向量数据库，也没有复杂召回策略。
说白了，现在先保证“有记忆、能落盘、能搜到”，后面再逐步升级。
"""

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class MemoryRecord:
    """一条记忆记录。"""

    role: str
    content: str
    kind: str = "dialogue"
    tags: list[str] | None = None
    created_at: float = 0.0

    def to_json(self) -> str:
        """把当前记忆转成一行 JSON 文本，方便写入 JSONL 文件。"""

        if not self.created_at:
            self.created_at = time.time()
        return json.dumps(asdict(self), ensure_ascii=False)


class JsonlMemory:
    """基于 JSONL 文件的轻量记忆存储。

    这里没有上数据库，就是因为项目当前优先要“简单可跑”。
    你甚至可以直接用文本编辑器打开记忆文件看里面存了什么。
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def add(
        self,
        role: str,
        content: str,
        *,
        kind: str = "dialogue",
        tags: list[str] | None = None,
    ) -> MemoryRecord:
        """追加一条记忆到磁盘。"""

        record = MemoryRecord(
            role=role,
            content=content,
            kind=kind,
            tags=tags or [],
            created_at=time.time(),
        )
        with self.path.open("a", encoding="utf-8") as file:
            file.write(record.to_json() + "\n")
        return record

    def all(self) -> list[MemoryRecord]:
        """读取全部记忆记录。"""

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
        """按简单关键词规则搜索记忆。

        这里不是语义搜索，而是很直接的文本匹配。
        这么做的优点是简单、稳定、没有外部依赖；
        缺点是智能程度有限。
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
