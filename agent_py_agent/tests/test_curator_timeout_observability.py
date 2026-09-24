from __future__ import annotations

"""Curator 失败路径的"调用形状"可观测性(真机 2026-09-13 归因缺陷)。

真机事实: owner `local/main` 两轮策展 run(run-1cd8134506ec… 03:48:32→03:56:36,run-a049d8f38e48…
04:29:19→04:37:28)都是 status=failed / failure_code=CURATOR_MODEL_FAILED,
warnings 只有 ['failure_diagnostic={"error_type":"ProviderTimeoutError"}']——看不出打了几次调用、
每次喂了多大的输入、授权多少秒、实际等了多久、有没有真的缩批。缩批计数 `shrink_attempts` 只在
成功路径(CURATOR_MODEL_FAILED 时压根不落盘),所以"超时缩批修复在真机上到底跑没跑"无法证明。

本测试锁定补上的口径:失败路径也按"每次尝试一条 warning"落盘**无正文**形状——
attempt / prompt_chars / schema_chars / granted_seconds / elapsed_ms / shrunk / outcome,
且严格沿用既有 warnings 字段(v2 键集不变、≤300 字符/条、≤32 条),绝不落 prompt/schema/正文。
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.backends.errors import ProviderTimeoutError
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.curator import (
    MemoryCuratorDependencies,
    MemoryCuratorIdentity,
    MemoryCuratorService,
    _attempt_shape_warnings,
)
from agent_py_agent.agent.memory_store.curator_backend import (
    _TIMEOUT_SHRINK_LIMIT,
    CuratorModelAttempt,
    adaptive_timeout_seconds,
    attempt_shape_payload,
    extract_with_retries,
    last_model_attempts,
    shrink_batch_for_timeout,
)
from agent_py_agent.agent.memory_store.curator_inputs import (
    CuratorInputBatch,
    CuratorMessageInput,
)
from agent_py_agent.agent.memory_store.curator_models import (
    CURATOR_OUTPUT_SCHEMA_VERSION,
    MemoryCuratorConfig,
    curator_response_schema,
)
from agent_py_agent.agent.memory_store.curator_run_log import CuratorRunLog, CuratorRunRecord
from agent_py_agent.agent.memory_store.curator_state import MemoryCuratorStateStore
from agent_py_agent.agent.memory_store.daily import DailyMemoryStore

_THREAD_ID = "thread-observability"


# 函数用途: 造一份与生产同构的 Curator 配置(timeout=1 让测试快,max_retries=2 让预算护栏远离边界)。
def _config(**overrides) -> MemoryCuratorConfig:
    values = {
        "interval_seconds": 60,
        "turn_threshold": 1,
        "batch_message_limit": 80,
        "max_input_chars": 40000,
        "timeout_seconds": 1,
        "max_retries": 2,
        "daily_finalize_hour": 23,
    }
    values.update(overrides)
    return MemoryCuratorConfig(**values)


# 函数用途: 造一条 Curator 消息输入,preview_chars 用于精确控制 prompt 规模。
def _message(message_id: str, *, preview_chars: int = 20) -> CuratorMessageInput:
    preview = "我的个人电脑使用 macOS。" * max(1, preview_chars // 10)
    return CuratorMessageInput(
        message_id=message_id,
        thread_id=_THREAD_ID,
        role="user",
        channel="internal",
        created_at=11.0,
        full_content=preview,
        content_preview=preview,
        content_hash="sha256:test",
        metadata={
            "session_id": "session-1",
            "request_id": "request-1",
            "task_id": "task-1",
            "run_id": "run-1",
        },
    )


# 函数用途: 造一批同构消息输入。
def _batch(count: int, *, preview_chars: int = 20) -> CuratorInputBatch:
    return CuratorInputBatch(
        messages=tuple(
            _message(f"message-{index:03d}", preview_chars=preview_chars)
            for index in range(1, count + 1)
        ),
        audit_events=(),
    )


# 函数用途: 按脚本逐次响应(异常实例 → 抛出;dict → 作为结构化响应返回),并记录每次的 prompt/schema。
class _ScriptedBackend:
    name = "scripted-curator"

    def __init__(self, script: list[object]) -> None:
        self.script = list(script)
        self.prompts: list[str] = []
        self.schemas: list[dict[str, object]] = []

    def generate_structured(self, prompt: str, *, response_schema: dict[str, object]):
        index = len(self.prompts)
        self.prompts.append(prompt)
        self.schemas.append(response_schema)
        item = self.script[min(index, len(self.script) - 1)]
        if isinstance(item, BaseException):
            raise item
        return ModelResponse(text=json.dumps(item, ensure_ascii=False), backend=self.name)


# 函数用途: 造一份只引用指定 message_id 的合法 Curator 输出。
def _valid_output(thread_id: str, message_ids: list[str]) -> dict[str, object]:
    return {
        "schema_version": CURATOR_OUTPUT_SCHEMA_VERSION,
        "daily_events": [],
        "candidates": [],
        "processed_message_refs": [{"message_id": mid} for mid in message_ids],
        "processed_audit_refs": [],
        "unresolved_refs": [],
        "warnings": [],
        "next_cursor": {
            "per_thread_cursors": [
                {"thread_id": thread_id, "message_id": message_ids[-1]}
            ],
            "last_audit_event_id": None,
        },
    }


# 函数用途: 造一个真实 ConversationStore 线程和若干条用户消息。
def _conversation(tmp_path: Path, count: int):
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
                "metadata": {
                    "session_id": "session-1",
                    "request_id": f"request-{index + 1}",
                    "task_id": "task-1",
                    "run_id": "run-1",
                },
                "now": 11.0 + index,
            }
        )
        for index in range(count)
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
            state_store=MemoryCuratorStateStore(
                tmp_path / "memory" / "curator" / "state.json"
            ),
            daily_store=DailyMemoryStore(tmp_path / "memory" / "daily"),
            candidate_service=CandidateService(tmp_path / "memory" / "candidates.jsonl"),
            run_log=CuratorRunLog(tmp_path / "memory" / "curator" / "runs"),
            identity=MemoryCuratorIdentity(
                provider="scripted-curator",
                model="curator-observability-test",
            ),
        ),
    )


# 函数用途: 解析一条 curator_model_attempt= warning,读账的人靠它做机器判断。
def _parse_attempt_warnings(warnings: tuple[str, ...]) -> list[dict[str, object]]:
    rows = [item for item in warnings if item.startswith("curator_model_attempt=")]
    return [json.loads(item.split("=", 1)[1]) for item in rows]


# 函数用途: (1) 失败路径此前只看得到异常类名;现在每次尝试的形状都在 warnings 里,且逐次缩小。
def test_failed_run_records_per_attempt_shape_in_warnings(tmp_path: Path) -> None:
    store, _thread, messages = _conversation(tmp_path, 8)
    backend = _ScriptedBackend([ProviderTimeoutError("idle", stage="stream_idle")])
    service = _service(tmp_path, backend, store)

    result = service.run(reason="admin")

    assert result.status == "failed"
    assert len(backend.prompts) == 1 + _TIMEOUT_SHRINK_LIMIT
    records = service.run_log.list()
    assert [record.failure_code for record in records] == ["CURATOR_MODEL_FAILED"]
    shapes = _parse_attempt_warnings(records[0].warnings)

    # 每次调用一条: 序号连续、结果类名是供应商异常、异常正文绝不入账。
    assert [row["attempt"] for row in shapes] == [1, 2, 3, 4]
    assert {row["outcome"] for row in shapes} == {"ProviderTimeoutError"}
    assert len(records[0].warnings) == len(shapes) + 1  # + 既有 failure_diagnostic=
    assert records[0].warnings[-1] == (
        'failure_diagnostic={"error_type":"ProviderTimeoutError","message":"idle"}'
    )
    # prompt 逐次变小(缩批真的发生了),第一次未缩批、之后都标 shrunk=True。
    assert [row["prompt_chars"] for row in shapes] == sorted(
        (row["prompt_chars"] for row in shapes), reverse=True
    )
    assert shapes[0]["prompt_chars"] > shapes[-1]["prompt_chars"]
    assert [row["shrunk"] for row in shapes] == [False, True, True, True]
    # 授权秒数必须与自适应公式一致,且实际耗时不会超过授权(否则就是超时判定本身坏了)。
    assert [row["granted_seconds"] for row in shapes] == [
        adaptive_timeout_seconds(1, len(prompt)) for prompt in backend.prompts
    ]
    assert all(row["elapsed_ms"] <= row["granted_seconds"] * 1000 for row in shapes)
    assert all(row["schema_chars"] == len(json.dumps(curator_response_schema())) for row in shapes)
    # 无正文: 消息、prompt、schema 正文都不在 warnings 文本里。
    joined = "\n".join(records[0].warnings)
    assert messages[0].message_id not in joined
    assert "个人电脑使用 macOS" not in joined
    assert "$defs" not in joined and "additionalProperties" not in joined
    # v2 严格键集不变(新增 dataclass 字段会让历史行读不回来)。
    assert set(records[0].to_record()) == set(CuratorRunRecord.__dataclass_fields__)


# 函数用途: (2) 成功运行不新增尝试形状 warning——成功路径的既有账形状逐字不变。
def test_successful_run_keeps_existing_warning_shape(tmp_path: Path) -> None:
    store, thread, messages = _conversation(tmp_path, 2)
    backend = _ScriptedBackend(
        [_valid_output(thread.thread_id, [item.message_id for item in messages])]
    )
    service = _service(tmp_path, backend, store)

    result = service.run(reason="admin")

    assert result.status == "succeeded"
    assert result.warnings == ()
    assert _parse_attempt_warnings(service.run_log.list()[0].warnings) == []


# 函数用途: (3) 非超时失败(同输入重试)同样留下逐次形状,并且每次 prompt 一样大、shrunk 全 False;
# 供应商调用阶段抛出的 ValueError(真机 2026-09-13 起:请求头缺宿主会话)按阶段归 CURATOR_MODEL_FAILED,
# 结果、state.json 与失败诊断三处一致,不再冒充 schema 错误;诊断附脱敏正文以便归因。
def test_non_timeout_failure_records_identical_prompt_shapes(tmp_path: Path) -> None:
    store, _thread, _messages = _conversation(tmp_path, 4)
    backend = _ScriptedBackend([ValueError("此服务商需要会话编号，但当前请求没有绑定宿主会话。")])
    service = _service(tmp_path, backend, store)

    result = service.run(reason="admin")

    assert result.status == "failed"
    assert result.failure_code == "CURATOR_MODEL_FAILED"
    assert service.state_store.load().last_failure_code == "CURATOR_MODEL_FAILED"
    assert len(backend.prompts) == 3  # max_retries + 1
    records = service.run_log.list()
    shapes = _parse_attempt_warnings(records[0].warnings)

    assert records[0].failure_code == "CURATOR_MODEL_FAILED"
    assert [row["outcome"] for row in shapes] == ["ValueError"] * 3
    assert len({row["prompt_chars"] for row in shapes}) == 1
    assert {row["shrunk"] for row in shapes} == {False}
    assert records[0].warnings[-1] == (
        'failure_diagnostic={"error_type":"CuratorModelCallError",'
        '"message":"此服务商需要会话编号，但当前请求没有绑定宿主会话。"}'
    )


# 函数用途: (4) 形状必须带出 extract_with_retries 的成功调用(缩批后成功也能看出缩了几次)。
def test_successful_attempt_shape_is_readable_after_shrink(monkeypatch) -> None:
    from agent_py_agent.agent.memory_store import curator_backend

    def fake_call(backend, *, prompt, response_schema, timeout_seconds):
        del backend, response_schema, timeout_seconds
        if len(prompt) > 15_000:
            raise ProviderTimeoutError("idle", stage="stream_idle")
        # 缩批后的合法输出必须恰好覆盖缩小后的清单(否则是宿主 schema 校验失败,不是超时路径)。
        shrunk = shrink_batch_for_timeout(batch)
        return SimpleNamespace(
            text=json.dumps(
                _valid_output(_THREAD_ID, [item.message_id for item in shrunk.messages]),
                ensure_ascii=False,
            )
        )

    monkeypatch.setattr(curator_backend, "call_backend_with_timeout", fake_call)
    # 32 条 600 字符预览 ≈ 36K 字符输入:两次超时逐级缩到 12.8K 后成功,形状里能看出三步。
    # timeout_seconds 取大值让 lease 预算护栏(见 extract_with_retries 的 budget_seconds)远离
    # 被测边界,断言才只反映缩批语义而不是"预算提前收口"。
    batch = _batch(32, preview_chars=400)
    config = _config(timeout_seconds=30, max_retries=2)

    before = last_model_attempts()
    attempt = extract_with_retries(object(), config, batch)
    shapes = last_model_attempts()

    assert attempt.shrink_attempts == 2
    assert len(shapes) == 3
    assert [row.outcome for row in shapes] == [
        "ProviderTimeoutError",
        "ProviderTimeoutError",
        "ok",
    ]
    assert [row.shrunk for row in shapes] == [False, True, True]
    assert [row.attempt for row in shapes] == [1, 2, 3]
    assert [row.prompt_chars for row in shapes] == sorted(
        (row.prompt_chars for row in shapes), reverse=True
    )
    # 形状以本次调用为单位整体替换:读到的是刚跑完这一批的输入规模,不是同线程上一次运行的残影。
    assert [row.prompt_chars for row in shapes] != [row.prompt_chars for row in before]


# 函数用途: (5) 形状 warning 的编码是有界的、键集固定的,不会撑爆 run 账 warnings 上限。
def test_attempt_shape_warning_is_bounded_and_allowlisted() -> None:
    attempt = CuratorModelAttempt(
        attempt=7,
        prompt_chars=40_000,
        schema_chars=4_723,
        granted_seconds=720,
        elapsed_ms=719_999,
        shrunk=True,
        outcome="ProviderTimeoutError",
    )

    warnings = _attempt_shape_warnings((attempt,) * 40)

    assert len(warnings) == 32  # 与 curator_run_log 的 warnings 条数上限一致
    assert all(len(item) <= 300 for item in warnings)  # 与单条长度上限一致
    payload = json.loads(warnings[0].split("=", 1)[1])
    assert set(payload) == {
        "attempt",
        "prompt_chars",
        "schema_chars",
        "granted_seconds",
        "elapsed_ms",
        "shrunk",
        "outcome",
    }
    assert payload == {
        "attempt": 7,
        "prompt_chars": 40_000,
        "schema_chars": 4_723,
        "granted_seconds": 720,
        "elapsed_ms": 719_999,
        "shrunk": True,
        "outcome": "ProviderTimeoutError",
    }
    # 编码只吃标量: 任何"正文"都不可能通过这个入口进账。
    assert all(isinstance(value, (int, bool, str)) for value in payload.values())
    assert attempt_shape_payload(attempt) == payload


# 函数用途: (6) 形状条目只对应真实发生过的调用:模型从没被调用时,账上不能出现"尝试过"的假事实。
def test_attempt_shapes_track_actual_calls_only(tmp_path: Path) -> None:
    store, _thread, _messages = _conversation(tmp_path, 4)
    backend = _ScriptedBackend([ProviderTimeoutError("idle", stage="stream_idle")])
    service = _service(tmp_path, backend, store)

    result = service.run(reason="admin")

    assert result.status == "failed"
    records = service.run_log.list()
    shapes = _parse_attempt_warnings(records[0].warnings)
    # 逐次形状条数与后端真实被调用的次数严格一致(不凭猜测补条目)。
    assert len(shapes) == len(backend.prompts)
    assert len(shapes) > 0
    assert records[0].warnings[-1] == (
        'failure_diagnostic={"error_type":"ProviderTimeoutError","message":"idle"}'
    )


# 函数用途: (8) 失败时同时打一行无正文日志:真机定位不必等下一轮读账,且日志里不能出现正文。
def test_failed_attempts_emit_bodiless_warning_log(caplog) -> None:
    import logging

    backend = _ScriptedBackend([ProviderTimeoutError("idle", stage="stream_idle")])
    config = _config(timeout_seconds=5, max_retries=2)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(ProviderTimeoutError):
            extract_with_retries(backend, config, _batch(8))

    messages = [record.getMessage() for record in caplog.records]
    assert any("memory curator model calls failed" in item for item in messages)
    joined = "\n".join(messages)
    assert "prompt_chars=" in joined and "schema_chars=" in joined
    assert "outcome=ProviderTimeoutError" in joined
    # 只记标量与类名: prompt 正文、schema 关键字、消息内容都不进日志。
    assert "我的个人电脑使用 macOS" not in joined
    assert "additionalProperties" not in joined
    assert backend.prompts[0] not in joined


# 函数用途: (7) 空批在 extract_with_retries 之前就短路成成功——真机账上的 msgs=0 不能当成"空批失败"。
def test_empty_batch_succeeds_before_any_model_call(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    backend = _ScriptedBackend([ProviderTimeoutError("idle", stage="stream_idle")])
    service = _service(tmp_path, backend, store)

    result = service.run(reason="admin")

    assert result.status == "succeeded"
    assert backend.prompts == []
    assert _parse_attempt_warnings(service.run_log.list()[0].warnings) == []
    store = ConversationStore(tmp_path / "conversations")
    backend = _ScriptedBackend([ProviderTimeoutError("idle", stage="stream_idle")])
    service = _service(tmp_path, backend, store)

    result = service.run(reason="admin")

    assert result.status == "succeeded"
    assert backend.prompts == []
    assert _parse_attempt_warnings(service.run_log.list()[0].warnings) == []
