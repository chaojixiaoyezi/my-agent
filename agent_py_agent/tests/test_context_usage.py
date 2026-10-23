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


# LLM: 旧快照缺raw时按current解释，白名单幂等；新增字段不能改变任一旧字段的值。
# 函数用途: 验证旧格式回读的默认来源与公开投影稳定性。
def test_legacy_context_usage_gets_conservative_attribution():
    legacy = _usage()
    public = public_context_usage(legacy)
    assert all(public[key] == value for key, value in legacy.items())
    assert public.get("raw_estimated_tokens") == public["current_tokens"]
    assert public.get("calibrated_tokens") == public["current_tokens"]
    assert public.get("measurement_kind") == "conservative_estimate"
    assert public.get("counter_id") == "conservative_cjk_utf8_v1"
    assert public.get("count_scope") == "full_outbound_request"
    assert public_context_usage(public) == public


# LLM: 等值校准也由已验证标签保留；非法标签按数字关系推断，不接受自由正文/任意counter作为来源。
# 函数用途: 核验来源枚举、raw清洗及counter白名单。
@pytest.mark.parametrize(("extra", "raw", "kind"), [
    ({"raw_estimated_tokens": 200}, 200, "calibrated_estimate"),
    ({"raw_estimated_tokens": 100, "measurement_kind": "calibrated_estimate"}, 100, "calibrated_estimate"),
    ({"raw_estimated_tokens": True, "measurement_kind": "private-marker"}, 0, "calibrated_estimate"),
    ({"raw_estimated_tokens": float("inf")}, 0, "calibrated_estimate"),
    ({"measurement_kind": {"unexpected": "private-marker"}}, 100, "conservative_estimate"),
])
def test_context_usage_attribution_is_sanitized(extra, raw, kind):
    public = public_context_usage(_usage(**extra, counter_id="private-marker", count_scope="private-marker"))
    assert public.get("raw_estimated_tokens") == raw
    assert public.get("measurement_kind") == kind
    assert public.get("counter_id") == ("provider_calibrated_v1" if kind == "calibrated_estimate" else "conservative_cjk_utf8_v1")
    assert public.get("count_scope") == "full_outbound_request"
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
    public = public_context_usage(_usage(raw_estimated_tokens=200))
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
