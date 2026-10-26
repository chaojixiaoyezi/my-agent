# LLM: 仅使用临时 owner 的真实会话、Compact 与人格文件，不触及用户 home；保持红绿断言。
# 模块用途: 验证工作区绑定、主会话分叉的回合闭合和压缩继承。
from __future__ import annotations

import hashlib
import importlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.compact_checkpoint import scoped_compact_checkpoint_id
from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE
from agent_py_agent.agent.conversation.compact_summary_view import resolve_compact_summary_view
from agent_py_agent.agent.conversation.models import ConversationThread
from agent_py_agent.agent.conversation.store import ConversationStore


# LLM: 延迟导入，缺失服务也能分别观察每个功能测试的失败。
# 函数用途: 调用真实分叉入口。
def _fork(store, **kwargs):
    service = importlib.import_module("agent_py_agent.agent.conversation.thread_fork_service")
    return service.fork_conversation_thread(store, **kwargs)


@pytest.fixture
# LLM: fixture只构造临时真实Owner与存储，不接触用户会话或部署。
# 函数用途: 准备目录独立的主会话分叉案例。
def case(tmp_path):
    home = tmp_path / "home" / "owners" / "local" / "main"
    home.mkdir(parents=True)
    work = tmp_path / "project"
    work.mkdir()
    store = ConversationStore(home / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "local/main", "owner_home": str(home),
        "owner_id": "local/main", "cwd": str(work), "runtime_workspace_roots": (str(work),), "title": "源会话"})
    return SimpleNamespace(store=store, thread=thread, home=home, work=work)


# LLM: 每条消息通过真实追加路径落盘，原时间与原 metadata 必须保留。
# 函数用途: 在临时源会话追加一条消息。
def _add(case, role="user", content="正文", metadata=None):
    return case.store.messages.append({"thread_id": case.thread.thread_id, "role": role,
        "content": content, "metadata": metadata or {}, "now": 1234.5})


# LLM: 纯模型新旧序列化检查，不产生目录或身份授权。
# 函数用途: 验证旧会话目录与分叉字段兼容。
def test_old_json_and_direct_model_workspace_defaults():
    old = ConversationThread.from_dict({"thread_id": "thread-old", "canonical_user_id": "u", "cwd": "/project"})
    assert old.workspace_root == "/project"
    assert ConversationThread("thread-new", "u", cwd="/direct").workspace_root == "/direct"
    assert old.forked_from_thread_id == old.forked_from_message_id == ""
    assert old.fork_lineage_depth == 0
    assert ConversationThread.from_dict(old.to_dict()) == old


# LLM: 临时真实线程共享原Store锁，检查按目录过滤及旧revision冲突。
# 函数用途: 验证创建、目录切换和列表筛选保持一致。
def test_create_workspace_filter_and_switch_cas(case, tmp_path):
    store = case.store
    other = tmp_path / "other"
    other.mkdir()
    second = store.threads.create(canonical_user_id="local/main", workspace_root=str(other))
    first = store.threads.require(case.thread.thread_id)
    assert second.cwd == second.workspace_root == str(other)
    assert second.runtime_workspace_roots == (str(other),)
    assert [t.thread_id for t in store.threads.list(workspace_dir=str(case.work))] == [first.thread_id]
    assert [t.thread_id for t in store.threads.list_threads(workspace_dir=str(other))] == [second.thread_id]
    selected, errors = store.threads.list_report(workspace_dir=str(other), limit=1)
    assert not errors and selected == [second]
    changed = store.threads.switch_workspace(first.thread_id, str(other), expected_revision=first.revision)
    assert changed.cwd == changed.workspace_root == str(other)
    assert changed.runtime_workspace_roots == (str(other),)
    with pytest.raises(RuntimeError, match="REVISION"):
        store.threads.switch_workspace(first.thread_id, str(case.work), expected_revision=first.revision)
    assert store.threads.require(second.thread_id) == second


# LLM: 两个真实线程使用同一旧revision竞争原锁，不以mock调用次数当CAS证据。
# 函数用途: 验证并发目录切换只能有一个成功。
def test_two_workspace_switches_cannot_both_win(case, tmp_path):
    dirs = [tmp_path / "one", tmp_path / "two"]
    for directory in dirs:
        directory.mkdir()
    revision = case.store.threads.require(case.thread.thread_id).revision

    # LLM: 只捕获预期修订冲突，其余错误保留给测试失败。
    # 函数用途: 执行一次真实并发目录切换。
    def switch(directory):
        try:
            return case.store.threads.switch_workspace(case.thread.thread_id, str(directory), expected_revision=revision)
        except RuntimeError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(switch, dirs))
    assert sum(result is not None for result in results) == 1


# LLM: 回读真实分叉并逐字节核对源线程/消息/索引；运行绑定不能复制。
# 函数用途: 验证最新分叉保留内容设置但不改源会话。
def test_latest_fork_preserves_history_settings_and_never_source(case):
    store, tid = case.store, case.thread.thread_id
    source = store.threads.update_atomic(tid, lambda t: replace(t, model_profile_id="profile-x",
        model_selection_revision=1, model_selection_source="explicit", model_selection_last_explicit_revision=1,
        reasoning_effort="high", verbose_level="on", task_ids=("task-active",), active_task_ids=("task-active",),
        workspace_task_id="task-active", metadata={"run_id": "running", "active_request_id": "req",
        "last_turn_status": "running", "subagent_run_ids": ["child"], "background_wake_locks": ["lock"]}))
    messages = [_add(case, "user", "问题"), _add(case, "assistant", "回答", {"custom": [1, "中文"]})]
    before = {p: p.read_bytes() for p in (store.storage.thread_path(tid), store.storage.message_path(tid),
        store.storage.bindings_path, store.storage.user_latest_path)}
    result = _fork(store, source_thread_id=tid, title="分支", owner_home=case.home)
    branch = store.threads.require(result["new_thread_id"])
    copied = store.messages.recent(branch.thread_id, limit=0)
    assert isinstance(result, dict) and result["forked_message_count"] == len(messages)
    assert result["boundary_adjusted"] is False
    assert branch.forked_from_thread_id == tid and branch.forked_from_message_id == messages[-1].message_id
    assert branch.fork_lineage_depth == 1 and branch.title == "分支"
    assert branch.cwd == branch.workspace_root == source.cwd
    assert branch.runtime_workspace_roots == source.runtime_workspace_roots
    assert branch.model_profile_id == "profile-x" and branch.reasoning_effort == "high"
    assert branch.verbose_level == "on" and not branch.active_task_ids and not branch.task_ids
    assert not branch.workspace_task_id and not branch.channel_bindings
    assert not branch.metadata.get("active_request_id") and branch.metadata.get("last_turn_status", "idle") == "idle"
    assert not any(branch.metadata.get(k) for k in ("run_id", "subagent_run_ids", "background_wake_locks"))
    for original, clone in zip(messages, copied):
        assert original.message_id != clone.message_id and clone.thread_id == branch.thread_id
        assert (clone.role, clone.content, clone.created_at, clone.metadata) == (
            original.role, original.content, original.created_at, original.metadata)
    assert all(p.read_bytes() == data for p, data in before.items())
    reopened = ConversationStore(store.storage.root)
    assert reopened.messages.recent(branch.thread_id, limit=0) == copied
    again = _fork(reopened, source_thread_id=branch.thread_id)
    assert reopened.threads.require(again["new_thread_id"]).fork_lineage_depth == 2


# LLM: 指定消息边界精确包含末条；无效ID不得创建新线程。
# 函数用途: 核对截止分叉与缺失消息错误的零副作用。
def test_cutoff_and_missing_message_have_no_side_effects(case):
    first = _add(case)
    _add(case, "assistant", "future")
    result = _fork(case.store, source_thread_id=case.thread.thread_id, up_to_message_id=first.message_id)
    assert [m.content for m in case.store.messages.recent(result["new_thread_id"], limit=0)] == [first.content]
    paths = set(case.store.storage.threads_dir.glob("*.json"))
    with pytest.raises(ValueError, match="FORK_MESSAGE_NOT_FOUND"):
        _fork(case.store, source_thread_id=case.thread.thread_id, up_to_message_id="missing")
    assert set(case.store.storage.threads_dir.glob("*.json")) == paths


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
# LLM: 覆盖原生和旧工具结构，合法修复只能补齐回合或退回完整边界。
# 函数用途: 防止OpenAI和Anthropic前缀出现孤立工具调用。
def test_tool_round_prefix_never_dangles(case, native, protocol):
    _add(case)
    call = ({"role": "assistant", "tool_calls": [{"id": "call-a", "type": "function", "function": {"name": "x", "arguments": "{}"}}]}
        if protocol == "openai" else {"role": "assistant", "content": [{"type": "tool_use", "id": "call-a", "name": "x", "input": {}}]})
    result = ({"role": "tool", "tool_call_id": "call-a", "content": "ok"} if protocol == "openai"
        else {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-a", "content": "ok"}]})
    call_row = _add(case, "assistant", "", {"canonical_native_messages": [call]} if native else call)
    _add(case, result["role"], "", {"canonical_native_messages": [result]} if native else result)
    branch = _fork(case.store, source_thread_id=case.thread.thread_id, up_to_message_id=call_row.message_id)
    copied = case.store.messages.recent(branch["new_thread_id"], limit=0)
    assert branch["boundary_adjusted"] is True
    assert len(copied) in (1, 3)
    if len(copied) == 3:
        assert copied[-1].metadata == ({"canonical_native_messages": [result]} if native else result)


# LLM: 并行工具只返回部分结果时不允许复制未闭合回合。
# 函数用途: 核对不完整工具历史回退安全边界。
def test_incomplete_tool_history_rolls_back(case):
    _add(case)
    _add(case, "assistant", "", {"canonical_native_messages": [{"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}]}]})
    _add(case, "tool", "ok", {"tool_call_id": "a"})
    branch = _fork(case.store, source_thread_id=case.thread.thread_id)
    assert branch["boundary_adjusted"] is True and branch["forked_message_count"] == 1


@pytest.mark.parametrize("admin", [False, True])
# LLM: 显式覆盖沿真实目录校验；普通用户仅家内，管理员才可安全外部项目。
# 函数用途: 核对分叉目录覆盖的身份边界。
def test_workspace_override_owner_and_admin_permissions(case, tmp_path, admin):
    inside = case.home / "project"
    inside.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    valid = external if admin else inside
    branch = _fork(case.store, source_thread_id=case.thread.thread_id, workspace_dir=str(valid), owner_home=case.home, is_admin=admin)
    thread = case.store.threads.require(branch["new_thread_id"])
    assert thread.cwd == thread.workspace_root == str(valid.resolve())
    assert thread.runtime_workspace_roots == (str(valid.resolve()),)
    if not admin:
        with pytest.raises(ValueError, match="WORKSPACE"):
            _fork(case.store, source_thread_id=case.thread.thread_id, workspace_dir=str(external), owner_home=case.home)


@pytest.mark.parametrize("bad", ["missing", "file", "root", "config", "other-owner", "symlink"])
# LLM: 管理员标记不绕过现存目录、系统根、控制面和其它Owner检查。
# 函数用途: 验证分叉不能绑定危险或跨用户目录。
def test_admin_cannot_override_dangerous_or_foreign_workspace(case, tmp_path, bad):
    target = tmp_path / "not-created"
    if bad == "file":
        target.write_text("file")
    if bad == "root":
        target = Path("/")
    if bad == "config":
        target = case.home.parents[2] / "config"
        target.mkdir()
    if bad in {"other-owner", "symlink"}:
        target = case.home.parent / "other" / "project"
        target.mkdir(parents=True)
        if bad == "symlink":
            link = tmp_path / "link"
            link.symlink_to(target, target_is_directory=True)
            target = link
    with pytest.raises(ValueError, match="WORKSPACE"):
        _fork(case.store, source_thread_id=case.thread.thread_id, workspace_dir=str(target), owner_home=case.home, is_admin=True)


# LLM: 测试真实 v3 内容封印及原 head，不构造第二个摘要权威。
# 函数用途: 向临时 owner 写入并提交压缩检查点。
def _compact(case, messages):
    row = {"schema": "conversation_compact_checkpoint.v3", "thread_id": case.thread.thread_id,
        "generation": 1, "previous_checkpoint_id": "", "summary_base_checkpoint_id": "",
        "scope": THREAD_COMPACT_SCOPE.to_dict(), "source_kind": "transcript", "created_at": 10.0,
        "source_message_ids": [m.message_id for m in messages], "source_tool_refs": [],
        "retained_tail_message_ids": [], "retained_tool_call_ids": [], "retained_tool_refs": [],
        "summary": "已完成摘要", "summary_sha256": hashlib.sha256("已完成摘要".encode()).hexdigest(),
        "operation_evidence": {}, "source_end_message_id": messages[-1].message_id,
        "source_end_byte_offset": case.store.messages.byte_offset_after(case.thread.thread_id, messages[-1].message_id)}
    row["checkpoint_id"] = scoped_compact_checkpoint_id(row)
    path = case.home / "compact" / "conversations" / f"{case.thread.thread_id}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(row) + "\n")
    case.store.threads.update_atomic(case.thread.thread_id, lambda t: replace(t, summary=row["summary"],
        compact_checkpoint_id=row["checkpoint_id"], compact_generation=1,
        compacted_through_message_id=messages[-1].message_id, compacted_through_byte_offset=row["source_end_byte_offset"]))
    return path


@pytest.mark.parametrize("early", [False, True])
# LLM: 检查点通过真实读取器验证覆盖、新ID和字节游标；早期分叉不能泄露未来摘要。
# 函数用途: 核对Compact继承与截止位置相符。
def test_committed_compact_inheritance_remaps_ids_and_cursors(case, early):
    messages = [_add(case), _add(case, "assistant", "第二条"), _add(case, "user", "尾部")]
    original_checkpoint = _compact(case, messages[:2])
    before = original_checkpoint.read_bytes()
    branch = _fork(case.store, source_thread_id=case.thread.thread_id,
        up_to_message_id=messages[0].message_id if early else None, owner_home=case.home)
    thread = case.store.threads.require(branch["new_thread_id"])
    if early:
        assert not thread.summary and not thread.compact_checkpoint_id and thread.compact_generation == 0
    else:
        agent = SimpleNamespace(home_paths=SimpleNamespace(owner_compact_dir=case.home / "compact"), backend=None)
        view = resolve_compact_summary_view(agent, thread, THREAD_COMPACT_SCOPE)
        clones = case.store.messages.recent(thread.thread_id, limit=0)
        assert view.summary == "已完成摘要" and view.source_message_ids == frozenset(m.message_id for m in clones[:2])
        assert thread.compacted_through_message_id == clones[1].message_id
        assert thread.compacted_through_byte_offset == case.store.messages.byte_offset_after(thread.thread_id, clones[1].message_id)
        tail, errors = case.store.messages.after_compact_report(thread)
        assert not errors and tail == clones[2:]
    assert original_checkpoint.read_bytes() == before


# LLM: 冻结人格按真实路径逐字节复制，不刷新源快照。
# 函数用途: 验证人格快照继承且原件不变。
def test_frozen_persona_snapshot_copied_without_source_mutation(case):
    _add(case)
    path = case.home / "persona" / "thread_prefix" / f"{case.thread.thread_id}.json"
    path.parent.mkdir(parents=True)
    snapshot = {"schema_version": 1, "epoch": {"compact_generation": 0, "model_profile_id": "", "protocol": ["echo", "native"]},
        "sections": {"agents": "规则", "soul": "人格", "user": "用户", "diagnostics": ""}}
    path.write_text(json.dumps(snapshot, ensure_ascii=False))
    original = path.read_bytes()
    branch = _fork(case.store, source_thread_id=case.thread.thread_id, owner_home=case.home)
    assert path.with_name(f"{branch['new_thread_id']}.json").read_bytes() == original
    assert path.read_bytes() == original


# LLM: 损坏JSONL不是空历史，拒绝分叉且不生成替代线程。
# 函数用途: 验证损坏来源不能静默丢消息后继续。
def test_corrupt_source_transcript_refuses_fork(case):
    _add(case)
    path = case.store.storage.message_path(case.thread.thread_id)
    with path.open("a") as stream:
        stream.write("{broken\n")
    before = set(case.store.storage.threads_dir.glob("*.json"))
    with pytest.raises((ValueError, OSError), match="FORK|transcript"):
        _fork(case.store, source_thread_id=case.thread.thread_id)
    assert set(case.store.storage.threads_dir.glob("*.json")) == before


@pytest.mark.parametrize("closed", [False, True])
# LLM: 使用真实canonical信封schema；仅闭合工具往返可按原metadata复制。
# 函数用途: 核对实际原生信封的安全继承。
def test_real_canonical_native_envelope_preserved_or_rolled_back(case, closed):
    _add(case)
    native = [{"role": "assistant", "content": [{"type": "tool_use", "id": "real-call", "name": "read_file", "input": {}}]}]
    if closed:
        native.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": "real-call", "content": "原工具结果"}]})
    metadata = {"canonical_native_messages": {"schema": "conversation_native_messages.v1", "messages": native}}
    _add(case, "assistant", "回复", metadata)
    branch = _fork(case.store, source_thread_id=case.thread.thread_id)
    copied = case.store.messages.recent(branch["new_thread_id"], limit=0)
    assert branch["boundary_adjusted"] is (not closed)
    assert len(copied) == (2 if closed else 1)
    if closed:
        assert copied[-1].metadata == metadata


# LLM: 工具角色即使正文为JSON也不能有无来源结果，不把它当普通用户文本。
# 函数用途: 防止分叉携带孤立工具结果。
def test_orphan_tool_result_and_json_payload_cannot_dangle(case):
    _add(case)
    _add(case, "tool", '{"tool_call_id":"orphan", "content":"no call"}')
    result = _fork(case.store, source_thread_id=case.thread.thread_id)
    assert result["boundary_adjusted"] and result["forked_message_count"] == 1


# LLM: 多代摘要经过真实封印与读取链，游标需匹配新消息文件字节数。
# 函数用途: 验证多代Compact和早期安全head重建。
def test_multigeneration_compact_offsets_and_latest_safe_head(case):
    messages = [_add(case, content=str(index)) for index in range(4)]
    path = _compact(case, messages[:2])
    first = json.loads(path.read_text())
    second = {**first, "generation": 2, "previous_checkpoint_id": first["checkpoint_id"],
        "summary_base_checkpoint_id": first["checkpoint_id"], "source_message_ids": [messages[2].message_id],
        "source_start_message_id": messages[2].message_id, "source_end_message_id": messages[2].message_id,
        "source_start_byte_offset": first["source_end_byte_offset"],
        "source_end_byte_offset": case.store.messages.byte_offset_after(case.thread.thread_id, messages[2].message_id),
        "summary": "第二代摘要", "summary_sha256": hashlib.sha256("第二代摘要".encode()).hexdigest()}
    second["checkpoint_id"] = scoped_compact_checkpoint_id(second)
    with path.open("a") as stream:
        stream.write(json.dumps(second) + "\n")
    case.store.threads.update_atomic(case.thread.thread_id, lambda t: replace(t, summary=second["summary"],
        compact_checkpoint_id=second["checkpoint_id"], compact_generation=2,
        compacted_through_message_id=messages[2].message_id, compacted_through_byte_offset=second["source_end_byte_offset"]))
    latest = _fork(case.store, source_thread_id=case.thread.thread_id)
    branch_path = path.with_name(f"{latest['new_thread_id']}.jsonl")
    rows = [json.loads(line) for line in branch_path.read_text().splitlines()]
    assert rows[1]["source_start_byte_offset"] == rows[0]["source_end_byte_offset"]
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_compact_dir=case.home / "compact"), backend=None)
    thread = case.store.threads.require(latest["new_thread_id"])
    view = resolve_compact_summary_view(agent, thread, THREAD_COMPACT_SCOPE)
    clones = case.store.messages.recent(thread.thread_id, limit=0)
    assert view.source_message_ids == frozenset(message.message_id for message in clones[:3])
    early = _fork(case.store, source_thread_id=case.thread.thread_id, up_to_message_id=messages[1].message_id)
    assert case.store.threads.require(early["new_thread_id"]).compact_generation == 1


# LLM: 用正式freeze读取已复制快照，不靠文件存在断言人格被采用。
# 函数用途: 验证分叉首轮确实沿用冻结人格。
def test_branch_persona_is_consumed_by_real_freeze_reader(case):
    from agent_py_agent.agent.prompting_parts.thread_persona import (
        RenderedPersonaSections,
        ThreadPersonaScope,
        freeze_thread_persona,
    )

    _add(case)
    sections = {"agents": "原规则", "soul": "原人格", "user": "原用户", "diagnostics": ""}
    scope = ThreadPersonaScope(case.thread.thread_id, 0, "", ("echo", "native"))
    freeze_thread_persona(case.home, scope, RenderedPersonaSections(sections, frozenset()))
    fork = _fork(case.store, source_thread_id=case.thread.thread_id)
    branch_scope = replace(scope, thread_id=fork["new_thread_id"])
    fixed, updates = freeze_thread_persona(case.home, branch_scope,
        RenderedPersonaSections({**sections, "soul": "新渲染人格"}, frozenset()))
    assert "原人格" in fixed and "新渲染人格" not in fixed and "新渲染人格" in updates


# LLM: 局部摘要不继承但不能阻断后续安全全线程摘要；验证真实读取视图。
# 函数用途: 核对交错作用域的Compact继承。
def test_safe_thread_compact_after_unrelated_scoped_checkpoint(case):
    from agent_py_agent.agent.conversation.compact_scope import CompactScope

    messages = [_add(case, content=str(index)) for index in range(3)]
    path = _compact(case, messages[:1])
    first = json.loads(path.read_text())
    local = {**first, "generation": 2, "previous_checkpoint_id": first["checkpoint_id"],
        "summary_base_checkpoint_id": "", "scope": CompactScope(kind="turn", turn_id="private-turn").to_dict(),
        "summary": "局部私有摘要", "summary_sha256": hashlib.sha256("局部私有摘要".encode()).hexdigest()}
    local["checkpoint_id"] = scoped_compact_checkpoint_id(local)
    final = {**first, "generation": 3, "previous_checkpoint_id": local["checkpoint_id"],
        "summary_base_checkpoint_id": first["checkpoint_id"], "source_message_ids": [messages[1].message_id],
        "source_end_message_id": messages[1].message_id,
        "summary": "安全全线程摘要", "summary_sha256": hashlib.sha256("安全全线程摘要".encode()).hexdigest()}
    final["checkpoint_id"] = scoped_compact_checkpoint_id(final)
    with path.open("a") as stream:
        stream.write(json.dumps(local) + "\n" + json.dumps(final) + "\n")
    case.store.threads.update_atomic(case.thread.thread_id, lambda t: replace(t, compact_generation=3,
        compact_checkpoint_id=final["checkpoint_id"], summary=final["summary"]))
    branch = _fork(case.store, source_thread_id=case.thread.thread_id)
    thread = case.store.threads.require(branch["new_thread_id"])
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_compact_dir=case.home / "compact"), backend=None)
    view = resolve_compact_summary_view(agent, thread, THREAD_COMPACT_SCOPE)
    assert view.summary == "安全全线程摘要"
    assert "局部私有摘要" not in path.with_name(f"{thread.thread_id}.jsonl").read_text()


# LLM: 显式空ID不是省略参数，不得隐式分叉全部历史。
# 函数用途: 验证空截止消息被明确拒绝。
def test_empty_explicit_cutoff_is_not_latest(case):
    _add(case)
    with pytest.raises(ValueError, match="FORK_MESSAGE_NOT_FOUND"):
        _fork(case.store, source_thread_id=case.thread.thread_id, up_to_message_id="")


# LLM: 真实工具角色与旧协议调用配对依call_id，不使用provider项目ID冒充调用身份。
# 函数用途: 核对Responses旧工具记录的配对字段。
def test_responses_tool_item_uses_call_id_not_item_id(case):
    _add(case)
    _add(case, "assistant", json.dumps({"type": "function_call", "id": "fc-item", "call_id": "call-real", "arguments": "{}"}))
    _add(case, "tool", json.dumps({"type": "function_call_output", "call_id": "call-real", "output": "结果"}))
    branch = _fork(case.store, source_thread_id=case.thread.thread_id)
    assert branch["boundary_adjusted"] is False and branch["forked_message_count"] == 3


# LLM: v1来源不能把旧checkpoint身份贴到新线程；正式读取器须消费重建v3链。
# 函数用途: 验证旧检查点分叉后的兼容重建。
def test_legacy_checkpoint_is_migrated_into_new_committed_chain(case):
    messages = [_add(case), _add(case, "assistant", "tail")]
    path = _compact(case, messages[:1])
    old = {"schema": "conversation_compact_checkpoint.v1", "checkpoint_id": "legacy-one", "thread_id": case.thread.thread_id,
        "generation": 1, "previous_checkpoint_id": "", "source_kind": "transcript", "source_end_message_id": messages[0].message_id,
        "summary": "旧摘要", "summary_sha256": hashlib.sha256("旧摘要".encode()).hexdigest(), "created_at": 20.0, "operation_evidence": {}}
    path.write_text(json.dumps(old) + "\n")
    case.store.threads.update_atomic(case.thread.thread_id, lambda t: replace(t, compact_checkpoint_id="legacy-one", summary="旧摘要"))
    branch = _fork(case.store, source_thread_id=case.thread.thread_id)
    thread = case.store.threads.require(branch["new_thread_id"])
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_compact_dir=case.home / "compact"), backend=None)
    view = resolve_compact_summary_view(agent, thread, THREAD_COMPACT_SCOPE)
    assert view.summary == "旧摘要" and thread.compact_checkpoint_id.startswith("compact-v3-")
    assert case.store.messages.after_compact_report(thread)[0][0].content == "tail"


# LLM: 分叉仅面向主会话，不复用子代理运行记录或上下文身份。
# 函数用途: 拒绝对代理运行线程调用主会话分叉。
def test_cannot_fork_agent_runtime_thread(case):
    case.store.threads.update_atomic(case.thread.thread_id, lambda t: replace(t,
        metadata={"thread_kind": "agent", "agent_run_id": "child-run"}))
    with pytest.raises(ValueError, match="FORK_MAIN_THREAD_REQUIRED"):
        _fork(case.store, source_thread_id=case.thread.thread_id)


# LLM: 普通正文即使长得像工具协议，也不是canonical信封或真实工具角色，不可截断用户历史。
# 函数用途: 防止粘贴JSON示例被误判为未闭合工具回合。
@pytest.mark.parametrize("role", ["user", "assistant"])
def test_plain_json_example_does_not_remove_history(case, role):
    _add(case, "user", "普通问题")
    _add(case, "assistant", "普通回答")
    _add(case, role, '{"type":"tool_use","id":"demo","name":"read_file","input":{}}')
    _add(case, "assistant", "这是格式示例")
    result = _fork(case.store, source_thread_id=case.thread.thread_id)
    assert result["boundary_adjusted"] is False
    assert result["forked_message_count"] == 4


# LLM: 继承旧路径不意味着永久授权；发布前仍须拒绝已失效或跨owner重定向的目录。
# 函数用途: 验证分叉不能传播已被替换的工作根。
def test_inherited_workspace_retarget_cannot_publish_branch(case):
    case.work.rename(case.work.with_name("original-project"))
    foreign = case.home.parent / "bob"
    foreign.mkdir()
    case.work.symlink_to(foreign, target_is_directory=True)
    before = set(case.store.storage.threads_dir.glob("*.json"))
    with pytest.raises(ValueError, match="WORKSPACE"):
        _fork(case.store, source_thread_id=case.thread.thread_id, owner_home=case.home, is_admin=True)
    assert set(case.store.storage.threads_dir.glob("*.json")) == before


# LLM: 所有正常线程更新都沿同一个文件锁，迟到的完整快照不能倒退已成功的目录CAS。
# 函数用途: 验证非目录字段更新不会复活旧工作区和旧revision。
def test_late_snapshot_cannot_revert_workspace_revision(case, tmp_path):
    stale = case.store.threads.require(case.thread.thread_id)
    other = tmp_path / "other"
    other.mkdir()
    latest = case.store.threads.switch_workspace(stale.thread_id, str(other), expected_revision=stale.revision)
    with pytest.raises((ValueError, RuntimeError), match="REVISION|修订"):
        case.store.threads.update_atomic(stale.thread_id, lambda _current: replace(stale, title="迟到更新"))
    assert case.store.threads.require(stale.thread_id) == latest


# LLM: 仅注入真实分叉写入或最终发布错误，不改产品回滚；检查源资源与新线程可见性。
# 函数用途: 验证消息/Compact/人格/最终线程写失败时不会留下半分叉。
@pytest.mark.parametrize("phase", ["messages", "compact", "persona", "thread"])
def test_fork_publish_failure_leaves_no_partial_branch(case, monkeypatch, phase):
    from agent_py_agent.agent.conversation import thread_fork_service as service

    messages = [_add(case), _add(case, "assistant", "答案")]
    _compact(case, messages[:1])
    persona = case.home / "persona/thread_prefix" / f"{case.thread.thread_id}.json"
    persona.parent.mkdir(parents=True)
    persona.write_text(json.dumps({"schema_version": 1, "epoch": {"compact_generation": 1,
        "model_profile_id": "", "protocol": ["echo", "native"]},
        "sections": {"agents": "规则", "soul": "人格", "user": "用户", "diagnostics": ""}}))
    before = {path: path.read_bytes() for path in case.home.rglob("*") if path.is_file()}
    write = service.write_private_text_file_atomic

    # LLM: 指定阶段抛出一次明确IO错误，其余写入继续真实执行。
    # 函数用途: 让每个资源阶段的回滚责任都被执行到。
    def fail_write(path, content):
        matches = {"messages": path.parent == case.store.storage.messages_dir,
            "compact": "compact" in path.parts, "persona": path.parent == persona.parent}
        if matches.get(phase, False):
            raise OSError("fork publish fault")
        return write(path, content)

    monkeypatch.setattr(service, "write_private_text_file_atomic", fail_write)
    if phase == "thread":
        # LLM: 前置资源全部写好后才让原子发布失败，不能只验证第一步失败。
        # 函数用途: 模拟最终线程创建失败。
        def fail_thread(*_args):
            raise OSError("fork publish fault")
        monkeypatch.setattr(service, "create_thread_atomic", fail_thread)
    with pytest.raises(OSError, match="fork publish fault"):
        _fork(case.store, source_thread_id=case.thread.thread_id, owner_home=case.home)
    after = {path: path.read_bytes() for path in case.home.rglob("*") if path.is_file() and path.suffix != ".lock"}
    assert after == {path: content for path, content in before.items() if path.suffix != ".lock"}


# LLM: 改写临时已提交检查点后重新封印并更新真实head，不用mock绕过原Compact读取器。
# 函数用途: 为分叉来源核验准备合法提交、但带危险附加引用的摘要。
def wp15_checkpoint_fields(case, path, **fields):
    row = json.loads(path.read_text())
    row.update(fields)
    row['checkpoint_id'] = scoped_compact_checkpoint_id(row)
    path.write_text(json.dumps(row) + '\n')
    case.store.threads.update_atomic(case.thread.thread_id, lambda thread: replace(thread,
        compact_checkpoint_id=row['checkpoint_id']))


# LLM: 三种位置的引用都必须属于实际分叉前缀；摘要文字不作为消息身份匹配条件。
# 函数用途: 防止顶层、boundary或metadata带入未来或未知消息引用。
@pytest.mark.parametrize('location', ['top', 'boundary', 'metadata'])
@pytest.mark.parametrize('reference', ['future', 'unknown'])
def test_wp15_compact_rejects_outside_prefix_message_references(case, location, reference):
    messages = [_add(case), _add(case, 'assistant', '安全前缀'), _add(case, content='未来正文')]
    path = _compact(case, messages[:1])
    value = messages[-1].message_id if reference == 'future' else 'message-unknown'
    fields = ({'retained_tail_message_ids': [value]} if location == 'top' else
              {location: {'source_message_ids': [value]}})
    wp15_checkpoint_fields(case, path, **fields)
    branch = _fork(case.store, source_thread_id=case.thread.thread_id,
        up_to_message_id=messages[1].message_id, owner_home=case.home)
    thread = case.store.threads.require(branch['new_thread_id'])
    assert thread.compact_generation == 0 and not thread.summary


# LLM: 正式canonical信封包含闭合call/result；检查点工具ID必须全在前缀，不能由归档字段自证来源。
# 函数用途: 验证无工具、混入未知工具与合法完整工具来源的继承边界。
@pytest.mark.parametrize('tool_ids,has_tool,allowed', [
    (['archive-only'], False, False), (['in-prefix', 'archive-only'], True, False),
    (['in-prefix'], True, True), ([], False, True),
])
def test_wp15_compact_tool_ids_are_proven_by_forked_messages(case, tool_ids, has_tool, allowed):
    messages = [_add(case)]
    if has_tool:
        messages.append(_add(case, 'assistant', '已执行工具', {'canonical_native_messages': {
            'schema': 'conversation_native_messages.v1', 'messages': [
                {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'in-prefix', 'name': 'read_file', 'input': {}}]},
                {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'in-prefix', 'content': 'result'}]},
            ]}}))
    path = _compact(case, messages)
    wp15_checkpoint_fields(case, path, source_tool_call_ids=tool_ids)
    branch = _fork(case.store, source_thread_id=case.thread.thread_id, owner_home=case.home)
    thread = case.store.threads.require(branch['new_thread_id'])
    assert bool(thread.compact_generation) is allowed


# LLM: 真正创建子线程后才抛错，区别于发表前失败；原源文件逐字节保持且新资源必须全部清除。
# 函数用途: 复现最终发布已经落盘后留下孤立线程的问题。
def test_wp15_fork_post_publish_failure_removes_child_record(case, monkeypatch):
    from agent_py_agent.agent.conversation import thread_fork_service as service

    _add(case)
    before = {path: path.read_bytes() for path in case.home.rglob('*') if path.is_file() and path.suffix != '.lock'}
    original = service.create_thread_atomic

    # LLM: 保留真实原子线程创建；仅在其成功返回后注入后置IO错误。
    # 函数用途: 构造资源和线程均已发表的异常出口。
    def fail_after_publish(*args):
        original(*args)
        raise OSError('post-publish failure')

    monkeypatch.setattr(service, 'create_thread_atomic', fail_after_publish)
    with pytest.raises(OSError, match='post-publish failure'):
        _fork(case.store, source_thread_id=case.thread.thread_id, owner_home=case.home)
    after = {path: path.read_bytes() for path in case.home.rglob('*') if path.is_file() and path.suffix != '.lock'}
    assert after == before


# LLM: 明确传空消息ID不能按省略处理；命令无效时仍保留明确错误，不产生分叉线程。
# 函数用途: 验证空引号参数在公开解析器中被拒绝。
@pytest.mark.parametrize('raw', ['/fork ""', '/fork "" --title branch', '/fork --from-message ""'])
def test_wp15_fork_parser_rejects_explicit_empty_message(raw):
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control

    command = parse_conversation_control(raw)
    assert command is not None and command.kind == 'fork' and not command.valid


# LLM: 临时让外层valid通过，领域层仍须识别显式空参数；这是测试负例，不给产品加绕过开关。
# 函数用途: 证明领域分叉入口不依赖解析器单层保护。
def test_wp15_workspace_control_rejects_explicit_empty_fork_after_outer_bypass(case):
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.agent.conversation.workspace_context import execute_workspace_control

    _add(case)
    command = replace(parse_conversation_control('/fork ""'), valid=True)
    before = set(case.store.storage.threads_dir.glob('*.json'))
    result = execute_workspace_control(case.store, command, thread_id=case.thread.thread_id, owner_home=case.home)
    assert not result.ok and result.error_code == 'FORK_MESSAGE_NOT_FOUND'
    assert set(case.store.storage.threads_dir.glob('*.json')) == before


# LLM: 用真实Compact链证明精确复数键也能携带来源，不把辅助函数漏检误说成未经验证的真实绕过。
# 函数用途: 防止metadata的message_ids、tool_call_ids等漏过前缀核验。
@pytest.mark.parametrize('key', ['message_ids', 'tool_call_ids', 'call_ids', 'tool_use_ids'])
def test_wp15_review_plural_provenance_fields_fail_closed(case, key):
    messages = [_add(case), _add(case, content='未来消息')]
    path = _compact(case, messages[:1])
    wp15_checkpoint_fields(case, path, metadata={key: [messages[1].message_id if key == 'message_ids' else 'future-call']})
    result = _fork(case.store, source_thread_id=case.thread.thread_id,
        up_to_message_id=messages[0].message_id, owner_home=case.home)
    assert case.store.threads.require(result['new_thread_id']).compact_generation == 0


# LLM: 碰撞记录和其实际消息/Compact/人格都必须可读；不能只保住线程JSON却删除它已引用的资源。
# 函数用途: 复现同ID碰撞的归属与整组资源一致性问题。
def test_wp15_review_collision_record_is_not_owned_by_fork(case, monkeypatch):
    from agent_py_agent.agent.conversation import thread_fork_service as service

    message = _add(case)
    _compact(case, [message])
    persona = case.home / 'persona/thread_prefix' / f'{case.thread.thread_id}.json'
    persona.parent.mkdir(parents=True)
    persona.write_text(json.dumps({'schema_version': 1, 'epoch': {'compact_generation': 1,
        'model_profile_id': '', 'protocol': ['echo', 'native']},
        'sections': {'agents': '规则', 'soul': '人格', 'user': '用户', 'diagnostics': ''}}))
    original, collided = service.create_thread_atomic, []

    # LLM: 在最终发表入口制造另一发表者先成功的精确碰撞，不改原原子创建及回滚实现。
    # 函数用途: 发布不含本次事务标记的线程，并让本次创建报告碰撞。
    def publish_collision(threads, branch):
        foreign = replace(branch, metadata={})
        collided.append(original(threads, foreign))
        raise FileExistsError('foreign collision')

    monkeypatch.setattr(service, 'create_thread_atomic', publish_collision)
    with pytest.raises(FileExistsError, match='foreign collision'):
        _fork(case.store, source_thread_id=case.thread.thread_id, owner_home=case.home)
    assert len(collided) == 1
    assert case.store.threads.require(collided[0].thread_id) == collided[0]
    copied = case.store.messages.recent(collided[0].thread_id, limit=0)
    assert len(copied) == 1 and copied[0].content == message.content
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_compact_dir=case.home / 'compact'), backend=None)
    assert resolve_compact_summary_view(agent, collided[0], THREAD_COMPACT_SCOPE).summary == '已完成摘要'
    assert persona.with_name(f'{collided[0].thread_id}.json').read_bytes() == persona.read_bytes()


# LLM: 用同一per-path锁的非阻塞竞争证明读取和删除均在原canonical锁内，不用时间猜测竞态。
# 函数用途: 验证回滚检查与删除之间不能插入遵守原锁的线程替换。
def test_wp15_review_rollback_holds_canonical_thread_lock(case, monkeypatch):
    from agent_py_agent.agent.common.json_io import locked_json_path
    from agent_py_agent.agent.conversation import thread_fork_service as service

    _add(case)
    result = _fork(case.store, source_thread_id=case.thread.thread_id, owner_home=case.home)
    branch = case.store.threads.require(result['new_thread_id'])
    path = case.store.storage.thread_path(branch.thread_id)
    original_read, original_unlink, observed = service.read_bytes_beneath, service.unlink_file_beneath, []

    # LLM: 只探测原锁，读取与删除仍执行真实nofollow原语；锁不在时必须产生断言失败。
    # 函数用途: 为回滚的两个关键观察点验证同一把锁仍被持有。
    def locked_call(operation, *args, **kwargs):
        with pytest.raises(BlockingIOError):
            with locked_json_path(path, blocking=False):
                pass
        observed.append(operation)
        return operation(*args, **kwargs)

    monkeypatch.setattr(service, 'read_bytes_beneath', lambda *a, **k: locked_call(original_read, *a, **k))
    monkeypatch.setattr(service, 'unlink_file_beneath', lambda *a, **k: locked_call(original_unlink, *a, **k))
    service._rollback_fork_thread(case.store, branch)
    assert observed == [original_read, original_unlink]
    assert case.store.threads.load(branch.thread_id) is None


# LLM: 使用真实闭合canonical工具消息和已提交Compact；四元身份只由消息元数据证明，不靠检查点自证。
# 函数用途: 构造同名call跨运行/尝试/回合的分叉核验案例。
@pytest.mark.parametrize('field', ['run_id', 'attempt_id', 'turn_id', 'missing', 'matching'])
@pytest.mark.parametrize('location', ['source_tool_refs', 'retained_tool_refs', 'nested'])
def test_wp19_compact_tool_reference_requires_exact_four_tuple(case, field, location):
    identity = {'run_id': 'run-1', 'attempt_id': 'attempt-1', 'turn_id': 'turn-1', 'call_id': 'call-x'}
    metadata = {key: value for key, value in identity.items() if key != 'call_id'} if field != 'missing' else {}
    metadata['canonical_native_messages'] = {'schema': 'conversation_native_messages.v1', 'messages': [
        {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'call-x', 'name': 'read_file', 'input': {}}]},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call-x', 'content': 'result'}]},
    ]}
    messages = [_add(case), _add(case, 'assistant', '工具已完成', metadata)]
    _add(case, 'user', '分叉点后的未来消息')
    path = _compact(case, messages)
    reference = {**identity, **({field: field + '-future'} if field in identity else {})}
    fields = ({'metadata': {'nested': [{'tool_reference': reference}]}} if location == 'nested'
              else {location: [reference]})
    wp15_checkpoint_fields(case, path, **fields)
    before = path.read_bytes()
    result = _fork(case.store, source_thread_id=case.thread.thread_id,
        up_to_message_id=messages[-1].message_id, owner_home=case.home)
    branch = case.store.threads.require(result['new_thread_id'])
    assert bool(branch.compact_generation) is (field == 'matching')
    assert path.read_bytes() == before


# LLM: 每代都走真实分叉/封印/读取器；嵌套列表、字典及精确message_id键与带前缀键必须统一重映射。
# 函数用途: 防止合法嵌套消息引用在A到B到C连续分叉时遗留旧ID并丢失摘要。
def test_wp19_nested_checkpoint_message_refs_survive_two_forks(case):
    messages = [_add(case), _add(case, 'assistant', '第二条'), _add(case, content='保留尾部')]
    ids = [message.message_id for message in messages]
    path = _compact(case, messages[:2])
    wp15_checkpoint_fields(case, path,
        boundary={'start_message_id': ids[0], 'nested': [{'message_id': ids[1], 'message_ids': ids[:2]}]},
        metadata={'source_message_ids': ids[:2], 'deep': [{'tail_message_id': ids[2]}],
                  'optional_message_id': '', 'nullable_message_id': None, 'text': ids[0]})
    source_id, original = case.thread.thread_id, path.read_bytes()
    for _ in range(2):
        result = _fork(case.store, source_thread_id=source_id, owner_home=case.home)
        branch = case.store.threads.require(result['new_thread_id'])
        assert branch.compact_generation == 1 and branch.summary == '已完成摘要'
        clones = case.store.messages.recent(branch.thread_id, limit=0)
        cloned_ids = [message.message_id for message in clones]
        checkpoint = json.loads(path.with_name(f'{branch.thread_id}.jsonl').read_text())
        assert checkpoint['boundary'] == {'start_message_id': cloned_ids[0],
            'nested': [{'message_id': cloned_ids[1], 'message_ids': cloned_ids[:2]}]}
        assert checkpoint['metadata'] == {'source_message_ids': cloned_ids[:2],
            'deep': [{'tail_message_id': cloned_ids[2]}], 'optional_message_id': '',
            'nullable_message_id': None, 'text': ids[0]}
        assert checkpoint['checkpoint_id'] == scoped_compact_checkpoint_id(checkpoint)
        agent = SimpleNamespace(home_paths=SimpleNamespace(owner_compact_dir=case.home / 'compact'), backend=None)
        assert resolve_compact_summary_view(agent, branch, THREAD_COMPACT_SCOPE).source_message_ids == frozenset(cloned_ids[:2])
        source_id = branch.thread_id
    assert path.read_bytes() == original


# LLM: 原生工具内容不能覆盖宿主消息所属运行元数据；消息没有身份或身份冲突时不能用payload自证四元归属。
# 函数用途: 防止同名call由原生消息附加字段补认或覆盖为另一回合的工具来源。
@pytest.mark.parametrize('has_message_identity', [False, True])
def test_wp19_tool_payload_cannot_override_message_identity(case, has_message_identity):
    identity = {'run_id': 'run-1', 'attempt_id': 'attempt-1', 'turn_id': 'turn-1'}
    reference = {**identity, 'turn_id': 'turn-2', 'call_id': 'call-x'}
    metadata = dict(identity) if has_message_identity else {}
    metadata['canonical_native_messages'] = {'schema': 'conversation_native_messages.v1', 'messages': [
        {**reference, 'role': 'assistant', 'content': [
            {'type': 'tool_use', 'id': 'call-x', 'name': 'read_file', 'input': {}}]},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call-x', 'content': 'ok'}]},
    ]}
    messages = [_add(case), _add(case, 'assistant', '工具完成', metadata)]
    path = _compact(case, messages)
    wp15_checkpoint_fields(case, path, source_tool_refs=[reference])
    result = _fork(case.store, source_thread_id=case.thread.thread_id, owner_home=case.home)
    assert case.store.threads.require(result['new_thread_id']).compact_generation == 0
