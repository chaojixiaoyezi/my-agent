"""运行库有界页回收：兼容零列 PRAGMA 游标，提交仍由原调用者负责。"""
from __future__ import annotations

import re
from contextlib import closing

from ..io.sqlite_scope import _cleanup_preserving_error


def _free_pages(connection, prefix):
    with closing(connection.cursor()) as probe:
        probe.row_factory = None
        return probe.execute(f"PRAGMA {prefix}freelist_count").fetchone()[0]


def execute_runtime_statement(connection, sql, parameters=(), *, cursor=None):
    """执行已通过代际栅栏的语句；只补足本次请求尚未回收的页，不隐式提交。"""
    statement = re.sub(r'\A(?:\s+|;+|--[^\n]*(?:\n|$)|/\*.*?\*/)*', '', sql, flags=re.S)
    match = re.fullmatch(r'PRAGMA\s+(?P<prefix>\w+\.)?incremental_vacuum\s*\(\s*(?P<pages>\d+)\s*\)\s*;?\s*',
                         statement, re.I)
    if match is None or (pages := int(match['pages'])) <= 0:
        return connection.execute(sql, parameters) if cursor is None else cursor.execute(sql, parameters)
    prefix = match['prefix'] or ''
    before = _free_pages(connection, prefix)
    owned = cursor is None
    cursor = connection.cursor() if owned else cursor
    try:
        cursor.execute(sql, parameters).fetchall()
        # Python 3.11 的零列结果可能只推进第一页；按同一事务内的实际页差补足，最多仍为原页预算。
        remaining = min(pages, max(0, _free_pages(connection, prefix) - max(0, before - pages)))
        for _ in range(remaining):
            cursor.execute(f"PRAGMA {prefix}incremental_vacuum(1)").fetchall()
        return cursor
    except BaseException as error:
        if owned:
            _cleanup_preserving_error(cursor.close, error)
        raise
