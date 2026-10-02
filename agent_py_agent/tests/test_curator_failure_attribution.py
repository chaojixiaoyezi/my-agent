from __future__ import annotations

"""Curator 失败账的根因归因(真机 2026-09-26~28 主 owner 无法归因的两类失败)。

真机事实: owner `local/main` 自 09-26 起 8 次 CURATOR_SCHEMA_INVALID,诊断只有
`{"error_type":"ValueError","message":"curator response is not strict JSON"}`,调用形状 outcome=ok——
分不清是输出被截断、上游回了空内容,还是中途格式坏;3 次 CURATOR_COMMIT_FAILED 只剩
`curator batch commit failed`,被包住的 OSError 连同 errno 都丢了(其中一次前 43 秒系统报 VQ_VERYLOWDISK)。

本测试锁定补上的口径: 失败码、重试语义、run 账键集都不变,只在既有 failure_diagnostic 里追加无正文标量——
解析失败带 response_chars / truncated / stop_reason / output_tokens 与 JSONDecodeError 出错位置,
包装异常带根因类名与 errno;整条仍 ≤300 字符、可解析,响应正文、路径、记忆内容一概不入账。
"""

import errno
import json
from pathlib import Path

import pytest

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.curator import (
    MemoryCuratorDependencies,
    MemoryCuratorIdentity,
    MemoryCuratorService,
    _failure_diagnostic,
    _failure_diagnostic_warning,
)
from agent_py_agent.agent.memory_store.curator_backend import _response_facts
from agent_py_agent.agent.memory_store.curator_models import (
    CURATOR_OUTPUT_SCHEMA_VERSION,
    MemoryCuratorConfig,
)
from agent_py_agent.agent.memory_store.curator_run_log import CuratorRunLog, CuratorRunRecord
from agent_py_agent.agent.memory_store.curator_state import MemoryCuratorStateStore
from agent_py_agent.agent.memory_store.daily import DailyMemoryStore

_TRUNCATED_TEXT = '{"schema_version": "' + CURATOR_OUTPUT_SCHEMA_VERSION + '", "daily_events": ['


# 函数用途: 造一份与生产同构的 Curator 配置(与 test_curator_timeout_observability 同口径)。
def _config() -> MemoryCuratorConfig:
    return MemoryCuratorConfig(
        interval_seconds=60,
        turn_threshold=1,
        batch_message_limit=80,
        max_input_chars=40000,
        timeout_seconds=1,
        max_retries=2,
        daily_finalize_hour=23,
    )


# 函数用途: 按脚本逐次返回 ModelResponse(可带 truncated/stop_reason/usage),并记录调用次数。
class _ResponseBackend:
    name = "scripted-curator"

    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = list(responses)
        self.calls = 0

    def generate_structured(self, prompt: str, *, response_schema: dict[str, object]) -> ModelResponse:
        del prompt, response_schema
        self.calls += 1
        return self.responses[min(self.calls, len(self.responses)) - 1]


# 函数用途: 造一份只引用指定 message_id 的合法 Curator 输出文本。
def _valid_text(thread_id: str, message_ids: list[str]) -> str:
    return json.dumps(
        {
            "schema_version": CURATOR_OUTPUT_SCHEMA_VERSION,
            "daily_events": [],
            "candidates": [],
            "processed_message_refs": [{"message_id": mid} for mid in message_ids],
            "processed_audit_refs": [],
            "unresolved_refs": [],
            "warnings": [],
            "next_cursor": {
                "per_thread_cursors": [{"thread_id": thread_id, "message_id": message_ids[-1]}],
                "last_audit_event_id": None,
            },
        },
        ensure_ascii=False,
    )


# 函数用途: 造一个真实 ConversationStore 线程和两条用户消息。
def _conversation(tmp_path: Path):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    messages = [
        store.messages.append(
            {
                "thread_id": thread.thread_id,
                "role": "user",
                "content": f"第{index + 1}条：我的个人电脑使用 macOS。",
                "channel": "internal",
                "metadata": {"session_id": "session-1", "request_id": f"request-{index + 1}"},
                "now": 11.0 + index,
            }
        )
        for index in range(2)
    ]
    return store, thread, messages


# 函数用途: 造一个使用真实 state/daily/candidate/run-log 权威的 Curator 服务。
def _service(tmp_path: Path, backend: object, store: ConversationStore) -> MemoryCuratorService:
    return MemoryCuratorService(
        config=_config(),
        dependencies=MemoryCuratorDependencies(
            backend=backend,
            conversation_store=store,
            audit_dir=tmp_path / "audit",
            state_store=MemoryCuratorStateStore(tmp_path / "memory" / "curator" / "state.json"),
            daily_store=DailyMemoryStore(tmp_path / "memory" / "daily"),
            candidate_service=CandidateService(tmp_path / "memory" / "candidates.jsonl"),
            run_log=CuratorRunLog(tmp_path / "memory" / "curator" / "runs"),
            identity=MemoryCuratorIdentity(provider="scripted-curator", model="attribution-test"),
        ),
    )


# 函数用途: 取出运行账最后一条 failure_diagnostic= 并解析成字典,读账方按同一口径切分。
def _diagnostic(record: CuratorRunRecord) -> dict[str, object]:
    [item] = [value for value in record.warnings if value.startswith("failure_diagnostic=")]
    assert len(item) <= 300
    return json.loads(item.split("=", 1)[1])


# 函数用途: (1) 截断的响应仍归 schema 错误、不重试,但诊断能看出"上游说到长度上限了、文本在末尾断开"。
def test_truncated_response_records_shape_without_body(tmp_path: Path) -> None:
    store, _thread, messages = _conversation(tmp_path)
    backend = _ResponseBackend([
        ModelResponse(text=_TRUNCATED_TEXT, backend="scripted-curator", truncated=True,
                      stop_reason="length", usage={"prompt_tokens": 9000, "completion_tokens": 8192}),
    ])
    service = _service(tmp_path, backend, store)

    result = service.run(reason="admin")

    assert result.status == "failed"
    assert result.failure_code == "CURATOR_SCHEMA_INVALID"
    assert backend.calls == 1  # 解析失败的语义不变:不同输入重试、不缩批
    assert service.state_store.load().per_thread_cursors == {}
    [record] = service.run_log.list()
    assert _diagnostic(record) == {
        "error_type": "CuratorResponseParseError",
        "message": "curator response is not strict JSON",
        "cause_type": "JSONDecodeError",
        "cause_pos": len(_TRUNCATED_TEXT),
        "response_chars": len(_TRUNCATED_TEXT),
        "truncated": True,
        "stop_reason": "length",
        "output_tokens": 8192,
        "violation_code": "json_invalid",
        "violation_path": "$",
    }
    joined = "\n".join(record.warnings)
    assert "daily_events" not in joined and "macOS" not in joined
    assert messages[0].message_id not in joined
    assert set(record.to_record()) == set(CuratorRunRecord.__dataclass_fields__)


# 函数用途: (2) 上游回了空内容:字符数 0、出错位置 0、未截断,与截断和中途格式坏可以区分开。
def test_empty_response_is_distinguishable_from_truncation(tmp_path: Path) -> None:
    store, _thread, _messages = _conversation(tmp_path)
    backend = _ResponseBackend([
        ModelResponse(text="", backend="scripted-curator", stop_reason="stop", usage={"output_tokens": 0}),
    ])
    service = _service(tmp_path, backend, store)

    result = service.run(reason="admin")

    assert result.failure_code == "CURATOR_SCHEMA_INVALID"
    diagnostic = _diagnostic(service.run_log.list()[0])
    assert (diagnostic["response_chars"], diagnostic["cause_pos"], diagnostic["truncated"]) == (0, 0, False)
    assert diagnostic["stop_reason"] == "stop"
    assert diagnostic["output_tokens"] == 0


# 函数用途: (3) 正向对照:同一假后端下一轮给合法输出就成功提交,游标从同一起点推进,失败批次不丢。
def test_failed_parse_batch_is_retried_and_committed_next_run(tmp_path: Path) -> None:
    store, thread, messages = _conversation(tmp_path)
    ids = [item.message_id for item in messages]
    backend = _ResponseBackend([
        ModelResponse(text=_TRUNCATED_TEXT, backend="scripted-curator", truncated=True, stop_reason="length"),
        ModelResponse(text=_valid_text(thread.thread_id, ids), backend="scripted-curator", stop_reason="stop"),
    ])
    service = _service(tmp_path, backend, store)

    failed = service.run(reason="admin")
    retried = service.run(reason="admin")

    assert (failed.status, retried.status) == ("failed", "succeeded")
    assert service.state_store.load().per_thread_cursors == {thread.thread_id: ids[-1]}
    success = service.run_log.list()[-1]
    assert success.processed_messages == len(ids)
    assert not [item for item in success.warnings if item.startswith("failure_diagnostic=")]


# 函数用途: (4) 提交时磁盘满:仍是 CURATOR_COMMIT_FAILED 且整批回滚,诊断带出被包住的 OSError 与 errno=28,
# 不带文件路径;恢复写入后重做一次即成功(正向对照)。
def test_commit_disk_full_records_root_cause_errno(tmp_path: Path) -> None:
    store, thread, messages = _conversation(tmp_path)
    ids = [item.message_id for item in messages]
    backend = _ResponseBackend([
        ModelResponse(text=_valid_text(thread.thread_id, ids), backend="scripted-curator", stop_reason="stop"),
    ])
    service = _service(tmp_path, backend, store)
    write_target = service.committer._write_target
    writes = 0

    def disk_full_on_second_write(path: Path, content: str) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError(errno.ENOSPC, "No space left on device", str(path))
        write_target(path, content)

    service.committer._write_target = disk_full_on_second_write
    failed = service.run(reason="admin")

    assert failed.failure_code == "CURATOR_COMMIT_FAILED"
    assert service.state_store.load().per_thread_cursors == {}
    record = service.run_log.list()[0]
    assert _diagnostic(record) == {
        "error_type": "CuratorBatchCommitError",
        "message": "curator batch commit failed",
        "cause_type": "OSError",
        "cause_errno": errno.ENOSPC,
    }
    assert str(tmp_path) not in "\n".join(record.warnings)

    service.committer._write_target = write_target
    assert service.run(reason="admin").status == "succeeded"
    assert service.state_store.load().per_thread_cursors == {thread.thread_id: ids[-1]}


# 函数用途: (5) 没有包装的异常不凭空长出根因键;根因只沿显式 from 链找,隐式上下文不算。
def test_cause_facts_follow_explicit_chain_only() -> None:
    assert _failure_diagnostic(RuntimeError("boom")) == {"error_type": "RuntimeError", "message": "boom"}
    try:
        try:
            raise OSError(errno.EACCES, "denied")
        except OSError:
            raise RuntimeError("implicit context only") from None
    except RuntimeError as exc:
        assert "cause_type" not in _failure_diagnostic(exc)
    try:
        try:
            raise FileNotFoundError(errno.ENOENT, "gone")
        except OSError as inner:
            raise RuntimeError("middle") from inner
    except RuntimeError as middle:
        wrapped = ValueError("outer")
        wrapped.__cause__ = middle
    diagnostic = _failure_diagnostic(wrapped)
    assert (diagnostic["cause_type"], diagnostic["cause_errno"]) == ("FileNotFoundError", errno.ENOENT)


# 函数用途: (6) 响应形状只收短码和整数:非码 stop_reason 记 other,布尔不当 token 数,正文只算长度。
def test_response_facts_keep_only_bounded_scalars() -> None:
    facts = _response_facts(
        ModelResponse(text="x" * 50, backend="b", stop_reason="stopped because of a very long reason",
                      usage={"completion_tokens": True, "output_tokens": 12}),
        "x" * 50,
    )

    assert facts == {"response_chars": 50, "truncated": False, "stop_reason": "other", "output_tokens": 12}
    assert _response_facts(ModelResponse(text="", backend="b"), "") == {"response_chars": 0, "truncated": False}


# 函数用途: (7) 事实全带上仍 ≤300 字符;类名异常长时退回只含类名的最小形状,绝不让失败记账本身失败。
def test_diagnostic_warning_stays_bounded() -> None:
    diagnostic = {
        "error_type": "CuratorResponseParseError",
        "message": "m" * 200,
        "cause_type": "JSONDecodeError",
        "cause_pos": 123456789,
        "response_chars": 123456789,
        "truncated": True,
        "stop_reason": "s" * 40,
        "output_tokens": 123456789,
    }
    text = _failure_diagnostic_warning(diagnostic)
    parsed = json.loads(text.split("=", 1)[1])
    assert len(text) <= 300
    assert {key: value for key, value in parsed.items() if key != "message"} == {
        key: value for key, value in diagnostic.items() if key != "message"
    }

    huge = _failure_diagnostic_warning({**diagnostic, "error_type": "E" * 400})
    assert len(huge) <= 300
    assert json.loads(huge.split("=", 1)[1]) == {"error_type": "E" * 200}


# 函数用途: 造一条除了被改的字段外都合合同的候选（引用第一条消息）。
def _candidate(message_id: str, **overrides: object) -> dict[str, object]:
    return {"candidate_type": "long_term_fact", "content": "用户的个人电脑使用 macOS", "subject_key": "fact.os",
            "scope": {"scope_type": "personal", "scope_key": "personal", "applies_when": "", "excludes_when": ""},
            "origin": "user_explicit", "source_message_refs": [{"message_id": message_id}], "source_tool_refs": [],
            "source_artifact_refs": [], "observed_at": None, "valid_from": None, "valid_until": None,
            "confidence": 0.9, "proposed_action": "add", "target_entry_id": None, "conflicts_with": [],
            "promotion_target": "long_term", **overrides}


# 函数用途: (8) 输出不合 schema 时，失败诊断带宿主写死的违规码和只由 schema 字段名/下标拼成的路径，不带正文。
def test_schema_violation_records_code_and_field_path(tmp_path: Path) -> None:
    store, thread, messages = _conversation(tmp_path)
    ids = [item.message_id for item in messages]
    payload = json.loads(_valid_text(thread.thread_id, ids))
    payload["candidates"] = [_candidate(ids[0], confidence=1.5)]
    backend = _ResponseBackend([ModelResponse(text=json.dumps(payload, ensure_ascii=False), backend="b")])
    service = _service(tmp_path, backend, store)

    result = service.run(reason="admin")

    assert (result.status, result.failure_code) == ("failed", "CURATOR_SCHEMA_INVALID")
    [record] = service.run_log.list()
    diagnostic = _diagnostic(record)
    assert (diagnostic["violation_code"], diagnostic["violation_path"]) == ("above_maximum", "$.candidates[0].confidence")
    assert "macOS" not in "\n".join(record.warnings), "正文不进运行记录"


# 函数用途: (9) 模型自己写的多余键名不进结构化路径（只到所在对象）；缺的字段名来自 schema，路径带上它。
@pytest.mark.parametrize("change,expected", [
    ("extra", ("unknown_property", "$.candidates[0]")),
    ("missing", ("missing_property", "$.candidates[0].scope")),
])
def test_unknown_or_missing_nested_key_paths(tmp_path: Path, change: str, expected: tuple[str, str]) -> None:
    store, thread, messages = _conversation(tmp_path)
    ids = [item.message_id for item in messages]
    payload = json.loads(_valid_text(thread.thread_id, ids))
    candidate = _candidate(ids[0], zz_model_key="x") if change == "extra" else _candidate(ids[0])
    if change == "missing":
        del candidate["scope"]
    payload["candidates"] = [candidate]
    service = _service(tmp_path, _ResponseBackend([ModelResponse(text=json.dumps(payload), backend="b")]), store)

    service.run(reason="admin")

    diagnostic = _diagnostic(service.run_log.list()[0])
    assert (diagnostic["violation_code"], diagnostic["violation_path"]) == expected


# 函数用途: (10) 证据不合格沿用已有的 detail_code；包装异常也能沿 __cause__ 找到违规码；超长时最小形状仍留违规码和路径。
def test_violation_facts_follow_the_cause_chain_and_survive_the_bound() -> None:
    from agent_py_agent.agent.memory_store.curator_validation import CuratorEvidenceError

    assert _failure_diagnostic(CuratorEvidenceError("candidate_without_evidence"))["violation_code"] == "candidate_without_evidence"
    try:
        try:
            raise CuratorEvidenceError("daily_without_evidence")
        except CuratorEvidenceError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        assert _failure_diagnostic(outer)["violation_code"] == "daily_without_evidence"
    assert "violation_code" not in _failure_diagnostic(ValueError("plain"))
    huge = _failure_diagnostic_warning({"error_type": "E" * 400, "message": "m" * 200, "response_chars": 1,
                                        "violation_code": "c" * 40, "violation_path": "$" + "p" * 79})
    assert len(huge) <= 300
    assert json.loads(huge.split("=", 1)[1]) == {"error_type": "E" * 100, "violation_code": "c" * 40,
                                                  "violation_path": "$" + "p" * 79}
