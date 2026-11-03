"""W11 Skill 管理与自学习候选动作的真实 Gateway/原生存储回归。"""
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from test_v13_w0_harness import v13_call, v13_space
from test_v13_w0_harness import v13_native as _native_fixture

v13_native = _native_fixture

USER = 'business-alice'
OTHER = 'business-bob'


# LLM: 测试技能直接落成原生目录形状，验证的是产品读口而不是自造 DTO。
# 函数用途: 在隔离 owner home 中写一个技能。
def _write_skill(home, layer, name, body):
    root = {'user': home / 'skills' / name,
            'workspace': home / 'workspace' / '.agents' / 'skills' / name,
            'learned': home / 'skills' / 'learned' / name}[layer]
    root.mkdir(parents=True, exist_ok=True)
    (root / 'SKILL.md').write_text(body, encoding='utf-8')
    return root


# LLM: 标准头部保证正文校验、名称和标签都有确定输入。
# 函数用途: 生成一份包含 frontmatter 的技能正文。
def _body(name='demo-skill', description='演示技能'):
    return ('---\nname: ' + name + '\ndescription: ' + description
            + '\nwhen_to_use: 需要演示时\ntags: alpha,beta\n---\n\n正文内容。\n')


# LLM: 业务调用的 owner 是真实渠道用户，不是启动网关的本地 owner；测试落盘必须用同一 owner 路径投影。
# 函数用途: 返回测试用户在隔离宿主中的 owner 路径集合。
def _user_paths(agent, user=USER):
    from agent_py_agent.agent.user_space.owner_resolver import (
        OwnerIdentity,
        home_paths_with_owner,
        resolve_owner_home,
    )

    paths = agent.home_paths
    return home_paths_with_owner(paths, resolve_owner_home(
        paths.root, OwnerIdentity.provider_user('feishu', user)))


# LLM: 学习登记只由原生 registry 决定，测试按原生 schema v1 写一行。
# 函数用途: 把一个技能登记为自学 Skill。
def _register_learned(home, name, body):
    from hashlib import sha256
    directory = home / 'data' / 'skill_learning'
    directory.mkdir(parents=True, exist_ok=True)
    stamp = '2026-11-02T00:00:00+00:00'
    record = {'schema_version': 'my-agent.skill-learning-registry.v1',
              'skills': {name: {'name': name, 'version': 1, 'revision': 1, 'last_operation_id': '',
                                'maintenance': {}, 'layout': 'learned',
                                'sha256': sha256(body.encode('utf-8')).hexdigest(),
                                'created_at': stamp, 'updated_at': stamp, 'source_run_ids': []}},
              'blocked_names': [], 'daily': {'date': '', 'calls': 0}, 'daily_history': {},
              'import_operations': {}, 'assets': {}, 'last_maintenance_at': 0}
    (directory / 'registry.json').write_text(json.dumps(record), encoding='utf-8')


# LLM: 真实 HTTP 入口覆盖层级遮蔽、筛选与分页，不手写响应。
# 函数用途: 验证 workspace > user > builtin/learned 的列表顺序。
def test_list_layers_precedence(v13_native):
    server, agent = v13_native
    home = _user_paths(agent).owner_home_dir
    _write_skill(home, 'user', 'demo-skill', _body('demo-skill', '用户层'))
    _write_skill(home, 'workspace', 'demo-skill', _body('demo-skill', '工作区层'))
    _write_skill(home, 'learned', 'lesson-abcdef123456', _body('lesson-abcdef123456', '自学层'))
    _register_learned(home, 'lesson-abcdef123456', _body('lesson-abcdef123456', '自学层'))

    status, body = v13_call(server, 'skills.list', {}, user=USER)
    assert status == 200, body
    assert body['state'] == 'completed' and body['error'] is None
    rows = {row['name']: row for row in body['data']['items'] if row['name'] == 'demo-skill'}
    assert rows['demo-skill']['layer'] == 'workspace'
    assert rows['demo-skill']['description'] == '工作区层'
    learned = [row for row in body['data']['items'] if row['name'] == 'lesson-abcdef123456']
    assert learned and learned[0]['layer'] == 'learned'

    status, body = v13_call(server, 'skills.list', {'layer': 'user'}, user=USER)
    assert status == 200, body
    assert all(row['layer'] == 'user' for row in body['data']['items'])
    assert body['data']['coverage'] == 'complete'


# LLM: 游标必须绑定筛选条件；篡改后的令牌走既有 CURSOR_EXPIRED 语义。
# 函数用途: 验证技能分页与游标过期。
def test_list_cursor_expired_on_tamper(v13_native):
    server, agent = v13_native
    home = _user_paths(agent).owner_home_dir
    for name in ('page-a', 'page-b', 'page-c'):
        _write_skill(home, 'user', name, _body(name))
    status, body = v13_call(server, 'skills.list', {'limit': 1}, user=USER)
    assert status == 200, body
    cursor = body['data']['next_cursor']
    assert cursor and body['data']['coverage'] == 'partial'
    status, body = v13_call(server, 'skills.list', {'limit': 1, 'cursor': cursor + 'x'}, user=USER)
    assert status == 503, body
    assert body['error']['code'] == 'CURSOR_EXPIRED'


# LLM: 正文、资源清单和链接拒绝来自同一次原生读取。
# 函数用途: 验证技能详情与资源枚举。
def test_get_body_and_resources(v13_native):
    server, agent = v13_native
    home = _user_paths(agent).owner_home_dir
    directory = _write_skill(home, 'user', 'detail-skill', _body('detail-skill'))
    (directory / 'references').mkdir()
    (directory / 'references' / 'notes.md').write_text('参考内容\n', encoding='utf-8')
    os.symlink('/etc/passwd', directory / 'references' / 'escape.md')

    status, body = v13_call(server, 'skills.get', {'skill_id': 'detail-skill'}, user=USER)
    assert status == 200, body
    data = body['data']
    assert '正文内容' in data['body_markdown']
    paths = {row['path']: row for row in data['resources']}
    assert paths['references/notes.md']['text'] == '参考内容\n'
    assert 'references/escape.md' not in paths
    assert data['skill']['layer'] == 'user' and len(data['skill']['version_ref']) == 64

    status, body = v13_call(server, 'skills.get', {'skill_id': 'missing-skill'}, user=USER)
    assert status == 404, body
    assert body['error']['code'] == 'RESOURCE_NOT_FOUND'


# LLM: 校验只在临时目录解析，坏头部与成功形状都要结构化返回。
# 函数用途: 验证 frontmatter 错误的现场报告。
def test_validate_reports_frontmatter_errors(v13_native):
    server, _agent = v13_native
    status, body = v13_call(server, 'skills.validate', {'body_markdown': '# 没有头部'}, user=USER)
    assert status == 200, body
    data = body['data']
    assert data['valid'] is False and data['descriptor'] is None
    assert data['errors'][0]['code'] == 'INVALID_FRONTMATTER'

    status, body = v13_call(server, 'skills.validate',
                            {'body_markdown': _body('checked-skill'), 'skill_id': 'checked-skill'},
                            user=USER)
    assert status == 200, body
    assert body['data']['valid'] is True and body['data']['errors'] == []
    assert body['data']['descriptor']['name'] == 'checked-skill'


# LLM: 创建/更新按正文版本 CAS；同键同正文是重放，异正文才是状态冲突。
# 函数用途: 验证 create/update 的版本引用与冲突分支。
def test_create_update_cas_by_version_ref(v13_native):
    server, agent = v13_native
    home = _user_paths(agent).owner_home_dir
    created = _body('cas-skill')
    status, body = v13_call(server, 'skills.create', {'skill_id': 'cas-skill', 'body_markdown': created},
                            user=USER, idempotency_key='create-' + uuid4().hex)
    assert status == 200, body
    version = body['data']['skill']['version_ref']
    assert (home / 'skills' / 'cas-skill' / 'SKILL.md').read_text(encoding='utf-8') == created

    status, body = v13_call(server, 'skills.create', {'skill_id': 'cas-skill', 'body_markdown': created},
                            user=USER, idempotency_key='create-' + uuid4().hex)
    assert status == 200, body
    assert body['data']['skill']['version_ref'] == version

    status, body = v13_call(server, 'skills.create',
                            {'skill_id': 'cas-skill', 'body_markdown': _body('cas-skill', '换了正文')},
                            user=USER, idempotency_key='create-' + uuid4().hex)
    assert status == 409, body
    assert body['error']['code'] == 'STATE_CONFLICT'

    status, body = v13_call(server, 'skills.update',
                            {'skill_id': 'cas-skill', 'body_markdown': _body('cas-skill', '新版本'),
                             'expected_version_ref': '0' * 64},
                            user=USER, idempotency_key='update-' + uuid4().hex)
    assert status == 409, body
    assert body['error']['code'] == 'REVISION_CONFLICT'

    status, body = v13_call(server, 'skills.update',
                            {'skill_id': 'cas-skill', 'body_markdown': _body('cas-skill', '新版本'),
                             'expected_version_ref': version},
                            user=USER, idempotency_key='update-' + uuid4().hex)
    assert status == 200, body
    assert body['data']['skill']['version_ref'] != version
    assert body['data']['skill']['description'] == '新版本'

    _write_skill(home, 'workspace', 'readonly-skill', _body('readonly-skill'))
    status, body = v13_call(server, 'skills.update',
                            {'skill_id': 'readonly-skill', 'body_markdown': _body('readonly-skill'),
                             'expected_version_ref': '0' * 64},
                            user=USER, idempotency_key='update-' + uuid4().hex)
    assert status == 409, body
    assert body['error']['code'] == 'STATE_CONFLICT'


# LLM: 启停只改原生侧元数据，重复启用不重复写，非用户层不允许切换。
# 函数用途: 验证 activate/retire 的幂等与层级拒绝。
def test_activate_retire_toggle(v13_native):
    server, agent = v13_native
    home = _user_paths(agent).owner_home_dir
    status, body = v13_call(server, 'skills.create',
                            {'skill_id': 'toggle-skill', 'body_markdown': _body('toggle-skill')},
                            user=USER, idempotency_key='create-' + uuid4().hex)
    assert status == 200, body
    status, body = v13_call(server, 'skills.retire', {'skill_id': 'toggle-skill'},
                            user=USER, idempotency_key='retire-' + uuid4().hex)
    assert status == 200, body
    assert body['data']['skill']['enabled'] is False
    status, body = v13_call(server, 'skills.retire', {'skill_id': 'toggle-skill'},
                            user=USER, idempotency_key='retire-' + uuid4().hex)
    assert status == 200, body
    assert body['data']['skill']['enabled'] is False
    status, body = v13_call(server, 'skills.activate', {'skill_id': 'toggle-skill'},
                            user=USER, idempotency_key='activate-' + uuid4().hex)
    assert status == 200, body
    assert body['data']['skill']['enabled'] is True
    meta = json.loads((home / 'skills' / 'toggle-skill' / 'skill_meta.json').read_text(encoding='utf-8'))
    assert meta['enabled'] is True

    _write_skill(home, 'workspace', 'fixed-skill', _body('fixed-skill'))
    status, body = v13_call(server, 'skills.retire', {'skill_id': 'fixed-skill'},
                            user=USER, idempotency_key='retire-' + uuid4().hex)
    assert status == 409, body
    assert body['error']['code'] == 'STATE_CONFLICT'


# LLM: 经验候选与 Skill 提案都按原生 schema 落盘，避免测试自造候选账本形状。
# 函数用途: 在隔离 owner home 中种入一条 lesson 候选和一条待确认 Skill 提案。
def _seed_lesson(agent, content='经验：演示技能'):
    from dataclasses import replace

    from agent_py_agent.agent.capability.skill_proposals import (
        SkillProposal,
        SkillProposalDraft,
        SkillProposalSource,
        SkillProposalTarget,
        lesson_skill_name,
        render_skill_markdown,
        skill_proposal_id,
    )
    from agent_py_agent.agent.capability.skill_snapshot import skill_content_sha256
    from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation
    from agent_py_agent.agent.memory_store.candidates import CandidateService

    paths = _user_paths(agent)
    service = CandidateService(Path(paths.owner_memory_candidates_jsonl))
    candidate = service.observe(CandidateObservation(
        candidate_type='lesson', content=content, subject_key='demo-subject',
        scope={'scope_type': 'global', 'scope_key': 'global'}, origin='subagent_lesson',
        promotion_target='lesson', source_task_ids=('task-demo',), source_run_ids=('run-demo',)))
    skill_name = lesson_skill_name(candidate.content_hash)
    target = SkillProposalTarget(skill_name=skill_name)
    draft = SkillProposalDraft(description=content, when_to_use='演示时使用', body='演示正文', sha256='0' * 64)
    draft = replace(draft, sha256=skill_content_sha256(render_skill_markdown(skill_name, draft)))
    proposal_id = skill_proposal_id(candidate.candidate_id, candidate.content_hash)
    stamp = '2026-11-02T00:00:00+00:00'
    proposal = SkillProposal(proposal_id, 1, 'pending_confirmation',
                             SkillProposalSource(candidate.candidate_id, candidate.content_hash,
                                                 ('task-demo',), ('run-demo',)),
                             target, draft, stamp, stamp)
    path = Path(paths.owner_skill_proposals_dir) / (proposal_id + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(proposal.to_record()), encoding='utf-8')
    return candidate, proposal


# LLM: 逐项状态来自原生候选/提案记录，不按标题或时间猜。
# 函数用途: 验证候选列表投影、状态映射与分页。
def test_candidates_list_example_params(v13_native):
    server, agent = v13_native
    _candidate, proposal = _seed_lesson(agent)
    status, body = v13_call(server, 'learning.candidates.list',
                            {'space_id': 'soc-default', 'status': 'pending'}, user=USER)
    assert status == 200, body
    assert body['state'] == 'completed' and body['error'] is None
    rows = {row['candidate_id']: row for row in body['data']['items']}
    assert rows[proposal.proposal_id]['kind'] == 'skill'
    assert rows[proposal.proposal_id]['status'] == 'pending'
    status, body = v13_call(server, 'learning.candidates.list', {'kind': 'memory'}, user=USER)
    assert status == 200, body
    assert all(row['kind'] == 'memory' for row in body['data']['items'])
    assert body['data']['items'], '种子候选必须出现在原生列表里'


# LLM: 预览返回用户将看到的全文与目标版本，不返回摘要代替完整版本。
# 函数用途: 验证 Skill 提案预览。
def test_preview_returns_full_version(v13_native):
    server, agent = v13_native
    _candidate, proposal = _seed_lesson(agent)
    status, body = v13_call(server, 'learning.candidates.preview', {'candidate_id': proposal.proposal_id},
                            user=USER, idempotency_key='preview-' + uuid4().hex)
    assert status == 200, body
    data = body['data']
    assert data['preview_id'].startswith('spv1-')
    assert data['target']['skill_name'] == proposal.target.skill_name
    assert '演示正文' in data['full_preview']
    assert len(data['version_ref']) == 64 and data['expires_at']


# LLM: 确认必须同时绑定 owner/actor/thread 和目标版本，任一项不符都不得提交。
# 函数用途: 验证 Skill 提案确认的正反例与终态。
def test_preview_confirm_bound_to_owner_actor_thread_and_cas(v13_native):
    server, agent = v13_native
    _candidate, proposal = _seed_lesson(agent)
    status, body = v13_call(server, 'learning.candidates.preview', {'candidate_id': proposal.proposal_id},
                            user=USER, idempotency_key='preview-' + uuid4().hex)
    assert status == 200, body
    preview_id, version = body['data']['preview_id'], body['data']['version_ref']

    status, body = v13_call(server, 'learning.candidates.confirm',
                            {'preview_id': preview_id, 'expected_version_ref': '0' * 64},
                            user=USER, idempotency_key='confirm-' + uuid4().hex)
    assert status == 409, body
    assert body['error']['code'] == 'REVISION_CONFLICT'

    status, body = v13_call(server, 'learning.candidates.confirm',
                            {'preview_id': preview_id, 'expected_version_ref': version},
                            user=OTHER, idempotency_key='confirm-' + uuid4().hex)
    assert status == 403, body
    assert body['error']['code'] == 'BUSINESS_ACCESS_DENIED'

    status, body = v13_call(server, 'learning.candidates.preview',
                            {'candidate_id': proposal.proposal_id, 'space_id': 'soc-default'},
                            user=USER, idempotency_key='preview-' + uuid4().hex)
    assert status == 200, body
    status, body = v13_call(server, 'learning.candidates.confirm',
                            {'preview_id': body['data']['preview_id'],
                             'expected_version_ref': body['data']['version_ref']},
                            user=USER, idempotency_key='confirm-' + uuid4().hex)
    assert status == 403, body
    assert body['error']['code'] == 'BUSINESS_ACCESS_DENIED'

    status, body = v13_call(server, 'learning.candidates.confirm',
                            {'preview_id': preview_id, 'expected_version_ref': version},
                            user=USER, idempotency_key='confirm-' + uuid4().hex)
    assert status == 200, body
    assert body['data']['candidate']['status'] == 'confirmed'
    installed = _user_paths(agent).owner_home_dir / 'skills' / proposal.target.skill_name / 'SKILL.md'
    assert installed.is_file()

    status, body = v13_call(server, 'learning.candidates.confirm',
                            {'preview_id': preview_id, 'expected_version_ref': version},
                            user=USER, idempotency_key='confirm-' + uuid4().hex)
    assert status == 200, body
    assert body['data']['candidate']['status'] == 'confirmed'


# LLM: 拒绝是终态；之后同一预览不能再被确认为已发布。
# 函数用途: 验证 Skill 提案拒绝后的最终态。
def test_reject_is_final(v13_native):
    server, agent = v13_native
    _candidate, proposal = _seed_lesson(agent, content='经验：第二条')
    status, body = v13_call(server, 'learning.candidates.preview', {'candidate_id': proposal.proposal_id},
                            user=USER, idempotency_key='preview-' + uuid4().hex)
    assert status == 200, body
    preview_id, version = body['data']['preview_id'], body['data']['version_ref']
    status, body = v13_call(server, 'learning.candidates.reject', {'preview_id': preview_id},
                            user=USER, idempotency_key='reject-' + uuid4().hex)
    assert status == 200, body
    assert body['data']['candidate']['status'] == 'rejected'
    status, body = v13_call(server, 'learning.candidates.reject', {'preview_id': preview_id},
                            user=USER, idempotency_key='reject-' + uuid4().hex)
    assert status == 200, body
    assert body['data']['candidate']['status'] == 'rejected'
    status, body = v13_call(server, 'learning.candidates.confirm',
                            {'preview_id': preview_id, 'expected_version_ref': version},
                            user=USER, idempotency_key='confirm-' + uuid4().hex)
    assert status == 409, body
    assert body['error']['code'] == 'STATE_CONFLICT'
    assert not (_user_paths(agent).owner_home_dir / 'skills' / proposal.target.skill_name).exists()


# LLM: 记忆候选复用原生 /memory candidates 回执与绑定判据。
# 函数用途: 验证记忆候选预览与确认的 actor/CAS 反例。
def test_memory_candidate_preview_confirm_binding(v13_native):
    server, agent = v13_native
    candidate, _proposal = _seed_lesson(agent, content='经验：记忆候选')
    status, body = v13_call(server, 'learning.candidates.preview',
                            {'candidate_id': candidate.candidate_id},
                            user=USER, idempotency_key='preview-' + uuid4().hex)
    assert status == 200, body
    preview_id, version = body['data']['preview_id'], body['data']['version_ref']
    assert preview_id.startswith('gwctl-msg-')
    assert body['data']['full_preview'] and body['data']['target']['kind'] == 'memory_candidate'
    # LLM: 轻量宿主没有可与 receipt 账配对的完整候选宿主；未知 nonce 必须在提交前被拒绝。
    # 函数用途: 验证记忆候选确认入口不接受未由本人预览签发的 nonce。
    status, body = v13_call(server, 'learning.candidates.confirm',
                            {'preview_id': 'gwctl-msg-' + '0' * 32,
                             'expected_version_ref': version},
                            user=USER, idempotency_key='confirm-' + uuid4().hex)
    assert status in (403, 404), body
    assert body['error']['code'] in ('BUSINESS_ACCESS_DENIED', 'RESOURCE_NOT_FOUND')


# LLM: 作业结果只接受结构化 JSON，缺字段必须失败而不是补默认值。
# 函数用途: 验证回测收口与真实 202 受理。
def test_backtest_job_kind_skill_backtest(v13_native):
    from agent_py_agent.agent.business_spaces.v13_w11_skills_learning import finalize_backtest

    parsed = finalize_backtest('```json\n{"skill_id": "bt-skill", "cases_run": 2, "passed": 1, '
                               '"failed": 1, "findings": ["边界用例失败"]}\n```')
    assert parsed == {'skill_id': 'bt-skill', 'cases_run': 2, 'passed': 1, 'failed': 1,
                      'findings': ['边界用例失败']}
    with pytest.raises(ValueError):
        finalize_backtest('{"skill_id": "bt-skill"}')

    server, _agent = v13_native
    space_id = v13_space(server)
    status, body = v13_call(server, 'skills.create',
                            {'skill_id': 'bt-skill', 'body_markdown': _body('bt-skill')},
                            user=USER, idempotency_key='create-' + uuid4().hex)
    assert status == 200, body
    status, body = v13_call(server, 'skills.backtest', {'skill_id': 'bt-skill', 'space_id': space_id},
                            user=USER, idempotency_key='backtest-' + uuid4().hex)
    # LLM: 这里必须断言真实 202 受理；W0 派发依赖缺陷造成的失败只记录，不放宽成 skip 或改断言。
    # 函数用途: 验证 skills.backtest 经 W0 派发出口拿到 accepted 回执。
    assert status == 202, ('W0 v13_start_model_job 强制 tasks.assign DomainResult(202)，'
                           '真实原生 200/receipt accepted 时该桥接返回 503: ' + json.dumps(
                               body.get('error'), ensure_ascii=False))
    assert body['state'] == 'accepted' and body['error'] is None
    assert body['data']['job_id'] and body['data']['task_id']


# LLM: 作业种类与空间解析由本模块唯一出口交给 W0 派发，不在这里重写派发或任务账。
# 函数用途: 验证回测向 W0 派发出口传入的 kind、指令与真实空间。
def test_backtest_dispatches_skill_backtest_kind(v13_native, monkeypatch):
    from agent_py_agent.agent.business_spaces import v13_w11_skills_learning as module
    from agent_py_agent.agent.gateway_parts.business_api_v13 import DomainResult

    captured = {}

    def fake_start(call, kind, instruction, *, space_id=None):
        captured.update(kind=kind, instruction=instruction, space_id=space_id)
        return DomainResult(202, {'state': 'accepted', 'operation_id': 'op-1',
                                  'job_id': 'job-1', 'task_id': 'task-1'})

    monkeypatch.setattr(module, 'v13_start_model_job', fake_start)
    server, _agent = v13_native
    space_id = v13_space(server)
    status, body = v13_call(server, 'skills.create',
                            {'skill_id': 'bt-kind', 'body_markdown': _body('bt-kind')},
                            user=USER, idempotency_key='create-' + uuid4().hex)
    assert status == 200, body
    status, body = v13_call(server, 'skills.backtest',
                            {'skill_id': 'bt-kind', 'space_id': space_id},
                            user=USER, idempotency_key='backtest-' + uuid4().hex)
    assert status == 202, body
    assert captured['kind'] == 'skill_backtest'
    assert captured['space_id'] == space_id
    assert 'bt-kind' in captured['instruction'] and '严格 JSON' in captured['instruction']


# LLM: 同键异参必须拒绝，重放旧启用回执不可改变后来停用的真实开关。
# 函数用途: 验证技能写动作完整参数绑定与耐久成功重放。
def test_skill_mutations_bind_idempotency_and_replay_without_effect(v13_native):
    server, agent = v13_native
    params = {'skill_id': 'idem-a', 'body_markdown': _body('idem-a')}
    key = 'w11-skill-idem-create'
    status, first = v13_call(server, 'skills.create', params, user=USER, idempotency_key=key)
    assert status == 200, first
    status, conflict = v13_call(server, 'skills.create',
        {'skill_id': 'idem-b', 'body_markdown': _body('idem-b')}, user=USER, idempotency_key=key)
    assert status == 409 and conflict['error']['code'] == 'IDEMPOTENCY_CONFLICT', conflict
    assert not (_user_paths(agent).owner_home_dir / 'skills' / 'idem-b').exists()
    toggle = {'skill_id': 'idem-a'}
    status, activated = v13_call(server, 'skills.activate', toggle, user=USER,
                                 idempotency_key='w11-skill-idem-activate')
    assert status == 200, activated
    status, retired = v13_call(server, 'skills.retire', toggle, user=USER,
                               idempotency_key='w11-skill-idem-retire')
    assert status == 200 and retired['data']['skill']['enabled'] is False, retired
    status, replay = v13_call(server, 'skills.activate', toggle, user=USER,
                              idempotency_key='w11-skill-idem-activate')
    assert status == 200 and replay['data'] == activated['data'], replay
    status, actual = v13_call(server, 'skills.get', toggle, user=USER)
    assert status == 200 and actual['data']['skill']['enabled'] is False, actual


# LLM: 两个线程同版本更新只能一个提交；屏障只协调测试启动，不替换资产锁或CAS。
# 函数用途: 验证并发技能更新不丢失版本检查。
def test_skill_concurrent_updates_one_cas_winner(v13_native):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    server, _agent = v13_native
    params = {'skill_id': 'race-skill', 'body_markdown': _body('race-skill')}
    status, created = v13_call(server, 'skills.create', params, user=USER,
                               idempotency_key='w11-skill-race-create')
    assert status == 200, created
    version = created['data']['skill']['version_ref']
    ready = Barrier(2)

    # LLM: 每个输入都是真实HTTP请求，同一旧版本、不同正文和幂等键。
    # 函数用途: 同步发起一个待竞争的版本更新。
    def update(index):
        ready.wait(timeout=10)
        return v13_call(server, 'skills.update', {'skill_id': 'race-skill',
            'body_markdown': _body('race-skill', '并发正文' + str(index)),
            'expected_version_ref': version}, user=USER,
            idempotency_key='w11-skill-race-update-' + str(index))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, (1, 2)))
    assert sorted(status for status, _body_result in results) == [200, 409], results
    rejected = next(body for status, body in results if status == 409)
    assert rejected['error']['code'] == 'REVISION_CONFLICT', rejected


# LLM: 候选和消息由原请求owner解析器创建；预览和提交必须穿过真实HTTP及原control耐久回执。
# 函数用途: 验证记忆候选完整预览后确认或拒绝的正向链路和稳定重放。
@pytest.mark.parametrize('decision', ['confirm', 'reject'])
def test_memory_candidate_real_preview_then_decision(v13_native, decision):
    from types import SimpleNamespace

    from agent_py_agent.agent.capability.memory_tool import RememberTool
    from agent_py_agent.agent.conversation.learning_source import dispatch_projection
    from agent_py_agent.agent.gateway_parts.request_worker import (
        _resolve_request_agent_for_owner,
    )
    from agent_py_agent.agent.memory_store.candidates import CandidateService
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    server, agent = v13_native
    paths = _user_paths(agent)
    owner_agent = _resolve_request_agent_for_owner(agent, OwnerIdentity.provider_user('feishu', USER))
    assert owner_agent.home_paths.owner_id == paths.owner_id
    thread = owner_agent.conversation_store.threads.get_or_create({
        'canonical_user_id': USER, 'owner_id': paths.owner_id, 'channel': 'feishu',
        'channel_conversation_id': 'w11-memory-source-' + decision, 'channel_user_id': USER,
        'learning_source': dispatch_projection()})
    source_id = 'w11-memory-source-message-' + decision
    owner_agent.conversation_store.messages.append({'thread_id': thread.thread_id,
        'role': 'user', 'content': 'W11人工审核的演示项目事实。',
        'metadata': {'gateway_request_id': source_id}})
    owner_agent._current_run_params = SimpleNamespace(request_id=source_id,
        run_id='w11-memory-source-run-' + decision, task_id='w11-memory-source-task-' + decision,
        task_attributes={'conversation_thread_id': thread.thread_id})
    draft = RememberTool(owner_agent).execute({'content': 'W11人工审核的演示项目事实。',
        'kind': 'project', 'origin': 'user_explicit', 'subject_key': 'w11-review-' + decision})
    assert draft.ok and not json.loads(draft.output)['saved'], draft
    service = CandidateService(Path(paths.owner_memory_candidates_jsonl))
    candidate = service.list()[0]
    assert candidate.status == 'pending_review' and owner_agent.memory.all() == []
    preview_key = 'memory-real-preview-' + decision
    status, body = v13_call(server, 'learning.candidates.preview',
        {'candidate_id': candidate.candidate_id}, user=USER, idempotency_key=preview_key)
    assert status == 200, json.dumps(body, ensure_ascii=False)
    preview = body['data']
    assert candidate.content in preview['full_preview']
    replay_status, replay = v13_call(server, 'learning.candidates.preview',
        {'candidate_id': candidate.candidate_id}, user=USER, idempotency_key=preview_key)
    assert replay_status == 200 and replay['data'] == preview, replay
    params = {'preview_id': preview['preview_id']}
    if decision == 'confirm':
        params['expected_version_ref'] = preview['version_ref']
    status, body = v13_call(server, 'learning.candidates.' + decision, params,
        user=USER, idempotency_key='memory-real-decision-' + decision)
    assert status == 200, json.dumps(body, ensure_ascii=False)
    assert body['data']['candidate']['status'] == ('confirmed' if decision == 'confirm' else 'rejected')
    final = service.get(candidate.candidate_id)
    assert final.status == ('promoted' if decision == 'confirm' else 'rejected')
    if decision == 'confirm':
        assert final.promotion_ref and body['data']['receipt']['state'] == 'completed'
        assert [row.content for row in owner_agent.memory.all()] == [candidate.content]
    else:
        assert owner_agent.memory.all() == []
    status, replay = v13_call(server, 'learning.candidates.' + decision, params,
        user=USER, idempotency_key='memory-real-decision-' + decision)
    assert status == 200 and replay['data'] == body['data'], replay
