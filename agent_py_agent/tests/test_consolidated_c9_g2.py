"""Wave 3 consolidated tests (shard c9, group 2: test_v14_compact_duration.py, test_v14_r2_workflow_projection_open.py, test_v14_r4_multiprocess_observation_acks.py, test_v14_script_run_out.py, test_v14_w1_background_cursor_successor.py, test_v14_w1_failure_diagnostics.py, test_v14_w1_reset_inflight_turn.py)."""
from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import queue
import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from dataclasses import fields, replace
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.workflow_store import WorkflowStore
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation import compact_progress as schema
from agent_py_agent.agent.conversation.message_stream import read_background_response_page
from agent_py_agent.agent.conversation.observation_index import (
    ObservationIndex,
    ObservationIndexIncomplete,
)
from agent_py_agent.agent.gateway_parts.multiprocess_background_request import (
    BackgroundSourceChanged,
    freeze_source,
    read_source,
)
from agent_py_agent.agent.gateway_parts.multiprocess_background_sources import observation_handled
from agent_py_agent.agent.gateway_parts.request_errors import gateway_client_error_message
from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool
from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaEnforcer
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, _owner_home_dir
from agent_py_agent.agent.workflows import models, run_list_index, run_store, run_summary_index
from agent_py_agent.agent.workflows import script_runner as runner_module
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.models import WorkflowRunRecord
from agent_py_agent.agent.workflows.run_list_index import RunListIndex, index_connection
from agent_py_agent.agent.workflows.run_summary_index import RunSummaryIndex
from agent_py_agent.agent.workflows.script_runner import ScriptRunner
from agent_py_agent.cli.chat_parts import tui_worker
from agent_py_agent.cli.chat_parts.plain_state import ChatJob
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime, TuiTurnSummary
from agent_py_agent.cli.chat_parts.tui_threading import _consume_background_notices
from agent_py_agent.cli.chat_parts.tui_worker_paths import _gateway_outcome
from agent_py_agent.tests.test_active_turn_compact_projection import carried_case as carried_case

# ============================================================================
# Source: agent_py_agent/tests/test_v14_compact_duration.py
# ============================================================================

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


# ============================================================================
# Source: agent_py_agent/tests/test_v14_r2_workflow_projection_open.py
# ============================================================================

# LLM: 同一进程同一观察点比较描述符数；连接、目录FD与锁FD都必须词法关闭。
# 函数用途: 返回当前进程打开的描述符数量。
def _fd_count():
    return len(os.listdir('/proc/self/fd' if Path('/proc/self/fd').is_dir() else '/dev/fd'))


# LLM: 只包住真实投影连接入口，记录异常后原样抛出；不替换存储行为，也不伪造任何记录。
# 函数用途: 让被 commit_run 吞掉的投影错误在测试中可见。
def _observe_projection(monkeypatch, failures):
    for module in (run_list_index, run_summary_index):
        real = module.index_connection

        @contextmanager
        def observed(owner_home, filename, real=real):
            try:
                with real(owner_home, filename) as conn:
                    yield conn
            except Exception as exc:
                failures.append(f'{filename}: {exc!r}')
                raise

        monkeypatch.setattr(module, 'index_connection', observed)


# LLM: 真实 DTO 的最小合法运行，状态推进全部经 WorkflowRunStore。
# 函数用途: 构造一个排队中的运行记录。
def _record(run_id, started_at=1):
    return WorkflowRunRecord(run_id, 'flow', 'alice', 'manual', 'queued', started_at, 0, 0)


# LLM: 两线程分别推进同 Owner 的真实 run.json 并反复打开摘要投影，同时在 data/ 增删兄弟文件，至少持续 2 秒。
# 函数用途: 复现祖先 ctime 比较把正常并发误判为路径替换，并约束规范写永不因投影失败。
def test_sibling_churn_never_reports_replacement_or_fails_canonical_writes(tmp_path, monkeypatch):
    home = tmp_path / 'owner'
    home.mkdir()
    store = run_store.WorkflowRunStore(home)
    summary = RunSummaryIndex(home)
    with summary.connection():
        pass
    failures, projection, written, cycles = [], [], [], []
    _observe_projection(monkeypatch, projection)
    before = _fd_count()
    started = time.monotonic()
    deadline = started + 2.0

    def transitions():
        number = 0
        while time.monotonic() < deadline:
            record = _record(f'run-{number:05d}', number + 1)
            try:
                store.create_run(record)
                running = replace(record, status='running')
                store.save_run(running)
                store.save_run(replace(running, status='succeeded'))
            except Exception as exc:
                failures.append(f'canonical {record.workflow_run_id}: {exc!r}')
                return
            written.append(record.workflow_run_id)
            number += 1

    def churn():
        number = 0
        while time.monotonic() < deadline:
            sibling = home / 'data' / f'sibling-{number % 4}.tmp'
            try:
                sibling.write_bytes(b'sibling')
                with summary.connection() as conn:
                    conn.execute('SELECT count(*) FROM summary_assets').fetchone()
                sibling.unlink()
            except Exception as exc:
                failures.append(f'summary: {exc!r}')
                return
            cycles.append(number)
            number += 1

    threads = [threading.Thread(target=transitions), threading.Thread(target=churn)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads)
    assert time.monotonic() - started >= 2.0
    assert failures == []
    assert projection == []
    assert len(written) >= 3 and len(cycles) >= 3
    assert _fd_count() == before
    runs = store.list_runs(status='succeeded', limit=len(written))
    assert sorted(run.workflow_run_id for run in runs) == sorted(written)


# LLM: 损坏投影只影响候选页；save_run 仍写 run.json，下一次列表读从规范运行目录重建并返回全部运行。
# 函数用途: 验证非忙碌 SQLite 错误使投影失效重建，而不是让每次规范写永久失败。
def test_corrupt_projection_never_blocks_save_and_next_list_rebuilds(tmp_path):
    store = run_store.WorkflowRunStore(tmp_path)
    records = [_record(f'corrupt-{number}', number + 1) for number in range(3)]
    for record in records:
        store.create_run(record)
    projection = tmp_path / 'data' / 'workflow_run_index.sqlite3'
    projection.write_bytes(b'damaged projection page ' * 64)
    before = _fd_count()
    running = replace(records[1], status='running')
    store.save_run(running)
    assert running.revision == 2
    assert store.load_run('corrupt-1').status == 'running'
    assert sorted(run.workflow_run_id for run in store.list_runs()) == ['corrupt-0', 'corrupt-1', 'corrupt-2']
    assert [run.workflow_run_id for run in store.list_runs(status='running')] == ['corrupt-1']
    assert sorted(run.workflow_run_id for run in store.list_runs(status='queued')) == ['corrupt-0', 'corrupt-2']
    with closing(sqlite3.connect(projection)) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 4
    assert _fd_count() == before


# LLM: 规范提交后一次模拟 I/O 错误只留下待发表行；save_run 正常返回，下一次读取重放并收敛到新状态。
# 函数用途: 验证提交后的投影失败不否认已经成功的规范写。
def test_publication_error_after_commit_returns_normally_and_converges(tmp_path, monkeypatch):
    store = run_store.WorkflowRunStore(tmp_path)
    record = _record('publish-run')
    store.create_run(record)
    publish = RunListIndex.publish_rows
    failed = []

    def flaky(index, rows):
        if not failed:
            failed.append(rows[0]['workflow_run_id'])
            raise OSError('simulated I/O error')
        return publish(index, rows)

    before = _fd_count()
    running = replace(record, status='running')
    with monkeypatch.context() as point:
        point.setattr(RunListIndex, 'publish_rows', flaky)
        store.save_run(running)
    assert failed == ['publish-run'] and running.revision == 2
    assert store.load_run('publish-run').status == 'running'
    assert RunListIndex(tmp_path).pending_ids() == ['publish-run']
    assert [run.workflow_run_id for run in store.list_runs(status='running')] == ['publish-run']
    assert store.list_runs(status='queued') == []
    assert RunListIndex(tmp_path).pending_ids() == []
    assert _fd_count() == before


# LLM: 投影叶为符号链接、data 目录为符号链接或叶有两个硬链接时仍拒绝打开；外部库字节不变，规范写照常完成。
# 函数用途: 验证新的私有打开方式保留链接拒绝，失效清理也不触碰外部文件。
@pytest.mark.parametrize('link', ['symlink', 'parent_symlink', 'hardlink'])
def test_linked_projection_is_still_rejected(tmp_path, link):
    home = tmp_path / 'owner'
    foreign = tmp_path / 'foreign'
    foreign.mkdir()
    foreign_db = foreign / 'workflow_run_index.sqlite3'
    with closing(sqlite3.connect(foreign_db)) as conn, conn:
        conn.execute('CREATE TABLE private(value)')
        conn.execute("INSERT INTO private VALUES('untouched')")
    foreign_bytes = foreign_db.read_bytes()
    home.mkdir()
    if link == 'parent_symlink':
        (home / 'data').symlink_to(foreign, target_is_directory=True)
    else:
        (home / 'data').mkdir()
        target = home / 'data' / 'workflow_run_index.sqlite3'
        if link == 'symlink':
            target.symlink_to(foreign_db)
        else:
            os.link(foreign_db, target)
    before = _fd_count()
    with pytest.raises(ValueError):
        with index_connection(home, 'workflow_run_index.sqlite3'):
            pytest.fail('linked projection was exposed')
    assert _fd_count() == before
    if link != 'parent_symlink':
        store = run_store.WorkflowRunStore(home)
        store.create_run(_record('linked-run'))
        assert store.load_run('linked-run').revision == 1
        assert _fd_count() == before
    assert foreign_db.read_bytes() == foreign_bytes


def _old_run_index(home, run_ids, *, version=2):
    path = home / 'data/workflow_run_index.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute('CREATE TABLE runs(run_id TEXT PRIMARY KEY,workflow_id TEXT NOT NULL,status TEXT NOT NULL,sort_time REAL NOT NULL,revision INTEGER NOT NULL)')
        conn.execute('CREATE INDEX runs_time ON runs(sort_time,run_id)')
        conn.execute('CREATE INDEX runs_workflow_time ON runs(workflow_id,sort_time,run_id)')
        conn.execute('CREATE INDEX runs_status_time ON runs(status,sort_time,run_id)')
        conn.execute('CREATE INDEX runs_workflow_status_time ON runs(workflow_id,status,sort_time,run_id)')
        conn.execute('CREATE TABLE legacy(run_id TEXT PRIMARY KEY)')
        conn.execute('CREATE TABLE pending(run_id TEXT PRIMARY KEY)')
        conn.executemany('INSERT INTO runs VALUES(?,?,?,?,?)', [(name, 'flow', 'succeeded', -1, 1) for name in run_ids])
        if version == 3:
            conn.execute('CREATE TABLE artifact_run_keys(run_key TEXT NOT NULL,run_id TEXT NOT NULL,PRIMARY KEY(run_key,run_id))')
        conn.execute(f'PRAGMA user_version={version}')
    return path


@pytest.mark.parametrize('version', [2, 3])
def test_old_long_run_keys_migrate_with_bounded_durable_keyset(tmp_path, monkeypatch, version):
    from agent_py_agent.agent.workflows.run_artifacts import workflow_artifact_run_key
    from agent_py_agent.agent.workflows.run_list_index import WorkflowArtifactLocatorIncomplete

    run_id = 'z' * 100
    path = _old_run_index(tmp_path, [f'a{number:05d}' for number in range(70)] + [run_id], version=version)
    index = RunListIndex(tmp_path)
    monkeypatch.setattr(run_list_index, 'list_names_beneath', lambda *a, **k: pytest.fail('artifact GET scanned run directories'))
    key = workflow_artifact_run_key(run_id)
    for expected_cursor in ('a00031', 'a00063'):
        with pytest.raises(WorkflowArtifactLocatorIncomplete):
            index.artifact_run_ids(key)
        with closing(sqlite3.connect(path)) as conn:
            assert conn.execute('SELECT after_run_id,complete FROM artifact_key_scan').fetchone() == (expected_cursor, 0)
    assert index.artifact_run_ids(key) == (run_id,)
    # 覆盖已完成且无待发表/旧记录时，才允许报告这个原索引中的不存在。
    assert index.artifact_run_ids('~' + '0' * 63) == ()


def test_v2_rebuilt_keymap_discards_stale_completed_v4_cursor(tmp_path, monkeypatch):
    """旧版回退仅丢keymap时，保留的v4完成游标不能证明新空映射已覆盖。"""
    from agent_py_agent.agent.workflows.run_artifacts import workflow_artifact_run_key

    run_id = 'z' * 100
    run_store.WorkflowRunStore(tmp_path).create_run(_record(run_id))
    index = RunListIndex(tmp_path)
    key = workflow_artifact_run_key(run_id)
    assert index.artifact_run_ids(key) == (run_id,)
    path = tmp_path / 'data/workflow_run_index.sqlite3'
    with closing(sqlite3.connect(path)) as conn, conn:
        assert conn.execute('SELECT complete FROM artifact_key_scan').fetchone() == (1,)
        conn.execute('DROP TABLE artifact_run_keys')
        conn.execute('PRAGMA user_version=2')
    monkeypatch.setattr(run_list_index, 'list_names_beneath',
        lambda *a, **k: pytest.fail('point locator scanned workflow directories'))
    assert index.artifact_run_ids(key) == (run_id,)


def test_missing_artifact_index_is_unknown_then_original_maintenance_recovers(tmp_path, monkeypatch):
    from agent_py_agent.agent.workflows.run_artifacts import workflow_artifact_run_key
    from agent_py_agent.agent.workflows.run_list_index import WorkflowArtifactLocatorIncomplete

    run_id = 'long-' + 'r' * 100
    store = run_store.WorkflowRunStore(tmp_path)
    store.create_run(_record(run_id))
    path = tmp_path / 'data/workflow_run_index.sqlite3'
    path.unlink()
    index = RunListIndex(tmp_path)
    with monkeypatch.context() as point:
        point.setattr(run_list_index, 'list_names_beneath', lambda *a, **k: pytest.fail('artifact GET scanned archive'))
        with pytest.raises(WorkflowArtifactLocatorIncomplete):
            index.artifact_run_ids(workflow_artifact_run_key(run_id))
    assert not path.exists()
    assert [row.workflow_run_id for row in store.list_runs()] == [run_id]
    assert index.artifact_run_ids(workflow_artifact_run_key(run_id)) == (run_id,)


@pytest.mark.parametrize('queue', ['legacy', 'pending'])
def test_unpublished_canonical_run_remains_unknown_until_original_maintenance(tmp_path, queue):
    from agent_py_agent.agent.workflows.run_artifacts import workflow_artifact_run_key
    from agent_py_agent.agent.workflows.run_list_index import WorkflowArtifactLocatorIncomplete

    run_id = 'old-' + 'x' * 100
    store = run_store.WorkflowRunStore(tmp_path)
    store.create_run(_record(run_id))
    path = _old_run_index(tmp_path, [])
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute('INSERT INTO ' + queue + ' VALUES(?)', (run_id,))
    index = RunListIndex(tmp_path)
    with pytest.raises(WorkflowArtifactLocatorIncomplete):
        index.artifact_run_ids(workflow_artifact_run_key(run_id))
    assert [row.workflow_run_id for row in store.list_runs()] == [run_id]
    assert index.artifact_run_ids(workflow_artifact_run_key(run_id)) == (run_id,)


def test_original_short_run_locator_never_opens_or_migrates_index(tmp_path, monkeypatch):
    from agent_py_agent.agent.workflows.run_artifacts import workflow_artifact_run_ids

    monkeypatch.setattr(RunListIndex, 'artifact_run_ids', lambda *a, **k: pytest.fail('short run opened locator'))
    assert workflow_artifact_run_ids(tmp_path, 'custom-short-run') == ('custom-short-run',)
    assert not (tmp_path/'data').exists()


# ============================================================================
# Source: agent_py_agent/tests/test_v14_r4_multiprocess_observation_acks.py
# ============================================================================

def _case(tmp_path, store_ref="conversations"):
    owner = OwnerIdentity.local_main()
    home = _owner_home_dir(tmp_path, owner)
    # 故意不初始化 managed_dirs 和 wake 索引，只由实际写者创建自身所需路径。
    store = ConversationStore(home / store_ref, initialize=False)
    thread = store.threads.get_or_create({"canonical_user_id": "u", "channel": "internal",
        "channel_conversation_id": "acks", "channel_user_id": "u", "now": 1})
    return home, store, thread.thread_id


def _r4_append(store, thread, key, **kwargs):
    return store.observations.append({"thread_id": thread, "_observation_id": key,
        "requires_main_agent": True, "summary": key, "now": 2, **kwargs})


def _freeze(root, store, thread):
    return freeze_source(root, OwnerIdentity.local_main(), store.storage.observation_path(thread), "observation")


def _fds():
    path = Path("/dev/fd") if Path("/dev/fd").exists() else Path("/proc/self/fd")
    return len(os.listdir(path))


@pytest.mark.parametrize("store_ref", ["conversations", "data/conversations",
    "workspace/runtime/workspaces/work-one/conversations"])
def test_worker_reads_only_unacknowledged_and_new_ack_invalidates_ref(tmp_path, store_ref):
    home, store, thread = _case(tmp_path, store_ref)
    first = _r4_append(store, thread, "acked")
    pending = _r4_append(store, thread, "pending", requires_main_agent=False, requires_llm_report=True)
    _r4_append(store, thread, "ordinary", requires_main_agent=False)
    raw = store.storage.observation_path(thread).read_bytes()
    store.observations.mark_handled([first.observation_id], now=10)
    assert not store.storage.observation_handled_path.exists()
    before = _fds()
    ref = _freeze(tmp_path, store, thread)
    for _ in range(5):
        assert read_source(tmp_path, ref) == {"observations": [pending.to_dict()], "thread_id": thread}
    store.observations.mark_handled([pending.observation_id], now=20)
    with pytest.raises(BackgroundSourceChanged):
        read_source(tmp_path, ref)
    successor = _freeze(tmp_path, store, thread)
    assert successor["source_sha256"] == ref["source_sha256"]
    assert successor["input_revision"] != ref["input_revision"]
    assert read_source(tmp_path, successor)["observations"] == []
    store.observations.mark_handled([pending.observation_id], now=30)
    assert _freeze(tmp_path, store, thread)["input_revision"] == successor["input_revision"]
    assert store.storage.observation_path(thread).read_bytes() == raw
    assert not store.storage.wake_task_index_path.exists()
    assert not store.storage.wake_queue_dir.exists()
    assert {path.name for path in store.storage.root.iterdir() if path.is_dir()} == {"threads", "observations"}
    fd_delta = _fds() - before
    assert fd_delta == 0
    print(json.dumps({"store_ref": store_ref, "fd_delta": fd_delta,
        "worker_ids": [pending.observation_id], "revision_changed": True}))
    assert home.is_dir()


def test_legacy_and_journal_merge_max_without_legacy_migration(tmp_path):
    home, store, thread = _case(tmp_path)
    for key in ("legacy", "both", "pending"):
        _r4_append(store, thread, key)
    store.observations.mark_handled(["both", "journal"], now=10)
    legacy = {**{f"old-{i}": 1 for i in range(300)}, "legacy": 20, "both": 30, "journal": 1}
    raw = json.dumps(legacy).encode()
    store.storage.observation_handled_path.write_bytes(raw)
    journal = store.observations._index.journal_path.read_bytes()
    before = _fds()
    handled, _ = observation_handled(home, "conversations", 1_048_576,
        observation_ids=("legacy", "both", "journal", "pending"))
    assert handled["both"] == 30 and handled["legacy"] == 20 and handled["journal"] == 10
    assert "pending" not in handled
    ref = _freeze(tmp_path, store, thread)
    assert [row["observation_id"] for row in read_source(tmp_path, ref)["observations"]] == ["pending"]
    assert store.storage.observation_handled_path.read_bytes() == raw
    assert store.observations._index.journal_path.read_bytes() == journal
    assert _fds() - before == 0


def test_worker_queries_all_raw_ids_before_payload_filtering(tmp_path, monkeypatch):
    _, store, thread = _case(tmp_path)
    _r4_append(store, thread, "pending")
    _r4_append(store, thread, "ordinary", requires_main_agent=False)
    _r4_append(store, thread, "embedded")
    # 历史已内嵌回执与普通观察也必须列入原分片精确ID查询。
    path = store.storage.observation_path(thread)
    rows = [json.loads(line) for line in path.read_bytes().splitlines()]
    rows[-1]["handled_at"] = 5
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    original = ObservationIndex.handled
    lookups = []

    def handled(index, ids, *, include_legacy=True):
        lookups.append((list(ids), include_legacy))
        return original(index, ids, include_legacy=include_legacy)

    monkeypatch.setattr(ObservationIndex, "handled", handled)
    ref = _freeze(tmp_path, store, thread)
    assert [row["observation_id"] for row in read_source(tmp_path, ref)["observations"]] == ["pending"]
    assert lookups and all(ids == ["pending", "ordinary", "embedded"] and not legacy
                           for ids, legacy in lookups)


def test_revision_uses_exact_legacy_bytes_and_nofollow_journal_identity(tmp_path):
    home, store, thread = _case(tmp_path)
    _r4_append(store, thread, "pending")
    for raw in (None, b"{}", b"{ }"):
        if raw is not None:
            store.storage.observation_handled_path.write_bytes(raw)
        _, revision = observation_handled(home, "conversations", 1_048_576)
        expected = hashlib.sha256(b"legacy:" + (b"missing" if raw is None else raw)
            + b"\0journal:" + json.dumps(None).encode()).hexdigest()
        assert revision == expected
    store.observations.mark_handled(["pending"], now=10)
    info = store.observations._index.journal_path.lstat()
    identity = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]
    expected = hashlib.sha256(b"legacy:{ }\0journal:" + json.dumps(identity).encode()).hexdigest()
    assert observation_handled(home, "conversations", 1_048_576)[1] == expected


def test_keyword_only_ids_and_revision_only_read_does_not_create_store(tmp_path):
    param = inspect.signature(observation_handled).parameters["observation_ids"]
    assert param.kind == inspect.Parameter.KEYWORD_ONLY and param.default == ()
    home = tmp_path / "home"
    home.mkdir()
    before = _fds()
    assert observation_handled(home, "data/conversations", 1_048_576)[0] == {}
    assert list(home.iterdir()) == []
    assert _fds() - before == 0


def test_partial_real_journal_maps_to_source_changed_and_resumes(tmp_path):
    _, store, thread = _case(tmp_path)
    pending = _r4_append(store, thread, "pending")
    store.observations.mark_handled([f"ack-{i}" for i in range(129)], now=10)
    ref = _freeze(tmp_path, store, thread)
    # 可丢弃投影损坏后必须逐固定批恢复，不能把未追平 journal 当空确认。
    store.observations._index.path.write_bytes(b"not sqlite")
    before = _fds()
    with pytest.raises(BackgroundSourceChanged) as caught:
        read_source(tmp_path, ref)
    assert isinstance(caught.value.__cause__, ObservationIndexIncomplete)
    assert read_source(tmp_path, ref)["observations"] == [pending.to_dict()]
    assert _fds() - before == 0


@pytest.mark.parametrize("change", ["bad-json", "array", "bool", "thread", "empty-id", "infinite"])
def test_invalid_shard_is_rejected_before_index_lookup(tmp_path, monkeypatch, change):
    _, store, thread = _case(tmp_path)
    event = _r4_append(store, thread, "pending")
    row = event.to_dict()
    if change == "bool":
        row["requires_llm_report"] = 1
    elif change == "thread":
        row["thread_id"] = "other"
    elif change == "empty-id":
        row["observation_id"] = ""
    elif change == "infinite":
        row["handled_at"] = "inf"
    raw = b"{bad\n" if change == "bad-json" else (b"[]\n" if change == "array" else json.dumps(row).encode() + b"\n")
    path = store.storage.observation_path(thread)
    path.write_bytes(raw)
    # freeze 会执行相同 worker 入口；不能先以宽松规则选出 id 再写投影。
    def unexpected_lookup(*args, **kwargs):
        pytest.fail("invalid shard reached observation index")
    monkeypatch.setattr(ObservationIndex, "handled", unexpected_lookup)
    with pytest.raises(ValueError):
        _freeze(tmp_path, store, thread)
    assert not store.observations._index.path.exists()


@pytest.mark.parametrize("target", ["store", "parent", "journal"])
def test_outside_home_symlinks_are_refused_without_writes(tmp_path, target):
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    if target == "store":
        (home / "conversations").symlink_to(outside, target_is_directory=True)
        store_ref = "conversations"
    elif target == "parent":
        (home / "data").symlink_to(outside, target_is_directory=True)
        store_ref = "data/conversations"
    else:
        (home / "conversations").mkdir()
        journal = outside / "acks.jsonl"
        journal.write_text('{"id":"x","at":10}\n')
        (home / ".conversations-observation-acks.jsonl").symlink_to(journal)
        store_ref = "conversations"
    original = sorted(str(p.relative_to(outside)) for p in outside.rglob("*"))
    before = _fds()
    with pytest.raises((OSError, ValueError)):
        observation_handled(home, store_ref, 1_048_576, observation_ids=("x",))
    assert sorted(str(p.relative_to(outside)) for p in outside.rglob("*")) == original
    assert _fds() - before == 0


# ============================================================================
# Source: agent_py_agent/tests/test_v14_script_run_out.py
# ============================================================================

# LLM: 源码只保存在pytest临时Owner，原资产Store负责合法描述符与IO合同。
# 函数用途: 保存一个输出value的真实Python脚本。
def save_script(home, body):
    return ScriptStore(home).save_script(AssetDescriptor.from_dict({
        'id': 'produce', 'name': 'produce', 'kind': 'script', 'access_targets': [],
        'data_sources': [], 'credential_refs': [], 'io_contract': {'outputs': ['value']},
    }), body, interpreter='python3', entrypoint='main.py')


# LLM: 新参数未实现时仍运行旧代码，红证据必须停在行为断言而非导入/构造失败。
# 函数用途: 创建有可选归档配额的真实执行器。
def runner(home, workspace, *, quota=True, restricted=False):
    options = {'use_sandbox': False, 'restrict_to_roots': restricted, 'workspace_root': workspace}
    if 'run_out_quota' in inspect.signature(ScriptRunner).parameters and quota:
        options['run_out_quota'] = (OwnerQuotaEnforcer(home, max_bytes=0) if quota is True else quota)
    return ScriptRunner(home, **options)


# LLM: 不改变步骤模式或脚本错误处理合同。
# 函数用途: 构造最小正式脚本工作流。
def definition():
    return {'descriptor': {'id': 'out-flow', 'name': 'out-flow', 'kind': 'workflow', 'io_contract': {}},
            'steps': [{'step_id': 'produce', 'name': 'produce', 'mode': 'deterministic_script',
                       'script_id': 'produce', 'on_error': 'fail'}]}


# LLM: 每个Owner和workspace均在pytest临时目录，不读取生产home。
# 函数用途: 准备真实脚本引擎、存储和工作区。
def setup(tmp_path, body, *, quota=True, restricted=False):
    home, workspace = tmp_path / 'global' / 'owners' / 'fixture', tmp_path / 'business'
    workspace.mkdir(parents=True, exist_ok=True)
    save_script(home, body)
    engine = WorkflowEngine(home, workspace_root=workspace)
    engine.runner = runner(home, workspace, quota=quota, restricted=restricted)
    return home, workspace, engine


# LLM: 缺少新环境变量时仍写旧scratch；从而观察文件被清理的原始缺陷。
# 函数用途: 生成可复现的真实脚本内容。
def code(body="(out / 'result.txt').write_text('中文产物', encoding='utf-8')", ending=None):
    return ("import json, os\nfrom pathlib import Path\n"
            "out = Path(os.environ.get('MY_AGENT_RUN_OUT', str(Path.home() / 'legacy')))\n"
            "out.mkdir(parents=True, exist_ok=True)\n" + body + '\n' +
            (ending if ending is not None else
             "print(json.dumps({'value':7, 'injected': 'MY_AGENT_RUN_OUT' in os.environ, "
             "'scratch': str(Path.home()), 'context': json.loads(os.environ.get('MY_AGENT_SCRIPT_WORK_CONTEXT_JSON', '{}'))}))\n"))


# LLM: 检查全部文件集合而非只测目录存在，不把失效scratch当产物。
# 函数用途: 对照真实字节、相对路径、大小和SHA256。
def assert_artifacts(row, home, run_id):
    assert getattr(row, 'output_dir', ''), '工作流文件产物没有归档目录'
    output = Path(row.output_dir)
    assert output.parent.parent == home / 'data' / 'workflow_runs' / run_id / 'artifacts'
    assert output.parent.name == 'produce'
    assert len(row.output_artifacts) == 1
    item = row.output_artifacts[0]
    raw = (output / item['path']).read_bytes()
    name_digest = hashlib.sha256(b'result.txt').hexdigest()[:24]
    assert item == {'path': 'result.txt', 'size_bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
                    'artifact_id': f'wfart-{run_id}-{output.name}-{name_digest}'}
    assert raw == '中文产物'.encode()
    assert not list(output.parent.glob('.staging-*'))


# LLM: WorkflowsTool、Engine、ScriptRunner、RunStore均真实；观察包装器只记录真实返回值。
# 函数用途: 验证工具运行到持久步骤、重启后工具投影的完整链路。
def test_real_tool_run_harvests_and_restart_lists_artifacts(tmp_path):
    home, workspace, engine = setup(tmp_path, code())
    WorkflowStore(home).save_workflow(definition())
    results, original = [], engine.runner.run_script

    # LLM: 不替换正式runner结果，只额外保存构造及透传后的对象供DTO核对。
    # 函数用途: 捕获真实脚本执行结果。
    def observe(*args, **kwargs):
        result = original(*args, **kwargs)
        results.append(result)
        return result

    engine.runner.run_script = observe
    tool = WorkflowsTool(home, engine=engine, workspace_root=workspace)
    outcome = tool.execute({'action': 'run', 'workflow_id': 'out-flow'})
    assert outcome.ok, outcome.output
    value = json.loads(outcome.output)
    run = engine.store.load_run(value['workflow_run_id'])
    assert run.status == 'succeeded' and results[0].ok
    assert_artifacts(run.steps[0], home, run.workflow_run_id)
    assert_artifacts(results[0], home, run.workflow_run_id)
    assert results[0].output_artifacts == run.steps[0].output_artifacts
    context = run.steps[0].output_snapshot['context']
    assert context['run_out_dir'] == str(Path(context['scratch_dir']) / 'run_out')
    assert not Path(context['scratch_dir']).exists()
    restarted = WorkflowsTool(home, engine=WorkflowEngine(home, workspace_root=workspace))
    inspected = restarted.execute({'action': 'inspect_run', 'workflow_run_id': run.workflow_run_id})
    assert inspected.ok
    assert json.loads(inspected.output)['steps'][0]['output_artifacts'] == list(run.steps[0].output_artifacts)


# LLM: 不把数据源或脚本错误换成归档成功；失败不能发布部分目录。
# 函数用途: 验证链接、特殊文件、上限、配额和脚本失败均有明确结果。
@pytest.mark.parametrize('body,ending,expected', [
    ("(out / 'first').write_text('ok'); (out / 'link').symlink_to(out / 'first')", None, 'SCRIPT_RUN_OUT_LINK_REJECTED'),
    ("os.mkfifo(out / 'pipe')", None, 'SCRIPT_RUN_OUT_LINK_REJECTED'),
    ("[(out / str(i)).write_bytes(b'x') for i in range(65)]", None, 'SCRIPT_RUN_OUT_LIMIT_EXCEEDED'),
    ("with (out / 'huge').open('wb') as f: f.truncate(64 * 1024 * 1024 + 1)", None, 'SCRIPT_RUN_OUT_LIMIT_EXCEEDED'),
    ("(out / 'result.txt').write_text('ok')", "raise SystemExit(4)", 'SCRIPT_EXIT_NONZERO'),
    ("(out / 'result.txt').write_text('ok')", "print('{invalid')", 'SCRIPT_INVALID_JSON'),
])
def test_invalid_or_failed_execution_publishes_nothing(tmp_path, body, ending, expected):
    home, _, engine = setup(tmp_path, code(body, ending))
    run = engine.run_workflow(definition())
    assert run.status == 'failed', run.to_dict()
    step = run.steps[0]
    assert step.error_code == expected, step.error_message
    assert not getattr(step, 'output_artifacts', ()) and not getattr(step, 'output_dir', '')
    assert not list((home / 'data' / 'workflow_runs' / run.workflow_run_id).glob('artifacts/produce/*'))


# LLM: 有限配额为真实OwnerQuotaEnforcer，不以假check代替计费。
# 函数用途: 验证磁盘配额拒绝后没有暂存或正式产物。
def test_quota_exhaustion_is_explicit_and_atomic(tmp_path):
    home, workspace, engine = setup(tmp_path, code())
    engine.runner = runner(home, workspace, quota=OwnerQuotaEnforcer(home, max_bytes=1))
    run = engine.run_workflow(definition())
    assert run.status == 'failed'
    assert run.steps[0].error_code == 'SCRIPT_RUN_OUT_QUOTA_EXCEEDED'
    assert not list((home / 'data' / 'workflow_runs' / run.workflow_run_id).glob('artifacts/produce/*'))


# LLM: 没有目标或没有配额都不注入；不把外部环境串到另一次脚本。
# 函数用途: 固定独立scripts.run、普通无quota工作流的旧行为及环境隔离。
@pytest.mark.parametrize('standalone', [False, True])
def test_no_target_or_no_quota_keeps_legacy_context(tmp_path, standalone):
    home, workspace, engine = setup(tmp_path, code(), quota=standalone)
    if standalone:
        result = engine.runner.run_script('produce', record_summary=False)
        assert result.ok
        output, serialized = result.output_data, models.payload(result)
    else:
        run = engine.run_workflow(definition())
        assert run.status == 'succeeded'
        output, serialized = run.steps[0].output_snapshot, run.steps[0].to_dict()
    assert output['injected'] is False and 'run_out_dir' not in output['context']
    assert 'output_dir' not in serialized and 'output_artifacts' not in serialized
    assert set(output['context']) == {'schema_version', 'workspace_root', 'scratch_dir', 'run_id', 'task_id', 'effective_path_scope'}


# LLM: 两个DTO扩展必须keyword-only，空字段不污染旧字典，未知键仍拒绝。
# 函数用途: 固定产物字段兼容及往返合同。
def test_dto_artifact_fields_and_roundtrip():
    for cls in (models.ScriptExecutionResult, models.StepExecutionRecord):
        selected = {item.name: item for item in fields(cls)}
        assert {'output_dir', 'output_artifacts'} <= selected.keys()
        assert selected['output_dir'].kw_only and selected['output_artifacts'].kw_only
    step = models.StepExecutionRecord('s', 's', 'deterministic_script', 'succeeded', 0, 0, 0, False)
    original = step.to_dict()
    assert 'output_dir' not in original and 'output_artifacts' not in original
    step.output_dir, step.output_artifacts = '/archive', ({'path': 'a', 'size_bytes': 1, 'sha256': 'x'},)
    assert models.StepExecutionRecord.from_dict(step.to_dict()) == step
    result = models.ScriptExecutionResult('s', True, 0, 0, 0, 0, {}, '', '')
    assert 'output_artifacts' not in result.to_dict()
    result.output_dir, result.output_artifacts = step.output_dir, step.output_artifacts
    assert models.ScriptExecutionResult.from_dict(result.to_dict()) == result
    with pytest.raises(TypeError):
        models.StepExecutionRecord.from_dict({**original, 'unknown': True})


# LLM: 宿主若拒绝os.link，显式模拟nlink元数据，不冒称真实硬链接端到端通过；正文读取仍必须为零。
# 函数用途: 在真实可创建硬链接的平台或受控nlink夹具上验证同一归档拒绝门。
def test_hardlink_rejected_before_any_content_read(tmp_path, monkeypatch, record_property):
    from types import SimpleNamespace

    from agent_py_agent.agent.workflows import run_artifacts as artifacts

    scratch = tmp_path / 'scratch'
    out = scratch / 'run_out'
    out.mkdir(parents=True)
    source = out / 'first'
    source.write_bytes(b'x')
    try:
        os.link(source, out / 'hard')
        record_property('hardlink_fixture', 'native')
    except PermissionError:
        record_property('hardlink_fixture', 'simulated_nlink_after_native_permission_denial')
        original = artifacts.os.scandir

        # LLM: 仅在原生硬链接权限拒绝后模拟nlink元数据，不把降级fixture称为原生E2E。
        # 类用途: 为硬链接拒绝门保留真实目录枚举和受控链接数。
        class Children:
            # LLM: 沿原描述符创建枚举器，不额外读取文件正文。
            # 函数用途: 持有可显式关闭的目录枚举器。
            def __init__(self, descriptor):
                self.children = original(descriptor)

            # LLM: 只改链接数观察值，其余目录入口沿真实枚举器返回。
            # 函数用途: 提供会被拒绝门识别的受控元数据。
            def __enter__(self):
                return (SimpleNamespace(name=entry.name, stat=lambda **kw: SimpleNamespace(
                    st_mode=source.stat().st_mode, st_nlink=2, st_size=1)) for entry in self.children)

            # LLM: 异常与正常退出都关闭真实目录描述符。
            # 函数用途: 不让降级测试夹具泄漏FD。
            def __exit__(self, *args):
                self.children.close()

        monkeypatch.setattr(artifacts.os, 'scandir', Children)

    # LLM: 发生任何正文读取都直接报错，不能以已返回链接码掩盖提前读取。
    # 函数用途: 固定链接拒绝发生在正文读取之前。
    def no_read(*args, **kwargs):
        pytest.fail('链接拒绝之前不应读取任何正文')

    monkeypatch.setattr(artifacts, 'read_bytes_beneath', no_read)
    with pytest.raises(artifacts.RunOutError) as caught:
        artifacts._manifest(scratch, 64, 64 * 1024 * 1024)
    assert caught.value.error_code == 'SCRIPT_RUN_OUT_LINK_REJECTED'


# LLM: 对真实目录制造超额，任何正文读取都使测试失败；不是先全量读取再断言计数。
# 函数用途: 固定文件数及字节热路径上限。
@pytest.mark.parametrize('kind', ['files', 'bytes'])
def test_caps_stop_before_reading_content(tmp_path, monkeypatch, kind):
    from agent_py_agent.agent.workflows import run_artifacts as artifacts

    scratch = tmp_path / 'scratch'
    out = scratch / 'run_out'
    out.mkdir(parents=True)
    if kind == 'files':
        for number in range(65):
            (out / str(number)).write_bytes(b'x')
    else:
        with (out / 'large').open('wb') as stream:
            stream.truncate(ScriptRunner.RUN_OUT_MAX_BYTES + 1)

    # LLM: 超额清单必须在读取正文之前结束，替身不能返回伪造数据使断言变绿。
    # 函数用途: 拦截超额后的任何正文读取。
    def no_read(*args, **kwargs):
        pytest.fail('超额清单不允许进入正文读取')

    monkeypatch.setattr(artifacts, 'read_bytes_beneath', no_read)
    with pytest.raises(artifacts.RunOutError) as caught:
        artifacts._manifest(scratch, ScriptRunner.RUN_OUT_MAX_FILES, ScriptRunner.RUN_OUT_MAX_BYTES)
    assert caught.value.error_code == 'SCRIPT_RUN_OUT_LIMIT_EXCEEDED'


# LLM: 故障发生在真实暂存已有文件之后或真实rename之后，要求整个集合回滚；不允许空执行假成功。
# 函数用途: 验证复制、rename/ACK及fsync失败不发表部分产物。
@pytest.mark.parametrize('phase', ['copy', 'rename_before', 'rename_after', 'fsync'])
def test_harvest_failure_rolls_back_whole_attempt(tmp_path, monkeypatch, phase):
    from types import SimpleNamespace

    from agent_py_agent.agent.workflows import run_artifacts as artifacts

    home, _, engine = setup(tmp_path, code("(out / 'a').write_bytes(b'a'); (out / 'b').write_bytes(b'b')"))
    original_write, original_rename, original_fsync = artifacts.write_bytes_atomic_beneath, artifacts.os.rename, artifacts.os.fsync
    state = {'copies': 0, 'faults': 0, 'parent_identity': None}

    # LLM: 只在第二份复制注入一次明确I/O错误，其余复制沿正式nofollow写入。
    # 函数用途: 验证集合中途复制失败不会发布半套产物。
    def copy(*args, **kwargs):
        state['copies'] += 1
        if phase == 'copy' and state['copies'] == 2:
            state['faults'] += 1
            raise OSError('simulated I/O error')
        return original_write(*args, **kwargs)

    # LLM: 区分rename执行前失败和已执行后ACK失败，不影响无关rename。
    # 函数用途: 覆盖原子发布两侧的失败与回滚。
    def rename(source, target, **kwargs):
        if not str(source).startswith('.staging-'):
            return original_rename(source, target, **kwargs)
        if phase == 'rename_before':
            state['faults'] += 1
            raise OSError('simulated rename error')
        result = original_rename(source, target, **kwargs)
        info = os.fstat(kwargs['src_dir_fd'])
        state['parent_identity'] = (info.st_dev, info.st_ino)
        if phase == 'rename_after':
            state['faults'] += 1
            raise OSError('simulated rename acknowledgement error')
        return result

    # LLM: 仅目标父目录第一次确认刷盘失败，其余fsync沿原端口。
    # 函数用途: 验证发布确认失败也撤回整个集合。
    def fsync(descriptor):
        info = os.fstat(descriptor)
        if phase == 'fsync' and not state['faults'] and (info.st_dev, info.st_ino) == state['parent_identity']:
            state['faults'] += 1
            raise OSError('simulated fsync error')
        return original_fsync(descriptor)

    monkeypatch.setattr(artifacts, 'write_bytes_atomic_beneath', copy)
    # 只替换归档模块端口；修改全局os.rename会破坏nofollow平台能力集合的函数身份。
    monkeypatch.setattr(artifacts, 'os', SimpleNamespace(**{**vars(os), 'rename': rename, 'fsync': fsync}))
    run = engine.run_workflow(definition())
    assert state['copies'] == 2 and state['faults'] == 1
    assert run.status == 'failed' and run.steps[0].error_code == 'SCRIPT_RUN_OUT_HARVEST_FAILED'
    assert not run.steps[0].output_artifacts
    assert not list((home / 'data' / 'workflow_runs' / run.workflow_run_id).glob('artifacts/produce/*'))


# LLM: 数据源清理真实执行后注入明确失败，不能因已暂存就发布成功。
# 函数用途: 验证scratch退出失败会删除已有暂存。
@pytest.mark.parametrize('policy', ['fail', 'skip', 'escalate_to_agent'])
def test_datasource_cleanup_failure_discards_staged_files(tmp_path, monkeypatch, policy):
    home, _, engine = setup(tmp_path, code())
    close = runner_module.ScriptDatasourceRuntime.close

    # LLM: 先运行真实清理，再抛明确错误，不伪造资源已关闭的事实。
    # 函数用途: 固定数据源清理失败的归档硬失败出口。
    def fail_close(runtime):
        close(runtime)
        raise OSError('simulated cleanup error')

    monkeypatch.setattr(runner_module.ScriptDatasourceRuntime, 'close', fail_close)
    flow, repairs = definition(), []
    flow['steps'][0]['on_error'] = policy
    engine.escalation_handler = lambda *args: repairs.append(1) or {'value': 7}
    run = engine.run_workflow(flow)
    assert run.status == 'failed' and run.steps[0].status == 'failed'
    assert run.steps[0].error_code == 'SCRIPT_RUN_OUT_HARVEST_FAILED'
    assert repairs == [] and not run.steps[0].output_artifacts
    assert not list((home / 'data' / 'workflow_runs' / run.workflow_run_id).glob('artifacts/produce/*'))


# LLM: 100次都经过真实脚本、复制、rename与RunStore；先预热解释器及摘要缓存，再比较同一FD观察点。
# 函数用途: 固定批量归档无文件描述符泄漏。
def test_hundred_harvested_runs_have_zero_fd_delta(tmp_path, record_property):
    home, _, engine = setup(tmp_path, code())
    assert engine.run_workflow(definition()).status == 'succeeded'
    descriptors = '/dev/fd' if Path('/dev/fd').is_dir() else '/proc/self/fd'
    before = len(os.listdir(descriptors))
    for _ in range(100):
        run = engine.run_workflow(definition())
        assert run.status == 'succeeded'
        assert_artifacts(run.steps[0], home, run.workflow_run_id)
    delta = len(os.listdir(descriptors)) - before
    record_property('fd_delta', delta)
    assert delta == 0


# LLM: Python路径守卫真实运行，run_out只是既有scratch子目录，不增加Owner脚本或tasks写根。
# 函数用途: 验证归档启用后原拒写范围仍生效。
def test_run_out_does_not_widen_script_write_scope(tmp_path):
    home = tmp_path / 'global' / 'owners' / 'fixture'
    (home / 'tasks').mkdir(parents=True)
    body = (
        "blocked = 0\n"
        f"for name in {list(map(str, (home / 'tasks' / 'probe', home / 'scripts' / 'produce' / 'main.py')))!r}:\n"
        "    try:\n        Path(name).write_text('unexpected')\n"
        "    except PermissionError:\n        blocked += 1\n"
        "assert blocked == 2\n"
        "(out / 'result.txt').write_text('中文产物', encoding='utf-8')"
    )
    home, _, engine = setup(tmp_path, code(body), restricted=True)
    run = engine.run_workflow(definition())
    assert run.status == 'succeeded', run.to_dict()
    assert_artifacts(run.steps[0], home, run.workflow_run_id)
    assert not (home / 'tasks' / 'probe').exists()
    assert ScriptStore(home).get_script('produce')['code'] == code(body)


# LLM: 全程使用真实账本/执行器，Event只控制首个create到执行锁的交错点。
# 函数用途: 固定取消后重启不复活，新运行归档后再重启仍可见。
def test_headless_cancel_restart_resume_then_artifact_continuity(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    home, workspace, engine = setup(tmp_path, code())
    committed, release = threading.Event(), threading.Event()
    original = engine.store.create_run

    # LLM: 真实运行提交后暂停，下一步只由Event放行，不改持久状态。
    # 函数用途: 为重启连续性用例固定启动取消窗口。
    def create(*args, **kwargs):
        original(*args, **kwargs)
        committed.set()
        assert release.wait(10)

    monkeypatch.setattr(engine.store, 'create_run', create)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(engine.run_workflow, definition(), workflow_run_id='cancel-window')
        try:
            assert committed.wait(5)
            row = engine.store.load_run('cancel-window')
            assert engine.cancel_workflow_run(row.workflow_run_id, expected_revision=row.revision, operation_key='cancel') == []
        finally:
            release.set()
        assert future.result(5).status == 'cancelled'
    restart = WorkflowEngine(home, workspace_root=workspace)
    with pytest.raises(ValueError, match='WORKFLOW_CANCELLED'):
        restart.resume_workflow_run('cancel-window')
    restart.runner = runner(home, workspace)
    new = restart.run_workflow(definition())
    assert new.status == 'succeeded'
    second_restart = WorkflowEngine(home, workspace_root=workspace)
    assert second_restart.store.load_run('cancel-window').status == 'cancelled'
    assert_artifacts(second_restart.store.load_run(new.workflow_run_id).steps[0], home, new.workflow_run_id)


# LLM: 记录check实参但仍执行真实有限配额准入与结算，不替换投影结果。
# 函数用途: 验证每个复制字节计费以及发表迁移同时记删除和增加。
def test_real_quota_accounts_for_stage_and_publish_bytes(tmp_path):
    from contextlib import contextmanager

    home, workspace, engine = setup(tmp_path, code())
    observed = []

    # LLM: 继承真实配额执行器，仅观察check实参，不放宽限额或替代结算。
    # 类用途: 捕获暂存与发布的逐字节配额变化。
    class RecordingQuota(OwnerQuotaEnforcer):
        # LLM: 原准入事务完整进入和退出，观察器不能吞掉拒绝。
        # 函数用途: 在真实准入中安装只读记账探针。
        @contextmanager
        def admission(self, **kwargs):
            with super().admission(**kwargs) as admission:
                check = admission.check

                # LLM: 保留原check返回值和错误，记录的数据是同一批变更。
                # 函数用途: 观察配额变化集合而不修改实际计算。
                def record(changes, **options):
                    changes = tuple(changes)
                    observed.append(changes)
                    return check(changes, **options)

                admission.check = record
                yield admission

    engine.runner = runner(home, workspace, quota=RecordingQuota(home, max_bytes=10 * 1024 * 1024))
    run = engine.run_workflow(definition())
    assert run.status == 'succeeded', run.to_dict()
    assert_artifacts(run.steps[0], home, run.workflow_run_id)
    size = len('中文产物'.encode())
    assert len(observed) == 2
    assert [change.final_size for change in observed[0]] == [size]
    assert '.staging-' in str(observed[0][0].path)
    assert [change.final_size for change in observed[1]] == [None, size]
    assert str(observed[1][1].path.parent) == run.steps[0].output_dir


# LLM: 文件归档损失不是可跳过或可由模型伪造输出恢复的成功，不改变其它脚本错误政策。
# 函数用途: 验证归档硬失败保留机器码且不会被skip/escalation吞掉。
@pytest.mark.parametrize('policy', ['fail', 'skip', 'escalate_to_agent'])
def test_harvest_failure_is_not_silently_skipped_or_recovered(tmp_path, policy):
    home, _, engine = setup(tmp_path, code("(out / 'source').write_bytes(b'x'); (out / 'link').symlink_to(out / 'source')"))
    flow = definition()
    flow['steps'][0]['on_error'] = policy
    repairs = []
    engine.escalation_handler = lambda *args: repairs.append(1) or {'value': 7}
    run = engine.run_workflow(flow)
    assert run.status == 'failed' and run.steps[0].status == 'failed'
    assert run.steps[0].error_code == 'SCRIPT_RUN_OUT_LINK_REJECTED'
    assert repairs == []
    assert not list((home / 'data' / 'workflow_runs' / run.workflow_run_id).glob('artifacts/produce/*'))


# LLM: 显式debug可执行脚本但没有正式运行目标，不能建立永久归档或污染正式运行账。
# 函数用途: 固定单步调试仍不注入run_out。
def test_ephemeral_debug_does_not_inject_or_harvest(tmp_path):
    home, _, engine = setup(tmp_path, code())
    WorkflowStore(home).save_workflow(definition())
    result = engine.debug_step('out-flow', 'produce', allow_side_effects=True)
    assert result['status'] == 'succeeded', result
    assert result['output_data']['injected'] is False
    assert 'run_out_dir' not in result['output_data']['context']
    assert not (home / 'data' / 'workflow_runs').exists()


# LLM: 宿主环境的同名目录不代表本次授权；无正式目标/无quota/debug都不得继承或归档它。
# 函数用途: 验证三种非正式归档调用先清除继承run-out环境。
@pytest.mark.parametrize('mode', ['standalone', 'no_quota', 'debug'])
def test_inactive_run_out_ignores_inherited_environment(tmp_path, monkeypatch, mode):
    inherited = tmp_path / 'inherited'
    inherited.mkdir()
    monkeypatch.setenv('MY_AGENT_RUN_OUT', str(inherited))
    home, workspace, engine = setup(tmp_path, code())
    if mode == 'standalone':
        result = engine.runner.run_script('produce')
        assert result.ok, result.to_dict()
        output = result.output_data
    elif mode == 'no_quota':
        engine.runner = runner(home, workspace, quota=None)
        result = engine.run_workflow(definition())
        assert result.status == 'succeeded', result.to_dict()
        output = result.steps[0].output_snapshot
    else:
        WorkflowStore(home).save_workflow(definition())
        result = engine.debug_step('out-flow', 'produce', allow_side_effects=True)
        assert result['status'] == 'succeeded', result
        output = result['output_data']
    assert output['injected'] is False
    assert 'run_out_dir' not in output['context']
    assert list(inherited.iterdir()) == []


# LLM: rename已经生效后发生首因，再让递归清理持续失败；最终命名空间必须先原子撤回，稳定码和首因不得丢。
# 函数用途: 验证回滚自身失败只残留私有staging，不留下半套正式产物。
@pytest.mark.parametrize('phase', ['ack', 'fsync'])
def test_publish_rollback_failure_withdraws_final_set_before_cleanup(tmp_path, monkeypatch, phase):
    from types import SimpleNamespace

    from agent_py_agent.agent.workflows import run_artifacts as artifacts

    home, scratch = tmp_path / 'owner', tmp_path / 'scratch'
    home.mkdir()
    (scratch / 'run_out').mkdir(parents=True)
    for name in ('a', 'b'):
        (scratch / 'run_out' / name).write_bytes(name.encode())
    batch = artifacts.RunArtifactBatch(artifacts.RunOutTarget(home, 'run', 'step', 'attempt'),
                                      OwnerQuotaEnforcer(home, max_bytes=1024 * 1024))
    assert batch.stage(scratch, max_files=64, max_bytes=1024)
    original_os, original_shutil = artifacts.os, artifacts.shutil
    primary = OSError('simulated publish error')
    state, removed = {'renamed': False, 'failed': False}, []

    # LLM: 仅正式发布rename后注入ACK失败，撤回rename保持真实。
    # 函数用途: 制造已生效但未确认的发布首因。
    def rename(source, target, **kwargs):
        result = original_os.rename(source, target, **kwargs)
        if source == batch.staging_name and target == batch.target.attempt_id:
            state['renamed'] = True
            if phase == 'ack':
                state['failed'] = True
                raise primary
        return result

    # LLM: 发布后的首个父目录刷盘失败一次，撤回清理的刷盘不替换首因。
    # 函数用途: 注入发布耐久性失败。
    def fsync(descriptor):
        if phase == 'fsync' and state['renamed'] and not state['failed']:
            state['failed'] = True
            raise primary
        return original_os.fsync(descriptor)

    # LLM: 先真实删除一个叶子，再持续拒绝其余清理；若在final名称递归删，就会观察到残缺正式集合。
    # 函数用途: 固定回滚清理也失败的I/O控制。
    def fail_cleanup(name, *, dir_fd):
        removed.append(name)
        child = original_os.open(name, original_os.O_RDONLY | original_os.O_DIRECTORY, dir_fd=dir_fd)
        try:
            try:
                original_os.unlink('a', dir_fd=child)
            except FileNotFoundError:
                pass
        finally:
            original_os.close(child)
        raise OSError('simulated cleanup error')

    monkeypatch.setattr(artifacts, 'os', SimpleNamespace(**{**vars(original_os), 'rename': rename, 'fsync': fsync}))
    monkeypatch.setattr(artifacts, 'shutil', SimpleNamespace(**{**vars(original_shutil), 'rmtree': fail_cleanup}))
    try:
        batch.publish()
    except (OSError, ValueError) as exc:
        error = exc
    else:
        pytest.fail('归档失败不能返回成功引用')
    parent = home.joinpath(*batch.parent_parts)
    assert state['failed'] and removed
    assert not (parent / 'attempt').exists()
    assert all(name != 'attempt' for name in removed)
    assert isinstance(error, artifacts.RunOutError) and error.error_code == 'SCRIPT_RUN_OUT_HARVEST_FAILED'
    assert error.__cause__ is primary and not batch.published
    assert (parent / batch.staging_name / 'b').read_bytes() == b'b'
    monkeypatch.setattr(artifacts, 'shutil', original_shutil)
    batch.discard()
    assert list(parent.iterdir()) == []


# LLM: 仅在正式归档端口注入ACK/fsync及回滚I/O失败，runner、engine、Store都真实执行。
# 函数用途: 固定原始errno与回滚诊断从runner DTO到耐久步骤的脱敏透传。
@pytest.mark.parametrize('phase', ['ack', 'fsync'])
def test_workflow_preserves_publish_cause_and_rollback_diagnostics(tmp_path, monkeypatch, phase):
    import errno
    from types import SimpleNamespace

    from agent_py_agent.agent.workflows import run_artifacts as artifacts

    home, _, engine = setup(tmp_path, code())
    original_os, original_shutil = artifacts.os, artifacts.shutil
    run_script, results = engine.runner.run_script, []
    state = {'renamed': False, 'failed': False, 'cleanups': 0}
    primary = OSError(errno.EIO, 'private diagnostic text')

    # LLM: 原执行结果不被替换；捕获同一正式runner返回对象。
    # 函数用途: 核对runner而非仅batch层的诊断。
    def observe(*args, **kwargs):
        result = run_script(*args, **kwargs)
        results.append(result)
        return result

    # LLM: 仅staging到final的rename后失败，回滚撤回仍执行真实原子rename。
    # 函数用途: 注入已生效但未确认的归档发布。
    def rename(source, target, **kwargs):
        result = original_os.rename(source, target, **kwargs)
        if str(source).startswith('.staging-'):
            state['renamed'] = True
            if phase == 'ack':
                state['failed'] = True
                raise primary
        return result

    # LLM: 只让发布后首次fsync失败，后续撤回的刷盘不阻塞。
    # 函数用途: 以真实OSError errno固定发布耐久性首因。
    def fsync(descriptor):
        if phase == 'fsync' and state['renamed'] and not state['failed']:
            state['failed'] = True
            raise primary
        return original_os.fsync(descriptor)

    # LLM: 只替换归档模块清理端口，不妨碍真实scratch及数据源清理。
    # 函数用途: 在发布首因之后持续制造私有staging清理失败。
    def fail_cleanup(*args, **kwargs):
        state['cleanups'] += 1
        raise OSError(errno.EPERM, 'private rollback text')

    monkeypatch.setattr(engine.runner, 'run_script', observe)
    monkeypatch.setattr(artifacts, 'os', SimpleNamespace(**{**vars(original_os), 'rename': rename, 'fsync': fsync}))
    monkeypatch.setattr(artifacts, 'shutil', SimpleNamespace(**{**vars(original_shutil), 'rmtree': fail_cleanup}))
    run = engine.run_workflow(definition())
    assert state['failed'] and state['cleanups'] and len(results) == 1
    assert run.status == 'failed' and results[0].error_code == 'SCRIPT_RUN_OUT_HARVEST_FAILED'
    diagnostic = results[0].escalation_context['execution_error']
    assert diagnostic.get('cause_chain') == [{'exception_type': 'OSError', 'errno': errno.EIO}]
    assert diagnostic.get('rollback_incomplete') is True
    assert 'private diagnostic text' not in json.dumps(diagnostic)
    assert 'private rollback text' not in json.dumps(diagnostic)
    persisted = engine.store.load_run(run.workflow_run_id)
    assert persisted.steps[0].escalation_result['execution_error'] == diagnostic
    assert not persisted.steps[0].output_artifacts and not results[0].output_artifacts
    parent = home / 'data' / 'workflow_runs' / run.workflow_run_id / 'artifacts' / 'produce'
    remaining = list(parent.iterdir())
    assert remaining and all(path.name.startswith('.staging-') for path in remaining)
    for path in remaining:
        original_shutil.rmtree(path)


@pytest.mark.parametrize('phase,path_role', [
    ('run_out_prepare', 'run_out_dir'),
    ('manifest_read', 'run_out_dir'),
    ('data_source_close', 'datasource_manifest'),
    ('scratch_cleanup', 'staging_root'),
])
def test_archive_missing_leaf_reports_safe_exact_phase(tmp_path, monkeypatch, phase, path_role):
    """同一errno须定位到宿主实际阶段；不回显路径，也不把执行后故障说成未启动。"""
    from agent_py_agent.agent.workflows import datasource_runtime, run_artifacts

    home, _, engine = setup(tmp_path, 'import json\nprint(json.dumps({"value":7}))')
    WorkflowStore(home).save_workflow(definition())

    def missing():
        return FileNotFoundError(2, 'private customer secret', '/private/customer/token-secret')

    if phase == 'run_out_prepare':
        original = runner_module.open_directory_beneath

        def open_directory(root, parts, **options):
            if parts == ('run_out',) and options.get('create'):
                raise missing()
            return original(root, parts, **options)

        monkeypatch.setattr(runner_module, 'open_directory_beneath', open_directory)
    elif phase == 'manifest_read':
        def manifest(*_args):
            raise missing()

        monkeypatch.setattr(run_artifacts, '_manifest', manifest)
    elif phase == 'data_source_close':
        original = datasource_runtime.ScriptDatasourceRuntime.close

        def close(runtime):
            original(runtime)
            raise missing()

        monkeypatch.setattr(datasource_runtime.ScriptDatasourceRuntime, 'close', close)
    else:
        original = runner_module.shutil.rmtree

        def remove(path, *args, **options):
            result = original(path, *args, **options)
            if Path(path).name.startswith('produce_'):
                raise missing()
            return result

        monkeypatch.setattr(runner_module.shutil, 'rmtree', remove)
    record = engine.run_workflow(definition(), owner_id='local/main')
    step = record.steps[0]
    assert record.status == 'failed' and step.error_code == 'SCRIPT_RUN_OUT_HARVEST_FAILED'
    facts = step.escalation_result['execution_error']
    assert facts['archive_phase'] == phase and facts['path_role'] == path_role
    assert {'exception_type': 'FileNotFoundError', 'errno': 2} in facts['cause_chain']
    expected = 'not_started' if phase == 'run_out_prepare' else 'unknown'
    assert step.escalation_result['effect_outcome'] == expected
    assert 'customer' not in json.dumps(record.to_dict()) and 'token-secret' not in json.dumps(record.to_dict())


@pytest.mark.parametrize('operation', ['publish', 'discard'])
def test_archive_rethrow_keeps_original_io_cause(tmp_path, monkeypatch, operation):
    """回滚重复遇同一RunOutError时不能用自身覆盖__cause__，丢掉原errno。"""
    from agent_py_agent.agent.workflows.run_artifacts import (
        RunArtifactBatch,
        RunOutError,
        RunOutTarget,
    )

    home, scratch = tmp_path / 'owner', tmp_path / 'scratch'
    home.mkdir()
    (scratch / 'run_out').mkdir(parents=True)
    (scratch / 'run_out/item.txt').write_text('synthetic')
    batch = RunArtifactBatch(RunOutTarget(home, 'new-diagnostic', 'produce', 'a' * 32),
                             OwnerQuotaEnforcer(home, max_bytes=0))
    assert batch.stage(scratch, max_files=64, max_bytes=1024)
    original = batch._parent
    cause = FileNotFoundError(2, 'private path', '/private/customer/secret')
    failure = RunOutError('SCRIPT_RUN_OUT_HARVEST_FAILED')
    failure.__cause__ = cause

    def missing_parent(**_options):
        raise failure

    with monkeypatch.context() as patch:
        patch.setattr(batch, '_parent', missing_parent)
        with pytest.raises(RunOutError) as caught:
            getattr(batch, operation)()
        assert caught.value is failure and failure.__cause__ is cause
        assert {'exception_type': 'FileNotFoundError', 'errno': 2} in failure.diagnostics()['cause_chain']
    assert batch._parent == original
    batch.discard()


# ============================================================================
# Source: agent_py_agent/tests/test_v14_w1_background_cursor_successor.py
# ============================================================================

# LLM: wire正文与字节边界由真实ConversationStore/page reader产生；只替换HTTP出口，无模型、网络或真实用户数据。
# 模块用途: 回归跨窗口/new的thread游标重基、归属去重、安全拒绝和迟到页隔离。





# LLM: 独立owner存储，thread通过真实succeed更换，未手造notice行或字节偏移返回值。
# 函数用途: 创建映射到固定聊天通道的真实会话。
def _store(tmp_path):
    store = ConversationStore(tmp_path / 'conversations')
    thread = store.threads.get_or_create({'canonical_user_id': 'local-agent', 'channel': 'chat',
        'channel_conversation_id': 'session', 'channel_user_id': 'local-agent', 'now': 1.0,
        'owner_id': 'owner-test', 'owner_home': str(tmp_path)})
    return store, thread.thread_id


# LLM: 前台final的归属元数据走生产append入口；不加background_delivery_reason，避免冒充后台续跑。
# 函数用途: 在canonical消息流写下一个真实可投影final。
def _w1_append(store, thread_id, text, request_id='gwreq-owner'):
    return store.messages.append({'thread_id': thread_id, 'role': 'assistant', 'content': text, 'now': 20.0,
        'metadata': {'assistant_part_id': 'final', 'gateway_request_id': request_id}})


# LLM: 只组装HTTP信封；notices、cursor和ok全部取真实server reader，失败也保留已解析的绑定。
# 类用途: 记录每次poll的输入和真实页结果，供接收端端到端回归。
class _Wire:
    # LLM: 读数仅用于断言，不替代生产runtime或通知去重。
    # 函数用途: 保存当前server线程和有限poll日志。
    def __init__(self, store, thread_id):
        self.store, self.thread_id, self.calls = store, thread_id, []

    # LLM: 生产API同形传输出口，每次只做一个有界消息页读取；不查全历史或凭文本伪造失败。
    # 函数用途: 用真实byte reader返回本次线程页，并原样记录失败种类。
    def request_background_notices(self, _session_id, *, after, event_after, event_stream_id):
        notices, cursor, ok = read_background_response_page(self.store, self.thread_id, after=after,
                                                           include_foreground=True)
        failure = '' if ok else 'corrupt_or_unknown'
        self.calls.append({'thread_id': self.thread_id, 'after': after, 'cursor': cursor,
                           'failure_kind': failure, 'ok': ok})
        print(json.dumps(self.calls[-1], ensure_ascii=False))
        return {'ok': ok, 'thread_id': self.thread_id, 'binding_state': 'confirmed',
                'failure_kind': failure, 'cursor': cursor, 'notices': notices,
                'transcript_events': [], 'event_cursor': event_after, 'event_stream_id': event_stream_id,
                'agent_activity': {'active_task_count': 0, 'compact_count': 0,
                                   'context_usage': {}, 'model_metrics': {}}}


def test_real_successor_invalid_16737_rebases_once_and_filters_owned_final(tmp_path):
    store, old = _store(tmp_path)
    _w1_append(store, old, '')
    row_size = store.storage.message_path(old).stat().st_size
    _w1_append(store, old, 'x' * (16737 - 2 * row_size))
    assert store.storage.message_path(old).stat().st_size == 16737
    successor = store.threads.succeed(old).thread_id
    own = _w1_append(store, successor, 'B own ' + 'b' * 4500)
    other = _w1_append(store, successor, 'A visible ' + 'a' * 18000, 'gwreq-other')
    first_boundary = store.messages.byte_offset_after(successor, own.message_id)
    end = store.messages.byte_offset_after(successor, other.message_id)
    assert first_boundary < 16737 < end
    assert read_background_response_page(store, successor, after=16737, include_foreground=True) == ([], 16737, False)
    wire, runtime, seen = _Wire(store, successor), TuiRuntime('session'), set()
    runtime.background_message_cursor, runtime.background_message_thread_id = 16737, old
    runtime.register_gateway_request('gwreq-owner')
    redraws = []
    app = SimpleNamespace(invalidate=lambda: redraws.append(1))
    assert _consume_background_notices(wire, 'session', runtime, [app], seen)
    assert runtime.background_message_cursor == 0 and runtime.background_message_thread_id == successor
    assert not runtime.store.snapshot().stable_blocks and not seen
    assert redraws
    for _ in range(3):
        assert _consume_background_notices(wire, 'session', runtime, [app], seen)
    assert [row['after'] for row in wire.calls] == [16737, 0, end, end]
    assert len(wire.calls) == 4 and sum(row['after'] == 0 for row in wire.calls) == 1
    texts = [block.text for block in runtime.store.snapshot().stable_blocks]
    assert texts.count(other.content) == 1 and own.content not in texts
    assert not any(text.startswith('x') for text in texts)
    assert runtime.background_message_cursor == end
    assert runtime.store.snapshot().status.thread_id == successor


def test_successor_equal_file_length_uses_identity_not_offset(tmp_path):
    store, old = _store(tmp_path)
    _w1_append(store, old, 'equal length old')
    successor = store.threads.succeed(old).thread_id
    message = _w1_append(store, successor, 'equal length new')
    old_size = store.storage.message_path(old).stat().st_size
    assert old_size == store.storage.message_path(successor).stat().st_size
    wire, runtime, seen = _Wire(store, successor), TuiRuntime('session'), set()
    runtime.background_message_cursor, runtime.background_message_thread_id = old_size, old
    assert _consume_background_notices(wire, 'session', runtime, [None], seen)
    assert runtime.background_message_cursor == 0
    assert _consume_background_notices(wire, 'session', runtime, [None], seen)
    assert _consume_background_notices(wire, 'session', runtime, [None], seen)
    assert [row['after'] for row in wire.calls] == [old_size, 0, old_size]
    assert [block.text for block in runtime.store.snapshot().stable_blocks] == [message.content]


@pytest.mark.parametrize('patch', [
    {'binding_state': 'invalid'}, {'error_code': 'PERMISSION_DENIED'},
    {'failure_kind': 'owner_mismatch'}, {'thread_id': 2}, {'thread_id': ' bad '},
    {'execution_target': {'thread_id': 'different-thread'}},
])
def test_untrusted_successor_never_rebases_or_publishes(tmp_path, patch):
    store, old = _store(tmp_path)
    successor = store.threads.succeed(old).thread_id
    _w1_append(store, successor, 'must not display')
    wire, runtime = _Wire(store, successor), TuiRuntime('session')
    runtime.background_message_cursor, runtime.background_message_thread_id = 16737, old
    original = wire.request_background_notices
    wire.request_background_notices = lambda *args, **kwargs: {**original(*args, **kwargs), **patch}
    assert not _consume_background_notices(wire, 'session', runtime, [None], set())
    assert (runtime.background_message_cursor, runtime.background_message_thread_id) == (16737, old)
    assert not runtime.store.snapshot().stable_blocks


def test_late_page_from_old_poll_cannot_revert_a_successor_rebase(tmp_path):
    store, old = _store(tmp_path)
    _w1_append(store, old, 'old must stay absent')
    successor = store.threads.succeed(old).thread_id
    current = _w1_append(store, successor, 'new stays visible')
    runtime, seen = TuiRuntime('session'), set()
    runtime.background_message_thread_id = old
    started, release, results = Event(), Event(), []
    old_wire, wire = _Wire(store, old), _Wire(store, successor)
    original = old_wire.request_background_notices
    # LLM: 事件排序只延迟传输返回；线程数据和回调仍来自真实store/client链。
    # 函数用途: 保留一张旧poll，等新线程重基完成后才送达。
    def delayed(*args, **kwargs):
        payload = original(*args, **kwargs)
        started.set()
        assert release.wait(5)
        return payload
    old_wire.request_background_notices = delayed
    thread = Thread(target=lambda: results.append(_consume_background_notices(old_wire, 'session', runtime, [None], seen)))
    thread.start()
    try:
        assert started.wait(5)
        assert _consume_background_notices(wire, 'session', runtime, [None], seen)
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert runtime.background_message_thread_id == successor and runtime.background_message_cursor == 0
    assert not runtime.store.snapshot().stable_blocks
    assert _consume_background_notices(wire, 'session', runtime, [None], seen)
    assert [block.text for block in runtime.store.snapshot().stable_blocks] == [current.content]


def test_reset_during_fetch_discards_page_and_rebase(tmp_path):
    store, old = _store(tmp_path)
    successor = store.threads.succeed(old).thread_id
    _w1_append(store, successor, 'late response')
    wire, runtime = _Wire(store, successor), TuiRuntime('session')
    runtime.background_message_cursor, runtime.background_message_thread_id = 16737, old
    original = wire.request_background_notices
    # LLM: 同步回调发生reset，不用sleep制造竞态。
    # 函数用途: 验证捕获代次之后返回的旧页没有任何副作用。
    def fetch(*args, **kwargs):
        payload = original(*args, **kwargs)
        runtime.publish_conversation_reset('newer-thread')
        return payload
    wire.request_background_notices = fetch
    assert _consume_background_notices(wire, 'session', runtime, [None], set())
    assert (runtime.background_message_cursor, runtime.background_message_thread_id) == (0, 'newer-thread')
    assert not runtime.store.snapshot().stable_blocks


def test_local_successor_rebinds_before_reading_and_never_replays_old_rows(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import message_stream
    store, old = _store(tmp_path)
    _w1_append(store, old, 'local old', request_id='')
    runtime, seen = TuiRuntime('session'), set()
    runtime.background_message_cursor = store.storage.message_path(old).stat().st_size
    runtime.background_message_thread_id = old
    successor = store.threads.succeed(old).thread_id
    message = store.messages.append({'thread_id': successor, 'role': 'assistant', 'content': 'local new',
        'metadata': {'assistant_part_id': 'final', 'background_delivery_reason': 'root_subagents_terminal'}})
    reads = []
    real_reader = message_stream.read_background_response_page
    # LLM: wrapper只计数并调用同一真实页读取，不返回定制行或吞掉校验错误。
    # 函数用途: 证明本地路径不再把旧offset交给新文件。
    def read(store, thread_id, **kwargs):
        reads.append((thread_id, kwargs['after']))
        return real_reader(store, thread_id, **kwargs)
    monkeypatch.setattr(message_stream, 'read_background_response_page', read)
    agent = SimpleNamespace(conversation_store=store)
    assert _consume_background_notices(agent, 'session', runtime, [None], seen)
    assert runtime.background_message_thread_id == successor
    assert _consume_background_notices(agent, 'session', runtime, [None], seen)
    assert reads[0] == (successor, 0)
    assert [block.text for block in runtime.store.snapshot().stable_blocks] == [message.content]


# LLM: canonical final行带请求真实thread；旧cursor不能替新前台请求决定线程归属。
# 函数用途: 检查先begin后确认successor且先消费owned final时回答仍显示一次。
def test_review_owned_successor_final_binds_exact_foreground_request(tmp_path):
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiTurnSummary
    store, old = _store(tmp_path)
    _w1_append(store, old, 'old row')
    successor = store.threads.succeed(old).thread_id
    own = _w1_append(store, successor, 'owned successor reply', 'gwreq-owner')
    runtime, wire, seen = TuiRuntime('session'), _Wire(store, successor), set()
    runtime.background_message_thread_id = old
    runtime.register_gateway_request('gwreq-owner')
    old_turn = runtime.begin_turn('other-old')
    runtime.begin_turn('new-local-id', task_progress_generation_id='gwreq-owner')
    assert _consume_background_notices(wire, 'session', runtime, [None], seen)
    assert _consume_background_notices(wire, 'session', runtime, [None], seen)
    assert (successor, own.message_id) in seen
    runtime.complete_turn('new-local-id', TuiTurnSummary(response_text=own.content))
    texts = [block.text for block in runtime.store.snapshot().stable_blocks]
    assert texts.count(own.content) == 1
    assert old_turn._thread_id == old
    assert not any(block.block_id.startswith('thinking:new-local-id:') for block in runtime.store.snapshot().active_blocks)


# LLM: 延迟发生在真实reader边界；保留navigation与外层finally生产实现。
# 函数用途: 迟到T1页不能在T2重基后把后台观察范围切回旧线程。
def test_review_late_local_poll_keeps_successor_navigation_scope(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import background_process_view, message_stream
    from agent_py_agent.cli.chat_parts.tui_agent_navigation import TuiAgentNavigationState
    store, old = _store(tmp_path)
    runtime, seen = TuiRuntime('session'), set()
    runtime.background_message_thread_id = old
    nav = TuiAgentNavigationState(runtime)
    agent = SimpleNamespace(conversation_store=store,
        home_paths=SimpleNamespace(owner_home_dir=tmp_path, owner_id='owner-test'))
    monkeypatch.setattr(background_process_view, 'list_background_processes',
        lambda _home, *, thread_id, expected_owner_id: {'projection_ok': True, 'items': [], 'active_count': 0, 'errors': []})
    started, release, errors = Event(), Event(), []
    reader = message_stream.read_background_response_page
    # LLM: 只延迟旧线程原页返回，不伪造IO结果。
    # 函数用途: 等待第二poll建立新观察范围后放行旧页。
    def delayed(store, thread_id, **kwargs):
        value = reader(store, thread_id, **kwargs)
        if thread_id == old:
            started.set()
            assert release.wait(5)
        return value
    monkeypatch.setattr(message_stream, 'read_background_response_page', delayed)
    # LLM: 异常交回主线程，结束前join不遗留后台工作。
    # 函数用途: 执行真正的旧本地poll，包括外层finally。
    def poll_old():
        try:
            _consume_background_notices(agent, 'session', runtime, [None], seen, agent_navigation=nav)
        except BaseException as exc:
            errors.append(exc)
    thread = Thread(target=poll_old)
    thread.start()
    try:
        assert started.wait(5)
        successor = store.threads.succeed(old).thread_id
        assert _consume_background_notices(agent, 'session', runtime, [None], seen, agent_navigation=nav)
        assert _consume_background_notices(agent, 'session', runtime, [None], seen, agent_navigation=nav)
        assert nav._background_watch_scope[3] == successor
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
    assert runtime.background_message_thread_id == successor
    assert nav._background_watch_scope[3] == successor


# LLM: Event仅排序完整finalize与真实notice消费；不替换reducer、final正文、cursor或去重。
# 函数用途: 覆盖pop后及finalize后两条交接缝隙，成功正文必须恰好显示一次且无残留thinking。
@pytest.mark.parametrize('arrival', ['before_finalize', 'after_finalize'])
def test_review_owned_final_during_completion_handoff(tmp_path, monkeypatch, arrival):
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiTurnSummary
    store, old = _store(tmp_path)
    successor = store.threads.succeed(old).thread_id
    own = _w1_append(store, successor, 'handoff successor reply', 'gwreq-owner')
    runtime, wire, seen = TuiRuntime('session'), _Wire(store, successor), set()
    runtime.background_message_thread_id = old
    runtime.register_gateway_request('gwreq-owner')
    old_turn = runtime.begin_turn('other-old')
    adapter = runtime.begin_turn('new-local-id', task_progress_generation_id='gwreq-owner')
    assert _consume_background_notices(wire, 'session', runtime, [None], seen)
    entered, release, errors = Event(), Event(), []
    finalize = adapter.finalize

    # LLM: complete_turn已进入真实完成路径；保留原finalize全部副作用，只暂停交接顺序。
    # 函数用途: 把owned final放在pop后或原finalize后、release前送达。
    def delayed(summary):
        if arrival == 'after_finalize':
            finalize(summary)
        entered.set()
        assert release.wait(5)
        if arrival == 'before_finalize':
            finalize(summary)

    monkeypatch.setattr(adapter, 'finalize', delayed)

    # LLM: 不吞工作线程异常，所有退出均join，运行结果回到主测试线程断言。
    # 函数用途: 运行真实complete_turn，允许通知线程完整进入显示交接窗口。
    def complete():
        try:
            runtime.complete_turn('new-local-id', TuiTurnSummary(response_text=own.content))
        except BaseException as exc:
            errors.append(exc)

    worker = Thread(target=complete)
    worker.start()
    try:
        assert entered.wait(5)
        assert _consume_background_notices(wire, 'session', runtime, [None], seen)
        assert (successor, own.message_id) in seen
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and not errors
    for _ in range(2):
        assert _consume_background_notices(wire, 'session', runtime, [None], seen)
    view = runtime.store.snapshot()
    assert [block.text for block in view.stable_blocks].count(own.content) == 1
    assert not any(block.block_id.startswith('thinking:new-local-id:') for block in view.active_blocks)
    assert old_turn._thread_id == old
    assert 'new-local-id' not in runtime._turns and 'new-local-id' not in runtime._retired_turns
    assert 'new-local-id' not in runtime._foreground_gateway_bindings
    assert 'gwreq-owner' not in runtime._owned_gateway_requests


# LLM: 第一张canonical final页严格晚于真实complete_turn；同正文不同请求仍须分别显示。
# 函数用途: 前台已经交付后才首次轮询，不能因释放adapter而再显示一次相同请求。
@pytest.mark.parametrize('thread_known', [True, False])
def test_review_first_canonical_final_after_release_is_not_duplicated(tmp_path, thread_known):
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiTurnSummary
    store, thread_id = _store(tmp_path)
    own = _w1_append(store, thread_id, 'already delivered reply')
    runtime, wire, seen = TuiRuntime('session'), _Wire(store, thread_id), set()
    if thread_known:
        runtime.background_message_thread_id = thread_id
    runtime.register_gateway_request('gwreq-owner')
    runtime.begin_turn('local-owner', task_progress_generation_id='gwreq-owner')
    runtime.complete_turn('local-owner', TuiTurnSummary(response_text=own.content))
    assert [block.text for block in runtime.store.snapshot().stable_blocks].count(own.content) == 1
    assert not seen
    assert _consume_background_notices(wire, 'session', runtime, [None], seen)
    assert (thread_id, own.message_id) in seen
    assert [block.text for block in runtime.store.snapshot().stable_blocks].count(own.content) == 1
    other = _w1_append(store, thread_id, own.content, 'gwreq-unrelated')
    assert _consume_background_notices(wire, 'session', runtime, [None], seen)
    assert (thread_id, other.message_id) in seen
    assert [block.text for block in runtime.store.snapshot().stable_blocks].count(own.content) == 2
    assert not runtime._turns and not runtime._completing_turns
    assert not runtime._foreground_gateway_bindings and not runtime._owned_gateway_requests


# LLM: 过程行由生产append/read产生；legacy变体只去掉可选thread字段，源观察仍在T1捕获。
# 函数用途: T1暂存过程在真实T2重基后必须丢弃，不能重开旧正文或动画。
@pytest.mark.parametrize('legacy_thread', [False, True])
def test_review_deferred_process_cannot_replay_on_successor(tmp_path, legacy_thread):
    from agent_py_agent.agent.conversation import background_transcript as transcript
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiTurnSummary
    store, old = _store(tmp_path)
    runtime = TuiRuntime('session')
    runtime.background_message_thread_id = old
    runtime.register_gateway_request('gwreq-owner')
    runtime.begin_turn('old-local', task_progress_generation_id='gwreq-owner')
    source = transcript.TranscriptSource(thread_id=old, task_id='task-old',
        request_id='bg-main:old-request', gateway_request_id='gwreq-owner')
    agent = SimpleNamespace()
    transcript.append_background_transcript_event(agent, source=source, kind='thinking_started',
        phase='started', block_id='bg-main:old-request:thinking:1', payload={'text': 'old progress'})
    transcript.append_background_transcript_event(agent, source=source, kind='assistant_completed',
        phase='completed', block_id='bg-main:old-request:assistant:1', payload={'text': 'retired response'})
    rows = transcript.read_background_transcript_events(agent, thread_id=old, after=0)['events']
    if legacy_thread:
        rows = [{key: value for key, value in row.items() if key != 'thread_id'} for row in rows]
    assert runtime.publish_background_transcript_events(rows) == 2
    assert len(runtime._deferred_background_events) == 2
    successor = store.threads.succeed(old).thread_id
    assert _consume_background_notices(_Wire(store, successor), 'session', runtime, [None], set())
    runtime.complete_turn('old-local', TuiTurnSummary(response_text='retired response'))
    view = runtime.store.snapshot()
    assert runtime.background_message_thread_id == successor
    assert not any(block.block_id.startswith('bg-main:old-request:')
                   for block in (*view.stable_blocks, *view.active_blocks))
    assert not runtime._deferred_background_events


# LLM: 保留原finalize与真实reader，仅在两者之间确定排序；不能用额外旧adapter掩盖running残留。
# 函数用途: canonical补显后关闭精确前台观察，同时不能结束另一个还在运行的回合。
@pytest.mark.parametrize('another_running', [False, True])
def test_review_fallback_final_closes_only_its_foreground_observation(tmp_path, monkeypatch, another_running):
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiTurnSummary
    store, old = _store(tmp_path)
    successor = store.threads.succeed(old).thread_id
    own = _w1_append(store, successor, 'late confirmed final')
    runtime, wire, seen = TuiRuntime('session'), _Wire(store, successor), set()
    runtime.background_message_thread_id = old
    runtime.register_gateway_request('gwreq-owner')
    adapter = runtime.begin_turn('local-owner', task_progress_generation_id='gwreq-owner')
    assert _consume_background_notices(wire, 'session', runtime, [None], seen)
    other = runtime.begin_turn('concurrent-new') if another_running else None
    finalize = adapter.finalize

    # LLM: 不改finalizer结果，不伪造notice；只把canonical首次消费放在finalize后、release前。
    # 函数用途: 重现正文可补显但前台running状态未收口的交错。
    def final_then_notice(summary):
        finalize(summary)
        assert _consume_background_notices(wire, 'session', runtime, [None], seen)

    monkeypatch.setattr(adapter, 'finalize', final_then_notice)
    runtime.complete_turn('local-owner', TuiTurnSummary(response_text=own.content))
    view = runtime.store.snapshot()
    assert [block.text for block in view.stable_blocks].count(own.content) == 1
    assert not any(block.block_id.startswith('thinking:local-owner:') for block in view.active_blocks)
    assert view.status.phase == ('running' if another_running else 'idle')
    assert view.has_active_work is another_running
    if other is not None:
        assert not other._finished and runtime._turns['concurrent-new'] is other


# LLM: 同一canonical请求的生产过程流首次晚于前台完成；不能靠消息页seen掩盖过程流入口。
# 函数用途: 已交付请求的迟到过程不重复正文、不重开thinking，别的请求仍正常展示。
def test_review_late_process_after_release_does_not_reopen_delivered_request(tmp_path):
    from agent_py_agent.agent.conversation import background_transcript as transcript
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiTurnSummary
    store, thread_id = _store(tmp_path)
    own = _w1_append(store, thread_id, 'process late reply')
    runtime = TuiRuntime('session')
    runtime.background_message_thread_id = thread_id
    runtime.register_gateway_request('gwreq-owner')
    runtime.begin_turn('local-owner', task_progress_generation_id='gwreq-owner')
    runtime.complete_turn('local-owner', TuiTurnSummary(response_text=own.content))
    agent = SimpleNamespace()
    source = transcript.TranscriptSource(thread_id=thread_id, task_id='task-owner',
        request_id='bg-main:owner', gateway_request_id='gwreq-owner')
    for kind, phase, suffix, text in [('thinking_started', 'started', 'thinking', 'old thinking'),
                                     ('assistant_completed', 'completed', 'assistant', own.content)]:
        transcript.append_background_transcript_event(agent, source=source, kind=kind, phase=phase,
            block_id=f'bg-main:owner:{suffix}', payload={'text': text})
    rows = transcript.read_background_transcript_events(agent, thread_id=thread_id, after=0)['events']
    assert runtime.publish_background_transcript_events(rows) == 2
    view = runtime.store.snapshot()
    assert [block.text for block in view.stable_blocks].count(own.content) == 1
    assert not any(block.block_id.startswith('bg-main:owner:') for block in (*view.stable_blocks, *view.active_blocks))
    unrelated = transcript.TranscriptSource(thread_id=thread_id, task_id='task-other',
        request_id='bg-main:other', gateway_request_id='gwreq-other')
    transcript.append_background_transcript_event(agent, source=unrelated, kind='assistant_completed',
        phase='completed', block_id='bg-main:other:assistant', payload={'text': own.content})
    rows = transcript.read_background_transcript_events(agent, thread_id=thread_id, after=2)['events']
    assert runtime.publish_background_transcript_events(rows) == 3
    assert [block.text for block in runtime.store.snapshot().stable_blocks].count(own.content) == 2


# LLM: 复用生产有界身份窗口，仅缩小测试容量；记录中不得含adapter或正文，reset后旧身份不能吞新页。
# 函数用途: 连续交付时淘汰旧身份，结束后清引用，并验证去重围栏也包含reset代次。
def test_review_delivery_identity_window_is_bounded_and_generation_scoped():
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiTurnSummary
    runtime = TuiRuntime('session')
    runtime._delivered_foreground_requests.maximum = 2
    for index in range(3):
        gateway_id, local_id = f'gwreq-{index}', f'local-{index}'
        runtime.register_gateway_request(gateway_id)
        runtime.begin_turn(local_id, task_progress_generation_id=gateway_id)
        runtime.complete_turn(local_id, TuiTurnSummary(response_text=f'answer {index}'))
        assert len(runtime._delivered_foreground_requests) <= 2
    assert {key[0] for key in runtime._delivered_foreground_requests} == {'gwreq-1', 'gwreq-2'}
    assert all(isinstance(key, tuple) and len(key) == 3 and isinstance(key[2], int)
               for key in runtime._delivered_foreground_requests)
    assert not runtime._turns and not runtime._retired_turns and not runtime._completing_turns
    assert not runtime._foreground_gateway_bindings
    runtime.publish_conversation_reset('thread-next')
    runtime.publish_background_response('new generation reply', thread_id='thread-next',
        message_id='next-message', gateway_request_id='gwreq-2')
    assert [block.text for block in runtime.store.snapshot().stable_blocks] == ['new generation reply']


# LLM: Direct入口不注册Gateway ownership；用真实本地poll重基，验证显示phase清理不依赖远端归属表。
# 函数用途: 旧本地回合跨successor结束后不残留running，另一在途回合仍可独立运行并正常结束。
@pytest.mark.parametrize('overlap', [False, True])
def test_review_local_successor_completion_releases_only_its_display_phase(tmp_path, overlap):
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiTurnSummary
    store, old = _store(tmp_path)
    runtime = TuiRuntime('session')
    runtime.background_message_thread_id = old
    adapter = runtime.begin_turn('local-old')
    generation = runtime.conversation_reset_generation
    assert not runtime._owned_gateway_requests
    successor = store.threads.succeed(old).thread_id
    agent = SimpleNamespace(conversation_store=store)
    assert _consume_background_notices(agent, 'session', runtime, [None], set())
    assert runtime.background_message_thread_id == successor
    assert runtime.conversation_reset_generation == generation
    current = runtime.begin_turn('local-new') if overlap else None
    runtime.complete_turn('local-old', TuiTurnSummary(response_text='old local final'))
    view = runtime.store.snapshot()
    assert view.status.phase == ('running' if overlap else 'idle')
    assert 'old local final' not in [block.text for block in view.stable_blocks]
    assert not any(block.block_id in adapter._display_block_ids for block in view.active_blocks)
    if current is None:
        current = runtime.begin_turn('local-new')
    assert not current._finished
    runtime.complete_turn('local-new', TuiTurnSummary(response_text='successor local reply'))
    view = runtime.store.snapshot()
    assert view.status.phase == 'idle'
    assert [block.text for block in view.stable_blocks].count('successor local reply') == 1
    assert not runtime._turns and not runtime._retired_turns and not runtime._completing_turns
    assert not runtime._foreground_gateway_bindings and not runtime._owned_gateway_requests


# ============================================================================
# Source: agent_py_agent/tests/test_v14_w1_failure_diagnostics.py
# ============================================================================

# LLM: 公共诊断只补充无 user_error 的最终通用提示，不改变错误映射和执行语义。
# 模块用途: 覆盖 W1 通用失败诊断、输入展示边界及真实 TUI 终态发表。





# LLM: 使用真实 runtime 接收 summary，不访问网络或模型，也不补造终态。
# 函数用途: 构造最小调用上下文，取得失败 summary 与真实 runtime。
def _outcome(fields):
    runtime = TuiRuntime("failure-diagnostics")
    ctx = SimpleNamespace(
        job=SimpleNamespace(show_prompt=False),
        cfg=SimpleNamespace(
            tui_runtime=runtime, state_lock=threading.Lock(), last_token_estimate_ref=[0],
        ),
    )
    text, recorded, summary = _gateway_outcome(ctx, {"ok": False, **fields})
    assert text == ""
    assert recorded is False
    assert summary.ok is False
    return summary, runtime


def test_generic_failure_shows_only_public_fields_in_compact_order():
    summary, _ = _outcome({
        "error_code": "UNCLASSIFIED_FAILURE", "request_id": "gwreq-public",
        "duration_seconds": 85.24, "attempts": 2,
        "error": "private provider detail", "diagnostic_ref": "private reference",
        "id": "not-the-public-request-id", "metadata": {"attempts": 9},
    })
    assert summary.error == (
        gateway_client_error_message(None)
        + "（错误码 UNCLASSIFIED_FAILURE · 请求 gwreq-public · 用时 85.2s · 尝试 2 次）"
    )


@pytest.mark.parametrize("fields,suffix", [
    ({"error_code": "UNCLASSIFIED_FAILURE"}, "错误码 UNCLASSIFIED_FAILURE"),
    ({"request_id": "gwreq-only"}, "请求 gwreq-only"),
    ({"request_id": "请求甲"}, "请求 请求甲"),
    ({"duration_seconds": 0}, "用时 0.0s"),
    ({"duration_seconds": 85}, "用时 85.0s"),
    ({"duration_seconds": 85.24}, "用时 85.2s"),
    ({"attempts": 0}, "尝试 0 次"),
    ({"attempts": 2}, "尝试 2 次"),
])
def test_individual_valid_diagnostics(fields, suffix):
    summary, _ = _outcome(fields)
    assert summary.error == gateway_client_error_message(None) + f"（{suffix}）"


@pytest.mark.parametrize("field", ["error_code", "request_id"])
@pytest.mark.parametrize("value", [
    None, "", "   ", False, 42, [], {},
    "line\nbreak", "tab\there", "return\rhere", "nul\x00here", "esc\x1b[31m",
    "delete\x7f", "next\x85line", "direction\u202ereverse",
])
def test_invalid_identifiers_are_not_displayed(field, value):
    summary, _ = _outcome({field: value})
    assert summary.error == gateway_client_error_message(None)


@pytest.mark.parametrize("field", ["error_code", "request_id"])
def test_long_identifiers_do_not_expand_the_diagnostic_without_bound(field):
    summary, _ = _outcome({field: "X" * 1000})
    assert summary.error.startswith(gateway_client_error_message(None))
    assert summary.error.isprintable()
    # 长字段可以截断或丢弃；不把某个具体截断阈值冻结为运行协议。
    assert len(summary.error) < 300


@pytest.mark.parametrize("value", [
    None, "85.2", "bad", "", True, False, [], {}, -1, float("nan"),
    float("inf"), float("-inf"), pytest.param(10**5000, id="oversized-int"),
])
def test_invalid_durations_do_not_break_the_error_path(value):
    summary, _ = _outcome({"duration_seconds": value})
    assert summary.error == gateway_client_error_message(None)


@pytest.mark.parametrize("value", [
    None, "2", "bad", "", True, False, [], {}, -1, 2.0, 2.5,
    float("nan"), float("inf"), pytest.param(10**5000, id="oversized-int"),
])
def test_invalid_attempt_counts_do_not_break_the_error_path(value):
    summary, _ = _outcome({"attempts": value})
    assert summary.error == gateway_client_error_message(None)


def test_large_finite_duration_still_has_bounded_display():
    summary, _ = _outcome({"duration_seconds": 1e300})
    assert summary.error.startswith(gateway_client_error_message(None))
    assert summary.error.isprintable()
    assert len(summary.error) < 300


def test_invalid_fields_do_not_hide_an_independent_valid_diagnostic():
    summary, _ = _outcome({
        "error_code": "bad\ncode", "request_id": "gwreq-valid",
        "duration_seconds": float("nan"), "attempts": True, "error": "private detail",
    })
    assert summary.error == gateway_client_error_message(None) + "（请求 gwreq-valid）"


@pytest.mark.parametrize("user_error", [
    "模型服务拒绝了当前配置。", gateway_client_error_message(None), " 原文保持空格。 ",
])
def test_user_error_is_byte_identical_even_when_it_equals_the_generic_text(user_error):
    summary, _ = _outcome({
        "user_error": user_error, "error_code": "UNCLASSIFIED_FAILURE",
        "request_id": "gwreq-public", "duration_seconds": 85.2, "attempts": 2,
    })
    assert summary.error.encode("utf-8") == user_error.encode("utf-8")


@pytest.mark.parametrize("code", [
    "PROVIDER_REQUEST_REJECTED", "PROVIDERTIMEOUTERROR", "COMPACT_UNKNOWN",
    "MODEL_UNKNOWN", "MODEL_NOT_CONFIGURED",
])
def test_specific_code_mapping_is_byte_identical(code):
    summary, _ = _outcome({
        "error_code": code, "request_id": "gwreq-public", "duration_seconds": 85.2,
        "attempts": 2, "error": "private detail",
    })
    assert summary.error.encode("utf-8") == gateway_client_error_message(code).encode("utf-8")


def test_no_public_diagnostics_keeps_exact_generic_text_and_never_uses_raw_error():
    summary, _ = _outcome({
        "error": "private detail", "id": "private-id", "diagnostic_ref": "private-ref",
        "metadata": {"error_code": "nested-code"},
    })
    assert summary.error == gateway_client_error_message(None)


def test_diagnostic_summary_reaches_the_real_runtime_failure_block():
    summary, runtime = _outcome({"request_id": "gwreq-visible", "error": "private detail"})
    runtime.begin_turn("failure-turn").finalize(summary)
    snapshot = runtime.store.snapshot()
    assert not snapshot.has_active_work
    assert any("请求 gwreq-visible" in block.text for block in snapshot.stable_blocks)
    assert not any("private detail" in block.text for block in snapshot.stable_blocks)


def test_gateway_outcome_signature_is_preserved():
    signature = inspect.signature(_gateway_outcome)
    assert str(signature) == "(ctx: 'Any', response: 'dict[str, Any]') -> 'tuple[str, bool, TuiTurnSummary]'"


# ============================================================================
# Source: agent_py_agent/tests/test_v14_w1_reset_inflight_turn.py
# ============================================================================

# LLM: 使用真实运行时、worker和本地执行入口，Event控制交错；仅Agent为有限的内存替身。
# 模块用途: 守住重置期间的回合责任、终态、队列清理与旧显示隔离。





# LLM: 队列仍执行真实计数，只用Event观察已消费两条任务，不用sleep猜测时序。
# 类用途: 在最后一条任务确认后通知测试线程。
class _ObservedQueue(queue.Queue):
    def __init__(self):
        super().__init__()
        self.drained = threading.Event()

    def task_done(self):
        super().task_done()
        if not self.unfinished_tasks:
            self.drained.set()


# LLM: 不连接模型或Gateway，run仍由原_worker_local_path调用，输出走真实adapter。
# 类用途: 用两个Event冻结首轮，让主线程准确插入reset和stop。
class _Agent:
    def __init__(self):
        self.config = SimpleNamespace(chat_history_max_turns=20)
        self.started = threading.Event()
        self.release = threading.Event()
        self.second = threading.Event()
        self.stopped = False

    def run(self, prompt, **kwargs):
        if prompt == 'old-input':
            self.started.set()
            assert self.release.wait(5), 'first turn release missing'
            kwargs['on_chunk'].write_model('old-visible', public_stream=True)
            if self.stopped:
                raise InterruptedError('confirmed local stop')
            text = 'old-reply'
        else:
            self.second.set()
            text = 'new-reply'
        return SimpleNamespace(response=text, prompt=prompt, runtime_status='ok', tool_rounds=0)


# LLM: 仅构造原worker依赖，历史、控制句柄、runtime与队列均共享实际引用。
# 函数用途: 启动有限测试worker并捕获其真实异常。
def _worker_case(*, show_prompt=False):
    runtime = TuiRuntime('reset-worker')
    agent = _Agent()
    jobs = _ObservedQueue()
    cfg = tui_worker.TuiWorkerConfig(jobs=jobs, state_lock=threading.RLock(),
        is_running_ref=[False], pending_jobs_ref=[2], running_prompt_ref=[''],
        running_request_id_ref=[''], running_started_at_ref=[0.0], agent=agent,
        args=SimpleNamespace(), paths=None, use_gateway=False, conversation_history=[],
        history_lock=threading.RLock(), build_history_context=lambda: '', assistant_outputs=[],
        last_token_estimate_ref=[0], stop_event=threading.Event(), tui_runtime=runtime)
    failures = []

    def work():
        try:
            tui_worker._tui_worker_body(cfg)
        except BaseException as exc:
            failures.append(exc)

    runtime.enqueue_prompt('old', 'old-input', queued=False)
    jobs.put(ChatJob(user='old-input', show_prompt=show_prompt, inject=[], prompt_files=[], request_id='old'))
    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    assert agent.started.wait(5)
    return runtime, cfg, agent, thread, failures


# LLM: 顺序为开始A、reset、A结束、开始B；最终总是退出本测试线程，不遗留后台运行。
# 函数用途: 验证reset不杀worker、不污染新历史，同时保留A自己的完成记录。
@pytest.mark.parametrize('resets,stop_first', [(1, False), (2, False), (1, True)])
def test_reset_inflight_worker_survives_and_next_reply_visible(resets, stop_first):
    runtime, cfg, agent, thread, failures = _worker_case()
    try:
        if stop_first:
            runtime.enqueue_active_turn_input('steer', 'supplement')
            runtime.promote_active_turn_inputs(('steer',), request_id='old')
            assert runtime.request_interrupt()
            assert cfg.local_run_ref[0].request_interrupt()
            agent.stopped = True
        for index in range(resets):
            runtime.publish_conversation_reset(f'thread-new-{index}')
        runtime.enqueue_prompt('new', 'new-input', queued=True)
        cfg.jobs.put(ChatJob(user='new-input', show_prompt=False, inject=[], prompt_files=[], request_id='new'))
        agent.release.set()
        assert agent.second.wait(5), f'worker stopped before second turn: {failures!r}'
        assert cfg.jobs.drained.wait(5)
        assert thread.is_alive()
        assert not failures
        assert cfg.jobs.unfinished_tasks == 0
        snapshot = runtime.store.snapshot()
        texts = [block.text for block in (*snapshot.stable_blocks, *snapshot.active_blocks)]
        assert 'new-reply' in texts
        assert not any('old-' in text for text in texts)
        assert cfg.conversation_history == [('new-input', 'new-reply')]
        assert cfg.assistant_outputs == ['new-reply']
        assert not snapshot.pending_steers and not snapshot.queued_inputs
        assert not cfg.is_running_ref[0] and cfg.local_run_ref[0] is None
        completion = runtime.completions.clocks['old'].completion
        assert completion.status == ('interrupted' if stop_first else 'completed')
        assert not runtime._turns and not runtime._retired_turns
    finally:
        agent.release.set()
        cfg.stop_event.set()
        thread.join(5)
        assert not thread.is_alive()


# LLM: reset在finalize之后不得使既有终态倒退；未知id仍是调用合同错误。
# 函数用途: 检查另一交错顺序、重复begin和未知完成。
def test_finalize_then_reset_keeps_terminal_and_unknown_id_raises():
    runtime = TuiRuntime('completed-reset')
    adapter = runtime.begin_turn('known')
    assert runtime.begin_turn('known') is adapter
    runtime.complete_turn('known', TuiTurnSummary(response_text='before'))
    done = runtime.completions.clocks['known'].completion
    runtime.publish_conversation_reset('next-thread')
    adapter.start()
    adapter.write_model('late', public_stream=True)
    adapter.finalize(TuiTurnSummary(ok=False, error='late'))
    assert runtime.completions.clocks['known'].completion is done
    assert runtime.store.snapshot().active_blocks == ()
    assert not runtime.store.snapshot().stable_blocks
    with pytest.raises(KeyError, match='not started'):
        runtime.complete_turn('never-started', TuiTurnSummary())


# LLM: 所有公开adapter输出入口都实际调用，不以一个context检查外推其它正文/工具路径。
# 函数用途: 验证退休adapter晚到事件与终态不进入新视图。
@pytest.mark.parametrize('late', ['model', 'thinking', 'tool', 'metrics', 'notice', 'unknown', 'consumed', 'submitted'])
def test_retired_adapter_never_publishes_after_two_resets(late):
    runtime = TuiRuntime('retired-events')
    turn = runtime.begin_turn('old')
    runtime.publish_conversation_reset('middle')
    assert runtime.begin_turn('old') is turn
    runtime.publish_conversation_reset('last')
    if late in {'consumed', 'submitted'}:
        runtime.enqueue_active_turn_input('late-id', 'new-thread-input')
    before = runtime.store.snapshot()
    if late == 'model':
        turn.write_model('old-text', public_stream=True)
    elif late == 'thinking':
        turn.write_thinking_delta('old-thinking')
    elif late == 'tool':
        turn.write_progress({'round': 1, 'call_index': 1, 'tool': 'read_file', 'phase': 'started'})
    elif late == 'metrics':
        turn.write_model_metrics({'schema': 'model_runtime_metrics.v1', 'input_tokens': 7000000})
    elif late == 'notice':
        turn.on_gateway_event({'kind': 'host_notice', 'notice': {'notice_id': 'old', 'text': 'old-notice'}})
    elif late in {'consumed', 'submitted'}:
        turn.on_gateway_event({'kind': f'active_turn_input_{late}', 'client_message_ids': ['late-id']})
    runtime.complete_turn('old', TuiTurnSummary(response_text='old-final', outcome_unknown=late == 'unknown'))
    assert turn._finished
    assert runtime.store.snapshot() == before
    assert not runtime._turns and not runtime._retired_turns


# LLM: reset保留的真实审批只能按原permission收口，不能恢复旧工具、正文或状态。
# 函数用途: 检查旧审批在reset后仍可决定或随原回合终结关闭。
@pytest.mark.parametrize('finish', ['decision', 'terminal', 'gateway'])
def test_review_reset_preserved_permission_can_resolve(finish):
    from agent_py_agent.agent.contracts.tool_approval import build_tool_approval_request
    from agent_py_agent.agent.tooling.runtime_contracts import ToolCall
    runtime = TuiRuntime('permission-reset')
    turn = runtime.begin_turn('old-permission')
    request = build_tool_approval_request(ToolCall(call_id='call', tool_name='run_command',
        arguments={'command': 'printf fixture'}, source_protocol='native', schema_hash='sha256:fixture',
        run_id='run', turn_id='turn', attempt_id='attempt'), request_id='old-permission',
        round_number=1, call_index=1, description='fixture')
    decisions = []
    turn.configure_gateway_permission_sink(lambda approval, decision: decisions.append((approval, decision)))
    assert turn.on_gateway_event({'kind': 'permission_requested', 'permission': request.to_dict()})
    runtime.publish_conversation_reset('fresh-permission-thread')
    assert runtime.store.snapshot().permission.permission_id == request.permission_id
    if finish == 'decision':
        assert runtime.move_permission_selection(1)
        assert runtime.store.snapshot().permission.selected_index == 1
        assert runtime.toggle_permission_feedback()
        assert runtime.update_permission_feedback(request.permission_id, 'inspection only')
        assert runtime.store.snapshot().permission.feedback == 'inspection only'
        assert runtime.resolve_permission(request.permission_id, 'denied')
        assert len(decisions) == 1
    elif finish == 'gateway':
        assert turn.on_gateway_event({'kind': 'permission_resolved',
            'permission_id': request.permission_id, 'decision': 'denied'})
        assert runtime.store.snapshot().permission is None
    runtime.complete_turn('old-permission', TuiTurnSummary())
    assert runtime.store.snapshot().permission is None
    assert not runtime.store.snapshot().active_blocks
    assert not runtime.store.snapshot().stable_blocks


# LLM: 使用原local path与worker，prompt同正文一起受reset围栏。
# 函数用途: 复现show_prompt直接写共享runtime导致旧内容重现。
def test_review_local_show_prompt_is_discarded_after_reset():
    runtime, cfg, agent, thread, failures = _worker_case(show_prompt=True)
    try:
        runtime.publish_conversation_reset('fresh-prompt-thread')
        agent.release.set()
        assert cfg.jobs.drained.wait(5)
        assert not failures and thread.is_alive()
        assert not runtime.store.snapshot().stable_blocks
    finally:
        agent.release.set()
        cfg.stop_event.set()
        thread.join(5)
        assert not thread.is_alive()


# LLM: 独立Gateway结果同样必须拒绝旧回合的FINAL PROMPT。
# 函数用途: 检查Gateway诊断输出在reset后不进入新会话。
def test_review_gateway_show_prompt_is_discarded_after_reset():
    from agent_py_agent.cli.chat_parts.tui_worker_paths import _gateway_outcome
    runtime = TuiRuntime('gateway-prompt-reset')
    turn = runtime.begin_turn('old')
    runtime.publish_conversation_reset('fresh')
    ctx = SimpleNamespace(turn_adapter=turn, job=SimpleNamespace(show_prompt=True),
        cfg=SimpleNamespace(tui_runtime=runtime, state_lock=threading.RLock(), last_token_estimate_ref=[0]))
    _gateway_outcome(ctx, {'ok': True, 'prompt': 'old internal prompt', 'response': 'old response'})
    assert not runtime.store.snapshot().stable_blocks


# LLM: 执行中产生非reset编程错误仍传播，但cleanup必须真实清空refs和确认队列。
# 函数用途: 检查complete_turn抛错时finally不会跳过task_done。
def test_completion_error_still_cleans_worker_refs(monkeypatch):
    runtime, cfg, agent, thread, failures = _worker_case()
    try:
        def fail(*args):
            raise KeyError('simulated completion error')
        monkeypatch.setattr(runtime, 'complete_turn', fail)
        agent.release.set()
        thread.join(5)
        assert not thread.is_alive()
        assert len(failures) == 1 and isinstance(failures[0], KeyError)
        assert cfg.jobs.unfinished_tasks == 0
        assert not cfg.is_running_ref[0] and cfg.local_run_ref[0] is None
    finally:
        agent.release.set()
        cfg.stop_event.set()
        thread.join(5)
