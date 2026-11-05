"""资产提交旁账与待发表证据；canonical仍是唯一可编辑资产权威。"""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

from . import revision_history as history
from .version_diff import script_diff, single_line, workflow_diff

_VERSION_OPERATION = ContextVar('asset_version_operation', default=None)


# LLM: 上下文只接受宿主验证的历史快照，不从模型metadata接受依赖覆盖；不授予写根或执行权限。
# 函数用途: 为一次工具保存建立隔离的版本回执上下文。
@contextmanager
def version_operation(*, summary_line='', restored_from=None):
    state = {'summary_line': single_line(summary_line), 'restored_from': restored_from, 'recording': {}}
    token = _VERSION_OPERATION.set(state)
    try:
        yield state
    finally:
        _VERSION_OPERATION.reset(token)


# LLM: 入口只接load_snapshot选定记录，复核原记录和全部依赖blob；覆盖仅在同Owner同资产保存栈内有效。
# 函数用途: 将选中发布记录的完整依赖带到原Store提交钩子。
@contextmanager
def restoring_snapshot(owner_home, record):
    selected = history.get_version(owner_home, 'workflow', record['asset_id'], record['version'])
    if selected != record or record.get('kind') != 'workflow':
        raise history.AssetHistoryError('ASSET_VERSION_CONFLICT')
    payload = _version_payload(owner_home, record)
    pins = deepcopy(record.get('pins'))
    if not isinstance(pins, dict) or any(not isinstance(pins.get(key), dict) for key in ('scripts', 'skills', 'workflows')):
        raise history.AssetHistoryError('ASSET_VERSION_NOT_FOUND')
    for group, kind in (('scripts', 'script'), ('skills', 'skill'), ('workflows', 'workflow')):
        for asset_id, pin in pins[group].items():
            if not isinstance(pin, dict) or type(pin.get('version')) is not int:
                raise history.AssetHistoryError('ASSET_VERSION_NOT_FOUND')
            dependency = history.get_version(owner_home, kind, asset_id, pin['version'])
            _version_payload(owner_home, dependency)
            if kind == 'script' and any(pin.get(key) != dependency.get(key)
                    for key in ('code_sha256', 'execution_definition_ref')):
                raise history.AssetHistoryError('ASSET_VERSION_CONFLICT')
    if any(step.get('script_id') and step['script_id'] not in pins['scripts'] for step in payload.get('steps', [])):
        raise history.AssetHistoryError('ASSET_VERSION_NOT_FOUND')
    state = _VERSION_OPERATION.get()
    if state is None:
        raise RuntimeError('restoration requires a version operation')
    previous = state.get('restoration')
    state['restoration'] = {'owner_home': str(Path(owner_home).absolute()), 'asset_id': record['asset_id'],
                            'pins': pins, 'version': record['version']}
    try:
        yield
    finally:
        if previous is None:
            state.pop('restoration', None)
        else:
            state['restoration'] = previous


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


# LLM: 只校验选中的不可变记录及其必要blob；不扫描历史目录，不读取当前资产代替历史。
# 函数用途: 验证发布依赖完整并取得其定义。
def _version_payload(owner_home, record):
    if record is None:
        raise history.AssetHistoryError('ASSET_VERSION_NOT_FOUND')
    raw = history.blob_get(owner_home, record.get('payload_blob'))
    hashes = [*record.get('side_blobs', {}).values()]
    if record['kind'] == 'script':
        hashes.append(record.get('code_sha256'))
    if raw is None or any(not isinstance(digest, str) or history.blob_get(owner_home, digest) is None for digest in hashes):
        raise history.AssetHistoryError('ASSET_HISTORY_BLOB_MISSING')
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError('invalid published payload')
    return payload


# LLM: 继承准确基础版本的全部pin；基础脚本必须仍有完整历史，不能改用当前代码或吞掉冲突。
# 函数用途: 为组合发布恢复基础依赖与基础步骤身份。
def _base_pins(store, payload):
    pins = {'scripts': {}, 'skills': {}, 'workflows': {}}
    base = payload.get('metadata', {}).get('base_workflow_pin')
    if base is None:
        return pins, {}
    if not isinstance(base, dict) or base.get('id') == payload['id']:
        raise ValueError('invalid base workflow pin')
    record = history.get_version(store.owner_home, 'workflow', base.get('id'), base.get('version'))
    definition = _version_payload(store.owner_home, record)
    for kind in pins:
        values = record.get('pins', {}).get(kind, {})
        if not isinstance(values, dict):
            raise ValueError('invalid published pins')
        pins[kind] = deepcopy(values)
    for script_id, pin in pins['scripts'].items():
        script = history.get_version(store.owner_home, 'script', script_id, pin.get('version'))
        _version_payload(store.owner_home, script)
        if (script['code_sha256'] != pin.get('code_sha256') or pin.get('execution_definition_ref')
                and pin['execution_definition_ref'] != script.get('execution_definition_ref')):
            raise history.AssetHistoryError('ASSET_VERSION_CONFLICT')
    for workflow_id, pin in pins['workflows'].items():
        _version_payload(store.owner_home, history.get_version(store.owner_home, 'workflow', workflow_id, pin.get('version')))
    base_pin = {'version': base['version']}
    if base['id'] in pins['workflows'] and pins['workflows'][base['id']] != base_pin:
        raise history.AssetHistoryError('ASSET_VERSION_CONFLICT')
    pins['workflows'][base['id']] = base_pin
    return pins, {step['step_id']: step['script_id'] for step in definition.get('steps', []) if step.get('script_id')}


# LLM: 调用者已持原资产根锁，直接_get不重入；编号、SHA、执行引用始终来自同一已提交记录。
# 函数用途: 冻结一个当前追加依赖，兼容无历史的旧脚本。
def _current_script_pin(store, scripts, script_id):
    current = scripts._get(script_id)
    if current is None:
        raise FileNotFoundError('referenced script missing')
    code = current['code'].encode('utf-8')
    definition = {key: value for key, value in current.items()
                  if key not in ('code', 'entrypoint_path', 'execution_definition_ref')}
    clean = _payload(definition)
    record = history.find_version(store.owner_home, 'script', script_id, code_sha256=sha256(code).hexdigest(),
                                  fingerprint=history.asset_fingerprint(payload=clean, code=code))
    if record is None:
        record = history.record_version(store.owner_home, 'script', script_id, payload=clean,
            code=code, storage_revision=current['revision'], legacy=True,
            execution_definition_ref=current['execution_definition_ref'], summary_line='旧资产快照')['record']
    _version_payload(store.owner_home, record)
    return {key: record[key] for key in ('version', 'code_sha256', 'execution_definition_ref')}


# LLM: 基础步骤沿历史pin，追加步骤才读当前脚本；同ID不同不可变身份在发布前拒绝，每个当前依赖仅读一次。
# 函数用途: 合并完整历史基础依赖和本次追加依赖，避免空pin或隐式换版。
def _pins(store, payload, *, use_restoration=True):
    from .script_store import ScriptStore

    if store.collection != 'workflows':
        return {'scripts': {}, 'skills': {}, 'workflows': {}}
    restoration = (_VERSION_OPERATION.get() or {}).get('restoration') if use_restoration else None
    if (restoration is not None and restoration['owner_home'] == str(store.owner_home.absolute())
            and restoration['asset_id'] == payload['id']):
        return deepcopy(restoration['pins'])
    pins, base_steps = _base_pins(store, payload)
    scripts, current = ScriptStore(store.owner_home), {}
    for step in payload.get('steps', []):
        script_id = step.get('script_id')
        if not script_id:
            continue
        if base_steps.get(step['step_id']) == script_id:
            if script_id not in pins['scripts']:
                raise history.AssetHistoryError('ASSET_VERSION_NOT_FOUND')
            continue
        if script_id not in current:
            current[script_id] = _current_script_pin(store, scripts, script_id)
        pin = current[script_id]
        previous = pins['scripts'].get(script_id)
        if previous is not None and any(previous.get(key) != pin[key] for key in ('version', 'code_sha256')):
            raise history.AssetHistoryError('ASSET_VERSION_CONFLICT')
        pins['scripts'].setdefault(script_id, pin)
    return pins


# LLM: 身份从快照本身计算，不把新定义的执行引用套到旧pre-image。
# 函数用途: 取得该脚本或工作流快照的既有执行身份。
def _reference(store, payload, code):
    from .descriptor import execution_definition_ref
    if store.collection == 'workflows':
        from .workflow_store import execution_definition_ref
    return execution_definition_ref(payload | ({'code': code.decode('utf-8')} if code is not None else {}))


# LLM: 指纹去重仍复用原历史语义，侧文件和原始代码额外保存在证据中，恢复不读后来依赖。
# 函数用途: 在canonical尚未销毁前构造一份完整、可重启的发表快照。
def _snapshot(store, payload, texts, *, legacy=False, summary=''):
    code, sides = _files(store, payload, texts)
    clean = _payload(payload)
    snapshot = {'payload': clean, 'code': code.decode('utf-8') if code is not None else None,
                'side_files': sides, 'storage_revision': payload.get('revision', 0), 'legacy': legacy,
                'execution_definition_ref': _reference(store, payload, code), 'summary_line': summary,
                'restored_from': None}
    latest = history.latest_version(store.owner_home, store.collection[:-1], payload['id'])
    state = _VERSION_OPERATION.get() or {}
    try:
        # 可信回滚优先于指纹去重，避免同正文不同pin被当前head替代。
        # 组合Store的publication_pins来自原宿主校验，不是描述符metadata。
        supplied = getattr(store, 'publication_pins', None) if not legacy else None
        snapshot['pins'] = (deepcopy(supplied) if supplied is not None else
            _pins(store, payload) if not legacy and state.get('restoration') else
            deepcopy(latest['pins']) if latest is not None and not latest.get('legacy')
            and latest['fingerprint'] == history.asset_fingerprint(payload=clean, code=code, side_files=sides)
            else _pins(store, payload, use_restoration=not legacy))
    except Exception as exc:
        # 不完整依赖不授权改用将来的版本；保留准确正文和失败事实，不清除证据。
        snapshot['error_code'] = getattr(exc, 'code', 'TOOL_EXECUTION_FAILED')
    if not legacy:
        snapshot['summary_line'] = state.get('summary_line') or summary
        snapshot['restored_from'] = state.get('restored_from')
    return snapshot


# LLM: 待发表证据不是可执行资产，只有committed或canonical精确匹配的prepared才到此；ACK前不删。
# 函数用途: 幂等发表已提交快照并确认对应义务。
def _publish(store, pending):
    snapshot = pending['snapshot']
    if snapshot.get('error_code'):
        raise history.AssetHistoryError(snapshot['error_code'])
    values = dict(snapshot)
    code = values.pop('code')
    result = history.record_version(store.owner_home, store.collection[:-1], pending['asset_id'],
        **values, code=code.encode('utf-8') if code is not None else None)
    history.acknowledge_publication(store.owner_home, store.collection[:-1], pending['asset_id'])
    return result


# LLM: 只比较本资产当前revision及全部快照字节，不凭fingerprint把未提交候选认领为正式内容。
# 函数用途: 判断prepared证据是否已在canonical事务结束后提交。
def _matches(store, snapshot, payload):
    if payload is None or payload.get('revision', 0) != snapshot['storage_revision']:
        return False
    code, sides = _files(store, payload, {})
    return (_payload(payload) == snapshot['payload'] and sides == snapshot['side_files']
            and (code.decode('utf-8') if code is not None else None) == snapshot['code'])


# LLM: 调用方持root/canonical锁且已恢复undo；旧证据未确认则拒绝销毁，prepared不匹配只代表未提交候选。
# 函数用途: 在覆盖或删除前补齐最后一个已提交快照，兼容无历史的旧资产。
def protect_preimage(store, payload):
    if not _eligible(store):
        return
    kind, asset_id = store.collection[:-1], payload['id'] if payload else None
    if asset_id is None:
        return
    pending = history.pending_publication(store.owner_home, kind, asset_id)
    if pending is not None:
        if pending['phase'] == 'prepared':
            if not _matches(store, pending['snapshot'], payload):
                history.acknowledge_publication(store.owner_home, kind, asset_id)
                pending = None
            else:
                pending = history.retain_publication(store.owner_home, kind, asset_id,
                    pending['snapshot'], committed=True)
        if pending is not None:
            _publish(store, pending)
    code, sides = _files(store, payload, {})
    fingerprint = history.asset_fingerprint(payload=_payload(payload), code=code, side_files=sides)
    if history.find_version(store.owner_home, kind, asset_id, fingerprint=fingerprint) is not None:
        return
    snapshot = _snapshot(store, payload, {}, legacy=True, summary='旧资产快照')
    pending = history.retain_publication(store.owner_home, kind, asset_id, snapshot, committed=True)
    _publish(store, pending)


# LLM: 在undo及canonical写入前保留候选，只有事务结束后的精确读回能将其转committed，不先发版本。
# 函数用途: 为本次提交冻结依赖、正文与侧文件，单次blob写失败后仍可恢复。
def prepare_commit(store, payload, previous, updates):
    if not _eligible(store):
        return
    before = json.loads(previous[store.filename]) if previous.get(store.filename) is not None else None
    code, _ = _files(store, payload, updates)
    old_code, _ = _files(store, before, previous) if before is not None else (b'', {})
    summary = (script_diff(before or {}, payload, previous_code=old_code or b'', current_code=code or b'')
               if store.collection == 'scripts' else workflow_diff(before or {}, payload))['summary_line']
    snapshot = _snapshot(store, payload, updates, summary=summary)
    history.retain_publication(store.owner_home, store.collection[:-1], payload['id'], snapshot)


# LLM: 任何发表故障不撤销已确认的canonical提交；快照证据留到不可变记录与索引都确认。
# 函数用途: 在真实提交结束后完成待发表证据，并向工具回报非致命失败。
def record_committed(store, payload, previous, updates):
    if not _eligible(store):
        return None
    state = _VERSION_OPERATION.get()
    try:
        kind, asset_id = store.collection[:-1], payload['id']
        pending = history.pending_publication(store.owner_home, kind, asset_id)
        if pending is None:
            prepare_commit(store, payload, previous, updates)
            pending = history.pending_publication(store.owner_home, kind, asset_id)
        if pending['phase'] != 'committed':
            if not _matches(store, pending['snapshot'], payload):
                raise history.AssetHistoryError('ASSET_VERSION_CONFLICT')
            pending = history.retain_publication(store.owner_home, kind, asset_id, pending['snapshot'], committed=True)
        result = _publish(store, pending)
        receipt = {'recorded': True, 'created': result['created'], 'version': result['version']}
    except Exception as exc:
        logging.getLogger(__name__).warning('asset version recording failed: %s', type(exc).__name__)
        receipt = {'recorded': False, 'error_code': getattr(exc, 'code', 'TOOL_EXECUTION_FAILED')}
    if state is not None:
        state['recording'] = receipt
    return receipt


# LLM: 兼容既有组合Store的record_version回执；只确认真实不可变记录与准确候选，不相信返回标志或模型metadata。
# 函数用途: 自定义宿主发布回调成功后确认共用快照，失败仍保留证据且不撤销canonical。
def acknowledge_callback(store, result):
    if not _eligible(store) or not isinstance(result, dict) or not isinstance(result.get('record'), dict):
        return
    try:
        record = result['record']
        kind, asset_id = store.collection[:-1], record['asset_id']
        if history.get_version(store.owner_home, kind, asset_id, record['version']) != record:
            return
        pending = history.pending_publication(store.owner_home, kind, asset_id)
        if pending is None:
            return
        snapshot = pending['snapshot']
        code = snapshot['code'].encode('utf-8') if snapshot['code'] is not None else None
        fingerprint = history.asset_fingerprint(payload=snapshot['payload'], code=code, side_files=snapshot['side_files'])
        if record['fingerprint'] == fingerprint and record['pins'] == snapshot.get('pins'):
            _version_payload(store.owner_home, record)
            history.acknowledge_publication(store.owner_home, kind, asset_id)
    except Exception as exc:
        logging.getLogger(__name__).warning('asset callback acknowledgment failed: %s', type(exc).__name__)


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
