"""角色提示词只改加载器读取的 owner JSON；历史只是有界摘要，不是另一份覆盖库。"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path

from ..common.json_io import locked_json_path
from ..common.nofollow_fs import read_bytes_beneath, unlink_file_beneath, write_bytes_atomic_beneath
from ..plugin_host.errors import HostAPIError
from ..subagents.role_templates import (
    RoleTemplateRegistration,
    RoleTemplateStore,
    _register_template,
    load_role_template_store,
    role_template_detail_text,
    valid_role_template_id,
)
from .v13_store import _page_rows, v13_store_for


def _access(call):
    from ..gateway_parts.business_api_v13 import v13_require_access
    return v13_require_access(call)


def _limit(name):
    """W0 尚未合入时只用已批准的合同默认值，存在模块的内部导入错误不能被吞掉。"""
    module_name = __package__ + '.gap158_limits'
    try:
        limits = importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        if error.name != module_name:
            raise
        return {'AGENT_PROMPT_HISTORY_MAX_PER_ROLE': 20, 'PROMPT_TRIGGERS_MAX_PER_ROLE': 32}[name]
    return getattr(limits, name)


def _role_id(value):
    if not valid_role_template_id(value):
        raise HostAPIError('INVALID_SCHEMA')
    return value.lower().replace('-', '_')


def role_directories(call):
    """读取实际 manager 的目录；冷宿主复用 manager 的同一个目录规范化函数。"""
    _access(call)
    host = call.context.host
    manager = getattr(host, 'subagents', None)
    if manager is not None and getattr(manager, 'role_template_dirs', None) is not None:
        directories = list(map(Path, manager.role_template_dirs))
    else:
        from ..subagents.manager import _normalized_template_dirs
        root = getattr(host, 'effective_workspace_root', None)
        if root is None:
            root = getattr(host.home_paths, 'owner_home_dir', call.owner_home)
        directories = _normalized_template_dirs(Path(root), getattr(host.config, 'subagent_role_template_dirs', None))
    owners = Path(host.home_paths.owners_dir).resolve()
    owner = Path(call.owner_home).resolve()
    # manager 的 resolve 会消去原链接形状；先按 canonical owner 边界排除别人的目录再加载。
    return [directory for directory in directories
            if not directory.resolve().is_relative_to(owners) or directory.resolve().is_relative_to(owner)]


def _writer_directory(call, directories):
    owner = Path(call.owner_home).resolve()
    for raw in directories:
        directory = Path(raw).absolute()
        if not directory.resolve().is_relative_to(owner):
            continue
        # 不把指向别处的目录链接当成本 owner 可写层；既有下层仍然可以读取。
        current = directory
        safe = True
        while current != owner:
            if current.is_symlink():
                safe = False
                break
            if not current.is_relative_to(owner):
                safe = False
                break
            current = current.parent
        ancestor = directory
        while not ancestor.exists() and ancestor != owner:
            ancestor = ancestor.parent
        if safe and ancestor.is_dir() and os.access(ancestor, os.W_OK):
            return directory
    raise HostAPIError('STATE_CONFLICT', 409, message='角色加载器没有可写的本人目录。')


@dataclass(frozen=True)
class RoleState:
    role: str
    directories: list[Path]
    path: Path
    raw: bytes | None
    store: RoleTemplateStore
    writable: bool = True

    @property
    def sha256(self):
        if not self.writable:
            return ''
        return 'absent' if self.raw is None else hashlib.sha256(self.raw).hexdigest()

    @property
    def template(self):
        return self.store.templates[self.role]


def role_state(call, value, *, require_writer=False, _attempt=0):
    _access(call)
    role = _role_id(value)
    directories = role_directories(call)
    store = load_role_template_store(directories)
    if role not in store.templates:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    try:
        directory = _writer_directory(call, directories)
    except HostAPIError:
        if require_writer:
            raise
        return RoleState(role, directories, Path(store.templates[role].source_path), None, store, False)
    path = directory / (role + '.json')
    # loader 允许文件名和 id 不同；编辑同目录的单角色源文件而不是制造会被它遮蔽的副本。
    source = store.templates[role]
    if source.source == 'user' and Path(source.source_path).parent == directory:
        candidate = Path(source.source_path)
        if candidate.suffix == '.json' and not candidate.name.startswith('.'):
            path = candidate
    try:
        raw = read_bytes_beneath(call.owner_home, path.relative_to(call.owner_home).parts)
    except (OSError, ValueError) as error:
        if require_writer:
            raise HostAPIError('INVALID_SCHEMA', message='角色覆盖路径不是安全的本人普通文件。') from error
        return RoleState(role, directories, path, None, store, False)
    observed = 'absent' if raw is None else hashlib.sha256(raw).hexdigest()
    if observed != store.source_hashes.get(str(path), 'absent'):
        # 展示正文和 CAS 摘要必须来自同一文件版本；只在确实变化时有限重读。
        if _attempt < 2:
            return role_state(call, value, require_writer=require_writer, _attempt=_attempt + 1)
        raise HostAPIError('HOST_NOT_READY', 503, message='角色文件持续变化，请重新读取当前版本。')
    return RoleState(role, directories, path, raw, store)


def _payload(state):
    if state.raw is None:
        # 新建软覆盖明确继承原角色能力，不能因为改提示词意外取消原能力。
        return {'id': state.role, 'inherit_capabilities': True}, None
    try:
        document = json.loads(state.raw)
    except (ValueError, UnicodeError) as error:
        raise HostAPIError('INVALID_SCHEMA', message='本人角色文件损坏，请先修复原文件。') from error
    if type(document) is not dict:
        raise HostAPIError('INVALID_SCHEMA')
    if isinstance(document.get('templates'), list):
        matches = [item for item in document['templates'] if isinstance(item, dict)
                   and valid_role_template_id(item.get('id')) and _role_id(item['id']) == state.role]
        if len(matches) != 1:
            raise HostAPIError('INVALID_SCHEMA')
        return matches[0], document
    if _role_id(document.get('id')) != state.role:
        raise HostAPIError('STATE_CONFLICT', 409)
    return document, None


def _prompt_result(state):
    item = state.template
    row = {'role': state.role, 'prompt': item.prompt_zh, 'wrapup': item.output_contract_zh,
           'source': item.source, 'sha256': state.sha256}
    if len(str(state.path)) <= 512:
        row['layer_path'] = str(state.path)
    return {'prompt': row}


def agent_prompts_get(call):
    return _prompt_result(role_state(call, call.params['role']))


def _expected(call):
    expected = call.params.get('expected_sha256')
    if expected != 'absent' and (not isinstance(expected, str) or re.fullmatch('[0-9a-f]{64}', expected) is None):
        raise HostAPIError('INVALID_SCHEMA')
    return expected


def edit_role(call, mutate, *, kind=None, result=None, request_params=None):
    """同一文件锁内重读摘要和原 JSON，既不丢兄弟字段，也不让并发旧摘要交错覆盖。"""
    role = _role_id(call.params.get('role', call.params.get('key')))
    expected = _expected(call)
    state = role_state(call, role, require_writer=True)
    if state.template.source == 'user' and Path(state.template.source_path) != state.path:
        raise HostAPIError('REVISION_CONFLICT', 409, message='首个可写层被后续角色层遮蔽。')
    original_params = call.params if request_params is None else request_params
    intent = hashlib.sha256(json.dumps([call.owner_id, call.subject_id, call.action,
        call.idempotency_key, original_params], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    attempted = False

    def apply():
        nonlocal attempted
        with locked_json_path(state.path):
            fresh = role_state(call, role, require_writer=True)
            if fresh.template.source == 'user' and Path(fresh.template.source_path) != fresh.path:
                raise HostAPIError('REVISION_CONFLICT', 409, message='首个可写层被后续角色层遮蔽，不能提交无效覆盖。')
            payload, document = _payload(fresh)
            receipt = payload.get('edit_receipt', {})
            # 文件已发表而历史提交失败时，同键重试复用这次结果，不再创建第二个触发器。
            if kind is not None and isinstance(receipt, dict) and receipt.get('intent') == intent:
                attempted = True
                return fresh, receipt['extra']
            if fresh.path != state.path or fresh.sha256 != expected:
                raise HostAPIError('REVISION_CONFLICT', 409)
            extra = mutate(payload, fresh) or {}
            remove = kind == 'reset' and set(payload) <= {'id', 'edit_receipt'} and document is None
            candidate = load_role_template_store(fresh.directories, exclude_paths=(fresh.path,))
            if not remove:
                _register_template(candidate.templates, candidate.issues, payload,
                                   registration=RoleTemplateRegistration('user', str(fresh.path)))
                if any(issue.source_path == str(fresh.path) for issue in candidate.issues):
                    raise HostAPIError('INVALID_SCHEMA', message='角色候选不符合原加载器合同。')
            if kind is not None:
                payload['edit_receipt'] = {'intent': intent, 'kind': kind, 'at': call.clock(),
                                           'owner_id': call.owner_id, 'extra': extra}
            _access(call)
            content = json.dumps(document if document is not None else payload, ensure_ascii=False,
                                 indent=2, sort_keys=True, allow_nan=False) + '\n'
            attempted = True
            parts = state.path.relative_to(call.owner_home).parts
            if remove:
                unlink_file_beneath(call.owner_home, parts)
            else:
                write_bytes_atomic_beneath(call.owner_home, parts, content.encode('utf-8'))
            updated = role_state(call, role)
            return updated, extra

    if kind is None:
        updated, extra = apply()
        return result(updated, extra) if result else _prompt_result(updated)
    store = v13_store_for(call, 'owner')
    fingerprint = hashlib.sha256(json.dumps(original_params, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    recoveries = {}

    def preserve_before_delete(records, current):
        """删除之前封存旧文件回执与摘要；未知的上一笔不能随物理文件消失。"""
        payload, _document = _payload(current)
        receipt = payload.get('edit_receipt', {})
        if not isinstance(receipt, dict) or receipt.get('owner_id') != call.owner_id or not receipt.get('intent'):
            return
        if receipt.get('kind') in {'set', 'reset'}:
            response = _prompt_result(current)
        elif receipt.get('kind') == 'wrapup':
            response = _wrapup_result(current)
        elif receipt.get('kind') == 'visibility':
            response = {'item': _agent_visibility(current)}
        else:
            response = {**receipt.get('extra', {}), 'sha256': current.sha256}
        recoveries['role-edit-' + receipt['intent']] = {'result': response}
        previous = records.get(role, {'owner_id': call.owner_id, 'role': role, 'versions': []})
        versions = list(previous.get('versions', []))
        if (receipt.get('kind') in {'set', 'reset', 'wrapup', 'trigger'}
                and current.sha256 not in {row['sha256'] for row in versions}):
            versions.append({'version': versions[-1]['version'] + 1 if versions else 1,
                             'sha256': current.sha256, 'at': receipt['at'], 'kind': receipt['kind'],
                             'prompt_preview': current.template.prompt_zh[:500]})
        previous['versions'] = versions[-_limit('AGENT_PROMPT_HISTORY_MAX_PER_ROLE'):]
        records[role] = previous

    def settle_reset(records):
        """删除的意图先持久绑定到原集合；后续编辑必须先结清，不靠已删文件留回执。"""
        nonlocal attempted
        previous = records.get(role, {})
        pending = previous.get('pending_reset')
        if not pending:
            return None
        if previous.get('owner_id') != call.owner_id or pending.get('owner_id') != call.owner_id:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        target = Path(call.owner_home).joinpath(*pending['parts'])
        with locked_json_path(target):
            raw = read_bytes_beneath(call.owner_home, pending['parts'])
            if raw is not None and hashlib.sha256(raw).hexdigest() != pending['expected_sha256']:
                raise HostAPIError('OUTCOME_UNKNOWN', 503, 'unknown', '待结算的角色文件已被外部修改，请核对原操作。')
            _access(call)
            attempted = True
            if raw is not None:
                unlink_file_beneath(call.owner_home, pending['parts'])
            updated = role_state(call, role)
            response = _prompt_result(updated)
            versions = list(previous.get('versions', []))
            versions.append({'version': versions[-1]['version'] + 1 if versions else 1,
                             'sha256': hashlib.sha256(b'').hexdigest(), 'at': pending['at'],
                             'kind': 'reset', 'prompt_preview': updated.template.prompt_zh[:500]})
            previous.pop('pending_reset')
            previous['versions'] = versions[-_limit('AGENT_PROMPT_HISTORY_MAX_PER_ROLE'):]
            records[role] = previous
            recoveries['role-edit-' + pending['intent']] = {'result': response}
            return pending['intent'], response

    def reserve(records):
        # 同键与参数指纹先在原 V13 回执中绑定；文件发布后的 unknown 也不能同键异参。
        settle_reset(records)
        if kind == 'reset':
            with locked_json_path(state.path):
                fresh = role_state(call, role, require_writer=True)
                if fresh.path != state.path or fresh.sha256 != expected:
                    raise HostAPIError('REVISION_CONFLICT', 409)
                preserve_before_delete(records, fresh)
                payload, document = _payload(fresh)
                mutate(payload, fresh)
                if document is None and set(payload) <= {'id', 'edit_receipt'}:
                    previous = records.get(role, {'owner_id': call.owner_id, 'role': role, 'versions': []})
                    previous['pending_reset'] = {'owner_id': call.owner_id, 'intent': intent,
                        'parts': list(fresh.path.relative_to(call.owner_home).parts),
                        'expected_sha256': fresh.sha256, 'at': call.clock()}
                    records[role] = previous
        return {'bound': True}

    def save(records):
        settled = settle_reset(records)
        if settled is not None and settled[0] == intent:
            return settled[1]
        previous = records.get(role, {})
        if previous.get('owner_id', call.owner_id) != call.owner_id:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        versions = list(previous.get('versions', []))
        before = role_state(call, role)
        before_payload, _document = _payload(before)
        receipt = before_payload.get('edit_receipt', {})
        if isinstance(receipt, dict) and receipt.get('owner_id') == call.owner_id and receipt.get('intent'):
            if receipt.get('kind') in {'set', 'reset'}:
                response = _prompt_result(before)
            elif receipt.get('kind') == 'wrapup':
                response = _wrapup_result(before)
            elif receipt.get('kind') == 'visibility':
                response = {'item': _agent_visibility(before)}
            else:
                response = {**receipt.get('extra', {}), 'sha256': before.sha256}
            recoveries['role-edit-' + receipt['intent']] = {'result': response}

        def record(current):
            payload, _document = _payload(current)
            receipt = payload.get('edit_receipt', {})
            if (not isinstance(receipt, dict) or receipt.get('owner_id') != call.owner_id
                    or receipt.get('kind') not in {'set', 'reset', 'wrapup', 'trigger'}
                    or current.sha256 in {row['sha256'] for row in versions}):
                return
            version = versions[-1]['version'] + 1 if versions else 1
            versions.append({'version': version, 'sha256': current.sha256, 'at': receipt['at'],
                             'kind': receipt['kind'], 'prompt_preview': current.template.prompt_zh[:500]})

        # 下一次真实写入可补记上次已发表的文件摘要；不保存提示词全文或建立平行覆盖库。
        record(before)
        updated, extra = apply()
        record(updated)
        if kind == 'reset' and updated.raw is None:
            versions.append({'version': versions[-1]['version'] + 1 if versions else 1,
                             'sha256': hashlib.sha256(b'').hexdigest(), 'at': call.clock(),
                             'kind': kind, 'prompt_preview': updated.template.prompt_zh[:500]})
        records[role] = {'owner_id': call.owner_id, 'role': role,
                         'versions': versions[-_limit('AGENT_PROMPT_HISTORY_MAX_PER_ROLE'):]}
        return result(updated, extra) if result else _prompt_result(updated)

    def metadata_update(metadata, response):
        for name, fact in recoveries.items():
            if metadata.fact(name) is None:
                metadata.seal_fact(name, fact)

    try:
        binding = hashlib.sha256(json.dumps([call.owner_id, call.subject_id, call.action,
                                            call.idempotency_key]).encode()).hexdigest()
        store.transaction('agent_prompt_history', reserve, idempotency_key='prepare-' + binding,
                          params_digest=fingerprint, metadata_update=metadata_update)
        # 原文件回执会被后续编辑替换；逐键事实只保存已核回执，不用于加载角色。
        recovered = store._rows('agent_prompt_history').get('metadata', 'role-edit-' + intent)
        if recovered is not None:
            _access(call)
            return recovered['result']
        return store.transaction('agent_prompt_history', save, idempotency_key=call.idempotency_key,
                                 params_digest=fingerprint, metadata_update=metadata_update)
    except Exception as error:
        if attempted:
            raise HostAPIError('OUTCOME_UNKNOWN', 503, 'unknown', '角色文件可能已发表，请先查询现状并核对原操作。') from error
        raise


def agent_prompts_set(call):
    def mutate(payload, state):
        payload['prompt_zh'] = call.params['prompt']
    return edit_role(call, mutate, kind='set')


def agent_prompts_reset(call):
    def mutate(payload, state):
        for field in ('prompt_zh', 'output_contract_zh', 'use_when_zh', 'do_not_use_when_zh', 'trigger_ids', 'edit_receipt'):
            payload.pop(field, None)
    return edit_role(call, mutate, kind='reset')


def agent_prompts_history(call):
    _access(call)
    role = _role_id(call.params['role'])
    store = v13_store_for(call, 'owner')
    row = store.get('agent_prompt_history', role)
    if row is not None and row.get('owner_id') != call.owner_id:
        row = None
    versions = list(reversed(row.get('versions', []))) if row else []
    page = _page_rows(versions, [call.owner_id, call.subject_id, 'agent_prompt_history', role, versions],
                      cursor=call.params.get('cursor'), limit=call.params.get('limit', 50))
    if page['next_cursor'] is None:
        page.pop('next_cursor')
    return page


def agent_prompts_variables(call):
    _access(call)
    # 当前角色 prompt 是原样片段，不声称存在运行时未实现的变量插值。
    return {'variables': [], 'editable_fields': ['prompt_zh', 'output_contract_zh', 'use_when_zh', 'do_not_use_when_zh', 'disabled']}


def agent_prompts_preview(call):
    state = role_state(call, call.params['role'])
    store = deepcopy(state.store)
    item = state.template
    candidate = replace(item, prompt_zh=call.params.get('prompt', item.prompt_zh),
                        output_contract_zh=call.params.get('wrapup', item.output_contract_zh), disabled=False)
    store.templates[state.role] = candidate
    return {'role': state.role, 'rendered_detail': role_template_detail_text(roles=[state.role], store=store),
            'effective_source': item.source, 'warnings': [issue.message[:256] for issue in store.issues[:16]]}


def _wrapup_result(state, extra=None):
    return {'role': state.role, 'wrapup': state.template.output_contract_zh,
            'source': state.template.source, 'sha256': state.sha256}


def agent_prompts_wrapup_get(call):
    return _wrapup_result(role_state(call, call.params['role']))


def agent_prompts_wrapup_set(call):
    def mutate(payload, state):
        payload['output_contract_zh'] = call.params['wrapup']
    return edit_role(call, mutate, kind='wrapup', result=_wrapup_result)


def _trigger_rows(state, payload=None):
    rows = []
    for kind, field in (('use_when', 'use_when_zh'), ('do_not_use_when', 'do_not_use_when_zh')):
        texts = getattr(state.template, field)
        for text in texts:
            identifier = hashlib.sha256(f'{state.role}|{kind}|{text}'.encode()).hexdigest()[:16]
            rows.append({'trigger_id': identifier, 'role': state.role, 'kind': kind, 'text': text})
    if len({row['trigger_id'] for row in rows}) != len(rows):
        raise HostAPIError('INVALID_SCHEMA')
    return rows


def agent_prompts_triggers_list(call):
    state = role_state(call, call.params['role'])
    return {'items': _trigger_rows(state), 'sha256': state.sha256}


def _save_trigger_rows(payload, rows):
    # 编号按合同由 role|kind|text 推导；重载和删除前项不会改变其余条目的编号。
    payload.pop('trigger_ids', None)
    for kind, field in (('use_when', 'use_when_zh'), ('do_not_use_when', 'do_not_use_when_zh')):
        selected = [row for row in rows if row['kind'] == kind]
        payload[field] = [row['text'] for row in selected]


def agent_prompts_triggers_create(call):
    text = call.params['text'].strip()
    if not text:
        raise HostAPIError('INVALID_SCHEMA', message='触发条件不能只有空白。')

    def mutate(payload, state):
        rows = _trigger_rows(state, payload)
        existing = next((row for row in rows if row['kind'] == call.params['kind']
                         and row['text'] == text), None)
        if existing is not None:
            return {'trigger': existing}
        if len(rows) >= _limit('PROMPT_TRIGGERS_MAX_PER_ROLE'):
            raise HostAPIError('LIMIT_EXCEEDED')
        identifier = hashlib.sha256(f"{state.role}|{call.params['kind']}|{text}".encode()).hexdigest()[:16]
        row = {'trigger_id': identifier, 'role': state.role,
               'kind': call.params['kind'], 'text': text}
        rows.append(row)
        _save_trigger_rows(payload, rows)
        return {'trigger': row}

    return edit_role(call, mutate, kind='trigger',
                     result=lambda state, extra: {**extra, 'sha256': state.sha256})


def agent_prompts_triggers_update(call):
    text = call.params['text'].strip()
    if not text:
        raise HostAPIError('INVALID_SCHEMA', message='触发条件不能只有空白。')

    def mutate(payload, state):
        rows = _trigger_rows(state, payload)
        row = next((row for row in rows if row['trigger_id'] == call.params['trigger_id']), None)
        if row is None:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        row['text'] = text
        row['trigger_id'] = hashlib.sha256(f"{state.role}|{row['kind']}|{row['text']}".encode()).hexdigest()[:16]
        if len({item['trigger_id'] for item in rows}) != len(rows):
            raise HostAPIError('INVALID_SCHEMA')
        _save_trigger_rows(payload, rows)
        return {'trigger': row}

    return edit_role(call, mutate, kind='trigger',
                     result=lambda state, extra: {**extra, 'sha256': state.sha256})


def agent_prompts_triggers_delete(call):
    def mutate(payload, state):
        rows = _trigger_rows(state, payload)
        remaining = [row for row in rows if row['trigger_id'] != call.params['trigger_id']]
        if len(rows) == len(remaining):
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        _save_trigger_rows(payload, remaining)
        return {'deleted': True}

    return edit_role(call, mutate, kind='trigger',
                     result=lambda state, extra: {**extra, 'sha256': state.sha256})


def visibility_agent_get(call):
    _access(call)
    key = call.params.get('key')
    directories = role_directories(call)
    store = load_role_template_store(directories)
    if key:
        role = _role_id(key)
        if role not in store.templates:
            return {'items': []}
        state = role_state(call, role)
        return {'items': [_agent_visibility(state)]}
    if len(store.templates) > 100:
        raise HostAPIError('HOST_NOT_READY', 503, message='角色数量超过此入口上限，请按 key 查询。')
    return {'items': [{'key': role, 'visible': not item.disabled, 'source': item.source}
                      for role, item in sorted(store.templates.items())]}


def _agent_visibility(state, extra=None):
    item = state.template
    return {'key': state.role, 'visible': not item.disabled, 'source': item.source, 'sha256': state.sha256}


def visibility_agent_set(call):
    _access(call)
    state = role_state(call, call.params['key'], require_writer=True)
    params = {**call.params, 'expected_sha256': call.params.get('expected_sha256') or state.sha256}

    def mutate(payload, fresh):
        deployment = [directory for directory in fresh.directories
                      if not directory.resolve().is_relative_to(Path(call.owner_home).resolve())]
        lower = load_role_template_store(deployment).templates.get(state.role)
        if call.params['visible'] and lower is not None and lower.disabled:
            raise HostAPIError('STATE_CONFLICT', 409, message='部署层禁用的角色不能由本人覆盖启用。')
        payload['disabled'] = not call.params['visible']

    return edit_role(replace(call, params=params), mutate, kind='visibility',
                     result=lambda current, extra: {'item': _agent_visibility(current)}, request_params=call.params)
