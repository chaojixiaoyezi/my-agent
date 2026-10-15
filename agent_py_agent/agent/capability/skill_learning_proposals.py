# LLM: S3候选暂存复用原learning store锁和原publisher，只有用户控制入口调用resolve；模型不能通过参数自行确认。
# 模块用途: 将总结保存为有界待确认草稿，按用户看到的候选修订确认或拒绝，正式内容不自动发布。
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace

from ..common.nofollow_fs import read_text_beneath, write_text_atomic_beneath
from .skill_learning_prompt import SkillLearningDecision
from .skill_learning_publish import (
    PublishRequest,
    SkillLearningGateError,
    _redacted_publish,
    _stage_and_check,
    publish_learned_skill,
    render_learned_skill,
)
from .skill_learning_store import SkillLearningEvent
from .skill_snapshot import skill_content_sha256


# LLM: 只接受宿主安全编号，不允许模型提供文件路径；候选在同owner学习目录，非正式Skill目录。
# 函数用途: 校验一个学习候选的稳定标识。
def _proposal_name(proposal_id):
    if not isinstance(proposal_id,str) or not proposal_id.startswith('learning-proposal-') or len(proposal_id) != 50 or any(c not in '0123456789abcdef' for c in proposal_id[18:]):
        raise ValueError('学习候选编号无效')
    return proposal_id+'.json'


# LLM: 生成候选前脱敏并走原解析/guard，不存完整聊天材料；相同请求与内容去重，不产生正式目录。
# 函数用途: 保存一份等待用户确认的总结草稿并返回结构化proposed事件。
def propose_learning(store, publish):
    publish, redacted = _redacted_publish(publish)
    store.directory.mkdir(parents=True,exist_ok=True)
    with store.state_lock():
        _stage_and_check(store,publish,render_learned_skill(publish.decision))
    identity = json.dumps({'request_key':publish.request.get('request_key'),'decision':asdict(publish.decision),
        'expected_revision':publish.expected_revision,'expected_sha256':publish.expected_sha256},sort_keys=True,ensure_ascii=False)
    proposal_id = 'learning-proposal-'+hashlib.sha256(identity.encode()).hexdigest()[:32]
    record = {'schema_version':'skill-learning-proposal.v1','proposal_id':proposal_id,'revision':1,'status':'pending',
        'decision':asdict(publish.decision),'expected_revision':publish.expected_revision,'expected_sha256':publish.expected_sha256,
        'request':{key:publish.request.get(key,'') for key in ('request_key','run_id','task_id','thread_id')},
        'taken_names':sorted(publish.taken_names),'max_skills':publish.max_skills,'redacted':redacted}
    store.directory.mkdir(parents=True,exist_ok=True)
    with store.state_lock():
        existing = read_text_beneath(store.directory,('proposals',_proposal_name(proposal_id)))
        if existing is None:
            if sum(item['status']=='pending' for item in list_learning_proposals(store)) >= 20:
                raise SkillLearningGateError('SKILL_LEARNING_PROPOSALS_FULL')
            text = json.dumps(record,ensure_ascii=False)
            store.check_write(store.directory/'proposals'/_proposal_name(proposal_id),text)
            write_text_atomic_beneath(store.directory,('proposals',_proposal_name(proposal_id)),text)
    return SkillLearningEvent('proposed',skill_name=publish.decision.name,request_key=str(publish.request.get('request_key','')),reason=proposal_id)


# LLM: 草稿展示不授予发布；只读原owner固定目录，不接受另一owner路径。
# 函数用途: 读取准确候选供用户查看与确认前核对。
def learning_proposal(store, proposal_id):
    text = read_text_beneath(store.directory,('proposals',_proposal_name(proposal_id)))
    row = json.loads(text) if text is not None else {}
    if row.get('schema_version') != 'skill-learning-proposal.v1' or row.get('proposal_id') != proposal_id:
        raise ValueError('学习候选不存在或已损坏')
    identity = json.dumps({'request_key':row['request'].get('request_key'),'decision':row['decision'],
        'expected_revision':row['expected_revision'],'expected_sha256':row['expected_sha256']},sort_keys=True,ensure_ascii=False)
    if 'learning-proposal-'+hashlib.sha256(identity.encode()).hexdigest()[:32] != proposal_id:
        raise ValueError('学习候选内容已被修改')
    return row


# LLM: 目录只作索引，候选状态取每条canonical行，坏记录不假装为空以继续发布。
# 函数用途: 列出学习候选，不扫描正式Skill或其它owner。
def list_learning_proposals(store):
    directory = store.directory/'proposals'
    if not directory.exists():
        return []
    if directory.is_symlink():
        raise ValueError('学习候选目录无效')
    return [learning_proposal(store,path.stem) for path in sorted(directory.glob('learning-proposal-*.json'))]


# LLM: 仅用户CLI/结构化控制调用，锁内核候选修订；确认经原发布CAS/guard，宿主人类actor只写审计不授予权限，拒绝不改正式内容；重放同结果幂等。
# 函数用途: 确认或拒绝准确一份草稿，不把开关或模型理由当确认。
def resolve_learning_proposal(store, proposal_id, expected_revision, *, decision, guard_config=None, actor_id=""):
    if not isinstance(actor_id,str) or len(actor_id)>300 or len(actor_id.splitlines())>1:
        raise ValueError('学习确认者编号无效')
    if decision not in {'confirm','reject'} or isinstance(expected_revision,bool) or not isinstance(expected_revision,int):
        raise ValueError('学习候选决定无效')
    # publisher使用同一state锁，先持候选专属锁，禁止与其它入口相反锁序。
    from ..common.json_io import locked_json_path
    with locked_json_path(store.directory/'proposals'/(_proposal_name(proposal_id)+'.decision')):
        row = learning_proposal(store,proposal_id)
        if row['status'] == ('committed' if decision == 'confirm' else 'rejected') and row['revision'] == expected_revision + 1:
            return row
        if row['revision'] != expected_revision or row['status'] != 'pending':
            raise SkillLearningGateError('SKILL_LEARNING_PROPOSAL_CONFLICT')
        if decision == 'confirm':
            draft = dict(row['decision'])
            draft['tags'] = tuple(draft.get('tags',[]))
            request = {**row['request'], 'operation_id':proposal_id}
            record = store.load_registry().skills.get(draft['name'])
            if record and record.last_operation_id == proposal_id and record.sha256 == skill_content_sha256(render_learned_skill(SkillLearningDecision(**draft))):
                event = SkillLearningEvent('published' if draft['decision']=='create' else 'updated',skill_name=record.name,version=record.version,sha256=record.sha256)
            else:
                event = publish_learned_skill(store,PublishRequest(SkillLearningDecision(**draft),request,frozenset(row['taken_names']),
                    row['max_skills'],guard_config,row['expected_revision'],row['expected_sha256']))
            if row.get('redacted'):
                event = replace(event,code='SKILL_LEARNING_REDACTED')
            store.append_event(event)
            row.update(status='committed',receipt=event.to_record())
        else:
            row['status'] = 'rejected'
        row['resolved_by'] = {'actor_id':actor_id,'owner_scope':'current_store'}
        row['revision'] += 1
        with store.state_lock():
            text = json.dumps(row,ensure_ascii=False)
            store.check_write(store.directory/'proposals'/_proposal_name(proposal_id),text)
            write_text_atomic_beneath(store.directory,('proposals',_proposal_name(proposal_id)),text)
    return row
