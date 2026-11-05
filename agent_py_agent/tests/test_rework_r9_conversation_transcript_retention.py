# LLM: 只用 pytest 临时 owner 与真实 ConversationStore/Compact 提交；冷转阈值保持产品原值（1 MiB / 128 行），
#   配额按 1 GiB 真实量级核算；每个恢复场景只出现一次普通模拟 I/O 错误，不放宽任何保留期安全门。
# 模块用途: 回归 P1-R9-05——会话保留期按 canonical 热/清单/暂存/冷段/索引整体收集，锁内重验后整体回收与清除。
from __future__ import annotations

import json
import os
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.context_compactor import RuntimeCompactPolicy
from agent_py_agent.agent.common import nofollow_fs
from agent_py_agent.agent.common.json_io import locked_json_path
from agent_py_agent.agent.conversation import ConversationStore, message_scan
from agent_py_agent.agent.conversation import compact_checkpoint as checkpoints
from agent_py_agent.agent.conversation.models import ConversationCompactCommit
from agent_py_agent.agent.memory_store import retention_apply as apply
from agent_py_agent.agent.memory_store import retention_scan as scan
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.retention import MemoryRetentionService
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.agent.user_space.owner_quota import (
    owner_logical_usage_bytes,
    owner_quota_enforcer_from_policy,
)

NOW = datetime(2026, 8, 4, tzinfo=timezone.utc)
OLD = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
# 产品默认的 1 GiB owner 配额（quota.json 的 max_disk_mb），不使用玩具配额放大瞬态偏差。
ONE_GIB_MB = 1024
# 140 行、每行约 16 KiB：同时越过原 128 行与 1 MiB 冷转阈值，并按原 1 MiB 段上限切成多个冷段。
ROWS = 140
ROW_BODY = "cold-segment-body " * 900
POLICY = RuntimeCompactPolicy(
    context_window_tokens=1000, trigger_percent=90, trigger_tokens=900,
    recovery_target_percent=60, recovery_target_tokens=600,
    allow_persistent_apply=True, recent_tail_max_turns=4, recent_tail_tokens=100,
    failure_threshold=3, failure_cooldown_seconds=300.0,
)


# LLM: 真实 home 初始化后只改本测试 owner 的保留天数与 1 GiB 配额，不触碰真实用户目录。
# 函数用途: 装配带 30 天会话/回收期和 1 GiB 配额的临时 owner 与唯一留存服务。
def _owner(tmp_path):
    home = ensure_my_agent_home(tmp_path / "home")
    policy = json.loads(home.owner_retention_json.read_text(encoding="utf-8"))
    policy.update(conversation_days=30, trash_days=30)
    home.owner_retention_json.write_text(json.dumps(policy, sort_keys=True), encoding="utf-8")
    quota = json.loads(home.owner_quota_json.read_text(encoding="utf-8"))
    quota["max_disk_mb"] = ONE_GIB_MB
    home.owner_quota_json.write_text(json.dumps(quota, sort_keys=True), encoding="utf-8")
    service = MemoryRetentionService(home_paths=home, candidates=CandidateService(home.owner_memory_candidates_jsonl))
    return home, service


# LLM: 真实追加、检查点与线程CAS后明确解绑，模拟闲置可退休会话；冷转阈值及普通I/O模拟不变。
# 函数用途: 生成一个按原阈值完成（或在指定点中断）冷转的旧会话，返回线程、热文件路径与原始完整字节。
def _rotated_conversation(home, monkeypatch, fault=None):
    store = ConversationStore(Path(home.owner_home_dir) / "conversations")
    thread = store.threads.get_or_create({
        "canonical_user_id": "retention-user", "channel": "internal", "channel_conversation_id": "retention-chat",
        "channel_user_id": "retention-user", "now": OLD,
    })
    rows = [store.messages.append({
        "thread_id": thread.thread_id, "role": "user" if number % 2 == 0 else "assistant",
        "content": f"row-{number}:" + ROW_BODY, "now": OLD + number,
        "metadata": {"dedupe_key": f"retention-{number}"},
    }) for number in range(ROWS)]
    path = store.storage.message_path(thread.thread_id)
    original = path.read_bytes()
    boundary = store.messages.byte_offset_after(thread.thread_id, rows[-2].message_id)
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_compact_dir=Path(home.owner_compact_dir)),
                            backend=SimpleNamespace(name="test", model_name="test-model"))
    checkpoint_id = checkpoints.write_compact_checkpoint(agent, checkpoints.CompactCheckpointRequest(
        thread=store.threads.require(thread.thread_id), summary="已覆盖旧行", operation_evidence={},
        compact_rows=tuple(rows[:-1]), retained_tail=(rows[-1],), source_end_byte_offset=boundary,
        projected_tokens_before=900, projected_tokens_after=300, policy=POLICY, forced=False,
    ))
    commit = ConversationCompactCommit(
        summary="已覆盖旧行", operation_evidence={}, checkpoint_id=checkpoint_id,
        compacted_through_message_id=rows[-2].message_id, compacted_through_byte_offset=boundary,
        source_messages=ROWS - 1, source_tool_pairs=0,
    )
    with monkeypatch.context() as patch:
        injected = _inject_rotation_fault(patch, thread.thread_id, fault)
        updated = store.threads.update_compact_state(thread.thread_id, commit=commit, expected_generation=0,
                                                     now=OLD + 1000)
    assert injected == ([] if fault is None else [fault])
    for name in ("channel_bindings.json", "user_latest_threads.json"):
        index = store.storage.root / name
        payload = json.loads(index.read_text())
        index.write_text(json.dumps({key: value for key, value in payload.items() if value != thread.thread_id}))
    return store, updated, path, original


# LLM: 三个中断点各只出现一次普通模拟 OSError：prepared 清单写入前、暂存段替换热文件时、最终清单写入前。
# 函数用途: 在原冷转写者的单个 I/O 点制造一次失败，记录确实触发的故障点。
def _inject_rotation_fault(patch, thread_id, fault):
    injected = []
    if fault is None:
        return injected
    manifest_name = f"{thread_id}.manifest.json"
    original_write, original_replace = nofollow_fs.write_text_atomic_beneath, os.replace

    def write(root, parts, text, **kwargs):
        if tuple(parts) == (manifest_name,) and not injected:
            prepared = json.loads(text).get("phase") == "prepared"
            if (fault == "prepared_write" and prepared) or (fault == "final_write" and not prepared):
                injected.append(fault)
                raise OSError("simulated manifest write error")
        return original_write(root, parts, text, **kwargs)

    def replace_file(source, destination, *args, **kwargs):
        if fault == "replace" and not injected and str(source).endswith(".stage"):
            injected.append(fault)
            raise OSError("simulated stage replace error")
        return original_replace(source, destination, *args, **kwargs)

    patch.setattr(nofollow_fs, "write_text_atomic_beneath", write)
    patch.setattr(os, "replace", replace_file)
    return injected


# LLM: 只按 canonical 命名枚举转写本家族，不解析正文；用于断言规划集合与清除后的残留。
# 函数用途: 列出某线程在会话根下的热文件、清单、暂存段与冷目录普通文件。
def _transcript_files(root: Path, thread_id: str) -> set[Path]:
    messages = root / "messages"
    found = {path for path in (messages / f"{thread_id}.jsonl", messages / f"{thread_id}.manifest.json")
             if path.is_file()}
    found.update(messages.glob(f".{thread_id}.jsonl.rotation-*.stage"))
    archive = messages / "archive" / thread_id
    if archive.exists():
        found.update(path for path in archive.rglob("*") if path.is_file())
    return found


# 函数用途: 计算一组文件的总字节数。
def _bytes(paths) -> int:
    return sum(path.stat().st_size for path in paths)


# 函数用途: 取出本线程唯一的会话保留动作。
def _conversation_action(report, thread_id):
    actions = [action for action in report.actions if action.category == "conversation"]
    assert [action.record_id for action in actions] == [thread_id]
    return actions[0]


# LLM: 原阈值真实冷转后，规划集合必须含热/清单/全部冷段与全部索引节点；回收后原位置无残留，
#   回收站期满清除后 1 GiB 配额口径下释放的字节不少于完整转写本，不留任何无法再被发现的冷集合。
# 函数用途: 端到端验证普通过期回收与之后的回收站清除。
def test_original_rotation_cold_set_is_trashed_then_purged_without_residue(tmp_path, monkeypatch):
    home, service = _owner(tmp_path)
    store, thread, path, original = _rotated_conversation(home, monkeypatch)
    root = store.storage.root.resolve()
    manifest = json.loads(path.with_suffix(".manifest.json").read_bytes())
    archive = root / "messages" / "archive" / thread.thread_id
    segments = sorted(archive.glob("seg-*.jsonl"))
    index_nodes = sorted((archive / "index").glob("*.json"))
    assert "phase" not in manifest and manifest["segment_count"] == len(segments) >= 2
    assert index_nodes and len(path.read_bytes()) < len(original)
    with message_scan.TranscriptReader(path) as reader:
        assert reader.read() == original
    transcript = _transcript_files(root, thread.thread_id)
    transcript_bytes = _bytes(transcript)
    assert transcript_bytes > 2 * 1024 * 1024

    owner_root = Path(home.owner_home_dir)
    enforcer = owner_quota_enforcer_from_policy(owner_root)
    assert enforcer.max_bytes == ONE_GIB_MB * 1024 * 1024
    audit = Path(home.owner_audit_log_jsonl)
    audit_before = audit.stat().st_size if audit.exists() else 0
    usage_before = owner_logical_usage_bytes(owner_root, exact=True)
    assert usage_before < enforcer.max_bytes

    plan = service.plan(now=NOW)
    action = _conversation_action(plan, thread.thread_id)
    assert transcript <= set(action.related_paths)
    assert not [error for error in plan.errors if "CONVERSATION" in error.error_code]

    trashed = service.apply(now=NOW)
    assert _conversation_action(trashed, thread.thread_id).status == "trashed"
    assert _transcript_files(root, thread.thread_id) == set()
    assert not archive.exists()
    payload = action.destination / "payload"
    for source in transcript:
        assert (payload / source.relative_to(root)).is_file()
    assert owner_logical_usage_bytes(owner_root, exact=True) >= transcript_bytes

    later = NOW + timedelta(days=31)
    purged = service.apply(now=later)
    purge = [item for item in purged.actions if item.category == "trash"]
    assert [item.status for item in purge] == ["deleted"]
    assert not action.destination.exists()
    assert not list(owner_root.rglob("seg-*.jsonl"))
    assert not list(owner_root.rglob(f"*{thread.thread_id}.manifest.json"))
    audit_growth = (audit.stat().st_size if audit.exists() else 0) - audit_before
    released = usage_before + audit_growth - owner_logical_usage_bytes(owner_root, exact=True)
    assert released >= transcript_bytes


# LLM: 每个中断点只有一次普通 I/O 错误；prepared/暂存/孤儿冷段必须作为同一可恢复集合整体回收，
#   把回收载荷原样放回独立目录后，原选择器仍能读出完整原始字节（证明回收的是连贯集合）。
# 函数用途: 验证中断冷转留下的 prepared 布局被完整、可恢复地回收，原位置不残留暂存段或冷目录。
@pytest.mark.parametrize("fault", ["prepared_write", "replace", "final_write"])
def test_prepared_rotation_is_trashed_as_one_recoverable_set(tmp_path, monkeypatch, fault):
    home, service = _owner(tmp_path)
    store, thread, path, original = _rotated_conversation(home, monkeypatch, fault=fault)
    assert thread.metadata["compact_transcript_maintenance"]["status"] == "pending"
    root = store.storage.root.resolve()
    transcript = _transcript_files(root, thread.thread_id)
    stages = [item for item in transcript if item.name.endswith(".stage")]
    assert bool(stages) is (fault != "final_write")
    assert any(item.name.startswith("seg-") for item in transcript)

    plan = service.plan(now=NOW)
    action = _conversation_action(plan, thread.thread_id)
    assert transcript <= set(action.related_paths)
    assert _conversation_action(service.apply(now=NOW), thread.thread_id).status == "trashed"
    assert _transcript_files(root, thread.thread_id) == set()
    assert not (root / "messages" / "archive" / thread.thread_id).exists()

    restored = tmp_path / "restored"
    shutil.copytree(action.destination / "payload" / "messages", restored / "messages")
    with message_scan.TranscriptReader(restored / "messages" / path.name) as reader:
        assert reader.read() == original


# LLM: 规划后出现的新冷成员（同一线程归档目录下）必须让本轮执行放弃，原件逐字节保留；下一轮重新规划后整体回收。
# 函数用途: 验证执行端在锁内重新枚举成员，而不是沿用规划时的旧集合只搬一部分。
def test_apply_reenumerates_members_and_defers_on_late_cold_member(tmp_path, monkeypatch):
    home, service = _owner(tmp_path)
    store, thread, path, _original = _rotated_conversation(home, monkeypatch)
    root = store.storage.root.resolve()
    plan = service.plan(now=NOW)
    action = _conversation_action(plan, thread.thread_id)
    late = root / "messages" / "archive" / thread.thread_id / "index" / "late-node.json"
    late.write_text("{}\n", encoding="utf-8")
    before = {item: item.read_bytes() for item in _transcript_files(root, thread.thread_id)}

    result = apply._trash_conversation(home=home, action=action, now=NOW)

    assert result.status == "state_changed"
    assert {item: item.read_bytes() for item in _transcript_files(root, thread.thread_id)} == before
    assert store.threads.load(thread.thread_id) is not None
    assert _conversation_action(service.apply(now=NOW), thread.thread_id).status == "trashed"
    assert _transcript_files(root, thread.thread_id) == set()


# LLM: 锁序必须与消息 writer 一致（先原消息锁、再线程转换锁）；消息锁被占用时不能先拿线程锁，也不能移动任何成员。
# 函数用途: 用真实锁与事件交错验证执行端等待原消息 writer，释放后才完成回收。
def test_trash_waits_for_message_writer_lock_before_thread_lock(tmp_path, monkeypatch):
    home, service = _owner(tmp_path)
    store, thread, path, _original = _rotated_conversation(home, monkeypatch)
    root = store.storage.root.resolve()
    action = _conversation_action(service.plan(now=NOW), thread.thread_id)
    order = []
    original_json_lock, original_transition = apply.locked_json_path, apply.locked_file_transition

    def json_lock(target, *args, **kwargs):
        order.append(("message", Path(target)))
        return original_json_lock(target, *args, **kwargs)

    def transition(target, *args, **kwargs):
        order.append(("thread", Path(target)))
        return original_transition(target, *args, **kwargs)

    monkeypatch.setattr(apply, "locked_json_path", json_lock)
    monkeypatch.setattr(apply, "locked_file_transition", transition)
    finished, outcome = threading.Event(), []

    def run():
        try:
            outcome.append(apply._trash_conversation(home=home, action=action, now=NOW))
        finally:
            finished.set()

    members = _transcript_files(root, thread.thread_id)
    with locked_json_path(path):
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        assert not finished.wait(0.5)
        with locked_json_path(store.storage.thread_path(thread.thread_id), blocking=False):
            pass
        assert _transcript_files(root, thread.thread_id) == members
    assert finished.wait(30)
    worker.join(30)
    assert outcome and outcome[0].status == "trashed"
    assert [kind for kind, _target in order] == ["message", "thread"]
    assert order[0][1] == root / "messages" / path.name
    assert _transcript_files(root, thread.thread_id) == set()


# LLM: 冷成员缺失、冷目录含链接都属于无法证明完整的布局：只报结构化错误并整会话保留，原件逐字节不动。
# 函数用途: 验证坏冷布局走结构化延期而不是只回收可见部分。
@pytest.mark.parametrize("damage", ["missing_segment", "symlink_member"])
def test_incomplete_cold_layout_defers_with_structured_error(tmp_path, monkeypatch, damage):
    home, service = _owner(tmp_path)
    store, thread, _path, _original = _rotated_conversation(home, monkeypatch)
    root = store.storage.root.resolve()
    archive = root / "messages" / "archive" / thread.thread_id
    if damage == "missing_segment":
        sorted(archive.glob("seg-*.jsonl"))[-1].unlink()
    else:
        (archive / "index" / "linked.json").symlink_to(tmp_path / "outside.json")
    before = {item: item.read_bytes() for item in _transcript_files(root, thread.thread_id)}

    plan = service.plan(now=NOW)
    assert not [action for action in plan.actions if action.category == "conversation"]
    assert "MEMORY_RETENTION_CONVERSATION_TRANSCRIPT_INVALID" in {error.error_code for error in plan.errors}
    service.apply(now=NOW)
    assert {item: item.read_bytes() for item in _transcript_files(root, thread.thread_id)} == before
    assert store.threads.load(thread.thread_id) is not None


# LLM: 全局法律保留与按任务保留都必须让冷分卷与热文件一起留在原处。
# 函数用途: 验证扩展后的成员收集不绕过原 legal hold 门。
@pytest.mark.parametrize("hold", ["global", "task"])
def test_legal_hold_keeps_rotated_transcript(tmp_path, monkeypatch, hold):
    home, service = _owner(tmp_path)
    store, thread, _path, _original = _rotated_conversation(home, monkeypatch)
    root = store.storage.root.resolve()
    if hold == "task":
        store.tasks.bind({"thread_id": thread.thread_id, "task_id": "held-task", "status": "completed",
                          "now": OLD + 2000})
    _conversation_action(service.plan(now=NOW), thread.thread_id)
    policy = json.loads(home.owner_retention_json.read_text(encoding="utf-8"))
    if hold == "global":
        policy["legal_hold"] = True
    else:
        policy["legal_hold_task_ids"] = ["held-task"]
    home.owner_retention_json.write_text(json.dumps(policy, sort_keys=True), encoding="utf-8")
    before = {item: item.read_bytes() for item in _transcript_files(root, thread.thread_id)}

    applied = service.apply(now=NOW)

    assert not [action for action in applied.actions if action.category == "conversation"]
    assert {item: item.read_bytes() for item in _transcript_files(root, thread.thread_id)} == before


# LLM: 直接核对扫描器的成员清单：完整冷段与索引节点都在，锁侧车永不进入搬移集合。
# 函数用途: 锁定 _conversation_related_paths 的 canonical 成员口径。
def test_related_paths_enumerate_exact_transcript_family(tmp_path, monkeypatch):
    home, _service = _owner(tmp_path)
    store, thread, path, _original = _rotated_conversation(home, monkeypatch)
    root = store.storage.root.resolve()
    with locked_json_path(path):
        pass
    related, errors = scan._conversation_related_paths(root, store.threads.require(thread.thread_id))
    assert errors == []
    assert _transcript_files(root, thread.thread_id) <= set(related)
    assert not [item for item in related if item.name.endswith(".lock")]
    assert root / "threads" / f"{thread.thread_id}.json" in related
