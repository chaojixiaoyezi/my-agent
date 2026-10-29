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


# LLM: 清理失败是独立失败而非成功；只登记本次准确stage，不记录任意路径或秘密。
# 函数用途: 故障注入验证残留工作有可定位的cleanup_pending，不能ignore_errors吞掉。
def test_asset_promotion_cleanup_failure_is_recorded(tmp_path, monkeypatch):
    home = tmp_path / 'owner'
    home.mkdir()
    original = shutil.rmtree
    stages = []

    # LLM: 只拒绝本次stage清理，保留真实nofollow支持标记，pytest自己目录仍使用原清理。
    # 函数用途: 模拟磁盘或权限导致的暂存清理失败。
    def fail_stage(path, *args, **kwargs):
        if '_stage_' in str(path):
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
    original(stages[0])


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
