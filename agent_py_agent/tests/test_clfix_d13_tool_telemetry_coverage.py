"""D-13：带结果引用的工具调用（外置原文、产物引用）也要写进工具遥测；遥测写入失败要留下告警，不能静默丢弃。"""
from __future__ import annotations

import json
import logging

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.backends.base import EchoBackend
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.observability.telemetry_ledger import ToolTelemetryLedger
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling import BaseTool, ToolHandlerOutcome
from agent_py_agent.agent.verification import runtime
from agent_py_agent.tests._tool_runtime_harness import (
    make_test_model_spec,
    make_test_runtime_policy,
)
from agent_py_agent.tests.test_verification_runtime import _agent, _call, _success


# 函数用途: 读出 owner 审计日志里全部真实工具调用事件的调用编号。
def _audited_call_ids(home) -> set[str]:
    rows = [json.loads(line) for path in sorted((home / "audit").glob("*.jsonl"))
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {row["tool_call_id"] for row in rows if row.get("action") == "tool_call" and row.get("tool_call_id")}


def test_every_audited_tool_call_gets_a_telemetry_row_even_when_its_result_carries_refs(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    home = agent.home_paths.owner_home_dir
    produced = home / "d13-produced.txt"
    produced.write_text("已生成", encoding="utf-8")

    # 类用途: 三种线上形态各一条——原文超出预览被外置（run_command/tool_search）、信封带产物引用（apply_patch）、无引用小结果。
    class Report(BaseTool):
        model_spec = make_test_model_spec("d13_report", input_schema={
            "type": "object", "properties": {"kind": {"type": "string"}}, "required": ["kind"],
            "additionalProperties": False})
        runtime_policy = make_test_runtime_policy("read_only", concurrency_mode="serial")

        def execute(self, params):
            if params["kind"] == "bulky":
                return ToolHandlerOutcome("d13_report", True, "统计结果第 1 行\n" * 1000)
            if params["kind"] == "produced":
                return ToolHandlerOutcome("d13_report", True, "已写入。", result_envelope={
                    "artifact_refs": [{"path": str(produced), "kind": "file", "status": "ready"}]})
            return ToolHandlerOutcome("d13_report", True, "无引用的小结果。")

    agent.tools.register(Report())

    # 类用途: 第一轮一次提交三条调用，第二轮直接收尾。
    class Backend(EchoBackend):
        rounds = 0

        def generate(self, prompt, on_chunk=None, **kwargs):
            Backend.rounds += 1
            if Backend.rounds == 1:
                return ModelResponse(text="", backend=self.name, tool_use_blocks=[
                    {"id": f"d13-{kind}", "name": "d13_report", "input": {"kind": kind}}
                    for kind in ("bulky", "produced", "small")])
            return ModelResponse(text="三份报告都已完成。", backend=self.name)

    agent.backend = Backend()
    result = agent.run("生成三份报告。", params=RunParams(
        request_id="d13-run", run_id="d13-run", task_id="d13-run", allowed_tools=["d13_report"], save=True))

    archived = {row["call_id"]: row for row in result.archive_tool_calls}
    assert archived["d13-bulky"]["output_externalized"] is True, "前提：大结果真的走了外置归档"
    audited = _audited_call_ids(home)
    assert audited == {"d13-bulky", "d13-produced", "d13-small"}
    ledger = ToolTelemetryLedger(home)
    rows = {call_id: ledger.get_tool_call(call_id) for call_id in sorted(audited)}
    assert not [call_id for call_id, row in rows.items() if row is None], "审计过的已执行调用都必须有遥测行"
    assert rows["d13-bulky"]["artifact_ref"] == archived["d13-bulky"]["output_path"]
    assert rows["d13-produced"]["artifact_ref"] == str(produced)
    assert rows["d13-small"]["artifact_ref"] == ""


def test_telemetry_write_failure_is_logged_once_per_kind_and_keeps_the_tool_result(tmp_path, monkeypatch, caplog):
    agent, _project, home = _agent(tmp_path)
    home.mkdir()
    monkeypatch.setattr(runtime, "_TELEMETRY_FAILURE_KINDS_WARNED", set(), raising=False)

    # 函数用途: 模拟遥测卷不可写。
    def unavailable(self, record):
        raise OSError("telemetry volume unavailable")

    monkeypatch.setattr(ToolTelemetryLedger, "record_tool_call", unavailable)
    call = _call("read_file", {"path": "README.md"})
    original = _success(call, "正文", {})
    with caplog.at_level(logging.DEBUG, logger=runtime.__name__):
        first = runtime.record_tool_verification(agent, call, original)
        second = runtime.record_tool_verification(agent, call, original)

    assert (first.ok, first.output, second.ok, second.output) == (True, "正文", True, "正文")
    records = [item for item in caplog.records
               if item.name == runtime.__name__ and "tool telemetry" in item.getMessage()]
    assert [item.levelno for item in records] == [logging.WARNING, logging.DEBUG], "首次告警，同类重复降为调试日志"
    assert records[0].exc_info is not None
    assert "read_file" in records[0].getMessage() and "call-1" in records[0].getMessage()
