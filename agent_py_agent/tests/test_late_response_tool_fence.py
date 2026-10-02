"""I3（第 8 条①）：宿主停机关门后，迟到的模型响应里的工具调用不执行。

锁定：每个未启动调用前的顺序屏障先读本进程模型调用准入（contracts.model_call_ledger.model_call_admission_closure）；
关门后当前及后续调用一律不启动，记成 HOST_SHUTDOWN_TOOL_NOT_STARTED（cancelled、未执行、无副作用），关门原因码与模型调用
账本把在途调用结清成 failed 时用的是同一份；轮中途关门只拦还没启动的；停机优先于普通取消。真实工具循环里，调用在途时
停机结清、响应稍后带着写文件调用回来：文件不落盘，账本保持 failed，下一次模型调用被准入拒绝、请求不发出。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model.call_runtime import (
    MODEL_CALL_INTERRUPTED_ERROR_CODE,
    model_call_ledger,
    settle_open_model_calls_for_shutdown,
)
from agent_py_agent.agent.agent_core.tool_loop.round_execution import execute_tool_round
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.common.cancellation import CancellationToken
from agent_py_agent.agent.contracts.error_taxonomy import error_contract
from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallAdmissionClosedError,
    ModelCallAdmissionClosure,
    close_model_call_admission,
    model_call_admission_closure,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.runtime_contracts import ProviderToolCapability
from agent_py_agent.tests._tool_runtime_harness import (
    make_test_model_spec,
    make_test_runtime_policy,
    runtime_snapshot_for_model_specs,
)
from agent_py_agent.tests.test_tool_round_execution import (
    _ROUND_RUN_ID,
    _approval_pending,
    _round_request,
    _success,
)

_CLOSURE = ModelCallAdmissionClosure("HostShutdownInterrupted", MODEL_CALL_INTERRUPTED_ERROR_CODE)
_SHUTDOWN_FACTS = {"reason_code": MODEL_CALL_INTERRUPTED_ERROR_CODE, "error_type": "HostShutdownInterrupted",
                   "admission_error_code": "MODEL_CALL_ADMISSION_CLOSED"}


def _run_round(calls, *, execute_one, params=None):
    records = []
    params = params or SimpleNamespace(tool_context=[])
    execute_tool_round(_round_request(
        agent=SimpleNamespace(), params=params, tool_rounds=1, response=ModelResponse(text="", backend="test"),
        calls=calls, execute_one=execute_one, record_one=records.append,
    ))
    return records, params


def _shape(record):
    result = record.result
    return (result.error_code, result.status, result.handler_executed, result.effect_outcome)


def test_no_tool_starts_once_admission_is_closed():
    close_model_call_admission(_CLOSURE)
    executed = []

    records, params = _run_round(
        [{"tool": "write_file", "path": "a.txt", "content": "x"}, {"tool": "read_file", "path": "b.txt"}],
        execute_one=lambda request: executed.append(request.call.tool_name),
    )

    assert executed == []
    assert [_shape(record) for record in records] == [("HOST_SHUTDOWN_TOOL_NOT_STARTED", "cancelled", False, "not_started")] * 2
    assert all(record.result.metadata["host_shutdown"] == _SHUTDOWN_FACTS for record in records)
    assert all("cancelled" in record.execution_states for record in records)
    assert any("停机" in str(item) for item in params.tool_context)
    assert error_contract("HOST_SHUTDOWN_TOOL_NOT_STARTED").recommended_action == "stop"


def test_closure_during_the_round_only_stops_calls_not_yet_started():
    executed = []

    def execute_one(request):
        executed.append(request.call.arguments["path"])
        close_model_call_admission(_CLOSURE)  # 第一个工具执行期间宿主停机结清
        return _success(request, '{"ok":true}')

    records, _ = _run_round(
        [{"tool": "write_file", "path": "first.txt", "content": "1"},
         {"tool": "write_file", "path": "second.txt", "content": "2"}],
        execute_one=execute_one,
    )

    assert executed == ["first.txt"]
    assert [record.result.error_code for record in records] == ["", "HOST_SHUTDOWN_TOOL_NOT_STARTED"]


def test_host_shutdown_takes_precedence_over_cancellation():
    close_model_call_admission(_CLOSURE)
    token = CancellationToken()
    token.cancel()

    records, _ = _run_round(
        [{"tool": "write_file", "path": "a.txt", "content": "x"}],
        execute_one=lambda request: pytest.fail("停机后不能启动工具"),
        params=SimpleNamespace(tool_context=[], cancellation_token=token),
    )

    assert [record.result.error_code for record in records] == ["HOST_SHUTDOWN_TOOL_NOT_STARTED"]
    result = records[0].result
    assert json.loads(result.output)["hint"] == "停止派发新动作，保存已有进展后收尾。", "用户喊停过，续跑时不能引导重做"
    assert result.metadata["host_shutdown"] == {**_SHUTDOWN_FACTS, "round_cancelled": True}


class _ClosingApprover:
    """等审批期间宿主停机关门，然后批准（9b 复审探针 A）。"""

    def __init__(self):
        self.requests = 0

    def request_permission(self, payload, *, cancellation_token=None):
        self.requests += 1
        close_model_call_admission(_CLOSURE)
        return {"permission_id": payload["permission_id"], "decision": "approved"}


def _parallel_reader_snapshot():
    spec = make_test_model_spec("read_probe", input_schema={
        "type": "object", "properties": {"slot": {"type": "integer"}}, "required": ["slot"], "additionalProperties": False})
    return runtime_snapshot_for_model_specs((spec,), run_id=_ROUND_RUN_ID, policies={
        spec.name: make_test_runtime_policy("read_only", concurrency_mode="parallel_safe", resource_parameters=("slot",))})


@pytest.mark.parametrize("segment", ["serial", "parallel"])
def test_approval_granted_after_closure_does_not_execute(segment):
    approver = _ClosingApprover()
    params = SimpleNamespace(tool_context=[], effective_on_chunk=approver, request_id="approval-fence",
                             cancellation_token=CancellationToken(), runtime_approved_actions=[])
    calls = [{"tool": "run_command", "command": "printf a"}, {"tool": "run_command", "command": "printf b"}]
    if segment == "parallel":  # 两条只读调用同一并行段跑完，审批在段后补
        params.tool_runtime_snapshot = _parallel_reader_snapshot()
        calls = [{"tool": "read_probe", "slot": 0}, {"tool": "read_probe", "slot": 1}]
    started = []

    def execute_one(request):
        started.append(model_call_admission_closure() is not None)
        return _approval_pending(request)

    records, _ = _run_round(calls, execute_one=execute_one, params=params)

    assert True not in started, "关门之后不能再进工具"
    assert started == ([False] if segment == "serial" else [False, False])
    assert [_shape(record) for record in records] == [("HOST_SHUTDOWN_TOOL_NOT_STARTED", "cancelled", False, "not_started")] * 2
    assert params.runtime_approved_actions == [], "没执行就不记批准绑定"
    assert approver.requests == (1 if segment == "serial" else 2)


def test_persisted_execution_facts_keep_only_whitelisted_shutdown_fields():
    from agent_py_agent.agent.agent_core.tool_call_archive_record import _compact_tool_execution
    from agent_py_agent.agent.memory_archive.tool_output_externalizer import _safe_tool_execution

    raw = {"handler_executed": False, "duration_ms": 0, "failure_stage": "runtime_gate",
           "host_shutdown": {**_SHUTDOWN_FACTS, "round_cancelled": True, "private": "不该落盘"}}
    expected = {**_SHUTDOWN_FACTS, "round_cancelled": True}
    assert _compact_tool_execution(raw)["host_shutdown"] == expected
    assert _safe_tool_execution(raw)["host_shutdown"] == expected
    assert "host_shutdown" not in _safe_tool_execution({**raw, "host_shutdown": {"private": "x", "round_cancelled": True}})


class _LateResponseBackend:
    """调用在途时宿主停机结清；物理响应稍后照常回来，并带一个写文件调用。"""

    name = "fake_late_response_after_shutdown"

    def __init__(self):
        self.requests = 0
        self.settled: tuple = ()

    def probe_tool_capability(self):
        return ProviderToolCapability(provider=self.name, endpoint="local://fake", model="", stream=False,
                                      native_supported=True, evidence="test_fake_native")

    def generate(self, prompt: str, on_chunk=None, **kwargs):
        self.requests += 1
        self.settled = settle_open_model_calls_for_shutdown()
        return ModelResponse(text="", backend=self.name, tool_use_blocks=[
            {"id": "call_late_write", "name": "write_file", "input": {"path": "late.txt", "content": "迟到响应想写的内容"}}])


def test_late_response_after_shutdown_settlement_writes_nothing(tmp_path):
    agent = SimpleAgent(AgentConfig(enable_tools=True, max_tool_rounds=5), tmp_path)
    backend = _LateResponseBackend()
    agent.backend = backend

    with pytest.raises(ModelCallAdmissionClosedError):
        agent.run("把这句话写进 late.txt", save=False, allowed_tools=["write_file"])

    assert not (Path(agent.effective_workspace_root) / "late.txt").exists(), "迟到响应里的写文件不能执行"
    assert backend.requests == 1, "下一次模型调用被准入拒绝，请求没有发出"
    assert [fact["error_code"] for fact in backend.settled] == [MODEL_CALL_INTERRUPTED_ERROR_CODE]
    record = next(item for item in model_call_ledger(agent).records() if item.call_id == backend.settled[0]["call_id"])
    assert (record.status, record.error_code) == ("failed", MODEL_CALL_INTERRUPTED_ERROR_CODE), "迟到的成功不重开终态"
    # 工具账写在本次运行的任务工作区里（runs/<日期>/<编号>/work/blobs/tool_outputs/index.jsonl）。
    rows = [json.loads(line) for index in Path(agent.home_paths.root).rglob("work/blobs/tool_outputs/index.jsonl")
            for line in index.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [(row.get("tool"), row.get("error_code")) for row in rows] == [("write_file", "HOST_SHUTDOWN_TOOL_NOT_STARTED")]
    # 落盘的停机原因和模型调用账本逐字段对得上（9b 复审建议 2）。
    assert rows[0]["tool_execution"]["host_shutdown"] == {
        "reason_code": record.error_code, "error_type": record.error_type, "admission_error_code": "MODEL_CALL_ADMISSION_CLOSED"}
    assert rows[0]["tool_execution"]["handler_executed"] is False
