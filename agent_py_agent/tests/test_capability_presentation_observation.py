"""能力推荐观测落进 Gateway 请求记录：只写码、版本、名称与计数，一个回合一次决策一条；不改展示回调、不含正文。"""
import json

import pytest

from agent_py_agent.agent.backends.errors import ProviderTransientError
from agent_py_agent.agent.gateway_parts import request_execution
from agent_py_agent.agent.gateway_parts.request_binding import (
    CAPABILITY_OBSERVATION_KEY,
    record_capability_presentation_observation,
)
from agent_py_agent.tests.test_decision_capability_consumer import provider
from agent_py_agent.tests.test_decision_settings import patch
from agent_py_agent.tests.test_gateway_capability_compact import (  # noqa: F401
    capture_gateway_loop,
    gateway_surface,
    tool_surface,
)

_ALLOWED = {"schema", "point", "operation_id", "mode", "status", "reason", "adopted", "retain_reason",
            "candidates_revision", "question_count", "shortlist_tool_names", "deferred_tool_names",
            "selected_skill_count", "required_skill_count"}


# LLM: 只读取请求记录文件里的观测块；和内存中的 request 对比，确认两边一致。
# 函数用途: 返回本请求记录里的观测条目列表，没有观测时返回 None。
def _entries(fixture):
    stored = json.loads(fixture.context.request_path.read_text(encoding="utf-8"))
    block = stored.get(CAPABILITY_OBSERVATION_KEY)
    assert block == fixture.context.request.get(CAPABILITY_OBSERVATION_KEY), "文件与内存中的请求记录必须一致"
    assert "capability_presentation" not in stored, "携带的展示值不能落盘"
    return None if block is None else block["entries"]


@pytest.mark.parametrize("choice", [None, "abstain"])
def test_gateway_turn_records_one_observation_for_its_one_decision(gateway_surface, monkeypatch, choice):  # noqa: F811
    calls = provider(monkeypatch, choice=choice)
    capture_gateway_loop(monkeypatch)
    request_execution._run_gateway_ask(gateway_surface.context)
    entries = _entries(gateway_surface)
    assert len(calls) == 1 and len(entries) == 1, "同一回合经过溢出重试也只有一次决策、一条观测"
    entry = entries[0]
    assert set(entry) <= _ALLOWED and entry["point"] == "skill_tool" and entry["mode"] == "apply"
    assert entry["status"] == "success" and entry["question_count"] >= 1 and entry["candidates_revision"]
    assert "核对来源" not in json.dumps(entry, ensure_ascii=False), "观测不含用户正文"
    if choice is None:
        assert entry["adopted"] is True and entry["retain_reason"] == ""
        assert entry["selected_skill_count"] == 1
        assert all(isinstance(name, str) for name in entry["shortlist_tool_names"] + entry["deferred_tool_names"])
    else:
        assert entry["adopted"] is False and entry["retain_reason"] == "abstain"
        assert "shortlist_tool_names" not in entry


def test_failed_decision_is_observed_with_its_structured_reason(gateway_surface, monkeypatch):  # noqa: F811
    provider(monkeypatch, fail=ProviderTransientError("temporary"))
    capture_gateway_loop(monkeypatch)
    request_execution._run_gateway_ask(gateway_surface.context)
    (entry,) = _entries(gateway_surface)
    assert (entry["adopted"], entry["status"], entry["reason"]) == (False, "error", "provider_failed")


def test_no_decision_means_no_observation(gateway_surface, monkeypatch):  # noqa: F811
    patch(gateway_surface.agent, {"points.skill_tool.mode": "off"})
    calls = provider(monkeypatch)
    capture_gateway_loop(monkeypatch)
    request_execution._run_gateway_ask(gateway_surface.context)
    assert calls == [] and _entries(gateway_surface) is None


def test_writer_keeps_other_keys_bounds_entries_and_mirrors_memory(gateway_surface):  # noqa: F811
    context = gateway_surface.context
    for index in range(10):
        record_capability_presentation_observation(context, {"schema": "capability_presentation_observation.v1", "n": index})
    stored = json.loads(context.request_path.read_text(encoding="utf-8"))
    assert [entry["n"] for entry in stored[CAPABILITY_OBSERVATION_KEY]["entries"]] == list(range(2, 10))
    assert stored["prompt"] == "核对来源" and stored["execution_attempt_id"] == "gateway-turn-1"
    assert context.request[CAPABILITY_OBSERVATION_KEY] == stored[CAPABILITY_OBSERVATION_KEY]


def test_writer_drops_on_io_failure_but_propagates_a_closed_turn(gateway_surface, monkeypatch):  # noqa: F811
    from agent_py_agent.agent.gateway_parts import request_binding

    context = gateway_surface.context
    stored = json.loads(context.request_path.read_text(encoding="utf-8"))
    context.request_path.write_text(json.dumps({**stored, "execution_attempt_id": "another-attempt"}), encoding="utf-8")
    with pytest.raises(InterruptedError):
        record_capability_presentation_observation(context, {"n": 1})
    context.request_path.write_text(json.dumps(stored), encoding="utf-8")

    def full_disk(*_args, **_kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(request_binding, "update_json_file_atomic", full_disk)
    record_capability_presentation_observation(context, {"n": 2})
    assert CAPABILITY_OBSERVATION_KEY not in json.loads(context.request_path.read_text(encoding="utf-8"))
    assert CAPABILITY_OBSERVATION_KEY not in context.request
