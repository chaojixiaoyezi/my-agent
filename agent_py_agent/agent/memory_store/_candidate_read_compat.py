# LLM: Legacy JSONL reader overrides receive a view of the already verified FD, never a reopened authority path.
# 模块用途: 保留候选读取器的旧注入入口，默认流式读取不经过这个兼容分支。
from __future__ import annotations

import hashlib
import json


# LLM: A borrowed text stream lives only inside the caller's nofollow context; the view grants no new file access.
# 类用途: 让旧JSONL读取器读取同一个已打开的候选快照，不重新按路径打开文件。
class _CandidateReadView:
    # LLM: Store only a local borrowed stream and its diagnostic path, not a global write source or callback.
    # 函数用途: 连接原读取器需要的路径外观与当前受保护的文件句柄。
    def __init__(self, path, stream):
        self.path, self.stream = path, stream
        self.digest = None
        self._text = None

    # LLM: Existence is established by the caller's successful nofollow open, not a second pathname lookup.
    # 函数用途: 向旧读取器报告当前句柄确实已打开。
    def exists(self):
        return True

    # LLM: Read/hash the verified FD only once; repeated Path.read_text-style calls return that same snapshot.
    # 函数用途: 为旧读取器提供当前快照的文字，并记录同次读取的内容摘要。
    def read_text(self, *, encoding="utf-8"):
        if encoding != "utf-8":
            raise ValueError("candidate ledger requires UTF-8")
        if self._text is None:
            self._text = self.stream.read()
            self.digest = hashlib.blake2b(self._text.encode("utf-8"), digest_size=16).hexdigest()
        return self._text

    # LLM: The original path is diagnostic text only; no filesystem conversion or pathname reopen is exposed.
    # 函数用途: 保留读取错误里的原账本路径。
    def __str__(self):
        return str(self.path)


# LLM: Overrides keep their real report/errors; every returned object still undergoes the original schema parser.
# 函数用途: 经旧注入入口读取同FD报告并生成候选，未实际读文件的替身结果不能进入文件版本缓存。
def read_candidate_override(path, stream, reader, parse_row):
    source = _CandidateReadView(path, stream)
    report = reader(source, context="memory_candidates.load")
    if report.load_errors:
        raise ValueError("candidate ledger contains unreadable rows")
    records = [parse_row(json.dumps(payload, ensure_ascii=False, sort_keys=True), index)
               for index, payload in enumerate(report.records, start=1)]
    return records, source.digest
