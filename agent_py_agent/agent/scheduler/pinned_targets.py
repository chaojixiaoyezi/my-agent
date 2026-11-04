"""调度器的历史目标解析，绝不从草稿或当前替代定义恢复固定版本。"""
import json
from copy import deepcopy
from pathlib import Path

from ..assets.monitor_adapter import _read_object, save_monitor_binding
from ..assets.revision_history import (
    blob_exists,
    blob_get,
    compatible_successor,
    find_version,
    get_version,
)
from ..assets.workflow_store import WorkflowDefinition, WorkflowStore


# LLM: 所有失败沿既有STALE错误码，reason只取结构化枚举，不从文案决定重试。
# 函数用途: 报告固定快照为何不能执行。
def _stale(reason):
    from .execution_policy import ExecutionTargetError
    raise ExecutionTargetError('固定资产发布快照不可用', error_code='SCHEDULER_EXECUTION_DEFINITION_STALE', reason=reason)


# LLM: 版本记录及所有必需blob在执行前核验；返回的历史对象不写回当前资产。
# 函数用途: 从准确版本或内容引用恢复调度目标与旧定义。
def resolve_pinned_target(owner_home, target, definition, reference):
    kind, identifier = target['kind'], target['asset_id']
    pin, frozen = target.get('pinned_version'), target.get('execution_definition_ref')
    record = (get_version(owner_home, kind, identifier, pin) if pin is not None else
              find_version(owner_home, kind, identifier, execution_definition_ref=frozen['definition_sha256']) if frozen else None)
    if record is None:
        if definition is not None and pin is None and frozen in (None, reference):
            return {**target, 'execution_definition_ref': reference}, definition
        _stale('version_deleted' if pin is not None else 'history_missing')
    hashes = [record['payload_blob'], *record.get('side_blobs', {}).values()]
    if record.get('code_sha256'):
        hashes.append(record['code_sha256'])
    if not all(blob_exists(owner_home, digest) for digest in hashes) or kind == 'script' and not record.get('code_sha256'):
        _stale('blob_missing')
    payload = json.loads(blob_get(owner_home, record['payload_blob']))
    resolved = {**target, 'definition_version': record['version']}
    if frozen is None:
        resolved['execution_definition_ref'] = {'schema_version': 'scheduler_execution_definition.v1',
            'kind': kind, 'asset_id': identifier, 'definition_sha256': record['execution_definition_ref']}
    if kind == 'workflow':
        historical = (WorkflowDefinition.from_any(payload) if 'descriptor' in payload
                      else WorkflowStore._decode_definition(payload))
    else:
        historical = {**payload, 'code': blob_get(owner_home, record['code_sha256']).decode('utf-8')}
        resolved['expected_script_pin'] = {'script_id': identifier, 'revision': record.get('storage_revision') or 0,
            'code_sha256': record['code_sha256'], 'source_scope': payload.get('source_scope', 'owner'),
            'execution_definition_ref': record.get('execution_definition_ref')}
    return resolved, historical


# LLM: 只有显式follow_compatible可推进，绑定更新沿原CAS入口。
# 函数用途: 合并监控绑定的固定目标及兼容升级事实。
def resolve_monitor_target(owner_home, monitor_id, target):
    binding = _read_object(Path(owner_home), ('data', 'monitors', monitor_id + '.json'))
    metadata = binding.get('metadata') if isinstance(binding, dict) else None
    if not isinstance(metadata, dict) or not isinstance(metadata.get('base_target'), dict):
        return deepcopy(target)
    base, pin = metadata['base_target'], metadata.get('pinned_version')
    execution = metadata.get('execution_target')
    if (base.get('kind'), base.get('asset_id')) != (target.get('kind'), target.get('asset_id')) and (
            not isinstance(execution, dict) or (execution.get('kind'), execution.get('asset_id')) != (target.get('kind'), target.get('asset_id'))):
        return deepcopy(target)
    if type(pin) is not int or pin < 1:
        return deepcopy(target)
    notice = None
    if metadata.get('upgrade_policy') == 'follow_compatible':
        successor = compatible_successor(owner_home, base['kind'], base['asset_id'], from_version=pin)
        if type(successor) is int and successor > pin:
            changed = save_monitor_binding(owner_home, monitor_id, pinned_version=successor,
                                           expected_revision=binding['revision'])
            metadata = changed['metadata']
            notice = {'from_version': pin, 'to_version': successor, 'reason': 'replay_passed'}
            pin = successor
    execution = metadata.get('execution_target') or base
    result = {**deepcopy(target), 'kind': execution['kind'], 'asset_id': execution['asset_id'],
              'pinned_version': metadata.get('composition_version') if metadata.get('composition') else pin,
              'execution_definition_ref': None}
    if notice:
        result['upgrade_notice'] = notice
    return result
