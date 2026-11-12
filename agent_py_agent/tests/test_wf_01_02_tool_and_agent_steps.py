"""WF-01 (Typed Direct-Tool Steps) + WF-02 (Analysis Isolation) + G5b (WorkflowAgentTaskPort.v1) 测试套件。

覆盖：
1. WF-01: execute_tool_step 参数解析映射 ($inputs, 上游 outputs, input_snapshot, 字面量)、
   参数 Schema 校验、权限与安全 (递归防护、黑白名单、破坏性工具限制)、幂等 effect_id、
   耐久工具回执 (workflow-tool-receipt.v1) 以及通过 WorkflowEngine 的正式运行。
2. WF-02: resolve_analysis_isolation 向后兼容降级、有 isolation_executor 时的轻量子代理隔离
   (只读分析工具集、零记忆写入、stateless_transcript)、禁止工具拒绝 (WORKFLOW_ISOLATION_TOOL_FORBIDDEN)、
   以及 run_isolated_analysis 结构化输出校验。
3. G5b: build_default_workflow_agent_steps 装配、read_session_task_completion 读取完成证据
   (身份核验、状态映射)、cancel_workflow_session_task 幂等取消。
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from agent_py_agent.agent.assets.descriptor import AssetDescriptor, AssetRefs, IOContract
from agent_py_agent.agent.assets.workflow_store import (
    WorkflowDefinition,
    WorkflowStepSpec,
    WorkflowStore,
)
from agent_py_agent.agent.conversation.session_tasks import (
    SessionTask,
    SessionTaskDraft,
    SessionTaskStore,
    SessionTaskUpdate,
)
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.workflows.agent_steps import (
    WorkflowAgentSteps,
    build_default_workflow_agent_steps,
    cancel_workflow_session_task,
    make_session_task_completion_reader,
    read_session_task_completion,
)
from agent_py_agent.agent.workflows.analysis_isolation import (
    DEFAULT_ISOLATION_ALLOWED_TOOLS,
    FORBIDDEN_ISOLATION_TOOLS,
    WorkflowIsolationToolForbidden,
    resolve_analysis_isolation,
    run_isolated_analysis,
)
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.models import (
    StepExecutionRecord,
    WorkflowRunRecord,
)
from agent_py_agent.agent.workflows.tool_steps import (
    WORKFLOW_TOOL_RECEIPT_SCHEMA,
    execute_tool_step,
)


def _make_descriptor(workflow_id: str = "wf_test", owner_id: str = "owner-1") -> AssetDescriptor:
    return AssetDescriptor(
        id=workflow_id,
        kind="workflow",
        name="测试工作流",
        summary="测试工作流摘要",
        version="1.0.0",
        owner_id=owner_id,
        access_targets=(),
        data_sources=(),
        credential_refs=(),
        io_contract=IOContract(),
        refs=AssetRefs(),
    )


def _make_run(workflow_id: str = "wf_test", run_id: str = "run-101", owner_id: str = "owner-1", inputs: dict | None = None) -> WorkflowRunRecord:
    return WorkflowRunRecord(
        workflow_run_id=run_id,
        workflow_id=workflow_id,
        owner_id=owner_id,
        trigger_kind="manual",
        status="running",
        started_at=1000.0,
        ended_at=0.0,
        duration_ms=0.0,
        inputs=inputs or {},
        business_context={"space_id": "space-test", "owner_id": owner_id},
    )


def _make_step(step_id: str = "step_tool", name: str = "工具步骤") -> StepExecutionRecord:
    return StepExecutionRecord(
        step_id=step_id,
        name=name,
        mode="tool_call",
        status="running",
        started_at=1000.0,
        ended_at=0.0,
        duration_ms=0.0,
        llm_involved=False,
        escalation_result={"attempt_id": "att-42"},
        attempt_count=1,
    )


# ==============================================================================
# WF-01: Typed Direct-Tool Steps Tests
# ==============================================================================

class MockToolRegistry:
    def __init__(self, tools: dict[str, Any] | None = None):
        self.tools = dict(tools or {})

    def get_tool(self, name: str) -> Any:
        return self.tools.get(name)


class MockTool:
    def __init__(self, name: str, execute_fn, parameters: dict[str, Any] | None = None):
        self.name = name
        self.execute_fn = execute_fn
        self.parameters = parameters

    def execute_scoped(self, arguments: dict[str, Any], **kwargs) -> Any:
        return self.execute_fn(arguments, **kwargs)


def test_tool_step_successful_execution_with_receipt():
    called_args = {}

    def run_calculator(arguments, **kwargs):
        called_args.update(arguments)
        called_args["_kwargs"] = kwargs
        return {"result": arguments["a"] + arguments["b"], "operation_id": "op-calc-1"}

    calc_tool = MockTool("calculator", run_calculator, parameters={
        "type": "object",
        "required": ["a", "b"],
        "properties": {
            "a": {"type": "integer"},
            "b": {"type": "integer"},
        },
    })
    engine = SimpleNamespace(tool_registry=MockToolRegistry({"calculator": calc_tool}))
    run = _make_run(inputs={"base_val": 10})
    step = _make_step()
    spec = {
        "step_id": "step_tool",
        "name": "计算",
        "mode": "tool_call",
        "tool_spec": {
            "tool_name": "calculator",
            "arguments": {"b": 5},
            "arguments_map": {"a": "$inputs.base_val"},
        },
    }
    outputs = {}

    execute_tool_step(
        engine=engine,
        run=run,
        workflow={},
        step=step,
        spec=spec,
        context=None,
        outputs=outputs,
    )

    assert step.status == "succeeded"
    assert step.error_code == ""
    assert not step.needs_repair
    assert step.output_snapshot == {"result": 15, "operation_id": "op-calc-1"}
    assert step.effect_id == "workflow:run-101:step_tool:att-42"

    # Durable Receipt 检查
    receipt = step.escalation_result.get("tool_receipt")
    assert receipt is not None
    assert receipt["schema"] == WORKFLOW_TOOL_RECEIPT_SCHEMA
    assert receipt["tool_name"] == "calculator"
    assert receipt["effect_id"] == "workflow:run-101:step_tool:att-42"
    assert receipt["operation_id"] == "op-calc-1"
    assert receipt["effect_outcome"] == "succeeded"
    assert receipt["status"] == "succeeded"
    assert receipt["output_summary"] == {"result": 15, "operation_id": "op-calc-1"}

    # Scoped 身份透传核验
    assert called_args["_kwargs"]["idempotency_key"] == "workflow:run-101:step_tool:att-42"
    assert called_args["_kwargs"]["owner_id"] == "owner-1"
    assert called_args["_kwargs"]["space_id"] == "space-test"


def test_tool_step_arguments_mapping_sources():
    captured = {}

    def echo(args, **_kwargs):
        captured.update(args)
        return {"echo": args}

    tool = MockTool("echo", echo)
    engine = SimpleNamespace(tool_registry=MockToolRegistry({"echo": tool}))
    run = _make_run(inputs={"query": "SELECT *", "limit": 100})
    step = _make_step()
    step.input_snapshot = {"initial_key": "initial_val"}
    outputs = {
        "prev_step": {"user_id": "u-999", "role": "admin"},
    }
    spec = {
        "step_id": "step_tool",
        "name": "回显",
        "mode": "tool_call",
        "config": {  # fallback from tool_spec to config
            "tool_name": "echo",
            "arguments": {"fixed": "literal_text"},
            "arguments_map": {
                "q": "$inputs.query",
                "uid": "prev_step.user_id",
                "structured_ref": {"step_id": "prev_step", "key": "role"},
                "from_snapshot": "initial_key",
            },
        },
    }

    execute_tool_step(
        engine=engine,
        run=run,
        workflow={},
        step=step,
        spec=spec,
        context=None,
        outputs=outputs,
    )

    assert step.status == "succeeded"
    assert captured["fixed"] == "literal_text"
    assert captured["q"] == "SELECT *"
    assert captured["uid"] == "u-999"
    assert captured["structured_ref"] == "admin"
    assert captured["from_snapshot"] == "initial_val"


def test_tool_step_invalid_spec_fails():
    engine = SimpleNamespace(tool_registry=MockToolRegistry())
    run = _make_run()
    step = _make_step()

    # 缺少 tool_name
    spec = {"step_id": "s1", "name": "无工具名", "mode": "tool_call", "tool_spec": {}}
    execute_tool_step(engine=engine, run=run, workflow={}, step=step, spec=spec, context=None, outputs={})
    assert step.status == "failed"
    assert step.error_code == "WORKFLOW_TOOL_SPEC_INVALID"
    assert step.needs_repair is True

    # tool_spec 不是字典
    step2 = _make_step()
    spec2 = {"step_id": "s1", "name": "非法spec", "mode": "tool_call", "tool_spec": "not-a-dict"}
    execute_tool_step(engine=engine, run=run, workflow={}, step=step2, spec=spec2, context=None, outputs={})
    assert step2.status == "failed"
    assert step2.error_code == "WORKFLOW_TOOL_SPEC_INVALID"


def test_tool_step_executor_unavailable_fails_closed():
    # engine 上没有任何 tool_dispatcher / tool_registry / coordinator
    engine = SimpleNamespace()
    run = _make_run()
    step = _make_step()
    spec = {
        "step_id": "s1",
        "name": "无执行器",
        "mode": "tool_call",
        "tool_spec": {"tool_name": "any_tool"},
    }

    execute_tool_step(engine=engine, run=run, workflow={}, step=step, spec=spec, context=None, outputs={})
    assert step.status == "failed"
    assert step.error_code == "WORKFLOW_TOOL_EXECUTOR_UNAVAILABLE"
    assert step.needs_repair is True


def test_tool_step_recursion_protection():
    tool = MockTool("workflows.run", lambda a, **k: {})
    engine = SimpleNamespace(tool_registry=MockToolRegistry({"workflows.run": tool}))
    run = _make_run()
    step = _make_step()

    # 未授权的工作流递归调用被拦截
    spec = {
        "step_id": "s1",
        "name": "递归工作流",
        "mode": "tool_call",
        "tool_spec": {"tool_name": "workflows.run", "arguments": {"workflow_id": "sub"}},
    }
    execute_tool_step(engine=engine, run=run, workflow={}, step=step, spec=spec, context=None, outputs={})
    assert step.status == "failed"
    assert step.error_code == "WORKFLOW_TOOL_PERMISSION_DENIED"

    # 显式授权的工作流递归调用被放行
    step_ok = _make_step()
    spec_ok = {
        "step_id": "s1",
        "name": "授权递归",
        "mode": "tool_call",
        "allow_recursive_workflow": True,
        "tool_spec": {"tool_name": "workflows.run", "arguments": {"workflow_id": "sub"}},
    }
    execute_tool_step(engine=engine, run=run, workflow={}, step=step_ok, spec=spec_ok, context=None, outputs={})
    assert step_ok.status == "succeeded"


def test_tool_step_allowlist_and_denylist_policy():
    tool_a = MockTool("tool_a", lambda a, **k: {"res": "a"})
    tool_b = MockTool("tool_b", lambda a, **k: {"res": "b"})
    engine = SimpleNamespace(tool_registry=MockToolRegistry({"tool_a": tool_a, "tool_b": tool_b}))

    run = _make_run()
    step = _make_step()

    # tool_b 被 spec.denied_tools 阻止
    spec_denied = {
        "step_id": "s1",
        "name": "黑名单拦截",
        "mode": "tool_call",
        "denied_tools": ["tool_b"],
        "tool_spec": {"tool_name": "tool_b"},
    }
    execute_tool_step(engine=engine, run=run, workflow={}, step=step, spec=spec_denied, context=None, outputs={})
    assert step.status == "failed"
    assert step.error_code == "WORKFLOW_TOOL_PERMISSION_DENIED"

    # tool_b 不在 spec.allowed_tools 中被白名单阻止
    step2 = _make_step()
    spec_allowlist = {
        "step_id": "s1",
        "name": "白名单拦截",
        "mode": "tool_call",
        "allowed_tools": ["tool_a"],
        "tool_spec": {"tool_name": "tool_b"},
    }
    execute_tool_step(engine=engine, run=run, workflow={}, step=step2, spec=spec_allowlist, context=None, outputs={})
    assert step2.status == "failed"
    assert step2.error_code == "WORKFLOW_TOOL_PERMISSION_DENIED"


def test_tool_step_destructive_tool_policy():
    rm_tool = MockTool("delete_file", lambda a, **k: {"deleted": True})
    engine = SimpleNamespace(tool_registry=MockToolRegistry({"delete_file": rm_tool}))
    run = _make_run()

    # 默认不允许破坏性工具
    step = _make_step()
    spec = {
        "step_id": "s1",
        "name": "删除文件",
        "mode": "tool_call",
        "tool_spec": {"tool_name": "delete_file", "arguments": {"path": "/tmp/test"}},
    }
    execute_tool_step(engine=engine, run=run, workflow={}, step=step, spec=spec, context=None, outputs={})
    assert step.status == "failed"
    assert step.error_code == "WORKFLOW_TOOL_PERMISSION_DENIED"

    # 显式 allow_destructive 放行
    step_ok = _make_step()
    spec_ok = {
        "step_id": "s1",
        "name": "授权删除文件",
        "mode": "tool_call",
        "allow_destructive": True,
        "tool_spec": {"tool_name": "delete_file", "arguments": {"path": "/tmp/test"}},
    }
    execute_tool_step(engine=engine, run=run, workflow={}, step=step_ok, spec=spec_ok, context=None, outputs={})
    assert step_ok.status == "succeeded"


def test_tool_step_schema_validation_failures():
    schema = {
        "type": "object",
        "required": ["file_path", "count"],
        "properties": {
            "file_path": {"type": "string"},
            "count": {"type": "integer"},
        },
        "additionalProperties": False,
    }
    tool = MockTool("read_lines", lambda a, **k: {"lines": []}, parameters=schema)
    engine = SimpleNamespace(tool_registry=MockToolRegistry({"read_lines": tool}))
    run = _make_run()

    # 1. 缺少必填参数
    step1 = _make_step()
    spec1 = {
        "step_id": "s1", "name": "缺参数", "mode": "tool_call",
        "tool_spec": {"tool_name": "read_lines", "arguments": {"file_path": "a.txt"}},
    }
    execute_tool_step(engine=engine, run=run, workflow={}, step=step1, spec=spec1, context=None, outputs={})
    assert step1.status == "failed"
    assert step1.error_code == "WORKFLOW_TOOL_SCHEMA_INVALID"

    # 2. 参数类型不匹配 (count 预期 integer，提供了 string)
    step2 = _make_step()
    spec2 = {
        "step_id": "s1", "name": "类型错误", "mode": "tool_call",
        "tool_spec": {"tool_name": "read_lines", "arguments": {"file_path": "a.txt", "count": "five"}},
    }
    execute_tool_step(engine=engine, run=run, workflow={}, step=step2, spec=spec2, context=None, outputs={})
    assert step2.status == "failed"
    assert step2.error_code == "WORKFLOW_TOOL_SCHEMA_INVALID"

    # 3. 未声明的附加参数 (additionalProperties=False)
    step3 = _make_step()
    spec3 = {
        "step_id": "s1", "name": "多余参数", "mode": "tool_call",
        "tool_spec": {"tool_name": "read_lines", "arguments": {"file_path": "a.txt", "count": 5, "extra": True}},
    }
    execute_tool_step(engine=engine, run=run, workflow={}, step=step3, spec=spec3, context=None, outputs={})
    assert step3.status == "failed"
    assert step3.error_code == "WORKFLOW_TOOL_SCHEMA_INVALID"


def test_tool_step_execution_error_records_failure_receipt():
    def failing_tool(args, **kwargs):
        raise ConnectionError("Upstream service unavailable")

    tool = MockTool("remote_api", failing_tool)
    engine = SimpleNamespace(tool_registry=MockToolRegistry({"remote_api": tool}))
    run = _make_run()
    step = _make_step()
    spec = {
        "step_id": "s1",
        "name": "报错节点",
        "mode": "tool_call",
        "tool_spec": {"tool_name": "remote_api"},
    }

    execute_tool_step(engine=engine, run=run, workflow={}, step=step, spec=spec, context=None, outputs={})
    assert step.status == "failed"
    assert step.error_code == "WORKFLOW_TOOL_EXECUTION_FAILED"
    assert "Upstream service unavailable" in step.error_message
    assert step.needs_repair is True

    receipt = step.escalation_result.get("tool_receipt")
    assert receipt is not None
    assert receipt["status"] == "failed"
    assert receipt["error_code"] == "WORKFLOW_TOOL_EXECUTION_FAILED"
    assert "Upstream service unavailable" in str(receipt["output_summary"])


def test_tool_step_formal_dag_execution_with_workflow_engine(tmp_path):
    executed = {}

    def fetch_weather(args, **kwargs):
        executed["city"] = args.get("city")
        return {"temp": 22, "condition": "Sunny"}

    weather_tool = MockTool("fetch_weather", fetch_weather)
    registry = MockToolRegistry({"fetch_weather": weather_tool})

    engine = WorkflowEngine(tmp_path)
    engine.tool_registry = registry  # 注入工具注册表

    step_spec = WorkflowStepSpec(
        step_id="t1",
        name="天气工具节点",
        mode="tool_call",
        tool_spec={
            "tool_name": "fetch_weather",
            "arguments_map": {"city": "$inputs.target_city"},
        },
    )
    wf = WorkflowDefinition(_make_descriptor("wf_tool_test"), (step_spec,))
    WorkflowStore(tmp_path).save_workflow(wf)

    run_record = engine.run_workflow(wf, inputs={"target_city": "Shanghai"})
    assert run_record.status == "succeeded"
    assert executed["city"] == "Shanghai"

    step_result = run_record.steps[0]
    assert step_result.status == "succeeded"
    assert step_result.output_snapshot == {"temp": 22, "condition": "Sunny"}
    assert step_result.escalation_result.get("tool_receipt") is not None
    assert step_result.escalation_result["tool_receipt"]["tool_name"] == "fetch_weather"


# ==============================================================================
# WF-02: Analysis Isolation Tests
# ==============================================================================

def test_analysis_isolation_backward_compatibility():
    # 未传 isolation_executor 时保持 100% 向后兼容
    res_default = resolve_analysis_isolation({})
    assert res_default == {"requested": "stateless_skill", "mode": "stateless_skill", "note": ""}

    res_subagent = resolve_analysis_isolation({"analysis_isolation": "lightweight_subagent"})
    assert res_subagent == {
        "requested": "lightweight_subagent",
        "mode": "stateless_skill",
        "note": "minimal_profile_entry_unavailable",
    }


def test_analysis_isolation_active_lightweight_subagent():
    def mock_executor(spec, context, profile):
        return {"analysis": "ok"}

    res = resolve_analysis_isolation(
        {"analysis_isolation": "lightweight_subagent"},
        step_config={"allowed_tools": ["read_file", "search_text"], "max_turns": 2, "timeout_seconds": 15.0},
        isolation_executor=mock_executor,
    )
    assert res["requested"] == "lightweight_subagent"
    assert res["mode"] == "lightweight_subagent"
    assert res["note"] == ""
    profile = res["profile"]
    assert profile["allowed_tools"] == ("read_file", "search_text")
    assert profile["memory_write"] is False
    assert profile["stateless_transcript"] is True
    assert profile["max_turns"] == 2
    assert profile["timeout_seconds"] == 15.0


def test_analysis_isolation_forbids_mutating_tools():
    def mock_executor(spec, context, profile):
        return {}

    for forbidden_tool in ("bash", "write_file", "delete_file", "exec", "workflows"):
        with pytest.raises(WorkflowIsolationToolForbidden) as excinfo:
            resolve_analysis_isolation(
                {"analysis_isolation": "lightweight_subagent"},
                step_config={"allowed_tools": [forbidden_tool]},
                isolation_executor=mock_executor,
            )
        assert excinfo.value.error_code == "WORKFLOW_ISOLATION_TOOL_FORBIDDEN"


def test_run_isolated_analysis_successful_and_structured():
    def mock_executor(spec, context, profile):
        assert profile["memory_write"] is False
        assert profile["stateless_transcript"] is True
        return {"summary": "Analysis complete", "confidence": 0.95}

    result = run_isolated_analysis(
        spec={"config": {"allowed_tools": ["read_file"]}},
        context=SimpleNamespace(),
        isolation_executor=mock_executor,
    )
    assert result == {"summary": "Analysis complete", "confidence": 0.95}


def test_run_isolated_analysis_validates_json_structure():
    # 模拟返回非 JSON 字符串
    def bad_executor(spec, context, profile):
        return "Not a JSON string"

    with pytest.raises(ValueError, match="valid JSON object"):
        run_isolated_analysis(
            spec={},
            context=None,
            isolation_executor=bad_executor,
        )


# ==============================================================================
# G5b: WorkflowAgentTaskPort.v1 Tests
# ==============================================================================

class MockBackgroundService:
    def __init__(self, store: Any):
        self.store = store
        self.cancelled_tasks = []

    def cancel_task(self, task_id: str, *, actor: Any = None) -> dict[str, Any]:
        self.cancelled_tasks.append((task_id, getattr(actor, "owner_id", "")))
        return {"operation_id": task_id, "state": "completed", "outstanding_refs": []}


def test_build_default_workflow_agent_steps_composition(tmp_path):
    tasks_dir = tmp_path / "conv_storage" / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    storage = SimpleNamespace(root=tmp_path / "conv_storage", session_tasks_dir=tasks_dir)
    session_tasks = SessionTaskStore(storage)
    conv_store = SimpleNamespace(session_tasks=session_tasks, threads=SimpleNamespace())

    bg_service = MockBackgroundService(conv_store)
    host = SimpleNamespace(
        owner_home=tmp_path,
        conversation_store=conv_store,
        business_background=bg_service,
    )

    agents = build_default_workflow_agent_steps(host)
    assert isinstance(agents, WorkflowAgentSteps)

    # Actor provider 验证
    run = SimpleNamespace(owner_id="owner-test", workflow_id="wf-99")
    actor = agents._actor(run)
    assert actor.owner_id == "owner-test"
    assert actor.source_thread_id == "workflow:wf-99"


def test_read_session_task_completion_lifecycle(tmp_path):
    tasks_dir = tmp_path / "conv_storage_2" / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    storage = SimpleNamespace(root=tmp_path / "conv_storage_2", session_tasks_dir=tasks_dir)
    session_tasks = SessionTaskStore(storage)
    conv_store = SimpleNamespace(session_tasks=session_tasks, threads=SimpleNamespace(get=lambda _id: None))

    # 1. 创建处于 queued/running 状态的任务
    draft = SessionTaskDraft(
        sender_thread_id="thread-send",
        target_thread_id="thread-target",
        goal="后台数据分析",
        dedupe_key="dedupe-1",
    )
    task = session_tasks.create(draft)
    task_id = task.task_id
    # 写入 business_context 包含 owner_id
    p = session_tasks._path(task_id)
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["metadata"] = {"business_context": {"owner_id": "owner-1"}}
    p.write_text(json.dumps(raw), encoding="utf-8")

    run = SimpleNamespace(owner_id="owner-1", workflow_run_id="run-1", workflow_id="wf-1")
    step = SimpleNamespace(step_id="step-agent", escalation_result={"task_id": task_id, "attempt_id": "att-1"})

    # 读取 running 状态
    res_running = read_session_task_completion(conv_store, run, step)
    assert res_running["state"] == "running"
    assert res_running["status"] == "running"

    # 2. 推进为 accepted -> done 状态
    session_tasks.advance(task_id, SessionTaskUpdate(status="accepted"))
    session_tasks.advance(
        task_id,
        SessionTaskUpdate(
            status="done",
            summary="分析成功",
            result_refs=("ref1",),
        ),
    )
    res_completed = read_session_task_completion(conv_store, run, step)
    assert res_completed["state"] == "succeeded"
    assert res_completed["status"] == "completed"
    assert res_completed["output"]["summary"] == "分析成功"
    assert res_completed["output"]["result_refs"] == ["ref1"]

    # 3. 校验所有者不匹配抛 PermissionError
    run_wrong_owner = SimpleNamespace(owner_id="owner-intruder", workflow_run_id="run-1", workflow_id="wf-1")
    with pytest.raises(PermissionError, match="WORKFLOW_AGENT_OWNER_MISMATCH"):
        read_session_task_completion(conv_store, run_wrong_owner, step)

    # 4. 校验派发身份不匹配抛 PermissionError
    step_dispatch_mismatch = SimpleNamespace(
        step_id="step-agent",
        escalation_result={
            "task_id": task_id,
            "attempt_id": "att-1",
            "session_task_dispatch": {"target_thread_id": "wrong-thread", "dedupe_key": "dedupe-1"},
        },
    )
    with pytest.raises(PermissionError, match="WORKFLOW_AGENT_DISPATCH_IDENTITY_MISMATCH"):
        read_session_task_completion(conv_store, run, step_dispatch_mismatch)


def test_cancel_workflow_session_task_idempotency(tmp_path):
    tasks_dir = tmp_path / "conv_storage_3" / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    storage = SimpleNamespace(root=tmp_path / "conv_storage_3", session_tasks_dir=tasks_dir)
    session_tasks = SessionTaskStore(storage)
    conv_store = SimpleNamespace(session_tasks=session_tasks)

    bg_service = MockBackgroundService(conv_store)
    run = SimpleNamespace(owner_id="owner-1", workflow_run_id="run-1")
    step = SimpleNamespace(step_id="s1", escalation_result={"task_id": "task-xyz", "attempt_id": "att-1"})

    receipt = cancel_workflow_session_task(
        bg_service,
        conv_store,
        run,
        step,
        actor=SimpleNamespace(owner_id="owner-1"),
    )
    assert receipt["state"] == "completed"
    assert receipt["outstanding_refs"] == []
    assert len(bg_service.cancelled_tasks) == 1
    assert bg_service.cancelled_tasks[0] == ("task-xyz", "owner-1")


def test_cancel_workflow_session_task_pending_on_failure():
    class FailingBackgroundService:
        def cancel_task(self, task_id: str, *, actor: Any = None):
            raise RuntimeError("background service cancel failure")

    class FailingSessionTasks:
        def require(self, task_id: str):
            raise KeyError(f"task not found: {task_id}")

    bg_service = FailingBackgroundService()
    conv_store = SimpleNamespace(session_tasks=FailingSessionTasks())
    run = SimpleNamespace(owner_id="owner-1", workflow_run_id="run-1")
    step = SimpleNamespace(step_id="s1", escalation_result={"task_id": "task-fail-xyz", "attempt_id": "att-1"})

    receipt = cancel_workflow_session_task(
        bg_service,
        conv_store,
        run,
        step,
        actor=SimpleNamespace(owner_id="owner-1"),
    )
    assert receipt["operation_id"] == "task-fail-xyz"
    assert receipt["state"] == "pending"
    assert receipt["outstanding_refs"] == ["task-fail-xyz"]


def test_tool_step_bearer_token_redaction_in_receipt():
    def auth_tool(args, **kwargs):
        return {"auth_header": "Bearer secret_token_12345", "user": "alice"}

    tool = MockTool("auth_check", auth_tool)
    engine = SimpleNamespace(tool_registry=MockToolRegistry({"auth_check": tool}))
    run = _make_run()
    step = _make_step()
    spec = {
        "step_id": "s1",
        "name": "鉴权工具",
        "mode": "tool_call",
        "tool_spec": {"tool_name": "auth_check"},
    }

    execute_tool_step(engine=engine, run=run, workflow={}, step=step, spec=spec, context=None, outputs={})
    assert step.status == "succeeded"
    receipt = step.escalation_result.get("tool_receipt")
    assert receipt is not None
    # 验证 Bearer 令牌已被脱敏
    summary = receipt["output_summary"]
    assert "secret_token_12345" not in str(summary)
    assert "<redacted>" in summary.get("auth_header", "")


def test_tool_step_via_tool_dispatcher_interface():
    dispatched_calls = []

    class MockDispatcher:
        def execute_scoped(self, tool_name, arguments, **kwargs):
            dispatched_calls.append((tool_name, arguments, kwargs))
            return {"dispatched": True, "count": arguments.get("c", 0)}

    engine = SimpleNamespace(tool_dispatcher=MockDispatcher())
    run = _make_run()
    step = _make_step()
    spec = {
        "step_id": "s1",
        "name": "分派器节点",
        "mode": "tool_call",
        "tool_spec": {"tool_name": "custom_worker", "arguments": {"c": 99}},
    }

    execute_tool_step(engine=engine, run=run, workflow={}, step=step, spec=spec, context=None, outputs={})
    assert step.status == "succeeded"
    assert step.output_snapshot == {"dispatched": True, "count": 99}
    assert len(dispatched_calls) == 1
    assert dispatched_calls[0][0] == "custom_worker"
    assert dispatched_calls[0][1] == {"c": 99}


def test_workflow_agent_steps_full_integration(tmp_path):
    tasks_dir = tmp_path / "conv_storage_integ" / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    storage = SimpleNamespace(root=tmp_path / "conv_storage_integ", session_tasks_dir=tasks_dir)
    session_tasks = SessionTaskStore(storage)
    conv_store = SimpleNamespace(session_tasks=session_tasks, threads=SimpleNamespace(get=lambda _id: None))

    bg_service = MockBackgroundService(conv_store)
    host = SimpleNamespace(
        owner_home=tmp_path,
        conversation_store=conv_store,
        business_background=bg_service,
    )

    agents = build_default_workflow_agent_steps(host)
    assert agents is not None

    draft = SessionTaskDraft(
        sender_thread_id="th-s",
        target_thread_id="th-t",
        goal="任务目标",
        dedupe_key="dk-10",
    )
    task = session_tasks.create(draft)
    task_id = task.task_id

    # 写入 metadata business_context
    p = session_tasks._path(task_id)
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["metadata"] = {"business_context": {"owner_id": "owner-1"}, "output": {"data": "done_val"}}
    p.write_text(json.dumps(raw), encoding="utf-8")

    session_tasks.advance(task_id, SessionTaskUpdate(status="accepted"))
    session_tasks.advance(task_id, SessionTaskUpdate(status="done", summary="任务成功"))

    run = SimpleNamespace(owner_id="owner-1", workflow_run_id="run-int", workflow_id="wf-int")
    step = SimpleNamespace(step_id="step-ag", escalation_result={"task_id": task_id, "attempt_id": "att-int"})

    # 验证通过 WorkflowAgentSteps.completion 读取
    completed_payload = agents.completion(run, step)
    assert completed_payload is not None
    assert completed_payload["status"] == "completed"
    assert completed_payload["output"] == {"data": "done_val"}

    # 验证通过 WorkflowAgentSteps.cancel 停止
    cancel_success = agents.cancel(run, step)
    assert cancel_success is True
    assert step.escalation_result.get("cancellation_receipt") is not None


def test_make_session_task_completion_reader_resolves_owner_from_threads_load(tmp_path):
    """验证当 SessionTask.metadata 中无 inline owner_id 时，make_session_task_completion_reader

    能通过真实的 ConversationStore / ThreadStore.load(target_thread_id) 准确解析出所属 owner_id，
    并对合法 owner 放行、对非法 owner 坚决拦截抛出 WORKFLOW_AGENT_OWNER_MISMATCH。
    """
    conv_store = ConversationStore(tmp_path / "conv_storage_real")

    # 1. 在真实 ThreadStore 创建目标线程，锁定 owner_id 为 "owner-alice"
    thread = conv_store.threads.create(owner_id="owner-alice", title="Alice Target Thread")
    assert hasattr(conv_store.threads, "load")

    # 2. 创建真实 SessionTask，指向 target_thread_id=thread.thread_id
    # 注意：新创建的 SessionTask 其 metadata 为空，内部无 business_context.owner_id
    draft = SessionTaskDraft(
        sender_thread_id="thread-sender",
        target_thread_id=thread.thread_id,
        goal="分析任务",
        dedupe_key="dedupe-real-task-1",
    )
    task = conv_store.session_tasks.create(draft)
    task_id = task.task_id

    # 验证此时 task.metadata 确实无 inline owner_id，task.owner_id 亦为空
    assert not getattr(task, "owner_id", None)
    meta = getattr(task, "metadata", {}) or {}
    assert "owner_id" not in meta.get("business_context", {})

    # 3. 构造 reader
    reader = make_session_task_completion_reader(conv_store)

    run_alice = SimpleNamespace(owner_id="owner-alice", workflow_run_id="run-1", workflow_id="wf-1")
    step = SimpleNamespace(step_id="step-ag", escalation_result={"task_id": task_id, "attempt_id": "att-1"})

    # 4. running 状态下通过 reader 读取：应当成功解析 thread.owner_id 并放行
    res_running = reader(run_alice, step)
    assert res_running is not None
    assert res_running["status"] == "running"
    assert res_running["state"] == "running"
    assert res_running["owner_id"] == "owner-alice"

    # 5. 支持 reader(task_id, owner_id=...) 单任务参数形式调用
    res_direct = reader(task_id, owner_id="owner-alice", attempt_id="att-1")
    assert res_direct is not None
    assert res_direct["status"] == "running"

    # 6. 非法 owner (intruder) 尝试读取：必须被拦截并抛出 PermissionError("WORKFLOW_AGENT_OWNER_MISMATCH")
    run_intruder = SimpleNamespace(owner_id="owner-intruder", workflow_run_id="run-1", workflow_id="wf-1")
    with pytest.raises(PermissionError, match="WORKFLOW_AGENT_OWNER_MISMATCH"):
        reader(run_intruder, step)

    with pytest.raises(PermissionError, match="WORKFLOW_AGENT_OWNER_MISMATCH"):
        reader(task_id, owner_id="owner-intruder")

    # 7. 推进任务到 accepted -> done 终态并附带结果，验证 reader 能读出完成证据
    conv_store.session_tasks.advance(task_id, SessionTaskUpdate(status="accepted"))
    conv_store.session_tasks.advance(
        task_id,
        SessionTaskUpdate(status="done", summary="执行成功", result_refs=("ref_evidence_1",)),
    )
    res_done = reader(run_alice, step)
    assert res_done is not None
    assert res_done["status"] == "completed"
    assert res_done["state"] == "succeeded"
    assert res_done["output"]["summary"] == "执行成功"
    assert res_done["output"]["result_refs"] == ["ref_evidence_1"]

    # 8. 降级兼容测试：测试桩若无 .load 仅有 .get，仍能平稳回退并正确解析
    class LegacyThreadStoreDouble:
        def __init__(self, owner_map):
            self._map = owner_map

        def get(self, tid):
            return SimpleNamespace(owner_id=self._map.get(tid))

    conv_store_legacy = SimpleNamespace(
        session_tasks=conv_store.session_tasks,
        threads=LegacyThreadStoreDouble({thread.thread_id: "owner-alice"}),
    )
    reader_legacy = make_session_task_completion_reader(conv_store_legacy)
    res_legacy = reader_legacy(run_alice, step)
    assert res_legacy is not None
    assert res_legacy["status"] == "completed"
