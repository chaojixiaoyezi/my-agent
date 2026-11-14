"""W7 检测与自动化动作的静态闭合契约；不增加 HTTP 旁路或模型授权。"""
from __future__ import annotations

import json

from ..business_spaces import (
    v13_w7_detection_lifecycle,
    v13_w7_monitors,
    v13_w7_playbooks,
    v13_w7_scripts,
    v13_w7_simulation_sessions,
)
from .business_api_v13_contracts import V13Action


# LLM: 就绪性只检查原生来源，不创建执行对象或把缓存结果当权限。
# 函数用途: 检查本地业务存储开关。
def _store_ready(call):
    return bool(getattr(call.services.host.config, 'business_spaces_enabled', False))


# LLM: 模型作业只经原后台端口与任务观察口，不构造第二个任务系统。
# 函数用途: 检查通用异步作业所需原生来源。
def _jobs_ready(call):
    return (_store_ready(call) and callable(getattr(call.services.background, 'execute', None))
            and call.services.ready('tasks.get'))


# LLM: 只接受结构化 JSON；后续由 W0 依据该 kind 的完整 schema 核验。
# 函数用途: 解析模型作业结果，不从自然语言猜执行状态。
def _finalize_json(text):
    result = json.loads(text)
    if type(result) is not dict:
        raise ValueError('模型结果必须是 JSON 对象')
    return result


# LLM: 签名遵循 W0 的 (call, job_row, result)，白名单只读服务端耐久快照。
# 函数用途: 在通用作业完成提交前拒绝跨挂载目录的草案引用。
def _validate_draft_result(call, job_row, result):
    from .business_api_v13 import v13_require_access
    v13_require_access(call, job_row)
    facts = job_row.get('context_facts')
    if type(facts) is not dict or any(type(facts.get(key)) is not list
            or any(type(item) is not str for item in facts[key])
            for key in ('skill_ids', 'script_ids', 'monitor_rule_ids')):
        raise ValueError('作业缺少可信目录快照')
    v13_w7_playbooks.validate_workflow_draft_references(result, facts)
    return True

V13_DEFS = {
    'V13DetectionBaseline': {'type': 'object', 'properties': {'baseline_id': {'$ref': '#/$defs/Id'}, 'space_id': {'$ref': '#/$defs/Id'}, 'rule_id': {'$ref': '#/$defs/NullableId'}, 'metric': {'type': 'string', 'enum': ['alerts_per_day', 'fp_ratio']}, 'value': {'type': 'number'}, 'window': {'$ref': '#/$defs/V13TimeWindow'}, 'created_at': {'$ref': '#/$defs/Timestamp'}}, 'required': ['baseline_id', 'space_id', 'rule_id', 'metric', 'value', 'window', 'created_at'], 'additionalProperties': False},
    'V13DetectionProposal': {'type': 'object', 'properties': {'proposal_id': {'$ref': '#/$defs/Id'}, 'space_id': {'$ref': '#/$defs/Id'}, 'title': {'type': 'string', 'minLength': 1, 'maxLength': 200}, 'rationale': {'type': 'string', 'minLength': 0, 'maxLength': 8000}, 'rule': {'$ref': '#/$defs/V13RuleDraft'}, 'mitre_techniques': {'type': 'array', 'items': {'type': 'string', 'minLength': 1, 'maxLength': 16}, 'maxItems': 100}, 'status': {'type': 'string', 'enum': ['draft', 'pending', 'evaluating', 'approved', 'rejected', 'changes_requested', 'promoted']}, 'author': {'type': 'string', 'minLength': 1, 'maxLength': 128}, 'source': {'type': 'string', 'enum': ['analyst', 'ai']}, 'comments': {'type': 'array', 'items': {'$ref': '#/$defs/V13ProposalComment'}, 'maxItems': 200}, 'evaluations': {'type': 'array', 'items': {'$ref': '#/$defs/V13ProposalEvaluation'}, 'maxItems': 50}, 'decision': {'anyOf': [{'$ref': '#/$defs/JsonObject'}, {'type': 'null'}]}, 'promoted_rule_id': {'$ref': '#/$defs/NullableId'}, 'revision': {'type': 'integer', 'minimum': 0}, 'created_at': {'$ref': '#/$defs/Timestamp'}, 'updated_at': {'$ref': '#/$defs/Timestamp'}}, 'required': ['proposal_id', 'space_id', 'title', 'rationale', 'rule', 'mitre_techniques', 'status', 'author', 'source', 'comments', 'evaluations', 'decision', 'promoted_rule_id', 'revision', 'created_at', 'updated_at'], 'additionalProperties': False},
    'V13DraftRefs': {'type': 'object', 'properties': {'skill_ids': {'type': 'array', 'items': {'$ref': '#/$defs/Id'}, 'maxItems': 100}, 'script_ids': {'type': 'array', 'items': {'$ref': '#/$defs/Id'}, 'maxItems': 100}, 'monitor_rule_ids': {'type': 'array', 'items': {'$ref': '#/$defs/Id'}, 'maxItems': 100}}, 'required': ['skill_ids', 'script_ids', 'monitor_rule_ids'], 'additionalProperties': False},
    'V13DriftSnapshot': {'type': 'object', 'properties': {'snapshot_id': {'$ref': '#/$defs/Id'}, 'space_id': {'$ref': '#/$defs/Id'}, 'rules_total': {'type': 'integer', 'minimum': 0}, 'rules_enabled': {'type': 'integer', 'minimum': 0}, 'techniques_covered': {'type': 'array', 'items': {'type': 'string', 'minLength': 1, 'maxLength': 16}, 'maxItems': 1000}, 'coverage_ratio': {'anyOf': [{'type': 'number'}, {'type': 'null'}]}, 'delta_since_previous': {'anyOf': [{'$ref': '#/$defs/JsonObject'}, {'type': 'null'}]}, 'created_at': {'$ref': '#/$defs/Timestamp'}}, 'required': ['snapshot_id', 'space_id', 'rules_total', 'rules_enabled', 'techniques_covered', 'coverage_ratio', 'delta_since_previous', 'created_at'], 'additionalProperties': False},
    'V13DryRunStep': {'type': 'object', 'properties': {'step_id': {'$ref': '#/$defs/Id'}, 'mode': {'type': 'string', 'minLength': 1, 'maxLength': 64}, 'would_run': {'type': 'boolean'}, 'side_effects': {'type': 'string', 'enum': ['none', 'external', 'unknown']}, 'blocked_reason': {'anyOf': [{'type': 'string', 'minLength': 1, 'maxLength': 256}, {'type': 'null'}]}}, 'required': ['step_id', 'mode', 'would_run', 'side_effects', 'blocked_reason'], 'additionalProperties': False},
    'V13FieldError': {'type': 'object', 'properties': {'path': {'type': 'string', 'minLength': 1, 'maxLength': 256}, 'code': {'type': 'string', 'minLength': 1, 'maxLength': 64}, 'message': {'type': 'string', 'minLength': 0, 'maxLength': 512}}, 'required': ['path', 'code', 'message'], 'additionalProperties': False},
    'V13LiveAction': {'type': 'object', 'properties': {'action_ref': {'$ref': '#/$defs/Id'}, 'capability': {'type': 'string', 'pattern': '^[a-z][a-z0-9_]{0,31}$'}, 'vendor_id': {'anyOf': [{'type': 'string', 'minLength': 1, 'maxLength': 128}, {'type': 'null'}]}, 'vendor_name': {'anyOf': [{'type': 'string', 'minLength': 1, 'maxLength': 128}, {'type': 'null'}]}, 'display_name': {'type': 'string', 'minLength': 1, 'maxLength': 200}, 'source': {'type': 'string', 'enum': ['script', 'plugin', 'mcp']}, 'source_ref': {'$ref': '#/$defs/Id'}, 'input_schema': {'$ref': '#/$defs/JsonObject'}, 'risk_level': {'type': 'string', 'enum': ['low', 'medium', 'high', 'critical']}, 'requires_approval': {'type': 'boolean'}, 'available': {'type': 'boolean'}, 'unavailable_reason': {'anyOf': [{'type': 'string', 'minLength': 1, 'maxLength': 256}, {'type': 'null'}]}}, 'required': ['action_ref', 'capability', 'vendor_id', 'vendor_name', 'display_name', 'source', 'source_ref', 'input_schema', 'risk_level', 'requires_approval', 'available', 'unavailable_reason'], 'additionalProperties': False},
    'V13MonitorRule': {'type': 'object', 'properties': {'rule_id': {'$ref': '#/$defs/Id'}, 'space_id': {'$ref': '#/$defs/Id'}, 'name': {'type': 'string', 'minLength': 1, 'maxLength': 200}, 'description': {'type': 'string', 'minLength': 0, 'maxLength': 4096}, 'enabled': {'type': 'boolean'}, 'severity': {'type': 'string', 'enum': ['info', 'low', 'medium', 'high', 'critical']}, 'query': {'$ref': '#/$defs/V13RuleQuery'}, 'schedule': {'anyOf': [{'$ref': '#/$defs/V13MonitorSchedule'}, {'type': 'null'}]}, 'script_id': {'$ref': '#/$defs/NullableId'}, 'skill_ids': {'type': 'array', 'items': {'$ref': '#/$defs/Id'}, 'maxItems': 50}, 'datasource_ids': {'type': 'array', 'items': {'$ref': '#/$defs/Id'}, 'maxItems': 50}, 'mitre_techniques': {'type': 'array', 'items': {'type': 'string', 'minLength': 1, 'maxLength': 16}, 'maxItems': 100}, 'tags': {'type': 'array', 'items': {'type': 'string', 'minLength': 1, 'maxLength': 128}, 'maxItems': 100}, 'exclusions': {'type': 'array', 'items': {'$ref': '#/$defs/V13RuleExclusion'}, 'maxItems': 200}, 'origin': {'type': 'string', 'enum': ['manual', 'proposal', 'community', 'sample']}, 'scheduler_job_id': {'$ref': '#/$defs/NullableId'}, 'last_run': {'anyOf': [{'$ref': '#/$defs/V13ScriptLastRun'}, {'type': 'null'}]}, 'revision': {'type': 'integer', 'minimum': 0}, 'created_at': {'$ref': '#/$defs/Timestamp'}, 'updated_at': {'$ref': '#/$defs/Timestamp'}}, 'required': ['rule_id', 'space_id', 'name', 'description', 'enabled', 'severity', 'query', 'schedule', 'script_id', 'skill_ids', 'datasource_ids', 'mitre_techniques', 'tags', 'exclusions', 'origin', 'scheduler_job_id', 'last_run', 'revision', 'created_at', 'updated_at'], 'additionalProperties': False},
    'V13MonitorSchedule': {'type': 'object', 'properties': {'kind': {'type': 'string', 'enum': ['every', 'cron']}, 'every_seconds': {'type': 'integer', 'minimum': 0}, 'expression': {'type': 'string', 'minLength': 1, 'maxLength': 128}, 'timezone': {'type': 'string', 'minLength': 1, 'maxLength': 64}}, 'required': ['kind'], 'additionalProperties': False},
    'V13ProposalComment': {'type': 'object', 'properties': {'comment_id': {'$ref': '#/$defs/Id'}, 'author': {'type': 'string', 'minLength': 1, 'maxLength': 128}, 'text': {'type': 'string', 'minLength': 0, 'maxLength': 4000}, 'created_at': {'$ref': '#/$defs/Timestamp'}}, 'required': ['comment_id', 'author', 'text', 'created_at'], 'additionalProperties': False},
    'V13ProposalEvaluation': {'type': 'object', 'properties': {'job_id': {'$ref': '#/$defs/Id'}, 'mode': {'type': 'string', 'enum': ['eval', 'rule', 'backtest']}, 'state': {'type': 'string', 'minLength': 1, 'maxLength': 32}, 'summary': {'anyOf': [{'$ref': '#/$defs/JsonObject'}, {'type': 'null'}]}, 'at': {'$ref': '#/$defs/Timestamp'}}, 'required': ['job_id', 'mode', 'state', 'summary', 'at'], 'additionalProperties': False},
    'V13QueueItem': {'type': 'object', 'properties': {'alert_id': {'$ref': '#/$defs/Id'}, 'title': {'type': 'string', 'minLength': 1, 'maxLength': 300}, 'severity': {'type': 'string', 'enum': ['info', 'low', 'medium', 'high', 'critical']}, 'status': {'type': 'string', 'minLength': 1, 'maxLength': 32}, 'created_at': {'$ref': '#/$defs/Timestamp'}, 'sla_due_at': {'$ref': '#/$defs/Timestamp'}, 'assignee': {'anyOf': [{'type': 'string', 'minLength': 1, 'maxLength': 128}, {'type': 'null'}]}, 'rule_id': {'$ref': '#/$defs/NullableId'}}, 'required': ['alert_id', 'title', 'severity', 'status', 'created_at', 'sla_due_at', 'assignee', 'rule_id'], 'additionalProperties': False},
    'V13RuleConfidence': {'type': 'object', 'properties': {'rule_id': {'$ref': '#/$defs/Id'}, 'alerts_total': {'type': 'integer', 'minimum': 0}, 'true_positive': {'type': 'integer', 'minimum': 0}, 'false_positive': {'type': 'integer', 'minimum': 0}, 'confidence': {'anyOf': [{'type': 'number'}, {'type': 'null'}]}}, 'required': ['rule_id', 'alerts_total', 'true_positive', 'false_positive', 'confidence'], 'additionalProperties': False},
    'V13RuleDraft': {'type': 'object', 'properties': {'name': {'type': 'string', 'minLength': 1, 'maxLength': 200}, 'severity': {'type': 'string', 'enum': ['info', 'low', 'medium', 'high', 'critical']}, 'query': {'$ref': '#/$defs/V13RuleQuery'}, 'script_id': {'$ref': '#/$defs/NullableId'}, 'skill_ids': {'type': 'array', 'items': {'$ref': '#/$defs/Id'}, 'maxItems': 50}, 'datasource_ids': {'type': 'array', 'items': {'$ref': '#/$defs/Id'}, 'maxItems': 50}}, 'required': ['name', 'severity', 'query'], 'additionalProperties': False},
    'V13RuleDrift': {'type': 'object', 'properties': {'rule_id': {'$ref': '#/$defs/Id'}, 'baseline_rate': {'anyOf': [{'type': 'number'}, {'type': 'null'}]}, 'current_rate': {'anyOf': [{'type': 'number'}, {'type': 'null'}]}, 'drift_ratio': {'anyOf': [{'type': 'number'}, {'type': 'null'}]}, 'status': {'type': 'string', 'enum': ['stable', 'drifting', 'unknown']}}, 'required': ['rule_id', 'baseline_rate', 'current_rate', 'drift_ratio', 'status'], 'additionalProperties': False},
    'V13RuleExclusion': {'type': 'object', 'properties': {'exclusion_id': {'$ref': '#/$defs/Id'}, 'field': {'type': 'string', 'minLength': 1, 'maxLength': 128}, 'value': {'type': 'string', 'minLength': 1, 'maxLength': 1024}, 'reason': {'type': 'string', 'minLength': 0, 'maxLength': 1024}, 'source': {'type': 'string', 'enum': ['manual', 'tuning', 'auto_tune']}}, 'required': ['exclusion_id', 'field', 'value', 'reason', 'source'], 'additionalProperties': False},
    'V13RuleQuery': {'type': 'object', 'properties': {'language': {'type': 'string', 'enum': ['sql', 'sigma', 'script', 'nl']}, 'text': {'type': 'string', 'minLength': 0, 'maxLength': 65536}}, 'required': ['language', 'text'], 'additionalProperties': False},
    'V13ScriptDetail': {'type': 'object', 'properties': {'script_id': {'$ref': '#/$defs/Id'}, 'name': {'type': 'string', 'minLength': 1, 'maxLength': 120}, 'description': {'type': 'string', 'minLength': 0, 'maxLength': 4096}, 'input_schema': {'$ref': '#/$defs/JsonObject'}, 'risk_level': {'type': 'string', 'enum': ['low', 'medium', 'high', 'critical']}, 'requires_approval': {'type': 'boolean'}, 'enabled': {'type': 'boolean'}, 'tags': {'type': 'array', 'items': {'type': 'string', 'minLength': 1, 'maxLength': 128}, 'maxItems': 100}, 'contexts': {'type': 'array', 'items': {'type': 'string', 'enum': ['alert', 'case', 'host', 'identity', 'any']}, 'maxItems': 5}, 'scope_layer': {'type': 'string', 'enum': ['workspace', 'owner', 'global', 'builtin']}, 'revision': {'type': 'integer', 'minimum': 0}, 'last_run': {'anyOf': [{'$ref': '#/$defs/V13ScriptLastRun'}, {'type': 'null'}]}, 'code': {'type': 'string', 'minLength': 0, 'maxLength': 200000}, 'entrypoint': {'type': 'string', 'minLength': 1, 'maxLength': 128}, 'interpreter': {'type': 'string', 'minLength': 1, 'maxLength': 64}, 'timeout_seconds': {'type': 'number'}, 'io_contract': {'$ref': '#/$defs/JsonObject'}, 'data_sources': {'type': 'array', 'items': {'$ref': '#/$defs/JsonObject'}, 'maxItems': 100}}, 'required': ['script_id', 'name', 'description', 'input_schema', 'risk_level', 'requires_approval', 'enabled', 'tags', 'contexts', 'scope_layer', 'revision', 'last_run', 'code', 'entrypoint', 'interpreter', 'timeout_seconds', 'io_contract', 'data_sources'], 'additionalProperties': False},
    'V13ScriptLastRun': {'type': 'object', 'properties': {'status': {'type': 'string', 'minLength': 1, 'maxLength': 32}, 'duration_ms': {'anyOf': [{'type': 'integer', 'minimum': 0}, {'type': 'null'}]}, 'run_at': {'$ref': '#/$defs/Timestamp'}, 'last_error': {'anyOf': [{'type': 'string', 'minLength': 0, 'maxLength': 1024}, {'type': 'null'}]}}, 'required': ['status', 'duration_ms', 'run_at', 'last_error'], 'additionalProperties': False},
    'V13ScriptRun': {'type': 'object', 'properties': {'execution_id': {'$ref': '#/$defs/Id'}, 'script_id': {'$ref': '#/$defs/Id'}, 'state': {'type': 'string', 'enum': ['accepted', 'awaiting_approval', 'running', 'completed', 'failed', 'unknown']}, 'requires_approval': {'type': 'boolean'}, 'task_id': {'$ref': '#/$defs/NullableId'}, 'approval_ids': {'type': 'array', 'items': {'$ref': '#/$defs/Id'}, 'maxItems': 20}, 'ok': {'anyOf': [{'type': 'boolean'}, {'type': 'null'}]}, 'output': {'anyOf': [{'$ref': '#/$defs/JsonObject'}, {'type': 'null'}]}, 'error': {'anyOf': [{'type': 'string', 'minLength': 0, 'maxLength': 2048}, {'type': 'null'}]}, 'duration_ms': {'anyOf': [{'type': 'integer', 'minimum': 0}, {'type': 'null'}]}, 'started_at': {'$ref': '#/$defs/Timestamp'}, 'finished_at': {'$ref': '#/$defs/Timestamp'}, 'coverage': {'$ref': '#/$defs/Coverage'}}, 'required': ['execution_id', 'script_id', 'state', 'requires_approval', 'task_id', 'approval_ids', 'ok', 'output', 'error', 'duration_ms', 'started_at', 'finished_at', 'coverage'], 'additionalProperties': False},
    'V13ScriptSummary': {'type': 'object', 'properties': {'script_id': {'$ref': '#/$defs/Id'}, 'name': {'type': 'string', 'minLength': 1, 'maxLength': 120}, 'description': {'type': 'string', 'minLength': 0, 'maxLength': 4096}, 'input_schema': {'$ref': '#/$defs/JsonObject'}, 'risk_level': {'type': 'string', 'enum': ['low', 'medium', 'high', 'critical']}, 'requires_approval': {'type': 'boolean'}, 'enabled': {'type': 'boolean'}, 'tags': {'type': 'array', 'items': {'type': 'string', 'minLength': 1, 'maxLength': 128}, 'maxItems': 100}, 'contexts': {'type': 'array', 'items': {'type': 'string', 'enum': ['alert', 'case', 'host', 'identity', 'any']}, 'maxItems': 5}, 'scope_layer': {'type': 'string', 'enum': ['workspace', 'owner', 'global', 'builtin']}, 'revision': {'type': 'integer', 'minimum': 0}, 'last_run': {'anyOf': [{'$ref': '#/$defs/V13ScriptLastRun'}, {'type': 'null'}]}}, 'required': ['script_id', 'name', 'description', 'input_schema', 'risk_level', 'requires_approval', 'enabled', 'tags', 'contexts', 'scope_layer', 'revision', 'last_run'], 'additionalProperties': False},
    'V13SimFinding': {'type': 'object', 'properties': {'finding_id': {'$ref': '#/$defs/Id'}, 'title': {'type': 'string', 'minLength': 1, 'maxLength': 300}, 'description': {'type': 'string', 'minLength': 0, 'maxLength': 8000}, 'severity': {'type': 'string', 'enum': ['info', 'low', 'medium', 'high', 'critical']}, 'technique_id': {'anyOf': [{'type': 'string', 'minLength': 1, 'maxLength': 16}, {'type': 'null'}]}, 'detection_rule_id': {'$ref': '#/$defs/NullableId'}, 'execution_id': {'$ref': '#/$defs/NullableId'}, 'created_by': {'type': 'string', 'minLength': 1, 'maxLength': 128}, 'created_at': {'$ref': '#/$defs/Timestamp'}}, 'required': ['finding_id', 'title', 'description', 'severity', 'technique_id', 'detection_rule_id', 'execution_id', 'created_by', 'created_at'], 'additionalProperties': False},
    'V13SimulationExecution': {'type': 'object', 'properties': {'execution_id': {'$ref': '#/$defs/Id'}, 'session_id': {'$ref': '#/$defs/Id'}, 'technique_id': {'type': 'string', 'minLength': 1, 'maxLength': 16}, 'detected': {'anyOf': [{'type': 'boolean'}, {'type': 'null'}]}, 'detection_rule_id': {'$ref': '#/$defs/NullableId'}, 'alert_id': {'$ref': '#/$defs/NullableId'}, 'notes': {'anyOf': [{'type': 'string', 'minLength': 0, 'maxLength': 4000}, {'type': 'null'}]}, 'revision': {'type': 'integer', 'minimum': 0}, 'created_at': {'$ref': '#/$defs/Timestamp'}, 'updated_at': {'$ref': '#/$defs/Timestamp'}}, 'required': ['execution_id', 'session_id', 'technique_id', 'detected', 'detection_rule_id', 'alert_id', 'notes', 'revision', 'created_at', 'updated_at'], 'additionalProperties': False},
    'V13SimulationSession': {'type': 'object', 'properties': {'session_id': {'$ref': '#/$defs/Id'}, 'space_id': {'$ref': '#/$defs/Id'}, 'scenario': {'type': 'string', 'pattern': '^[a-z][a-z0-9_]{0,31}$'}, 'title': {'type': 'string', 'minLength': 1, 'maxLength': 200}, 'objectives': {'type': 'array', 'items': {'type': 'string', 'minLength': 0, 'maxLength': 500}, 'maxItems': 50}, 'participants': {'type': 'array', 'items': {'type': 'string', 'minLength': 1, 'maxLength': 128}, 'maxItems': 50}, 'status': {'type': 'string', 'enum': ['active', 'completed']}, 'findings': {'type': 'array', 'items': {'$ref': '#/$defs/V13SimFinding'}, 'maxItems': 500}, 'summary': {'anyOf': [{'type': 'string', 'minLength': 0, 'maxLength': 8000}, {'type': 'null'}]}, 'completed_at': {'$ref': '#/$defs/Timestamp'}, 'revision': {'type': 'integer', 'minimum': 0}, 'created_at': {'$ref': '#/$defs/Timestamp'}, 'updated_at': {'$ref': '#/$defs/Timestamp'}}, 'required': ['session_id', 'space_id', 'scenario', 'title', 'objectives', 'participants', 'status', 'findings', 'summary', 'completed_at', 'revision', 'created_at', 'updated_at'], 'additionalProperties': False},
    'V13SlaBreach': {'type': 'object', 'properties': {'alert_id': {'$ref': '#/$defs/NullableId'}, 'case_id': {'$ref': '#/$defs/NullableId'}, 'severity': {'type': 'string', 'enum': ['info', 'low', 'medium', 'high', 'critical']}, 'due_at': {'$ref': '#/$defs/Timestamp'}, 'breached': {'type': 'boolean'}, 'stage': {'type': 'string', 'enum': ['ack', 'resolve']}}, 'required': ['alert_id', 'case_id', 'severity', 'due_at', 'breached', 'stage'], 'additionalProperties': False},
    'V13SlaPolicy': {'type': 'object', 'properties': {'severity': {'type': 'string', 'enum': ['info', 'low', 'medium', 'high', 'critical']}, 'ack_minutes': {'anyOf': [{'type': 'integer', 'minimum': 0}, {'type': 'null'}]}, 'resolve_minutes': {'anyOf': [{'type': 'integer', 'minimum': 0}, {'type': 'null'}]}}, 'required': ['severity', 'ack_minutes', 'resolve_minutes'], 'additionalProperties': False},
    'V13TechniqueCoverage': {'type': 'object', 'properties': {'technique_id': {'type': 'string', 'minLength': 1, 'maxLength': 16}, 'tactic': {'anyOf': [{'type': 'string', 'minLength': 1, 'maxLength': 64}, {'type': 'null'}]}, 'rule_ids': {'type': 'array', 'items': {'$ref': '#/$defs/Id'}, 'maxItems': 100}, 'enabled_rule_count': {'type': 'integer', 'minimum': 0}}, 'required': ['technique_id', 'tactic', 'rule_ids', 'enabled_rule_count'], 'additionalProperties': False},
    'V13TimeWindow': {'type': 'object', 'properties': {'since': {'$ref': '#/$defs/Timestamp'}, 'until': {'$ref': '#/$defs/Timestamp'}}, 'required': ['since', 'until'], 'additionalProperties': False},
    'V13ToggleResult': {'type': 'object', 'properties': {'rule_id': {'$ref': '#/$defs/Id'}, 'enabled': {'anyOf': [{'type': 'boolean'}, {'type': 'null'}]}, 'revision': {'anyOf': [{'type': 'integer', 'minimum': 0}, {'type': 'null'}]}, 'error_code': {'anyOf': [{'type': 'string', 'minLength': 1, 'maxLength': 128}, {'type': 'null'}]}}, 'required': ['rule_id', 'enabled', 'revision', 'error_code'], 'additionalProperties': False},
    'V13TuningSuggestion': {'type': 'object', 'properties': {'suggestion_id': {'$ref': '#/$defs/Id'}, 'rule_id': {'$ref': '#/$defs/Id'}, 'kind': {'type': 'string', 'enum': ['exclusion', 'threshold', 'disable']}, 'field': {'anyOf': [{'type': 'string', 'minLength': 1, 'maxLength': 128}, {'type': 'null'}]}, 'value': {'anyOf': [{'type': 'string', 'minLength': 1, 'maxLength': 1024}, {'type': 'null'}]}, 'evidence_alert_ids': {'type': 'array', 'items': {'$ref': '#/$defs/Id'}, 'maxItems': 100}, 'false_positive_count': {'type': 'integer', 'minimum': 0}, 'status': {'type': 'string', 'enum': ['open', 'applied', 'dismissed']}, 'reason': {'anyOf': [{'type': 'string', 'minLength': 0, 'maxLength': 1024}, {'type': 'null'}]}}, 'required': ['suggestion_id', 'rule_id', 'kind', 'field', 'value', 'evidence_alert_ids', 'false_positive_count', 'status', 'reason'], 'additionalProperties': False},
    'V13WorkflowTemplate': {'type': 'object', 'properties': {'template_id': {'$ref': '#/$defs/Id'}, 'name': {'type': 'string', 'minLength': 1, 'maxLength': 200}, 'summary': {'type': 'string', 'minLength': 0, 'maxLength': 4096}, 'tags': {'type': 'array', 'items': {'type': 'string', 'minLength': 1, 'maxLength': 128}, 'maxItems': 100}, 'source': {'type': 'string', 'enum': ['package', 'builtin']}, 'package_id': {'$ref': '#/$defs/NullableId'}, 'definition': {'$ref': '#/$defs/WorkflowDefinition'}, 'required_script_ids': {'type': 'array', 'items': {'$ref': '#/$defs/Id'}, 'maxItems': 100}, 'required_skill_ids': {'type': 'array', 'items': {'$ref': '#/$defs/Id'}, 'maxItems': 100}}, 'required': ['template_id', 'name', 'summary', 'tags', 'source', 'package_id', 'definition', 'required_script_ids', 'required_skill_ids'], 'additionalProperties': False},
}

V13_COLLECTIONS = {'detection_baselines': 'owner',
 'detection_drift_snapshots': 'owner',
 'detection_proposals': 'owner',
 'detection_tuning': 'owner',
 'monitor_rules': 'owner',
 'monitor_sla_policies': 'owner',
 'script_runs': 'owner',
 'simulation_executions': 'owner',
 'simulation_sessions': 'owner'}

V13_JOB_KINDS = {
    'monitor_rule_test': {"result_schema": {'type': 'object', 'properties': {'matched': {'anyOf': [{'type': 'integer', 'minimum': 0}, {'type': 'null'}]}, 'sample_matches': {'type': 'array', 'items': {'$ref': '#/$defs/JsonObject'}, 'maxItems': 20}, 'errors': {'type': 'array', 'items': {'type': 'string', 'minLength': 0, 'maxLength': 512}, 'maxItems': 20}, 'coverage': {'$ref': '#/$defs/Coverage'}}, 'required': ['matched', 'sample_matches', 'errors', 'coverage'], 'additionalProperties': False}, "finalize": _finalize_json},
    'monitor_rule_backtest': {"result_schema": {'type': 'object', 'properties': {'window': {'$ref': '#/$defs/V13TimeWindow'}, 'evaluated_events': {'anyOf': [{'type': 'integer', 'minimum': 0}, {'type': 'null'}]}, 'matches': {'anyOf': [{'type': 'integer', 'minimum': 0}, {'type': 'null'}]}, 'estimated_alerts_per_day': {'anyOf': [{'type': 'number'}, {'type': 'null'}]}, 'sample_matches': {'type': 'array', 'items': {'$ref': '#/$defs/JsonObject'}, 'maxItems': 20}, 'coverage': {'$ref': '#/$defs/Coverage'}}, 'required': ['window', 'evaluated_events', 'matches', 'estimated_alerts_per_day', 'sample_matches', 'coverage'], 'additionalProperties': False}, "finalize": _finalize_json},
    'workflow_draft': {"result_schema": {'type': 'object', 'properties': {'definition': {'$ref': '#/$defs/WorkflowDefinition'}, 'referenced': {'$ref': '#/$defs/V13DraftRefs'}, 'warnings': {'type': 'array', 'items': {'type': 'string', 'minLength': 0, 'maxLength': 512}, 'maxItems': 50}}, 'required': ['definition', 'referenced', 'warnings'], 'additionalProperties': False}, "finalize": v13_w7_playbooks.finalize_workflow_draft, 'validate_result': _validate_draft_result},
    'detection_proposal_evaluation': {
        'result_schema': v13_w7_detection_lifecycle.EVALUATION_RESULT_SCHEMA,
        'finalize': v13_w7_detection_lifecycle.finalize_evaluation,
        'validate_result': v13_w7_detection_lifecycle.validate_evaluation_result,
        'on_complete': v13_w7_detection_lifecycle.complete_evaluation,
    },
}

V13_ACTIONS = (
    V13Action(
        name='scripts.list', effect='read',
        params_schema={'type': 'object', 'properties': {'space_id': {'$ref': '#/$defs/Id'}, 'context': {'type': 'string', 'enum': ['alert', 'case', 'host', 'identity', 'any']}, 'tag': {'type': 'string', 'minLength': 1, 'maxLength': 128}, 'enabled': {'type': 'boolean'}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 50}, 'cursor': {'$ref': '#/$defs/Cursor'}}, 'required': [], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'items': {'type': 'array', 'items': {'$ref': '#/$defs/V13ScriptSummary'}, 'maxItems': 100}, 'next_cursor': {'anyOf': [{'$ref': '#/$defs/Cursor'}, {'type': 'null'}]}, 'coverage': {'$ref': '#/$defs/Coverage'}}, 'required': ['items', 'next_cursor', 'coverage'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_scripts.list_scripts, ready=_store_ready,
        admin=False, idempotency_required=False,
        expected_revision_required=False, aliases=()),
    V13Action(
        name='scripts.get', effect='read',
        params_schema={'type': 'object', 'properties': {'script_id': {'$ref': '#/$defs/Id'}, 'space_id': {'$ref': '#/$defs/Id'}}, 'required': ['script_id'], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'script': {'$ref': '#/$defs/V13ScriptDetail'}}, 'required': ['script'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_scripts.get_script, ready=_store_ready,
        admin=False, idempotency_required=False,
        expected_revision_required=False, aliases=()),
    V13Action(
        name='scripts.create', effect='config',
        params_schema={'type': 'object', 'properties': {'space_id': {'$ref': '#/$defs/Id'}, 'script_id': {'$ref': '#/$defs/Id'}, 'name': {'type': 'string', 'minLength': 1, 'maxLength': 120}, 'description': {'type': 'string', 'minLength': 0, 'maxLength': 4096}, 'code': {'type': 'string', 'minLength': 0, 'maxLength': 200000}, 'input_schema': {'$ref': '#/$defs/JsonObject'}, 'risk_level': {'type': 'string', 'enum': ['low', 'medium', 'high', 'critical']}, 'requires_approval': {'type': 'boolean'}, 'tags': {'type': 'array', 'items': {'type': 'string', 'minLength': 1, 'maxLength': 128}, 'maxItems': 100}, 'contexts': {'type': 'array', 'items': {'type': 'string', 'enum': ['alert', 'case', 'host', 'identity', 'any']}, 'maxItems': 5}, 'entrypoint': {'type': 'string', 'minLength': 1, 'maxLength': 128}, 'interpreter': {'type': 'string', 'minLength': 1, 'maxLength': 64}, 'timeout_seconds': {'type': 'number', 'minimum': 0.001, 'maximum': 3600.0}}, 'required': ['script_id', 'name', 'code'], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'script': {'$ref': '#/$defs/V13ScriptDetail'}}, 'required': ['script'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_scripts.create_script, ready=_store_ready,
        admin=False, idempotency_required=True,
        expected_revision_required=False, aliases=()),
    V13Action(
        name='scripts.update', effect='config',
        params_schema={'type': 'object', 'properties': {'script_id': {'$ref': '#/$defs/Id'}, 'space_id': {'$ref': '#/$defs/Id'}, 'name': {'type': 'string', 'minLength': 1, 'maxLength': 120}, 'description': {'type': 'string', 'minLength': 0, 'maxLength': 4096}, 'code': {'type': 'string', 'minLength': 0, 'maxLength': 200000}, 'input_schema': {'$ref': '#/$defs/JsonObject'}, 'risk_level': {'type': 'string', 'enum': ['low', 'medium', 'high', 'critical']}, 'requires_approval': {'type': 'boolean'}, 'tags': {'type': 'array', 'items': {'type': 'string', 'minLength': 1, 'maxLength': 128}, 'maxItems': 100}, 'contexts': {'type': 'array', 'items': {'type': 'string', 'enum': ['alert', 'case', 'host', 'identity', 'any']}, 'maxItems': 5}, 'timeout_seconds': {'type': 'number', 'minimum': 0.001, 'maximum': 3600.0}, 'enabled': {'type': 'boolean'}}, 'required': ['script_id'], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'script': {'$ref': '#/$defs/V13ScriptDetail'}}, 'required': ['script'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_scripts.update_script, ready=_store_ready,
        admin=False, idempotency_required=True,
        expected_revision_required=True, aliases=()),
    V13Action(
        name='scripts.delete', effect='config',
        params_schema={'type': 'object', 'properties': {'script_id': {'$ref': '#/$defs/Id'}, 'space_id': {'$ref': '#/$defs/Id'}}, 'required': ['script_id'], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'script_id': {'$ref': '#/$defs/Id'}, 'deleted': {'type': 'boolean'}}, 'required': ['script_id', 'deleted'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_scripts.delete_script, ready=_store_ready,
        admin=False, idempotency_required=True,
        expected_revision_required=True, aliases=()),
    V13Action(
        name='scripts.run', effect='execute',
        params_schema={'type': 'object', 'properties': {'script_id': {'$ref': '#/$defs/Id'}, 'space_id': {'$ref': '#/$defs/Id'}, 'inputs': {'$ref': '#/$defs/JsonObject'}, 'context': {'type': 'object', 'properties': {'kind': {'type': 'string', 'enum': ['alert', 'case', 'host', 'identity']}, 'id': {'$ref': '#/$defs/Id'}}, 'required': ['kind', 'id'], 'additionalProperties': False}}, 'required': ['script_id'], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'run': {'$ref': '#/$defs/V13ScriptRun'}}, 'required': ['run'], 'additionalProperties': False},
        accepted_result_schema={'type': 'object', 'properties': {'state': {'const': 'accepted'}, 'operation_id': {'$ref': '#/$defs/Id'}, 'execution_id': {'$ref': '#/$defs/Id'}, 'task_id': {'$ref': '#/$defs/Id'}, 'requires_approval': {'type': 'boolean'}}, 'required': ['state', 'operation_id', 'execution_id', 'task_id', 'requires_approval'], 'additionalProperties': False},
        handler=v13_w7_scripts.run_script, ready=_store_ready,
        admin=False, idempotency_required=True,
        expected_revision_required=False, aliases=()),
    V13Action(
        name='scripts.runs.get', effect='read',
        params_schema={'type': 'object', 'properties': {'execution_id': {'$ref': '#/$defs/Id'}}, 'required': ['execution_id'], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'run': {'$ref': '#/$defs/V13ScriptRun'}}, 'required': ['run'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_scripts.get_run, ready=_store_ready,
        admin=False, idempotency_required=False,
        expected_revision_required=False, aliases=()),
    V13Action(
        name='monitors.rules.list', effect='read',
        params_schema={'type': 'object', 'properties': {'space_id': {'$ref': '#/$defs/Id'}, 'enabled': {'type': 'boolean'}, 'severity': {'type': 'string', 'enum': ['info', 'low', 'medium', 'high', 'critical']}, 'tag': {'type': 'string', 'minLength': 1, 'maxLength': 128}, 'mitre_technique': {'type': 'string', 'minLength': 1, 'maxLength': 16}, 'origin': {'type': 'string', 'enum': ['manual', 'proposal', 'community', 'sample']}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 50}, 'cursor': {'$ref': '#/$defs/Cursor'}}, 'required': [], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'items': {'type': 'array', 'items': {'$ref': '#/$defs/V13MonitorRule'}, 'maxItems': 100}, 'next_cursor': {'anyOf': [{'$ref': '#/$defs/Cursor'}, {'type': 'null'}]}, 'coverage': {'$ref': '#/$defs/Coverage'}}, 'required': ['items', 'next_cursor', 'coverage'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_monitors.list_rules, ready=_store_ready,
        admin=False, idempotency_required=False,
        expected_revision_required=False, aliases=()),
    V13Action(
        name='monitors.rules.get', effect='read',
        params_schema={'type': 'object', 'properties': {'rule_id': {'$ref': '#/$defs/Id'}}, 'required': ['rule_id'], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'rule': {'$ref': '#/$defs/V13MonitorRule'}}, 'required': ['rule'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_monitors.get_rule, ready=_store_ready,
        admin=False, idempotency_required=False,
        expected_revision_required=False, aliases=()),
    V13Action(
        name='monitors.rules.create', effect='config',
        params_schema={'type': 'object', 'properties': {'space_id': {'$ref': '#/$defs/Id'}, 'name': {'type': 'string', 'minLength': 1, 'maxLength': 200}, 'severity': {'type': 'string', 'enum': ['info', 'low', 'medium', 'high', 'critical']}, 'query': {'$ref': '#/$defs/V13RuleQuery'}, 'description': {'type': 'string', 'minLength': 0, 'maxLength': 4096}, 'enabled': {'type': 'boolean'}, 'schedule': {'anyOf': [{'$ref': '#/$defs/V13MonitorSchedule'}, {'type': 'null'}]}, 'script_id': {'$ref': '#/$defs/NullableId'}, 'skill_ids': {'type': 'array', 'items': {'$ref': '#/$defs/Id'}, 'maxItems': 50}, 'datasource_ids': {'type': 'array', 'items': {'$ref': '#/$defs/Id'}, 'maxItems': 50}, 'mitre_techniques': {'type': 'array', 'items': {'type': 'string', 'minLength': 1, 'maxLength': 16}, 'maxItems': 100}, 'tags': {'type': 'array', 'items': {'type': 'string', 'minLength': 1, 'maxLength': 128}, 'maxItems': 100}}, 'required': ['name', 'severity', 'query'], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'rule': {'$ref': '#/$defs/V13MonitorRule'}}, 'required': ['rule'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_monitors.create_rule, ready=_store_ready,
        admin=False, idempotency_required=True,
        expected_revision_required=False, aliases=()),
    V13Action(
        name='monitors.rules.update', effect='config',
        params_schema={'type': 'object', 'properties': {'rule_id': {'$ref': '#/$defs/Id'}, 'name': {'type': 'string', 'minLength': 1, 'maxLength': 200}, 'description': {'type': 'string', 'minLength': 0, 'maxLength': 4096}, 'enabled': {'type': 'boolean'}, 'severity': {'type': 'string', 'enum': ['info', 'low', 'medium', 'high', 'critical']}, 'query': {'$ref': '#/$defs/V13RuleQuery'}, 'schedule': {'anyOf': [{'$ref': '#/$defs/V13MonitorSchedule'}, {'type': 'null'}]}, 'script_id': {'$ref': '#/$defs/NullableId'}, 'skill_ids': {'type': 'array', 'items': {'$ref': '#/$defs/Id'}, 'maxItems': 50}, 'datasource_ids': {'type': 'array', 'items': {'$ref': '#/$defs/Id'}, 'maxItems': 50}, 'mitre_techniques': {'type': 'array', 'items': {'type': 'string', 'minLength': 1, 'maxLength': 16}, 'maxItems': 100}, 'tags': {'type': 'array', 'items': {'type': 'string', 'minLength': 1, 'maxLength': 128}, 'maxItems': 100}}, 'required': ['rule_id'], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'rule': {'$ref': '#/$defs/V13MonitorRule'}}, 'required': ['rule'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_monitors.update_rule, ready=_store_ready,
        admin=False, idempotency_required=True,
        expected_revision_required=True, aliases=()),
    V13Action(
        name='monitors.rules.delete', effect='config',
        params_schema={'type': 'object', 'properties': {'rule_id': {'$ref': '#/$defs/Id'}}, 'required': ['rule_id'], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'rule_id': {'$ref': '#/$defs/Id'}, 'deleted': {'type': 'boolean'}}, 'required': ['rule_id', 'deleted'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_monitors.delete_rule, ready=_store_ready,
        admin=False, idempotency_required=True,
        expected_revision_required=True, aliases=()),
    V13Action(
        name='monitors.rules.bulk_toggle', effect='config',
        params_schema={'type': 'object', 'properties': {'rule_ids': {'type': 'array', 'items': {'$ref': '#/$defs/Id'}, 'maxItems': 100}, 'enabled': {'type': 'boolean'}, 'space_id': {'$ref': '#/$defs/Id'}}, 'required': ['rule_ids', 'enabled'], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'results': {'type': 'array', 'items': {'$ref': '#/$defs/V13ToggleResult'}, 'maxItems': 100}}, 'required': ['results'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_monitors.bulk_toggle, ready=_store_ready,
        admin=False, idempotency_required=True,
        expected_revision_required=False, aliases=()),
    V13Action(
        name='monitors.rules.test', effect='execute',
        params_schema={'type': 'object', 'properties': {'space_id': {'$ref': '#/$defs/Id'}, 'rule_id': {'$ref': '#/$defs/Id'}, 'rule': {'$ref': '#/$defs/V13RuleDraft'}, 'window': {'$ref': '#/$defs/V13TimeWindow'}}, 'required': [], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'job_id': {'$ref': '#/$defs/Id'}}, 'required': ['job_id'], 'additionalProperties': False},
        accepted_result_schema={'type': 'object', 'properties': {'state': {'const': 'accepted'}, 'operation_id': {'$ref': '#/$defs/Id'}, 'job_id': {'$ref': '#/$defs/Id'}, 'task_id': {'$ref': '#/$defs/Id'}}, 'required': ['state', 'operation_id', 'job_id', 'task_id'], 'additionalProperties': False},
        handler=v13_w7_monitors.test_rule, ready=_jobs_ready,
        admin=False, idempotency_required=True,
        expected_revision_required=False, aliases=()),
    V13Action(
        name='monitors.rules.backtest', effect='execute',
        params_schema={'type': 'object', 'properties': {'rule_id': {'$ref': '#/$defs/Id'}, 'window': {'$ref': '#/$defs/V13TimeWindow'}}, 'required': ['rule_id', 'window'], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'job_id': {'$ref': '#/$defs/Id'}}, 'required': ['job_id'], 'additionalProperties': False},
        accepted_result_schema={'type': 'object', 'properties': {'state': {'const': 'accepted'}, 'operation_id': {'$ref': '#/$defs/Id'}, 'job_id': {'$ref': '#/$defs/Id'}, 'task_id': {'$ref': '#/$defs/Id'}}, 'required': ['state', 'operation_id', 'job_id', 'task_id'], 'additionalProperties': False},
        handler=v13_w7_monitors.backtest_rule, ready=_jobs_ready,
        admin=False, idempotency_required=True,
        expected_revision_required=False, aliases=()),
    V13Action(
        name='monitors.coverage.get', effect='read',
        params_schema={'type': 'object', 'properties': {'space_id': {'$ref': '#/$defs/Id'}, 'framework': {'type': 'string', 'enum': ['mitre']}}, 'required': [], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'framework': {'type': 'string', 'enum': ['mitre']}, 'techniques': {'type': 'array', 'items': {'$ref': '#/$defs/V13TechniqueCoverage'}, 'maxItems': 1000}, 'covered_technique_count': {'type': 'integer', 'minimum': 0}, 'total_known': {'anyOf': [{'type': 'integer', 'minimum': 0}, {'type': 'null'}]}, 'coverage_ratio': {'anyOf': [{'type': 'number'}, {'type': 'null'}]}, 'coverage': {'$ref': '#/$defs/Coverage'}}, 'required': ['framework', 'techniques', 'covered_technique_count', 'total_known', 'coverage_ratio', 'coverage'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_monitors.coverage, ready=_store_ready,
        admin=False, idempotency_required=False,
        expected_revision_required=False, aliases=()),
    V13Action(
        name='monitors.confidence.get', effect='read',
        params_schema={'type': 'object', 'properties': {'space_id': {'$ref': '#/$defs/Id'}, 'rule_id': {'$ref': '#/$defs/Id'}}, 'required': [], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'rules': {'type': 'array', 'items': {'$ref': '#/$defs/V13RuleConfidence'}, 'maxItems': 1000}, 'coverage': {'$ref': '#/$defs/Coverage'}}, 'required': ['rules', 'coverage'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_monitors.confidence, ready=_store_ready,
        admin=False, idempotency_required=False,
        expected_revision_required=False, aliases=()),
    V13Action(
        name='monitors.drift.get', effect='read',
        params_schema={'type': 'object', 'properties': {'space_id': {'$ref': '#/$defs/Id'}, 'window_days': {'type': 'integer', 'minimum': 1, 'maximum': 90}}, 'required': [], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'rules': {'type': 'array', 'items': {'$ref': '#/$defs/V13RuleDrift'}, 'maxItems': 1000}, 'coverage': {'$ref': '#/$defs/Coverage'}}, 'required': ['rules', 'coverage'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_monitors.drift, ready=_store_ready,
        admin=False, idempotency_required=False,
        expected_revision_required=False, aliases=()),
    V13Action(
        name='monitors.sla.get', effect='read',
        params_schema={'type': 'object', 'properties': {'space_id': {'$ref': '#/$defs/Id'}, 'severity': {'type': 'string', 'enum': ['info', 'low', 'medium', 'high', 'critical']}}, 'required': [], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'policies': {'type': 'array', 'items': {'$ref': '#/$defs/V13SlaPolicy'}, 'maxItems': 5}, 'breaches': {'type': 'array', 'items': {'$ref': '#/$defs/V13SlaBreach'}, 'maxItems': 500}, 'coverage': {'$ref': '#/$defs/Coverage'}}, 'required': ['policies', 'breaches', 'coverage'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_monitors.sla, ready=_store_ready,
        admin=False, idempotency_required=False,
        expected_revision_required=False, aliases=()),
    V13Action(
        name='monitors.sla.set', effect='config',
        params_schema={'type': 'object', 'properties': {'space_id': {'$ref': '#/$defs/Id'}, 'policies': {'type': 'array', 'items': {'$ref': '#/$defs/V13SlaPolicy'}, 'maxItems': 5}}, 'required': ['policies'], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'policies': {'type': 'array', 'items': {'$ref': '#/$defs/V13SlaPolicy'}, 'maxItems': 5}, 'revision': {'type': 'integer', 'minimum': 0}}, 'required': ['policies', 'revision'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_monitors.set_sla, ready=_store_ready,
        admin=False, idempotency_required=True,
        expected_revision_required=True, aliases=()),
    V13Action(
        name='monitors.queue.list', effect='read',
        params_schema={'type': 'object', 'properties': {'space_id': {'$ref': '#/$defs/Id'}, 'status': {'type': 'string', 'enum': ['new', 'acknowledged', 'in_progress']}, 'severity': {'type': 'string', 'enum': ['info', 'low', 'medium', 'high', 'critical']}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 50}, 'cursor': {'$ref': '#/$defs/Cursor'}}, 'required': [], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'items': {'type': 'array', 'items': {'$ref': '#/$defs/V13QueueItem'}, 'maxItems': 100}, 'next_cursor': {'anyOf': [{'$ref': '#/$defs/Cursor'}, {'type': 'null'}]}, 'coverage': {'$ref': '#/$defs/Coverage'}}, 'required': ['items', 'next_cursor', 'coverage'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_monitors.queue, ready=_store_ready,
        admin=False, idempotency_required=False,
        expected_revision_required=False, aliases=()),
    V13Action(
        name='workflows.templates.list', effect='read',
        params_schema={'type': 'object', 'properties': {'space_id': {'$ref': '#/$defs/Id'}, 'tag': {'type': 'string', 'minLength': 1, 'maxLength': 128}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 50}, 'cursor': {'$ref': '#/$defs/Cursor'}}, 'required': [], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'items': {'type': 'array', 'items': {'$ref': '#/$defs/V13WorkflowTemplate'}, 'maxItems': 100}, 'next_cursor': {'anyOf': [{'$ref': '#/$defs/Cursor'}, {'type': 'null'}]}, 'coverage': {'$ref': '#/$defs/Coverage'}}, 'required': ['items', 'next_cursor', 'coverage'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_playbooks.list_templates, ready=_store_ready,
        admin=False, idempotency_required=False,
        expected_revision_required=False, aliases=()),
    V13Action(
        name='workflows.draft_from_prompt', effect='execute',
        params_schema={'type': 'object', 'properties': {'space_id': {'$ref': '#/$defs/Id'}, 'prompt': {'type': 'string', 'minLength': 0, 'maxLength': 8000}, 'name': {'type': 'string', 'minLength': 1, 'maxLength': 120}}, 'required': ['prompt'], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'job_id': {'$ref': '#/$defs/Id'}}, 'required': ['job_id'], 'additionalProperties': False},
        accepted_result_schema={'type': 'object', 'properties': {'state': {'const': 'accepted'}, 'operation_id': {'$ref': '#/$defs/Id'}, 'job_id': {'$ref': '#/$defs/Id'}, 'task_id': {'$ref': '#/$defs/Id'}}, 'required': ['state', 'operation_id', 'job_id', 'task_id'], 'additionalProperties': False},
        handler=v13_w7_playbooks.draft_from_prompt, ready=_jobs_ready,
        admin=False, idempotency_required=True,
        expected_revision_required=False, aliases=()),
    V13Action(
        name='workflows.dry_run', effect='read',
        params_schema={'type': 'object', 'properties': {'workflow_id': {'$ref': '#/$defs/Id'}, 'space_id': {'$ref': '#/$defs/Id'}, 'inputs': {'$ref': '#/$defs/JsonObject'}}, 'required': ['workflow_id'], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'workflow_id': {'$ref': '#/$defs/Id'}, 'steps': {'type': 'array', 'items': {'$ref': '#/$defs/V13DryRunStep'}, 'maxItems': 1000}, 'required_capabilities': {'type': 'array', 'items': {'type': 'string', 'minLength': 1, 'maxLength': 128}, 'maxItems': 100}, 'errors': {'type': 'array', 'items': {'$ref': '#/$defs/Error'}, 'maxItems': 100}, 'external_side_effects': {'const': 'none'}}, 'required': ['workflow_id', 'steps', 'required_capabilities', 'errors', 'external_side_effects'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_playbooks.dry_run, ready=_store_ready,
        admin=False, idempotency_required=False,
        expected_revision_required=False, aliases=()),
    V13Action(
        name='detection_proposals.list', effect='read',
        params_schema={'type': 'object', 'properties': {'space_id': {'$ref': '#/$defs/Id'}, 'status': {'type': 'string', 'enum': ['draft', 'pending', 'evaluating', 'approved', 'rejected', 'changes_requested', 'promoted']}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 50}, 'cursor': {'$ref': '#/$defs/Cursor'}}, 'required': [], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'items': {'type': 'array', 'items': {'$ref': '#/$defs/V13DetectionProposal'}, 'maxItems': 100}, 'next_cursor': {'anyOf': [{'$ref': '#/$defs/Cursor'}, {'type': 'null'}]}, 'coverage': {'$ref': '#/$defs/Coverage'}}, 'required': ['items', 'next_cursor', 'coverage'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_detection_lifecycle.list_proposals, ready=_store_ready,
        admin=False, idempotency_required=False,
        expected_revision_required=False, aliases=('monitors.proposals.list',)),
    V13Action(
        name='detection_proposals.get', effect='read',
        params_schema={'type': 'object', 'properties': {'proposal_id': {'$ref': '#/$defs/Id'}}, 'required': ['proposal_id'], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'proposal': {'$ref': '#/$defs/V13DetectionProposal'}}, 'required': ['proposal'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_detection_lifecycle.get_proposal, ready=_store_ready,
        admin=False, idempotency_required=False,
        expected_revision_required=False, aliases=('monitors.proposals.get',)),
    V13Action(
        name='detection_proposals.create', effect='config',
        params_schema={'type': 'object', 'properties': {'space_id': {'$ref': '#/$defs/Id'}, 'title': {'type': 'string', 'minLength': 1, 'maxLength': 200}, 'rationale': {'type': 'string', 'minLength': 0, 'maxLength': 8000}, 'rule': {'$ref': '#/$defs/V13RuleDraft'}, 'mitre_techniques': {'type': 'array', 'items': {'type': 'string', 'minLength': 1, 'maxLength': 16}, 'maxItems': 100}, 'source': {'type': 'string', 'enum': ['analyst', 'ai']}}, 'required': ['title', 'rule'], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'proposal': {'$ref': '#/$defs/V13DetectionProposal'}}, 'required': ['proposal'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_detection_lifecycle.create_proposal, ready=_store_ready,
        admin=False, idempotency_required=True,
        expected_revision_required=False, aliases=('monitors.proposals.create',)),
    V13Action(
        name='detection_proposals.comment', effect='config',
        params_schema={'type': 'object', 'properties': {'proposal_id': {'$ref': '#/$defs/Id'}, 'text': {'type': 'string', 'minLength': 0, 'maxLength': 4000}}, 'required': ['proposal_id', 'text'], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'proposal': {'$ref': '#/$defs/V13DetectionProposal'}}, 'required': ['proposal'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_detection_lifecycle.comment, ready=_store_ready,
        admin=False, idempotency_required=True,
        expected_revision_required=False, aliases=()),
    V13Action(
        name='detection_proposals.evaluate', effect='execute',
        params_schema={'type': 'object', 'properties': {'space_id': {'$ref': '#/$defs/Id'}, 'proposal_id': {'$ref': '#/$defs/Id'}, 'mode': {'type': 'string', 'enum': ['eval', 'rule', 'backtest']}, 'window': {'$ref': '#/$defs/V13TimeWindow'}}, 'required': [], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'job_ids': {'type': 'array', 'items': {'$ref': '#/$defs/Id'}, 'maxItems': 100}}, 'required': ['job_ids'], 'additionalProperties': False},
        accepted_result_schema={'type': 'object', 'properties': {'state': {'const': 'accepted'}, 'operation_id': {'$ref': '#/$defs/Id'}, 'job_ids': {'type': 'array', 'items': {'$ref': '#/$defs/Id'}, 'maxItems': 100}}, 'required': ['state', 'operation_id', 'job_ids'], 'additionalProperties': False},
        handler=v13_w7_detection_lifecycle.evaluate, ready=_jobs_ready,
        admin=False, idempotency_required=True,
        expected_revision_required=False, aliases=('monitors.proposals.evaluate',)),
    V13Action(
        name='detection_proposals.decide', effect='control',
        params_schema={'type': 'object', 'properties': {'proposal_id': {'$ref': '#/$defs/Id'}, 'decision': {'type': 'string', 'enum': ['approve', 'reject', 'request_changes']}, 'comment': {'type': 'string', 'minLength': 0, 'maxLength': 4000}}, 'required': ['proposal_id', 'decision'], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'proposal': {'$ref': '#/$defs/V13DetectionProposal'}}, 'required': ['proposal'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_detection_lifecycle.decide, ready=_store_ready,
        admin=False, idempotency_required=True,
        expected_revision_required=True, aliases=('monitors.proposals.decide',)),
    V13Action(
        name='detection_proposals.promote', effect='config',
        params_schema={'type': 'object', 'properties': {'proposal_id': {'$ref': '#/$defs/Id'}, 'enabled': {'type': 'boolean'}}, 'required': ['proposal_id'], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'proposal': {'$ref': '#/$defs/V13DetectionProposal'}, 'rule': {'$ref': '#/$defs/V13MonitorRule'}}, 'required': ['proposal', 'rule'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_detection_lifecycle.promote, ready=_store_ready,
        admin=False, idempotency_required=True,
        expected_revision_required=True, aliases=('monitors.proposals.promote',)),
    V13Action(
        name='detection_proposals.baselines.list', effect='read',
        params_schema={'type': 'object', 'properties': {'space_id': {'$ref': '#/$defs/Id'}, 'rule_id': {'$ref': '#/$defs/Id'}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 50}, 'cursor': {'$ref': '#/$defs/Cursor'}}, 'required': [], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'items': {'type': 'array', 'items': {'$ref': '#/$defs/V13DetectionBaseline'}, 'maxItems': 100}, 'next_cursor': {'anyOf': [{'$ref': '#/$defs/Cursor'}, {'type': 'null'}]}, 'coverage': {'$ref': '#/$defs/Coverage'}}, 'required': ['items', 'next_cursor', 'coverage'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_detection_lifecycle.list_baselines, ready=_store_ready,
        admin=False, idempotency_required=False,
        expected_revision_required=False, aliases=()),
    V13Action(
        name='detection_proposals.baselines.create', effect='config',
        params_schema={'type': 'object', 'properties': {'space_id': {'$ref': '#/$defs/Id'}, 'rule_id': {'$ref': '#/$defs/Id'}, 'metric': {'type': 'string', 'enum': ['alerts_per_day', 'fp_ratio']}, 'window': {'$ref': '#/$defs/V13TimeWindow'}}, 'required': ['metric', 'window'], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'baseline': {'$ref': '#/$defs/V13DetectionBaseline'}}, 'required': ['baseline'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_detection_lifecycle.create_baseline, ready=_store_ready,
        admin=False, idempotency_required=True,
        expected_revision_required=False, aliases=()),
    V13Action(
        name='detection_tuning.list', effect='read',
        params_schema={'type': 'object', 'properties': {'space_id': {'$ref': '#/$defs/Id'}, 'rule_id': {'$ref': '#/$defs/Id'}, 'status': {'type': 'string', 'enum': ['open', 'applied', 'dismissed']}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 50}, 'cursor': {'$ref': '#/$defs/Cursor'}}, 'required': [], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'items': {'type': 'array', 'items': {'$ref': '#/$defs/V13TuningSuggestion'}, 'maxItems': 100}, 'next_cursor': {'anyOf': [{'$ref': '#/$defs/Cursor'}, {'type': 'null'}]}, 'coverage': {'$ref': '#/$defs/Coverage'}}, 'required': ['items', 'next_cursor', 'coverage'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_detection_lifecycle.list_tuning, ready=_store_ready,
        admin=False, idempotency_required=False,
        expected_revision_required=False, aliases=()),
    V13Action(
        name='detection_tuning.summary', effect='read',
        params_schema={'type': 'object', 'properties': {'space_id': {'$ref': '#/$defs/Id'}}, 'required': [], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'rules_with_suggestions': {'type': 'integer', 'minimum': 0}, 'open': {'type': 'integer', 'minimum': 0}, 'applied': {'type': 'integer', 'minimum': 0}, 'dismissed': {'type': 'integer', 'minimum': 0}, 'estimated_alert_reduction': {'anyOf': [{'type': 'number'}, {'type': 'null'}]}, 'coverage': {'$ref': '#/$defs/Coverage'}}, 'required': ['rules_with_suggestions', 'open', 'applied', 'dismissed', 'estimated_alert_reduction', 'coverage'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_detection_lifecycle.tuning_summary, ready=_store_ready,
        admin=False, idempotency_required=False,
        expected_revision_required=False, aliases=()),
    V13Action(
        name='detection_tuning.apply', effect='config',
        params_schema={'type': 'object', 'properties': {'rule_id': {'$ref': '#/$defs/Id'}, 'suggestion_id': {'$ref': '#/$defs/Id'}}, 'required': ['rule_id', 'suggestion_id'], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'suggestion': {'$ref': '#/$defs/V13TuningSuggestion'}, 'rule': {'$ref': '#/$defs/V13MonitorRule'}}, 'required': ['suggestion', 'rule'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_detection_lifecycle.apply_tuning, ready=_store_ready,
        admin=False, idempotency_required=True,
        expected_revision_required=True, aliases=()),
    V13Action(
        name='detection_tuning.dismiss', effect='config',
        params_schema={'type': 'object', 'properties': {'rule_id': {'$ref': '#/$defs/Id'}, 'suggestion_id': {'$ref': '#/$defs/Id'}, 'reason': {'type': 'string', 'minLength': 0, 'maxLength': 1024}}, 'required': ['rule_id', 'suggestion_id'], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'suggestion': {'$ref': '#/$defs/V13TuningSuggestion'}}, 'required': ['suggestion'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_detection_lifecycle.dismiss_tuning, ready=_store_ready,
        admin=False, idempotency_required=True,
        expected_revision_required=False, aliases=()),
    V13Action(
        name='detection_tuning.auto_tune', effect='config',
        params_schema={'type': 'object', 'properties': {'rule_id': {'$ref': '#/$defs/Id'}, 'dry_run': {'type': 'boolean'}}, 'required': ['rule_id'], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'applied': {'type': 'array', 'items': {'$ref': '#/$defs/V13TuningSuggestion'}, 'maxItems': 200}, 'dry_run': {'type': 'boolean'}, 'rule': {'anyOf': [{'$ref': '#/$defs/V13MonitorRule'}, {'type': 'null'}]}}, 'required': ['applied', 'dry_run', 'rule'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_detection_lifecycle.auto_tune, ready=_store_ready,
        admin=False, idempotency_required=True,
        expected_revision_required=True, aliases=()),
    V13Action(
        name='purple_team.drift_snapshot', effect='config',
        params_schema={'type': 'object', 'properties': {'space_id': {'$ref': '#/$defs/Id'}}, 'required': [], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'snapshot': {'$ref': '#/$defs/V13DriftSnapshot'}}, 'required': ['snapshot'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_simulation_sessions.drift_snapshot, ready=_store_ready,
        admin=False, idempotency_required=True,
        expected_revision_required=False, aliases=()),
    V13Action(
        name='purple_team.tabletop.create', effect='config',
        params_schema={'type': 'object', 'properties': {'space_id': {'$ref': '#/$defs/Id'}, 'scenario': {'type': 'string', 'pattern': '^[a-z][a-z0-9_]{0,31}$'}, 'title': {'type': 'string', 'minLength': 1, 'maxLength': 200}, 'objectives': {'type': 'array', 'items': {'type': 'string', 'minLength': 0, 'maxLength': 500}, 'maxItems': 50}, 'participants': {'type': 'array', 'items': {'type': 'string', 'minLength': 1, 'maxLength': 128}, 'maxItems': 50}}, 'required': ['scenario', 'title'], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'session': {'$ref': '#/$defs/V13SimulationSession'}}, 'required': ['session'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_simulation_sessions.create_session, ready=_store_ready,
        admin=False, idempotency_required=True,
        expected_revision_required=False, aliases=()),
    V13Action(
        name='purple_team.tabletop.add_finding', effect='config',
        params_schema={'type': 'object', 'properties': {'session_id': {'$ref': '#/$defs/Id'}, 'title': {'type': 'string', 'minLength': 1, 'maxLength': 300}, 'description': {'type': 'string', 'minLength': 0, 'maxLength': 8000}, 'severity': {'type': 'string', 'enum': ['info', 'low', 'medium', 'high', 'critical']}, 'technique_id': {'type': 'string', 'minLength': 1, 'maxLength': 16}, 'detection_rule_id': {'$ref': '#/$defs/Id'}, 'detected': {'type': 'boolean'}}, 'required': ['session_id', 'title', 'severity'], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'session': {'$ref': '#/$defs/V13SimulationSession'}, 'execution': {'anyOf': [{'$ref': '#/$defs/V13SimulationExecution'}, {'type': 'null'}]}}, 'required': ['session', 'execution'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_simulation_sessions.add_finding, ready=_store_ready,
        admin=False, idempotency_required=True,
        expected_revision_required=True, aliases=()),
    V13Action(
        name='purple_team.tabletop.get', effect='read',
        params_schema={'type': 'object', 'properties': {'session_id': {'$ref': '#/$defs/Id'}}, 'required': ['session_id'], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'session': {'$ref': '#/$defs/V13SimulationSession'}, 'executions': {'type': 'array', 'items': {'$ref': '#/$defs/V13SimulationExecution'}, 'maxItems': 500}}, 'required': ['session', 'executions'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_simulation_sessions.get_session, ready=_store_ready,
        admin=False, idempotency_required=False,
        expected_revision_required=False, aliases=()),
    V13Action(
        name='purple_team.tabletop.complete', effect='control',
        params_schema={'type': 'object', 'properties': {'session_id': {'$ref': '#/$defs/Id'}, 'summary': {'type': 'string', 'minLength': 0, 'maxLength': 8000}}, 'required': ['session_id'], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'session': {'$ref': '#/$defs/V13SimulationSession'}}, 'required': ['session'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_simulation_sessions.complete, ready=_store_ready,
        admin=False, idempotency_required=True,
        expected_revision_required=True, aliases=()),
    V13Action(
        name='purple_team.executions.update_detection', effect='config',
        params_schema={'type': 'object', 'properties': {'execution_id': {'$ref': '#/$defs/Id'}, 'detected': {'type': 'boolean'}, 'detection_rule_id': {'$ref': '#/$defs/Id'}, 'alert_id': {'$ref': '#/$defs/Id'}, 'notes': {'type': 'string', 'minLength': 0, 'maxLength': 4000}}, 'required': ['execution_id', 'detected'], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'execution': {'$ref': '#/$defs/V13SimulationExecution'}}, 'required': ['execution'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_simulation_sessions.update_detection, ready=_store_ready,
        admin=False, idempotency_required=True,
        expected_revision_required=True, aliases=()),
    V13Action(
        name='live_actions.discover', effect='read',
        params_schema={'type': 'object', 'properties': {'space_id': {'$ref': '#/$defs/Id'}, 'capability': {'type': 'string', 'pattern': '^[a-z][a-z0-9_]{0,31}$'}, 'vendor_id': {'type': 'string', 'minLength': 1, 'maxLength': 128}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 50}, 'cursor': {'$ref': '#/$defs/Cursor'}}, 'required': [], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'items': {'type': 'array', 'items': {'$ref': '#/$defs/V13LiveAction'}, 'maxItems': 100}, 'next_cursor': {'anyOf': [{'$ref': '#/$defs/Cursor'}, {'type': 'null'}]}, 'coverage': {'$ref': '#/$defs/Coverage'}}, 'required': ['items', 'next_cursor', 'coverage'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_playbooks.discover_live_actions, ready=_store_ready,
        admin=False, idempotency_required=False,
        expected_revision_required=False, aliases=()),
    V13Action(
        name='live_actions.by_capability', effect='read',
        params_schema={'type': 'object', 'properties': {'capability': {'type': 'string', 'pattern': '^[a-z][a-z0-9_]{0,31}$'}, 'space_id': {'$ref': '#/$defs/Id'}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 50}, 'cursor': {'$ref': '#/$defs/Cursor'}}, 'required': ['capability'], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'items': {'type': 'array', 'items': {'$ref': '#/$defs/V13LiveAction'}, 'maxItems': 100}, 'next_cursor': {'anyOf': [{'$ref': '#/$defs/Cursor'}, {'type': 'null'}]}, 'coverage': {'$ref': '#/$defs/Coverage'}}, 'required': ['items', 'next_cursor', 'coverage'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_playbooks.live_actions_by_capability, ready=_store_ready,
        admin=False, idempotency_required=False,
        expected_revision_required=False, aliases=()),
    V13Action(
        name='live_actions.by_vendor', effect='read',
        params_schema={'type': 'object', 'properties': {'vendor_id': {'type': 'string', 'minLength': 1, 'maxLength': 128}, 'space_id': {'$ref': '#/$defs/Id'}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 50}, 'cursor': {'$ref': '#/$defs/Cursor'}}, 'required': ['vendor_id'], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'items': {'type': 'array', 'items': {'$ref': '#/$defs/V13LiveAction'}, 'maxItems': 100}, 'next_cursor': {'anyOf': [{'$ref': '#/$defs/Cursor'}, {'type': 'null'}]}, 'coverage': {'$ref': '#/$defs/Coverage'}}, 'required': ['items', 'next_cursor', 'coverage'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_playbooks.live_actions_by_vendor, ready=_store_ready,
        admin=False, idempotency_required=False,
        expected_revision_required=False, aliases=()),
    V13Action(
        name='live_actions.dry_run', effect='read',
        params_schema={'type': 'object', 'properties': {'action_ref': {'$ref': '#/$defs/Id'}, 'inputs': {'$ref': '#/$defs/JsonObject'}, 'target': {'$ref': '#/$defs/JsonObject'}, 'space_id': {'$ref': '#/$defs/Id'}}, 'required': ['action_ref', 'inputs'], 'additionalProperties': False},
        result_schema={'type': 'object', 'properties': {'action_ref': {'$ref': '#/$defs/Id'}, 'valid': {'type': 'boolean'}, 'errors': {'type': 'array', 'items': {'$ref': '#/$defs/V13FieldError'}, 'maxItems': 100}, 'scope_check': {'type': 'object', 'properties': {'in_scope': {'type': 'boolean'}, 'reasons': {'type': 'array', 'items': {'type': 'string', 'minLength': 0, 'maxLength': 512}, 'maxItems': 20}}, 'required': ['in_scope', 'reasons'], 'additionalProperties': False}, 'would_require_approval': {'type': 'boolean'}, 'side_effects': {'const': 'none'}}, 'required': ['action_ref', 'valid', 'errors', 'scope_check', 'would_require_approval', 'side_effects'], 'additionalProperties': False},
        accepted_result_schema=None,
        handler=v13_w7_playbooks.dry_run_live_action, ready=_store_ready,
        admin=False, idempotency_required=False,
        expected_revision_required=False, aliases=()),
)
