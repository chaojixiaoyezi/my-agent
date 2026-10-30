"""线程摘要去重不能放宽 Gateway 的 1MiB、有界读取和 nofollow 合同。"""
from dataclasses import replace

import pytest

from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE
from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic
from agent_py_agent.tests.test_compact_scoped_checkpoint import (
    _commit_transcript,
)
from agent_py_agent.tests.test_compact_scoped_checkpoint import (
    compact_case as compact_case,
)


def test_compact_summary_fits_header_and_replays_complete_evidence(compact_case):
    import json

    from agent_py_agent.agent.conversation import ConversationStore
    from agent_py_agent.agent.conversation.compact_checkpoint import hydrate_compact_checkpoint

    summary = "s" * 750000
    thread, _, _ = _commit_transcript(
        compact_case, scope=THREAD_COMPACT_SCOPE, summary=summary, content="source", publish=True,
    )
    path = compact_case.store.storage.thread_path(thread.thread_id)
    assert path.stat().st_size < 1024 * 1024
    reopened = ConversationStore(compact_case.root / "conversations")
    loaded = reopened.threads.require(thread.thread_id)
    assert loaded.summary == summary
    assert loaded.compact_operation_evidence == {"summary": summary}
    updated = reopened.threads.update_atomic(
        thread.thread_id, lambda current: replace(current, title="ordinary update after compact"),
    )
    assert updated.summary == summary and updated.compact_operation_evidence == loaded.compact_operation_evidence
    assert path.stat().st_size < 1024 * 1024
    checkpoint = json.loads((compact_case.root / "compact" / "conversations" / f"{thread.thread_id}.jsonl").read_text().splitlines()[-1])
    checkpoint = hydrate_compact_checkpoint(compact_case.agent, checkpoint)
    assert checkpoint["summary"] == summary and checkpoint["operation_evidence"] == {"summary": summary}


@pytest.mark.parametrize("extra", [0, 1])
def test_header_byte_limit_is_exact_and_failure_never_calls_updater(tmp_path, extra):
    limit = 1024 * 1024
    path = tmp_path / "header.json"
    path.write_bytes(b'{"value":"' + b'x' * (limit - 12 + extra) + b'"}')
    before = path.read_bytes()
    calls = []

    def update(value):
        calls.append(value)
        return {"changed": True}

    if extra:
        with pytest.raises(ValueError, match="bounded read limit"):
            update_json_file_atomic(path, update)
        assert not calls and path.read_bytes() == before
    else:
        assert update_json_file_atomic(path, update) == {"changed": True}
        assert len(calls) == 1


def test_header_symlink_is_not_followed_or_overwritten(tmp_path):
    target = tmp_path / "target.json"
    target.write_text('{"protected":true}')
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(OSError):
        update_json_file_atomic(link, lambda _: pytest.fail("symlink updater ran"))
    assert link.is_symlink() and target.read_text() == '{"protected":true}'


@pytest.mark.parametrize("evidence", [{}, {"summary": "distinct", "extra": {"retain": [1, 2]}}])
def test_thread_evidence_roundtrip_preserves_nonduplicate_and_unknown_fields(evidence):
    from agent_py_agent.agent.conversation.models import ConversationThread

    thread = ConversationThread(thread_id="thread", canonical_user_id="owner", summary="different", compact_operation_evidence=evidence)
    assert ConversationThread.from_dict(thread.to_dict()).compact_operation_evidence == evidence


def test_duplicate_evidence_is_lossless_without_mutating_logical_input():
    from agent_py_agent.agent.conversation.models import ConversationThread

    summary = "shared" * 1000
    evidence = {"summary": summary, "operations": [{"id": "preserve"}]}
    thread = ConversationThread("thread", "owner", summary=summary, compact_operation_evidence=evidence)
    payload = thread.to_dict()
    assert payload["compact_operation_evidence"]["summary"] != summary
    assert thread.compact_operation_evidence == evidence == {"summary": summary, "operations": [{"id": "preserve"}]}
    assert ConversationThread.from_dict(payload).compact_operation_evidence == evidence
    payload["summary"] = "changed independently"
    with pytest.raises(ValueError, match="summary reference"):
        ConversationThread.from_dict(payload)
