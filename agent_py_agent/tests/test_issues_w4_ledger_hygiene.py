"""W4：真实writer、原锁和退休journal驱动的会话侧账退休；不访问真实owner。

来源：plan-w4.md W4-1至W4-6、C-WAKE-RETIREMENT与退休journal消费者水位规则。
做法：真实Store发布/处理/结案、真实guidance提交确认；故障只注入本用例目标unlink或目录枚举。
额外反例：部分提交后水位、跨片/提交时恢复、非法时间、超大回执饥饿、零预算写门。
复现：仓库根执行pytest本文件；完整文件列表与唯一目录合同冲突见交接ACK。
"""
from __future__ import annotations

import ast
import inspect
import json
import os
import time
from pathlib import Path
from threading import Event, Thread

import pytest

from agent_py_agent.agent.conversation.models import ConversationThread
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.conversation.store_wake_publication import signal_responsibility_guard
from agent_py_agent.agent.memory_store.retired_thread_journal import (
    append_retired_thread,
    iter_retired_threads,
)
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.agent.user_space.storage_lanes import (
    StorageLaneBudget,
    StorageLaneRequest,
    validate_lane_receipt,
)
from agent_py_agent.tests.test_wake_quarantine_archive import _quarantine

# 测试中一天对应的秒数。
DAY = 86400.0


# LLM: 每例独立真实home，只创建临时owner及规范会话根，不接触运行中的用户数据。
# 函数用途: 构造真实会话存储和线程。
def _owner(tmp_path, scope=None):
    home = ensure_my_agent_home(tmp_path / "home")
    root = (home.owner_home_dir / "conversations" if scope is None else
            home.owner_workspace_dir / "runtime" / "workspaces" / scope / "conversations")
    store = ConversationStore(root)
    store.threads.write(ConversationThread(thread_id="thread-a", canonical_user_id="test"))
    return home, store


# LLM: 通过原观察+唤醒事务发布，保留配对和retain_handled语义，不手造发布账。
# 函数用途: 创建一个可选已处理、永久保护或task门的真实唤醒。
def _signal(store, *, key="key", retain=False, handled=True, task=False):
    _, signal = store.wakes.append_observation(
        {"thread_id": "thread-a", "event_type": "test", "summary": "原观察"},
        {"thread_id": "thread-a", "reason": "session_task" if task else "test",
         "dedupe_key": key, "retain_handled": retain, "summary": "原唤醒",
         "metadata": {"session_task_id": "stask-test"} if task else {}},
    )
    if handled:
        store.wakes.mark_handled(signal.wake_signal_id)
    return signal


# LLM: 请求字段使用冻结顺序；只有显式commit时模拟协调器写门，不增加其他策略。
# 函数用途: 构造一片维护请求。
def _request(home, *, now, cursor=None, commit=None, budget=None, legal_hold=False):
    return StorageLaneRequest("conversation_ledgers", home, now, cursor or {},
        budget or StorageLaneBudget(None, None, None), legal_hold, commit is not None, None, commit)


# LLM: 每片原样接回游标并校验正式回执；有界循环失败直接报错，绝不绕过未完成状态。
# 函数用途: 持续执行当前测试的一轮维护并保留逐片回执。
def _drain(home, now, *, commit=None, cursor=None, budget=None):
    from agent_py_agent.agent.conversation.store_ledger_retention import (
        maintain_conversation_ledgers_lane,
    )

    receipts = []
    for _ in range(256):
        receipt = maintain_conversation_ledgers_lane(_request(home, now=now, cursor=cursor, commit=commit, budget=budget))
        validate_lane_receipt(receipt)
        assert len(json.dumps(receipt["cursor"]).encode()) < 16384
        receipts.append(receipt)
        cursor = receipt["cursor"]
        if receipt["complete"]:
            return receipts
    raise AssertionError("bounded lane failed to finish its pass")


# LLM: 汇总只使用实际回执，不把已检查条数当成释放条数。
# 函数用途: 合计同一测试各片的一项计量。
def _count(receipts, field, reason=None):
    return sum(row.get(field, {}).get(reason, 0) if reason else row.get(field, 0) for row in receipts)


# LLM: 必须真删dedupe后删handled，原发布入口随后能安装全新pending，精确核对物理计量。
# 函数用途: 验证普通配对唤醒安全退休并可再次发布。
def test_nonretained_pair_retires_dedupe_first_and_republishes(tmp_path, monkeypatch):
    home, store = _owner(tmp_path)
    signal = _signal(store)
    handled = store.storage.wake_handled_dir / (signal.wake_signal_id + ".json")
    dedupe = store.storage.wake_dedupe_path(signal.thread_id, signal.dedupe_key)
    sizes = handled.stat().st_size + dedupe.stat().st_size
    original, order = Path.unlink, []

    def unlink(path, *args, **kwargs):
        if path in (handled, dedupe):
            order.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    receipts = _drain(home, time.time() + 15 * DAY)
    assert order == [dedupe, handled]
    assert not handled.exists() and not dedupe.exists()
    assert _count(receipts, "deleted_items") == 2 and _count(receipts, "freed_bytes") == sizes
    assert store.wakes.delivery_receipt(signal.thread_id, signal.dedupe_key) == ""
    fresh = _signal(store, handled=False)
    assert fresh.wake_signal_id != signal.wake_signal_id
    assert store.wakes.delivery_receipt(signal.thread_id, signal.dedupe_key) == "pending"


# LLM: 永久幂等事实不是TTL垃圾，必须保持handled回执且零写门调用。
# 函数用途: 验证retain_handled保护整对原权威。
def test_retained_pair_remains_delivery_authority_without_commit(tmp_path):
    home, store = _owner(tmp_path)
    signal = _signal(store, retain=True)
    commits = []
    receipts = _drain(home, time.time() + 15 * DAY,
        commit=lambda operation: (commits.append(True), operation())[1])
    assert _count(receipts, "protected", "retain_handled") == 1
    assert store.wakes.delivery_receipt(signal.thread_id, signal.dedupe_key) == "handled"
    assert (store.storage.wake_handled_dir / (signal.wake_signal_id + ".json")).exists()
    assert commits == []


@pytest.mark.parametrize("age,removed", [(13, False), (15, True)])
# LLM: 固定14天阈值不因压力或文件不存在而缩短。
# 函数用途: 验证无key回执在13天保留、15天删除。
def test_unkeyed_handled_uses_fourteen_days(tmp_path, age, removed):
    home, store = _owner(tmp_path)
    signal = _signal(store, key="")
    _drain(home, time.time() + age * DAY)
    assert (store.storage.wake_handled_dir / (signal.wake_signal_id + ".json")).exists() is not removed


# LLM: 首次dedupe已删而handled删除失败时，不得留下指向缺失信号的发布回执。
# 函数用途: 验证真实部分I/O失败可以安全补完并再次发布。
def test_partial_pair_unlink_recovers_without_broken_publication(tmp_path, monkeypatch):
    home, store = _owner(tmp_path)
    signal = _signal(store)
    handled = store.storage.wake_handled_dir / (signal.wake_signal_id + ".json")
    dedupe = store.storage.wake_dedupe_path(signal.thread_id, signal.dedupe_key)
    original, failed = Path.unlink, []

    def fail_once(path, *args, **kwargs):
        if path == handled and not failed:
            failed.append(True)
            raise OSError("simulated handled unlink failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_once)
    first = _drain(home, time.time() + 15 * DAY)
    assert failed and not dedupe.exists() and handled.exists()
    assert any(row.get("errors") for row in first)
    assert store.wakes.delivery_receipt(signal.thread_id, signal.dedupe_key) == ""
    _drain(home, time.time() + 15 * DAY)
    assert not handled.exists()
    assert _signal(store, handled=False).status == "pending"


# LLM: 另一线程持原task门时只能计锁忙；释放后重跑同一真实维护入口必须可回收。
# 函数用途: 验证维护不会等待或越过原发布者锁。
def test_busy_original_task_guard_keeps_receipt_and_retries(tmp_path):
    home, store = _owner(tmp_path)
    signal = _signal(store, task=True)
    held, release = Event(), Event()
    failures = []

    def hold():
        try:
            with signal_responsibility_guard(store.storage, signal):
                held.set()
                assert release.wait(10)
        except BaseException as error:
            failures.append(error)

    thread = Thread(target=hold)
    thread.start()
    try:
        assert held.wait(10)
        rows = _drain(home, time.time() + 15 * DAY)
        assert _count(rows, "skipped", "lock_busy") == 1
        assert store.wakes.delivery_receipt(signal.thread_id, signal.dedupe_key) == "handled"
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive() and failures == []
    _drain(home, time.time() + 15 * DAY)
    assert store.wakes.delivery_receipt(signal.thread_id, signal.dedupe_key) == ""


# LLM: 用原policy writer与GC产生真实移动归档，满30天才删且复用伴生锁回收器。
# 函数用途: 验证老归档和孤儿锁回收、新归档保留。
def test_real_policy_archive_purge_and_moved_lock_cleanup(tmp_path):
    home, store = _owner(tmp_path)
    start = time.time()
    policy = store.progress.create({"thread_id": "thread-a", "task_id": "task-test", "interval_seconds": 30,
        "now": start - 8 * DAY - 60})
    store.progress.disable(policy.policy_id, now=start - 8 * DAY)
    store.gc_stale_ledger_records(now=start)
    archive = store.storage.root / ".ledger_archive" / "policies"
    old = archive / (policy.policy_id + ".json")
    assert old.exists()
    lock = Path(str(old) + ".lock")
    if not lock.exists():
        lock.write_text("")
    young = archive / "young.json"
    young.write_bytes(old.read_bytes())
    os.utime(young, (start + 2 * DAY, start + 2 * DAY))
    _drain(home, start + 31 * DAY)
    assert not old.exists() and not lock.exists()
    assert young.exists()


# LLM: 结案归档只在没有dedupe时清主档和ledger，重放次数权威始终原样。
# 函数用途: 验证quarantine归档保护与删除边界。
def test_quarantine_archive_keeps_dedupe_and_never_reads_replayed(tmp_path):
    from agent_py_agent.agent.conversation.store_wake_quarantine_archive import (
        archive_stale_wake_quarantine,
    )

    home, store = _owner(tmp_path)
    loose = _signal(store, key="", handled=False)
    retained = _signal(store, key="quarantine-key", handled=False)
    for signal in (loose, retained):
        _quarantine(store, signal)
    assert archive_stale_wake_quarantine(store.storage, current=time.time() + 15 * DAY) == 2
    archive = store.storage.wake_quarantine_archive_dir
    ledger = archive / "ledger" / (loose.wake_signal_id + ".json")
    ledger.parent.mkdir(exist_ok=True)
    ledger.write_text("original ledger")
    replayed = store.storage.wake_replayed_dir / "wake-kept"
    replayed.mkdir(parents=True)
    (replayed / "1.json").write_text("original replay authority")
    rows = _drain(home, time.time() + 31 * DAY)
    assert not store.storage.wake_quarantine_archive_path(loose.wake_signal_id).exists() and not ledger.exists()
    assert store.storage.wake_quarantine_archive_path(retained.wake_signal_id).exists()
    assert _count(rows, "protected", "quarantine_dedupe") == 1
    assert (replayed / "1.json").read_text() == "original replay authority"


# LLM: consumed通过真实认领、提交及模型确认产生，批次权威不能由测试伪造。
# 函数用途: 创建一条指定结算状态的guidance及其精确引用。
def _guidance(store, key, *, target="thread-a", status="consumed"):
    turn, input_id = "turn-" + key, "input-" + key
    entry = store.guidance.append_once({"target_type": "thread", "target_id": target,
        "message": "原消息", "metadata": {"expected_turn_id": turn, "gateway_input_request_id": input_id}}, dedupe_key=key)
    if status == "rejected":
        store.guidance.mark_status(key, "rejected")
    elif status == "consumed":
        assert store.guidance.claim_for_turn(entry, expected_turn_id=turn, attempt_id=turn)
        store.guidance.submissions.mark_submitted(turn, [entry], attempt_id=turn, provider_call_id="provider-" + key)
        store.guidance.acknowledgements.consume_submitted(turn, [entry], provider_call_id="provider-" + key)
    return store.storage.guidance_dedupe_path(key), store.storage.guidance_turn_index_path(turn, key), store.storage.guidance_input_index_path(input_id)


# LLM: 只在本例临时home中删除线程并写正式退休journal，waiting由真实trash目录表示。
# 函数用途: 构造可验证的purged或waiting退休证明。
def _retired(home, store, thread_id="thread-a", *, waiting=False):
    path = store.storage.thread_path(thread_id)
    if path.exists():
        path.unlink()
    container = home.owner_data_dir / "trash" / ("retired-" + thread_id)
    if waiting:
        container.mkdir(parents=True, exist_ok=True)
    append_retired_thread(home, thread_id=thread_id, conversation_root=store.storage.root,
        action_id="retire-" + thread_id, trash_container=container, retired_at=time.time())
    return container


@pytest.mark.parametrize("status", ["consumed", "rejected"])
# LLM: 只清已结算侧记录，顺序为turn/input引用再receipt，pending和模型批次权威都保留。
# 函数用途: 验证guidance退休的精确对象、顺序和保留集。
def test_guidance_requires_purged_proof_and_unlinks_references_first(tmp_path, monkeypatch, status):
    home, store = _owner(tmp_path)
    receipt, turn, input_path = _guidance(store, "settled", status=status)
    pending, _, _ = _guidance(store, "pending", status="pending")
    batches = {path: path.read_bytes() for directory in (store.storage.guidance_ack_batches_dir,
        store.storage.guidance_submission_batches_dir) for path in directory.rglob("*.json")}
    _retired(home, store)
    original, removed = Path.unlink, []

    def unlink(path, *args, **kwargs):
        if path in (receipt, turn, input_path):
            removed.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    rows = _drain(home, time.time() + 31 * DAY)
    assert removed == [turn, input_path, receipt]
    assert pending.exists() and _count(rows, "protected", "guidance_unsettled") == 1
    assert {path: path.read_bytes() for path in batches} == batches
    if batches:
        assert _count(rows, "skipped", "guidance_batches") >= 1


# LLM: 前一purged可消费，但水位必须停在waiting前；真实trash移除后同一续点再处理。
# 函数用途: 验证journal屏障和后续恢复清理。
def test_guidance_waiting_watermark_and_later_purge(tmp_path):
    home, store = _owner(tmp_path)
    first = _guidance(store, "first")
    _retired(home, store)
    boundary = iter_retired_threads(home, max_rows=1).next_offset
    store.threads.write(ConversationThread(thread_id="thread-b", canonical_user_id="test"))
    second = _guidance(store, "second", target="thread-b")
    trash = _retired(home, store, "thread-b", waiting=True)
    rows = _drain(home, time.time() + 31 * DAY)
    assert not first[0].exists() and second[0].exists()
    assert rows[-1]["cursor"]["journal_offset"] == boundary
    trash.rmdir()
    following = _drain(home, time.time() + 31 * DAY, cursor=rows[-1]["cursor"])
    assert not second[0].exists()
    assert following[-1]["cursor"]["journal_offset"] > boundary


# LLM: 线程叶缺失本身不是退休授权，任意长TTL也不能替代正面journal证明。
# 函数用途: 验证不会按孤儿猜测删guidance。
def test_absent_thread_without_journal_never_authorizes_guidance_retirement(tmp_path):
    home, store = _owner(tmp_path)
    receipt, _, _ = _guidance(store, "untracked")
    store.storage.thread_path("thread-a").unlink()
    _drain(home, time.time() + 366 * DAY)
    assert receipt.exists()


# LLM: 不同workspace根都应被处理，单条预算每片不超量且游标受正式校验。
# 函数用途: 验证多会话根的有界续扫。
def test_two_roots_resume_with_single_item_slices(tmp_path):
    home, first = _owner(tmp_path, "scope-a")
    _, second = _owner(tmp_path, "scope-b")
    old = [_signal(store, key="") for store in (first, second)]
    rows = _drain(home, time.time() + 15 * DAY, budget=StorageLaneBudget(1, None, None))
    for store, signal in zip((first, second), old, strict=True):
        assert not (store.storage.wake_handled_dir / (signal.wake_signal_id + ".json")).exists()
    assert _count(rows, "deleted_items") == 2
    assert all(row["items"] <= 1 for row in rows)


# LLM: 空owner无合格变更，legal_hold则连枚举和改写也不允许，所有原文件字节必须保留。
# 函数用途: 验证两种零写门、零删除路径。
def test_empty_owner_and_legal_hold_make_zero_commits(tmp_path):
    from agent_py_agent.agent.conversation.store_ledger_retention import (
        maintain_conversation_ledgers_lane,
    )

    home = ensure_my_agent_home(tmp_path / "home")
    commits = []
    def commit(operation):
        commits.append(True)
        return operation()
    assert _drain(home, time.time() + 366 * DAY, commit=commit)[-1]["complete"]
    _, store = _owner(tmp_path)
    _signal(store)
    before = {p: p.read_bytes() for p in store.storage.root.rglob("*") if p.is_file()}
    row = maintain_conversation_ledgers_lane(_request(home, now=time.time() + 366 * DAY, commit=commit, legal_hold=True))
    assert row["complete"] and row["deferred_reason"] == "legal_hold" and row["items"] == row["bytes"] == 0
    assert {p: p.read_bytes() for p in before} == before
    assert commits == []


@pytest.mark.parametrize("kind", ["unreadable", "legacy", "retained", "new-generation", "unknown-schema"])
# LLM: 每个分支核对原dedupe字节；不同代只能删旧handled，其他未知或受保护分支必须两者保留。
# 函数用途: 覆盖dedupe完整决策表及未知格式保护。
def test_dedupe_decision_table_and_unknown_preservation(tmp_path, kind):
    home, store = _owner(tmp_path)
    signal = _signal(store)
    path = store.storage.wake_dedupe_path(signal.thread_id, signal.dedupe_key)
    record = json.loads(path.read_text())
    if kind == "unreadable":
        path.write_text("{unreadable")
    elif kind == "legacy":
        path.write_text(json.dumps({"schema_version": "wake_dedupe.v1", "thread_id": signal.thread_id,
            "dedupe_key": signal.dedupe_key, "wake_signal_id": signal.wake_signal_id}))
    elif kind == "retained":
        path.write_text(json.dumps({**record, "retain_handled": True}))
    elif kind == "unknown-schema":
        path.write_text(json.dumps({**record, "schema_version": "wake_dedupe.future"}))
    else:
        newest = _signal(store, handled=False)
        assert newest.wake_signal_id != signal.wake_signal_id
    before = path.read_bytes()
    rows = _drain(home, time.time() + 15 * DAY)
    assert path.read_bytes() == before
    old = store.storage.wake_handled_dir / (signal.wake_signal_id + ".json")
    assert old.exists() is (kind != "new-generation")
    if kind != "new-generation":
        assert sum(sum(row.get("protected", {}).values()) for row in rows) >= 1


# LLM: 满30天的坏handled只报警不删除，错误引用不能逃出owner。
# 函数用途: 验证不可读回执保留和结构化错误路径。
def test_unreadable_old_handled_is_kept_with_owner_relative_error(tmp_path):
    home, store = _owner(tmp_path)
    signal = _signal(store)
    handled = store.storage.wake_handled_dir / (signal.wake_signal_id + ".json")
    handled.write_text("{bad")
    rows = _drain(home, time.time() + 31 * DAY)
    errors = [error for row in rows for error in row.get("errors", [])]
    assert handled.exists() and any(error["code"] == "handled_wake_unreadable" for error in errors)
    assert all(not Path(error["path"]).is_absolute() and ".." not in Path(error["path"]).parts for error in errors)


# LLM: 外部文件不能因owner内的链接而获得退休授权；保留链接与目标原字节。
# 函数用途: 验证handled链接保护。
def test_symlink_handled_is_never_followed_or_unlinked(tmp_path):
    home, store = _owner(tmp_path)
    signal = _signal(store, key="")
    handled = store.storage.wake_handled_dir / (signal.wake_signal_id + ".json")
    original = handled.read_bytes()
    external = tmp_path / "external.json"
    handled.rename(external)
    handled.symlink_to(external)
    _drain(home, time.time() + 366 * DAY)
    assert handled.is_symlink() and external.read_bytes() == original


# LLM: 只注入精确的nlink=2元数据来验证防护分支；本环境真实os.link建fixture已返回EPERM，不宣称实机硬链接验收。
# 函数用途: 验证多链接普通文件被保护且不触发写门、读取或释放计量。
def test_multilink_metadata_is_protected_without_commit(tmp_path, monkeypatch):
    home, store = _owner(tmp_path)
    signal = _signal(store, key="")
    path = store.storage.wake_handled_dir / (signal.wake_signal_id + ".json")
    original, snapshot, commits = os.lstat, path.read_bytes(), []

    def lstat(candidate, *args, **kwargs):
        info = original(candidate, *args, **kwargs)
        if candidate == path:
            values = list(info)
            values[3] = 2
            return os.stat_result(values)
        return info

    def commit(operation):
        commits.append(True)
        return operation()

    monkeypatch.setattr(os, "lstat", lstat)
    rows = _drain(home, time.time() + 366 * DAY, commit=commit)
    assert _count(rows, "protected", "unsafe_path") == 1
    assert _count(rows, "deleted_items") == _count(rows, "freed_bytes") == _count(rows, "bytes") == 0
    assert commits == [] and path.read_bytes() == snapshot
    assert original(path).st_nlink == 1


# LLM: 冻结入口只收一个request，源码不能调用会迁移的writer或递归扫描来偷偷扩范围。
# 函数用途: 钉住维护入口签名和只读解析边界。
def test_lane_source_keeps_frozen_writers_and_single_request_signature():
    from agent_py_agent.agent.conversation import store_ledger_retention as module

    signature = inspect.signature(module.maintain_conversation_ledgers_lane)
    assert tuple(signature.parameters) == ("request",)
    source = inspect.getsource(module)
    tree = ast.parse(source)
    calls = {node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert not calls & {"read_receipt", "write_receipt", "publish_deduped", "raise_signal", "mark_handled", "rglob", "walk"}


# LLM: journal水位不能丢掉失败删除的正面证明，部分引用已删也必须能恢复。
# 函数用途: 钉住guidance一次unlink失败后的跨片重试。
def test_guidance_unlink_failure_preserves_journal_retry(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.store_ledger_retention import (
        maintain_conversation_ledgers_lane,
    )

    home, store = _owner(tmp_path)
    receipt, turn, input_path = _guidance(store, "retry")
    _retired(home, store)
    original, failed = Path.unlink, []

    def fail_once(path, *args, **kwargs):
        if path == receipt and not failed:
            failed.append(True)
            raise OSError("simulated guidance receipt failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_once)
    cursor = None
    for _ in range(32):
        row = maintain_conversation_ledgers_lane(_request(home, now=time.time() + 31 * DAY, cursor=cursor))
        cursor = row["cursor"]
        if row.get("errors"):
            break
    assert failed and receipt.exists() and not turn.exists() and not input_path.exists()
    assert row["cursor"]["journal_offset"] == 0
    _drain(home, time.time() + 31 * DAY, cursor=row["cursor"])
    assert not receipt.exists()


# LLM: 恢复batch不能只信游标中的线程ID，必须重新读原退休证明。
# 函数用途: 证明跨片恢复成活线程时不会删除其已结算guidance。
def test_guidance_restored_between_slices_is_preserved(tmp_path):
    from agent_py_agent.agent.conversation.store_ledger_retention import (
        maintain_conversation_ledgers_lane,
    )

    home, store = _owner(tmp_path)
    receipt, _, _ = _guidance(store, "restore")
    _retired(home, store)
    cursor = None
    for _ in range(32):
        row = maintain_conversation_ledgers_lane(_request(home, now=time.time() + 31 * DAY, cursor=cursor,
            budget=StorageLaneBudget(1, None, None)))
        cursor = row["cursor"]
        if cursor.get("batch"):
            break
    assert cursor["batch"] and receipt.exists()
    store.threads.write(ConversationThread(thread_id="thread-a", canonical_user_id="test"))
    _drain(home, time.time() + 31 * DAY, cursor=cursor)
    assert receipt.exists()


# LLM: 相同线程ID在不同会话根不是同一权威，只有journal点名的根获准退休。
# 函数用途: 验证guidance正面证明不会跨workspace根串用。
def test_guidance_purged_proof_is_root_specific(tmp_path):
    home, first = _owner(tmp_path, "a")
    _, second = _owner(tmp_path, "b")
    first_receipt, _, _ = _guidance(first, "same-key", status="rejected")
    second_receipt, _, _ = _guidance(second, "same-key", status="rejected")
    _retired(home, first)
    _drain(home, time.time() + 31 * DAY)
    assert not first_receipt.exists() and second_receipt.exists()


@pytest.mark.parametrize("stamp", [None, 0, True, float("nan"), float("inf"), "invalid"])
# LLM: 非法handled_at不能当作完成时刻；按较新文件时间满14天后才退。
# 函数用途: 覆盖无效业务时间的安全文件年龄回退。
def test_invalid_handled_time_uses_file_age(tmp_path, stamp):
    home, store = _owner(tmp_path)
    signal = _signal(store, key="")
    path = store.storage.wake_handled_dir / (signal.wake_signal_id + ".json")
    raw = json.loads(path.read_text())
    path.write_text(json.dumps({**raw, "handled_at": stamp}))
    _drain(home, time.time() + 13 * DAY)
    assert path.exists()
    _drain(home, time.time() + 15 * DAY)
    assert not path.exists()


# LLM: 单文件大于整个片预算不能永远堵住同目录其它可删除记录，也不能越限读取。
# 函数用途: 验证超大handled保护且后续记录继续处理。
def test_oversize_handled_does_not_starve_later_receipt(tmp_path):
    from agent_py_agent.agent.user_space.storage_lanes import STORAGE_LANE_SLICE_BYTES

    home, store = _owner(tmp_path)
    signals = [_signal(store, key="") for _ in range(2)]
    first, second = sorted(store.storage.wake_handled_dir / (signal.wake_signal_id + ".json") for signal in signals)
    raw = json.loads(first.read_text())
    first.write_text(json.dumps({**raw, "padding": "x" * (STORAGE_LANE_SLICE_BYTES + 1)}))
    rows = _drain(home, time.time() + 31 * DAY)
    assert first.exists() and not second.exists()
    assert all(row["bytes"] <= STORAGE_LANE_SLICE_BYTES for row in rows)
    assert _count(rows, "protected", "oversize") == 1


# LLM: 已耗尽的预算或不足一个JSON的预算均不能触发写门，也不越过当前候选。
# 函数用途: 钉住零额度和小字节额度的无副作用行为。
def test_exhausted_and_small_budgets_do_not_commit(tmp_path):
    from agent_py_agent.agent.conversation.store_ledger_retention import (
        maintain_conversation_ledgers_lane,
    )

    home, store = _owner(tmp_path)
    signal = _signal(store)
    commits = []

    def commit(operation):
        commits.append(True)
        return operation()

    for budget in (StorageLaneBudget(0, None, None), StorageLaneBudget(None, 0, None),
                   StorageLaneBudget(None, None, time.monotonic() - 1), StorageLaneBudget(None, 1, None)):
        row = maintain_conversation_ledgers_lane(_request(home, now=time.time() + 31 * DAY, budget=budget, commit=commit))
        assert not row["complete"] and row["deleted_items"] == row["bytes"] == 0
    assert commits == []
    assert store.wakes.delivery_receipt(signal.thread_id, signal.dedupe_key) == "handled"


# LLM: journal已证明purged也不代表目录已成功检查，枚举失败不能消费该批水位。
# 函数用途: 钉住guidance目录读失败后保留证明、下一片重试。
def test_guidance_directory_error_preserves_journal_retry(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.store_ledger_retention import (
        maintain_conversation_ledgers_lane,
    )

    home, store = _owner(tmp_path)
    receipt, _, _ = _guidance(store, "directory-retry", status="rejected")
    _retired(home, store)
    original, failed = os.scandir, []

    def fail_once(path):
        if Path(path) == store.storage.guidance_dedupe_dir and not failed:
            failed.append(True)
            raise OSError("simulated guidance directory read failure")
        return original(path)

    monkeypatch.setattr(os, "scandir", fail_once)
    cursor = None
    for _ in range(32):
        row = maintain_conversation_ledgers_lane(_request(home, now=time.time() + 31 * DAY, cursor=cursor))
        cursor = row["cursor"]
        if failed:
            break
    assert failed and receipt.exists()
    assert cursor["journal_offset"] == 0
    _drain(home, time.time() + 31 * DAY, cursor=cursor)
    assert not receipt.exists()


# LLM: 候选预读后线程可能恢复，写门内第一处unlink前仍须复核原证明四态。
# 函数用途: 验证恢复发生在提交窗口中时保留所有guidance引用。
def test_guidance_restored_at_commit_keeps_all_references(tmp_path):
    home, store = _owner(tmp_path)
    paths = _guidance(store, "commit-restore", status="rejected")
    _retired(home, store)
    commits = []

    def commit(operation):
        commits.append(True)
        store.threads.write(ConversationThread(thread_id="thread-a", canonical_user_id="test"))
        return operation()

    _drain(home, time.time() + 31 * DAY, commit=commit)
    assert commits == [True]
    assert all(path.exists() for path in paths)


# LLM: 证明集合很小不代表物理行很小；超大journal行不能突破本片8MiB读取上限。
# 函数用途: 把独立复核的原始字节预算问题落实成真实文件反例。
def test_journal_raw_line_obeys_byte_budget(tmp_path):
    from agent_py_agent.agent.conversation.store_ledger_retention import (
        maintain_conversation_ledgers_lane,
    )
    from agent_py_agent.agent.user_space.storage_lanes import STORAGE_LANE_SLICE_BYTES

    home, store = _owner(tmp_path)
    receipt, _, _ = _guidance(store, "large-journal", status="rejected")
    _retired(home, store)
    journal = home.owner_data_dir / "retention" / "retired_threads.jsonl"
    journal.write_bytes(b" " * (STORAGE_LANE_SLICE_BYTES + 1) + journal.read_bytes())
    row = maintain_conversation_ledgers_lane(_request(home, now=time.time() + 31 * DAY))
    validate_lane_receipt(row)
    assert row["bytes"] <= STORAGE_LANE_SLICE_BYTES
    assert row["cursor"]["journal_offset"] == 0 and receipt.exists()


# LLM: 原try-flock/inode清扫协议不变，但提供给它的候选也必须在单片预算内。
# 函数用途: 证明大量老孤儿锁可以分片回收而不一次删完。
def test_archive_orphan_lock_cleanup_is_sliced(tmp_path):
    from agent_py_agent.agent.conversation.store_ledger_retention import (
        maintain_conversation_ledgers_lane,
    )

    home, store = _owner(tmp_path)
    archive = store.storage.root / ".ledger_archive" / "policies"
    archive.mkdir(parents=True)
    locks = []
    for index in range(13):
        path = archive / (str(index) + ".json.lock")
        path.write_text("original lock")
        locks.append(path)
    row = maintain_conversation_ledgers_lane(_request(home, now=time.time() + 31 * DAY,
        budget=StorageLaneBudget(1, None, None)))
    assert row["items"] <= 1 and row["deleted_items"] <= 1
    rows = [row, *_drain(home, time.time() + 31 * DAY, cursor=row["cursor"], budget=StorageLaneBudget(1, None, None))]
    assert all(part["items"] <= 1 and part["deleted_items"] <= 1 for part in rows)
    assert all(not path.exists() for path in locks)
    assert _count(rows, "freed_bytes") == sum(len(b"original lock") for _ in locks)


# LLM: 完整游标而不只是batch必须小于16KiB；不可信超长after只可重置扫描，不能截断并跳过记录。
# 函数用途: 验证恢复输入中的超大游标会安全重扫且仍能清掉合格记录。
def test_oversize_input_cursor_resets_without_losing_proof(tmp_path):
    from agent_py_agent.agent.conversation.store_ledger_retention import (
        maintain_conversation_ledgers_lane,
    )
    from agent_py_agent.agent.user_space.storage_lanes import STORAGE_LANE_CURSOR_MAX_BYTES

    home, store = _owner(tmp_path)
    receipt, _, _ = _guidance(store, "large-cursor", status="rejected")
    _retired(home, store)
    cursor = {"root": 0, "phase": "guidance", "after": "x" * STORAGE_LANE_CURSOR_MAX_BYTES,
              "journal_offset": 0, "batch": {}}
    row = maintain_conversation_ledgers_lane(_request(home, now=time.time() + 31 * DAY, cursor=cursor,
        budget=StorageLaneBudget(1, None, None)))
    validate_lane_receipt(row)
    assert len(json.dumps(row["cursor"]).encode()) <= STORAGE_LANE_CURSOR_MAX_BYTES
    _drain(home, time.time() + 31 * DAY, cursor=row["cursor"])
    assert not receipt.exists()


# LLM: 每行状态检查前后都应核同一个deadline，不能整页处理完才发现已经超时。
# 函数用途: 用受控单调时钟验证journal状态检查是协作式有界的。
def test_journal_state_checks_stop_at_slice_deadline(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import store_ledger_retention as module
    from agent_py_agent.agent.user_space import storage_lanes

    home, store = _owner(tmp_path)
    for index in range(10):
        thread_id = "thread-old-" + str(index)
        store.threads.write(ConversationThread(thread_id=thread_id, canonical_user_id="test"))
        _retired(home, store, thread_id)
    clock, checks = [0.0], []
    original = module.retired_thread_state

    def state(home, row):
        checks.append(row["thread_id"])
        clock[0] += 0.01
        return original(home, row)

    monkeypatch.setattr(storage_lanes.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module, "retired_thread_state", state)
    row = module.maintain_conversation_ledgers_lane(_request(home, now=time.time() + 31 * DAY))
    assert len(checks) <= 3
    assert not row["complete"]
