"""能力包跨真实 Compact、分页续读和递归授权；仅模型后端使用内存替身。"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
from agent_py_agent.agent.backends import http
from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.backends.errors import ProviderContextWindowError
from agent_py_agent.agent.capability.skill_search_tool import SkillSearchTool
from agent_py_agent.agent.capability.task_references import task_skill_references
from agent_py_agent.agent.gateway_parts import request_execution
from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.plugin_content_activation import PluginContentActivation
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_installation import PluginInstallRequest
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.runtime_context import (
    restore_current_subagent_context,
    set_current_subagent_context,
)
from agent_py_agent.agent.subagents.services.hierarchy.scheduler_models import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)
from agent_py_agent.agent.subagents.services.lifecycle import RecordCapabilityGrantParams
from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
from agent_py_agent.tests.test_capability_package import content_bundle
from agent_py_agent.tests.test_capability_package_task_refs import _agent
from agent_py_agent.tests.test_gateway_model_adoption import actual_request, fake_http
from agent_py_agent.tests.test_gateway_model_observation import install_backend
from agent_py_agent.tests.test_subagent_capability_compact import append_prior_turn
from agent_py_agent.tests.test_subagent_runtime_compact import _OverflowThenCompleteChildBackend


# LLM: 使用真实 owner 安装库及内容 activation；不替换 core、SkillsService 或逐轮快照。
# 函数用途: 在隔离 home 安装可分页资源，供 Compact 故障注入复用。
def _install(agent, package_id, body):
    agent.config.enable_plugins = True
    store = PluginInstallStore(resolve_owner_home(agent.home_paths.root))
    package = inspect_plugin_package(content_bundle(
        files={"CAPABILITY.md": body.encode()},
        change=lambda row: row.update(plugin_id=package_id),
    ))
    installed = store.install(PluginInstallRequest(package, "install-" + package_id, 0)).installation
    activation = PluginContentActivation("enable-" + package_id, package_id, installed.package_sha256,
                                        installed.revision, installed.settings_revision)
    active = store.change_activation(PluginActivationRequest(activation.operation_id, installed.revision, activation)).installation
    return store, active


# LLM: 同字节重新激活也必须产生新代次；按原 revoke/release/enable 事务推进，不手写安装 JSON。
# 函数用途: 在确切模型发送窗口制造版本失效，验证旧任务不能静默重绑。
def _reactivate(agent, store, active):
    revoked = store.change_activation(PluginActivationRequest("revoke-original", active.revision,
                                                               replace(active.activation, phase="revoked"))).installation
    released, _ = store.release_activation(resolve_owner_home(agent.home_paths.root), None, revoked, "revoke-original")
    row = released.installation
    activation = PluginContentActivation("enable-again", row.manifest.plugin_id, row.package_sha256,
                                        row.revision, row.settings_revision)
    return store.change_activation(PluginActivationRequest(activation.operation_id, row.revision, activation)).installation


# LLM: 观察原 handler 的输入输出，不替代校验、pin、工具权限或模型上下文；这里只捕获真实执行证据。
# 函数用途: 保留分页参数与结构化结果，以便 fake 后端原样携带 continuation。
def _capture_search(monkeypatch):
    calls = []
    original = SkillSearchTool.execute

    def execute(self, params):
        result = original(self, params)
        calls.append((dict(params), result))
        return result

    monkeypatch.setattr(SkillSearchTool, "execute", execute)
    return calls


# LLM: 只在原供应商 HTTP 边界返回一项原生调用，仍由真实 adapter 和工具主链解释执行。
# 函数用途: 为隔离的 Anthropic/OpenAI 测试配置生成相同 skill_search 动作。
def _tool_response(wire, call_id, arguments):
    tool = {"type": "tool_use", "id": call_id, "name": "skill_search", "input": arguments}
    if "system" in wire:
        return {"content": [tool], "stop_reason": "tool_use", "usage": {"input_tokens": 101, "output_tokens": 5}}
    return {"choices": [{"message": {"content": "", "tool_calls": [{
        "id": call_id, "type": "function", "function": {"name": "skill_search", "arguments": json.dumps(arguments)},
    }]}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 101, "completion_tokens": 5}}


@pytest.mark.parametrize("rotation", ["none", "before_reprepare", "after_compact"])
def test_main_package_pages_keep_original_task_pin_across_real_compact(tmp_path, monkeypatch, rotation):
    fixture = actual_request(tmp_path, mode="disabled", tools=True)
    agent = fixture.agent
    body = "第一页原文。" * 50 + "第二页仍属于同一个包。" * 50
    store, active = _install(agent, "story-pages", body)
    expected = agent.current_skill_snapshot().resolve_package("story-pages").to_ref()
    install_backend(monkeypatch, fixture)
    calls = _capture_search(monkeypatch)
    events, pins = [], []

    def on_business(_wire):
        number = len(business)
        events.append(number)
        if number == 2:
            assert len(calls) == 1 and calls[0][1].ok
            tasks = agent.conversation_store.tasks.list(fixture.thread_id)
            assert len(tasks) == 1 and tasks[0].skill_snapshot_refs == (expected,)
            pins.append(tasks[0])
            if rotation == "before_reprepare":
                renewed = _reactivate(agent, store, active)
                assert renewed.package_sha256 == active.package_sha256
                assert renewed.activation_id != active.activation_id
            raise ProviderContextWindowError("测试能力包第一页后的上下文溢出")
        if number == 4:
            assert agent.conversation_store.threads.require(fixture.thread_id).compact_generation == 1
            if rotation == "after_compact":
                _reactivate(agent, store, active)

    business, _ = fake_http(monkeypatch, fixture, on_business=on_business)
    original_http = http.post_json

    def send(request):
        response = original_http(request)
        if events and events[-1] in {1, 4}:
            names = [row.get("name") or row.get("function", {}).get("name") for row in request.payload.get("tools", [])]
            if names != ["my_agent_capability_probe"]:
                arguments = ({"action": "get", "package_id": "story-pages", "max_chars": 300} if events[-1] == 1
                             else json.loads(calls[0][1].output)["continuation"])
                return _tool_response(request.payload, "page-" + str(events[-1]), arguments)
        return response

    monkeypatch.setattr(http, "post_json", send)
    result = request_execution._run_gateway_ask(fixture.context)
    assert result.response == "资料整理完成。"
    assert events == [1, 2, 3, 4, 5]
    assert len(calls) == 2
    first = json.loads(calls[0][1].output)
    second = calls[1][1]
    assert calls[1][0] == first["continuation"]
    assert first["body"] == body[:300]
    if rotation == "none":
        assert second.ok, second.output
        page = json.loads(second.output)
        assert page["body"] == body[300:600] and page["offset"] == 300
        assert page["source_ref"] == first["source_ref"]
    else:
        assert not second.ok and "body" not in json.loads(second.output)
        expected_error = ("CAPABILITY_PACKAGE_NOT_AVAILABLE" if rotation == "before_reprepare"
                          else "CAPABILITY_RESOURCE_UNAVAILABLE package=story-pages")
        assert json.loads(second.output)["error"] == expected_error
        if rotation == "before_reprepare":
            for wire, _thread in business[2:]:
                assert "CAPABILITY_PACKAGE_PIN_STALE" in json.dumps(wire, ensure_ascii=False)
    saved = agent.conversation_store.tasks.load(pins[0].task_id)
    assert saved.skill_snapshot_refs == (expected,)
    assert saved.status == "completed"


def test_child_compact_and_grandchild_keep_later_canonical_package_grant(tmp_path, monkeypatch):
    agent, _store, _entries = _agent(tmp_path)
    _install(agent, "story-c", "未获授权的内部正文")
    refs = {row.package_id: row.to_ref() for row in agent.current_skill_snapshot().packages}
    result = CreateSubagentsTool(agent).execute({"goal": "先按已授权故事方法整理来源", "allowed_skills": ["capability:story-a"],
                                                "allowed_tools": ["skill_search"], "defer_start": True})
    assert result.ok, result.output
    task = agent.subagents.list_runs()[0]
    original_refs = list(task.attributes["skill_snapshot_refs"])
    agent.subagents.lifecycle.record_capability_grant(task.id, params=RecordCapabilityGrantParams(
        request_id="grant-b", skills=["capability:story-b"], capability_cards=[refs["story-b"]], reason="补充范围内的资料包",
    ))
    append_prior_turn(agent, task.agent_thread_id)
    calls = _capture_search(monkeypatch)

    class Backend(_OverflowThenCompleteChildBackend):
        def generate(self, prompt, on_chunk=None, **kwargs):
            response = super().generate(prompt, on_chunk=on_chunk, **kwargs)
            if len(self.model_prompts) == 2 and "You maintain a conversation summary" not in prompt:
                return ModelResponse(text="读取已经授予的资料。", backend=self.name, tool_use_blocks=[{
                    "id": "read-granted-b", "name": "skill_search", "input": {"action": "get", "package_id": "story-b"},
                }])
            return response

    agent.backend = Backend()
    completed = agent.run_subagent(task.id, dry_run=False, probe=False)
    assert completed.ok, completed
    assert len(agent.backend.summary_prompts) == 1
    assert len(agent.backend.model_prompts) == 3
    assert len(calls) == 1 and calls[0][1].ok, calls
    assert json.loads(calls[0][1].output)["activation_id"] == refs["story-b"]["activation_id"]
    assert agent.conversation_store.threads.require(task.agent_thread_id).compact_generation == 1
    parent = agent.subagents.load(task.id)
    assert parent.attributes["skill_snapshot_refs"] == original_refs
    assert {row["package_id"] for row in task_skill_references(parent)} == {"story-a", "story-b"}
    denied = agent.subagents.hierarchy.schedule_child_runs(params=HierarchyScheduleRequest(parent_run_id=task.id,
        child_specs=[HierarchyChildSpec(goal="未授权的分工", allowed_skills=["capability:story-c"])], apply=True))
    assert denied.blocked and not denied.created_run_ids
    scheduled = agent.subagents.hierarchy.schedule_child_runs(params=HierarchyScheduleRequest(parent_run_id=task.id,
        child_specs=[HierarchyChildSpec(goal="继续整理已授权来源", allowed_skills=["capability:story-b"],
                                       allowed_tools=["skill_search"])], apply=True))
    assert not scheduled.blocked and len(scheduled.created_run_ids) == 1
    child = agent.subagents.load(scheduled.created_run_ids[0])
    previous = set_current_subagent_context(agent, run_id=child.id, task_attributes=child.attributes)
    try:
        snapshot = agent.current_skill_snapshot()
        assert [row.package_id for row in snapshot.packages] == ["story-b"]
        assert snapshot.resolve_package("story-b").to_ref() == refs["story-b"]
        assert SkillSearchTool(agent).execute({"action": "get", "package_id": "story-b"}).ok
        assert not SkillSearchTool(agent).execute({"action": "get", "package_id": "story-c"}).ok
    finally:
        restore_current_subagent_context(agent, previous)
