"""验证内容寻址历史、发布版本与兼容链，不执行历史脚本。"""
from __future__ import annotations

import ast
import hashlib
import inspect
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

from agent_py_agent.agent.assets import revision_history as history
from agent_py_agent.agent.common import nofollow_fs


# LLM: 所有读写均在tmp_path，路径约定来自跨分片合同。
# 函数用途: 返回测试历史根。
def _root(home):
    return home / 'data' / 'asset_evolution'


# LLM: 描述符只提供执行所需定义，不读取真实用户资产。
# 函数用途: 为测试构造独立脚本定义。
def _payload():
    return {'id': 'demo', 'kind': 'script', 'entrypoint': 'main.py',
            'metadata': {'source_system': 'local'}, 'io_contract': {'outputs': ['result']}}


# LLM: 字节身份与原store读出的UTF-8代码及现有pin计算式相同。
# 函数用途: 验证原始blob身份、幂等和二进制无损。
def test_blob_raw_identity_and_store_pin(tmp_path):
    from agent_py_agent.agent.assets.descriptor import AssetDescriptor
    from agent_py_agent.agent.assets.script_store import ScriptStore
    code = 'print("result")\r\n'
    row = ScriptStore(tmp_path).save_script(AssetDescriptor('demo', 'script', 'Demo'), code)
    expected = hashlib.sha256(row['code'].encode('utf-8')).hexdigest()
    actual = history.blob_put(tmp_path, code.encode('utf-8'))
    assert actual == expected
    assert history.blob_put(tmp_path, code.encode('utf-8'), max_total_bytes=0) == actual
    assert history.blob_get(tmp_path, actual) == code.encode('utf-8')
    assert history.blob_exists(tmp_path, actual) is True
    assert history.blob_get(tmp_path, '0' * 64) is None
    assert history.blob_exists(tmp_path, '0' * 64) is False
    raw = b'\x00\xff\x80\r\n'
    sha = history.blob_put(tmp_path, raw)
    assert sha == hashlib.sha256(raw).hexdigest()
    assert (_root(tmp_path) / 'blobs' / sha[:2] / sha).read_bytes() == raw


# LLM: 指纹忽略行尾和末尾空行，不忽略缩进或内部空行。
# 函数用途: 验证精确代码规范化合同。
def test_code_fingerprint_normalization():
    assert history.normalize_code_text('if True:\r\n  x = 1 \t\r\n\r\n') == 'if True:\n  x = 1'
    first = history.code_fingerprint(b'if True:\n  x = 1\n')
    assert first is not None
    assert first == history.code_fingerprint('if True:\r\n  x = 1 \t\r\n\r\n')
    assert first != history.code_fingerprint('if True:\n    x = 1\n')
    assert first != history.code_fingerprint('if True:\n\n  x = 1\n')


# LLM: 执行外统计和宿主metadata不参与指纹，来源系统和用户参数参与。
# 函数用途: 验证载荷的规范JSON、易变字段剥离和不可变输入。
def test_payload_and_asset_fingerprint():
    payload = _payload()
    before = json.dumps(payload, sort_keys=True)
    expected = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                         separators=(',', ':')).encode()).hexdigest()
    fingerprint = history.payload_fingerprint(payload)
    assert fingerprint == 'sha256:' + expected
    assert history.payload_fingerprint(payload | {'revision': 9, 'scope_layer': 'workspace',
        'source_scope': 'workspace', 'workspace_root': '/not-used', 'runtime_summary': {'run_count': 5},
        'created_at': 'later', 'updated_at': 'later', 'last_run_at': 'later', 'run_count': 5}) == fingerprint
    other = payload | {'metadata': payload['metadata'] | {'used_by': ['ignored'], 'version': 4}}
    assert history.payload_fingerprint(other) == fingerprint
    assert history.payload_fingerprint(payload | {'metadata': {'source_system': 'changed'}}) != fingerprint
    code_fp = history.code_fingerprint(b'print(1)\n')
    side_fp = history.code_fingerprint('note\n')
    raw = f'code={code_fp}\npayload={fingerprint}\nside=README.md:{side_fp}'
    expected_asset = 'sha256:' + hashlib.sha256(raw.encode()).hexdigest()
    assert history.asset_fingerprint(payload=payload, code=b'print(1)\n', side_files={'README.md': 'note\n'}) == expected_asset
    assert history.asset_fingerprint(payload=payload, side_files={}) == history.asset_fingerprint(payload=payload, side_files={'removed.md': None})
    assert json.dumps(payload, sort_keys=True) == before


# LLM: 只规范化去重，真实缩进与内容更改必须升版；旧记录字节不变。
# 函数用途: 验证两个计数分离和不可变追加。
def test_versions_deduplicate_and_append(tmp_path):
    first = history.record_version(tmp_path, 'script', 'demo', payload=_payload(), code=b'print(1)\n', storage_revision=7)
    assert isinstance(first, dict)
    assert first['created'] is True and first['version'] == 1
    first_path = _root(tmp_path) / 'versions' / 'script' / 'demo' / 'v1.json'
    frozen = first_path.read_bytes()
    unchanged = history.record_version(tmp_path, 'script', 'demo', payload=_payload() | {'revision': 99}, code=b'print(1) \t\r\n\r\n')
    assert unchanged == {'created': False, 'version': 1, 'record': first['record']}
    second = history.record_version(tmp_path, 'script', 'demo', payload=_payload(), code=b'print(2)\n', expected_latest=1)
    third = history.record_version(tmp_path, 'script', 'demo', payload=_payload(), code=b'  print(2)\n')
    assert second['version'] == 2 and third['version'] == 3
    assert first_path.read_bytes() == frozen
    assert history.latest_version(tmp_path, 'script', 'demo') == third['record']
    assert history.get_version(tmp_path, 'script', 'demo', 1) == first['record']
    assert history.get_version(tmp_path, 'script', 'demo', 99) is None
    assert history.latest_version(tmp_path, 'workflow', 'missing') is None


# LLM: 元信息按原样冻结；多条同sha时返回最新匹配，多个过滤条件同时成立才匹配。
# 函数用途: 验证记录字段、pins和历史查询顺序。
def test_record_fields_and_find_newest(tmp_path):
    pins = {'scripts': {'child': {'version': 1, 'code_sha256': 'a' * 64, 'execution_definition_ref': None}},
            'skills': {'builtin:method': {'content_sha256': 'b' * 64}}, 'workflows': {}}
    first = history.record_version(tmp_path, 'workflow', 'demo', payload={'steps': []}, code=b'pass\n',
        side_files={'WORKFLOW.md': 'text\n', 'removed.md': None}, pins=pins, legacy=True,
        restored_from=2, storage_revision=8, execution_definition_ref='definition',
        published_at='2026-11-01T00:00:00+00:00', summary_line='one\nline\x00' + 'x' * 150)
    assert isinstance(first, dict)
    record = first['record']
    assert record['schema_version'] == 'asset_version.v1'
    assert record['legacy'] is True and record['restored_from'] == 2
    assert record['storage_revision'] == 8 and record['pins'] == pins
    assert record['code_sha256'] == hashlib.sha256(b'pass\n').hexdigest()
    assert record['published_at'] == '2026-11-01T00:00:00+00:00'
    assert len(record['summary_line']) <= 120 and not any(c in record['summary_line'] for c in '\n\x00')
    assert set(record['side_blobs']) == {'WORKFLOW.md'}
    second = history.record_version(tmp_path, 'workflow', 'demo', payload={'steps': [1]}, code=b'pass\n', execution_definition_ref='definition')
    assert history.find_version(tmp_path, 'workflow', 'demo', code_sha256=record['code_sha256']) == second['record']
    assert history.find_version(tmp_path, 'workflow', 'demo', execution_definition_ref='definition') == second['record']
    assert history.find_version(tmp_path, 'workflow', 'demo', fingerprint=record['fingerprint']) == record
    assert history.find_version(tmp_path, 'workflow', 'demo', code_sha256='0' * 64, execution_definition_ref='definition') is None
    rows = history.list_versions(tmp_path, 'workflow', 'demo', limit=1)
    assert [row['version'] for row in rows] == [2]
    assert 'payload_blob' not in rows[0]
    assert history.list_versions(tmp_path, 'workflow', 'demo', limit=0) == []
    index = json.loads((_root(tmp_path) / 'versions' / 'workflow' / 'demo.json').read_text())
    assert [row['version'] for row in index['versions']] == [1, 2]


# LLM: 版本CAS须在同一资产锁内核验；不把bool当整数版本。
# 函数用途: 验证冲突错误和并发写者不丢行。
def test_version_cas_and_concurrent_writers(tmp_path):
    history.record_version(tmp_path, 'script', 'demo', payload=_payload(), code=b'first')
    with pytest.raises(history.AssetHistoryError) as error:
        history.record_version(tmp_path, 'script', 'demo', payload=_payload(), code=b'next', expected_latest=0)
    assert error.value.code == 'ASSET_VERSION_CONFLICT'
    barrier = Barrier(2)

    # LLM: 同时进入真实存储调用，不用睡眠猜测线程顺序。
    # 函数用途: 竞争发布不同代码。
    def publish(code):
        barrier.wait(timeout=10)
        return history.record_version(tmp_path, 'script', 'demo', payload=_payload(), code=code)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, (b'two', b'three')))
    assert sorted(result['version'] for result in results) == [2, 3]
    assert [row['version'] for row in history.list_versions(tmp_path, 'script', 'demo')] == [3, 2, 1]


# LLM: 历史代码按raw字节落盘；任何所需blob缺失都不能回退到新版本。
# 函数用途: 验证staging布局、侧文件及结构化缺失错误。
@pytest.mark.parametrize('missing', ['code_sha256', 'payload_blob', 'side'])
def test_materialize_and_missing_blob(tmp_path, missing):
    result = history.record_version(tmp_path, 'script', 'demo', payload=_payload(),
        code=b'print(1)\r\n', side_files={'README.md': 'note\n'})
    assert isinstance(result, dict)
    record = result['record']
    staging = tmp_path / 'stage'
    actual = history.materialize_script(tmp_path, record, staging)
    assert actual == staging / 'demo' / 'v1'
    assert (actual / 'main.py').read_bytes() == b'print(1)\r\n'
    assert (actual / 'README.md').read_text() == 'note\n'
    sha = record['side_blobs']['README.md'] if missing == 'side' else record[missing]
    (_root(tmp_path) / 'blobs' / sha[:2] / sha).unlink()
    with pytest.raises(history.AssetHistoryError) as error:
        history.materialize_script(tmp_path, record, tmp_path / 'second-stage')
    assert error.value.code == 'ASSET_HISTORY_BLOB_MISSING'
    assert not (tmp_path / 'second-stage' / 'demo' / 'v1' / 'main.py').exists()


# LLM: 索引可重建，发布记录和软删除标记不是可清理版本。
# 函数用途: 验证损坏索引修复和删除后历史保留。
@pytest.mark.parametrize('broken', ['not json', '{}', '{"latest":999}', '[]'])
def test_corrupt_index_rebuild_and_deleted_history(tmp_path, broken):
    first = history.record_version(tmp_path, 'script', 'demo', payload=_payload(), code=b'first')
    assert isinstance(first, dict)
    history.record_version(tmp_path, 'script', 'demo', payload=_payload(), code=b'second')
    index_path = _root(tmp_path) / 'versions' / 'script' / 'demo.json'
    index_path.write_text(broken)
    assert history.latest_version(tmp_path, 'script', 'demo')['version'] == 2
    assert json.loads(index_path.read_text())['latest'] == 2
    history.mark_deleted(tmp_path, 'script', 'demo', deleted_at='2026-11-02T00:00:00Z')
    assert json.loads(index_path.read_text())['deleted_at'] is not None
    assert history.get_version(tmp_path, 'script', 'demo', 1) == first['record']


# LLM: 写索引失败后旧索引原样可读，已落盘版本不可在重试时被覆盖。
# 函数用途: 注入一次I/O故障并检查追加恢复不变量。
def test_index_write_error_preserves_previous_index(tmp_path, monkeypatch):
    first = history.record_version(tmp_path, 'script', 'demo', payload=_payload(), code=b'first')
    assert isinstance(first, dict)
    index_path = _root(tmp_path) / 'versions' / 'script' / 'demo.json'
    previous = index_path.read_bytes()
    real_write = history.write_text_atomic_beneath
    raised = []

    # LLM: 只在指定索引写入前失败一次，其他真实原子写照常执行。
    # 函数用途: 模拟索引替换入口I/O错误。
    def simulated_write(root, parts, text):
        if parts[-1] == 'demo.json' and 'versions' in parts and not raised:
            raised.append(True)
            raise OSError('simulated I/O error')
        return real_write(root, parts, text)

    monkeypatch.setattr(history, 'write_text_atomic_beneath', simulated_write)
    with pytest.raises(OSError, match='simulated I/O error'):
        history.record_version(tmp_path, 'script', 'demo', payload=_payload(), code=b'second')
    assert index_path.read_bytes() == previous
    assert json.loads(previous)['latest'] == 1
    second_path = _root(tmp_path) / 'versions' / 'script' / 'demo' / 'v2.json'
    immutable = second_path.read_bytes()
    third = history.record_version(tmp_path, 'script', 'demo', payload=_payload(), code=b'third')
    assert third['version'] == 3
    assert second_path.read_bytes() == immutable
    assert [row['version'] for row in history.list_versions(tmp_path, 'script', 'demo')] == [3, 2, 1]


# LLM: 草稿不占发布版本号；按注入时间处理三类TTL，不动正式历史与blob。
# 函数用途: 验证草稿CRUD、过期清理和无限正式保留。
def test_drafts_prune_and_published_retention(tmp_path):
    result = history.save_draft(tmp_path, 'script', 'demo', payload=_payload(), code=b'draft',
        side_files={'README.md': 'draft'}, base_version=2)
    assert isinstance(result, dict)
    assert 'version' not in result and result['base_version'] == 2
    assert history.get_draft(tmp_path, 'script', 'demo') == result
    assert history.latest_version(tmp_path, 'script', 'demo') is None
    assert history.delete_draft(tmp_path, 'script', 'demo') is True
    assert history.delete_draft(tmp_path, 'script', 'demo') is False
    result = history.save_draft(tmp_path, 'script', 'demo', payload=_payload(), code=b'draft')
    old = '2020-01-01T00:00:00+00:00'
    for number in range(1, 102):
        history.record_version(tmp_path, 'script', 'demo', payload=_payload(), code=str(number).encode(), published_at=old)
    root = _root(tmp_path)
    request = root / 'requests' / 'run.json'
    request.parent.mkdir(parents=True)
    request.write_text(json.dumps({'created_at': old}))
    candidate = root / 'candidates' / 'script' / 'demo' / 'candidate.json'
    candidate.parent.mkdir(parents=True)
    candidate.write_text(json.dumps({'created_at': old}))
    now = datetime.fromisoformat(result['updated_at'])
    history.prune_history(tmp_path, now=(now + timedelta(days=13)).isoformat())
    assert history.get_draft(tmp_path, 'script', 'demo') == result
    removed = history.prune_history(tmp_path, now=(now + timedelta(days=15)).isoformat())
    assert isinstance(removed, dict)
    assert history.get_draft(tmp_path, 'script', 'demo') is None
    assert not request.exists() and not candidate.exists()
    rows = history.list_versions(tmp_path, 'script', 'demo', limit=200)
    assert len(rows) == 101
    assert history.get_version(tmp_path, 'script', 'demo', 1)['legacy'] is False
    for row in rows:
        record = history.get_version(tmp_path, 'script', 'demo', row['version'])
        assert history.blob_exists(tmp_path, record['code_sha256'])
        assert history.blob_exists(tmp_path, record['payload_blob'])


# LLM: 回放判定只认枚举；后写入的相同边结论覆盖旧观察，非连续边不跳级。
# 函数用途: 验证兼容链在首个未通过边停止。
def test_compatibility_chain(tmp_path):
    assert history.compatible_successor(tmp_path, 'script', 'demo', 1) is None
    first = history.record_compat_edge(tmp_path, 'script', 'demo', from_version=1, to_version=2,
        verdict='replay_passed', replayed=10, evidence_ref='ref-one')
    assert isinstance(first, dict)
    assert first['replayed'] == 10 and first['evidence_ref'] == 'ref-one'
    history.record_compat_edge(tmp_path, 'script', 'demo', from_version=2, to_version=3, verdict='replay_passed')
    assert history.compatible_successor(tmp_path, 'script', 'demo', 1) == 3
    history.record_compat_edge(tmp_path, 'script', 'demo', from_version=2, to_version=3, verdict='replay_failed')
    assert history.compatible_successor(tmp_path, 'script', 'demo', 1) == 2
    history.record_compat_edge(tmp_path, 'script', 'demo', from_version=2, to_version=4, verdict='replay_passed')
    assert history.compatible_successor(tmp_path, 'script', 'demo', 1) == 2
    assert history.read_compat_edges(tmp_path, 'script', 'demo')[-1]['to_version'] == 4
    with pytest.raises(ValueError):
        history.record_compat_edge(tmp_path, 'script', 'demo', from_version=1, to_version=2, verdict='unknown')


# LLM: 配额按Owner原始字节计算；重复blob不多计，独立写者不能共用最后空间。
# 函数用途: 验证配额边界、并发准入和隔离。
def test_blob_quota_concurrent_admission(tmp_path):
    assert history.blob_put(tmp_path, b'abc', max_total_bytes=3) == hashlib.sha256(b'abc').hexdigest()
    with pytest.raises(history.AssetHistoryError) as error:
        history.blob_put(tmp_path, b'd', max_total_bytes=3)
    assert error.value.code == 'ASSET_HISTORY_QUOTA_EXCEEDED'
    other = tmp_path / 'other'
    barrier = Barrier(2)

    # LLM: 两个资产共用Owner blob配额，返回结构化准入结果。
    # 函数用途: 同时竞争三字节配额。
    def put(raw):
        barrier.wait(timeout=10)
        try:
            return history.blob_put(other, raw, max_total_bytes=3)
        except history.AssetHistoryError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(put, (b'one', b'two')))
    assert results.count('ASSET_HISTORY_QUOTA_EXCEEDED') == 1


# LLM: 用户段必须是安全单段，非法输入在发生任何状态写入前拒绝。
# 函数用途: 验证kind、ID、digest及侧文件路径边界。
@pytest.mark.parametrize('kind,asset_id', [('unknown', 'demo'), ('script', '../demo'), ('script', '/absolute'), ('script', 'x/y')])
def test_invalid_asset_identity(tmp_path, kind, asset_id):
    with pytest.raises(ValueError):
        history.record_version(tmp_path, kind, asset_id, payload={})
    assert not _root(tmp_path).exists()


# LLM: 标识与文件名不能穿越；所有历史读写入口共用相同校验。
# 函数用途: 验证错误版本、摘要hash和侧文件均被拒绝。
def test_invalid_blob_and_side_names(tmp_path):
    with pytest.raises(ValueError):
        history.blob_get(tmp_path, '../bad')
    with pytest.raises(ValueError):
        history.record_version(tmp_path, 'script', 'demo', payload={}, side_files={'../bad': 'text'})
    with pytest.raises(ValueError):
        history.get_version(tmp_path, 'script', 'demo', True)


# LLM: Windows不支持dir_fd时走既有portable原子写，不引入fcntl。
# 函数用途: 验证替代文件系统入口上的真实往返。
def test_portable_atomic_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(nofollow_fs, '_supports_dir_fd', lambda: False)
    result = history.record_version(tmp_path, 'script', 'demo', payload=_payload(), code=b'pass\n')
    assert isinstance(result, dict)
    assert history.get_version(tmp_path, 'script', 'demo', 1) == result['record']
    actual = history.materialize_script(tmp_path, result['record'], tmp_path / 'stage')
    assert (actual / 'main.py').read_bytes() == b'pass\n'


# LLM: 合同公开参数按名字、位置/关键字、默认值核对，不依赖测试作者手抄。
# 函数用途: 验证跨分片公共API形状。
def test_history_public_signatures():
    expected = {
        'blob_put': 'owner_home, data: bytes, *, max_total_bytes: int = 268435456',
        'blob_get': 'owner_home, sha256: str', 'blob_exists': 'owner_home, sha256: str',
        'record_version': "owner_home, kind, asset_id, *, payload: Mapping, code: bytes | None = None, side_files: Mapping[str, str | None] | None = None, execution_definition_ref: str | None = None, storage_revision: int | None = None, pins: Mapping | None = None, summary_line: str = '', published_at: str | None = None, legacy: bool = False, restored_from: int | None = None, expected_latest: int | None = None",
        'latest_version': 'owner_home, kind, asset_id', 'get_version': 'owner_home, kind, asset_id, version: int',
        'find_version': 'owner_home, kind, asset_id, *, code_sha256: str | None = None, execution_definition_ref: str | None = None, fingerprint: str | None = None',
        'list_versions': 'owner_home, kind, asset_id, *, limit: int = 20',
        'mark_deleted': 'owner_home, kind, asset_id, *, deleted_at: str',
        'materialize_script': 'owner_home, record: Mapping, staging_root',
        'save_draft': 'owner_home, kind, asset_id, *, payload: Mapping, code: bytes | None = None, side_files: Mapping[str, str | None] | None = None, base_version: int | None = None',
        'get_draft': 'owner_home, kind, asset_id', 'delete_draft': 'owner_home, kind, asset_id',
        'prune_history': 'owner_home, *, now: str, draft_ttl_days: int = 14, request_ttl_days: int = 14, candidate_ttl_days: int = 30',
        'normalize_code_text': 'text: str', 'code_fingerprint': 'code: bytes | str',
        'payload_fingerprint': 'payload: Mapping',
        'asset_fingerprint': '*, payload: Mapping, code: bytes | str | None = None, side_files: Mapping[str, str | None] | None = None',
        'record_compat_edge': "owner_home, kind, asset_id, *, from_version: int, to_version: int, verdict: str, replayed: int = 0, evidence_ref: str = '', recorded_at: str | None = None",
        'read_compat_edges': 'owner_home, kind, asset_id',
        'compatible_successor': 'owner_home, kind, asset_id, from_version: int',
    }
    for name, arguments in expected.items():
        tree = ast.parse(f'def f({arguments}): pass')
        namespace = {}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), tree.body[0]], type_ignores=[])), '<signature>', 'exec'), namespace)
        actual = inspect.signature(getattr(history, name))
        reference = inspect.signature(namespace['f'])
        assert [(p.name, p.kind, p.default) for p in actual.parameters.values()] == [(p.name, p.kind, p.default) for p in reference.parameters.values()], name
