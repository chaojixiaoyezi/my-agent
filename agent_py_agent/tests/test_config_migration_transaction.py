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
