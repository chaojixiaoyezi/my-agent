# LLM: 只在调用方临时根合成原格式数据，观察真实文件读取，不读正式owner、不启动模型。
# 模块用途: 给E11c等价、扫描量和性能测试共用合成数据及计数观察器。
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from agent_py_agent.agent.conversation import ConversationStore


# LLM: 原Store创建身份及样本，其余行只写测试canonical文件；不改线程格式或产品追加路径。
# 函数用途: 生成可直接经after_report读取的合成线程和原消息对象。
def make_messages(root, count=20, content_chars=100):
    store = ConversationStore(root / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "synthetic-owner", "channel": "chat",
                                         "channel_user_id": "synthetic-owner", "channel_conversation_id": "scan", "now": 1.0})
    sample = store.messages.append({"thread_id": thread.thread_id, "role": "user", "content": "x" * content_chars, "now": 2.0})
    rows = [replace(sample, message_id=f"msg-{i:05}", created_at=float(i + 2)) for i in range(count)]
    path = store.storage.message_path(thread.thread_id)
    path.write_bytes(b"".join(encode_row(row.to_dict()) for row in rows))
    return store, thread.thread_id, rows


# LLM: 合成审计只落测试根，事件身份显式固定便于跨owner隔离测试；没有任何工具执行副作用。
# 函数用途: 写若干日分片，返回文件和事件序列。
def make_audit(root, days=3, rows_per_day=4):
    root.mkdir(parents=True, exist_ok=True)
    paths, records = [], []
    for day in range(days):
        path = root / f"2026-01-{day + 1:02}.jsonl"
        rows = [{"event_id": f"event-{day}-{i}", "event_type": "tool_call", "created_at": 1.0,
                 "tool_name": "read_file", "status": "ok", "preview": "合成预览"} for i in range(rows_per_day)]
        path.write_bytes(b"".join(encode_row(row) for row in rows))
        paths.append(path)
        records.extend(rows)
    return paths, records


# LLM: 按canonical物理LF边界编码；UTF-8里的NEL等不是行分隔，不用splitlines制造测试假损坏。
# 函数用途: 编码一条合成JSONL记录。
def encode_row(row):
    return json.dumps(row, ensure_ascii=False).encode("utf-8") + b"\n"


# LLM: 包装真实描述符只计量，seek/fstat/EOF与内容仍由文件提供，不能伪造扫描量。
# 类用途: 保存被观察文件的打开次数和实际读取字节。
class ReadCounts:
    def __init__(self):
        self.opens = []
        self.bytes = 0

    def clear(self):
        self.opens.clear()
        self.bytes = 0


# LLM: 不接管文件权限或异常；上下文关闭原handle，属性均转发以保留真实IO语义。
# 类用途: 给指定文件的read/readline附加字节计数。
class ObservedFile:
    def __init__(self, handle, counts):
        self.handle, self.counts = handle, counts

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self.handle.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self.handle, name)

    def read(self, *args):
        return self._count(self.handle.read(*args))

    def readline(self, *args):
        return self._count(self.handle.readline(*args))

    def __iter__(self):
        while line := self.readline():
            yield line

    def _count(self, data):
        self.counts.bytes += len(data.encode("utf-8") if isinstance(data, str) else data)
        return data


# LLM: 只包指定canonical路径的实际读取；stat不算打开，baseline与优化路径可用同一观察点对照。
# 函数用途: 在pytest中安装读取计数器，恢复由monkeypatch负责。
def observe_reads(monkeypatch, paths):
    counts = ReadCounts()
    observed = set(paths)
    original = Path.open

    def counted(path, *args, **kwargs):
        handle = original(path, *args, **kwargs)
        if path not in observed or "r" not in (args[0] if args else kwargs.get("mode", "r")):
            return handle
        counts.opens.append(str(path))
        return ObservedFile(handle, counts)

    monkeypatch.setattr(Path, "open", counted)
    return counts
