"""检测提案、证据驱动调优、实算基线及宿主绑定的社区规则目录。

身份与空间取实时 V13Call；公开投影保持闭合。跨集合操作采用准备回执、
稳定子幂等键和独立编排锁，不嵌套 Store 事务。社区网络仅由提供者持有。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone

from ..common.directory_lock import locked_private_directory
from ..conversation.business_access import BusinessError, require_revision
from ..plugin_host.contracts import DomainResult
from ..plugin_host.errors import HostAPIError
from ..plugin_host.schemas import thaw, validate_object
from .v13_store import v13_store_for

EVALUATION_RESULT_SCHEMA = {
    'type': 'object', 'properties': {
        'proposal_id': {'$ref': '#/$defs/Id'},
        'mode': {'type': 'string', 'enum': ['eval', 'rule', 'backtest']},
        'matches': {'anyOf': [{'type': 'integer', 'minimum': 0}, {'type': 'null'}]},
        'estimated_false_positive_ratio': {'anyOf': [{'type': 'number'}, {'type': 'null'}]},
        'findings': {'type': 'array', 'items': {'type': 'string', 'minLength': 0, 'maxLength': 1024},
                     'maxItems': 50},
        'coverage': {'$ref': '#/$defs/Coverage'},
    },
    'required': ['proposal_id', 'mode', 'matches', 'estimated_false_positive_ratio', 'findings', 'coverage'],
    'additionalProperties': False,
}


# LLM: 分片可先于注册器导入，顶层反向导入会在静态登记动作时形成环。
# 函数用途: 实际调用时取得基础 API，不构造第二份注册表。
def _api():
    from ..gateway_parts import business_api_v13
    return business_api_v13


# LLM: 指纹只编码结构化事实，不依赖展示文字或字典插入顺序。
# 函数用途: 计算稳定 JSON 摘要。
def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


# LLM: 编号绑定真实主体与规范动作，同键跨进程重试不会创建第二条记录。
# 函数用途: 生成领域记录编号。
def _operation_id(call, label):
    return label + '-' + _digest([call.owner_id, call.subject_id, call.action, call.idempotency_key])[:48]


# LLM: 子步骤不能复用同一回执槽。
# 函数用途: 派生受限长度的内部幂等键。
def _step_key(call, label):
    return 'lifecycle-' + _digest([call.action, call.idempotency_key, label])[:48]


# LLM: 记录 CAS 参与请求指纹，但不误传为集合 CAS。
# 函数用途: 生成写入参数指纹。
def _fingerprint(call):
    return _digest([call.action, call.params, call.expected_revision])


# LLM: 所有时间来自注入时钟，不从正文推断。
# 函数用途: 生成统一 UTC 时间。
def _now(call):
    return datetime.fromtimestamp(call.clock(), timezone.utc).isoformat()


# LLM: 缺省空间沿同片唯一活动空间解析器，实际空间再由 W0 冻结入口核验。
# 函数用途: 解析并授权目标空间。
def _space(call, space_id=None):
    from .v13_w7_simulation_sessions import resolve_space
    return resolve_space(call, space_id)['space_id']


# LLM: 内部晋升预留不作为同行评审内容展示。
# 函数用途: 生成闭合提案投影。
def _proposal_view(row):
    keys = ['proposal_id', 'space_id', 'title', 'rationale', 'rule', 'mitre_techniques', 'status', 'author', 'source', 'comments', 'evaluations', 'decision', 'promoted_rule_id', 'revision', 'created_at', 'updated_at']
    result = {key: row[key] for key in keys}
    if isinstance(result['decision'], dict):
        result['decision'] = {key: value for key, value in result['decision'].items() if key != '_promotion'}
    return result


# LLM: 私有空间、版本和时间不进入调优结果的闭合 schema。
# 函数用途: 投影一条建议。
def _suggestion_view(row):
    return {key: row[key] for key in ['suggestion_id', 'rule_id', 'kind', 'field', 'value', 'evidence_alert_ids', 'false_positive_count', 'status', 'reason']}


# LLM: 基线值仅由实算产生。
# 函数用途: 投影基线公开字段。
def _baseline_view(row):
    return {key: row[key] for key in ['baseline_id', 'space_id', 'rule_id', 'metric', 'value', 'window', 'created_at']}


# LLM: 缺记录与其它 owner 都返回 404。
# 函数用途: 核对记录归属和实时访问权。
def _record(call, row):
    if row is None or row.get('owner_id') != call.owner_id:
        raise BusinessError('RESOURCE_NOT_FOUND')
    _api().v13_require_access(call, row)
    return row


# LLM: 编排锁与 Store 锁不同，不能在 mutate 中再发起集合事务。
# 函数用途: 串行同 owner 生命周期变更。
def _lifecycle_lock(call):
    return locked_private_directory(call.owner_home, relative_parts=('data', 'business_v13'),
                                    lock_name='.detection-lifecycle.lock')


# LLM: Store 回放先于 mutate，记录权限必须绑定每次准入和提交末核，不可只在回调内检查。
# 函数用途: 将准确目标记录及已解析空间的实时权限绑定到事务句柄。
def _guarded_store(call, *, records=(), space_ids=()):
    store = v13_store_for(call)
    previous = store.authorize
    targets = list(records)
    for field, collection, required in (
        ('proposal_id', 'detection_proposals', True), ('rule_id', 'monitor_rules', True),
        ('suggestion_id', 'detection_tuning', False), ('job_id', 'async_jobs', False),
    ):
        if call.params.get(field) is not None:
            targets.append((collection, call.params[field], required))
    spaces = set(space_ids)
    if call.params.get('space_id') is not None:
        spaces.add(call.params['space_id'])

    # LLM: 每次读取当前记录，不缓存授权决定，不把输入 ID 当实际 record。
    # 函数用途: 在回执重放前及提交前复核精确空间和记录。
    def authorize():
        previous()
        for collection, record_id, required in targets:
            row = store.get(collection, record_id)
            if row is not None or required:
                _record(call, row)
        for space_id in spaces:
            _api().v13_require_access(call, {'owner_id': call.owner_id, 'space_id': space_id})
        return True

    store.authorize = authorize
    authorize()
    return store


# LLM: 基础 Store 在执行 mutate 前校验同笔回执。
# 函数用途: 执行不带集合 CAS 的记录事务。
def _transaction(call, collection, mutate, *, step=None, space_ids=(), records=()):
    return _guarded_store(call, space_ids=space_ids, records=records).transaction(collection, mutate,
        idempotency_key=call.idempotency_key if step is None else _step_key(call, step),
        params_digest=_fingerprint(call))


# LLM: 列表省略空间时跨调用者可见空间读取，不隐式选择默认空间。
# 函数用途: 构造精确分页条件。
def _filters(call, names):
    filters = {'owner_id': call.owner_id}
    if 'space_id' in call.params:
        _space(call)
    for key in names:
        if key in call.params:
            filters[key] = call.params[key]
    return filters


# LLM: 游标的 owner、筛选、revision 绑定由基础 Store 持有。
# 函数用途: 分页并逐条复核公开记录。
def _page(call, collection, filters, project):
    result = v13_store_for(call).page(collection, filters=filters,
        limit=call.params.get('limit', 50), cursor=call.params.get('cursor'))
    result['items'] = [project(_record(call, row)) for row in result['items']]
    return result


# LLM: 不接受空值、无时区或自然语言时间。
# 函数用途: 将合同时间转换为 epoch 秒。
def _epoch(value):
    if type(value) is not str:
        raise ValueError('timestamp')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timezone')
    return parsed.timestamp()


# LLM: 源记录坏 ID 不得进入证据数组或聚合分母；字节限制与冻结 Id 一致。
# 函数用途: 检查来源编号是否可无损投影到公开合同。
def _valid_id(value):
    return type(value) is str and 1 <= len(value.encode('utf-8')) <= 128


# LLM: Timestamp schema 可空，但实算窗口必须有实际递增边界。
# 函数用途: 检查回测与基线窗口。
def _window(window):
    try:
        since, until = _epoch(window['since']), _epoch(window['until'])
        if since >= until:
            raise ValueError('window')
        return since, until
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise BusinessError('INVALID_SCHEMA', details={'path': 'window'}) from error


# LLM: 入口 schema 已检查非空短字符串；不额外收紧冻结契约为 MITRE 正则。
# 函数用途: 保留调用方提供的结构化技术标签。
def _techniques(values):
    return values


# LLM: 示例只给 status 时不要求 space_id。
# 函数用途: 列出检测提案。
def list_proposals(call):
    return _page(call, 'detection_proposals', _filters(call, ('space_id', 'status')), _proposal_view)


# LLM: 归属以耐久记录为准。
# 函数用途: 读取单个提案或返回 404。
def get_proposal(call):
    row = v13_store_for(call).get('detection_proposals', call.params['proposal_id'])
    return {'proposal': _proposal_view(_record(call, row))}


# LLM: 保存草案不执行查询、脚本或模型。
# 函数用途: 幂等创建 pending 提案。
def create_proposal(call):
    space_id = _space(call)
    techniques = _techniques(call.params.get('mitre_techniques', []))

    # LLM: 时间和编号只在首次提交时确定。
    # 函数用途: 写入提案及其回执。
    def mutate(records):
        proposal_id, at = _operation_id(call, 'proposal'), _now(call)
        row = {'proposal_id': proposal_id, 'owner_id': call.owner_id, 'space_id': space_id,
            'title': call.params['title'], 'rationale': call.params.get('rationale', ''),
            'rule': call.params['rule'], 'mitre_techniques': techniques, 'status': 'pending',
            'author': call.subject_id, 'source': call.params.get('source', 'analyst'),
            'comments': [], 'evaluations': [], 'decision': None, 'promoted_rule_id': None,
            'revision': 0, 'created_at': at, 'updated_at': at}
        records[proposal_id] = row
        return {'proposal': _proposal_view(row)}

    with _lifecycle_lock(call):
        return _transaction(call, 'detection_proposals', mutate, space_ids=(space_id,))


# LLM: 评论作者只取实时主体。
# 函数用途: 原子追加同行评论。
def comment(call):
    # LLM: 使用锁内最新数组，防止丢失并发评论。
    # 函数用途: 追加有界评论并增加版本。
    def mutate(records):
        row = _record(call, records.get(call.params['proposal_id']))
        if len(row['comments']) >= 200:
            raise BusinessError('LIMIT_EXCEEDED')
        at = _now(call)
        row['comments'].append({'comment_id': _operation_id(call, 'comment'), 'author': call.subject_id,
                                'text': call.params['text'], 'created_at': at})
        row.update(revision=row['revision'] + 1, updated_at=at)
        return {'proposal': _proposal_view(row)}

    with _lifecycle_lock(call):
        return _transaction(call, 'detection_proposals', mutate)


# LLM: 拒绝终态复活、评估中改判及晋升预留后的并发改判。
# 函数用途: 按记录 CAS 保存同行评审。
def decide(call):
    # LLM: 记录 CAS 在 mutate 内检查，不使用集合 revision。
    # 函数用途: 执行合法的评审状态转换。
    def mutate(records):
        row = _record(call, records.get(call.params['proposal_id']))
        require_revision(call.expected_revision, row['revision'])
        if (row['status'] not in {'draft', 'pending', 'changes_requested', 'approved'}
                or (row['decision'] or {}).get('_promotion') is not None):
            raise BusinessError('STATE_CONFLICT')
        decision, at = call.params['decision'], _now(call)
        row.update(status={'approve': 'approved', 'reject': 'rejected',
            'request_changes': 'changes_requested'}[decision],
            decision={'decision': decision, 'reviewer': call.subject_id,
                'comment': call.params.get('comment', ''), 'at': at},
            revision=row['revision'] + 1, updated_at=at)
        return {'proposal': _proposal_view(row)}

    with _lifecycle_lock(call):
        return _transaction(call, 'detection_proposals', mutate)


# LLM: 模型正文不是执行事实；这里只接受严格单个 JSON 对象和已冻结结果 schema。
# 函数用途: 解析并校验 detection_proposal_evaluation 的模型结果。
def finalize_evaluation(text):
    result = json.loads(text)
    json.dumps(result, allow_nan=False)
    return validate_object(result, _api().v13_schema(EVALUATION_RESULT_SCHEMA))


# LLM: 每个提案各有独立子键，批量重试不会重复派发已受理作业。
# 函数用途: 提交单项或当前空间全部 pending 提案的异步评估。
def evaluate(call):
    mode = call.params.get('mode', 'eval')
    if 'window' in call.params:
        _window(call.params['window'])
    if mode == 'backtest' and 'window' not in call.params:
        raise BusinessError('INVALID_SCHEMA', details={'path': 'window'})
    store = v13_store_for(call)
    proposal_id = call.params.get('proposal_id')
    if proposal_id is not None:
        selected = _record(call, store.get('detection_proposals', proposal_id))
        space_id = _space(call, selected['space_id'])
        if call.params.get('space_id', space_id) != space_id:
            raise BusinessError('SPACE_SCOPE_MISMATCH')
    else:
        space_id = _space(call)

    # LLM: 受理前先耐久保存有界目标快照；失败重试沿原目标，不重新扫描扩批。
    # 函数用途: 预留评估条目并使提案进入 evaluating。
    def prepare(records):
        rows = ([_record(call, records.get(proposal_id))] if proposal_id is not None else
            [row for row in records.values() if row.get('owner_id') == call.owner_id
             and row.get('space_id') == space_id and row.get('status') == 'pending'])
        rows = sorted(rows, key=lambda row: row['proposal_id'])
        if not rows:
            raise BusinessError('STATE_CONFLICT')
        if len(rows) > 100:
            raise BusinessError('LIMIT_EXCEEDED')
        targets = []
        for row in rows:
            _record(call, row)
            if row['status'] not in {'draft', 'pending', 'changes_requested'}:
                raise BusinessError('STATE_CONFLICT')
            if len(row['evaluations']) >= 50:
                raise BusinessError('LIMIT_EXCEEDED')
            child = replace(call, idempotency_key=_step_key(call, row['proposal_id']),
                params={'proposal_id': row['proposal_id'], 'mode': mode, 'space_id': space_id,
                        **({'window': call.params['window']} if 'window' in call.params else {})})
            job_id, at = _operation_id(child, 'job'), _now(call)
            row['evaluations'].append({'job_id': job_id, 'mode': mode, 'state': 'preparing',
                                       'summary': None, 'at': at})
            row.update(status='evaluating', revision=row['revision'] + 1, updated_at=at)
            targets.append({'proposal_id': row['proposal_id'], 'rule': row['rule'],
                            'job_id': job_id, 'child_key': child.idempotency_key})
        return {'targets': targets, 'space_id': space_id}

    with _lifecycle_lock(call):
        prepared = _transaction(call, 'detection_proposals', prepare, step='evaluation-prepare', space_ids=(space_id,))
        job_ids, operation_ids = [], []
        for target in prepared['targets']:
            child = replace(call, idempotency_key=target['child_key'],
                params={'proposal_id': target['proposal_id'], 'mode': mode,
                    'space_id': prepared['space_id'],
                    **({'window': call.params['window']} if 'window' in call.params else {})})
            facts = {'proposal_id': target['proposal_id'], 'mode': mode, 'rule': target['rule'],
                     'window': call.params.get('window'), 'result_schema': EVALUATION_RESULT_SCHEMA}
            instruction = ('按给定规则和数据源评估；没有证据时数值用 null、coverage 用 unknown。'
                '只返回符合 result_schema 的 JSON，不执行规则发布。\n' +
                json.dumps(facts, ensure_ascii=False, sort_keys=True, allow_nan=False))
            accepted = _api().v13_start_model_job(child, 'detection_proposal_evaluation', instruction,
                                           space_id=prepared['space_id'])
            data = thaw(accepted.data) if isinstance(accepted, DomainResult) else accepted
            job_ids.append(data['job_id'])
            operation_ids.append(data['operation_id'])

            # LLM: 作业已受理后的回放不能把已完成评估倒退为 accepted。
            # 函数用途: 绑定原生作业编号并单调推进评估条目。
            def bind(records):
                row = _record(call, records.get(target['proposal_id']))
                entry = next((entry for entry in row['evaluations']
                              if entry['job_id'] == target['job_id']), None)
                if entry is None:
                    raise BusinessError('STATE_CONFLICT')
                if entry['state'] == 'preparing':
                    entry.update(job_id=data['job_id'], state='accepted', at=_now(call))
                    row.update(revision=row['revision'] + 1, updated_at=_now(call))
                return {'job_id': data['job_id']}

            _transaction(call, 'detection_proposals', bind, step='evaluation-bind-' + target['proposal_id'],
                space_ids=(prepared['space_id'],), records=(('detection_proposals', target['proposal_id'], True),))
        return DomainResult(202, {'state': 'accepted',
            'operation_id': operation_ids[0],
            'job_ids': job_ids})


# LLM: 在 W0 提交 completed 前验证同 owner、同空间、准确提案与 mode；仅只读，不改终态。
# 函数用途: 防止模型把另一提案或另一模式的结果记成已完成。
def validate_evaluation_result(call, job_row, result):
    _record(call, job_row)
    result = validate_object(result, _api().v13_schema(EVALUATION_RESULT_SCHEMA))
    matches = [row for row in v13_store_for(call).list('detection_proposals')
        if row.get('owner_id') == call.owner_id and any(
            item['job_id'] == job_row['job_id'] for item in row.get('evaluations', []))]
    if job_row.get('kind') != 'detection_proposal_evaluation' or len(matches) != 1:
        raise BusinessError('DOMAIN_RESULT_INVALID')
    proposal = _record(call, matches[0])
    entry = next(item for item in proposal['evaluations'] if item['job_id'] == job_row['job_id'])
    if (job_row.get('space_id') != proposal['space_id'] or result['proposal_id'] != proposal['proposal_id']
            or result['mode'] != entry['mode']):
        raise BusinessError('DOMAIN_RESULT_INVALID')
    return True


# LLM: W0 必须在 async_jobs.get 的终态重读也调用此函数；失败后的同笔回写可恢复。
# 函数用途: 将绑定的作业终态落回提案，拒绝错提案、错模式和晚到状态回退。
def complete_evaluation(call, job):
    if job.get('kind') != 'detection_proposal_evaluation' or job.get('state') not in {'completed', 'failed'}:
        return {'updated': False}
    _record(call, job)
    store = v13_store_for(call)
    matches = [row for row in store.list('detection_proposals')
        if row.get('owner_id') == call.owner_id and any(
            item['job_id'] == job['job_id'] for item in row.get('evaluations', []))]
    if len(matches) != 1:
        raise BusinessError('DOMAIN_RESULT_INVALID')
    observed = _record(call, matches[0])
    if job.get('space_id') != observed['space_id']:
        raise BusinessError('DOMAIN_RESULT_INVALID')
    entry = next(item for item in observed['evaluations'] if item['job_id'] == job['job_id'])
    result = job.get('result')
    if job['state'] == 'completed':
        result = validate_object(result, _api().v13_schema(EVALUATION_RESULT_SCHEMA))
        if result['proposal_id'] != observed['proposal_id'] or result['mode'] != entry['mode']:
            raise BusinessError('DOMAIN_RESULT_INVALID')
    if entry['state'] in {'completed', 'failed'}:
        return {'updated': False}

    # LLM: 锁内检查当前 job 绑定和状态，不以锁外提案快照整条覆盖。
    # 函数用途: 追加评估摘要并使当前 evaluating 提案回到 pending 待评审。
    def mutate(records):
        row = _record(call, records.get(observed['proposal_id']))
        current = next((item for item in row['evaluations'] if item['job_id'] == job['job_id']), None)
        if current is None or current['state'] in {'completed', 'failed'}:
            return {'updated': False}
        if row['status'] != 'evaluating' or row['evaluations'][-1]['job_id'] != job['job_id']:
            return {'updated': False}
        current.update(state=job['state'], summary=result if job['state'] == 'completed' else None,
                       at=_now(call))
        row.update(status='pending', revision=row['revision'] + 1, updated_at=_now(call))
        return {'updated': True, 'proposal': _proposal_view(row)}

    with _lifecycle_lock(call):
        completion = replace(call, idempotency_key='evaluation-result-' + _digest(job['job_id'])[:48],
            params={'job_id': job['job_id'], 'proposal_id': observed['proposal_id'],
                    'state': job['state'], 'result': result}, expected_revision=None)
        return _transaction(completion, 'detection_proposals', mutate)


# LLM: 晋升必须走与手动创建相同的原生规则路径，不能只插一条 monitor_rules JSON。
# 函数用途: 将已批准提案幂等晋升为一个真实规则。
def promote(call):
    from .v13_w7_monitors import create_rule
    store = _guarded_store(call)

    # LLM: 准备回执只冻结输入，不预先写规则或伪造已晋升状态。
    # 函数用途: 核验首次 CAS 并冻结晋升输入。
    def prepare(records):
        row = _record(call, records.get(call.params['proposal_id']))
        require_revision(call.expected_revision, row['revision'])
        if row['status'] != 'approved':
            raise BusinessError('STATE_CONFLICT')
        return {'params': {**row['rule'], 'space_id': row['space_id'],
            'mitre_techniques': row['mitre_techniques'], 'enabled': call.params.get('enabled', True)}}

    # LLM: 同笔多集合回执先于 mutate，已晋升后同键仍返回第一次完整结果。
    # 函数用途: 将原生规则创建和提案状态绑定在同一个日志提交点。
    def commit(native_mutate, extra):
        # LLM: 在原生调度等副作用前检查最新提案 CAS，失败不写任一业务集合。
        # 函数用途: 原子创建规则并完成晋升。
        def mutate(tables):
            row = _record(call, tables['detection_proposals'].get(call.params['proposal_id']))
            require_revision(call.expected_revision, row['revision'])
            if row['status'] != 'approved':
                raise BusinessError('STATE_CONFLICT')
            result = native_mutate(tables['monitor_rules'])
            row.update(status='promoted', promoted_rule_id=result['rule']['rule_id'],
                       revision=row['revision'] + 1, updated_at=_now(call))
            return {'proposal': _proposal_view(row), 'rule': result['rule']}

        return store.transaction_many(('monitor_rules', 'detection_proposals'), mutate,
            idempotency_key=call.idempotency_key,
            params_digest=_digest([_fingerprint(call), prepared, extra]))

    with _lifecycle_lock(call):
        prepared = _transaction(call, 'detection_proposals', prepare, step='promotion-prepare')
        child = replace(call, params=prepared['params'], expected_revision=None,
                        idempotency_key=_step_key(call, 'promotion-native'))
        return create_rule(child, origin='proposal', commit=commit)


V13_JOB_KINDS = {'detection_proposal_evaluation': {
    'result_schema': EVALUATION_RESULT_SCHEMA, 'finalize': finalize_evaluation,
}}


# LLM: 聚合前剔除缺 ID、错误 owner、无时区时间和坏实体，重复业务 ID 只计一次。
# 函数用途: 读取 W8 的真实告警及反馈，并返回排除数量供覆盖说明使用。
def _soc_facts(call):
    store, alerts, feedback, excluded = v13_store_for(call), {}, {}, 0
    for row in store.list('soc_alerts'):
        try:
            if (row.get('owner_id') != call.owner_id or not _valid_id(row.get('alert_id'))
                    or not _valid_id(row.get('space_id')) or 'rule_id' not in row
                    or (row['rule_id'] is not None and not _valid_id(row['rule_id']))
                    or type(row.get('entities')) is not list):
                raise ValueError('alert')
            _epoch(row['created_at'])
            _record(call, row)
            if row['alert_id'] in alerts:
                excluded += 1
                continue
            alerts[row['alert_id']] = row
        except (KeyError, TypeError, ValueError, OverflowError):
            excluded += 1
    for row in store.list('soc_feedback'):
        try:
            if (row.get('owner_id') != call.owner_id or not _valid_id(row.get('override_id'))
                    or 'rule_id' not in row or type(row.get('override_verdict')) is not str
                    or row.get('source') not in {'override', 'bulk_triage', 'redisposition', 'alert_update'}):
                raise ValueError('feedback')
            alert = alerts.get(row.get('alert_id'))
            if (alert is None or row.get('space_id') != alert['space_id']
                    or row.get('rule_id') != alert['rule_id']):
                raise ValueError('feedback_binding')
            _epoch(row['created_at'])
            _record(call, row)
            if row['override_id'] in feedback:
                excluded += 1
                continue
            feedback[row['override_id']] = row
        except (KeyError, TypeError, ValueError, OverflowError):
            excluded += 1
    return alerts, list(feedback.values()), excluded


# LLM: 后来的真实改判取代旧反馈，不把同一告警的反复改判累加为多个证据。
# 函数用途: 按告警选择最新结构化反馈。
def _latest_feedback(feedback):
    latest = {}
    for row in sorted(feedback, key=lambda item: (_epoch(item['created_at']), item['override_id'])):
        latest[row['alert_id']] = row
    return latest


# LLM: 省略 rule_id 的基线可覆盖整个真实空间。
# 函数用途: 分页读取已计算基线。
def list_baselines(call):
    return _page(call, 'detection_baselines', _filters(call, ('space_id', 'rule_id')), _baseline_view)


# LLM: value 不从请求接受；缺少反馈分母时拒绝创建，不能伪造 0。
# 函数用途: 从窗口内真实告警与最终反馈实算基线。
def create_baseline(call):
    since, until = _window(call.params['window'])
    space_id, store = _space(call), v13_store_for(call)
    rule_id = call.params.get('rule_id')
    if rule_id is not None:
        rule = _record(call, store.get('monitor_rules', rule_id))
        if rule['space_id'] != space_id:
            raise BusinessError('SPACE_SCOPE_MISMATCH')

    # LLM: 计算与回执在第一次事务内完成，重放不受随后新增反馈影响。
    # 函数用途: 保存实算数值与准确窗口。
    def mutate(records):
        alerts, feedback, _excluded = _soc_facts(call)
        selected = {key: row for key, row in alerts.items() if row['space_id'] == space_id
            and (rule_id is None or row['rule_id'] == rule_id)
            and since <= _epoch(row['created_at']) < until}
        if call.params['metric'] == 'alerts_per_day':
            value = len(selected) / ((until - since) / 86400)
        else:
            latest = _latest_feedback([row for row in feedback if row['alert_id'] in selected
                and since <= _epoch(row['created_at']) < until])
            verdicts = [row['override_verdict'] for row in latest.values()]
            positive = sum(value in {'false_positive', 'benign'} for value in verdicts)
            total = positive + verdicts.count('true_positive')
            if total == 0:
                raise BusinessError('STATE_CONFLICT', details={'reason': 'insufficient_feedback'})
            value = positive / total
        at, baseline_id = _now(call), _operation_id(call, 'baseline')
        row = {'baseline_id': baseline_id, 'owner_id': call.owner_id, 'space_id': space_id,
            'rule_id': rule_id, 'metric': call.params['metric'], 'value': value,
            'window': call.params['window'], 'revision': 0, 'created_at': at, 'updated_at': at}
        records[baseline_id] = row
        return {'baseline': _baseline_view(row)}

    with _lifecycle_lock(call):
        return _transaction(call, 'detection_baselines', mutate, space_ids=(space_id,))


# LLM: 字段和值必须来自 entities 的真实 type/value，不读取虚构 host 或 feedback.verdict 字段。
# 函数用途: 从最新误报反馈确定性重建建议，合并 sticky 终态和已提交排除项。
def _derived_tuning(call):
    store = v13_store_for(call)
    alerts, feedback, excluded = _soc_facts(call)
    rules = {row['rule_id']: row for row in store.list('monitor_rules')
             if row.get('owner_id') == call.owner_id and row.get('rule_id')}
    persisted = {row['suggestion_id']: row for row in store.list('detection_tuning')
                 if row.get('owner_id') == call.owner_id and row.get('suggestion_id')}
    groups = {}
    for alert_id, fact in _latest_feedback(feedback).items():
        if fact['override_verdict'] not in {'false_positive', 'benign'}:
            continue
        alert = alerts[alert_id]
        rule = rules.get(alert['rule_id'])
        if rule is None or rule['space_id'] != alert['space_id']:
            excluded += 1
            continue
        _record(call, rule)
        for entity in alert['entities']:
            if (type(entity) is not dict or set(entity) != {'type', 'value'} or type(entity['type']) is not str
                    or entity['type'] not in {'user', 'host', 'ip', 'domain', 'file_hash', 'process', 'url', 'email'}
                    or type(entity['value']) is not str or not 1 <= len(entity['value']) <= 512):
                excluded += 1
                continue
            key = (rule['rule_id'], entity['type'], entity['value'])
            groups.setdefault(key, set()).add(alert_id)
    rows = {key: row for key, row in persisted.items() if row['status'] in {'applied', 'dismissed'}}
    for (rule_id, field, value), ids in sorted(groups.items()):
        suggestion_id = 'tuning-' + hashlib.sha256((rule_id + '|' + field + '|' + value).encode()).hexdigest()
        previous, rule = persisted.get(suggestion_id), rules[rule_id]
        status = previous['status'] if previous is not None else 'open'
        if status == 'dismissed':
            rows[suggestion_id] = previous
            continue
        if any(item['exclusion_id'] == 'exclusion-' + _digest(suggestion_id)[:48]
               for item in rule['exclusions']) and status != 'dismissed':
            status = 'applied'
        at = _now(call)
        row = {'suggestion_id': suggestion_id, 'owner_id': call.owner_id,
            'space_id': rule['space_id'], 'rule_id': rule_id, 'kind': 'exclusion', 'field': field,
            'value': value, 'evidence_alert_ids': sorted(ids)[:100], 'false_positive_count': len(ids),
            'status': status, 'reason': previous['reason'] if previous is not None else None,
            'revision': previous['revision'] if previous is not None else 0,
            'created_at': previous['created_at'] if previous is not None else at,
            'updated_at': previous['updated_at'] if previous is not None else at}
        rows[suggestion_id] = row
        if len(ids) > 100:
            excluded += len(ids) - 100
    return rows, 'partial' if excluded else 'complete'


# LLM: dry_run 不调用此函数；普通派生读只在实际记录变化时物化，重复读不刷审计或 revision。
# 函数用途: 分批提交建议变化并保留 sticky 终态。
def _refresh_tuning(call):
    rows, coverage = _derived_tuning(call)
    store = v13_store_for(call)
    existing = {row['suggestion_id']: row for row in store.list('detection_tuning')
                if row.get('owner_id') == call.owner_id}
    changed = sorted(key for key in rows.keys() | existing.keys() if rows.get(key) != existing.get(key))
    for offset in range(0, len(changed), 100):
        keys = changed[offset:offset + 100]

        # LLM: 锁内末核终态，不能用重建的旧 open 建议覆盖 dismissed。
        # 函数用途: 物化一批有界建议或移除已失效的 open 项。
        def mutate(records):
            for key in keys:
                current, proposed = records.get(key), rows.get(key)
                if current is not None and current['status'] == 'dismissed':
                    continue
                if proposed is None:
                    if current is not None and current['status'] == 'open':
                        del records[key]
                    continue
                updated = {**proposed, 'revision': 0 if current is None else current['revision'] + 1,
                           'updated_at': _now(call)}
                records[key] = updated
            return {'updated': len(keys)}

        identity = _digest([keys, [rows.get(key) for key in keys], [existing.get(key) for key in keys]])
        _guarded_store(call, records=tuple(('detection_tuning', key, False) for key in keys),
            space_ids=tuple(rows[key]['space_id'] for key in keys if key in rows)).transaction('detection_tuning', mutate,
            idempotency_key='derive-' + identity[:48], params_digest=identity)
    return coverage


# LLM: 物化属于契约明确的派生读；筛选与游标仍绑定当前 owner 和 revision。
# 函数用途: 列出真实反馈驱动的调优建议。
def list_tuning(call):
    filters = _filters(call, ('space_id', 'rule_id', 'status'))
    with _lifecycle_lock(call):
        coverage = _refresh_tuning(call)
        result = _page(call, 'detection_tuning', filters, _suggestion_view)
        if coverage == 'partial':
            result['coverage'] = coverage
        return result


# LLM: 没有回测证据时不编造预计降噪百分比。
# 函数用途: 汇总建议终态和有待调优的规则数。
def tuning_summary(call):
    filters = _filters(call, ('space_id',))
    with _lifecycle_lock(call):
        coverage = _refresh_tuning(call)
        rows = [row for row in v13_store_for(call).list('detection_tuning')
                if all(row.get(key) == value for key, value in filters.items())]
        for row in rows:
            _record(call, row)
        return {'rules_with_suggestions': len({row['rule_id'] for row in rows if row['status'] == 'open'}),
            'open': sum(row['status'] == 'open' for row in rows),
            'applied': sum(row['status'] == 'applied' for row in rows),
            'dismissed': sum(row['status'] == 'dismissed' for row in rows),
            'estimated_alert_reduction': None, 'coverage': coverage}


# LLM: 同一建议始终对应同一排除项，避免重放或多个实体重复插入。
# 函数用途: 将真实建议转换为内部排除定义。
def _exclusion(row, source):
    if row['kind'] != 'exclusion' or row['field'] is None or row['value'] is None:
        raise BusinessError('STATE_CONFLICT')
    return {'exclusion_id': 'exclusion-' + _digest(row['suggestion_id'])[:48],
            'field': row['field'], 'value': row['value'], 'reason': row['reason'] or '', 'source': source}


# LLM: 应用仍使用监控模块的校验、原生计划提示词重绘和记录 CAS。
# 函数用途: 用一个多集合提交原子应用单条或全部 open 排除建议。
def _apply_suggestions(call, *, automatic):
    from .v13_w7_monitors import update_rule
    store = _guarded_store(call)

    # LLM: 准备回执冻结第一次选择与排除列表；重放不能重新扫描扩展批次。
    # 函数用途: 验证规则版本并准备有界调优输入。
    def prepare(records):
        rule = _record(call, store.get('monitor_rules', call.params['rule_id']))
        require_revision(call.expected_revision, rule['revision'])
        derived, _coverage = _derived_tuning(call)
        if automatic:
            selected = [row for row in derived.values() if row['rule_id'] == rule['rule_id']
                        and row['status'] == 'open' and row['kind'] == 'exclusion']
        else:
            row = derived.get(call.params['suggestion_id'])
            if row is None or row['rule_id'] != rule['rule_id']:
                raise BusinessError('RESOURCE_NOT_FOUND')
            selected = [row]
        selected = sorted(selected, key=lambda row: row['suggestion_id'])
        if len(selected) > 200:
            raise BusinessError('LIMIT_EXCEEDED')
        exclusions = list(rule['exclusions'])
        for row in selected:
            _record(call, row)
            current = records.get(row['suggestion_id'], row)
            if current['status'] != 'open':
                raise BusinessError('STATE_CONFLICT')
            exclusion = _exclusion(row, 'auto_tune' if automatic else 'tuning')
            if not any(item['exclusion_id'] == exclusion['exclusion_id'] for item in exclusions):
                exclusions.append(exclusion)
        if len(exclusions) > 200:
            raise BusinessError('LIMIT_EXCEEDED')
        return {'selected': selected, 'exclusions': exclusions}

    # LLM: 受信回调只组合原生 mutate；不在多集合事务内调用第二次 Store 写口。
    # 函数用途: 同一日志提交点写入规则和全部建议状态。
    def commit(native_mutate, extra):
        # LLM: 基于锁内最新建议核验 sticky dismissed，随后调用原监控规则变更逻辑。
        # 函数用途: 原子执行两集合领域变更并生成公开结果。
        def mutate(tables):
            rows = tables['detection_tuning']
            for selected in prepared['selected']:
                current = rows.get(selected['suggestion_id'])
                if current is not None and _record(call, current)['status'] != 'open':
                    raise BusinessError('STATE_CONFLICT')
            result = native_mutate(tables['monitor_rules'])
            applied = []
            for selected in prepared['selected']:
                previous = rows.get(selected['suggestion_id'])
                row = {**selected, 'status': 'applied', 'updated_at': _now(call),
                       'revision': (previous or selected)['revision'] + 1}
                rows[row['suggestion_id']] = row
                applied.append(_suggestion_view(row))
            if automatic:
                return {'applied': applied, 'dry_run': False, 'rule': result['rule']}
            return {'suggestion': applied[0], 'rule': result['rule']}

        return store.transaction_many(('monitor_rules', 'detection_tuning'), mutate,
            idempotency_key=call.idempotency_key,
            params_digest=_digest([_fingerprint(call), prepared, extra]))

    with _lifecycle_lock(call):
        prepared = _transaction(call, 'detection_tuning', prepare, step='tuning-prepare')
        native = replace(call, params={'rule_id': call.params['rule_id']})
        return update_rule(native, exclusions=prepared['exclusions'], commit=commit)


# LLM: 期望版本属于规则，不属于建议或整个集合。
# 函数用途: 应用一条反馈驱动的调优建议。
def apply_tuning(call):
    return _apply_suggestions(call, automatic=False)


# LLM: dismissed 是 sticky 状态，后续反馈重建不能恢复为 open。
# 函数用途: 幂等忽略单条建议并保存原因。
def dismiss_tuning(call):
    with _lifecycle_lock(call):
        derived, _coverage = _derived_tuning(call)

        # LLM: 最新持久终态优先于派生快照；已应用建议不能被忽略。
        # 函数用途: 保存建议的 dismissed 终态。
        def mutate(records):
            row = records.get(call.params['suggestion_id'], derived.get(call.params['suggestion_id']))
            row = _record(call, row)
            if row['rule_id'] != call.params['rule_id']:
                raise BusinessError('RESOURCE_NOT_FOUND')
            if row['status'] == 'applied':
                raise BusinessError('STATE_CONFLICT')
            if row['status'] != 'dismissed':
                row = {**row, 'status': 'dismissed', 'reason': call.params.get('reason', ''),
                       'revision': row['revision'] + 1, 'updated_at': _now(call)}
                records[row['suggestion_id']] = row
            return {'suggestion': _suggestion_view(row)}

        return _transaction(call, 'detection_tuning', mutate)


# LLM: dry_run 不取会创建锁文件的编排锁，不物化建议、不保存回执、不调用原生更新。
# 函数用途: 预览或原子应用规则全部 open 排除建议。
def auto_tune(call):
    if call.params.get('dry_run', False):
        rule = _record(call, v13_store_for(call).get('monitor_rules', call.params['rule_id']))
        require_revision(call.expected_revision, rule['revision'])
        derived, _coverage = _derived_tuning(call)
        selected = sorted((row for row in derived.values() if row['rule_id'] == rule['rule_id']
            and row['status'] == 'open' and row['kind'] == 'exclusion'), key=lambda row: row['suggestion_id'])
        if len(selected) > 200:
            raise BusinessError('LIMIT_EXCEEDED')
        return {'applied': [_suggestion_view(row) for row in selected], 'dry_run': True, 'rule': None}
    return _apply_suggestions(call, automatic=True)


# LLM: provider 由 operator 绑定；元数据只取已声明字段，额外私有内容不返回。
# 函数用途: 校验社区条目的闭合元数据。
def _community_view(raw):
    if type(raw) is not dict:
        raise HostAPIError('HOST_NOT_READY', 503)
    metadata = raw.get('item', raw.get('metadata', raw))
    if type(metadata) is not dict:
        raise HostAPIError('HOST_NOT_READY', 503)
    try:
        item = {key: metadata.get(key) for key in
            ('item_id', 'kind', 'title', 'summary', 'author', 'tags', 'format', 'version', 'updated_at')}
        item = validate_object(item, _api().v13_schema({'$ref': '#/$defs/V13CommunityItem'}))
        if item['kind'] != 'detection' or item['format'] != 'sigma':
            raise ValueError('community_kind')
        return item
    except (HostAPIError, ValueError, TypeError, KeyError) as error:
        raise HostAPIError('HOST_NOT_READY', 503) from error


# LLM: 网络访问只发生在宿主提供者中；来源错误不能伪装成空目录。
# 函数用途: 读取并校验真实社区分页。
def list_community(call):
    provider = _api().v13_provider(call, 'community_catalog')
    try:
        result = provider.list('detection', query=call.params.get('query', ''),
            tag=call.params.get('tag', ''), cursor=call.params.get('cursor'), limit=call.params.get('limit', 50))
        items = [_community_view(row) for row in result['items']]
        data = {'items': items, 'next_cursor': result['next_cursor'],
                'coverage': 'partial' if result['next_cursor'] is not None else 'complete'}
        return validate_object(data, _api().v13_schema({'type': 'object', 'properties': {
            'items': {'type': 'array', 'maxItems': call.params.get('limit', 50),
                      'items': {'$ref': '#/$defs/V13CommunityItem'}},
            'next_cursor': {'anyOf': [{'$ref': '#/$defs/Cursor'}, {'type': 'null'}]},
            'coverage': {'$ref': '#/$defs/Coverage'}},
            'required': ['items', 'next_cursor', 'coverage'], 'additionalProperties': False}))
    except HostAPIError as error:
        if error.code in {'HOST_NOT_READY', 'CURSOR_EXPIRED'}:
            raise
        raise HostAPIError('HOST_NOT_READY', 503) from error
    except (OSError, TypeError, KeyError, RuntimeError) as error:
        raise HostAPIError('HOST_NOT_READY', 503) from error


# LLM: Sigma 正文作为不透明查询保存，不在 handler 执行或用不完整 YAML 解析器改写内容。
# 函数用途: 获取目录元数据与可安装的规则草案。
def _community_document(call):
    provider = _api().v13_provider(call, 'community_catalog')
    try:
        raw = provider.get('detection', call.params['detection_id'])
        if raw is None:
            raise BusinessError('RESOURCE_NOT_FOUND')
        item = _community_view(raw)
        if item['item_id'] != call.params['detection_id']:
            raise HostAPIError('HOST_NOT_READY', 503)
        text = raw.get('document', raw.get('sigma'))
        if type(text) is not str or not text.strip():
            raise HostAPIError('HOST_NOT_READY', 503)
        metadata = raw.get('metadata', raw.get('item', raw))
        draft = {'name': item['title'], 'severity': metadata.get('severity', 'medium'),
                 'query': {'language': 'sigma', 'text': text}}
        draft = validate_object(draft, _api().v13_schema({'$ref': '#/$defs/V13RuleDraft'}))
        return {'item': item, 'rule': draft}
    except HostAPIError as error:
        if error.code in {'HOST_NOT_READY', 'RESOURCE_NOT_FOUND'}:
            raise
        raise HostAPIError('HOST_NOT_READY', 503) from error
    except (OSError, TypeError, KeyError, RuntimeError) as error:
        raise HostAPIError('HOST_NOT_READY', 503) from error


# LLM: 缺社区记录保持 404，未绑定 provider 保持 503。
# 函数用途: 读取一条社区规则与 Sigma 草案。
def get_community(call):
    return _community_document(call)


# LLM: 文档快照和子幂等键固定第一次安装，提供者后续变更不会导致同键重复安装。
# 函数用途: 经原生规则创建路径安装默认禁用的社区规则。
def install_community(call):
    from .v13_w7_monitors import create_rule
    space_id = _space(call)

    # LLM: 提供者读取在首次回调内，回执重放不再访问可能变更或移除的条目。
    # 函数用途: 保存本次安装使用的完整规则参数。
    def prepare(records):
        document = _community_document(call)
        return {'params': {**document['rule'], 'space_id': space_id,
                           'enabled': call.params.get('enabled', False),
                           'tags': document['item']['tags']}}

    with _lifecycle_lock(call):
        prepared = _transaction(call, 'monitor_rules', prepare, step='community-document', space_ids=(space_id,))
        return create_rule(replace(call, params=prepared['params'], expected_revision=None,
            idempotency_key=_step_key(call, 'community-native')), origin='community')
