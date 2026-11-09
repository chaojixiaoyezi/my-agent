"""可丢弃的 owner 目录分页投影；目录事实仍是发现与权限的唯一来源。"""
from __future__ import annotations

import sqlite3
import threading
import time


class OwnerDirectoryProjection:
    """临时 SQLite 保存排序键，固定内存页缓存；冷建和重新核对都分片推进。"""

    def __init__(self):
        # 空文件名由 SQLite 创建独占临时库并在 close 时移除，不写 owner 目录。
        self.connection = sqlite3.connect('', check_same_thread=False)
        self.connection.execute('PRAGMA cache_size=-1024')
        self.connection.execute('CREATE TABLE homes (provider TEXT,kind TEXT,id TEXT,path TEXT,generation INTEGER,'
                                'PRIMARY KEY(provider,kind,id)) WITHOUT ROWID')
        self.connection.execute('CREATE INDEX homes_generation ON homes(generation)')
        self.lock = threading.RLock()
        self.iterator = None
        self.signature = None
        self.generation = 0
        self.ready = False
        self.next_scan = 0.0

    def close(self):
        with self.lock:
            if self.iterator is not None:
                self.iterator.close()
                self.iterator = None
            self.connection.close()

    def page(self, *, signature, stream, cursor, limit, scan_budget, rescan_seconds):
        """排序查询只读 LIMIT+1；bootstrap未完成返回续扫标记，不伪装为空目录。"""
        with self.lock:
            now = time.monotonic()
            if self.iterator is None and (not self.ready or signature != self.signature or now >= self.next_scan):
                self.generation += 1
                self.signature = signature
                self.iterator = stream()
            scanned = 0
            if self.iterator is not None:
                try:
                    with self.connection:
                        for _ in range(scan_budget):
                            try:
                                row = next(self.iterator)
                            except StopIteration:
                                self.iterator = None
                                self.ready = True
                                self.next_scan = now + rescan_seconds
                                break
                            scanned += 1
                            if row is not None:
                                self.connection.execute('INSERT INTO homes VALUES(?,?,?,?,?) '
                                    'ON CONFLICT(provider,kind,id) DO UPDATE SET path=excluded.path,generation=excluded.generation',
                                    (*row, self.generation))
                except BaseException:
                    if self.iterator is not None:
                        self.iterator.close()
                        self.iterator = None
                    self.signature = None
                    raise
            if not self.ready:
                return (), ('', '', ''), scanned
            # 删除只在本轮完整枚举后推进；每次最多scan_budget项，不在末页集中删整个历史。
            if self.iterator is None:
                with self.connection:
                    self.connection.execute('DELETE FROM homes WHERE (provider,kind,id) IN '
                        '(SELECT provider,kind,id FROM homes WHERE generation<? LIMIT ?)',
                        (self.generation, scan_budget))
            rows = self.connection.execute('SELECT provider,kind,id,path FROM homes WHERE (provider,kind,id)>(?,?,?) '
                'ORDER BY provider,kind,id LIMIT ?', (*(cursor or ('', '', '')), limit + 1)).fetchall()
            selected = rows[:limit]
            next_cursor = tuple(selected[-1][:3]) if len(rows) > limit else None
            return tuple(selected), next_cursor, scanned
