# LLM: S3候选暂存复用原learning store锁和原publisher，只有用户控制入口调用resolve；模型不能通过参数自行确认。
# 模块用途: 将总结保存为有界待确认草稿，按用户看到的候选修订确认或拒绝，正式内容不自动发布。
from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import contextmanager
from dataclasses import asdict, replace

from ..common.json_io import (
    append_private_jsonl_capped,
    locked_json_path,
    read_jsonl_objects_report,
)
from ..common.nofollow_fs import (
    list_names_beneath,
    open_readonly_file_beneath,
    read_bytes_beneath,
    read_text_beneath,
    unlink_file_beneath,
    write_text_atomic_beneath,
)
from ..user_space.owner_quota import OwnerQuotaChange
from .skill_learning_prompt import SkillLearningDecision
from .skill_learning_publish import (
    PublishRequest,
    SkillLearningGateError,
    _redacted_publish,
    _stage_and_check,
    publish_learned_skill,
    render_learned_skill,
)
from .skill_learning_store import MAX_LEDGER_EVENTS, SkillLearningEvent
from .skill_snapshot import skill_content_sha256

# 单个用户待确认经验提案队列的软上限。
MAX_PENDING_PROPOSALS = 20
# 单个用户待确认经验提案队列的硬上限。
MAX_OVERFLOW_PROPOSALS = 100
_ACTIVE = 'proposals'
_OVERFLOW = 'proposals_overflow'
_INTENT = ('.proposals-intent.json',)
# 提案意图恢复记录允许读取的最大字节数。
_INTENT_BYTES = 65536
_META = frozenset({'revision','status','created_at_ns','archived_at_ns','superseded_by'})


# LLM: 只接受宿主安全编号，不允许模型提供文件路径；候选在同owner学习目录，非正式Skill目录。
# 函数用途: 校验一个学习候选的稳定标识。
def _proposal_name(proposal_id):
    if not isinstance(proposal_id,str) or not proposal_id.startswith('learning-proposal-') or len(proposal_id) != 50 or any(c not in '0123456789abcdef' for c in proposal_id[18:]):
        raise ValueError('学习候选编号无效')
    return proposal_id+'.json'


# LLM: 原state_lock先持owner配额再持state，最后才取候选锁；轮转事件在全部锁外沿原账本入口追加。
# 函数用途: 先收敛上次中断，再保存脱敏且过guard的草稿；同名替代、满额轮转而不自动发布。
def propose_learning(store, publish):
    publish, redacted = _redacted_publish(publish)
    record = {'schema_version':'skill-learning-proposal.v1','revision':1,'status':'pending',
        'decision':asdict(publish.decision),'expected_revision':publish.expected_revision,'expected_sha256':publish.expected_sha256,
        'request':{key:publish.request.get(key,'') for key in ('request_key','run_id','task_id','thread_id')},
        'taken_names':sorted(publish.taken_names),'max_skills':publish.max_skills,'redacted':redacted,
        'created_at_ns':time.time_ns()}
    proposal_id = record['proposal_id'] = _identity_id(record)
    with store.state_lock() as admission:
        with locked_json_path(store.directory/'.proposals'):
            _recover_proposals(store,admission)
            rows = _all_proposals(store)
            if any(row['proposal_id'] == proposal_id for _, row in rows):
                events = []
            else:
                _stage_and_check(store,publish,render_learned_skill(publish.decision))
                final, events = _rotation_plan(store, rows, record)
                _apply_plan(store, rows, final, admission)
    for event in events:
        store.append_event(event)
    return SkillLearningEvent('proposed',skill_name=publish.decision.name,request_key=str(publish.request.get('request_key','')),reason=proposal_id)


# LLM: 创建时间和位置不参与原身份哈希，兼容旧候选；正文、来源request_key和原发布CAS仍逐字绑定。
# 函数用途: 按既有协议计算学习候选稳定编号。
def _identity_id(row):
    identity = json.dumps({'request_key':row['request'].get('request_key'),'decision':row['decision'],
        'expected_revision':row['expected_revision'],'expected_sha256':row['expected_sha256']},sort_keys=True,ensure_ascii=False)
    return 'learning-proposal-'+hashlib.sha256(identity.encode()).hexdigest()[:32]


# LLM: 两处目录共用schema与身份验证，坏文件不当成缺失；目录与叶均走原no-follow链。
# 函数用途: 读取指定固定目录的一份候选，缺失时返回None。
def _read_proposal(store, folder, proposal_id):
    text = read_text_beneath(store.directory,(folder,_proposal_name(proposal_id)))
    if text is None:
        return None
    row = json.loads(text) if text is not None else {}
    if not isinstance(row,dict) or row.get('schema_version') != 'skill-learning-proposal.v1' or row.get('proposal_id') != proposal_id:
        raise ValueError('学习候选不存在或已损坏')
    if _identity_id(row) != proposal_id:
        raise ValueError('学习候选内容已被修改')
    if type(row.get('revision')) is not int or row['revision'] < 1 or row.get('status') not in {'pending','committed','rejected','superseded'}:
        raise ValueError('学习候选状态已损坏')
    return row


# LLM: 只从验证后的事务投影定位，不以主区优先掩盖superseded；调用方已持候选锁。
# 函数用途: 自动定位主区或归档中的准确候选，包括崩溃后尚未落盘的投影。
def _locate_proposal(store, proposal_id):
    _proposal_name(proposal_id)
    found = [(folder,row) for folder,row in _all_proposals(store) if row['proposal_id'] == proposal_id]
    if len(found) != 1:
        raise ValueError('学习候选不存在、重复或已损坏')
    return found[0]


# LLM: 只读判空不创建锁；有候选时统一沿owner配额→state→proposals读取投影，不恢复或修改业务文件。
# 函数用途: 按编号读取主区或overflow候选供用户核对。
def learning_proposal(store, proposal_id):
    _proposal_name(proposal_id)
    if not _has_proposals(store):
        raise ValueError('学习候选不存在、重复或已损坏')
    with store.state_lock(), locked_json_path(store.directory/'.proposals'):
        return _locate_proposal(store,proposal_id)[1]


# LLM: 物理快照允许迁移产生双位置，但绝不跳过坏文件；验证和去重必须在事务投影之后。
# 函数用途: 在调用方已持候选锁时读取两个固定目录的原始候选。
def _physical_proposals(store):
    rows = []
    for folder in (_ACTIVE,_OVERFLOW):
        for name in list_names_beneath(store.directory,(folder,)):
            if not name.startswith('learning-proposal-') or not name.endswith('.json'):
                continue
            row = _read_proposal(store,folder,name[:-5])
            if row is None:
                raise ValueError('学习候选不存在、重复或已损坏')
            rows.append((folder,row))
    return rows


# LLM: 查询只验证、投影，不清理意图或修改业务文件；没有可证明的迁移关系仍拒绝双位置。
# 函数用途: 返回崩溃一致的候选索引，可选只展示主区。
def _all_proposals(store, include_overflow=True):
    rows = _physical_proposals(store)
    intent = _read_intent(store)
    projected = _intent_projection(rows,intent) if intent else _legacy_projection(rows)
    if len({row['proposal_id'] for _,row in projected}) != len(projected):
        raise ValueError('学习候选不存在、重复或已损坏')
    return [(folder,row) for folder,row in projected if include_overflow or folder == _ACTIVE]


# LLM: 空目录只读判空，意图存在即需投影；不清理文件，不因首次查询创建owner或领域锁。
# 函数用途: 判断所选候选范围是否存在可读取文件或待投影事务。
def _has_proposals(store, include_overflow=True):
    folders = (_ACTIVE,_OVERFLOW) if include_overflow else (_ACTIVE,)
    return bool(_read_intent(store)) or any(name.startswith('learning-proposal-') and name.endswith('.json')
               for folder in folders for name in list_names_beneath(store.directory,(folder,)))


# LLM: 默认涵盖归档pending；hot-only调用须显式False，非空查询按owner配额→state→proposals锁序只读投影。
# 函数用途: 列出当前owner完整候选，可显式只读主区，不执行崩溃恢复或迁移。
def list_learning_proposals(store, include_overflow=True):
    if not _has_proposals(store,include_overflow):
        return []
    with store.state_lock(), locked_json_path(store.directory/'.proposals'):
        return [row for _,row in _all_proposals(store,include_overflow)]


# LLM: 新记录用宿主创建时间，旧记录回退真实FD的mtime；绝不拿随机哈希编号作时间排序。
# 函数用途: 取得候选创建或归档的纳秒时间排序键。
def _proposal_time(store, folder, row, field='created_at_ns'):
    value = row.get(field)
    if value is not None:
        if type(value) is not int or value < 1:
            raise ValueError('学习候选时间已损坏')
        return value
    descriptor = open_readonly_file_beneath(store.directory,(folder,_proposal_name(row['proposal_id'])))
    try:
        return os.fstat(descriptor).st_mtime_ns
    finally:
        os.close(descriptor)


# LLM: 移动pending保留原revision和创建时间；superseded单独递增修订，不能伪装成用户拒绝。
# 函数用途: 生成一份归档记录，可选标记它被新同名候选替代。
def _archived_row(store, folder, row, superseded_by=''):
    updated = {**row,'created_at_ns':_proposal_time(store,folder,row),
               'archived_at_ns':row.get('archived_at_ns') or time.time_ns()}
    if superseded_by:
        updated.update(status='superseded',revision=row['revision']+1,superseded_by=superseded_by)
    return updated


# LLM: 只改内存计划，原文件通过验证和配额前不动；替代涵盖主区和overflow所有同名pending。
# 函数用途: 规划同名替代、最旧pending轮转及100条归档裁剪。
def _rotation_plan(store, rows, record):
    active = {row['proposal_id']:row for folder,row in rows if folder == _ACTIVE}
    overflow = {row['proposal_id']:row for folder,row in rows if folder == _OVERFLOW}
    for folder,row in rows:
        if row['status'] != 'pending' or row['decision']['name'] != record['decision']['name']:
            continue
        proposal_id = row['proposal_id']
        active.pop(proposal_id,None)
        overflow[proposal_id] = _archived_row(store,folder,row,record['proposal_id'])
    pending = [row for row in active.values() if row['status'] == 'pending']
    events = []
    while len(pending) >= MAX_PENDING_PROPOSALS:
        oldest = min(pending,key=lambda row:_proposal_time(store,_ACTIVE,row))
        proposal_id = oldest['proposal_id']
        overflow[proposal_id] = _archived_row(store,_ACTIVE,oldest)
        del active[proposal_id]
        pending.remove(oldest)
        events.append(SkillLearningEvent('overflowed','SKILL_LEARNING_PROPOSAL_OVERFLOW_ROTATED',
                      skill_name=oldest['decision']['name'],reason=proposal_id))
    active[record['proposal_id']] = record
    ordered = sorted(overflow.values(),key=lambda row:_proposal_time(store,_OVERFLOW,row,'archived_at_ns'))
    overflow = {row['proposal_id']:row for row in ordered[-MAX_OVERFLOW_PROPOSALS:]}
    return [*((_ACTIVE,row) for row in active.values()), *((_OVERFLOW,row) for row in overflow.values())], events


# LLM: 只执行调用方已获原owner批量配额准入的差异，写在删之前防止迁移丢失；每步仍走原no-follow。
# 函数用途: 应用一组候选内容变更，None表示清理原位置。
def _write_changes(store, changes):
    for parts,text in changes.items():
        if text is not None:
            write_text_atomic_beneath(store.directory,parts,text)
    for parts,text in changes.items():
        if text is None:
            unlink_file_beneath(store.directory,parts)


# LLM: 意图先准入并刷盘，新候选首写作为提交点；普通IO回滚最后删新候选，任意中断仍可重做或验证原态。
# 函数用途: 在候选锁与原owner配额/state事务内安全应用可重启的轮转计划。
def _apply_plan(store, before, after, admission):
    previous = {(folder,row['proposal_id']):row for folder,row in before}
    final = {(folder,row['proposal_id']):row for folder,row in after}
    changes = {(folder,_proposal_name(proposal_id)):json.dumps(row,ensure_ascii=False)
              for (folder,proposal_id),row in final.items() if previous.get((folder,proposal_id)) != row}
    for folder,proposal_id in previous.keys()-final.keys():
        changes[(folder,_proposal_name(proposal_id))] = None
    backup = {parts:read_text_beneath(store.directory,parts) for parts in changes}
    new_ids = {row['proposal_id'] for _,row in after}-{row['proposal_id'] for _,row in before}
    new_id = next(iter(new_ids),'')
    entries = [[folder,proposal_id,_row_hash(previous.get((folder,proposal_id))),_row_hash(final.get((folder,proposal_id))),
                {key:value for key,value in final.get((folder,proposal_id),{}).items() if key in _META}]
               for folder,name in changes for proposal_id in (name[:-5],)]
    intent = json.dumps({'v':1,'new':new_id,'changes':entries},ensure_ascii=False,separators=(',',':'))
    if len(intent.encode()) > _INTENT_BYTES:
        raise ValueError('学习候选事务过大')
    _check_changes(store,admission,changes)
    store.check_write(store.directory.joinpath(*_INTENT),intent)
    write_text_atomic_beneath(store.directory,_INTENT,intent)
    # 新候选必须在所有旧候选修改前完整刷盘，意图本身不复制或授权正文。
    changes = dict(sorted(changes.items(),key=lambda item:item[0] != (_ACTIVE,_proposal_name(new_id)) if new_id else False))
    try:
        _write_changes(store,changes)
    except OSError:
        backup = dict(sorted(backup.items(),key=lambda item:item[0] == (_ACTIVE,_proposal_name(new_id)) if new_id else False))
        _write_changes(store,backup)
        unlink_file_beneath(store.directory,_INTENT)
        raise
    unlink_file_beneath(store.directory,_INTENT)


# LLM: 哈希绑定全部状态、正文与来源字段，不只依赖忽略revision/status的稳定候选ID。
# 函数用途: 为事务变更前后记录计算规范化完整哈希。
def _row_hash(row):
    return hashlib.sha256(json.dumps(row,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest() if row is not None else None


# LLM: 单个固定意图文件有字节/条数上限，路径只接受两处固定目录和原安全编号；损坏不当缺失。
# 函数用途: 通过原no-follow链读取并校验有界候选事务描述。
def _read_intent(store):
    raw = read_bytes_beneath(store.directory,_INTENT,max_bytes=_INTENT_BYTES)
    if raw is None:
        return None
    intent = json.loads(raw)
    if (not isinstance(intent,dict) or type(intent.get('v')) is not int or intent['v'] != 1 or not isinstance(intent.get('new'),str)
            or not isinstance(intent.get('changes'),list) or not 1 <= len(intent['changes']) <= 243):
        raise ValueError('学习候选事务已损坏')
    if intent['new']:
        _proposal_name(intent['new'])
    seen = set()
    for entry in intent['changes']:
        if not isinstance(entry,list) or len(entry) != 5:
            raise ValueError('学习候选事务已损坏')
        folder,proposal_id,before,after,meta = entry
        _proposal_name(proposal_id)
        if folder not in (_ACTIVE,_OVERFLOW) or (folder,proposal_id) in seen or not isinstance(meta,dict) or meta.keys()-_META:
            raise ValueError('学习候选事务路径或状态无效')
        for digest in (before,after):
            if digest is not None and (not isinstance(digest,str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest)):
                raise ValueError('学习候选事务哈希无效')
        seen.add((folder,proposal_id))
    return intent


# LLM: 新候选落盘前只能投影原态；之后按前后哈希验证每个物理前缀，只重建身份不变的宿主元数据。
# 函数用途: 对正常写入、清理或回滚任意中断计算唯一可验证的最终候选状态。
def _intent_projection(rows, intent):
    physical = {(folder,row['proposal_id']):row for folder,row in rows}
    new_id = intent['new']
    committed = not new_id or (_ACTIVE,new_id) in physical
    if new_id and not any(folder == _ACTIVE and proposal_id == new_id and before is None and after
                          for folder,proposal_id,before,after,_ in intent['changes']):
        raise ValueError('学习候选事务提交点无效')
    projected = dict(physical)
    for folder,proposal_id,before,after,meta in intent['changes']:
        key = (folder,proposal_id)
        digest = _row_hash(physical.get(key))
        if digest not in (before,after) or (not committed and digest != before):
            raise ValueError('学习候选事务内容已被修改')
        if not committed:
            continue
        projected.pop(key,None)
        if after is not None:
            candidates = [{**{k:v for k,v in row.items() if k not in _META},**meta}
                          for _,row in rows if row['proposal_id'] == proposal_id]
            matches = [row for row in candidates if _row_hash(row) == after]
            if not matches:
                raise ValueError('学习候选事务缺少可信正文')
            projected[key] = matches[0]
    result = list(projected.items())
    return [(folder,row) for (folder,_),row in result]


# LLM: 兼容旧版本无意图的双位置，只接受原记录加归档元数据或精确同名替代；绝不简单主区/归档优先。
# 函数用途: 验证旧版已写归档尚未删主区的迁移，并生成只读收敛投影。
def _legacy_projection(rows):
    active = {row['proposal_id']:row for folder,row in rows if folder == _ACTIVE}
    removed = set()
    for folder,row in rows:
        old = active.get(row['proposal_id']) if folder == _OVERFLOW else None
        if old is None:
            continue
        expected = {**old,'created_at_ns':row.get('created_at_ns'),'archived_at_ns':row.get('archived_at_ns')}
        if row.get('superseded_by'):
            replacement = active.get(row['superseded_by'])
            if old['status'] != 'pending' or not replacement or replacement['status'] != 'pending' or replacement['decision']['name'] != old['decision']['name']:
                raise ValueError('学习候选替代关系已损坏')
            expected.update(status='superseded',revision=old['revision']+1,superseded_by=replacement['proposal_id'])
        if expected != row or any(type(row.get(field)) is not int or row[field] < 1 for field in ('created_at_ns','archived_at_ns')):
            raise ValueError('学习候选重复或已损坏')
        removed.add(row['proposal_id'])
    return [(folder,row) for folder,row in rows if folder != _ACTIVE or row['proposal_id'] not in removed]


# LLM: 删除和写入一起投影最终占额；意图清理同时计入，移动的短暂副本不误算为永久双份。
# 函数用途: 通过原owner准入检查完整候选变更的最终字节。
def _check_changes(store, admission, changes):
    admission.check([OwnerQuotaChange(store.directory.joinpath(*parts),len(text.encode('utf-8')) if text is not None else None)
                     for parts,text in {**changes,_INTENT:None}.items()])


# LLM: 只在owner配额→state→候选锁序的写入口重放，恢复不移除意图直到所有目标写入和删除完成。
# 函数用途: 将已验证的事务或旧版迁移投影幂等落盘，允许恢复自身再次崩溃。
def _recover_proposals(store, admission):
    rows = _physical_proposals(store)
    intent = _read_intent(store)
    projected = _intent_projection(rows,intent) if intent else _legacy_projection(rows)
    if len({row['proposal_id'] for _,row in projected}) != len(projected):
        raise ValueError('学习候选重复或已损坏')
    if not intent:
        if rows != projected:
            _apply_plan(store,rows,projected,admission)
        return
    physical = {(folder,_proposal_name(row['proposal_id'])):row for folder,row in rows}
    final = {(folder,_proposal_name(row['proposal_id'])):row for folder,row in projected}
    changes = {parts:json.dumps(row,ensure_ascii=False) for parts,row in final.items() if physical.get(parts) != row}
    changes.update(dict.fromkeys(physical.keys()-final.keys()))
    _check_changes(store,admission,changes)
    _write_changes(store,changes)
    unlink_file_beneath(store.directory,_INTENT)


# LLM: 只在本次原state事务内借用admission；不修改共享store，也不让publisher重取不可重入的quota/state锁。
# 类用途: 给原publisher提供已有事务的同步视图，其余属性和写入仍委托原store。
class _ProposalPublishStore:
    # LLM: 该视图仅由持原state锁的resolve栈创建，不注册或缓存额外配额权威。
    # 函数用途: 绑定原store及其当前准入对象。
    def __init__(self, store, admission):
        self._store, self._admission = store, admission

    # LLM: 原写者的check_write、save_registry及版本写入仍读取原ContextVar准入。
    # 函数用途: 将发布数据操作委托到同一owner原store。
    def __getattr__(self, name):
        return getattr(self._store,name)

    # LLM: 同步publisher只复用仍生效的准入；视图逃逸或事务结束立即拒绝，绝不把旧admission当新授权。
    # 函数用途: 在已持锁的发布调用内免除重复取锁。
    @contextmanager
    def state_lock(self):
        if self._store._quota_admission.get() is not self._admission:
            raise RuntimeError('学习候选发布缺少原配额事务')
        yield self._admission


# LLM: resolve已持原owner准入；复用原有界私有JSONL写者而非append_event再次取quota，保留先记账后确认的崩溃重试点。
# 函数用途: 在原准入中核算并追加确认事件，账本路径、容量与编码合同同SkillLearningStore.append_event。
def _append_resolution_event(store, admission, event):
    row = event.to_record()
    existing = read_jsonl_objects_report(store.ledger_path,context='skill_learning.ledger.quota').records if store.ledger_path.exists() else []
    text = ''.join(json.dumps(item,ensure_ascii=False,sort_keys=True)+'\n' for item in [*existing,row][-MAX_LEDGER_EVENTS:])
    admission.check([OwnerQuotaChange(store.ledger_path,len(text.encode()))])
    append_private_jsonl_capped(store.ledger_path,row,max_records=MAX_LEDGER_EVENTS)


# LLM: 仅用户CLI/结构化控制调用；owner配额→state→候选锁贯穿原publisher和确认落盘，CAS/guard/actor不变。
# 函数用途: 先恢复候选迁移再确认或拒绝准确草稿，不把开关或模型理由当确认。
def resolve_learning_proposal(store, proposal_id, expected_revision, *, decision, guard_config=None, actor_id=""):
    if not isinstance(actor_id,str) or len(actor_id)>300 or len(actor_id.splitlines())>1:
        raise ValueError('学习确认者编号无效')
    if decision not in {'confirm','reject'} or isinstance(expected_revision,bool) or not isinstance(expected_revision,int):
        raise ValueError('学习候选决定无效')
    # publisher复用当前state准入，账本沿原私有writer追加，不能在候选锁内重新申请owner配额。
    with store.state_lock() as admission, locked_json_path(store.directory/'.proposals'):
        _recover_proposals(store,admission)
        folder,row = _locate_proposal(store,proposal_id)
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
                event = publish_learned_skill(_ProposalPublishStore(store,admission),PublishRequest(SkillLearningDecision(**draft),request,frozenset(row['taken_names']),
                    row['max_skills'],guard_config,row['expected_revision'],row['expected_sha256']))
            if row.get('redacted'):
                event = replace(event,code='SKILL_LEARNING_REDACTED')
            _append_resolution_event(store,admission,event)
            row.update(status='committed',receipt=event.to_record())
        else:
            row['status'] = 'rejected'
        row['resolved_by'] = {'actor_id':actor_id,'owner_scope':'current_store'}
        row['revision'] += 1
        text = json.dumps(row,ensure_ascii=False)
        store.check_write(store.directory/folder/_proposal_name(proposal_id),text)
        write_text_atomic_beneath(store.directory,(folder,_proposal_name(proposal_id)),text)
    return row
