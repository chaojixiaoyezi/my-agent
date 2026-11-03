"""R9-R2 P1-R9-09：可写 SDK 事务与迁移切换/规格保存经物理库闸门协调。

真实 DatasourceStore 迁移与保存、真实 SDK query_sql 事务及 ScriptRunner 子进程；只在原调用链的
open_sqlite / sqlite_fingerprint / _write 处暂停一次，构造受控的正常切换交错。不变量：已确认的写入
要么包含在切换后的当前库中，要么阻止切换；切换期间到达的写者不能提交到被切走的旧库。
"""
import json
import os
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agent_py_agent.agent.assets import datasource_store
from agent_py_agent.agent.assets.datasource_store import DatasourceStore
from agent_py_agent.agent.assets.descriptor import AssetDescriptor, DataSourceSpec
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.attempt import sandbox as attempt
from agent_py_agent.agent.tooling import sandbox as tooling_sandbox
from agent_py_agent.agent.workflows import datasource_runtime as runtime
from agent_py_agent.agent.workflows import runtime_sdk as sdk
from agent_py_agent.agent.workflows import script_runner
from agent_py_agent.agent.workflows.script_runner import ScriptRunner

WRITE = "INSERT INTO orders VALUES(3,'third')"
READ_WRITE = [DataSourceSpec('orders', 'sqlite', query_scope={'access': 'read_write'})]


def _create_database(path):
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript("CREATE TABLE orders(id INTEGER PRIMARY KEY, value TEXT NOT NULL);"
                           "INSERT INTO orders VALUES(1,'first'),(2,'second');")
        conn.commit()


@pytest.fixture
def store(tmp_path):
    store = DatasourceStore(tmp_path / 'owner')
    path = store.owner_home / 'orders.sqlite'
    path.parent.mkdir(parents=True)
    _create_database(path)
    store.save_datasource({'datasource_id': 'orders', 'kind': 'sqlite', 'title': '订单', 'database_path': str(path)})
    return store


def _rows(path):
    with closing(sqlite3.connect(path)) as conn:
        return [row[0] for row in conn.execute('SELECT id FROM orders ORDER BY id')]


def _prepare(store):
    prepared = store.migrate_datasource('orders', {'datasource_id': 'new', 'kind': 'sqlite', 'title': '新库',
                                                   'database_path': str(store.owner_home / 'new.sqlite')})
    assert prepared['state'] == 'READY_TO_SWITCH'
    return prepared


def _thread(target, outcome):
    def run():
        try:
            outcome['result'] = target()
        except Exception as exc:
            outcome['error'] = exc
    return threading.Thread(target=run)


def _sdk_write(store):
    # 原 SDK 可写事务：线程内绑定 read_write 合同；query_sql 正常返回即为已确认写入。
    with runtime.bind_runtime_datasources(store, READ_WRITE):
        return sdk.query_sql('orders', WRITE)


def _switch(store, prepared):
    return lambda: store.migrate_datasource('orders', {'migration_id': prepared['migration_id']}, mode='switch')


def test_in_flight_write_is_included_or_prevents_the_switch(store, monkeypatch):
    prepared = _prepare(store)
    original = store.get_datasource_spec('orders').database_path
    opened, release = threading.Event(), threading.Event()
    real_open = sdk.open_sqlite

    def paused_open(spec, *, read_only=False):
        connection = real_open(spec, read_only=read_only)
        if not read_only:
            # 写者已通过复核并打开连接，在事务提交前暂停。
            opened.set()
            release.wait(10)
        return connection

    monkeypatch.setattr(sdk, 'open_sqlite', paused_open)
    written, switched = {}, {}
    writer, switch = _thread(lambda: _sdk_write(store), written), _thread(_switch(store, prepared), switched)
    try:
        writer.start()
        assert opened.wait(10)
        switch.start()
        switch.join(0.5)
        # 写者持共享闸门期间，切换只能等待其提交/回滚并关闭连接。
        assert switch.is_alive()
    finally:
        release.set()
        writer.join(10)
        switch.join(30)

    assert written == {'result': []}
    assert switched['result']['state'] == 'FAILED'
    assert store.get_datasource_spec('orders').database_path == original
    assert _rows(original) == [1, 2, 3]


def test_writer_arriving_during_switch_cannot_commit_to_the_retired_database(store, monkeypatch):
    prepared = _prepare(store)
    original = store.get_datasource_spec('orders').database_path
    paused, release, calls = threading.Event(), threading.Event(), []
    real_fingerprint = datasource_store.sqlite_fingerprint

    def paused_fingerprint(connection):
        result = real_fingerprint(connection)
        calls.append(result)
        if len(calls) == 1:
            # 切换已取得源库最终指纹，尚未提交规格。
            paused.set()
            release.wait(10)
        return result

    monkeypatch.setattr(datasource_store, 'sqlite_fingerprint', paused_fingerprint)
    written, switched = {}, {}
    writer, switch = _thread(lambda: _sdk_write(store), written), _thread(_switch(store, prepared), switched)
    try:
        switch.start()
        assert paused.wait(10)
        writer.start()
        writer.join(0.5)
        # 切换方持排他闸门，后到写者在共享闸门前等待。
        assert writer.is_alive()
    finally:
        release.set()
        switch.join(30)
        writer.join(30)

    assert switched['result']['state'] == 'SWITCHED'
    assert 'result' not in written and isinstance(written['error'], PermissionError)
    assert str(written['error']) == 'DATASOURCE_BINDING_CHANGED'
    current = store.get_datasource_spec('orders').database_path
    assert current != original and _rows(original) == [1, 2] and _rows(current) == [1, 2]
    # 切换后重新签发的绑定写入当前库。
    assert _sdk_write(store) == [] and _rows(current) == [1, 2, 3] and _rows(original) == [1, 2]


def test_writer_arriving_during_repointing_save_cannot_commit_to_the_old_database(store, monkeypatch):
    original = store.get_datasource_spec('orders').database_path
    replacement = store.owner_home / 'replacement.sqlite'
    _create_database(replacement)
    paused, release = threading.Event(), threading.Event()
    real_write = store._write

    def paused_write(root, parts, data):
        if parts[-1] == 'orders.json' and not paused.is_set():
            # 保存已持闸门与规格锁，规格尚未提交。
            paused.set()
            release.wait(10)
        return real_write(root, parts, data)

    monkeypatch.setattr(store, '_write', paused_write)
    written, saved = {}, {}
    payload = {'datasource_id': 'orders', 'kind': 'sqlite', 'title': '订单', 'database_path': str(replacement)}
    saver, writer = _thread(lambda: store.save_datasource(payload), saved), _thread(lambda: _sdk_write(store), written)
    try:
        saver.start()
        assert paused.wait(10)
        writer.start()
        writer.join(0.5)
        assert writer.is_alive()
    finally:
        release.set()
        saver.join(30)
        writer.join(30)

    assert saved['result']['database_path'] == str(replacement)
    assert str(written.get('error')) == 'DATASOURCE_BINDING_CHANGED' and 'result' not in written
    assert _rows(original) == [1, 2] and _rows(replacement) == [1, 2]


def test_gate_identity_follows_the_physical_database_and_the_exact_spec_leaf(store):
    spec = store.get_datasource_spec('orders')
    control = store.writer_control(spec)
    path = Path(spec.database_path)
    alias = path.with_name(path.name.upper())
    if alias.exists() and os.path.samefile(path, alias):
        # 大小写不敏感文件系统上的别名路径落在同一物理闸门。
        assert datasource_store.database_gate_facts(store.owner_home, alias) == datasource_store.database_gate_facts(
            store.owner_home, path)
    with datasource_store.shared_writer_gate(control):
        pass
    # 原子替换后残留的旧规格叶（链接数 0，正如过期的单叶挂载所见）不能通过复核。
    stale = store.owner_home / 'stale.json'
    stale.write_bytes(Path(control['spec_path']).read_bytes())
    descriptor = os.open(stale, os.O_RDONLY)
    try:
        stale.unlink()
        with pytest.raises(PermissionError, match='DATASOURCE_BINDING_CHANGED'):
            with datasource_store.shared_writer_gate({**control, 'spec_path': f'/dev/fd/{descriptor}'}):
                pass
    finally:
        os.close(descriptor)
    # 任一规格变化（即便不换库）都使旧签发事实失效，失败关闭。
    store.save_datasource({'datasource_id': 'orders', 'kind': 'sqlite', 'title': '订单-改名',
                           'database_path': spec.database_path})
    with pytest.raises(PermissionError, match='DATASOURCE_BINDING_CHANGED'):
        with datasource_store.shared_writer_gate(control):
            pass


_SCRIPT = '''import json, os
from pathlib import Path
from agent_py_agent.runtime_sdk import query_sql
manifest = json.loads(Path(os.environ["MY_AGENT_DATASOURCE_RUNTIME_MANIFEST"]).read_text())
control = manifest["bindings"]["orders"]["control"]
report = {}
for name in ("spec_path", "gate_path"):
    try:
        with open(control[name], "a"):
            report[name] = "writable"
    except OSError:
        report[name] = "denied"
probe = Path(control["spec_path"]).parent.parent / "probe.txt"
probe.write_text("broader write root")
report["probe"] = probe.read_text()
try:
    report["rows"] = query_sql("orders", "INSERT INTO orders VALUES(3,'third')")
except Exception as exc:
    report["write_error"] = type(exc).__name__
print(json.dumps(report))
'''


# LLM: 直接调用正式bwrap构造器，跨平台覆盖Linux常见的/tmp权威根；不宣称实际施加了Linux内核隔离。
# 函数用途: 验证临时区重映射不改选人格权威，较宽写根中的控制叶仍以精确只读挂载获胜。
def test_bwrap_script_controls_with_persona_authority_beneath_tmp():
    with TemporaryDirectory(prefix="ci3-bwrap-authority-", dir="/tmp") as directory:
        home = Path(directory) / "owner"
        work = home / "tmp/script_exec/report"
        work.mkdir(parents=True)
        broader = (home / "assets", home / "data")
        leaves = (broader[0] / "datasources/orders.json", broader[1] / "writers/gate.lock")
        for leaf in leaves:
            leaf.parent.mkdir(parents=True)
            leaf.write_text("host control")
        (home / "AGENTS.md").write_text("original authority")
        spec = tooling_sandbox.SandboxSpec(owner_home=work, workspace=work,
            protected_persona_root=home, write_roots=(work, *broader), write_scope_denied_roots=(home,),
            read_only_paths=leaves, bwrap_path="/fixture/bwrap")
        argv = tooling_sandbox.build_bwrap_argv(spec)
        mounts = tooling_sandbox._mount_snapshot(argv)
        for leaf in leaves:
            mount = tooling_sandbox._effective_mount(leaf.resolve(), mounts)
            assert mount is not None and not mount.writable and mount.source == leaf.resolve()
        probe_mount = tooling_sandbox._effective_mount((broader[0] / "probe.txt").resolve(), mounts)
        assert probe_mount is not None and probe_mount.writable
        frozen = tooling_sandbox._freeze_persona_root(spec)
        assert frozen.authority_root == home.resolve()
        assert (home / "AGENTS.md").read_text() == "original authority"


# LLM: 非原生scratch的异名覆盖仍必须失败关闭；不能为修复/tmp合法底图移除changed-authority闸。
# 函数用途: 防止显式换源挂载借临时目录例外冒充原人格权威。
def test_bwrap_rejects_non_scratch_persona_authority_replacement(tmp_path, monkeypatch):
    home, replacement = tmp_path / "owner", tmp_path / "replacement"
    home.mkdir()
    replacement.mkdir()
    original = tooling_sandbox._build_bwrap_base_argv

    def replaced_root(spec):
        argv = original(spec)
        argv[-2:-2] = ["--bind", str(replacement), str(home)]
        return argv

    monkeypatch.setattr(tooling_sandbox, "_build_bwrap_base_argv", replaced_root)
    spec = tooling_sandbox.SandboxSpec(owner_home=home, workspace=home,
        protected_persona_root=home, bwrap_path="/fixture/bwrap")
    with pytest.raises(tooling_sandbox.SandboxUnavailable, match="PATH_AGENTS_SOURCE_PROTECTION_INVALID"):
        tooling_sandbox.build_bwrap_argv(spec)


@pytest.mark.parametrize('use_sandbox', [False, True])
def test_script_writer_control_leaves_stay_read_only_under_broader_write_roots(store, use_sandbox):
    if use_sandbox:
        if script_runner._outer_seatbelt_active():
            pytest.skip('外层 Seatbelt 禁止嵌套 sandbox_apply；未验证内层原生执行')
        try:
            attempt.AttemptExecutionSandbox(attempt.AttemptSandboxSpec(
                attempt_view=store.owner_home, staging_root=store.owner_home, shared_workspace=store.owner_home,
                owner_home=store.owner_home, network_access=False)).require_ready()
        except attempt.SandboxUnavailableError as exc:
            pytest.skip(f'原生沙箱未就绪：{exc}')
    ScriptStore(store.owner_home).save_script(AssetDescriptor('report', 'script', '报表', data_sources=tuple(READ_WRITE)),
                                              _SCRIPT)
    spec = store.get_datasource_spec('orders')
    leaf = store.owner_home / 'assets' / 'datasources' / 'orders.json'
    before = leaf.read_bytes()
    broader = (store.owner_home / 'assets', store.owner_home / 'data')

    result = ScriptRunner(store.owner_home, use_sandbox=use_sandbox, strict_sandbox=use_sandbox,
                          restrict_to_roots=True, extra_write_roots=broader).run_script('report')

    assert result.ok, result.stderr_preview
    report = result.output_data
    # 更宽写根确实可写，但签发的精确规格/闸门叶仍只读。
    assert (report['spec_path'], report['gate_path'], report['probe']) == ('denied', 'denied', 'broader write root')
    assert leaf.read_bytes() == before
    if use_sandbox and 'write_error' in report:
        # 已知平台阻塞（基线同样存在）：macOS Seatbelt 拒绝 open_sqlite 的逐段 nofollow 目录遍历，只读查询也失败；
        # 此时必须在任何写入之前失败。
        assert _rows(spec.database_path) == [1, 2]
    else:
        assert report['rows'] == [] and _rows(spec.database_path) == [1, 2, 3]
