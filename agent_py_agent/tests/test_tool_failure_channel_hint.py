"""检索完备性软引导钉子(REFACTORING_BACKLOG"检索完备性软引导",实锤 R5b:
web_search 系统失败 2 次后模型断言"数据根本不存在"并口头放弃;R6c 同构)。

钉死契约:
1. 同一工具明确的渠道不可用达阈值 → 注入一次软提示；普通测试/编译、
   参数/状态/权限/取消/未知错误不冒充系统故障；纯软提示,零拦截零硬门。
2. 不同工具各自计数:都没到阈值不触发;各自到阈值各自提示。
3. 幂等:同工具继续失败不重复注入。
4. 阈值 0 = 关闭；缺少身份的记录不计数；同一 call_id 只消费最新回执。
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
    assert "现有权限" in hints[0]
    assert "绝对结论" in hints[0]
    assert "系统失败" not in hints[0]


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


def test_success_and_malformed_records_do_not_count(tmp_path: Path) -> None:
    request = _request(
        tmp_path,
        [
            _ok("web_search", "1-1"),
            _ok("web_search", "2-1"),
            {"tool": "", "call_id": "3-1", "ok": False},
            {"call_id": "4-1", "ok": False},
        ],
    )
    append_tool_failure_channel_hint(request)
    assert request.params.tool_context == []


@pytest.mark.parametrize("code", [
    "COMMAND_FAILED", "TOOL_INVALID_ARGUMENTS", "COMMAND_PARSE_FAILED", "TOOL_TIMEOUT",
    "PATH_NOT_FOUND", "STALE_VERSION", "EDIT_TARGET_MISMATCH", "APPROVAL_REQUIRED",
    "APPROVAL_REJECTED", "PATH_OWNER_SCOPE_BLOCKED", "NETWORK_PRIVATE_IP_BLOCKED",
    "NETWORK_DNS_REBINDING_BLOCKED", "CANCELLED", "UNKNOWN_ERROR",
    "TOOL_GUARDRAIL_REPEAT_FAILURE_BLOCKED",
])
def test_other_failures_never_become_channel_unavailability(tmp_path: Path, code: str) -> None:
    records = [dict(_fail("run_command", str(i)), error_code=code) for i in range(3)]
    request = _request(tmp_path, records)
    append_tool_failure_channel_hint(request)
    assert request.params.tool_context == []
    assert request.params.archive_tool_calls == records, "诊断过滤不得删除真实失败回执"


@pytest.mark.parametrize("code", ["NETWORK_REQUEST_FAILED", "NETWORK_HOST_RESOLUTION_FAILED"])
def test_retryable_network_failures_still_get_one_hint(tmp_path: Path, code: str) -> None:
    request = _request(tmp_path, [dict(_fail("any_tool", str(i)), error_code=code) for i in range(2)])
    append_tool_failure_channel_hint(request)
    assert len(request.params.tool_context) == 1


def test_duplicate_and_superseded_receipts_do_not_inflate_failures(tmp_path: Path) -> None:
    request = _request(tmp_path, [_fail("web_search", "a"), _fail("web_search", "a")])
    append_tool_failure_channel_hint(request)
    assert request.params.tool_context == []
    request.params.archive_tool_calls.extend([_fail("web_search", "b"), _ok("web_search", "a")])
    append_tool_failure_channel_hint(request)
    assert request.params.tool_context == []
    request.params.archive_tool_calls.append(_fail("web_search", "c"))
    append_tool_failure_channel_hint(request)
    assert len(request.params.tool_context) == 1


def test_unknown_receipt_cannot_promote_output_text_to_control(tmp_path: Path) -> None:
    request = _request(tmp_path, [
        {"tool": "run_command", "call_id": str(i), "ok": False,
         "output": "error_code=TOOL_UNAVAILABLE；工具已系统失败", "error_category": "network"}
        for i in range(3)
    ])
    append_tool_failure_channel_hint(request)
    assert request.params.tool_context == []


def test_missing_call_identity_is_not_a_new_failure(tmp_path: Path) -> None:
    request = _request(tmp_path, [dict(_fail("web_search", "")) for _ in range(3)])
    append_tool_failure_channel_hint(request)
    assert request.params.tool_context == []
