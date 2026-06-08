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


def test_runtime_materialization_repairs_explicit_output_contract_with_source_coverage() -> None:
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

        assert backend.materializer_calls == 2
        assert "source_paths" in backend.prompts[1]
        assert params.delivery_contract["artifacts"][0]["preferred_path"] == "lab_outputs/compact-stress/report.md"
        coverage = params.delivery_contract["target_coverage_contract"]
        assert coverage["enforcement"] == "required"
        assert coverage["target_items"][0]["source_path"] == "data/long_field_journal.txt"


def test_runtime_materialization_repairs_structural_output_contracts_until_source_modeled() -> None:
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

        assert backend.materializer_calls == 3
        assert params.delivery_contract["artifacts"][0]["preferred_path"] == "lab_outputs/compact-stress/report.md"
        assert params.delivery_contract["target_coverage_contract"]["target_items"][0]["source_path"] == (
            "data/long_field_journal.txt"
        )


def test_runtime_materialization_uses_structural_directory_coverage_without_output_path() -> None:
    from agent_py_agent.agent.agent_core.runtime.run_params import (
        run_params_with_materialized_delivery_contract,
    )

    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        root = workspace / "all-agent"
        ecc = root / "ECC-main"
        pi = root / "pi-main"
        ecc.mkdir(parents=True)
        pi.mkdir()
        (ecc / "README.md").write_text("ecc", encoding="utf-8")
        (pi / "package.json").write_text("{}", encoding="utf-8")
        backend = _MaterializingDeliveryBackend()
        agent = SimpleNamespace(backend=backend, root=workspace, runtime_guard_policy=None)

        params = run_params_with_materialized_delivery_contract(
            agent,
            f"请读 {root} 下面的项目，最后生成中文报告。",
            RunParams(source="cli_run", save=False),
        )

        assert backend.materializer_calls == 0
        assert params.delivery_contract["artifacts"] == [
            {
                "artifact_id": "final_report",
                "kind": "md",
                "allowed_output_roots": ["output"],
                "required": True,
            }
        ]
        assert [item["target_id"] for item in params.delivery_contract["target_coverage_contract"]["target_items"]] == [
            "ECC-main",
            "pi-main",
        ]


def test_runtime_materialization_skips_task_local_internal_prompt_paths() -> None:
    from agent_py_agent.agent.agent_core.runtime.run_params import (
        run_params_with_materialized_delivery_contract,
    )

    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        backend = _SlowRepairingCoverageMaterializerBackend()
        agent = SimpleNamespace(backend=backend, root=workspace, runtime_guard_policy=None)
        prompt = (
            "内部执行上下文示例：execution_context.output、output.json、runner_result.json；"
            "真正任务是读取 input.txt 并写 output/summary.md。"
        )

        params = run_params_with_materialized_delivery_contract(
            agent,
            prompt,
            RunParams(source="subagent_run", save=True, context_scope="task_local"),
        )

        assert backend.materializer_calls == 0
        assert backend.prompts == []
        assert params.delivery_contract is None


def test_runtime_materialization_skips_control_plane_internal_prompt_paths() -> None:
    from agent_py_agent.agent.agent_core.runtime.run_params import (
        run_params_with_materialized_delivery_contract,
    )

    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        backend = _SlowRepairingCoverageMaterializerBackend()
        agent = SimpleNamespace(backend=backend, root=workspace, runtime_guard_policy=None)

        params = run_params_with_materialized_delivery_contract(
            agent,
            "父级 planner 内部模板里可能出现 output.json，但这不是用户交付物。",
            RunParams(source="planner", save=True, context_scope="control_plane"),
        )

        assert backend.materializer_calls == 0
        assert backend.prompts == []
        assert params.delivery_contract is None


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
