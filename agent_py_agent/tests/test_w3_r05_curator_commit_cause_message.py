"""R-05: CURATOR_COMMIT_FAILED with a bare ValueError keeps a redacted cause message next to cause_type."""
from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.memory_store.curator import (
    CuratorBatchCommitError,
    _failure_diagnostic,
    _failure_diagnostic_warning,
)
from agent_py_agent.tests.test_curator_failure_attribution import (
    _conversation,
    _diagnostic,
    _ResponseBackend,
    _service,
    _valid_text,
)

_TOKEN = "Bearer abcdefghijklmnopqrstuvwxyz0123456789"


def test_commit_value_error_records_redacted_cause_message(tmp_path: Path) -> None:
    store, thread, messages = _conversation(tmp_path)
    ids = [item.message_id for item in messages]
    backend = _ResponseBackend([
        ModelResponse(text=_valid_text(thread.thread_id, ids), backend="scripted-curator", stop_reason="stop"),
    ])
    service = _service(tmp_path, backend, store)
    write_target = service.committer._write_target

    def reject_second_target(path: Path, content: str) -> None:
        if service.committer._write_target is reject_second_target and path.name.endswith(".jsonl"):
            raise ValueError(f"daily event shape invalid for {path} ({_TOKEN})")
        write_target(path, content)

    service.committer._write_target = reject_second_target
    failed = service.run(reason="admin")
    assert failed.failure_code == "CURATOR_COMMIT_FAILED"
    record = service.run_log.list()[0]
    diagnostic = _diagnostic(record)
    assert diagnostic["cause_type"] == "ValueError"
    assert diagnostic["cause_message"].startswith("daily event shape invalid for <path>")
    joined = "\n".join(record.warnings)
    assert str(tmp_path) not in joined and "abcdefghijklmnop" not in joined
    service.committer._write_target = write_target
    assert service.run(reason="admin").status == "succeeded"


def test_cause_message_is_trimmed_before_dropping_the_shape() -> None:
    try:
        raise CuratorBatchCommitError("curator batch commit failed") from ValueError("x" * 400)
    except CuratorBatchCommitError as exc:
        diagnostic = _failure_diagnostic(exc)
    assert len(diagnostic["cause_message"]) == 120
    text = _failure_diagnostic_warning({**diagnostic, "message": "m" * 200})
    parsed = json.loads(text.split("=", 1)[1])
    assert len(text) <= 300 and parsed["cause_type"] == "ValueError" and parsed["cause_message"].startswith("x")


def test_cause_message_is_not_repeated_when_the_outer_message_already_says_it() -> None:
    try:
        raise RuntimeError("模型调用失败：服务商需要会话编号") from ValueError("服务商需要会话编号")
    except RuntimeError as exc:
        diagnostic = _failure_diagnostic(exc)
    assert diagnostic["cause_type"] == "ValueError" and "cause_message" not in diagnostic
