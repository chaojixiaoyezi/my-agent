"""调度器的历史目标解析，绝不从草稿或当前替代定义恢复固定版本。"""
import json
import logging
from copy import deepcopy
from pathlib import Path

from ..assets.monitor_adapter import _read_object, save_monitor_binding
from ..assets.revision_history import (
    AssetHistoryError,
    blob_exists,
    blob_get,
    compatible_successor,
    find_version,
    get_version,
    list_versions,
)
from ..assets.workflow_store import WorkflowDefinition, WorkflowStore


# LLM: 所有失败沿既有STALE错误码，reason只取结构化枚举，不从文案决定重试。
# 函数用途: 报告固定快照为何不能执行。
def _stale(reason):
    from .execution_policy import ExecutionTargetError
    raise ExecutionTargetError('固定资产发布快照不可用', error_code='SCHEDULER_EXECUTION_DEFINITION_STALE', reason=reason)


# LLM: 版本及全部blob执行前核验；准确发布编号与SHA/ref一起交原resolver，历史payload不签发当前授权。
# 函数用途: 恢复准确历史目标并传递完整pin，即使当前字节相同也不丢侧文件或借用新重试许可。
def resolve_pinned_target(owner_home, target, definition, reference):
    kind, identifier = target['kind'], target['asset_id']
    pin, frozen = target.get('pinned_version'), target.get('execution_definition_ref')
    record = (get_version(owner_home, kind, identifier, pin) if pin is not None else
              find_version(owner_home, kind, identifier, execution_definition_ref=frozen['definition_sha256']) if frozen else None)
    from .execution_policy import _legacy_execution_definition_ref
    if record is None and pin is None and frozen:
        if definition is not None and frozen['definition_sha256'] == _legacy_execution_definition_ref(definition):
            return deepcopy(target), definition
        try:
            record = _find_legacy_version(owner_home, kind, identifier, frozen['definition_sha256'])
        except AssetHistoryError:
            _stale('blob_missing')
    if record is None:
        if definition is not None and pin is None and frozen in (None, reference):
            return {**target, 'execution_definition_ref': reference}, definition
        _stale('version_deleted' if pin is not None else 'history_missing')
    hashes = [record['payload_blob'], *record.get('side_blobs', {}).values()]
    if record.get('code_sha256'):
        hashes.append(record['code_sha256'])
    try:
        if not all(blob_exists(owner_home, digest) for digest in hashes) or kind == 'script' and not record.get('code_sha256'):
            _stale('blob_missing')
        payload = json.loads(blob_get(owner_home, record['payload_blob']))
        code = blob_get(owner_home, record['code_sha256']).decode('utf-8') if kind == 'script' else None
    except (AssetHistoryError, UnicodeError, json.JSONDecodeError):
        _stale('blob_missing')
    resolved = {**target, 'definition_version': record['version']}
    if frozen is None:
        resolved['execution_definition_ref'] = {'schema_version': 'scheduler_execution_definition.v1',
            'kind': kind, 'asset_id': identifier, 'definition_sha256': record['execution_definition_ref']}
    if kind == 'workflow':
        historical = (WorkflowDefinition.from_any(payload) if 'descriptor' in payload
                      else WorkflowStore._decode_definition(payload))
    else:
        # 不给去除易变字段的历史 payload 补 revision；storage_revision 只属于待核验 pin，不能恢复当前权限。
        historical = {**payload, 'code': code}
        resolved['expected_script_pin'] = {'script_id': identifier, 'version': record['version'],
            'revision': record.get('storage_revision') or 0,
            'code_sha256': record['code_sha256'], 'source_scope': payload.get('source_scope', 'owner'),
            'execution_definition_ref': record.get('execution_definition_ref')}
    if frozen is not None and frozen['definition_sha256'] not in (
            record['execution_definition_ref'], _legacy_execution_definition_ref(historical)):
        _stale('history_missing')
    return resolved, historical


# LLM: 只有旧摘要未命中新索引时读取有界发布窗口；候选必须重算完整旧内容身份，不按版本接近程度猜。
# 函数用途: 从最近发布记录恢复旧冻结引用，超出窗口明确失败关闭。
def _find_legacy_version(owner_home, kind, identifier, digest, *, history_limit=100):
    from .execution_policy import _legacy_execution_definition_ref
    for item in list_versions(owner_home, kind, identifier, limit=history_limit):
        record = get_version(owner_home, kind, identifier, item['version'])
        if record is None or not blob_exists(owner_home, record['payload_blob']):
            continue
        payload = json.loads(blob_get(owner_home, record['payload_blob']))
        if kind == 'workflow':
            definition = (WorkflowDefinition.from_any(payload) if 'descriptor' in payload else WorkflowStore._decode_definition(payload))
        elif record.get('code_sha256') and blob_exists(owner_home, record['code_sha256']):
            definition = {**payload, 'code': blob_get(owner_home, record['code_sha256']).decode('utf-8')}
        else:
            continue
        if _legacy_execution_definition_ref(definition) == digest:
            return record
    return None


# LLM: 显式binding是用户的版本选择，job未指定pin时只是同资产选择器；输入明确pin冲突拒绝，自动升级仅follow_compatible。
# 函数用途: 合并监控固定目标，沿原CAS升级并发出不写资产定义的临时通知。
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
    expected = metadata.get('composition_version') if metadata.get('composition') and target.get('asset_id') == execution.get('asset_id') else pin
    if target.get('pinned_version') not in (None, expected):
        _stale('history_missing')
    notice = None
    if metadata.get('upgrade_policy') == 'follow_compatible':
        successor = compatible_successor(owner_home, base['kind'], base['asset_id'], from_version=pin)
        if type(successor) is int and successor > pin:
            changed = save_monitor_binding(owner_home, monitor_id, pinned_version=successor,
                                           expected_revision=binding['revision'])
            metadata = changed['metadata']
            notice = {'from_version': pin, 'to_version': successor, 'reason': 'replay_passed'}
            pin = successor
            logging.getLogger(__name__).info('监控固定版本已沿兼容链推进',
                extra={'event': 'monitor_pin_advanced', 'monitor_id': monitor_id, **notice})
    execution = metadata.get('execution_target') or base
    result = {**deepcopy(target), 'kind': execution['kind'], 'asset_id': execution['asset_id'],
              'pinned_version': metadata.get('composition_version') if metadata.get('composition') else pin,
              'execution_definition_ref': None}
    if notice:
        result['upgrade_notice'] = notice
    return result
