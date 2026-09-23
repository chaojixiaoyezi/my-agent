"""子代理完整恢复后的工具轮与活动归档：仅HTTP替身，真实runner/store/read_file。"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.agent_core import (
    _tool_loop_service,
    compact_request_recovery,
    runtime_mixin,
)
from agent_py_agent.agent.agent_core.subagent import compact_recovery, run_flow
from agent_py_agent.agent.backends import http
from agent_py_agent.agent.backends.base import ProviderRequestOptions
from agent_py_agent.agent.backends.errors import ProviderContextWindowError
from agent_py_agent.agent.backends.tool_ir import AssistantTurn, ToolResult
from agent_py_agent.agent.conversation import active_turn_compact
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
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
def test_child_first_request_compact_noop_keeps_following_tool_round(tmp_path, monkeypatch, backend):
    agent, task = _child(tmp_path, backend=backend, tools=True)
    material = _material(agent, "first-noop-tool.txt", "首轮预检后仍需保留的工具结果。")
    recoveries, preparations = [], []
    original_recovery = compact_recovery.prepare_subagent_compact_recovery
    original_prepare = runtime_mixin._prepare_runtime_context

    def recovery(*args, **kwargs):
        value = original_recovery(*args, **kwargs)
        recoveries.append(value)
        return value

    def prepare(*args, **kwargs):
        preparations.append(1)
        return original_prepare(*args, **kwargs)

    monkeypatch.setattr(compact_recovery, "prepare_subagent_compact_recovery", recovery)
    monkeypatch.setattr(runtime_mixin, "_prepare_runtime_context", prepare)
    business, _ = _http(monkeypatch, backend=backend, on_business=lambda *_: None)
    original_http = http.post_json

    def send(request):
        response = original_http(request)
        if len(business) == 1 and not _is_probe(request.payload):
            return _response(backend, tool={
                "type": "tool_use", "id": "read-after-first-noop", "name": "read_file",
                "input": {"path": str(material)},
            })
        return response

    monkeypatch.setattr(http, "post_json", send)
    result = agent.run_subagent(task.id, dry_run=False, probe=False)
    assert result.ok
    assert len(preparations) == len(recoveries) == 1
    assert recoveries[0].consumed and recoveries[0].resolved_input is not None
    assert not recoveries[0].committed
    assert agent.conversation_store.threads.require(task.agent_thread_id).compact_generation == 0
    assert len(business) == 2
    assert "read-after-first-noop" in json.dumps(business[1], ensure_ascii=False)
    assert "首轮预检后仍需保留的工具结果。" in json.dumps(business[1], ensure_ascii=False)


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
    assert len(preparations) == len(recoveries) == 2 and len(candidates) == 1
    assert recoveries[0].consumed and not recoveries[0].committed
    assert recoveries[0].resolved_input is not None and recoveries[1].committed
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
    assert restored[0].cancellation_token is recoveries[1].render_params.cancellation_token
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
@pytest.mark.parametrize("fail_projection", [False, True])
@pytest.mark.parametrize("archive_present", [True, False])
def test_child_active_turn_only(tmp_path, monkeypatch, backend, fail_projection, archive_present):
    agent, prior_task = _child(tmp_path, backend=backend, tools=True)
    task = agent.subagents.create_run(goal='读取当前资料并汇报', thought='', plan=[], allowed_tools=['read_file'])
    agent.config.max_tool_rounds = 0
    full_body = 'SOURCE-HEAD-' + '甲' * 2_500 + 'EXACT-IR-MIDDLE-CHILD' + '乙' * 2_500 + '-SOURCE-TAIL'
    material = _material(agent, 'active-material.txt', full_body)
    following_material = _material(agent, 'following-material.txt', '恢复后的下一次工具读取材料。')
    prepares, model_calls, compact_results, recoveries, read_calls, candidates = ([], [], [], [], [], [])
    source_plans = []
    original_prepare = runtime_mixin._prepare_runtime_context
    original_generate = _tool_loop_service.generate_model_response
    original_compact = active_turn_compact.compact_carried_active_turn_archive
    original_recovery = compact_recovery.prepare_subagent_compact_recovery
    original_project = compact_recovery._project_subagent_active_candidate
    original_mixed = compact_request_recovery._project_mixed_recovery_material
    original_summary = active_turn_compact._active_turn_replacement_summary
    original_attempt = run_flow._run_subagent_recovery_attempt
    original_read = _filesystem_read.ReadFileTool.execute
    overflow_sources = []
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
    def project(*args):
        if fail_projection:
            raise ConversationCompactError('测试child活动候选无法投影', code='COMPACT_REQUEST_PROJECTION_UNKNOWN')
        return original_project(*args)
    def mixed(material, view, max_chars):
        selected = original_mixed(material, view, max_chars)
        if view.is_candidate:
            candidates.append(selected)
        return selected
    def summarize(*args, **kwargs):
        source_plans.append(args[1])
        return original_summary(*args, **kwargs)
    def attempt(*args, **kwargs):
        result, current = original_attempt(*args, **kwargs)
        if result.runtime_status == 'context_overflow' and not overflow_sources:
            carry = result.native_compact_carry
            assert carry is not None
            native_results = [item for item in carry.history if isinstance(item, ToolResult)]
            assert len(native_results) == 1 and full_body in native_results[0].output
            archive = list(result.archive_tool_calls or [])
            assert len(archive) == 1 and archive[0]['call_id'] == native_results[0].call_id
            overflow_sources.append((carry, archive))
            if not archive_present:
                result = replace(result, archive_tool_calls=[])
        return result, current
    def read(self, params):
        read_calls.append(dict(params))
        return original_read(self, params)
    monkeypatch.setattr(runtime_mixin, '_prepare_runtime_context', prepare)
    monkeypatch.setattr(_tool_loop_service, 'generate_model_response', generate)
    monkeypatch.setattr(active_turn_compact, 'compact_carried_active_turn_archive', compact)
    monkeypatch.setattr(compact_recovery, 'prepare_subagent_compact_recovery', recovery)
    monkeypatch.setattr(compact_recovery, '_project_subagent_active_candidate', project)
    monkeypatch.setattr(compact_request_recovery, '_project_mixed_recovery_material', mixed)
    monkeypatch.setattr(active_turn_compact, '_active_turn_replacement_summary', summarize)
    monkeypatch.setattr(run_flow, '_run_subagent_recovery_attempt', attempt)
    monkeypatch.setattr(_filesystem_read.ReadFileTool, 'execute', read)
    def on_business(_wire, number):
        if number == 2:
            raise ProviderContextWindowError('测试活动回合单次工具后溢出')
        if number == 4:
            material = candidates[-1]
            projection = material.projection
            frozen = material.request_input
            expected = agent.backend.project_generate_payload(
                projection.provider_prompt, tools=list(frozen.native_tools) or None,
                tool_choice=projection.tool_choice if frozen.native_tools else None,
                messages=projection.messages, request_options=ProviderRequestOptions(
                    system_instruction=projection.system_instruction,
                    thinking_disabled=bool(frozen.native_tools) and projection.tool_choice.mode != 'auto',
                ),
            )
            assert _wire == expected
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
        if len(business) == 4:
            return _response(backend, tool={'type': 'tool_use', 'id': 'read-following-child-tool', 'name': 'read_file', 'input': {'path': str(following_material)}})
        return result
    monkeypatch.setattr(http, 'post_json', send)
    result = agent.run_subagent(task.id, dry_run=False, probe=False)
    assert len(overflow_sources) == 1
    if fail_projection:
        assert not result.ok and len(business) == 3 and prepares == [0, 0]
        assert [row['path'] for row in read_calls] == [str(material)]
        assert len(recoveries) == 1 and not recoveries[0].committed
        assert not compact_results and not candidates
        assert agent.conversation_store.threads.require(task.agent_thread_id).compact_generation == 0
        return
    assert result.ok, result
    assert len(business) == 5 and prepares == [0, 0], (len(business), prepares)
    assert [row['path'] for row in read_calls] == [str(material), str(following_material)]
    assert len(recoveries) == 1 and recoveries[0].committed
    assert len(compact_results) == len(candidates) == 1 and compact_results[0].compacted
    assert compact_results[0].source_call_ids == ('read-active-once',)
    assert len(source_plans) == 1
    source_ir = source_plans[0].source_ir_history
    assert len(source_ir) == 2 and isinstance(source_ir[0], AssistantTurn) and isinstance(source_ir[1], ToolResult)
    assert source_ir[0].tool_calls[0].call_id == source_ir[1].call_id == 'read-active-once'
    source_call = source_ir[0].tool_calls[0]
    assert len(source_plans[0].source_records) == int(archive_present)
    assert len(source_plans[0].source_tool_refs) == 1
    if archive_present:
        assert all(source_plans[0].source_records[0][field] == getattr(source_call, field)
                   for field in ('run_id', 'attempt_id', 'turn_id', 'call_id'))
    assert all(source_plans[0].source_tool_refs[0][field] == getattr(source_call, field)
               for field in ('run_id', 'attempt_id', 'turn_id', 'call_id'))
    assert full_body in source_ir[1].output
    assert len(overflow_sources[0][1][0]['output_preview']) < len(source_ir[1].output)
    summary_wire = json.dumps(business[2], ensure_ascii=False)
    assert summary_wire.count('EXACT-IR-MIDDLE-CHILD') == 1
    assert not any(isinstance(item, (AssistantTurn, ToolResult)) for item in candidates[0].request_input.tool_ir_history)
    assert [params.conversation_history_seed.compact_generation for params in model_calls] == [0, 0, 1, 1]
    assert model_calls[0].runtime_injections[0] == ''
    assert model_calls[-1] is model_calls[-2] and model_calls[-2] is not candidates[0].params
    assert model_calls[-2].compact_context.view.checkpoint_id == agent.conversation_store.threads.require(
        task.agent_thread_id,
    ).compact_checkpoint_id
    assert candidates[0].params.compact_context.view.checkpoint_id == ''
    assert model_calls[-2].compact_context.view.summary == candidates[0].params.compact_context.view.summary
    assert [row['call_id'] for row in model_calls[-1].archive_tool_calls] == (
        ['read-active-once', 'read-following-child-tool'] if archive_present
        else ['read-following-child-tool']
    )
    assert 'current_progress' in json.dumps(business[3], ensure_ascii=False)
    assert '恢复后的下一次工具读取材料' in json.dumps(business[-1], ensure_ascii=False)
    assert agent.conversation_store.threads.require(task.agent_thread_id).compact_generation == 1
    assert agent.conversation_store.threads.require(prior_task.agent_thread_id).compact_generation == 0
