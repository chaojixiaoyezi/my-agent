from __future__ import annotations

import tempfile
from pathlib import Path

from agent_py_agent.agent.agent_core.runtime_loop_models import RunParams
from agent_py_agent.agent.backend import ModelResponse
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent


def test_cli_run_materializes_delivery_contract_before_tool_loop() -> None:
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        backend = _MaterializingDeliveryBackend()
        agent = SimpleAgent(AgentConfig(enable_tools=True, memory_path="memory.jsonl"), workspace)
        agent.backend = backend

        result = agent.run(
            "生成一个 HTML 文件放到 outputs/auto/index.html",
            params=RunParams(source="cli_run", save=False),
        )

        assert backend.calls == 3
        assert "delivery_contract.v1" in backend.prompts[0]
        assert "[tool-system delivery-contract]" in backend.prompts[1]
        assert "outputs/auto/index.html" in backend.prompts[1]
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in result.response
        assert (workspace / "outputs/auto/index.html").exists()


class _MaterializingDeliveryBackend:
    name = "fake_materializing_delivery_backend"

    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls == 1:
            return ModelResponse(
                text="""```json
{"schema_version":"delivery_requirement_materializer.v1","artifacts":[{"artifact_id":"auto_html","kind":"html","preferred_path":"outputs/auto/index.html"}]}
```""",
                backend=self.name,
            )
        if self.calls == 2:
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool":"write_file","path":"outputs/auto/index.html",'
                    '"content":"<!doctype html><html><head><title>Auto</title><style>body{font-family:sans-serif}</style></head>'
                    '<body><main><h1>Ready</h1></main></body></html>"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        if self.calls == 3:
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"submit_for_acceptance","note":"产物已写好，提交最终验收。"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        raise AssertionError("delivery contract should close out after explicit submit_for_acceptance")
