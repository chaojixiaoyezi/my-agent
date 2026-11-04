"""资产提交后的历史旁账；不改变canonical写入或事务恢复语义。"""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from hashlib import sha256

from . import revision_history as history
from .version_diff import script_diff, single_line, workflow_diff

_VERSION_OPERATION = ContextVar('asset_version_operation', default=None)


# LLM: 上下文仅传展示摘要、恢复来源和提交回执，不授予写根或执行权限。
# 函数用途: 为一次工具保存建立隔离的版本回执上下文。
@contextmanager
def version_operation(*, summary_line='', restored_from=None):
    state = {'summary_line': single_line(summary_line), 'restored_from': restored_from, 'recording': {}}
    token = _VERSION_OPERATION.set(state)
    try:
        yield state
    finally:
        _VERSION_OPERATION.reset(token)


# LLM: 过滤只发生于历史快照；不修改传入payload或资产目录内容。
# 函数用途: 去掉宿主私有运行摘要凭据。
def _payload(payload):
    result = deepcopy(payload)
    if isinstance(result.get('metadata'), dict):
        for key in ('_run_summary_generation', '_run_summary_spool'):
            result['metadata'].pop(key, None)
    return result


# LLM: 只处理可写Owner脚本/工作流；global/admin及workspace都不产生Owner历史。
# 函数用途: 判定当前Store是否接版本钩子。
def _eligible(store):
    return (store.collection in ('scripts', 'workflows') and not store._read_only
            and not store.is_admin and not getattr(store, '_scope_workspace', ''))


# LLM: pre-image优先取同一事务已经冻结的旧文本；未修改叶才从当前Store读取。
# 函数用途: 构造脚本代码或工作流侧文件快照。
def _files(store, payload, texts):
    name = payload['entrypoint'] if store.collection == 'scripts' else 'WORKFLOW.md'
    text = texts[name] if name in texts else store._text(payload['id'], name)
    if store.collection == 'scripts':
        if text is None:
            raise FileNotFoundError('history script entrypoint missing')
        return text.encode('utf-8'), {}
    return None, {name: text} if text is not None else {}


# LLM: pin的版本和SHA来自同一不可变记录；空白等价保存复用正式快照而不组合错误身份。
# 函数用途: 冻结本版本的脚本依赖。
def _pins(store, payload):
    from .script_store import ScriptStore

    pins = {'scripts': {}, 'skills': {}, 'workflows': {}}
    if store.collection != 'workflows':
        return pins
    scripts = ScriptStore(store.owner_home)
    # 调用者已持根锁，直接锁内读取，避免重入flock。
    for script_id in dict.fromkeys(step.get('script_id') for step in payload.get('steps', []) if step.get('script_id')):
        current = scripts._get(script_id)
        if current is None:
            raise FileNotFoundError('referenced script missing')
        digest = sha256(current['code'].encode('utf-8')).hexdigest()
        definition = {key: value for key, value in current.items()
                      if key not in ('code', 'entrypoint_path', 'execution_definition_ref')}
        clean = _payload(definition)
        fingerprint = history.asset_fingerprint(payload=clean, code=current['code'].encode('utf-8'))
        record = history.find_version(store.owner_home, 'script', script_id, code_sha256=digest, fingerprint=fingerprint)
        if record is None:
            record = history.record_version(store.owner_home, 'script', script_id, payload=clean,
                code=current['code'].encode('utf-8'), storage_revision=current['revision'], legacy=True,
                execution_definition_ref=current['execution_definition_ref'], summary_line='旧资产快照')['record']
        pins['scripts'][script_id] = {'version': record['version'], 'code_sha256': record['code_sha256']}
    return pins


# LLM: 身份从快照本身计算，不把新定义的执行引用套到旧pre-image。
# 函数用途: 取得该脚本或工作流快照的既有执行身份。
def _reference(store, payload, code):
    from .descriptor import execution_definition_ref
    if store.collection == 'workflows':
        from .workflow_store import execution_definition_ref
    return execution_definition_ref(payload | ({'code': code.decode('utf-8')} if code is not None else {}))


# LLM: 任何历史故障仅影响旁账，绝不在canonical已经提交后抛出保存失败；下次提交会重新比较指纹。
# 函数用途: 在成功提交后记录旧pre-image和新正式版本。
def record_committed(store, payload, previous, updates):
    if not _eligible(store):
        return None
    state = _VERSION_OPERATION.get()
    try:
        kind, asset_id = store.collection[:-1], payload['id']
        before = json.loads(previous[store.filename]) if previous.get(store.filename) is not None else None
        latest = history.latest_version(store.owner_home, kind, asset_id)
        if latest is None and before is not None:
            code, sides = _files(store, before, previous)
            history.record_version(store.owner_home, kind, asset_id, payload=_payload(before), code=code,
                side_files=sides, storage_revision=before.get('revision', 0), legacy=True,
                execution_definition_ref=_reference(store, before, code), summary_line='旧资产快照')
        code, sides = _files(store, payload, updates)
        old_code, _ = _files(store, before, previous) if before is not None else (b'', {})
        summary = (script_diff(before or {}, payload, previous_code=old_code or b'', current_code=code or b'')
                   if kind == 'script' else workflow_diff(before or {}, payload))['summary_line']
        reference = _reference(store, payload, code)
        clean = _payload(payload)
        fingerprint = history.asset_fingerprint(payload=clean, code=code, side_files=sides)
        latest = history.latest_version(store.owner_home, kind, asset_id)
        # 去重先于依赖读取，避免摘要更新或相同保存因依赖暂缺而产生噪音。
        pins = latest['pins'] if latest is not None and latest['fingerprint'] == fingerprint else _pins(store, payload)
        result = history.record_version(store.owner_home, kind, asset_id, payload=clean, code=code,
            side_files=sides, storage_revision=payload['revision'], execution_definition_ref=reference, pins=pins,
            summary_line=(state or {}).get('summary_line') or summary,
            restored_from=(state or {}).get('restored_from'))
        receipt = {'recorded': True, 'created': result['created'], 'version': result['version']}
    except Exception as exc:
        logging.getLogger(__name__).warning('asset version recording failed: %s', type(exc).__name__)
        receipt = {'recorded': False, 'error_code': getattr(exc, 'code', 'TOOL_EXECUTION_FAILED')}
    if state is not None:
        state['recording'] = receipt
    return receipt


# LLM: 删除之后仅给独立索引打时间标记；历史错误不恢复已经删除的canonical资产。
# 函数用途: 保留被删除资产的历史及deleted_at。
def record_deleted(store, asset_id):
    if not _eligible(store):
        return
    try:
        history.mark_deleted(store.owner_home, store.collection[:-1], asset_id, deleted_at=history._iso_time())
        receipt = {'recorded': True, 'deleted': True}
    except Exception as exc:
        logging.getLogger(__name__).warning('asset deletion history failed: %s', type(exc).__name__)
        receipt = {'recorded': False, 'error_code': getattr(exc, 'code', 'TOOL_EXECUTION_FAILED')}
    state = _VERSION_OPERATION.get()
    if state is not None:
        state['recording'] = receipt
    return receipt
