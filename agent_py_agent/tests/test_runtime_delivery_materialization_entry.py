from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.backends.errors import ProviderTransientError
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


def test_cli_run_uses_structural_output_contract_without_llm_materializer() -> None:
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        backend = _MaterializingDeliveryBackend()
        agent = SimpleAgent(AgentConfig(enable_tools=True, memory_path="memory.jsonl"), workspace)
        agent.backend = backend

        result = agent.run(
            "生成一个 HTML 文件放到 outputs/auto/index.html",
            params=RunParams(source="cli_run", save=False),
        )

        assert backend.calls == 1
        assert backend.materializer_calls == 0
        assert "[tool-system delivery-contract]" in backend.prompts[0]
        assert "outputs/auto/index.html" in backend.prompts[0]
        assert result.response == "工具循环已启动，未先物化 delivery_contract。"


def test_cli_run_no_longer_retries_auto_materializer_provider_transient(monkeypatch) -> None:
    from agent_py_agent.agent.agent_core import provider_transient_auto_resume

    sleeps: list[float] = []
    monkeypatch.setattr(provider_transient_auto_resume.time, "sleep", sleeps.append)
    monkeypatch.setattr(
        provider_transient_auto_resume,
        "provider_transient_retry_delays",
        lambda _policy=None: (10.0, 25.0),
    )

    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        backend = _MaterializingDeliveryBackend(transient_materializer_failures=1)
        agent = SimpleAgent(AgentConfig(enable_tools=True, memory_path="memory.jsonl"), workspace)
        agent.backend = backend
        chunks: list[str] = []

        result = agent.run(
            "生成一个 HTML 文件放到 outputs/auto/index.html",
            params=RunParams(source="cli_run", save=False, on_chunk=chunks.append),
        )

        assert result.response == "工具循环已启动，未先物化 delivery_contract。"
        assert sleeps == []
        assert "自动重试" not in "".join(chunks)
        assert backend.materializer_calls == 0


def test_runtime_materialization_entry_adds_explicit_output_contract_without_source_coverage() -> None:
    from agent_py_agent.agent.agent_core.runtime.run_params import (
        run_params_with_materialized_delivery_contract,
    )

    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        backend = _RepairingCoverageMaterializerBackend()
        agent = SimpleNamespace(backend=backend, root=workspace, runtime_guard_policy=None)
        prompt = (
            "请完整读完 data/long_field_journal.txt。\n"
            "最终把报告写到 lab_outputs/compact-stress/report.md。"
        )

        params = run_params_with_materialized_delivery_contract(
            agent,
            prompt,
            RunParams(source="cli_run", save=False),
        )

        assert backend.materializer_calls == 0
        assert backend.prompts == []
        assert params.delivery_contract == {
            "schema_version": "delivery_contract.v1",
            "artifacts": [
                {
                    "artifact_id": "user_requested_report_md",
                    "preferred_path": "lab_outputs/compact-stress/report.md",
                    "allowed_output_roots": ["lab_outputs/compact-stress"],
                    "required": True,
                    "kind": "md",
                }
            ],
        }


def test_runtime_materialization_entry_does_not_repair_structural_output_contracts() -> None:
    from agent_py_agent.agent.agent_core.runtime.run_params import (
        run_params_with_materialized_delivery_contract,
    )

    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        backend = _SlowRepairingCoverageMaterializerBackend()
        agent = SimpleNamespace(backend=backend, root=workspace, runtime_guard_policy=None)
        prompt = (
            "请完整读完 data/long_field_journal.txt。\n"
            "最终把报告写到 lab_outputs/compact-stress/report.md。"
        )

        params = run_params_with_materialized_delivery_contract(
            agent,
            prompt,
            RunParams(source="cli_run", save=False),
        )

        assert backend.materializer_calls == 0
        assert backend.prompts == []
        assert params.delivery_contract["artifacts"][0]["preferred_path"] == "lab_outputs/compact-stress/report.md"


class _MaterializingDeliveryBackend:
    name = "fake_materializing_delivery_backend"

    def __init__(self, *, transient_materializer_failures: int = 0) -> None:
        self.calls = 0
        self.materializer_calls = 0
        self.transient_materializer_failures = transient_materializer_failures
        self.prompts: list[str] = []

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        self.prompts.append(prompt)
        if prompt.startswith("请把下面的用户需求转换成一个最小 delivery_contract.v1 JSON 对象。"):
            self.materializer_calls += 1
            if self.materializer_calls <= self.transient_materializer_failures:
                raise ProviderTransientError("HTTP 429: plan limited")
            return ModelResponse(
                text="""```json
{"schema_version":"delivery_requirement_materializer.v1","artifacts":[{"artifact_id":"auto_html","kind":"html","preferred_path":"outputs/auto/index.html"}]}
```""",
                backend=self.name,
            )
        return ModelResponse(text="工具循环已启动，未先物化 delivery_contract。", backend=self.name)


class _RepairingCoverageMaterializerBackend:
    name = "fake_repairing_coverage_materializer_backend"

    def __init__(self) -> None:
        self.materializer_calls = 0
        self.prompts: list[str] = []

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.prompts.append(prompt)
        self.materializer_calls += 1
        if self.materializer_calls == 1:
            return ModelResponse(
                text="""```json
{"schema_version":"delivery_requirement_materializer.v1","artifacts":[{"artifact_id":"report","kind":"md","preferred_path":"lab_outputs/compact-stress/report.md"}]}
```""",
                backend=self.name,
            )
        return ModelResponse(
            text="""```json
{"schema_version":"delivery_requirement_materializer.v1","artifacts":[{"artifact_id":"report","kind":"md","preferred_path":"lab_outputs/compact-stress/report.md"}],"target_coverage_contract":{"scope_label":"source file","coverage_requirement":"full_source_read","enforcement":"required","target_items":[{"target_id":"data/long_field_journal.txt","source_path":"data/long_field_journal.txt","coverage_kind":"full_source_read"}]}}
```""",
            backend=self.name,
        )


class _SlowRepairingCoverageMaterializerBackend(_RepairingCoverageMaterializerBackend):
    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        if self.materializer_calls < 2:
            self.prompts.append(prompt)
            self.materializer_calls += 1
            return ModelResponse(
                text="""```json
{"schema_version":"delivery_requirement_materializer.v1","artifacts":[{"artifact_id":"report","kind":"md","preferred_path":"lab_outputs/compact-stress/report.md"}]}
```""",
                backend=self.name,
            )
        return super().generate(prompt, on_chunk=on_chunk)
