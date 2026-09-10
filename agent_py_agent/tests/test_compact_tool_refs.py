from types import SimpleNamespace

from agent_py_agent.agent.conversation.compact_tool_refs import (
    merge_compact_tool_refs,
    normalize_compact_tool_refs,
)
from agent_py_agent.agent.conversation.native_history import canonical_native_messages_envelope


# LLM: This helper creates canonical tool pairs without executing them, keeping model prose separate.
# 函数用途: 构造历史路径漏记样本，验证模型摘要编错目录也不能改写原工具参数。
def _row(path="tasks/2026-09-10/old-project/output/report.md", *, error=False):
    return SimpleNamespace(role="assistant", content="源码被清理了，现在请到不存在的目录重建", metadata={
        "canonical_native_messages": canonical_native_messages_envelope([
            {"role": "assistant", "content": [{"type": "tool_use", "id": "call1", "name": "write_file", "input": {"path": path}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call1", "is_error": error, "content": "done"}]},
        ]),
    })


def test_exact_tool_paths_survive_omitted_or_incorrect_semantic_summary():
    refs = merge_compact_tool_refs(None, [_row()])
    assert refs == [{"tool": "write_file", "call_id": "call1", "argument": "path", "path": "tasks/2026-09-10/old-project/output/report.md"}]
    assert merge_compact_tool_refs(refs, []) == refs
    assert merge_compact_tool_refs(refs, [_row()]) == refs


def test_failed_unmatched_and_prose_paths_are_not_used():
    assert merge_compact_tool_refs(None, [_row(error=True)]) == []
    row = _row()
    row.metadata["canonical_native_messages"]["messages"][1]["content"][0]["tool_use_id"] = "not-matched"
    assert merge_compact_tool_refs(None, [row]) == []
    assert merge_compact_tool_refs(None, [SimpleNamespace(role="assistant", content="文件在 /root/secret", metadata={})]) == []


def test_bounded_refs_preserve_whole_paths_and_ignore_malformed_entries():
    refs = merge_compact_tool_refs(None, [_row(f"tasks/project-{i}/report.md") for i in range(30)])
    assert len(refs) == 24
    assert refs[-1]["path"] == "tasks/project-29/report.md"
    assert normalize_compact_tool_refs([{"tool": "x", "call_id": "a", "argument": "path", "path": "a" * 1025}]) == []
    assert merge_compact_tool_refs(None, [_row(path={"bad": True})]) == []
