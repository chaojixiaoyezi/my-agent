"""子代理完整恢复后的工具轮与活动归档：仅HTTP替身，真实runner/store/read_file。"""
from __future__ import annotations

import json

import pytest

from agent_py_agent.agent.agent_core import _tool_loop_service, runtime_mixin
from agent_py_agent.agent.agent_core.subagent import compact_recovery
from agent_py_agent.agent.backends import http
from agent_py_agent.agent.backends.errors import ProviderContextWindowError
from agent_py_agent.agent.conversation import active_turn_compact
from agent_py_agent.agent.tooling import _filesystem_read
from agent_py_agent.tests.test_subagent_compact_recovery import _child, _http


# LLM: 测试只构造供应商HTTP响应，不绕过原backend解析或执行工具。
# 函数用途: 返回两个真实协议支持的文本/工具响应。
def _response(backend, text="", *, tool=None):
    if backend == "anthropic_compatible":
        return {
            "content": [tool] if tool else [{"type": "text", "text": text}],
            "stop_reason": "tool_use" if tool else "end_turn",
        }
    message = {"content": text}
    if tool:
        message["tool_calls"] = [{
            "id": tool["id"], "type": "function", "function": {
                "name": tool["name"], "arguments": json.dumps(tool["input"]),
            },
        }]
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if tool else "stop"}]}


# LLM: 只按原结构化探针工具名区分探针，不解析业务prompt。
# 函数用途: 使HTTP测试包装器保留原探针回答。
def _is_probe(wire):
    names = [row.get("name") or row.get("function", {}).get("name") for row in wire.get("tools", [])]
    return names == ["my_agent_capability_probe"]


# LLM: 仅准备测试输入文件，产物由真实被测工具读取，不能代替被测任务完成。
# 函数用途: 写入隔离测试工作区的阅读材料。
def _material(agent, name, content):
    path = agent.home_paths.owner_workspace_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


@pytest.mark.parametrize("backend", ["anthropic_compatible", "openai_compatible"])
def test_child_transcript_then_tool(tmp_path, monkeypatch, backend):
    agent, task = _child(tmp_path, backend=backend, tools=True)
    agent.config.max_tool_rounds = 0
    material = _material(agent, 'recovery-material.txt', '恢复后必须保留的真实文件材料。')
    model_calls, preparations, recoveries, candidates = ([], [], [], [])
    original_generate = _tool_loop_service.generate_model_response
    original_prepare = runtime_mixin._prepare_runtime_context
    original_recovery = compact_recovery.prepare_subagent_compact_recovery
    original_project = compact_recovery._project_subagent_candidate
    def generate(req):
        model_calls.append((req.params, tuple(req.params.runtime_injections), json.dumps(req.params.provider_history_messages, ensure_ascii=False)))
        return original_generate(req)
    def prepare(*args, **kwargs):
        preparations.append(1)
        return original_prepare(*args, **kwargs)
    def recovery(*args, **kwargs):
        result = original_recovery(*args, **kwargs)
        recoveries.append(result)
        return result
    def project(*args, **kwargs):
        result = original_project(*args, **kwargs)
        if args[-1].is_candidate:
            candidates.append(result)
        return result
    monkeypatch.setattr(_tool_loop_service, 'generate_model_response', generate)
    monkeypatch.setattr(runtime_mixin, '_prepare_runtime_context', prepare)
    monkeypatch.setattr(compact_recovery, 'prepare_subagent_compact_recovery', recovery)
    monkeypatch.setattr(compact_recovery, '_project_subagent_candidate', project)
    def on_business(_wire, number):
        if number == 1:
            raise ProviderContextWindowError('测试第一次请求溢出')
    business, _probes = _http(monkeypatch, backend=backend, on_business=on_business)
    original_http = http.post_json
    def send(request):
        response = original_http(request)
        if len(business) == 3 and (not _is_probe(request.payload)):
            return _response(backend, tool={'type': 'tool_use', 'id': 'read-after-child-compact', 'name': 'read_file', 'input': {'path': str(material)}})
        return response
    monkeypatch.setattr(http, 'post_json', send)
    result = agent.run_subagent(task.id, dry_run=False, probe=False)
    assert result.ok, result
    assert len(business) == 4
    assert len(preparations) == 2 and len(recoveries) == len(candidates) == 1
    assert len(model_calls) == 3
    first, restored, after_tool = model_calls
    assert first[0].conversation_history_seed.compact_generation == 0
    assert restored[0] is after_tool[0]
    assert restored[0] is not candidates[0].params
    assert restored[0].conversation_history_seed == candidates[0].params.conversation_history_seed
    assert restored[0].provider_history_messages == candidates[0].params.provider_history_messages
    assert restored[0].compact_context is not None
    assert restored[0].compact_context == after_tool[0].compact_context
    assert restored[0].compact_context.view.summary == candidates[0].params.compact_context.view.summary
    assert restored[0].compact_context.view.operation_evidence == candidates[0].params.compact_context.view.operation_evidence
    assert candidates[0].params.compact_context.view.checkpoint_id == ""
    assert restored[0].compact_context.view.checkpoint_id == agent.conversation_store.threads.require(
        task.agent_thread_id,
    ).compact_checkpoint_id
    assert restored[0].compact_context.view.source_message_ids
    assert restored[0].cancellation_token is recoveries[0].render_params.cancellation_token
    assert restored[0].conversation_history_seed.compact_generation == 1
    assert after_tool[0].conversation_history_seed.compact_generation == 1
    assert restored[1:] == after_tool[1:] and restored[2] != first[2]
    assert 'generation 1' in restored[2]
    wire_text = json.dumps(business[-1], ensure_ascii=False)
    assert '恢复后必须保留的真实文件材料。' in wire_text
    assert 'read-after-child-compact' in wire_text
    assert agent.conversation_store.threads.require(task.agent_thread_id).compact_generation == 1
    assert len([item for item in after_tool[0].archive_tool_calls if item.get('tool') == 'read_file']) == 1


@pytest.mark.parametrize("backend", ["anthropic_compatible", "openai_compatible"])
def test_child_active_turn_only(tmp_path, monkeypatch, backend):
    agent, prior_task = _child(tmp_path, backend=backend, tools=True)
    task = agent.subagents.create_run(goal='读取当前资料并汇报', thought='', plan=[], allowed_tools=['read_file'])
    agent.config.max_tool_rounds = 0
    material = _material(agent, 'active-material.txt', '只允许一次读取的活动回合材料。')
    prepares, model_calls, compact_results, recoveries, read_calls = ([], [], [], [], [])
    original_prepare = runtime_mixin._prepare_runtime_context
    original_generate = _tool_loop_service.generate_model_response
    original_compact = active_turn_compact.compact_carried_active_turn_archive
    original_recovery = compact_recovery.prepare_subagent_compact_recovery
    original_read = _filesystem_read.ReadFileTool.execute
    def prepare(*args, **kwargs):
        prepares.append(agent.conversation_store.threads.require(task.agent_thread_id).compact_generation)
        return original_prepare(*args, **kwargs)
    def generate(req):
        model_calls.append(req.params)
        return original_generate(req)
    def compact(*args, **kwargs):
        result = original_compact(*args, **kwargs)
        compact_results.append(result)
        return result
    def recovery(*args, **kwargs):
        result = original_recovery(*args, **kwargs)
        recoveries.append(result)
        return result
    def read(self, params):
        read_calls.append(dict(params))
        return original_read(self, params)
    monkeypatch.setattr(runtime_mixin, '_prepare_runtime_context', prepare)
    monkeypatch.setattr(_tool_loop_service, 'generate_model_response', generate)
    monkeypatch.setattr(active_turn_compact, 'compact_carried_active_turn_archive', compact)
    monkeypatch.setattr(compact_recovery, 'prepare_subagent_compact_recovery', recovery)
    monkeypatch.setattr(_filesystem_read.ReadFileTool, 'execute', read)
    def on_business(_wire, number):
        if number == 2:
            raise ProviderContextWindowError('测试活动回合单次工具后溢出')
    business, _probes = _http(monkeypatch, backend=backend, on_business=on_business)
    original_http = http.post_json
    def send(request):
        result = original_http(request)
        if _is_probe(request.payload):
            return result
        if len(business) == 1:
            return _response(backend, tool={'type': 'tool_use', 'id': 'read-active-once', 'name': 'read_file', 'input': {'path': str(material)}})
        if len(business) == 3:
            return _response(backend, '[compact-live-handoff.v1]\ncurrent_progress: 已读取当前材料。\nuser_constraints: 保持任务范围，不重复读取。\ncompleted: 文件读取成功，调用记录保留。\nfailures: none。\nunresolved: 形成最终答复。\nnext_step: 依据已读材料汇报。')
        return result
    monkeypatch.setattr(http, 'post_json', send)
    result = agent.run_subagent(task.id, dry_run=False, probe=False)
    assert result.ok, result
    assert len(business) == 4 and prepares == [0, 1], (len(business), prepares)
    assert len(read_calls) == 1 and read_calls[0]['path'] == str(material)
    assert not recoveries
    assert len(compact_results) == 1 and compact_results[0].compacted
    assert compact_results[0].source_call_ids == ('read-active-once',)
    assert [params.conversation_history_seed.compact_generation for params in model_calls] == [0, 0, 1]
    assert len(model_calls[-1].archive_tool_calls) == 1
    assert 'current_progress' in json.dumps(business[-1], ensure_ascii=False)
    assert agent.conversation_store.threads.require(task.agent_thread_id).compact_generation == 1
    assert agent.conversation_store.threads.require(prior_task.agent_thread_id).compact_generation == 0
