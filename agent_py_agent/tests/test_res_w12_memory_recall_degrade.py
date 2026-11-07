"""前台记忆读取按轮降级；真实权威库、写入拒绝和 native 异常保持严格。"""

import logging
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime import loop_support
from agent_py_agent.agent.agent_core.runtime.loop_models import RuntimeContextRequest
from agent_py_agent.agent.capability.memory_search_tool import MemorySearchTool
from agent_py_agent.agent.common.owner_write_errors import OwnerManagementIngressUnavailable
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.memory_store.generation_manifest import MemoryGenerationStore
from agent_py_agent.agent.memory_store.jsonl import MemoryAuthorityCorruptError, MemoryRecord
from agent_py_agent.agent.memory_store.recall import MemoryRecallScope
from agent_py_agent.agent.prompting_parts.memory_context import (
    BudgetedMemoryRecords,
    memory_context_text,
)
from agent_py_agent.agent.settings.config import AgentConfig


@pytest.fixture
def recall_case(tmp_path):
    config = AgentConfig(
        my_agent_home=tmp_path / "home", model_backend="echo", model_name="echo",
        enable_tools=False, enable_self_learning=False, memory_semantic_recall=False,
        memory_curator_enabled=False, auto_save_memory=False,
    )
    agent = SimpleAgent(config, Path(__file__).resolve().parents[2],
                        workspace_roots=[tmp_path], defer_mcp_connect=True)
    rows = [MemoryRecord(
        "user", f"sentinel confirmed fact {number}", entry_id=f"fact-{number}", kind="fact",
        attributes={"origin": "user_explicit", "scope_type": "personal", "scope_key": "personal",
                    "subject_key": f"fact-{number}"},
    ) for number in range(2)]
    agent.memory.path.write_text("".join(row.to_json() + "\n" for row in rows), encoding="utf-8")
    return agent, rows


def _prepare(agent):
    # 将已知原缺陷变成明确断言，而不是把无关环境错误当作有效红灯。
    error, prepared = None, None
    try:
        prepared = loop_support._prepare_runtime_context(
            agent, RuntimeContextRequest("sentinel", None, False, allowed_tools=[]),
        )
    except (OSError, UnicodeError, ValueError, KeyError) as exc:
        error = exc
    assert error is None, f"foreground prompt failed instead of degrading: {type(error).__name__}"
    return prepared


def _assert_degraded(prepared, caplog, error_type):
    assert prepared.memories == []
    assert prepared.routed_context.findings.count(
        f"long-term memory recall unavailable: {error_type}") == 1
    assert "sentinel confirmed fact" not in memory_context_text(prepared.memories)
    assert f"MEMORY_RECALL_DEGRADED type={error_type}" in caplog.text


def _warm_generation(agent, rows):
    agent.memory.migrate_tiers()
    assert {row.entry_id for row in _prepare(agent).memories} == {row.entry_id for row in rows}
    agent.memory.flush_access_events()
    assert not agent.memory._pending_access
    return MemoryGenerationStore(agent.memory.path)


@pytest.mark.parametrize("fault,error_type", [
    ("middle", "JSONDecodeError"), ("tail", "JSONDecodeError"),
    ("utf8", "UnicodeDecodeError"), ("invalid_record", "ValueError"),
])
def test_corrupt_legacy_rows_degrade_prompt_without_repair(recall_case, caplog, fault, error_type):
    agent, rows = recall_case
    good = (rows[0].to_json() + "\n").encode()
    payload = {
        "middle": good + b"{bad\n" + (rows[1].to_json() + "\n").encode(),
        "tail": good + b'{"role":',
        "utf8": good + b"\xff\n",
        "invalid_record": good + b'{"unexpected": "not a memory event"}\n',
    }[fault]
    agent.memory.path.write_bytes(payload)
    caplog.set_level(logging.WARNING)
    for _ in range(2):
        _assert_degraded(_prepare(agent), caplog, error_type)
        assert agent.memory.path.read_bytes() == payload
        assert not agent.memory._pending_access
    assert not MemorySearchTool(agent).execute({"query": "sentinel"}).ok


def test_healthy_legacy_recall_and_access_are_unchanged(recall_case, caplog):
    agent, rows = recall_case
    before = agent.memory.path.read_bytes()
    prepared = _prepare(agent)
    assert {row.entry_id for row in prepared.memories} == {row.entry_id for row in rows}
    assert set(agent.memory._pending_access) == {row.entry_id for row in rows}
    assert not prepared.routed_context.findings
    assert "sentinel confirmed fact" in memory_context_text(prepared.memories)
    assert "MEMORY_RECALL_DEGRADED" not in caplog.text
    assert agent.memory.path.read_bytes() == before


@pytest.mark.parametrize("fault", ["directory", "child", "corrupt_child", "truncated_child"])
def test_generation_loss_on_cached_agent_degrades_but_writes_stay_strict(recall_case, caplog, fault):
    agent, rows = recall_case
    memory = agent.memory
    store = _warm_generation(agent, rows)
    manifest = store.manifest()
    child = memory.path.parent / manifest["segments"][0]["path"]
    generation_dir = memory.path.with_name(memory.path.name + ".generations")
    if fault == "directory":
        shutil.rmtree(generation_dir)
        missing = generation_dir
    elif fault == "child":
        child.unlink()
        missing = child
    else:
        child.write_bytes(b"corrupt" if fault == "corrupt_child" else b"")
        missing = None
    survivors = {path: path.read_bytes() for path in (memory.path, store.manifest_path,
        store.tombstones.path, *(memory.path.parent / part["path"] for part in manifest["segments"]))
        if path.is_file()}
    caplog.set_level(logging.WARNING)
    for _ in range(2):
        _assert_degraded(_prepare(agent), caplog, "ValueError")
        assert agent.memory is memory
        assert not memory._pending_access
    tool_result = MemorySearchTool(agent).execute({"query": "sentinel"})
    assert not tool_result.ok and tool_result.error_code == "TOOL_EXECUTION_FAILED"
    with pytest.raises(ValueError, match="MEMORY_GENERATION_UNREADABLE"):
        memory.add("user", "must not replace lost authority", kind="fact", attributes={
            **rows[0].attributes, "subject_key": "replacement"})
    assert all(path.read_bytes() == raw for path, raw in survivors.items())
    if missing is not None:
        assert not missing.exists()


@pytest.mark.parametrize("fault,error_type", [
    ("missing_manifest", "ValueError"), ("bad_manifest", "JSONDecodeError"),
    ("bad_tombstones", "ValueError"),
])
def test_control_record_corruption_never_resurrects_cached_facts(recall_case, caplog, fault, error_type):
    agent, rows = recall_case
    store = _warm_generation(agent, rows)
    if fault == "missing_manifest":
        store.manifest_path.unlink()
        target, expected = store.manifest_path, None
    else:
        target = store.manifest_path if fault == "bad_manifest" else store.tombstones.path
        expected = b"{" if fault == "bad_manifest" else b'{"schema_version":"invalid"}'
        target.write_bytes(expected)
    before = agent.memory.path.read_bytes()
    caplog.set_level(logging.WARNING)
    _assert_degraded(_prepare(agent), caplog, error_type)
    assert agent.memory.path.read_bytes() == before
    assert not agent.memory._pending_access
    assert (target.read_bytes() if target.exists() else None) == expected
    assert not MemorySearchTool(agent).execute({"query": "sentinel"}).ok


@pytest.mark.parametrize("error_class", [OSError, UnicodeError, ValueError, KeyError, MemoryAuthorityCorruptError])
def test_confirmation_only_error_drops_unconfirmed_records_and_keeps_fixed_sources(
    recall_case, monkeypatch, caplog, error_class,
):
    agent, rows = recall_case
    fixed = MemoryRecord("system", "fixed source remains", kind="lesson", entry_id="fixed",
        source="candidate:fixed", attributes={"scope_type": "personal", "scope_key": "personal"})
    failure = error_class("PRIVATE-ROW-CONTENT")
    calls = []

    def fail_confirm(candidates, predicate):
        calls.append([row.entry_id for row in candidates])
        raise failure

    monkeypatch.setattr(agent.memory, "confirm_scoped_access", fail_confirm)
    monkeypatch.setattr(loop_support, "hot_memory_records", lambda *_args, **_kwargs: [fixed])
    caplog.set_level(logging.WARNING)
    prepared = _prepare(agent)
    assert calls and set(calls[0]) == {row.entry_id for row in rows}
    assert prepared.memories == [fixed]
    text = memory_context_text(prepared.memories)
    assert fixed.content in text and all(row.content not in text for row in rows)
    assert prepared.routed_context.findings == [
        f"long-term memory recall unavailable: {error_class.__name__}"]
    assert f"MEMORY_RECALL_DEGRADED type={error_class.__name__}" in caplog.text
    assert "PRIVATE-ROW-CONTENT" not in caplog.text + str(prepared.routed_context.findings)
    assert not agent.memory._pending_access

    # 同轮再次确认只保留一个 finding；预算遗漏、固定源和已有访问信号不被重置。
    routed = prepared.routed_context
    routed.supplement_entry_ids = [rows[0].entry_id, fixed.entry_id]
    agent.memory._note_access("previously-confirmed")
    selected = loop_support._confirm_final_memory_access(agent,
        BudgetedMemoryRecords([fixed, *rows], omitted_count=3), MemoryRecallScope.from_runtime(),
        {fixed.entry_id}, routed)
    assert selected == [fixed] and selected.omitted_count == 3
    assert routed.supplement_entry_ids == [fixed.entry_id]
    assert len(routed.findings) == 1
    assert set(agent.memory._pending_access) == {"previously-confirmed"}


@pytest.mark.parametrize("boundary", ["search_scoped_candidates", "confirm_scoped_access"])
@pytest.mark.parametrize("error_class", [RuntimeError, InterruptedError, OwnerManagementIngressUnavailable])
def test_unexpected_cancel_and_native_errors_propagate_unchanged(
    recall_case, monkeypatch, caplog, boundary, error_class,
):
    agent, _ = recall_case
    failure = error_class("must propagate unchanged")

    def fail_read(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(agent.memory, boundary, fail_read)
    with pytest.raises(error_class) as caught:
        loop_support._prepare_runtime_context(agent, RuntimeContextRequest("sentinel", None, False))
    assert caught.value is failure
    assert not agent.memory._pending_access
    assert "MEMORY_RECALL_DEGRADED" not in caplog.text


def test_suppressed_recall_does_not_attempt_either_read(recall_case, monkeypatch, caplog):
    agent, _ = recall_case

    def unexpected_read(*_args, **_kwargs):
        pytest.fail("suppressed recall must not touch long-term authority")

    for name in ("all", "search_scoped_candidates", "confirm_scoped_access"):
        monkeypatch.setattr(agent.memory, name, unexpected_read)
    agent.owner_policy = SimpleNamespace(memory_enabled=False)
    prepared = _prepare(agent)
    assert prepared.memories == [] and not prepared.routed_context.findings
    assert "MEMORY_RECALL_DEGRADED" not in caplog.text
