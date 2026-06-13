# LLM: 本模块属于 gateway_parts,提供"机器级进程单例"scoped lock(长期助手 风格,
#   pid+进程启动时间判归属)。契约:锁的持有者是【进程】不是线程——同一进程内任意
#   线程 acquire 同一把锁都视为持有者重入,只刷新锁记录(heartbeat 语义)并返回成功;
#   release 同样按进程维度删除。因此它只提供进程对进程互斥,绝不能当线程临界区锁用;
#   线程互斥请直接用 threading.Lock(参考 agent/io/jsonl.py 的"线程锁+flock"双层模式,
#   对照组 长期助手 ProcessRegistry 也是同样的语义分离)。改动 _owns_lock/release 的归属
#   判定时,必须同步检查 supervisor/daemon_control 的进程单例用途,以及
#   tests/test_real_io_concurrency.py(进程级语义钉子)和 tests/test_daemon_control.py。
# 模块用途: gateway 网关身份的机器本地锁:保证同一台机器上同一 scope+identity 只有
#   一个活着的进程持有(例如防止两个 gateway 进程同时服务同一个身份)。锁文件落在
#   XDG_STATE_HOME/my-agent/locks/,记录 pid、进程启动时间、metadata 和更新时间;
#   持有进程重复 acquire 等于刷新心跳,死进程残留的锁会被自动接管清理。
from __future__ import annotations

"""Machine-local scoped locks for gateway identities."""

import json
import os
from pathlib import Path
from typing import Any

from ..runtime_errors import DataCorruptionError, runtime_error_report
from .daemon_metadata import (
    _build_pid_record,
    _get_process_start_time,
    _scope_hash,
    _utc_now_iso,
    _write_json_file,
)
from .process_control import is_pid_alive


# LLM: 锁目录唯一权威位置;测试通过 monkeypatch XDG_STATE_HOME 或 patch 本函数隔离。
# 函数用途: 返回机器本地锁目录(XDG_STATE_HOME/my-agent/locks),不负责创建目录。
def _get_lock_dir() -> Path:
    state_home = Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return state_home / "my-agent" / "locks"


# LLM: 锁文件名由 scope + identity 哈希构成;identity 本身不落盘到文件名,避免泄露。
# 函数用途: 把 scope+identity 映射成锁文件路径,acquire/release 都用它定位同一把锁。
def _get_scope_lock_path(scope: str, identity: str) -> Path:
    return _get_lock_dir() / f"{scope}-{_scope_hash(identity)}.lock"


# LLM: 锁记录 schema:pid 记录(pid+start_time)+ scope/identity_hash/metadata/updated_at;
#   updated_at 即心跳时间,重入刷新时整条重写。改 schema 要同步 _owns_lock/_lock_process_stale。
# 函数用途: 构造一条要写进锁文件的 JSON 记录,描述"当前进程此刻持有这把锁"。
def _build_scope_lock_record(scope: str, identity: str, metadata: dict[str, Any] | None) -> dict:
    return {
        **_build_pid_record(),
        "scope": scope,
        "identity_hash": _scope_hash(identity),
        "metadata": metadata or {},
        "updated_at": _utc_now_iso(),
    }


# LLM: 容错读取 pid 字段;记录损坏/缺字段时返回 None,调用方按"无法判定归属"处理。
# 函数用途: 从锁记录里安全取出 pid 整数,取不到就返回 None。
def _lock_pid(record: dict | None) -> int | None:
    try:
        return int((record or {})["pid"])
    except (KeyError, TypeError, ValueError):
        return None


# LLM: 锁归属判定的唯一权威:pid+start_time 都相同才算"本进程持有"。这里【故意】
#   不含 thread id——同进程线程重入=心跳刷新是 supervisor 依赖的特性,不是缺陷;
#   给这里加线程维度会破坏进程单例语义。线程互斥需求请用 threading.Lock,别改这里。
# 函数用途: 判断已存在的锁记录是不是当前进程自己写的(进程维度,不区分线程)。
def _owns_lock(existing: dict, record: dict) -> bool:
    pid = _lock_pid(existing)
    return pid == os.getpid() and existing.get("start_time") == record.get("start_time")


# LLM: 失活判定三连:pid 非法 / 进程不存在 / pid 被复用(start_time 不匹配)。
#   start_time 任一侧缺失时保守判为"未失活",避免误抢活进程的锁。
# 函数用途: 判断锁的持有进程是否已经死了或 pid 已被新进程复用,死锁可被接管。
def _lock_process_stale(existing: dict) -> bool:
    pid = _lock_pid(existing)
    if pid is None:
        return True
    if not is_pid_alive(pid):
        return True
    current_start = _get_process_start_time(pid)
    recorded_start = existing.get("start_time")
    return recorded_start is not None and current_start is not None and current_start != recorded_start


# LLM: 删锁文件,OSError 静默吞掉(锁清理是尽力而为,失败下轮 stale 判定兜底)。
# 函数用途: 安全删除锁文件,文件不存在或删不掉都不抛异常。
def _remove_lock_file(lock_path: Path) -> None:
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


# LLM: O_CREAT|O_EXCL 原子创建是进程间互斥的根基;FileExistsError=别人先到,返回 False。
#   写 JSON 失败时回滚删文件再抛,避免留下半截锁。副作用:创建并写入锁文件。
# 函数用途: 原子地"先到先得"创建锁文件并写入持有记录,创建失败说明锁已被占。
def _create_lock_file(lock_path: Path, record: dict) -> bool:
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
    except Exception:
        _remove_lock_file(lock_path)
        raise
    return True


# LLM: 读锁记录,返回 (payload, load_error) 二选一;损坏文件必须显式报 load_error,
#   不能静默当作"没有锁"(铁律:数据损坏走结构化报告,不硬猜)。
# 函数用途: 读取并解析锁文件,文件缺失返回 (None, None),损坏返回 (None, 错误报告)。
def _read_lock_record_report(lock_path: Path) -> tuple[dict | None, dict | None]:
    if not lock_path.exists():
        return None, None
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        return None, _lock_load_error(lock_path, exc)
    if not raw:
        return None, _lock_load_error(lock_path, DataCorruptionError("scoped lock file is empty"))
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, _lock_load_error(lock_path, exc)
    if not isinstance(payload, dict):
        return None, _lock_load_error(
            lock_path,
            DataCorruptionError(f"scoped lock root is {type(payload).__name__}, expected object"),
        )
    return payload, None


# LLM: 错误报告统一走 runtime_error_report,context 固定 gateway.scoped_lock.read,
#   外层用 lock_load_error 键识别;测试断言依赖这两个键名。
# 函数用途: 把锁文件读取异常包装成带路径的结构化错误报告。
def _lock_load_error(lock_path: Path, exc: BaseException) -> dict:
    report = runtime_error_report(exc, context="gateway.scoped_lock.read")
    report["path"] = str(lock_path)
    return {"lock_load_error": report}


# LLM: acquire 失败兜底回读:优先返回 load_error,其次返回当前持有者记录。
# 函数用途: 抢锁失败后再读一次锁文件,告诉调用方"现在到底谁拿着"。
def _read_lock_record_or_error(lock_path: Path) -> dict | None:
    record, load_error = _read_lock_record_report(lock_path)
    return load_error or record


# LLM: 对外入口之一。返回 (acquired, existing):本进程重入→(True, 旧记录并刷新心跳);
#   他进程活着持有→(False, 持有者记录);损坏→(False, {lock_load_error});死锁接管→
#   清理后原子重建。注意:同进程内不同线程都会走"重入刷新"分支,这是契约不是 bug,
#   绝不能拿本函数做线程临界区互斥。副作用:写/删锁文件。
# 函数用途: 以当前进程身份抢占(或刷新)一把机器级单例锁,用于 gateway 身份独占。
def acquire_scoped_lock(
    scope: str, identity: str, metadata: dict[str, Any] | None = None
) -> tuple[bool, dict | None]:
    lock_path = _get_scope_lock_path(scope, identity)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    record = _build_scope_lock_record(scope, identity, metadata)
    existing, load_error = _read_lock_record_report(lock_path)
    if load_error is not None:
        return False, load_error
    if existing and _owns_lock(existing, record):
        _write_json_file(lock_path, record)
        return True, existing
    if existing and not _lock_process_stale(existing):
        return False, existing
    if existing:
        _remove_lock_file(lock_path)
    if not _create_lock_file(lock_path, record):
        return False, _read_lock_record_or_error(lock_path)
    return True, None


# LLM: 对外入口之一。只有锁记录的 pid+start_time 与当前进程完全一致才删文件;
#   他进程的锁、损坏的锁一律不动(防止误删活锁)。同进程内任意线程都可释放,
#   与 acquire 的进程维度契约对称。副作用:删锁文件。
# 函数用途: 释放当前进程持有的机器级单例锁;不是自己进程的锁调了也不会误删。
def release_scoped_lock(scope: str, identity: str) -> None:
    lock_path = _get_scope_lock_path(scope, identity)
    existing, load_error = _read_lock_record_report(lock_path)
    if load_error is not None:
        return
    if not existing or existing.get("pid") != os.getpid():
        return
    if existing.get("start_time") != _get_process_start_time(os.getpid()):
        return
    _remove_lock_file(lock_path)


# LLM: 维护入口:扫整个锁目录,只清理持有进程已死/pid 被复用的 stale 锁,
#   活锁不动。返回清理数量。副作用:批量删锁文件。
# 函数用途: 启动或修复时清扫残留死锁文件,返回清掉的数量。
def release_all_scoped_locks() -> int:
    lock_dir = _get_lock_dir()
    if not lock_dir.exists():
        return 0
    removed = 0
    for lock_file in lock_dir.glob("*.lock"):
        if _release_lock_if_stale(lock_file):
            removed += 1
    return removed


# LLM: 单个锁文件的 stale 清理;损坏文件保留并返回 False(留给人/上层报告处理,
#   不静默销毁证据)。pid 非法时直接删。副作用:可能删锁文件。
# 函数用途: 判断一个锁文件的持有进程是否已死,死了就删掉并返回 True。
def _release_lock_if_stale(lock_file: Path) -> bool:
    record, load_error = _read_lock_record_report(lock_file)
    if load_error is not None:
        return False
    if not record:
        return False
    try:
        pid = int(record["pid"])
    except (ProcessLookupError, PermissionError, ValueError):
        lock_file.unlink()
        return True
    if not is_pid_alive(pid):
        lock_file.unlink()
        return True
    if _get_process_start_time(pid) != record.get("start_time"):
        lock_file.unlink()
        return True
    return False
