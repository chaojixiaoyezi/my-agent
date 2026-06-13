"""检索完备性软引导钉子(REFACTORING_BACKLOG"检索完备性软引导",实锤 R5b:
web_search 系统失败 2 次后模型断言"数据根本不存在"并口头放弃;R6c 同构)。

钉死契约:
1. 同一工具系统失败(archive ok=false)达阈值 → 注入一次枚举引导软提示
   (含工具名/失败计数/tried+untried 枚举指引);纯软提示,零拦截零硬门。
2. 不同工具各自计数:都没到阈值不触发;各自到阈值各自提示。
3. 幂等:同工具继续失败不重复注入。
4. 阈值 0 = 关闭;成功调用与 __parse_error__ 不计数。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.tool_guard.loop_hints import (
    append_tool_failure_channel_hint,
)
from agent_py_agent.agent.settings.config import AgentConfig

pytestmark = pytest.mark.integration


def _request(tmp_path: Path, archive: list, *, threshold: int = 2) -> SimpleNamespace:
    agent = SimpleNamespace(config=AgentConfig(tool_failure_channel_hint_threshold=threshold))
    params = SimpleNamespace(tool_context=[], archive_tool_calls=archive)
    return SimpleNamespace(agent=agent, params=params, tool_rounds=3)


def _fail(tool: str, call_id: str) -> dict:
    return {"tool": tool, "call_id": call_id, "ok": False, "error_code": "TOOL_UNAVAILABLE"}


def _ok(tool: str, call_id: str) -> dict:
    return {"tool": tool, "call_id": call_id, "ok": True}


def test_same_tool_failures_reach_threshold_inject_hint(tmp_path: Path) -> None:
    """R5b 形态:web_search 失败 2 次 → 引导枚举未试渠道。"""
    request = _request(tmp_path, [_fail("web_search", "1-1"), _ok("read_file", "1-2"), _fail("web_search", "2-1")])

    append_tool_failure_channel_hint(request)

    hints = [item for item in request.params.tool_context if "[tool-failure-channel-hint]" in item]
    assert len(hints) == 1
    assert "tool=web_search" in hints[0]
    assert "failures=2" in hints[0]
    assert "untried_channels_known" in hints[0], "必须指向不可行报告的枚举字段"
    assert "绝对结论" in hints[0]


def test_different_tools_count_separately(tmp_path: Path) -> None:
    request = _request(tmp_path, [_fail("web_search", "1-1"), _fail("fetch_url", "2-1")])

    append_tool_failure_channel_hint(request)

    assert request.params.tool_context == [], "各工具失败 1 次未达阈值,不触发"

    request.params.archive_tool_calls.extend([_fail("web_search", "3-1"), _fail("fetch_url", "3-2")])
    append_tool_failure_channel_hint(request)
    hints = [item for item in request.params.tool_context if "[tool-failure-channel-hint]" in item]
    assert len(hints) == 2, "各自到阈值各自提示一次"
    assert any("tool=web_search" in hint for hint in hints)
    assert any("tool=fetch_url" in hint for hint in hints)


def test_hint_is_idempotent_per_tool(tmp_path: Path) -> None:
    archive = [_fail("web_search", "1-1"), _fail("web_search", "2-1")]
    request = _request(tmp_path, archive)

    append_tool_failure_channel_hint(request)
    archive.append(_fail("web_search", "3-1"))
    append_tool_failure_channel_hint(request)

    hints = [item for item in request.params.tool_context if "tool=web_search" in item]
    assert len(hints) == 1, "同工具继续失败不重复堆叠提示"


def test_threshold_zero_disables_hint(tmp_path: Path) -> None:
    request = _request(tmp_path, [_fail("web_search", "1-1"), _fail("web_search", "2-1")], threshold=0)
    append_tool_failure_channel_hint(request)
    assert request.params.tool_context == []


def test_success_and_parse_errors_do_not_count(tmp_path: Path) -> None:
    request = _request(
        tmp_path,
        [
            _ok("web_search", "1-1"),
            _ok("web_search", "2-1"),
            {"tool": "__parse_error__", "call_id": "3-1", "ok": False},
            {"tool": "__parse_error__", "call_id": "4-1", "ok": False},
        ],
    )
    append_tool_failure_channel_hint(request)
    assert request.params.tool_context == []
