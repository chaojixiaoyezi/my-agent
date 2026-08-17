from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

from agent_py_agent.agent.agent_core.orchestration.dispatch.params import DispatchParams
from agent_py_agent.agent.backends import BaseBackend, ModelResponse
from .backends import _TestNativeBackend
from agent_py_agent.agent.capability import CapabilityRouter
from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.manager_runner_result_payload import RecordRunnerResultParams


def _accepted_result(summary: str) -> ModelResponse:
    return ModelResponse(
        text=(
            "[SUBAGENT_RESULT]\n"
            "{\n"
            '  "status": "DONE",\n'
            f'  "summary": "{summary}",\n'
            '  "used_tools": [],\n'
            '  "used_skills": [],\n'
            '  "evidence": [{"kind": "note", "summary": "worker pool 证据", "ok": true}],\n'
            '  "evidence_packets": [{"id": "evpkt-worker-pool", "claim": "worker pool 任务已完成", "checked_scope": "worker pool runner", "evidence_refs": ["runner_result.json"], "artifact_refs": ["output.json"], "confidence": 0.9}],\n'
            '  "capability_requests": [],\n'
            '  "artifacts": [],\n'
            '  "tests": [{"name": "worker-pool", "command": "", "ok": true, "summary": "通过"}],\n'
            '  "patches": [],\n'
            '  "lessons": [],\n'
            '  "next_actions": [],\n'
            '  "blocked_reason": "",\n'
            '  "failure_type": ""\n'
            "}\n"
            "[/SUBAGENT_RESULT]"
        ),
        backend="worker-pool-test",
    )


class CountingAcceptedBackend(_TestNativeBackend):
    def __init__(self):
        self.lock = threading.Lock()
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        assert "[SUBAGENT_RESULT]" in prompt
        with self.lock:
            self.calls += 1
            call_no = self.calls
        time.sleep(0.03)
        return _accepted_result(f"worker call {call_no}")


class OneSlowOneFastBackend(_TestNativeBackend):
    def __init__(self):
        self.lock = threading.Lock()
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        assert "[SUBAGENT_RESULT]" in prompt
        with self.lock:
            self.calls += 1
            call_no = self.calls
        if call_no == 1:
            time.sleep(6.0)
            return _accepted_result("slow worker eventually finished")
        return _accepted_result("fast worker finished")


def test_dispatch_parallel_runner_pool_respects_start_rate(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        backend = CountingAcceptedBackend()
        monkeypatch.setattr("agent_py_agent.agent.core.get_backend", lambda _name, _config: backend)
        cfg = AgentConfig(
            model_backend="worker-pool-test",
            subagent_workspace="subs",
            runner_concurrency="3",
            runner_start_rate="2",
        )
        agent = SimpleAgent(cfg, root)
        tasks = [
            agent.subagents.create_run(
                goal=f"并发任务 {index}", thought="等待 worker pool。", plan=["执行", "验收"]
            )
            for index in range(3)
        ]

        router = CapabilityRouter(config=CapabilityConfig(), tool_specs=agent.tools.specs())
        report = agent.dispatch_subagents(
            router,
            CapabilityConfig(),
            apply=True,
            start_runners=True,
            max_runners=3,
            probe=False,
            reviewer="worker-pool-test",
        )

        loaded = [agent.subagents.load(task.id) for task in tasks]
        done = [item for item in loaded if item.status == "DONE"]
        planning = [item for item in loaded if item.status == "PLANNING"]

        assert backend.calls == 2
        assert len([item for item in report.records if item.step == "runner"]) == 2
        assert len(done) == 2
        assert len(planning) == 1


def test_dispatch_parallel_runner_pool_does_not_broadcast_specific_instruction(
    monkeypatch, tmp_path
):
    captured: list[tuple[str, str]] = []
    cfg = AgentConfig(
        model_backend="worker-pool-test",
        subagent_workspace="subs",
        runner_concurrency="2",
        runner_start_rate="2",
        runner_timeout_seconds="3.0",
    )
    monkeypatch.setattr(
        "agent_py_agent.agent.core.get_backend", lambda _name, _config: CountingAcceptedBackend()
    )
    agent = SimpleAgent(cfg, tmp_path)
    tasks = [
        agent.subagents.create_run(
            goal="auth coordinator task", thought="等待 worker。", plan=["执行"]
        ),
        agent.subagents.create_run(
            goal="catalog coordinator task", thought="等待 worker。", plan=["执行"]
        ),
    ]

    def fake_worker(params):
        captured.append((params.run_id, params.instruction))
        return agent.subagents.runner_result.record_runner_result(
            RecordRunnerResultParams(
                run_id=params.run_id,
                dry_run=False,
                ok=True,
                message="done",
                status="DONE",
                verification_status="VERIFIED",
            )
        )

    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.runner.dispatch._run_subagent_worker", fake_worker
    )

    router = CapabilityRouter(config=CapabilityConfig(), tool_specs=agent.tools.specs())
    report = agent.dispatch_subagents(
        router,
        CapabilityConfig(),
        params=None,
        apply=True,
        start_runners=True,
        max_runners=2,
        probe=False,
        reviewer="worker-pool-test",
        runner_instruction="你是 auth-coordinator，只能写 auth 页面。",
    )

    assert {run_id for run_id, _ in captured} == {task.id for task in tasks}
    assert [instruction for _, instruction in captured] == ["", ""]
    assert any(record.action == "ignore_multi_runner_instruction" for record in report.records)


def test_dispatch_single_runner_keeps_specific_instruction(monkeypatch, tmp_path):
    captured: list[str] = []
    cfg = AgentConfig(
        model_backend="worker-pool-test",
        subagent_workspace="subs",
        runner_concurrency="2",
        runner_start_rate="2",
        runner_timeout_seconds="3.0",
    )
    monkeypatch.setattr(
        "agent_py_agent.agent.core.get_backend", lambda _name, _config: CountingAcceptedBackend()
    )
    agent = SimpleAgent(cfg, tmp_path)
    task = agent.subagents.create_run(
        goal="auth coordinator task", thought="等待 worker。", plan=["执行"]
    )

    def fake_worker(params):
        captured.append(params.instruction)
        return agent.subagents.runner_result.record_runner_result(
            RecordRunnerResultParams(
                run_id=params.run_id,
                dry_run=False,
                ok=True,
                message="done",
                status="DONE",
                verification_status="VERIFIED",
            )
        )

    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.runner.dispatch._run_subagent_worker", fake_worker
    )

    router = CapabilityRouter(config=CapabilityConfig(), tool_specs=agent.tools.specs())
    agent.dispatch_subagents(
        router,
        CapabilityConfig(),
        params=DispatchParams(
            apply=True,
            start_runners=True,
            include_run_ids=[task.id],
            max_runners=1,
            probe=False,
            reviewer="worker-pool-test",
            runner_instruction="你是 auth-coordinator，只能写 auth 页面。",
        ),
    )

    assert captured == ["你是 auth-coordinator，只能写 auth 页面。"]


def test_dispatch_worker_reuses_parent_runtime_context_config(monkeypatch, tmp_path):
    """子代理 runner 默认复用主代理上下文和 compact 配置，不另开一套常规参数。"""

    captured: list[tuple[int, int, str]] = []
    cfg = AgentConfig(
        model_backend="worker-pool-test",
        subagent_workspace="subs",
        runner_concurrency="1",
        runner_start_rate="1",
        runner_timeout_seconds="off",
        model_context_window_tokens=200_000,
        memory_compact_auto_trigger_percent=70,
    )
    monkeypatch.setattr(
        "agent_py_agent.agent.core.get_backend", lambda _name, _config: CountingAcceptedBackend()
    )
    agent = SimpleAgent(cfg, tmp_path)
    task = agent.subagents.create_run(
        goal="读取大项目并写报告", thought="等待 worker。", plan=["执行"]
    )

    def fake_worker(params):
        captured.append(
            (
                params.config.model_context_window_tokens,
                params.config.memory_compact_auto_trigger_percent,
                params.config.runner_timeout_seconds,
            )
        )
        return agent.subagents.runner_result.record_runner_result(
            RecordRunnerResultParams(
                run_id=params.run_id,
                dry_run=False,
                ok=True,
                message="done",
                status="DONE",
                verification_status="VERIFIED",
            )
        )

    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.runner.dispatch._run_subagent_worker", fake_worker
    )

    router = CapabilityRouter(config=CapabilityConfig(), tool_specs=agent.tools.specs())
    agent.dispatch_subagents(
        router,
        CapabilityConfig(),
        params=DispatchParams(
            apply=True,
            start_runners=True,
            include_run_ids=[task.id],
            max_runners=1,
            probe=False,
        ),
    )

    assert captured == [(200_000, 70, "off")]


def _parent_child_pair(agent):
    parent = agent.subagents.create_run(
        goal="cart coordinator",
        thought="create cart worker",
        plan=["dispatch child"],
        role="coordinator",
    )
    child = agent.subagents.create_run(
        goal="cart worker",
        thought="write cart",
        plan=["work"],
        parent_id=parent.id,
        root_id=parent.id,
        depth=1,
    )
    return parent, child


def _capture_runner_ids(monkeypatch, agent, captured: list[str]) -> None:
    def fake_worker(params):
        captured.append(params.run_id)
        return agent.subagents.runner_result.record_runner_result(
            RecordRunnerResultParams(
                run_id=params.run_id,
                dry_run=False,
                ok=True,
                message="should not run",
                status="DONE",
                verification_status="VERIFIED",
            )
        )

    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.runner.dispatch._run_subagent_worker", fake_worker
    )


def test_dispatch_blocks_invalid_scoped_run_id_with_valid_child_hint(monkeypatch, tmp_path):
    captured: list[str] = []
    cfg = AgentConfig(
        model_backend="worker-pool-test",
        subagent_workspace="subs",
        runner_concurrency="1",
        runner_start_rate="1",
    )
    monkeypatch.setattr(
        "agent_py_agent.agent.core.get_backend", lambda _name, _config: CountingAcceptedBackend()
    )
    agent = SimpleAgent(cfg, tmp_path)
    parent, child = _parent_child_pair(agent)
    wrong_id = f"subagent-0000000000-{child.id.rsplit('-', 1)[-1]}"
    _capture_runner_ids(monkeypatch, agent, captured)

    router = CapabilityRouter(config=CapabilityConfig(), tool_specs=agent.tools.specs())
    report = agent.dispatch_subagents(
        router,
        CapabilityConfig(),
        params=DispatchParams(
            apply=True,
            start_runners=True,
            parent_run_id=parent.id,
            include_run_ids=[wrong_id],
            max_runners=1,
            probe=False,
        ),
    )

    selection_records = [
        record
        for record in report.records
        if record.step == "runner_selection" and record.action == "invalid_run_ids"
    ]

    assert captured == []
    assert len(selection_records) == 1
    assert selection_records[0].ok is False
    assert wrong_id in selection_records[0].message
    assert child.id in selection_records[0].message
    assert agent.subagents.load(child.id).status == "PLANNING"


def test_dispatch_parallel_runner_pool_timeout_does_not_block_other_workers(monkeypatch, tmp_path):
    root = tmp_path
    backend = OneSlowOneFastBackend()
    monkeypatch.setattr("agent_py_agent.agent.core.get_backend", lambda _name, _config: backend)
    cfg = AgentConfig(
        model_backend="worker-pool-test",
        subagent_workspace="subs",
        runner_concurrency="2",
        runner_start_rate="2",
        runner_timeout_seconds="3.0",
    )
    agent = SimpleAgent(cfg, root)
    tasks = [
        agent.subagents.create_run(
            goal=f"超时隔离任务 {index}", thought="等待 worker pool。", plan=["执行", "验收"]
        )
        for index in range(2)
    ]

    router = CapabilityRouter(config=CapabilityConfig(), tool_specs=agent.tools.specs())
    report = agent.dispatch_subagents(
        router,
        CapabilityConfig(),
        apply=True,
        start_runners=True,
        max_runners=2,
        probe=False,
        reviewer="worker-pool-timeout-test",
    )

    loaded = [agent.subagents.load(task.id) for task in tasks]
    statuses = sorted(item.status for item in loaded)
    runner_records = [item for item in report.records if item.step == "runner"]
    timeout_task = next(item for item in loaded if item.status == "TIMEOUT")

    assert len(runner_records) == 2
    assert any(item.status == "TIMEOUT" for item in loaded)
    assert any(item.status == "DONE" for item in loaded)
    assert statuses == ["DONE", "TIMEOUT"]
    assert Path(timeout_task.runner_prompt_file).exists()
    assert any(not item.ok and "timed out" in item.message for item in runner_records)
    assert any(item.ok for item in runner_records)
