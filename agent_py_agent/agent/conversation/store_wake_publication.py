# LLM: 有稳定去重键的发布以原dedupe文件冻结信号；POSIX祖先目录共享锁与owner维护排他锁覆盖准备、安装及失败重放。
#   本模块只管理发布安装，不消费唤醒、不运行模型、不扫描另一个队列；查询绝不修账。
# 模块用途: 让唤醒与观察在文件半写后重试同一份内容，避免重发、丢观察或把坏账当空账。
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import ExitStack, closing, contextmanager
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..common.json_io import (
    append_private_jsonl_records,
    write_private_json_file_atomic,
    write_private_json_file_atomic_unlocked,
)
from ..common.nofollow_fs import ensure_private_dir, open_directory_beneath
from ..common.opaque_id import validate_opaque_id
from ..gateway_parts.io import locked_file_transition
from ..runtime_errors import DataCorruptionError, runtime_error_report
from ..user_space.home_indexes import is_sqlite_busy
from .models import ObservationEvent, WakeSignal
from .store_io import read_jsonl_report
from .store_layout import ConversationStorage
from .wake_poison import WAKE_STATUS_FAILED_PERMANENTLY

_SCHEMA = "wake_dedupe.v2"
_LEGACY_SCHEMA = "wake_dedupe.v1"
_TASK_GUARDS = ContextVar("wake_task_responsibility_guards", default=())
# 同根重建由索引EX串行化；None只表示在途，代次指纹只节流已确认的canonical坏账。
# 临时I/O、SQLite错误和取消必须在finally释放，不能把一次失败变成永久禁止恢复。
_UNKNOWN_INDEX_REBUILDS: dict[str, tuple[int, int, int, int] | None] = {}
# 保护上面登记表的进程内锁，只在判定/登记时短暂持有，不包住任何文件锁或重建IO。
_UNKNOWN_INDEX_REBUILDS_LOCK = threading.Lock()


# LLM: 发布先按根到叶对现有祖先目录持SH；任何祖先owner的维护EX均互斥，不靠路径形状猜owner或创建全局锁文件。
# 函数用途: 覆盖完整发布与重放事务，非阻塞锁忙在任何prepared写入前抛错并关闭全部FD。
@contextmanager
def _publication_guard(root: Path, *, blocking: bool):
    if os.name != "posix":
        # 非POSIX保持原发布；维护端本来就要求fcntl，不能在不支持的平台执行删除。
        yield
        return
    import fcntl

    resolved = Path(root).resolve(strict=True)
    with ExitStack() as stack:
        for path in (*reversed(resolved.parents), resolved):
            descriptor = open_directory_beneath(path, (), require_dir_fd=True)
            stack.callback(os.close, descriptor)
            fcntl.flock(descriptor, fcntl.LOCK_SH | (0 if blocking else fcntl.LOCK_NB))
        yield


# LLM: 写者持共享门、显式重建持排他门；锁先于task/dedupe锁，查询不创建或获取此文件。
# 函数用途: 防止重建覆盖并行发布产生的定位引用，保留非阻塞拒绝语义。
@contextmanager
def _task_index_guard(storage: ConversationStorage, *, exclusive: bool, blocking: bool = True):
    # 只读打开（initialize=False）的根可能还没有wake_queue/；两个平台分支开锁前都按0700私有补建，已有目录不动。
    ensure_private_dir(storage.wake_queue_dir)
    if os.name != "posix":
        with locked_file_transition(storage.wake_task_index_lock_path, blocking=blocking):
            yield
        return
    import fcntl

    descriptor = os.open(storage.wake_task_index_lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(descriptor, mode | (0 if blocking else fcntl.LOCK_NB))
        yield
    finally:
        os.close(descriptor)


# LLM: 只取结构化准确身份，不从dedupe字符串猜任务；没有可验证身份的普通事件不进入任务投影。
# 函数用途: 提取session_task信号的任务归属。
def session_task_identity(signal: WakeSignal) -> tuple[str, str] | None:
    task_id = (signal.metadata or {}).get("session_task_id")
    if (str(signal.reason).strip().lower() == "session_task" and isinstance(task_id, str)
            and task_id.strip() and signal.thread_id):
        return signal.thread_id, task_id
    return None


# LLM: owner祖先SH→索引SH→准确task锁→原发布/队列锁；仅同进程同线程同身份可重入，不继承别的执行者的锁。
#   索引不是普通文件时在取任何锁之前懒初始化；本上下文已持同根门（已有索引SH）时再取EX会自锁，故跳过，异根门用另一把锁不受影响。
# 函数用途: 串行化一个任务的检查和发布；锁忙在提交定位或canonical之前拒绝。
@contextmanager
def task_responsibility_guard(storage: ConversationStorage, thread_id: str, task_id: str, *, blocking: bool = True):
    identity = (str(storage.root.resolve()), os.getpid(), threading.get_ident(), thread_id, task_id)
    active = _TASK_GUARDS.get()
    if identity in active:
        yield
        return
    if all(entry[0] != identity[0] for entry in active) and not storage.wake_task_index_path.is_file():
        try:
            _initialize_task_index(storage, blocking=blocking)
        except (OSError, ValueError, TypeError, sqlite3.Error, DataCorruptionError):
            # 投影修不好不挡canonical：非阻塞忙（BlockingIOError）由下面原门按原语义在提交前拒绝，其它错误交给定位写入按投影丢失处理。
            pass
    with (_publication_guard(storage.root, blocking=blocking),
          _task_index_guard(storage, exclusive=False, blocking=blocking),
          locked_file_transition(storage.wake_task_lock_path(thread_id, task_id), blocking=blocking)):
        token = _TASK_GUARDS.set((*active, identity))
        try:
            yield
        finally:
            _TASK_GUARDS.reset(token)


# LLM: 普通事件保持原owner维护门；session_task额外共用检查者的task门，不把不同事件强行合成一个wake。
# 函数用途: 为原发布及结案路径选择正确的锁边界。
@contextmanager
def signal_responsibility_guard(storage: ConversationStorage, signal: WakeSignal, *, blocking: bool = True):
    identity = session_task_identity(signal)
    if identity is None:
        with _publication_guard(storage.root, blocking=blocking):
            yield
    else:
        with task_responsibility_guard(storage, *identity, blocking=blocking):
            yield


# LLM: ro/rw均禁止SQLite自动创建；query只读，不建表、不修账、不写journal。阻塞调用（含只读报告）等在途定位写最多5秒，非阻塞立即报忙。
# 函数用途: 打开已存在的派生定位库，调用方负责关闭连接。
def _task_index_connection(storage: ConversationStorage, *, writable: bool = False, blocking: bool = True):
    mode = "rw" if writable else "ro"
    return sqlite3.connect(storage.wake_task_index_path.resolve().as_uri() + "?mode=" + mode,
        uri=True, timeout=5.0 if blocking else 0)


# LLM: 纯ro只读metadata与表结构，不建表、不修账；缺失、空文件、无效字节或schema不符都返回None，锁忙原样上抛而不当成缺失。
# 函数用途: 返回派生定位库的覆盖标记（complete/unknown），供初始化决定保留、原子新建或从canonical重建。
def _task_index_coverage(storage: ConversationStorage, *, blocking: bool = True) -> str | None:
    try:
        with closing(_task_index_connection(storage, blocking=blocking)) as connection:
            metadata = dict(connection.execute("SELECT key, value FROM metadata"))
            locators = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='locators'").fetchone()
    except FileNotFoundError:
        return None
    except sqlite3.DatabaseError as exc:
        if is_sqlite_busy(exc):
            raise
        return None
    coverage = metadata.get("coverage")
    if locators is None or coverage not in {"complete", "unknown"}:
        return None
    return coverage if metadata == {"schema": "wake-task-locators.v1", "coverage": coverage} else None


# LLM: schema只描述定位字段与覆盖，不复制状态；旧无发布账的canonical在显式重建时标为legacy引用。
# 函数用途: 在新派生库内建立按thread/task寻址的主键。
def _create_task_index(connection, coverage: str) -> None:
    connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("CREATE TABLE locators (thread_id TEXT NOT NULL, task_id TEXT NOT NULL, "
        "wake_id TEXT NOT NULL, dedupe_key TEXT NOT NULL, urgency TEXT NOT NULL, receipt_required INTEGER NOT NULL, "
        "PRIMARY KEY(thread_id, task_id, wake_id))")
    connection.executemany("INSERT INTO metadata VALUES (?, ?)", (
        ("schema", "wake-task-locators.v1"), ("coverage", coverage)))


# LLM: 可写初始化（ensure_dirs）入口；索引只是投影，创建或重建的任何错误都不得让ensure_dirs失败，读者保持fail-closed，task门下次懒修复。
# 函数用途: 为空库发布complete覆盖，为旧库或unknown覆盖从canonical升级。
def initialize_task_responsibility_index(storage: ConversationStorage) -> None:
    try:
        _initialize_task_index(storage, blocking=True)
    except (OSError, ValueError, TypeError, sqlite3.Error, DataCorruptionError):
        return


# LLM: ownerSH→索引EX，与所有发布/确认/重放（索引SH）互斥；complete原样返回；缺失/无效且无旧账时原子发布空complete库。
#   有旧账或unknown从canonical重建；只有稳定坏账按索引代次节流，临时失败与取消释放在途标记，错误交给原调用边界裁决。
# 函数用途: 供可写初始化与task门懒修复共用，建立或升级派生定位库，不改任何canonical唤醒或发布记录。
def _initialize_task_index(storage: ConversationStorage, *, blocking: bool) -> None:
    import tempfile

    with (_publication_guard(storage.root, blocking=blocking),
          _task_index_guard(storage, exclusive=True, blocking=blocking)):
        coverage = _task_index_coverage(storage, blocking=blocking)
        if coverage == "complete":
            return
        legacy = any(next(directory.glob("*.json"), None) is not None for directory in (
            storage.wake_queue_dir / "urgent", storage.wake_queue_dir / "normal", storage.wake_dedupe_dir))
        if coverage is None and not legacy:
            descriptor, name = tempfile.mkstemp(prefix=".task-responsibility-", suffix=".sqlite3", dir=storage.wake_queue_dir)
            os.close(descriptor)
            temporary = Path(name)
            try:
                with closing(sqlite3.connect(temporary)) as connection, connection:
                    _create_task_index(connection, "complete")
                os.replace(temporary, storage.wake_task_index_path)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
            if os.name == "posix":
                descriptor = os.open(storage.wake_queue_dir, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            return
        root = str(storage.root.resolve())
        generation = _task_index_generation(storage)
        with _UNKNOWN_INDEX_REBUILDS_LOCK:
            if coverage == "unknown" and generation is not None and _UNKNOWN_INDEX_REBUILDS.get(root) == generation:
                return
            _UNKNOWN_INDEX_REBUILDS[root] = None
        invalid_source = False
        try:
            _rebuild_task_index_locked(storage)
        except (OSError, ValueError, TypeError, sqlite3.Error, DataCorruptionError) as exc:
            invalid_source = _invalid_task_index_source(exc)
            raise
        finally:
            # 重建的finally可能已替换unknown库，必须绑定新代次；取指纹失败也不能掩盖原始异常或保留在途抑制。
            failed_generation = None
            if invalid_source:
                try:
                    failed_generation = _task_index_generation(storage)
                except OSError:
                    pass
            with _UNKNOWN_INDEX_REBUILDS_LOCK:
                _UNKNOWN_INDEX_REBUILDS.pop(root, None)
                if failed_generation is not None:
                    _UNKNOWN_INDEX_REBUILDS[root] = failed_generation


# LLM: 仅比较派生库自身身份，不扫描canonical；同一unknown坏账代次最多重建一次。
# 函数用途: 为坏账节流绑定一个确切索引代次，替换后的新库可重新恢复。
def _task_index_generation(storage: ConversationStorage) -> tuple[int, int, int, int] | None:
    try:
        observed = storage.wake_task_index_path.stat()
    except FileNotFoundError:
        return None
    return observed.st_dev, observed.st_ino, observed.st_mtime_ns, observed.st_size


# LLM: _read_record把OS错误包装成DataCorruptionError；必须沿显式cause核实，不能只按外层类型永久节流。
# 函数用途: 将真正的格式/身份坏账与可重试的读取、SQLite失败区分开。
def _invalid_task_index_source(error: BaseException) -> bool:
    while error is not None:
        if isinstance(error, (OSError, sqlite3.Error)):
            return False
        error = error.__cause__
    return True


# LLM: 显式可写恢复边界，必须在普通task/index共享门外；complete仅点查metadata，unknown才按ownerSH→indexEX重建。
#   错误原样传播保留cause；节流坏账仍unknown时显式拒绝，不能把恢复未完成当成空责任或成功。
# 函数用途: 供resume/restore/drain一次性恢复投影，不要求重启或操作员重建，不改canonical。
def recover_task_responsibility_index(storage: ConversationStorage, *, blocking: bool = True) -> None:
    root = str(storage.root.resolve())
    if any(entry[0] == root for entry in _TASK_GUARDS.get()):
        raise RuntimeError("wake index recovery must run outside a task guard")
    if _task_index_coverage(storage, blocking=blocking) == "complete":
        return
    _initialize_task_index(storage, blocking=blocking)
    if _task_index_coverage(storage, blocking=blocking) != "complete":
        raise DataCorruptionError("task wake locator recovery is pending")


# LLM: 定位先于prepared/canonical提交；崩溃留下孤儿引用会返回unknown，绝不会漏掉已提交责任。
#   锁忙原样上抛，非阻塞争用仍在任何prepared/canonical写前拒绝；其它SQLite错误是投影丢失：删索引让读者报unknown，
#   canonical照常发布，下次task门入口或可写初始化从权威源重建；删不掉则上抛，不能带着缺这条定位的complete覆盖继续。
# 函数用途: 在原task锁内预留可重建位置，不保存状态或冻结内容的副本。
def register_task_locator(storage: ConversationStorage, signal: WakeSignal, *, blocking: bool = True) -> None:
    identity = session_task_identity(signal)
    if identity is None:
        return
    try:
        with closing(_task_index_connection(storage, writable=True, blocking=blocking)) as connection, connection:
            connection.execute("INSERT OR REPLACE INTO locators VALUES (?, ?, ?, ?, ?, ?)",
                (*identity, signal.wake_signal_id, signal.dedupe_key, signal.urgency, int(bool(signal.dedupe_key))))
    except sqlite3.DatabaseError as exc:
        if is_sqlite_busy(exc):
            raise
        storage.wake_task_index_path.unlink(missing_ok=True)


# LLM: 原task锁内确认两个pending位置均不存在后才清定位；尽力删除失败或stat错误保留引用，索引坏时保留unknown。
# 函数用途: 删除终态的派生引用，避免积累历史代次导致热查询放大。
def remove_task_locator(storage: ConversationStorage, signal: WakeSignal) -> None:
    identity = session_task_identity(signal)
    if identity is None:
        return
    for kind in ("urgent", "normal"):
        try:
            (storage.wake_queue_dir / kind / f"{signal.wake_signal_id}.json").stat()
        except FileNotFoundError:
            continue
        except OSError:
            return
        else:
            return
    try:
        with closing(_task_index_connection(storage, writable=True)) as connection, connection:
            connection.execute("DELETE FROM locators WHERE thread_id=? AND task_id=? AND wake_id=?",
                (*identity, signal.wake_signal_id))
    except (OSError, sqlite3.Error):
        # 底层canonical已经结案；后续report会暴露索引缺失/损坏，不把它解释成空责任。
        return


# LLM: 原文件通过原子替换发布，查询可直接读完整快照；不创建锁文件，只有不存在才是空账。
# 函数用途: 严格读取发布或信号记录，损坏、权限或其它读取错误均向调用方报错。
def _read_record(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise DataCorruptionError("wake publication record is unreadable") from exc
    if not isinstance(payload, dict) or not payload:
        raise DataCorruptionError("wake publication record is invalid")
    return payload


# LLM: ID 必须符合原通用不透明身份合同，读取持久记录不能借路径字段跨出队列。
# 函数用途: 从结构化记录建立信号对象并核对基本身份与状态。
def _signal(payload: dict[str, Any]) -> WakeSignal:
    signal = WakeSignal.from_dict(payload)
    validate_opaque_id(signal.wake_signal_id, kind="wake_signal_id")
    if not signal.thread_id or signal.status not in {"pending", "handled", WAKE_STATUS_FAILED_PERMANENTLY}:
        raise DataCorruptionError("wake publication signal identity is invalid")
    return signal


# LLM: 宿主消费只允许追加 owner_delivery/required_user_action，消费只改变 status/handled_at；发布身份、原事件不可漂移。
# 函数用途: 提取用于对账的信号不可变字段，不把处理时间当发布冲突。
def _stable_signal(signal: WakeSignal) -> dict[str, Any]:
    payload = signal.to_dict()
    payload.pop("status")
    payload.pop("handled_at")
    metadata = dict(payload["metadata"])
    metadata.pop("owner_delivery", None)
    metadata.pop("required_user_action", None)
    payload["metadata"] = metadata
    return payload


# LLM: 先查 pending 再查 handled，再查结案位置，最后查结案归档（quarantine/archive/，超过保留期的结案记录移到这里，
#   只换物理位置、不改发布语义：同键再发布仍返回原结案信号，不新开一代）；消费和结案都先落终态记录后删 pending，
#   不能把交接误当信号丢失。结案记录顶层的 quarantine 键不是 WakeSignal 字段，不参与冻结内容对账。
# 函数用途: 严格读回固定信号，保留合法的已消费或已结案事实并拒绝同 ID 的内容冲突。
def _installed_signal(storage: ConversationStorage, frozen: WakeSignal) -> WakeSignal | None:
    paths = (storage.wake_signal_path(frozen), storage.wake_handled_dir / f"{frozen.wake_signal_id}.json",
             storage.wake_quarantine_path(frozen.wake_signal_id),
             storage.wake_quarantine_archive_path(frozen.wake_signal_id))
    for path in paths:
        payload = _read_record(path)
        if payload is None:
            continue
        existing = _signal(payload)
        if _stable_signal(existing) != _stable_signal(frozen):
            raise DataCorruptionError("wake publication signal conflicts with frozen content")
        return existing
    return None


# LLM: 同一观察 ID 只能对应一行完整原负载；原账损坏时不能追加后假报修复成功。
# 函数用途: 从原观察账查固定事件，供安装重试和旧版回执迁移共同使用。
def _find_observation(storage: ConversationStorage, thread_id: str, observation_id: str) -> ObservationEvent | None:
    path = storage.observation_path(thread_id)
    if not path.exists():
        return None
    report = read_jsonl_report(path, context="conversation.wake_publication.observation")
    if report.load_errors:
        raise DataCorruptionError("wake publication observation ledger is unreadable")
    matching = [row for row in report.rows if row.get("observation_id") == observation_id]
    if len(matching) > 1:
        raise DataCorruptionError("wake publication observation identity is duplicated")
    return ObservationEvent.from_dict(matching[0]) if matching else None


# LLM: 发布记录冻结全部原内容；新请求不能给旧信号补新摘要，链接双方身份须完全一致。
# 函数用途: 校验 v2 发布记录并还原本次固定信号与可选配对观察。
def _frozen_pair(record: dict[str, Any], thread_id: str, key: str) -> tuple[ObservationEvent | None, WakeSignal]:
    if record.get("schema_version") != _SCHEMA or record.get("phase") not in {"prepared", "published"}:
        raise DataCorruptionError("wake publication schema or phase is invalid")
    if record.get("thread_id") != thread_id or record.get("dedupe_key") != key:
        raise DataCorruptionError("wake publication key identity conflicts")
    if not isinstance(record.get("signal"), dict) or type(record.get("retain_handled")) is not bool:
        raise DataCorruptionError("wake publication frozen signal or retention is invalid")
    signal = _signal(record["signal"])
    if (signal.thread_id, signal.dedupe_key, signal.wake_signal_id) != (thread_id, key, record.get("wake_signal_id")):
        raise DataCorruptionError("wake publication signal identity conflicts")
    raw_observation = record.get("observation")
    if raw_observation is None:
        return None, signal
    if not isinstance(raw_observation, dict):
        raise DataCorruptionError("wake publication frozen observation is invalid")
    observation = ObservationEvent.from_dict(raw_observation)
    if not observation.observation_id or (observation.thread_id, observation.observation_id, observation.wake_signal_id) != (
        thread_id, signal.observation_id, signal.wake_signal_id,
    ):
        raise DataCorruptionError("wake publication observation identity conflicts")
    return observation, signal


# LLM: 此记录仍位于原 dedupe 路径，prepared 是发布提交点；只有后续安装完成才可报告投递回执。
# 函数用途: 冻结本次信号和配对观察，生成可恢复的发布记录。
def _prepare(signal: WakeSignal, observation: ObservationEvent | None, retain_handled: bool) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA, "thread_id": signal.thread_id, "dedupe_key": signal.dedupe_key,
        "wake_signal_id": signal.wake_signal_id, "phase": "prepared", "retain_handled": retain_handled,
        "signal": replace(signal, status="pending", handled_at=0.0).to_dict(),
        "observation": replace(observation, wake_signal_id=signal.wake_signal_id).to_dict() if observation else None,
        "updated_at": signal.created_at,
    }


# LLM: v1 只能从其已发布信号和原观察升级；缺失内容不准从新请求伪造，查询不执行迁移。
# 函数用途: 在原锁内显式迁移健康的旧回执，保留旧信号身份及处理状态。
def _migrate_legacy(storage: ConversationStorage, record: dict[str, Any], incoming: WakeSignal,
                    *, paired: bool, retain_handled: bool) -> dict[str, Any]:
    if record.get("thread_id") != incoming.thread_id or record.get("dedupe_key") != incoming.dedupe_key:
        raise DataCorruptionError("legacy wake receipt identity conflicts")
    identity = str(record.get("wake_signal_id") or "")
    validate_opaque_id(identity, kind="wake_signal_id")
    existing = _read_existing_identity(storage, identity)
    if existing is None or (existing.thread_id, existing.dedupe_key) != (incoming.thread_id, incoming.dedupe_key):
        raise DataCorruptionError("legacy wake receipt has no matching signal")
    observation = _find_observation(storage, existing.thread_id, existing.observation_id) if paired else None
    if paired and (observation is None or observation.wake_signal_id != identity):
        raise DataCorruptionError("legacy wake receipt has no complete paired observation")
    migrated = {**_prepare(existing, observation, retain_handled), "phase": "published"}
    migrated["migration"] = {"from_schema": _LEGACY_SCHEMA, "wake_signal_id": identity}
    return migrated


# LLM: 旧版回执和只读查询按原两个队列、handled、结案位置及结案归档查精确 ID，不全局猜同名或内容。
# 函数用途: 读取一个旧信号身份，缺失与损坏保持不同结果。
def _read_existing_identity(storage: ConversationStorage, identity: str) -> WakeSignal | None:
    validate_opaque_id(identity, kind="wake_signal_id")
    paths = [storage.wake_queue_dir / kind / f"{identity}.json" for kind in ("urgent", "normal", "handled")]
    paths.extend((storage.wake_quarantine_path(identity), storage.wake_quarantine_archive_path(identity)))
    for path in paths:
        payload = _read_record(path)
        if payload is not None:
            signal = _signal(payload)
            if signal.wake_signal_id != identity:
                raise DataCorruptionError("wake receipt signal ID conflicts")
            return signal
    return None


# LLM: 安装只重放原 prepared 内容；已被消费的信号不重建，观察追加在原去重锁下按固定 ID 幂等。
# 函数用途: 补齐信号与观察后再保存 published，任何失败保留原发布供调用方重试。
# LLM: 非阻塞只透传同一原锁；prepared/installed/published顺序不变，paired半提交保原账而非假原子成功。
# 函数用途: C可立即拒绝发布锁忙，后续仍重放原同键内容。
def _install(storage: ConversationStorage, path: Path, record: dict[str, Any], *, blocking: bool = True) -> tuple[ObservationEvent | None, WakeSignal]:
    observation, frozen = _frozen_pair(record, record["thread_id"], record["dedupe_key"])
    installed = _installed_signal(storage, frozen)
    if installed is None:
        # 唤醒信号属宿主运行数据：私有原子写（0600/0700），存量宽权限信号下次写入即收紧；内容逐字节不变。
        write_private_json_file_atomic(storage.wake_signal_path(frozen), frozen.to_dict(), **({} if blocking else {"blocking": False}))
        installed = frozen
    if observation is not None:
        existing = _find_observation(storage, frozen.thread_id, observation.observation_id)
        if existing is None:
            # 私有追加：0600/0700，存量宽权限观察账本下次写入即收紧；内容逐字节不变。
            append_private_jsonl_records(storage.observation_path(frozen.thread_id), [observation.to_dict()], sort_keys=True, **({} if blocking else {"blocking": False}))
        elif existing.to_dict() != observation.to_dict():
            raise DataCorruptionError("wake publication observation conflicts with frozen content")
    write_private_json_file_atomic_unlocked(path, {**record, "phase": "published"})
    return observation, installed


# LLM: owner/index/task锁先于原dedupe锁；定位先于prepared，原冻结/重试/retain_handled语义不变，不合并不同事件。
# 函数用途: 发布或恢复一个有稳定键的信号及观察，返回原内容，不新增后台恢复服务。
# LLM: 非阻塞只透传同一原锁；prepared/installed/published顺序不变，paired半提交保原账而非假原子成功。
# 函数用途: C可立即拒绝发布锁忙，后续仍重放原同键内容。
def publish_deduped(storage: ConversationStorage, signal: WakeSignal, *, observation: ObservationEvent | None = None,
                     retain_handled: bool = False, blocking: bool = True) -> tuple[ObservationEvent | None, WakeSignal]:
    if type(retain_handled) is not bool:
        raise ValueError("wake retain_handled must be boolean")
    path = storage.wake_dedupe_path(signal.thread_id, signal.dedupe_key)
    with signal_responsibility_guard(storage, signal, blocking=blocking), locked_file_transition(path, blocking=blocking):
        record = _read_record(path)
        if record is not None and record.get("schema_version") == _LEGACY_SCHEMA:
            record = _migrate_legacy(storage, record, signal, paired=observation is not None, retain_handled=retain_handled)
            write_private_json_file_atomic_unlocked(path, record)
        if record is not None:
            previous_observation, frozen = _frozen_pair(record, signal.thread_id, signal.dedupe_key)
            if record["retain_handled"] != retain_handled or (previous_observation is None) != (observation is None):
                raise DataCorruptionError("wake publication retention or pair contract conflicts")
            if record["phase"] == "prepared":
                register_task_locator(storage, frozen, blocking=blocking)
                return _install(storage, path, record, blocking=blocking)
            existing = _installed_signal(storage, frozen)
            if existing is None:
                raise DataCorruptionError("published wake signal is missing")
            # 已结案的同一事件不能靠再次发布自己复活，只能人工重放。
            if existing.status in {"pending", WAKE_STATUS_FAILED_PERMANENTLY} or retain_handled:
                if existing.status == "pending":
                    register_task_locator(storage, existing, blocking=blocking)
                return previous_observation, existing
            remove_task_locator(storage, existing)
        migration = record.get("migration") if record is not None else None
        record = _prepare(signal, observation, retain_handled)
        if migration is not None:
            record["migration"] = migration
        _frozen_pair(record, signal.thread_id, signal.dedupe_key)
        register_task_locator(storage, signal, blocking=blocking)
        write_private_json_file_atomic_unlocked(path, record)
        return _install(storage, path, record, blocking=blocking)


# LLM: 回执只读原子快照，不取写锁、不迁移或补安装；prepared 不算完整交付，坏账不能退化为空。
# 函数用途: 查询固定键是否已完整发布并处于 pending/handled，供观察界面与诊断使用。
def publication_receipt(storage: ConversationStorage, thread_id: str, key: str) -> str:
    if not thread_id or not key:
        return ""
    path = storage.wake_dedupe_path(thread_id, key)
    record = _read_record(path)
    if record is None:
        return ""
    if record.get("schema_version") == _LEGACY_SCHEMA:
        if record.get("thread_id") != thread_id or record.get("dedupe_key") != key:
            raise DataCorruptionError("legacy wake receipt identity conflicts")
        signal = _read_existing_identity(storage, str(record.get("wake_signal_id") or ""))
        if signal is None or (signal.thread_id, signal.dedupe_key) != (thread_id, key):
            raise DataCorruptionError("legacy wake receipt has no matching signal")
        return signal.status
    _, frozen = _frozen_pair(record, thread_id, key)
    if record["phase"] == "prepared":
        return ""
    signal = _installed_signal(storage, frozen)
    if signal is None:
        raise DataCorruptionError("published wake signal is missing")
    return signal.status


# LLM: 引用不拥有liveness；必须回读原发布及canonical，prepared算责任，缺失/冲突不能降为空。
# 函数用途: 精确核验一个定位引用并返回仍待处理的责任事实。
def _task_locator_fact(storage, thread_id, task_id, locator):
    wake_id, key, urgency, receipt_required = locator
    phase = "published"
    if receipt_required:
        record = _read_record(storage.wake_dedupe_path(thread_id, key))
        if record is None:
            raise DataCorruptionError("task wake publication is missing")
        if record.get("schema_version") == _LEGACY_SCHEMA:
            if (record.get("thread_id"), record.get("dedupe_key"), record.get("wake_signal_id")) != (thread_id, key, wake_id):
                raise DataCorruptionError("legacy task wake publication conflicts")
            signal = _read_existing_identity(storage, wake_id)
        else:
            _, frozen = _frozen_pair(record, thread_id, key)
            if frozen.wake_signal_id != wake_id:
                # 上一代结案后同键可以再发布；只允许忽略准确旧ID的已结案引用。
                previous = _read_existing_identity(storage, wake_id)
                if previous is not None and previous.status != "pending":
                    return None
                raise DataCorruptionError("task wake locator identity conflicts")
            signal = _installed_signal(storage, frozen)
            if record["phase"] == "prepared" and signal is None:
                signal, phase = frozen, "prepared"
    else:
        signal = _read_existing_identity(storage, wake_id)
    if signal is None:
        raise DataCorruptionError("task wake canonical signal is missing")
    if (session_task_identity(signal) != (thread_id, task_id) or signal.dedupe_key != key
            or signal.urgency != urgency):
        raise DataCorruptionError("task wake locator does not match canonical identity")
    return {"wake_signal_id": wake_id, "phase": phase} if signal.status == "pending" else None


# LLM: 纯ro连接只按主键前缀读取一个task；无glob/全队列/历史代次/修账，覆盖不明和任何坏账均返回unknown。
# 函数用途: 查询准确任务仍承担调度的原wake或prepared发布，供持task锁的producer判定。
def task_responsibility_report(storage: ConversationStorage, thread_id: str, task_id: str):
    try:
        with closing(_task_index_connection(storage)) as connection:
            metadata = dict(connection.execute("SELECT key, value FROM metadata"))
            if metadata != {"schema": "wake-task-locators.v1", "coverage": "complete"}:
                raise DataCorruptionError("task wake locator coverage is unknown")
            locators = connection.execute("SELECT wake_id, dedupe_key, urgency, receipt_required FROM locators "
                "WHERE thread_id=? AND task_id=? ORDER BY wake_id", (thread_id, task_id)).fetchall()
        rows = [fact for locator in locators if (fact := _task_locator_fact(storage, thread_id, task_id, locator)) is not None]
        return rows, None
    except (OSError, ValueError, TypeError, sqlite3.Error, DataCorruptionError) as exc:
        return [], runtime_error_report(exc, context="conversation.wake_task_responsibility.read")


# LLM: 仅由显式重建入口调用；读取当前队列和原发布账，不读取handled历史正文，canonical决定是否仍有责任。
# 函数用途: 在热路径之外从原权威源提取可重建定位引用。
def _task_projection_sources(storage: ConversationStorage):
    for kind in ("urgent", "normal"):
        for path in (storage.wake_queue_dir / kind).glob("*.json"):
            signal = _signal(_read_record(path))
            if path.stem != signal.wake_signal_id:
                raise DataCorruptionError("task wake filename identity conflicts")
            if signal.status == "pending" and session_task_identity(signal) is not None:
                yield signal, bool(signal.dedupe_key and storage.wake_dedupe_path(signal.thread_id, signal.dedupe_key).is_file())
    for path in storage.wake_dedupe_dir.glob("*.json"):
        record = _read_record(path)
        if record.get("schema_version") == _LEGACY_SCHEMA:
            signal = _read_existing_identity(storage, str(record.get("wake_signal_id") or ""))
            if signal is None or (signal.thread_id, signal.dedupe_key) != (record.get("thread_id"), record.get("dedupe_key")):
                raise DataCorruptionError("legacy task publication has no canonical signal")
        else:
            _, frozen = _frozen_pair(record, record.get("thread_id"), record.get("dedupe_key"))
            if session_task_identity(frozen) is None:
                continue
            signal = _installed_signal(storage, frozen)
            if signal is None:
                if record["phase"] != "prepared":
                    raise DataCorruptionError("published task wake is missing during rebuild")
                signal = frozen
        if signal.status == "pending" and session_task_identity(signal) is not None:
            yield signal, True


# LLM: 明确写入口，ownerSH→索引EX与所有发布/确认/replay互斥；新库原子替换，失败也发布unknown而不留下旧空覆盖。
# 函数用途: 显式重建旧库或损坏投影；不在查询或每task热路径触发。
def rebuild_task_responsibility_index(storage: ConversationStorage) -> None:
    with _publication_guard(storage.root, blocking=True), _task_index_guard(storage, exclusive=True):
        _rebuild_task_index_locked(storage)


# LLM: 调用方已持ownerSH→索引EX；只读当前队列与原发布账，临时库先提交unknown再填定位，finally原子替换并fsync目录。
# 函数用途: 在已持锁内从canonical重建定位库，供显式重建与初始化升级共用。
def _rebuild_task_index_locked(storage: ConversationStorage) -> None:
    import tempfile

    descriptor, name = tempfile.mkstemp(prefix=".task-responsibility-", suffix=".sqlite3", dir=storage.wake_queue_dir)
    os.close(descriptor)
    temporary = Path(name)
    try:
        with closing(sqlite3.connect(temporary)) as connection, connection:
            _create_task_index(connection, "unknown")
        with closing(sqlite3.connect(temporary)) as connection, connection:
            for signal, receipt_required in _task_projection_sources(storage):
                connection.execute("INSERT OR REPLACE INTO locators VALUES (?, ?, ?, ?, ?, ?)",
                    (*session_task_identity(signal), signal.wake_signal_id, signal.dedupe_key, signal.urgency, int(receipt_required)))
            connection.execute("UPDATE metadata SET value='complete' WHERE key='coverage'")
    finally:
        os.replace(temporary, storage.wake_task_index_path)
        if os.name == "posix":
            descriptor = os.open(storage.wake_queue_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
