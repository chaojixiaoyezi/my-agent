# LLM: 辅助游标、原审计版本和准确完成事实全部附在原quota的同次原子提交内；不保存第二份count，不签P3或C权限。
# 模块用途: 在原T/controller热门外固定批读取策展日账，返回可CAS的原会计候选。
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import select
import sys
import threading
import uuid
import weakref
from collections import OrderedDict
from dataclasses import asdict
from pathlib import Path

from ..common.nofollow_fs import (
    open_directory_beneath,
    open_readonly_file_beneath,
    read_bytes_beneath,
)
from ..common.strict_json import load_strict_json
from ..memory_store.curator_run_log import CuratorRunRecord
from ..user_space.owner_resolver import OwnerIdentity, _owner_home_dir
from .multiprocess_route import owner_digest


# LLM: 仅区分已核绑定的来源换代；身份冲突和不可读异常不允许走自动重建。
# 函数用途: 向准备叶表达旧receipt来源已失效，短门仍按ValueError拒绝。
class CompletionSourceChanged(ValueError):
    pass


# 仅保存可丢弃的目录读取句柄；完成事实、位置和唯一count仍以原quota的CAS结果为准。
_directory_cursors = weakref.WeakValueDictionary()
_standalone_cursors = OrderedDict()
_directory_lock = threading.RLock()


# LLM: 内核事件只给目录审计提供可撤销版本，不保存count/事实；任一事件或来源丢失都使整代失效。
# 函数用途: 用kqueue/inotify替代热门门逐个open/fstat全部provider，注册仍由冷发现固定批执行。
class _DirectoryWatch:
    # LLM: 每代只有一个事件队列；无后台线程、TTL或持久ready授权，未知平台明确拒绝。
    # 函数用途: 建立进程内、可丢弃的原生目录变化监听。
    def __init__(self, root):
        self.root, self.token, self.valid = Path(root), uuid.uuid4().hex, True
        self.directories, self.queue, self.descriptor = {}, None, None
        if hasattr(select, "kqueue"):
            self.queue = select.kqueue()
            self.descriptor = self.queue.fileno()
        elif sys.platform.startswith("linux"):
            import ctypes

            self.libc = ctypes.CDLL(None, use_errno=True)
            self.libc.inotify_init1.argtypes = [ctypes.c_int]
            self.libc.inotify_init1.restype = ctypes.c_int
            self.libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
            self.libc.inotify_add_watch.restype = ctypes.c_int
            self.descriptor = self.libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
            if self.descriptor < 0:
                raise OSError(ctypes.get_errno(), "curator_quota_inotify_init")
        else:
            raise ValueError("curator_quota_directory_events_unsupported")
        stat = os.fstat(self.descriptor)
        self.identity = (stat.st_dev, stat.st_ino, stat.st_mode)

    # LLM: 先注册后复核同fd及canonical版本，封住打开到订阅的间隙；缺子目录由已订阅父目录覆盖。
    # 函数用途: 在当前固定发现预算内，为一个规范目录建立无漏窗监听。
    def watch(self, relative):
        relative = tuple(relative)
        if relative in self.directories:
            return
        if not self.valid:
            raise ValueError("curator_quota_directory_events_lost")
        try:
            descriptor = open_directory_beneath(self.root, relative, require_dir_fd=True)
        except FileNotFoundError:
            return
        retained = False
        try:
            before = _fd_version(descriptor)
            if self.queue is not None:
                flags = (select.KQ_NOTE_WRITE | select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME
                         | select.KQ_NOTE_ATTRIB | select.KQ_NOTE_REVOKE | select.KQ_NOTE_EXTEND)
                self.queue.control([select.kevent(descriptor, filter=select.KQ_FILTER_VNODE,
                    flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR, fflags=flags)], 0, 0)
            else:
                import ctypes

                # 增删/移动/撤销/属性均使版本失效，队列overflow/ignored不做名称过滤。
                mask = 0x00000FC4 | 0x01000000
                if self.libc.inotify_add_watch(self.descriptor,
                        os.fsencode(f"/proc/self/fd/{descriptor}"), mask) < 0:
                    raise OSError(ctypes.get_errno(), "curator_quota_inotify_watch")
            expected = [before[0], before[1], before[3], before[4]]
            if (_fd_version(descriptor) != before
                    or _directory_version(self.root, relative) != expected):
                self.valid = False
                raise ValueError("curator_quota_owner_inventory_changed")
            retained = self.queue is not None
            self.directories[relative] = descriptor if retained else None
        finally:
            if not retained:
                os.close(descriptor)

    # LLM: vnode事件按注册项合并而不丢脏位，inotify含overflow/ignored也按任意字节失效；不耗尽事件流。
    # 函数用途: 用一次固定大小的非阻塞内核读取，返回可信版本或unknown。
    def version(self):
        if not self.valid or self.descriptor is None:
            return None
        try:
            stat = os.fstat(self.descriptor)
            if (stat.st_dev, stat.st_ino, stat.st_mode) != self.identity:
                self.valid = False
            elif self.queue is not None:
                self.valid = not bool(self.queue.control(None, 1, 0))
            else:
                try:
                    os.read(self.descriptor, 4096)
                    self.valid = False
                except BlockingIOError:
                    pass
        except (OSError, ValueError):
            self.valid = False
        return ["native-directory-events", self.token] if self.valid else None

    # LLM: 先关闭事件权威再有界释放目录fd，重审期间永不沿用旧ready；最终controller关闭可一次回收。
    # 函数用途: 每拍最多释放指定数量的旧代监听资源。
    def retire(self, limit):
        self.valid = False
        if self.descriptor is not None:
            if self.queue is not None:
                self.queue.close()
            else:
                os.close(self.descriptor)
            self.descriptor = None
        for _ in range(min(limit, len(self.directories))):
            _relative, descriptor = self.directories.popitem()
            if descriptor is not None:
                os.close(descriptor)
        return not self.directories


# LLM: 一个controller只有一个当前根游标，不持count/事实；丢失、fork或CAS分歧后从规范根重新有界核验。
# 函数用途: 持有跨tick的原scandir句柄，避免每页从头listdir/sort。
class _DirectoryCursor:
    # LLM: 随机代次只防止旧位置续接另一目录流，不是执行或会计权威。
    # 函数用途: 建立无打开fd的可丢弃读取状态。
    def __init__(self, root):
        self.root, self.pid = Path(root), os.getpid()
        self.token, self.position, self.stream = uuid.uuid4().hex, 0, None
        self.relative, self.observer = None, None

    # LLM: 关闭是幂等资源回收，不改原quota或P3；即使异常也忘记原句柄。
    # 函数用途: 关闭当前目录流。
    def close(self):
        stream, self.stream = self.stream, None
        self.relative = None
        if stream is not None:
            stream.close()

    # LLM: controller被回收时也释放冷扫描句柄，不依赖GC维持任何业务事实。
    # 函数用途: 回收未显式关闭的读取资源。
    def __del__(self):
        self.dispose()

    # LLM: 原C退出才无预算限制地回收全部监听；不改原quota、P3或租约。
    # 函数用途: 幂等释放目录流及原生事件资源。
    def dispose(self):
        self.close()
        if self.observer is not None:
            self.observer.retire(len(self.observer.directories))
            self.observer = None

    # LLM: 旧代最多回收32个目录fd后让出tick，期间token失效；新代只订阅四个固定祖先。
    # 函数用途: 在冷准备预算内重建内核变更来源，不在失效拍隐藏线性清理。
    def reset_observer(self):
        self.close()
        if self.observer is not None and not self.observer.retire(32):
            return False
        self.observer = _DirectoryWatch(self.root)
        for relative in ((), ("owners",), ("owners", "local"), ("owners", "providers")):
            self.observer.watch(relative)
        if self.observer.version() is None:
            raise ValueError("curator_quota_owner_inventory_changed")
        return True

    # LLM: fd由原no-follow目录生产者建立；scandir复制的fd交迭代器关闭，原fd总在finally中关闭。
    # 函数用途: 打开准确规范目录的一条可续迭代器。
    def open(self, relative):
        self.close()
        descriptor = open_directory_beneath(self.root, tuple(relative), require_dir_fd=True)
        try:
            self.stream = os.scandir(descriptor)
            self.relative = list(relative)
        finally:
            os.close(descriptor)


# LLM: 只把资源寄存在原controller生命周期，弱根表不延长controller生命；无controller纯准备口有小型LRU上界。
# 函数用途: 找到本次原C的目录读取器，换C/根/fork时拒绝旧内存位置。
def _cursor_for(home_root, controller):
    key = str(Path(home_root))
    if controller is None:
        cursor = _standalone_cursors.pop(key, None)
    else:
        cursor = getattr(controller, "_curator_quota_directory_cursor", None)
    if cursor is None or cursor.pid != os.getpid() or cursor.root != Path(home_root):
        if cursor is not None:
            cursor.dispose()
        cursor = _DirectoryCursor(home_root)
    if controller is None:
        _standalone_cursors[key] = cursor
        while len(_standalone_cursors) > 8:
            _standalone_cursors.popitem(last=False)[1].dispose()
    else:
        controller._curator_quota_directory_cursor = cursor
    _directory_cursors[key] = cursor
    return cursor


# LLM: 原controller.close可调用本叶；无controller纯准备口按准确根回收，均只撤销资源缓存，不改会计/P3/租约。
# 函数用途: 为原C与纯准备调用者提供幂等资源回收接口。
def close_directory_cursor(controller=None, *, home_root=None):
    if controller is None and home_root is None:
        return
    with _directory_lock:
        cursor = (_standalone_cursors.pop(str(Path(home_root)), None) if controller is None
                  else getattr(controller, "_curator_quota_directory_cursor", None))
        if cursor is not None:
            cursor.dispose()
            if controller is not None:
                controller._curator_quota_directory_cursor = None
            if _directory_cursors.get(str(cursor.root)) is cursor:
                _directory_cursors.pop(str(cursor.root), None)


# LLM: 只用于CAS及原v2身份，摘要不替代原租约、P3或原审计校验。
# 函数用途: 对规范结构生成确定性摘要。
def digest(value):
    content = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(content.encode()).hexdigest()


# LLM: 与已有v2 owner/run键逐字一致；UTC日由外层quota分片和receipt明确绑定。
# 函数用途: 保持Count0与多进程的完成去重身份一致。
def fact_identity(owner, run_id):
    content = json.dumps({"owner": asdict(owner), "run_id": run_id}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode()).hexdigest()


# LLM: 使用同一个no-follow fd的版本；变短、原地改写或原子替换均不能沿用字节游标。
# 函数用途: 固定原审计实例的元数据版本。
def _fd_version(descriptor):
    value = os.fstat(descriptor)
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns]


# LLM: 缺文件才是None；链接、权限和路径错误向上传递，不把未知当空。
# 函数用途: 只读获得准确一天原审计的版本。
def audit_version(home_root, owner, day):
    try:
        descriptor = open_readonly_file_beneath(_owner_home_dir(Path(home_root), owner),
            ("memory", "curator", "runs", day + ".jsonl"))
    except FileNotFoundError:
        return None
    try:
        return _fd_version(descriptor)
    finally:
        os.close(descriptor)


# LLM: 原生订阅覆盖所有已发现provider/bucket及祖先，任意变化撤销整代；磁盘inventory不能自行恢复信任。
# 函数用途: 以固定内核观察成本取得目录变更版本，绝不遍历inventory。
def inventory_version(home_root, inventory=None):
    with _directory_lock:
        cursor = _directory_cursors.get(str(Path(home_root)))
        if cursor is None or cursor.pid != os.getpid() or cursor.observer is None:
            return None
        return cursor.observer.version()


# LLM: 保留inode与目录mtime/ctime来发现增删和换代；目录不可读不是合法空版本。
# 函数用途: 读取一个固定规范目录的原实例版本。
def _directory_version(root, relative):
    try:
        descriptor = open_directory_beneath(root, relative, require_dir_fd=True)
    except FileNotFoundError:
        return None
    try:
        value = os.fstat(descriptor)
        return [value.st_dev, value.st_ino, value.st_mtime_ns, value.st_ctime_ns]
    finally:
        os.close(descriptor)


# LLM: audit可能先于state提交；活lease或原state实例变化都不能沿用receipt，即使同字节原子替换也重核，不保存正文。
# 函数用途: 在同一个no-follow fd读取安静state，前后及canonical重新打开核对inode版本。
def _quiet_state(home_root, owner):
    home = _owner_home_dir(Path(home_root), owner)
    relative = ("memory", "curator", "state.json")
    try:
        descriptor = open_readonly_file_beneath(home, relative)
    except FileNotFoundError:
        return "missing"
    try:
        version, chunks = _fd_version(descriptor), []
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        if _fd_version(descriptor) != version:
            raise ValueError("curator_quota_state_changed")
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    descriptor = open_readonly_file_beneath(home, relative)
    try:
        if _fd_version(descriptor) != version:
            raise ValueError("curator_quota_state_changed")
    finally:
        os.close(descriptor)
    state = load_strict_json(raw)
    if type(state) is not dict or state.get("active_lease"):
        raise ValueError("curator_quota_audit_transaction_pending")
    return [version, hashlib.sha256(raw).hexdigest()]


# LLM: 分片游标绑定完整原版本与quiet state；跨拍保留未成行字节，不将原子重写误当append。
# 函数用途: 为一个owner初始化准确的有界日账读取。
def _new_job(home_root, owner, day):
    return {"owner": asdict(owner), "day": day, "source": audit_version(home_root, owner, day),
            "quiet": _quiet_state(home_root, owner), "offset": 0, "buffer": "", "runs": {}, "eof": False}


# LLM: 预算按真实bytes/rows双限；元数据重核在门外，原规范版本变化丢弃局部候选重新核，未知不提交为完成。
# 函数用途: 继续一小批原JSONL，支持跨批大行及原文件被替换后的重核。
def _scan_job(home_root, job, budget):
    owner = OwnerIdentity(**job["owner"])
    day = job["day"]
    if (job["source"] != audit_version(home_root, owner, day)
            or job["quiet"] != _quiet_state(home_root, owner)):
        job = _new_job(home_root, owner, day)
    if job["source"] is None:
        return job, True
    relative = ("memory", "curator", "runs", day + ".jsonl")
    descriptor = open_readonly_file_beneath(_owner_home_dir(Path(home_root), owner), relative)
    try:
        if _fd_version(descriptor) != job["source"]:
            raise ValueError("curator_quota_audit_changed")
        data = base64.b64decode(job["buffer"], validate=True)
        os.lseek(descriptor, job["offset"], os.SEEK_SET)
        while budget["rows"] > 0:
            position = data.find(b"\n")
            if position < 0:
                if not job["eof"]:
                    if budget["bytes"] <= 0:
                        break
                    chunk = os.read(descriptor, min(budget["bytes"], 65536))
                    budget["bytes"] -= len(chunk)
                    job["offset"] += len(chunk)
                    job["eof"] = not chunk
                    data += chunk
                    continue
                if not data:
                    break
                line, data = data, b""
            else:
                line, data = data[:position], data[position + 1:]
            budget["rows"] -= 1
            _accept_line(job, owner, line)
        if (_fd_version(descriptor) != job["source"]
                or audit_version(home_root, owner, day) != job["source"]
                or _quiet_state(home_root, owner) != job["quiet"]):
            raise ValueError("curator_quota_audit_changed")
        job["buffer"] = base64.b64encode(data).decode("ascii")
        return job, job["eof"] and not data
    finally:
        os.close(descriptor)


# LLM: 只读取原严格v2记录；同一run的不同final或重复行都拒绝，record_id不是额度去重键。
# 函数用途: 从一行真实审计提取无正文完成事实。
def _accept_line(job, owner, line):
    if not line.strip():
        raise ValueError("curator_quota_audit_invalid")
    record = CuratorRunRecord.from_record(load_strict_json(line))
    if record.finished_at[:10] != job["day"]:
        raise ValueError("curator_quota_audit_date_conflict")
    if record.phase != "final" or record.status not in {"succeeded", "failed"}:
        return
    if record.run_id in job["runs"]:
        raise ValueError("curator_quota_run_binding_ambiguous")
    job["runs"][record.run_id] = {"identity": fact_identity(owner, record.run_id),
        "record_id": record.record_id, "finished_at": record.finished_at, "status": record.status}


# LLM: 全部持久位置与唯一count同次CAS；目录流丢失后重核规范目录，不把内存offset当会计权威。
# 函数用途: 给本日原会计建立有界迁移状态，保留legacy及旧v2基数。
def _initial_index(value, home_root, day, cursor):
    previous = value.get("quota_index", {})
    same_day = previous.get("date") == day
    cursor.close()
    cursor.token, cursor.position = uuid.uuid4().hex, 0
    return {"date": day, "status": "scanning", "inventory": inventory_version(home_root),
        "discovery": {"token": cursor.token, "position": 0, "current": None,
            "todo": [[["owners", "local"], "local", "local", "user"],
                     [["owners", "providers"], "providers", None, None]]},
        "queue": [asdict(OwnerIdentity.local_main())], "page_done": False,
        "job": None, "collected": list(previous.get("collected", [])) if same_day else [],
        "base_count": previous.get("base_count", value["count"]) if same_day else value["count"],
        "base_records": list(previous.get("base_records", value.get("accounted_records", []))) if same_day
            else list(value.get("accounted_records", [])),
        "new_records": list(previous.get("new_records", [])) if same_day else [], "receipts": {}}


# LLM: 读口同时要求当前读取代次完成与内核变更版本匹配；重启/丢事件不直接授权，签名不替代P3/lease。
# 函数用途: 判断本轮有界迁移是否完整覆盖当前注册目录。
def ready(value, home_root, day):
    index = value.get("quota_index")
    cursor = _directory_cursors.get(str(Path(home_root)))
    return (type(index) is dict and index.get("date") == day and index.get("status") == "ready"
            and cursor is not None and cursor.pid == os.getpid()
            and index.get("discovery", {}).get("token") == cursor.token
            and index.get("inventory") is not None
            and index.get("inventory") == inventory_version(home_root, index.get("inventory")))


# LLM: 全程在门外准备；游标锁仅串行化读取资源，不替原C/quota CAS；每tick至多32项/32owner/64行/64KiB。
# 函数用途: 门外准备一个可由原短门CAS提交的会计候选。
def prepare_batch(value, home_root, day, owners=(), *, controller=None):
    with _directory_lock:
        cursor = _cursor_for(home_root, controller)
        try:
            return _prepare_batch(value, home_root, day, owners, cursor)
        except Exception:
            cursor.close()
            if cursor.observer is not None:
                cursor.observer.valid = False
            cursor.token = uuid.uuid4().hex
            raise


# LLM: 目录流位置必须与原quota提交位置精确相等，候选CAS失败或进程换代重新有界核验，旧count/去重保留。
# 函数用途: 准备唯一原会计候选，不在helper或最终tick隐藏目录全扫。
def _prepare_batch(value, home_root, day, owners, cursor):
    candidate = copy.deepcopy(value)
    index = candidate.get("quota_index")
    if (type(index) is not dict or index.get("date") != day
            or index.get("inventory") is None
            or index.get("inventory") != inventory_version(home_root, index.get("inventory"))
            or index.get("discovery", {}).get("token") != cursor.token
            or index.get("discovery", {}).get("position") != cursor.position):
        if not cursor.reset_observer():
            if type(index) is dict:
                index["status"] = "scanning"
            return candidate
        index = candidate["quota_index"] = _initial_index(candidate, home_root, day, cursor)
    candidate.setdefault("schema_version", "curator_daily_quota.v2")
    candidate.setdefault("accounted_records", [])
    candidate.setdefault("migration", {"from": "legacy-date-count", "preserved_count": value["count"]})
    budget = {"owners": 32, "entries": 32, "directories": 32, "rows": 64, "bytes": 65536}
    if index["status"] == "scanning":
        _migrate_page(candidate, home_root, budget, cursor)
    elif owners or index.get("job") is not None:
        _refresh_owners(candidate, home_root, owners, budget)
    inventory = candidate["quota_index"]["inventory"]
    if inventory is None or inventory_version(home_root, inventory) != inventory:
        raise ValueError("curator_quota_owner_inventory_changed")
    return candidate


# LLM: 原fd逐项读取不排序、不listdir；provider/bucket也在固定预算下订阅，目录换代/丢事件撤销整代。
# 函数用途: 返回最多32个真实目录项产生的规范Owner，持久待办与位置由外层同次CAS提交。
def _discover_page(index, home_root, budget, cursor):
    discovery = index["discovery"]
    while budget["entries"] > 0 and budget["directories"] > 0:
        if discovery["current"] is None:
            if not discovery["todo"]:
                index["page_done"] = True
                break
            current = discovery["todo"].pop(0)
            budget["directories"] -= 1
            cursor.position += 1
            try:
                cursor.observer.watch(current[0])
                cursor.open(current[0])
            except FileNotFoundError:
                continue
            discovery["current"] = current
        current = discovery["current"]
        if cursor.stream is None or cursor.relative != current[0]:
            raise ValueError("curator_quota_directory_cursor_lost")
        try:
            entry = next(cursor.stream)
        except StopIteration:
            cursor.close()
            cursor.position += 1
            discovery["current"] = None
            continue
        budget["entries"] -= 1
        cursor.position += 1
        if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
            continue
        relative, mode, provider, kind = current
        if mode == "providers":
            # provider目录自身版本发现新增bucket，bucket版本发现新增owner，均不枚举条目。
            provider_path = relative + [entry.name]
            cursor.observer.watch(provider_path)
            for bucket, owner_kind in (("users", "user"), ("groups", "group")):
                if entry.name == "local" and bucket == "users":
                    continue
                path = provider_path + [bucket]
                cursor.observer.watch(path)
                discovery["todo"].append([path, "bucket", entry.name, owner_kind])
            continue
        if mode == "local":
            if entry.name == "main":
                continue
            owner = OwnerIdentity.local_user(entry.name)
        else:
            owner = (OwnerIdentity.provider_user(provider, entry.name) if kind == "user"
                     else OwnerIdentity.provider_group(provider, entry.name))
        index["queue"].append(asdict(owner))
    discovery["position"] = cursor.position


# LLM: queue及目录流位置只是原quota内的进度；目录页只读一次，原审计行/字节预算全页共享。
# 函数用途: 用剩余预算核完当前迁移页及尚未完成的原审计。
def _migrate_page(value, home_root, budget, cursor):
    index, paged = value["quota_index"], False
    while budget["owners"] > 0:
        if index["job"] is None and not index["queue"]:
            if index["page_done"]:
                _finish_migration(value)
                return
            if paged:
                return
            _discover_page(index, home_root, budget, cursor)
            paged = True
            if not index["queue"]:
                if index["page_done"]:
                    _finish_migration(value)
                return
        if index["job"] is None:
            index["job"] = _new_job(home_root, OwnerIdentity(**index["queue"].pop(0)), index["date"])
        budget["owners"] -= 1
        job, complete = _scan_job(home_root, index["job"], budget)
        index["job"] = job
        if not complete:
            return
        _store_receipt(value, job, migrating=True)
        index["job"] = None
    if index["job"] is None and not index["queue"] and index["page_done"]:
        _finish_migration(value)


# LLM: 完成事实已来自同版本、无活事务的严格原日账；曾见的final不能因替换/截断悄悄撤销。
# 函数用途: 更新准确owner分片receipt，并把完成键交给唯一全局会计。
def _store_receipt(value, job, *, migrating):
    index = value["quota_index"]
    owner = OwnerIdentity(**job["owner"])
    sha = owner_digest(owner)
    previous = index["receipts"].get(sha)
    if previous is not None and any(previous["runs"].get(key) != job["runs"].get(key) for key in previous["runs"]):
        raise ValueError("curator_quota_audit_history_changed")
    if job["source"] is not None:
        index["receipts"][sha] = {"owner": job["owner"], "source": job["source"],
            "quiet": job["quiet"], "runs": job["runs"]}
    identities = {row["identity"] for row in job["runs"].values()}
    if migrating:
        index["collected"] = sorted(set(index["collected"]) | identities)
    else:
        merge_facts(value, identities, actual=False)


# LLM: 原迁移采用max(legacy,已发现事实)而非相加；重审前已计事实不是未知增量，不能因事件失效重扣。
# 函数用途: 完成同日有界迁移并保持唯一count与去重列表原子一致。
def _finish_migration(value):
    index = value["quota_index"]
    known = set(value["accounted_records"])
    new = set(index["new_records"])
    collected = set(index["collected"]) | set(index["base_records"])
    unidentified = max(0, value["count"] - max(index["base_count"], len(known - new)) - len(new))
    value["count"] = max(index["base_count"], len(collected - new)) + len(new) + unidentified
    value["accounted_records"] = sorted(known | collected)
    value["quota_index"] = {"date": index["date"], "status": "ready", "inventory": index["inventory"],
        "discovery": index["discovery"],
        "receipts": index["receipts"], "job": None, "base_count": index["base_count"],
        "base_records": index["base_records"], "new_records": sorted(new), "collected": sorted(collected)}


# LLM: 只改调用者持有的原quota候选，真正持锁写入仍由原C提交；事实和总数不会分开落盘。
# 函数用途: 按完整owner/run身份幂等增加真实完成次数。
def merge_facts(value, identities, *, actual):
    existing = set(value["accounted_records"])
    incoming = set(identities)
    added = incoming - existing
    value["count"] += len(added)
    value["accounted_records"] = sorted(existing | incoming)
    index = value.get("quota_index")
    if type(index) is dict and "base_records" in index:
        new = set(index["new_records"])
        promoted = (incoming & existing & set(index["collected"]) - set(index["base_records"]) - new
                    if actual else set())
        if index.get("status") != "scanning" and promoted:
            before = max(index["base_count"], len(set(index["collected"]) - new))
            after = max(index["base_count"], len(set(index["collected"]) - new - promoted))
            value["count"] += len(promoted) + after - before
        index["new_records"] = sorted(new | added | promoted)
    return value


# LLM: 稳态只点查本轮活跃Owner；改写的原JSONL从头按固定批重核，旧token绝不无条件seek。
# 函数用途: 刷新指定owner的变更及前一拍未完审计，不扫描无关注册用户。
def _refresh_owners(value, home_root, owners, budget):
    index = value["quota_index"]
    pending = index.get("job")
    ordered = [OwnerIdentity(**pending["owner"])] if pending is not None else []
    ordered.extend(owner for owner in owners if owner not in ordered)
    for owner in ordered:
        if budget["owners"] <= 0:
            return
        budget["owners"] -= 1
        if pending is not None and pending["owner"] == asdict(owner):
            job = pending
        else:
            version = audit_version(home_root, owner, index["date"])
            old = index["receipts"].get(owner_digest(owner))
            quiet = _quiet_state(home_root, owner)
            if version is None and old is None:
                continue
            if old is not None and old["source"] == version and old["quiet"] == quiet:
                continue
            job = _new_job(home_root, owner, index["date"])
        job, complete = _scan_job(home_root, job, budget)
        if not complete:
            index["job"], index["status"] = job, "refreshing"
            return
        _store_receipt(value, job, migrating=False)
        index["job"], index["status"] = None, "ready"
        pending = None


# LLM: receipt只是原会计已核的同日来源投影；必须在使用时点核原文件/state版本，不由元数据伪造运行。
# 函数用途: 取得准确Owner的已核完成事实，变化或未完一律拒绝。
def owner_facts(value, home_root, owner, day):
    index = value.get("quota_index")
    if type(index) is not dict or index.get("date") != day or index.get("status") != "ready":
        raise ValueError("curator_quota_migration_pending")
    receipt = index["receipts"].get(owner_digest(owner))
    version = audit_version(home_root, owner, day)
    if receipt is None and version is None:
        return {}
    if (receipt is None or receipt.get("owner") != asdict(owner) or receipt["source"] != version
            or receipt["quiet"] != _quiet_state(home_root, owner)):
        raise ValueError("curator_quota_audit_pending")
    return receipt["runs"]


# LLM: 私有读口不签原运行身份；精确终态结清另核原P3/attempt，日归属来自原finished_at而非请求创建时间。
# 函数用途: 从已核原receipt找准确run，给原释放叶提供无正文会计事实。
def completed_fact(value, home_root, owner, run_id, day):
    records = owner_facts(value, home_root, owner, day)
    result = records.get(run_id)
    if result is None:
        raise ValueError("curator_quota_actual_audit_required")
    if result["identity"] != fact_identity(owner, run_id) or result["finished_at"][:10] != day:
        raise ValueError("curator_quota_actual_audit_conflict")
    return result


# LLM: terminal的原请求/route/attempt/epoch只绑定准备结果，不赋予释放权；仍须原release_admission核准。
# 函数用途: 防止完成准备被另一请求或另一次原执行借用。
def completion_binding(terminal, owner, run_id):
    return digest({"request_id": terminal.get("id"), "route": terminal.get("route"),
        "attempt": terminal.get("execution_attempt_id"), "epoch": terminal.get("lease_epoch"),
        "owner": asdict(owner), "run_id": run_id})


# LLM: state中的准确last_committed_run_id仅作定位提示，最终仍读原严格审计；没有准确提示才按原lease时间有界找日分片。
# 函数用途: 给迟到完成确定可核的UTC日期范围，不遍历全部历史文件。
def _completion_days(home_root, owner, run_id, terminal):
    from datetime import datetime, timedelta, timezone

    raw = read_bytes_beneath(_owner_home_dir(Path(home_root), owner),
        ("memory", "curator", "state.json"), require_dir_fd=True)
    state = load_strict_json(raw) if raw is not None else {}
    if type(state) is not dict or state.get("active_lease"):
        raise ValueError("curator_quota_audit_transaction_pending")
    if state.get("last_committed_run_id") == run_id and state.get("last_success_at"):
        exact = datetime.fromisoformat(state["last_success_at"]).astimezone(timezone.utc).date()
        return exact.isoformat(), exact.isoformat()
    start, end = terminal.get("lease_started_at"), terminal.get("completed_at")
    if (type(start) not in {int, float} or type(end) not in {int, float}
            or start <= 0 or end < start):
        raise ValueError("curator_quota_completion_time_unknown")
    first = datetime.fromtimestamp(start, timezone.utc).date() - timedelta(days=1)
    last = datetime.fromtimestamp(end, timezone.utc).date()
    return first.isoformat(), last.isoformat()


# LLM: 失效来源只废弃候选receipt并重建定位；绑定冲突不重建，原日账仍限四日/64行/64KiB，原P3不变。
# 函数用途: 门外为跨日完成准备一条准确、可重放的原审计定位。
def prepare_completion(value, home_root, owner, run_id, terminal):
    from datetime import date, timedelta

    candidate = copy.deepcopy(value)
    binding = completion_binding(terminal, owner, run_id)
    receipts = candidate.setdefault("quota_completion_receipts", {})
    receipt = receipts.get(binding)
    previous_fact = None
    if receipt is not None:
        try:
            validate_completion(receipt, home_root, owner, run_id, terminal)
        except CompletionSourceChanged:
            previous_fact = receipt["fact"]
            del receipts[binding]
            candidate.setdefault("quota_pending_completions", {}).pop(binding, None)
        else:
            return candidate, True
    jobs = candidate.setdefault("quota_pending_completions", {})
    progress = jobs.get(binding)
    if progress is None:
        first, last = _completion_days(home_root, owner, run_id, terminal)
        progress = jobs[binding] = {"owner": asdict(owner), "run_id": run_id,
            "binding": binding, "day": first, "last_day": last, "job": None,
            "previous_fact": previous_fact}
    if (progress["owner"] != asdict(owner) or progress["run_id"] != run_id
            or progress["binding"] != binding):
        raise ValueError("curator_quota_completion_binding_conflict")
    budget = {"owners": 4, "rows": 64, "bytes": 65536}
    while budget["owners"] > 0 and progress["day"] <= progress["last_day"]:
        budget["owners"] -= 1
        job = progress["job"] or _new_job(home_root, owner, progress["day"])
        job, complete = _scan_job(home_root, job, budget)
        progress["job"] = job
        if not complete:
            return candidate, False
        fact = job["runs"].get(run_id)
        if fact is not None:
            if progress.get("previous_fact") is not None and progress["previous_fact"] != fact:
                raise ValueError("curator_quota_completion_binding_conflict")
            receipts[binding] = {"binding": binding, "owner": asdict(owner), "run_id": run_id,
                "day": job["day"], "source": job["source"], "quiet": job["quiet"], "fact": fact}
            del jobs[binding]
            return candidate, True
        progress["job"] = None
        progress["day"] = (date.fromisoformat(progress["day"]) + timedelta(days=1)).isoformat()
    if progress["day"] > progress["last_day"]:
        raise ValueError("curator_quota_actual_audit_required")
    return candidate, False


# LLM: 真正计账仍点核原审计和quiet state；绑定冲突不可重建，来源换代用独立异常使门外按原预算重核。
# 函数用途: 在原短门内验证门外已读的准确完成事实。
def validate_completion(receipt, home_root, owner, run_id, terminal):
    if (receipt.get("binding") != completion_binding(terminal, owner, run_id)
            or receipt.get("owner") != asdict(owner) or receipt.get("run_id") != run_id
            or receipt["fact"]["identity"] != fact_identity(owner, run_id)
            or receipt["fact"]["finished_at"][:10] != receipt["day"]):
        raise ValueError("curator_quota_completion_binding_conflict")
    if (receipt.get("source") != audit_version(home_root, owner, receipt["day"])
            or receipt.get("quiet") != _quiet_state(home_root, owner)):
        raise CompletionSourceChanged("curator_quota_completion_source_changed")
    return receipt["fact"]
