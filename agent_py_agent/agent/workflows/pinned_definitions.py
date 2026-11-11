"""从W1发布历史恢复定义及冻结脚本引用，绝不回退当前可编辑资产。"""

import json

from ..assets.revision_history import blob_get, find_version, get_version
from ..assets.workflow_store import WorkflowDefinition, WorkflowStore


class PinnedDefinitionError(ValueError):
    """向引擎提供无执行副作用的结构化版本读取错误。"""

    # LLM: 沿用W1既有错误码，不把缺失历史伪装成当前版本。
    # 函数用途: 保存精确版本错误供公开入口返回。
    def __init__(self, code):
        self.error_code = code
        super().__init__(code)


# LLM: 只读取内容寻址blob；缺失或损坏都转为引擎既有结构化错误，不读取当前文件补齐。
# 函数用途: 获取历史快照字节。
def required_blob(owner_home, sha):
    try:
        value = blob_get(owner_home, sha) if isinstance(sha, str) else None
    except (OSError, ValueError, TypeError) as exc:
        raise PinnedDefinitionError('ASSET_HISTORY_BLOB_MISSING') from exc
    if value is None:
        raise PinnedDefinitionError('ASSET_HISTORY_BLOB_MISSING')
    return value


# LLM: 显式编号只能点读同一版本；无编号旧pin沿原索引并同时约束SHA和执行身份，全部blob均核验。
# 函数用途: 为可用性检查、工作流与执行接缝解析同一份完整脚本历史。
def script_snapshot(owner_home, pin):
    if not isinstance(pin, dict) or not pin.get('script_id') or not pin.get('code_sha256'):
        raise PinnedDefinitionError('ASSET_VERSION_NOT_FOUND')
    if 'version' in pin:
        if type(pin['version']) is not int or pin['version'] < 1:
            raise PinnedDefinitionError('ASSET_VERSION_NOT_FOUND')
        record = get_version(owner_home, 'script', pin['script_id'], pin['version'])
    else:
        record = find_version(owner_home, 'script', pin['script_id'], code_sha256=pin['code_sha256'],
                              execution_definition_ref=pin.get('execution_definition_ref') or None)
    if (record is None or record.get('code_sha256') != pin['code_sha256']
            or pin.get('execution_definition_ref') and pin['execution_definition_ref'] != record.get('execution_definition_ref')):
        raise PinnedDefinitionError('ASSET_VERSION_NOT_FOUND')
    payload = json.loads(required_blob(owner_home, record.get('payload_blob')))
    if not isinstance(payload, dict):
        raise PinnedDefinitionError('ASSET_HISTORY_BLOB_MISSING')
    code = required_blob(owner_home, record.get('code_sha256'))
    for digest in record.get('side_blobs', {}).values():
        required_blob(owner_home, digest)
    if pin.get('source_scope', 'owner') != payload.get('source_scope', 'owner'):
        raise PinnedDefinitionError('ASSET_VERSION_NOT_FOUND')
    return record, payload, code


# LLM: 与实际执行共用精确版本和完整blob校验；任何不可用或损坏历史均不能由相同SHA的其它合同替代。
# 函数用途: 在创建或恢复步骤前判断原冻结脚本能否执行。
def historical_pin_available(owner_home, pin):
    try:
        script_snapshot(owner_home, pin)
    except (OSError, ValueError, TypeError, KeyError):
        return False
    return True


# LLM: 只使用workflow发布的编号/SHA/执行身份；同脚本每次解析只读一次，不能按SHA改绑其它合同。
# 函数用途: 恢复历史工作流、Markdown与以step_id为键的冻结脚本pins。
def resolve_definition(owner_home, workflow_id, version):
    if type(version) is not int or version < 1:
        raise ValueError('definition_version必须是正整数')
    record = get_version(owner_home, 'workflow', workflow_id, version)
    if record is None:
        raise PinnedDefinitionError('ASSET_VERSION_NOT_FOUND')
    value = json.loads(required_blob(owner_home, record.get('payload_blob')))
    definition = (WorkflowDefinition.from_any(value) if 'descriptor' in value else
                  WorkflowStore._decode_definition(value)).to_dict()
    side = record.get('side_blobs', {}).get('WORKFLOW.md')
    if side is not None:
        definition['body_markdown'] = required_blob(owner_home, side).decode('utf-8')
    pins = record.get('pins', {}).get('scripts', {})
    frozen, snapshots = {}, {}
    for step in definition['steps']:
        script_id = step.get('script_id')
        if not script_id:
            continue
        pin = pins.get(script_id)
        if not isinstance(pin, dict) or not pin.get('code_sha256'):
            raise PinnedDefinitionError('ASSET_VERSION_NOT_FOUND')
        if script_id not in snapshots:
            snapshots[script_id] = script_snapshot(owner_home, {**pin, 'script_id': script_id})
        script, script_payload, _ = snapshots[script_id]
        frozen[step['step_id']] = {'script_id': script_id, 'revision': script.get('storage_revision'),
            'version': script['version'],
            'code_sha256': pin['code_sha256'], 'source_scope': script_payload.get('source_scope', 'owner'),
            'execution_definition_ref': pin.get('execution_definition_ref') or script.get('execution_definition_ref')}
    return definition, frozen


# LLM: 冻结当前或指定工作流定义的执行内容指纹，绝不向外暴露未固化的临时字段。
# 函数用途: 返回包含 execution_definition_ref 与标准化定义的不可变快照。
def freeze_execution_definition(owner_home, workflow_or_id):
    from ..assets.descriptor import execution_definition_ref
    from ..assets.workflow_store import WorkflowDefinition, WorkflowStore

    if isinstance(workflow_or_id, (WorkflowDefinition, dict)):
        definition = workflow_or_id.to_dict() if hasattr(workflow_or_id, 'to_dict') else dict(workflow_or_id)
        ref = execution_definition_ref(definition, kind='workflow')
        descriptor = definition.get('descriptor', {})
        return {
            'execution_definition_ref': ref,
            'workflow_id': descriptor.get('id', ''),
            'revision': descriptor.get('revision', 0),
            'definition': definition,
        }
    workflow_id = str(workflow_or_id)
    store = WorkflowStore(owner_home)
    asset = store.get_workflow(workflow_id)
    if asset is None:
        raise PinnedDefinitionError('ASSET_VERSION_NOT_FOUND')
    definition = asset.to_dict()
    ref = execution_definition_ref(definition, kind='workflow')
    return {
        'execution_definition_ref': ref,
        'workflow_id': asset.descriptor.id,
        'revision': getattr(asset.descriptor, 'revision', 0),
        'definition': definition,
    }


# LLM: 支持按整数版本或内容摘要 (digest) 校验并恢复工作流；不匹配时抛出 PinnedDefinitionError。
# 函数用途: 为子工作流与回放机制提供固定版本定义解析。
def resolve_pinned_definition(owner_home, workflow_id, pinned_ref=None, *, definition_version=None):
    from ..assets.descriptor import execution_definition_ref
    from ..assets.workflow_store import WorkflowStore

    target_version = definition_version
    if target_version is None and isinstance(pinned_ref, int):
        target_version = pinned_ref

    if target_version is not None:
        if type(target_version) is not int or target_version < 1:
            raise PinnedDefinitionError('ASSET_VERSION_NOT_FOUND')
        definition, frozen_pins = resolve_definition(owner_home, workflow_id, target_version)
    else:
        store = WorkflowStore(owner_home)
        asset = store.get_workflow(workflow_id)
        if asset is None:
            raise PinnedDefinitionError('ASSET_VERSION_NOT_FOUND')
        definition = asset.to_dict()
        frozen_pins = {}

    actual_ref = execution_definition_ref(definition, kind='workflow')
    if pinned_ref is not None and not isinstance(pinned_ref, int):
        expected = str(pinned_ref).strip()
        if expected.startswith('sha256:'):
            expected = expected[len('sha256:'):].strip()
        chk_ref = actual_ref
        if chk_ref.startswith('sha256:'):
            chk_ref = chk_ref[len('sha256:'):].strip()
        if expected != chk_ref:
            descriptor = definition.get('descriptor', {})
            rev = str(descriptor.get('revision', ''))
            if expected != rev:
                raise PinnedDefinitionError('SUBWORKFLOW_PIN_MISMATCH')

    return definition, frozen_pins
