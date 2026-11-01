import errno
import json
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.skill_learning_prompt import parse_skill_learning_decision
from agent_py_agent.agent.capability.skill_learning_publish import (
    PublishRequest,
    SkillLearningGateError,
    publish_learned_skill,
    revert_learned_skill,
)
from agent_py_agent.agent.capability.skill_learning_store import (
    LearnedSkill,
    SkillLearningRegistry,
    SkillLearningStore,
    SkillLearningStoreError,
)
from agent_py_agent.agent.capability.skill_snapshot import skill_content_sha256
from agent_py_agent.tests.test_skill_learning import (
    BODY,
    NAME,
    _ctx,
    _learned_file,
    _output,
    _publish_first,
    _runtime,
)


def _update(ctx, run='run-2', body=BODY+'\n额外核对行数。'):
    used = [{'tool':'skill_search','ok':True,'parameters':{'skill_id':f'owner:{NAME}'}}]
    ctx.backend.responses.append(_output('update',update_target=NAME,body=body))
    ctx.service.enqueue_from_finalize(_ctx(run,archive_tool_calls=used))
    return ctx.service.run_pending()


# LLM: owner配额小快照带10ms墙钟额度；固定配额模块的单调钟，让真实准入只由128项额度决定，不随机器负载抖动。
# 函数用途: 为本文件所有真实配额准入固定小快照时钟。
@pytest.fixture(autouse=True)
def _pinned_quota_snapshot_clock(monkeypatch):
    from agent_py_agent.agent.user_space import owner_quota
    monkeypatch.setattr(owner_quota, "time", SimpleNamespace(**{**vars(time), "monotonic": lambda: 100.0}))


def test_update_after_revert_rejects_stale_summary_and_preserves_history(tmp_path):
    ctx = _runtime(tmp_path, _output())
    _publish_first(ctx)
    assert _update(ctx).status == 'updated'
    current = ctx.store.load_registry().skills[NAME]
    historical = ctx.store.read_version(NAME, 2)
    decision = parse_skill_learning_decision(_output('update',update_target=NAME,body=BODY+'\n迟到旧总结。'),frozenset({NAME}))
    revert_learned_skill(ctx.store, NAME)
    with pytest.raises(SkillLearningGateError) as caught:
        publish_learned_skill(ctx.store, PublishRequest(decision,expected_revision=current.revision,expected_sha256=current.sha256))
    assert caught.value.code == 'SKILL_LEARNING_REVISION_CONFLICT'
    assert ctx.store.read_version(NAME, 2) == historical
    assert _learned_file(ctx).read_text() == ctx.store.read_version(NAME, 1)
    assert _update(ctx,'run-3',BODY+'\n新总结修订。').status == 'updated'
    assert ctx.store.load_registry().skills[NAME].version == 4
    assert ctx.store.read_version(NAME,2) == historical


def test_old_snapshot_reads_frozen_version_after_update_and_rollback(tmp_path):
    ctx = _runtime(tmp_path,_output())
    _publish_first(ctx)
    old = ctx.skills.snapshot_for(force_reload=True)
    body = old.read_body(f'owner:{NAME}')
    assert old.resolve(f'owner:{NAME}').frozen_path
    assert _update(ctx).status == 'updated'
    assert old.read_body(f'owner:{NAME}') == body
    revert_learned_skill(ctx.store, NAME)
    assert old.read_body(f'owner:{NAME}') == body


def test_version_identity_cannot_be_overwritten_and_old_versions_not_pruned(tmp_path):
    ctx = _runtime(tmp_path,_output())
    _publish_first(ctx)
    with ctx.store.state_lock():
        with pytest.raises(SkillLearningStoreError):
            ctx.store.save_version(NAME,1,'different')
        for version in range(2,10):
            ctx.store.save_version(NAME,version,f'body-{version}')
    assert ctx.store.read_version(NAME,1) is not None


def test_real_service_rejects_model_result_after_user_revert(tmp_path, monkeypatch):
    ctx = _runtime(tmp_path,_output())
    _publish_first(ctx)
    assert _update(ctx).status == 'updated'
    old_v2 = ctx.store.read_version(NAME,2)
    original = ctx.service._call_model

    def revert_while_model_running(material):
        result = original(material)
        revert_learned_skill(ctx.store,NAME)
        return result

    monkeypatch.setattr(ctx.service,'_call_model',revert_while_model_running)
    result = _update(ctx,'run-3',BODY+'\n旧结果不应提交。')
    assert result.status == 'rejected' and result.code == 'SKILL_LEARNING_REVISION_CONFLICT'
    assert ctx.store.read_version(NAME,2) == old_v2


def test_removed_skill_old_snapshot_cannot_read_archived_body(tmp_path):
    from agent_py_agent.agent.capability.skill_learning_publish import remove_learned_skill
    from agent_py_agent.agent.capability.skill_snapshot import SkillSnapshotError
    ctx = _runtime(tmp_path,_output())
    _publish_first(ctx)
    old = ctx.skills.snapshot_for(force_reload=True)
    remove_learned_skill(ctx.store,NAME)
    with pytest.raises(SkillSnapshotError):
        old.read_body(f'owner:{NAME}')


def test_restored_task_resolves_exact_historical_learned_reference(tmp_path):
    ctx = _runtime(tmp_path,_output())
    _publish_first(ctx)
    old = ctx.skills.snapshot_for(force_reload=True)
    old_ref = old.resolve(f'owner:{NAME}').to_ref()
    old_body = old.read_body(f'owner:{NAME}')
    assert _update(ctx).status=='updated'
    current = ctx.skills.snapshot_for(force_reload=True)
    restored = current.restricted([f'owner:{NAME}'],expected_refs=[old_ref])
    assert restored.read_body(f'owner:{NAME}')==old_body
    assert restored.resolve(f'owner:{NAME}').content_sha256==old_ref['content_sha256']


@pytest.mark.parametrize('count', [3, 6, 800])
def test_cold_learning_snapshot_parses_one_registry_and_hot_view_reuses_it(tmp_path, monkeypatch, count, record_testsuite_property):
    ctx = _runtime(tmp_path)
    records = {}
    for index in range(count):
        name = f'batch-method-{index}'
        text = f'---\nname: {name}\ndescription: 批量核对方法\n---\n\n先检查输入，再核对输出。\n'
        active = ctx.store.learned_root / name / 'SKILL.md'
        version = ctx.store.versions_dir / name / 'v1.md'
        for path in (active, version):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8')
        records[name] = LearnedSkill(name, 1, skill_content_sha256(text), 'now', 'now')
    ctx.store.registry_path.write_text(json.dumps(SkillLearningRegistry(skills=records).to_record()), encoding='utf-8')
    original, calls = SkillLearningStore.load_registry, []

    def counted(store):
        calls.append(store.registry_path)
        return original(store)

    monkeypatch.setattr(SkillLearningStore, 'load_registry', counted)
    started = time.perf_counter()
    cold = ctx.skills.snapshot_for(force_reload=True)
    record_testsuite_property(f'cold_wall_seconds_{count}', time.perf_counter() - started)
    assert len(calls) == 1
    for name, record in records.items():
        entry = cold.resolve(f'owner:{name}')
        assert entry.content_sha256 == record.sha256
        assert entry.frozen_path == str(ctx.store.versions_dir / name / 'v1.md')
    started = time.perf_counter()
    assert ctx.skills.snapshot_for() is cold
    record_testsuite_property(f'hot_wall_seconds_{count}', time.perf_counter() - started)
    assert len(calls) == 1


@pytest.mark.parametrize('quota_mb', [100, 100 * 1024])
def test_revert_registry_eio_restores_body_and_only_discards_uncommitted_version(tmp_path, monkeypatch, quota_mb):
    ctx = _runtime(tmp_path, _output())
    quota = ctx.home.owner_home_dir / 'quota.json'
    policy = json.loads(quota.read_text())
    policy['max_disk_mb'] = quota_mb
    quota.write_text(json.dumps(policy))
    _publish_first(ctx)
    assert _update(ctx).status == 'updated'
    before = ctx.store.load_registry().skills[NAME]
    previous = _learned_file(ctx).read_bytes()
    histories = {number: ctx.store.read_version(NAME, number) for number in (1, 2)}
    failure = OSError(errno.EIO, 'registry replace rejected before commit')

    def fail_registry(_registry):
        raise failure

    with monkeypatch.context() as patch:
        patch.setattr(ctx.store, 'save_registry', fail_registry)
        with pytest.raises(OSError) as caught:
            revert_learned_skill(ctx.store, NAME)
    assert caught.value is failure
    fresh = SkillLearningStore.for_home(ctx.home)
    assert fresh.load_registry().skills[NAME] == before
    assert _learned_file(ctx).read_bytes() == previous
    assert fresh.read_version(NAME, 3) is None
    assert {number: fresh.read_version(NAME, number) for number in (1, 2)} == histories
    snapshot = ctx.skills.snapshot_for(force_reload=True)
    assert snapshot.read_body(f'owner:{NAME}') == previous.decode()
    # 启动全新解释器，从原管理详情和真实skill_search/get入口交叉读取失败后的v2。
    probe = '''
import json, sys
from pathlib import Path
from types import SimpleNamespace
from agent_py_agent.agent.capability.skill_learning_report import learned_skill_detail
from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
from agent_py_agent.agent.capability.skill_search_tool import SkillSearchTool
from agent_py_agent.agent.capability.skill_service import SkillsService
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.agent.user_space.owner_policy import resolve_effective_owner_policy
home = ensure_my_agent_home(Path(sys.argv[1]))
skills = SkillsService(home_paths=home, workspace_root=Path(sys.argv[2]),
                       policy_provider=lambda: resolve_effective_owner_policy(home))
outcome = SkillSearchTool(SimpleNamespace(home_paths=home, current_skill_snapshot=skills.snapshot_for)).execute(
    {'action': 'get', 'skill_id': 'owner:' + sys.argv[3]})
print(json.dumps({'report': learned_skill_detail(SkillLearningStore.for_home(home), sys.argv[3]),
                  'ok': outcome.ok, 'read': json.loads(outcome.output)}))
'''
    child = subprocess.run([sys.executable, '-c', probe, str(ctx.home.root), str(ctx.skills.workspace_root), NAME],
                           cwd=Path(__file__).resolve().parents[2], check=True, capture_output=True, text=True)
    observed = json.loads(child.stdout)
    assert observed['report']['skill']['state'] == 'active'
    assert observed['report']['skill']['sha256'] == before.sha256
    assert observed['ok'] is True and observed['read']['content_sha256'] == before.sha256
    assert observed['read']['body'] == previous.decode()
    assert revert_learned_skill(fresh, NAME).event == 'reverted'
    reverted = fresh.load_registry().skills[NAME]
    assert reverted.version == 1 and reverted.revision == 3
    assert fresh.read_version(NAME, 3) == _learned_file(ctx).read_text() == histories[1]


def test_cached_learning_view_refreshes_after_control_import_update_and_delete(tmp_path):
    from agent_py_agent.agent.capability.skill_learning_maintenance import control_learning
    from agent_py_agent.agent.capability.skill_learning_publish import remove_learned_skill
    from agent_py_agent.agent.capability.skill_snapshot import SkillSnapshotError

    ctx = _runtime(tmp_path, _output())
    _publish_first(ctx)
    stable_id = f'owner:{NAME}'
    initial = ctx.skills.snapshot_for()
    assert initial.resolve(stable_id).enabled
    control_learning(ctx.store, NAME, operation='disable')
    disabled = ctx.skills.snapshot_for()
    assert disabled is not initial and disabled.resolve(stable_id) is None
    assert not disabled.resolve(stable_id, enabled_only=False).enabled
    with pytest.raises(SkillSnapshotError):
        initial.read_body(stable_id)
    control_learning(ctx.store, NAME, operation='enable')
    enabled = ctx.skills.snapshot_for()
    assert enabled is not disabled and enabled.resolve(stable_id).enabled

    # 使用原登记的导入事务状态，验证未提交目标隐藏，提交后同一服务马上重新可见。
    # 导入生产者仍由原资产入口持有；这里不另建导入器或扫描副本。
    with ctx.store.state_lock():
        registry = ctx.store.load_registry()
        ctx.store.save_registry(replace(registry, import_operations={
            'test-import': {'status': 'pending', 'target_names': [NAME]}}))
    pending = ctx.skills.snapshot_for()
    assert pending is not enabled
    assert pending.resolve(stable_id) is None
    with ctx.store.state_lock():
        registry = ctx.store.load_registry()
        ctx.store.save_registry(replace(registry, import_operations={
            'test-import': {'status': 'committed', 'target_names': [NAME]}}))
    committed = ctx.skills.snapshot_for()
    assert committed is not pending and committed.resolve(stable_id).enabled
    assert _update(ctx).status == 'updated'
    updated = ctx.skills.snapshot_for()
    assert updated is not committed
    assert updated.read_body(stable_id) != committed.read_body(stable_id)
    remove_learned_skill(ctx.store, NAME)
    removed = ctx.skills.snapshot_for()
    assert removed is not updated
    assert removed.resolve(stable_id) is None
