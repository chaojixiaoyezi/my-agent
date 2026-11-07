"""三种真实压缩发射器的计时、日志和公开投影合同。"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import compact_progress as schema
from agent_py_agent.tests.test_active_turn_compact_projection import carried_case as carried_case


# LLM: 使用真实发射器；只有时钟和展示sink是可控夹具，候选/CAS不在这个单元测试中伪装成功。
# 函数用途: 为三种发射器提供相同的阶段输入和稳定操作身份。
def _emitter(kind, events, monkeypatch):
    from agent_py_agent.agent.agent_core import _tool_loop_service as native
    from agent_py_agent.agent.conversation import active_turn_compact, compact

    base = {"generation": 1, "operation_id": "duration-op", "before_tokens": 100,
            "trigger_tokens": 80, "source_messages": 2,
            "source_kind": schema.COMPACT_SOURCE_ACTIVE_TURN,
            "commit_authority": schema.COMPACT_AUTHORITY_CONVERSATION}
    if kind == "transcript":
        request = SimpleNamespace(progress_callback=events.append if events is not None else None,
            thread=SimpleNamespace(compact_generation=0, thread_id="thread"), operation_id="duration-op",
            projected_tokens=100, policy=SimpleNamespace(trigger_tokens=80, context_window_tokens=200),
            pending=[1, 2], trigger_source="preflight", started_monotonic=100.0, timing_state={})
        return lambda **kwargs: compact._emit_compact_progress(request, **kwargs)
    if kind == "active":
        base["_thread_id"] = "thread"
        base["trigger_source"] = "preflight"
        return lambda **kwargs: active_turn_compact._emit_progress(events.append if events is not None else None, base, **kwargs)
    params = SimpleNamespace(live_archive_state={}, task_attributes={"conversation_thread_id": "thread"},
        effective_on_chunk=SimpleNamespace(write_conversation_compact_progress=events.append) if events is not None else None)
    base.update(source_kind=schema.COMPACT_SOURCE_TURN_LOCAL, commit_authority=schema.COMPACT_AUTHORITY_TURN_LOCAL)
    return lambda **kwargs: native._emit_native_compact_progress(params, **base, **kwargs)


@pytest.mark.parametrize("kind", ["transcript", "active", "native"])
@pytest.mark.parametrize("terminal,stage", [("completed", "completed"), ("failed", "failed"), ("superseded", "candidate_discarded")])
def test_all_emitters_measure_stages_and_log_terminal_once(kind, terminal, stage, monkeypatch, caplog):
    import time

    now, events = [100.0], []
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    emit = _emitter(kind, events, monkeypatch)
    with caplog.at_level(logging.INFO):
        emit(phase="started", stage="preparing", percent=5)
        now[0] = 101.0
        emit(phase="progress", stage="summarizing", percent=20)
        now[0] = 103.0
        emit(phase=terminal, stage=stage, percent=100, after_tokens=40)
        emit(phase=terminal, stage=stage, percent=100, after_tokens=40)
    assert [row["duration_ms"] for row in events] == [0, 1000, 3000, 3000]
    assert all("stage_durations_ms" not in row for row in events[:2])
    assert events[-1]["stage_durations_ms"]["preparing"] == 1000
    assert events[-1]["stage_durations_ms"]["summarizing"] == 2000
    for row in events:
        public = schema.normalize_conversation_compact_progress(row)
        assert public["duration_ms"] == row["duration_ms"]
        assert all(type(value) is int and value >= 0 for value in row.get("stage_durations_ms", {}).values())
        assert set(row.get("stage_durations_ms", {})) <= schema.CONVERSATION_COMPACT_PROGRESS_STAGES
    records = [r for r in caplog.records if getattr(r, "event", None) == "conversation_compaction"]
    assert len(records) == 1
    record = records[0]
    assert record.getMessage() == "会话压缩结束"
    assert record.phase == terminal and record.duration_ms == 3000
    assert (record.before_tokens, record.after_tokens, record.source_messages) == (100, 40, 2)
    assert hasattr(record, "thread_id") and hasattr(record, "trigger_source")
    assert not any("summary" in key for key in vars(record))


@pytest.mark.parametrize("kind", ["transcript", "active", "native"])
def test_terminal_logging_does_not_depend_on_display_callback(kind, monkeypatch, caplog):
    import time

    monkeypatch.setattr(time, "monotonic", lambda: 100.0)
    emit = _emitter(kind, None, monkeypatch)
    with caplog.at_level(logging.INFO):
        emit(phase="started", stage="preparing", percent=5)
        emit(phase="failed", stage="failed", percent=0)
    assert len([r for r in caplog.records if getattr(r, "event", None) == "conversation_compaction"]) == 1


@pytest.mark.parametrize("value", [True, False, -1, 1.5, "12", None, [], {}])
def test_duration_normalizer_drops_non_integer_or_negative_values(value):
    from agent_py_agent.tests.test_compact_progress import _progress

    payload = schema.normalize_conversation_compact_progress(_progress(duration_ms=value,
        phase="completed", stage="completed", stage_durations_ms={"preparing": value, "summarizing": 42, "unknown": 1}))
    assert "duration_ms" not in payload
    assert payload["stage_durations_ms"] == {"summarizing": 42}


def test_duration_optional_fields_leave_legacy_events_byte_identical():
    from agent_py_agent.tests.test_compact_progress import _progress

    value = schema.normalize_conversation_compact_progress(_progress())
    assert "duration_ms" not in value and "stage_durations_ms" not in value
    expected = {"schema": schema.CONVERSATION_COMPACT_PROGRESS_SCHEMA, "phase": "progress", "stage": "summarizing",
        "percent": 35, "generation": 2, "operation_id": "compact:test", "source_kind": "legacy", "commit_authority": "legacy",
        "before_tokens": 120000, "after_tokens": 0, "trigger_tokens": 115200, "source_messages": 80}
    assert json.dumps(value) == json.dumps(expected)
    payload = schema.normalize_conversation_compact_progress(_progress(duration_ms=0, stage_durations_ms={"preparing": 1}))
    assert payload["duration_ms"] == 0 and "stage_durations_ms" not in payload


# LLM: 实际Gateway预检→摘要→检查点/CAS→业务发送；只有provider HTTP为假，事件经真实writer及TUI再归一化。
# 函数用途: 证明新增计时字段穿过真实公开流而非仅存在于局部发射器。
def test_gateway_compact_wire_preserves_timing_through_real_consumers(tmp_path, monkeypatch, caplog):
    from dataclasses import replace

    from agent_py_agent.agent.gateway_parts import request_execution
    from agent_py_agent.agent.gateway_parts.paths import gateway_chunk_path
    from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter
    from agent_py_agent.cli.chat_parts.tui_runtime import _tui_conversation_compact_progress_payload
    from agent_py_agent.tests.test_gateway_compact_recovery import _history_request
    from agent_py_agent.tests.test_gateway_model_adoption import fake_http

    fixture = _history_request(tmp_path, original_window=20000, history_repeat=10000)
    fixture.context.request["client_capabilities"] = {"rich_transcript": True}
    fixture.context.request_path.write_text(json.dumps(fixture.context.request))
    fake_http(monkeypatch, fixture)
    writer = BufferedChunkStreamWriter(gateway_chunk_path(fixture.paths, fixture.context.request_id), rich_transcript=True)
    try:
        with caplog.at_level(logging.INFO):
            result = request_execution._run_gateway_ask(replace(fixture.context, on_chunk=writer))
    finally:
        writer.close()
    assert result.response == "资料整理完成。"
    assert fixture.agent.conversation_store.threads.require(fixture.thread_id).compact_generation == 1
    lines = [json.loads(line) for line in gateway_chunk_path(fixture.paths, fixture.context.request_id).read_text().splitlines()]
    events = [line["compact_progress"] for line in lines if line.get("kind") == "conversation_compaction_progress"]
    assert events and events[0]["phase"] == "started" and events[-1]["phase"] == "completed"
    for event in events:
        assert type(event["duration_ms"]) is int and event["duration_ms"] >= 0
        # 计时 schema 只消费阶段与数字；Gateway 同次附带的公开身份仍须完整穿过事件流。
        identity_fields = ("thread_id", "producer", "model_name", "reasoning_level")
        progress = {key: value for key, value in event.items() if key not in identity_fields}
        identity = {key: event[key] for key in identity_fields}
        assert _tui_conversation_compact_progress_payload(event) == progress
        assert schema.normalize_conversation_compact_progress(event) == progress
        assert event == {**progress, **identity}
        assert identity["thread_id"] == fixture.thread_id
        assert identity["model_name"] == "original-model" and identity["reasoning_level"] == ""
        assert identity["producer"]["scope"] == "main"
        assert set(identity["producer"]) == {"scope", "agent_run_id"}
    assert "stage_durations_ms" in events[-1]
    logs = [r for r in caplog.records if getattr(r, "event", None) == "conversation_compaction"]
    assert len(logs) == 1 and logs[0].thread_id == fixture.thread_id


# LLM: 静态保护三个结构性红线；运行态budget/顺序/终态另有真实入口回归，不能只测字符串存在。
# 函数用途: 给实际语法树和反例夹具使用同一套守门判据。
def _w7_ast_violations(source):
    import ast

    tree = ast.parse(source)
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_segment_exclusion_reason":
            branch = next(item for item in node.body if isinstance(item, ast.If))
            tests = list(ast.walk(branch.test))
            origin = any(isinstance(item, ast.Compare) and isinstance(item.left, ast.Name) and item.left.id == "origin"
                and any(isinstance(other, ast.Name) and other.id == "_OUTPUT_LIMIT_SEGMENT_ORIGIN" for other in item.comparators) for item in tests)
            completed = any(isinstance(item, ast.Name) and item.id == "completed" for item in tests)
            if not origin or not completed:
                violations.append("truncated_origin_gate")
        if isinstance(node, ast.FunctionDef) and node.name == "bounded_authority_records":
            for call in (item for item in ast.walk(node) if isinstance(item, ast.Call)):
                if isinstance(call.func, ast.Attribute) and call.func.attr == "load" and any(keyword.arg == "on_record" for keyword in call.keywords):
                    violations.append("barrier_charged_as_live")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"info", "debug", "warning", "error", "log"}:
            if any(isinstance(item, ast.Attribute) and item.attr == "summary" or isinstance(item, ast.Name) and item.id == "summary"
                   for argument in [*node.args, *(keyword.value for keyword in node.keywords)] for item in ast.walk(argument)):
                violations.append("summary_logged")
    return violations


def test_w7_ast_guard_accepts_production_and_rejects_invalid_fixtures():
    import ast
    from pathlib import Path

    from agent_py_agent.agent.agent_core.tool_loop import response_decision
    from agent_py_agent.agent.conversation import compact
    from agent_py_agent.agent.memory_store import _jsonl_indexing

    for module in (response_decision, _jsonl_indexing, compact):
        assert _w7_ast_violations(Path(module.__file__).read_text()) == []
    invalid = '''
def _segment_exclusion_reason(response, *, origin='', completed=False):
    if response.truncated:
        return 'truncated'
def bounded_authority_records(memory, *, max_records, max_bytes):
    return memory.tombstones.load(on_record=charge_record)
logger.info('compact', summary)
'''
    assert set(_w7_ast_violations(invalid)) == {"truncated_origin_gate", "barrier_charged_as_live", "summary_logged"}
    tree = ast.parse(Path(response_decision.__file__).read_text())
    assert any(isinstance(item, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "_OUTPUT_LIMIT_SEGMENT_ORIGIN" for target in item.targets)
               and isinstance(item.value, ast.Constant) and item.value.value == "output_limit_resume_prefix" for item in tree.body)


# LLM: 使用真正请求DTO、Store和发射器；空pending只用于首事件即取消，不伪装候选提交。
# 函数用途: 给取消与候选投影计时提供独立的transcript请求。
def _duration_transcript_request(case, events, *, projector=None, started=100.0):
    import time

    from agent_py_agent.agent.conversation.compact import _CompactRunRequest

    return _CompactRunRequest(
        agent=case.agent, store=case.store, thread=case.thread, current_prompt="继续核对",
        pending=(), policy=SimpleNamespace(trigger_tokens=9000, context_window_tokens=10000),
        projected_tokens=10000, forced=True, attempted_at=time.time(), operation_id="timed-transcript",
        progress_callback=events.append, request_projector=projector, started_monotonic=started,
    )


# LLM: 回调是原取消来源，不能因首事件位于try外而漏终态；原异常身份、无CAS、无失败熔断均须保留。
# 函数用途: 验证transcript与活动归档的早期取消真正收口并记录一次结构化日志。
@pytest.mark.parametrize("kind,stage", [("transcript", "preparing"), ("active", "preparing"), ("active", "summarizing")])
def test_initial_progress_cancellation_closes_the_real_operation(carried_case, kind, stage, caplog):
    from dataclasses import replace

    from agent_py_agent.agent.common.cancellation import ToolCancelled
    from agent_py_agent.agent.conversation import compact
    from agent_py_agent.tests.test_active_turn_compact_projection import (
        _assert_uncommitted,
        _compact,
    )

    case, events = carried_case, []
    cancellation = ToolCancelled("simulated progress cancellation")

    # LLM: 只在一个指定的非终态上抛原对象；终态仍能被真实发射器展示。
    # 函数用途: 构造首阶段取消，保留全部事件供外部核对。
    def callback(event):
        events.append(event)
        if event["stage"] == stage:
            raise cancellation

    with caplog.at_level(logging.INFO), pytest.raises(ToolCancelled) as caught:
        if kind == "transcript":
            request = replace(_duration_transcript_request(case, events), progress_callback=callback)
            compact._execute_compact_request(request)
        else:
            _compact(case, progress_callback=callback)
    assert caught.value is cancellation
    assert events[-1]["phase"] == "superseded" and "stage_durations_ms" in events[-1]
    assert type(events[-1]["duration_ms"]) is int and events[-1]["duration_ms"] >= 0
    assert _assert_uncommitted(case).compact_consecutive_failures == 0
    logs = [record for record in caplog.records if getattr(record, "event", None) == "conversation_compaction"]
    assert len(logs) == 1 and logs[0].phase == "superseded"


# LLM: 时钟只在真实候选投影中推进，阶段归属不能依赖“计量完成后才发measuring”的旧位置。
# 函数用途: 证明transcript候选计量和活动归档完整投影的耗时实际计入measuring。
@pytest.mark.parametrize("kind", ["transcript", "active"])
def test_real_candidate_projection_time_belongs_to_measuring(carried_case, kind, monkeypatch):
    import time

    from agent_py_agent.agent.conversation import compact
    from agent_py_agent.agent.conversation.compact_projection import ConversationCompactProjection
    from agent_py_agent.tests.test_active_turn_compact_projection import _compact

    case, now, events = carried_case, [100.0], []
    monkeypatch.setattr(time, "monotonic", lambda: now[0])

    # LLM: 只替代宿主投影输入/计量；候选门、检查点、CAS和发射器仍使用原实现。
    # 函数用途: 精确提供2秒计量区间，防止阶段总和看似正确但归属错误。
    def project(*_args):
        now[0] += 2.0
        return ConversationCompactProjection(40, {"prompt": "timed projection"})

    if kind == "transcript":
        request = _duration_transcript_request(case, events, projector=project)
        compact._emit_compact_progress(request, phase="started", stage="preparing", percent=5)
        compact._emit_compact_progress(request, phase="progress", stage="summarizing", percent=20)
        measured = compact._measure_candidate(request, compact._CandidateTail((), {}), "摘要")
        assert measured.tokens == 40
        compact._emit_compact_progress(request, phase="completed", stage="completed", percent=100, after_tokens=40)
    else:
        assert _compact(case, request_projector=project, progress_callback=events.append).compacted
        assert case.store.threads.require(case.thread.thread_id).compact_generation == 1
    assert events[-1]["stage_durations_ms"].get("measuring") == 2000
    assert events[-1]["stage_durations_ms"].get("summarizing", 0) == 0


# LLM: 复用只读回归的真实native上下文、Store及摘要backend；没有替换CAS、检查点或历史裁剪。
# 函数用途: 建立会真正产生持久代次的native压缩环境。
def _duration_native_case(tmp_path):
    from dataclasses import replace

    from agent_py_agent.agent.conversation.authority import (
        CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR,
    )
    from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE
    from agent_py_agent.agent.conversation.compact_summary_view import (
        AppliedCompactContext,
        resolve_compact_summary_view,
    )
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.tests.test_native_tool_ir_compact_and_orphan_sweep import (
        _ContextCompactionSink,
        _native_agent,
        _params,
        _record_large_write_calls,
        _SummaryBackend,
    )

    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "local/main", "channel": "test",
        "channel_conversation_id": "native-duration", "channel_user_id": "local/main"})
    agent = _native_agent(tmp_path)
    agent.backend = _SummaryBackend(10000)
    agent.config.model_context_window_tokens = 10000
    agent.config.memory_compact_trigger_percent = 90
    agent.prompts = SimpleNamespace(build=lambda *_args, **_kwargs: "base-prompt")
    agent.conversation_store, agent.home_paths = store, SimpleNamespace(owner_compact_dir=tmp_path / "compact")
    sink = _ContextCompactionSink()
    context = AppliedCompactContext(thread_id=thread.thread_id, scope=THREAD_COMPACT_SCOPE,
        view=resolve_compact_summary_view(agent, thread, THREAD_COMPACT_SCOPE))
    params = replace(_params(), save=True, effective_on_chunk=sink, compact_context=context,
        task_attributes={CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True, "conversation_thread_id": thread.thread_id})
    _record_large_write_calls(agent, params, start=1, stop=6, chars=12000)
    return SimpleNamespace(agent=agent, params=params, sink=sink, store=store, thread=thread)


# LLM: 在真实裁剪及冻结估算前推进时钟；CAS后还应有且仅有一次完成日志。
# 函数用途: 检查native计算耗时归属而不是仅测试发射器的人造阶段顺序。
def test_real_native_reduction_time_belongs_to_measuring(tmp_path, monkeypatch, caplog):
    import time

    from agent_py_agent.agent.agent_core import _tool_loop_service as service

    case, now = _duration_native_case(tmp_path), [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    original = service.reduce_native_compact_candidate

    # LLM: 在原函数入口计时，不改候选、估算、历史或提交结果。
    # 函数用途: 给真实native计算放入可核对的2秒区间。
    def reduce(*args, **kwargs):
        now[0] += 2.0
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "reduce_native_compact_candidate", reduce)
    with caplog.at_level(logging.INFO):
        service.build_tool_loop_prompt(case.agent, case.params)
    assert case.store.threads.require(case.thread.thread_id).compact_generation == 1
    terminal = case.sink.progress_rows[-1]
    assert terminal["phase"] == "completed" and terminal["stage_durations_ms"].get("measuring") == 2000
    assert terminal["stage_durations_ms"].get("summarizing", 0) == 0
    assert len([record for record in caplog.records if getattr(record, "event", None) == "conversation_compaction"]) == 1


# LLM: CAS成功已是压缩成功；后处理失败保留原异常/代次，不改成失败或superseded、不回滚/计熔断。
#   未取得最终投影计量时after_tokens=0表示未知，不把候选数冒充最终发送大小。
# 函数用途: 验证持久提交后的刷新/投影异常仍有完成观测，且不谎称请求成功。
@pytest.mark.parametrize("point", ["refresh", "projection"])
@pytest.mark.parametrize("failure_type", [RuntimeError, InterruptedError])
def test_post_commit_error_closes_committed_compaction_without_rollback(tmp_path, monkeypatch, caplog, point, failure_type):
    from agent_py_agent.agent.agent_core import _tool_loop_service as service

    case = _duration_native_case(tmp_path)
    original_ir, original_context = list(case.params.tool_ir_history), list(case.params.tool_context)
    failure = failure_type("simulated post-commit projection failure")
    committed = {}

    # LLM: 只在已提交持久代次后抛原对象，不伪装提交失败或改写历史。
    # 函数用途: 将异常放在回滚边界之外，核对新增计时是否漏终态。
    def fail(*_args, **_kwargs):
        assert case.store.threads.require(case.thread.thread_id).compact_generation == 1
        committed["ir"], committed["context"] = list(case.params.tool_ir_history), list(case.params.tool_context)
        raise failure

    monkeypatch.setattr(service, "_refresh_native_compact_context" if point == "refresh" else "record_conversation_compact_generation", fail)
    with caplog.at_level(logging.INFO), pytest.raises(failure_type) as caught:
        service.build_tool_loop_prompt(case.agent, case.params)
    assert caught.value is failure
    assert case.params.tool_ir_history == committed["ir"] != original_ir
    assert case.params.tool_context == committed["context"] != original_context
    stored = case.store.threads.require(case.thread.thread_id)
    assert stored.compact_generation == 1 and stored.compact_checkpoint_id and stored.compact_consecutive_failures == 0
    assert not [row for row in case.sink.progress_rows if row["phase"] in {"failed", "superseded"}]
    terminal = case.sink.progress_rows[-1]
    assert terminal["phase"] == "completed" and terminal["after_tokens"] == 0
    assert terminal["generation"] == 1 and "stage_durations_ms" in terminal
    logs = [record for record in caplog.records if getattr(record, "event", None) == "conversation_compaction"]
    assert len(logs) == 1 and logs[0].phase == "completed"


# LLM: 摘要内部最后一次停止检查通过后仍可能收到真实取消；外层复查不能留下无终态的started块。
# 函数用途: 在摘要返回与计划返回之间取消，验证无提交、无失败熔断且有一次撤销日志。
def test_native_cancel_between_summary_return_and_plan_closes_progress(tmp_path, monkeypatch, caplog):
    from dataclasses import replace

    from agent_py_agent.agent.agent_core import _tool_loop_service as service
    from agent_py_agent.agent.common.cancellation import CancellationToken

    case = _duration_native_case(tmp_path)
    cancellation = CancellationToken()
    case.params = replace(case.params, cancellation_token=cancellation)
    original_ir = list(case.params.tool_ir_history)
    summarize = service._summarize_live_compact

    # LLM: 摘要本体和内部停止检查全部真实执行，返回之后才设置真实run取消信号。
    # 函数用途: 确定性重现两个检查之间的取消时序。
    def cancel_after_summary(*args, **kwargs):
        result = summarize(*args, **kwargs)
        cancellation.cancel("simulated cancellation after summary return")
        return result

    monkeypatch.setattr(service, "_summarize_live_compact", cancel_after_summary)
    with caplog.at_level(logging.INFO), pytest.raises(InterruptedError):
        service.build_tool_loop_prompt(case.agent, case.params)
    stored = case.store.threads.require(case.thread.thread_id)
    assert stored.compact_generation == 0 and stored.compact_consecutive_failures == 0
    assert case.params.tool_ir_history == original_ir
    terminal = case.sink.progress_rows[-1]
    assert terminal["phase"] == "superseded" and "stage_durations_ms" in terminal
    logs = [record for record in caplog.records if getattr(record, "event", None) == "conversation_compaction"]
    assert len(logs) == 1 and logs[0].phase == "superseded"
