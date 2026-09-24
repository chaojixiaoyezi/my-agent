from __future__ import annotations

"""Curator 超时 → 有界自适应缩批(真机 2026-09-09 记忆策展长期失败的真机缺陷修复)。

真机账(run 03:48:32Z→03:56:36Z, provider=anthropic_compatible, model=qwen3.8-flash):
status=failed, failure_code=CURATOR_MODEL_FAILED,
warnings=['failure_diagnostic={"error_type":"ProviderTimeoutError"}']。同一 profile 的小 prompt
手工 generate_structured 秒级成功,所以根因是"大批量输入 + 固定等待上限",不是 schema、不是鉴权。
本测试锁定修复口径:超时后**缩小本次输入**再试(绝不加长 timeout、绝不关闭超时)、缩批步数
独立有界且不污染 config.max_retries、到下限即按原语义 typed 失败、游标与事务契约不变。
"""

import json
import time
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
)
from agent_py_agent.agent.memory_store.curator_backend import (
    _TIMEOUT_SHRINK_LIMIT,
    CuratorModelCallError,
    CuratorModelTimeoutError,
    adaptive_timeout_seconds,
    call_backend_with_timeout,
    curator_prompt,
    extract_with_retries,
    is_curator_timeout_error,
    last_model_attempts,
    shrink_batch_for_timeout,
)
from agent_py_agent.agent.memory_store.curator_inputs import (
    CuratorAuditInput,
    CuratorInputBatch,
    CuratorMessageInput,
)
from agent_py_agent.agent.memory_store.curator_models import (
    CURATOR_OUTPUT_SCHEMA_VERSION,
    MemoryCuratorConfig,
    curator_response_schema,
)
from agent_py_agent.agent.memory_store.curator_run_log import CuratorRunLog
from agent_py_agent.agent.memory_store.curator_state import MemoryCuratorStateStore
from agent_py_agent.agent.memory_store.daily import DailyMemoryStore

_THREAD_ID = "thread-1"
# LLM: 缩批测试统一使用"预算余量充足"的配置:timeout_seconds=1 让测试快,max_retries=2 让
# lease 预算护栏(见 extract_with_retries 的 budget_seconds)远离被测边界,断言才只反映缩批语义。
_SHRINK_CONFIG = {"timeout_seconds": 1, "max_retries": 2}


def _config(**overrides) -> MemoryCuratorConfig:
    values = {
        "interval_seconds": 60,
        "turn_threshold": 1,
        "batch_message_limit": 80,
        "max_input_chars": 40000,
        "timeout_seconds": 90,
        "max_retries": 1,
        "daily_finalize_hour": 23,
    }
    values.update(overrides)
    return MemoryCuratorConfig(**values)


# LLM: 测试内的输入对象必须与生产同构(不能只造 SimpleNamespace),否则缩批/清单/length 断言
# 会脱离真实 prompt 结构。
# 函数用途: 造一条 Curator 消息输入,preview_chars 用于精确控制 prompt 规模。
def _message(
    message_id: str,
    *,
    thread_id: str = _THREAD_ID,
    role: str = "user",
    preview_chars: int = 20,
) -> CuratorMessageInput:
    preview = "我的个人电脑使用 macOS。" * max(1, preview_chars // 10)
    return CuratorMessageInput(
        message_id=message_id,
        thread_id=thread_id,
        role=role,
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


# 函数用途: 造一条 Curator audit 工具输入,用于验证消息/审计两类输入各自缩批。
def _audit(event_id: str) -> CuratorAuditInput:
    return CuratorAuditInput(
        event_id=event_id,
        event_type="tool_call",
        created_at="1970-01-01T00:00:11+00:00",
        status="ok",
        operation_id="operation-" + event_id,
        tool_call_id="call-" + event_id,
        tool_name="read_file",
        tool_success=True,
        error_code="",
        effect_outcome="",
        session_id="session-1",
        thread_id=_THREAD_ID,
        request_id="request-1",
        task_id="task-1",
        run_id="run-1",
        content_hash="sha256:tool",
        source_ref="",
        artifact_ref="",
        artifact_hash="",
        artifact_size_bytes=0,
        preview="工具预览",
    )


# 函数用途: 造一批同构消息输入。
def _batch(count: int, *, preview_chars: int = 20, audits: int = 0) -> CuratorInputBatch:
    return CuratorInputBatch(
        messages=tuple(
            _message(f"message-{index:03d}", preview_chars=preview_chars)
            for index in range(1, count + 1)
        ),
        audit_events=tuple(_audit(f"event-{index:03d}") for index in range(1, audits + 1)),
    )


# 函数用途: 按脚本逐次响应(异常实例 → 抛出;dict → 作为结构化响应返回)。
class _ScriptedBackend:
    name = "scripted-curator"

    def __init__(self, script: list[object], on_call=None) -> None:
        self.script = list(script)
        self.prompts: list[str] = []
        self.on_call = on_call

    def generate_structured(self, prompt: str, *, response_schema: dict[str, object]):
        index = len(self.prompts)
        self.prompts.append(prompt)
        if self.on_call is not None:
            self.on_call(index)
        item = self.script[min(index, len(self.script) - 1)]
        if isinstance(item, BaseException):
            raise item
        return ModelResponse(text=json.dumps(item, ensure_ascii=False), backend=self.name)


# 函数用途: 造一份只引用指定 message_id 的合法 Curator 输出(processed 覆盖恰好这些消息)。
def _valid_output(thread_id: str, message_ids: list[str]) -> dict[str, object]:
    first = {"message_id": message_ids[0]}
    return {
        "schema_version": CURATOR_OUTPUT_SCHEMA_VERSION,
        "daily_events": [
            {
                "event_type": "conversation",
                "summary": "用户说明个人电脑使用 macOS。",
                "actor": "user",
                "origin": "user_explicit",
                "message_refs": [first],
                "tool_refs": [],
                "artifact_refs": [],
                "decisions": [],
                "lessons": [],
                "next_actions": [],
            }
        ],
        "candidates": [
            {
                "candidate_type": "long_term_fact",
                "content": "个人电脑使用 macOS。",
                "subject_key": "device.personal.os",
                "scope": {
                    "scope_type": "personal",
                    "scope_key": "personal",
                    "applies_when": "个人电脑",
                    "excludes_when": "公司服务器",
                },
                "origin": "user_explicit",
                "source_message_refs": [first],
                "source_tool_refs": [],
                "source_artifact_refs": [],
                "observed_at": "1970-01-01T00:00:11+00:00",
                "valid_from": None,
                "valid_until": None,
                "confidence": 0.99,
                "proposed_action": "add",
                "target_entry_id": None,
                "conflicts_with": [],
                "promotion_target": "long_term",
            }
        ],
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
def _service(
    tmp_path: Path,
    backend: object,
    store: ConversationStore,
    *,
    config: MemoryCuratorConfig | None = None,
) -> MemoryCuratorService:
    return MemoryCuratorService(
        config=config
        or MemoryCuratorConfig(
            interval_seconds=60,
            turn_threshold=1,
            timeout_seconds=1,
            max_retries=2,
        ),
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
                model="curator-timeout-test",
            ),
        ),
    )


# 函数用途: 锁定缩批的按比例减半、前缀保序与"永不空批"下限。
def test_shrink_batch_halves_prefix_and_never_empties_batch() -> None:
    batch = _batch(8, audits=4)

    shrunk = shrink_batch_for_timeout(batch)

    assert shrunk is not None
    # 前缀保序: 只丢尾部,游标才能安全地只推进连续 processed 前缀。
    assert [item.message_id for item in shrunk.messages] == [
        "message-001",
        "message-002",
        "message-003",
        "message-004",
    ]
    assert [item.event_id for item in shrunk.audit_events] == ["event-001", "event-002"]
    # 缩批只动模型可见输入,审计/加载错误等其他字段保持原样。
    assert shrunk.load_errors == batch.load_errors
    assert shrunk.formal_memories == batch.formal_memories

    # 下限: 每族剩 1 条时不再缩,返回 None(不允许缩成空批)。
    assert shrink_batch_for_timeout(_batch(1, audits=1)) is None
    assert shrink_batch_for_timeout(_batch(2)).messages == (_message("message-001"),)
    assert shrink_batch_for_timeout(_batch(1)) is None
    # 消息已经为空时,审计仍按比例缩,但绝不缩到 0 条。
    audits_only = shrink_batch_for_timeout(_batch(0, audits=3))
    assert audits_only is not None
    assert audits_only.messages == ()
    assert len(audits_only.audit_events) == 2


# 函数用途: 锁定超时判定只看异常类型(含类名推导),不看异常正文。
def test_timeout_classification_reads_type_not_provider_text() -> None:
    class _GatewayReadTimeout(Exception):
        pass

    assert is_curator_timeout_error(CuratorModelTimeoutError("memory curator model timed out"))
    assert is_curator_timeout_error(ProviderTimeoutError("idle", stage="stream_idle"))
    assert is_curator_timeout_error(TimeoutError("builtin"))
    assert is_curator_timeout_error(_GatewayReadTimeout("new provider stage"))
    assert not is_curator_timeout_error(ValueError("CURATOR_SCHEMA_INVALID"))
    # 正文里出现 timeout 不构成超时证据(不解析自然语言)。
    assert not is_curator_timeout_error(ConnectionError("read timeout while streaming"))


# 函数用途: 锁定主机侧等待上限仍然生效(不是把超时关掉),并抛稳定超时类型。
def test_call_backend_with_timeout_still_enforces_host_bound() -> None:
    class _SleepingBackend:
        def generate_structured(self, prompt: str, *, response_schema: dict[str, object]):
            del prompt, response_schema
            time.sleep(5)
            return SimpleNamespace(text="{}")

    started = time.monotonic()
    with pytest.raises(CuratorModelTimeoutError):
        call_backend_with_timeout(
            _SleepingBackend(),
            prompt="prompt",
            response_schema={},
            timeout_seconds=1,
        )
    assert time.monotonic() - started < 5


# 函数用途: (c) 无超时时行为逐字一致——同 prompt、同自适应超时、原快照原样返回。
def test_extract_without_timeout_keeps_existing_prompt_and_timeout(monkeypatch) -> None:
    from agent_py_agent.agent.memory_store import curator_backend

    captured: list[tuple[str, int]] = []

    def fake_call(backend, *, prompt, response_schema, timeout_seconds):
        del backend, response_schema
        captured.append((prompt, timeout_seconds))
        return SimpleNamespace(text=json.dumps(_valid_output(_THREAD_ID, ["message-001"])))

    monkeypatch.setattr(curator_backend, "call_backend_with_timeout", fake_call)
    batch = _batch(1)
    config = _config()

    attempt = extract_with_retries(object(), config, batch)

    assert len(captured) == 1
    prompt, timeout_seconds = captured[0]
    assert prompt == curator_prompt(batch)  # 输入未被任何缩批整形
    assert timeout_seconds == adaptive_timeout_seconds(config.timeout_seconds, len(prompt))
    assert attempt.batch is batch  # 原快照原样用于证据验证与游标计算
    assert attempt.shrink_attempts == 0


# 函数用途: (c) 非超时失败仍然是"同输入重试 max_retries 次",一次都不缩批;供应商阶段的 ValueError
# 出口时带上阶段标记 CuratorModelCallError,原异常保留在 __cause__ 供诊断。
def test_non_timeout_failure_retries_identical_prompt_without_shrinking() -> None:
    backend = _ScriptedBackend([ValueError("provider rejected the request")])
    config = _config(**{**_SHRINK_CONFIG, "max_retries": 2})

    with pytest.raises(CuratorModelCallError) as raised:
        extract_with_retries(backend, config, _batch(8))

    assert isinstance(raised.value.__cause__, ValueError)
    assert len(backend.prompts) == 3  # max_retries + 1
    assert len(set(backend.prompts)) == 1  # 逐字相同的输入


# 函数用途: 锁定"缩批计数"与"同输入重试计数"互不污染:同一次运行里两者各自独立消耗。
def test_shrink_and_retry_budgets_are_independent() -> None:
    backend = _ScriptedBackend(
        [
            ProviderTimeoutError("idle", stage="stream_idle"),
            ValueError("CURATOR_SCHEMA_INVALID"),
            ProviderTimeoutError("idle", stage="stream_idle"),
            ValueError("CURATOR_SCHEMA_INVALID"),
            _valid_output(_THREAD_ID, ["message-001", "message-002"]),
        ]
    )
    config = _config(**{**_SHRINK_CONFIG, "max_retries": 3})

    attempt = extract_with_retries(backend, config, _batch(8))

    assert len(backend.prompts) == 5
    # 非超时重试复用同一输入(不缩批),超时才换更小输入。
    assert backend.prompts[1] == backend.prompts[2]
    assert backend.prompts[3] == backend.prompts[4]
    assert len(backend.prompts[3]) < len(backend.prompts[1])
    assert attempt.shrink_attempts == 2
    assert [item.message_id for item in attempt.batch.messages] == [
        "message-001",
        "message-002",
    ]


# 函数用途: 锁定缩批重试不会把模型调用总时长推出 lease 覆盖的预算。
def test_shrink_retries_stay_within_lease_budget(monkeypatch) -> None:
    from agent_py_agent.agent.memory_store import curator_backend

    captured: list[tuple[str, int]] = []

    def fake_call(backend, *, prompt, response_schema, timeout_seconds):
        del backend, response_schema
        captured.append((prompt, timeout_seconds))
        raise ProviderTimeoutError("idle", stage="stream_idle")

    monkeypatch.setattr(curator_backend, "call_backend_with_timeout", fake_call)
    # 32 条 400 字符预览 ≈ 36K 字符输入,首次自适应超时直接顶到封顶值。
    batch = _batch(32, preview_chars=400)
    config = _config()
    assert len(curator_prompt(batch)) <= config.max_input_chars

    with pytest.raises(ProviderTimeoutError):
        extract_with_retries(object(), config, batch)

    budget = adaptive_timeout_seconds(
        config.timeout_seconds, config.max_input_chars
    ) * (config.max_retries + 1)
    assert sum(seconds for _prompt, seconds in captured) <= budget
    # 每次授予的超时仍是自适应值(预算护栏不截断单次超时)。
    assert all(
        seconds == adaptive_timeout_seconds(config.timeout_seconds, len(prompt))
        for prompt, seconds in captured
    )
    # 预算是硬上界:它在缩批上限之前收口,且至少允许一次缩批重试。
    assert 2 <= len(captured) < 1 + _TIMEOUT_SHRINK_LIMIT
    # 输入确实一批比一批小。
    lengths = [len(prompt) for prompt, _seconds in captured]
    assert lengths == sorted(lengths, reverse=True)
    assert lengths[0] > lengths[-1]
    # 失败路径的形状账必须与真实发出的调用逐项对齐(不记正文,但要能被机器核对)。
    shapes = last_model_attempts()
    assert [row.attempt for row in shapes] == list(range(1, len(captured) + 1))
    assert [row.prompt_chars for row in shapes] == lengths
    assert [row.granted_seconds for row in shapes] == [
        seconds for _prompt, seconds in captured
    ]
    assert [row.shrunk for row in shapes] == [False] + [True] * (len(captured) - 1)
    assert {row.outcome for row in shapes} == {"ProviderTimeoutError"}
    assert {row.schema_chars for row in shapes} == {len(json.dumps(curator_response_schema()))}


# 函数用途: (a) 首次超时 → 缩批 → 成功:断言输入变小、重试期间游标不动、成功提交且尾部留待重放。
def test_timeout_shrinks_input_then_commits_shrunk_prefix(tmp_path: Path) -> None:
    store, thread, messages = _conversation(tmp_path, 4)
    head = [item.message_id for item in messages[:2]]
    tail = [item.message_id for item in messages[2:]]
    backend = _ScriptedBackend(
        [
            ProviderTimeoutError("idle", stage="stream_idle"),
            _valid_output(thread.thread_id, head),
        ]
    )
    service = _service(tmp_path, backend, store)
    cursor_snapshots: list[dict[str, str]] = []
    backend.on_call = lambda _index: cursor_snapshots.append(
        dict(service.state_store.load().per_thread_cursors)
    )

    result = service.run(reason="admin")

    assert result.status == "succeeded"
    assert len(backend.prompts) == 2
    # 输入确实变小:第二次只喂前半批消息,尾部不在这轮模型输入里。
    assert len(backend.prompts[1]) < len(backend.prompts[0])
    assert all(mid in backend.prompts[1] for mid in head)
    assert all(mid not in backend.prompts[1] for mid in tail)
    # 超时与缩批重试期间游标一动不动(两次调用前都还是空游标)。
    assert cursor_snapshots == [{}, {}]
    state = service.state_store.load()
    assert state.per_thread_cursors == {thread.thread_id: head[-1]}
    assert state.last_failure_code == ""
    # 成功提交走既有事务路径并如实记录缩批步数(不落任何 prompt 正文)。
    assert result.warnings == ("memory_curator_input_shrunk:1",)
    assert service.candidate_service.list()
    # 证据只允许异常类名/状态码,警示里不得出现消息正文。
    assert messages[0].content not in result.warnings[0]

    # 不丢数据:被丢弃的尾部留在原游标之后,下一轮从同一游标重放。
    replay = _ScriptedBackend([ProviderTimeoutError("idle", stage="stream_idle")])
    follow_up = _service(tmp_path, replay, store).run(reason="admin")

    assert follow_up.status == "failed"
    assert all(mid in replay.prompts[0] for mid in tail)
    assert service.state_store.load().per_thread_cursors == {thread.thread_id: head[-1]}


# 函数用途: (b) 连续超时到上限 → typed 失败,诊断形状与游标契约保持不变。
def test_repeated_timeouts_stop_at_bound_with_typed_failure(tmp_path: Path) -> None:
    store, thread, messages = _conversation(tmp_path, 8)
    backend = _ScriptedBackend([ProviderTimeoutError("idle", stage="stream_idle")])
    service = _service(tmp_path, backend, store)

    result = service.run(reason="admin")

    assert result.status == "failed"
    # 供应商超时沿用既有失败码分类(不新增失败码/不新增账本字段)。
    assert result.failure_code == "CURATOR_MODEL_FAILED"
    assert len(backend.prompts) == 1 + _TIMEOUT_SHRINK_LIMIT
    lengths = [len(prompt) for prompt in backend.prompts]
    assert lengths == sorted(lengths, reverse=True)
    assert lengths[0] > lengths[-1]
    # 下限保护:每批仍含真实消息,绝不出现空批。
    assert all(messages[0].message_id in prompt for prompt in backend.prompts)
    assert messages[-1].message_id in backend.prompts[0]
    assert messages[-1].message_id not in backend.prompts[-1]

    state = service.state_store.load()
    assert state.per_thread_cursors == {}
    assert state.last_failure_code == "CURATOR_MODEL_FAILED"
    assert state.active_lease == {}
    assert service.candidate_service.list() == []
    assert list((tmp_path / "memory" / "daily").glob("*.jsonl")) == []
    records = service.run_log.list()
    assert [record.failure_code for record in records] == ["CURATOR_MODEL_FAILED"]
    # 既有 failure_diagnostic= 形状逐字保留,且不夹带供应商正文;它前面新增的是每次调用的
    # 无正文形状(见 test_curator_timeout_observability.py),让失败路径也能看出缩批真的发生过。
    assert records[0].warnings[-1] == (
        'failure_diagnostic={"error_type":"ProviderTimeoutError","message":"idle"}'
    )
    shapes = [
        json.loads(item.split("=", 1)[1])
        for item in records[0].warnings
        if item.startswith("curator_model_attempt=")
    ]
    assert [row["attempt"] for row in shapes] == [1, 2, 3, 4]
    assert [row["prompt_chars"] for row in shapes] == lengths
    assert [row["shrunk"] for row in shapes] == [False, True, True, True]
    assert "个人电脑使用 macOS" not in "\n".join(records[0].warnings)


# 函数用途: (b') 主机侧等待上限超时走既有超时码,诊断形状同样只含类名。
def test_host_bound_timeout_keeps_timeout_code_and_diagnostic_shape(tmp_path: Path) -> None:
    store, _thread, _messages = _conversation(tmp_path, 1)
    backend = _ScriptedBackend(
        [CuratorModelTimeoutError("memory curator model request timed out")]
    )
    service = _service(tmp_path, backend, store)

    result = service.run(reason="admin")

    assert result.failure_code == "CURATOR_MODEL_TIMEOUT"
    records = service.run_log.list()
    # failure_diagnostic= 仍是最后一条且形状逐字不变;前面多出的是本次唯一一次调用的无正文形状。
    assert records[0].warnings[-1] == (
        'failure_diagnostic={"error_type":"CuratorModelTimeoutError",'
        '"message":"memory curator model request timed out"}'
    )
    assert len(records[0].warnings) == 2
    assert records[0].warnings[0].startswith("curator_model_attempt=")
    assert service.state_store.load().per_thread_cursors == {}


# 函数用途: (d) 已到缩批下限(单条输入)→ 不再发无意义调用,直接 typed 失败。
def test_shrink_floor_blocks_extra_calls_for_single_item_batch(tmp_path: Path) -> None:
    store, _thread, messages = _conversation(tmp_path, 1)
    backend = _ScriptedBackend([ProviderTimeoutError("idle", stage="stream_idle")])
    service = _service(tmp_path, backend, store)

    result = service.run(reason="admin")

    assert result.status == "failed"
    assert result.failure_code == "CURATOR_MODEL_FAILED"
    assert len(backend.prompts) == 1  # 不能再缩就不打了
    assert messages[0].message_id in backend.prompts[0]  # 也绝不缩成空批
    assert service.state_store.load().per_thread_cursors == {}


# 函数用途: (c) 无超时的成功运行不新增任何 warning,游标行为与修复前逐字一致。
def test_success_without_timeout_adds_no_shrink_warning(tmp_path: Path) -> None:
    store, thread, messages = _conversation(tmp_path, 2)
    backend = _ScriptedBackend(
        [_valid_output(thread.thread_id, [item.message_id for item in messages])]
    )
    service = _service(tmp_path, backend, store)

    result = service.run(reason="admin")

    assert result.status == "succeeded"
    assert len(backend.prompts) == 1
    assert result.warnings == ()
    state = service.state_store.load()
    assert state.per_thread_cursors == {thread.thread_id: messages[-1].message_id}
    assert state.last_failure_code == ""
