# LLM: 审计只投影显式可信root内权威路径，coverage未知不能转成成功空记录。
# 模块用途: 提供审计固定分页、SQL统计及共用原追加锁的安全保留叶。
from __future__ import annotations

import errno
import json
import math
import os
import secrets
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..common.cache_freshness import cache_stat_signature
from ..common.json_io import _flock_descriptor, _funlock_descriptor, path_thread_lock
from ..common.nofollow_fs import NoFollowPathError, open_directory_beneath
from ..local_storage.owner_read_index import OwnerReadIndex, require_complete

if TYPE_CHECKING:
    from ..settings.config import AgentConfig

from .logger import AuditAction, AuditEntry
from .paths import resolve_audit_paths

_AUDIT_RETENTION_MAX_BYTES = 16 * 1024 * 1024
_AUDIT_RETENTION_ROW_MAX_BYTES = 1024 * 1024


@dataclass
class AuditQueryResult:
    """审计查询结果。"""

    entries: list[AuditEntry]
    total_count: int
    query_time_ms: float


@dataclass(frozen=True)
class AuditQueryParams:
    """Bundle of AuditQuery.query parameters."""

    user_id: str | None = None
    action: AuditAction | str | None = None
    target_id: str | None = None
    target_type: str | None = None
    status: str | None = None
    start_time: float | None = None
    end_time: float | None = None
    limit: int = 100
    offset: int = 0


def _normalize_query_params(
    params: AuditQueryParams | None,
    *,
    user_id: str | None = None,
    action: AuditAction | str | None = None,
    target_id: str | None = None,
    target_type: str | None = None,
    status: str | None = None,
    start_time: float | None = None,
    end_time: float | None = None,
    limit: int = 100,
    offset: int = 0,
) -> AuditQueryParams:
    if isinstance(params, AuditQueryParams):
        return params
    return AuditQueryParams(
        user_id=user_id,
        action=action,
        target_id=target_id,
        target_type=target_type,
        status=status,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
        offset=offset,
    )


# LLM: 运行时权威审计路径（可能带后缀）与配置里的 audit_log_path 语义相同，所以用同一个解析器；
#   这是一个只暴露 audit_log_path 的最小适配器，不引入第二套路径规则。
# 类用途: 把运行时权威审计路径包装成 resolve_audit_paths 认识的配置形状。
class _RuntimeAuditPathConfig:
    # 函数用途: 保存运行时权威审计路径供解析器读取。
    def __init__(self, audit_log_path: Path | str) -> None:
        self.audit_log_path = str(audit_log_path)


# LLM: owner只能由可信调用方传入，不从配置路径或来源父目录猜测。
# 类用途: 绑定审计路径并提供带coverage的索引查询。
class AuditQuery:

    # LLM: root 是调用方的 canonical 基准（owner home）；给了它，配置里的相对路径就不再随进程 cwd 漂移。
    #   不给时保持旧行为，避免影响既有测试与旧调用方。
    # 函数用途: 建立审计查询视图，可选地按给定基准解析相对审计路径。
    # LLM: root 是调用方的 canonical 基准（owner home）；给了它，相对路径就不再随进程 cwd 漂移。
    #   runtime_audit_path 是运行时路径的权威值（Agent 启动时注入 config 的那一份），给了它就以它为准——
    #   审计日志地址不该由"配置恰好空值 → 落到默认 data/audit"这条隐式链决定。
    # 函数用途: 建立审计查询视图，按给定基准或运行时权威路径解析审计路径。
    def __init__(
        self,
        config: AgentConfig,
        *,
        root: Path | str | None = None,
        runtime_audit_path: Path | str | None = None,
    ):
        self.config = config
        if runtime_audit_path is not None:
            # 权威值交给同一个解析器，和写入端 audit/logger.py 走同一条规则：带后缀就是文件本身，
            # 否则是目录。这里再拼一次 "/audit.jsonl" 会让自定义文件名（如 .../custom-audit.jsonl）
            # 的查询落到 <文件>/audit.jsonl：查询为空、清理空转（dev 2026-09-29 第 6 条）。
            paths = resolve_audit_paths(
                _RuntimeAuditPathConfig(runtime_audit_path), root=root
            )
        else:
            paths = resolve_audit_paths(config, root=root)
        self._audit_root = paths.root
        self._audit_file = paths.log_file
        self._trusted_root = Path(os.path.abspath(os.path.expanduser(os.fspath(root)))) if root is not None else None
        self._read_index = None
        self._read_parts = None
        self._index_reason = 'trusted_root_required'
        if root is not None:
            home = Path(os.path.abspath(os.path.expanduser(os.fspath(root))))
            source = Path(os.path.abspath(os.fspath(self._audit_file)))
            try:
                self._read_parts = source.relative_to(home).parts
            except ValueError:
                self._index_reason = 'source_outside_trusted_root'
            else:
                self._read_index = OwnerReadIndex(home)

    # LLM: 旧接口不能表达未知总数，仅完整报告构造原结果类型。
    # 函数用途: 保持原参数及完整成功时的AuditQueryResult。
    def query(
        self,
        params: AuditQueryParams | None = None,
        *,
        user_id: str | None = None,
        action: AuditAction | str | None = None,
        target_id: str | None = None,
        target_type: str | None = None,
        status: str | None = None,
        start_time: float | None = None,
        end_time: float | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> AuditQueryResult:
        params = _normalize_query_params(
            params,
            user_id=user_id,
            action=action,
            target_id=target_id,
            target_type=target_type,
            status=status,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
            offset=offset,
        )
        report = require_complete(self.query_report(params))
        return AuditQueryResult(
            entries=report['entries'], total_count=report['total_count'],
            query_time_ms=report['query_time_ms'],
        )

    # LLM: 未提供可信root或来源在根外时不读取也不创建缓存。
    # 函数用途: 捕获固定审计view或明确的不可用原因。
    def _capture_view(self):
        if self._read_index is None:
            return {'status': 'unavailable', 'reason': self._index_reason}
        return self._read_index.advance('audit', self._read_parts)

    # LLM: 时间相等仍按物理顺序，seek绑定固定view和过滤；旧offset仅首拍使用，内部续页不重复COUNT或偏移扫描。
    # 函数用途: 返回审计条目、总数、coverage和下一页游标，不扩大固定through范围。
    def query_report(self, params=None, *, view=None, after_cursor=None, **kwargs):
        params = _normalize_query_params(params, **kwargs)
        if (type(params.limit) is not int or type(params.offset) is not int
                or min(params.limit, params.offset) < 0):
            raise ValueError('invalid pagination')
        if after_cursor is not None and params.offset:
            raise ValueError('after_cursor and offset are mutually exclusive')
        started = time.perf_counter()
        view = self._capture_view() if view is None else view
        if self._read_index is None:
            return {'entries': [], 'total_count': None, 'query_time_ms': 0,
                    'coverage': self._capture_view(), 'view': view, 'next_cursor': None}
        action = params.action.value if isinstance(params.action, AuditAction) else params.action
        filters = {key: value for key, value in {
            'user_id': params.user_id, 'action': action, 'target_id': params.target_id,
            'target_type': params.target_type, 'status': params.status,
        }.items() if value}
        if params.start_time:
            filters['since'] = params.start_time
        if params.end_time:
            filters['until'] = params.end_time
        entries, offset, remaining = [], params.offset, params.limit
        cursor = after_cursor
        while True:
            page = self._read_index.query(view, filters=filters, limit=min(remaining, 1000),
                                          offset=offset, descending=True, after_cursor=cursor)
            entries.extend(AuditEntry.from_dict(row) for row in page['rows'])
            count = len(page['rows'])
            remaining -= count
            cursor = page['next_cursor']
            offset = 0
            if (page['coverage']['status'] != 'complete' or remaining <= 0
                    or count == 0 or cursor is None):
                return {'entries': entries, 'total_count': page['total_count'],
                        'query_time_ms': (time.perf_counter() - started) * 1000,
                        'coverage': page['coverage'], 'view': view, 'next_cursor': cursor}

    # LLM: 不可用来源不生成零摘要，用户过滤在固定SQL内执行。
    # 函数用途: 返回带coverage的审计统计。
    def summary_report(self, user_id=None, *, view=None):
        view = self._capture_view() if view is None else view
        if self._read_index is None:
            return {'value': None, 'coverage': self._capture_view()}
        return self._read_index.audit_rollup(view, user_id=user_id)

    # LLM: 成功字典只代表完整覆盖，调用方可捕获异常coverage展示未知。
    # 函数用途: 保持原审计摘要接口。
    def summary(self, user_id: str | None = None) -> dict[str, Any]:
        return require_complete(self.summary_report(user_id))['value']

    # LLM: 用户最后活动同时间时以首次出现物理顺序排序。
    # 函数用途: 返回带coverage的有限最近用户列表。
    def recent_users_report(self, limit=10, *, view=None):
        view = self._capture_view() if view is None else view
        if self._read_index is None:
            return {'value': None, 'coverage': self._capture_view()}
        return self._read_index.audit_rollup(view, recent_limit=limit)

    # LLM: 部分观察不返回成功空列表，未知通过结构异常传播。
    # 函数用途: 保持原最近用户列表接口。
    def recent_users(self, limit: int = 10) -> list[dict[str, Any]]:
        return require_complete(self.recent_users_report(limit))['value']

    # LLM: 默认永久保留；从可信根逐段nofollow后取得原路径线程锁及同名flock，所有文件操作锚定该父FD。
    # 函数用途: 安全裁剪已证明过期的审计记录，不把未知或I/O错误算作删除。
    def cleanup_old_entries(self, days: int = 0) -> int:
        if days <= 0:
            return 0
        try:
            with _audit_parent(self._audit_file, self._trusted_root) as (parent, root):
                _audit_regular(os.stat(self._audit_file.name, dir_fd=parent, follow_symlinks=False))
                handle = path_thread_lock(self._audit_file)
                with handle.lock, _audit_append_lock(parent, self._audit_file.name):
                    plan = _plan_audit_retention(self._audit_file, cutoff_time=time.time() - days * 86400,
                                                 parent_fd=parent, trusted_root=root)
                    return _apply_audit_retention_unlocked(plan, parent_fd=parent)
        except FileNotFoundError:
            return 0


# LLM: 计划只在本次原append锁内消费，绑定准确文件与五元generation，不能作为授权或持久事实。
# 类用途: 保存审计保留字节及已证实的删除计数。
@dataclass(frozen=True)
class _AuditRetentionPlan:
    path: Path
    generation: tuple[int, int, int, int, int]
    kept_bytes: bytes
    deleted_count: int
    trusted_root: Path
    parent_identity: tuple[int, int]


# LLM: 拒绝时只暴露固定原因码，不携带外来正文；该错误不能伪装为已清理零条。
# 类用途: 为审计路径边界失败提供稳定的结构化诊断。
class UnsafeAuditPathError(OSError):
    reason_code = 'unsafe_audit_path'


# LLM: 根由调用方显式传入；旧私有入口无root时从文件系统根逐段验证，不把最近已存在父目录当可信锚点。
# 函数用途: 逐段nofollow打开审计父目录并在所有退出分支释放FD。
@contextmanager
def _audit_parent(path, trusted_root=None):
    path = Path(os.path.abspath(path))
    root = Path(os.path.abspath(trusted_root)) if trusted_root is not None else Path(path.anchor)
    try:
        parts = path.relative_to(root).parts
        if not parts:
            raise UnsafeAuditPathError('unsafe audit path')
        descriptor = open_directory_beneath(root, parts[:-1], require_dir_fd=True)
    except (NoFollowPathError, ValueError) as exc:
        raise UnsafeAuditPathError('unsafe audit path') from exc
    try:
        yield descriptor, root
    finally:
        os.close(descriptor)


# LLM: nofollow本身不拒绝硬链；源和旁锁必须为单链接普通文件，检查在任何改权限或写入前执行。
# 函数用途: 拒绝软链、特殊文件及硬链别名。
def _audit_regular(info):
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise UnsafeAuditPathError('unsafe audit path')


# LLM: 沿原json_io旁锁命名和flock协议；只相对已验证父FD创建，锁叶被替换或改成别名时拒绝。
# 函数用途: 安全取得与logger兼容的审计追加锁并保证释放。
@contextmanager
def _audit_append_lock(parent, name):
    lock_name = name + '.lock'
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0)
    try:
        descriptor = os.open(lock_name, flags, 0o600, dir_fd=parent)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise UnsafeAuditPathError('unsafe audit path') from exc
        raise
    try:
        _audit_regular(os.fstat(descriptor))
        os.fchmod(descriptor, 0o600)
        _flock_descriptor(descriptor)
        try:
            current = os.stat(lock_name, dir_fd=parent, follow_symlinks=False)
            _audit_regular(current)
            held = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
                raise BlockingIOError('audit append lock changed')
            yield
        finally:
            _funlock_descriptor(descriptor)
    finally:
        os.close(descriptor)


# LLM: 同叶generation不能替代祖先验证；重新从原可信根打开后比对父inode，不通过新路径跟随软链。
# 函数用途: 拒绝规划或应用期间发生的审计父目录替换。
def _audit_parent_current(path, root, parent, expected=None):
    held = os.fstat(parent)
    identity = (held.st_dev, held.st_ino)
    with _audit_parent(path, root) as (current, _root):
        observed = os.fstat(current)
        if (observed.st_dev, observed.st_ino) != identity or (expected is not None and expected != identity):
            raise BlockingIOError('audit parent changed')
    return identity


# LLM: 只有可解码对象的有限数值时间戳才可删除，其余字节保持原样，bool不当数值。
# 函数用途: 判断一条物理LF记录是否已证明过期。
def _audit_row_expired(raw_line, cutoff_time):
    try:
        row = json.loads(raw_line.decode('utf-8'))
        timestamp = row.get('timestamp') if isinstance(row, dict) else None
        return type(timestamp) in (int, float) and math.isfinite(timestamp) and timestamp < cutoff_time
    except (UnicodeError, ValueError, OverflowError, RecursionError):
        return False


# LLM: 不吞I/O错误或删除UTF8坏行；祖先逐段验证，来源始终相对同一父FD，源generation和父身份均封入计划。
# 函数用途: 以物理LF规划安全审计保留。
def _plan_audit_retention(path, *, cutoff_time, parent_fd=None, trusted_root=None):
    path = Path(os.path.abspath(path))
    if parent_fd is None:
        with _audit_parent(path, trusted_root) as (parent, root):
            return _plan_audit_retention(path, cutoff_time=cutoff_time, parent_fd=parent, trusted_root=root)
    identity = _audit_parent_current(path, trusted_root, parent_fd)
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    try:
        descriptor = os.open(path.name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise UnsafeAuditPathError('unsafe audit path') from exc
        raise
    try:
        before = os.fstat(descriptor)
        _audit_regular(before)
        if before.st_size > _AUDIT_RETENTION_MAX_BYTES:
            raise OSError('audit retention byte budget exceeded')
        generation = cache_stat_signature(before)
        kept = []
        deleted_count = 0
        consumed = 0
        with os.fdopen(descriptor, 'rb', closefd=False) as source:
            while raw_line := source.readline(min(_AUDIT_RETENTION_ROW_MAX_BYTES,
                                                  _AUDIT_RETENTION_MAX_BYTES - consumed) + 1):
                consumed += len(raw_line)
                if consumed > _AUDIT_RETENTION_MAX_BYTES or len(raw_line) > _AUDIT_RETENTION_ROW_MAX_BYTES:
                    raise OSError('audit retention byte budget exceeded')
                if _audit_row_expired(raw_line, cutoff_time):
                    deleted_count += 1
                else:
                    kept.append(raw_line)
        after = cache_stat_signature(os.fstat(descriptor))
        current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        _audit_regular(current)
        _audit_parent_current(path, trusted_root, parent_fd, identity)
        if generation != after or after != cache_stat_signature(current):
            raise BlockingIOError('安全审计文件在规划期间发生变化')
    finally:
        os.close(descriptor)
    return _AuditRetentionPlan(path, generation, b''.join(kept), deleted_count, trusted_root, identity)


# LLM: 调用方持原线程锁和同名flock；复核父身份及源generation，在同父FD写私有临时文件并替换，不重新跟随路径。
# 函数用途: 应用已证明的安全审计保留计划，零删除不创建tmp。
def _apply_audit_retention_unlocked(plan, *, parent_fd=None):
    if plan is None or not plan.deleted_count:
        return 0
    if parent_fd is None:
        with _audit_parent(plan.path, plan.trusted_root) as (parent, _root):
            return _apply_audit_retention_unlocked(plan, parent_fd=parent)
    _audit_parent_current(plan.path, plan.trusted_root, parent_fd, plan.parent_identity)
    current = os.stat(plan.path.name, dir_fd=parent_fd, follow_symlinks=False)
    _audit_regular(current)
    if cache_stat_signature(current) != plan.generation:
        raise BlockingIOError('安全审计文件在应用前发生变化')
    name = '.' + plan.path.name + '.' + secrets.token_hex(16) + '.tmp'
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(plan.kept_bytes)
            output.flush()
            os.fsync(output.fileno())
        _audit_parent_current(plan.path, plan.trusted_root, parent_fd, plan.parent_identity)
        current = os.stat(plan.path.name, dir_fd=parent_fd, follow_symlinks=False)
        _audit_regular(current)
        if cache_stat_signature(current) != plan.generation:
            raise BlockingIOError('安全审计文件在应用前发生变化')
        os.replace(name, plan.path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        try:
            os.unlink(name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
    return plan.deleted_count


__all__ = ["AuditQuery", "AuditQueryResult"]
