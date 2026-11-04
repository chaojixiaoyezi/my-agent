"""从W1发布历史恢复定义及冻结脚本引用，绝不回退当前可编辑资产。"""

import json

from ..assets.revision_history import blob_exists, blob_get, find_version, get_version
from ..assets.workflow_store import WorkflowDefinition, WorkflowStore


class PinnedDefinitionError(ValueError):
    """向引擎提供无执行副作用的结构化版本读取错误。"""

    # LLM: 沿用W1既有错误码，不把缺失历史伪装成当前版本。
    # 函数用途: 保存精确版本错误供公开入口返回。
    def __init__(self, code):
        self.error_code = code
        super().__init__(code)


# LLM: 只读取内容寻址blob，缺失时显式报错，不读取工作流当前文件。
# 函数用途: 获取历史快照字节。
def required_blob(owner_home, sha):
    value = blob_get(owner_home, sha) if isinstance(sha, str) else None
    if value is None:
        raise PinnedDefinitionError('ASSET_HISTORY_BLOB_MISSING')
    return value


# LLM: 必须精确匹配代码摘要和已有执行身份；检查完整必要blob集合，不只看索引行。
# 函数用途: 核实一个冻结脚本仍可由W1历史执行。
def historical_pin_available(owner_home, pin):
    if not isinstance(pin, dict) or not pin.get('script_id') or not pin.get('code_sha256'):
        return False
    record = find_version(owner_home, 'script', pin['script_id'], code_sha256=pin['code_sha256'],
                          execution_definition_ref=pin.get('execution_definition_ref') or None)
    if record is None:
        return False
    blobs = [record.get('payload_blob'), record.get('code_sha256'), *record.get('side_blobs', {}).values()]
    return all(isinstance(sha, str) and blob_exists(owner_home, sha) for sha in blobs)


# LLM: 只使用workflow版本的pins.scripts；version号是展示数据，脚本解析以code_sha256为身份。
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
    frozen = {}
    for step in definition['steps']:
        script_id = step.get('script_id')
        if not script_id:
            continue
        pin = pins.get(script_id)
        if not isinstance(pin, dict) or not pin.get('code_sha256'):
            raise PinnedDefinitionError('ASSET_VERSION_NOT_FOUND')
        script = find_version(owner_home, 'script', script_id, code_sha256=pin['code_sha256'],
                              execution_definition_ref=pin.get('execution_definition_ref') or None)
        if script is None:
            raise PinnedDefinitionError('ASSET_VERSION_NOT_FOUND')
        script_payload = json.loads(required_blob(owner_home, script.get('payload_blob')))
        frozen[step['step_id']] = {'script_id': script_id, 'revision': script.get('storage_revision'),
            'code_sha256': pin['code_sha256'], 'source_scope': script_payload.get('source_scope', 'owner'),
            'execution_definition_ref': pin.get('execution_definition_ref') or script.get('execution_definition_ref')}
    return definition, frozen
