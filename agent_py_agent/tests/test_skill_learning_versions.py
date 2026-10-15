from dataclasses import replace

import pytest

from agent_py_agent.agent.capability.skill_learning_prompt import parse_skill_learning_decision
from agent_py_agent.agent.capability.skill_learning_publish import (
    PublishRequest,
    SkillLearningGateError,
    publish_learned_skill,
    revert_learned_skill,
)
from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStoreError
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
