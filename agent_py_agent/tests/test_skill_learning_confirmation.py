import pytest

from agent_py_agent.agent.capability.skill_learning import SkillLearningService
from agent_py_agent.agent.capability.skill_learning_proposals import (
    list_learning_proposals,
    resolve_learning_proposal,
)
from agent_py_agent.agent.capability.skill_learning_publish import SkillLearningGateError
from agent_py_agent.tests.test_skill_learning import NAME, _ctx, _output, _runtime


def test_real_service_drafts_only_and_requires_exact_user_confirmation(tmp_path):
    ctx = _runtime(tmp_path,_output())
    ctx.service = SkillLearningService(ctx.store,ctx.service.settings,ctx.service.runtime)
    ctx.service.enqueue_from_finalize(_ctx())
    assert ctx.service.run_pending().status == 'proposed'
    assert not ctx.store.skill_path(NAME).exists() and not ctx.store.load_registry().skills
    proposal = list_learning_proposals(ctx.store)[0]
    with pytest.raises(SkillLearningGateError):
        resolve_learning_proposal(ctx.store,proposal['proposal_id'],2,decision='confirm')
    result = resolve_learning_proposal(ctx.store,proposal['proposal_id'],1,decision='confirm')
    assert result['status'] == 'committed' and ctx.store.skill_path(NAME).exists()
    assert resolve_learning_proposal(ctx.store,proposal['proposal_id'],1,decision='confirm') == result


def test_rejection_does_not_write_formal_skill(tmp_path):
    ctx = _runtime(tmp_path,_output())
    ctx.service = SkillLearningService(ctx.store,ctx.service.settings,ctx.service.runtime)
    ctx.service.enqueue_from_finalize(_ctx())
    ctx.service.run_pending()
    proposal = list_learning_proposals(ctx.store)[0]
    assert resolve_learning_proposal(ctx.store,proposal['proposal_id'],1,decision='reject')['status'] == 'rejected'
    assert not ctx.store.skill_path(NAME).exists()
    with pytest.raises(SkillLearningGateError):
        resolve_learning_proposal(ctx.store,proposal['proposal_id'],1,decision='confirm')


def test_user_disable_blocks_previously_proposed_update(tmp_path):
    from agent_py_agent.agent.capability.skill_learning_maintenance import control_learning
    from agent_py_agent.tests.test_skill_learning import BODY, _publish_first
    ctx = _runtime(tmp_path,_output())
    _publish_first(ctx)
    ctx.backend.responses.append(_output('update',update_target=NAME,body=BODY+'\n新候选内容。'))
    raw = SkillLearningService(ctx.store,ctx.service.settings,ctx.service.runtime)
    raw.enqueue_from_finalize(_ctx('run-2',archive_tool_calls=[{'tool':'skill_search','ok':True,'parameters':{'skill_id':f'owner:{NAME}'}}]))
    assert raw.run_pending().status=='proposed'
    candidate = next(row for row in list_learning_proposals(ctx.store) if row['status']=='pending')
    before = ctx.store.skill_path(NAME).read_bytes()
    control_learning(ctx.store,NAME,operation='disable')
    with pytest.raises(SkillLearningGateError):
        resolve_learning_proposal(ctx.store,candidate['proposal_id'],1,decision='confirm')
    assert ctx.store.skill_path(NAME).read_bytes()==before


def test_im_control_reaches_candidate_confirmation_without_cli(tmp_path):
    from agent_py_agent.agent.conversation.control_commands import (
        parse_conversation_control,
    )
    from agent_py_agent.agent.gateway_parts.skill_control_service import (
        SkillControlHost,
        run_skill_control,
    )
    ctx = _runtime(tmp_path,_output())
    raw = SkillLearningService(ctx.store,ctx.service.settings,ctx.service.runtime)
    raw.enqueue_from_finalize(_ctx())
    raw.run_pending()
    proposal = list_learning_proposals(ctx.store)[0]
    # 与Gateway相同用户命令解析器；这里仅替代已认证owner host，不伪造模型批准。
    command = parse_conversation_control(f"/skills learned confirm {proposal['proposal_id']} 1")
    assert command.valid and command.operation=='learning_proposal'
    assert '已确认' in run_skill_control(SkillControlHost(ctx.home,None),command)
