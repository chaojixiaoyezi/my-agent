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


# LLM: 子进程内用真实两把锁和确定性事件复现登记/提炼锁序；仅跳过与锁序无关的候选执行，挂住的daemon随子进程退出回收。
# 函数用途: 给并发回归提供不会把死锁线程或FD泄漏给pytest的隔离探针。
def _promotion_lock_probe(home):
    import threading
    from unittest.mock import patch

    from agent_py_agent.agent.assets import asset_root_lock
    from agent_py_agent.agent.assets.descriptor import AssetDescriptor
    from agent_py_agent.agent.assets.script_store import ScriptStore
    from agent_py_agent.agent.capability import asset_promotion as module

    store = SkillLearningStore(home / 'data' / 'skill_learning', home / 'skills')
    ScriptStore(home).save_script(AssetDescriptor(id='lock-probe', kind='script', name='lock-probe'), 'print("{}")')
    original = module._formal_binding
    expected = original(home, 'script', 'lock-probe')
    ready, held, entering = (threading.Event() for _ in range(3))
    errors, reads = [], []

    # LLM: 只在record的第三次正式读回设置交错，不替代真实ScriptStore和锁实现。
    # 函数用途: 让提炼线程在登记取资产锁之前先持有资产根锁。
    def read_binding(*args):
        reads.append(True)
        if len(reads) == 3:
            ready.set()
            assert held.wait(3)
            entering.set()
        return original(*args)

    # LLM: 执行真实promote登记，验证执行阶段仅返回既存正式版本摘要，不制造授权/执行结果。
    # 函数用途: 运行会在旧版拿反序锁的登记一侧。
    def promote():
        try:
            module.promote_verified_asset(home, {'kind': 'script', 'name': 'lock-probe', 'description': '检查锁序', 'code': 'print("{}")'})
        except BaseException as exc:
            errors.append(type(exc).__name__)

    # LLM: 保留turn_extractor的真实asset_root_lock→state_lock顺序，不重入不可重入的资产根锁。
    # 函数用途: 运行与登记竞争的提炼锁序一侧。
    def extract():
        try:
            assert ready.wait(3)
            with asset_root_lock(home):
                held.set()
                assert entering.wait(3)
                with store.state_lock():
                    store.load_registry()
        except BaseException as exc:
            errors.append(type(exc).__name__)

    with patch.object(module, '_formal_binding', read_binding), patch.object(module, '_promote_script_asset', return_value=expected):
        workers = [threading.Thread(target=action, daemon=True) for action in (promote, extract)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(3)
        return {'stuck': [worker.is_alive() for worker in workers], 'errors': errors, 'interleaved': entering.is_set()}


# LLM: 真正并发锁探针放到短命子进程，旧版死锁以结构化stuck失败，不把卡住线程留在测试进程。
# 函数用途: 证明record和提炼的锁序不会相互等待。
def test_promotion_record_and_extraction_lock_order_never_deadlocks(tmp_path):
    import json
    import subprocess
    import sys

    command = ('import json; from pathlib import Path; '
               'from agent_py_agent.tests.test_opt_learning_carrier import _promotion_lock_probe; '
               f'print(json.dumps(_promotion_lock_probe(Path({str(tmp_path)!r}))))')
    result = subprocess.run([sys.executable, '-B', '-c', command], capture_output=True, text=True, timeout=18)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed['interleaved']
    assert observed['stuck'] == [False, False], observed
    assert not observed['errors'], observed


# LLM: 在真实A发布返回与外层读回之间，用真实CAS发布同名B；只替换交错时点，不替换Store或发布器。
# 函数用途: 证明A调用绝不能拿B的摘要或验证回执冒充自己的晋升证明。
def test_r2_skill_promotion_rejects_later_same_name_receipt(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability import asset_promotion as module
    from agent_py_agent.agent.capability.skill_learning_publish import (
        PublishRequest,
        SkillLearningGateError,
    )

    original = module.promote_verified_skill
    published = []

    # LLM: 先完成A真实发布，再以A的revision/hash执行B的真实更新，最后仍返回A事件。
    # 函数用途: 确定性制造同名技能被并发更新的读回窗口。
    def interleave(store, request, verifier, *, commit=None):
        event_a = original(store, request, verifier, commit=commit)
        current = store.load_registry().skills[event_a.skill_name]
        update = replace(request.decision, decision='update', body=request.decision.body + '\n再次核对字段。')
        event_b = original(store, PublishRequest(update, expected_revision=current.revision,
                                                expected_sha256=current.sha256), commit=commit)
        proof_b = store.load_registry().skills[event_a.skill_name].maintenance['verification_receipt']
        published.append((event_a, event_b, proof_b))
        return event_a

    monkeypatch.setattr(module, 'promote_verified_skill', interleave)
    candidate = {'kind': 'skill', 'name': 'receipt-race', 'description': '核对资料',
                 'body': '# 核对\n1. 比较输入字段、编码与数据类型。\n2. 核对输出并保留原始数据。\n' * 4}
    with pytest.raises(SkillLearningGateError, match='LEARNING_FORMAL_REVISION_CHANGED'):
        module.promote_verified_asset(tmp_path, candidate)
    event_a, event_b, proof_b = published[0]
    assert event_a.version == 1 and event_b.version == 2
    assert event_a.sha256 != event_b.sha256 == proof_b['sha256']
    store = SkillLearningStore(tmp_path / 'data' / 'skill_learning', tmp_path / 'skills')
    assert store.load_registry().skills[candidate['name']].maintenance['verification_receipt'] == proof_b


# LLM: 保留真实已发布记录与匹配的版本/hash，只将返回事件改成候选状态；候选绝不能借旧正式记录签成功。
# 函数用途: 明确候选保存事件不是正式晋升回执。
def test_r2_candidate_saved_event_is_not_formal_success(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability import asset_promotion as module

    original = module.promote_verified_skill

    # LLM: 真实验证和写入完成后仅注入非正式事件类型，以排除其它字段不匹配造成的误通过。
    # 函数用途: 检查事件类别门而非版本/hash门。
    def candidate_event(*args, **kwargs):
        return replace(original(*args, **kwargs), event='candidate_saved')

    monkeypatch.setattr(module, 'promote_verified_skill', candidate_event)
    candidate = {'kind': 'skill', 'name': 'not-published', 'description': '核对资料',
                 'body': '# 核对\n1. 比较输入字段、编码与数据类型。\n2. 核对输出并保留原始数据。\n' * 4}
    assert module.promote_verified_asset(tmp_path, candidate) is None


# LLM: 没有并发写入时仍用真实默认静态验证、真实发布和读回，不以拒绝所有请求伪造竞态修复。
# 函数用途: 正常晋升返回准确已发布版本的证明。
def test_r2_skill_promotion_returns_own_receipt(tmp_path):
    from agent_py_agent.agent.capability.asset_promotion import promote_verified_asset

    candidate = {'kind': 'skill', 'name': 'receipt-own', 'description': '核对资料',
                 'body': '# 核对\n1. 比较输入字段、编码与数据类型。\n2. 核对输出并保留原始数据。\n' * 4}
    result = promote_verified_asset(tmp_path, candidate)
    store = SkillLearningStore(tmp_path / 'data' / 'skill_learning', tmp_path / 'skills')
    record = store.load_registry().skills[candidate['name']]
    assert result == {'asset_id': record.name, **record.maintenance['verification_receipt']}
    assert result['sha256'] == record.sha256


# LLM: 真实发布后仅改回执或目标正文，保持事件不变；不以registry单独匹配代替正式目标与证明绑定。
# 函数用途: 拒绝摘要被换的证明及发布后被改写的活动文件。
@pytest.mark.parametrize('damage', ['receipt', 'body'])
def test_r2_skill_receipt_and_target_must_match_event(tmp_path, monkeypatch, damage):
    from agent_py_agent.agent.capability import asset_promotion as module
    from agent_py_agent.agent.capability.skill_learning_publish import SkillLearningGateError

    original = module.promote_verified_skill

    # LLM: 保留真实发布/验证，仅在读回前制造持久化损坏；正常锁内更新由另一条A/B测试覆盖。
    # 函数用途: 区分registry字段匹配与回执、正文真的匹配。
    def interleave(store, request, *args, **kwargs):
        event = original(store, request, *args, **kwargs)
        with store.state_lock():
            registry = store.load_registry()
            rec = registry.skills[event.skill_name]
            if damage == 'receipt':
                proof = {**rec.maintenance['verification_receipt'], 'sha256': '0' * 64}
                changed = replace(rec, maintenance={**rec.maintenance, 'verification_receipt': proof})
                store.save_registry(replace(registry, skills={**registry.skills, rec.name: changed}))
            else:
                store.skill_path(rec.name).write_text('# 用户改写的正文\n')
        return event

    monkeypatch.setattr(module, 'promote_verified_skill', interleave)
    candidate = {'kind': 'skill', 'name': 'proof-binding', 'description': '核对资料',
                 'body': '# 核对\n1. 比较输入字段、编码与数据类型。\n2. 核对输出并保留原始数据。\n' * 4}
    with pytest.raises(SkillLearningGateError, match='LEARNING_FORMAL_REVISION_CHANGED'):
        module.promote_verified_asset(tmp_path, candidate)


# LLM: 真实发布后锁内换同字节目录、文件或proof字段；revision/hash不变仍须拒绝不同身份。
# 函数用途: 防止相同正文掩盖不同的安装对象或验证证明。
@pytest.mark.parametrize('change', ['directory_same_body', 'file_same_body', 'receipt_id_only', 'executed_only', 'kind_only'])
def test_r3_receipt_binds_installed_objects_and_original_proof(tmp_path, monkeypatch, change):
    from agent_py_agent.agent.capability import asset_promotion as module
    from agent_py_agent.agent.capability.skill_learning_publish import SkillLearningGateError

    original = module.promote_verified_skill
    observed = []

    # LLM: 只包装真实发布后的交错点，移动原对象避免inode复用；不伪造发布成功。
    # 函数用途: 保持摘要相同，单独替换物理对象或证明字段。
    def interleave(store, request, *args, **kwargs):
        event = original(store, request, *args, **kwargs)
        with store.state_lock():
            registry = store.load_registry()
            record = registry.skills[event.skill_name]
            path = store.skill_path(record.name)
            proof = dict(record.maintenance['verification_receipt'])
            if change == 'directory_same_body':
                body, previous = path.read_bytes(), path.parent.stat()
                path.parent.rename(store.directory / 'original-installation')
                path.parent.mkdir()
                path.write_bytes(body)
                (path.parent / 'extra.txt').write_text('unscanned extra member')
                assert path.parent.stat().st_ino != previous.st_ino
            elif change == 'file_same_body':
                body, previous = path.read_bytes(), path.stat()
                path.rename(store.directory / 'original-skill.md')
                path.write_bytes(body)
                assert path.stat().st_ino != previous.st_ino
            else:
                key, value = {'receipt_id_only': ('receipt_id', 'different-host-receipt'),
                              'executed_only': ('executed', True),
                              'kind_only': ('verification_kind', 'runtime_execution')}[change]
                assert proof[key] != value
                changed = replace(record, maintenance={**record.maintenance, 'verification_receipt': {**proof, key: value}})
                store.save_registry(replace(registry, skills={**registry.skills, record.name: changed}))
            observed.append(record.sha256)
        return event

    monkeypatch.setattr(module, 'promote_verified_skill', interleave)
    candidate = {'kind': 'skill', 'name': 'original-proof', 'description': '核对资料',
                 'body': '# 核对\n1. 比较输入字段、编码与数据类型。\n2. 核对输出并保留原始数据。\n' * 4}
    with pytest.raises(SkillLearningGateError, match='LEARNING_FORMAL_REVISION_CHANGED'):
        module.promote_verified_asset(tmp_path, candidate)
    assert len(observed) == 1
    store = SkillLearningStore(tmp_path / 'data' / 'skill_learning', tmp_path / 'skills')
    assert store.load_registry().skills[candidate['name']].sha256 == observed[0]


# LLM: 创建和更新均走真实发布；内部身份与proof冻结，to_record仍只输出原公共事件字段。
# 函数用途: 检查绑定随真实写入换代，且不改变账本序列化格式。
def test_r3_publish_event_retains_binding_without_changing_public_record(tmp_path):
    import json

    from agent_py_agent.agent.capability.skill_learning_promotion import promote_verified_skill
    from agent_py_agent.agent.capability.skill_learning_publish import PublishRequest

    store = SkillLearningStore(tmp_path / 'data' / 'skill_learning', tmp_path / 'skills')
    decision = SkillLearningDecision('create', '核对', name='bound-event', description='核对资料',
                                    body='# 核对\n1. 比较输入字段、编码与数据类型。\n2. 核对输出并保留原始数据。\n' * 4)
    request = PublishRequest(decision)
    for expected_event in ('published', 'updated'):
        event = promote_verified_skill(store, request)
        assert event.event == expected_event
        record = store.load_registry().skills[decision.name]
        path = store.skill_path(record.name)
        directory, leaf = path.parent.stat(), path.stat()
        assert getattr(event, 'target_dir_identity', None) == (directory.st_dev, directory.st_ino)
        assert getattr(event, 'skill_file_identity', None) == (leaf.st_dev, leaf.st_ino)
        frozen = getattr(event, 'verification_proof_json', '')
        assert frozen and json.loads(frozen) == record.maintenance['verification_receipt']
        record.maintenance['verification_receipt']['receipt_id'] = 'changed-local-copy'
        assert event.verification_proof_json == frozen
        assert set(event.to_record()) == {'schema_version', 'event_id', 'at', 'event', 'code', 'skill_name',
                                          'version', 'sha256', 'request_key', 'run_id', 'reason', 'carrier'}
        request = PublishRequest(replace(decision, decision='update', body=decision.body + '\n核对空值。'),
                                 expected_revision=record.revision, expected_sha256=record.sha256)
