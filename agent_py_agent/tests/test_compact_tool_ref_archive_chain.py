"""Compact 工具来源沿真实归档链的组合复现（2026-09-27 Codex G02：每组工具往返都带引用回执，
修复前整组留在保留区、归档记录被连带排除，自动 Compact 报 COMPACT_TOOL_COVERAGE_UNKNOWN）。

零网络：真实外置/归档投影、与 executor 同形的成功回执、归档记录、reducer 实时投影、原生 IR 记录器、
真实 read_artifact 读取器、分区与 CarriedToolCompactSource、摘要素材。只把归档根、产物登记根和运行范围
解析指到 tmp_path，不起模型、不读宿主配置。

锁定：
- 外置文本与内联结果同组、read_artifact 来源引用、恢复 attempt 的调用整组移入摘要来源；
- 摘要素材就是模型当时看到的原回执，不按引用去取全文；read_artifact 引用上的摘要值描述本次回执，判据不看它；
- 工具自报引用的组保留原位，其归档记录不进入来源；
- 同步：归档投影给出的“完整原始输出”引用总是归档自己写下的输出位置字段，工具自报引用从不属于这些字段。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import tool_call_archive_record as archive_module
from agent_py_agent.agent.agent_core.compact_tool_partition import (
    archive_ref_values_by_key,
    partition_recovery_tool_source,
)
from agent_py_agent.agent.agent_core.tool_context.reducer import render_tool_result_for_live_prompt
from agent_py_agent.agent.agent_core.tool_ir_history import record_tool_call_ir
from agent_py_agent.agent.agent_core.tool_loop.round_execution import ToolCallRecordParams
from agent_py_agent.agent.conversation.compact_tool_summary import (
    compact_tool_summary_history,
    compact_tool_summary_text,
)
from agent_py_agent.agent.memory_archive.artifact.reader import (
    ReadToolOutputArtifactRequest,
    read_tool_output_artifact,
)
from agent_py_agent.agent.tooling.artifact import _model_visible_read_payload
from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall, ToolResult, ToolSuccessFacts

_RUN = "run-1"
_MIN_CHARS = 400


def _call(call_id: str, tool: str, arguments: dict[str, object], *, turn: str, attempt: str = "attempt-1") -> ToolCall:
    return ToolCall(call_id, tool, arguments, "native", "sha256:fixture", _RUN, turn, attempt)


def _ref(call: ToolCall) -> dict[str, str]:
    return {key: getattr(call, key) for key in ("run_id", "attempt_id", "turn_id", "call_id")}


@pytest.fixture
def chain(tmp_path, monkeypatch):
    root = tmp_path / "task"
    monkeypatch.setattr(archive_module, "_tool_output_archive_root", lambda *_: root)
    monkeypatch.setattr(archive_module, "_artifact_registry_root", lambda *_: root)
    monkeypatch.setattr(archive_module, "runtime_run_scope", lambda *_: SimpleNamespace(to_dict=lambda: {"run_id": _RUN}))
    agent = SimpleNamespace(config=SimpleNamespace(
        tool_output_externalize_min_chars=_MIN_CHARS, tool_output_preview_chars=120,
        tool_output_externalize_on_low_headroom=False,
    ))
    params = SimpleNamespace(
        request_id="req-1", task_id="task-1", run_id=_RUN, tool_runtime_snapshot=None, task_attributes={},
        live_archive_state={}, tool_ir_history=[], archive_tool_calls=[],
    )
    return SimpleNamespace(root=root, agent=agent, params=params, tmp_path=tmp_path)


# LLM: 与 executor._canonical_result 同形组装成功回执，再按 _tool_loop_service 的顺序写归档记录、做 reducer 实时投影、
#   记入原生 IR；归档记录进入 params.archive_tool_calls，正是恢复分区读取的那份列表。
# 函数用途: 让一次工具调用沿真实归档链进入原生历史与归档记录列表。
def _run_tool(chain, call: ToolCall, outcome: ToolHandlerOutcome, *, tool_round: int, idx: int = 0):
    projection = archive_module.archive_tool_output_projection(chain.agent, chain.params, call, outcome)
    result = ToolResult.succeeded(call, facts=ToolSuccessFacts(
        content_blocks=projection.content_blocks, refs=projection.refs,
        metadata={**projection.metadata, "handler_details": dict(outcome.result_envelope or {})},
    ))
    record = archive_module.archive_tool_call_record(chain.agent, ToolCallRecordParams(
        params=chain.params, tool_rounds=tool_round, idx=idx, call=call, result=result,
    ))
    chain.params.archive_tool_calls.append(record)
    live = result.with_live_prompt_projection(render_tool_result_for_live_prompt(result, record))
    record_tool_call_ir(chain.params, tool_rounds=tool_round, call=call, result=live)
    return result, record


def _search_output(label: str, count: int) -> str:
    return json.dumps({"results": [{"name": f"{label}-{index}", "summary": "能力包资源说明"} for index in range(count)]},
                      ensure_ascii=False)


def test_real_archive_chain_sources_ref_results_and_keeps_handler_refs_retained(chain):
    big = _call("search-big", "skill_search", {"query": "能力包资源"}, turn="turn-1")
    small = _call("search-small", "skill_search", {"query": "checker"}, turn="turn-1")
    _, big_record = _run_tool(chain, big, ToolHandlerOutcome("skill_search", True, _search_output("pack", 40)),
                              tool_round=1)
    small_result, _ = _run_tool(chain, small, ToolHandlerOutcome("skill_search", True, _search_output("check", 1)),
                                tool_round=1, idx=1)
    assert big_record["output_externalized"] is True and small_result.refs == ()

    scoped = str(big_record["scoped_call_id"])
    payload = read_tool_output_artifact(ReadToolOutputArtifactRequest(
        root=chain.root, artifact_ref=scoped, max_chars=300, run_id=_RUN,
    ))
    assert payload["ok"] is True and payload["reads_artifact_body"] is True
    reader = _call("reader", "read_artifact", {"artifact_ref": scoped, "max_chars": 300}, turn="turn-2")
    reader_result, reader_record = _run_tool(chain, reader, ToolHandlerOutcome(
        "read_artifact", True, json.dumps(_model_visible_read_payload(payload), ensure_ascii=False),
    ), tool_round=2)
    # 来源引用指向被读调用；引用上的摘要值是本次回执的，不是被读归档的。
    assert [ref.ref for ref in reader_result.refs] == [scoped] == [reader_record["source_artifact_ref"]]
    assert reader_result.refs[0].sha256 == reader_record["output_hash"] != big_record["output_hash"]

    deliverable = chain.tmp_path / "deliverable.md"
    deliverable.write_text("交付物正文", encoding="utf-8")
    declared = _call("declared", "write_file", {"path": str(deliverable)}, turn="turn-3")
    declared_result, declared_record = _run_tool(chain, declared, ToolHandlerOutcome(
        "write_file", True, "written", result_envelope={"output_refs": [{"kind": "artifact", "ref": str(deliverable)}]},
    ), tool_round=3)
    assert [ref.ref for ref in declared_result.refs] == [str(deliverable)]
    assert declared_record["tool_result_refs"][0]["ref"] == str(deliverable)

    resumed = _call("search-resumed", "skill_search", {"query": "续跑"}, turn="turn-4", attempt="attempt-2")
    _run_tool(chain, resumed, ToolHandlerOutcome("skill_search", True, _search_output("resume", 40)), tool_round=4)

    history = chain.params.tool_ir_history
    records = chain.params.archive_tool_calls
    source = partition_recovery_tool_source(records, history)

    assert source.source_tool_refs == (_ref(big), _ref(small), _ref(reader), _ref(resumed))
    assert source.retained_tool_refs == (_ref(declared),)
    assert source.source_records == (records[0], records[1], records[2], records[4])
    assert source.retained_records == (records[3],)
    assert source.source_ir_history == (*history[:5], *history[7:])
    assert source.retained_ir_history == tuple(history[5:7])
    material = compact_tool_summary_history(source.source_records, source.source_ir_history)
    assert material == list(source.source_ir_history)
    assert scoped in compact_tool_summary_text(material)


@pytest.mark.parametrize("tool, size", [("skill_search", 2_000), ("skill_search", 50), ("read_file", 2_000)])
def test_projection_complete_output_ref_is_an_archive_owned_field(chain, tool, size):
    call = _call("call-1", tool, {"path": "/workspace/notes.txt"}, turn="turn-1")
    declared = "/handler/declared.md"
    result, record = _run_tool(chain, call, ToolHandlerOutcome(
        tool, True, "x" * size, result_envelope={"output_refs": [{"ref": declared}]},
    ), tool_round=1)

    (owned,) = archive_ref_values_by_key([record]).values()
    complete = [ref.ref for ref in result.refs if ref.summary == "complete raw tool output"]
    assert set(complete) <= owned
    assert bool(complete) == (size >= _MIN_CHARS)
    assert [ref.ref for ref in result.refs][-1] == declared and declared not in owned
