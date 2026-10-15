# LLM: 复刻记忆只接显式approved/promoted候选ID，不遍历聊天/Persona/配置；目标仍进入原候选状态机，不自动复制权限或人格。
# 模块用途: 固定允许分享的记忆候选，并在同一接收事务写原candidates.jsonl，失败/崩溃不留活动记忆副本。
from __future__ import annotations

import json
from contextlib import contextmanager

from ..common.json_io import locked_json_path, write_private_text_file_atomic_unlocked
from ..memory_store.candidate_models import CandidateObservation
from ..memory_store.candidates import CandidateService, merge_candidate_observations
from ..user_space.owner_quota import OwnerQuotaChange
from .skill_transfer_manifest import SkillTransferError, manifest_hash, safe_payload


# LLM: 来源必须是本owner明确审核过的候选，导出仅内容与类型/来源ID，不复制原聊天证据、私有路径、人格或权限落点。
# 函数用途: 按准确候选ID冻结复刻清单；普通Skill投递不调用此入口。
def build_memory_candidates(agent, identifiers):
    if not isinstance(identifiers, (list, tuple)) or len(identifiers) > 50 or len(set(identifiers)) != len(identifiers):
        raise SkillTransferError('SKILL_TRANSFER_MEMORY_MANIFEST_INVALID')
    result = []
    for identifier in identifiers:
        if not isinstance(identifier, str):
            raise SkillTransferError('SKILL_TRANSFER_MEMORY_MANIFEST_INVALID')
        row = agent.memory_candidates.get(identifier)
        if row.status not in {'approved', 'promoted'} or row.candidate_type in {'soul_change', 'hot_rule', 'working_agreement', 'discard'}:
            raise SkillTransferError('SKILL_TRANSFER_MEMORY_NOT_EXPORTABLE')
        raw = safe_payload(row.content.encode())
        result.append({'source_candidate_id': row.candidate_id, 'candidate_type': row.candidate_type,
            'content': raw.decode(), 'content_hash': manifest_hash(row.content),
            'target_scope': {'scope_type': 'global', 'scope_key': 'global'}, 'target_status': 'pending_review'})
    return result


# LLM: target候选采用原merge/schema，origin=reviewed且long_term为manual_required；迁入候选不等于已晋升长期事实。
# 函数用途: 为已双端确认的一批记忆产生稳定、互不覆盖原候选的观察。
def _observations(manifest, operation_id):
    result = []
    for row in manifest.get('memory_candidates', []):
        if manifest_hash(row['content']) != row['content_hash'] or len(row['content']) > 2000:
            raise SkillTransferError('SKILL_TRANSFER_MEMORY_MANIFEST_INVALID')
        safe_payload(row['content'].encode())
        key = 'transfer:' + operation_id + ':' + row['source_candidate_id']
        result.append(CandidateObservation(candidate_type=row['candidate_type'], content=row['content'], subject_key=key,
            scope=row['target_scope'], origin='reviewed', promotion_target='long_term', proposed_action='add',
            observation_id=key, evidence_refs=({'kind': 'skill_transfer', 'operation_id': operation_id,
                'source_candidate_id': row['source_candidate_id'], 'manifest_hash': manifest_hash(manifest)},)))
    return result


# LLM: 候选原文件锁覆盖Skill批次到registry提交，读取者不能观察写入后又回滚的中间态；quota使用外层同一admission。
# 函数用途: 合并与保存整批候选，失败回滚原账，不另建记忆库或自动晋升。
@contextmanager
def memory_import_transaction(host, manifest, operation_id, admission):
    path = host.home_paths.owner_memory_candidates_jsonl
    if not manifest.get('memory_candidates'):
        yield _MemoryImport(path, [], [], [], admission)
        return
    service = CandidateService(path)
    with locked_json_path(path):
        before = service._load_unlocked()
        existing = [row for row in before if row.subject_key.startswith('transfer:' + operation_id + ':')]
        expected = {'transfer:' + operation_id + ':' + row['source_candidate_id']: row['content'] for row in manifest['memory_candidates']}
        if any(row.status not in {'pending_review', 'observed', 'blocked_missing_evidence'} or row.content != expected.get(row.subject_key) for row in existing):
            raise SkillTransferError('SKILL_TRANSFER_OPERATION_CONFLICT')
        baseline = [row for row in before if row not in existing]
        observations = _observations(manifest, operation_id)
        merged, selected = merge_candidate_observations(baseline, observations) if observations else (baseline, [])
        plan = _MemoryImport(path, baseline, merged, selected, admission)
        try:
            yield plan
        finally:
            if plan.applied and not plan.finished:
                plan.restore()


# LLM: 计划只在原文件锁/Skill事务生命周期存在，finish不是独立成功权威，真正成功仍读取registry.import_operations。
# 类用途: 暂存本次记忆候选的原账前后像与受控写入动作。
class _MemoryImport:
    # LLM: 不写文件，计量用同一序列化，不能先observe_many再补Skill。
    # 函数用途: 准备原候选文件的批次替换。
    def __init__(self, path, before, after, selected, admission):
        self.path, self.before, self.after, self.admission = path, before, after, admission
        self.ids = [row.candidate_id for row in selected]
        self.text = self._text(after)
        self.changes = [OwnerQuotaChange(path, len(self.text.encode()))] if selected else []
        self.applied = self.finished = False

    # LLM: 与原CandidateService相同JSONL schema/排序，不引入影子格式。
    # 函数用途: 将已校验候选序列化为原账本。
    @staticmethod
    def _text(rows):
        return ''.join(json.dumps(row.to_record(), ensure_ascii=False, sort_keys=True) + '\n' for row in rows)

    # LLM: 写前标记可能已发生副作用，异常也触发同锁回滚；空记忆清单不重写原文件。
    # 函数用途: 保存原候选批次，仍未宣称整个复刻成功。
    def apply(self):
        if self.ids:
            self.admission.check(self.changes)
            self.applied = True
            write_private_text_file_atomic_unlocked(self.path, self.text)

    # LLM: 只在registry同批committed已可读后调用，不单独扣一次复刻计数。
    # 函数用途: 解除本事务的回滚标志。
    def finish(self):
        self.finished = True

    # LLM: 原锁下恢复准确前像，不会删除其它并发候选；外层state仍保护同一操作。
    # 函数用途: 撤销未提交复刻对原候选文件的修改。
    def restore(self):
        write_private_text_file_atomic_unlocked(self.path, self._text(self.before))
