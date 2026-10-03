# LLM: 锁定 response_renderer 两个 when-ready 轮询入口的去重语义：完整文件代次指纹相同 + 已离开粗
#   mtime 窗口才算“没变”。窗口内必须重读（同时间片的原子替换不能漏），稳定文件必须只读一次。
#   改判定要同步 common/cache_freshness 和 cli 轮询调用方的回归测试。
# 模块用途: 回复文件/终态归档“按 stat 只在变了才读”的回归测试，覆盖 ino 变化失效、窗口内不入缓存、
#   稳定文件靠计数桩断言只读一次（不依赖计时）。
from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from agent_py_agent.agent.common import cache_freshness
from agent_py_agent.agent.gateway_parts import response_renderer
from agent_py_agent.agent.gateway_parts.response_renderer import (
    GatewayResponsePollState,
    read_gateway_response_file_when_ready,
    read_gateway_terminal_response_file_when_ready,
)

pytestmark = pytest.mark.integration


# 函数用途: 一个被测入口的完整调用说明：读什么文件、用什么载荷、给入口传哪些参数。
@dataclass(frozen=True)
class _Window:
    kind: str
    read_attr: str
    reader: Callable[[Path, GatewayResponsePollState], dict]
    payload_for: Callable[[str], str]
    entry_kwargs: dict


# 函数用途: 普通响应文件入口的载荷（结构化响应投影，不做 canonical 信封校验）。
def _plain_response_text(response: str) -> str:
    return json.dumps({"ok": True, "id": "req", "response": response})


# 函数用途: 合法 canonical 终态归档载荷（schema + id + terminal_response），供终态入口校验通过。
def _terminal_envelope_text(response: str) -> str:
    return json.dumps(
        {
            "schema_version": "gateway_terminal_request.v1",
            "id": "req",
            "terminal_response": {"ok": True, "id": "req", "response": response},
        }
    )


def _make_stable(path: Path) -> int:
    """把测试文件的 mtime 拨到粗窗口之外，让后续读取走“文件已稳定”的缓存路径。"""
    old_ns = time.time_ns() - 3_000_000_000
    os.utime(path, ns=(old_ns, old_ns))
    return old_ns


def _atomic_replace(path: Path, text: str, *, keep_mtime_ns: int | None = None) -> None:
    """原子替换一个文件（先写临时文件再 os.replace），可选把 mtime 钉回替换前的值。"""
    replacement = path.with_name(path.name + ".tmp")
    replacement.write_text(text, encoding="utf-8")
    os.replace(replacement, path)
    if keep_mtime_ns is not None:
        os.utime(path, ns=(keep_mtime_ns, keep_mtime_ns))


def _count_reads(monkeypatch, module_attr: str) -> list[str]:
    """给入口的读取函数装计数桩，返回记录每次读取的列表；不改被测文件内容。"""
    reads: list[str] = []
    real = getattr(response_renderer, module_attr)

    def counted(path, *args, **kwargs):
        reads.append(str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(response_renderer, module_attr, counted)
    return reads


# 函数用途: 两个入口的调用清单，参数化用例共用。
WINDOWS = (
    _Window(
        kind="response_file",
        read_attr="read_gateway_response_file",
        reader=lambda path, state: read_gateway_response_file_when_ready(
            path, state=state, context="gateway.test.response.read"
        ),
        payload_for=_plain_response_text,
        entry_kwargs={"context": "gateway.test.response.read"},
    ),
    _Window(
        kind="terminal_file",
        read_attr="read_gateway_terminal_response_file",
        reader=lambda path, state: read_gateway_terminal_response_file_when_ready(
            path, state=state, request_id="req", context="gateway.test.terminal.read"
        ),
        payload_for=_terminal_envelope_text,
        entry_kwargs={"request_id": "req", "context": "gateway.test.terminal.read"},
    ),
)


# 函数用途: 造出该入口的测试文件并返回路径；内容与入口的合法性要求一致。
def _write_window_file(window: _Window, tmp_path: Path, response: str) -> Path:
    path = tmp_path / f"{window.kind}.json"
    path.write_text(window.payload_for(response), encoding="utf-8")
    return path


@pytest.mark.parametrize("window", WINDOWS, ids=lambda item: item.kind)
def test_same_mtime_atomic_replace_invalidates_poll_state(tmp_path, monkeypatch, window) -> None:
    """同一 mtime、同一大小、只换 inode：必须重读，不能把上一代内容当成“没变”。"""
    path = _write_window_file(window, tmp_path, "first")
    stale_mtime_ns = _make_stable(path)
    # renderer 是 from-import 的本地绑定，打桩要打在 renderer 模块里它用的那个名字上（ds5 初审：打在 cache_freshness 上不生效）。
    monkeypatch.setattr(response_renderer, "cache_entry_trustworthy", lambda _mtime_ns: True)

    state = GatewayResponsePollState()
    assert window.reader(path, state)["response"] == "first"
    before = path.stat()
    assert window.reader(path, state) == {}, "指纹没变、文件已稳定时应跳过重读"

    _atomic_replace(path, window.payload_for("second"), keep_mtime_ns=stale_mtime_ns)
    after = path.stat()
    assert after.st_mtime_ns == before.st_mtime_ns
    assert after.st_ino != before.st_ino

    assert window.reader(path, state)["response"] == "second", "只换 inode 的原子替换必须让缓存失效"


@pytest.mark.parametrize("window", WINDOWS, ids=lambda item: item.kind)
def test_inside_coarse_mtime_window_always_rereads(tmp_path, monkeypatch, window) -> None:
    """mtime 距今不足 2 秒：即使指纹相同也重读，文件真的换了内容必须读到新的那一代。"""
    path = _write_window_file(window, tmp_path, "first")
    stale_mtime_ns = _make_stable(path)
    reads = _count_reads(monkeypatch, window.read_attr)

    state = GatewayResponsePollState()
    assert window.reader(path, state)["response"] == "first"
    assert len(reads) == 1

    # 把文件改成“刚写过”：指纹沿用首次读取那一代，只有窗口判断能发现它已不可信。
    _atomic_replace(path, window.payload_for("first"), keep_mtime_ns=stale_mtime_ns)
    fresh_ns = time.time_ns()
    os.utime(path, ns=(fresh_ns, fresh_ns))
    state.stat_signature = response_renderer.cache_stat_signature(path.stat())

    assert window.reader(path, state)["response"] == "first"
    assert len(reads) == 2, "窗口内即使指纹相同也必须重读"

    _atomic_replace(path, window.payload_for("second"), keep_mtime_ns=fresh_ns)
    assert window.reader(path, state)["response"] == "second"


@pytest.mark.parametrize("window", WINDOWS, ids=lambda item: item.kind)
def test_stable_file_is_read_only_once(tmp_path, monkeypatch, window) -> None:
    """稳定文件（mtime 很早）连续读两次：第二次走缓存，用计数桩断言，不靠计时。"""
    path = _write_window_file(window, tmp_path, "stable")
    _make_stable(path)
    reads = _count_reads(monkeypatch, window.read_attr)

    state = GatewayResponsePollState()
    assert window.reader(path, state)["response"] == "stable"
    assert window.reader(path, state) == {}
    assert len(reads) == 1, "指纹与窗口都允许时，第二次读取不应再碰文件"
