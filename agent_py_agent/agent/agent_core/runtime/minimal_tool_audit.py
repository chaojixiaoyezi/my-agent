# LLM: 最小审计复用owner原raw事件库，只投影结构事实，不存参数、工具正文、表格或秘密，不替代operation权威。
# 模块用途: save=False时仍保留后台工具身份、动作、操作编号与供应商结果，聊天和长期记忆继续不保存。
from __future__ import annotations

import hashlib
import json
import logging
import re

from ...contracts.error_taxonomy import error_contract
from ...memory_archive.models import RawMemoryEvent
from ...memory_archive.storage import append_raw_event
from ...user_space.runtime_paths import runtime_owner_root

_SAFE_ID = re.compile(r'[A-Za-z0-9_.:/@-]{1,256}')


# LLM: 不自由字符串化对象或复制自然语言，编号只接已有结构化宿主字段，数字保留精确类型。
# 函数用途: 为最小审计限制ID形状，避免异常正文或长载荷意外进入事件。
def _identifier(value):
    return value if isinstance(value, str) and _SAFE_ID.fullmatch(value) else ''


# LLM: 动作只取原注册工具schema声明的枚举值；不将开放文本参数转为审计动作或执行依据。
# 函数用途: 从实际调用中提取可安全保存的动作名，不序列化其它参数。
def _declared_action(agent, record):
    tool = getattr(getattr(agent, 'tools', None), 'tools', {}).get(record.get('tool'))
    spec = getattr(tool, 'model_spec', None)
    properties = getattr(spec, 'input_schema', {}).get('properties', {})
    parameters = record.get('parameters')
    parameters = parameters if isinstance(parameters, dict) else {}
    for name in ('action', 'operation'):
        value = parameters.get(name)
        declared = properties.get(name, {}).get('enum', [])
        if isinstance(value, str) and value in declared:
            return _identifier(value)
    return ''


# LLM: 身份来自原runner/HostActor而非模型输出；HTTP/API码只取安全typed envelope，不解析output正文。
# 函数用途: 构造不含表格值、密码、认证头或模型文案的最小事件事实。
def _audit_facts(agent, params, record):
    attributes = getattr(params, 'task_attributes', None) or {}
    current = getattr(agent, '_current_run_params', None)
    same_run = current is not None and getattr(current, 'run_id', '') == getattr(params, 'run_id', '')
    actor = getattr(params, 'host_channel_actor', None) or (getattr(current, 'host_channel_actor', None) if same_run else None)
    paths = getattr(agent, 'home_paths', None)
    facts = {'schema_version':'tool_execution_audit.v1', 'action':_declared_action(agent, record),
        'owner_kind':_identifier(getattr(paths, 'owner_kind', '')),
        'owner_id':_identifier(getattr(paths, 'owner_id', '')),
        'actor_id':_identifier(getattr(actor, 'actor_id', '')),
        'thread_id':_identifier(getattr(actor, 'source_thread_id', '') or attributes.get('conversation_thread_id')),
        'job_id':_identifier(attributes.get('scheduler_job_id') or attributes.get('job_id')),
        'rule_id':_identifier(attributes.get('scheduler_rule_id') or attributes.get('rule_id')),
        'failure_stage':_identifier(record.get('failure_stage')),
        'handler_executed':record.get('handler_executed') is True}
    envelope = record.get('tool_result_envelope')
    cloud = envelope.get('cloud_error') if isinstance(envelope, dict) else None
    operation = envelope.get('tool_operation') if isinstance(envelope, dict) else None
    if isinstance(operation, dict):
        facts['operation_status'] = _identifier(operation.get('status'))
        facts['operation_action'] = _identifier(operation.get('action'))
    facts.update(_scheduler_identity(agent, params))
    if isinstance(cloud, dict):
        for key in ('http_status', 'provider_code'):
            if type(cloud.get(key)) is int:
                facts[key] = cloud[key]
        for key in ('reason', 'request_stage'):
            facts[key] = _identifier(cloud.get(key))
    return facts


# LLM: 写原owner审计事件与save解耦；失败仅报告异常类型，不改变操作终态、清空UNKNOWN或触发重试。
# 函数用途: 尽力保存一次工具最小审计，并把原档案引用附回内存记录供宿主追踪。
def archive_minimal_tool_audit(agent, params, record):
    try:
        facts = _audit_facts(agent, params, record)
        identity = {key:_identifier(getattr(params, key, '')) for key in ('request_id', 'run_id', 'task_id')}
        call_id = _identifier(record.get('id') or record.get('call_id'))
        envelope = record.get('tool_result_envelope') or {}
        operation = envelope.get('tool_operation') or {}
        operation_id = _identifier(operation.get('operation_id') or record.get('operation_id'))
        event_id = hashlib.sha256(json.dumps([identity, call_id, operation_id, facts], sort_keys=True).encode()).hexdigest()
        event = RawMemoryEvent(event_id=event_id, session_id=_identifier(getattr(agent, 'session_id', '')),
            **identity, speaker='tool', target='host', action='tool_audit',
            status='succeeded' if record.get('ok') is True else 'failed',
            error_code=_audit_error_code(record),
            tool_name=_identifier(record.get('tool')), tool_call_id=call_id, tool_success=record.get('ok') is True,
            operation_id=operation_id, effect_outcome=_identifier(record.get('effect_outcome')),
            content_preview=json.dumps(facts, ensure_ascii=False, sort_keys=True), source='minimal_tool_audit')
        path = append_raw_event(runtime_owner_root(agent), event)
        record['raw_archive_event_id'], record['raw_archive_path'] = event_id, str(path)
    except Exception as exc:  # noqa: BLE001 - 审计失败不能冒称操作未发生或复制异常正文
        record['raw_archive_error_type'] = type(exc).__name__
        logging.getLogger(__name__).warning('minimal_tool_audit_failed error_type=%s', type(exc).__name__)


# LLM: job/rule优先核对当前owner原调度run；仅查准确task/run编号，不扫描跨owner任务或解析提示文本。
# 函数用途: 为最小审计补齐后台输入未携带的规则身份，读取失败不捏造编号或阻断工具。
def _scheduler_identity(agent, params):
    repository = getattr(agent, 'scheduler_repository', None)
    if repository is None:
        return {}
    task_id = _identifier(getattr(params, 'task_id', ''))
    try:
        run = repository.get_active_run(task_id)
    except (OSError, ValueError, RuntimeError, TypeError):
        return {}
    if not isinstance(run, dict) or run.get('run_id') != task_id:
        return {}
    return {'job_id':_identifier(run.get('job_id')), 'rule_id':_identifier(run.get('rule_id'))}


# LLM: 供应商/插件任意reported字符串可能含秘密，只保存原taxonomy登记码，未知值归固定UNKNOWN_ERROR。
# 函数用途: 保留可追踪错误类别，防止自由错误字段成为no-save审计的正文泄露入口。
def _audit_error_code(record):
    code = record.get('reported_error_code') or record.get('error_code')
    return error_contract(code).code if isinstance(code, str) and code else ''
