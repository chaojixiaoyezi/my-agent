"""固定脚本的业务投影、原生存储 CRUD 与受控单次运行。

脚本正文只由 ScriptStore 持有；V13Store 保存运行关联与请求回执。
审批路径只派发原生空间助手任务，不签发审批、不以模型正文判定执行成功。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import uuid
from contextlib import ExitStack
from dataclasses import asdict, fields, replace
from datetime import datetime, timezone
from pathlib import Path

from ..assets.descriptor import AssetDescriptor
from ..assets.script_store import ScriptStore
from ..common.directory_lock import locked_private_directory
from ..common.log_redaction import redact_sensitive_value
from ..common.nofollow_fs import (
    list_names_beneath,
    open_directory_beneath,
    read_bytes_beneath,
    unlink_file_beneath,
    write_bytes_atomic_beneath,
)
from ..conversation.business_access import BusinessError
from ..plugin_host.contracts import DomainResult
from ..plugin_host.errors import HostAPIError
from ..plugin_host.schemas import canonical_json, thaw, validate_object
from ..tooling.input_schema import canonicalize_tool_input_schema, validate_tool_input
from ..tooling.runtime_contracts import tool_arguments_hash
from ..workflows.datasource_runtime import run_script_with_datasources
from ..workflows.script_runner import ScriptRunner
from .v13_store import v13_store_for

_PAGE_KEY = os.urandom(32)


# LLM: 分片被注册器反向导入，模块加载期不能再次装载注册器。
# 函数用途: 在实际调用阶段取得已经完成初始化的 W0 基础模块。
def _foundation():
    from ..gateway_parts import business_api_v13
    return business_api_v13


# LLM: 摘要只绑定结构化请求，不从文本内容取得权限。
# 函数用途: 生成稳定 JSON 参数指纹。
def digest(value):
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


# LLM: 时间只使用本次宿主注入的时钟，空值不伪造为当前时间。
# 函数用途: 将 epoch 或现有时间字符串转换为公开时间。
def timestamp(value):
    if value is None or isinstance(value, str):
        return value
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


# LLM: 显式核对空间后才使用宿主项目引用；正文中的路径绝不作为权限来源。
# 函数用途: 为脚本原生读口解析可选项目覆盖。
def script_store_for(call):
    _foundation().v13_require_access(call)
    workspace = None
    if call.params.get('space_id'):
        space = _foundation().v13_resolve_space(call, call.params['space_id'])
        project = space.get('default_project_ref')
        if project is not None:
            services = call.services
            actor = services.actor(call.context)
            services.spaces.reference('project', project, actor=actor, space_id=space['space_id'])
            row = services.spaces.reference_resolver('project', project, actor)
            workspace = row.get('workspace_root') if isinstance(row, dict) else None
            if (row is None or row.get('owner_id') != call.owner_id or row.get('ref') != project
                    or not isinstance(workspace, str) or not Path(workspace).is_absolute()):
                raise HostAPIError('HOST_NOT_READY', 503)
    return ScriptStore(call.owner_home, workspace_root=workspace)


# LLM: 元数据是脚本声明，不是执行授权；旧脚本仍按原 IO 输入集合合成闭合 schema。
# 函数用途: 返回固定脚本公开的输入 schema。
def script_input_schema(record):
    schema = record.get('metadata', {}).get('input_schema')
    if isinstance(schema, dict) and schema.get('type') == 'object':
        return thaw(schema)
    names = record.get('io_contract', {}).get('inputs', [])
    return {'type': 'object', 'properties': {key: {} for key in names},
            'required': list(names), 'additionalProperties': False}


# LLM: 只消费风险枚举和布尔声明；未声明风险时由显式资源引用决定。
# 函数用途: 投影脚本风险与审批要求。
def script_risk(record):
    meta = record.get('metadata', {})
    risk = meta.get('risk_level')
    if risk not in ('low', 'medium', 'high', 'critical'):
        risk = 'high' if record.get('access_targets') or record.get('credential_refs') else 'low'
    approval = meta.get('requires_approval')
    return risk, approval if type(approval) is bool else risk in ('high', 'critical')


# LLM: 只投影正式合同字段，运行摘要来自原生资产而非推测。
# 函数用途: 把 ScriptStore 行转换为列表或详情数据。
def project_script(record, *, detail=False):
    risk, approval = script_risk(record)
    runtime = record.get('runtime_summary', {})
    last_run = None
    if runtime.get('last_run_at') is not None:
        duration = runtime.get('last_duration_ms')
        last_run = {'status': runtime.get('last_status') or 'unknown',
            'duration_ms': None if duration is None else max(0, round(duration)),
            'run_at': timestamp(runtime.get('last_run_at')),
            'last_error': str(redact_sensitive_value(runtime.get('last_error', '')))[:1024] or None}
    contexts = record.get('metadata', {}).get('contexts', ['any'])
    if not isinstance(contexts, list) or any(item not in ('alert', 'case', 'host', 'identity', 'any') for item in contexts):
        contexts = ['any']
    result = {'script_id': record['id'], 'name': record['name'],
        'description': record.get('summary', ''), 'input_schema': script_input_schema(record),
        'risk_level': risk, 'requires_approval': approval, 'enabled': record.get('enabled', True),
        'tags': list(record.get('tags', [])), 'contexts': contexts,
        'scope_layer': record.get('scope_layer', 'owner'), 'revision': record.get('revision', 0),
        'last_run': last_run}
    if detail:
        result.update({'code': record['code'], 'entrypoint': record['entrypoint'],
            'interpreter': record['interpreter'], 'timeout_seconds': record['timeout_seconds'],
            'io_contract': record['io_contract'], 'data_sources': record.get('data_sources', [])})
    return redact_sensitive_value(result)


# LLM: 绑定 owner、主体、筛选和完整可见快照；游标偏移可读但不能由调用方改写。
# 函数用途: 为原生资产等非 V13 集合提供 HMAC 有界分页。
def page_records(call, rows, *, scope='scripts'):
    params = call.params
    limit = params.get('limit', 50)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise HostAPIError('INVALID_SCHEMA')
    binding = digest([str(call.owner_home), call.owner_id, call.subject_id, scope,
        {key: value for key, value in params.items() if key not in ('cursor', 'limit')}, rows])
    offset = 0
    if params.get('cursor') is not None:
        try:
            token = json.loads(base64.b64decode(params['cursor'], altchars=b'-_', validate=True))
            if (type(token) is not dict or set(token) != {'binding', 'offset', 'signature'}
                    or token['binding'] != binding or type(token['offset']) is not int
                    or not 0 <= token['offset'] <= len(rows)):
                raise ValueError('cursor')
            signature = hmac.digest(_PAGE_KEY, canonical_json([binding, token['offset']]).encode(), 'sha256').hex()
            if not hmac.compare_digest(signature, token['signature']):
                raise ValueError('cursor')
            offset = token['offset']
        except (ValueError, TypeError, KeyError, UnicodeError) as error:
            raise HostAPIError('CURSOR_EXPIRED', 503) from error
    end = min(offset + limit, len(rows))
    cursor = None
    if end < len(rows):
        signature = hmac.digest(_PAGE_KEY, canonical_json([binding, end]).encode(), 'sha256').hex()
        cursor = base64.urlsafe_b64encode(canonical_json(
            {'binding': binding, 'offset': end, 'signature': signature}).encode()).decode()
    return {'items': rows[offset:end], 'next_cursor': cursor,
            'coverage': 'partial' if cursor else 'complete'}


# LLM: 其它 owner 的资产不投影为可见条目，全局只读资产保留原生来源。
# 函数用途: 列出当前范围固定脚本并按结构化上下文和标签筛选。
def list_scripts(call):
    rows = []
    for record in script_store_for(call).list_scripts():
        if record.get('owner_id') not in ('', call.owner_id, None) and record.get('scope_layer') not in ('global', 'builtin'):
            continue
        item = project_script(record)
        if call.params.get('context') and call.params['context'] not in item['contexts'] and 'any' not in item['contexts']:
            continue
        if call.params.get('tag') and call.params['tag'] not in item['tags']:
            continue
        if 'enabled' in call.params and item['enabled'] != call.params['enabled']:
            continue
        rows.append(item)
    return page_records(call, sorted(rows, key=lambda item: item['script_id']))


# LLM: 文件路径安全由原 ScriptStore 负责，缺失或其它 owner 都使用相同错误。
# 函数用途: 读取并授权固定脚本原始记录。
def required_script(call, store, script_id):
    try:
        row = store.get_script(script_id)
    except ValueError as error:
        raise HostAPIError('INVALID_SCHEMA') from error
    if row is None:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    if row.get('scope_layer') not in ('global', 'builtin'):
        _foundation().v13_require_access(call, {**row, 'owner_id': row.get('owner_id') or call.owner_id})
    return row


# LLM: 详情正文只来自安全读副本，绝不返回 entrypoint_path 等宿主路径。
# 函数用途: 返回当前 owner 可见脚本详情。
def get_script(call):
    store = script_store_for(call)
    return {'script': project_script(required_script(call, store, call.params['script_id']), detail=True)}


class _NativeCASStore(ScriptStore):
    """只在原生写方法的首个锁内读取点复核记录，不替代原生写入链。"""

    # LLM: 回调仅绑定当前 V13Call，不能从参数提供任意授权函数。
    # 函数用途: 安装一次性原生写入末核。
    def arm(self, call, *, create=False, revision=None, identity=None):
        self._write_check = (call, create, revision, identity)

    # LLM: save/set_enabled/delete 的原锁内读口共用此校验；后续提交读回不再比较旧版本。
    # 函数用途: 在原生文件临界区核对存在性、owner 和记录 CAS。
    def _get(self, script_id):
        row = super()._get(script_id)
        check = getattr(self, '_write_check', None)
        if check is not None:
            self._write_check = None
            call, create, revision, identity = check
            _foundation().v13_require_access(call)
            if create:
                if row is not None:
                    raise HostAPIError('STATE_CONFLICT', 409)
            else:
                if row is None:
                    raise HostAPIError('RESOURCE_NOT_FOUND', 404)
                _foundation().v13_require_access(call, {**row, 'owner_id': row.get('owner_id') or call.owner_id})
                if row.get('revision', 0) != revision:
                    raise HostAPIError('REVISION_CONFLICT', 409)
                if identity is not None and _script_identity(self, row) != identity:
                    raise HostAPIError('REVISION_CONFLICT', 409)
        return row

    # LLM: 身份只来自已持久化的准备回执；调用者不能从请求参数指定或认领操作。
    # 函数用途: 将准确操作身份与本次原生提交阶段绑定。
    def bind_operation(self, operation, phase):
        self._config_operation = (operation, phase)

    # LLM: 操作证明与原生 metadata/code 进入同一 undo 事务；成功标记不能先于真实资产提交。
    # 函数用途: 通过原生 _write 原子保存资产与准确结果证明。
    def _write(self, payload, side_files=None):
        operation, phase = self._config_operation
        updates = dict(side_files or {})
        code = updates.get(payload['entrypoint'])
        if code is None:
            code = self._text(payload['id'], payload['entrypoint'])
        previous = self._read(payload['id'])
        expected = {**payload, 'code': code, 'scope_layer': 'owner',
                    'revision': previous.get('revision', 0) + 1 if previous else 1}
        proof = _operation_proof(operation, phase, {'script': project_script(expected, detail=True)})
        updates[_operation_file(operation)] = canonical_json(proof)
        super()._write(payload, updates)

    # LLM: 原生 delete_script 已持资产锁并完成 CAS；每个目录 FD 获取后立即登记关闭，证明、移位及刷盘顺序不变。
    # 函数用途: 将准确目录移至私有证明区，目的目录获取失败或取消也释放已取得的源描述符。
    def _delete(self, script_id):
        operation, phase = self._config_operation
        for name in list_names_beneath(self.owner_home, (self.collection, script_id)):
            self._text(script_id, name)
        proof = _operation_proof(operation, 'deleted', {'script_id': script_id, 'deleted': True})
        write_bytes_atomic_beneath(self.owner_home,
            (self.collection, script_id, _operation_file(operation)), canonical_json(proof).encode())
        with ExitStack() as cleanup:
            source = open_directory_beneath(self.owner_home, (self.collection,), require_dir_fd=True)
            cleanup.callback(os.close, source)
            destination = open_directory_beneath(self.owner_home,
                ('data', 'business_v13', 'script_delete_proofs'), create=True, require_dir_fd=True)
            cleanup.callback(os.close, destination)
            try:
                os.stat(operation['operation_id'], dir_fd=destination, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise BusinessError('STORE_UNAVAILABLE')
            os.rename(script_id, operation['operation_id'], src_dir_fd=source, dst_dir_fd=destination)
            os.fsync(source)
            os.fsync(destination)
        _purge_deleted_code(self, operation)
        return True


class _ReceiptMissing(Exception):
    """只用于无副作用回执探测，不向公开协议暴露。"""


# LLM: 记录身份使用原目录内普通文件的 inode/ctime，不以同内容或同版本认领被重建的资产。
# 函数用途: 在原生锁保护下捕获脚本元数据和代码的物理身份。
def _script_identity(store, record):
    descriptor = open_directory_beneath(store.owner_home, (store.collection, record['id']), require_dir_fd=True)
    try:
        result = []
        for name in (store.filename, record['entrypoint']):
            status = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            result.append([status.st_dev, status.st_ino, status.st_ctime_ns, status.st_size])
        return result
    finally:
        os.close(descriptor)


# LLM: 文件名只从宿主生成的耐久 UUID 推导，不消费模型路径。
# 函数用途: 取得本操作的原生事务证明叶名。
def _operation_file(operation):
    return operation['operation_id'] + '.json'


# LLM: 证明携带完整操作身份、请求指纹与精确阶段，不接受代码相同作为恢复依据。
# 函数用途: 构造与原生写入同时持久化的安全结果证明。
def _operation_proof(operation, phase, result):
    return {'schema_version': 'script-config-proof.v1', 'operation_id': operation['operation_id'],
        'fingerprint': operation['fingerprint'], 'script_id': operation['script_id'],
        'owner_id': operation['owner_id'], 'subject_id': operation['subject_id'], 'action': operation['action'],
        'before_identity': operation['before_identity'], 'phase': phase, 'result': result}


# LLM: 已移位的目录只保留本操作证明；不向调用方返回已删除正文，清理中断可重复继续。
# 函数用途: 清除删除证明目录内的代码和旧元数据，保留无秘密的操作收据。
def _purge_deleted_code(store, operation):
    parts = ('data', 'business_v13', 'script_delete_proofs', operation['operation_id'])
    for name in list_names_beneath(store.owner_home, parts):
        if name != _operation_file(operation):
            unlink_file_beneath(store.owner_home, (*parts, name))


# LLM: 只读取准确 prepare 身份对应的事务叶；删除证明必须位于原子移位后的私有目录。
# 函数用途: 核验原生已提交阶段并恢复原响应，损坏或错绑证明明确拒绝。
def _read_operation(store, operation, *, deleted=False):
    parts = (('data', 'business_v13', 'script_delete_proofs', operation['operation_id']) if deleted
             else (store.collection, operation['script_id']))
    raw = read_bytes_beneath(store.owner_home, (*parts, _operation_file(operation)))
    if raw is None:
        return None
    try:
        proof = json.loads(raw)
        expected = _operation_proof(operation, proof['phase'], proof['result'])
        if (proof != expected or type(proof['result']) is not dict
                or proof['phase'] not in ('saved', 'enabled', 'deleted')
                or (deleted and proof['phase'] != 'deleted')):
            raise ValueError('proof')
        if deleted:
            _purge_deleted_code(store, operation)
        return proof
    except (ValueError, TypeError, KeyError) as error:
        raise BusinessError('STORE_UNAVAILABLE') from error


# LLM: 写作用域固定为 owner；workspace/global 仅可发现，不静默覆盖为同名 owner 脚本。
# 函数用途: 取得可写的原生脚本存储并拒绝只读来源。
def _write_store(call, store, row=None):
    if row is not None and row.get('scope_layer', 'owner') != 'owner':
        raise HostAPIError('BUSINESS_ACCESS_DENIED', 403)
    return _NativeCASStore(call.owner_home, workspace_root=store.workspace_root)


# LLM: 准备回执先于任何原生副作用；物理身份在原生锁内捕获，不能以相同内容恢复别人的操作。
# 函数用途: 生成含耐久随机身份、原版本和原文件身份的准备记录。
def _prepare_config(call, store, fingerprint):
    current = store.get_script(call.params['script_id'])
    native = _write_store(call, store, current)
    if call.action == 'scripts.create' and current is not None:
        raise HostAPIError('STATE_CONFLICT', 409)
    with native._locked():
        old = native._get(call.params['script_id'])
        if call.action == 'scripts.create':
            if old is not None:
                raise HostAPIError('STATE_CONFLICT', 409)
        else:
            if old is None:
                raise HostAPIError('RESOURCE_NOT_FOUND', 404)
            _foundation().v13_require_access(call, {**old, 'owner_id': old.get('owner_id') or call.owner_id})
            if old.get('revision', 0) != call.expected_revision:
                raise HostAPIError('REVISION_CONFLICT', 409)
        return {'schema_version': 'script-config-prepare.v1', 'operation_id': 'v13-config-' + uuid.uuid4().hex,
            'owner_id': call.owner_id, 'subject_id': call.subject_id, 'action': call.action,
            'fingerprint': fingerprint, 'script_id': call.params['script_id'],
            'before_revision': old.get('revision', 0) if old else 0,
            'before_identity': _script_identity(native, old) if old else None}


# LLM: 缓存命中也要重读真实记录授权；记录消失时禁止从创建/更新回执返回旧正文。
# 函数用途: 在返回任意配置详情前重新核验当前存在性和读写作用域。
def _config_result(call, store, result):
    if 'script' in result:
        current = required_script(call, store, call.params['script_id'])
        if current.get('scope_layer', 'owner') != 'owner':
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    return result


# LLM: 原 V13Store 保存准备和最终回执；原生阶段证明支撑崩溃恢复，不创建业务集合或伪执行记录。
# 函数用途: 以准备→原生提交→最终回执协议提交配置，并逐次校验当前记录授权。
def _config_transaction(call, store, mutate):
    business = v13_store_for(call)
    original_authorize = business.authorize
    fingerprint = digest([call.action, call.params, call.expected_revision])

    # LLM: 保留 W0 原授权，在每个 Store 授权点补入实时原生记录，回执重放也不能跳过。
    # 函数用途: 复核当前动作及当前目标记录权限。
    def authorize():
        if original_authorize is not None and original_authorize() is not True:
            return False
        current = store.get_script(call.params['script_id'])
        if current is not None:
            _foundation().v13_require_access(call, {**current, 'owner_id': current.get('owner_id') or call.owner_id})
        else:
            _foundation().v13_require_access(call)
        return True

    # LLM: 探测未命中时不产生记录、审计或回执，兼容升级前已经完成的同键请求。
    # 函数用途: 在原 Store 锁域内识别已提交的最终回执。
    def missing(records):
        raise _ReceiptMissing()

    # LLM: 准备仅保存身份和前态，不宣称执行成功，不复制原始脚本秘密。
    # 函数用途: 在原 Store 提交点发布本请求的持久准备记录。
    def prepare(records):
        return _prepare_config(call, store, fingerprint)

    business.authorize = authorize
    try:
        try:
            result = business.transaction('script_runs', missing,
                idempotency_key=call.idempotency_key, params_digest=fingerprint)
            return _config_result(call, store, result)
        except _ReceiptMissing:
            pass
        operation = business.transaction('script_runs', prepare,
            idempotency_key='prepare-' + digest([call.subject_id, call.idempotency_key]), params_digest=fingerprint)

        # LLM: 只有本请求已持久化的操作身份可用于原生恢复，结果仍由原 Store 原子保存。
        # 函数用途: 在最终事务中执行或恢复本操作。
        def apply(records):
            return mutate(records, operation)

        result = business.transaction('script_runs', apply,
            idempotency_key=call.idempotency_key, params_digest=fingerprint)
        return _config_result(call, store, result)
    except (HostAPIError, BusinessError):
        raise
    except (ValueError, TypeError) as error:
        raise HostAPIError('INVALID_SCHEMA') from error


# LLM: 不接受非对象 schema，IO 合同输入来自顶层属性；其它原生字段按正式描述符保留。
# 函数用途: 从创建或更新参数构造标准资产描述符。
def _descriptor(call, old=None):
    params = call.params
    if old is None:
        descriptor = AssetDescriptor(id=params['script_id'], kind='script', name=params['name'], owner_id=call.owner_id)
    else:
        names = {item.name for item in fields(AssetDescriptor)}
        descriptor = AssetDescriptor.from_dict({key: value for key, value in old.items() if key in names})
    meta = dict(descriptor.metadata)
    for key in ('input_schema', 'risk_level', 'requires_approval', 'contexts'):
        if key in params:
            meta[key] = params[key]
    if 'input_schema' in params:
        schema = canonicalize_tool_input_schema(params['input_schema'])
        if (schema.get('type') != 'object' or not isinstance(schema.get('properties', {}), dict)
                or not isinstance(schema.get('required', []), list)):
            raise HostAPIError('INVALID_SCHEMA')
        inputs = tuple(schema.get('properties', {}))
        io = replace(descriptor.io_contract, inputs=inputs)
    else:
        io = descriptor.io_contract
    return replace(descriptor, name=params.get('name', descriptor.name),
        summary=params.get('description', descriptor.summary), tags=tuple(params.get('tags', descriptor.tags)),
        metadata=meta, io_contract=io)


# LLM: 同键先从耐久回执重放；新键碰到已有 ID 不执行 upsert。
# 函数用途: 通过原生 ScriptStore 新增固定脚本。
def create_script(call):
    store = script_store_for(call)

    # LLM: 领域写副作用在集合串行区内且保留原生文件锁与版本末核。
    # 函数用途: 拒绝重复 ID 后保存正式脚本并投影结果。
    def mutate(records, operation):
        native = _write_store(call, store)
        proof = _read_operation(native, operation)
        if proof is not None:
            if proof['phase'] != 'saved':
                raise BusinessError('STORE_UNAVAILABLE')
            return proof['result']
        if store.get_script(call.params['script_id']) is not None:
            raise HostAPIError('STATE_CONFLICT', 409)
        descriptor = _descriptor(call)
        interpreter = call.params.get('interpreter', 'python3')
        if interpreter not in ('python3', 'bash', 'sh'):
            raise HostAPIError('INVALID_SCHEMA')
        native.arm(call, create=True)
        native.bind_operation(operation, 'saved')
        row = native.save_script(descriptor, call.params['code'],
            entrypoint=call.params.get('entrypoint', 'main.py'), interpreter=interpreter,
            timeout_seconds=call.params.get('timeout_seconds', 120), expected_revision=0)
        return {'script': project_script(row, detail=True)}

    return _config_transaction(call, store, mutate)


# LLM: 所有未指定字段从同一原生快照保留，启停仍走原生 set_enabled。
# 函数用途: CAS 更新固定脚本，不把脱敏正文回写到磁盘。
def update_script(call):
    store = script_store_for(call)

    # LLM: 记录级版本在 mutate 和原生锁内双核；enabled 额外提交的版本继续沿原生规则递增。
    # 函数用途: 更新正文元数据与可选启停状态。
    def mutate(records, operation):
        old = required_script(call, store, call.params['script_id'])
        native = _write_store(call, store, old)
        proof = _read_operation(native, operation)
        if proof is not None:
            if proof['phase'] == 'enabled' or proof['phase'] == 'saved' and 'enabled' not in call.params:
                return proof['result']
            if proof['phase'] != 'saved':
                raise BusinessError('STORE_UNAVAILABLE')
        elif old.get('revision', 0) != call.expected_revision:
            raise HostAPIError('REVISION_CONFLICT', 409)
        row = old
        if proof is None and set(call.params) - {'script_id', 'space_id', 'enabled'}:
            native.arm(call, revision=call.expected_revision, identity=operation['before_identity'])
            native.bind_operation(operation, 'saved')
            row = native.save_script(_descriptor(call, old), call.params.get('code', old['code']),
                entrypoint=old['entrypoint'], interpreter=old['interpreter'],
                timeout_seconds=call.params.get('timeout_seconds', old['timeout_seconds']),
                expected_revision=call.expected_revision)
        if 'enabled' in call.params:
            revision = proof['result']['script']['revision'] if proof is not None else row['revision']
            identity = operation['before_identity'] if proof is None and row is old else None
            native.arm(call, revision=revision, identity=identity)
            native.bind_operation(operation, 'enabled')
            native.set_enabled(call.params['script_id'], call.params['enabled'])
            row = native.get_script(call.params['script_id'])
        return {'script': project_script(row, detail=True)}

    return _config_transaction(call, store, mutate)


# LLM: monitor_rules 与脚本回执共享 V13 锁，检查启用引用不会读到锁外旧快照。
# 函数用途: CAS 删除未被启用监控引用的 owner 固定脚本。
def delete_script(call):
    store = script_store_for(call)
    business = v13_store_for(call)

    # LLM: 原生 delete 的首个锁内读取再次核版本，不靠先前详情决定删除权。
    # 函数用途: 在同一 V13 串行域内核引用并删除原生资产。
    def mutate(records, operation):
        native = _write_store(call, store)
        proof = _read_operation(native, operation, deleted=True)
        if proof is not None:
            return proof['result']
        old = required_script(call, store, call.params['script_id'])
        if old.get('revision', 0) != call.expected_revision:
            raise HostAPIError('REVISION_CONFLICT', 409)
        if any(row.get('owner_id') == call.owner_id and row.get('script_id') == call.params['script_id']
               and row.get('enabled') is True for row in business.list('monitor_rules')):
            raise BusinessError('STATE_CONFLICT', details={'reason': 'in_use'})
        native = _write_store(call, store, old)
        native.arm(call, revision=call.expected_revision, identity=operation['before_identity'])
        native.bind_operation(operation, 'deleted')
        return {'script_id': call.params['script_id'], 'deleted': native.delete_script(call.params['script_id'])}

    return _config_transaction(call, store, mutate)


# LLM: 原生输出先脱敏再计算字节界，超界时明确标注截断而不假装完整。
# 函数用途: 给运行结果生成符合 JsonObject 上限的安全副本。
def bounded_output(value):
    clean = redact_sensitive_value(value)
    if not isinstance(clean, dict):
        return None
    encoded = canonical_json(clean).encode('utf-8')
    if len(encoded) <= 65536:
        return clean
    return {'truncated': True, 'original_bytes': len(encoded),
            'preview': encoded[:4096].decode('utf-8', errors='ignore')}


# LLM: 公共运行视图不包含参数或内部摘要，也不从空结果推测成功。
# 函数用途: 投影 script_runs 的公开字段和当前覆盖范围。
def _has_run_result(row):
    # 已核原操作的unknown也是耐久执行事实；不能退回accepted或被坏来源覆盖。
    return row['state'] in ('completed', 'failed') or (
        row['state'] == 'unknown' and isinstance(row.get('execution_result_ref'), dict))


def _run_view(row, *, approval_ids=None, coverage='complete'):
    names = ('execution_id', 'script_id', 'state', 'requires_approval', 'task_id', 'ok',
             'output', 'error', 'duration_ms', 'started_at', 'finished_at')
    result = {key: row[key] for key in names}
    if _has_run_result(row):
        coverage = 'unknown' if row['state'] == 'unknown' else row.get('terminal_coverage', coverage)
        approval_ids = []
    result.update(approval_ids=list(approval_ids or []), coverage=coverage)
    return result


# LLM: 状态补丁在同一记录锁内合并，旧观察不能把已确认终态改回运行中。
# 函数用途: 保存一次运行状态变更和有界输出。
def _patch_run(call, execution_id, changes):
    store = v13_store_for(call)

    # LLM: 仅合并当前变更字段，不用锁外整行覆盖新状态。
    # 函数用途: 在事务中保持运行终态单调并递增记录版本。
    def mutate(records):
        row = records.get(execution_id)
        if row is None:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        _foundation().v13_require_access(call, row)
        if _has_run_result(row):
            return row
        row.update(changes, revision=row['revision'] + 1, updated_at=timestamp(call.clock()))
        return row

    store.transaction('script_runs', mutate,
        idempotency_key='run-state-' + digest([execution_id, changes])[:48],
        params_digest=digest([execution_id, changes]))
    # 回执可能保存旧的非终态投影；返回前重读当前记录，不能让旧状态补丁重放倒退终态。
    current = store.get('script_runs', execution_id)
    _foundation().v13_require_access(call, current)
    return current


# LLM: 输入与静态输入校验器同源，错误仅包含结构路径，不带用户值。
# 函数用途: 校验脚本输入并应用 schema 默认字段。
def validate_script_inputs(record, inputs):
    schema = script_input_schema(record)
    validation = validate_tool_input(inputs, schema)
    if not validation.ok:
        raise BusinessError('INVALID_SCHEMA', details={'path': 'inputs' + validation.issues[0].path.removeprefix('$')})
    return validate_object(inputs, schema)


# LLM: 无持久结果的中断运行不能安全重放；原进程结果未知时宁可保持 unknown。
# 函数用途: 通过原生 runner 执行已验版本并保存准确结局。
def _run_low(call, store, script, row, inputs):
    execution_id = row['execution_id']
    if row['state'] != 'accepted':
        if row['state'] == 'running':
            row = _patch_run(call, execution_id, {'state': 'unknown', 'error': 'RUN_OUTCOME_UNKNOWN'})
        return {'run': _run_view(row, coverage='unknown' if row['state'] == 'unknown' else 'complete')}
    _patch_run(call, execution_id, {'state': 'running', 'started_at': timestamp(call.clock())})
    _foundation().v13_require_access(call)
    pin = {'script_id': script['id'], 'revision': script.get('revision', 0),
        'code_sha256': hashlib.sha256(script['code'].encode()).hexdigest(),
        'source_scope': script.get('source_scope', 'owner')}
    try:
        result = run_script_with_datasources(call.owner_home, script['id'], inputs,
            runner=ScriptRunner(call.owner_home, workspace_root=store.workspace_root,
                owner_scope_root=call.owner_home, agent_home_root=call.context.host.home_paths.root),
            workspace_root=store.workspace_root, expected_script_pin=pin)
        raw = result if isinstance(result, dict) else asdict(result)
        unknown = raw.get('effect_outcome') == 'unknown'
        ok = raw.get('ok') is True
        changes = {'state': 'unknown' if unknown else 'completed' if ok else 'failed',
            'ok': None if unknown else ok, 'output': bounded_output(raw.get('output_data')),
            'error': str(redact_sensitive_value(raw.get('error_code') or ''))[:2048] or None,
            'duration_ms': max(0, round(raw.get('duration_ms') or 0)),
            'started_at': timestamp(raw.get('started_at')), 'finished_at': timestamp(raw.get('ended_at'))}
    except (OSError, ValueError, RuntimeError, TypeError):
        _patch_run(call, execution_id, {'state': 'unknown', 'error': 'RUN_OUTCOME_UNKNOWN'})
        raise HostAPIError('OUTCOME_UNKNOWN', 503, 'unknown') from None
    saved = _patch_run(call, execution_id, changes)
    return {'run': _run_view(saved, coverage='unknown' if unknown else 'complete')}


# LLM: 原生同步派工 receipt 已耐久受理，以其 assignment_ref 作为操作身份，不生成新 operation_id。
# 函数用途: 严格适配冻结 tasks.assign 的同步 receipt 或原生 202 accepted 投影。
def _assignment_receipt(accepted):
    from ..plugin_host.business_action_specs import business_action_spec
    spec = business_action_spec('tasks.assign')
    try:
        if isinstance(accepted, DomainResult):
            if accepted.http_status != 202:
                raise HostAPIError('HOST_NOT_READY', 503)
            data = spec.validate_result(thaw(accepted.data), accepted=True)
            return {'task_id': data['task_id'], 'operation_id': data['operation_id']}
        data = spec.validate_result(accepted)
        if data['receipt_state'] != 'accepted':
            raise HostAPIError('HOST_NOT_READY', 503)
        return {'task_id': data['task_id'], 'operation_id': data['assignment_ref']}
    except HostAPIError as error:
        raise HostAPIError('HOST_NOT_READY', 503) from error


# LLM: 派工使用原生任务幂等账，绝不从 HTTP 线程执行 ScriptsTool 或自行签发审批。
# 函数用途: 向空间助手派发固定脚本运行指令并保存真实任务引用。
def _run_approval(call, script, row, inputs):
    from .v13_w7_simulation_sessions import resolve_space
    space = resolve_space(call, row['space_id'])
    agent_id = _foundation()._assistant_agent(call, space['space_id'])
    instruction = 'call tool scripts action=run script_id=' + script['id'] + ' inputs=' + canonical_json(inputs)
    business = v13_store_for(call)

    # LLM: 原任务端口与 V13 回执各自保留幂等性，提交中断可用同一任务键安全恢复。
    # 函数用途: 保存任务关联及准确 operation_id 回执，不扩展运行记录 schema。
    def accept(records):
        accepted = _foundation().v13_call_frozen(call, 'tasks.assign', {'agent_id': agent_id,
            'space_id': space['space_id'], 'instruction': instruction}, idempotency_key=call.idempotency_key)
        data = _assignment_receipt(accepted)
        current = records[row['execution_id']]
        current.update(task_id=data['task_id'], requires_approval=True, space_id=space['space_id'],
            revision=current['revision'] + 1, updated_at=timestamp(call.clock()))
        return {'state': 'accepted', 'operation_id': data['operation_id'],
            'execution_id': row['execution_id'], 'task_id': data['task_id'], 'requires_approval': True}

    accepted = business.transaction('script_runs', accept,
        idempotency_key='run-accepted-' + digest(row['execution_id'])[:48], params_digest=digest(call.params))
    return DomainResult(202, accepted)


# LLM: 幂等身份与 actor/owner 绑定；记录先提交再执行，重启后不重复启动未知的低风险运行。
# 函数用途: 受控执行一个已登记固定脚本，或返回原生助手受理回执。
def run_script(call):
    native = script_store_for(call)
    business = v13_store_for(call)
    execution_id = 'script-run-' + digest([call.owner_id, call.subject_id, call.action, call.idempotency_key])[:48]

    # LLM: 同一键参数冲突由 Store 回执核验，回放无需脚本仍存在或仍启用。
    # 函数用途: 在执行前持久化唯一运行记录，不保存明文输入。
    def claim(records):
        script = required_script(call, native, call.params['script_id'])
        if not script.get('enabled', True):
            raise HostAPIError('STATE_CONFLICT', 409)
        inputs = validate_script_inputs(script, call.params.get('inputs', {}))
        tool_args_hashes = [tool_arguments_hash({'action': 'run', 'script_id': script['id'], 'inputs': inputs})]
        if inputs == {}:
            tool_args_hashes.append(tool_arguments_hash({'action': 'run', 'script_id': script['id']}))
        risk, approval = script_risk(script)
        approval = approval or risk in ('high', 'critical')
        if approval and not callable(getattr(call.services.background, 'execute', None)):
            raise HostAPIError('HOST_NOT_READY', 503)
        now = timestamp(call.clock())
        records[execution_id] = {'execution_id': execution_id, 'owner_id': call.owner_id,
            'space_id': call.params.get('space_id'), 'script_id': script['id'], 'subject_id': call.subject_id,
            'requires_approval': approval, 'task_id': None, 'context': call.params.get('context'),
            'inputs_sha256': digest(inputs), 'tool_args_hashes': tool_args_hashes,
            'state': 'accepted', 'ok': None, 'output': None,
            'error': None, 'duration_ms': None, 'started_at': None, 'finished_at': None,
            'revision': 0, 'created_at': now, 'updated_at': now}
        return {'execution_id': execution_id}

    with locked_private_directory(call.owner_home, relative_parts=('data', 'business_v13'),
                                  lock_name='.' + execution_id + '.lock'):
        business.transaction('script_runs', claim, idempotency_key=call.idempotency_key,
            params_digest=digest([call.action, call.params, call.expected_revision]))
        row = business.get('script_runs', execution_id)
        _foundation().v13_require_access(call, row)
        if _has_run_result(row):
            return {'run': _run_view(row)}
        if row['task_id'] is not None:
            observed = get_run(replace(call, params={'execution_id': execution_id}))
            if observed['run']['state'] in ('completed', 'failed', 'unknown'):
                return observed
            return _run_accepted_replay(call, row)
        if row['state'] != 'accepted':
            return _run_low(call, native, {}, row, {})
        script = required_script(call, native, row['script_id'])
        inputs = validate_script_inputs(script, call.params.get('inputs', {}))
        if digest(inputs) != row['inputs_sha256']:
            raise HostAPIError('IDEMPOTENCY_CONFLICT', 409)
        risk, approval = script_risk(script)
        if row['requires_approval'] or approval or risk in ('high', 'critical'):
            return _run_approval(call, script, row, inputs)
        return _run_low(call, native, script, row, inputs)


# LLM: 已有 task_id 的重放只读取原任务，不重发指令或推测新的 operation_id。
# 函数用途: 从原任务详情取得同一已受理操作的准确引用。
def _run_accepted_replay(call, row):
    # LLM: 正常路径只重放已提交的受理回执；缺回执不能捏造引用或再次执行。
    # 函数用途: 在异常缺失受理回执时明确失败。
    def absent(records):
        raise HostAPIError('OUTCOME_UNKNOWN', 503, 'unknown')

    accepted = v13_store_for(call).transaction('script_runs', absent,
        idempotency_key='run-accepted-' + digest(row['execution_id'])[:48], params_digest=digest(call.params))
    return DomainResult(202, accepted)


# LLM: 逐页读取完整匹配任务的原生观察；超过有界窗口时保留 partial，不假称全量。
# 函数用途: 收集某任务的审批或工具调用投影。
def _task_observations(call, action, task_id):
    params = {'filter': {'task_id': task_id}, 'limit': 100}
    rows, coverage = [], 'complete'
    for page_index in range(10):
        page = _foundation().v13_call_frozen(call, action, params)
        rows.extend(page['items'])
        if page.get('coverage') == 'unknown':
            coverage = 'unknown'
        elif page.get('coverage') == 'partial' and coverage != 'unknown':
            coverage = 'partial'
        if page['next_cursor'] is None:
            return rows, coverage
        params['cursor'] = page['next_cursor']
    return rows, 'partial' if coverage != 'unknown' else 'unknown'


# LLM: 工具名称或 SUCCEEDED 状态不能证明 run；冻结详情核任务来源，原操作库核身份与准入时的规范参数摘要。
# 函数用途: 从当前任务的原生 scripts.run 操作提取真实脚本结果。
def _task_script_result(call, row, tools):
    from ..local_storage import (
        TOOL_OPERATION_FAILED,
        TOOL_OPERATION_SUCCEEDED,
        TOOL_OPERATION_UNKNOWN,
    )
    from ..runtime_db.managed_operation_store import ManagedOperationStore
    from ..runtime_db.repository import RuntimeRepository
    from ..runtime_db.schema import runtime_db_path
    repo = RuntimeRepository(runtime_db_path(call.owner_home), read_only=True)
    operations = ManagedOperationStore(repo)
    for tool in reversed(tools):
        if (row['owner_id'] != call.owner_id or tool['tool_name'] != 'scripts' or tool['executed'] is not True
                or tool['task_id'] != row['task_id']):
            continue
        details = _foundation().v13_call_frozen(call, 'tool_calls.get', {'operation_id': tool['operation_id']})
        actual = details['tool_call']
        if (actual['task_id'] != row['task_id'] or actual['executed'] is not True
                or any(actual[key] != tool[key] for key in ('operation_id', 'run_id', 'attempt_id', 'tool_name'))):
            continue
        record = operations.get_tool_operation(owner_id=call.owner_id, run_id=tool['run_id'],
            operation_id=tool['operation_id'], attempt_id=tool['attempt_id'], tool_name='scripts')
        if record is None or record.status not in (TOOL_OPERATION_SUCCEEDED, TOOL_OPERATION_FAILED, TOOL_OPERATION_UNKNOWN):
            continue
        if not record.args_hash or record.args_hash not in row.get('tool_args_hashes', ()):
            continue
        payload = record.result
        if payload.get('schema_version') != 'tool_execution_result.v1' or payload.get('handler_executed') is not True:
            continue
        try:
            result = json.loads(payload['output'])
        except (ValueError, TypeError, KeyError):
            continue
        if (not isinstance(result, dict) or result.get('script_id') != row['script_id']
                or type(result.get('ok')) is not bool or not isinstance(result.get('output_data'), dict)):
            continue
        unknown = result.get('effect_outcome') == 'unknown'
        if record.status == TOOL_OPERATION_UNKNOWN and not unknown:
            continue
        return {'state': 'unknown' if unknown else 'completed' if result['ok'] else 'failed',
            'ok': None if unknown else result['ok'], 'output': bounded_output(result['output_data']),
            'error': str(redact_sensitive_value(result.get('error_code') or ''))[:2048] or None,
            'duration_ms': max(0, round(result.get('duration_ms') or 0)),
            'started_at': timestamp(result.get('started_at')), 'finished_at': timestamp(result.get('ended_at')),
            'execution_result_ref': {key: tool[key] for key in ('operation_id', 'run_id', 'attempt_id')}}
    return None


# LLM: 完成状态必须同时有任务终态和实际 scripts 工具确认，不以助手回复正文冒充执行结果。
# 函数用途: 读取持久运行并通过冻结任务及审批接口刷新公开状态。
def get_run(call):
    business = v13_store_for(call)
    row = business.get('script_runs', call.params['execution_id'])
    if row is None:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    _foundation().v13_require_access(call, row)
    if _has_run_result(row) or row['task_id'] is None:
        coverage = 'unknown' if row['state'] == 'unknown' else 'complete'
        return {'run': _run_view(row, coverage=coverage)}
    try:
        task = _foundation().v13_call_frozen(call, 'tasks.get', {'task_id': row['task_id']})['task']
        approvals, coverage = _task_observations(call, 'approvals.list', row['task_id'])
        pending = [item['approval_id'] for item in approvals if item['state'] == 'pending']
        if len(pending) > 20:
            coverage = 'partial'
        changes = {}
        if pending:
            changes['state'] = 'awaiting_approval'
        elif task['status'] in ('failed', 'cancelled'):
            changes = {'state': 'failed', 'ok': False, 'error': 'SCRIPT_TASK_FAILED',
                       'finished_at': timestamp(call.clock())}
        elif task['status'] in ('done', 'completed'):
            tools, tool_coverage = _task_observations(call, 'tool_calls.list', row['task_id'])
            changes = _task_script_result(call, row, tools) if tools else None
            if changes is None:
                changes = {'state': 'unknown', 'ok': None, 'error': 'SCRIPT_RESULT_UNAVAILABLE'}
                coverage = 'unknown'
            if tool_coverage == 'unknown' or changes['state'] == 'unknown':
                coverage = 'unknown'
            elif tool_coverage == 'partial' and coverage != 'unknown':
                coverage = 'partial'
        elif task['status'] == 'running':
            changes['state'] = 'running'
        elif task['status'] in ('pending', 'queued', 'accepted'):
            changes['state'] = 'accepted'
        else:
            changes['state'] = 'unknown'
            coverage = 'unknown'
        if changes and (changes.get('state') in ('completed', 'failed') or changes.get('execution_result_ref')):
            changes['terminal_coverage'] = coverage
        if changes and any(row.get(key) != value for key, value in changes.items()):
            row = _patch_run(call, row['execution_id'], changes)
        return {'run': _run_view(row, approval_ids=pending[:20], coverage=coverage)}
    except (HostAPIError, BusinessError, OSError, ValueError, RuntimeError, sqlite3.Error):
        current = business.get('script_runs', row['execution_id'])
        if current is not None and _has_run_result(current):
            _foundation().v13_require_access(call, current)
            return {'run': _run_view(current)}
        unknown = {**row, 'state': 'unknown', 'ok': None}
        return {'run': _run_view(unknown, coverage='unknown')}
