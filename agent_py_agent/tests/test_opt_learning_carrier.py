# LLM: 回归真实原Store/发布器；host验证替身只观察锁位置，不替代静态/配额/安全检查。
# 模块用途: 验证私包发现和学习Verify不占原短事务，撤销与所有权继续真实生效。
from contextlib import contextmanager
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.package_provider import enabled_capability_packages
from agent_py_agent.agent.capability.skill_learning import (
    SkillLearningService,
    SkillLearningSettings,
)
from agent_py_agent.agent.capability.skill_learning_prompt import (
    OUTPUT_SCHEMA_VERSION,
    SkillLearningDecision,
    SkillLearningMaterial,
)
from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.plugin_content_activation import PluginContentActivation
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_installation import PluginInstallationError, PluginInstallRequest
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from agent_py_agent.tests.test_capability_package import content_bundle


# LLM: 普通用户只能见本人private包；旧快照每读仍检查原activation，不能复活撤销安装。
# 函数用途: 用真实安装库回归非管理员私包可发现和即时停用。
def test_capability_private_owner_discovery_and_revocation(tmp_path):
    owner = resolve_owner_home(tmp_path, OwnerIdentity.local_user('alice'))
    other = resolve_owner_home(tmp_path, OwnerIdentity.local_user('bob'))
    store = PluginInstallStore(owner)
    inst = store.install(PluginInstallRequest(inspect_plugin_package(content_bundle()), 'i', 0)).installation
    act = PluginContentActivation('enable', inst.manifest.plugin_id, inst.package_sha256, inst.revision, inst.settings_revision)
    active = store.change_activation(PluginActivationRequest('enable', inst.revision, act)).installation
    packages = enabled_capability_packages(owner)
    assert len(packages) == 1
    assert packages[0].read('CAPABILITY.md')
    assert enabled_capability_packages(other) == ()
    store.change_activation(PluginActivationRequest('disable', active.revision, replace(act, phase='revoked')))
    assert enabled_capability_packages(owner) == ()
    with pytest.raises(PluginInstallationError):
        packages[0].read('CAPABILITY.md')


# LLM: 记录真实调用点是否处于原short transaction，静态和正式publisher仍使用产品实现。
# 函数用途: 慢验证不能占用短提交窗口，最终发布依然使用原短门。
def test_skill_verify_runs_outside_short_commit(tmp_path, monkeypatch):
    import json

    from agent_py_agent.agent.capability import skill_learning as module

    state = {'depth': 0, 'commits': 0}

    # LLM: 只观测进出次数，不改变业务来源或给真实产品签授权。
    # 函数用途: 为本地组件测试标记短事务范围。
    @contextmanager
    def gate(*args):
        state['depth'] += 1
        state['commits'] += 1
        try:
            yield
        finally:
            state['depth'] -= 1

    # LLM: 宿主验证器只认可准确候选hash，拒绝处于短锁内运行。
    # 函数用途: 复现原布局把外部Verify包在短事务中的错误。
    def verify(publish, sha):
        assert state['depth'] == 0
        return {'status': 'passed', 'executed': True, 'contract_valid': True, 'output': {}, 'sha256': sha, 'receipt_id': 'test-verifier'}

    monkeypatch.setattr(module, 'gateway_short_transaction', gate)
    store = SkillLearningStore(tmp_path / 'data' / 'skill_learning', tmp_path / 'skills')
    runtime = SimpleNamespace(guard_config=None, promotion_verifier=verify)
    service = SkillLearningService(store, SkillLearningSettings(autonomous_verified=True), runtime)
    decision = SkillLearningDecision('create', '方法', name='verify-phase', description='检查资料', when_to_use='整理资料', body=('# 核对\n1. 检查输入编码与字段，记录异常并保留原件。\n2. 比较输出数量和类型，失败时停止并报告证据。\n' * 3))
    output = json.dumps({'schema_version': OUTPUT_SCHEMA_VERSION, **asdict(decision)})
    event = service._decide(SkillLearningMaterial({'run_id': 'run'}), output)
    assert event.event == 'published'
    assert state['commits'] >= 1


# LLM: 代次来自宿主注入而非SKILL正文；可变根不声明代次，必须继续检查后代改写。
# 函数用途: 检查不可变根热命中不扫全树，换代、撤销和force刷新都能使旧结果失效。
def test_skill_immutable_generation_cache_avoids_tree_stats(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability import skill_service as module
    from agent_py_agent.tests.test_wave8_p1_selection_skill_schema_caches import _service, _skill

    path, _ = _skill(tmp_path / 'builtin')
    service = _service(tmp_path)
    service.home_paths.immutable_skill_root_generations = {str(tmp_path / 'builtin'): 'release-one'}
    first = service.snapshot_for()
    original = module._path_stat_signature
    touched = []

    # LLM: 只记录实际stat路径，不替代文件元信息或guard判定。
    # 函数用途: 识别热命中是否仍触碰每个Skill后代。
    def observe(candidate):
        touched.append(candidate)
        return original(candidate)

    monkeypatch.setattr(module, '_path_stat_signature', observe)
    assert service.snapshot_for() is first
    assert path not in touched
    path.write_text(path.read_text().replace('original', 'new-release'))
    service.home_paths.immutable_skill_root_generations[str(tmp_path / 'builtin')] = 'release-two'
    assert service.snapshot_for().resolve('method').description == 'new-release'
    service.home_paths.immutable_skill_root_generations.clear()
    path.write_text(path.read_text().replace('new-release', 'editable'))
    assert service.snapshot_for().resolve('method').description == 'editable'


# LLM: 试用/静态验证不能成为真实执行质量分；原host receipt即使成功退出也不覆盖显式未执行标记。
# 函数用途: 防dry_run和executed=false回执抬高方法有效率。
@pytest.mark.parametrize('extra', [{'dry_run': True}, {'executed': False}, {'verification_kind': 'deterministic_static'}])
def test_skill_feedback_excludes_non_execution_receipts(tmp_path, extra):
    from agent_py_agent.agent.capability.skill_learning_host_feedback import (
        observe_command_verification,
    )
    from agent_py_agent.agent.capability.skill_learning_maintenance import (
        LearningObservationSource,
        observe_learning,
    )
    from agent_py_agent.agent.capability.skill_learning_publish import (
        PublishRequest,
        publish_learned_skill,
    )
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path / 'home')
    store = SkillLearningStore.for_home(home)
    dec = SkillLearningDecision('create', '方法', name='feedback-method', description='核对资料', body='# 核对\n1. 比较输入\n2. 验证输出\n')
    publish_learned_skill(store, PublishRequest(dec))
    current = store.load_registry().skills[dec.name]
    observe_learning(store, dec.name, sha256=current.sha256, task_id='t', outcome='adopted', source=LearningObservationSource(run_id='r'))
    params = SimpleNamespace(run_id='r', attempt_id='a')
    agent = SimpleNamespace(config=SimpleNamespace(enable_self_learning=True), home_paths=home, _current_run_params=params)
    observe_command_verification(agent, params, {'id': 1, 'status': 'passed', 'exit_code': 0, **extra})
    assert not any(row['authority'] == 'host' for row in store.load_registry().skills[dec.name].maintenance['observations'])
