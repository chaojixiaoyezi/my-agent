# LLM: 旧受管派单只按原thread/task/request/wake身份交接；持久回执先记意图，实读证实规范发布后才经调用方原提交门处理旧唤醒。
#   活动、已领取、不可读或身份冲突的旧工作原样保留；不新建身份、不重投已消费工作、不改写旧任务。
# 模块用途: 把旧owner直写会话库中待处理的管理员派单有界迁入唯一规范受管会话库。
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

HANDOFF_SCHEMA = "managed_dispatch_handoff.v1"
_HANDOFF_PARTS = ("data", "managed_dispatch_handoffs")
# 单个旧线程每拍最多复制16个物理历史位置，原max_ms同时限制墙钟。
_HISTORY_COPY_SLICE_ITEMS_COUNT = 16
_DISPATCH_REASON = "admin_dispatched_task"
_DISPATCH_FIELDS = ("dispatched_by", "target_owner_id", "execution_owner_id", "request_id", "task_id")


# LLM: 与插件宿主/受管运行时同一解析：根为owner home，结果是唯一规范受管会话库；不读配置覆盖。
# 函数用途: 返回受管用户唯一规范会话库目录，不创建目录。
def canonical_conversation_root(home) -> Path:
    from .runtime_paths import runtime_paths_for_agent

    scoped = SimpleNamespace(owner_home_dir=home.home_dir, owner_workspace_dir=home.workspace_dir)
    return runtime_paths_for_agent(SimpleNamespace(), home.home_dir, scoped)["conversation_workspace"]


# LLM: 旧派单曾直写owner/conversations；该库只作交接来源与保留工作的只读可见性，不再接新派单。
# 函数用途: 返回旧owner直写会话库目录。
def legacy_conversation_root(home) -> Path:
    return home.home_dir / "conversations"


# LLM: 先以nofollow目录链核实旧库仍在owner home内；缺失返回None，不创建目录。
# 函数用途: 只读打开旧owner直写会话库。
def open_legacy_conversation_store(home):
    from ..common.nofollow_fs import open_directory_beneath
    from ..conversation.store import ConversationStore

    root = legacy_conversation_root(home)
    try:
        os.close(open_directory_beneath(home.root, root.relative_to(home.root).parts))
    except FileNotFoundError:
        return None
    return ConversationStore(root, initialize=False)


# LLM: 规范库目录链nofollow私有创建后才打开；不跟随链接也不从线程元数据选库。
# 函数用途: 打开受管用户唯一规范会话库。
def open_canonical_conversation_store(home, *, initialize: bool):
    from ..common.nofollow_fs import ensure_private_dir, open_directory_beneath
    from ..conversation.store import ConversationStore

    root = canonical_conversation_root(home)
    if initialize:
        ensure_private_dir(root, canonical_root=home.root)
    try:
        os.close(open_directory_beneath(home.root, root.relative_to(home.root).parts))
    except FileNotFoundError:
        if initialize:
            raise
    return ConversationStore(root, initialize=initialize)


# LLM: 每次最多交接limit条、耗时预算max_ms；commit_transition是调用方原短提交门，规范发布与旧唤醒处理都只经它写入。
#   单条失败只保留并报告该原工作，不扩散为其它owner或其它派单的失败；超出预算标记truncated留待下一轮。
# 函数用途: 将旧直写库中尚未开始的管理员派单交接到规范库，并在规范发布实读成立后处理旧唤醒。
def reconcile_managed_dispatches(home, *, commit_transition, limit=32, max_ms=50):
    from ..runtime_errors import DataCorruptionError

    if not callable(commit_transition) or type(limit) is not int or limit < 1:
        raise ValueError("invalid managed dispatch reconcile options")
    if type(max_ms) not in (int, float) or max_ms <= 0:
        raise ValueError("invalid managed dispatch reconcile budget")
    report = {"handed_off": [], "retained": [], "load_errors": 0, "truncated": False}
    if legacy_conversation_root(home) == canonical_conversation_root(home):
        return report
    legacy = open_legacy_conversation_store(home)
    if legacy is None:
        return report
    deadline = time.monotonic() + max_ms / 1000.0
    signals, errors = legacy.wakes.pending_report(limit=0)
    report["load_errors"] = len(errors)
    handled = 0
    for signal in signals:
        if signal.reason != _DISPATCH_REASON:
            continue
        if handled >= limit or time.monotonic() >= deadline:
            report["truncated"] = True
            break
        handled += 1
        try:
            reason = _hand_off(home, legacy, signal, commit_transition, deadline=deadline)
        except (OSError, ValueError, KeyError, TypeError, DataCorruptionError) as error:
            reason = "handoff_error:" + type(error).__name__
        if reason:
            if reason == "history_copy_pending":
                report["truncated"] = True
            report["retained"].append({"task_id": signal.root_task_id, "wake_signal_id": signal.wake_signal_id,
                                       "reason": reason})
        else:
            report["handed_off"].append(signal.root_task_id)
        if reason == "history_copy_pending":
            break
    return report


# LLM: 只接受原派单写下的五个身份字段且互相一致；任何缺失或冲突都不是可交接的准确派单。
# 函数用途: 从旧唤醒还原原派单身份元数据。
def _dispatch_identity(home, signal):
    from ..common.opaque_id import validate_opaque_id

    metadata = signal.metadata if isinstance(signal.metadata, dict) else {}
    identity = {key: metadata.get(key) for key in _DISPATCH_FIELDS}
    task_id = str(signal.root_task_id or "")
    if (identity["dispatched_by"] != "local/main" or identity["target_owner_id"] != home.owner_id
            or identity["execution_owner_id"] != home.owner_id or identity["task_id"] != task_id
            or signal.dedupe_key != task_id or type(identity["request_id"]) is not str or not identity["request_id"]):
        return None
    validate_opaque_id(task_id, kind="task_id")
    validate_opaque_id(identity["request_id"], kind="request_id")
    return identity


# LLM: 已开始、已结束、已领取或不可读的旧工作都属活动/不确定，必须保留，不能交接后造成第二次执行。
# 函数用途: 判断旧派单是否仍是可安全交接的未开始排队工作。
def _legacy_retention_reason(legacy, signal, task_id):
    link, error = legacy.tasks.load_report(task_id)
    if error is not None:
        return "legacy_task_unreadable"
    if link is None or link.thread_id != signal.thread_id:
        return "legacy_task_missing"
    if link.status != "queued":
        return "legacy_task_" + str(link.status or "unknown")
    claim, error = legacy.claims.load_report(signal.thread_id)
    if error is not None:
        return "legacy_claim_unreadable"
    if claim.get("status") == "running":
        return "legacy_thread_claimed"
    if legacy.wakes.attempts.has_ledger(signal.wake_signal_id):
        return "legacy_wake_attempted"
    return ""


# LLM: 回执跨进程锁覆盖本派单完整交接；阶段prepared→published→completed单调推进，冲突身份或损坏回执保留原工作。
# 函数用途: 交接一条旧派单；返回空串表示已完成交接，否则返回保留原因。
def _hand_off(home, legacy, signal, commit_transition, *, deadline=None):
    from ..common.json_io import locked_json_path
    from ..common.nofollow_fs import ensure_private_dir

    identity = _dispatch_identity(home, signal)
    if identity is None:
        return "not_exact_dispatch"
    task_id = identity["task_id"]
    path = home.home_dir.joinpath(*_HANDOFF_PARTS, task_id + ".json")
    ensure_private_dir(path.parent, canonical_root=home.root)
    expected = {"schema": HANDOFF_SCHEMA, "owner_id": home.owner_id, "task_id": task_id,
                "request_id": identity["request_id"], "thread_id": signal.thread_id,
                "legacy_wake_signal_id": signal.wake_signal_id,
                "legacy_store": legacy_conversation_root(home).relative_to(home.home_dir).as_posix(),
                "canonical_store": canonical_conversation_root(home).relative_to(home.home_dir).as_posix()}
    with locked_json_path(path):
        receipt = _read_receipt(path)
        if receipt is not None and {key: receipt.get(key) for key in expected} != expected:
            return "handoff_receipt_conflict"
        phase = receipt.get("phase") if receipt is not None else ""
        if phase not in {"", "prepared", "published", "completed"}:
            return "handoff_receipt_invalid"
        if phase in {"", "prepared"}:
            reason = _legacy_retention_reason(legacy, signal, task_id)
            if reason:
                return reason
            prepared = {**expected, "phase": "prepared", "updated_at": time.time(),
                        **({"history_copy": receipt["history_copy"]} if receipt is not None and "history_copy" in receipt else {})}
            ready = commit_transition(lambda: _prepare_and_publish(path, phase, prepared, home, legacy, signal, identity,
                                                                  deadline=deadline))
            if ready is False:
                return "history_copy_pending"
        status = _verified_canonical_status(home, signal)
        if status not in {"pending", "handled"}:
            return "canonical_publication_unverified"
        published = {**expected, "phase": "published", "canonical_receipt": status, "updated_at": time.time()}
        commit_transition(lambda: _retire_legacy_wake(path, published, legacy, signal))
    return ""


# LLM: 严格读取，缺失为None；损坏或非对象回执原样报错，不能当作无交接。
# 函数用途: 读取一条持久交接回执。
def _read_receipt(path):
    from ..runtime_errors import DataCorruptionError

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise DataCorruptionError("managed dispatch handoff receipt is unreadable") from error
    if not isinstance(value, dict):
        raise DataCorruptionError("managed dispatch handoff receipt is invalid")
    return value


# LLM: 在原提交门内先落prepared意图，再按线程→消息→任务→唤醒发布；每步按原身份幂等，已存在的规范发布不重写。
#   规范任务与新派单同为可运行active；仅把从未运行过的queued原链接CAS为active，终态或其它状态原样保留不复活。
# 函数用途: 记录交接意图并把旧派单按原身份发布到规范库。
def _prepare_and_publish(path, phase, prepared, home, legacy, signal, identity, *, deadline=None):
    from ..common.json_io import write_private_json_file_atomic_unlocked
    from ..conversation.store_wake_publication import publish_deduped

    if not phase:
        write_private_json_file_atomic_unlocked(path, prepared)
    canonical = open_canonical_conversation_store(home, initialize=True)
    task_id, thread_id = identity["task_id"], signal.thread_id
    receipt = canonical.wakes.delivery_receipt(thread_id, task_id)
    if receipt == "handled":
        # 规范库同键唤醒已结清：已消费工作不重投。
        return
    if receipt == "pending":
        # 旧双写已在规范库完整发布本派单（含其原唤醒），保留该唯一规范执行，不再发布第二份。
        canonical.tasks.update_status({"task_id": task_id, "status": "active", "expected_status": "queued"})
        return
    thread = canonical.threads.load(thread_id)
    if thread is None:
        from ..common.json_io import locked_json_path

        with locked_json_path(legacy.storage.thread_path(thread_id)):
            thread = canonical.threads.load(thread_id)
            if thread is None:
                thread = legacy.threads.require(thread_id)
                _require_owner_thread(home, thread)
                if not _copy_history_slice(path, prepared, legacy, canonical, thread, deadline):
                    return False
                if deadline is not None and time.monotonic() >= deadline:
                    return False
                from ..conversation.task_history import PAGE_SIZE

                thread = replace(thread, task_ids=thread.task_ids[-PAGE_SIZE:], task_history_version=1)
                canonical.threads.write(thread)
    _require_owner_thread(home, thread)
    link = legacy.tasks.load(task_id)
    metadata = {key: identity[key] for key in _DISPATCH_FIELDS}
    canonical.messages.append_once({"thread_id": thread_id, "role": "user", "content": link.goal,
                                    "channel": "internal", "metadata": metadata},
                                   dedupe_key="managed-dispatch:" + task_id)
    if canonical.tasks.load(task_id) is None:
        canonical.tasks.bind({"thread_id": thread_id, "task_id": task_id, "request_id": identity["request_id"],
                              "goal": link.goal, "status": "active"})
    else:
        canonical.tasks.update_status({"task_id": task_id, "status": "active", "expected_status": "queued"})
    publish_deduped(canonical.storage, replace(signal, status="pending", handled_at=0.0))
    return True


# LLM: 只读核对规范库线程归属、任务链接和同键唤醒发布回执；prepared或缺失都不算已发布。
# 函数用途: 返回规范发布的唤醒回执状态，未证实时返回空串。
def _verified_canonical_status(home, signal):
    canonical = open_canonical_conversation_store(home, initialize=False)
    thread = canonical.threads.load(signal.thread_id)
    if thread is None:
        return ""
    _require_owner_thread(home, thread)
    link = canonical.tasks.load(signal.root_task_id)
    if link is None or link.thread_id != signal.thread_id:
        return ""
    return canonical.wakes.delivery_receipt(signal.thread_id, signal.root_task_id)


# LLM: 已证实后在原提交门内先落published，再处理旧唤醒，最后落completed；旧任务链接和消息保持原样。
# 函数用途: 在规范发布成立后结清旧唤醒并完成交接回执。
def _retire_legacy_wake(path, published, legacy, signal):
    from ..common.json_io import write_private_json_file_atomic_unlocked

    write_private_json_file_atomic_unlocked(path, published)
    legacy.wakes.mark_handled(signal.wake_signal_id)
    write_private_json_file_atomic_unlocked(path, {**published, "phase": "completed", "updated_at": time.time()})


# LLM: 线程必须属于同一受管owner及其规范home，否则拒绝交接，不跨owner复制。
# 函数用途: 核对交接线程的owner归属。
def _require_owner_thread(home, thread):
    if thread.owner_id != home.owner_id or Path(thread.owner_home) != home.home_dir:
        raise ValueError("managed_dispatch_owner_conflict")


def _copy_history_slice(receipt_path, prepared, legacy, canonical, thread, deadline):
    """源线程锁内冻结完整性代次；目标未发表页按原handoff游标续作，不扫描/删除整树。"""
    from ..common.json_io import locked_json_path, write_private_json_file_atomic_unlocked
    from ..conversation import task_history

    source = legacy.storage.thread_path(thread.thread_id)
    source_manifest = task_history.history_root(source) / 'index.json'

    def stamp(path):
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        return [info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns]

    def source_identity():
        manifest = task_history._read(task_history.history_root(source), ('index.json',))
        generation = ({key: manifest.get(key) for key in ('version', 'count', 'generation', 'incomplete', 'copy_epoch')}
                      if manifest is not None else None)
        return [stamp(source), stamp(source_manifest), generation]

    source_stamp = source_identity()
    # 先读取一项验证完整性；不能从缺失索引/线程窗口声称完整迁移。
    task_history.page(source, thread, cursor=0, limit=1)
    target = canonical.storage.thread_path(thread.thread_id)
    root = task_history.history_root(target)
    progress = prepared.get('history_copy')
    if 'history_copy' in prepared and not _valid_history_copy_progress(progress):
        raise ValueError('invalid managed history copy progress')
    with locked_json_path(target):
        manifest = task_history._read(root, ('index.json',))
        if (not progress or progress.get('source_stamp') != source_stamp or manifest is None
                or manifest.get('copy_epoch') != progress.get('epoch')):
            progress = {'source_stamp': source_stamp, 'epoch': uuid.uuid4().hex, 'cursor': 0, 'done': False}
            # 新epoch使旧中断成员失效，按原散列位置覆盖，不为每次重启建新目录或rmtree。
            manifest = {'version': 1, 'count': 0, 'generation': 0, 'incomplete': True,
                        'copy_epoch': progress['epoch']}
            write_private_json_file_atomic_unlocked(root / 'index.json', manifest)
        position = progress['cursor']
        if not progress['done']:
            remaining = min(_HISTORY_COPY_SLICE_ITEMS_COUNT, task_history.PAGE_SIZE - manifest['count'] % task_history.PAGE_SIZE)
            records, next_cursor = task_history.page(source, thread, cursor=position, limit=remaining, with_positions=True)
            def stop():
                return deadline is not None and time.monotonic() >= deadline

            consumed = task_history.append_copy_window(target, (identifier for _, identifier in records), should_stop=stop)
            if consumed == len(records):
                progress = {**progress, 'cursor': next_cursor, 'done': next_cursor is None}
            elif consumed:
                progress = {**progress, 'cursor': records[consumed - 1][0] + 1}
        # 复制循环不改变源；再次核原代次后才发表本片续点/完整证明。
        if source_identity() != source_stamp:
            raise ValueError('legacy task history changed during copy')
        prepared['history_copy'] = progress
        write_private_json_file_atomic_unlocked(receipt_path, prepared)
        if progress['done']:
            manifest = task_history._read(root, ('index.json',))
            write_private_json_file_atomic_unlocked(root / 'index.json', {**manifest, 'incomplete': False})
        return progress['done']


def _valid_history_copy_progress(progress):
    return (type(progress) is dict and type(progress.get('done')) is bool
            and type(progress.get('epoch')) is str and len(progress['epoch']) == 32
            and all(char in '0123456789abcdef' for char in progress['epoch'])
            and isinstance(progress.get('source_stamp'), list) and len(progress['source_stamp']) == 3
            and ((progress['done'] and progress.get('cursor') is None)
                 or (not progress['done'] and type(progress.get('cursor')) is int and progress['cursor'] >= 0)))


def history_copy_pending(home_dir, ref, source, canonical_store):
    """只读精确handoff事实仅允许延期，绝不授权改投或运行旧库。"""
    from ..common.nofollow_fs import read_bytes_beneath
    from ..common.opaque_id import validate_opaque_id

    task_id = source.get('root_task_id')
    validate_opaque_id(task_id, kind='task_id')
    raw = read_bytes_beneath(home_dir, (*_HANDOFF_PARTS, task_id + '.json'), max_bytes=65536)
    if raw is None:
        return False
    receipt = json.loads(raw)
    metadata = source.get('metadata') or {}
    expected = {'schema': HANDOFF_SCHEMA, 'phase': 'prepared', 'task_id': task_id,
                'thread_id': ref['thread_id'], 'legacy_wake_signal_id': ref['source_id'],
                'legacy_store': ref['store_ref'], 'canonical_store': canonical_store,
                'owner_id': metadata.get('target_owner_id'), 'request_id': metadata.get('request_id')}
    owner = ref.get('owner') or {}
    return (isinstance(receipt, dict) and owner.get('provider') == 'local' and owner.get('owner_kind') == 'user'
            and receipt.get('owner_id') == 'local/' + str(owner.get('owner_id'))
            and source.get('wake_signal_id') == ref['source_id'] and source.get('thread_id') == ref['thread_id']
            and source.get('dedupe_key') == task_id and metadata.get('dispatched_by') == 'local/main'
            and all(receipt.get(key) == value for key, value in expected.items())
            and metadata.get('execution_owner_id') == receipt['owner_id']
            and metadata.get('task_id') == task_id
            and _valid_history_copy_progress(receipt.get('history_copy')))
