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


# LLM: 私有投影按私有索引模式打开：data目录FD、nofollow单链接叶、连接后核叶身份及实际新开FD；附属叶拒绝链接，短连接始终关闭。
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
        # 私有投影与同Owner其它写者共用data/，只核本叶身份，不比较祖先ctime（兄弟文件增删会改变它）。
        parent = open_directory_beneath(root, ('data',), require_dir_fd=True)
        try:
            before = os.stat(filename, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ValueError('运行投影库必须是单链接普通文件')
            descriptors = _regular_fds()
            connection = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=5)
            try:
                after = os.stat(filename, dir_fd=parent, follow_symlinks=False)
                if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
                    raise ValueError('workflow run projection was replaced')
                # 连接窗口内路径可被换走再换回；只认SQLite实际新开的常规FD，不能以前后路径一致放行。
                opened = _regular_fds()
                if descriptors is not None and opened is not None:
                    fresh = {identity for fd, identity in opened.items() if descriptors.get(fd) != identity}
                    if fresh and (before.st_dev, before.st_ino) not in fresh:
                        raise PermissionError('DATASOURCE_SQLITE_PATH_REPLACED')
            except BaseException:
                connection.close()
                raise
        finally:
            os.close(parent)
        try:
            connection.row_factory = sqlite3.Row
            with connection:
                yield connection
        finally:
            connection.close()


# LLM: 只读进程FD表，不打开被列出的文件；没有FD目录的平台返回None，由调用方跳过该项复核。
# 函数用途: 取得当前进程常规文件FD到(dev, ino)的映射。
def _regular_fds():
    directory = '/proc/self/fd' if os.path.isdir('/proc/self/fd') else '/dev/fd'
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    result = {}
    for name in names:
        try:
            info = os.fstat(int(name))
        except (OSError, ValueError):
            continue
        if stat.S_ISREG(info.st_mode):
            result[int(name)] = (info.st_dev, info.st_ino)
    return result


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


# LLM: SQLite待发表行先于canonical提交，投影晚于提交；投影失败既不阻塞也不否认规范提交，只有commit()异常外抛。
# 函数用途: 包围一次 run.json 写入，并在规范提交后发布可重放的索引行。
def commit_run(owner_home, value, commit):
    run_id = value['workflow_run_id']
    safe_path(owner_home, run_id)
    index = RunListIndex(owner_home)
    marked = _projection_step(index.mark_pending, run_id)
    commit()
    published = _projection_step(index.publish_rows, [value]) and _projection_step(index.ack_pending, run_id)
    # 既无耐久待发表行也无确认发布时，投影可能停在旧状态；失效后下次连接从规范运行目录重建。
    if not marked and not published:
        _invalidate_projection(owner_home, 'workflow_run_index.sqlite3')


# LLM: fn为RunListIndex单参绑定方法；非忙碌的SQLite库错误按损坏处理并就地失效，忙碌只等下一次重放。
# 函数用途: 执行一个投影步骤并只回报成败，不让派生投影错误外抛。
def _projection_step(fn, argument):
    try:
        fn(argument)
    except sqlite3.DatabaseError as exc:
        if getattr(exc, 'sqlite_errorcode', 0) & 0xFF not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            _invalidate_projection(fn.__self__.owner_home, 'workflow_run_index.sqlite3')
        return False
    except (OSError, ValueError, sqlite3.Error):
        return False
    return True


# LLM: 与连接持同一投影锁，删除时没有协作连接打开该库；只删本库及SQLite附属叶，失败不影响已完成的规范提交。
# 函数用途: 丢弃一个派生投影，使下一次连接以user_version=0重新初始化并回填。
def _invalidate_projection(owner_home, filename):
    root = Path(owner_home).absolute()
    try:
        path = safe_path(root, 'data', filename)
        with locked_json_path(path):
            safe_path(root, 'data', filename)
            for suffix in ('', '-wal', '-shm', '-journal'):
                path.with_name(path.name + suffix).unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


class WorkflowArtifactLocatorIncomplete(ValueError):
    """派生定位尚未覆盖，不能据此否认仍可能存在的规范归档。"""

    error_code = 'WORKFLOW_ARTIFACT_RUN_LOCATOR_INCOMPLETE'


def _artifact_locator_schema(conn, version):
    if version < 3:
        conn.execute('CREATE TABLE IF NOT EXISTS artifact_run_keys('
                     'run_key TEXT NOT NULL, run_id TEXT NOT NULL, PRIMARY KEY(run_key,run_id))')
    if version < 4:
        conn.execute('CREATE TABLE IF NOT EXISTS artifact_key_scan('
                     'singleton INTEGER PRIMARY KEY CHECK(singleton=1), '
                     'after_run_id TEXT NOT NULL, complete INTEGER NOT NULL)')
        # 旧版本可能重建了空keymap却留下v4完成游标；它不再是本次迁移的覆盖证明。
        conn.execute("INSERT INTO artifact_key_scan VALUES(1,'',0) ON CONFLICT(singleton) "
                     "DO UPDATE SET after_run_id='',complete=0")
        conn.execute('PRAGMA user_version=4')


def _advance_artifact_keys(conn):
    """仅在已有runs主键上推进32行，游标与映射同事务；不枚举运行目录或扫描全表。"""
    from .run_artifacts import workflow_artifact_run_key

    cursor, complete = conn.execute('SELECT after_run_id,complete FROM artifact_key_scan WHERE singleton=1').fetchone()
    if complete:
        return
    rows = conn.execute('SELECT run_id FROM runs WHERE run_id>? ORDER BY run_id LIMIT 32', (cursor,)).fetchall()
    for row in rows:
        if len(row[0]) > 64:
            conn.execute('INSERT OR IGNORE INTO artifact_run_keys VALUES(?,?)',
                         (workflow_artifact_run_key(row[0]), row[0]))
    conn.execute('UPDATE artifact_key_scan SET after_run_id=?,complete=? WHERE singleton=1',
                 (rows[-1][0] if rows else cursor, int(len(rows) < 32)))


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
            _artifact_locator_schema(conn, version)
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
                run_id = row['workflow_run_id']
                if len(run_id) > 64:
                    from .run_artifacts import workflow_artifact_run_key
                    conn.execute('INSERT OR IGNORE INTO artifact_run_keys VALUES(?,?)',
                                 (workflow_artifact_run_key(run_id), run_id))
            _advance_artifact_keys(conn)

    def artifact_run_ids(self, run_key):
        """已有库有界自愈；未覆盖与缺索引返回unknown，由公开读取端映射可恢复503。"""
        path = safe_path(self.owner_home, 'data', 'workflow_run_index.sqlite3')
        if not path.is_file():
            raise WorkflowArtifactLocatorIncomplete('原运行定位索引缺失；等待普通运行列表维护重建，不能据此断言归档不存在')
        # 绝不调用会初始化旧目录的connection；GET只迁移已存在的私有runs索引。
        with index_connection(self.owner_home, path.name) as conn:
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            if version < 2:
                raise WorkflowArtifactLocatorIncomplete('原定位索引尚未初始化；等待普通维护重建')
            _artifact_locator_schema(conn, version)
            _advance_artifact_keys(conn)
            found = tuple(row[0] for row in conn.execute(
                'SELECT run_id FROM artifact_run_keys WHERE run_key=? LIMIT 2', (run_key,)))
            complete = conn.execute('SELECT complete FROM artifact_key_scan WHERE singleton=1').fetchone()[0]
            pending = conn.execute('SELECT 1 FROM pending LIMIT 1').fetchone()
            legacy = conn.execute('SELECT 1 FROM legacy LIMIT 1').fetchone()
        # 在连接事务提交之后报告未覆盖，避免抛异常把刚推进的游标也回滚。
        if not found and (not complete or pending is not None or legacy is not None):
            raise WorkflowArtifactLocatorIncomplete('原长运行定位仍在有界迁移；重读同一编号或等待普通维护推进')
        return found

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
