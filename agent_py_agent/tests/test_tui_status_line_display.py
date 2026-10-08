"""TUI 状态行布局（2026-10-08，07）：本轮模型轮 · 当轮工具 · 总缓存（两位小数） · 累计会话（只算输入） · 决策（关闭/观察/实际）
及成败 · [重试] · 输出 · [速度]；Context 行压缩点后面接模型名与思考档位。只造结构化快照，不发请求。"""
from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.model.context_pressure import _display_reasoning_level
from agent_py_agent.agent.contracts.model_call_ledger import ModelCallLedger
from agent_py_agent.agent.conversation.context_usage import public_context_usage
from agent_py_agent.agent.conversation.model_metrics import (
    public_model_metrics,
    publish_model_metrics,
)
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts.stream_events import public_context_usage_payload
from agent_py_agent.agent.settings.decision_settings_projection import (
    decision_summary_mode_from_read,
)
from agent_py_agent.agent.settings.decision_settings_schema import empty_decision_settings
from agent_py_agent.agent.settings.model_profiles import (
    SelectedModelRead,
    capture_selected_model_read,
)
from agent_py_agent.cli.chat_parts.tui_block_renderer import _render_context_usage
from agent_py_agent.cli.chat_parts.tui_model_metrics import render_model_metrics
from agent_py_agent.cli.chat_parts.tui_runtime import (
    _normalize_context_usage,
    _tui_context_usage_payload,
)
from agent_py_agent.cli.chat_parts.tui_view_model import (
    TuiContextUsage,
    _context_usage_from_mapping,
)


# 函数用途: 用给定字段渲染一条统计行并取出纯文本（宽度足够，不裁剪）。
def _line(**fields) -> str:
    metrics = {"schema": "model_runtime_metrics.v1", "totals_known": True, **fields}
    return "".join(part[1] for line in render_model_metrics(metrics, 400) for part in line)


def test_line_order_cache_two_decimals_session_input_and_output():
    text = _line(model_rounds=34, tool_count=1, input_tokens=796_640, cache_read_input_tokens=684_160,
                 cache_read_reported_calls=21, output_tokens=6_118, decision_mode="observe", output_tps=33.25)
    assert text == "  ▤ 本轮模型轮 34 · 当轮工具 1 · 总缓存 85.88% · 累计会话 796.6k · 决策（观察） · 输出 6.1k · 速度 33.2 tok/s"


def test_session_total_counts_input_only_and_marks_local_estimates():
    assert "累计会话 900 · 决策 · 输出 500" in _line(input_tokens=900, output_tokens=500)
    assert "累计会话 ~940 · " in _line(input_tokens=900, estimated_tokens=40, output_tokens=500)
    assert "累计会话 未知" in _line(input_tokens=900, totals_known=False)
    assert "总缓存 —" in _line(input_tokens=900, cache_percent=50.0), "供应商一次都没回报缓存时不拿最近值冒充"


def test_decision_mode_label_in_brackets():
    assert "决策（关闭） · 输出 0" in _line(decision_mode="off")
    assert "决策（实际） · 输出 0" in _line(decision_mode="apply")
    text = _line(decision_mode="observe", decision_call_count=1, decision_success_count=1, decision_input_reported_calls=1,
                 decision_input_tokens=120, decision_unfinished_calls=0, decision_unknown_failures=0)
    assert "决策（观察） 成功 1 · 失败 0" in text and "token" not in text.split("决策")[1], "决策只看个数，不看 token"
    # 旧快照 / 未知值：不带括号，也不猜
    assert "决策 · 输出 0" in _line() and "决策 · 输出 0" in _line(decision_mode="maybe")
    assert "decision_mode" not in public_model_metrics({"schema": "model_runtime_metrics.v1", "decision_mode": "maybe"})


def test_retry_and_pending_and_llm_segment():
    text = _line(retry_count=2, pending=True, failure_count=1, unfinished_calls=1, unfinished_tokens=5000)
    assert " · 重试 2 · 输出 0" in text and "待结算" not in text and "LLM" not in text and "估算" not in text


def test_narrow_width_keeps_order_and_bound():
    text = "".join(part[1] for line in render_model_metrics(
        {"schema": "model_runtime_metrics.v1", "totals_known": True, "model_rounds": 3, "input_tokens": 1000}, 60) for part in line)
    assert text.startswith("  ▤ 本轮 3 · 当轮工具 — · 总缓存 —") and len(text) <= 61


# 函数用途: 造一份"总开关开、planning 点位 observe"的 owner 决策设置。
def _owner_settings(**overrides) -> dict:
    settings = empty_decision_settings()
    settings["overrides"] = dict(overrides)
    return settings


def test_summary_mode_folds_thread_points():
    config = SimpleNamespace()  # 全部走默认：总开关默认关
    thread = SimpleNamespace(decision_settings=empty_decision_settings())
    assert decision_summary_mode_from_read(config, empty_decision_settings(), thread) == "off"
    observe = _owner_settings(**{"enabled": True, "points.planning.mode": "observe"})
    assert decision_summary_mode_from_read(config, observe, thread) == "observe"
    apply = _owner_settings(**{"enabled": True, "points.planning.mode": "observe", "points.action_candidate.mode": "apply"})
    assert decision_summary_mode_from_read(config, apply, thread) == "apply"
    # 线程覆盖只对线程范围点位生效：线程把 planning 关掉，owner 的 observe 就不算
    closed = SimpleNamespace(decision_settings=_owner_settings(**{"points.planning.mode": "off"}))
    assert decision_summary_mode_from_read(config, observe, closed) == "off"
    # 总开关关则全部 off，不看点位
    assert decision_summary_mode_from_read(config, _owner_settings(**{"enabled": False, "points.planning.mode": "apply"}), thread) == "off"


def test_publish_carries_decision_mode_only_inside_the_captured_scope(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "u", "owner_id": "u"})
    agent = SimpleNamespace(conversation_store=store, _model_call_ledger=ModelCallLedger(), config=SimpleNamespace())
    params = SimpleNamespace(request_id="now", run_id="now", live_archive_state={},
                             task_attributes={"conversation_thread_id": thread.thread_id})
    with capture_selected_model_read() as captured:
        captured.append(SelectedModelRead("p1", _owner_settings(**{"enabled": True, "points.planning.mode": "observe"})))
        assert publish_model_metrics(agent, params, pending=True)["decision_mode"] == "observe"
    # 作用域外没有捕获：不沿用旧值（用户关了决策不能还显示"观察"）
    assert "decision_mode" not in publish_model_metrics(agent, params, pending=True)


def test_context_line_appends_model_and_thinking():
    base = dict(current_tokens=59_000, context_window_tokens=272_000, compact_trigger_tokens=60_000)
    line = "".join(part[1] for part in _render_context_usage(
        TuiContextUsage(**base, model_name="gpt-6.1-sol", reasoning_level="xhigh"), 160, compact_count=3)[0])
    assert line.endswith("· compact 3 · 压缩点 22% · gpt-6.1-sol · 思考 xhigh")
    off = "".join(part[1] for part in _render_context_usage(
        TuiContextUsage(**base, model_name="MiniMax-M2.7", reasoning_level="off"), 160)[0])
    assert off.endswith("· MiniMax-M2.7 · 思考 关")
    plain = "".join(part[1] for part in _render_context_usage(TuiContextUsage(**base), 160)[0])
    assert plain.endswith("· 压缩点 22%"), "旧 Gateway 没有标签时原行不变"


def test_context_usage_labels_survive_public_copy_and_reducer():
    raw = {"schema": "model_visible_context_usage.v1", "estimated": True, "current_tokens": 10, "context_window_tokens": 100,
           "compact_trigger_tokens": 90, "protocol": "native", "model_name": "  gpt-6-astra ", "reasoning_level": "x" * 100}
    public = public_context_usage(raw)
    assert public["model_name"] == "gpt-6-astra" and len(public["reasoning_level"]) == 80
    assert public_context_usage({**raw, "model_name": ["not", "a", "label"]})["model_name"] == ""
    usage = _context_usage_from_mapping(public, None)
    assert usage.model_name == "gpt-6-astra" and usage.reasoning_level == "x" * 80
    # Gateway 事件清洗也要放行这两个短标签（否则 TUI 永远收不到），同样只收字符串、截 80
    gateway = public_context_usage_payload(raw)
    assert gateway["model_name"] == "gpt-6-astra" and len(gateway["reasoning_level"]) == 80
    assert public_context_usage_payload({**raw, "model_name": {"text": "x"}})["model_name"] == ""
    assert _context_usage_from_mapping(gateway, None).model_name == "gpt-6-astra"
    # TUI 运行时的两份拷贝（后台 main 快照、本地事件载荷）也要带标签，否则真机 Context 行看不到
    assert _normalize_context_usage(gateway)["model_name"] == "gpt-6-astra"
    assert _tui_context_usage_payload(gateway)["reasoning_level"] == "x" * 80
    assert _tui_context_usage_payload({**gateway, "model_name": 7})["model_name"] == ""


def test_display_reasoning_level_matches_what_is_sent():
    agent = SimpleNamespace(config=SimpleNamespace(model_reasoning_effort="low"), backend=SimpleNamespace(reasoning_control="effort"))
    assert _display_reasoning_level(agent, None) == "low"
    agent.backend.reasoning_control = "none"
    assert _display_reasoning_level(agent, None) == "auto", "后端不接受档位时交给模型默认"
    agent = SimpleNamespace(config=SimpleNamespace(model_reasoning_effort="off"), backend=SimpleNamespace(reasoning_control="effort"))
    assert _display_reasoning_level(agent, None) == "off"
    assert _display_reasoning_level(SimpleNamespace(), None) == "auto"
