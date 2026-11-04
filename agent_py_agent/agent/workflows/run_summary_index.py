# LLM: SQLite事务同时持久化唯一运行凭据和聚合，不把运行历史写回资产定义。
# 模块用途: 保存每资产增量摘要，并在固定批次中接续旧幂等凭据迁移。
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

from ..assets import safe_asset_id
from ..assets.descriptor import RuntimeSummary
from .models import safe_path
from .run_list_index import index_connection


# LLM: 旧metadata仍是迁移输入而非新历史容器，损坏身份必须拒绝而非静默计数。
# 函数用途: 严格恢复旧fold的幂等凭据列表。
def legacy_ids(metadata):
    state = metadata.get('_run_summary_spool')
    if state is None:
        return []
    if (not isinstance(state, dict) or set(state) != {'schema_version', 'applied_run_ids'}
            or state['schema_version'] != 'run_summary_spool.v1'
            or not isinstance(state['applied_run_ids'], list)):
        raise ValueError('运行摘要幂等凭据损坏')
    return [safe_asset_id(value) for value in state['applied_run_ids']]


# LLM: 代次来自锁内新建的私有定义字段；旧定义的空代次不能与删除后同ID新定义混用。
# 函数用途: 返回摘要生命周期分区，不用执行内容hash替代创建身份。
def generation(metadata):
    value = metadata.get('_run_summary_generation', '')
    return '' if value == '' else safe_asset_id(value)


# LLM: 摘要以run_at/run_id稳定排序；最新成功和最新错误重置点足以增量维护原错误继承语义。
# 类用途: 在短连接中管理独立运行行与一个资产聚合行。
class RunSummaryIndex:
    # LLM: 构造不创建目录，来源及写权限由公开资产入口确定。
    # 函数用途: 绑定已授权Owner根。
    def __init__(self, owner_home):
        self.owner_home = Path(owner_home).absolute()

    # LLM: 复用列表索引的单链接数据库、附属叶检查与短连接寿命，不取资产根锁。
    # 函数用途: 初始化摘要专库并给调用者提供一个完整事务。
    @contextmanager
    def connection(self):
        with index_connection(self.owner_home, 'asset_run_summary.sqlite3') as conn:
            if conn.execute('PRAGMA user_version').fetchone()[0] == 0:
                conn.executescript('''
                    CREATE TABLE IF NOT EXISTS summary_runs(kind TEXT NOT NULL, asset_id TEXT NOT NULL, generation TEXT NOT NULL,
                        run_id TEXT NOT NULL, run_at TEXT, failed INTEGER,
                        source_leaf TEXT, payload TEXT, resets_error INTEGER,
                        PRIMARY KEY(kind, asset_id, generation, run_id));
                    CREATE INDEX IF NOT EXISTS summary_failures ON summary_runs(kind, asset_id, generation, failed, run_at, run_id);
                    CREATE INDEX IF NOT EXISTS summary_order ON summary_runs(kind, asset_id, generation, run_at, run_id);
                    CREATE INDEX IF NOT EXISTS summary_errors ON summary_runs(kind, asset_id, generation, resets_error, run_at, run_id);
                    CREATE TABLE IF NOT EXISTS summary_assets(kind TEXT NOT NULL, asset_id TEXT NOT NULL, generation TEXT NOT NULL,
                        state TEXT NOT NULL, PRIMARY KEY(kind, asset_id, generation));
                    PRAGMA user_version=1;
                ''')
            yield conn

    # LLM: 聚合已存在时不读取任何运行行，空Owner的公开读取不隐式创建数据库。
    # 函数用途: 直接恢复一个固定大小摘要，缺少迁移聚合时返回定义的旧基准。
    def project(self, kind, asset_id, summary, metadata=None):
        if not safe_path(self.owner_home, 'data', 'asset_run_summary.sqlite3').exists():
            return summary
        with self.connection() as conn:
            row = conn.execute('SELECT state FROM summary_assets WHERE kind=? AND asset_id=? AND generation=?',
                               (kind, asset_id, generation(metadata or {}))).fetchone()
            return RuntimeSummary.from_dict(json.loads(row[0])['summary']) if row else summary

    # LLM: 每条新运行只插入自己的凭据并改一个聚合；常规顺序追加不查询历史范围。
    # 函数用途: 合并有界观察批次与一页旧凭据，并返回摘要和凭据迁移完成标记。
    def ingest(self, kind, asset_id, summary, metadata, records, *, legacy_records=(), receipt_batch_size=128):
        if kind not in {'script', 'workflow'}:
            raise ValueError('运行摘要kind必须是script或workflow')
        safe_asset_id(asset_id)
        key = (kind, asset_id, generation(metadata))
        with self.connection() as conn:
            row = conn.execute('SELECT state FROM summary_assets WHERE kind=? AND asset_id=? AND generation=?',
                               key).fetchone()
            state = json.loads(row[0]) if row else {
                'summary': summary.to_dict(), 'last_key': None, 'success_key': None, 'error_key': None,
                'receipt_offset': 0, 'receipts_complete': False, 'base_summary': summary.to_dict(), 'new_failures': 0}
            if not state['receipts_complete']:
                previous = legacy_ids(metadata)
                offset = state['receipt_offset']
                end = min(len(previous), offset + receipt_batch_size)
                conn.executemany('INSERT OR IGNORE INTO summary_runs(kind,asset_id,generation,run_id) VALUES(?,?,?,?)',
                                 ((*key, value) for value in previous[offset:end]))
                state['receipt_offset'] = end
                state['receipts_complete'] = end == len(previous)
                # 尚未分页导入的旧身份也必须先作为已累计凭据，不能再次应用。
                old = set(previous) if not state['receipts_complete'] else set()
            else:
                old = set()
            for leaf, record in (*legacy_records, *(('', value) for value in records)):
                run_id = record['run_id']
                if run_id in old:
                    conn.execute('INSERT OR IGNORE INTO summary_runs(kind,asset_id,generation,run_id) VALUES(?,?,?,?)',
                                 (*key, run_id))
                    continue
                failed = record['status'] in {'failed', 'error', 'timeout', 'timed_out'}
                payload = json.dumps(record, ensure_ascii=False, allow_nan=False)
                inserted = conn.execute('INSERT OR IGNORE INTO summary_runs VALUES(?,?,?,?,?,?,?,?,?)',
                    (*key, run_id, record['run_at'], int(failed), leaf, payload,
                     int(not failed or bool(record['last_error'])))).rowcount
                if inserted:
                    self._apply(conn, key, state, record, failed)
                elif leaf:
                    previous = conn.execute('SELECT run_at,source_leaf,failed FROM summary_runs '
                        'WHERE kind=? AND asset_id=? AND generation=? AND run_id=?', (*key, run_id)).fetchone()
                    # 旧spool跨批次的重复叶仍按原(run_at,run_id,leaf)选择首次观察；已fold凭据不替换。
                    if previous['run_at'] is not None and (record['run_at'], leaf) < (previous['run_at'], previous['source_leaf']):
                        conn.execute('UPDATE summary_runs SET run_at=?,failed=?,source_leaf=?,payload=?,resets_error=? '
                            'WHERE kind=? AND asset_id=? AND generation=? AND run_id=?',
                            (record['run_at'], int(failed), leaf, payload,
                             int(not failed or bool(record['last_error'])), *key, run_id))
                        state['new_failures'] += int(failed) - previous['failed']
                        self._refresh_ordered(conn, key, state)
            conn.execute('INSERT INTO summary_assets VALUES(?,?,?,?) ON CONFLICT(kind,asset_id,generation) DO UPDATE SET state=excluded.state',
                         (*key, json.dumps(state, ensure_ascii=False, allow_nan=False)))
            return RuntimeSummary.from_dict(state['summary']), state['receipts_complete']

    # LLM: 只有晚到且位于最新成功之后的新成功需对失败尾段做索引计数，不反序列化或fold历史。
    # 函数用途: 用排序边界增量更新次数、最近观察、失败尾段和最后错误。
    @staticmethod
    def _apply(conn, asset_key, state, record, failed):
        summary = state['summary']
        key = [record['run_at'], record['run_id']]
        last_key, success_key, error_key = state['last_key'], state['success_key'], state['error_key']
        summary['run_count'] += 1
        state['new_failures'] += int(failed)
        if last_key is None or key > last_key:
            summary.update(last_run_at=record['run_at'], last_status=record['status'],
                           last_duration_ms=record['duration_ms'])
            state['last_key'] = key
        if failed:
            if success_key is None or key > success_key:
                summary['consecutive_failures'] += 1
        elif success_key is None or key > success_key:
            state['success_key'] = key
            summary['consecutive_failures'] = (0 if last_key is None or key > last_key else conn.execute(
                '''SELECT count(*) FROM summary_runs INDEXED BY summary_failures
                   WHERE kind=? AND asset_id=? AND generation=? AND failed=1 AND (run_at,run_id)>(?,?)''',
                (*asset_key, *key)).fetchone()[0])
        if (not failed or record['last_error']) and (error_key is None or key > error_key):
            summary['last_error'] = record['last_error']
            state['error_key'] = key

    # LLM: 仅迁移时发现更早重复叶才重取三个索引边界，不能回放已ACK历史。
    # 函数用途: 修正旧叶首次身份选择后恢复稳定排序摘要，不改变已累计次数。
    @staticmethod
    def _refresh_ordered(conn, asset_key, state):
        latest = conn.execute('SELECT run_at,run_id,payload FROM summary_runs INDEXED BY summary_order '
            'WHERE kind=? AND asset_id=? AND generation=? AND run_at IS NOT NULL ORDER BY run_at DESC,run_id DESC LIMIT 1',
            asset_key).fetchone()
        success = conn.execute('SELECT run_at,run_id FROM summary_runs INDEXED BY summary_failures '
            'WHERE kind=? AND asset_id=? AND generation=? AND failed=0 ORDER BY run_at DESC,run_id DESC LIMIT 1',
            asset_key).fetchone()
        error = conn.execute('SELECT run_at,run_id,payload FROM summary_runs INDEXED BY summary_errors '
            'WHERE kind=? AND asset_id=? AND generation=? AND resets_error=1 ORDER BY run_at DESC,run_id DESC LIMIT 1',
            asset_key).fetchone()
        value = json.loads(latest['payload'])
        state['last_key'] = [latest['run_at'], latest['run_id']]
        state['success_key'] = [success['run_at'], success['run_id']] if success else None
        state['error_key'] = [error['run_at'], error['run_id']] if error else None
        failures = (conn.execute('SELECT count(*) FROM summary_runs INDEXED BY summary_failures '
            'WHERE kind=? AND asset_id=? AND generation=? AND failed=1 AND (run_at,run_id)>(?,?)',
            (*asset_key, *state['success_key'])).fetchone()[0] if success else
            state['base_summary']['consecutive_failures'] + state['new_failures'])
        state['summary'].update(last_run_at=value['run_at'], last_status=value['status'],
            last_duration_ms=value['duration_ms'], consecutive_failures=failures,
            last_error=json.loads(error['payload'])['last_error'] if error else state['base_summary']['last_error'])
