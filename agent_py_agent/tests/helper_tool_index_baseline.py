"""真正基线9e843077d的整读工具索引口径，仅用于旧路径内存测量。"""
from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.common.json_io import jsonl_lines
from agent_py_agent.agent.common.tool_output_paths import tool_output_index_paths_for_lookup
from agent_py_agent.agent.memory_archive import control_plane


def legacy_tool_output_query(root: Path, options) -> dict:
    rows = []
    for path in tool_output_index_paths_for_lookup(root):
        if path.exists():
            rows.extend(control_plane._decode_line(path, number, text)
                        for number, text in enumerate(jsonl_lines(path.read_text(encoding="utf-8")), 1)
                        if text.strip())
    return {"tool_outputs": control_plane._limited(control_plane._matching_records(rows, options), options.limit)}
