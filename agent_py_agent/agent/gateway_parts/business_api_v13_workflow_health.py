"""WF-11的单workflow只读投影；原服务授权、原Store采样、唯一helper聚合。"""
from .business_api_v13_contracts import V13Action


def health_ready(call):
    return callable(getattr(call.services.workflows, 'health_get', None))


def get_health(call):
    from .business_api_v13 import v13_require_access

    v13_require_access(call)
    result = call.services.workflows.health_get(call.params, actor=call.services.actor(call.context))
    v13_require_access(call)
    return result


_PARAMS = {'type': 'object', 'additionalProperties': False, 'required': ['workflow_ref'],
    'properties': {'workflow_ref': {'type': 'string', 'minLength': 1, 'maxLength': 128,
                                  'x-max-bytes': 128},
                   'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 50}}}
_COUNT = {'type': 'integer', 'minimum': 0}
_COVERAGE = {'type': 'object', 'additionalProperties': False,
    'properties': {'coverage': {'enum': ['complete', 'partial', 'unknown']},
        'truncated': {'type': 'boolean'}, 'scan_limit': _COUNT, 'scan_count': _COUNT,
        'read_limit': _COUNT, 'read_count': _COUNT, 'max_file_bytes': _COUNT,
        'sampling_mode': {'const': 'directory_prefix_run_snapshots'},
        'latest_guaranteed': {'const': False},
        'errors': {'type': 'array', 'maxItems': 401, 'items': {'type': 'object',
            'additionalProperties': False, 'required': ['code', 'error_type'],
            'properties': {'code': {'type': 'string', 'maxLength': 64},
                           'error_type': {'type': 'string', 'maxLength': 128}}}}},
    'required': ['coverage', 'truncated', 'scan_limit', 'scan_count', 'read_limit', 'read_count',
                 'max_file_bytes', 'sampling_mode', 'latest_guaranteed', 'errors']}
_HEALTH = {'type': 'object', 'additionalProperties': {'$ref': '#/$defs/JsonValue'},
    'properties': {'schema_version': {'const': 'workflow_health.v1'},
        'workflow_id': _PARAMS['properties']['workflow_ref'],
        'coverage': {'enum': ['complete', 'partial', 'unknown']},
        'truncated': {'type': 'boolean'}, 'sampled_runs': {'type': 'integer', 'minimum': 0, 'maximum': 100},
        'bounded_limit': _PARAMS['properties']['limit'],
        **{name: {'$ref': '#/$defs/JsonObject'} for name in ('run_counts', 'workflow_counts',
            'step_health', 'reason_classes', 'retry_stats', 'canonical_references')}},
    'required': ['schema_version', 'workflow_id', 'coverage', 'truncated', 'sampled_runs',
        'bounded_limit', 'run_counts', 'workflow_counts', 'step_health', 'reason_classes',
        'retry_stats', 'canonical_references']}
_RESULT = {'type': 'object', 'additionalProperties': False,
    'required': ['workflow_ref', 'health', 'health_read_coverage'], 'properties': {
        'workflow_ref': _PARAMS['properties']['workflow_ref'],
        # 动态指标/字段覆盖继续由唯一helper解释，不在handler复制统计算法。
        'health': _HEALTH, 'health_read_coverage': _COVERAGE}}
V13_ACTIONS = (V13Action('workflows.health.get', 'read', _PARAMS, _RESULT,
    get_health, ready=health_ready),)
