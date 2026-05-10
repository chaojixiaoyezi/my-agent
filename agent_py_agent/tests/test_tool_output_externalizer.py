from __future__ import annotations

"""LLM: regression tests for externalized runtime tool outputs.

给人看的解释：
这组测试确认大工具输出会进入 artifact 文件，归档记录只留下摘要、hash 和路径；
下一轮 live prompt 只读取摘要和恢复锚点，避免把黑盒大输出直接塞回上下文。
"""

import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core._tool_loop_service import ToolCallRecordParams, ToolLoopService
from agent_py_agent.agent.agent_core.tool_call_context_reducer import (
    AssistantToolRoundContextRequest,
    render_assistant_tool_round_context,
    render_tool_payload_for_live_prompt,
)
from agent_py_agent.agent.memory_archive.runtime.turn_archiver import (
    ArchiveRunTurnParams,
    ArchiveTurnContext,
    archive_run_turn,
)
from agent_py_agent.agent.memory_archive.schema import RUNTIME_MEMORY_SCHEMA_VERSION
from agent_py_agent.agent.tooling.models import ToolExecutionResult


def test_tool_loop_externalizes_large_tool_output_for_archive(tmp_path: Path) -> None:
    service = ToolLoopService(SimpleNamespace(root=tmp_path))
    params = _tool_loop_params(request_id="req-tool", run_id="run-tool", task_id="task-tool")
    large_output = "line\n" + ("x" * 1400)

    service._record_tool_call(
        ToolCallRecordParams(
            params=params,
            tool_rounds=1,
            idx=1,
            payload={"tool": "read_file", "path": "large.log"},
            result=ToolExecutionResult("read_file", True, large_output),
        )
    )
    record = params.archive_tool_calls[0]
    artifact_path = Path(record["output_path"])
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    index = [
        json.loads(line)
        for line in (artifact_path.parent / "index.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert record["output_externalized"] is True
    _assert_schema_v2(record, "tool_output_archive_record")
    _assert_schema_v2(artifact, "tool_output_artifact")
    _assert_schema_v2(index[-1], "tool_output_index")
    assert record["output_hash"] == artifact["sha256"]
    assert record["output_size_bytes"] == len(large_output.encode("utf-8"))
    assert "x" * 700 not in json.dumps(record, ensure_ascii=False)
    assert artifact["content"] == large_output
    assert artifact["request_id"] == "req-tool"
    assert index[-1]["path"] == str(artifact_path)
    assert index[-1]["sha256"] == artifact["sha256"]
    assert large_output not in params.tool_context[-1]
    assert str(artifact_path) in params.tool_context[-1]
    assert "完整工具输出已外置" in params.tool_context[-1]
    assert record["fail_safe_checkpoint_path"] in params.tool_context[-1]


def test_tool_loop_summarizes_large_tool_call_payload_for_live_prompt() -> None:
    huge_html = "<html>" + ("x" * 9000) + "</html>"
    response = (
        "我要写商品页面。\n"
        "[TOOL_CALL]\n"
        + json.dumps({"tool": "write_file", "path": "shop/list.html", "content": huge_html})
        + "\n[/TOOL_CALL]"
    )

    rendered = render_assistant_tool_round_context(
        AssistantToolRoundContextRequest(
            response_text=response,
            tool_calls=[{"tool": "write_file", "path": "shop/list.html", "content": huge_html}],
        )
    )

    assert "assistant tool-call response summarized" in rendered
    assert "tool_call_1: tool=write_file" in rendered
    assert "path: shop/list.html" in rendered
    assert "large text omitted" in rendered
    assert huge_html not in rendered


def test_tool_call_record_summarizes_large_payload_for_live_prompt(tmp_path: Path) -> None:
    service = ToolLoopService(SimpleNamespace(root=tmp_path))
    params = _tool_loop_params(request_id="req-tool", run_id="run-tool", task_id="task-tool")
    huge_html = "<html>" + ("x" * 9000) + "</html>"

    service._record_tool_call(
        ToolCallRecordParams(
            params=params,
            tool_rounds=1,
            idx=1,
            payload={"tool": "write_file", "path": "shop/cart.html", "content": huge_html},
            result=ToolExecutionResult("write_file", False, "路径不在 allowed_write_roots 内"),
        )
    )

    live_context = params.tool_context[-1]
    assert "tool_call_1: tool=write_file" in live_context
    assert "path: shop/cart.html" in live_context
    assert "large text omitted" in live_context
    assert huge_html not in live_context


def test_render_tool_payload_keeps_small_payload_readable() -> None:
    payload = {"tool": "read_file", "path": "README.md"}
    rendered = render_tool_payload_for_live_prompt(payload)

    assert "tool_call_1: tool=read_file" in rendered
    assert "path: README.md" in rendered
    assert "{'tool'" not in rendered


def test_tool_loop_writes_fail_safe_checkpoint_before_externalizing_large_output(tmp_path: Path) -> None:
    service = ToolLoopService(SimpleNamespace(root=tmp_path))
    params = _tool_loop_params(request_id="req-tool", run_id="run-tool", task_id="task-tool")
    large_output = "danger\n" + ("x" * 1400)

    service._record_tool_call(
        ToolCallRecordParams(
            params=params,
            tool_rounds=1,
            idx=1,
            payload={"tool": "blackbox_tool"},
            result=ToolExecutionResult("blackbox_tool", True, large_output),
        )
    )

    record = params.archive_tool_calls[0]
    checkpoint_path = Path(record["fail_safe_checkpoint_path"])
    snapshots = [
        json.loads(line)
        for line in checkpoint_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert record["fail_safe_checkpoint_written"] is True
    assert checkpoint_path.exists()
    assert snapshots[-1]["turn_range"]["source"] == "tool_output_externalizer"
    assert snapshots[-1]["tool_calls"][0]["tool"] == "blackbox_tool"
    assert snapshots[-1]["tool_calls"][0]["output_hash"] == record["output_hash"]
    assert snapshots[-1]["next_actions"] == ["先读取工具输出 artifact 摘要和 fail-safe checkpoint，再决定是否把内容切片读回 prompt。"]


def test_archive_tool_event_keeps_externalized_output_path(tmp_path: Path) -> None:
    output_path = tmp_path / "memory_archive" / "artifacts" / "tool_outputs" / "demo.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("{}", encoding="utf-8")

    result = archive_run_turn(
        ArchiveRunTurnParams(
            root=tmp_path,
            ctx=ArchiveTurnContext(
                session_id="session-tool",
                user_prompt="run tool",
                response_text="done",
                backend="echo",
                request_id="req-tool",
                run_id="run-tool",
                task_id="task-tool",
                tool_calls=[{
                    "tool": "read_file",
                    "id": "1-1",
                    "ok": True,
                    "output_preview": "preview",
                    "output_hash": "hash",
                    "output_path": str(output_path),
                    "output_externalized": True,
                    "parameters": {"path": "large.log"},
                }],
            ),
        )
    )
    tool_event = next(event for event in result.events if event.speaker == "tool")

    assert tool_event.content_path == str(output_path)
    assert tool_event.content_preview
    assert tool_event.tool_name == "read_file"


# LLM: _assert_schema_v2 protects the externalized tool output version and reserved fields contract.
# 函数用途: 校验工具输出归档记录、artifact 正文和 index 行都使用统一 runtime memory schema v2。
def _assert_schema_v2(record: dict[str, object], name: str) -> None:
    assert record["version"] == RUNTIME_MEMORY_SCHEMA_VERSION
    assert record["schema"]["name"] == name
    assert record["reserved"]["schema_name"] == name
    assert record["reserved"]["schema_version"] == RUNTIME_MEMORY_SCHEMA_VERSION
    assert set(record["reserved"]) >= {"extensions", "compat", "future"}


def _tool_loop_params(*, request_id: str, run_id: str, task_id: str) -> ToolLoopExecuteParams:
    return ToolLoopExecuteParams(
        user_prompt="",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        granted_capabilities=None,
        write_boundary=None,
        task_attributes=None,
        request_id=request_id,
        run_id=run_id,
        task_id=task_id,
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
    )
