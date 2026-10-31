# LLM: R1在真实原JSON/SQLite入口拒写高代际，保留未知字段，不借测试创建第二库权威或修改用户home。
# 模块用途: 验证存储版本栅栏、完整往返和错误前零产品写入。
from __future__ import annotations

import json
import sqlite3

import pytest

from agent_py_agent.agent.contracts.optimization_contracts import (
    StoreSchemaNewerError,
    decode_versioned_record,
    require_schema_generation,
)


# LLM: 字段嵌套引用不能泄露给调用方修改原输入；两个R1写者应保留未来字段。
# 函数用途: 验证解析后改已知字段仍完整带回扩展字段。
def test_storage_transaction_unknown_fields_roundtrip_without_aliasing():
    raw = {'schema_generation': 1, 'state': 'pending', 'future': {'pins': ['a']}}
    record = decode_versioned_record(raw, {'state', 'schema_generation'})
    assert record._extra_fields == {'future': {'pins': ['a']}}
    record.fields['state'] = 'done'
    record._extra_fields['future']['pins'].append('b')
    assert raw['future']['pins'] == ['a']
    result = record.to_record()
    assert result == {'schema_generation': 1, 'state': 'done', 'future': {'pins': ['a', 'b']}}


# LLM: 旧header缺失为代际0，显式非法值不是0；高代际有稳定错误码。
# 函数用途: 验证代际的类型、边界与关闭式失败。
@pytest.mark.parametrize('generation', [2, True, -1, '1'])
def test_storage_transaction_generation_cannot_silently_fallback(generation):
    with pytest.raises((StoreSchemaNewerError, ValueError)):
        require_schema_generation({'schema_generation': generation})


# LLM: updater不应在高代际上执行，错误前原文件逐字节不变。
# 函数用途: 验证Gateway原更新入口真正消费栅栏。
def test_storage_transaction_gateway_newer_generation_is_not_overwritten(tmp_path):
    from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic
    path = tmp_path / 'state.json'
    raw = json.dumps({'schema_generation': 2, 'future': ['protected']}).encode()
    path.write_bytes(raw)
    with pytest.raises(StoreSchemaNewerError) as error:
        update_json_file_atomic(path, lambda row: pytest.fail('newer schema updater must not run'))
    assert error.value.code == 'STORE_SCHEMA_NEWER'
    assert path.read_bytes() == raw


# LLM: 原runtime.db元数据是唯一header，任何可写连接和已有连接借用都要重新核代际。
# 函数用途: 验证RuntimeRepository不会初始化或更新更高代际库。
def test_storage_transaction_runtime_db_newer_generation_refuses_writer(tmp_path):
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
    path = tmp_path / 'runtime.db'
    RuntimeRepository(path)
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('schema_generation','2')")
    with pytest.raises(StoreSchemaNewerError):
        RuntimeRepository(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT value FROM metadata WHERE key='schema_generation'").fetchone()[0] == '2'


# LLM: RD-01明确退役无生产调用的旧轮转实现，锁定物理移除，不能仅留空壳或兼容别名。
# 函数用途: 防止废弃的日志轮转模块被重新带回安装包。
def test_storage_transaction_retired_rotating_log_is_removed():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    assert not (root / 'agent/common/rotating_log.py').exists()


# LLM: 不仅update，原两种直接写入口也不能覆盖高代际；保留原锁和调用边界。
# 函数用途: 防止完整替换绕过R1版本栅栏。
@pytest.mark.parametrize('writer_name', ['write_json_file', 'write_json_file_atomic'])
def test_storage_transaction_direct_gateway_writer_cannot_replace_newer(tmp_path, writer_name):
    from agent_py_agent.agent.gateway_parts import io
    path = tmp_path / 'direct.json'
    original = b'{"schema_generation":2,"future":["keep"]}'
    path.write_bytes(original)
    with pytest.raises(StoreSchemaNewerError):
        getattr(io, writer_name)(path, {'state': 'new'})
    assert path.read_bytes() == original


# LLM: RD-01退役专用测试的私有写性质迁到真实Gateway历史写入口，不再保留备用轮转实现。
# 函数用途: 验证真实append/同ID替换的0600、0700、内容与幂等性。
def test_storage_transaction_gateway_history_private_append_and_replace(tmp_path):
    import os
    import stat
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts.io import append_gateway_history_once

    root = tmp_path / 'gateway'
    paths = SimpleNamespace(root=root, history=root / 'gateway_requests.jsonl')
    before_umask = os.umask(0o022)
    try:
        assert append_gateway_history_once(paths, {'id': 'privacy-1', 'status': 'done'}) is True
        assert append_gateway_history_once(paths, {'id': 'privacy-1', 'status': 'done'}) is False
        assert append_gateway_history_once(paths, {'id': 'privacy-1', 'status': 'complete'}) is True
    finally:
        os.umask(before_umask)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.history.stat().st_mode) == 0o600
    assert [json.loads(row) for row in paths.history.read_text().splitlines()] == [{'id': 'privacy-1', 'status': 'complete'}]


# LLM: 入口缺失以明确断言表示功能缺口，不把ImportError当作有效红测。
# 函数用途: 找到白名单内唯一的配置事务迁移函数。
def _configuration_migrator():
    import importlib
    import importlib.util
    name = 'agent_py_agent.agent.settings.config_migration'
    assert importlib.util.find_spec(name) is not None, 'transactional configuration migration is not implemented'
    operation = getattr(importlib.import_module(name), 'migrate_configuration', None)
    assert callable(operation), 'canonical migration function is missing'
    return operation


# LLM: 真实文件备份必须保留完整前像字节；显式已知字段可删除，未来扩展不能丢。
# 函数用途: 验证事务备份、迁移、验证、代际发布及未知字段保真。
def test_configuration_migration_preserves_unknown_and_exact_private_backup(tmp_path):
    import stat
    path = tmp_path / 'settings.json'
    before = b'{"old": 7, "future":{"pins":["keep"]}}\n'
    path.write_bytes(before)
    result = _configuration_migrator()(path, lambda row: {'new': row['old'] + 1},
        lambda row: row['new'] == 8, known_fields={'schema_generation', 'old'})
    assert result['applied'] is True
    assert json.loads(path.read_bytes()) == {'schema_generation': 1, 'new': 8, 'future': {'pins': ['keep']}}
    backup = tmp_path / result['backup_name']
    assert backup.read_bytes() == before
    assert stat.S_IMODE(backup.stat().st_mode) == stat.S_IMODE(path.stat().st_mode) == 0o600


# LLM: 验证失败或transform异常不能发布部分配置，备份保留供审计，不碰其他文件。
# 函数用途: 验证交换前两种失败均不改变权威字节。
@pytest.mark.parametrize('stage', ['transform', 'validate'])
def test_configuration_migration_pre_swap_failure_keeps_authority(tmp_path, stage):
    path = tmp_path / 'settings.json'
    before = b'{"schema_generation":0,"value":4}'
    path.write_bytes(before)

    # LLM: 只在指定步骤抛错以定位事务边界，不mock文件操作。
    # 函数用途: 注入一个真实迁移回调故障。
    def transform(row):
        if stage == 'transform':
            raise ValueError('transform rejected')
        return {'value': 5}

    with pytest.raises(ValueError):
        _configuration_migrator()(path, transform, lambda row: False)
    assert path.read_bytes() == before


# LLM: 高代际先拒绝，不能先备份/调用transform再发现旧进程不支持。
# 函数用途: 验证迁移写者同样严格消费R1版本栅栏。
def test_configuration_migration_newer_refuses_before_backup(tmp_path):
    path = tmp_path / 'settings.json'
    path.write_text('{"schema_generation":2,"future":1}')
    with pytest.raises(StoreSchemaNewerError):
        _configuration_migrator()(path, lambda row: pytest.fail('newer transform'), lambda row: True)
    assert set(tmp_path.glob('*.bak')) == set()


# LLM: 模拟底层已经replace但fsync回执失败；必须复核后恢复准确前像，不声称未发生交换。
# 函数用途: 验证交换后失败回滚而不是只验证交换前原文件未变。
def test_configuration_migration_restores_after_swap_ack_failure(tmp_path, monkeypatch):
    migrate = _configuration_migrator()
    from agent_py_agent.agent.settings import config_migration as module
    path = tmp_path / 'settings.json'
    before = b'{"value":4}'
    path.write_bytes(before)
    writer = module.write_bytes_atomic_beneath
    calls = []

    # LLM: 第一次是备份，第二次是权威交换；故障发生在真实写入完成之后。
    # 函数用途: 注入提交确认丢失并允许第三次真实回滚。
    def fail_after_swap(root, parts, data, **options):
        result = writer(root, parts, data, **options)
        calls.append(tuple(parts))
        if tuple(parts) == (path.name,) and len(calls) == 2:
            raise OSError('swap ack lost')
        return result

    monkeypatch.setattr(module, 'write_bytes_atomic_beneath', fail_after_swap)
    with pytest.raises(OSError, match='swap ack lost'):
        migrate(path, lambda row: {'value': 5}, lambda row: True)
    assert path.read_bytes() == before
    assert len(calls) == 3


# LLM: 不跟随配置叶链接，否则迁移可能在锁定别名后覆盖另一owner的权威。
# 函数用途: 验证外部目标未被迁移或建立备份。
def test_configuration_migration_rejects_symlink_source(tmp_path):
    actual = tmp_path / 'actual.json'
    actual.write_text('{"value":4}')
    alias = tmp_path / 'alias.json'
    alias.symlink_to(actual)
    with pytest.raises((OSError, ValueError)):
        _configuration_migrator()(alias, lambda row: {'value': 5}, lambda row: True)
    assert actual.read_text() == '{"value":4}'


# LLM: 调用方未声明已知字段时不能推断transform遗漏的键已经废弃，默认保留全部未知扩展。
# 函数用途: 验证默认迁移不会丢未来嵌套字段。
def test_configuration_migration_default_preserves_omitted_extensions(tmp_path):
    path = tmp_path / 'settings.json'
    path.write_text('{"value":4,"future":{"keep":[1]}}')
    _configuration_migrator()(path, lambda row: {'value': 5}, lambda row: True)
    assert json.loads(path.read_text())['future'] == {'keep': [1]}


# LLM: 已达目标代际不重复运行非幂等transform，也不生成多余备份。
# 函数用途: 验证重复迁移是无业务副作用的空操作。
def test_configuration_migration_current_generation_is_noop(tmp_path):
    path = tmp_path / 'settings.json'
    before = b'{"schema_generation":1,"value":4}'
    path.write_bytes(before)
    result = _configuration_migrator()(path, lambda row: pytest.fail('already migrated'), lambda row: True)
    assert result['applied'] is False and path.read_bytes() == before
    assert not list(tmp_path.glob('*.bak'))


# LLM: 在真实借连接之后插入第二连接升级，不mock SQL结果；必须确认高代际内没有旧写行。
# 函数用途: 固定F1并发窗口并验证原create_task/显式transaction关闭式拒写。
@pytest.mark.parametrize('entrance', ['create_task', 'transaction'])
def test_runtime_generation_rechecked_after_borrow_before_first_write(tmp_path, monkeypatch, entrance):
    from contextlib import contextmanager

    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
    path = tmp_path / 'runtime.db'
    repo = RuntimeRepository(path)
    connection_scope = repo._runtime_connection if entrance == 'create_task' else repo._runtime_connect

    # LLM: 独立物理连接提交真实header升级，发生在旧连接的原先检查之后。
    # 函数用途: 安排与旧写事务的确定性交错。
    def upgrade():
        with sqlite3.connect(path) as other:
            other.execute("INSERT OR REPLACE INTO metadata VALUES('schema_generation','2')")

    if entrance == 'create_task':
        # LLM: 包装真实借用，仅控制时序，不改变连接或读写返回。
        # 函数用途: 借用成功后、首条业务SQL之前升级代际。
        @contextmanager
        def borrow():
            with connection_scope() as conn:
                upgrade()
                yield conn
        monkeypatch.setattr(repo, '_runtime_connection', borrow)
    else:
        # LLM: 独立事务连接仍是产品工厂；返回业务前升级代际。
        # 函数用途: 安排独立事务入口同一竞态。
        def connect():
            conn = connection_scope()
            upgrade()
            return conn
        monkeypatch.setattr(repo, '_runtime_connect', connect)
    with pytest.raises(StoreSchemaNewerError):
        if entrance == 'create_task':
            repo.create_task(owner_id='alice', task_id='old-writer')
        else:
            with repo.transaction() as conn:
                conn.execute("INSERT INTO tasks(task_id,owner_id,created_at,updated_at) VALUES('old-writer','alice',1,1)")
    with sqlite3.connect(path) as other:
        assert other.execute("SELECT task_id FROM tasks WHERE task_id='old-writer'").fetchall() == []


# LLM: 子进程只操作测试FIFO；外层timeout是反阻塞断言，失败时communicate清理自有进程。
# 函数用途: 证明F3真实直接写入口不会被无写端FIFO挂死。
def test_gateway_direct_json_writer_fifo_is_nonblocking(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    fifo = tmp_path / 'state.json'
    os.mkfifo(fifo)
    code = '''
import sys
from pathlib import Path
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
try:
    write_json_file_atomic(Path(sys.argv[1]), {'value': 1})
except (OSError, ValueError):
    pass
print('FINISHED', flush=True)
'''
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, 'PYTHONPATH': str(root) + os.pathsep + str(root / 'agent_py_agent')}
    result = subprocess.run([sys.executable, '-B', '-c', code, str(fifo)], env=env, cwd=root,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0 and result.stdout.strip() == 'FINISHED'


# LLM: SQLite接受前导空语句；连接与手工游标不能用空token绕过同事务代际核验。
# 函数用途: 核实独立复核给出的分号反例。
@pytest.mark.parametrize('api', ['execute', 'cursor', 'executemany'])
def test_runtime_empty_statement_prefix_cannot_bypass_generation(tmp_path, api):
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
    path = tmp_path / 'runtime.db'
    repo = RuntimeRepository(path)
    with repo._runtime_connection() as conn:
        with sqlite3.connect(path) as other:
            other.execute("INSERT OR REPLACE INTO metadata VALUES('schema_generation','2')")
        sql = "; /* leading empty statement */ UPDATE metadata SET value='1' WHERE key='schema_generation'"
        with pytest.raises(StoreSchemaNewerError):
            if api == 'cursor':
                conn.cursor().execute(sql)
            elif api == 'executemany':
                conn.executemany(sql, [()])
            else:
                conn.execute(sql)
        assert not conn.in_transaction
    with sqlite3.connect(path) as other:
        assert other.execute("SELECT value FROM metadata WHERE key='schema_generation'").fetchone()[0] == '2'


# LLM: 无法读取的前像不等于坏JSON；注入只影响目标open，真实锁与原子替换仍运行。
# 函数用途: 验证EACCES不能使未来header被空对象替代后覆盖。
def test_gateway_unreadable_header_refuses_replacement(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import io
    path = tmp_path / 'state.json'
    before = b'{"schema_generation":2,"future":"keep"}'
    path.write_bytes(before)
    original = io.os.open

    # LLM: 精确模拟目标FD打开拒绝，不阻止其他锁/临时文件操作，避免外层失败掩盖真实覆盖。
    # 函数用途: 在指定目标的读口返回权限错误。
    def denied(target, flags, *args, **kwargs):
        if str(target) == str(path) and flags & io.os.O_ACCMODE == io.os.O_RDONLY:
            raise PermissionError('unreadable header')
        return original(target, flags, *args, **kwargs)

    monkeypatch.setattr(io.os, 'open', denied)
    with pytest.raises(PermissionError):
        io.write_json_file_atomic(path, {'new': 1})
    assert path.read_bytes() == before


# LLM: FD稳定不能证明路径仍指向该FD；精确安排读中换inode，原子替换前应拒绝未知前像。
# 函数用途: 保留F3文件替换交错反例。
def test_gateway_header_read_detects_path_replacement(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import io
    path = tmp_path / 'state.json'
    path.write_text('{"old":1}')
    replacement = tmp_path / 'new.json'
    future = b'{"schema_generation":2,"future":"keep"}'
    replacement.write_bytes(future)
    original = io.os.fstat
    replaced = False

    # LLM: 只在目标数据FD第一次fstat之后替换路径，不改变FD上的读取结果。
    # 函数用途: 模拟未遵守原锁协议的外部替换者。
    def swap(fd):
        nonlocal replaced
        info = original(fd)
        if not replaced and info.st_ino == path.stat().st_ino:
            replaced = True
            replacement.replace(path)
        return info

    monkeypatch.setattr(io.os, 'fstat', swap)
    with pytest.raises(ValueError, match='changed'):
        io.write_json_file_atomic(path, {'new': 1})
    assert replaced and path.read_bytes() == future


# LLM: 按 CPython 3.12 Windows 语义构造同一文件的 FD/路径 stat 视图：fstat 的 st_ctime 为 ChangeTime，lstat 为 CreationTime；
#   只替换 io 模块看到的平台名，不改真实文件。POSIX 下两视图的 st_ctime 不同仍必须判为变化。
# 函数用途: 验证 Windows 上 FD/路径版本一致，而 mtime 或 inode 变化仍被识别。
def test_gateway_json_version_uses_same_source_change_stamp_on_windows(monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts import io
    same = {'st_dev': 7, 'st_ino': 11, 'st_size': 3, 'st_mtime_ns': 500, 'st_birthtime_ns': 100}
    fd_view = SimpleNamespace(**same, st_ctime_ns=500)
    path_view = SimpleNamespace(**same, st_ctime_ns=100)
    rewritten = SimpleNamespace(**{**same, 'st_mtime_ns': 501}, st_ctime_ns=100)
    replaced = SimpleNamespace(**{**same, 'st_ino': 12}, st_ctime_ns=100)
    monkeypatch.setattr(io, 'os', SimpleNamespace(name='nt'))
    assert io._json_file_version(fd_view) == io._json_file_version(path_view)
    assert io._json_file_version(fd_view) != io._json_file_version(rewritten)
    assert io._json_file_version(fd_view) != io._json_file_version(replaced)
    monkeypatch.setattr(io, 'os', SimpleNamespace(name='posix'))
    assert io._json_file_version(fd_view) != io._json_file_version(path_view)
