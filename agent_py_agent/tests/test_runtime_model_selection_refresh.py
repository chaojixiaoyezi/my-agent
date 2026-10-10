"""请求安全点选模：真实 Gateway/工具/协议配假 HTTP，全部任务内容为合成夹具。"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.agent_core.model.call_runtime import model_call_ledger
from agent_py_agent.agent.gateway_parts import request_execution
from agent_py_agent.agent.settings.model_profiles import (
    execute_model_profile_operation,
    selected_model_config,
)
from agent_py_agent.agent.settings.model_scope import (
    selected_model_scope,
    thread_model_execution_status,
)
from agent_py_agent.agent.tooling._filesystem_read import ReadFileTool
from agent_py_agent.tests.test_gateway_model_adoption import actual_request, fake_http
from agent_py_agent.tests.test_model_profiles import add
from agent_py_agent.tests.test_thread_model_selection import host_with_store


@pytest.mark.parametrize("selection_during", ["response", "tool"])
def test_next_real_request_adopts_selection_after_old_tool_result(tmp_path, monkeypatch, selection_during):
    fixture = actual_request(tmp_path, mode="off", tools=True)
    material = Path(fixture.agent.home_paths.owner_home_dir) / "material.txt"
    material.parent.mkdir(parents=True, exist_ok=True)
    material.write_text("合成材料：条目 001 的核验值为 73。")
    selected, _ = add(fixture.agent, model_name="selected-latest", temperature=0.3,
                      model_context_window_tokens=96000)
    original_read = ReadFileTool.execute
    reads = []

    def select():
        execute_model_profile_operation(fixture.agent, "select", {"profile_id": selected}, thread_id=fixture.thread_id)

    def read(tool, params):
        reads.append(fixture.agent.config.model_name)
        if selection_during == "tool":
            select()
        return original_read(tool, params)

    def during_request(wire):
        if wire["model"] == "original-model" and selection_during == "response":
            select()
            status = thread_model_execution_status(fixture.agent, fixture.thread_id)
            assert status["pending"] is True
            assert status["bound"]["model_name"] == "original-model"
            assert status["last_request"]["model_name"] == "original-model"

    monkeypatch.setattr(ReadFileTool, "execute", read)
    business, _ = fake_http(monkeypatch, fixture, on_business=during_request, use_tool=True, read_path=material)
    result = request_execution._run_gateway_ask(fixture.context)
    assert result.response == "资料整理完成。"
    assert reads == ["original-model"], [block for wire, _ in business for message in wire.get("messages", [])
                                        for block in message.get("content", []) if isinstance(block, dict)
                                        and block.get("type") == "tool_result"]
    assert [wire["model"] for wire, _ in business] == ["original-model", "selected-latest"]
    assert business[1][0]["temperature"] == 0.3
    assert reads == ["original-model"]
    assert "条目 001" in json.dumps(business[1][0], ensure_ascii=False)
    records = [row for row in model_call_ledger(fixture.agent).records()
               if row.request_id == fixture.context.request_id and row.model == "selected-latest"
               and row.metadata.get("model_selection")]
    assert len(records) == 1
    provenance = records[0].metadata["model_selection"]
    assert provenance["profile_id"] == selected
    assert provenance["revision"] == business[1][1].model_selection_revision
    assert provenance["adopted_at"] >= provenance["selected_at"]
    assert fixture.agent.config.model_backend == "echo"
    status = thread_model_execution_status(fixture.agent, fixture.thread_id)
    assert status["last_request"]["model_name"] == "selected-latest"
    assert status["last_request"]["status"] == "finished"


def test_rapid_explicit_choices_adopt_latest_without_executing_intermediate(tmp_path, monkeypatch):
    fixture = actual_request(tmp_path, mode="off", tools=True)
    material = Path(fixture.agent.home_paths.owner_home_dir) / "material.txt"
    material.parent.mkdir(parents=True, exist_ok=True)
    material.write_text("合成核验材料")
    middle, _ = add(fixture.agent, model_name="middle")
    latest, _ = add(fixture.agent, model_name="latest")
    def select(wire):
        if wire["model"] == "original-model":
            for key in (middle, latest):
                execute_model_profile_operation(fixture.agent, "select", {"profile_id": key}, thread_id=fixture.thread_id)
    business, probes = fake_http(monkeypatch, fixture, on_business=select, use_tool=True, read_path=material)
    request_execution._run_gateway_ask(fixture.context)
    assert [wire["model"] for wire, _ in business] == ["original-model", "latest"]
    assert "middle" not in [wire["model"] for wire in probes]
    assert "合成核验材料" in json.dumps(business[1][0], ensure_ascii=False)


def test_nested_profile_resolution_uses_deployment_defaults(tmp_path):
    host = host_with_store(tmp_path)
    original_temperature = host.config.temperature
    a, _ = add(host, model_name="A", temperature=0.12)
    b, _ = add(host, model_name="B")
    from agent_py_agent.agent.settings.thread_model_selection import execute_local_model_operation

    thread = execute_local_model_operation(host, "s", "select", {"profile_id": a})
    with selected_model_scope(host, thread_id=thread["thread_id"]):
        assert float(host.config.temperature) == 0.12
        assert selected_model_config(host, profile_id=b).temperature == original_temperature
        assert selected_model_config(host, profile_id="default").model_name == "deployment-model"


def test_model_selection_time_is_bound_to_selection_transaction(tmp_path):
    host = host_with_store(tmp_path)
    from agent_py_agent.agent.settings.thread_model_selection import execute_local_model_operation

    row = execute_local_model_operation(host, "s", "select", {"profile_id": "default"})
    thread = host.conversation_store.threads.require(row["thread_id"])
    assert thread.model_selection_selected_at > 0
    changed = host.conversation_store.threads.update_atomic(thread.thread_id, lambda value: replace(value, summary="synthetic"))
    assert changed.model_selection_selected_at == thread.model_selection_selected_at
    legacy = thread.to_dict()
    legacy.pop("model_selection_selected_at")
    assert type(thread).from_dict(legacy).model_selection_selected_at is None


def test_switch_between_native_backends_rebuilds_request_for_smaller_window(tmp_path, monkeypatch):
    fixture = actual_request(tmp_path, mode="off", tools=True)
    material = Path(fixture.agent.home_paths.owner_home_dir) / "cross-backend.txt"
    material.parent.mkdir(parents=True, exist_ok=True)
    material.write_text("保留已经执行的工具结果，条目 cross-001。")
    selected, _ = add(fixture.agent, model_name="new-native", model_backend="openai_compatible",
                      model_context_window_tokens=64000, temperature=0.2)
    def choose(wire):
        if len(business) == 1:
            execute_model_profile_operation(fixture.agent, "select", {"profile_id": selected}, thread_id=fixture.thread_id)
    business, _ = fake_http(monkeypatch, fixture, on_business=choose, use_tool=True, read_path=material)
    request_execution._run_gateway_ask(fixture.context)
    assert len(business) == 2
    assert "system" in business[0][0] and "system" not in business[1][0]
    assert business[1][0]["model"] == "new-native" and business[1][0]["temperature"] == 0.2
    assert any(row.get("role") == "tool" and "cross-001" in row["content"] for row in business[1][0]["messages"])
    assert thread_model_execution_status(fixture.agent, fixture.thread_id)["last_request"]["backend"] == "openai_compatible"


def test_explicit_choice_changed_during_probe_adopts_newest_generation(tmp_path, monkeypatch):
    fixture = actual_request(tmp_path, mode="off", tools=True)
    middle, _ = add(fixture.agent, model_name="middle-racing")
    latest, _ = add(fixture.agent, model_name="latest-racing")
    material = Path(fixture.agent.home_paths.owner_home_dir) / "race.txt"
    material.parent.mkdir(parents=True, exist_ok=True)
    material.write_text("合成竞态材料")
    def select_middle(wire):
        if len(business) == 1:
            execute_model_profile_operation(fixture.agent, "select", {"profile_id": middle}, thread_id=fixture.thread_id)
    def select_latest():
        execute_model_profile_operation(fixture.agent, "select", {"profile_id": latest}, thread_id=fixture.thread_id)
    business, probes = fake_http(monkeypatch, fixture, on_business=select_middle, before_probe=select_latest,
                                use_tool=True, read_path=material)
    request_execution._run_gateway_ask(fixture.context)
    assert [wire["model"] for wire, _ in business] == ["original-model", "latest-racing"]
    assert any(wire["model"] == "middle-racing" for wire in probes)


def test_selection_adoption_clears_old_one_call_reasoning_override(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.model import selection_refresh
    from agent_py_agent.agent.agent_core.model.selection_refresh import refresh_selected_model
    from agent_py_agent.agent.settings.model_scope import model_dependency_lifetime
    from agent_py_agent.agent.settings.reasoning_effort import (
        next_call_reasoning_level,
        set_next_call_reasoning_level,
    )
    from agent_py_agent.tests.test_model_selection_history import _params

    host = host_with_store(tmp_path)
    monkeypatch.setattr(selection_refresh, "select_tool_protocol", lambda *args, **kwargs: None)
    from agent_py_agent.agent.settings.thread_model_selection import execute_local_model_operation
    row = execute_local_model_operation(host, "reasoning", "select", {"profile_id": "default"})
    params = replace(_params(live_archive_state={}), task_attributes={"conversation_thread_id": row["thread_id"]})
    with selected_model_scope(host, thread_id=row["thread_id"]), model_dependency_lifetime(host):
        assert set_next_call_reasoning_level(params, "high")
        refresh_selected_model(host, params)
        assert next_call_reasoning_level(params) == "high"
        execute_local_model_operation(host, "reasoning", "select", {"profile_id": "default"})
        refresh_selected_model(host, params)
        assert next_call_reasoning_level(params) == ""


def test_deleted_selected_profile_does_not_send_next_request_with_old_or_default_model(tmp_path, monkeypatch):
    from agent_py_agent.agent.settings.model_profiles import ModelProfileError

    fixture = actual_request(tmp_path, mode="off", tools=True)
    selected, _ = add(fixture.agent, model_name="deleted-before-adoption")
    material = Path(fixture.agent.home_paths.owner_home_dir) / "deleted.txt"
    material.parent.mkdir(parents=True, exist_ok=True)
    material.write_text("合成引用失效材料")
    def select_and_delete(wire):
        execute_model_profile_operation(fixture.agent, "select", {"profile_id": selected}, thread_id=fixture.thread_id)
        execute_model_profile_operation(fixture.agent, "delete_model", {"profile_id": selected})
    business, _ = fake_http(monkeypatch, fixture, on_business=select_and_delete, use_tool=True, read_path=material)
    with pytest.raises(ModelProfileError):
        request_execution._run_gateway_ask(fixture.context)
    assert [wire["model"] for wire, _ in business] == ["original-model"]
