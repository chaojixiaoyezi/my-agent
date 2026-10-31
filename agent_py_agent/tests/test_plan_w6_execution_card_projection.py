"""W6 B1：真实工具事实的纯执行视图与飞书 Card JSON 2.0。"""
from __future__ import annotations

import copy
import json

import pytest

from agent_py_agent.agent.adapter.feishu_progress_card import render_progress_card
from agent_py_agent.agent.delivery.execution_view import project_execution_view
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall, ToolResult


def _call(index):
    return ToolCall(
        call_id=f"call-{index}", tool_name="read_file", arguments={"path": "private-input"},
        source_protocol="native", schema_hash="sha256:test", run_id="run", turn_id="turn", attempt_id="attempt",
    )


def _result(index, *, status="succeeded"):
    return ToolResult(call_id=f"call-{index}", tool_name="read_file", status=status,
                      handler_executed=True, duration_ms=index + 10)


def _progress(index, phase, **fields):
    return {"kind": "tool_progress", "request_id": "request", "progress": {
        "call_id": f"call-{index}", "tool": "read_file", "phase": phase,
        "round": 1, "call_index": index, **fields,
    }}


def _card_text(card):
    return json.dumps(card, ensure_ascii=False)


def test_five_real_tool_calls_and_results_project_to_five_ordered_steps():
    events = [item for index in range(5) for item in (_call(index), _result(index))]
    view = project_execution_view(events, request_id="request", source_revision=10)
    assert [step["step_id"] for step in view["steps"]] == [f"call-{index}" for index in range(5)]
    assert view["request_id"] == "request" and view["source_revision"] == 10
    assert view["total_steps"] is None
    for index, step in enumerate(view["steps"]):
        assert step["tool_name"] == "read_file" and step["phase"] == "succeeded"
        assert step["ok"] is True and step["handler_executed"] is True
        assert step["duration_ms"] == index + 10 and step["display_only"] is False
    card = render_progress_card(view)
    assert card["schema"] == "2.0"
    panel = next(element for element in card["body"]["elements"] if element["tag"] == "collapsible_panel")
    assert panel["expanded"] is False and len(panel["elements"]) == 5
    assert "private-input" not in _card_text(card)


def test_late_started_cannot_regress_confirmed_terminal_even_with_newer_cursor():
    events = [_call(1), _progress(1, "completed", ok=True, handler_executed=True, duration_ms=42),
              _progress(1, "started", source_revision=999)]
    view = project_execution_view(events, request_id="request")
    assert len(view["steps"]) == 1
    assert view["steps"][0]["phase"] == "completed"
    assert view["steps"][0]["ok"] is True and view["steps"][0]["duration_ms"] == 42


def test_only_model_words_and_request_events_produce_no_tool_steps():
    events = ["我已调用五个工具，100%完成", {"kind": "assistant_commentary", "text": "read_file started"},
              {"kind": "assistant_final", "tool_name": "read_file", "phase": "completed", "text": "已完成"},
              {"kind": "request_completed", "request_id": "request", "status": "completed"}]
    view = project_execution_view(events, request_id="request")
    assert view["steps"] == []
    assert "100%" not in _card_text(render_progress_card(view))


def test_legacy_progress_without_call_id_has_only_a_display_key():
    event = {"kind": "tool_progress", "request_id": "request", "progress": {
        "tool": "list_files", "phase": "started", "round": 2, "call_index": 0,
    }}
    view = project_execution_view([event], request_id="request")
    assert len(view["steps"]) == 1
    step = view["steps"][0]
    assert "request" in step["step_id"] and "2" in step["step_id"]
    assert step["display_only"] is True
    assert step["ok"] is None and step["handler_executed"] is None and step["duration_ms"] is None
    assert not any(key in step for key in ("operation_id", "attempt_id", "idempotency_key"))


def test_raw_structured_progress_is_accepted_but_foreign_requests_are_not():
    events = [{"tool": "read_file", "round": 1, "call_index": 0, "phase": "started"},
              {**_progress(2, "started"), "request_id": "other-request"}]
    view = project_execution_view(events, request_id="request")
    assert len(view["steps"]) == 1 and view["steps"][0]["tool_name"] == "read_file"


def test_twenty_updates_remain_one_step_and_do_not_mutate_input():
    events = [_progress(0, "started"), *[_progress(0, "updated", label=f"读取 {index}") for index in range(20)],
              _progress(0, "failed", ok=False, handler_executed=False, duration_ms=0)]
    original = copy.deepcopy(events)
    view = project_execution_view(events, request_id="request")
    assert events == original and len(view["steps"]) == 1
    assert view["steps"][0]["ok"] is False and view["steps"][0]["handler_executed"] is False
    assert view["steps"][0]["duration_ms"] == 0
    snapshot = copy.deepcopy(view)
    first = render_progress_card(view)
    assert first == render_progress_card(view) and view == snapshot
    assert "action" not in first


def test_view_bounds_steps_and_label_and_keeps_unknown_total_unknown():
    events = [_progress(index, "updated", label="很长的中文🙂" * 10000, output="不应展示的工具正文") for index in range(8)]
    view = project_execution_view(events, request_id="request", max_steps=3)
    assert len(view["steps"]) == 3
    assert view["truncation"]["observed_steps"] == 8
    assert view["truncation"]["omitted_steps"] == 5
    assert view["total_steps"] is None
    assert all(len(step["label"].encode("utf-8")) <= 240 for step in view["steps"])
    text = _card_text(render_progress_card(view))
    assert "truncation" in text and "总数未知" in text and "%" not in text
    assert "不应展示的工具正文" not in text


@pytest.mark.parametrize("text", ["中文🙂" * 12000, '\\"\n\t' * 12000, "组合é\u0301和汉字" * 12000])
def test_long_multibyte_card_is_valid_collapsed_and_strictly_byte_bounded(text):
    snapshot = {"request_id": text, "source_revision": 20, "total_steps": None, "steps": [
        {"step_id": f"call-{index}", "tool_name": text, "phase": "updated", "ok": None,
         "handler_executed": None, "duration_ms": None, "label": text, "detail_ref": text}
        for index in range(200)
    ], "truncation": {"observed_steps": 200, "omitted_steps": 0}}
    before = copy.deepcopy(snapshot)
    card = render_progress_card(snapshot)
    encoded = _card_text(card).encode("utf-8")
    assert len(encoded) < 30_000
    assert len(json.dumps(card).encode("utf-8")) < 30_000
    assert json.loads(encoded)["schema"] == "2.0"
    panel = next(element for element in card["body"]["elements"] if element["tag"] == "collapsible_panel")
    assert panel["expanded"] is False and 0 < len(panel["elements"]) < 200
    assert "truncation" in _card_text(card) and "总数未知" in _card_text(card)
    assert snapshot == before


def test_unknown_phase_is_not_relabelled_as_completed():
    view = project_execution_view([_progress(0, "future_phase", ok=None)], request_id="request")
    assert len(view["steps"]) == 1
    assert view["steps"][0]["phase"] == "future_phase" and view["steps"][0]["ok"] is None
    assert "已完成" not in _card_text(render_progress_card(view))


@pytest.mark.parametrize("ok, label", [(True, "已完成"), (False, "失败"), (None, "结果未确认")])
def test_production_finished_phase_is_kept_after_late_started(ok, label):
    finished = {"tool": "read_file", "round": 1, "call_index": 0, "phase": "finished",
                "ok": ok, "handler_executed": True, "duration_ms": 25}
    view = project_execution_view([finished, {**finished, "phase": "started"}], request_id="request")
    assert view["steps"][0]["phase"] == "finished"
    assert view["steps"][0]["ok"] is ok
    assert label in _card_text(render_progress_card(view))


def test_approval_required_result_can_advance_to_original_call_success():
    view = project_execution_view([_call(0), _result(0, status="approval_required"), _result(0)], request_id="request")
    assert len(view["steps"]) == 1
    assert view["steps"][0]["phase"] == "succeeded"
    assert view["steps"][0]["handler_executed"] is True


def test_unknown_result_can_be_reconciled_without_inventing_completion():
    pending = project_execution_view([_result(0, status="unknown")], request_id="request")
    assert "已完成" not in _card_text(render_progress_card(pending))
    confirmed = project_execution_view([_result(0, status="unknown"), _result(0)], request_id="request")
    assert confirmed["steps"][0]["phase"] == "succeeded"
