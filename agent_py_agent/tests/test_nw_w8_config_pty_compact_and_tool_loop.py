"""W8 回归单测：5 项新配置注册、#27 实时事实有界增量更新、#34 PTY 双级配额、#35 续跑上限配置化、#36 截断零工具调用续跑、#37 文件变更缓存淘汰。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import _tool_loop_service
from agent_py_agent.agent.agent_core.finalization_compact_auto import (
    _compact_auto_continue_depth_exhausted,
    _max_compact_auto_continue_depth,
    compact_auto_cycle_fields,
)
from agent_py_agent.agent.agent_core.runtime import live_archive
from agent_py_agent.agent.agent_core.tool_loop.response_decision import (
    _TRUNCATED_OUTPUT_RESUME_COUNT,
    ToolLoopRepairCounters,
    _no_tool_calls_decision,
    _NoToolCallsRequest,
)
from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.conversation.host_notices import pending_host_notices
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.conversation.technical_continuation import (
    WAKE_REASON,
    claim_wake,
    publish_pending,
    receipt_for,
    record_closeout,
)
from agent_py_agent.agent.memory_archive import runtime_fact_source
from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.agent.settings.memory import normalize_memory_settings
from agent_py_agent.agent.settings.parameter_metadata import field_readers, ranges, unit_for_key
from agent_py_agent.agent.settings.parameter_registry import (
    DEPLOYMENT_ONLY_KEYS,
    listed_parameters,
)
from agent_py_agent.agent.settings.user_config_capability import (
    EFFECT_IMMEDIATE,
    TUNABLE_KEYS,
)
from agent_py_agent.agent.tooling import pty_sessions as pty_mod
from agent_py_agent.agent.tooling.models import EffectResolverPolicy, ToolRuntimePolicy
from agent_py_agent.agent.tooling.process_scope import ProcessExecutionScope
from agent_py_agent.agent.tooling.pty_sessions import (
    PtyHostPolicy,
    PtySession,
    PtySessionLimitError,
    PtySessionRegistry,
    TerminalSessionTool,
)
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams

# ------------------------------------------------------------------
# 1. Config & Parameter Registry Wiring (5 New Keys)
# ------------------------------------------------------------------


def test_w8_new_config_keys_defaults_and_yaml_loading(tmp_path: Path) -> None:
    cfg = AgentConfig()
    assert cfg.pty_max_sessions == 32
    assert cfg.pty_max_sessions_per_owner == 8
    assert cfg.memory_runtime_fact_recent_tests == 20
    assert cfg.memory_compact_auto_continue_max_depth == 50
    assert cfg.technical_cutoff_continuation_max_count == 3

    default_yaml = Path(__file__).resolve().parents[1] / "config" / "agent_config.yaml"
    loaded_default = load_config(default_yaml)
    assert loaded_default.pty_max_sessions == 32
    assert loaded_default.pty_max_sessions_per_owner == 8
    assert loaded_default.memory_runtime_fact_recent_tests == 20
    assert loaded_default.memory_compact_auto_continue_max_depth == 50
    assert loaded_default.technical_cutoff_continuation_max_count == 3

    custom_yaml = tmp_path / "custom.yaml"
    custom_yaml.write_text(
        "\n".join(
            [
                "pty_max_sessions: 64",
                "pty_max_sessions_per_owner: 12",
                "memory_runtime_fact_recent_tests: 0",
                "memory_compact_auto_continue_max_depth: 0",
                "technical_cutoff_continuation_max_count: 5",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    loaded_custom = load_config(custom_yaml)
    assert loaded_custom.pty_max_sessions == 64
    assert loaded_custom.pty_max_sessions_per_owner == 12
    assert loaded_custom.memory_runtime_fact_recent_tests == 0
    assert loaded_custom.memory_compact_auto_continue_max_depth == 0
    assert loaded_custom.technical_cutoff_continuation_max_count == 5


def test_w8_parameter_registry_classification_metadata_and_memory_coercion() -> None:
    registry = listed_parameters()
    for key in (
        "pty_max_sessions",
        "pty_max_sessions_per_owner",
        "memory_runtime_fact_recent_tests",
        "memory_compact_auto_continue_max_depth",
        "technical_cutoff_continuation_max_count",
    ):
        assert key in registry
        assert registry[key].description

    all_ranges = ranges()
    readers = field_readers()
    for dep_key in ("pty_max_sessions", "pty_max_sessions_per_owner"):
        assert dep_key in DEPLOYMENT_ONLY_KEYS
        spec = registry[dep_key]
        assert spec.tier == "deployment"
        assert spec.safety == "boundary"
        assert spec.effect == "restart_gateway"
        assert not spec.writable
        assert not spec.user_editable
        assert unit_for_key(dep_key) == ""
        assert AgentConfig.__dataclass_fields__[dep_key].metadata["unit"] == "个"
        assert AgentConfig.__dataclass_fields__[dep_key].metadata["bounds"] == (0, None)
        assert dep_key in readers

    for tunable_key in (
        "memory_runtime_fact_recent_tests",
        "memory_compact_auto_continue_max_depth",
        "technical_cutoff_continuation_max_count",
    ):
        assert tunable_key in TUNABLE_KEYS
        tunable = TUNABLE_KEYS[tunable_key]
        assert tunable.effect == EFFECT_IMMEDIATE
        ok_zero, _, val_zero = tunable.validate(0)
        assert ok_zero and val_zero == 0
        ok_pos, _, val_pos = tunable.validate(7)
        assert ok_pos and val_pos == 7
        ok_neg, _, _ = tunable.validate(-1)
        assert not ok_neg
        spec = registry[tunable_key]
        assert spec.tier == "advanced"
        assert spec.writable and spec.user_editable
        assert spec.effect == "immediate"
        assert tunable_key in readers
        if tunable_key != "technical_cutoff_continuation_max_count":
            assert tunable_key in all_ranges and all_ranges[tunable_key]

    coerced_zero, warnings_zero = normalize_memory_settings(
        {"memory_runtime_fact_recent_tests": 0, "memory_compact_auto_continue_max_depth": 0}
    )
    assert coerced_zero.memory_runtime_fact_recent_tests == 0
    assert coerced_zero.memory_compact_auto_continue_max_depth == 0
    assert warnings_zero == []

    coerced_bad, warnings_bad = normalize_memory_settings(
        {"memory_runtime_fact_recent_tests": -3, "memory_compact_auto_continue_max_depth": True}
    )
    assert coerced_bad.memory_runtime_fact_recent_tests == 20
    assert coerced_bad.memory_compact_auto_continue_max_depth == 50
    assert len(warnings_bad) == 2


def test_w8_pty_limits_wire_into_tool_registry_and_shell_tool(tmp_path: Path) -> None:
    registry = ToolRegistry(
        ToolRegistryParams(
            workspace_root=tmp_path,
            max_chars=4096,
            max_entries=100,
            max_matches=50,
            web_max_chars=4096,
            http_timeout=10,
            catalog_limit=20,
            retrieval_limit=5,
            vector_search_enabled=False,
            defer_mcp_connect=True,
            pty_max_sessions=44,
            pty_max_sessions_per_owner=6,
        )
    )
    shell_tool = registry.tools["run_command"]
    assert shell_tool.pty_max_sessions == 44
    assert shell_tool.pty_max_sessions_per_owner == 6


# ------------------------------------------------------------------
# 2. Item #27 — Incremental Cursor & Bounded Tail in Live Runtime Facts
# ------------------------------------------------------------------


def _fact_agent(root: Path, recent_tests_limit: int) -> SimpleNamespace:
    return SimpleNamespace(
        root=root,
        session_id="sess-w8",
        home_paths=SimpleNamespace(owner_home_dir=root, root=root),
        config=SimpleNamespace(
            auto_save_memory=True,
            memory_archive_level=3,
            memory_runtime_fact_recent_tests=recent_tests_limit,
        ),
    )


def test_w8_27_live_archive_progress_uses_incremental_cursor_and_caches_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scanned_batches: list[int] = []
    orig_extract = runtime_fact_source._tool_test_items

    def spy_extract(tool_calls: list[object]) -> list[str]:
        scanned_batches.append(len(tool_calls))
        return orig_extract(tool_calls)

    intent_calls: list[int] = []
    orig_build_intent = runtime_fact_source.build_run_intent

    def spy_build_intent(*args, **kwargs):
        intent_calls.append(1)
        return orig_build_intent(*args, **kwargs)

    monkeypatch.setattr(runtime_fact_source, "_tool_test_items", spy_extract)
    monkeypatch.setattr(runtime_fact_source, "build_run_intent", spy_build_intent)

    historical = [
        {"tool": "run_command", "command": f"pytest test_case_{idx}.py", "ok": True}
        for idx in range(500)
    ]
    state: dict[str, object] = {}
    params = SimpleNamespace(
        request_id="req-w8-27",
        root_user_prompt="执行测试验证",
        run_id="run-1",
        task_id="task-1",
        source="run",
        context_scope="task_local",
        live_archive_state=state,
        tool_context=[],
        executed_tools=["read_file", "run_command"],
        archive_tool_calls=historical,
        runtime_injections=(),
        delivery_contract=None,
    )
    agent = _fact_agent(tmp_path, recent_tests_limit=5)

    live_archive.update_runtime_fact_progress_if_enabled(agent, params, tool_round=1)
    assert scanned_batches == [5]
    assert len(intent_calls) == 1
    assert state["runtime_fact_call_cursor"] == 500

    fact_path = tmp_path / "memory_archive" / "runtime_facts" / "req-w8-27" / "task.json"
    first_payload = json.loads(fact_path.read_text(encoding="utf-8"))
    assert len(first_payload["latest_tests"]) == 5
    assert "test_case_499.py" in first_payload["latest_tests"][-1]

    # Round 2 adds only 2 new calls -> scans ONLY 2 items and reuses cached run_intent.
    historical.append({"tool": "run_command", "command": "pytest test_case_500.py", "ok": True})
    historical.append({"tool": "run_command", "command": "pytest test_case_501.py", "ok": True})
    params.executed_tools.append("pytest_runner")
    live_archive.update_runtime_fact_progress_if_enabled(agent, params, tool_round=2)

    assert scanned_batches == [5, 2]
    assert len(intent_calls) == 1
    assert state["runtime_fact_call_cursor"] == 502
    second_payload = json.loads(fact_path.read_text(encoding="utf-8"))
    assert len(second_payload["latest_tests"]) == 5
    assert "test_case_501.py" in second_payload["latest_tests"][-1]
    assert "test_case_497.py" in second_payload["latest_tests"][0]


def test_w8_27_zero_limit_skips_extraction_and_root_failure_does_not_advance_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_if_called(_calls):
        raise AssertionError("_tool_test_items must not be called when memory_runtime_fact_recent_tests=0")

    monkeypatch.setattr(runtime_fact_source, "_tool_test_items", fail_if_called)

    state: dict[str, object] = {}
    params = SimpleNamespace(
        request_id="req-w8-zero",
        root_user_prompt="任务",
        run_id="run-zero",
        task_id="task-zero",
        source="run",
        context_scope="task_local",
        live_archive_state=state,
        tool_context=[],
        executed_tools=["run_command"],
        archive_tool_calls=[{"tool": "run_command", "command": "pytest x.py", "ok": True}],
        runtime_injections=(),
        delivery_contract=None,
    )
    agent_zero = _fact_agent(tmp_path, recent_tests_limit=0)
    live_archive.update_runtime_fact_progress_if_enabled(agent_zero, params, tool_round=1)
    fact_path = tmp_path / "memory_archive" / "runtime_facts" / "req-w8-zero" / "task.json"
    fact_doc = json.loads(fact_path.read_text(encoding="utf-8"))
    assert fact_doc["latest_tests"] == []
    assert state["runtime_fact_call_cursor"] == 1

    # Partial multi-root failure: root-2 fails -> cursor must remain uncommitted.
    monkeypatch.undo()
    root_one = tmp_path / "root_one"
    root_two = tmp_path / "root_two"
    monkeypatch.setattr(live_archive, "runtime_archive_roots", lambda _agent: (root_one, root_two))
    orig_write = live_archive.write_runtime_fact_source

    def flaky_write(req):
        if Path(req.root) == root_two:
            raise OSError("simulated disk error on root_two")
        return orig_write(req)

    monkeypatch.setattr(live_archive, "write_runtime_fact_source", flaky_write)
    state_multi: dict[str, object] = {}
    params_multi = SimpleNamespace(
        request_id="req-w8-multi",
        root_user_prompt="多根更新",
        run_id="run-m",
        task_id="task-m",
        source="run",
        context_scope="task_local",
        live_archive_state=state_multi,
        tool_context=[],
        executed_tools=["read_file"],
        archive_tool_calls=[{"tool": "run_command", "command": "pytest m1.py", "ok": True}],
        runtime_injections=(),
        delivery_contract=None,
    )
    live_archive.update_runtime_fact_progress_if_enabled(_fact_agent(root_one, 10), params_multi, tool_round=1)
    assert state_multi["runtime_fact_call_cursor"] == 0
    assert state_multi.get("live_archive_errors")


# ------------------------------------------------------------------
# 3. Item #34 — PTY Per-Owner & Global Quotas + Retired Eviction
# ------------------------------------------------------------------


class _DummyProcess:
    def __init__(self, *, running: bool = True) -> None:
        self._running = running

    def poll(self):
        return None if self._running else 0


def _fake_pty_session(session_id: str, owner_id: str, *, retired: bool = False) -> PtySession:
    run_scope = {"owner_id": owner_id, "session_id": f"conv-{owner_id}"}
    scope = ProcessExecutionScope.from_run_scope(run_scope, f"/tmp/{owner_id}")
    access = pty_mod._pty_access_scope(f"/tmp/{owner_id}", (), (), (), run_scope)
    term = (
        pty_mod.ProcessTerminationReceipt(
            method="terminated",
            confirmed=True,
            return_code=0,
            observed_processes=1,
        )
        if retired
        else None
    )
    r_fd, w_fd = os.pipe()
    os.close(w_fd)
    return PtySession(
        session_id=session_id,
        command="sleep 1",
        process=_DummyProcess(running=not retired),
        master_fd=r_fd,
        started_at=1.0,
        last_active_at=1.0,
        access_scope=access,
        execution_scope=scope,
        closed=retired,
        termination=term,
        owner_id=owner_id,
    )


def test_w8_34_pty_registry_enforces_per_owner_and_global_limits_and_evicts_retired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = PtySessionRegistry(max_sessions=3, max_sessions_per_owner=2)
    created_fds: list[int] = []

    def fake_spawn(session_id, command, target, sandbox, run_scope, idle_timeout_seconds=None):
        launch = registry._pending_starts[session_id]
        session = _fake_pty_session(session_id, launch.owner_id, retired=False)
        created_fds.append(session.master_fd)
        with registry._lock:
            registry._sessions[session_id] = session
        return session

    monkeypatch.setattr(registry, "_spawn", fake_spawn)

    owner_a_home = tmp_path / "owner-a"
    owner_b_home = tmp_path / "owner-b"
    scope_a = {"owner_id": "owner-a", "conversation_id": "c-a"}
    scope_b = {"owner_id": "owner-b", "conversation_id": "c-b"}

    try:
        s1 = registry.start("cmd", owner_a_home, owner_a_home, run_scope=scope_a)
        s2 = registry.start("cmd", owner_a_home, owner_a_home, run_scope=scope_a)
        # Owner A reaches per-owner limit (2) -> rejected even while global slots (3) remain.
        with pytest.raises(PtySessionLimitError) as exc_owner:
            registry.start("cmd", owner_a_home, owner_a_home, run_scope=scope_a)
        assert exc_owner.value.scope == "owner"
        assert "PTY_SESSION_LIMIT" in str(exc_owner.value)

        # Owner B can still start 1 session (total active = 3 == global cap).
        _ = registry.start("cmd", owner_b_home, owner_b_home, run_scope=scope_b)
        with pytest.raises(PtySessionLimitError) as exc_global:
            registry.start("cmd", owner_b_home, owner_b_home, run_scope=scope_b)
        assert exc_global.value.scope == "global"

        # Add unpinned retired sessions + one pinned proof; retiring s1 frees slots and unpinned excess gets pruned.
        s1.closed = True
        s1.process._running = False
        s1.termination = pty_mod.ProcessTerminationReceipt(
            method="terminated",
            confirmed=True,
            return_code=0,
            observed_processes=1,
        )
        # Pre-seed 4 extra retired unpinned sessions and 1 retired pinned proof.
        # With s1 retired (5 unpinned finished total) and global keep_count=3, s1 & retired-0 get pruned.
        for idx in range(4):
            ret = _fake_pty_session(f"retired-{idx}", "owner-old", retired=True)
            created_fds.append(ret.master_fd)
            registry._sessions[ret.session_id] = ret
        pinned = _fake_pty_session("pinned-proof", "owner-old", retired=True)
        created_fds.append(pinned.master_fd)
        registry._sessions[pinned.session_id] = pinned
        registry._cleanup_proofs[pinned.session_id] = pty_mod._PtyCleanupProof(
            session_id=pinned.session_id,
            execution_scope=pinned.execution_scope,
        )

        # Now active count == 2 (s2 + s_b1) < 3; oldest unpinned finished (s1, retired-0) are evicted while pinned-proof stays.
        s_b2 = registry.start("cmd", owner_b_home, owner_b_home, run_scope=scope_b)
        assert s_b2.session_id in registry._sessions
        assert "pinned-proof" in registry._sessions
        assert s1.session_id not in registry._sessions
        assert "retired-0" not in registry._sessions

        # Unlimited mode (0 = unlimited) permits starting past prior limits.
        unlimited = PtySessionRegistry(max_sessions=0, max_sessions_per_owner=0)
        monkeypatch.setattr(unlimited, "_spawn", lambda sid, *a, **kw: _fake_pty_session(sid, "u", retired=False))
        for _ in range(10):
            s_u = unlimited.start("cmd", owner_a_home, owner_a_home, run_scope=scope_a)
            os.close(s_u.master_fd)
    finally:
        for fd in created_fds:
            try:
                os.close(fd)
            except OSError:
                pass


def test_w8_34_terminal_tool_returns_structured_error_envelope_on_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_shell = SimpleNamespace(
        path_access_policy=SimpleNamespace(owner_scope_root=tmp_path, foreground_admin_full_access=False),
        default_timeout=30,
        host_private_roots=(),
        host_credential_env_names=frozenset(),
        protected_persona_root=None,
        pty_session_idle_timeout_seconds=None,
        pty_max_sessions=4,
        pty_max_sessions_per_owner=2,
        _execution_target=lambda _params, _cmd: tmp_path,
        _sandbox_read_roots=lambda: (tmp_path,),
        _sandbox_write_roots=lambda: (tmp_path,),
    )
    tool = TerminalSessionTool(fake_shell)
    monkeypatch.setattr(pty_mod, "_sandbox_read_roots", lambda *_a, **_kw: (tmp_path,))
    monkeypatch.setattr(pty_mod, "_sandbox_write_roots", lambda *_a, **_kw: (tmp_path,))

    def raise_owner_limit(*args, **kwargs):
        host_policy = kwargs["host_policy"]
        assert isinstance(host_policy, PtyHostPolicy)
        assert host_policy.max_sessions == 4
        assert host_policy.max_sessions_per_owner == 2
        raise PtySessionLimitError(
            "PTY_SESSION_LIMIT: active per-owner session limit is 2",
            scope="owner",
            limit=2,
            active_count=2,
            owner_id="owner-a",
        )

    monkeypatch.setattr(pty_mod.pty_session_registry, "start", raise_owner_limit)
    owner_outcome = tool.execute({"action": "start", "command": "sleep 10"})
    assert not owner_outcome.ok
    assert owner_outcome.error_code == "TOOL_RUNTIME_ERROR"
    assert owner_outcome.reported_error_code == "PTY_OWNER_SESSION_LIMIT_REACHED"
    assert owner_outcome.effect_outcome == "not_started"
    assert "terminal_session" in owner_outcome.next_action
    assert owner_outcome.result_envelope["error_code"] == "PTY_OWNER_SESSION_LIMIT_REACHED"
    assert owner_outcome.result_envelope["limit"] == 2
    assert owner_outcome.result_envelope["active_count"] == 2
    assert "terminal_session" in owner_outcome.result_envelope["next_action"]

    monkeypatch.setattr(
        pty_mod.pty_session_registry,
        "start",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            PtySessionLimitError(
                "PTY_SESSION_LIMIT: active session limit is 4",
                scope="global",
                limit=4,
                active_count=4,
            )
        ),
    )
    global_outcome = tool.execute({"action": "start", "command": "sleep 10"})
    assert not global_outcome.ok
    assert global_outcome.error_code == "TOOL_RUNTIME_ERROR"
    assert global_outcome.reported_error_code == "PTY_GLOBAL_SESSION_LIMIT_REACHED"
    assert global_outcome.effect_outcome == "not_started"
    assert global_outcome.result_envelope["error_code"] == "PTY_GLOBAL_SESSION_LIMIT_REACHED"
    assert "terminal_session" in global_outcome.next_action


# ------------------------------------------------------------------
# 4. Item #35 — Dynamic Compact Auto-Continue Depth & Cutoff Continuation Limit
# ------------------------------------------------------------------


def test_w8_35_compact_auto_continue_depth_reads_config_and_supports_zero_unlimited() -> None:
    agent_default = SimpleNamespace(config=SimpleNamespace())
    assert _max_compact_auto_continue_depth(agent_default) == 50

    agent_seven = SimpleNamespace(config=SimpleNamespace(memory_compact_auto_continue_max_depth=7))
    assert _max_compact_auto_continue_depth(agent_seven) == 7
    ctx_six = SimpleNamespace(task_attributes={}, compact_auto_continue_depth=6)
    ctx_seven = SimpleNamespace(task_attributes={}, compact_auto_continue_depth=7)
    assert not _compact_auto_continue_depth_exhausted(agent_seven, ctx_six)
    assert _compact_auto_continue_depth_exhausted(agent_seven, ctx_seven)

    capped = compact_auto_cycle_fields(agent_seven, ctx_seven, {"total_tokens": 1000})
    assert capped["memory_compact_trigger_reason"] == "continuation_depth_cap_reached"
    assert capped["memory_compact_auto_status"] == "returned_after_depth_cap"
    assert "(7)" in capped["memory_compact_message"]

    agent_unlimited = SimpleNamespace(config=SimpleNamespace(memory_compact_auto_continue_max_depth=0))
    assert _max_compact_auto_continue_depth(agent_unlimited) == 0
    ctx_high = SimpleNamespace(task_attributes={}, compact_auto_continue_depth=250)
    assert not _compact_auto_continue_depth_exhausted(agent_unlimited, ctx_high)


def test_w8_35_technical_cutoff_continuation_respects_config_five_and_zero_unlimited(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.create(canonical_user_id="user", owner_id="owner")
    store.messages.append(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": "继续任务",
            "metadata": {"conversation_request_id": "req-1"},
        }
    )
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": "task-5", "goal": "长任务"})
    policy = ToolRuntimePolicy(effect_resolver=EffectResolverPolicy(default_effect="mutating"))
    tools = SimpleNamespace(
        runtime_snapshot=lambda: SimpleNamespace(
            runtime=lambda _name: SimpleNamespace(runtime_policy=policy)
        )
    )
    agent = SimpleNamespace(
        conversation_store=store,
        home_paths=SimpleNamespace(owner_id="owner"),
        config=SimpleNamespace(
            technical_cutoff_continuation_enabled=True,
            technical_cutoff_continuation_max_count=5,
        ),
        tools=tools,
    )
    ctx = SimpleNamespace(
        do_save=True,
        context_scope="conversation",
        source="gateway",
        request_id="req-1",
        run_id="run-1",
        attempt_id="attempt-1",
        task_attributes={"conversation_task_id": "task-5", "conversation_thread_id": thread.thread_id},
        archive_tool_calls=[{"tool": "write_file", "ok": True, "handler_executed": True}],
        final_response=SimpleNamespace(
            runtime_reason="TOOL_ROUND_LIMIT_REACHED",
            runtime_source="tool_loop",
            runtime_status="unfinished",
        ),
    )

    first = record_closeout(agent, ctx)
    assert first is not None and first.status == "pending" and first.sequence == 1
    assert publish_pending(agent, store, "task-5") is True
    prev_receipt = first

    for seq in range(2, 7):
        wake = next(
            row
            for row in store.wakes.pending(limit=50)
            if row.reason == WAKE_REASON and row.metadata.get("cutoff_receipt_key") == prev_receipt.key
        )
        assert claim_wake(agent, store, wake) is True
        ctx.source, ctx.do_save = "background_main_agent", False
        ctx.run_id, ctx.attempt_id = f"run-{seq}", f"attempt-{seq}"
        rec = record_closeout(agent, ctx)
        assert rec is not None
        prev_receipt = rec
        if seq <= 5:
            assert rec.status == "pending" and rec.sequence == seq
            loaded = receipt_for(store.tasks.load("task-5"))
            assert loaded is not None and loaded.sequence == seq
        else:
            assert rec.status == "stopped" and rec.sequence == 5

    notices = pending_host_notices(store, thread.thread_id)
    assert any("达到 5 片上限" in item.text for item in notices)

    # Zero = unlimited continuations (past default 3 and past 5).
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": "task-0", "goal": "无限续跑"})
    agent.config.technical_cutoff_continuation_max_count = 0
    ctx.source, ctx.do_save = "gateway", True
    ctx.run_id, ctx.attempt_id = "run-0-1", "attempt-0-1"
    ctx.task_attributes = {"conversation_task_id": "task-0", "conversation_thread_id": thread.thread_id}
    r1 = record_closeout(agent, ctx)
    assert r1 is not None and r1.status == "pending" and r1.sequence == 1
    prev_unlimited = r1
    for seq in range(2, 7):
        wake = next(
            row
            for row in store.wakes.pending(limit=50)
            if row.reason == WAKE_REASON and row.metadata.get("cutoff_receipt_key") == prev_unlimited.key
        )
        assert claim_wake(agent, store, wake) is True
        ctx.source, ctx.do_save = "background_main_agent", False
        ctx.run_id, ctx.attempt_id = f"run-0-{seq}", f"attempt-0-{seq}"
        rk = record_closeout(agent, ctx)
        assert rk is not None and rk.status == "pending" and rk.sequence == seq
        prev_unlimited = rk


# ------------------------------------------------------------------
# 5. Item #36 — Truncated Length Output With Zero Valid Tool Calls
# ------------------------------------------------------------------


def test_w8_36_truncated_length_zero_tool_calls_routes_into_bounded_continuation() -> None:
    params = SimpleNamespace(
        context_scope="task_local",
        task_attributes={},
        live_archive_state={},
        tool_context=[],
        executed_tools=["write_file"],
    )
    response = ModelResponse(
        text="",
        backend="fake",
        truncated=True,
        stop_reason="length",
    )
    agent = SimpleNamespace(config=SimpleNamespace(enable_tools=True))

    # First two truncated empty-text responses after tool execution must enter output-limit-resume, NOT empty nudge.
    first = _no_tool_calls_decision(
        _NoToolCallsRequest(
            agent=agent,
            params=params,
            response=response,
            counters=ToolLoopRepairCounters(),
            has_protected_marker=False,
        )
    )
    assert first.action == "continue"
    assert first.counters.truncated_output_repairs == 1
    assert first.counters.empty_text_repairs == 0
    assert any("output-limit-resume" in str(item) for item in params.tool_context)

    # Exhausting _TRUNCATED_OUTPUT_RESUME_COUNT terminates cleanly as unfinished from tool_loop.
    exhausted = _no_tool_calls_decision(
        _NoToolCallsRequest(
            agent=agent,
            params=params,
            response=response,
            counters=ToolLoopRepairCounters(truncated_output_repairs=_TRUNCATED_OUTPUT_RESUME_COUNT),
            has_protected_marker=False,
        )
    )
    assert exhausted.action == "break"
    assert exhausted.response.runtime_status == "unfinished"
    assert exhausted.response.runtime_reason == "MODEL_RESPONSE_TRUNCATED"
    assert exhausted.response.runtime_source == "tool_loop"

    # stop_reason="max_tokens" without truncated=True still routes into bounded continuation.
    max_tok_params = SimpleNamespace(
        context_scope="task_local",
        task_attributes={},
        live_archive_state={},
        tool_context=[],
        executed_tools=[],
    )
    max_tok_response = ModelResponse(text="", backend="fake", stop_reason="max_tokens")
    max_tok_decision = _no_tool_calls_decision(
        _NoToolCallsRequest(
            agent=agent,
            params=max_tok_params,
            response=max_tok_response,
            counters=ToolLoopRepairCounters(),
            has_protected_marker=False,
        )
    )
    assert max_tok_decision.action == "continue"
    assert max_tok_decision.counters.truncated_output_repairs == 1

    # Non-length error status (MODEL_STREAM_INCOMPLETE) still breaks without continuation.
    err_params = SimpleNamespace(
        context_scope="task_local",
        task_attributes={},
        live_archive_state={},
        tool_context=[],
        executed_tools=[],
    )
    err_response = ModelResponse(
        text="",
        backend="fake",
        truncated=True,
        runtime_status="error",
        runtime_reason="MODEL_STREAM_INCOMPLETE",
        runtime_source="model_provider",
    )
    err_decision = _no_tool_calls_decision(
        _NoToolCallsRequest(
            agent=agent,
            params=err_params,
            response=err_response,
            counters=ToolLoopRepairCounters(),
            has_protected_marker=False,
        )
    )
    assert err_decision.action == "break"
    assert err_decision.counters.truncated_output_repairs == 0


# ------------------------------------------------------------------
# 6. Item #37 — Asset Mutation State Map Eviction Beyond 32 Entries
# ------------------------------------------------------------------


def test_w8_37_asset_mutation_states_evicts_clean_before_mutated_and_keeps_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed_calls: list[tuple[str, str]] = []

    class _FakeLedger:
        def observe(self, state, tool_name, args, value, **kwargs):
            book = state.setdefault("asset_mutations", {"targets": {}, "evidence_tags": []})
            observed_calls.append((tool_name, str(args.get("path") or "")))
            if tool_name == "write_file":
                target_path = str(args.get("path") or "")
                book["targets"][target_path] = {"kind": "script", "path": target_path}

    from agent_py_agent.agent.assets import mutation_ledger

    monkeypatch.setattr(mutation_ledger, "AssetMutationLedger", _FakeLedger)

    agent = SimpleNamespace(
        root=tmp_path,
        home_paths=SimpleNamespace(owner_home_dir=tmp_path),
        config=SimpleNamespace(enable_self_learning=True),
    )

    def _observe(run_idx: int, *, tool_name: str, file_path: str) -> dict[str, object]:
        state: dict[str, object] = {}
        params = SimpleNamespace(
            context_scope="conversation",
            run_id=f"run-{run_idx}",
            attempt_id="attempt-1",
            live_archive_state=state,
            in_use_method_ids=(),
            task_attributes={},
        )
        record = SimpleNamespace(
            params=params,
            call=SimpleNamespace(tool_name=tool_name, arguments={"path": file_path}, call_id=f"c-{run_idx}"),
            result=SimpleNamespace(ok=True, output={}, error_code=""),
        )
        archive_record = {"tool": tool_name, "parameters": {"path": file_path}, "call_id": f"c-{run_idx}"}
        _tool_loop_service._observe_turn_asset_mutations(agent, record, archive_record)
        return state

    # Fill 32 slots: first 10 clean read-only runs, next 22 real mutated runs.
    for idx in range(10):
        _observe(idx, tool_name="read_file", file_path=str(tmp_path / f"read_{idx}.py"))
    for idx in range(10, 32):
        _observe(idx, tool_name="write_file", file_path=str(tmp_path / f"mut_{idx}.py"))
    assert len(agent._asset_mutation_states) == 32

    # Runs 32..39 mutate files -> clean runs (0..7) must be evicted BEFORE any mutated run (10..31).
    for idx in range(32, 40):
        _observe(idx, tool_name="write_file", file_path=str(tmp_path / f"mut_{idx}.py"))

    assert len(agent._asset_mutation_states) == 32
    for idx in range(10, 40):
        assert (f"run-{idx}", "attempt-1") in agent._asset_mutation_states
    assert ("run-8", "attempt-1") in agent._asset_mutation_states
    assert ("run-9", "attempt-1") in agent._asset_mutation_states
    for idx in range(8):
        assert (f"run-{idx}", "attempt-1") not in agent._asset_mutation_states

    # Now all 32 retained entries have mutations; inserting run-40 evicts oldest remaining (run-8).
    _observe(40, tool_name="write_file", file_path=str(tmp_path / "mut_40.py"))
    assert len(agent._asset_mutation_states) == 32
    assert ("run-8", "attempt-1") not in agent._asset_mutation_states
    assert ("run-40", "attempt-1") in agent._asset_mutation_states
