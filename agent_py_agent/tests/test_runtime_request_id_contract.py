from __future__ import annotations

"""LLM: regression tests for run-level request_id scope contracts.

给人看的解释：
这些测试确认主代理普通 run 在工具调用前就有稳定 request_id，避免大工具输出、任务事实源和
compact/resume 交接包使用不同标识。
"""

import json
from pathlib import Path

from agent_py_agent.agent.backend import ModelResponse
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent


class _LargeReadSaveBackend:
    name = "fake_large_read_save_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"read_file","path":"big.txt"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        assert "output_artifact_ref:" in prompt
        return ModelResponse(text="已读取并记录大文件线索。", backend=self.name)


def test_saved_run_generates_request_id_before_externalized_tool_outputs(tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_text("TRACE-RUN-ID\n" + ("x" * 3000), encoding="utf-8")
    agent = SimpleAgent(
        AgentConfig(enable_tools=True, memory_path="memory.jsonl", my_agent_home=str(tmp_path / "home")),
        tmp_path,
    )
    agent.backend = _LargeReadSaveBackend()

    result = agent.run("读取 big.txt 并总结。\n验收条件:\n- 需要保留 TRACE-RUN-ID 线索", save=True)

    index_path = agent.home_paths.owner_blob_tool_outputs_dir / "index.jsonl"
    index_rows = [json.loads(line) for line in index_path.read_text(encoding="utf-8").splitlines()]
    request_id = index_rows[-1]["request_id"]
    fact_path = agent.home_paths.owner_home_dir / "memory_archive" / "runtime_facts" / request_id / "task.json"
    bundle = json.loads(Path(result.main_context_bundle_path).read_text(encoding="utf-8"))

    assert request_id.startswith("run-")
    assert index_rows[-1]["tool"] == "read_file"
    assert fact_path.exists()
    assert index_path.exists()
    artifact_read = agent.tools.execute_call(
        {
            "tool": "read_artifact",
            "artifact_ref": index_rows[-1]["scoped_call_id"],
            "request_id": request_id,
            "run_id": request_id,
            "max_chars": 80,
        }
    )
    assert artifact_read.ok is True
    assert "TRACE-RUN-ID" in artifact_read.output
    assert bundle["scope"]["request_id"] == request_id
    assert result.tool_rounds == 1
