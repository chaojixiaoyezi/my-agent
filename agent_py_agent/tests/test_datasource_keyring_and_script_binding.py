# LLM: 仅在临时Owner内使用合成秘密、真实SQLite和受控脚本，原沙箱测试仍独立验收，不连接外部服务。
# 模块用途: 验证数据源秘密隔离、脚本授权、SQL只读、迁移完整性及运行结束清理。
import base64
import hashlib
import hmac
import importlib
import importlib.util
import inspect
import json
import os
import sqlite3
from dataclasses import asdict
from pathlib import Path

import pytest


# LLM: 用接口存在断言保留未实现红灯，不把ImportError误当权限验证通过。
# 函数用途: 取得本轮数据源存储类型。
@pytest.fixture
def store(tmp_path):
    name = 'agent_py_agent.agent.assets.datasource_store'
    assert importlib.util.find_spec(name), 'DATASOURCE_STORE_NOT_IMPLEMENTED'
    return importlib.import_module(name).DatasourceStore(tmp_path / 'owner')


# LLM: 写入的SQLite全在当前测试Owner内；源代号和存储路径均由正常save入口登记。
# 函数用途: 准备具有真实表、索引与数据的已登记连接。
def database(store, datasource_id='orders', **options):
    path = store.owner_home / f'{datasource_id}.sqlite'
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript('CREATE TABLE orders(id INTEGER PRIMARY KEY, value TEXT NOT NULL);'
            'CREATE INDEX orders_value ON orders(value);'
            "INSERT INTO orders VALUES(1,'first'),(2,'second');CREATE TABLE hidden(value TEXT);")
    store.save_datasource({'datasource_id': datasource_id, 'kind': 'sqlite', 'title': '订单',
        'database_path': str(path), **options})
    return path


# LLM: 用合成认证及真实私有文件验证提取、版本冲突与公开摘要，不接外部库。
# 函数用途: 守住秘密不进规格/磁盘明文、0600权限与CAS更新。
def test_secret_extraction_cas_and_private_storage(store):
    password, token = 'unit-password-9Jq7', 'unit-token-kL29'
    result = store.save_datasource({'datasource_id': 'remote', 'kind': 'postgres', 'title': '远程库',
        'dsn_template': f'postgresql://analyst:{password}@db.example.invalid/app',
        'options': {'headers': {'Authorization': token}}})
    assert isinstance(result, dict), '保存必须返回脱敏公开结果'
    assert result['has_secret'] is True and result['revision'] == 1
    spec = store.get_datasource_spec('remote')
    assert spec.credential_ref and password not in json.dumps(asdict(spec))
    path = store.owner_home / 'assets/datasources/remote.json'
    assert password not in path.read_text() and token not in path.read_text()
    keyring = store.owner_home / 'data/datasource_secrets' / f'{spec.credential_ref}.json'
    assert keyring.stat().st_mode & 0o777 == 0o600
    assert password not in keyring.read_text() and token not in keyring.read_text()
    assert password in store.resolve_secret(spec.credential_ref).values()
    with pytest.raises(ValueError, match='REVISION'):
        store.save_datasource(asdict(spec), expected_revision=0)
    updated = store.save_datasource({**asdict(spec), 'title': '更新'}, expected_revision=1)
    assert updated['revision'] == 2 and updated['has_secret']
    assert password not in json.dumps(store.list_datasources(), ensure_ascii=False)


# LLM: 连通性和schema均使用真实SQLite，不能用模拟SELECT结果代替本机观察。
# 函数用途: 验证SQLite连接、字段映射、行数和零配置建库。
def test_sqlite_real_connection_schema_and_zero_config(store):
    path = database(store)
    result = store.test_datasource('orders')
    assert result['ok'] is True and result['latency_ms'] >= 0
    schema = store.inspect_schema('orders')
    assert schema['ok']
    table = next(t for t in schema['tables'] if t['name'] == 'orders')
    assert table['row_count_estimate'] == 2
    assert table['columns'][0] == {'name': 'id', 'type': 'INTEGER', 'notnull': False, 'pk': True}
    store.save_datasource({'datasource_id': 'local', 'kind': 'sqlite', 'title': '零配置'})
    assert store.test_datasource('local')['ok']
    assert Path(store.get_datasource_spec('local').database_path).exists() and path.exists()


# LLM: 同ID在两个层级独立落盘，另一个Owner只观察自己的钥匙串。
# 函数用途: 验证workspace覆盖Owner但不共享凭据。
def test_workspace_overlay_and_owner_secret_isolation(store, tmp_path):
    owner = store.save_datasource({'datasource_id': 'shared', 'kind': 'custom', 'title': 'Owner'},
        secret_payload={'password': 'unit-owner-secret'})
    workspace = tmp_path / 'project'
    workspace.mkdir()
    store.save_datasource({'datasource_id': 'shared', 'kind': 'custom', 'title': 'Workspace'},
        scope='workspace', workspace_root=workspace)
    assert store.list_datasources(workspace_root=workspace)[0]['title'] == 'Workspace'
    assert store.get_datasource_spec('shared').title == 'Owner'
    other = type(store)(tmp_path / 'other')
    assert other.list_datasources() == []
    assert not other.has_secret(owner['credential_ref'])


# LLM: 无效输入应在规格写入前拒绝，不能靠宽松类型转换制造合法身份。
# 函数用途: 验证ID、布尔、端口及必填文本的类型门。
@pytest.mark.parametrize('field,value', [('datasource_id', '../bad'), ('read_only', 'false'), ('port', True), ('kind', 12), ('title', ['bad'])])
def test_invalid_spec_fails_before_writing(store, field, value):
    with pytest.raises((ValueError, TypeError)):
        store.save_datasource({'datasource_id': 'bad', 'kind': 'sqlite', 'title': '坏配置', field: value})
    assert not (store.owner_home / 'assets/datasources/bad.json').exists()


# LLM: 实际建立符号链接和外部目录，核对目标目录没有被写入。
# 函数用途: 验证钥匙串nofollow与数据库路径范围。
def test_symlinked_keyring_and_external_database_are_rejected(store, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    data = store.owner_home / 'data'
    data.mkdir(parents=True)
    (data / 'datasource_secrets').symlink_to(outside, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        store.save_datasource({'datasource_id': 'secret', 'kind': 'custom', 'title': '秘密'},
            secret_payload={'password': 'unit-private'})
    assert list(outside.iterdir()) == []
    with pytest.raises((OSError, ValueError)):
        store.save_datasource({'datasource_id': 'external', 'kind': 'sqlite', 'title': '越界',
            'database_path': str(outside / 'escape.sqlite')})


# LLM: 不向私网端点发HTTP请求，校验拒绝必须表现为失败观察。
# 函数用途: 防止把私网HTTP地址验证报为连通成功。
def test_http_private_host_test_is_not_connectivity_success(store):
    store.save_datasource({'datasource_id': 'api', 'kind': 'http_api', 'title': 'API',
        'dsn_template': 'http://127.0.0.1/private'})
    result = store.test_datasource('api')
    assert result['ok'] is False and result['error_code']


# LLM: 标准描述符保持c4的source_id/system/query_scope；访问等级是显式结构化access，不通过SQL正文猜权限。
# 函数用途: 构造规范资产声明，并为运行时直接绑定提供同一列表。
def declarations(access='read'):
    from agent_py_agent.agent.assets.descriptor import DataSourceSpec
    return [DataSourceSpec('orders', 'sqlite', query_scope={'access': access})]


# LLM: 真实SDK依赖当前ContextVar声明，退出绑定后不得继续复用访问权。
# 函数用途: 验证参数化查询、未声明拒绝及上下文清理。
def test_sdk_binding_deny_undeclared_and_context_reset(store):
    database(store)
    runtime = importlib.import_module('agent_py_agent.agent.workflows.datasource_runtime')
    sdk = importlib.import_module('agent_py_agent.runtime_sdk')
    with runtime.bind_runtime_datasources(store, declarations()):
        assert sdk.query_sql('orders', 'SELECT value FROM orders WHERE id=:id', {'id': 2}) == [{'value': 'second'}]
        assert 'unit-' not in repr(sdk.get_datasource('orders'))
        with pytest.raises(PermissionError, match='DATASOURCE_NOT_DECLARED_IN_SCRIPT_CONTRACT'):
            sdk.query_sql('hidden', 'SELECT 1')
    with pytest.raises(PermissionError, match='DATASOURCE_NOT_DECLARED_IN_SCRIPT_CONTRACT'):
        sdk.get_datasource('orders')


# LLM: 原生SQLite授权必须识别实际动作，包括CTE和注释变体；执行后核对原数据不变。
# 函数用途: 覆盖只读合同下的写入、DDL、PRAGMA及ATTACH拒绝。
@pytest.mark.parametrize('sql', [
    "INSERT INTO orders VALUES(3,'third')", "UPDATE orders SET value='changed'", 'DELETE FROM orders',
    'DROP TABLE orders', 'ALTER TABLE orders ADD COLUMN extra TEXT', 'CREATE TABLE extra(id)',
    "REPLACE INTO orders VALUES(1,'changed')", "/*lead*/ WITH v AS(SELECT 3) INSERT INTO orders SELECT *,'x' FROM v",
    'PRAGMA user_version=7', "ATTACH DATABASE ':memory:' AS extra", 'VACUUM',
])
def test_read_contract_cannot_mutate_even_with_sql_obfuscation(store, sql):
    path = database(store)
    runtime = importlib.import_module('agent_py_agent.agent.workflows.datasource_runtime')
    sdk = importlib.import_module('agent_py_agent.runtime_sdk')
    with runtime.bind_runtime_datasources(store, declarations()):
        with pytest.raises(PermissionError, match='DATASOURCE_READ_ONLY_VIOLATION'):
            sdk.query_sql('orders', sql)
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT * FROM orders ORDER BY id').fetchall() == [(1, 'first'), (2, 'second')]


# LLM: 脚本read_write不是覆盖数据源read_only或表范围的权力。
# 函数用途: 验证可写合同、数据源只读与表白名单组合。
def test_read_write_and_datasource_read_only_and_tables(store):
    database(store, allowed_tables=['orders'])
    runtime = importlib.import_module('agent_py_agent.agent.workflows.datasource_runtime')
    sdk = importlib.import_module('agent_py_agent.runtime_sdk')
    with runtime.bind_runtime_datasources(store, declarations('read_write')):
        sdk.query_sql('orders', "INSERT INTO orders VALUES(3,'third')")
        with pytest.raises(PermissionError):
            sdk.query_sql('orders', 'SELECT * FROM hidden')
    spec = store.get_datasource_spec('orders')
    store.save_datasource({**asdict(spec), 'read_only': True}, expected_revision=spec.revision)
    with runtime.bind_runtime_datasources(store, declarations('read_write')):
        with pytest.raises(PermissionError, match='DATASOURCE_READ_ONLY_VIOLATION'):
            sdk.query_sql('orders', 'DELETE FROM orders')


# LLM: 准备不得偷切连接；显式切换复用同一状态账并保留原库。
# 函数用途: 验证SQLite迁移五阶段、完整验收与零改代码绑定切换。
def test_sqlite_migration_validates_before_logical_switch(store):
    original = database(store)
    target = {'datasource_id': 'new-orders', 'kind': 'sqlite', 'title': '新库',
        'database_path': str(store.owner_home / 'new.sqlite')}
    prepared = store.migrate_datasource('orders', target)
    assert prepared['state'] == 'READY_TO_SWITCH'
    assert prepared['validation']['ok'] and prepared['validation']['row_counts']['orders'] == 2
    assert store.get_datasource_spec('orders').database_path == str(original)
    # 显式switch可复用同一个已经验证的迁移，不覆盖现有目标库或再拷贝一次。
    switched = store.migrate_datasource('orders', {'migration_id': prepared['migration_id']}, mode='switch')
    assert switched['state'] == 'SWITCHED'
    assert [h['state'] for h in switched['history']] == ['PREPARED', 'COPYING', 'VALIDATING', 'READY_TO_SWITCH', 'SWITCHED']
    assert store.get_datasource_spec('orders').database_path == target['database_path']
    assert original.exists()
    assert json.loads((store.owner_home / 'data/datasource_migrations' / f"{prepared['migration_id']}.json").read_text())['state'] == 'SWITCHED'


# LLM: 已有目标禁止覆盖，准备后改过的源规格必须触发CAS冲突。
# 函数用途: 验证迁移失败保留原逻辑连接。
def test_migration_existing_target_and_stale_source_never_switch(store):
    original = database(store)
    target = database(store, 'target')
    result = store.migrate_datasource('orders', {'datasource_id': 'new', 'kind': 'sqlite', 'title': '不可覆盖',
        'database_path': str(target)}, mode='switch')
    assert result['state'] == 'FAILED'
    assert store.get_datasource_spec('orders').database_path == str(original)
    prepared = store.migrate_datasource('orders', {'datasource_id': 'next', 'kind': 'sqlite', 'title': '准备',
        'database_path': str(store.owner_home / 'next.sqlite')})
    spec = store.get_datasource_spec('orders')
    store.save_datasource({**asdict(spec), 'title': '并发修改'}, expected_revision=spec.revision)
    result = store.migrate_datasource('orders', {'migration_id': prepared['migration_id']}, mode='switch')
    assert result['state'] == 'FAILED' and store.get_datasource_spec('orders').database_path == str(original)


# LLM: 通过工具公开入口保存及列表，非法Owner参数不能成为身份授权。
# 函数用途: 验证工具回执脱敏、公开字段与参数边界。
def test_datasources_tool_public_output_and_effects(store):
    tool_cls = importlib.import_module('agent_py_agent.agent.tooling.datasources_tool').DatasourcesTool
    tool = tool_cls(store.owner_home)
    result = tool.execute({'action': 'save', 'spec_payload': {'datasource_id': 'remote', 'kind': 'custom', 'title': '库'},
        'secret_payload': {'password': 'unit-tool-secret-19'}})
    assert result.ok and 'unit-tool-secret-19' not in result.output
    listed = tool.execute({'action': 'list'})
    assert listed.ok and json.loads(listed.output)['datasources'][0]['has_secret']
    assert not tool.execute({'action': 'get', 'datasource_id': '../escape'}).ok
    assert not tool.execute({'action': 'list', 'owner_home': '/escape'}).ok


# LLM: 真实ScriptStore冻结代码，runner的进程内测试不冒称内核沙箱验收；原工具测试仍用其默认沙箱配置。
# 函数用途: 创建一个具有规范数据源合同的脚本。
def script(store, code, access='read'):
    from agent_py_agent.agent.assets.descriptor import AssetDescriptor
    from agent_py_agent.agent.assets.script_store import ScriptStore
    ScriptStore(store.owner_home).save_script(AssetDescriptor(
        'report', 'script', '报表', data_sources=tuple(declarations(access))), code)


# LLM: 使用真实-I子进程并核对SDK源码位置，stdout/stderr及结果对象均不得泄露合成秘密。
# 函数用途: 验证运行时注入、当前版本导入、双流脱敏与manifest删除。
def test_script_subprocess_current_sdk_and_secret_redaction_cleanup(store):
    from agent_py_agent.agent.workflows.script_runner import ScriptRunner
    database(store)
    spec = store.get_datasource_spec('orders')
    store.save_datasource(asdict(spec), secret_payload={'password': 'unit-print-secret-91'})
    script(store, '''import os,json,sys
import agent_py_agent.runtime_sdk as sdk
manifest = os.environ['MY_AGENT_DATASOURCE_RUNTIME_MANIFEST']
with open(manifest) as stream:
    secret = json.load(stream)['bindings']['orders']['secret']['password']
print(secret, file=sys.stderr)
print(json.dumps({'rows':sdk.query_sql('orders','SELECT value FROM orders WHERE id=?',[1]),
    'oops':secret,'manifest':manifest,'sdk_file':sdk.__file__}))
''')
    result = ScriptRunner(store.owner_home, use_sandbox=False).run_script('report')
    assert result.ok, result.stderr_preview
    assert result.output_data['rows'] == [{'value': 'first'}]
    assert 'unit-print-secret-91' not in json.dumps(asdict(result))
    assert result.output_data['oops'] == '<redacted>'
    assert not Path(result.output_data['manifest']).exists()
    assert Path(result.output_data['sdk_file']).resolve() == Path(__file__).resolve().parents[1] / 'runtime_sdk.py'


# LLM: 子进程合同必须来自冻结脚本，override只能改查询范围不能授权新源。
# 函数用途: 验证真实脚本的未声明访问及只读写入拒绝。
@pytest.mark.parametrize('code,error', [
    ("query_sql('undeclared', 'SELECT 1')", 'DATASOURCE_NOT_DECLARED_IN_SCRIPT_CONTRACT'),
    ("query_sql('orders', 'DELETE FROM orders')", 'DATASOURCE_READ_ONLY_VIOLATION'),
    ("get_datasource('undeclared')", 'DATASOURCE_NOT_DECLARED_IN_SCRIPT_CONTRACT'),
])
def test_real_subprocess_denials_and_no_override_escalation(store, code, error):
    from agent_py_agent.agent.workflows.script_runner import ScriptRunner
    database(store)
    script(store, 'from agent_py_agent.runtime_sdk import get_datasource, query_sql\n' + code)
    result = ScriptRunner(store.owner_home, use_sandbox=False).run_script('report', override_data_sources=[{'id': 'undeclared', 'access': 'read_write'}])
    assert not result.ok and error in result.stderr_preview
    assert not list((store.owner_home / 'data/script_exec').glob('*/datasources-*.json'))


# LLM: dump本地回放不能冒充远端导入，外库验收缺失时禁止switch。
# 函数用途: 验证私有SQL导出、验收范围和外库切换硬门。
def test_external_dump_is_verified_locally_but_cannot_switch(store):
    original = database(store)
    prepared = store.migrate_datasource('orders', {'datasource_id': 'external', 'kind': 'postgres',
        'title': '远程', 'host': 'db.example.invalid'})
    assert prepared['validation']['ok']
    assert prepared['validation']['scope'] == 'sql_dump_roundtrip'
    assert prepared['external_target_validated'] is False
    assert Path(prepared['dump_path']).stat().st_mode & 0o777 == 0o600
    result = store.migrate_datasource('orders', {'migration_id': prepared['migration_id']}, mode='switch')
    assert result['state'] == 'FAILED'
    assert store.get_datasource_spec('orders').database_path == str(original)


# LLM: 单独提交的认证同样必须参与规格全值脱敏，而非只剥离敏感键。
# 函数用途: 复现并守住description/options重复秘密的修复。
def test_explicit_secret_payload_also_masks_same_value_in_spec_text(store):
    secret = 'unit-explicit-secret-72'
    saved = store.save_datasource({'datasource_id': 'remote', 'kind': 'custom', 'title': '远程',
        'description': f'不可包含 {secret}', 'options': {'innocent': secret}}, secret_payload={'password': secret})
    assert secret not in json.dumps(saved, ensure_ascii=False)
    assert secret not in (store.owner_home / 'assets/datasources/remote.json').read_text()


# LLM: 行数相同不能证明内容相同，切换时必须重验源和目标。
# 函数用途: 验证准备后任一侧的数据内容漂移都会阻止切换。
@pytest.mark.parametrize('changed', ['source', 'target'])
def test_migration_content_drift_is_not_hidden_by_same_row_count(store, changed):
    source = database(store)
    target = store.owner_home / 'new.sqlite'
    prepared = store.migrate_datasource('orders', {'datasource_id': 'new', 'kind': 'sqlite', 'title': '新库', 'database_path': str(target)})
    with sqlite3.connect(source if changed == 'source' else target) as conn:
        conn.execute("UPDATE orders SET value='changed' WHERE id=1")
    result = store.migrate_datasource('orders', {'migration_id': prepared['migration_id']}, mode='switch')
    assert result['state'] == 'FAILED'
    assert store.get_datasource_spec('orders').database_path == str(source)


# LLM: 受限根使用真实Python审计guard，不冒称内核沙箱；合法连接根仍应可用。
# 函数用途: 验证guard入口的SDK只读和读写路径。
@pytest.mark.parametrize('access', ['read', 'read_write'])
def test_restricted_root_subprocess_can_use_only_declared_sqlite(store, access):
    from agent_py_agent.agent.workflows.script_runner import ScriptRunner
    database(store)
    sql = 'SELECT value FROM orders WHERE id=1' if access == 'read' else "UPDATE orders SET value='written' WHERE id=1"
    script(store, f'from agent_py_agent.runtime_sdk import query_sql\nimport json\nprint(json.dumps({{"rows":query_sql("orders",{sql!r})}}))', access)
    result = ScriptRunner(store.owner_home, use_sandbox=False, restrict_to_roots=True).run_script('report')
    assert result.ok, result.stderr_preview


# LLM: 超时启动真实进程，异常仅注入执行OS故障；两条出口都检查秘密及文件残留。
# 函数用途: 验证失败回执脱敏和manifest清理。
@pytest.mark.parametrize('failure', ['timeout', 'exception'])
def test_runtime_manifest_cleanup_and_secret_redaction_on_failure(store, monkeypatch, failure):
    from agent_py_agent.agent.workflows import script_runner
    database(store)
    spec = store.get_datasource_spec('orders')
    secret = 'unit-failure-secret-87'
    store.save_datasource(asdict(spec), secret_payload={'password': secret})
    script(store, 'import time\ntime.sleep(5)')
    if failure == 'exception':
        # LLM: 仅替代不可稳定制造的OS异常，前面的Store/manifest仍真实准备。
        # 函数用途: 验证异常消息的真实解析秘密也必须清除。
        def fail(*args, **kwargs):
            raise OSError('failure ' + secret)
        monkeypatch.setattr(script_runner, '_execute', fail)
    result = script_runner.ScriptRunner(store.owner_home, use_sandbox=False).run_script('report', timeout_seconds=0.1)
    assert not result.ok
    assert result.error_code == ('SCRIPT_TIMEOUT' if failure == 'timeout' else 'SCRIPT_CONFIGURATION_ERROR')
    assert secret not in json.dumps(asdict(result))
    assert not list((store.owner_home / 'data/script_exec').glob('*/datasources-*.json'))


# LLM: 迁移仅改位置时不能隐式清空原只读或表范围，除非目标显式另行声明。
# 函数用途: 验证逻辑连接的访问限制随迁移保留。
def test_migration_keeps_logical_access_policy_when_only_location_changes(store):
    database(store, read_only=True, allowed_tables=['orders'])
    result = store.migrate_datasource('orders', {'datasource_id': 'new', 'kind': 'sqlite', 'title': '新库',
        'database_path': str(store.owner_home / 'new.sqlite')}, mode='switch')
    assert result['state'] == 'SWITCHED'
    spec = store.get_datasource_spec('orders')
    assert spec.read_only and spec.allowed_tables == ('orders',)


# LLM: schema验收以摘要比较，不把带敏感DEFAULT的原始DDL返回模型。
# 函数用途: 验证迁移回执不泄露schema中的认证常量。
def test_migration_schema_report_cannot_leak_secret_sql_defaults(store):
    path = database(store)
    secret = 'unit-schema-default-secret-62'
    with sqlite3.connect(path) as conn:
        conn.execute(f"CREATE TABLE credentials_hint(value TEXT DEFAULT '{secret}')")
    result = store.migrate_datasource('orders', {'datasource_id': 'new', 'kind': 'sqlite', 'title': '新库',
        'database_path': str(store.owner_home / 'new.sqlite')})
    assert result['state'] == 'READY_TO_SWITCH'
    assert secret not in json.dumps(result)


# LLM: 可信SDK预载不等于允许脚本自行ctypes加载；已有审计拒绝必须保持。
# 函数用途: 验证用户代码加载原生库仍被阻止。
def test_preloaded_sdk_does_not_disable_user_native_load_guard(store):
    from agent_py_agent.agent.workflows.script_runner import ScriptRunner
    database(store)
    script(store, 'import ctypes\nctypes.CDLL(None)')
    result = ScriptRunner(store.owner_home, use_sandbox=False, restrict_to_roots=True).run_script('report')
    assert not result.ok and 'Subprocess execution denied' in result.stderr_preview


# LLM: 四个并发调用必须共用迁移锁，任何迟到失败不得覆盖已成功终态。
# 函数用途: 验证并发切换幂等及唯一SWITCHED历史。
def test_parallel_migration_switch_is_idempotent_and_keeps_one_history(store):
    from concurrent.futures import ThreadPoolExecutor
    database(store)
    result = store.migrate_datasource('orders', {'datasource_id': 'new', 'kind': 'sqlite', 'title': '新库',
        'database_path': str(store.owner_home / 'new.sqlite')})
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.migrate_datasource('orders',
            {'migration_id': result['migration_id']}, mode='switch'), range(4)))
    assert all(row['state'] == 'SWITCHED' for row in results)
    persisted = json.loads((store.owner_home / 'data/datasource_migrations' / (result['migration_id'] + '.json')).read_text())
    assert persisted['state'] == 'SWITCHED'
    assert sum(item['state'] == 'SWITCHED' for item in persisted['history']) == 1


# LLM: workspace新覆盖与Owner整数revision可以碰撞；必须按原作用域绑定身份拒绝。
# 函数用途: 验证迁移不覆盖准备后新建的工作区配置。
def test_migration_cas_binds_owner_layer_not_just_integer_revision(store, tmp_path):
    source = database(store)
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    prepared = store.migrate_datasource('orders', {'datasource_id': 'new', 'kind': 'sqlite', 'title': '新库',
        'database_path': str(store.owner_home / 'new.sqlite')}, workspace_root=workspace)
    store.save_datasource({'datasource_id': 'orders', 'kind': 'sqlite', 'title': '工作区新覆盖',
        'database_path': str(source), 'read_only': True}, scope='workspace', workspace_root=workspace)
    result = store.migrate_datasource('orders', {'migration_id': prepared['migration_id']}, mode='switch', workspace_root=workspace)
    assert result['state'] == 'FAILED'
    assert store.get_datasource_spec('orders', workspace_root=workspace).title == '工作区新覆盖'
    assert store.get_datasource_spec('orders', workspace_root=workspace).read_only
    assert store.get_datasource_spec('orders').revision == 1


# LLM: 源规格提交和终态账分属原子文件，故障窗口不能冒称失败未切或持久化成功。
# 函数用途: 验证终态写失败报告unknown，并从实际绑定恢复而不重复提交。
def test_switch_state_write_failure_is_unknown_and_can_reconcile_committed_binding(store, monkeypatch):
    database(store)
    prepared = store.migrate_datasource('orders', {'datasource_id': 'new', 'kind': 'sqlite', 'title': '新库',
        'database_path': str(store.owner_home / 'new.sqlite')})
    original, failed = store._write, []
    # LLM: 仅对终态账本写入注入一次失败，不替代源规格提交或SQLite校验。
    # 函数用途: 复现连接已切换但迁移终态尚未落盘的窗口。
    def fail_once(root, parts, data):
        if 'datasource_migrations' in parts and data.get('state') == 'SWITCHED' and not failed:
            failed.append(True)
            raise OSError('state_write_failed')
        return original(root, parts, data)
    monkeypatch.setattr(store, '_write', fail_once)
    first = store.migrate_datasource('orders', {'migration_id': prepared['migration_id']}, mode='switch')
    assert first['state'] != 'SWITCHED' and first['effect_outcome'] == 'unknown'
    second = store.migrate_datasource('orders', {'migration_id': prepared['migration_id']}, mode='switch')
    assert second['state'] == 'SWITCHED'
    assert store.get_datasource_spec('orders').revision == 2


# LLM: 仅复用旧引用的更新也要读取已持凭据的实际值做脱敏。
# 函数用途: 防止引用更新把秘密重新写回公开说明。
def test_reusing_credential_reference_masks_its_value_in_updated_text(store):
    secret = 'unit-ref-secret-94'
    saved = store.save_datasource({'datasource_id': 'remote', 'kind': 'custom', 'title': '原始'},
        secret_payload={'password': secret})
    updated = store.save_datasource({'datasource_id': 'remote', 'kind': 'custom', 'title': '更新',
        'description': secret, 'credential_ref': saved['credential_ref']}, expected_revision=1)
    assert secret not in json.dumps(updated)
    assert secret not in (store.owner_home / 'assets/datasources/remote.json').read_text()


# LLM: 模拟可选外库驱动只验证调用合同，不把fake连接当真实远端验收。
# 函数用途: 验证已安装驱动优先、内存凭据注入与SELECT 1观察。
def test_installed_database_driver_is_used_without_tcp_false_positive(store, monkeypatch):
    import sys
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import agent_py_agent.agent.assets.datasource_store as module
    conn = MagicMock()
    conn.cursor.return_value.fetchone.return_value = (1,)
    driver = SimpleNamespace(__name__='psycopg', connect=MagicMock(return_value=conn))
    monkeypatch.setitem(sys.modules, 'psycopg', driver)
    monkeypatch.setattr(module.socket, 'create_connection', MagicMock())
    store.save_datasource({'datasource_id': 'postgres', 'kind': 'postgres', 'title': '驱动', 'host': 'db.example.invalid',
        'database': 'app', 'username': 'reader'}, secret_payload={'password': 'unit-driver-secret-56'})
    result = store.test_datasource('postgres')
    assert result['ok'] and result['details']['authenticated']
    assert driver.connect.call_args.kwargs['password'] == 'unit-driver-secret-56'
    conn.cursor.return_value.execute.assert_called_once_with('SELECT 1')
    assert 'unit-driver-secret-56' not in json.dumps(result)


# LLM: 同一认证槽位的冲突值不能相互覆盖后遗失脱敏集合，错误不能回显任何原值。
# 函数用途: 验证重复密码来源发生冲突时在落盘前明确拒绝。
@pytest.mark.parametrize('explicit', [False, True])
def test_conflicting_secret_values_cannot_silently_replace_each_other(store, explicit):
    first, second = 'unit-secret-first-32', 'unit-secret-second-48'
    data = {'datasource_id': 'conflict', 'kind': 'postgres', 'title': '冲突', 'password': first,
            'description': first + ' / ' + second}
    options = {'secret_payload': {'password': second}} if explicit else {}
    if not explicit:
        data['dsn_template'] = f'postgresql://user:{second}@db.example.invalid/app'
    with pytest.raises(ValueError, match='DATASOURCE_SECRET_CONFLICT'):
        store.save_datasource(data, **options)
    assert not (store.owner_home / 'assets/datasources/conflict.json').exists()


# LLM: 只公开真实钥匙串顶层键名，嵌套字段不展开，任何凭据值都不能进入摘要。
# 函数用途: 验证保存、直接摘要及列表都返回排序credential_keys并保留旧字段。
def test_public_summaries_include_only_sorted_credential_keys(store):
    secrets = {'z_token': 'unit-wave13-token', 'password': 'unit-wave13-password',
               'headers': {'Authorization': 'unit-wave13-bearer'}}
    saved = store.save_datasource({'datasource_id': 'keys', 'kind': 'custom', 'title': '凭据字段'}, secret_payload=secrets)
    direct = store.public_summary(store.get_datasource_spec('keys'))
    tool_cls = importlib.import_module('agent_py_agent.agent.tooling.datasources_tool').DatasourcesTool
    fetched = tool_cls(store.owner_home).execute({'action': 'get', 'datasource_id': 'keys'})
    assert fetched.ok
    for result in (saved, direct, store.list_datasources()[0], json.loads(fetched.output)):
        assert result['credential_keys'] == ['headers', 'password', 'z_token']
        assert result['has_secret'] is True and result['credential_ref'] == saved['credential_ref']
        assert all(value not in json.dumps(result) for value in ('unit-wave13-token', 'unit-wave13-password', 'unit-wave13-bearer'))
    saved['credential_keys'].append('not_saved')
    assert store.list_datasources()[0]['credential_keys'] == ['headers', 'password', 'z_token']


# LLM: 缺失或空凭据没有键名；不把一个不透明引用当成实际存在的秘密。
# 函数用途: 验证无秘密、缺失文件和空钥匙串摘要的兼容性。
@pytest.mark.parametrize('credential_ref', ['', 'dscred-missing', 'dscred-empty'])
def test_missing_or_empty_credentials_report_empty_keys(store, credential_ref):
    if credential_ref == 'dscred-empty':
        store._write(store.owner_home, ('data', 'datasource_secrets', credential_ref + '.json'), {})
    saved = store.save_datasource({'datasource_id': 'no-keys', 'kind': 'custom', 'title': '无凭据', 'credential_ref': credential_ref})
    assert saved['credential_keys'] == [] and saved['has_secret'] is False
    assert saved['credential_ref'] == credential_ref
    assert store.list_datasources()[0]['credential_keys'] == []


# LLM: 纯规格投影没有钥匙串上下文时仍有空列表，不推测或读取其它Owner凭据。
# 函数用途: 验证公开摘要函数本身的新增字段默认值。
def test_standalone_public_summary_defaults_to_empty_credential_keys():
    from agent_py_agent.agent.assets.datasource_store import (
        DatasourceConnectionSpec,
        DatasourcePublicSummary,
    )

    summary = DatasourcePublicSummary(DatasourceConnectionSpec('plain', 'custom', '摘要'))
    assert summary['credential_keys'] == [] and summary['credential_ref'] == '' and summary['has_secret'] is False


# LLM: 独立按任务规定的PBKDF2/CTR/HMAC配方验收，不调用产品解密器充当自身oracle；仅处理合成测试凭据。
# 函数用途: 核对密文信封可被独立解密且认证码覆盖nonce与实际密文字节。
def wp16_decode_envelope(home, envelope):
    key = bytes.fromhex((home / 'data/datasource_secrets/.vault_key').read_text())
    nonce = base64.b64decode(envelope['nonce'], validate=True)
    ciphertext = base64.b64decode(envelope['ciphertext'], validate=True)
    derived = hashlib.pbkdf2_hmac('sha256', key, nonce, 10000, dklen=64)
    assert len(key) == 32 and len(nonce) == 16
    assert hmac.compare_digest(envelope['mac'], hmac.new(derived[32:], nonce + ciphertext, hashlib.sha256).hexdigest())
    plaintext = bytes(value ^ hashlib.sha256(derived[:32] + (index // 32).to_bytes(8, 'big')).digest()[index % 32]
                      for index, value in enumerate(ciphertext))
    return json.loads(plaintext)


# LLM: 两次真实保存与另一个临时Owner对照，验证随机nonce、独立主密钥和磁盘无明文，不以0600冒充加密。
# 函数用途: 守住任务指定的认证密文格式、独立解密和公开键名。
def test_wp16_vault_encrypts_and_isolates_owner_keys(store, tmp_path):
    secrets = {'password': 'unit-wp16-encrypted-password', 'token': 'unit-wp16-encrypted-token'}
    saved = store.save_datasource({'datasource_id': 'encrypted', 'kind': 'custom', 'title': '加密'}, secret_payload=secrets)
    path = store.owner_home / 'data/datasource_secrets' / (saved['credential_ref'] + '.json')
    envelope = json.loads(path.read_text())
    assert envelope.get('schema_version') == 'datasource_vault.v1'
    assert envelope['alg'] == 'pbkdf2_sha256_ctr_hmac'
    assert envelope['credential_keys'] == ['password', 'token']
    assert all(value not in path.read_text() for value in secrets.values())
    assert wp16_decode_envelope(store.owner_home, envelope) == secrets
    assert store.resolve_secret(saved['credential_ref']) == secrets
    assert (path.parent / '.vault_key').stat().st_mode & 0o777 == 0o600
    assert path.stat().st_mode & 0o777 == 0o600
    second = store.save_datasource({'datasource_id': 'next', 'kind': 'custom', 'title': '新盐'}, secret_payload=secrets)
    other_envelope = json.loads((path.parent / (second['credential_ref'] + '.json')).read_text())
    assert (envelope['nonce'], envelope['ciphertext']) != (other_envelope['nonce'], other_envelope['ciphertext'])
    other = type(store)(tmp_path / 'other-owner')
    other.save_datasource({'datasource_id': 'remote', 'kind': 'custom', 'title': '隔离'}, secret_payload=secrets)
    assert (path.parent / '.vault_key').read_bytes() != (other.owner_home / 'data/datasource_secrets/.vault_key').read_bytes()


# LLM: 直接改真实私有信封的一个字段；错误必须是固定PermissionError而不是继续把损坏信封当旧秘密。
# 函数用途: 覆盖MAC、密文、nonce、键名以及schema/算法降级篡改。
@pytest.mark.parametrize('field,value', [
    ('mac', '0' * 64), ('ciphertext', 'AA=='), ('nonce', 'invalid-base64!'),
    ('credential_keys', ['forged']), ('schema_version', 'datasource_vault.v0'), ('alg', 'plaintext'),
])
def test_wp16_vault_tampering_fails_closed(store, field, value):
    saved = store.save_datasource({'datasource_id': 'tamper', 'kind': 'custom', 'title': '篡改'},
                                  secret_payload={'password': 'unit-wp16-authenticated'})
    path = store.owner_home / 'data/datasource_secrets' / (saved['credential_ref'] + '.json')
    data = json.loads(path.read_text())
    data[field] = value
    path.write_text(json.dumps(data))
    with pytest.raises(PermissionError, match='DATASOURCE_VAULT_CORRUPTED'):
        store.resolve_secret(saved['credential_ref'])


# LLM: 旧明文只读保持兼容，保存引用时必须原位加密，不能新建引用后把旧明文留在磁盘。
# 函数用途: 验证旧记录透明读取及成功保存后的同引用密文升级。
def test_wp16_legacy_secret_is_encrypted_on_save(store):
    ref, secrets = 'dscred-legacy-wp16', {'password': 'unit-wp16-legacy-password'}
    store._write(store.owner_home, ('data', 'datasource_secrets', ref + '.json'), secrets)
    path = store.owner_home / 'data/datasource_secrets' / (ref + '.json')
    assert store.resolve_secret(ref) == secrets
    assert not (path.parent / '.vault_key').exists()
    saved = store.save_datasource({'datasource_id': 'legacy', 'kind': 'custom', 'title': '旧记录', 'credential_ref': ref})
    assert saved['credential_ref'] == ref
    assert secrets['password'] not in path.read_text()
    assert json.loads(path.read_text())['schema_version'] == 'datasource_vault.v1'
    assert store.resolve_secret(ref) == secrets


# LLM: 没有原主密钥的密文不能靠自动重建钥匙或旧格式fallback恢复成健康记录。
# 函数用途: 验证丢失主密钥后拒绝读取且不生成替代密钥。
def test_wp16_missing_vault_key_does_not_recreate_it(store):
    saved = store.save_datasource({'datasource_id': 'lost', 'kind': 'custom', 'title': '丢钥匙'},
                                  secret_payload={'password': 'unit-wp16-lost-key'})
    key = store.owner_home / 'data/datasource_secrets/.vault_key'
    key.unlink(missing_ok=True)
    with pytest.raises(PermissionError, match='DATASOURCE_VAULT_CORRUPTED'):
        store.resolve_secret(saved['credential_ref'])
    assert not key.exists()


# LLM: Master key由同Owner多个真实线程首次保存共同创建，不能各自覆盖密钥而产生不可解的已发布凭据。
# 函数用途: 验证并发首次保存始终使用一个持久主密钥。
def test_wp16_parallel_first_saves_share_one_key(store):
    from concurrent.futures import ThreadPoolExecutor
    # LLM: 每个工作使用独立规格ID与合成秘密，避免业务CAS冲突掩盖密钥创建竞争。
    # 函数用途: 并行保存并立即解密一次正常凭据。
    def save(index):
        secret = {'password': f'unit-wp16-parallel-{index}'}
        saved = store.save_datasource({'datasource_id': f'ds-{index}', 'kind': 'custom', 'title': '并发'}, secret_payload=secret)
        return saved['credential_ref'], secret
    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(save, range(12)))
    assert (store.owner_home / 'data/datasource_secrets/.vault_key').is_file()
    assert all(store.resolve_secret(ref) == secret for ref, secret in rows)


# LLM: 支持两个显式参数但冲突不可覆盖；先断言公开签名存在，把缺接口与权限拒绝区分开。
# 函数用途: 验证凭据别名合并、排序键名和实际秘密保留。
def test_wp16_credential_values_alias_merges_without_leaking(store):
    assert 'credential_values' in inspect.signature(store.save_datasource).parameters
    secrets = {'password': 'unit-wp16-primary', 'token': 'unit-wp16-alias'}
    saved = store.save_datasource({'datasource_id': 'alias', 'kind': 'custom', 'title': '别名'},
                                  secret_payload={'password': secrets['password']}, credential_values={'token': secrets['token']})
    assert saved['credential_keys'] == ['password', 'token']
    assert store.resolve_secret(saved['credential_ref']) == secrets
    assert all(value not in json.dumps(saved) for value in secrets.values())
    with pytest.raises(ValueError, match='DATASOURCE_SECRET_CONFLICT'):
        store.save_datasource({'datasource_id': 'conflict-alias', 'kind': 'custom', 'title': '冲突'},
                              secret_payload={'password': 'first'}, credential_values={'password': 'second'})


# LLM: 经真实模型schema及工具execute验证别名，不只调用Store或绕过preflight。
# 函数用途: 验证credential_values在工具公开合同与分派中都接通。
def test_wp16_tool_accepts_credential_values(store):
    tool = importlib.import_module('agent_py_agent.agent.tooling.datasources_tool').DatasourcesTool(store.owner_home)
    result = tool.execute({'action': 'save', 'spec_payload': {'datasource_id': 'tool-alias', 'kind': 'custom', 'title': '工具别名'},
                           'credential_values': {'password': 'unit-wp16-tool-alias'}})
    assert result.ok, result.output
    assert json.loads(result.output)['credential_keys'] == ['password']
    assert 'unit-wp16-tool-alias' not in result.output


# LLM: 数字也可能是口令；长短整数/负数/零/浮点秘密在紧凑JSON、冒号紧邻文本、嵌套同名键、迁移和子进程双流中都必须脱敏且不破坏顶层结构整型字段。
# 函数用途: 验证全部数值凭据及子进程输出的统一脱敏。
@pytest.mark.parametrize('secret', [0, 7, 42, -1, 76543129, 9876.125])
def test_wp16_numeric_credentials_redact_values_and_script_output(store, secret):
    from agent_py_agent.agent.workflows.script_runner import ScriptRunner
    database(store)
    spec = store.get_datasource_spec('orders')
    saved = store.save_datasource({**asdict(spec), 'description': f'PIN:{secret}', 'options': {'copy': secret, 'revision': secret, 'port': secret}},
                                  secret_payload={'pin': secret})
    assert saved['description'] == 'PIN:<redacted>'
    assert saved['options'] == {'copy': '<redacted>', 'revision': '<redacted>', 'port': '<redacted>'}
    assert saved['port'] == 0 and saved['revision'] >= 1
    persisted_spec = json.loads((store.owner_home / 'assets/datasources/orders.json').read_text())
    assert persisted_spec['description'] == 'PIN:<redacted>'
    assert persisted_spec['options'] == {'copy': '<redacted>', 'revision': '<redacted>', 'port': '<redacted>'}
    mig = store.migrate_datasource('orders', {'datasource_id': 'mig_target', 'kind': 'sqlite', 'title': '迁移',
        'database_path': str(store.owner_home / 'mig-target.sqlite'),
        'description': f'PIN:{secret}', 'options': {'copy': secret, 'revision': secret}})
    assert mig['target_spec']['description'] == 'PIN:<redacted>'
    assert mig['target_spec']['options'] == {'copy': '<redacted>', 'revision': '<redacted>'}
    script(store, f'import json, sys\nsys.stderr.write("ERR_PIN:{secret}\\n")\nprint(json.dumps({{"copy":{secret!r},"revision":{secret!r}}}, separators=(",", ":")))')
    result = ScriptRunner(store.owner_home, use_sandbox=False).run_script('report')
    assert result.ok and result.output_data == {'copy': '<redacted>', 'revision': '<redacted>'}
    assert result.stdout_preview.strip() == '{"copy":<redacted>,"revision":<redacted>}'
    assert result.stderr_preview.strip() == 'ERR_PIN:<redacted>'
    assert str(secret) not in result.stdout_preview and str(secret) not in result.stderr_preview


# LLM: 源引用和目标引用中的既有秘密、目标新提取秘密必须共同清洗迁移元数据，返回和落盘均检查。
# 函数用途: 防止迁移复用凭据时description/options泄漏认证。
def test_wp16_migration_masks_source_target_and_new_secret_values(store):
    database(store)
    source_secret, target_secret, new_secret = 'unit-wp16-source-ref', 'unit-wp16-target-ref', 'unit-wp16-target-new'
    store.save_datasource(asdict(store.get_datasource_spec('orders')), secret_payload={'password': source_secret})
    target = store.save_datasource({'datasource_id': 'remote-target', 'kind': 'custom', 'title': '目标引用'},
                                   secret_payload={'token': target_secret})
    result = store.migrate_datasource('orders', {'datasource_id': 'new', 'kind': 'sqlite', 'title': '迁移',
        'database_path': str(store.owner_home / 'wp16-new.sqlite'), 'credential_ref': target['credential_ref'],
        'description': source_secret + ' / ' + target_secret + ' / ' + new_secret,
        'options': {'copy': [source_secret, target_secret, new_secret]}, 'password': new_secret})
    assert result['state'] == 'READY_TO_SWITCH'
    persisted = store.owner_home / 'data/datasource_migrations' / (result['migration_id'] + '.json')
    for secret in (source_secret, target_secret, new_secret):
        assert secret not in json.dumps(result)
        assert secret not in persisted.read_text()


# LLM: 通过真实-I启动在第一次用户import前/后改写清单；chmod可突破文件位，但不能突破已验签冻结的SDK合同。
# 函数用途: 复现表白名单清空及只读升级攻击，并要求稳定权限错误。
@pytest.mark.parametrize('prime', [False, True])
@pytest.mark.parametrize('attack', ['tables', 'access'])
def test_wp16_manifest_tampering_cannot_escalate_sql(store, prime, attack):
    from agent_py_agent.agent.workflows.script_runner import ScriptRunner
    database(store, allowed_tables=['orders'])
    code = f'''import json,os
from pathlib import Path
if {prime!r}:
    from agent_py_agent.runtime_sdk import query_sql
    query_sql('orders', 'SELECT value FROM orders')
path=Path(os.environ['MY_AGENT_DATASOURCE_RUNTIME_MANIFEST'])
data=json.loads(path.read_text())
if {attack!r} == 'tables':
    data['bindings']['orders']['spec']['allowed_tables']=[]
else:
    data['bindings']['orders']['access']='read_write'
os.chmod(path,0o600)
path.write_text(json.dumps(data))
from agent_py_agent.runtime_sdk import query_sql
query_sql('orders', {'SELECT * FROM hidden' if attack == 'tables' else 'DELETE FROM orders'!r})
print(json.dumps({{'unexpected_success':True}}))
'''
    script(store, code)
    result = ScriptRunner(store.owner_home, use_sandbox=False).run_script('report')
    assert not result.ok
    assert 'DATASOURCE_INVALID_RUNTIME_MANIFEST' in result.stderr_preview
    assert not list((store.owner_home / 'data/script_exec').glob('*/datasources-*.json'))


# LLM: 检查真实运行时构造出的签名和权限，不预设HMAC实现正确；两个run必须各用新密钥。
# 函数用途: 验证400清单、run独立密钥与规范bindings的精确HMAC。
def test_wp16_manifest_readonly_and_signature_are_run_specific(store):
    from agent_py_agent.agent.workflows.datasource_runtime import ScriptDatasourceRuntime
    database(store)
    folder = store.owner_home / 'data/script_exec/wp16'
    folder.mkdir(parents=True)
    keys = []
    for _ in range(2):
        environment = {}
        runtime = ScriptDatasourceRuntime(store.owner_home, {'data_sources': declarations()}, folder, environment, 10, {})
        try:
            path = Path(environment['MY_AGENT_DATASOURCE_RUNTIME_MANIFEST'])
            assert path.stat().st_mode & 0o777 == 0o400
            key = environment.get('MY_AGENT_DATASOURCE_RUNTIME_SECRET')
            assert isinstance(key, str) and len(key) == 64
            keys.append(key)
            data = json.loads(path.read_text())
            canonical = json.dumps(data['bindings'], ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':')).encode()
            assert hmac.compare_digest(data['signature'], hmac.new(bytes.fromhex(key), canonical, hashlib.sha256).hexdigest())
        finally:
            runtime.close()
    assert keys[0] != keys[1]


# LLM: 用户代码开始前可信bootstrap必须完成验签/去签名密钥，嵌套bindings也必须只读，而非浅拷贝缓存。
# 函数用途: 验证内存快照不允许修改访问等级、表范围或options，且不向脚本保留重签密钥。
def test_wp16_frozen_bindings_and_removed_signing_key_in_real_script(store):
    from agent_py_agent.agent.workflows.script_runner import ScriptRunner
    database(store, allowed_tables=['orders'], options={'nested': {'value': 1}})
    script(store, '''import json,os
from agent_py_agent.agent.workflows.datasource_runtime import current_datasource_bindings
bindings=current_datasource_bindings()
denied=0
for target,key,value in ((bindings['orders'],'access','read_write'),
                         (bindings['orders']['spec'],'allowed_tables',[]),
                         (bindings['orders']['spec']['options']['nested'],'value',2)):
    try:
        target[key]=value
    except TypeError:
        denied+=1
print(json.dumps({'denied':denied,'key_present':'MY_AGENT_DATASOURCE_RUNTIME_SECRET' in os.environ}))
''')
    result = ScriptRunner(store.owner_home, use_sandbox=False).run_script('report')
    assert result.ok, result.stderr_preview
    assert result.output_data == {'denied': 3, 'key_present': False}


# LLM: HMAC只认证初始清单；freeze后不应再相信新增key或替换环境指针去绑定另一个伪造清单。
# 函数用途: 验证换路径/删除清单不能让SDK放弃原冻结上下文或静默回退。
@pytest.mark.parametrize('change', ['replace_path', 'remove_file'])
def test_wp16_frozen_manifest_cannot_be_rebound_or_removed(store, change):
    from agent_py_agent.agent.workflows.script_runner import ScriptRunner
    database(store)
    script(store, f'''import os,json
from pathlib import Path
from agent_py_agent.runtime_sdk import query_sql
query_sql('orders','SELECT 1')
path=Path(os.environ['MY_AGENT_DATASOURCE_RUNTIME_MANIFEST'])
if {change!r} == 'remove_file':
    path.unlink()
else:
    replacement=path.with_name('forged.json')
    replacement.write_text(path.read_text())
    os.environ['MY_AGENT_DATASOURCE_RUNTIME_MANIFEST']=str(replacement)
query_sql('orders','SELECT 1')
print(json.dumps({{'unexpected_success':True}}))
''')
    result = ScriptRunner(store.owner_home, use_sandbox=False).run_script('report')
    assert not result.ok and 'DATASOURCE_INVALID_RUNTIME_MANIFEST' in result.stderr_preview


# LLM: 公开SDK句柄和Store应使用同一个credential_keys投影，不能为补键名而暴露原始值。
# 函数用途: 验证SDK摘要的凭据键名与嵌套只读options兼容。
def test_wp16_sdk_handle_config_keeps_keys_and_redacts_known_values(store):
    from agent_py_agent.agent.workflows.datasource_runtime import bind_runtime_datasources
    from agent_py_agent.runtime_sdk import get_datasource
    database(store, options={'nested': {'value': 1}})
    store.save_datasource(asdict(store.get_datasource_spec('orders')), secret_payload={'token': 'unit-wp16-sdk-secret'})
    with bind_runtime_datasources(store, declarations()):
        summary = get_datasource('orders').config
        assert summary['credential_keys'] == ['token']
        assert summary['options'] == {'nested': {'value': 1}}
        assert 'unit-wp16-sdk-secret' not in json.dumps(summary)


# LLM: 仅在真实SQLite connect窗口交换临时普通文件；必须关闭已打开的错误连接，不返回任何错误库数据。
# 函数用途: 复现FD核验后路径替换，并验证只读/读写两条打开路径都拒绝。
@pytest.mark.parametrize('read_only', [False, True])
def test_wp16_sqlite_replacement_closes_connection(store, read_only, monkeypatch):
    import agent_py_agent.agent.assets.datasource_store as module
    path = database(store)
    spec = store.get_datasource_spec('orders')
    replacement = store.owner_home / 'replacement.sqlite'
    original_connect, connections = sqlite3.connect, []
    with original_connect(replacement) as conn:
        conn.execute('CREATE TABLE stolen(value TEXT)')
    # LLM: 故障注入保留真实sqlite3.connect，只控制路径替换发生在校验与打开之间。
    # 函数用途: 打开已经被换成另一个inode的实际数据库并记录句柄。
    def swap_then_connect(*args, **kwargs):
        os.replace(replacement, path)
        conn = original_connect(*args, **kwargs)
        connections.append(conn)
        return conn
    monkeypatch.setattr(module.sqlite3, 'connect', swap_then_connect)
    with pytest.raises(PermissionError, match='DATASOURCE_SQLITE_PATH_REPLACED'):
        module.open_sqlite(spec, read_only=read_only)
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        connections[0].execute('SELECT 1')


# LLM: 在真实connect回调内fstat验证原FD仍存活，返回连接后原FD必须回收；不把连接成功当FD身份核验通过。
# 函数用途: 验证SQLite打开期间持有已验证普通文件FD及正常查询不回归。
def test_wp16_sqlite_holds_verified_fd_through_connect(store, monkeypatch):
    import agent_py_agent.agent.assets.datasource_store as module
    path = database(store)
    spec = store.get_datasource_spec('orders')
    expected = path.stat()
    original_fstat, original_connect, descriptors = os.fstat, sqlite3.connect, []
    # LLM: 观察真实fstat，不替换os.open（其身份属于supports_dir_fd能力检测），也不伪造元数据。
    # 函数用途: 记录真正指向初始数据库inode的FD且不扰动平台能力探测。
    def observed(fd):
        actual = original_fstat(fd)
        if (actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino) and fd not in descriptors:
            descriptors.append(fd)
        return actual
    # LLM: 只在连接窗口断言FD存在且仍指向原文件，再执行真实SQLite连接。
    # 函数用途: 核对FD生命周期跨过connect而不是预先关闭。
    def connected(*args, **kwargs):
        assert descriptors, 'SQLITE_VERIFIED_FD_MISSING_AT_CONNECT'
        assert original_fstat(descriptors[0]).st_ino == expected.st_ino
        return original_connect(*args, **kwargs)
    monkeypatch.setattr(module.os, 'fstat', observed)
    monkeypatch.setattr(module.sqlite3, 'connect', connected)
    conn = module.open_sqlite(spec, read_only=True)
    try:
        assert conn.execute('SELECT count(*) FROM orders').fetchone() == (2,)
    finally:
        conn.close()
    for fd in descriptors:
        with pytest.raises(OSError):
            os.fstat(fd)


# LLM: 已存在但权限过宽的主密钥也须收紧，不能只给首次os.open提供mode而遗漏复用路径。
# 函数用途: 验证保存前已有0644主密钥会变为0600，且密钥内容不被轮换。
def test_wp16_existing_vault_key_permissions_are_tightened(store):
    key = store.owner_home / 'data/datasource_secrets/.vault_key'
    key.parent.mkdir(parents=True)
    key.write_text('ab' * 32)
    key.chmod(0o644)
    saved = store.save_datasource({'datasource_id': 'permissions', 'kind': 'custom', 'title': '权限'},
                                  secret_payload={'password': 'unit-wp16-key-permissions'})
    assert key.stat().st_mode & 0o777 == 0o600
    assert key.read_text() == 'ab' * 32
    assert store.resolve_secret(saved['credential_ref']) == {'password': 'unit-wp16-key-permissions'}


# LLM: 即使秘密叶路径正常，主密钥本身的symlink也不得跟随，外部目标不发生任何写入。
# 函数用途: 验证主密钥nofollow与固定Owner边界。
def test_wp16_symlinked_master_key_is_rejected(store):
    key = store.owner_home / 'data/datasource_secrets/.vault_key'
    key.parent.mkdir(parents=True)
    outside = store.owner_home.parent / 'outside.key'
    outside.write_text('cd' * 32)
    key.symlink_to(outside)
    with pytest.raises(OSError):
        store.save_datasource({'datasource_id': 'symlink-key', 'kind': 'custom', 'title': '链接'},
                              secret_payload={'password': 'unit-wp16-key-symlink'})
    assert outside.read_text() == 'cd' * 32
    assert not list(key.parent.glob('dscred-*.json'))


# LLM: 补足首次加载的拒绝分支；只重置测试进程内部槽位以隔离用例，不替代真实子进程bootstrap验收。
# 函数用途: 验证签名、初始有效期、JSON/类型及临时密钥损坏均是固定权限错误。
@pytest.mark.parametrize('fault', ['signature', 'expiry', 'json', 'key', 'missing_key', 'relative', 'schema', 'expiry_type', 'bindings'])
def test_wp16_invalid_initial_manifest_fails_closed(store, monkeypatch, fault):
    from agent_py_agent.agent.workflows import datasource_runtime as runtime
    database(store)
    folder = store.owner_home / 'data/script_exec/invalid'
    folder.mkdir(parents=True)
    environment = {}
    active = runtime.ScriptDatasourceRuntime(store.owner_home, {'data_sources': declarations()}, folder, environment, 10, {})
    path = Path(environment[runtime.MANIFEST_ENV])
    data = json.loads(path.read_text())
    fields = {'signature': ('signature', '0' * 64), 'expiry': ('expires_at', 0), 'schema': ('schema_version', 2),
              'expiry_type': ('expires_at', 'forever'), 'bindings': ('bindings', [])}
    if fault in fields:
        key, value = fields[fault]
        data[key] = value
    path.chmod(0o600)
    path.write_text('{' if fault == 'json' else json.dumps(data))
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(runtime, '_FROZEN_MANIFEST', None)
    if fault == 'key':
        monkeypatch.setenv(runtime.MANIFEST_SECRET_ENV, 'not-a-key')
    elif fault == 'missing_key':
        monkeypatch.delenv(runtime.MANIFEST_SECRET_ENV)
    elif fault == 'relative':
        monkeypatch.setenv(runtime.MANIFEST_ENV, path.name)
    try:
        with pytest.raises(PermissionError, match='DATASOURCE_INVALID_RUNTIME_MANIFEST'):
            runtime.current_datasource_bindings()
    finally:
        active.close()


# LLM: 只在不可稳定制造的驱动connect错误处注入异常；nofollow打开与FD清理都是真实执行，不改平台能力判断。
# 函数用途: 验证连接失败也会回收已验证数据库FD。
def test_wp16_sqlite_connect_failure_releases_verified_fd(store, monkeypatch):
    import agent_py_agent.agent.assets.datasource_store as module
    database(store)
    spec = store.get_datasource_spec('orders')
    original, descriptors = module.open_readonly_file_beneath, []
    # LLM: 完全透传原nofollow入口，记录其真实返回的FD供失败后核对。
    # 函数用途: 观察实际数据库FD，不创建假描述符。
    def observed(*args, **kwargs):
        fd = original(*args, **kwargs)
        descriptors.append(fd)
        return fd
    # LLM: 原生驱动故障无法可靠强制，替身仅抛同类型异常，不接管FD处理。
    # 函数用途: 在connect窗口拒绝连接以覆盖finally。
    def failed(*args, **kwargs):
        raise sqlite3.OperationalError('unit-connect-refused')
    monkeypatch.setattr(module, 'open_readonly_file_beneath', observed)
    monkeypatch.setattr(module.sqlite3, 'connect', failed)
    with pytest.raises(sqlite3.OperationalError, match='unit-connect-refused'):
        module.open_sqlite(spec)
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


# LLM: shell可启动尚未预载SDK的Python；只用临时Owner合成绑定，完整重签后走当前checkout的真实SDK。
# 函数用途: 验证非Python绑定在用户代码运行前拒绝，而不是把重签密钥交给shell。
@pytest.mark.parametrize('interpreter', ['sh', 'bash'])
@pytest.mark.parametrize('escalation', ['tables', 'access'])
def test_wp16_review_shell_cannot_resign_manifest(store, interpreter, escalation):
    import shlex
    import sys

    from agent_py_agent.agent.assets.descriptor import AssetDescriptor
    from agent_py_agent.agent.assets.script_store import ScriptStore
    from agent_py_agent.agent.workflows.script_runner import ScriptRunner
    path = database(store, allowed_tables=['orders'])
    source_root = str(Path(__file__).resolve().parents[2])
    mutation = "row['spec']['allowed_tables']=[]" if escalation == 'tables' else "row['access']='read_write'"
    sql = 'SELECT * FROM hidden' if escalation == 'tables' else "UPDATE orders SET value='unauthorized'"
    child = f'''import sys,os,json,hashlib,hmac
sys.path.insert(0,{source_root!r})
path=os.environ['MY_AGENT_DATASOURCE_RUNTIME_MANIFEST']
key=bytes.fromhex(os.environ['MY_AGENT_DATASOURCE_RUNTIME_SECRET'])
with open(path) as stream: data=json.load(stream)
row=data['bindings']['orders']
{mutation}
body=json.dumps(data['bindings'],ensure_ascii=False,allow_nan=False,sort_keys=True,separators=(',',':')).encode()
data['signature']=hmac.new(key,body,hashlib.sha256).hexdigest()
os.chmod(path,0o600)
with open(path,'w') as stream: json.dump(data,stream)
import agent_py_agent.runtime_sdk as sdk
assert sdk.__file__=={str(Path(__file__).resolve().parents[1] / 'runtime_sdk.py')!r}
print(json.dumps({{'rows':sdk.query_sql('orders',{sql!r})}}))
'''
    ScriptStore(store.owner_home).save_script(AssetDescriptor('shell-bound', 'script', '非Python绑定',
        data_sources=tuple(declarations())), shlex.quote(sys.executable) + ' -I -B -c ' + shlex.quote(child),
        interpreter=interpreter, entrypoint='main.sh')
    result = ScriptRunner(store.owner_home, use_sandbox=False).run_script('shell-bound')
    assert not result.ok and 'DATASOURCE_PYTHON_RUNTIME_REQUIRED' in result.stderr_preview
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT value FROM orders ORDER BY id').fetchall() == [('first',), ('second',)]
    assert not list((store.owner_home / 'data/script_exec').glob('*/datasources-*.json'))


# LLM: 无SDK连接的普通shell及旧被动查询来源仍可运行，但不能接收本run清单或重签密钥。
# 函数用途: 验证收窄连接绑定不破坏旧查询来源元数据和shell执行能力。
@pytest.mark.parametrize('interpreter', ['sh', 'bash'])
@pytest.mark.parametrize('legacy_sources', [False, True])
def test_wp16_review_plain_shell_has_no_runtime_signing_environment(store, interpreter, legacy_sources):
    from agent_py_agent.agent.assets.descriptor import AssetDescriptor, DataSourceSpec
    from agent_py_agent.agent.assets.script_store import ScriptStore
    from agent_py_agent.agent.workflows.script_runner import ScriptRunner
    code = '''if [ -n "${MY_AGENT_DATASOURCE_RUNTIME_SECRET:-}${MY_AGENT_DATASOURCE_RUNTIME_MANIFEST:-}" ]; then
printf '{"present":true}\n'
else
printf '{"present":false}\n'
fi
'''
    sources = (DataSourceSpec('logs-only', 'logs', query_scope={'active_appnames': ['A']}),) if legacy_sources else ()
    ScriptStore(store.owner_home).save_script(AssetDescriptor('shell-plain', 'script', '普通shell', data_sources=sources), code,
        interpreter=interpreter, entrypoint='main.sh')
    result = ScriptRunner(store.owner_home, use_sandbox=False).run_script('shell-plain')
    assert result.ok, result.stderr_preview
    assert result.output_data == {'present': False}


# LLM: 故障注入交换文件、父目录或祖先目录并保留真实连接；恢复原inode不能掩盖曾打开另一库。
# 函数用途: 覆盖只读/可写connect窗口的三类ABA替换并核对错误连接已关闭。
@pytest.mark.parametrize('read_only', [True, False])
@pytest.mark.parametrize('swap_part', ['file', 'parent', 'ancestor'])
def test_wp16_review_sqlite_aba_swap_is_rejected(store, monkeypatch, read_only, swap_part):
    import agent_py_agent.agent.assets.datasource_store as module
    path = database(store)
    replacement = database(store, 'replacement')
    if swap_part != 'file':
        original_dir, replacement_dir = store.owner_home / 'original-tree/leaf', store.owner_home / 'replacement-tree/leaf'
        for directory in (original_dir, replacement_dir):
            directory.mkdir(parents=True)
        path = path.replace(original_dir / 'orders.sqlite')
        replacement = replacement.replace(replacement_dir / 'orders.sqlite')
        spec = store.get_datasource_spec('orders')
        store.save_datasource({**asdict(spec), 'database_path': str(path)}, expected_revision=spec.revision)
    first, second = path, replacement
    if swap_part != 'file':
        first, second = path.parent, replacement.parent
        if swap_part == 'ancestor':
            first, second = first.parent, second.parent
    aside = path.with_name('original-aside.sqlite')
    if swap_part != 'file':
        aside = store.owner_home / 'aside-tree'
    original_connect, opened = sqlite3.connect, []
    # LLM: 在connect前换入B，真实打开后恢复A；不伪造stat、fstat或连接返回对象。
    # 函数用途: 制造后置路径身份相同但SQLite实际打开B的时序。
    def connect_then_restore(*args, **kwargs):
        os.replace(first, aside)
        os.replace(second, first)
        try:
            conn = original_connect(*args, **kwargs)
            opened.append(conn)
        finally:
            os.replace(first, second)
            os.replace(aside, first)
        return conn
    monkeypatch.setattr(module.sqlite3, 'connect', connect_then_restore)
    with pytest.raises(PermissionError, match='DATASOURCE_SQLITE_PATH_REPLACED'):
        module.open_sqlite(store.get_datasource_spec('orders'), read_only=read_only)
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        opened[0].execute('SELECT 1')


# LLM: 失钥不是首次初始化；新引用也不得生成另一代密钥，更不得提交无法恢复的一组混合密文。
# 函数用途: 验证已有密文且主密钥丢失时新数据源写入拒绝并保留原密文。
def test_wp16_review_missing_master_key_blocks_new_secret_write(store):
    first = store.save_datasource({'datasource_id': 'first', 'kind': 'custom', 'title': '第一份'},
                                 secret_payload={'pin': 'unit-review-first-pin'})
    root = store.owner_home / 'data/datasource_secrets'
    record = root / (first['credential_ref'] + '.json')
    before = record.read_bytes()
    (root / '.vault_key').unlink()
    with pytest.raises(PermissionError, match='DATASOURCE_VAULT_CORRUPTED'):
        store.save_datasource({'datasource_id': 'second', 'kind': 'custom', 'title': '第二份'},
                              secret_payload={'pin': 'unit-review-second-pin'})
    assert not (root / '.vault_key').exists()
    assert not (store.owner_home / 'assets/datasources/second.json').exists()
    assert record.read_bytes() == before and len(list(root.glob('dscred-*.json'))) == 1


# LLM: Python数值相等不代表秘密JSON表示相同；别名冲突必须在规格落盘前拒绝，不丢弃任一原值。
# 函数用途: 验证数字、布尔与嵌套数字的表示差异不能绕过凭据冲突门。
@pytest.mark.parametrize('first,second', [(123, 123.0), (123.0, 123), (True, 1), ([123], [123.0])])
def test_wp16_review_alias_representation_conflict_fails_before_write(store, first, second):
    with pytest.raises(ValueError, match='DATASOURCE_SECRET_CONFLICT'):
        store.save_datasource({'datasource_id': 'ambiguous', 'kind': 'custom', 'title': '冲突', 'description': str(first)},
                              secret_payload={'pin': first}, credential_values={'pin': second})
    assert not (store.owner_home / 'assets/datasources/ambiguous.json').exists()
