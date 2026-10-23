# LLM: 仅测试数值遥测投影及真实线程CAS，不改模型预算、不调用供应商、不将正文放入遥测。
# 模块用途: 覆盖上下文计量来源字段与旧快照、主子线程隔离的兼容性。
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.context_usage import (
    capture_context_usage_binding,
    context_usage_from_thread,
    public_context_usage,
    record_model_context_usage,
)
from agent_py_agent.agent.conversation.store import ConversationStore


# LLM: 合成数字与原schema一致；不伪造真实provider usage或把缺失actual填零。
# 函数用途: 为新旧遥测提供可复算的最小输入。
def _usage(**extra):
    return {"schema": "model_visible_context_usage.v1", "estimated": True,
            "model_name": "test-model", "reasoning_level": "auto", "context_window_tokens": 1000,
            "compact_trigger_tokens": 900, "current_tokens": 100, "prompt_tokens": 40,
            "messages_tokens": 30, "runtime_guidance_tokens": 10, "tool_schema_tokens": 20,
            "protocol": "native", **extra}


# LLM: 旧快照只有预算数字不证明来源，不能把current反填成raw；原字段与白名单幂等性仍保留。
# 函数用途: 验证旧格式保留原数字但不编造计量来源。
def test_legacy_context_usage_keeps_unknown_attribution():
    legacy = _usage()
    public = public_context_usage(legacy)
    assert all(public[key] == value for key, value in legacy.items())
    assert public == legacy
    assert not (_PROVENANCE_FIELDS & public.keys())
    assert public_context_usage(public) == public


# LLM: 只有生产采样显式给出的非负整数raw与合法估算标签才能保留；不采信调用者自由counter/scope。
# 函数用途: 核验可信估算来源和等值校准标记。
@pytest.mark.parametrize(("extra", "raw", "kind"), [
    ({"raw_estimated_tokens": 200, "measurement_kind": "calibrated_estimate"}, 200, "calibrated_estimate"),
    ({"raw_estimated_tokens": 100, "measurement_kind": "calibrated_estimate"}, 100, "calibrated_estimate"),
    ({"raw_estimated_tokens": 100, "measurement_kind": "conservative_estimate"}, 100, "conservative_estimate"),
])
def test_context_usage_attribution_is_sanitized(extra, raw, kind):
    public = public_context_usage(_usage(**extra, counter_id="private-marker", count_scope="private-marker"))
    assert public.get("raw_estimated_tokens") == raw
    assert public.get("measurement_kind") == kind
    assert public.get("counter_id") == ("provider_calibrated_v1" if kind == "calibrated_estimate" else "conservative_cjk_utf8_v1")
    assert public.get("count_scope") == "full_outbound_request"
    assert "private-marker" not in str(public)


# LLM: 数字差异不是校准证据，bool不是真实计数，provider_actual不能由预检快照自称；未知字段不回显。
# 函数用途: 拒绝缺失、矛盾和坏形状的来源，保留已有预算数字。
@pytest.mark.parametrize("extra", [
    {"raw_estimated_tokens": 200}, {"measurement_kind": "calibrated_estimate"},
    {"raw_estimated_tokens": True, "measurement_kind": "calibrated_estimate"},
    {"raw_estimated_tokens": -1, "measurement_kind": "calibrated_estimate"},
    {"raw_estimated_tokens": 100.0, "measurement_kind": "conservative_estimate"},
    {"raw_estimated_tokens": float("inf"), "measurement_kind": "calibrated_estimate"},
    {"raw_estimated_tokens": "100", "measurement_kind": "conservative_estimate"},
    {"raw_estimated_tokens": 100, "measurement_kind": {"private-marker": True}},
    {"raw_estimated_tokens": 100, "measurement_kind": "provider_actual"},
    {"raw_estimated_tokens": 200, "measurement_kind": "conservative_estimate"},
])
def test_context_usage_does_not_infer_or_fabricate_provenance(extra):
    public = public_context_usage(_usage(**extra, counter_id="private-marker", count_scope="private-marker"))
    assert public["current_tokens"] == 100
    assert not (_PROVENANCE_FIELDS & public.keys())
    assert "private-marker" not in str(public)


# LLM: 数值扩展仍走原store身份和两代CAS；无关owner、旧模型revision、旧compact_generation不能获得新写权。
# 函数用途: 用真实store验证新增遥测字段往返以及主子绑定和迟到保护。
def test_extended_context_usage_roundtrips_and_remains_thread_fenced(tmp_path):
    store = ConversationStore(tmp_path / "owner")
    main = store.threads.get_or_create({"canonical_user_id": "main"})
    child = store.threads.get_or_create({"canonical_user_id": "child", "channel_conversation_id": "child"})
    agent = SimpleNamespace(conversation_store=store)
    params = SimpleNamespace(context_scope="default", task_attributes={"conversation_thread_id": main.thread_id,
                                                                      "agent_thread_id": child.thread_id})
    binding = capture_context_usage_binding(agent, params)
    public = public_context_usage(_usage(raw_estimated_tokens=200, measurement_kind="calibrated_estimate"))
    assert record_model_context_usage(agent, params, public, binding=binding) is True
    saved = store.threads.require(child.thread_id)
    assert context_usage_from_thread(saved) == public
    assert context_usage_from_thread(saved).get("raw_estimated_tokens") == 200
    assert store.threads.require(main.thread_id).model_context_usage == {}
    store.threads.update_atomic(child.thread_id, lambda row: replace(row, model_profile_id="new-model",
        model_selection_revision=1, model_selection_source="explicit", model_selection_last_explicit_revision=1))
    assert record_model_context_usage(agent, params, public, binding=binding) is False
    foreign = SimpleNamespace(conversation_store=ConversationStore(tmp_path / "other-owner"))
    assert record_model_context_usage(foreign, params, public, binding=binding) is False
    assert context_usage_from_thread(replace(saved, compact_generation=saved.compact_generation + 1)) == {}


_PROVENANCE_FIELDS = {"raw_estimated_tokens", "calibrated_tokens", "measurement_kind", "counter_id", "count_scope"}


# LLM: 五个真实消费者串接，不mock投影器；未知来源不填零，坏事件仍保持旧view model而不是清空状态。
# 函数用途: 验证来源经过Gateway、TUI两层清洗、view model及/context报告不丢失或冒造。
@pytest.mark.parametrize("known", [False, True])
def test_context_usage_provenance_survives_gateway_and_tui_consumers(known):
    from agent_py_agent.agent.gateway_parts.stream_events import public_context_usage_payload
    from agent_py_agent.cli.chat_parts.tui_block_renderer import render_tui_context_usage_report
    from agent_py_agent.cli.chat_parts.tui_runtime import (
        _normalize_context_usage,
        _tui_context_usage_payload,
    )
    from agent_py_agent.cli.chat_parts.tui_view_model import _context_usage_from_mapping

    original = _usage(raw_estimated_tokens=200, measurement_kind="calibrated_estimate") if known else _usage()
    expected = public_context_usage(original)
    for project in (public_context_usage_payload, _normalize_context_usage, _tui_context_usage_payload):
        actual = project({**original, "prompt": "private-marker"})
        assert actual == expected
        assert "private-marker" not in str(actual)
    model = _context_usage_from_mapping(_tui_context_usage_payload(_normalize_context_usage(
        public_context_usage_payload(original))), None)
    assert model is not None and model.current_tokens == 100
    report = render_tui_context_usage_report(model)
    if known:
        assert model.raw_estimated_tokens == 200 and model.calibrated_tokens == 100
        assert model.measurement_kind == "calibrated_estimate"
        assert model.counter_id == "provider_calibrated_v1" and model.count_scope == "full_outbound_request"
        assert "原始估算：200；当前预算估算：100 tokens" in report
        assert "计量来源：calibrated_estimate（provider_calibrated_v1）" in report
    else:
        assert model.raw_estimated_tokens is None and model.calibrated_tokens is None
        assert model.measurement_kind == model.counter_id == model.count_scope == ""
        assert "原始估算/校准来源：未知（旧快照或未提供）" in report
        assert "计量来源：" not in report
    assert _context_usage_from_mapping(None, model) is model


# LLM: 真实TUI store/reducer接收Gateway回执，随后旧快照到达不能沿用上次新来源；不打开终端或连接Gateway。
# 函数用途: 验证来源实际进入状态模型，旧记录再来时恢复未知而不是残留旧标签。
def test_tui_runtime_state_preserves_new_provenance_and_clears_it_for_legacy():
    from agent_py_agent.agent.gateway_parts.stream_events import public_context_usage_payload
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime

    runtime = TuiRuntime("usage-provenance")
    current = _usage(raw_estimated_tokens=200, measurement_kind="calibrated_estimate")
    runtime.update_background_activity(0, {"context_usage": public_context_usage_payload(current)})
    model = runtime.store.snapshot().status.context_usage
    assert model is not None and getattr(model, "raw_estimated_tokens", None) == 200
    runtime.update_background_activity(0, {"context_usage": public_context_usage_payload(_usage())})
    legacy = runtime.store.snapshot().status.context_usage
    assert legacy is not None and legacy.current_tokens == 100
    assert legacy.raw_estimated_tokens is None and legacy.measurement_kind == ""
