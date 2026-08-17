"""LLM: focused tests for single-round tool execution safeguards.

模块用途: 验证工具执行轮如何处理模型同轮依赖调用，避免子代理树拿脑补 run id 继续调度。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.subagent.progress_closeout import (
    subagent_progress_closeout_response,
)
from agent_py_agent.agent.agent_core.tool_loop.round_execution import (
    ToolRoundExecutionRequest,
    execute_tool_round,
    subagent_output_json_response,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.tooling.action_policy import ActionDecision
from agent_py_agent.agent.tooling.cancellation import CancellationToken
from agent_py_agent.agent.tooling.executor import ToolExecution
from agent_py_agent.agent.tooling.runtime_contracts import (
    ProviderToolCapability,
    ToolCall,
    ToolFailureFacts,
    ToolProtocolSnapshot,
    ToolResult,
    ToolSuccessFacts,
)
from agent_py_agent.tests._tool_runtime_harness import (
    make_test_model_spec,
    make_test_runtime_policy,
    runtime_snapshot_for_model_specs,
)

_ROUND_RUN_ID = "round-execution-test-run"


def _text_protocol_snapshot() -> ToolProtocolSnapshot:
    return ToolProtocolSnapshot(
        _ROUND_RUN_ID,
        "text",
        ProviderToolCapability(
            provider="test",
            endpoint="local://round-execution-test",
            model="test-model",
            stream=False,
            native_supported=False,
            evidence="canonical_test_fixture",
        ),
    )


def _canonical_calls(payloads: list[dict[str, object]]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for index, raw in enumerate(payloads, start=1):
        payload = dict(raw)
        tool_name = str(payload.pop("tool"))
        calls.append(
            ToolCall(
                call_id=f"round-test-call-{index}",
                tool_name=tool_name,
                arguments=payload,
                source_protocol="native",
                schema_hash="sha256:round-test-schema",
                run_id=_ROUND_RUN_ID,
                turn_id=f"{_ROUND_RUN_ID}:turn",
                attempt_id=f"{_ROUND_RUN_ID}:attempt",
            )
        )
    return calls


def _payload(call: ToolCall) -> dict[str, object]:
    return {"tool": call.tool_name, **call.arguments}


def _round_request(**kwargs) -> ToolRoundExecutionRequest:
    params = kwargs["params"]
    if not hasattr(params, "tool_protocol_snapshot"):
        params.tool_protocol_snapshot = _text_protocol_snapshot()
    calls = _canonical_calls(kwargs["calls"])
    snapshot = getattr(params, "tool_runtime_snapshot", None)
    if snapshot is not None:
        calls = [
            replace(
                call,
                schema_hash=(
                    runtime.model_spec.schema_hash
                    if (runtime := snapshot.runtime(call.tool_name)) is not None
                    else call.schema_hash
                ),
            )
            for call in calls
        ]
    kwargs["calls"] = calls
    return ToolRoundExecutionRequest(**kwargs)


def _success(
    request,
    output: str,
    *,
    result_envelope: dict[str, object] | None = None,
) -> ToolExecution:
    result = ToolResult.succeeded(
        request.call,
        output,
        facts=ToolSuccessFacts(
            effect_outcome="confirmed",
            metadata={"handler_details": dict(result_envelope or {})},
        ),
    )
    return ToolExecution(
        request.call,
        ActionDecision("allow"),
        result,
        (
            "received",
            "normalized",
            "validated",
            "authorized",
            "approved",
            "running",
            "succeeded",
            "reconciled",
            "persisted",
            "projected",
        ),
    )


def _failure(
    request,
    output: str,
    *,
    error_code: str,
    effect_outcome: str = "not_started",
    handler_executed: bool = False,
) -> ToolExecution:
    result = ToolResult.failed(
        request.call,
        output,
        error_code=error_code,
        failure_stage="handler" if handler_executed else "runtime_gate",
        facts=ToolFailureFacts(
            handler_executed=handler_executed,
            effect_outcome=effect_outcome,
        ),
    )
    return ToolExecution(
        request.call,
        ActionDecision("deny", (error_code,)),
        result,
        ("received", "normalized", "failed", "reconciled", "persisted", "projected"),
    )


def test_tool_round_defers_dependent_dispatch_after_schedule():
    calls = [
        {"tool": "schedule_child_subagents", "children": [{"goal": "child"}]},
        {"tool": "dispatch_subagents", "run_ids": ["hallucinated-run-id"]},
    ]
    executed: list[str] = []
    records: list[tuple[str, bool, str]] = []

    def execute_one(request):
        tool_name = request.call.tool_name
        executed.append(tool_name)
        return _success(request, '{"created_run_ids":["real-child-id"]}')

    def record_one(record):
        records.append((str(record.payload["tool"]), record.result.ok, record.result.output))

    completed = execute_tool_round(
        _round_request(
            agent=SimpleNamespace(),
            params=SimpleNamespace(tool_context=[]),
            tool_rounds=1,
            response=ModelResponse(text="tool round", backend="test"),
            calls=calls,
            execute_one=execute_one,
            record_one=record_one,
        )
    )

    assert completed is False
    assert executed == ["schedule_child_subagents"]
    assert records[0] == ("schedule_child_subagents", True, '{"created_run_ids":["real-child-id"]}')
    assert records[1][0] == "dispatch_subagents"
    assert records[1][1] is False
    assert "已延后" in records[1][2]


def test_tool_round_executes_independent_same_round_create_calls_serially():
    calls = [
        {"tool": "create_subagents", "goal": "完成课程日历"},
        {"tool": "create_subagents", "goal": "完成导师计划"},
        {"tool": "create_subagents", "goal": "完成预算"},
    ]
    executed: list[str] = []
    records: list[tuple[str, bool, str]] = []

    def execute_one(request):
        goal = str(request.call.arguments["goal"])
        executed.append(goal)
        return _success(request, '{"created_run_ids":["child"]}')

    def record_one(record):
        records.append((str(record.payload["goal"]), record.result.ok, record.result.error_code))

    execute_tool_round(
        _round_request(
            agent=SimpleNamespace(),
            params=SimpleNamespace(tool_context=[]),
            tool_rounds=1,
            response=ModelResponse(text="", backend="test"),
            calls=calls,
            execute_one=execute_one,
            record_one=record_one,
        )
    )

    assert executed == ["完成课程日历", "完成导师计划", "完成预算"]
    assert records == [(goal, True, "") for goal in executed]


def test_tool_round_stops_at_typed_context_refresh_boundary():
    calls = [
        {"tool": "watch_stream", "action": "open"},
        {"tool": "watch_stream", "action": "pull"},
    ]
    executed: list[str] = []
    records: list[tuple[str, str]] = []
    params = SimpleNamespace(tool_context=[], live_archive_state={})

    def execute_one(request):
        action = str(request.call.arguments["action"])
        executed.append(action)
        return _success(
            request,
            '{"ok":true}',
            result_envelope={
                "runtime_transition": {
                    "kind": "context_refresh",
                    "reason": "durable_tool_scope_changed",
                    "resume": "next_durable_slice",
                }
            },
        )

    def record_one(record):
        records.append((str(record.payload["action"]), str(record.result.error_code or "")))

    execute_tool_round(
        _round_request(
            agent=SimpleNamespace(),
            params=params,
            tool_rounds=3,
            response=ModelResponse(text="", backend="test"),
            calls=calls,
            execute_one=execute_one,
            record_one=record_one,
        )
    )

    assert executed == ["open"]
    assert records == [
        ("open", ""),
        ("pull", "RUNTIME_TRANSITION_DEFERRED"),
    ]
    assert params.live_archive_state["pending_runtime_transition"] == {
        "kind": "context_refresh",
        "reason": "durable_tool_scope_changed",
        "resume": "next_durable_slice",
        "tool": "watch_stream",
        "tool_round": 3,
        "tool_index": 1,
    }
    assert any("剩余 1 个没有执行" in str(item) for item in params.tool_context)


def test_tool_round_stops_after_durable_slice_commit():
    calls = [
        {"tool": "watch_stream", "action": "verdict"},
        {"tool": "watch_stream", "action": "pull"},
    ]
    executed: list[str] = []
    params = SimpleNamespace(tool_context=[], live_archive_state={})

    def execute_one(request):
        action = str(request.call.arguments["action"])
        executed.append(action)
        return _success(
            request,
            '{"ok":true}',
            result_envelope={
                "runtime_transition": {
                    "kind": "context_refresh",
                    "reason": "durable_slice_committed",
                    "resume": "next_durable_slice",
                }
            },
        )

    execute_tool_round(
        _round_request(
            agent=SimpleNamespace(),
            params=params,
            tool_rounds=4,
            response=ModelResponse(text="", backend="test"),
            calls=calls,
            execute_one=execute_one,
            record_one=lambda _record: None,
        )
    )

    assert executed == ["verdict"]
    assert params.live_archive_state["pending_runtime_transition"]["reason"] == (
        "durable_slice_committed"
    )


def test_tool_round_records_partial_write_outcomes_in_original_order():
    calls = [
        {"tool": "send_message", "message": "first"},
        {"tool": "send_message", "message": "second"},
        {"tool": "send_message", "message": "third"},
    ]
    executed: list[str] = []
    records: list[tuple[str, bool, str]] = []

    def execute_one(request):
        message = str(request.call.arguments["message"])
        executed.append(message)
        if message == "second":
            return _failure(
                request,
                "outcome unknown",
                error_code="CHANNEL_SEND_FAILED",
                effect_outcome="unknown",
                handler_executed=True,
            )
        return _success(request, "sent")

    def record_one(record):
        records.append(
            (
                str(record.payload["message"]),
                record.result.ok,
                record.result.error_code,
            )
        )

    execute_tool_round(
        _round_request(
            agent=SimpleNamespace(),
            params=SimpleNamespace(tool_context=[]),
            tool_rounds=1,
            response=ModelResponse(text="", backend="test"),
            calls=calls,
            execute_one=execute_one,
            record_one=record_one,
        )
    )

    assert executed == ["first", "second", "third"]
    assert records == [
        ("first", True, ""),
        ("second", False, "CHANNEL_SEND_FAILED"),
        ("third", True, ""),
    ]


def test_deferred_orchestration_has_specific_retryable_error_code():
    calls = [
        {"tool": "schedule_child_subagents", "children": [{"goal": "child"}]},
        {"tool": "inspect_agent_tree"},
    ]
    records: list[ToolResult] = []

    execute_tool_round(
        _round_request(
            agent=SimpleNamespace(),
            params=SimpleNamespace(tool_context=[]),
            tool_rounds=1,
            response=ModelResponse(text="", backend="test"),
            calls=calls,
            execute_one=lambda request: _success(request, "ok"),
            record_one=lambda record: records.append(record.result),
        )
    )

    assert records[1].ok is False
    assert records[1].error_code == "ORCHESTRATION_CALL_DEFERRED"


def test_tool_round_can_defer_excess_model_tool_calls_to_next_round():
    calls = [{"tool": "read_file", "path": f"/tmp/source-{idx}.md"} for idx in range(5)]
    executed: list[str] = []
    records: list[tuple[str, str]] = []
    params = SimpleNamespace(
        task_attributes={"max_tool_calls_per_round": 2},
        tool_context=[],
    )

    def execute_one(request):
        path = str(request.call.arguments["path"])
        executed.append(path)
        return _success(request, f"read {path}")

    def record_one(record):
        records.append((str(record.payload["path"]), str(record.result.error_code or "")))

    completed = execute_tool_round(
        _round_request(
            agent=SimpleNamespace(),
            params=params,
            tool_rounds=1,
            response=ModelResponse(text="tool round", backend="test"),
            calls=calls,
            execute_one=execute_one,
            record_one=record_one,
        )
    )

    assert completed is False
    assert executed == ["/tmp/source-0.md", "/tmp/source-1.md"]
    assert records == [
        ("/tmp/source-0.md", ""),
        ("/tmp/source-1.md", ""),
        ("/tmp/source-2.md", "TOOL_CALL_LIMIT_DEFERRED"),
        ("/tmp/source-3.md", "TOOL_CALL_LIMIT_DEFERRED"),
        ("/tmp/source-4.md", "TOOL_CALL_LIMIT_DEFERRED"),
    ]
    assert any(
        "[tool-system]" in str(item) and "本轮模型请求了 5 个工具调用" in str(item)
        for item in params.tool_context
    )
    assert any(
        "只处理到前 2 个" in str(item) and "剩余 3 个没有执行" in str(item)
        for item in params.tool_context
    )


def test_tool_round_does_not_limit_model_tool_calls_by_default():
    calls = [{"tool": "read_file", "path": f"/tmp/source-{idx}.md"} for idx in range(4)]
    executed: list[str] = []
    params = SimpleNamespace(
        task_attributes={},
        tool_context=[],
    )

    def execute_one(request):
        path = str(request.call.arguments["path"])
        executed.append(path)
        return _success(request, f"read {path}")

    def record_one(record):
        record.params.tool_context.append(f"[tool-record]\n{record.result.output}")

    completed = execute_tool_round(
        _round_request(
            agent=SimpleNamespace(config=SimpleNamespace(max_tool_calls_per_round=None)),
            params=params,
            tool_rounds=1,
            response=ModelResponse(text="tool round", backend="test"),
            calls=calls,
            execute_one=execute_one,
            record_one=record_one,
        )
    )

    assert completed is False
    assert executed == [
        "/tmp/source-0.md",
        "/tmp/source-1.md",
        "/tmp/source-2.md",
        "/tmp/source-3.md",
    ]
    assert not any("剩余" in str(item) and "没有执行" in str(item) for item in params.tool_context)


def test_parallel_safe_readers_overlap_but_records_keep_provider_order():
    spec = make_test_model_spec(
        "read_probe",
        input_schema={
            "type": "object",
            "properties": {"slot": {"type": "integer"}},
            "required": ["slot"],
            "additionalProperties": False,
        },
    )
    snapshot = runtime_snapshot_for_model_specs(
        (spec,),
        run_id=_ROUND_RUN_ID,
        policies={
            spec.name: make_test_runtime_policy(
                "read_only",
                concurrency_mode="parallel_safe",
                resource_parameters=("slot",),
            )
        },
    )
    rendezvous = threading.Barrier(2, timeout=2)
    completion_order: list[int] = []
    records: list[int] = []
    lock = threading.Lock()

    def execute_one(request):
        slot = int(request.call.arguments["slot"])
        rendezvous.wait()
        if slot == 0:
            time.sleep(0.03)
        with lock:
            completion_order.append(slot)
        return _success(request, f"read {slot}")

    execute_tool_round(
        _round_request(
            agent=SimpleNamespace(),
            params=SimpleNamespace(
                tool_context=[],
                tool_runtime_snapshot=snapshot,
                cancellation_token=CancellationToken(),
            ),
            tool_rounds=1,
            response=ModelResponse(text="", backend="test"),
            calls=[
                {"tool": "read_probe", "slot": 0},
                {"tool": "read_probe", "slot": 1},
            ],
            execute_one=execute_one,
            record_one=lambda record: records.append(int(record.call.arguments["slot"])),
        )
    )

    assert completion_order == [1, 0], "barrier proves the two read handlers overlapped"
    assert records == [0, 1], "durable ToolResult order follows provider call order"


def test_parallel_limit_caps_parallel_safe_segment():
    """EXEC-01: max_parallel_tool_calls 配置生效——超过上限的并行调用转串行,
    全部仍执行, 不丢调用。"""
    spec = make_test_model_spec(
        "read_probe",
        input_schema={
            "type": "object",
            "properties": {"slot": {"type": "integer"}},
            "required": ["slot"],
            "additionalProperties": False,
        },
    )
    snapshot = runtime_snapshot_for_model_specs(
        (spec,),
        run_id=_ROUND_RUN_ID,
        policies={
            spec.name: make_test_runtime_policy(
                "read_only",
                concurrency_mode="parallel_safe",
                resource_parameters=("slot",),
            )
        },
    )
    max_concurrency = [0]
    in_flight: list[int] = []
    records: list[int] = []
    lock = threading.Lock()

    def execute_one(request):
        slot = int(request.call.arguments["slot"])
        with lock:
            max_concurrency[0] = max(max_concurrency[0], len(in_flight) + 1)
            in_flight.append(slot)
        time.sleep(0.02)
        with lock:
            in_flight.remove(slot)
        return _success(request, f"read {slot}")

    execute_tool_round(
        _round_request(
            agent=SimpleNamespace(),
            params=SimpleNamespace(
                task_attributes={"max_parallel_tool_calls": 2},
                tool_context=[],
                tool_runtime_snapshot=snapshot,
                cancellation_token=CancellationToken(),
            ),
            tool_rounds=1,
            response=ModelResponse(text="", backend="test"),
            calls=[
                {"tool": "read_probe", "slot": idx} for idx in range(5)
            ],
            execute_one=execute_one,
            record_one=lambda record: records.append(int(record.call.arguments["slot"])),
        )
    )

    assert max_concurrency[0] == 2, "parallel segment must be capped at the configured limit"
    assert records == [0, 1, 2, 3, 4], "all calls still executed, provider order preserved"


def test_parallel_limit_zero_means_unlimited():
    """EXEC-01: 显式 0 = 不限制并行度(与 max_tool_rounds 0 同约定)。"""
    spec = make_test_model_spec(
        "read_probe",
        input_schema={
            "type": "object",
            "properties": {"slot": {"type": "integer"}},
            "required": ["slot"],
            "additionalProperties": False,
        },
    )
    snapshot = runtime_snapshot_for_model_specs(
        (spec,),
        run_id=_ROUND_RUN_ID,
        policies={
            spec.name: make_test_runtime_policy(
                "read_only",
                concurrency_mode="parallel_safe",
                resource_parameters=("slot",),
            )
        },
    )
    rendezvous = threading.Barrier(4, timeout=2)
    records: list[int] = []

    def execute_one(request):
        slot = int(request.call.arguments["slot"])
        rendezvous.wait()
        return _success(request, f"read {slot}")

    execute_tool_round(
        _round_request(
            agent=SimpleNamespace(),
            params=SimpleNamespace(
                task_attributes={"max_parallel_tool_calls": 0},
                tool_context=[],
                tool_runtime_snapshot=snapshot,
                cancellation_token=CancellationToken(),
            ),
            tool_rounds=1,
            response=ModelResponse(text="", backend="test"),
            calls=[
                {"tool": "read_probe", "slot": idx} for idx in range(4)
            ],
            execute_one=execute_one,
            record_one=lambda record: records.append(int(record.call.arguments["slot"])),
        )
    )

    assert records == [0, 1, 2, 3], "barrier(4) proves all four overlapped (0 = unlimited)"


def test_mutating_effect_is_a_barrier_even_if_manifest_marks_parallel_safe():
    spec = make_test_model_spec(
        "write_probe",
        input_schema={
            "type": "object",
            "properties": {"slot": {"type": "integer"}},
            "required": ["slot"],
            "additionalProperties": False,
        },
    )
    snapshot = runtime_snapshot_for_model_specs(
        (spec,),
        run_id=_ROUND_RUN_ID,
        policies={
            spec.name: make_test_runtime_policy(
                "mutating",
                concurrency_mode="parallel_safe",
                approval_mode="never",
                resource_parameters=("slot",),
            )
        },
    )
    lock = threading.Lock()
    active = 0
    maximum_active = 0

    def execute_one(request):
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return _success(request, "written")

    execute_tool_round(
        _round_request(
            agent=SimpleNamespace(),
            params=SimpleNamespace(
                tool_context=[],
                tool_runtime_snapshot=snapshot,
                cancellation_token=CancellationToken(),
            ),
            tool_rounds=1,
            response=ModelResponse(text="", backend="test"),
            calls=[
                {"tool": "write_probe", "slot": 0},
                {"tool": "write_probe", "slot": 1},
            ],
            execute_one=execute_one,
            record_one=lambda _record: None,
        )
    )

    assert maximum_active == 1


def test_parallel_segment_cancellation_blocks_later_barrier_and_pairs_every_call():
    reader = make_test_model_spec(
        "read_probe",
        input_schema={
            "type": "object",
            "properties": {"slot": {"type": "integer"}},
            "required": ["slot"],
            "additionalProperties": False,
        },
    )
    writer = make_test_model_spec(
        "write_probe",
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    )
    snapshot = runtime_snapshot_for_model_specs(
        (reader, writer),
        run_id=_ROUND_RUN_ID,
        policies={
            reader.name: make_test_runtime_policy(
                "read_only",
                concurrency_mode="parallel_safe",
                resource_parameters=("slot",),
            ),
            writer.name: make_test_runtime_policy(
                "mutating",
                approval_mode="never",
            ),
        },
    )
    token = CancellationToken()
    rendezvous = threading.Barrier(2, timeout=2)
    executed: list[str] = []
    records: list[tuple[str, str]] = []
    lock = threading.Lock()

    def execute_one(request):
        with lock:
            executed.append(request.call.tool_name)
        if request.call.tool_name == "read_probe":
            rendezvous.wait()
            if request.call.arguments["slot"] == 0:
                token.cancel("test cancellation")
        return _success(request, "done")

    execute_tool_round(
        _round_request(
            agent=SimpleNamespace(),
            params=SimpleNamespace(
                tool_context=[],
                tool_runtime_snapshot=snapshot,
                cancellation_token=token,
            ),
            tool_rounds=1,
            response=ModelResponse(text="", backend="test"),
            calls=[
                {"tool": "read_probe", "slot": 0},
                {"tool": "read_probe", "slot": 1},
                {"tool": "write_probe"},
            ],
            execute_one=execute_one,
            record_one=lambda record: records.append(
                (record.call.tool_name, str(record.result.error_code or ""))
            ),
        )
    )

    assert executed == ["read_probe", "read_probe"]
    assert records == [
        ("read_probe", ""),
        ("read_probe", ""),
        ("write_probe", "CANCELLED"),
    ]


def test_tool_round_rebases_placeholder_paths_after_conversation_task_selection():
    old_root = "/owner/tasks/req-new"
    selected_root = "/owner/tasks/req-old"
    attrs = {
        "conversation_rebase_from_task_root": old_root,
        "run_workspace": {"task_root": selected_root},
    }
    agent = SimpleNamespace(
        _current_run_params=SimpleNamespace(task_attributes=attrs),
        config=SimpleNamespace(memory_compact_auto_trigger_percent=90),
    )
    params = SimpleNamespace(task_attributes=attrs, tool_context=[])
    executed: list[dict[str, object]] = []

    def execute_one(request):
        executed.append(_payload(request.call))
        return _success(request, "{}")

    execute_tool_round(
        _round_request(
            agent=agent,
            params=params,
            tool_rounds=1,
            response=ModelResponse(text="tool round", backend="test"),
            calls=[
                {
                    "tool": "create_subagents",
                    "goal": f"在 {old_root}/output 继续实现",
                    "output_files": [f"{old_root}/output/app.py"],
                }
            ],
            execute_one=execute_one,
            record_one=lambda _record: None,
        )
    )

    assert executed == [
        {
            "tool": "create_subagents",
            "goal": f"在 {selected_root}/output 继续实现",
            "output_files": [f"{selected_root}/output/app.py"],
        }
    ]


def test_tool_round_stops_batch_when_new_tool_context_crosses_compact_budget():
    calls = [{"tool": "read_file", "path": f"fragment-{idx}.md"} for idx in range(10)]
    executed: list[str] = []
    records: list[tuple[str, bool, str]] = []
    params = SimpleNamespace(
        task_attributes={},
        tool_context=[],
    )
    agent = SimpleNamespace(
        config=SimpleNamespace(memory_compact_auto_trigger_percent=50),
        backend=SimpleNamespace(context_window_tokens=1000),
    )

    def execute_one(request):
        path = str(request.call.arguments["path"])
        executed.append(path)
        return _success(request, "片段正文" * 40)

    def record_one(record):
        records.append(
            (
                str(record.payload.get("path") or ""),
                bool(record.result.ok),
                str(record.result.error_code or ""),
            )
        )
        record.params.tool_context.append(
            f"[tool-record round={record.tool_rounds} index={record.idx}]\n{record.result.output}"
        )

    execute_tool_round(
        _round_request(
            agent=agent,
            params=params,
            tool_rounds=1,
            response=ModelResponse(text="tool batch", backend="test"),
            calls=calls,
            execute_one=execute_one,
            record_one=record_one,
            current_prompt="系统上下文" * 70,
        )
    )

    assert 0 < len(executed) < len(calls)
    deferred = [item for item in records if item[2] == "CONTEXT_COMPACT_DEFERRED"]
    assert len(deferred) == len(calls) - len(executed)
    assert all(item[0].startswith("fragment-") and item[1] is False for item in deferred)
    assert any("剩余" in str(item) and "没有执行" in str(item) for item in params.tool_context)
    assert any("已登记为 CONTEXT_COMPACT_DEFERRED" in str(item) for item in params.tool_context)


def test_tool_round_records_deferred_content_tool_when_context_needs_compact():
    executed: list[str] = []
    records: list[tuple[str, bool, str, str]] = []
    params = SimpleNamespace(
        task_attributes={},
        tool_context=[
            "[tool-record round=1 index=1]\n[tool-output-record round=1 index=1]\n上一批读取结果"
        ],
        live_archive_state={},
    )
    agent = SimpleNamespace(
        config=SimpleNamespace(memory_compact_auto_trigger_percent=50),
        backend=SimpleNamespace(context_window_tokens=1000),
    )

    def execute_one(request):
        executed.append(request.call.tool_name)
        return _success(request, "不应该执行")

    def record_one(record):
        records.append(
            (
                str(record.payload["tool"]),
                bool(record.result.ok),
                str(record.result.error_code),
                str(record.result.output),
            )
        )

    execute_tool_round(
        _round_request(
            agent=agent,
            params=params,
            tool_rounds=2,
            response=ModelResponse(text="tool batch", backend="test"),
            calls=[{"tool": "read_file", "path": "next-fragment.md"}],
            execute_one=execute_one,
            record_one=record_one,
            current_prompt="系统上下文" * 120,
        )
    )

    assert executed == []
    assert records == [
        (
            "read_file",
            False,
            "CONTEXT_COMPACT_DEFERRED",
            "CONTEXT_COMPACT_DEFERRED: 当前上下文需要先 compact/resume；本次工具调用未执行，恢复后从同一目标继续。",
        )
    ]
    assert any("已达到 compact 阈值" in str(item) for item in params.tool_context)
    assert "tool_context_checkpoint_required" not in params.live_archive_state


def test_tool_round_runs_first_reader_before_context_pressure_deferral():
    executed: list[str] = []
    params = SimpleNamespace(
        task_attributes={},
        tool_context=[],
        live_archive_state={},
    )
    agent = SimpleNamespace(
        config=SimpleNamespace(memory_compact_auto_trigger_percent=50),
        backend=SimpleNamespace(context_window_tokens=1000),
    )

    def execute_one(request):
        executed.append(request.call.tool_name)
        return _success(request, "第一片段已读取")

    def record_one(record):
        record.params.tool_context.append(
            f"[tool-record round={record.tool_rounds} index={record.idx}]\n"
            f"[tool-output-record round={record.tool_rounds} index={record.idx}]\n"
            f"{record.result.output}"
        )

    execute_tool_round(
        _round_request(
            agent=agent,
            params=params,
            tool_rounds=2,
            response=ModelResponse(text="tool batch", backend="test"),
            calls=[{"tool": "read_file", "path": "first-fragment.md"}],
            execute_one=execute_one,
            record_one=record_one,
            current_prompt="系统上下文" * 120,
        )
    )

    assert executed == ["read_file"]
    assert any("第一片段已读取" in str(item) for item in params.tool_context)
    assert "tool_context_checkpoint_required" not in params.live_archive_state


def test_tool_round_does_not_inject_checkpoint_work_after_chunked_read():
    params = SimpleNamespace(task_attributes={}, tool_context=[], live_archive_state={})
    agent = SimpleNamespace(config=SimpleNamespace(memory_compact_auto_trigger_percent=0))

    def execute_one(request):
        return _success(request, "第001章：南京，CP-001-037")

    def record_one(record):
        record.params.tool_context.append(f"[tool-record]\n{record.result.output}")

    execute_tool_round(
        _round_request(
            agent=agent,
            params=params,
            tool_rounds=1,
            response=ModelResponse(text="tool batch", backend="test"),
            calls=[{"tool": "read_file", "path": "data/long.txt", "offset": 0, "max_chars": 50000}],
            execute_one=execute_one,
            record_one=record_one,
            current_prompt="",
        )
    )

    assert not any("[tool-system:long-read-facts]" in str(item) for item in params.tool_context)
    assert not any("先把对象、事实" in str(item) for item in params.tool_context)
    assert any("第001章：南京，CP-001-037" in str(item) for item in params.tool_context)


def test_tool_round_defers_more_reading_to_compact_when_previous_results_already_exist():
    executed: list[str] = []
    records: list[tuple[str, bool, str]] = []
    params = SimpleNamespace(
        task_attributes={},
        tool_context=[
            "[tool-record round=3 index=1]\n[tool-output-record round=3 index=1]\n上一批读取结果"
        ],
        live_archive_state={},
    )
    agent = SimpleNamespace(
        config=SimpleNamespace(memory_compact_auto_trigger_percent=50),
        backend=SimpleNamespace(context_window_tokens=1000),
    )

    def execute_one(request):
        executed.append(request.call.tool_name)
        return _success(request, "不应该执行")

    def record_one(record):
        records.append(
            (str(record.payload["tool"]), bool(record.result.ok), str(record.result.error_code))
        )

    execute_tool_round(
        _round_request(
            agent=agent,
            params=params,
            tool_rounds=4,
            response=ModelResponse(text="tool batch", backend="test"),
            calls=[{"tool": "read_file", "path": "next-fragment.md"}],
            execute_one=execute_one,
            record_one=record_one,
            current_prompt="系统上下文" * 120,
        )
    )

    assert executed == []
    assert records == [("read_file", False, "CONTEXT_COMPACT_DEFERRED")]
    assert any("CONTEXT_COMPACT_DEFERRED" in str(item) for item in params.tool_context)


def test_tool_round_allows_checkpoint_writes_at_compact_budget():
    executed: list[str] = []
    params = SimpleNamespace(
        task_attributes={},
        tool_context=[],
        live_archive_state={},
    )
    agent = SimpleNamespace(
        config=SimpleNamespace(memory_compact_auto_trigger_percent=50),
        backend=SimpleNamespace(context_window_tokens=1000),
    )

    def execute_one(request):
        tool_name = request.call.tool_name
        executed.append(tool_name)
        return _success(request, "written")

    def record_one(record):
        record.params.tool_context.append(record.result.output)

    execute_tool_round(
        _round_request(
            agent=agent,
            params=params,
            tool_rounds=2,
            response=ModelResponse(text="tool batch", backend="test"),
            calls=[{"tool": "write_file", "path": "progress.json", "content": "{}"}],
            execute_one=execute_one,
            record_one=record_one,
            current_prompt="系统上下文" * 120,
        )
    )

    assert executed == ["write_file"]


def test_tool_round_detects_bundled_filesystem_output_json(tmp_path):
    output_json = tmp_path / "output.json"
    task = SimpleNamespace(output_json=str(output_json))
    agent = SimpleNamespace(
        _current_subagent_run_id="run-1",
        subagents=SimpleNamespace(load=lambda run_id: task),
    )
    payload = {"tool": "write_file", "filesystem": {"path": str(output_json), "content": "{}"}}
    records: list[str] = []

    def execute_one(request):
        assert _payload(request.call) == payload
        return _success(request, "ok")

    def record_one(record):
        records.append(record.result.tool_name)

    completed = execute_tool_round(
        _round_request(
            agent=agent,
            params=SimpleNamespace(tool_context=[]),
            tool_rounds=1,
            response=ModelResponse(text="", backend="test"),
            calls=[payload],
            execute_one=execute_one,
            record_one=record_one,
        )
    )

    assert completed is True
    assert records == ["write_file"]


def test_tool_round_reports_subagent_output_json_scope_load_error(tmp_path):
    output_json = tmp_path / "output.json"

    def broken_load(run_id):
        del run_id
        raise OSError("canonical state missing")

    agent = SimpleNamespace(
        _current_subagent_run_id="run-1",
        subagents=SimpleNamespace(load=broken_load),
    )
    payload = {"tool": "write_file", "path": str(output_json), "content": "{}"}
    params = SimpleNamespace(tool_context=[])

    def execute_one(request):
        return _success(request, "ok")

    completed = execute_tool_round(
        _round_request(
            agent=agent,
            params=params,
            tool_rounds=1,
            response=ModelResponse(text="", backend="test"),
            calls=[payload],
            execute_one=execute_one,
            record_one=lambda _record: None,
        )
    )

    assert completed is False
    assert any("SUBAGENT_RESULT_LOAD_ERROR" in str(item) for item in params.tool_context)
    assert any("subagent_output_json.subagents.load" in str(item) for item in params.tool_context)


def test_subagent_output_json_response_derives_packet_from_report(tmp_path):
    output_json = tmp_path / "output.json"
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    report = reports_dir / "coordinator_report.md"
    report.write_text("完成协调报告", encoding="utf-8")
    output_json.write_text(
        json.dumps(
            {
                "status": "DONE",
                "summary": "root 已完成协调并写出报告。",
                "evidence_packets": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    task = SimpleNamespace(id="root-1", output_json=str(output_json), reports_dir=str(reports_dir))
    agent = SimpleNamespace(
        _current_subagent_run_id="root-1", subagents=SimpleNamespace(load=lambda _: task)
    )

    response = subagent_output_json_response(
        agent, ModelResponse(text="base response", backend="test")
    )
    written = json.loads(output_json.read_text(encoding="utf-8"))

    assert "[SUBAGENT_RESULT]" in response.text
    assert written["evidence_packets"][0]["id"] == "evpkt-output-json-root-1"
    assert str(report) in written["evidence_packets"][0]["evidence_refs"]
    assert str(output_json) in written["evidence_packets"][0]["evidence_refs"]


def test_subagent_output_json_response_reports_task_load_error():
    def broken_load(run_id):
        del run_id
        raise OSError("subagent ledger unavailable")

    agent = SimpleNamespace(
        _current_subagent_run_id="root-1", subagents=SimpleNamespace(load=broken_load)
    )

    response = subagent_output_json_response(
        agent, ModelResponse(text="base response", backend="test")
    )

    assert "[SUBAGENT_RESULT_LOAD_ERROR]" in response.text
    assert "subagent_output_json.subagents.load" in response.text
    assert "subagent ledger unavailable" in response.text


def test_subagent_output_json_response_does_not_hide_bad_packet(tmp_path):
    output_json = tmp_path / "output.json"
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    (reports_dir / "coordinator_report.md").write_text("report", encoding="utf-8")
    original = {
        "status": "DONE",
        "summary": "bad packet",
        "evidence_packets": [{"id": "bad", "claim": "done"}],
    }
    output_json.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    task = SimpleNamespace(id="root-1", output_json=str(output_json), reports_dir=str(reports_dir))
    agent = SimpleNamespace(
        _current_subagent_run_id="root-1", subagents=SimpleNamespace(load=lambda _: task)
    )

    subagent_output_json_response(agent, ModelResponse(text="base response", backend="test"))
    written = json.loads(output_json.read_text(encoding="utf-8"))

    assert written == original


def test_subagent_progress_closeout_response_uses_latest_tool_progress(tmp_path):
    artifact = tmp_path / "lab_outputs" / "site-output" / "index.html"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("<html><body><main id='home'>done</main></body></html>", encoding="utf-8")
    workspace = tmp_path / "tasks" / "worker" / "agents" / "worker"
    progress_dir = workspace / "progress"
    progress_dir.mkdir(parents=True)
    progress_ref = progress_dir / "latest_tool_progress.json"
    progress_ref.write_text(
        json.dumps(
            {
                "schema_version": "subagent_tool_progress.v1",
                "kind": "subagent_tool_progress",
                "run_id": "worker",
                "latest_written_path": str(artifact),
                "artifact_integrity": {
                    "kind": "html",
                    "ok": True,
                    "blocker_codes": [],
                    "warning_codes": [],
                    "issues": [],
                },
                "latest_tool_progress_ref": str(progress_ref),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    task = SimpleNamespace(
        id="worker",
        agent_run_workspace_dir=str(workspace),
        output_json=str(tmp_path / ".my_agent" / "subagents" / "worker" / "output.json"),
        attributes={
            "output_files": [str(artifact)],
            "artifact_registry_refs": [
                {
                    "run_id": "worker",
                    "path": str(artifact),
                    "status": "ready",
                    "size_bytes": artifact.stat().st_size,
                }
            ],
        },
        context_packs=[],
    )
    agent = SimpleNamespace(
        _current_subagent_run_id="worker", subagents=SimpleNamespace(load=lambda _: task)
    )

    response = subagent_progress_closeout_response(
        agent, ModelResponse(text="base response", backend="test")
    )

    assert response is not None
    assert "[SUBAGENT_RESULT]" in response.text
    assert '"status": "DONE"' in response.text
    assert str(artifact) in response.text
    assert str(progress_ref) in response.text


def test_subagent_progress_closeout_reports_task_load_error():
    def broken_load(run_id):
        del run_id
        raise OSError("progress ledger unavailable")

    agent = SimpleNamespace(
        _current_subagent_run_id="worker", subagents=SimpleNamespace(load=broken_load)
    )

    response = subagent_progress_closeout_response(
        agent, ModelResponse(text="base response", backend="test")
    )

    assert response is not None
    assert "[SUBAGENT_PROGRESS_LOAD_ERROR]" in response.text
    assert "subagent_progress_closeout.subagents.load" in response.text
    assert "progress ledger unavailable" in response.text


def test_subagent_progress_closeout_reports_dirty_latest_progress(tmp_path):
    workspace = tmp_path / "tasks" / "worker" / "agents" / "worker"
    progress_dir = workspace / "progress"
    progress_dir.mkdir(parents=True)
    progress_ref = progress_dir / "latest_tool_progress.json"
    progress_ref.write_text("{bad-progress", encoding="utf-8")
    task = SimpleNamespace(
        id="worker",
        agent_run_workspace_dir=str(workspace),
        output_json=str(tmp_path / "output.json"),
    )
    agent = SimpleNamespace(
        _current_subagent_run_id="worker", subagents=SimpleNamespace(load=lambda _: task)
    )

    response = subagent_progress_closeout_response(
        agent, ModelResponse(text="base response", backend="test")
    )

    assert response is not None
    assert "[SUBAGENT_PROGRESS_LOAD_ERROR]" in response.text
    assert "subagent_progress_closeout.latest_tool_progress" in response.text
    assert str(progress_ref) in response.text
