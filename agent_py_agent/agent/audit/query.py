# LLM: 审计只投影显式可信root内权威路径，coverage未知不能转成成功空记录。
# 模块用途: 提供审计固定分页、SQL统计及共用原追加锁的安全保留叶。
from __future__ import annotations

import json
import math
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..common.cache_freshness import cache_stat_signature
from ..common.json_io import locked_json_path, write_private_text_file_atomic_unlocked
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

    # LLM: 时间相等仍按物理顺序，固定through续页不扩大来源范围。
    # 函数用途: 返回审计条目、未知或精确总数、耗时和coverage。
    def query_report(self, params=None, *, view=None, **kwargs):
        params = _normalize_query_params(params, **kwargs)
        if (type(params.limit) is not int or type(params.offset) is not int
                or min(params.limit, params.offset) < 0):
            raise ValueError('invalid pagination')
        started = time.perf_counter()
        view = self._capture_view() if view is None else view
        if self._read_index is None:
            return {'entries': [], 'total_count': None, 'query_time_ms': 0,
                    'coverage': self._capture_view(), 'view': view}
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
        while True:
            page = self._read_index.query(view, filters=filters, limit=min(remaining, 1000),
                                          offset=offset, descending=True)
            entries.extend(AuditEntry.from_dict(row) for row in page['rows'])
            count = len(page['rows'])
            remaining -= count
            offset += count
            if page['coverage']['status'] != 'complete' or remaining <= 0 or count == 0:
                return {'entries': entries, 'total_count': page['total_count'],
                        'query_time_ms': (time.perf_counter() - started) * 1000,
                        'coverage': page['coverage'], 'view': view}

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

    # LLM: 旧入口委托唯一plan/apply叶，默认永久保留；显式days仅本次有效，与logger追加共用一把锁。
    # 函数用途: 安全裁剪已证明过期的审计记录，不把未知或I/O错误算作删除。
    def cleanup_old_entries(self, days: int = 0) -> int:
        if days <= 0:
            return 0
        try:
            self._audit_file.stat(follow_symlinks=False)
        except FileNotFoundError:
            return 0
        with locked_json_path(self._audit_file):
            plan = _plan_audit_retention(self._audit_file, cutoff_time=time.time() - days * 86400)
            return _apply_audit_retention_unlocked(plan)


# LLM: 计划只在本次原append锁内消费，绑定准确文件与五元generation，不能作为授权或持久事实。
# 类用途: 保存审计保留字节及已证实的删除计数。
@dataclass(frozen=True)
class _AuditRetentionPlan:
    path: Path
    generation: tuple[int, int, int, int, int]
    kept_bytes: bytes
    deleted_count: int


# LLM: 只有可解码对象的有限数值时间戳才可删除，其余字节保持原样，bool不当数值。
# 函数用途: 判断一条物理LF记录是否已证明过期。
def _audit_row_expired(raw_line, cutoff_time):
    try:
        row = json.loads(raw_line.decode('utf-8'))
        timestamp = row.get('timestamp') if isinstance(row, dict) else None
        return type(timestamp) in (int, float) and math.isfinite(timestamp) and timestamp < cutoff_time
    except (UnicodeError, ValueError, OverflowError, RecursionError):
        return False


# LLM: 不吞I/O错误，不将UTF8坏行删除；同FD前后及当前路径generation必须相同，单次及单行有字节硬界。
# 函数用途: 以物理LF规划安全审计保留。
def _plan_audit_retention(path, *, cutoff_time):
    path = Path(os.path.abspath(path))
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OSError('安全审计保留仅接受普通文件')
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
        if generation != after or after != cache_stat_signature(path.stat(follow_symlinks=False)):
            raise BlockingIOError('安全审计文件在规划期间发生变化')
    finally:
        os.close(descriptor)
    return _AuditRetentionPlan(path, generation, b''.join(kept), deleted_count)


# LLM: 调用方必须持原locked_json_path，复核generation后复用原私有原子writer，不重复取锁。
# 函数用途: 应用已证明的安全审计保留计划，零删除不创建tmp。
def _apply_audit_retention_unlocked(plan):
    if plan is None or not plan.deleted_count:
        return 0
    if cache_stat_signature(plan.path.stat(follow_symlinks=False)) != plan.generation:
        raise BlockingIOError('安全审计文件在应用前发生变化')
    write_private_text_file_atomic_unlocked(plan.path, plan.kept_bytes.decode('utf-8', 'surrogateescape'),
                                          preserve_bytes=True)
    return plan.deleted_count


__all__ = ["AuditQuery", "AuditQueryResult"]
