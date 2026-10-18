# LLM: 使用真实临时C备料、P4 Worker及原全局拒门；不假造远程管理员，不启Gateway、模型或原生进程。
# 模块用途: 检查公开配置与管理员治理写入先拒受限进程，同时保留默认单进程权限及owner私有记账。
import json

import pytest

from agent_py_agent.agent.common import global_write_guard as guard
from agent_py_agent.agent.gateway_parts import multiprocess_mode as mode
from agent_py_agent.agent.gateway_parts.multiprocess_worker_agent import create_worker_pool
from agent_py_agent.agent.settings import parameter_changes as changes
from agent_py_agent.agent.settings.config import load_config
from agent_py_agent.agent.user_space.admin_password import (
    admin_password_status,
    clear_admin_password,
    set_admin_password,
)
from agent_py_agent.agent.user_space.approval_mode import is_permission_admin
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_admin_controls import (
    OwnerAdminControlsError,
    read_owner_admin_controls,
    set_owner_admin_controls,
)
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    ensure_owner_home,
    home_paths_with_owner,
    resolve_owner_home,
)
from agent_py_agent.agent.user_space.resource_grants import (
    read_resource_grants,
    set_resource_grant,
)
from agent_py_agent.tests.test_gateway_multiprocess_worker_agent import worker as worker
from agent_py_agent.tests.test_gateway_worker_global_write_boundaries import physical_snapshot
from agent_py_agent.tests.test_model_profiles import add

_ACTIONS = ('parameter_set', 'parameter_create', 'parameter_reset', 'parameter_revert',
            'password_set', 'password_clear', 'admin_controls', 'resource_grant', 'resource_revoke')
_GRANT = {'provider': 'test', 'resource_type': 'sheet', 'resource_token': 'isolated_sheet',
          'operations': ['read', 'append']}
_ORIGIN = changes.ChangeOrigin('local/main', '隔离全局写边界回归')


# LLM: 全部输入由真实临时C构造准备，限制模式只在测试动作发生前切换；不替换任何写入或权限函数。
# 函数用途: 生成真实typed owner路径、有效配置回滚记录、密码及资源授权，避免坏参数掩盖缺门。
@pytest.fixture
def prepared(worker, monkeypatch):
    frozen, config, root, owner, _ = worker
    base = frozen.launch.owner_root
    with monkeypatch.context() as setup:
        setup.setattr(mode, '_WORKER_PARTITION', None)
        main = home_paths_with_owner(home_paths(base), resolve_owner_home(base, OwnerIdentity.local_main()))
        target = home_paths_with_owner(home_paths(base), ensure_owner_home(base, owner))
        assert is_permission_admin(main) and not is_permission_admin(target)
        desktop = base / 'config' / 'isolated-desktop.yaml'
        desktop.write_text('max_tokens: 4096\n', encoding='utf-8')
        paths = changes.WritePaths(user_path=desktop)
        saved = changes.set_parameter('max_tokens', '8192', paths=paths, origin=_ORIGIN)
        assert saved['ok'] and load_config(desktop).max_tokens == 8192
        set_admin_password(base, 'Temporary-Only-Password-42', now=100)
        assert set_resource_grant(main, target, _GRANT) == [{**_GRANT, 'operations': ['append', 'read']}]
    return frozen, config, root, owner, main, target, paths, saved['change_id']


# LLM: 调用原公开函数且使用原C真实生成的账本编号；路径仅来自同次临时宿主输入。
# 函数用途: 复用有效动作矩阵，覆盖缺capability文件在低层config守门前被创建的反例。
def original_admin_write(case, action):
    frozen, _, _, _, main, target, paths, change_id = case
    fresh = frozen.launch.owner_root / 'not-created' / 'capability_config.yaml'
    actions = {
        'parameter_set': lambda: changes.set_parameter('max_tokens', '16384', paths=paths, origin=_ORIGIN),
        'parameter_create': lambda: changes.set_parameter('capability_candidate_limit', '8',
            paths=changes.WritePaths(user_path=paths.user_path, capability_path=fresh), origin=_ORIGIN),
        'parameter_reset': lambda: changes.reset_parameter('max_tokens', paths=paths, origin=_ORIGIN),
        'parameter_revert': lambda: changes.revert_change(change_id, paths=paths, origin=_ORIGIN),
        'password_set': lambda: set_admin_password(frozen.launch.owner_root, 'Temporary-Replacement-42', now=200),
        'password_clear': lambda: clear_admin_password(frozen.launch.owner_root),
        'admin_controls': lambda: set_owner_admin_controls(main, target, {'audit_allowed': False}, actor='local/main'),
        'resource_grant': lambda: set_resource_grant(main, target, {**_GRANT, 'operations': ['read']}),
        'resource_revoke': lambda: set_resource_grant(main, target, _GRANT, revoke=True),
    }
    return actions[action]


@pytest.mark.parametrize('action', _ACTIONS)
@pytest.mark.parametrize('process_kind', ['worker', 'executor'])
def test_restricted_process_refuses_before_any_global_side_effect(prepared, monkeypatch, action, process_kind):
    base = prepared[0].launch.owner_root
    if process_kind == 'executor':
        monkeypatch.setattr(mode, '_WORKER_PARTITION', None)
        monkeypatch.setattr(guard, '_restricted_executor', False)
        guard.restrict_executor_global_writes()  # 产品锁存事实；不冒称已验真实native启动。
        expected = guard.GlobalWriteInExecutorError
    else:
        assert mode.current_worker_partition() is prepared[0].launch.partition
        expected = guard.GlobalWriteInWorkerError
    before = physical_snapshot(base)
    with pytest.raises(expected) as caught:
        original_admin_write(prepared, action)()
    assert type(caught.value) is expected
    assert physical_snapshot(base) == before


@pytest.mark.parametrize('action', _ACTIONS)
def test_count_zero_preserves_original_success_and_other_policy_fields(prepared, monkeypatch, action):
    frozen, _, _, _, main, target, paths, _ = prepared
    monkeypatch.setattr(mode, '_WORKER_PARTITION', None)
    original_policy = json.loads(target.owner_tool_policy_json.read_text())
    result = original_admin_write(prepared, action)()
    if action.startswith('parameter_'):
        assert result['ok'] is True and result['written_value_matches'] is True
        assert changes.parameter_history(user_path=paths.user_path)[0]['action'] == result['action']
        if action == 'parameter_set':
            assert load_config(paths.user_path).max_tokens == 16384
        elif action == 'parameter_revert':
            assert load_config(paths.user_path).max_tokens == 4096
        elif action == 'parameter_reset':
            assert 'max_tokens:' not in paths.user_path.read_text()
        else:
            fresh = frozen.launch.owner_root / 'not-created' / 'capability_config.yaml'
            assert fresh.is_file() and fresh.stat().st_mode & 0o777 == 0o600
    elif action == 'password_set':
        assert result == {'configured': True, 'state': 'configured', 'algorithm': 'scrypt', 'updated_at': 200}
    elif action == 'password_clear':
        assert result is True and admin_password_status(frozen.launch.owner_root)['state'] == 'missing'
    elif action == 'admin_controls':
        assert result['audit_allowed'] is False and result['decision_model_allowed'] is True
        assert read_owner_admin_controls(target) == result
    else:
        assert result == read_resource_grants(target)
        assert result == ([] if action == 'resource_revoke' else [{**_GRANT, 'operations': ['read']}])
    if action in {'admin_controls', 'resource_grant', 'resource_revoke'}:
        after = json.loads(target.owner_tool_policy_json.read_text())
        changed = 'admin_controls' if action == 'admin_controls' else 'resource_grants'
        assert {k: v for k, v in after.items() if k != changed} == {
            k: v for k, v in original_policy.items() if k != changed}
    assert is_permission_admin(main) and not is_permission_admin(target)


@pytest.mark.parametrize('action', ['admin_controls', 'resource_grant', 'cross_owner', 'wildcard'])
def test_count_zero_preserves_original_admin_and_exact_resource_refusals(prepared, monkeypatch, action):
    _, _, _, _, main, target, _, _ = prepared
    monkeypatch.setattr(mode, '_WORKER_PARTITION', None)
    before = physical_snapshot(main.root)
    if action == 'admin_controls':
        with pytest.raises(OwnerAdminControlsError, match='只有本机管理员'):
            set_owner_admin_controls(target, target, {'audit_allowed': True}, actor='ordinary')
    elif action == 'cross_owner':
        with pytest.raises(OwnerAdminControlsError, match='只能写在管理员自己'):
            set_owner_admin_controls(main, target, {'cross_owner_audit_allowed': True}, actor='local/main')
    elif action == 'resource_grant':
        with pytest.raises(PermissionError):
            set_resource_grant(target, target, _GRANT)
    else:
        with pytest.raises(ValueError, match='不支持'):
            set_resource_grant(main, target, {**_GRANT, 'resource_token': '*'})
    assert physical_snapshot(main.root) == before


def test_worker_private_decision_ledger_and_model_skill_reads_remain_available(prepared):
    frozen, config, root, owner, _, target, _, _ = prepared
    before = physical_snapshot(frozen.launch.owner_root / 'shared')
    agent = create_worker_pool(frozen, config, root).get(owner)
    assert agent.home_paths.owner_tool_policy_json == target.owner_tool_policy_json
    row = changes.record_decision_change(agent.home_paths, operation='patch', fields=['background_timeout_seconds'],
                                        reason='隔离owner私有记账')
    assert row['key'] == 'decision_settings'
    assert changes.profile_change_history(agent.home_paths)[0]['id'] == row['id']
    assert agent.skills_service.snapshot_for() is not None
    assert physical_snapshot(frozen.launch.owner_root / 'shared') == before


@pytest.mark.parametrize('action', ['set', 'reset', 'revert'])
@pytest.mark.parametrize('process_kind', ['worker', 'executor'])
def test_restricted_public_entry_refuses_before_validation_or_history_read(prepared, monkeypatch, action, process_kind):
    if process_kind == 'executor':
        monkeypatch.setattr(mode, '_WORKER_PARTITION', None)
        monkeypatch.setattr(guard, '_restricted_executor', False)
        guard.restrict_executor_global_writes()
    paths = prepared[6]
    before = physical_snapshot(prepared[0].launch.owner_root)
    with pytest.raises(guard.GlobalWriteInWorkerError):
        if action == 'set':
            changes.set_parameter('unknown-key', 'value', paths=paths, origin=_ORIGIN)
        elif action == 'reset':
            changes.reset_parameter('unknown-key', paths=paths, origin=_ORIGIN)
        else:
            changes.revert_change('no-such-change', paths=paths, origin=_ORIGIN)
    assert physical_snapshot(prepared[0].launch.owner_root) == before


def test_worker_private_model_field_and_revert_keep_original_owner_ledger(prepared):
    frozen, config, root, owner, _, _, _, _ = prepared
    agent = create_worker_pool(frozen, config, root).get(owner)
    before = physical_snapshot(frozen.launch.owner_root / 'shared')
    profile_id, added = add(agent, reasoning_control='none')
    assert added['ok'] is True
    saved = changes.set_profile_field(agent, changes.ProfileFieldChange(profile_id, 'reasoning_control', 'budget'),
                                      origin=changes.ChangeOrigin(owner.owner_id))
    assert saved['ok'] is True and saved['previous'] == 'none'
    assert changes.revert_profile_change(agent, saved['change_id'], origin=_ORIGIN)['ok'] is True
    assert len(changes.profile_change_history(agent.home_paths)) == 2
    assert physical_snapshot(frozen.launch.owner_root / 'shared') == before
