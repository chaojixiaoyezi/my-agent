# LLM: 隔离home与真实生命周期锁，进程liveness仅受控替换；sealed正例复用原producer，不构造或修复关闭证明。
# 模块用途: 验证死亡代次整组退役、失败不移动、重启准入和业务文件保全。
import json
import os
import stat
from argparse import Namespace
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.gateway_parts import daemon_metadata
from agent_py_agent.agent.gateway_parts import multiprocess_generation_retirement as retirement
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap_publish import (
    publish_worker_registry,
)
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import (
    CoordinatorUnavailable,
    coordinator_guard,
)
from agent_py_agent.agent.gateway_parts.multiprocess_generation_metadata import (
    _publish_generation_once,
    generation_metadata_parts,
    read_generation_metadata,
)
from agent_py_agent.agent.gateway_parts.multiprocess_takeover_runtime import (
    prepare_coordinator_takeover,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.standalone_lifecycle import standalone_lifecycle_guard
from agent_py_agent.agent.gateway_parts.standalone_sources import (
    StandaloneSourceUnproven,
    require_standalone_sources,
)
from agent_py_agent.cli import common, gateway_coordinated_run, gateway_process
from agent_py_agent.cli.gateway_worker_runtime import worker_process_identity
from agent_py_agent.tests.test_gateway_role_lifecycle_closure import (
    closing as closing,
)
from agent_py_agent.tests.test_gateway_role_lifecycle_closure import (
    context as context,
)
from agent_py_agent.tests.test_gateway_role_lifecycle_closure import (
    coordinator_handles as coordinator_handles,
)
from agent_py_agent.tests.test_gateway_role_lifecycle_closure import (
    physical as physical,
)


# LLM: 只使用原P4 publisher/原初始v2 publisher与完整W格式；死亡由daemon_metadata原入口模拟，不写closure。
# 函数用途: 创建包含设施与不可移动业务文件的崩溃旧代夹具。
def _dead_home(tmp_path, monkeypatch):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    identity = worker_process_identity(os.getpid())
    workers = [{'worker': index, 'instance_id': f'instance-{index}',
                'process_identity': {**identity, 'pid': 900000000 + index},
                'executor_ledger_ref': f'workers/executors/w{index}'} for index in range(2)]
    publish_worker_registry(paths.root, {'schema_version': 'multiprocess_worker_registry.v1',
        'c_run_id': 'crashed-c', 'worker_count': 2, 'process_identity': identity,
        'http_port': 43123, 'workers': workers}, None)
    _publish_generation_once(paths.root, {'schema_version': 'multiprocess_c_generation.v2',
        'c_run_id': 'crashed-c', 'worker_count': 2, 'closure': None})
    write_json_file_atomic(paths.state, {'c_run_id': 'crashed-c', 'worker_count': 2,
        'status': 'killed', 'pid': identity['pid']})
    for row in workers:
        write_json_file_atomic(paths.root / f"workers/w{row['worker']}.json", {
            'schema_version': 'multiprocess_worker_heartbeat.v1', 'worker': row['worker'], 'of': 2,
            'pid': row['process_identity']['pid'], 'process_identity': row['process_identity'],
            'c_run_id': 'crashed-c', 'instance_id': row['instance_id'], 'ready': False,
            'loop_health': 'stopped', 'inflight': 0, 'updated_at': 1.0,
            'readiness': {'ready': False, 'reason': 'stopped'}})
    write_json_file_atomic(paths.root / 'workers/control.json', {'c_run_id': 'crashed-c', 'exit': True})
    for relative in ('workers/takeover_evidence/old.json', 'admission/reservations/old.json',
                     'responsibility_index/settlements/old.json', 'requests/pending/keep.json',
                     'control_operations/keep.json', 'ingress/keep.json', 'responses/keep.json'):
        write_json_file_atomic(paths.root / relative, {'preserve': relative})
    monkeypatch.setattr(daemon_metadata, 'process_identity_is_live', lambda _identity: False)
    return paths


# LLM: 只读取测试目录中的普通叶内容，不做产品来源推断，供失败不移动与业务保全断言。
# 函数用途: 冻结夹具的每个文件字节。
def _files(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()}


# LLM: 真实Count0锁内调用完整入口，成功后再调用未修改的原来源门；manifest与每个归档路径逐项核字节。
# 函数用途: 验证崩溃代次退役后单进程来源门通过，业务文件完全保留。
def test_count0_retires_dead_unsealed_generation_then_sources_pass(tmp_path, monkeypatch):
    paths = _dead_home(tmp_path, monkeypatch)
    before = _files(paths.root)
    with standalone_lifecycle_guard(paths.root) as lifecycle:
        manifest = retirement.retire_dead_multiprocess_generations(paths, target_count=0, lifecycle=lifecycle)
        require_standalone_sources(paths, lifecycle)
        assert retirement.retire_dead_multiprocess_generations(paths, target_count=0, lifecycle=lifecycle) is None
    archives = list((paths.root / 'multiprocess_retired').iterdir())
    assert len(archives) == 1 and stat.S_IMODE(archives[0].stat().st_mode) == 0o700
    assert json.loads((archives[0] / 'manifest.json').read_bytes()) == manifest
    assert manifest['generations'] == [{'c_run_id': 'crashed-c', 'state': 'unsealed', 'worker_count': 2}]
    assert manifest['moved'][-1] == 'gateway_state.json'
    for relative in manifest['moved']:
        source = archives[0] / relative
        if source.is_dir():
            for child in source.rglob('*'):
                if child.is_file():
                    key = str(child.relative_to(archives[0]))
                    assert child.read_bytes() == before[key]
        else:
            assert source.read_bytes() == before[relative]
        assert not (paths.root / relative).exists()
    for name, raw in before.items():
        if name.startswith(('requests/', 'control_operations/', 'ingress/', 'responses/')):
            assert paths.root.joinpath(name).read_bytes() == raw
    assert set(_files(archives[0])) - {'manifest.json'} == {
        name for name in before if name.startswith(('workers/', 'admission/', 'responsibility_index/'))
        or name in {'gateway_state.json', 'gateway_state.json.lock'}}
    print('RETIREMENT_MANIFEST=' + json.dumps(manifest, sort_keys=True))


# LLM: 不mock接班控制器，退役后走原prepare入口，验证冷源不再被旧未封存角色隔离。
# 函数用途: 验证MP崩溃重启恢复启动准入。
def test_mp_retires_unsealed_generation_and_takeover_is_not_quarantined(tmp_path, monkeypatch):
    paths = _dead_home(tmp_path, monkeypatch)
    with coordinator_guard(paths.root, 2):
        manifest = retirement.retire_dead_multiprocess_generations(paths, target_count=2, current_c_run_id='next-c')
        assert manifest['generations'][0]['state'] == 'unsealed'
        successor = prepare_coordinator_takeover(paths, 2, 'next-c')
        assert successor.active() and not successor._all_quarantined


# LLM: sealed事实只能来自原正常producer，结束旧guard后才取得不同count的原新guard。
# 函数用途: 验证正常停机后改变worker数量不会被旧count卡死。
def test_mp_retires_count_mismatch_sealed_generation(closing, coordinator_handles, monkeypatch):
    closing.seal_generation()
    paths, count = closing.control.paths, closing.scope.count
    before = read_generation_metadata(paths.root, closing.scope.c_run_id)
    coordinator_handles['close']()
    monkeypatch.setattr(daemon_metadata, 'process_identity_is_live', lambda _identity: False)
    with coordinator_guard(paths.root, count + 1):
        manifest = retirement.retire_dead_multiprocess_generations(paths, target_count=count + 1,
                                                                  current_c_run_id='next-c')
        assert manifest['generations'] == [{'c_run_id': closing.scope.c_run_id,
            'state': 'count_mismatch', 'worker_count': count}]
        successor = prepare_coordinator_takeover(paths, count + 1, 'next-c')
        assert successor.active() and not successor._all_quarantined
    archive = next((paths.root / 'multiprocess_retired').iterdir())
    assert archive.joinpath(*generation_metadata_parts(closing.scope.c_run_id)).read_bytes() == before[1]


# LLM: 失败启动state本身不是闭包或死亡凭据；只有原根锁与其它角色死亡核验之后才能移走。
# 函数用途: 验证没有registry和history的失败marker不会卡住下一次启动。
def test_mp_retires_state_only_failed_start_marker(tmp_path):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    write_json_file_atomic(paths.state, {'status': 'failed', 'c_run_id': 'failed-start'})
    with coordinator_guard(paths.root, 2):
        manifest = retirement.retire_dead_multiprocess_generations(paths, target_count=2, current_c_run_id='next-c')
        assert manifest['generations'] == [{'c_run_id': 'failed-start', 'state': 'state_only', 'worker_count': None}]
        successor = prepare_coordinator_takeover(paths, 2, 'next-c')
        assert successor.active() and not successor._all_quarantined


# LLM: 原producer的匹配sealed代完全留给既有successor路径，不调用移动器或重写metadata。
# 函数用途: 验证同数量正常重启不会被自动退役抢走关闭证据。
def test_sealed_matching_generation_not_retired(closing):
    closing.seal_generation()
    paths = closing.control.paths
    before = _files(paths.root)
    assert retirement.retire_dead_multiprocess_generations(
        paths, target_count=closing.scope.count, current_c_run_id='next-c') is None
    assert _files(paths.root) == before


# LLM: C死亡由原锁证明，只有指定W判活/未知；在移动之前核拒绝文案及完整原字节。
# 函数用途: 验证不确定的worker永远不能被当作已死亡退役。
@pytest.mark.parametrize('verdict', [True, None])
@pytest.mark.parametrize('count', [0, 2])
def test_live_or_unknown_worker_refuses_without_moving(tmp_path, monkeypatch, capsys, verdict, count):
    paths = _dead_home(tmp_path, monkeypatch)
    monkeypatch.setattr(daemon_metadata, 'process_identity_is_live',
                        lambda identity: verdict if identity and identity['pid'] == 900000000 else False)
    guard = standalone_lifecycle_guard(paths.root) if count == 0 else coordinator_guard(paths.root, count)
    with guard as lock:
        before = _files(paths.root)
        reason = 'retirement_worker_alive' if verdict is True else 'retirement_worker_liveness_unknown'
        with pytest.raises((StandaloneSourceUnproven, CoordinatorUnavailable), match=reason):
            retirement.retire_dead_multiprocess_generations(paths, target_count=count,
                                                           lifecycle=lock if count == 0 else None)
        assert _files(paths.root) == before
    refusal = json.loads(capsys.readouterr().err.strip())
    assert refusal['event'] == 'gateway_generation_retirement_refused' and refusal['reason'] == reason
    assert any(row['role'] == 'W' and row['pid'] == 900000000 for row in refusal['identities'])


# LLM: 陌生设施不得自动接纳，不因所有已知PID已死就移动未知叶。
# 函数用途: 验证worker目录白名单之外的名称导致零移动拒绝。
def test_unknown_worker_entry_refuses(tmp_path, monkeypatch):
    paths = _dead_home(tmp_path, monkeypatch)
    (paths.root / 'workers/unknown.json').write_text('{}')
    with standalone_lifecycle_guard(paths.root) as lifecycle:
        before = _files(paths.root)
        with pytest.raises(StandaloneSourceUnproven, match='retirement_unknown_source'):
            retirement.retire_dead_multiprocess_generations(paths, target_count=0, lifecycle=lifecycle)
        assert _files(paths.root) == before


# LLM: 真实os.replace成功三次后故意抛错，state须原位；重新调用重新证明剩余源，不修补closure。
# 函数用途: 验证中途失败后幂等续搬，多个manifest联合覆盖全部原设施。
def test_retirement_is_idempotent_after_partial_move(tmp_path, monkeypatch):
    paths = _dead_home(tmp_path, monkeypatch)
    replace, calls = os.replace, []
    before = _files(paths.root)

    # LLM: 只注入搬移调用的第四次失败，原atomic manifest writer仍实际执行。
    # 函数用途: 模拟退役期间磁盘操作中断。
    def fail_after_three(*args, **kwargs):
        if len(calls) == 3:
            raise OSError('controlled_partial_move')
        result = replace(*args, **kwargs)
        calls.append(args)
        return result

    with standalone_lifecycle_guard(paths.root) as lifecycle:
        monkeypatch.setattr(os, 'replace', fail_after_three)
        with pytest.raises(OSError, match='controlled_partial_move'):
            retirement.retire_dead_multiprocess_generations(paths, target_count=0, lifecycle=lifecycle)
        assert paths.state.exists()
        monkeypatch.setattr(os, 'replace', replace)
        assert retirement.retire_dead_multiprocess_generations(paths, target_count=0, lifecycle=lifecycle)
        require_standalone_sources(paths, lifecycle)
        assert retirement.retire_dead_multiprocess_generations(paths, target_count=0, lifecycle=lifecycle) is None
    archived = {}
    for archive in (paths.root / 'multiprocess_retired').iterdir():
        manifest = json.loads((archive / 'manifest.json').read_bytes())
        assert manifest['moved']
        archived.update({name: raw for name, raw in _files(archive).items() if name != 'manifest.json'})
    expected = {name: raw for name, raw in before.items() if name.startswith(
        ('workers/', 'admission/', 'responsibility_index/')) or name in {'gateway_state.json', 'gateway_state.json.lock'}}
    assert archived == expected


# LLM: 未持锁、错误root或无真实C对象都在任何退休I/O前拒绝。
# 函数用途: 验证退役调用必须拥有准确根的生命周期锁。
@pytest.mark.parametrize('count', [0, 2])
def test_lock_required(tmp_path, count):
    paths = gateway_paths_from_root(tmp_path)
    with pytest.raises(ValueError, match='^retirement_lock_required$'):
        retirement.retire_dead_multiprocess_generations(paths, target_count=count)


# LLM: 真实公开拓扑冻结/持锁/退役/来源门，只有进入单进程运行的最后业务边界被mock。
# 函数用途: 验证Count0公开run入口自动退役死亡代次后继续启动。
def test_cmd_gateway_run_count0_auto_retires_dead_generation(tmp_path, monkeypatch, capsys):
    paths = _dead_home(tmp_path, monkeypatch)
    config = tmp_path / 'config.yaml'
    config.write_text(f'my_agent_home: "{tmp_path / "home"}"\ngateway_worker_process_count: 0\n')
    monkeypatch.setattr(gateway_coordinated_run, '_cold_gateway_paths', lambda _: paths)
    monkeypatch.setattr(gateway_process, 'mark_hosting_gateway_process', lambda: None)
    monkeypatch.setattr(gateway_process, 'wait_for_predecessor_exit', lambda _: True)
    run = Mock(return_value=0)
    monkeypatch.setattr(gateway_process, '_run_standalone_gateway', run)
    assert gateway_process.cmd_gateway_run(Namespace(config=config)) == 0
    run.assert_called_once()
    assert '[gateway-run] retired multiprocess generation(s) crashed-c' in capsys.readouterr().err
    assert not paths.state.exists()


# LLM: MP原入口持真实锁，活W拒绝必须先于配置构造和接班，不冒称模拟业务factory为真实服务启动。
# 函数用途: 验证MP退役拒绝映射为原结构码与退出码2。
def test_mp_hook_refuses_before_takeover_or_business(tmp_path, monkeypatch, capsys):
    paths = _dead_home(tmp_path, monkeypatch)
    monkeypatch.setattr(daemon_metadata, 'process_identity_is_live',
                        lambda identity: identity and identity['pid'] == 900000000)
    effect = Mock(side_effect=AssertionError('must refuse before configuration'))
    monkeypatch.setattr(common, 'agent_configuration', effect)
    with coordinator_guard(paths.root, 2):
        assert gateway_coordinated_run._construct_coordinated_gateway(
            Namespace(_gateway_worker_count=2, _gateway_c_run_id='next-c'), paths) == 2
    effect.assert_not_called()
    assert 'GATEWAY_STANDALONE_SOURCE_UNPROVEN' in capsys.readouterr().err


# LLM: 原P1 ledger产生reserved/registered记录，不绕codec；只在隔离设施中构造待核实的执行者身份。
# 函数用途: 为死亡与未知执行者用例创建规范账本行。
def _executor(paths):
    from agent_py_agent.agent.gateway_parts.multiprocess_executor_ledger import ExecutorLedger
    from agent_py_agent.agent.gateway_parts.multiprocess_executor_runtime import _reservation
    from agent_py_agent.agent.gateway_parts.multiprocess_route import WorkerPartition

    ledger = ExecutorLedger(paths.root, WorkerPartition(0, 2, 'crashed-c'))
    binding = {'request_id': 'request-old', 'execution_attempt_id': 'attempt-old',
               'tool_call_id': 'call-old', 'owner_sha': '0' * 64, 'lease_epoch': 1}
    row = ledger.reserve(_reservation(ledger, binding, 'tool', 'executor-old'))
    identity = {**worker_process_identity(os.getpid()), 'pid': 900000002}
    row = ledger.advance(row, 'registered', {'process_identity': identity, 'pgid': identity['pid'], 'sid': identity['pid']})
    return ledger, row


# LLM: 每一条P1即使终态也保留死亡核验，活/未知或不可解析都不得被移走。
# 函数用途: 验证执行者证明拒绝的精确原因和零移动。
@pytest.mark.parametrize('fault,reason', [
    ('alive', 'retirement_executor_alive_or_unknown'),
    ('unknown', 'retirement_executor_alive_or_unknown'),
    ('unreadable', 'retirement_executor_unreadable'),
])
def test_executor_alive_unknown_or_unreadable_refuses(tmp_path, monkeypatch, fault, reason):
    paths = _dead_home(tmp_path, monkeypatch)
    _executor(paths)
    if fault == 'unreadable':
        next((paths.root / 'workers/executors/w0').glob('*.json')).write_text('{broken')
    else:
        monkeypatch.setattr(daemon_metadata, 'process_identity_is_live', lambda identity:
                            (True if fault == 'alive' else None) if identity and identity['pid'] == 900000002 else False)
    with standalone_lifecycle_guard(paths.root) as lifecycle:
        before = _files(paths.root)
        with pytest.raises(StandaloneSourceUnproven, match=reason):
            retirement.retire_dead_multiprocess_generations(paths, target_count=0, lifecycle=lifecycle)
        assert _files(paths.root) == before


# LLM: 用原P1 ledger和死亡入口，移动整个旧分区目录，空executors目录须能通过原Count0来源门。
# 函数用途: 验证死亡执行者被完整归档而不留下阻塞来源的空子分区。
def test_dead_executor_partition_is_archived_and_sources_pass(tmp_path, monkeypatch):
    paths = _dead_home(tmp_path, monkeypatch)
    _executor(paths)
    before = _files(paths.root / 'workers/executors/w0')
    with standalone_lifecycle_guard(paths.root) as lifecycle:
        manifest = retirement.retire_dead_multiprocess_generations(paths, target_count=0, lifecycle=lifecycle)
        require_standalone_sources(paths, lifecycle)
    assert 'workers/executors/w0' in manifest['moved']
    archive = next((paths.root / 'multiprocess_retired').iterdir())
    assert _files(archive / 'workers/executors/w0') == before
    assert {'role': 'executor', 'pid': 900000002, 'verdict': 'dead'} in manifest['identities']


# LLM: 持根锁不许可忽略仍然live的完整C身份，不能以PID缓存或state状态替代这个额外拒绝门。
# 函数用途: 验证记录的C仍live时不退役。
def test_live_recorded_coordinator_refuses(tmp_path, monkeypatch):
    paths = _dead_home(tmp_path, monkeypatch)
    monkeypatch.setattr(daemon_metadata, 'process_identity_is_live', lambda identity:
                        identity is not None and identity['pid'] == os.getpid())
    with standalone_lifecycle_guard(paths.root) as lifecycle:
        before = _files(paths.root)
        with pytest.raises(StandaloneSourceUnproven, match='retirement_coordinator_alive'):
            retirement.retire_dead_multiprocess_generations(paths, target_count=0, lifecycle=lifecycle)
        assert _files(paths.root) == before


# LLM: 真实flock由本测试持有另一个FD，退役不能移动它造成原锁路径换inode；state本身仍最后移动。
# 函数用途: 验证已锁的state sidecar保持原位且不阻塞其它已证明死亡设施退役。
def test_held_state_sidecar_is_not_moved(tmp_path, monkeypatch):
    import fcntl

    paths = _dead_home(tmp_path, monkeypatch)
    lock = paths.root / 'gateway_state.json.lock'
    with lock.open('rb') as handle, standalone_lifecycle_guard(paths.root) as lifecycle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        signature = lock.stat().st_ino
        manifest = retirement.retire_dead_multiprocess_generations(paths, target_count=0, lifecycle=lifecycle)
        assert lock.stat().st_ino == signature
        assert 'gateway_state.json.lock' not in manifest['moved']
        assert manifest['moved'][-1] == 'gateway_state.json'
        require_standalone_sources(paths, lifecycle)


# LLM: 代次集合来自所有角色和历史；一个不兼容旧代触发后，不能只移动它而保留其它旧代。
# 函数用途: 验证整组退役和原v1历史字节保全。
def test_all_old_generations_retire_together(tmp_path, monkeypatch):
    paths = _dead_home(tmp_path, monkeypatch)
    _publish_generation_once(paths.root, {'schema_version': 'multiprocess_c_generation.v1',
                                         'c_run_id': 'older-c', 'worker_count': 4})
    with coordinator_guard(paths.root, 2):
        manifest = retirement.retire_dead_multiprocess_generations(paths, target_count=2, current_c_run_id='new-c')
    assert {row['c_run_id']: row['state'] for row in manifest['generations']} == {'crashed-c': 'unsealed', 'older-c': 'v1'}
    assert list((paths.root / 'workers/generations').iterdir()) == []


# LLM: 本代被明确排除，不因本代初始closure为空而将其退役。
# 函数用途: 验证当前代次不进入旧代集合。
def test_current_generation_is_excluded(tmp_path, monkeypatch):
    paths = _dead_home(tmp_path, monkeypatch)
    with coordinator_guard(paths.root, 2):
        before = _files(paths.root)
        assert retirement.retire_dead_multiprocess_generations(
            paths, target_count=2, current_c_run_id='crashed-c') is None
        assert _files(paths.root) == before


# LLM: 只替换原P2观测叶；ledger、codec、精确scope、原树求值和退役路径均真实运行。
# 函数用途: 构造父已消失而原组/会话仍有活后代的受控观察。
def _executor_table(row, *, descendant=False, failure=''):
    import sys

    identity = row['process_identity']
    pid = identity['pid'] + 1
    rows = {pid: {'pid': pid, 'ppid': 1, 'pgid': row['pgid'] if descendant else pid,
                  'sid': row['sid'] if descendant else pid,
                  'birth': identity['start_time'], 'state': 'S'}}
    return {'platform': sys.platform, 'host_id': identity['host_id'], 'rows': rows, 'failure': failure}


# LLM: 原v1 producer加真实ledger；先确认同一P2树求值发现活后代，再要求两种启动模式零移动。
# 函数用途: 防止父进程已退出掩盖活后代或不完整观察。
@pytest.mark.parametrize('count', [0, 2])
@pytest.mark.parametrize('observation', ['descendant', 'incomplete'])
def test_r2_executor_tree_must_be_completely_stopped(tmp_path, monkeypatch, count, observation):
    from agent_py_agent.agent.gateway_parts import multiprocess_takeover as p2
    from agent_py_agent.agent.gateway_parts.multiprocess_takeover_tree import evaluate_tree

    paths = _dead_home(tmp_path, monkeypatch)
    _ledger, row = _executor(paths)
    table = _executor_table(row, descendant=observation == 'descendant',
                            failure='enumeration_incomplete' if observation == 'incomplete' else '')
    assert evaluate_tree(row, table)['status'] == ('live' if observation == 'descendant' else 'unknown')
    monkeypatch.setattr(p2, 'observe_processes', lambda: table)
    guard = standalone_lifecycle_guard(paths.root) if count == 0 else coordinator_guard(paths.root, count)
    with guard as lifecycle:
        before = _files(paths.root)
        refusal = StandaloneSourceUnproven if count == 0 else CoordinatorUnavailable
        with pytest.raises(refusal, match='retirement_executor_alive_or_unknown') as caught:
            retirement.retire_dead_multiprocess_generations(paths, target_count=count,
                current_c_run_id='next-c', lifecycle=lifecycle if count == 0 else None)
        assert caught.value.__cause__ is not None
        assert _files(paths.root) == before
        assert not (paths.root / 'multiprocess_retired').exists()


# LLM: reserved/abandoned的全空槽是合法P0形状，却不证明创建前死亡；缺原未执行证明必须保留责任。
# 函数用途: 防止缺进程身份的原记录被静默丢弃。
@pytest.mark.parametrize('state', ['reserved', 'abandoned'])
def test_r2_empty_executor_identity_cannot_disappear(tmp_path, monkeypatch, state):
    from agent_py_agent.agent.gateway_parts.multiprocess_executor_ledger import ExecutorLedger
    from agent_py_agent.agent.gateway_parts.multiprocess_executor_runtime import _reservation
    from agent_py_agent.agent.gateway_parts.multiprocess_route import WorkerPartition

    paths = _dead_home(tmp_path, monkeypatch)
    ledger = ExecutorLedger(paths.root, WorkerPartition(0, 2, 'crashed-c'))
    row = ledger.reserve(_reservation(ledger, {'request_id': 'never-proved', 'execution_attempt_id': 'attempt',
        'tool_call_id': 'tool', 'owner_sha': '0' * 64, 'lease_epoch': 1}, 'tool', 'unproven'))
    if state == 'abandoned':
        ledger.abandon(row)
    with standalone_lifecycle_guard(paths.root) as lifecycle:
        before = _files(paths.root)
        with pytest.raises(StandaloneSourceUnproven, match='retirement_executor_alive_or_unknown'):
            retirement.retire_dead_multiprocess_generations(paths, target_count=0, lifecycle=lifecycle)
        assert _files(paths.root) == before


# LLM: 原P2两次完整稳定观察加原ledger/scope重读才能作退休依据；输出不是执行许可或native域receipt。
# 函数用途: 验证确已停止的v1原范围可退役并保留完整原账。
def test_r2_complete_original_tree_exit_can_retire(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import multiprocess_takeover as p2

    paths = _dead_home(tmp_path, monkeypatch)
    ledger, row = _executor(paths)
    observed = []

    def probe():
        observed.append(row['reservation_id'])
        return _executor_table(row)

    monkeypatch.setattr(p2, 'observe_processes', probe)
    original = ledger.read(row['reservation_id'])
    with standalone_lifecycle_guard(paths.root) as lifecycle:
        manifest = retirement.retire_dead_multiprocess_generations(paths, target_count=0, lifecycle=lifecycle)
        require_standalone_sources(paths, lifecycle)
    assert len(observed) >= 2
    archive = next((paths.root / 'multiprocess_retired').iterdir())
    archived = archive.joinpath(*ledger._parts(row['reservation_id']))
    from agent_py_agent.agent.gateway_parts.multiprocess_records import deserialize_record
    assert deserialize_record(archived.read_bytes()) == original
    assert {'role': 'executor', 'pid': row['process_identity']['pid'], 'verdict': 'dead'} in manifest['identities']


# LLM: 子进程真实执行原publisher，在原atomic writer的rename前直接退出，不运行finally删除stage。
# 函数用途: 留下原协议生成的完整或部分暂存原件，而非手写一个相似文件名。
def _interrupted_publication(paths, family, *, partial=False, sibling=True):
    import subprocess
    import sys

    if family == 'executor':
        ledger, row = _executor(paths)
        target = paths.root.joinpath(*ledger._parts(row['reservation_id']))
    elif family == 'worker':
        target = paths.root / 'workers/w0.json'
        row = json.loads(target.read_bytes())
    else:
        target = paths.root / 'workers/registry.json'
        row = json.loads(target.read_bytes())
    if not sibling:
        target.unlink()
    script = r'''
import json, os, sys
from pathlib import Path
from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap_publish import publish_worker_registry, read_worker_registry
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.gateway_parts.multiprocess_executor_ledger import ExecutorLedger
from agent_py_agent.agent.gateway_parts.multiprocess_route import WorkerPartition
root, family, row, target, partial = Path(sys.argv[1]), sys.argv[2], json.loads(sys.argv[3]), Path(sys.argv[4]), sys.argv[5] == 'True'
rename, write, fdopen = os.rename, os.write, os.fdopen

def publication_event(event, args):
    if event == 'os.rename' and os.fspath(args[1]) in {target.name, str(target)}:
        os._exit(71)

def at_write(fd, data):
    if partial and bytes(data).startswith(b'{'):
        write(fd, bytes(data)[:7])
        os._exit(72)
    return write(fd, data)

class PartialFile:
    def __init__(self, handle):
        self.handle = handle
    def __enter__(self):
        self.handle.__enter__()
        return self
    def __exit__(self, *args):
        return self.handle.__exit__(*args)
    def __getattr__(self, name):
        return getattr(self.handle, name)
    def write(self, data):
        self.handle.write(data[:7])
        self.handle.flush()
        os._exit(72)

def at_fdopen(fd, *args, **kwargs):
    handle = fdopen(fd, *args, **kwargs)
    return PartialFile(handle) if partial else handle

sys.addaudithook(publication_event)
os.write, os.fdopen = at_write, at_fdopen
if family == 'registry':
    publish_worker_registry(root, row, read_worker_registry(root))
elif family == 'executor':
    ledger = ExecutorLedger(root, WorkerPartition(0, 2, row['c_run_id']))
    with ledger._locked():
        ledger._write(row)
else:
    write_json_file_atomic(target, row)
raise AssertionError('publication was not interrupted')
'''
    result = subprocess.run([sys.executable, '-c', script, str(paths.root), family,
                             json.dumps(row), str(target), str(partial)], capture_output=True, timeout=15)
    assert result.returncode == (72 if partial else 71), (result.returncode, result.stderr)
    stages = [path for path in target.parent.iterdir() if path.name.startswith('.' + target.name + '.')]
    assert len(stages) == 1
    assert stages[0].read_bytes()
    return stages[0], row


# LLM: 精确原writer三种family，两个启动锁及partial/full和canonical兄弟组合；stage字节不是已发表身份或权限。
# 函数用途: 验证中断发布遗留可在全部原责任退出后原样归档并恢复启动准入。
@pytest.mark.parametrize('family', ['registry', 'worker', 'executor'])
@pytest.mark.parametrize('partial,sibling', [(False, True), (True, True), (False, False), (True, False)])
@pytest.mark.parametrize('count', [0, 2])
def test_r2_interrupted_publication_archives_exact_bytes(tmp_path, monkeypatch, family, partial, sibling, count):
    from agent_py_agent.agent.gateway_parts import multiprocess_takeover as p2

    paths = _dead_home(tmp_path, monkeypatch)
    stage, row = _interrupted_publication(paths, family, partial=partial, sibling=sibling)
    if family == 'executor':
        monkeypatch.setattr(p2, 'observe_processes', lambda: _executor_table(row))
    relative, content = stage.relative_to(paths.root), stage.read_bytes()
    guard = standalone_lifecycle_guard(paths.root) if count == 0 else coordinator_guard(paths.root, count)
    with guard as lifecycle:
        manifest = retirement.retire_dead_multiprocess_generations(paths, target_count=count,
            current_c_run_id='next-c', lifecycle=lifecycle if count == 0 else None)
        if count == 0:
            require_standalone_sources(paths, lifecycle)
        else:
            successor = prepare_coordinator_takeover(paths, count, 'next-c')
            assert successor.active() and not successor._all_quarantined
    archive = next((paths.root / 'multiprocess_retired').iterdir())
    assert archive.joinpath(relative).read_bytes() == content
    assert not stage.exists() and manifest['moved']
