"""W11 会话与 Copilot 动作：owner 会话集合、上下文事实和原生线程派发。

会话记录只保存会话元数据；消息、任务和运行事件仍由冻结原生端口持有，
本模块不新建执行器、不保存第二份消息副本，也不替代 W7 的脚本运行入口。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

from ..common.log_redaction import redact_sensitive_value
from ..conversation.business_access import BusinessError
from ..gateway_parts.business_api_v13 import (
    v13_call_frozen,
    v13_require_access,
    v13_resolve_space,
)
from ..plugin_host.contracts import DomainResult
from ..plugin_host.errors import HostAPIError
from ..plugin_host.schemas import thaw
from .v13_store import _page_rows, v13_store_for

# 上下文类型只映射到拥有该集合的分片，读数仍按当前 owner 复核。
_CONTEXT_COLLECTIONS = {'alert': 'soc_alerts', 'case': 'soc_cases', 'rule': 'monitor_rules'}
_CONTEXT_LABELS = {'alert': '告警', 'case': '案件', 'rule': '检测规则'}
# AI 快捷动作目录是文档化静态清单；脚本动作只出现在目录里，执行走 W7 scripts.run。
_AI_ACTIONS = (
    ('explain', '单步研判解释', '按一条记录的真实事实给出单步研判解释'),
    ('summarize', '事实摘要', '把一条记录的关键事实压缩成摘要'),
    ('next_steps', '建议下一步', '给出可执行的后续处置步骤建议'),
    ('related_alerts', '关联告警梳理', '梳理同空间内与该记录相关的告警'),
    ('draft_report', '草稿报告', '按记录事实起草一份研判报告草稿'),
)
_CONVERSATION_FIELDS = ('conversation_id', 'thread_id', 'agent_id', 'space_id', 'title', 'archived',
    'context_ref', 'last_message_at', 'revision', 'created_at', 'updated_at')


# LLM: 幂等键与参数指纹只依赖严格 JSON，不从自然语言猜意图。
# 函数用途: 生成稳定的十六进制摘要。
def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


# LLM: 时间只来自注入时钟，统一输出可排序的 UTC 时间。
# 函数用途: 把当前调用时钟转成审计时间。
def _timestamp(clock):
    return datetime.fromtimestamp(clock(), timezone.utc).isoformat()


# LLM: 对外只给冻结契约声明的字段，owner_id 和内部标记不出现在会话视图里。
# 函数用途: 把会话记录投影成 V13Conversation。
def _view(row):
    if type(row) is not dict or any(key not in row for key in _CONVERSATION_FIELDS):
        raise BusinessError('DOMAIN_RESULT_INVALID')
    return {key: row[key] for key in _CONVERSATION_FIELDS}


# LLM: 缺记录与跨owner一致404；真实会话记录必须进入原实时资源授权，不能只核输入ID。
# 函数用途: 读取当前 owner 的会话记录。
def _conversation(call, conversation_id):
    row = v13_store_for(call).get('conversations', conversation_id)
    if row is None or row.get('owner_id') != call.owner_id:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    v13_require_access(call, row)
    return row


# LLM: 上下文事实只取结构化字段，凭据命名值统一脱敏，不透传整条原始记录。
# 函数用途: 投影告警记录的研判事实。
def _alert_facts(record):
    entities = record.get('entities') or []
    return {'context_kind': 'alert', 'context_id': record['alert_id'], 'space_id': record['space_id'],
        'title': record['title'], 'severity': record['severity'], 'status': record['status'],
        'verdict': record.get('verdict'), 'ai_confidence': record.get('ai_confidence'),
        'rule_id': record.get('rule_id'), 'source': record.get('source'),
        'techniques': list(record.get('techniques') or ())[:20],
        'entities': [{'type': item.get('type'), 'value': item.get('value')} for item in entities[:20]],
        'tags': list(record.get('tags') or ())[:20],
        'evidence_refs': list(record.get('evidence_refs') or ())[:20]}


# LLM: 案件事实只带状态与关联引用，不展开其它告警正文。
# 函数用途: 投影案件记录的研判事实。
def _case_facts(record):
    alert_ids = list(record.get('alert_ids') or ())
    return {'context_kind': 'case', 'context_id': record['case_id'], 'space_id': record['space_id'],
        'title': record['title'], 'severity': record['severity'], 'status': record['status'],
        'assignee': record.get('assignee'), 'alert_count': len(alert_ids), 'alert_ids': alert_ids[:50],
        'tags': list(record.get('tags') or ())[:20]}


# LLM: 规则事实只读查询语言与覆盖技术，不执行规则或脚本。
# 函数用途: 投影检测规则记录的研判事实。
def _rule_facts(record):
    query = record.get('query') or {}
    return {'context_kind': 'rule', 'context_id': record['rule_id'], 'space_id': record['space_id'],
        'name': record['name'], 'severity': record['severity'], 'enabled': record['enabled'],
        'query_language': query.get('language'), 'query_text': query.get('text'),
        'mitre_techniques': list(record.get('mitre_techniques') or ())[:50],
        'tags': list(record.get('tags') or ())[:20], 'script_id': record.get('script_id')}


_FACT_PROJECTIONS = {'alert': _alert_facts, 'case': _case_facts, 'rule': _rule_facts}


# LLM: 上下文必须命中本owner真实记录并按该记录空间实时授权；随后只投影脱敏事实。
# 函数用途: 读取并脱敏上下文事实。
def _context_facts(call, context_ref):
    kind = context_ref['kind']
    collection = _CONTEXT_COLLECTIONS[kind]
    record = v13_store_for(call).get(collection, context_ref['id'])
    if record is None or record.get('owner_id') != call.owner_id:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    v13_require_access(call, record)
    return redact_sensitive_value(_FACT_PROJECTIONS[kind](record))


# LLM: 事务准入和最后提交都复查真实资源与上下文空间；原Store仍负责quota/锁/审计。
# 函数用途: 为会话写操作补齐记录级实时末核。
def _record_store(call, record):
    store = v13_store_for(call)

    # LLM: 每次重新读取上下文，撤权或空间迁移不能复用旧授权快照。
    # 函数用途: 核对本次会话记录及其引用上下文。
    def authorize():
        v13_require_access(call, record)
        reference = call.params.get('context_ref')
        if reference is not None:
            facts = _context_facts(call, reference)
            if facts['space_id'] != record['space_id']:
                raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        return True

    store.authorize = authorize
    return store


# LLM: 列表只隐藏确实被原授权器拒绝的记录，来源故障或异常绝不吞为空成功。
# 函数用途: 判断单条会话是否对本次主体可见。
def _visible(call, record):
    try:
        v13_require_access(call, record)
    except (HostAPIError, BusinessError) as error:
        if error.code in ('CAPABILITY_DENIED', 'BUSINESS_ACCESS_DENIED', 'RESOURCE_NOT_FOUND'):
            return False
        raise
    return True


# LLM: 指令只由结构化事实与调用方显式补充文本组成，长度受冻结端口上限约束。
# 函数用途: 生成给原生任务派发的单步指令文本。
def _instruction(label, facts, extra):
    parts = ['以下为已核验的' + label + '事实（JSON）：',
        json.dumps(facts, ensure_ascii=False, sort_keys=True)]
    if extra:
        parts.append('补充说明：' + extra)
    return '\n'.join(parts)[:32768]


# LLM: 会话标题来自显式参数或上下文类型，不解析用户自然语言。
# 函数用途: 生成不超过契约上限的会话标题。
def _title(title, label, context_ref):
    if title:
        return title[:200]
    if context_ref is not None:
        return (label + '会话 ' + context_ref['id'])[:200]
    return 'Copilot 会话'


# LLM: 独立thread由冻结后台身份创建；完整参数先由Store准入，再发生第一个原生副作用。
# 函数用途: 在会话集合事务内创建原生后台线程并写入会话记录。
def _create(call, *, space_id, title, context_ref, idempotency_key, params_digest):
    store = _record_store(call, {'owner_id': call.owner_id, 'space_id': space_id})
    name = ('copilot ' + title)[:120]

    # LLM: 事务内只创建一次；同键重放由 Store 回执与冻结创建回执共同保证。
    # 函数用途: 绑定原生后台身份并返回会话视图。
    def mutate(records):
        store.authorize()
        created = v13_call_frozen(call, 'background_agents.create',
            {'space_id': space_id, 'name': name, 'purpose': 'copilot 会话上下文', 'after_task': 'reuse'},
            idempotency_key=idempotency_key)
        agent = created['agent']
        at = _timestamp(call.clock)
        row = {'conversation_id': agent['thread_id'], 'thread_id': agent['thread_id'],
            'agent_id': agent['agent_id'], 'space_id': space_id, 'title': title, 'archived': False,
            'context_ref': context_ref, 'last_message_at': at, 'revision': 0, 'created_at': at,
            'updated_at': at, 'owner_id': call.owner_id}
        records[agent['thread_id']] = row
        return _view(row)

    return store.transaction('conversations', mutate, idempotency_key=idempotency_key,
        params_digest=params_digest)


# LLM: 已有会话、输入空间和真实上下文必须同空间；隐式建会话的阶段键不能随参数变化。
# 函数用途: 决定本轮解释使用的会话与空间。
def _conversation_for(call, *, context_ref, context_space, title, label):
    selected = call.params.get('space_id')
    if call.params.get('conversation_id'):
        row = _conversation(call, call.params['conversation_id'])
        if row['space_id'] != context_space or (selected is not None and row['space_id'] != selected):
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        return row['space_id'], row
    if selected is not None and selected != context_space:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    space = v13_resolve_space(call, selected or context_space)
    key = 'conversation-' + _digest([call.action, call.idempotency_key])[:48]
    row = _create(call, space_id=space['space_id'], title=_title(title, label, context_ref),
        context_ref=context_ref, idempotency_key=key,
        params_digest=_digest([call.action, call.params]))
    if row['space_id'] != context_space:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    return space['space_id'], row


# LLM: 原生assign的200耐久回执明确receipt_state=accepted才算异步受理；真正202保留operation_id。
# 函数用途: 规范化已校验冻结派发结果，拒绝把任意dict升格成功。
def _accepted_receipt(result):
    if isinstance(result, DomainResult):
        data = thaw(result.data)
        if (result.http_status != 202 or data.get('state') != 'accepted'
                or not data.get('operation_id') or not data.get('task_id')):
            raise HostAPIError('DOMAIN_RESULT_INVALID', 500, outcome='unknown')
        return data['task_id'], data['operation_id']
    if (type(result) is not dict or set(result) != {'assignment_ref', 'task_id', 'receipt_state'}
            or result.get('receipt_state') != 'accepted'
            or not result.get('assignment_ref') or not result.get('task_id')):
        raise HostAPIError('DOMAIN_RESULT_INVALID', 500, outcome='unknown')
    return result['task_id'], result['assignment_ref']


# LLM: 派发前与提交末核均复核记录；只消费原生耐久受理回执，同键不再派发。
# 函数用途: 向会话线程派发一次单步任务并返回受理数据。
def _dispatch(call, *, space_id, conversation, instruction):
    store = _record_store(call, conversation)
    observed = conversation['revision']

    # LLM: 会话行只更新最后消息时间与修订，不改写线程或空间归属。
    # 函数用途: 在会话事务内派发并记录最后消息时间。
    def mutate(records):
        row = records.get(conversation['conversation_id'])
        if row is None or row.get('owner_id') != call.owner_id:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        if row['space_id'] != space_id:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        v13_require_access(call, row)
        store.authorize()
        accepted = v13_call_frozen(call, 'tasks.assign', {'agent_id': row['thread_id'],
            'space_id': space_id, 'instruction': instruction}, idempotency_key=call.idempotency_key)
        task_id, operation_id = _accepted_receipt(accepted)
        at = _timestamp(call.clock)
        if row['revision'] == observed:
            row.update(last_message_at=at, revision=row['revision'] + 1, updated_at=at)
        return {'state': 'accepted', 'operation_id': operation_id,
            'conversation_id': row['conversation_id'], 'task_id': task_id, 'thread_id': row['thread_id']}

    return DomainResult(202, store.transaction('conversations', mutate,
        idempotency_key=call.idempotency_key, params_digest=_digest([call.action, call.params])))


# LLM: 脚本动作只来自声明了该上下文类型的元数据，未声明即不出现。
# LLM: 目录不执行脚本，也不读取脚本代码。
# 函数用途: 列出适用于当前上下文的固化脚本动作。
def _script_actions(call, context_kind):
    from ..assets.script_store import ScriptStore

    try:
        records = ScriptStore(call.owner_home).list_scripts()
    except (OSError, ValueError) as error:
        raise HostAPIError('HOST_NOT_READY', 503) from error
    items = []
    for record in records:
        metadata = record.get('metadata') or {}
        contexts = metadata.get('contexts')
        if not isinstance(contexts, (list, tuple)) or context_kind not in contexts:
            continue
        if record.get('enabled') is False:
            continue
        slug = re.sub(r'[^a-z0-9_]+', '_', str(record['id']).lower()).strip('_')[:24] or 'script'
        items.append({'action': 'script_' + slug, 'title': str(record.get('name') or record['id'])[:120],
            'description': str(record.get('summary') or '')[:500], 'kind': 'script',
            'script_id': record['id']})
    return items


# LLM: 目录先给静态 AI 动作，再给声明匹配的脚本；同一上下文顺序稳定。
# 函数用途: 组装指定上下文类型的快捷动作目录。
def _catalog(call, context_kind):
    items = [{'action': name, 'title': title, 'description': description, 'kind': 'ai', 'script_id': None}
        for name, title, description in _AI_ACTIONS]
    items.extend(_script_actions(call, context_kind))
    return sorted(items, key=lambda item: item['action'])


# LLM: query与逐行授权在分页之前执行，游标绑定主体、完整筛选和可见行快照。
# 函数用途: 分页返回当前 owner 的会话列表。
def list_conversations(call):
    params = call.params
    filters = {'owner_id': call.owner_id, 'archived': params.get('archived', False)}
    if params.get('space_id'):
        filters['space_id'] = params['space_id']
    needle = params.get('query', '').casefold()
    rows = [row for row in v13_store_for(call).list('conversations')
            if all(row.get(key) == value for key, value in filters.items())
            and needle in str(row.get('title', '')).casefold() and _visible(call, row)]
    page = _page_rows(rows, [str(call.owner_home), call.owner_id, call.subject_id,
        call.action, filters, needle, rows], cursor=params.get('cursor'), limit=params.get('limit', 50))
    return {'items': [_view(row) for row in page['items']], 'next_cursor': page['next_cursor'],
        'coverage': page['coverage']}


# LLM: 新建会话经冻结后台身份完成；若给了上下文引用先校验同空间真实权限。
# 函数用途: 新建一个独立 thread_id 的会话。
def create_conversation(call):
    params = call.params
    space = v13_resolve_space(call, params['space_id'])
    if params.get('context_ref') is not None:
        facts = _context_facts(call, params['context_ref'])
        if facts['space_id'] != space['space_id']:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    title = _title(params.get('title'), 'Copilot', params.get('context_ref'))
    row = _create(call, space_id=space['space_id'], title=title, context_ref=params.get('context_ref'),
        idempotency_key=call.idempotency_key, params_digest=_digest([call.action, call.params]))
    return {'conversation': row}


# LLM: 详情恢复读原生消息页，不复制或重排消息正文。
# 函数用途: 返回会话元数据与一页原生消息。
def get_conversation(call):
    params = call.params
    row = _conversation(call, params['conversation_id'])
    request = {'target': {'kind': 'background_main', 'id': row['thread_id']},
        'limit': params.get('limit', 50)}
    if params.get('cursor'):
        request['cursor'] = params['cursor']
    page = v13_call_frozen(call, 'messages.list', request)
    return {'conversation': _view(row), 'messages': page['items'], 'next_cursor': page['next_cursor'],
        'coverage': page['coverage']}


# LLM: 归档只改会话元数据；真实记录参与准入和提交末核，记录级CAS防并发覆盖。
# 函数用途: 归档或恢复一个会话。
def archive_conversation(call):
    if call.expected_revision is None:
        raise HostAPIError('INVALID_SCHEMA')
    observed = _conversation(call, call.params['conversation_id'])
    archived = call.params.get('archived', True)

    # LLM: 修订在锁内比对最新记录，过期请求不覆盖他人已提交的修改。
    # 函数用途: 在会话事务内校验修订并更新归档态。
    def mutate(records):
        row = records.get(call.params['conversation_id'])
        if row is None or row.get('owner_id') != call.owner_id:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        v13_require_access(call, row)
        if row['revision'] != call.expected_revision:
            raise BusinessError('REVISION_CONFLICT')
        at = _timestamp(call.clock)
        row.update(archived=archived, revision=row['revision'] + 1, updated_at=at)
        return {'conversation': _view(row)}

    return _record_store(call, observed).transaction('conversations', mutate, idempotency_key=call.idempotency_key,
        params_digest=_digest([call.action, call.params, call.expected_revision]))


# LLM: 解释只读已授权上下文事实，默认空间取真实记录，客户端经既有stream订阅进度。
# 函数用途: 对一条告警、案件或规则发起单步流式解释。
def explain(call):
    params = call.params
    facts = _context_facts(call, params['context_ref'])
    label = _CONTEXT_LABELS[params['context_ref']['kind']]
    space_id, conversation = _conversation_for(call, context_ref=params['context_ref'],
        context_space=facts['space_id'], title=None, label=label)
    return _dispatch(call, space_id=space_id, conversation=conversation,
        instruction=_instruction(label, facts, params.get('question')))


# LLM: 快捷动作必须是目录内AI动作，真实上下文与目标同空间；脚本只走W7运行入口。
# 函数用途: 按上下文一键派发 AI 辅助调查任务。
def contextual_action(call):
    params = call.params
    selected = next((item for item in _catalog(call, params['context_ref']['kind'])
        if item['action'] == params['action']), None)
    if selected is None or selected['kind'] != 'ai':
        raise HostAPIError('INVALID_SCHEMA')
    facts = _context_facts(call, params['context_ref'])
    label = _CONTEXT_LABELS[params['context_ref']['kind']]
    space_id, conversation = _conversation_for(call, context_ref=params['context_ref'],
        context_space=facts['space_id'], title=selected['title'], label=label)
    instruction = _instruction(label, {'action_intent': selected['title'], **facts},
        params.get('instruction'))
    return _dispatch(call, space_id=space_id, conversation=conversation, instruction=instruction)


# LLM: 目录是只读投影，不执行脚本也不签发运行权限。
# LLM: 脚本项只声明 kind=script 与 script_id，运行仍由 W7 复核。
# 函数用途: 返回指定上下文类型的快捷动作目录。
def contextual_actions(call):
    if call.params.get('space_id'):
        v13_resolve_space(call, call.params['space_id'])
    return {'items': _catalog(call, call.params['context_kind'])}
