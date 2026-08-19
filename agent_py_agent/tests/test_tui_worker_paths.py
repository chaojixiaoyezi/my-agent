from __future__ import annotations

import subprocess
import sys
import threading
from types import SimpleNamespace

from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.cli.chat_parts.tui_worker import (
    _build_turn_inject,
    _tui_prepare_gateway_job,
    _tui_update_running_state,
    _worker_history_context,
)
from agent_py_agent.cli.chat_parts.tui_worker_paths import _gateway_outcome


def test_gateway_tui_worker_import_does_not_load_full_runtime() -> None:
    script = (
        "import sys; import agent_py_agent.cli.chat_parts.tui_worker; "
        "forbidden = {"
        "'agent_py_agent.agent.core', "
        "'agent_py_agent.cli.common', "
        "'agent_py_agent.agent.gateway_parts.request_worker'"
        "}; loaded = forbidden.intersection(sys.modules); "
        "assert not loaded, sorted(loaded)"
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_gateway_running_state_never_exposes_local_job_id_as_exact_turn() -> None:
    cfg = SimpleNamespace(
        state_lock=threading.Lock(),
        pending_jobs_ref=[1],
        is_running_ref=[False],
        running_prompt_ref=[""],
        running_request_id_ref=["old"],
        running_started_at_ref=[0.0],
        use_gateway=True,
    )

    _tui_update_running_state(
        cfg,
        SimpleNamespace(user="first", request_id="chat-local", gateway_request_id=""),
    )

    assert cfg.running_request_id_ref == [""]
    assert cfg.is_running_ref == [True]
    assert cfg.pending_jobs_ref == [0]

    cfg.pending_jobs_ref[0] = 1
    _tui_update_running_state(
        cfg,
        SimpleNamespace(
            user="queued",
            request_id="gwreq-existing",
            gateway_request_id="gwreq-existing",
        ),
    )
    assert cfg.running_request_id_ref == ["gwreq-existing"]


def test_gateway_job_is_durably_submitted_before_running_snapshot(monkeypatch) -> None:
    calls: list[tuple[str, list[str]]] = []
    cfg = SimpleNamespace(
        use_gateway=True,
        build_history_context=lambda: "must not be injected by thin client",
    )
    job = SimpleNamespace(
        request_id="chat-local",
        gateway_request_id="",
        inject=["runtime rule"],
        inject_complete=False,
    )

    def submit(_cfg, submitted_job, turn_inject):
        calls.append((submitted_job.request_id, list(turn_inject)))
        submitted_job.gateway_request_id = "gwreq-canonical"

    monkeypatch.setattr(
        "agent_py_agent.cli.chat_parts.tui_worker_paths._submit_new_gateway_job",
        submit,
    )

    _tui_prepare_gateway_job(cfg, job)

    assert job.gateway_request_id == "gwreq-canonical"
    assert calls[0][0] == "chat-local"
    assert calls[0][1][0] == "runtime rule"


def test_complete_queued_inject_does_not_append_chat_style_twice() -> None:
    from agent_py_agent.cli.chat_parts.chat_style import CHAT_RESPONSE_STYLE_INJECT

    completed = ["runtime rule", CHAT_RESPONSE_STYLE_INJECT]

    assert _build_turn_inject(
        completed,
        "",
        inject_complete=True,
    ) == completed


def _context(*, show_prompt: bool = False):
    runtime = TuiRuntime("session-worker-path")
    cfg = SimpleNamespace(
        state_lock=threading.Lock(),
        last_token_estimate_ref=[0],
        tui_runtime=runtime,
    )
    return SimpleNamespace(cfg=cfg, job=SimpleNamespace(show_prompt=show_prompt)), runtime


def test_gateway_outcome_returns_one_structured_final_without_printing() -> None:
    ctx, runtime = _context()
    response = {
        "ok": True,
        "response": "streamed final",
        "current_context_token_estimate": 42,
        "tool_rounds": 2,
    }

    text, recorded, summary = _gateway_outcome(ctx, response)

    assert text == "streamed final"
    assert recorded is False
    assert summary.response_text == "streamed final"
    assert summary.context_tokens == 42
    assert summary.tool_rounds == 2
    assert ctx.cfg.last_token_estimate_ref == [42]
    assert runtime.store.snapshot().stable_blocks == ()


def test_gateway_outcome_user_stop_is_typed_and_silent() -> None:
    ctx, _runtime = _context()
    response = {
        "ok": True,
        "status": "interrupted",
        "error_code": "INTERRUPTED",
        "response": "当前任务已停止。",
    }

    text, recorded, summary = _gateway_outcome(ctx, response)

    assert text == ""
    assert recorded is False
    assert summary.interrupted is True
    assert summary.response_text == ""


def test_gateway_outcome_without_typed_code_uses_safe_generic_error() -> None:
    ctx, _runtime = _context()

    text, recorded, summary = _gateway_outcome(
        ctx,
        {"ok": False, "error": "provider unavailable"},
    )

    assert (text, recorded) == ("", False)
    assert summary.ok is False
    assert summary.error == "任务处理失败，请稍后重试；如持续失败，请查看运行诊断。"


def test_gateway_outcome_prefers_channel_neutral_user_error() -> None:
    ctx, _runtime = _context()

    _text, _recorded, summary = _gateway_outcome(
        ctx,
        {
            "ok": False,
            "error_code": "PROVIDER_REQUEST_REJECTED",
            "error": "ProviderRequestRejectedError: internal detail",
            "user_error": "模型服务拒绝了当前配置。",
        },
    )

    assert summary.error == "模型服务拒绝了当前配置。"
    assert "internal detail" not in summary.error


def test_gateway_outcome_show_prompt_routes_through_runtime_console() -> None:
    ctx, runtime = _context(show_prompt=True)

    _gateway_outcome(
        ctx,
        {"ok": True, "prompt": "final prompt", "response": "answer"},
    )

    blocks = runtime.store.snapshot().stable_blocks
    assert len(blocks) == 1
    assert "final prompt" in blocks[0].text


def test_gateway_worker_does_not_duplicate_client_history_context() -> None:
    calls: list[str] = []
    gateway_cfg = SimpleNamespace(
        use_gateway=True,
        build_history_context=lambda: calls.append("gateway") or "旧历史",
    )
    direct_cfg = SimpleNamespace(
        use_gateway=False,
        build_history_context=lambda: calls.append("direct") or "旧历史",
    )

    assert _worker_history_context(gateway_cfg) == ""
    assert _worker_history_context(direct_cfg) == "旧历史"
    assert calls == ["direct"]
