# LLM: 所有来源和暂存均为pytest隔离目录；不使用真实Owner，不放松nofollow、身份或配额门。
# 模块用途: 回归e905学习staging与精确资产身份，防安全复制和语义族误复用退化。
import os
import shutil
from dataclasses import replace

import pytest

from agent_py_agent.agent.capability.asset_promotion import _stage_dir, register_asset
from agent_py_agent.agent.capability.skill_learning_promotion import propose_or_promote_skill
from agent_py_agent.agent.capability.skill_learning_prompt import SkillLearningDecision
from agent_py_agent.agent.capability.skill_learning_publish import (
    PublishRequest,
    SkillLearningGateError,
)
from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore


# LLM: 不相关目录不能进执行沙箱；故意在技能目录挂根外文件，默认无依赖时不复制全库。
# 函数用途: 防止copytree把不相关文件和链接秘密带进候选执行环境。
def test_asset_promotion_stage_does_not_copy_unrequested_libraries(tmp_path):
    home = tmp_path / 'home'
    skills = home / 'skills'
    skills.mkdir(parents=True)
    marker = tmp_path / 'outside'
    marker.write_text('outside-secret')
    (skills / 'leak').symlink_to(marker)
    with _stage_dir(home) as stage:
        assert not (stage / 'skills' / 'leak').exists()
    assert marker.read_text() == 'outside-secret'
    assert not list((home / 'data' / 'skill_learning').glob('_stage_*'))


# LLM: family只用于推荐；规范ID、成员、依赖和scope变化必须成为独立精确身份。
# 函数用途: 不让同正文包或改名技能取得另一正式资产的身份和哈希。
def test_asset_promotion_registry_keeps_distinct_exact_identities(tmp_path):
    store = SkillLearningStore(tmp_path / 'data' / 'skill_learning', tmp_path / 'skills')
    first = {'name': 'one', 'body': '# 方法\n1. 核对输入\n', 'files': {'a.txt': 'one'}, 'version': '1'}
    a = register_asset(store, kind='capability_package', asset_id='one', asset=first, source={}, sha256='a'*64)
    second = {**first, 'name': 'two', 'files': {'a.txt': 'two'}}
    b = register_asset(store, kind='capability_package', asset_id='two', asset=second, source={}, sha256='b'*64)
    assert (a['asset_id'], b['asset_id']) == ('one', 'two')
    rows = list(store.load_registry().assets.values())
    assert {r['asset_id'] for r in rows} == {'one', 'two'}
    assert a['identity_sha256'] != b['identity_sha256']


# LLM: 不关闭自主发布；只禁止同族改名被误判为已经验证的同一规范Skill。
# 函数用途: 两个相同方法的不同规范ID都经原安全发布链，而不是回用旧资产名。
def test_asset_promotion_renamed_skill_is_not_family_reuse(tmp_path):
    store = SkillLearningStore(tmp_path / 'data' / 'skill_learning', tmp_path / 'skills')
    dec = SkillLearningDecision('create', '方法', name='alpha', description='核对资料', when_to_use='整理时', body='# 核对\n1. 检查输入\n2. 比较输出\n')
    first = propose_or_promote_skill(store, PublishRequest(dec))
    second = propose_or_promote_skill(store, PublishRequest(replace(dec, name='beta')))
    assert first.event == second.event == 'published'
    assert second.skill_name == 'beta'
    assert set(store.load_registry().skills) == {'alpha', 'beta'}


# LLM: 明确依赖必须沿原根strict nofollow，根外链接、父链接、硬链接均在复制前拒绝，不遗留stage。
# 函数用途: 覆盖依赖读取的链接和预算失败路径，不让copytree问题换个入口复活。
@pytest.mark.parametrize('damage', ['leaf_link', 'parent_link', 'hardlink', 'oversized', 'missing'])
def test_asset_promotion_stage_dependency_failures_cleanup(tmp_path, damage, monkeypatch, record_property):
    home = tmp_path / 'owner'
    root = home / 'scripts' / 'dep'
    root.mkdir(parents=True)
    outside = tmp_path / 'outside'
    outside.write_text('untouched')
    leaf = root / 'main.py'
    if damage == 'leaf_link':
        leaf.symlink_to(outside)
    elif damage == 'parent_link':
        root.rmdir()
        root.symlink_to(tmp_path, target_is_directory=True)
    elif damage == 'hardlink':
        try:
            os.link(outside, leaf)
            record_property('hardlink_fixture', 'real_os_link')
        except PermissionError:
            # 本机实测os.link返回EPERM；不跳过拒绝门，改为注入叶FD的双链接元信息，并明确不算真实硬链接验证。
            import stat
            from types import SimpleNamespace
            leaf.write_text('untouched')
            original_stat = os.fstat
            inode = leaf.stat().st_ino

            # LLM: 只为本测试叶FD模拟nlink=2，其余目录和文件保持原stat，不替代产品拒绝逻辑。
            # 函数用途: 在宿主不能创建hardlink时继续测试strict nofollow对双链接叶的硬拒绝。
            def linked_metadata(descriptor):
                value = original_stat(descriptor)
                if value.st_ino == inode and stat.S_ISREG(value.st_mode):
                    return SimpleNamespace(**{key: getattr(value, key) for key in dir(value) if key.startswith('st_') and key != 'st_nlink'}, st_nlink=2)
                return value

            monkeypatch.setattr(os, 'fstat', linked_metadata)
            record_property('hardlink_fixture', 'simulated_nlink_after_os_link_permission_error')
    elif damage == 'oversized':
        leaf.write_bytes(b'x' * (4 * 1024 * 1024 + 1))
    with pytest.raises((OSError, ValueError, SkillLearningGateError)):
        with _stage_dir(home, dependencies=(('scripts', 'dep', 'main.py'),)):
            pytest.fail('invalid dependency entered sandbox')
    assert outside.read_text() == 'untouched'
    assert not list((home / 'data' / 'skill_learning').glob('_stage_*'))


# LLM: 清理失败只登记本次stage/inode与墓碑名；测试自行按该残留定位回收，不假定原stage名字仍存在。
# 函数用途: 故障注入验证cleanup_pending可定位同一残留对象，不能ignore_errors吞掉。
def test_asset_promotion_cleanup_failure_is_recorded(tmp_path, monkeypatch):
    home = tmp_path / 'owner'
    home.mkdir()
    original = shutil.rmtree
    stages = []

    # LLM: 只拒绝本次stage清理，保留真实nofollow支持标记，pytest自己目录仍使用原清理。
    # 函数用途: 模拟磁盘或权限导致的暂存清理失败。
    def fail_stage(path, *args, **kwargs):
        if path == '.' or '_stage_' in str(path):
            raise OSError('cleanup failed')
        return original(path, *args, **kwargs)

    fail_stage.avoids_symlink_attacks = original.avoids_symlink_attacks
    with monkeypatch.context() as patch:
        patch.setattr(shutil, 'rmtree', fail_stage)
        with pytest.raises(OSError):
            with _stage_dir(home) as stage:
                stages.append(stage)
    receipt = home / 'data' / 'skill_learning' / 'cleanup_pending' / (stages[0].name + '.json')
    assert receipt.exists() and stages[0].name in receipt.read_text()
    import json
    row = json.loads(receipt.read_text())
    tombstone = stages[0].parent / row['tombstone']
    assert (tombstone.stat().st_dev, tombstone.stat().st_ino) == tuple(row['identity'])
    original(tombstone)


# LLM: 仅执行器结果为确定性替身，写入/CAS及并发B均为真Store；不声称这里验证了沙箱执行能力。
# 函数用途: 复现验证A保存后被B替换，不能给B签发A的执行receipt。
def test_asset_promotion_rejects_version_replaced_after_commit(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.assets.script_store import ScriptStore
    from agent_py_agent.agent.capability.asset_promotion import promote_verified_asset
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.workflows import ScriptRunner

    home = ensure_my_agent_home(tmp_path / 'home').owner_home_dir
    original = ScriptStore.save_script
    replaced = []

    # LLM: 在正式保存返回准确A记录后、handler读回前插入真实B版本，stage保存不受影响。
    # 函数用途: 用单线程确定性时序复现并发提交窗口，不靠sleep。
    def race(self, descriptor, code, **kwargs):
        result = original(self, descriptor, code, **kwargs)
        if self.owner_home == home and not replaced:
            replaced.append(True)
            original(self, descriptor, 'print(\'{"executed": false}\')', expected_revision=result['revision'])
        return result

    monkeypatch.setattr(ScriptStore, 'save_script', race)
    monkeypatch.setattr(ScriptRunner, 'run_script', lambda *a, **kw: SimpleNamespace(ok=True, output_data={'executed': True}))
    with pytest.raises(SkillLearningGateError):
        promote_verified_asset(home, {'kind': 'script', 'name': 'race-script', 'description': '核对提交', 'code': 'print(\'{"executed": true}\')'})
    assert replaced
    assert not SkillLearningStore(home / 'data' / 'skill_learning', home / 'skills').load_registry().assets


# LLM: 原目录被改名后放回替代目录，清理只能拒绝并登记原inode，不能删除替代用户文件。
# 函数用途: 验证stage对象身份而非同名路径决定清理归属。
def test_asset_promotion_stage_replacement_keeps_both_objects(tmp_path):
    home = tmp_path / 'owner'
    home.mkdir()
    moved = None
    with pytest.raises(SkillLearningGateError):
        with _stage_dir(home) as stage:
            moved = stage.with_name(stage.name + '-moved')
            stage.rename(moved)
            stage.mkdir()
            (stage / 'user.txt').write_text('keep')
    assert moved.exists() and (stage / 'user.txt').read_text() == 'keep'
    receipt = stage.parent / 'cleanup_pending' / (stage.name + '.json')
    assert receipt.exists() and str(moved.stat().st_ino) in receipt.read_text()


# LLM: 依赖文件变化要在最终Commit拒绝；执行器替身仅制造验证成功时序，不伪称真实沙箱业务证据。
# 函数用途: 验证工作流不能把依赖A的验证结果用于引用已换版依赖B的新定义。
def test_asset_promotion_workflow_dependency_change_rejects_commit(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.assets.descriptor import AssetDescriptor
    from agent_py_agent.agent.assets.script_store import ScriptStore
    from agent_py_agent.agent.assets.workflow_store import WorkflowStore
    from agent_py_agent.agent.capability.asset_promotion import promote_verified_asset
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.workflows import WorkflowEngine

    home = ensure_my_agent_home(tmp_path / 'home').owner_home_dir
    descriptor = AssetDescriptor(id='dependency', kind='script', name='dependency')
    scripts = ScriptStore(home)
    scripts.save_script(descriptor, 'print("{}")')
    monkeypatch.setattr(WorkflowEngine, 'run_workflow', lambda *a, **kw: SimpleNamespace(status='succeeded', final_output={}))

    # LLM: 仅第一次短Commit前改变真实脚本版本，原workflow存储/CAS不替代。
    # 函数用途: 确定性触发Verify后、写入前的依赖换版窗口。
    def commit(operation):
        current = scripts.get_script('dependency')
        scripts.save_script(descriptor, 'print(1)', expected_revision=current['revision'])
        return operation()

    with pytest.raises(SkillLearningGateError):
        promote_verified_asset(home, {'kind': 'workflow', 'name': 'with-dependency', 'description': '核对依赖',
            'steps': [{'step_id': 'one', 'name': '执行依赖', 'mode': 'deterministic_script', 'script_id': 'dependency'}]}, commit=commit)
    assert WorkflowStore(home).get_workflow('with-dependency') is None


# LLM: 验证输入、成员、版本、依赖和声明范围变化均使精确身份变化，不能套旧receipt。
# 函数用途: 将验证观察条件和完整包定义锁入identity回归。
@pytest.mark.parametrize('field,value', [('sample_inputs', {'n': 2}), ('version', '2.0'), ('files', {'a': 'new'}),
                                        ('dependencies', ['new']), ('application_scope', {'domains': ['other']})])
def test_asset_promotion_identity_tracks_full_verification_context(tmp_path, field, value):
    from agent_py_agent.agent.capability.asset_promotion import asset_identity
    candidate = {'name': 'one', 'body': 'method', 'sample_inputs': {'n': 1}, 'version': '1.0', 'files': {'a': 'old'}}
    assert asset_identity('capability_package', candidate, tmp_path) != asset_identity('capability_package', {**candidate, field: value}, tmp_path)


# LLM: 交换发生在yield后首次写入前；保留真实publisher与文件系统，只有交错时点由wrapper指定。
# 函数用途: 暂存名被换成根外链接时，SKILL正文不得先写出去再报错。
def test_stage_yield_symlink_swap_never_writes_outside(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability import skill_learning_publish as module

    store = SkillLearningStore(tmp_path / 'home' / 'data' / 'skill_learning', tmp_path / 'home' / 'skills')
    outside = tmp_path / 'outside'
    outside.mkdir()
    original = module._staged_skill

    # LLM: 重命名真实stage并放入真实软链接，后续正文写入、解析、扫描仍调用产品代码。
    # 函数用途: 固定yield与_staged_skill之间的替换窗口。
    def swap(store, stage, publish, text, **kwargs):
        stage.rename(stage.with_name(stage.name + '-moved'))
        stage.symlink_to(outside, target_is_directory=True)
        return original(store, stage, publish, text, **kwargs)

    monkeypatch.setattr(module, '_staged_skill', swap)
    decision = SkillLearningDecision('create', '核对', name='safe-method', description='核对资料', body='# 检查\n1. 比较输入\n')
    with pytest.raises((OSError, SkillLearningGateError)):
        module.publish_learned_skill(store, PublishRequest(decision))
    assert list(outside.iterdir()) == []
    assert not store.load_registry().skills
    assert not (store.learned_root / decision.name).exists()


# LLM: 只代理asset_promotion本模块的stat，保留nofollow能力检测及真实rmtree；在最后核对返回后真实换目录。
# 函数用途: 防清理检查与删除之间换入的用户文件被误删。
def test_stage_cleanup_stat_swap_preserves_replacement(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from agent_py_agent.agent.capability import asset_promotion as module

    home = tmp_path / 'home'
    home.mkdir()
    state = {'armed': False, 'swapped': False}
    original_stat = os.stat

    # LLM: 返回真实旧stat结果后才换目录，不能让前面的身份门提前发现并掩盖TOCTOU。
    # 函数用途: 确定性复现pre-cleanup stat与rename/rmtree之间的交错。
    def observe(name, *args, **kwargs):
        result = original_stat(name, *args, **kwargs)
        if state['armed'] and not state['swapped'] and name == stage.name and kwargs.get('dir_fd') is not None:
            state['swapped'] = True
            stage.rename(stage.with_name(stage.name + '-moved'))
            stage.mkdir()
            (stage / 'user.txt').write_text('keep-user-file')
        return result

    monkeypatch.setattr(module, 'os', SimpleNamespace(**{**vars(os), 'stat': observe}))
    with pytest.raises((OSError, SkillLearningGateError)):
        with module._stage_dir(home) as stage:
            original_identity = (stage.stat().st_dev, stage.stat().st_ino)
            state['armed'] = True
    assert state['swapped']
    files = list(stage.parent.rglob('user.txt'))
    assert len(files) == 1 and files[0].read_text() == 'keep-user-file'
    pending = json.loads((stage.parent / 'cleanup_pending' / (stage.name + '.json')).read_text())
    assert tuple(pending['identity']) == original_identity


# LLM: 真实只读子目录必须先对已核验FD修权限再清理；不修外部链接目标或用户替代目录。
# 函数用途: 防候选留下只读子树使每次晋升都失败并积累残留。
def test_stage_cleanup_repairs_readonly_subdirectories(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    with _stage_dir(home) as stage:
        child = stage / 'readonly'
        child.mkdir()
        (child / 'result.txt').write_text('temporary')
        child.chmod(0o500)
    assert not stage.exists()
    assert not list(stage.parent.glob('.purge_*'))
    assert not list((stage.parent / 'cleanup_pending').glob('*.json'))


# LLM: 仅第一次stage清理注入EACCES；正式发布/登记/回滚/重试全走真实组件。
# 函数用途: 已移动Skill遇stage收尾失败必须一致回滚并可干净重试。
def test_stage_cleanup_failure_after_move_rolls_back_and_retry_succeeds(tmp_path, monkeypatch):
    import errno

    from agent_py_agent.agent.capability import skill_learning_publish as module
    from agent_py_agent.agent.capability.skill_snapshot import skill_content_sha256

    store = SkillLearningStore(tmp_path / 'home' / 'data' / 'skill_learning', tmp_path / 'home' / 'skills')
    decision = SkillLearningDecision('create', '核对', name='retry-method', description='核对资料', body='# 检查\n1. 比较输入\n')
    original, failures = shutil.rmtree, []

    # LLM: 故障只命中stage/tombstone，不影响正式目录回滚及pytest临时目录清理。
    # 函数用途: 在rename已完成后让stage清理失败一次。
    def fail_once(path, *args, **kwargs):
        if (path == '.' or '_stage_' in str(path)) and not failures:
            failures.append(True)
            raise PermissionError(errno.EACCES, 'injected stage cleanup')
        return original(path, *args, **kwargs)

    fail_once.avoids_symlink_attacks = original.avoids_symlink_attacks
    with monkeypatch.context() as patch:
        patch.setattr(shutil, 'rmtree', fail_once)
        with pytest.raises(SkillLearningGateError):
            module.publish_learned_skill(store, PublishRequest(decision))
    assert failures
    assert not (store.learned_root / decision.name).exists()
    assert decision.name not in store.load_registry().skills
    event = module.publish_learned_skill(store, PublishRequest(decision))
    assert event.event == 'published'
    text = (store.learned_root / decision.name / 'SKILL.md').read_text()
    assert store.load_registry().skills[decision.name].sha256 == skill_content_sha256(text)


# LLM: 支持旧路径重开与新版固定FD下子目录打开两个等价注入点；只有取得target_fd后的第二个资源获取失败。
# 函数用途: EMFILE发生时，第一个目标目录FD必须已关闭。
def test_stage_second_directory_open_failure_closes_target_fd(tmp_path, monkeypatch):
    import errno

    from agent_py_agent.agent.capability import skill_learning_publish as module
    from agent_py_agent.agent.common import nofollow_fs

    store = SkillLearningStore(tmp_path / 'home' / 'data' / 'skill_learning', tmp_path / 'home' / 'skills')
    decision = SkillLearningDecision('create', '核对', name='fd-method', description='核对资料', body='# 核对\n1. 比较输入\n')
    opened, injected = [], []
    original, child_open = nofollow_fs.open_directory_beneath, nofollow_fs._openat_directory

    # LLM: 第一次返回真实target_fd；旧版随后按stage路径重开时制造EMFILE，不替代其它打开。
    # 函数用途: 捕获需要独立释放的第一个FD。
    def directory(root, parts, **kwargs):
        if opened and not parts and str(root).split('/')[-1].startswith('_stage_'):
            injected.append(True)
            raise OSError(errno.EMFILE, 'second directory acquisition')
        result = original(root, parts, **kwargs)
        if parts == ('skills', 'learned'):
            opened.append(result)
        return result

    # LLM: 新版不重开stage路径，在固定stage FD下第二次取得Skill目录FD时制造相同资源失败。
    # 函数用途: 保证改用FD以后仍真实覆盖第二个FD获取失败出口。
    def child(parent_fd, name):
        if opened and name == decision.name:
            injected.append(True)
            raise OSError(errno.EMFILE, 'second directory acquisition')
        return child_open(parent_fd, name)

    monkeypatch.setattr(nofollow_fs, 'open_directory_beneath', directory)
    monkeypatch.setattr(nofollow_fs, '_openat_directory', child)
    with pytest.raises(SkillLearningGateError):
        module.publish_learned_skill(store, PublishRequest(decision))
    assert injected and len(opened) == 1
    with pytest.raises(OSError) as closed:
        os.fstat(opened[0])
    assert closed.value.errno == errno.EBADF


# LLM: 在无关外层except中调用stage，清理失败只能归因本次cleanup，不能重抛/标注外层异常。
# 函数用途: 防sys.exception把调用方异常冒认成本次首因。
def test_stage_cleanup_does_not_capture_unrelated_outer_exception(tmp_path, monkeypatch):
    original = shutil.rmtree

    # LLM: 只拒绝本次stage/tombstone，保留真实rmtree的安全能力标记。
    # 函数用途: 注入确定的清理失败供首因断言。
    def fail_stage(path, *args, **kwargs):
        if path == '.' or '_stage_' in str(path):
            raise OSError('cleanup only')
        return original(path, *args, **kwargs)

    fail_stage.avoids_symlink_attacks = original.avoids_symlink_attacks
    home = tmp_path / 'home'
    home.mkdir()
    failure = None
    try:
        raise ValueError('unrelated caller exception')
    except ValueError as outer:
        with monkeypatch.context() as patch:
            patch.setattr(shutil, 'rmtree', fail_stage)
            try:
                with _stage_dir(home):
                    pass
            except BaseException as exc:
                failure = exc
        assert isinstance(failure, OSError), type(failure)
        assert not getattr(outer, '__notes__', ())


# LLM: 坏JSON、非对象、缺入口、无效入口均是损坏依赖，不能抛裸JSONDecodeError/KeyError或意外通过。
# 函数用途: 为工作流依赖损坏提供稳定结构化错误码。
@pytest.mark.parametrize('metadata', ['{', '{}', '[]', '{"entrypoint":null}'])
def test_workflow_dependency_invalid_metadata_is_structured(tmp_path, metadata):
    from agent_py_agent.agent.capability.asset_promotion import _workflow_dependencies

    folder = tmp_path / 'scripts' / 'dep'
    folder.mkdir(parents=True)
    (folder / 'script.json').write_text(metadata)
    with pytest.raises(SkillLearningGateError) as error:
        _workflow_dependencies(tmp_path, [{'script_id': 'dep'}])
    assert error.value.code == 'LEARNING_DEPENDENCY_INVALID'


# LLM: 只替代Skill晋升结果制造并发记录缺失/旧版无receipt；原registry加载与载体分派保持真实。
# 函数用途: Skill载体缺正式记录或验证证明时返回未晋升，而非崩溃或伪造成功。
@pytest.mark.parametrize('missing', ['record', 'receipt'])
def test_skill_carrier_missing_record_or_receipt_is_not_success(tmp_path, monkeypatch, missing):
    from types import SimpleNamespace

    from agent_py_agent.agent.capability import asset_promotion as module
    from agent_py_agent.agent.capability.skill_learning_store import LearnedSkill

    store = SkillLearningStore(tmp_path / 'data' / 'skill_learning', tmp_path / 'skills')
    if missing == 'receipt':
        record = LearnedSkill('legacy-method', 1, 'a'*64, 'now', 'now')
        with store.state_lock():
            store.save_registry(replace(store.load_registry(), skills={record.name: record}))
    monkeypatch.setattr(module, 'promote_verified_skill', lambda *a, **kw: SimpleNamespace(skill_name='legacy-method'))
    result = module.promote_verified_asset(tmp_path, {'kind': 'skill', 'name': 'legacy-method', 'description': '核对资料',
        'body': '# 核对\n1. 检查输入和输出，保留异常证据。\n2. 失败时停止处理，不修改原件。\n' * 3})
    assert result is None


# LLM: 在随机墓碑的真实stat返回后替换目录，证明随机名字本身不是原inode保证；只代理本模块os。
# 函数用途: 回归墓碑核验后的第二个清理窗口，替代目录中的用户文件不得删除。
def test_stage_tombstone_swap_never_deletes_replacement_files(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.capability import asset_promotion as module

    home = tmp_path / 'home'
    home.mkdir()
    swapped = []
    original_stat = os.stat

    # LLM: 先取真实墓碑元信息再交换真实目录，后续修权限/删除仍由原产品执行。
    # 函数用途: 把交错固定在墓碑身份检查之后，而不是最早的stage名字检查。
    def observe(name, *args, **kwargs):
        result = original_stat(name, *args, **kwargs)
        if str(name).startswith('.purge_') and kwargs.get('dir_fd') is not None and not swapped:
            swapped.append(True)
            tombstone = home / 'data' / 'skill_learning' / name
            tombstone.rename(tombstone.with_name(tombstone.name + '-moved'))
            tombstone.mkdir()
            (tombstone / 'user.txt').write_text('keep-after-tombstone-check')
        return result

    monkeypatch.setattr(module, 'os', SimpleNamespace(**{**vars(os), 'stat': observe}))
    try:
        with module._stage_dir(home):
            pass
    except (OSError, SkillLearningGateError):
        pass
    assert swapped
    files = list((home / 'data' / 'skill_learning').rglob('user.txt'))
    assert len(files) == 1 and files[0].read_text() == 'keep-after-tombstone-check'
    assert list((home / 'data' / 'skill_learning' / 'cleanup_pending').glob('*.json'))


# LLM: 业务首因与真实FD关闭后的注入异常同时出现，所有FD仍关闭，主异常必须是原对象。
# 函数用途: 补足ExitStack继续关闭与不掩盖首因是两项不同保证。
def test_stage_fd_close_failure_preserves_primary_exception(tmp_path, monkeypatch):
    import errno

    from agent_py_agent.agent.capability import asset_promotion as module

    home = tmp_path / 'home'
    home.mkdir()
    original_close, failed, state = os.close, [], {'fd': None}
    primary = SkillLearningGateError('ORIGINAL_BUSINESS_FAILURE')

    # LLM: 实际关闭指定stage FD后再报错；只注入一次，不影响其他FD的真实释放。
    # 函数用途: 让外层descriptor stack在已有业务错误时也遭遇关闭失败。
    def close(descriptor):
        original_close(descriptor)
        if descriptor == state['fd'] and not failed:
            failed.append(True)
            raise OSError(errno.EIO, 'injected close failure')

    observed = None
    with monkeypatch.context() as patch:
        patch.setattr(os, 'close', close)
        try:
            with module._stage_dir(home, with_fd=True) as (_, stage_fd):
                state['fd'] = stage_fd
                raise primary
        except BaseException as exc:
            observed = exc
    assert failed
    assert observed is primary, type(observed)
    assert any('close' in note for note in getattr(primary, '__notes__', ()))
    with pytest.raises(OSError) as error:
        os.fstat(stage_fd)
    assert error.value.errno == errno.EBADF


# LLM: source名字在最后核验后换为另一真实目录；错误对象只能留隔离区，不能留在正式Skill名字下。
# 函数用途: 防move后发现inode不符却把未登记替代物暴露给正式发现入口。
def test_stage_source_swap_is_quarantined_before_formal_name(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.capability import skill_learning_publish as module

    store = SkillLearningStore(tmp_path / 'home' / 'data' / 'skill_learning', tmp_path / 'home' / 'skills')
    decision = SkillLearningDecision('create', '核对', name='source-race', description='核对资料', body='# 核对\n1. 比较输入\n')
    original_rename, swapped = os.rename, []

    # LLM: 仅在第一次移动Skill子目录前换源；本模块局部os代理保留原nofollow能力检测与registry门。
    # 函数用途: 把已扫描A替换为未扫描B，核对B不会出现在正式名称下。
    def rename(source, target, **kwargs):
        if source == decision.name and not swapped and kwargs.get('src_dir_fd') is not None:
            swapped.append(True)
            stage = next((store.skills_root.parent / 'data' / 'skill_learning').glob('_stage_*'))
            (stage / source).rename(stage / (source + '-original'))
            (stage / source).mkdir()
            (stage / source / 'user.txt').write_text('unverified-replacement')
        return original_rename(source, target, **kwargs)

    monkeypatch.setattr(module, 'os', SimpleNamespace(**{**vars(os), 'rename': rename}))
    with pytest.raises(SkillLearningGateError):
        module.publish_learned_skill(store, PublishRequest(decision))
    assert swapped
    assert not (store.learned_root / decision.name).exists()
    assert decision.name not in store.load_registry().skills
    files = list((store.skills_root.parent / 'data' / 'skill_learning').rglob('user.txt'))
    assert len(files) == 1 and files[0].read_text() == 'unverified-replacement'


# LLM: 覆盖元数据准备及版本持久化的非OS异常，不把补偿限定为rmtree的EACCES一种形态。
# 函数用途: 任何登记前错误都不能留下无registry的正式新Skill。
@pytest.mark.parametrize('failure_at', ['metadata', 'version'])
def test_stage_post_move_registration_failures_leave_no_unregistered_skill(tmp_path, monkeypatch, failure_at):
    from agent_py_agent.agent.capability import skill_learning_publish as module

    store = SkillLearningStore(tmp_path / 'home' / 'data' / 'skill_learning', tmp_path / 'home' / 'skills')
    decision = SkillLearningDecision('create', '核对', name='registration-failure', description='核对资料', body='# 核对\n1. 比较输入\n')

    # LLM: 仅在指定的准备或持久化边界抛原ValueError，真实安装/registry及补偿路径不替代。
    # 函数用途: 测出异常不在旧OSError捕获集合内时是否仍保磁盘/登记一致。
    def fail(*args, **kwargs):
        raise ValueError('injected registration failure')

    monkeypatch.setattr(module, 'utc_now', fail) if failure_at == 'metadata' else monkeypatch.setattr(store, 'save_version', fail)
    with pytest.raises(ValueError):
        module.publish_learned_skill(store, PublishRequest(decision))
    assert not (store.learned_root / decision.name).exists()
    assert decision.name not in store.load_registry().skills


# LLM: 更新真实已登记Skill后在版本保存处抛非OS错误，不能把新正文留给仍指旧hash的registry。
# 函数用途: 登记前任意异常也要将更新正文恢复到旧版，而不把方法永久变成user_modified。
def test_stage_update_registration_failure_restores_previous_text(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability import skill_learning_publish as module

    store = SkillLearningStore(tmp_path / 'home' / 'data' / 'skill_learning', tmp_path / 'home' / 'skills')
    decision = SkillLearningDecision('create', '核对', name='update-failure', description='核对资料', body='# 核对\n1. 比较输入\n')
    module.publish_learned_skill(store, PublishRequest(decision))
    record = store.load_registry().skills[decision.name]
    path = store.learned_root / decision.name / 'SKILL.md'
    previous = path.read_text()

    # LLM: 只拒绝更新版本文件的保存，真实原子正文替换与registry保持产品实现。
    # 函数用途: 强制触发replace之后、registry提交之前的非OS失败。
    def fail(*args, **kwargs):
        raise ValueError('injected update version failure')

    monkeypatch.setattr(store, 'save_version', fail)
    update = replace(decision, decision='update', update_target=decision.name, body=decision.body + '2. 检查异常\n')
    with pytest.raises(ValueError):
        module.publish_learned_skill(store, PublishRequest(update, expected_revision=record.revision, expected_sha256=record.sha256))
    assert path.read_text() == previous
    assert store.load_registry().skills[decision.name] == record


# LLM: 仅在第二次正式rename前换源，可附加一次目标打开EMFILE；发布/扫描/Store/发现仍为真实实现，未知内容只能保留隔离。
# 函数用途: 防未扫描SKILL被留在正式发现根，且隔离不能删除替代物或阻断干净重试。
@pytest.mark.parametrize('fail_open', [False, True])
def test_r2_final_rename_source_swap_is_quarantined_and_not_discoverable(tmp_path, monkeypatch, fail_open):
    import errno
    from types import SimpleNamespace

    from agent_py_agent.agent.capability import skill_learning_publish as module
    from agent_py_agent.agent.capability.skill_service import (
        SkillRoot,
        _discover_root,
        _parse_manifest_entry,
    )
    from agent_py_agent.agent.common import nofollow_fs

    store = SkillLearningStore(tmp_path / 'home' / 'data' / 'skill_learning', tmp_path / 'home' / 'skills')
    decision = SkillLearningDecision('create', '核对', name='final-source-race', description='核对资料', body='# 核对\n1. 比较输入\n')
    replacement = module.render_learned_skill(replace(decision, body='# 未扫描替代正文\n1. 保留原件\n'))
    original_rename, injected = os.rename, []
    original_open, failures = nofollow_fs._openat_directory, []

    # LLM: 将已扫描A另存，把真实B放到待rename的名字上；不替换扫描、回滚和发现逻辑。
    # 函数用途: 固定最后核验与正式移动之间的第二窗口。
    def swap(source, target, **kwargs):
        if str(source).startswith('.publish_') and target == decision.name and not injected:
            prepared = store.directory / source
            prepared.rename(prepared.with_name(prepared.name + '-original'))
            prepared.mkdir()
            (prepared / 'SKILL.md').write_text(replacement)
            injected.append((prepared.stat().st_dev, prepared.stat().st_ino))
        return original_rename(source, target, **kwargs)

    # LLM: 只有正式rename成功后的首次目标打开报EMFILE，回滚后续真实打开不替代；不需要第二次并发改名。
    # 函数用途: 覆盖未取得installed_fd时也必须移出正式根的异常出口。
    def open_target(parent_fd, name):
        if fail_open and injected and not failures and name == decision.name and (store.learned_root / name).exists():
            failures.append(True)
            raise OSError(errno.EMFILE, 'installed directory open failed')
        return original_open(parent_fd, name)

    with monkeypatch.context() as patch:
        patch.setattr(module, 'os', SimpleNamespace(**{**vars(os), 'rename': swap}))
        patch.setattr(nofollow_fs, '_openat_directory', open_target)
        with pytest.raises((SkillLearningGateError, OSError)) as failure:
            module.publish_learned_skill(store, PublishRequest(decision))
    assert injected
    assert bool(failures) == fail_open
    manifest, errors = _discover_root(SkillRoot(store.learned_root, 'owner'))
    assert not errors
    assert not [entry for item in manifest for entry, error in [_parse_manifest_entry(item)] if entry and entry.enabled]
    assert not (store.learned_root / decision.name).exists()
    assert decision.name not in store.load_registry().skills
    quarantined = list(store.directory.glob('.quarantine_*'))
    assert len(quarantined) == 1
    assert (quarantined[0].stat().st_dev, quarantined[0].stat().st_ino) == injected[0]
    assert (quarantined[0] / 'SKILL.md').read_text() == replacement
    assert isinstance(failure.value, SkillLearningGateError)
    assert failure.value.code == 'SKILL_LEARNING_INSTALL_FAILED'
    if fail_open:
        assert failure.value.__cause__.errno == errno.EMFILE
    assert module.publish_learned_skill(store, PublishRequest(decision)).event == 'published'


# LLM: 仅本模块chmod模拟旧glibc，禁用O_PATH；真实stage、目录FD、pending和删除路径不替代。
# 函数用途: 只读目录无需不支持的chmod；000失败也须结构化且关净全部FD。
@pytest.mark.parametrize('mode', [0o500, 0o000])
@pytest.mark.parametrize('unsupported', [ValueError, NotImplementedError])
def test_r3_old_glibc_cleanup_is_safe_and_structured(tmp_path, monkeypatch, mode, unsupported):
    import errno
    import json
    from types import SimpleNamespace

    from agent_py_agent.agent.capability import asset_promotion as module
    from agent_py_agent.agent.common import nofollow_fs

    opened, chmod_calls = [], []
    original_directory, original_child = nofollow_fs.open_directory_beneath, nofollow_fs._openat_directory

    # LLM: 跟踪真实父目录FD，不改变nofollow能力检测。
    # 函数用途: 记录必须由stage收尾的目录资源。
    def directory(*args, **kwargs):
        descriptor = original_directory(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    # LLM: 对000显式注入PermissionError使root跑测试也覆盖拒绝打开路径，其余返回真实FD。
    # 函数用途: 跨UID稳定复现无读权限分支并追踪子目录FD。
    def child(parent, name):
        if name == 'restricted' and mode == 0:
            raise PermissionError(errno.EACCES, 'unreadable directory')
        descriptor = original_child(parent, name)
        opened.append(descriptor)
        return descriptor

    # LLM: 只拒绝旧glibc不支持的参数组合，不替换fchmod及其它清理操作。
    # 函数用途: 确定性模拟CPython的旧平台异常。
    def chmod(path, value, **kwargs):
        if kwargs.get('dir_fd') is not None and kwargs.get('follow_symlinks') is False:
            chmod_calls.append(path)
            raise unsupported('chmod: cannot use dir_fd and follow_symlinks together')
        return os.chmod(path, value, **kwargs)

    proxy = SimpleNamespace(**{key: value for key, value in vars(os).items() if key != 'O_PATH'})
    proxy.chmod = chmod
    failure = None
    with monkeypatch.context() as patch:
        patch.setattr(module, 'os', proxy)
        patch.setattr(nofollow_fs, 'open_directory_beneath', directory)
        patch.setattr(nofollow_fs, '_openat_directory', child)
        try:
            with module._stage_dir(tmp_path) as stage:
                restricted = stage / 'restricted'
                restricted.mkdir()
                (restricted / 'result.txt').write_text('temporary')
                restricted.chmod(mode)
        except BaseException as exc:
            failure = exc
    try:
        assert opened
        for descriptor in opened:
            with pytest.raises(OSError) as closed:
                os.fstat(descriptor)
            assert closed.value.errno == errno.EBADF
        pending = list((stage.parent / 'cleanup_pending').glob('*.json'))
        if mode:
            assert failure is None
            assert not chmod_calls and not stage.exists()
            assert not list(stage.parent.glob('.purge_*')) and not pending
        else:
            assert isinstance(failure, SkillLearningGateError), repr(failure)
            assert failure.code == 'LEARNING_CLEANUP_UNSUPPORTED'
            assert chmod_calls == ['restricted'] and len(pending) == 1
            row = json.loads(pending[0].read_text())
            tombstone = stage.parent / row['tombstone']
            assert (tombstone.stat().st_dev, tombstone.stat().st_ino) == tuple(row['identity'])
    finally:
        for tombstone in stage.parent.glob('.purge_*'):
            (tombstone / 'restricted').chmod(0o700)
            shutil.rmtree(tombstone)


# LLM: 真实写版本和registry后才注入异常，直接覆盖版本事务补偿而非用mock伪造已提交状态。
# 函数用途: 已落盘的新revision不能被后续异常误删版本文件。
def test_r3_committed_revision_survives_late_registry_error(tmp_path, monkeypatch):
    from contextlib import ExitStack

    from agent_py_agent.agent.capability import skill_learning_publish as module
    from agent_py_agent.agent.capability.skill_learning_store import LearnedSkill
    from agent_py_agent.agent.capability.skill_snapshot import skill_content_sha256

    store = SkillLearningStore(tmp_path / 'data' / 'skill_learning', tmp_path / 'skills')
    text, original = '# 已提交\n', store.save_registry
    record = LearnedSkill('committed', 2, skill_content_sha256(text), 'now', 'now', revision=2)
    primary = ValueError('post-registry-write failure')

    # LLM: 先运行真实原子保存再抛同一异常，迫使rollback读取落盘权威revision。
    # 函数用途: 模拟写成功但返回阶段发生故障。
    def save_then_fail(registry):
        original(registry)
        raise primary

    monkeypatch.setattr(store, 'save_registry', save_then_fail)
    with store.state_lock():
        with pytest.raises(ValueError) as caught:
            with ExitStack() as rollback:
                module._save_version_with_rollback(store, record.name, 2, text, rollback)
                store.save_registry(replace(store.load_registry(), skills={record.name: record}))
                rollback.pop_all()
        assert caught.value is primary
        assert store.load_registry().skills[record.name].revision == 2
        assert store.read_version(record.name, 2) == text


# LLM: 第二次正式rename前真实换源为外部目录链接；发布器只可隔离链接自身，并登记准确位置与lstat身份。
# 函数用途: 未扫描链接不进正式发现根，也不修改、修权限或删除外部内容。
def test_r3_final_rename_symlink_is_quarantined_and_recorded(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from agent_py_agent.agent.capability import skill_learning_publish as module

    store = SkillLearningStore(tmp_path / 'home' / 'data' / 'skill_learning', tmp_path / 'home' / 'skills')
    decision = SkillLearningDecision('create', '核对', name='symlink-source', description='核对资料', body='# 核对\n1. 比较输入\n')
    outside = tmp_path / 'external'
    outside.mkdir()
    (outside / 'SKILL.md').write_text('unscanned external skill')
    (outside / 'keep.txt').write_text('keep outside')
    outside.chmod(0o500)
    before = outside.stat()
    rename, identities = os.rename, []

    # LLM: 原prepare对象保留，替换为真实symlink，后续rename/open/quarantine仍由产品代码执行。
    # 函数用途: 在最后一次检查之后注入链接替换。
    def swap(source, target, **kwargs):
        if str(source).startswith('.publish_') and target == decision.name and not identities:
            prepared = store.directory / source
            prepared.rename(prepared.with_name(prepared.name + '-original'))
            prepared.symlink_to(outside, target_is_directory=True)
            identities.append((prepared.lstat().st_dev, prepared.lstat().st_ino))
        return rename(source, target, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(module, 'os', SimpleNamespace(**{**vars(os), 'rename': swap}))
            with pytest.raises(SkillLearningGateError, match='SKILL_LEARNING_INSTALL_FAILED'):
                module.publish_learned_skill(store, PublishRequest(decision))
        assert identities
        assert not os.path.lexists(store.learned_root / decision.name)
        assert decision.name not in store.load_registry().skills
        quarantined = list(store.directory.glob('.quarantine_*'))
        assert len(quarantined) == 1 and quarantined[0].is_symlink()
        assert (quarantined[0].lstat().st_dev, quarantined[0].lstat().st_ino) == identities[0]
        assert os.readlink(quarantined[0]) == str(outside)
        row = json.loads((store.directory / 'cleanup_pending' / (quarantined[0].name + '.json')).read_text())
        assert row['location'] == ['data', 'skill_learning', quarantined[0].name]
        assert tuple(row['identity']) == identities[0]
        after = outside.stat()
        assert (after.st_dev, after.st_ino, after.st_mode, after.st_mtime_ns) == (before.st_dev, before.st_ino, before.st_mode, before.st_mtime_ns)
        assert (outside / 'SKILL.md').read_text() == 'unscanned external skill'
        assert (outside / 'keep.txt').read_text() == 'keep outside'
    finally:
        outside.chmod(0o700)


# LLM: 经过真实创建/更新外层，registry已落盘后才抛普通异常或取消；版本保留不能同时把正式正文回滚掉。
# 函数用途: 保证写后异常的registry、版本和正式安装仍一致，并保持原异常对象。
@pytest.mark.parametrize('operation', ['create', 'update'])
@pytest.mark.parametrize('failure_type', [ValueError, KeyboardInterrupt])
def test_r3_published_commit_survives_late_registry_error(tmp_path, monkeypatch, operation, failure_type):
    from agent_py_agent.agent.capability import skill_learning_publish as module
    from agent_py_agent.agent.capability.skill_snapshot import skill_content_sha256

    store = SkillLearningStore(tmp_path / 'data' / 'skill_learning', tmp_path / 'skills')
    decision = SkillLearningDecision('create', '核对', name='committed-publication', description='核对资料', body='# 核对\n1. 比较输入\n')
    request = PublishRequest(decision)
    if operation == 'update':
        module.publish_learned_skill(store, request)
        previous = store.load_registry().skills[decision.name]
        request = PublishRequest(replace(decision, decision='update', body=decision.body + '2. 检查空值\n'),
                                 expected_revision=previous.revision, expected_sha256=previous.sha256)
    original = store.save_registry
    primary = failure_type('failure after durable commit')
    written = []

    # LLM: 保存真实registry并保留本次记录，之后抛原异常，绝不模拟版本补偿或正文回滚函数。
    # 函数用途: 用生产发布入口复现提交成功后返回失败的时序。
    def save_then_fail(registry):
        original(registry)
        written.append(registry.skills[decision.name])
        raise primary

    monkeypatch.setattr(store, 'save_registry', save_then_fail)
    with pytest.raises(failure_type) as failure:
        module.publish_learned_skill(store, request)
    assert failure.value is primary
    assert len(written) == 1
    record = store.load_registry().skills[decision.name]
    assert record == written[0]
    text = store.skill_path(record.name).read_text()
    assert skill_content_sha256(text) == record.sha256
    assert store.read_version(record.name, record.revision) == text
    assert not list(store.directory.glob('.purge_*'))
