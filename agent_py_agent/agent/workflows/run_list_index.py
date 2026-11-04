"""运行列表只读投影：canonical JSON 决定事实，SQLite 只决定候选页。"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
from contextlib import contextmanager
from itertools import islice
from pathlib import Path

from ..assets import _verify_directory_beneath
from ..assets.datasource_store import DatasourceConnectionSpec, open_sqlite
from ..common.json_io import locked_json_path
from ..common.nofollow_fs import (
    _supports_dir_fd,
    ensure_private_dir,
    list_names_beneath,
    open_directory_beneath,
    read_text_beneath,
    write_text_atomic_beneath,
)
from .models import safe_path


# LLM: 复用仓库 SQLite 持有 FD/完整父链复核；附属叶拒绝链接，短连接始终关闭，不形成常驻缓存。
# 函数用途: 为一个 Owner 的私有投影库提供事务和连接寿命边界。
@contextmanager
def index_connection(owner_home, filename):
    root = Path(owner_home).absolute()
    path = safe_path(root, 'data', filename)
    ensure_private_dir(path.parent, canonical_root=root)
    with locked_json_path(path):
        safe_path(root, 'data', filename)
        if not path.exists():
            write_text_atomic_beneath(root, ('data', filename), '')
        for suffix in ('', '-wal', '-shm', '-journal'):
            target = path.with_name(path.name + suffix)
            try:
                info = target.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('运行投影库必须是单链接普通文件')
        connection = open_sqlite(DatasourceConnectionSpec(
            'workflow-run-projection', 'sqlite', '运行投影', database_path=str(path)))
        try:
            connection.row_factory = sqlite3.Row
            with connection:
                yield connection
        finally:
            connection.close()


# LLM: 批次只计正式未ACK叶；旧迁移可越过未发表临时名称，但不读或删除它们，所有FD均关闭。
# 函数用途: 取得至多一批正式旧输入名称，不让崩溃临时叶饿死迁移。
def pending_names(owner_home, parts, batch_size):
    if not _supports_dir_fd():
        try:
            parent = _verify_directory_beneath(Path(owner_home), parts)
            with os.scandir(parent) as entries:
                names = (entry.name for entry in entries
                         if not (entry.name.startswith('.') and '.tmp-' in entry.name))
                return list(islice(names, batch_size))
        except FileNotFoundError:
            return []
    try:
        parent = open_directory_beneath(owner_home, parts)
    except FileNotFoundError:
        return []
    try:
        with os.scandir(parent) as entries:
            names = (entry.name for entry in entries
                     if not (entry.name.startswith('.') and '.tmp-' in entry.name))
            return list(islice(names, batch_size))
    finally:
        os.close(parent)


# LLM: SQLite待发表行先于canonical提交，投影晚于提交；ACK失败只导致同代重放，不消费临时叶。
# 函数用途: 包围一次 run.json 写入，并在规范提交后发布可重放的索引行。
def commit_run(owner_home, value, commit):
    run_id = value['workflow_run_id']
    safe_path(owner_home, run_id)
    index = RunListIndex(owner_home)
    index.mark_pending(run_id)
    commit()
    index.publish_rows([value])
    index.ack_pending(run_id)


# LLM: 四种过滤各用覆盖索引，负时间与 run_id 构成稳定 keyset；不持 SQLite 锁再等待运行锁。
# 类用途: 维护运行候选页、未迁移目录游标与 canonical 提交的恢复入口。
class RunListIndex:
    # LLM: 构造不触盘，不扫描；实际初始化在短连接的第一次事务内完成。
    # 函数用途: 绑定一个可信 Owner 根。
    def __init__(self, owner_home):
        self.owner_home = Path(owner_home).absolute()

    # LLM: 旧目录只在初次初始化登记名称，逐批读正文；游标队列与投影同库提交以支持重启。
    # 函数用途: 打开并初始化当前 Owner 的列表投影。
    @contextmanager
    def connection(self):
        with index_connection(self.owner_home, 'workflow_run_index.sqlite3') as conn:
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            if version == 0:
                conn.executescript('''
                    CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL,
                        status TEXT NOT NULL, sort_time REAL NOT NULL, revision INTEGER NOT NULL);
                    CREATE INDEX IF NOT EXISTS runs_time ON runs(sort_time, run_id);
                    CREATE INDEX IF NOT EXISTS runs_workflow_time ON runs(workflow_id, sort_time, run_id);
                    CREATE INDEX IF NOT EXISTS runs_status_time ON runs(status, sort_time, run_id);
                    CREATE INDEX IF NOT EXISTS runs_workflow_status_time ON runs(workflow_id, status, sort_time, run_id);
                    CREATE TABLE IF NOT EXISTS legacy(run_id TEXT PRIMARY KEY);
                ''')
                names = list_names_beneath(self.owner_home, ('data', 'workflow_runs'))
                conn.executemany('INSERT OR IGNORE INTO legacy VALUES(?)', ((name,) for name in names))
            if version < 2:
                conn.execute('CREATE TABLE IF NOT EXISTS pending(run_id TEXT PRIMARY KEY)')
                conn.execute('PRAGMA user_version=2')
            yield conn

    # LLM: 待发表队列用主键索引，不把原子写临时文件当作未确认规范提交。
    # 函数用途: 在规范写前持久登记本运行，失败时不得继续提交正文。
    def mark_pending(self, run_id):
        with self.connection() as conn:
            conn.execute('INSERT OR IGNORE INTO pending VALUES(?)', (run_id,))

    # LLM: 调用方持相应run锁，发布当前规范值后才删除待发表行。
    # 函数用途: 幂等确认一个已发表的运行候选。
    def ack_pending(self, run_id):
        with self.connection() as conn:
            conn.execute('DELETE FROM pending WHERE run_id=?', (run_id,))

    # LLM: SQL在主键队列上应用LIMIT，不枚举临时叶或已完成历史。
    # 函数用途: 取得一批准确未确认的运行身份。
    def pending_ids(self, limit=32):
        with self.connection() as conn:
            return [row[0] for row in conn.execute('SELECT run_id FROM pending ORDER BY run_id LIMIT ?', (limit,))]

    # LLM: 业务 revision 决定新旧；同 revision 的私有占位更新不改变筛选键；不读步骤。
    # 函数用途: 在一个短事务内发布已核验的规范行并推进相应回填游标。
    def publish_rows(self, rows):
        with self.connection() as conn:
            for row in rows:
                conn.execute('''INSERT INTO runs VALUES(?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET
                    workflow_id=excluded.workflow_id, status=excluded.status,
                    sort_time=excluded.sort_time, revision=excluded.revision
                    WHERE excluded.revision >= runs.revision''',
                    (row['workflow_run_id'], row['workflow_id'], row['status'], -row['started_at'], row.get('revision', 0)))
                conn.execute('DELETE FROM legacy WHERE run_id=?', (row['workflow_run_id'],))

    # LLM: SQL 选择有界窗口后关闭连接，运行锁与新发布可按原 run→index 顺序执行。
    # 函数用途: 读取尚未迁移的名称游标，limit=None 仅供迁移期精确合并余集。
    def legacy_ids(self, limit=None):
        with self.connection() as conn:
            query = 'SELECT run_id FROM legacy ORDER BY run_id'
            return [r[0] for r in conn.execute(query + (' LIMIT ?' if limit is not None else ''),
                                              (limit,) if limit is not None else ())]

    # LLM: 每次最多修复固定批次，不在单次请求中修复全量旧正文；缺失 run 仅移除迁移项。
    # 函数用途: 让公开列表入口持续、可重启地推进旧历史回填。
    def backfill(self, store, batch_size=32):
        names = self.legacy_ids(batch_size)
        for name in names:
            with store._lock(name):
                raw = read_text_beneath(self.owner_home, ('data', 'workflow_runs', name, 'run.json'))
                if raw is None:
                    with self.connection() as conn:
                        conn.execute('DELETE FROM legacy WHERE run_id=?', (name,))
                    continue
                data = json.loads(raw)
                if data['workflow_run_id'] != name:
                    raise ValueError('运行身份与目录不匹配')
                self.publish_rows([data])

    # LLM: marker 只定位原运行，不授权旧载荷覆盖新版本；持运行锁读回后幂等发布并 ACK。
    # 函数用途: 恢复 canonical 已提交但尚未确认的索引发布。
    def replay(self, store, batch_size=32):
        for run_id in self.pending_ids(batch_size):
            with store._lock(run_id):
                current = read_text_beneath(self.owner_home, ('data', 'workflow_runs', run_id, 'run.json'))
                if current is not None:
                    data = json.loads(current)
                    if data['workflow_run_id'] != run_id:
                        raise ValueError('运行身份与目录不匹配')
                    self.publish_rows([data])
                self.ack_pending(run_id)
        # 大于一批的错误积压不能伪装成完整列表；下一请求继续有界推进同一 inbox。
        if self.pending_ids(1):
            raise RuntimeError('WORKFLOW_RUN_INDEX_RECOVERY_PENDING')

    # LLM: 条件只由明确字段组成，动态 SQL 仅拼固定片段；同时间按目录名称升序匹配锚点稳定排序。
    # 函数用途: 为四类过滤构造索引范围查询和后继 keyset。
    def query(self, workflow_id=None, status=None, *, after=None, limit=51):
        predicates, args = [], []
        for column, value in (('workflow_id', workflow_id), ('status', status)):
            if value is not None:
                predicates.append(column + '=?')
                args.append(value)
        if after is not None:
            predicates.append('(sort_time,run_id) > (?,?)')
            args.extend(after)
        index = ('runs_workflow_status_time' if workflow_id is not None and status is not None else
                 'runs_workflow_time' if workflow_id is not None else
                 'runs_status_time' if status is not None else 'runs_time')
        sql = 'SELECT run_id,sort_time FROM runs INDEXED BY ' + index
        if predicates:
            sql += ' WHERE ' + ' AND '.join(predicates)
        return sql + ' ORDER BY sort_time,run_id LIMIT ?', (*args, limit)

    # LLM: 只返回候选标识，消费者必须读 canonical 验证；多取一行用于 keyset 续页。
    # 函数用途: 取得一页有界运行候选。
    def page(self, workflow_id=None, status=None, *, after=None, limit=51):
        sql, args = self.query(workflow_id, status, after=after, limit=limit)
        with self.connection() as conn:
            return [(row['run_id'], row['sort_time']) for row in conn.execute(sql, args)]
