from __future__ import annotations

import json
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

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


def test_thin_tui_initial_gateway_job_carries_client_workspace(tmp_path, monkeypatch) -> None:
    """Initial TUI submission must not lose cwd when the audit Agent is intentionally absent."""
    from agent_py_agent.agent.gateway_parts import request_client
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.cli.chat_parts.tui_worker_paths import _submit_new_gateway_job

    project = tmp_path / "project"
    project.mkdir()
    paths = gateway_paths_from_root(tmp_path / "gateway")
    client = SimpleNamespace(
        gateway_client_only=True,
        root=project,
        workspace_roots=[project],
    )
    cfg = SimpleNamespace(
        agent=client,
        paths=paths,
        current_session_id="session-project",
        tui_runtime=TuiRuntime("session-project"),
    )
    write_request = request_client.write_gateway_request

    def inspect_before_admission(target, payload):
        assert payload["id"] in cfg.tui_runtime._owned_gateway_requests
        assert job.gateway_request_id == ""
        assert "on_request_allocated" not in payload
        return write_request(target, payload)

    monkeypatch.setattr(request_client, "write_gateway_request", inspect_before_admission)
    job = SimpleNamespace(
        user="创建项目",
        prompt_files=[],
        save=True,
        show_prompt=False,
        resume_context=None,
        system_task=None,
        tool_approval=True,
        rich_transcript=True,
        gateway_request_id="",
    )

    request_id, _chunk_path, _terminal_path = _submit_new_gateway_job(cfg, job, [])

    payload = json.loads((paths.inbox / f"{request_id}.json").read_text(encoding="utf-8"))
    assert payload["workspace"] == {
        "cwd": str(project.resolve()),
        "roots": [str(project.resolve())],
    }
    assert job.gateway_request_id == request_id


def test_display_registration_failure_does_not_submit_a_request(tmp_path):
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.gateway_parts.request_client import (
        GatewayAskParams,
        submit_gateway_ask,
    )

    paths = gateway_paths_from_root(tmp_path / "gateway")

    def fail(_request_id):
        raise RuntimeError("display registration failed")

    with pytest.raises(RuntimeError, match="display registration failed"):
        submit_gateway_ask(paths, params=GatewayAskParams(prompt="普通消息"), on_request_allocated=fail)
    assert list(paths.inbox.glob("*.json")) == []


def test_attached_gateway_job_registers_display_without_resubmitting():
    runtime = TuiRuntime("session")
    cfg = SimpleNamespace(use_gateway=True, tui_runtime=runtime)
    _tui_prepare_gateway_job(cfg, SimpleNamespace(gateway_request_id="gwreq-existing"))
    assert runtime._owned_gateway_requests == {"gwreq-existing"}


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


@pytest.mark.parametrize("fields", [
    {"turn_end_reason": "max-tokens"},
    {"runtime_status": "unfinished", "runtime_reason": "MODEL_RESPONSE_TRUNCATED"},
])
def test_gateway_truncated_reply_is_preserved_with_visible_failure(fields):
    ctx, runtime = _context()
    text, recorded, summary = _gateway_outcome(ctx, {"ok": True, "response": "接下来准备", **fields})
    assert text == "接下来准备" and recorded is False
    assert summary.ok is False and "长度限制" in summary.error
    adapter = runtime.begin_turn("truncated")
    adapter.finalize(summary)
    snapshot = runtime.store.snapshot()
    assert any(block.text == text for block in snapshot.stable_blocks)
    assert any("长度限制" in block.text for block in snapshot.stable_blocks)
    assert not snapshot.has_active_work


def test_length_notice_does_not_parse_response_or_override_explicit_completion():
    from agent_py_agent.cli.chat_parts.tui_worker_paths import _model_length_error

    assert _model_length_error({"response": "MODEL_RESPONSE_TRUNCATED", "runtime_status": "ok"}) == ""
    assert _model_length_error({"turn_end_reason": "completed", "runtime_reason": "MODEL_RESPONSE_TRUNCATED"}) == ""
    assert "长度限制" in _model_length_error(SimpleNamespace(runtime_status="unfinished", runtime_reason="MODEL_RESPONSE_TRUNCATED"))


def test_empty_truncated_reply_ends_working_without_fake_assistant_body():
    from agent_py_agent.agent.gateway_parts.request_errors import (
        gateway_empty_model_response_projection,
    )

    ctx, runtime = _context()
    adapter = runtime.begin_turn("empty-truncated")
    response = gateway_empty_model_response_projection(SimpleNamespace(
        response="", runtime_status="unfinished", runtime_reason="MODEL_RESPONSE_TRUNCATED",
    ))
    text, recorded, summary = _gateway_outcome(ctx, response)
    assert text == "" and not recorded and not summary.ok
    assert "尚未形成完整正文" in summary.error
    adapter.finalize(summary)
    snapshot = runtime.store.snapshot()
    assert not snapshot.has_active_work
    assert not any(block.role == "assistant" for block in snapshot.stable_blocks)
    assert any("已有工具操作和历史保留" in block.text for block in snapshot.stable_blocks)


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
