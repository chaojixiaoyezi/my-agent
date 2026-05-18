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


# LLM: _LargeReadSaveBackend reproduces a saved root run whose first tool output is externalized.
# 类用途: 测试普通 run 未显式传 request_id 时，工具输出 artifact 仍能和收尾事实源对齐。
class _LargeReadSaveBackend:
    name = "fake_large_read_save_backend"

    # LLM: __init__ tracks the two model turns in a deterministic tool loop.
    # 函数用途: 初始化调用计数，让测试后端先请求工具，再输出最终回答。
    def __init__(self):
        self.calls = 0

    # LLM: generate emits one read_file tool call, then verifies the next prompt exposes artifact refs.
    # 函数用途: 模拟读取大文件后的二轮模型行为，确认外置 artifact 引用进入 live prompt。
    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"read_file","path":"big.txt"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        assert "output_artifact_ref:" in prompt
        return ModelResponse(text="已读取并记录大文件线索。", backend=self.name)


# LLM: saved root runs must scope tool-output artifacts before finalization starts.
# 函数用途: 防止 request_id 到收尾阶段才生成，导致 compact 找不到本轮大工具输出 artifact。
def test_saved_run_generates_request_id_before_externalized_tool_outputs(tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_text("TRACE-RUN-ID\n" + ("x" * 3000), encoding="utf-8")
    agent = SimpleAgent(AgentConfig(enable_tools=True, memory_path="memory.jsonl"), tmp_path)
    agent.backend = _LargeReadSaveBackend()

    result = agent.run("读取 big.txt 并总结。\n验收条件:\n- 需要保留 TRACE-RUN-ID 线索", save=True)

    index_path = tmp_path / "memory_archive" / "artifacts" / "tool_outputs" / "index.jsonl"
    index_rows = [json.loads(line) for line in index_path.read_text(encoding="utf-8").splitlines()]
    request_id = index_rows[-1]["request_id"]
    fact_path = tmp_path / "memory_archive" / "runtime_facts" / request_id / "task.json"
    bundle = json.loads(Path(result.main_context_bundle_path).read_text(encoding="utf-8"))

    assert request_id.startswith("run-")
    assert index_rows[-1]["tool"] == "read_file"
    assert fact_path.exists()
    assert bundle["scope"]["request_id"] == request_id
    assert result.tool_rounds == 1
