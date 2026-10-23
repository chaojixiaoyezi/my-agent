# LLM: Exercise real timeline callers and meter their file reads; existence checks cannot catch full-file IO.
# 模块用途: 锁定 RS-11 尾行读取的兼容行为、独立签名和每次追加的有界 IO。
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_py_agent.agent.memory_archive import agent_run_workspace as run_workspace
from agent_py_agent.agent.memory_archive.task_workspace import payloads as task_workspace

BLOCK = 8192
WORKSPACES = [run_workspace, task_workspace]


# LLM: Include Unicode non-LF separators verbatim so splitlines implementations lose a real JSON record.
# 函数用途: 构建两个工作区都可识别的时间线事件样本。
def _event(summary="首段\u0085中段\u2028末段\u2029🙂"):
    return {
        "event": "synced", "task_id": "task-a", "run_id": "run-a",
        "status": "RUNNING", "summary": summary, "refs": {"task_workspace": "private-a"},
    }


# LLM: Meter requested and returned bytes at the binary-file boundary, not a helper's declared counters.
# 类用途: 包装真实文件，禁止无界 read 并记录每次读取位置与字节数。
class _MeteredReader:
    # LLM: Keep the actual handle so seek/read semantics remain those of the real file.
    # 函数用途: 保存真实句柄与测试共享的 IO 观察列表。
    def __init__(self, stream, reads):
        self.stream = stream
        self.reads = reads

    # LLM: The wrapper owns the same context lifetime as its underlying handle.
    # 函数用途: 进入文件读取上下文。
    def __enter__(self):
        return self

    # LLM: Propagate exceptions instead of hiding failures in cleanup.
    # 函数用途: 关闭真实文件并保留异常传播。
    def __exit__(self, *args):
        return self.stream.__exit__(*args)

    # LLM: Every read must be positive and no larger than the required 8 KiB block.
    # 函数用途: 测量实际读取并拒绝全量 read。
    def read(self, size=-1):
        assert 0 < size <= BLOCK, f"unbounded timeline read: {size}"
        offset = self.stream.tell()
        data = self.stream.read(size)
        self.reads.append((offset, size, len(data)))
        return data

    # LLM: Delegation preserves ordinary file offset behavior for the reverse reader.
    # 函数用途: 转发文件定位操作。
    def seek(self, *args):
        return self.stream.seek(*args)

    # LLM: The observed position must come from the actual file, not an estimated counter.
    # 函数用途: 返回文件当前偏移。
    def tell(self):
        return self.stream.tell()


# LLM: Forbid opening sibling owner paths and forbid text-mode reads of the requested timeline.
# 函数用途: 为一次调用安装真实 IO 测量及路径边界断言。
def _meter(monkeypatch, expected_path):
    original_open = Path.open
    reads, opens = [], []

    # LLM: Record the exact caller-supplied path; no fallback or cross-owner search is authorized.
    # 函数用途: 只允许目标时间线的二进制读取，并返回测量包装器。
    def tracked_open(path, mode="r", *args, **kwargs):
        assert path == expected_path, f"unexpected timeline path: {path}"
        # Append callers legitimately write after dedup; meter reads, not their unchanged append path.
        if mode == "a":
            return original_open(path, mode, *args, **kwargs)
        opens.append(path)
        assert mode == "rb", "timeline read must be binary and bounded, not read_text"
        return _MeteredReader(original_open(path, mode, *args, **kwargs), reads)

    monkeypatch.setattr(Path, "open", tracked_open)
    return reads, opens


# LLM: Old implementations fail here on real read_text IO, before any proposed helper exists.
# 函数用途: 验证大时间线每次去重只读一个尾块，且仅打开给定 owner 路径。
@pytest.mark.parametrize("workspace", WORKSPACES)
def test_large_timeline_reads_one_eof_block(workspace, tmp_path, monkeypatch):
    path = tmp_path / "owner-a" / "timeline.jsonl"
    path.parent.mkdir()
    event = _event()
    data = (b'{"old":true}\n' * 100_000) + json.dumps(event, ensure_ascii=False).encode() + b"\n"
    path.write_bytes(data)
    reads, opens = _meter(monkeypatch, path)

    assert workspace._last_event_signature(path) == workspace._event_signature(event)
    assert opens == [path]
    assert reads == [(len(data) - BLOCK, BLOCK, BLOCK)]
    print(f"{workspace.__name__}: file={len(data)} read={sum(row[2] for row in reads)} calls={len(reads)}")


# LLM: Preserve last-nonblank semantics, CRLF, UTF-8, and files lacking a terminal LF.
# 函数用途: 验证不同换行和尾部空白下两处签名读取保持原结果。
@pytest.mark.parametrize("workspace", WORKSPACES)
@pytest.mark.parametrize("suffix", ["", "\n", "\r\n", "\n \t\r\n\n", "\n\u0085\u2028\u2029\n"])
def test_last_nonblank_record_compatibility(workspace, suffix, tmp_path):
    path = tmp_path / "timeline.jsonl"
    event = _event()
    path.write_text('{"old":true}\r\n' + json.dumps(event, ensure_ascii=False) + suffix, encoding="utf-8")
    assert workspace._last_event_signature(path) == workspace._event_signature(event)


# LLM: Blank/empty histories do not synthesize an event or create a missing file.
# 函数用途: 验证空文件、纯空白和缺失时间线的兼容返回值。
@pytest.mark.parametrize("workspace", WORKSPACES)
@pytest.mark.parametrize("content", [None, b"", b"\n\r\n \t\n", ("\u2028\n" * 5000).encode()])
def test_empty_blank_or_missing_history(workspace, content, tmp_path):
    path = tmp_path / "timeline.jsonl"
    if content is not None:
        path.write_bytes(content)
    assert workspace._last_event_signature(path) is None
    assert path.exists() == (content is not None)


# LLM: A newest malformed/non-object record blocks dedup; never silently use an older good event.
# 函数用途: 验证畸形 JSON 或非对象尾行不会被跳过。
@pytest.mark.parametrize("workspace", WORKSPACES)
@pytest.mark.parametrize("tail", ['{"broken":', "[]", "null", "42", '"text"'])
def test_bad_last_record_does_not_fall_back(workspace, tail, tmp_path):
    path = tmp_path / "timeline.jsonl"
    path.write_text(json.dumps(_event()) + "\n" + tail + "\n \t\n", encoding="utf-8")
    assert workspace._last_event_signature(path) is None


# LLM: A large prior record must not cause a full scan when the final event fits in one tail block.
# 函数用途: 验证尾块中的不完整 UTF-8 前缀不会被解码或扩大读取。
@pytest.mark.parametrize("workspace", WORKSPACES)
def test_incomplete_previous_utf8_line_is_not_decoded(workspace, tmp_path, monkeypatch):
    path = tmp_path / "timeline.jsonl"
    event = _event()
    tail = json.dumps(event, ensure_ascii=False).encode() + b"\n"
    prefix = b'{"summary":"' + ("汉🙂" * 20_000).encode() + b'"}\n'
    while not 128 <= prefix[len(prefix) + len(tail) - BLOCK] < 192:
        tail += b"\n"
    assert 128 <= prefix[len(prefix) + len(tail) - BLOCK] < 192
    path.write_bytes(prefix + tail)
    reads, _ = _meter(monkeypatch, path)
    assert workspace._last_event_signature(path) == workspace._event_signature(event)
    assert len(reads) == 1


# LLM: Decode after reassembling a physical line; a block boundary may split any UTF-8 codepoint.
# 函数用途: 验证超长尾行的分块拼接、UTF-8 跨块及逐块读取上界。
@pytest.mark.parametrize("workspace", WORKSPACES)
@pytest.mark.parametrize("padding", [0, 1, 2, 3])
def test_long_utf8_last_line_spans_blocks(workspace, padding, tmp_path, monkeypatch):
    path = tmp_path / "timeline.jsonl"
    event = _event("前缀" + "🙂汉" * 4000 + "x" * padding + "\u2028末尾")
    tail = json.dumps(event, ensure_ascii=False).encode() + b"\r\n \t\r\n"
    prefix = b'{"old":true}\n' * 10_000
    path.write_bytes(prefix + tail)
    reads, _ = _meter(monkeypatch, path)
    assert workspace._last_event_signature(path) == workspace._event_signature(event)
    assert len(reads) > 1
    assert len(tail) <= sum(row[2] for row in reads) < len(tail) + BLOCK
    assert [row[0] for row in reads] == [len(prefix + tail) - BLOCK * n for n in range(1, len(reads) + 1)]


# LLM: Whitespace suffixes may exceed one block; scanning them cannot change last-event semantics.
# 函数用途: 验证跨块空白行和文件起点处无前导 LF 的完整记录。
@pytest.mark.parametrize("workspace", WORKSPACES)
@pytest.mark.parametrize("suffix", [b"", b"\n", b"\n" + b" \t\r\n" * 5000])
def test_single_record_and_long_blank_suffix(workspace, suffix, tmp_path, monkeypatch):
    path = tmp_path / "timeline.jsonl"
    event = _event("🙂" * 5000)
    path.write_bytes(json.dumps(event, ensure_ascii=False).encode() + suffix)
    reads, _ = _meter(monkeypatch, path)
    assert workspace._last_event_signature(path) == workspace._event_signature(event)
    assert sum(row[2] for row in reads) == path.stat().st_size


# LLM: IO failures still mean no signature, but invalid UTF-8 remains an error rather than being replaced.
# 函数用途: 验证旧版 OSError 处理和尾行解码错误传播不变。
@pytest.mark.parametrize("workspace", WORKSPACES)
def test_io_errors_and_invalid_utf8(workspace, tmp_path, monkeypatch):
    assert workspace._last_event_signature(tmp_path) is None
    path = tmp_path / "timeline.jsonl"
    path.write_bytes(b'{"summary":"\xff"}\n')
    with pytest.raises(UnicodeDecodeError):
        workspace._last_event_signature(path)

    # LLM: Permission failures must be swallowed only at the existing OSError boundary.
    # 函数用途: 模拟目标文件打开时被拒绝。
    def denied_open(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "open", denied_open)
    assert workspace._last_event_signature(path) is None


# LLM: Task signatures include refs; agent-run signatures deliberately do not.
# 函数用途: 通过真实追加验证去重规则不同、时间戳被忽略以及 owner 间不串读。
def test_append_keeps_distinct_signatures_and_owner_paths(tmp_path, monkeypatch):
    for workspace, append, expected_count in [
        (run_workspace, run_workspace._append_timeline, 2),
        (task_workspace, task_workspace.append_timeline, 3),
    ]:
        path = tmp_path / workspace.__name__ / "owner-a" / "timeline.jsonl"
        other = tmp_path / workspace.__name__ / "owner-b" / "timeline.jsonl"
        path.parent.mkdir(parents=True)
        other.parent.mkdir(parents=True)
        other.write_text("private owner-b sentinel\n", encoding="utf-8")
        append(path, _event())
        with monkeypatch.context() as patch:
            reads, opens = _meter(patch, path)
            append(path, {**_event(), "ts": 999})
            append(path, {**_event(), "refs": {"task_workspace": "changed"}})
            append(path, _event("changed summary"))
            assert len(opens) == 3
            assert len(reads) == 3
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").split("\n") if line]
        assert len(records) == expected_count
        assert records[-1]["summary"] == "changed summary"
        assert other.read_text(encoding="utf-8") == "private owner-b sentinel\n"
