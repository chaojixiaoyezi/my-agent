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


# LLM: 旧代由原P6 producer封存，新代重新获取guard及paths DTO；原完整控制面/P4/heartbeat均执行，不手造成功closure。
# 函数用途: 在原首attach处运行有界观察，并准确回收所有受控Popen及租约。
def successor_probe(closing, coordinator_handles, monkeypatch, *, interleave, fault=None):
    closing.seal_generation()
    old, paths = closing.control, gateway_paths_from_root(closing.control.paths.root)
    history = read_generation_metadata(paths.root, old.c_run_id)
    assert history[0]['closure'] is not None
    coordinator_handles['close']()
    monkeypatch.setattr(daemon_metadata, 'process_identity_is_live', lambda _identity: False)
    outcome = SimpleNamespace(seen=[], anchor_calls=0)
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

        # LLM: 原reader返回值不变；只在首完整读后运行一次原W producer及明确非法刺激，不读取真实业务。
        # 函数用途: 确定性建立首末角色交错，不靠sleep或改变预算。
        def reading(root):
            value = snapshot(root)
            outcome.seen.append(value)
            if interleave and len(outcome.seen) == 1:
                publish_heartbeat(workers, monkeypatch, index=0, stamp=2.0)
                if fault is not None and fault[0] == 'same_round':
                    fault[1](paths.root, workers)
            return value

        # LLM: 原锚点reader仍执行；只在真实typed重读前注入测试变更，不能用伪造identity洗掉替换。
        # 函数用途: 观察跨轮来源是否固定，所有变更仅在本临时目录。
        def anchored(root, **kwargs):
            outcome.anchor_calls += 1
            if outcome.anchor_calls == 4 and fault is not None and fault[0] == 'next_round':
                fault[1](paths.root, workers)
            return anchors(root, **kwargs)

        # LLM: 原P4已发表且对象已捕获才进入此点；只观察原controller完整门，不建立issuer或伪造业务完成。
        # 函数用途: 截取首个合法当前W发表窗口，随后让原close消费准确受控退出。
        def attached(_lane, _control, _process):
            with monkeypatch.context() as patch:
                patch.setattr(sources, '_role_snapshot', reading)
                if anchors is not None:
                    patch.setattr(sources, '_role_directory_anchors', anchored)
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
def test_original_closed_generation_then_first_current_worker_publication(
    closing, coordinator_handles, monkeypatch, interleave
):
    result = successor_probe(closing, coordinator_handles, monkeypatch, interleave=interleave)
    assert len(result.seen) >= 2
    assert result.seen[0]['gateway_state.json'] == result.seen[1]['gateway_state.json']
    assert (result.seen[0]['workers/w0.json'] != result.seen[1]['workers/w0.json']) is interleave
    assert result.history_unchanged is True
    assert result.refresh is True and result.quarantined is False


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
def test_legal_publication_never_retries_past_hard_or_fixed_source_change(
    closing, coordinator_handles, monkeypatch, phase, kind
):
    result = successor_probe(closing, coordinator_handles, monkeypatch, interleave=True,
        fault=(phase, lambda root, workers: invalid_change(kind, root, workers,
            old_generation=closing.scope.c_run_id)))
    assert result.refresh is False and result.quarantined is True
    if phase == 'same_round':
        assert result.anchor_calls == 2
    else:
        assert result.anchor_calls == 4


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
