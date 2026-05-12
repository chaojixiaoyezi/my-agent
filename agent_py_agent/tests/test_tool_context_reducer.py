import json

from agent_py_agent.agent.agent_core.tool_context_reducer import render_tool_result_for_live_prompt
from agent_py_agent.agent.tools import ToolExecutionResult


# LLM: dispatch externalization tests protect root context from growing by artifact rereads.
# 函数用途: 验证大型 dispatch_subagents 输出外置后，live prompt 仍保留 refs-first 下一步，而不是诱导模型读正文。
def test_dispatch_externalized_result_keeps_compact_next_action_without_read_hint():
    output = json.dumps(
        {
            "summary": {"runner": 3, "acceptance": 2},
            "records": [{"message": "x" * 2000}],
            "direct_children": {
                "parent_run_id": "root-1",
                "total": 1,
                "by_status": {"AWAITING_ACCEPTANCE": 1},
                "needs_recovery": True,
                "next_action": "inspect_or_rescue_direct_children",
                "recovery_run_ids": ["child-1"],
                "suggested_tool_call": {
                    "tool": "dispatch_subagents",
                    "run_ids": ["child-1"],
                    "execute_runners": True,
                },
            },
        }
    )
    rendered = render_tool_result_for_live_prompt(
        ToolExecutionResult("dispatch_subagents", True, output),
        {
            "output_externalized": True,
            "artifact_ref": "/tmp/tool_outputs/dispatch_subagents-1.json",
            "output_path": "/tmp/tool_outputs/dispatch_subagents-1.json",
            "call_id": "1-1",
            "scoped_call_id": "root-1:1-1",
            "output_hash": "abc",
            "output_size_bytes": len(output),
        },
    )

    assert "orchestration_summary" in rendered
    assert "inspect_or_rescue_direct_children" in rendered
    assert "child-1" in rendered
    assert "output_scoped_call_id: root-1:1-1" in rendered
    assert "prefer output_scoped_call_id" in rendered
    assert "records" not in rendered
    assert "read_artifact_hint" not in rendered
    assert len(rendered) < 1400


# LLM: read_artifact reducer tests prevent repeated full dispatch artifact rereads.
# 函数用途: 验证模型显式读了 dispatch artifact 后，下一轮 prompt 也只保留调度摘要和少量正文预览。
def test_read_artifact_dispatch_content_is_summarized_for_live_prompt():
    dispatch_content = json.dumps(
        {
            "direct_children": {
                "parent_run_id": "root-1",
                "next_action": "create_quality_children_from_ready_refs",
                "quality_advice": {"phase": "quality_wave_ready"},
                "suggested_tool_call": {
                    "tool": "schedule_child_subagents",
                    "children": [
                        {"role": "tester", "agent_name": "tester", "goal": "check refs"},
                    ],
                },
            },
            "records": [{"message": "y" * 3000}],
        }
    )
    output = json.dumps(
        {
            "ok": True,
            "artifact_ref": "/tmp/tool_outputs/dispatch_subagents-1.json",
            "tool": "dispatch_subagents",
            "content": dispatch_content,
            "content_chars": len(dispatch_content),
            "truncated": True,
        }
    )
    rendered = render_tool_result_for_live_prompt(
        ToolExecutionResult("read_artifact", True, output),
        {
            "output_externalized": True,
            "artifact_ref": "/tmp/tool_outputs/read_artifact-2.json",
            "output_path": "/tmp/tool_outputs/read_artifact-2.json",
            "output_hash": "def",
            "output_size_bytes": len(output),
        },
    )

    assert "artifact_read_summary" in rendered
    assert "create_quality_children_from_ready_refs" in rendered
    assert "schedule_child_subagents" in rendered
    assert "records" not in rendered
    assert "read_artifact_hint" not in rendered
    assert len(rendered) < 1600
