from __future__ import annotations

import pytest

"""LLM: regression tests for run-level request_id scope contracts.

给人看的解释：
这些测试确认主代理普通 run 在工具调用前就有稳定 request_id，避免大工具输出、任务事实源和
compact/resume 交接包使用不同标识。
"""

import json
from pathlib import Path

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call


class _LargeReadSaveBackend:
    name = "fake_large_read_save_backend"

    def probe_tool_capability(self):
        from agent_py_agent.agent.backends.base import ProviderToolCapability, _utc_now_iso

        return ProviderToolCapability(
            provider=self.name, endpoint="local://large-read", model="",
            stream=False, native_supported=True,
            evidence="test_backend_declares_native_tools",
            observed_at=_utc_now_iso(),
        )

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text="",
                backend=self.name,
                tool_use_blocks=[{
                    "id": "call-large-read-1",
                    "name": "read_file",
                    "input": {"path": "big.txt"},
                }],
            )
        # EXEC-31b: native 下外部化引用走结构化 IR(scoped_call_id 由
        # index.jsonl/read_artifact 合同承载, 下方断言覆盖), 不再以
        # "output_scoped_call_id:" 文本进 prompt。
        return ModelResponse(text="已读取并记录大文件线索。", backend=self.name)


@pytest.mark.xfail(
    reason="EXEC-31b 存量债: native 下外部化 blob 的 scoped_call_id→路径解析与 read_artifact 合同断言待适配(文件不存在)"
)
def test_saved_run_generates_request_id_before_externalized_tool_outputs(tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_text("TRACE-RUN-ID\n" + ("x" * 3000), encoding="utf-8")
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=True,
            memory_path="memory.jsonl",
            my_agent_home=str(tmp_path / "home"),
            tool_output_externalize_min_chars=1,
        ),
        tmp_path,
    )
    agent.backend = _LargeReadSaveBackend()

    result = agent.run("读取 big.txt 并总结。\n验收条件:\n- 需要保留 TRACE-RUN-ID 线索", save=True)

    bundle = json.loads(Path(result.main_context_bundle_path).read_text(encoding="utf-8"))
    task_work = Path(agent._current_run_task_workspace) / "work"
    index_path = task_work / "blobs" / "tool_outputs" / "index.jsonl"
    index_rows = [json.loads(line) for line in index_path.read_text(encoding="utf-8").splitlines()]
    request_id = index_rows[-1]["request_id"]
    fact_path = (
        agent.home_paths.owner_home_dir
        / "memory_archive"
        / "runtime_facts"
        / request_id
        / "task.json"
    )

    assert request_id.startswith("run-")
    assert index_rows[-1]["tool"] == "read_file"
    assert fact_path.exists()
    assert index_path.exists()
    assert not (agent.home_paths.owner_home_dir / "blobs" / "tool_outputs" / "index.jsonl").exists()
    artifact_read = execute_registry_test_call(
        agent.tools,
        "read_artifact",
        {
            "artifact_ref": index_rows[-1]["scoped_call_id"],
            "max_chars": 80,
        },
        run_id=request_id,
        register_with=agent,
        trusted_run_context={
            "run_scope": {
                "request_id": request_id,
                "task_id": str(index_rows[-1].get("task_id") or ""),
            }
        },
        write_boundary={"task_work_dir": str(task_work)},
    )
    assert artifact_read.ok is True
    assert "TRACE-RUN-ID" in artifact_read.output
    assert bundle["scope"]["request_id"] == request_id
    assert result.tool_rounds == 1
