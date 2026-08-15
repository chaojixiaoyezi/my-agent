from __future__ import annotations

import tempfile
from pathlib import Path

# 防回归：工具大检查(对照 study-agent 标杆)发现"失败路径系统性漏 error_code"——跨 10+ 工具,
# 是 read_artifact UNKNOWN_ERROR 的同族隐患。补全后参数/IO 失败应返回精确 error_code,不再
# fallback 成 UNKNOWN_ERROR(retryable=False)误导模型放弃→草草完成。


def _tmp_text_file(content: str = "a\nb\nc\n") -> tuple[Path, str]:
    d = Path(tempfile.mkdtemp())
    (d / "x.txt").write_text(content, encoding="utf-8")
    return d, "x.txt"


def test_web_search_param_failure_has_precise_code():
    from agent_py_agent.agent.tooling.web_search import WebSearchTool

    r = WebSearchTool(max_results=5, timeout=10).execute({"query": ""})
    assert r.ok is False
    assert r.error_code == "TOOL_INVALID_ARGUMENTS"


def test_read_file_invalid_param_has_precise_code():
    from agent_py_agent.agent.tooling._filesystem_read import ReadFileTool

    d, name = _tmp_text_file()
    r = ReadFileTool(d, 100000).execute({"path": name, "start_line": "notanumber"})
    assert r.ok is False
    assert r.error_code == "TOOL_INVALID_ARGUMENTS"


def test_read_file_line_range_failure_not_unknown():
    from agent_py_agent.agent.tooling._filesystem_read import ReadFileTool

    d, name = _tmp_text_file()
    r = ReadFileTool(d, 100000).execute({"path": name, "start_line": 999, "end_line": 1000})
    assert r.ok is False
    assert r.error_code != "UNKNOWN_ERROR"


def test_edit_file_carries_replacement_evidence():
    from agent_py_agent.agent.tooling._filesystem_edit import EditFileTool

    d, name = _tmp_text_file("timeout = 30\n")
    r = EditFileTool(d).execute({"path": name, "old_string": "30", "new_string": "60"})
    assert r.ok is True
    # 大检查:成功返回带可验证证据(对标 长期助手 的 result 校验)
    assert r.result_envelope.get("replacement_count") == 1
