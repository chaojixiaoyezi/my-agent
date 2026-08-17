from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model.context_pressure import (
    preflight_context_pressure_response,
)
from agent_py_agent.agent.agent_core.runtime.owner_roots import runtime_scope_root
from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.context_bundle_refs import runtime_task_attributes
from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot


class _TwoOverflowChildBackend:
    name = "two-overflow-child"
    context_window_tokens = 60_000

    def probe_tool_capability(self):
        from agent_py_agent.agent.backends.base import ProviderToolCapability
        from agent_py_agent.agent.backends.base import _utc_now_iso

        return ProviderToolCapability(
            provider=self.name, endpoint="local://two-overflow", model="",
            stream=False, native_supported=True,
            evidence="test_backend_declares_native_tools",
            observed_at=_utc_now_iso(),
        )

    def __init__(self, output_path: Path):
        self.output_path = output_path
        self.prompts: list[str] = []

    def generate(self, prompt: str, on_chunk=None, **kwargs):
        self.prompts.append(prompt)
        turn = len(self.prompts)
        if turn in {1, 3}:
            return ModelResponse(
                text=f"context overflow {turn}",
                backend=self.name,
                runtime_status="context_overflow",
                runtime_reason="context_overflow",
                runtime_source="provider_error",
                usage={"input_tokens": 19_500, "output_tokens": 10},
            )
        if turn == 2:
            # EXEC-31b: native 下文本 [TOOL_CALL] 是伪调用(零执行), 改结构化块。
            return ModelResponse(
                text="",
                backend=self.name,
                tool_use_blocks=[{
                    "id": "call-compact-write-2",
                    "name": "write_file",
                    "input": {
                        "path": str(self.output_path),
                        "content": "child progress",
                    },
                }],
                usage={"input_tokens": 500, "output_tokens": 100},
            )
        return ModelResponse(
            text="child completed after repeated Compact",
            backend=self.name,
            usage={"input_tokens": 500, "output_tokens": 100},
        )


def test_subagent_reuses_generic_compact_in_own_home_without_owner_memory_pollution(
    tmp_path: Path,
) -> None:
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            enable_tools=True,
            my_agent_home=str(tmp_path / "home"),
            tool_context_ptl_retry_max=0,
        ),
        tmp_path,
    )
    task = agent.subagents.create_run(
        goal="执行需要两次上下文压缩的子任务",
        thought="复用通用 Compact。",
        plan=["写进展", "继续完成"],
        role="worker",
    )
    run_home = Path(task.agent_run_workspace_dir)
    output_path = run_home / "output" / "progress.txt"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    backend = _TwoOverflowChildBackend(output_path)
    agent.backend = backend
    owner_memory_before = _file_bytes(agent.memory.path)

    result = agent.run(
        "完成这个子任务，并在上下文溢出后从当前进度继续。",
        save=True,
        allowed_tools=["write_file"],
        write_boundary={
            "allowed_write_roots": [str(run_home)],
            "forbidden_write_roots": [],
        },
        run_id=task.id,
        task_id=task.root_id or task.id,
        task_attributes=runtime_task_attributes(task),
        context_scope="task_local",
        source="subagent_run_model_turn",
    )

    apply_dir = run_home / "memory_archive" / "runs" / task.id / "compact_applies"
    metadata = [
        path
        for path in apply_dir.glob("apply-*.json")
        if not any(
            marker in path.name
            for marker in (
                ".apply_bundle.",
                ".restore_refs.",
                ".work_state_snapshot.",
                ".self_check",
                ".compaction_state.",
            )
        )
    ]
    assert len(backend.prompts) == 4
    assert len(metadata) == 2
    assert result.memory_compact_auto_continuation_depth == 2
    assert result.executed_tools == ["write_file"]
    assert output_path.read_text(encoding="utf-8") == "child progress"
    assert _file_bytes(agent.memory.path) == owner_memory_before
    assert not (run_home / "compactions").exists()
    assert not (run_home / "recovery").exists()


def test_task_local_preflight_uses_the_configured_exact_compact_threshold(
    monkeypatch,
    tmp_path: Path,
) -> None:
    run_home = tmp_path / "task" / "work" / "agents" / "run-1"
    run_home.mkdir(parents=True)
    agent = SimpleNamespace(
        config=AgentConfig(
            auto_save_memory=True,
            memory_compact_auto_trigger_percent=90,
            model_context_window_tokens=1_000,
        ),
        backend=SimpleNamespace(context_window_tokens=1_000, name="fake"),
    )
    params = SimpleNamespace(
        context_scope="task_local",
        task_attributes={"agent_run_workspace_dir": str(run_home)},
        live_archive_state={},
        tool_protocol_snapshot=make_test_protocol_snapshot(),
    )
    request = SimpleNamespace(agent=agent, params=params, prompt="child prompt", tool_rounds=0)

    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.model.context_pressure.estimate_tokens",
        lambda _prompt: 899,
    )
    assert preflight_context_pressure_response(request) is None

    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.model.context_pressure.estimate_tokens",
        lambda _prompt: 900,
    )
    response = preflight_context_pressure_response(request)

    assert response is not None
    assert response.runtime_status == "context_overflow"
    assert "compact_threshold=900" in response.text


def test_task_local_storage_fails_closed_without_a_child_run_home(tmp_path: Path) -> None:
    agent = SimpleNamespace(root=tmp_path)

    with pytest.raises(RuntimeError, match="refusing to fall back to owner storage"):
        runtime_scope_root(
            agent,
            context_scope="task_local",
            task_attributes={},
        )


def _file_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""
