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
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.runtime_contracts import ProviderToolCapability
from agent_py_agent.tests.test_tool_round_execution import _round_request, _success

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
