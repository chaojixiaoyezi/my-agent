"""只读历史来源与具体种子在原 native/text 准备边界逐项等价；互斥与读取失败合同。"""
from __future__ import annotations

import json
import os
import time
from functools import partial
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._tool_loop_service import _text_conversation_history_section
from agent_py_agent.agent.agent_core.runtime.loop_support import _native_provider_history_messages
from agent_py_agent.agent.agent_core.tool_ir_history import project_native_provider_messages
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.agent_thread import (
    _agent_thread_history_seed,
    _bounded_agent_history,
    project_agent_history_row,
)
from agent_py_agent.agent.conversation.background_context import TaskScopeDecision
from agent_py_agent.agent.conversation.background_history_seed import (
    BackgroundHistoryProjection,
    project_background_history_seed,
)
from agent_py_agent.agent.conversation.compact import load_conversation_compact_source
from agent_py_agent.agent.conversation.compact_projection import ConversationCompactView
from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE
from agent_py_agent.agent.conversation.display_checkpoint import DISPLAY_CHECKPOINT_ROLE
from agent_py_agent.agent.conversation.history_projection import (
    conversation_history_rows,
    history_row_selected,
    project_history_row,
)
from agent_py_agent.agent.conversation.history_seed import freeze_history_source, seed_text_messages
from agent_py_agent.agent.conversation.message_replay import MessageSnapshotRows
from agent_py_agent.agent.conversation.models import ConversationHistorySeed
from agent_py_agent.agent.conversation.native_history import (
    CANONICAL_NATIVE_MESSAGES_METADATA_KEY,
    canonical_native_messages_envelope,
    provider_history_messages_from_rows,
)
from agent_py_agent.agent.conversation.tool_context_window import (
    TERMINAL_TOOL_FOLD_METADATA_KEY,
    build_conversation_terminal_tool_fold,
)
from agent_py_agent.agent.gateway_parts import request_context, request_prompt
from agent_py_agent.agent.runtime_errors import DataCorruptionError

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
CURRENT_REQUEST = "gw-current"
FOLD_BUILT_AT = 1_000_000.0


# LLM: 只写 tmp_path 的真实 canonical 文件；覆盖媒体、工具配对、匿名信封、孤儿调用、终态折叠及三类应被选择排除的行。
# 函数用途: 构造一段已结束的历史，同时返回磁盘地址视图与内存行两种来源。
def _history(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "owner", "now": 1})
    tid = thread.thread_id
    agent = SimpleNamespace(
        conversation_store=store, home_paths=SimpleNamespace(owner_compact_dir=tmp_path / "compact"),
        backend=SimpleNamespace(name="fake", model_name="fake", max_tokens=128),
        prompts=SimpleNamespace(build=lambda prompt, *_args, **_kwargs: prompt),
        config=SimpleNamespace(model_context_window_tokens=20_000, model_context_window_explicit=True,
                               conversation_history_max_chars=1000),
    )
    identified = [
        {"role": "user", "content": [{"type": "text", "text": "带图的第一轮"},
                                     {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG}}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "call-read", "name": "read_file", "input": {"path": "a.txt"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-read", "content": "READ-RESULT"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "资料已读"}]},
    ]
    orphan = [{"role": "assistant", "content": [{"type": "tool_use", "id": "orphan-call", "name": "read_file", "input": {}}]}]
    fold = build_conversation_terminal_tool_fold(
        SimpleNamespace(config=SimpleNamespace()),
        [{"tool": "write_file", "ok": True, "call_id": "call-write", "parameters": {"path": "out.txt"}}],
        built_at_epoch=FOLD_BUILT_AT,
    )
    for role, content, metadata in (
        ("user", "第一轮：请读取资料", {"conversation_request_id": "turn-1"}),
        ("assistant", "资料已读", {"conversation_request_id": "turn-1",
                                  CANONICAL_NATIVE_MESSAGES_METADATA_KEY: canonical_native_messages_envelope(identified)}),
        ("assistant", "匿名信封可见正文",
         {CANONICAL_NATIVE_MESSAGES_METADATA_KEY: canonical_native_messages_envelope(
             [{"role": "assistant", "content": [{"type": "text", "text": "匿名信封正文"}]}])}),
        ("assistant", "孤儿调用轮", {"conversation_request_id": "turn-orphan",
                                    CANONICAL_NATIVE_MESSAGES_METADATA_KEY: canonical_native_messages_envelope(orphan)}),
        ("assistant", "文件已修改", {"conversation_request_id": "turn-fold", TERMINAL_TOOL_FOLD_METADATA_KEY: fold}),
        ("assistant", "", {"conversation_request_id": "turn-empty"}),
        ("assistant", "当前请求自己的回复", {"gateway_request_id": CURRENT_REQUEST}),
        ("assistant", "Audit 后台投递", {"task_id": "audit-1", "reason": "audit_finding",
                                        "background_delivery_reason": "finding"}),
        (DISPLAY_CHECKPOINT_ROLE, "显示检查点", {}),
        ("user", "最后一个问题", {"conversation_request_id": "turn-last"}),
    ):
        store.messages.append({"thread_id": tid, "role": role, "content": content, "metadata": metadata})
    snapshot = load_conversation_compact_source(agent, store, thread, scope=THREAD_COMPACT_SCOPE).messages
    assert isinstance(snapshot, MessageSnapshotRows), "必须覆盖磁盘地址重放路径"
    # 内存行由同一地址视图物化，两种来源覆盖完全相同的行（loader 自身已按原规则排除 display 检查点）。
    memory = tuple(snapshot)
    assert len(memory) == len(snapshot) >= 9
    return agent, thread, snapshot, memory


# LLM: 具体种子逐字沿原算法构造（conversation_history_rows / _bounded_agent_history + 原 messages 规则），作为等价基准。
# 函数用途: 按宿主规则构造旧式具体种子。
def _concrete_seed(agent, thread, rows, rule, current_request_id):
    if rule in {"conversation", "background"}:
        selected = conversation_history_rows(agent, thread.thread_id, current_request_id, [], rows=rows, preserve_complete=True)
        # Gateway 原规则过滤空正文；后台原规则保留全部已选行。
        messages = tuple(
            (row.role, row.content) for row in selected
            if row.role in {"user", "assistant"} and (row.content or rule == "background")
        )
        return ConversationHistorySeed(messages=messages, canonical_messages=provider_history_messages_from_rows(selected))
    bounded = _bounded_agent_history(agent, list(rows), 0, preserve_complete=True)
    messages = tuple(
        (str(row.role).strip().lower(), row.content) for row in bounded
        if str(row.role).strip().lower() in {"user", "assistant"} and row.content
    )
    return ConversationHistorySeed(messages=messages, canonical_messages=provider_history_messages_from_rows(bounded))


# LLM: 来源只冻结原选择与原单行投影，不另写规则。
# 函数用途: 按宿主规则构造只读来源种子。
def _source_seed(rows, rule, current_request_id):
    if rule in {"conversation", "background"}:
        source = freeze_history_source(
            rows, project_row=project_history_row,
            select=partial(history_row_selected, current_request_id=current_request_id, work_scope=None),
            keep_empty_text=rule == "background",
        )
    else:
        source = freeze_history_source(rows, project_row=project_agent_history_row)
    return ConversationHistorySeed(source=source)


# 函数用途: 以运行参数形态调用原生准备边界。
def _native(seed):
    return _native_provider_history_messages(SimpleNamespace(conversation_history_seed=seed, compact_context=None))


@pytest.mark.parametrize("rule", ["conversation", "background", "agent_thread"])
@pytest.mark.parametrize("kind", ["disk", "memory"])
def test_source_seed_resolves_identically_at_native_and_text_boundaries(tmp_path, rule, kind):
    agent, thread, snapshot, memory = _history(tmp_path)
    rows = snapshot if kind == "disk" else memory
    concrete = _concrete_seed(agent, thread, memory, rule, CURRENT_REQUEST)
    source = _source_seed(rows, rule, CURRENT_REQUEST)
    assert source.source is not None and not source.messages and not source.canonical_messages

    native_concrete, native_source = _native(concrete), _native(source)
    assert native_source == native_concrete
    encoded = json.dumps(native_source, ensure_ascii=False)
    assert PNG in encoded and "READ-RESULT" in encoded and "匿名信封正文" in encoded
    assert "orphan-call" in encoded and "result_unavailable" not in encoded, "原生历史本身保留孤儿调用，由下游统一清扫"
    swept_concrete = project_native_provider_messages((), prior_messages=native_concrete)
    swept_source = project_native_provider_messages((), prior_messages=native_source)
    assert swept_source == swept_concrete
    # 下游孤儿清扫为缺结果的调用补上"结果不可用"占位，两种种子形式得到同一份修补结果。
    assert "result_unavailable" in json.dumps(swept_source, ensure_ascii=False)

    assert _text_conversation_history_section(source) == _text_conversation_history_section(concrete)
    assert seed_text_messages(source) == concrete.messages
    text = json.dumps(seed_text_messages(source), ensure_ascii=False)
    assert "conversation-terminal-tool-fold" in text or "call-write" in text, "终态工具折叠必须进入文本历史"
    if rule != "agent_thread":
        for excluded in ("当前请求自己的回复", "Audit 后台投递", "显示检查点"):
            assert excluded not in text and excluded not in encoded


def test_three_host_builders_emit_source_seeds_equivalent_to_original_projection(tmp_path, monkeypatch):
    agent, thread, snapshot, memory = _history(tmp_path)
    gateway = request_prompt.gateway_conversation_history_seed(request_context.GatewayConversationContext(
        thread_id=thread.thread_id, history_source=request_context._gateway_history_source(snapshot, CURRENT_REQUEST, None),
    ))
    background = project_background_history_seed(
        agent, BackgroundHistoryProjection(thread.thread_id, TaskScopeDecision("", frozenset(), None, False), {}, 1),
        snapshot, scope_applied=True,
    )
    child = _agent_thread_history_seed(agent, ConversationCompactView(thread.thread_id, 0, "", snapshot, {}, {}, 1, False))
    for seed, rule, request_id in (
        (gateway, "conversation", CURRENT_REQUEST), (background, "background", ""), (child, "agent_thread", ""),
    ):
        assert seed.source is not None and not seed.messages and not seed.canonical_messages
        concrete = _concrete_seed(agent, thread, memory, rule, request_id)
        assert _native(seed) == _native(concrete)
        assert _text_conversation_history_section(seed) == _text_conversation_history_section(concrete)
        # 渲染段会跳过空正文，逐项核对原 messages 规则（后台保留空正文行，Gateway/child 过滤）。
        assert seed_text_messages(seed) == concrete.messages


def test_source_and_concrete_history_are_exclusive(tmp_path):
    source = freeze_history_source((), project_row=project_history_row)
    with pytest.raises(ValueError, match="exclusive"):
        ConversationHistorySeed(messages=(("user", "x"),), source=source)
    with pytest.raises(ValueError, match="exclusive"):
        ConversationHistorySeed(canonical_messages=({"role": "user", "content": []},), source=source)
    with pytest.raises(ValueError, match="explicit source rows"):
        freeze_history_source(iter(()), project_row=project_history_row)


def test_rewritten_canonical_file_fails_instead_of_becoming_empty_history(tmp_path):
    agent, thread, snapshot, _memory = _history(tmp_path)
    seed = _source_seed(snapshot, "conversation", CURRENT_REQUEST)
    path = agent.conversation_store.storage.message_path(thread.thread_id)
    raw = path.read_bytes()
    path.write_bytes(raw.replace("第一轮".encode(), "改写后".encode(), 1))
    with pytest.raises(DataCorruptionError):
        _native(seed)
    with pytest.raises(DataCorruptionError):
        _text_conversation_history_section(seed)


@pytest.mark.parametrize("rule", ["conversation", "background", "agent_thread"])
def test_frozen_projection_time_keeps_terminal_fold_stable_after_hot_window(tmp_path, monkeypatch, rule):
    clock = [FOLD_BUILT_AT + 10]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    agent, thread, snapshot, memory = _history(tmp_path)
    concrete = _concrete_seed(agent, thread, memory, rule, CURRENT_REQUEST)
    source = _source_seed(snapshot, rule, CURRENT_REQUEST)
    hot_text = _text_conversation_history_section(concrete)
    # 热尾窗口（300 秒）过后再解析：仍按冻结时刻取热尾，与准备时的具体种子逐项相同，多次解析结果不变。
    for later in (FOLD_BUILT_AT + 400, FOLD_BUILT_AT + 4000):
        clock[0] = later
        assert _text_conversation_history_section(source) == hot_text
        assert _native(source) == _native(concrete)
    clock[0] = FOLD_BUILT_AT + 400
    assert _text_conversation_history_section(_concrete_seed(agent, thread, memory, rule, CURRENT_REQUEST)) != hot_text, (
        "对照：同一时刻重新准备的具体种子会取冷折叠，证明冻结时刻确实生效"
    )


def test_legal_append_after_freeze_keeps_frozen_selection(tmp_path):
    agent, thread, snapshot, memory = _history(tmp_path)
    source = _source_seed(snapshot, "conversation", CURRENT_REQUEST)
    before_native, before_text = _native(source), _text_conversation_history_section(source)
    agent.conversation_store.messages.append({
        "thread_id": thread.thread_id, "role": "user", "content": "冻结之后才追加", "metadata": {},
    })
    assert _native(source) == before_native
    assert _text_conversation_history_section(source) == before_text
    assert "冻结之后才追加" not in json.dumps(_native(source), ensure_ascii=False)


@pytest.mark.parametrize("damage", ["truncate", "replace", "delete"])
def test_truncated_replaced_or_deleted_canonical_file_fails_instead_of_empty_history(tmp_path, damage):
    agent, thread, snapshot, _memory = _history(tmp_path)
    seed = _source_seed(snapshot, "conversation", CURRENT_REQUEST)
    path = agent.conversation_store.storage.message_path(thread.thread_id)
    expected = DataCorruptionError
    if damage == "truncate":
        path.write_bytes(path.read_bytes()[: path.stat().st_size // 2])
    elif damage == "replace":
        # 原子替换成逐字节相同的新文件：文件身份已变，仍不能成为新的冻结来源。
        replacement = path.with_name(path.name + ".replacement")
        replacement.write_bytes(path.read_bytes())
        os.replace(replacement, path)
    else:
        path.unlink()
        expected = FileNotFoundError
    with pytest.raises(expected):
        _native(seed)
    with pytest.raises(expected):
        _text_conversation_history_section(seed)


def test_empty_source_seed_is_read_once_at_native_boundary(monkeypatch):
    from agent_py_agent.agent.conversation import history_seed

    def second_read(_seed):
        raise AssertionError("只读来源在原生边界不能再为旧文本回退二次读行")

    source = ConversationHistorySeed(source=freeze_history_source((), project_row=project_history_row))
    expected = _native(ConversationHistorySeed())
    monkeypatch.setattr(history_seed, "seed_text_messages", second_read)
    assert _native(source) == expected == []


# LLM: 与 loop_support 原旧文本回退逐字同规则，仅用于对照"跳过回退"前后的结果。
# 函数用途: 按改前逻辑计算只读来源在原生边界的旧文本回退输出。
def _legacy_fallback(seed):
    from agent_py_agent.agent.agent_core.runtime.loop_support import _native_user_task_text
    from agent_py_agent.agent.backends.message_adapter import AnthropicMessageAdapter
    from agent_py_agent.agent.backends.tool_ir import AssistantTurn, UserTurn

    legacy = []
    for role, content in seed_text_messages(seed):
        if role == "user" and content:
            legacy.append(UserTurn(_native_user_task_text(content)))
        elif role == "assistant" and content:
            legacy.append(AssistantTurn(text=content))
    return AnthropicMessageAdapter().to_provider_messages(legacy) if legacy else []


@pytest.mark.parametrize("case", ["background_empty_rows", "agent_thread_display_rows"])
def test_rows_native_drops_but_text_keeps_do_not_change_native_output(tmp_path, case):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "owner", "now": 1})
    if case == "background_empty_rows":
        # 后台规则保留空正文行：text 有 ("assistant", "")，native 无消息；旧回退本来就跳过空正文。
        for role in ("user", "assistant"):
            store.messages.append({"thread_id": thread.thread_id, "role": role, "content": "",
                                   "metadata": {"conversation_request_id": "turn-empty"}})
        rows = tuple(store.messages.recent(thread.thread_id, limit=0))
        source = freeze_history_source(rows, project_row=project_history_row, keep_empty_text=True,
                                       select=partial(history_row_selected, current_request_id="", work_scope=None))
    else:
        # child 规则不做选择：display 行进入冻结行，native 过滤它，text 也只保留 user/assistant。
        store.messages.append({"thread_id": thread.thread_id, "role": DISPLAY_CHECKPOINT_ROLE, "content": "显示检查点"})
        rows = tuple(store.messages.recent(thread.thread_id, limit=0))
        source = freeze_history_source(rows, project_row=project_agent_history_row)
    seed = ConversationHistorySeed(source=source)
    text = seed_text_messages(seed)
    assert (text == (("user", ""), ("assistant", ""))) if case == "background_empty_rows" else (text == ())
    assert _native(seed) == _legacy_fallback(seed) == [], "跳过旧文本回退前后，原生边界输出一致"
