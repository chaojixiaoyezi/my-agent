# LLM: 原关闭producer/新C租约/P4/心跳producer真实运行；物理端点沿受控fixture，无服务、模型、真实Home或新的授权。
# 模块用途: 验证完整旧W首次发表为当前W时的有限整组读及跨轮固定来源，不能认定682现场的具体writer。
import copy
import os
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import daemon_metadata
from agent_py_agent.agent.gateway_parts import multiprocess_takeover_runtime as takeover
from agent_py_agent.agent.gateway_parts import multiprocess_takeover_sources as sources
from agent_py_agent.agent.gateway_parts.control_plane import GatewayControlPlane
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
from agent_py_agent.agent.gateway_parts.multiprocess_generation_metadata import (
    generation_metadata_parts,
    read_generation_metadata,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.cli.gateway_coordinated_run import publish_coordinated_state
from agent_py_agent.cli.gateway_worker_runtime import CoordinatorWorkers
from agent_py_agent.cli.models import GatewayRunContext
from agent_py_agent.tests.test_gateway_role_heartbeat_stability import publish_heartbeat
from agent_py_agent.tests.test_gateway_role_lifecycle_closure import closing as closing
from agent_py_agent.tests.test_gateway_role_lifecycle_closure import context as context
from agent_py_agent.tests.test_gateway_role_lifecycle_closure import (
    coordinator_handles as coordinator_handles,
)
from agent_py_agent.tests.test_gateway_worker_runtime import ControlledLane
from agent_py_agent.tests.test_gateway_worker_runtime import physical as physical


# LLM: 仅探针主动结束本测试已创建的受控Popen，原close仍消费同对象；不是业务异常或wire状态。
# 类用途: 在首个真实P4发表/attach入口完成读观察后退出测试启动链。
class ProbeComplete(Exception):
    pass


# LLM: 原P6/新C/P4/heartbeat真实运行；快照调用前计数、分类异常原样透传，不以末锚点复核冒充新整轮或手造成功closure。
# 函数用途: 在首attach观察真实整轮及来源变化边界，并回收准确受控Popen和租约。
def successor_probe(closing, coordinator_handles, monkeypatch, *, interleave, fault=None):
    closing.seal_generation()
    old, paths = closing.control, gateway_paths_from_root(closing.control.paths.root)
    history = read_generation_metadata(paths.root, old.c_run_id)
    assert history[0]['closure'] is not None
    coordinator_handles['close']()
    monkeypatch.setattr(daemon_metadata, 'process_identity_is_live', lambda _identity: False)
    outcome = SimpleNamespace(seen=[], snapshot_calls=0, classifications=[], faults=[], anchor_calls=0)
    with coordinator_guard(paths.root, old.worker_count):
        controller = takeover.prepare_coordinator_takeover(paths, old.worker_count, 'successor-c')
        assert controller._closed_role_sources == ((old.c_run_id, history[1]),)
        control = GatewayControlPlane(copy.deepcopy(old.config), old.home_root, paths, old.worker_count,
            'successor-c', agent_root=old.agent_root, workspace_roots=old.workspace_roots)
        run = GatewayRunContext(None, paths, old.home_root / 'config.yaml',
            coordinator_worker_count=old.worker_count, control_plane=control)
        publish_coordinated_state(run, {'status': 'starting', 'pid': os.getpid()})
        workers = CoordinatorWorkers(control, old.home_root / 'config.yaml', 43123)
        snapshot, anchors = sources._role_snapshot, getattr(sources, '_role_directory_anchors', None)
        stable_roles = sources._require_stable_roles

        # LLM: 只记录实际注入时的快照/分类位置，刺激仍由原writer或临时文件系统执行。
        # 函数用途: 证明错误确实发生在指定整轮边界，而不是探针未触发就误判通过。
        def inject_fault():
            outcome.faults.append((outcome.snapshot_calls, tuple(outcome.classifications)))
            fault[1](paths.root, workers)

        # LLM: 调用前计数保留硬错的失败读；原reader返回不变，只在首完整读后运行一次原W producer及明确刺激。
        # 函数用途: 记录真实角色读取次数并确定性建立交错，不靠锚点数量推断重试。
        def reading(root):
            outcome.snapshot_calls += 1
            value = snapshot(root)
            outcome.seen.append(value)
            if interleave and len(outcome.seen) == 1:
                publish_heartbeat(workers, monkeypatch, index=0, stamp=2.0)
                if fault is not None and fault[0] == 'same_round':
                    inject_fault()
            return value

        # LLM: 分类器真实运行且原异常透传；仅准确typed候选之后注入跨轮刺激，不依赖第几次锚点检查。
        # 函数用途: 区分合法发表与硬拒绝，并准确观察重读边界。
        def classifying(*args, **kwargs):
            try:
                result = stable_roles(*args, **kwargs)
            except ValueError as error:
                outcome.classifications.append(type(error))
                if type(error) is sources._ClosedWorkerPublished and fault is not None and fault[0] == 'next_round':
                    inject_fault()
                raise
            outcome.classifications.append(None)
            return result

        # LLM: 完整执行原锚点reader；计数仅用于诊断，不混同于整轮重试预算。
        # 函数用途: 保留目录身份检查的真实观察。
        def anchored(root, **kwargs):
            outcome.anchor_calls += 1
            return anchors(root, **kwargs)

        # LLM: 原P4已发表且对象已捕获；仅在真实检查返回后注入明确刺激，不改检查结果、issuer或完成事实。
        # 函数用途: 观察原controller完整门及末检窗口，再让原close消费受控退出。
        def attached(_lane, _control, _process):
            with monkeypatch.context() as patch:
                patch.setattr(sources, '_role_snapshot', reading)
                patch.setattr(sources, '_require_stable_roles', classifying)
                if anchors is not None:
                    patch.setattr(sources, '_role_directory_anchors', anchored)
                if fault is not None and fault[0] in {'after_roles', 'after_sources'}:
                    index = controller._executor_active_index
                    method = 'check_roles' if fault[0] == 'after_roles' else 'check_sources'
                    original_check = getattr(index, method)

                    # LLM: 真实检查完整执行，返回值和异常不改；只在成功返回边界改临时来源。
                    # 函数用途: 钉住角色读完至refresh返回成功之间的来源末检窗口。
                    def checked(*args, **kwargs):
                        result = original_check(*args, **kwargs)
                        inject_fault()
                        return result

                    patch.setattr(index, method, checked)
                with controller._mutex:
                    outcome.refresh = takeover._refresh_targets(controller)
            outcome.quarantined = controller._all_quarantined
            for _lane, process, _observer in workers._created_origins:
                process.exit_status = 0
            raise ProbeComplete

        monkeypatch.setattr(ControlledLane, 'attach', attached)
        with pytest.raises(ProbeComplete):
            workers.start()
        outcome.history_unchanged = read_generation_metadata(paths.root, old.c_run_id) == history
        return outcome


@pytest.mark.parametrize('interleave', [False, True])
# LLM: 原正常关闭/发表入口必须成功；精确快照及分类次数验证只有合法发表才触发一轮重读。
# 函数用途: 保住不交错和首次当前W发表两条正常路径，旧历史不得改变。
def test_original_closed_generation_then_first_current_worker_publication(
    closing, coordinator_handles, monkeypatch, interleave
):
    result = successor_probe(closing, coordinator_handles, monkeypatch, interleave=interleave)
    assert len(result.seen) >= 2
    assert result.seen[0]['gateway_state.json'] == result.seen[1]['gateway_state.json']
    assert (result.seen[0]['workers/w0.json'] != result.seen[1]['workers/w0.json']) is interleave
    assert result.history_unchanged is True
    assert result.refresh is True and result.quarantined is False
    assert result.snapshot_calls == (4 if interleave else 2)
    assert result.classifications == ([sources._ClosedWorkerPublished, None] if interleave else [None])


# LLM: 非法变化只用原writer/测试fs刺激；旧history只取原closing scope身份的canonical locator，不能glob误选当前代；不输出正文或真实凭据。
# 函数用途: 让合法首W发表同时遇到安全错；精确旧代仍追加同一个空格，保持原非法漂移刺激。
def invalid_change(kind, root, workers, *, old_generation):
    import json

    leaf = root / 'workers/w0.json'
    if kind in {'symlink', 'hardlink'}:
        other = root / 'synthetic-other'
        other.write_bytes(leaf.read_bytes())
        leaf.unlink()
        leaf.symlink_to(other) if kind == 'symlink' else os.link(other, leaf)
    elif kind == 'unknown_after_candidate':
        record = publish_heartbeat(workers, pytest.MonkeyPatch(), index=0, stamp=3.0)
        record['extra'] = True
        write_json_file_atomic(root / 'workers/w1.json', record)
    elif kind == 'C_raw':
        path = root / 'gateway_state.json'
        path.write_bytes(path.read_bytes() + b' ')
    elif kind == 'registry':
        path = root / 'workers/registry.json'
        record = json.loads(path.read_bytes())
        record['http_port'] += 1
        write_json_file_atomic(path, record)
    elif kind == 'history':
        path = root.joinpath(*generation_metadata_parts(old_generation))
        path.write_bytes(path.read_bytes() + b' ')
    else:
        path = root if kind == 'root_anchor' else root / 'workers'
        moved = root.with_name(root.name + '-original') if kind == 'root_anchor' else root / 'original-workers'
        path.rename(moved)
        import shutil
        shutil.copytree(moved, path)


@pytest.mark.parametrize('context', [2], indirect=True)
@pytest.mark.parametrize('phase,kind', [
    ('same_round', 'symlink'), ('same_round', 'hardlink'),
    ('same_round', 'unknown_after_candidate'), ('same_round', 'C_raw'),
    ('next_round', 'registry'), ('next_round', 'history'),
    ('next_round', 'C_raw'), ('next_round', 'workers_anchor'), ('next_round', 'root_anchor'),
])
# LLM: 硬错与固定来源漂移仍须整体隔离；直接核失败读和准确typed分类，不放宽预算来容纳额外整轮。
# 函数用途: 证明同轮硬错不重试，跨轮漂移不被新前像洗掉，也不进入第三轮。
def test_legal_publication_never_retries_past_hard_or_fixed_source_change(
    closing, coordinator_handles, monkeypatch, phase, kind
):
    result = successor_probe(closing, coordinator_handles, monkeypatch, interleave=True,
        fault=(phase, lambda root, workers: invalid_change(kind, root, workers,
            old_generation=closing.scope.c_run_id)))
    assert result.refresh is False and result.quarantined is True
    if phase == 'same_round':
        assert result.faults == [(1, ())]
        assert result.snapshot_calls == 2
        assert sources._ClosedWorkerPublished not in result.classifications
    else:
        assert result.faults == [(2, (sources._ClosedWorkerPublished,))]
        assert result.snapshot_calls == (2 if kind in {'workers_anchor', 'root_anchor'} else 4)
        assert result.classifications[0] is sources._ClosedWorkerPublished
        assert result.classifications.count(sources._ClosedWorkerPublished) == 1


@pytest.mark.parametrize('context', [2], indirect=True)
@pytest.mark.parametrize('phase', ['after_roles', 'after_sources'])
@pytest.mark.parametrize('kind', ['registry', 'history', 'C_raw'])
# LLM: 保留真实indexed角色及active来源检查，只在其返回边界改变原canonical来源；不能靠旧metadata stamp预检放行。
# 函数用途: 验证成功返回前仍会拒绝角色检查期间或之后的固定来源漂移。
def test_refresh_rechecks_fixed_sources_before_success(
    closing, coordinator_handles, monkeypatch, phase, kind
):
    result = successor_probe(closing, coordinator_handles, monkeypatch, interleave=True,
        fault=(phase, lambda root, workers: invalid_change(kind, root, workers,
            old_generation=closing.scope.c_run_id)))
    assert result.faults == [(4, (sources._ClosedWorkerPublished, None))]
    assert result.snapshot_calls == 4
    assert result.refresh is False and result.quarantined is True


@pytest.mark.parametrize('context', [2], indirect=True)
@pytest.mark.parametrize('parts', [('gateway_state.json',), ('workers', 'registry.json')])
# LLM: 原合同固定完整raw而非C/P4 inode；同字节原子重发仍需成功，W和旧history合同均保持。
# 函数用途: 防止把严格版本戳误用到当前C/P4导致正常同字节发表被隔离。
def test_same_raw_fixed_roles_can_be_republished_during_retry(
    closing, coordinator_handles, monkeypatch, parts
):
    from agent_py_agent.agent.common.nofollow_fs import write_bytes_atomic_beneath

    # LLM: 仅原strict atomic写叶重发临时目录中的相同字节，不手造角色或关闭证明。
    # 函数用途: 明确建立inode改变但角色完整raw未变的合法反例。
    def republish(root, _workers):
        path = root.joinpath(*parts)
        before = path.stat()
        write_bytes_atomic_beneath(root, parts, path.read_bytes(), require_dir_fd=True)
        assert path.stat().st_ino != before.st_ino

    result = successor_probe(closing, coordinator_handles, monkeypatch, interleave=True,
        fault=('next_round', republish))
    assert result.faults == [(2, (sources._ClosedWorkerPublished,))]
    assert result.snapshot_calls == 4
    assert result.classifications == [sources._ClosedWorkerPublished, None]
    assert result.history_unchanged is True
    assert result.refresh is True and result.quarantined is False


@pytest.mark.parametrize('kind,expected', [('typed', 3), ('ordinary', 1), ('subclass', 1)])
def test_only_exact_typed_publication_has_three_whole_passes(tmp_path, monkeypatch, kind, expected):
    calls = []

    class OtherPublished(sources._ClosedWorkerPublished):
        pass

    error_type = {'typed': sources._ClosedWorkerPublished, 'ordinary': ValueError, 'subclass': OtherPublished}[kind]

    def failing(*_args, **_kwargs):
        calls.append(True)
        raise error_type('generation_role_sources_changed')

    monkeypatch.setattr(sources, '_closed_role_coverage_once', failing)
    with pytest.raises(error_type, match='^generation_role_sources_changed$'):
        sources.require_role_coverage(SimpleNamespace(root=tmp_path), 'current', count=2)
    assert len(calls) == expected
