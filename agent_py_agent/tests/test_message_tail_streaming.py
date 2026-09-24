"""消息尾部流式读取与原整块读取逐项等价：同窗、同错、同停止边界，覆盖跨块多字节、CRLF、NEL/U+2028、坏行与未落盘尾行。"""
from __future__ import annotations

import json
import random
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.store_io import (
    _TAIL_BLOCK_BYTES,
    iter_jsonl_tail_lines,
    json_row,
    read_jsonl_tail_report,
)
from agent_py_agent.agent.gateway_parts.request_history import (
    _conversation_row_artifacts,
    _dedupe_facts,
    _gateway_message_matches_part,
    gateway_recent_artifacts,
)

_CONTEXT = "conversation.messages.read"
_LIMITS = (1, 2, 3, 5, 8, 20, 80, 100, 200, 1000)
_TEXT = "资料核对abc😀 中文xyz\u0085尾"


# LLM: 生成确定性的账本行：正常/展示/解析失败/坏 JSON/非对象/空白/仅分隔符行，偶尔 CRLF 与大行；只写测试目录。
# 函数用途: 按种子产出一行原始字节（含换行），大行保证窗口跨越多个 64KiB 块。
def _line(rng: random.Random, index: int, *, mode: str, allow_invalid_utf8: bool) -> bytes:
    faults = 0 if mode != "mixed" else 1
    display = 240 if mode == "display_heavy" else 8
    kind = rng.choices(["row", "display", "parse", "json", "nondict", "blank", "utf8"],
                       weights=[60, display, 4 * faults, 4 * faults, 3 * faults, 6,
                                3 * faults if allow_invalid_utf8 else 0])[0]
    ending = b"\r\n" if rng.random() < 0.15 else b"\n"
    if kind == "blank":
        return rng.choice(["", " \t", "\u0085", " "]).encode() + ending
    if kind == "json":
        return b'{"message_id": "broken' + ending
    if kind == "nondict":
        return b"[1, 2]" + ending
    if kind == "utf8":
        return b'{"message_id": "\xff\xfe", "role": "user"}' + ending
    size = rng.choice([0, 7, 300, 5000, 90_000 if rng.random() < 0.2 else 1200])
    start = rng.randrange(len(_TEXT))
    content = (_TEXT * (size // len(_TEXT) + 2))[start:start + size]
    role = "display" if kind == "display" else rng.choice(["user", "assistant"])
    metadata = {"gateway_request_id": f"req-{index % 7}", "assistant_part_id": rng.choice(["final", "p1", ""]),
                "delivery_artifacts": [{"artifact_id": f"a{index % 5}", "path": f"out/{index % 3}.md"}]}
    row = {"message_id": f"m{index}", "thread_id": "thread-x", "role": role, "content": content,
           "channel_message_id": f"c{index % 4}", "created_at": "oops" if kind == "parse" else index,
           "metadata": metadata}
    return json.dumps(row, ensure_ascii=False).encode() + ending


# 函数用途: 写一份确定性的测试账本；模式按种子轮换（混合故障/干净/大量展示行以触发翻倍重读），末行可不带换行或被截断。
def _write(tmp_path, seed: int, *, allow_invalid_utf8: bool = True):
    rng = random.Random(seed)
    mode = ("mixed", "clean", "display_heavy")[seed % 3]
    lines = [_line(rng, index, mode=mode, allow_invalid_utf8=allow_invalid_utf8)
             for index in range(rng.choice([3, 40, 160, 400]))]
    ending = (seed // 3) % 3
    if ending == 1 and lines:
        lines[-1] = lines[-1].rstrip(b"\r\n")
    elif ending == 2 and lines:
        lines[-1] = lines[-1][: max(1, len(lines[-1]) // 2)]
    store = ConversationStore(tmp_path / f"store-{seed}")
    path = store.storage.message_path("thread-x")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(lines))
    return store, path


# 函数用途: 把新迭代器产出的行按原实现的方式解析成 (行, 错误)，便于与原报告逐项比较。
def _parse(lines, path):
    parsed = [json_row(line, context=_CONTEXT, path=path, line_number=0) for line in lines]
    return [row for row, error in parsed if error is None], [error for _row, error in parsed if error is not None]


@pytest.mark.parametrize("seed", range(24))
def test_tail_lines_match_original_tail_report(tmp_path, seed):
    _store, path = _write(tmp_path, seed)
    for limit in _LIMITS:
        expected = read_jsonl_tail_report(path, context=_CONTEXT, limit=limit)
        rows, errors = _parse(list(iter_jsonl_tail_lines(path, limit=limit))[::-1], path)
        assert (rows, errors) == (expected.rows, expected.load_errors), limit


@pytest.mark.parametrize("seed", range(24))
def test_recent_projection_matches_recent_report(tmp_path, seed):
    store, _path = _write(tmp_path, seed)
    for limit in _LIMITS:
        entries, errors = store.messages.recent_report("thread-x", limit=limit)
        projected, projected_errors = store.messages.recent_projection_report("thread-x", limit=limit, project=lambda e: e)
        assert projected[::-1] == entries and projected_errors == errors, limit


@pytest.mark.parametrize("seed", range(24))
def test_visit_all_matches_full_recent_report(tmp_path, seed):
    store, _path = _write(tmp_path, seed, allow_invalid_utf8=seed % 4 == 0)
    visited = []
    try:
        entries, errors = store.messages.recent_report("thread-x", limit=0)
    except UnicodeDecodeError:
        with pytest.raises(UnicodeDecodeError):
            store.messages.visit_all_report("thread-x", visited.append)
        assert not visited, "严格解码失败时一条都不访问"
        return
    visit_errors = store.messages.visit_all_report("thread-x", visited.append)
    assert visit_errors == errors
    assert visited == ([] if errors else entries), "有坏行时一条都不交给索引，否则逐条同序"


# LLM: 与改动前 gateway_recent_artifacts 的原实现逐字相同，只作参照。
# 函数用途: 原"读最近 80 行再倒序挑 20 个产物"的结果与错误。
def _original_recent_artifacts(store, current_request_id):
    rows, errors = store.messages.recent_report("thread-x", limit=80)
    selected, seen = [], set()
    candidates = [ref for row in reversed(rows) for ref in reversed(_conversation_row_artifacts(row, current_request_id))]
    for ref in candidates:
        key = (str(ref.get("artifact_id") or ""), str(ref.get("path") or ""))
        if key in seen:
            continue
        seen.add(key)
        selected.append(ref)
        if len(selected) >= 20:
            return tuple(reversed(selected)), errors
    return tuple(reversed(selected)), errors


@pytest.mark.parametrize("seed", range(24))
def test_recent_artifacts_match_original(tmp_path, seed):
    store, _path = _write(tmp_path, seed)
    for current in ("req-0", "req-3", "none"):
        load_errors = []
        refs = gateway_recent_artifacts(SimpleNamespace(conversation_store=store), "thread-x", current, load_errors)
        assert (refs, load_errors) == _original_recent_artifacts(store, current)


@pytest.mark.parametrize("seed", range(24))
def test_dedupe_decision_matches_original_window(tmp_path, seed):
    store, _path = _write(tmp_path, seed)
    for limit in (100, 200):
        rows, errors = store.messages.recent_report("thread-x", limit=limit)
        facts, fact_errors = store.messages.recent_projection_report("thread-x", limit=limit, project=_dedupe_facts)
        assert fact_errors == errors
        for role, request_id, channel_message_id, part in (
            ("assistant", "req-1", "", "final"), ("assistant", "req-2", "", "p1"), ("user", "req-5", "", ""),
            ("user", "none", "c3", ""), ("user", "none", "", ""),
        ):
            kwargs = {"role": role, "request_id": request_id, "channel_message_id": channel_message_id,
                      "assistant_part_id": part}
            assert (any(_gateway_message_matches_part(fact, **kwargs) for fact in facts)
                    == any(_gateway_message_matches_part(row, **kwargs) for row in rows))


# LLM: 边界手工构造：多字节字符与 CRLF 恰好跨越 64KiB 块边界、LF 恰在块边界，另有窗口首行只能在文件头保留的情形。
# 函数用途: 写一份确定性边界账本并返回路径。
def _boundary_file(tmp_path, *, split: str):
    store = ConversationStore(tmp_path / f"boundary-{split}")
    path = store.storage.message_path("thread-x")
    path.parent.mkdir(parents=True, exist_ok=True)
    tail = json.dumps({"message_id": "last", "thread_id": "thread-x", "role": "user", "content": "尾"},
                      ensure_ascii=False).encode() + b"\n"
    head = {"message_id": "big", "thread_id": "thread-x", "role": "assistant", "content": ""}
    prefix = json.dumps(head, ensure_ascii=False).encode()[:-2]
    # 让"大行"在距文件尾恰好一个块的位置被 split 指定的字节序列切开。
    filler_len = _TAIL_BLOCK_BYTES - len(tail) - len(b'"}\r\n' if split == "crlf" else b'"}\n')
    if split == "multibyte":
        body = ("x" * (filler_len - 2)).encode() + "中".encode()
    elif split == "lf":
        body = ("y" * filler_len).encode()
    else:
        body = ("z" * filler_len).encode()
    ending = b'"}\r\n' if split == "crlf" else b'"}\n'
    older = json.dumps({"message_id": "old", "thread_id": "thread-x", "role": "user", "content": "旧"},
                       ensure_ascii=False).encode() + b"\n"
    path.write_bytes(older + prefix + b'"' + body + ending + tail)
    return store, path


@pytest.mark.parametrize("split", ["multibyte", "lf", "crlf"])
def test_block_boundaries_match_original(tmp_path, split):
    store, path = _boundary_file(tmp_path, split=split)
    for limit in (1, 2, 3):
        expected = read_jsonl_tail_report(path, context=_CONTEXT, limit=limit)
        assert _parse(list(iter_jsonl_tail_lines(path, limit=limit))[::-1], path) == (expected.rows, expected.load_errors)
        entries, errors = store.messages.recent_report("thread-x", limit=limit)
        projected, projected_errors = store.messages.recent_projection_report("thread-x", limit=limit, project=lambda e: e)
        assert (projected[::-1], projected_errors) == (entries, errors)


def test_blank_line_inside_closed_window_returns_fewer_rows_exactly_like_original(tmp_path):
    # 原实现的窗口按"换行数超过 limit"在块边界关闭；窗口内有空行时返回的行数可能少于 limit，新实现必须一致。
    store = ConversationStore(tmp_path / "blank-window")
    path = store.storage.message_path("thread-x")
    path.parent.mkdir(parents=True, exist_ok=True)
    big = [json.dumps({"message_id": f"big{i}", "thread_id": "thread-x", "role": "user", "content": "长" * 40_000},
                      ensure_ascii=False).encode() + b"\n" for i in range(2)]
    small = json.dumps({"message_id": "small", "thread_id": "thread-x", "role": "user", "content": "短"},
                       ensure_ascii=False).encode() + b"\n"
    path.write_bytes(b"".join(big) + b"\n" + small)
    expected = read_jsonl_tail_report(path, context=_CONTEXT, limit=2)
    assert [row["message_id"] for row in expected.rows] == ["small"], "前提：原实现在此窗口只返回 1 行"
    assert _parse(list(iter_jsonl_tail_lines(path, limit=2))[::-1], path) == (expected.rows, expected.load_errors)
