"""Wave 3 consolidated tests (test_consolidated_c2_g2).

- test_rework_r9_datasource_switch_writers.py: R9-R2 P1-R9-09：可写 SDK 事务与迁移切换/规格保存经物理库闸门协调。
- test_rework_r9_native_business_control.py: R9-R2 P1-R9-02：W2 控制在原 live 授权后取准确 owner 完整宿主，租用跨整个控制并在所有路径释放。
- test_rework_r9_passwordless_sso.py: R9-R2 P1-R9-06：免密账号经可信SSO签发/验证会话；不创建密码、不开放免密密码登录。
- test_rework_r9_plugin_script_definition_pin.py: R9-R2 P1-R9-08 插件：准入后的活记录改写不能替换本次被调用的脚本正文。
- test_v13_w0_aliases.py: thread_id参数显式归一、真实会话消息和原W2任务直派回归。
- test_v13_w0_async_jobs.py: 通用模型作业经真实Gateway、W2派发和canonical会话任务推进。
- test_v13_w0_event_stream.py: W0事件流：原运行库、确定性时钟及真实HTTP共同验证有界交付与恢复。
"""
import hashlib
import json
import os
import socket
import sqlite3
import threading
import traceback
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import (
    Event,
    Thread,
)
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.assets import datasource_store
from agent_py_agent.agent.assets.datasource_store import DatasourceStore
from agent_py_agent.agent.assets.descriptor import (
    AssetDescriptor,
    DataSourceSpec,
)
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.attempt import sandbox as attempt
from agent_py_agent.agent.business_spaces.v13_store import V13Store
from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.conversation.session_tasks import SessionTaskUpdate
from agent_py_agent.agent.gateway_parts import (
    business_api as api,
)
from agent_py_agent.agent.gateway_parts import (
    business_api_v13 as v13,
)
from agent_py_agent.agent.gateway_parts.business_event_stream import stream_business_events
from agent_py_agent.agent.plugin_host.contracts import DomainResult
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.tooling import sandbox as tooling_sandbox
from agent_py_agent.agent.user_space import user_auth
from agent_py_agent.agent.user_space.local_user_lifecycle import LocalUserLifecycle
from agent_py_agent.agent.user_space.local_user_store import LocalUserStore
from agent_py_agent.agent.user_space.totp_sso_auth import (
    TotpSSOAuthService,
    VerifiedSSOIdentity,
    authentication_stamp,
    totp_code,
)
from agent_py_agent.agent.workflows import (
    datasource_runtime as runtime,
)
from agent_py_agent.agent.workflows import (
    engine as workflow_engine,
)
from agent_py_agent.agent.workflows import (
    runtime_sdk as sdk,
)
from agent_py_agent.agent.workflows import (
    script_runner,
)
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
from agent_py_agent.agent.workflows.script_runner import ScriptRunner
from agent_py_agent.tests import test_v13_w0_harness as harness
from agent_py_agent.tests.test_business_v12_f7_observation import fixture
from agent_py_agent.tests.test_mega_business_api_actions import (
    background_call,
    create_background,
    open_native_observation,
    seed_observed_runtime,
)
from agent_py_agent.tests.test_mega_business_api_actions import background_http as background_http
from agent_py_agent.tests.test_plugin_host_gateway_composition import (
    production_host as production_host,
)
from agent_py_agent.tests.test_rework_plugin_host_source_scope import (
    _EXPECTED,
    _call,
    _configure,
    _flow,
)
from agent_py_agent.tests.test_rework_plugin_host_source_scope import asset_case as asset_case
from agent_py_agent.tests.test_rework_plugin_host_source_scope import asset_root as asset_root
from agent_py_agent.tests.test_v13_w0_harness import (
    v13_call,
    v13_space,
)
from agent_py_agent.tests.test_v13_w0_recover_consumer import seed_task

# === Source: agent_py_agent/tests/test_rework_r9_datasource_switch_writers.py ===

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


# === Source: agent_py_agent/tests/test_rework_r9_native_business_control.py ===

USER = 'business-alice'


class _BlockingModel:
    """第一轮真实模型调用阻塞到测试放行；之后普通返回。"""

    name = 'r9-native-control-model'

    def __init__(self):
        self.calls = 0
        self.waiting, self.release, self.returned = Event(), Event(), Event()

    def probe_tool_capability(self):
        from agent_py_agent.agent.backends.base import ProviderToolCapability, _utc_now_iso
        return ProviderToolCapability(provider=self.name, endpoint='local://test', model='', stream=False,
            native_supported=True, evidence='contract_test', observed_at=_utc_now_iso())

    def generate(self, prompt, on_chunk=None, **kwargs):
        from agent_py_agent.agent.backends import ModelResponse
        self.calls += 1
        if self.calls == 1:
            self.waiting.set()
            assert self.release.wait(15), 'test did not release the original model call'
            self.returned.set()
        return ModelResponse(text='original result', backend=self.name)


# LLM: 限流属于每个真实HostTransport，不改生产配额或admit；只在测试安排阶段结束后清空该宿主桶。
# 函数用途: 让被测控制请求不受准备空间/线程所消费的写入额度干扰。
@pytest.fixture
def reset_business_limiter():
    def reset(server):
        limiter = server.plugin_host.limiter
        with limiter._lock:
            limiter._rows.clear()
    return reset


@pytest.fixture
def scoped_gateway(tmp_path, monkeypatch):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts.http_service import (
        GatewayHTTPServer,
        GatewayHTTPServerParams,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.settings import AgentConfig

    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'native-control-home'))
    base = SimpleAgent(AgentConfig(model_backend='echo', enable_tools=False, business_spaces_enabled=True,
        memory_path='memory.jsonl', request_timeout=0, prompt_files=[]), tmp_path / 'workspace')
    server = GatewayHTTPServer(0, gateway_paths_from_root(base.home_paths.root / 'gateway'),
        params=GatewayHTTPServerParams(agent=base))
    server.start()
    try:
        yield server, base
    finally:
        server.stop()


def _running_scoped_task(native):
    """经真实HTTP派发；原owner池实例上的原调度器把任务跑到阻塞的模型调用。"""
    from agent_py_agent.agent.conversation import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
    )
    from agent_py_agent.agent.gateway_parts.request_worker import _owner_pool
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    server, base = native
    params, agent = create_background(native)
    status, assigned = background_call(native, 'tasks.assign', {'agent_id': agent['agent_id'],
        'space_id': params['space_id'], 'instruction': '保持运行直到取消'}, idempotency_key='r9-control-task')
    assert status == 200, assigned
    pool = _owner_pool(base)
    owner = OwnerIdentity.provider_user('feishu', USER)
    owner_agent = pool.get(owner)
    owner_agent.backend = model = _BlockingModel()
    store = owner_agent.conversation_store
    scheduler = BackgroundMainAgentScheduler({'runtime': BackgroundMainAgentRuntime(agent=owner_agent, store=store),
                                              'store': store})
    outcome = {}

    def tick():
        try:
            outcome['reports'] = scheduler.tick()
        except BaseException as error:  # noqa: BLE001 - 交给测试线程断言
            outcome['error'] = error

    thread = Thread(target=tick, name='r9-native-control-slice')
    thread.start()
    case = SimpleNamespace(server=server, base=base, pool=pool, owner=owner, owner_agent=owner_agent, model=model,
        store=store, repo=owner_agent.subagents.runtime_db, thread=thread, outcome=outcome, view=agent,
        task_id=assigned['data']['task_id'])
    try:
        assert model.waiting.wait(15), 'original scheduler did not reach the model call'
        task = store.session_tasks.require(case.task_id)
        case.target = task.target_thread_id
        case.claim = store.claims.load(case.target)
        assert case.claim['status'] == 'running'
        case.authority = case.claim['runtime_authority']
    except BaseException:
        _finish(case)
        raise
    return case


def _finish(case):
    case.model.release.set()
    case.thread.join(timeout=15)
    assert not case.thread.is_alive(), 'original scheduler slice did not return'
    assert 'error' not in case.outcome, case.outcome.get('error')


def _control(case, action, key):
    """原冻结请求封套：tasks.cancel 必带原修订，执行取消只认准确 run/attempt。"""
    if action == 'executions.cancel':
        return {'params': {'run_id': case.authority['run_id'], 'attempt_id': case.authority['attempt_id'],
                           'include_descendants': True}, 'idempotency_key': key}
    task = case.store.session_tasks.require(case.task_id)
    return {'params': {'task_id': case.task_id, 'include_descendants': True}, 'idempotency_key': key,
            'expected_revision': int(task.updated_at * 1000000)}


def _call_control(native, case, action, key):
    request = _control(case, action, key)
    return background_call(native, action, request.pop('params'), **request)


def _pins(case):
    return case.pool._pins.get(id(case.owner_agent), 0)


@pytest.mark.parametrize('action', ['tasks.cancel', 'executions.cancel'])
def test_scoped_owner_control_runs_on_pinned_complete_host_and_releases(scoped_gateway, monkeypatch, action):
    from agent_py_agent.agent.gateway_parts import session_task_stop

    case = _running_scoped_task(scoped_gateway)
    try:
        observed = []
        original_stop = session_task_stop.stop_business_session_task

        def observe_stop(agent, task, **options):
            # 完整宿主就是原池里这一代准确owner实例，且在整个控制期间持有租用。
            observed.append((agent is case.owner_agent, case.pool._pins.get(id(agent), 0)))
            return original_stop(agent, task, **options)

        monkeypatch.setattr(session_task_stop, 'stop_business_session_task', observe_stop)
        key = 'r9-control-' + action.replace('.', '-')
        status, pending = _call_control(scoped_gateway, case, action, key)
        assert status == 200, pending
        assert pending['error'] is None and pending['data']['state'] == 'pending'
        assert observed == [(True, 1)]
        assert _pins(case) == 0, 'control lease must be released after the call'
        # 原运行权威已被关闭；passive宿主做不到这一步，只会永久pending。
        assert case.repo.get_agent_run(case.authority['agent_run_id'])['status'] == 'cancelled'
        control = case.store.session_tasks.require(case.task_id).metadata['cancellation_control']
        assert control['execution_ref']['observed_attempt_id'] == case.authority['attempt_id']
        assert not case.model.returned.is_set(), 'logical cancel must not report physical exit'
        _finish(case)
        status, done = _call_control(scoped_gateway, case, action, key)
        assert status == 200 and done['data']['state'] == 'completed', done
        assert _pins(case) == 0
    finally:
        case.model.release.set()
        case.thread.join(timeout=15)


def test_passive_running_detail_reads_observation_runtime_without_owner_host(scoped_gateway, monkeypatch):
    from agent_py_agent.agent.owner_scoped_pool import OwnerScopedAgentPool

    case = _running_scoped_task(scoped_gateway)
    try:
        def forbidden(*args, **kwargs):
            raise AssertionError('passive read must not acquire or construct an owner Agent')

        for name in ('get', 'acquire_pinned', '_build_once'):
            monkeypatch.setattr(OwnerScopedAgentPool, name, forbidden)
        status, fetched = background_call(scoped_gateway, 'background_agents.get', {'agent_id': case.view['agent_id']})
        assert status == 200, fetched
        current = fetched['data']['current_execution']
        assert current is not None and current['status'] == 'running'
        assert (current['run_id'], current['attempt_id']) == (case.authority['run_id'], case.authority['attempt_id'])
        assert current['task_id'] == case.task_id
        assert _pins(case) == 0
    finally:
        _finish(case)


def test_failed_control_and_post_lease_revocation_release_pin_without_bookkeeping(scoped_gateway, monkeypatch):
    from agent_py_agent.agent.conversation import background_agents
    from agent_py_agent.agent.gateway_parts import control_service
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    case = _running_scoped_task(scoped_gateway)
    try:
        before = case.store.session_tasks.require(case.task_id).to_dict()
        run = dict(case.repo.get_agent_run(case.authority['agent_run_id']))

        def failing_cancel(host, service, params, *, actor):
            assert host is case.owner_agent and case.pool._pins.get(id(host), 0) == 1
            raise OSError('simulated I/O error inside the original control')

        monkeypatch.setattr(background_agents, 'cancel_business_execution', failing_cancel)
        status, failed = _call_control(scoped_gateway, case, 'executions.cancel', 'r9-control-failure')
        assert status == 503 and failed['error']['code'] == 'OUTCOME_UNKNOWN', failed
        assert _pins(case) == 0, 'exception path must release the control lease'
        monkeypatch.undo()

        # 构造/租用期间授权变化：同一原live check在任何账本变更前拒绝，并释放刚取得的租用。
        revoked, original_owner, original_host = [], control_service.resolve_gateway_scope_owner, api.owner_complete_host

        def drifting_owner(base, scope):
            owner = original_owner(base, scope)
            return OwnerIdentity.provider_user('feishu', 'someone-else') if revoked else owner

        def leased_then_revoked(server, owner, stack):
            host = original_host(server, owner, stack)
            assert case.pool._pins.get(id(host), 0) == 1
            revoked.append(owner)
            return host

        monkeypatch.setattr(control_service, 'resolve_gateway_scope_owner', drifting_owner)
        monkeypatch.setattr(api, 'owner_complete_host', leased_then_revoked)
        status, denied = _call_control(scoped_gateway, case, 'tasks.cancel', 'r9-control-revoked')
        assert revoked and status == 403 and denied['error']['code'] == 'CAPABILITY_DENIED', denied
        assert denied['effect_outcome'] == 'not_started'
        assert _pins(case) == 0
        assert case.store.session_tasks.require(case.task_id).to_dict() == before
        assert dict(case.repo.get_agent_run(case.authority['agent_run_id'])) == run
    finally:
        _finish(case)


def test_registered_users_keep_original_ordinary_pool_rejection(scoped_gateway):
    from agent_py_agent.agent.gateway_parts.request_worker import _owner_pool
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    from agent_py_agent.agent.user_space.local_user_lifecycle import LocalUserLifecycle
    from agent_py_agent.agent.user_space.local_user_store import LocalUserStore
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    _, base = scoped_gateway
    LocalUserLifecycle(LocalUserStore(base.home_paths.root)).create('alice', idempotency_key='r9-control-alice')
    with pytest.raises(HostAPIError) as refused:
        with _owner_pool(base).acquire_pinned(OwnerIdentity('local', 'user', 'alice')):
            pass
    assert refused.value.code == 'HOST_NOT_READY'
    assert not _owner_pool(base)._pins


# LLM: 保留原HTTP、限流器及503/无账本副作用断言；清桶只隔开arrange与实际控制请求。
# 函数用途: 验证多进程协调器在任何业务写入前按HOST_NOT_READY拒绝控制。
def test_countn_coordinator_control_is_not_ready_before_any_bookkeeping(production_host, reset_business_limiter):
    from agent_py_agent.agent.plugin_host.business_actions import gateway_business_port
    from agent_py_agent.tests.test_mega_business_plugin_compatibility import plugin_observation_task
    from agent_py_agent.tests.test_plugin_host_v2_grants import http_request

    services, task = plugin_observation_task(production_host)
    server = production_host[0]
    store = services.host.conversation_store
    before = (store.session_tasks.require(task.task_id).to_dict(), store.wakes.pending(limit=0))
    # 多进程协调器：只有原控制面，没有业务Agent；读口仍冷可用，W2控制在接线和调用两侧都不可用。
    server.control_plane, server.agent = SimpleNamespace(config=server.agent.config, home_paths=server.agent.home_paths,
                                                         worker_count=2), None
    reset_business_limiter(server)
    status, denied = http_request(server, production_host[1], {'schema': 'plugin_host_call.v2',
        'action': 'business.tasks.cancel', 'idempotency_key': 'r9-countn-cancel',
        'payload': {'task_id': task.task_id, 'include_descendants': True, 'expected_revision': 0}})
    assert status == 503 and denied['error']['code'] == 'HOST_NOT_READY', denied
    assert denied['error']['outcome'] == 'not_started'
    assert (store.session_tasks.require(task.task_id).to_dict(), store.wakes.pending(limit=0)) == before
    # 冷读仍经同一上下文进入原领域，并按新接线事实重建本owner服务。
    status, listed = http_request(server, production_host[1], {'schema': 'plugin_host_call.v2',
        'action': 'business.spaces.list', 'payload': {}})
    assert status == 200 and listed['ok'], listed
    backend = gateway_business_port(server.plugin_host.composition.ports.conversation)
    assert backend.ready('business.tasks.cancel') is False
    assert backend.ready('business.executions.cancel') is False
    assert backend.ready('business.background_agents.get') is True
    [current] = [value for value in backend._services.values() if value.owner_id == services.owner_id]
    assert current.complete_host_wired is False and current.ready('tasks.cancel') is False
    assert current.ready('background_agents.get') is True


def test_native_child_guidance_and_plugin_async_exclusions_are_preserved(scoped_gateway):
    from agent_py_agent.agent.plugin_host.business_actions import GatewayBusinessDomain

    status, capabilities = background_call(scoped_gateway, 'system.capabilities')
    assert status == 200, capabilities
    supported = set(capabilities['data']['supported_actions'])
    assert {'subagents.messages.send', 'subagents.cancel', 'executions.cancel', 'tasks.cancel'} <= supported
    domain = GatewayBusinessDomain(SimpleNamespace(root=scoped_gateway[1].home_paths.root, server=scoped_gateway[0]),
                                   None)
    assert domain.ready('business.subagents.messages.send') is False
    assert domain.ready('business.background_agents.resume') is False
    assert domain.ready('business.subagents.cancel') is True


@pytest.mark.parametrize("run_status,attempt_status,current_attempt,accepted", [
    ("created", "running", "attempt", True),
    ("running", "running", "attempt", True),
    ("completed", "running", "attempt", False),
    ("cancelled", "running", "attempt", False),
    ("unknown", "running", "attempt", False),
    ("created", "created", "attempt", False),
    ("created", "completed", "attempt", False),
    ("created", "unknown", "attempt", False),
    ("created", "running", "new-attempt", False),
])
def test_execution_detail_requires_live_current_attempt(run_status, attempt_status, current_attempt, accepted):
    """纯验证边界；真实只读 HTTP 往返由本文件的 passive_running_detail 用例覆盖。"""
    from agent_py_agent.agent.conversation.background_agents import BackgroundAgentService
    from agent_py_agent.agent.conversation.background_execution_view import (
        validate_execution_context,
    )
    from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext

    context = BusinessExecutionContext(
        owner_id="owner", task_id="task", space_id="space", project_ref=None, space_revision=0,
        binding_id=None, selected_method_refs=(), asset_version_refs=(), schema_version="business-context.v1")
    thread = SimpleNamespace(thread_id="thread", owner_id="owner", metadata={})
    authority = {"agent_run_id": "agent-run", "attempt_id": "attempt", "task_id": "task",
                 "invocation_run_id": "request"}
    ref = SimpleNamespace(agent_run_id="agent-run", observed_attempt_id="attempt", task_run_id="task-run")
    task = SimpleNamespace(target_thread_id="thread", status="running", task_run_id="task-run",
                           conversation_request_id="request", metadata={"business_context": context.to_dict()})
    runtime = SimpleNamespace(
        get_agent_run=lambda _id: {"status": run_status, "current_attempt_id": current_attempt},
        get_attempt=lambda _id: {"status": attempt_status},
        get_task_run=lambda _id: {"task_id": "task"},
    )
    service = BackgroundAgentService(SimpleNamespace(
        conversation_store=SimpleNamespace(session_tasks=SimpleNamespace(require=lambda _id: task))))
    if accepted:
        validate_execution_context(service.store, thread, authority, ref, context, runtime=runtime)
    else:
        with pytest.raises(ValueError, match="EXECUTION_AUTHORITY_UNAVAILABLE"):
            validate_execution_context(service.store, thread, authority, ref, context, runtime=runtime)


def test_business_delivery_directory_reaches_native_model_request(scoped_gateway, monkeypatch):
    prompts = []
    original = _BlockingModel.generate

    def capture(self, prompt, *args, **kwargs):
        prompts.append(str(prompt) + repr(kwargs.get('messages', [])))
        return original(self, prompt, *args, **kwargs)

    monkeypatch.setattr(_BlockingModel, 'generate', capture)
    case = _running_scoped_task(scoped_gateway)
    try:
        assert prompts
        assert '本任务已声明交付目录:' in prompts[0]
    finally:
        _finish(case)
    assert case.store.session_tasks.require(case.task_id).status == 'done'


def test_done_session_task_still_cancels_its_live_original_attempt(scoped_gateway):
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskUpdate

    case = _running_scoped_task(scoped_gateway)
    try:
        case.store.session_tasks.advance(case.task_id, SessionTaskUpdate('done'))
        status, pending = _call_control(scoped_gateway, case, 'tasks.cancel', 'done-task-active-attempt')
        assert status == 200, pending
        assert pending['data']['state'] == 'pending'
        assert pending['data']['outstanding_refs']
        assert case.store.session_tasks.require(case.task_id).status == 'done'
        assert case.repo.get_agent_run(case.authority['agent_run_id'])['status'] == 'cancelled'
        assert not case.model.returned.is_set()  # 控制受理不冒充原worker已退出。
    finally:
        _finish(case)


def test_closed_task_ignores_verified_other_order_claim_and_keeps_unknown_pending():
    from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
    from agent_py_agent.agent.conversation.session_messaging import _unbound_task_run_proven_closed

    context = BusinessExecutionContext('local/main', 'space-own', None, 'order-own', 1, None, (), (), 'business-execution-context.v1')
    other = BusinessExecutionContext('local/main', 'space-other', None, 'order-other', 1, None, (), (), 'business-execution-context.v1')
    task = SimpleNamespace(task_id='order-own', task_run_id='own-run', target_thread_id='shared-controller',
        metadata={'business_context': context.to_dict()})
    claim = {'schema_version': 'background_run_claim.v1', 'claim_scope_id': 'shared-controller',
        'status': 'running', 'thread_id': 'shared-controller',
        'runtime_authority': {'schema_version': 'conversation_claim_execution.v1', 'run_id': 'other', 'attempt_id': 'other-attempt', 'agent_run_id': 'other-agent'}}
    ref = SimpleNamespace(task_run_id='other-run', run_id='other', observed_attempt_id='other-attempt', agent_run_id='other-agent')
    repo = SimpleNamespace(get_task_run=lambda _: {'task_id': 'order-own', 'closed_at': 1.0, 'status': 'done'},
        agent_runs_for_task_run=lambda _: [{'status': 'done'}], parent_execution_ref=lambda **_: ref,
        business_context_for_execution=lambda **_: other)
    host = SimpleNamespace(subagents=SimpleNamespace(runtime_db=repo),
        conversation_store=SimpleNamespace(claims=SimpleNamespace(load_report=lambda _: (claim, None))))
    assert _unbound_task_run_proven_closed(host, task, allow_other_claim=True)
    ref.task_run_id = 'own-run'
    assert not _unbound_task_run_proven_closed(host, task, allow_other_claim=True)
    ref.task_run_id = 'other-run'
    host.conversation_store.claims.load_report = lambda _: (claim, {'category': 'data_corruption'})
    assert not _unbound_task_run_proven_closed(host, task, allow_other_claim=True)


# === Source: agent_py_agent/tests/test_rework_r9_passwordless_sso.py ===

BROWSER = 'browser-binding-r9-passwordless'


def _verifier(config, response, nonce):
    # 外部协议替身：只回显本次nonce，state/MAC/映射/会话仍走原产品链。
    if response != 'signed-provider-response':
        raise HostAPIError('SSO_ASSERTION_INVALID', 401)
    return VerifiedSSOIdentity(config['issuer_url'], 'subject-alice', nonce, ('employees',))


def _service(root):
    service = TotpSSOAuthService(root, assertion_verifier=_verifier)
    service.update_sso_config({'protocol': 'oidc', 'issuer_url': 'https://idp.example',
        'client_id': 'client', 'redirect_uri': 'https://portal.example/callback',
        'subject_mapping': {'subject-alice': 'alice'}, 'role_mapping': {'employees': 'USER'}, 'enabled': True})
    return service


def _complete(service, ticket, **extra):
    return service.complete_sso_callback(state=ticket['state'], nonce=ticket['nonce'],
        response='signed-provider-response', browser_binding=BROWSER, **extra)


def _handshake(service, **extra):
    return _complete(service, service.initiate_sso_login(browser_binding=BROWSER), **extra)


@pytest.fixture
def passwordless(tmp_path):
    LocalUserLifecycle(LocalUserStore(tmp_path)).create('alice', idempotency_key='r9-passwordless-alice')
    assert LocalUserStore(tmp_path).read_user('alice').password_hash is None
    return tmp_path


def test_trusted_sso_issues_and_verifies_passwordless_session(passwordless):
    service = _service(passwordless)
    result = _handshake(service)
    assert result['principal_id'] == 'local/alice' and result['session_token']
    verified = user_auth.verify_user_session(passwordless, result['session_token'])
    assert verified.principal_id == 'local/alice' and verified.owner_id == result['owner_id']
    # 免密账号不因签发而获得密码。
    assert LocalUserStore(passwordless).read_user('alice').password_hash is None


def test_fresh_repeated_handshakes_issue_independent_valid_sessions(passwordless):
    service = _service(passwordless)
    tickets = [service.initiate_sso_login(browser_binding=BROWSER) for _ in range(3)]
    assert len({ticket['state'] for ticket in tickets}) == 3
    for ticket in tickets:
        result = _complete(service, ticket)
        assert user_auth.verify_user_session(passwordless, result['session_token']).principal_id == 'local/alice'
        # 每次握手state仍单次消费；成功签发不放宽重放。
        with pytest.raises(HostAPIError) as replayed:
            _complete(service, ticket)
        assert replayed.value.code == 'SSO_STATE_REPLAYED'


@pytest.mark.parametrize('password', ['', 'any-password', 'TestPassword123!'])
def test_password_login_stays_rejected_for_passwordless_account(passwordless, password):
    _service(passwordless)
    with pytest.raises(HostAPIError) as denied:
        user_auth.authenticate_user(passwordless, account_source='local', user_id='alice', password=password)
    assert denied.value.code == 'INVALID_CREDENTIALS'
    assert LocalUserStore(passwordless).read_user('alice').password_hash is None


def test_mfa_is_required_and_its_activation_revokes_older_passwordless_session(passwordless):
    service = _service(passwordless)
    before = _handshake(service)
    setup = service.setup_totp('alice')
    counter = int(__import__('time').time()) // 30
    service.verify_totp('alice', totp_code(setup['secret'], counter))
    with pytest.raises(HostAPIError) as denied:
        user_auth.verify_user_session(passwordless, before['session_token'])
    assert denied.value.code == 'SESSION_REVOKED'
    with pytest.raises(HostAPIError) as required:
        _handshake(service)
    assert required.value.code == 'TOTP_REQUIRED'
    after = _handshake(service, totp_code=setup['recovery_codes'][0])
    assert user_auth.verify_user_session(passwordless, after['session_token']).principal_id == 'local/alice'


def test_account_disable_and_authentication_stamp_change_revoke_passwordless_sessions(passwordless):
    service = _service(passwordless)
    stamped = _handshake(service)
    old_stamp = authentication_stamp(passwordless, 'alice')
    service.update_sso_config({'role_mapping': {'employees': 'USER', 'contractors': 'USER'}})
    assert authentication_stamp(passwordless, 'alice') != old_stamp
    with pytest.raises(HostAPIError) as revoked:
        user_auth.verify_user_session(passwordless, stamped['session_token'])
    assert revoked.value.code == 'SESSION_REVOKED'
    current = _handshake(service)
    LocalUserLifecycle(LocalUserStore(passwordless)).disable('alice', idempotency_key='r9-passwordless-disable')
    with pytest.raises(HostAPIError):
        user_auth.verify_user_session(passwordless, current['session_token'])
    with pytest.raises(HostAPIError):
        _handshake(service)


def test_existing_password_fingerprints_and_sessions_are_unchanged(tmp_path):
    LocalUserLifecycle(LocalUserStore(tmp_path)).create('bob', password='TestPassword123!',
        idempotency_key='r9-password-account-bob')
    session = user_auth.authenticate_user(tmp_path, account_source='local', user_id='bob', password='TestPassword123!')
    current = user_auth._current_credential(tmp_path, 'local', 'bob', '')
    stamp = authentication_stamp(tmp_path, 'bob')
    legacy = hashlib.sha256((current.password_hash + ('\n' + stamp if stamp else '')).encode()).hexdigest()
    assert current.password_hash and user_auth._credential_fingerprint(tmp_path, current) == legacy
    assert user_auth.verify_user_session(tmp_path, session.session_token).principal_id == 'local/bob'


# === Source: agent_py_agent/tests/test_rework_r9_plugin_script_definition_pin.py ===

_EDITED = '''import json, sys
from pathlib import Path
inputs = json.load(sys.stdin)
Path(inputs["report"]).write_text(json.dumps({"body": "edited"}))
print(json.dumps({"body": "edited"}))
'''


def _payload(case, action):
    payload = {'thread_id': case.thread, 'expected_revision': 1}
    if action == 'execute.script':
        return {**payload, 'script_id': 'source-probe', 'inputs': case.inputs}
    return {**payload, 'workflow_id': 'source-flow'}


def _edit_live_script_before_runner_load(case, monkeypatch):
    real = script_runner.load_script
    edits = []

    def edit_then_load(owner_home, script_id, **kwargs):
        if not edits:
            # 模拟准入后的定义漂移：准入与宿主核对已完成，原 runner 读取记录前活记录被改写。
            ScriptStore(case.home, extra_roots=()).save_script(
                AssetDescriptor('source-probe', 'script', 'source-probe', metadata={'revision': 1}), _EDITED)
            edits.append(script_id)
        return real(owner_home, script_id, **kwargs)

    monkeypatch.setattr(script_runner, 'load_script', edit_then_load)
    return edits


def _terminal_rows(case):
    resources = case.host[0].plugin_host.composition.ports.observations.services.resources
    paths = resources.paths_for(SimpleNamespace(owner_home=resources.users.owner_home('alice')))
    return [json.loads(path.read_text()) for path in sorted(paths.terminal.glob('plugin-exec-*.json'))]


@pytest.mark.parametrize('action', ['execute.script', 'execute.workflow'])
def test_live_script_edit_after_admission_cannot_replace_invoked_body(asset_case, monkeypatch, action):
    case = asset_case
    _configure(case, 'agent')
    monkeypatch.setattr(script_runner, '_outer_seatbelt_active', lambda: True)
    edits = _edit_live_script_before_runner_load(case, monkeypatch)

    with pytest.raises(HostAPIError) as changed:
        _call(case, action, _payload(case, action), status=202)

    # 被调用的是已准入正文；活记录确已改写，原 live fence 仍在执行后拒绝漂移。
    assert edits == ['source-probe']
    assert json.loads(case.report.read_text()) == _EXPECTED
    assert ScriptStore(case.home, extra_roots=()).get_script('source-probe')['code'] == _EDITED
    assert (changed.value.code, changed.value.status) == ('ASSET_CHANGED', 409)
    (terminal,) = _terminal_rows(case)
    assert terminal['status'] == 'failed' and terminal['terminal_response']['error_code'] == 'ASSET_CHANGED'


def test_engine_reload_cannot_replace_admitted_workflow_steps(asset_case, monkeypatch):
    case = asset_case
    _configure(case, 'agent')
    monkeypatch.setattr(script_runner, '_outer_seatbelt_active', lambda: True)
    edited_report = case.workspace / 'edited-report.json'
    edited = _flow('source-flow', {**case.inputs, 'report': str(edited_report)})

    class DriftedStore(workflow_engine.WorkflowStore):
        # 模拟引擎自身按 ID 重读时观察到另一份可变版本（准入与 live fence 读取的仍是原定义）。
        def get_workflow(self, workflow_id):
            return edited if workflow_id == 'source-flow' else super().get_workflow(workflow_id)

    monkeypatch.setattr(workflow_engine, 'WorkflowStore', DriftedStore)

    accepted = _call(case, 'execute.workflow', _payload(case, 'execute.workflow'), status=202)

    assert accepted['accepted']
    assert json.loads(case.report.read_text()) == _EXPECTED and not edited_report.exists()
    (record,) = WorkflowRunStore(case.home).list_runs(workflow_id='source-flow')
    assert record.status == 'succeeded' and record.steps[0].output_snapshot == _EXPECTED
    (terminal,) = _terminal_rows(case)
    assert terminal['status'] == 'done' and terminal['terminal_response']['ok'] is True


# 不替换原生沙箱、不伪造就绪；平台/就绪缺失明确 skip。
@pytest.mark.parametrize('action', ['execute.script', 'execute.workflow'])
def test_native_execution_runs_only_the_admitted_body(asset_case, monkeypatch, action):
    if script_runner._outer_seatbelt_active():
        pytest.skip('外层 Seatbelt 禁止嵌套 sandbox_apply；未验证内层原生执行')
    case = asset_case
    _configure(case, 'agent')
    sandbox = attempt.AttemptExecutionSandbox(attempt.AttemptSandboxSpec(
        attempt_view=case.workspace, staging_root=case.workspace, shared_workspace=case.workspace,
        owner_home=case.home, network_access=False))
    try:
        sandbox.require_ready()
    except attempt.SandboxUnavailableError as exc:
        pytest.skip(f'原生沙箱未就绪：{exc}')
    edits = _edit_live_script_before_runner_load(case, monkeypatch)

    with pytest.raises(HostAPIError) as changed:
        _call(case, action, _payload(case, action), status=202)

    assert edits == ['source-probe'] and changed.value.code == 'ASSET_CHANGED'
    assert json.loads(case.report.read_text()) == _EXPECTED


# === Source: agent_py_agent/tests/test_v13_w0_aliases.py ===

# 绑定原夹具对象，保留 pytest 发现语义，避免导入名与测试参数触发重定义。
v13_native = harness.v13_native


# LLM: 通过冻结原生创建后台身份；不把显示名解释成线程归属。
# 函数用途: 创建带规范空间的真实后台线程。
def background(native):
    space = v13_space(native)
    status, body = v13_call(native, 'background_agents.create', {'space_id':space,'name':'别名测试'},
        user='business-alice', idempotency_key='alias-' + uuid4().hex)
    assert status == 200, body
    services = next(row for row in native[0]._business_services.values() if row.owner_id.endswith('/business-alice'))
    return space, body['data']['agent']['thread_id'], services


# LLM: 别名与规范参数走同一canonical消息和游标签名。
# 函数用途: 验证消息、事件别名真实HTTP和显式参数冲突。
def test_thread_aliases_messages_events_and_conflicts(v13_native):
    _space, thread, services = background(v13_native)
    message = services.host.conversation_store.messages.append({'thread_id':thread,'role':'assistant','content':'别名原文'})
    status, alias = v13_call(v13_native, 'messages.list', {'thread_id':thread}, user='business-alice')
    assert status == 200 and alias['error'] is None, alias
    assert message.message_id in [row['message_id'] for row in alias['data']['items']]
    status, canonical = v13_call(v13_native, 'messages.list',
        {'target': {'kind':'background_main','id':thread}}, user='business-alice')
    assert status == 200 and canonical['data']['items'] == alias['data']['items']
    status, events = v13_call(v13_native, 'events.list', {'thread_id':thread,'filter':{}}, user='business-alice')
    assert status == 200 and events['state'] == 'completed' and events['error'] is None, events
    conflicts = [('messages.list', {'thread_id':thread,'target':{'kind':'background_main','id':thread}}),
        ('events.list', {'thread_id':thread,'filter':{'agent_id':'other-thread'}})]
    for action, params in conflicts:
        status, body = v13_call(v13_native, action, params, user='business-alice')
        assert status == 400 and body['error']['code'] == 'INVALID_SCHEMA'
    status, denied = v13_call(v13_native, 'messages.list', {'thread_id':thread}, user='business-bob')
    assert status == 404 and denied['error']['code'] == 'RESOURCE_NOT_FOUND'


# LLM: 原生直派仍经W2幂等账与原submit受理schema，不调用模型线程。
# 函数用途: 验证thread_id任务别名与真实任务ID及空间边界。
def test_submit_thread_alias_uses_native_assignment(v13_native):
    space, thread, services = background(v13_native)
    params = {'thread_id':thread,'space_id':space,'instruction':'记录规范任务'}
    key = 'alias-task-' + uuid4().hex
    status, body = v13_call(v13_native, 'tasks.submit', params, user='business-alice', idempotency_key=key)
    assert status == 202 and body['state'] == 'accepted', body
    task = services.host.conversation_store.session_tasks.require(body['data']['task_id'])
    assert task.target_thread_id == thread and task.goal == params['instruction']
    status, replay = v13_call(v13_native, 'tasks.submit', params, user='business-alice', idempotency_key=key)
    assert status == 202 and replay['data']['task_id'] == task.task_id
    for extra in ({'asset_refs':[]}, {'controller_agent_id':thread}):
        status, bad = v13_call(v13_native, 'tasks.submit', {**params,**extra},
            user='business-alice', idempotency_key='alias-bad-' + uuid4().hex)
        assert status == 400 and bad['error']['code'] == 'INVALID_SCHEMA'
    status, other = v13_call(v13_native, 'spaces.create', {'name':'别的空间'},
        user='business-alice', idempotency_key='alias-space-' + uuid4().hex)
    assert status == 200
    status, mismatch = v13_call(v13_native, 'tasks.submit', {**params,'space_id':other['data']['space_id']},
        user='business-alice', idempotency_key='alias-mismatch-' + uuid4().hex)
    assert status == 404 and mismatch['error']['code'] == 'SPACE_SCOPE_MISMATCH'


# LLM: W11会话登记是读取普通线程的必要事实，不允许任意同owner线程冒充后台身份。
# 函数用途: 验证规范会话消息、分页和未登记线程拒绝。
def test_registered_conversation_thread_messages(v13_native):
    space = v13_space(v13_native)
    services = next(row for row in v13_native[0]._business_services.values() if row.owner_id.endswith('/business-alice'))
    native_store = services.host.conversation_store
    paths = services.host.home_paths
    thread = native_store.threads.create(canonical_user_id='business-alice', title='普通会话',
        owner_id=services.owner_id, owner_home=str(paths.owner_home_dir))
    status, refused = v13_call(v13_native, 'messages.list', {'thread_id':thread.thread_id}, user='business-alice')
    assert status == 404 and refused['error']['code'] == 'RESOURCE_NOT_FOUND'
    store = V13Store(services.owner_home, owner_id=services.owner_id)
    row = {'conversation_id':thread.thread_id,'owner_id':services.owner_id,'space_id':space,'revision':0}
    store.transaction('conversations', lambda rows: rows.setdefault(thread.thread_id, row),
        idempotency_key='conversation-' + uuid4().hex, params_digest=uuid4().hex)
    expected = [native_store.messages.append({'thread_id':thread.thread_id,'role':'assistant','content':f'原文{index}'}).message_id
        for index in range(3)]
    status, first = v13_call(v13_native, 'messages.list', {'thread_id':thread.thread_id,'limit':2}, user='business-alice')
    assert status == 200 and first['data']['coverage'] == 'partial', first
    assert first['data']['next_cursor'] is not None
    status, second = v13_call(v13_native, 'messages.list',
        {'thread_id':thread.thread_id,'limit':2,'cursor':first['data']['next_cursor']}, user='business-alice')
    assert status == 200 and second['data']['coverage'] == 'complete', second
    assert {item['message_id'] for page in (first,second) for item in page['data']['items']} == set(expected)
    assert all(item['space_id'] == space for item in first['data']['items'])
    status, denied = v13_call(v13_native, 'messages.list', {'thread_id':thread.thread_id}, user='business-bob')
    assert status == 404 and denied['error']['code'] == 'RESOURCE_NOT_FOUND'


# LLM: 原可信context末核撤权不能由别名绕过；冻结target参数仍原样工作。
# 函数用途: 验证伪造context不获得thread别名访问。
def test_alias_rejects_unissued_context(v13_native):
    _space, thread, services = background(v13_native)
    status, body = api.execute_business({'action':'messages.list','request_id':'fake-context',
        'params':{'thread_id':thread}}, object(), services)
    assert status == 403 and body['error']['code'] == 'CAPABILITY_DENIED'


# LLM: 路径只由可信组合根绑定；业务JSON不能传路径，句柄本身也不授予跨owner读取。
# 函数用途: 核验真实native owner宿主拿到准确Gateway路径且外部输入仍闭合。
def test_native_gateway_paths_are_trusted_internal_binding(v13_native):
    _space, thread, services = background(v13_native)
    assert services.host.business_v13_gateway_paths is v13_native[0].paths
    assert services.owner_home != services.host.business_v13_gateway_paths.root
    status, bad = v13_call(v13_native, 'messages.list', {'thread_id': thread,
        'gateway_paths': {'root': '/synthetic/other-owner'}}, user='business-alice')
    assert status == 400 and bad['error']['code'] == 'INVALID_SCHEMA'


# === Source: agent_py_agent/tests/test_v13_w0_async_jobs.py ===

# LLM: 测试仅注册合成kind，仍使用正式HTTP注册器与原W2受理路径。
# 函数用途: 注册一个结果闭合的结构化模型作业。
@pytest.fixture
def model_kind(monkeypatch):
    result = {'type':'object','properties': {'score': {'type':'integer'}},
        'required':['score'],'additionalProperties':False}
    accepted = {'type':'object','properties': {'state': {'const':'accepted'},
        'operation_id': {'$ref':'#/$defs/Id'}, 'job_id': {'$ref':'#/$defs/Id'},
        'task_id': {'$ref':'#/$defs/Id'}},'required':['state','operation_id','job_id','task_id'],
        'additionalProperties':False}
    params = {'type':'object','properties': {'space_id': {'$ref':'#/$defs/Id'},
        'instruction': {'type':'string','minLength':1}}, 'required':['space_id','instruction'],
        'additionalProperties':False}
    definition = v13.V13Action('example.job.start', 'write', params, result,
        lambda call: v13.v13_start_model_job(call, 'example_job', call.params['instruction']),
        accepted_result_schema=accepted, ready=v13._jobs_ready)
    monkeypatch.setitem(v13.JOB_KINDS, 'example_job', {'result_schema':result, 'finalize':json.loads})
    monkeypatch.setitem(v13._ACTIONS, definition.name, definition)
    return definition


# LLM: 准确owner服务从真实请求缓存获取，不捏造owner上下文。
# 函数用途: 取得本测试用户当前使用的领域服务。
def current_services(native):
    return next(value for value in native[0]._business_services.values()
        if value.owner_id.endswith('/business-alice'))


# LLM: 辅助操作只经过原W2任务状态机与canonical消息追加。
# 函数用途: 给后台作业保存可核实的最终模型正文及完成状态。
def complete(native, task_id, text, *, failed=False):
    services = current_services(native)
    store = services.host.conversation_store
    task = store.session_tasks.require(task_id)
    store.session_tasks.advance(task_id, SessionTaskUpdate(status='accepted'))
    store.messages.append({'thread_id':task.target_thread_id,'role':'assistant','content':text,
        'metadata': {'task_id':task_id}})
    store.session_tasks.advance(task_id, SessionTaskUpdate(status='failed' if failed else 'done',
        summary='这不是结构化模型输出'))


# LLM: 返回结果必须来自当前任务正文，summary和别的任务消息均不能补空。
# 函数用途: 验证受理、幂等、隔离、完成及schema有效结果的真实HTTP链。
def test_model_job_states_and_schema_validated_result(v13_native, model_kind):
    space = v13_space(v13_native)
    key = 'job-' + uuid4().hex
    params = {'space_id':space,'instruction':'返回score整数'}
    status, body = v13_call(v13_native, model_kind.name, params,
        user='business-alice', idempotency_key=key)
    assert status == 202 and body['state'] == 'accepted' and body['error'] is None, body
    job = body['data']
    assert job['job_id'] and job['task_id'] and body['operation_id']
    replay = v13_call(v13_native, model_kind.name, params,
        user='business-alice', idempotency_key=key)
    assert replay[0] == 202 and replay[1]['data'] == job
    changed = v13_call(v13_native, model_kind.name, {**params,'instruction':'不同输入'},
        user='business-alice', idempotency_key=key)
    assert changed[0] == 409 and changed[1]['error']['code'] == 'IDEMPOTENCY_CONFLICT'
    status, accepted = v13_call(v13_native, 'async_jobs.get', {'job_id':job['job_id']}, user='business-alice')
    assert status == 200 and accepted['data']['state'] == 'accepted', accepted
    assert accepted['data']['result'] is None and accepted['data']['error'] is None
    status, denied = v13_call(v13_native, 'async_jobs.get', {'job_id':job['job_id']}, user='business-bob')
    assert status == 404 and denied['error']['code'] == 'RESOURCE_NOT_FOUND'
    complete(v13_native, job['task_id'], '{"score":7}')
    status, final = v13_call(v13_native, 'async_jobs.get', {'job_id':job['job_id']}, user='business-alice')
    assert status == 200 and final['state'] == 'completed' and final['error'] is None, final
    assert final['data']['state'] == 'completed' and final['data']['result'] == {'score':7}
    assert final['data']['coverage'] == 'complete' and final['data']['error'] is None
    assert v13_call(v13_native, 'async_jobs.get', {'job_id':job['job_id']}, user='business-alice')[1]['data'] == final['data']


# LLM: 坏JSON和不符schema的JSON都转作业失败，不是HTTP成功伪造模型结果。
# 函数用途: 验证完成后的输出格式校验与任务失败。
@pytest.mark.parametrize('text,failed,error_code', [('not json',False,'JOB_RESULT_INVALID'),
    ('{"score":"wrong"}',False,'JOB_RESULT_INVALID'), ('{"score":3}',True,'JOB_TASK_FAILED')])
def test_invalid_result_never_fabricated(v13_native, model_kind, text, failed, error_code):
    space = v13_space(v13_native)
    status, body = v13_call(v13_native, model_kind.name, {'space_id':space,'instruction':'结构化输出'},
        user='business-alice', idempotency_key='job-' + uuid4().hex)
    assert status == 202, body
    complete(v13_native, body['data']['task_id'], text, failed=failed)
    status, observed = v13_call(v13_native, 'async_jobs.get', {'job_id':body['data']['job_id']}, user='business-alice')
    assert status == 200, observed
    assert observed['data']['state'] == 'failed' and observed['data']['result'] is None
    assert observed['data']['error']['code'] == error_code


# LLM: 未声明kind不能派发，缺源不能把模型任务当同步完成。
# 函数用途: 验证模型端口缺失时显式拒绝及DomainResult受理形状。
def test_job_requires_kind_and_native_sources(v13_native, model_kind, monkeypatch):
    space = v13_space(v13_native)
    monkeypatch.delitem(v13.JOB_KINDS, 'example_job')
    status, body = v13_call(v13_native, model_kind.name, {'space_id':space,'instruction':'无kind'},
        user='business-alice', idempotency_key='job-' + uuid4().hex)
    assert status == 503 and body['error']['code'] == 'HOST_NOT_READY'
    assert body['state'] != 'completed' and body['data'] == {}
    assert DomainResult(202, {'state':'accepted'}).http_status == 202


# LLM: 派发事实只由W0保存；相同业务意图的重放不能重算挂载快照或新派任务。
# 函数用途: 验证首次上下文钉住、动态提示变化重放及结果约束钩子。
@pytest.mark.parametrize('score,expected', [(3, 'completed'), (9, 'failed')])
def test_context_facts_are_pinned_and_validate_result_hook(v13_native, model_kind, monkeypatch, score, expected):
    from dataclasses import replace

    from agent_py_agent.agent.business_spaces.v13_store import V13Store
    space = v13_space(v13_native)
    facts = {'max_score': 5}

    # LLM: 同一请求派生的运行提示可随目录变化，但首次任务上下文不可重写。
    # 函数用途: 调用带上下文的正式公共作业助手。
    def start(call):
        return v13.v13_start_model_job(call, 'example_job', call.params['instruction'] + json.dumps(facts),
            context_facts=facts)

    monkeypatch.setitem(v13._ACTIONS, model_kind.name, replace(model_kind, handler=start))
    monkeypatch.setitem(v13.JOB_KINDS['example_job'], 'validate_result',
        lambda call, row, result: result['score'] <= row['context_facts']['max_score'])
    params = {'space_id': space, 'instruction': '按固定上下文生成'}
    key = 'context-' + uuid4().hex
    status, accepted = v13_call(v13_native, model_kind.name, params, user='business-alice', idempotency_key=key)
    assert status == 202, accepted
    facts['max_score'] = 20
    status, replay = v13_call(v13_native, model_kind.name, params, user='business-alice', idempotency_key=key)
    assert status == 202 and replay['data'] == accepted['data'], replay
    services = current_services(v13_native)
    stored = V13Store(services.owner_home, owner_id=services.owner_id).get('async_jobs', accepted['data']['job_id'])
    assert stored['context_facts'] == {'max_score': 5}
    complete(v13_native, accepted['data']['task_id'], json.dumps({'score': score}))
    status, observed = v13_call(v13_native, 'async_jobs.get', {'job_id': stored['job_id']}, user='business-alice')
    assert status == 200 and observed['data']['state'] == expected, observed
    if expected == 'failed':
        assert observed['data']['error']['code'] == 'JOB_RESULT_INVALID' and observed['data']['result'] is None


# LLM: 终态已提交后回调失败可重试，回调不能更改公开或持久job，也不能重复派模型。
# 函数用途: 验证终态回调首次失败及后续终态读取的恢复入口。
def test_terminal_hook_retries_after_job_commit(v13_native, model_kind, monkeypatch):
    from agent_py_agent.agent.business_spaces.v13_store import V13Store
    from agent_py_agent.agent.plugin_host.contracts import HostAPIError
    calls = []

    # LLM: 模拟分片自身幂等回写暂时失源；只改传入副本不影响W0记录。
    # 函数用途: 记录终态回调并注入一次可重试故障。
    def finish(call, job):
        calls.append((call.owner_id, job['job_id'], job['state']))
        job['state'] = 'unknown'
        if len(calls) == 1:
            raise HostAPIError('HOST_NOT_READY', 503)

    monkeypatch.setitem(v13.JOB_KINDS['example_job'], 'on_complete', finish)
    space = v13_space(v13_native)
    status, accepted = v13_call(v13_native, model_kind.name, {'space_id': space, 'instruction': '终态回调'},
        user='business-alice', idempotency_key='finish-' + uuid4().hex)
    assert status == 202, accepted
    job = accepted['data']
    complete(v13_native, job['task_id'], '{"score":3}')
    status, failed = v13_call(v13_native, 'async_jobs.get', {'job_id': job['job_id']}, user='business-alice')
    assert status == 503 and failed['error']['code'] == 'HOST_NOT_READY', failed
    services = current_services(v13_native)
    assert V13Store(services.owner_home, owner_id=services.owner_id).get('async_jobs', job['job_id'])['state'] == 'completed'
    for _ in range(2):
        status, final = v13_call(v13_native, 'async_jobs.get', {'job_id': job['job_id']}, user='business-alice')
        assert status == 200 and final['data']['state'] == 'completed', final
    assert len(calls) == 3 and all(row[2] == 'completed' for row in calls)


# LLM: W2已派发而W0 journal尚未提交时注入故障，必须用首次上下文恢复原任务。
# 函数用途: 验证派发前耐久预留及中断后的真实HTTP恢复，不手工制造任务事实。
def test_assignment_before_job_commit_recovers_first_context(v13_native, model_kind, monkeypatch):
    from dataclasses import replace

    from agent_py_agent.agent.business_spaces import v13_store as storage
    space = v13_space(v13_native)
    facts, dispatched = {'catalog_revision': 1}, []
    failure = {'pending': True}
    original_call, original_write = v13.v13_call_frozen, storage.write_bytes_atomic_beneath

    # LLM: 相同params可派生不同目录提示，重试仍须使用第一次快照。
    # 函数用途: 调用携带可变目录上下文的正式helper。
    def start(call):
        return v13.v13_start_model_job(call, 'example_job', call.params['instruction'] + json.dumps(facts),
            context_facts=facts)

    # LLM: 不伪造W2回包，仅记录真实派发得到的任务引用。
    # 函数用途: 捕获模型作业的真实受理结果。
    def dispatch(call, action, params, *, idempotency_key=None, expected_revision=None):
        result = original_call(call, action, params, idempotency_key=idempotency_key,
            expected_revision=expected_revision)
        if action == 'tasks.assign':
            dispatched.append(v13._assignment_receipt(result)['task_id'])
        return result

    # LLM: 只中断W0派发后的提交，不拦W2事务或准备阶段。
    # 函数用途: 在真实W0 journal提交前注入一次磁盘故障。
    def interrupt(owner, parts, raw):
        if dispatched and failure['pending'] and parts[-1] == '.transaction.json':
            failure['pending'] = False
            raise OSError('synthetic precommit interruption')
        return original_write(owner, parts, raw)

    monkeypatch.setitem(v13._ACTIONS, model_kind.name, replace(model_kind, handler=start))
    monkeypatch.setattr(v13, 'v13_call_frozen', dispatch)
    monkeypatch.setattr(storage, 'write_bytes_atomic_beneath', interrupt)
    params, key = {'space_id': space, 'instruction': '固定首次目录'}, 'precommit-' + uuid4().hex
    status, failed = v13_call(v13_native, model_kind.name, params, user='business-alice', idempotency_key=key)
    assert status == 503 and len(dispatched) == 1, failed
    services = current_services(v13_native)
    store = storage.V13Store(services.owner_home, owner_id=services.owner_id)
    rows = store.list('async_jobs')
    assert len(rows) == 1 and rows[0]['state'] == 'unknown' and rows[0]['task_id'] is None
    assert rows[0]['context_facts'] == {'catalog_revision': 1}
    pinned_instruction = rows[0]['instruction']
    facts['catalog_revision'] = 2
    status, recovered = v13_call(v13_native, model_kind.name, params, user='business-alice', idempotency_key=key)
    assert status == 202 and recovered['data']['task_id'] == dispatched[0], recovered
    assert len(set(dispatched)) == 1
    final = store.get('async_jobs', recovered['data']['job_id'])
    assert final['context_facts'] == {'catalog_revision': 1} and final['instruction'] == pinned_instruction
    assert services.host.conversation_store.session_tasks.require(final['task_id']).goal == pinned_instruction


# LLM: 校验器是只读业务约束，即便它误写参数也不能污染持久事实和公开结果。
# 函数用途: 实测validate_result的两个参数均为副本。
def test_result_validator_cannot_mutate_job_or_output(v13_native, model_kind, monkeypatch):
    from agent_py_agent.agent.business_spaces.v13_store import V13Store

    # LLM: 主动误改传入对象，用最终HTTP与Store读回证伪共享引用。
    # 函数用途: 注入会改写参数的合成校验器。
    def validator(call, row, result):
        row['state'] = 'failed'
        result['score'] = 999
        return True

    monkeypatch.setitem(v13.JOB_KINDS['example_job'], 'validate_result', validator)
    space = v13_space(v13_native)
    status, accepted = v13_call(v13_native, model_kind.name, {'space_id': space, 'instruction': '只读校验'},
        user='business-alice', idempotency_key='readonly-' + uuid4().hex)
    assert status == 202, accepted
    complete(v13_native, accepted['data']['task_id'], '{"score":3}')
    status, observed = v13_call(v13_native, 'async_jobs.get', {'job_id': accepted['data']['job_id']}, user='business-alice')
    assert status == 200 and observed['data']['state'] == 'completed' and observed['data']['result'] == {'score': 3}, observed
    services = current_services(v13_native)
    row = V13Store(services.owner_home, owner_id=services.owner_id).get('async_jobs', accepted['data']['job_id'])
    assert row['state'] == 'completed' and row['result'] == {'score': 3}


# === Source: agent_py_agent/tests/test_v13_w0_event_stream.py ===

class Clock:
    """只推进测试逻辑时间，不替换来源或响应。"""

    # LLM: 用单调逻辑时间验证退避，避免真实睡眠掩盖轮询缺陷。
    # 函数用途: 初始化逻辑时钟与等待记录。
    def __init__(self):
        self.now = 0
        self.waits = []

    # LLM: 时钟读取不推进时间。
    # 函数用途: 返回当前逻辑秒数。
    def __call__(self):
        return self.now

    # LLM: 等待只推进请求的时间，保留每次超时供断言。
    # 函数用途: 记录并推进测试等待。
    def wait(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


# LLM: 测试只向原运行库追加规范事件。
# 函数用途: 生成可被原观察投影读取的进度事件。
def append(case):
    return case.runtime.append_event(event_type='attempt.progress', attempt_id=case.root['attempt_id'],
        agent_run_id=case.root['agent_run_id'], task_run_id=case.root['task_run_id'])


# LLM: 仅注入时钟与等待，授权及事件源均使用原对象。
# 函数用途: 创建确定性typed流与时钟。
def make_stream(case, *, transport='native', cursor=None, signal=None):
    clock = Clock()
    request = {'request_id': 'w0-event-stream', 'filter': {'space_id': 'space-a'}}
    if cursor is not None:
        request['cursor'] = cursor
    stream = stream_business_events(request, authenticated_context=case.actor,
        services=SimpleNamespace(observation=case.service, clock=clock, wait=clock.wait,
            stream_transport=transport, event_signal=signal))
    return stream, clock


# LLM: 每次交付都复核界限，验证真实来源中的顺序和稳定身份。
# 函数用途: 确认突发130事件不会产生背压缺口或清空积压。
@pytest.mark.parametrize('transport,bound', [('native', 128), ('plugin', 8)])
def test_burst_of_130_is_delivered_in_order_without_gap_or_clear(tmp_path, transport, bound):
    case = fixture(tmp_path)
    baseline = case.service.query('events.list', {'filter': {'space_id': 'space-a'}}, actor=case.actor)
    stream, clock = make_stream(case, transport=transport, cursor=baseline['next_cursor'])
    iterator = stream.events()
    assert next(iterator)['stream_item'] == 'snapshot'
    expected = [append(case)['event_id'] for _ in range(130)]
    received = []
    for _ in expected:
        frame = next(iterator)
        assert frame['stream_item'] == 'event'
        assert stream.pending_count <= bound
        received.append(frame['event_id'])
    assert received == expected
    assert next(iterator)['stream_item'] == 'heartbeat'
    assert clock.now >= stream.heartbeat_seconds
    iterator.close()
    assert stream.pending_count == 0


# LLM: 来源故意忽略limit，但仍以真实查询返回事件和游标。
# 函数用途: 验证超长页截断后续点是实际入队末项，且满队列不再查询。
def test_truncated_page_uses_last_admitted_cursor_and_full_queue_skips_query(tmp_path, monkeypatch):
    case = fixture(tmp_path)
    expected = [append(case)['event_id'] for _ in range(30)]
    original = case.service.query
    calls = []

    # LLM: 只改变来源的页长行为，不手工制造游标或事件字典。
    # 函数用途: 模拟未遵守limit但遵守cursor的真实来源。
    def oversized(action, params, *, actor):
        calls.append(params)
        return original(action, {**params, 'limit': 100}, actor=actor)

    monkeypatch.setattr(case.service, 'query', oversized)
    stream, _clock = make_stream(case, transport='plugin')
    gap, _coverage = stream._fill()
    assert gap is None and stream.pending_count == 8
    assert stream._cursor == stream._pending[-1]['cursor']
    before = len(calls)
    assert stream._fill()[0] is None and len(calls) == before
    assert all(0 < call['limit'] <= 8 for call in calls)
    received = [row['event_id'] for row in stream._pending]
    stream._pending.clear()
    while not all(identifier in received for identifier in expected):
        assert stream._fill()[0] is None
        assert stream.pending_count <= 8
        received.extend(row['event_id'] for row in stream._pending)
        stream._pending.clear()
    assert received[-30:] == expected and len(received) == len(set(received))


# LLM: gap是结构化测试故障，故障后的数据仍来自原运行库。
# 函数用途: 确认gap及一次游标过期都不结束迭代器。
def test_gap_and_cursor_expired_do_not_end_the_generator(tmp_path, monkeypatch):
    case = fixture(tmp_path)
    stream, _clock = make_stream(case)
    iterator = stream.events()
    assert next(iterator)['stream_item'] == 'snapshot'
    original = stream._fill
    faults = ['gap', 'expired']

    # LLM: 每种故障只触发一次，让后续循环走真实来源。
    # 函数用途: 注入gap或过期，再恢复原填充路径。
    def fail_once():
        if faults:
            if faults.pop(0) == 'gap':
                return stream._gap('gap', 'SOURCE_RESET'), 'partial'
            raise BusinessError('CURSOR_EXPIRED')
        return original()

    monkeypatch.setattr(stream, '_fill', fail_once)
    assert next(iterator)['stream_item'] == 'gap'
    assert next(iterator)['stream_item'] == 'cursor_expired'
    assert next(iterator)['stream_item'] == 'event'
    iterator.close()


# LLM: 连续过期必须有限停止，不能把snapshot误当恢复成功。
# 函数用途: 验证第二次连续过期后停止查询且不会热循环。
def test_second_consecutive_expiry_stops_without_hot_loop(tmp_path, monkeypatch):
    case = fixture(tmp_path)
    stream, _clock = make_stream(case)
    iterator = stream.events()
    assert next(iterator)['stream_item'] == 'snapshot'
    calls = []

    # LLM: 只注入结构化错误码。
    # 函数用途: 模拟持续过期的不可恢复来源。
    def expired():
        calls.append(True)
        raise BusinessError('CURSOR_EXPIRED')

    monkeypatch.setattr(stream, '_fill', expired)
    assert next(iterator)['stream_item'] == 'cursor_expired'
    assert next(iterator)['stream_item'] == 'cursor_expired'
    with pytest.raises(StopIteration):
        next(iterator)
    assert len(calls) == 2


# LLM: 连续过期保护不能跨越真实进展永久锁死流。
# 函数用途: 验证事件或心跳交付后允许再次从过期恢复。
@pytest.mark.parametrize('progress', ['event', 'heartbeat'])
def test_delivery_resets_consecutive_expiry_guard(tmp_path, monkeypatch, progress):
    case = fixture(tmp_path)
    baseline = case.service.query('events.list', {'filter': {'space_id': 'space-a'}}, actor=case.actor)
    stream, _clock = make_stream(case, cursor=baseline['next_cursor'])
    iterator = stream.events()
    assert next(iterator)['stream_item'] == 'snapshot'
    original = stream._fill
    remaining = [True]

    # LLM: 每次由测试明确安排一次错误，不凭文案判断来源。
    # 函数用途: 注入可恢复过期后再交由原来源读取。
    def once():
        if remaining:
            remaining.pop()
            raise BusinessError('CURSOR_EXPIRED')
        return original()

    monkeypatch.setattr(stream, '_fill', once)
    for _ in range(2):
        assert next(iterator)['stream_item'] == 'cursor_expired'
        # 重置续点会重新扫描原事件；它们也是实际恢复进展，不能算第二次连续过期。
        frame = next(iterator)
        assert frame['stream_item'] == 'event'
        if progress == 'heartbeat':
            while frame['stream_item'] != 'heartbeat':
                frame = next(iterator)
        else:
            while stream.pending_count:
                assert next(iterator)['stream_item'] == 'event'
        remaining.append(True)
    iterator.close()


# LLM: 用真实空页和可控时间验证轮询与心跳不是同一个频率。
# 函数用途: 验证首次小等待、倍增上限、完整空闲后才发心跳，以及事件重置退避。
def test_idle_poll_backoff_starts_small_and_heartbeat_only_after_interval(tmp_path):
    case = fixture(tmp_path)
    baseline = case.service.query('events.list', {'filter': {'space_id': 'space-a'}}, actor=case.actor)
    stream, clock = make_stream(case, cursor=baseline['next_cursor'])
    iterator = stream.events()
    assert next(iterator)['stream_item'] == 'snapshot'
    assert next(iterator)['stream_item'] == 'heartbeat'
    heartbeat = stream.heartbeat_seconds
    assert clock.now == heartbeat
    assert clock.waits[:5] == [heartbeat / 60 * 2 ** power for power in range(5)]
    assert all(0 < seconds <= heartbeat for seconds in clock.waits)
    clock.waits.clear()
    assert next(iterator)['stream_item'] == 'heartbeat'
    assert clock.waits[0] == heartbeat / 60
    append(case)
    assert next(iterator)['stream_item'] == 'event'
    clock.waits.clear()
    idle_since = clock.now
    assert next(iterator)['stream_item'] == 'heartbeat'
    assert clock.waits[0] == heartbeat / 60
    assert clock.now - idle_since == heartbeat
    iterator.close()


# LLM: 信号在等待入口真实追加规范事件，不用sleep或假响应制造唤醒。
# 函数用途: 验证event_signal优先等待且事件在心跳前立即交付。
def test_event_signal_wakes_immediately(tmp_path):
    case = fixture(tmp_path)
    baseline = case.service.query('events.list', {'filter': {'space_id': 'space-a'}}, actor=case.actor)
    seen = []

    # LLM: 同步模拟宿主通知到达，不推进逻辑时间。
    # 函数用途: 在首次wait时写入来源并返回已唤醒。
    def wake(timeout):
        seen.append((timeout, append(case)['event_id']))
        return True

    stream, clock = make_stream(case, cursor=baseline['next_cursor'], signal=SimpleNamespace(wait=wake))
    iterator = stream.events()
    assert next(iterator)['stream_item'] == 'snapshot'
    frame = next(iterator)
    assert frame['stream_item'] == 'event' and frame['event_id'] == seen[0][1]
    assert seen[0][0] == stream.heartbeat_seconds / 60
    assert clock.now == 0 and clock.waits == []
    iterator.close()


# LLM: 此用例走真实HTTP，不能用编译或typed生成器替代连接观察点。
# 函数用途: 验证同一NDJSON连接跨越130条突发仍能交付新事件。
@pytest.mark.parametrize('small_send_buffer', [False, True])
def test_native_http_stream_keeps_connection_across_burst(background_http, monkeypatch, small_send_buffer):
    import json

    params, agent = create_background(background_http)
    [services] = [value for value in background_http[0]._business_services.values()
        if value.owner_id.endswith('/business-alice')]
    task = seed_task(services, params['space_id'], agent['agent_id'])
    runtime, run = seed_observed_runtime(services, task)
    with runtime._runtime_connection() as conn:
        initial_ids = [row[0] for row in conn.execute('SELECT event_id FROM runtime_events ORDER BY seq')]
    if small_send_buffer:
        pool = background_http[0].business_stream_pool
        submit = pool.submit
        def bounded_socket(ticket, sock, iterator, encode=None, check=None):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            return submit(ticket, sock, iterator, encode, check)
        monkeypatch.setattr(pool, 'submit', bounded_socket)
    connection, response = open_native_observation(background_http, {'request_id': 'w0-burst-http', 'filter': {}})
    transport = response.fp.raw._sock
    frames, failures = [], []
    reader = None
    try:
        assert response.status == 200
        assert response.getheader('Connection') == 'close'
        assert response.getheader('Content-Type').startswith('application/x-ndjson')
        assert json.loads(response.readline())['stream_item'] == 'snapshot'
        # 持续消费真实连接，不让fixture的130次fsync把自己变成超过原sendall预算的慢消费者。
        def consume():
            try:
                for _ in range(len(initial_ids) + 130):
                    line = response.readline()
                    assert line, '突发事件尚未读完，原生连接已EOF'
                    frames.append(json.loads(line))
            except BaseException:
                failures.append(traceback.format_exc())
        reader = threading.Thread(target=consume, daemon=True)
        reader.start()
        expected = [runtime.append_event(event_type='attempt.progress', attempt_id=run['attempt_id'],
            agent_run_id=run['agent_run_id'], task_run_id=run['task_run_id'])['event_id'] for _ in range(130)]
        with runtime._runtime_connection() as conn:
            all_ids = [row[0] for row in conn.execute('SELECT event_id FROM runtime_events ORDER BY seq')]
        reader.join(timeout=20)
        assert not reader.is_alive() and failures == [], failures
        assert all_ids == initial_ids + expected
        assert len(frames) == len(all_ids)
        assert all(frame['stream_item'] == 'event' for frame in frames)
        assert [frame['event_id'] for frame in frames] == all_ids
        assert all_ids[-130:] == expected
        later = runtime.append_event(event_type='attempt.progress', attempt_id=run['attempt_id'],
            agent_run_id=run['agent_run_id'], task_run_id=run['task_run_id'])
        assert json.loads(response.readline())['event_id'] == later['event_id']
    finally:
        try:
            transport.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        if reader is not None and reader.ident is not None:
            reader.join(timeout=5)
            assert not reader.is_alive(), '原生读取线程未回收'
        response.close()
        connection.close()


def test_event_attempt_snapshot_survives_concurrent_append_without_expiring_history_cursor(tmp_path, monkeypatch):
    case = fixture(tmp_path)
    baseline = case.service.query('events.list', {'filter': {'space_id': 'space-a'}}, actor=case.actor)
    first = append(case)
    original = case.service._event_execution_page
    later = []
    selected = []
    def concurrent_append(conn, rows):
        selected.append(tuple(dict.fromkeys(row['attempt_id'] for row in rows if row['attempt_id'])))
        assert len(selected[-1]) <= 100
        if not later:
            later.append(append(case))  # 真实独立WAL写事务，发生在事件读取与attempt批量join之间。
        return original(conn, rows)
    monkeypatch.setattr(case.service, '_event_execution_page', concurrent_append)
    page = case.service.query('events.list', {'filter': {'space_id': 'space-a'},
        'cursor': baseline['next_cursor'], 'limit': 100}, actor=case.actor)
    assert [row['event_id'] for row in page['events']] == [first['event_id']]
    following = case.service.query('events.list', {'filter': {'space_id': 'space-a'},
        'cursor': page['next_cursor'], 'limit': 100}, actor=case.actor)
    assert [row['event_id'] for row in following['events']] == [later[0]['event_id']]
    assert selected == [(case.root['attempt_id'],), (case.root['attempt_id'],)]
