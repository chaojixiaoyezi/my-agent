from __future__ import annotations

from agent_py_agent.agent.runtime_errors import DataCorruptionError, runtime_error_report


def test_runtime_error_report_uses_structured_context_for_subagent_message() -> None:
    payload = runtime_error_report(OSError("ledger unreadable"), context="tool_call_scope.subagents.load")

    assert payload["context"] == "tool_call_scope.subagents.load"
    assert "子代理账本读取失败" in payload["model_message"]


def test_runtime_error_report_does_not_infer_subagent_context_from_free_text() -> None:
    payload = runtime_error_report(OSError("ledger unreadable"), context="please load the subagent note")

    assert "子代理账本读取失败" not in payload["model_message"]
    assert "本地状态或路径读取失败" in payload["model_message"]


def test_runtime_error_report_uses_structured_context_for_guidance_message() -> None:
    payload = runtime_error_report(OSError("guidance unreadable"), context="conversation.guidance.read")

    assert "运行中补充提示读取失败" in payload["model_message"]


def test_data_corruption_report_uses_generic_message_without_matching_context() -> None:
    payload = runtime_error_report(DataCorruptionError("bad json"), context="memory.routing.load")

    assert "子代理没产物" not in payload["model_message"]
    assert "没有数据" in payload["model_message"]
