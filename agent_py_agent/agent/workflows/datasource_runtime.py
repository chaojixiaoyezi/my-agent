# LLM: 仅冻结合同声明可绑定；子进程用户代码前验HMAC并深度冻结，每次SDK访问复核同清单；不代替Python/OS沙箱。
# 模块用途: 归一声明、创建只读签名清单并固定本次运行的数据源权限快照。
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import time
import uuid
from collections import OrderedDict
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import asdict, fields
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from types import MappingProxyType

from ..assets import safe_asset_id
from ..assets.datasource_store import DatasourceStore, secret_values
from ..assets.descriptor import AssetDescriptor, DataSourceSpec, clean_json
from ..common.nofollow_fs import (
    append_text_beneath,
    open_readonly_file_beneath,
    read_text_beneath,
    unlink_file_beneath,
    write_text_atomic_beneath,
)

_RUNTIME_BINDINGS = ContextVar('datasource_runtime_bindings', default=None)
MANIFEST_ENV = 'MY_AGENT_DATASOURCE_RUNTIME_MANIFEST'
MANIFEST_SECRET_ENV = 'MY_AGENT_DATASOURCE_RUNTIME_SECRET'
_FROZEN_MANIFEST = None
_SCRIPT_WORKSPACE = ContextVar('script_workspace', default=None)
_ASSET_IM_NOTIFIER = ContextVar('asset_im_notifier', default=None)
_SCRIPT_EXECUTION_RECORD = ContextVar('script_execution_record', default=None)
_SOURCE_CHECK_TIMESTAMPS = OrderedDict()
_SOURCE_CHECK_TIMESTAMP_LOCK = RLock()


# LLM: 仅可信执行包装器绑定已验版本；副本按Owner/脚本/workspace隔离，用户子进程不继承宿主ContextVar。
# 函数用途: 让原runner的Store加载取得本次已核对的同份代码与合同。
@contextmanager
def bind_script_execution_record(owner_home, script_id, workspace_root, record):
    key = (Path(owner_home).absolute(), script_id, Path(workspace_root).resolve() if workspace_root else None)
    token = _SCRIPT_EXECUTION_RECORD.set((key, deepcopy(record)))
    try:
        yield
    finally:
        _SCRIPT_EXECUTION_RECORD.reset(token)


# LLM: 仅同调用栈的相同授权范围命中，返回独立副本；不把持久字段或模型输入当作快照授权。
# 函数用途: 给原ScriptStore加载器取得当前执行记录。
def script_execution_record(owner_home, script_id, workspace_root):
    current = _SCRIPT_EXECUTION_RECORD.get()
    key = (Path(owner_home).absolute(), script_id, Path(workspace_root).resolve() if workspace_root else None)
    return deepcopy(current[1]) if current and current[0] == key else None


# LLM: 只有可信调用方可绑定workspace，线程/异步上下文退出即恢复；不从脚本输入或持久投影取得路径权限。
# 函数用途: 给既有runner的Store读取传递同Owner的项目覆盖范围。
@contextmanager
def bind_script_workspace(owner_home, workspace_root):
    token = _SCRIPT_WORKSPACE.set((Path(owner_home).absolute(), workspace_root))
    try:
        yield
    finally:
        _SCRIPT_WORKSPACE.reset(token)


# LLM: 必须匹配原Owner，不能把一个Owner的项目环境传播给另一个Owner的Store。
# 函数用途: 读取当前脚本执行栈的可信项目范围。
def script_workspace_root(owner_home):
    current = _SCRIPT_WORKSPACE.get()
    return current[1] if current and current[0] == Path(owner_home).absolute() else None


# LLM: 展示字段不属于AssetDescriptor；只取权威字段并复用严格模型，不持久化源检查结果。
# 函数用途: 从完整脚本记录或描述符取得统一源声明。
def _script_descriptor(record):
    if isinstance(record, AssetDescriptor):
        return record.to_dict()
    record = record.get('descriptor', record)
    names = {item.name for item in fields(AssetDescriptor)}
    return AssetDescriptor.from_dict({key: value for key, value in record.items() if key in names}).to_dict()


# LLM: 名称只投影声明/已登记安全options，不参与权限；显式绑定名优先，用于发现同ID连接换源。
# 函数用途: 构造固定三字段源绑定。
def _source_binding(source_id, scope):
    return {'source_id': source_id, **{key: value if isinstance(value := scope.get(key), str) else ''
            for key in ('device_name', 'log_source_name')}}


# LLM: 无enabled顶层字段的旧连接采用options.enabled；不改变原DatasourceConnectionSpec契约。
# 函数用途: 读取已登记连接是否被明确停用。
def _source_enabled(row):
    return row.get('enabled', row.get('options', {}).get('enabled', True)) is not False


# LLM: 文件检查不越出原Owner/workspace，nofollow持FD取大小；SQL仅原声明查询且强制read合同，不做网络探测。
# 函数用途: 检查一个已登记源是否缺失或在本次有界查询中为空。
def _source_content_status(owner_home, store, source, row, workspace_root):
    from ..assets.datasource_store import database_path
    from .runtime_sdk import query_sql

    scope, options = source['query_scope'], row.get('options', {})
    path = options.get('path') or options.get('file_path') or row.get('database_path')
    try:
        if path and path != ':memory:':
            path = database_path(path, owner_home, workspace_root)
            root = next(root for root in (Path(owner_home).absolute(), *([Path(workspace_root).absolute()] if workspace_root else []))
                        if path.is_relative_to(root))
            fd = open_readonly_file_beneath(root, path.relative_to(root).parts)
            try:
                if os.fstat(fd).st_size == 0:
                    return 'empty_sources'
            finally:
                os.close(fd)
        sql = scope.get('query') or scope.get('sql')
        if sql and row['kind'] == 'sqlite':
            declared = [DataSourceSpec(source['source_id'], source['system'], query_scope={'access': 'read'})]
            with bind_runtime_datasources(store, declared, workspace_root=workspace_root):
                if not query_sql(source['source_id'], sql, scope.get('params')):
                    return 'empty_sources'
    except Exception:
        return 'missing_sources'
    return ''


# LLM: 仅缓存展示时间不缓存健康状态，Owner/workspace/版本/声明隔离且有界；显式检查刷新，同一状态的读取稳定。
# 函数用途: 为本次实际检查结果补充稳定或显式刷新的时间戳。
def _stamp_source_check(owner_home, descriptor, bindings, check, workspace_root, refresh):
    key = (str(Path(owner_home).resolve()), str(Path(workspace_root).resolve()) if workspace_root else '',
           descriptor['id'], descriptor.get('revision'), hashlib.sha256(_canonical(descriptor['data_sources'])).digest())
    state = _canonical([bindings, check])
    with _SOURCE_CHECK_TIMESTAMP_LOCK:
        previous = _SOURCE_CHECK_TIMESTAMPS.pop(key, None)
        stamp = previous[1] if not refresh and previous and previous[0] == state else datetime.now(timezone.utc).isoformat()
        _SOURCE_CHECK_TIMESTAMPS[key] = (state, stamp)
        while len(_SOURCE_CHECK_TIMESTAMPS) > 512:
            _SOURCE_CHECK_TIMESTAMPS.popitem(last=False)
    check['checked_at'] = stamp
    return bindings, check


# LLM: 每次只读检查真实连接/内容；仅空连接目录的非空被动参数兼容旧脚本，不授予SDK权限；缺失优先于空源和新增。
# 函数用途: 比较脚本冻结绑定和当前连接，不执行脚本；普通读回可保持展示时间稳定。
def evaluate_script_source_check(owner_home: str | Path, record_or_descriptor: Mapping | AssetDescriptor, *, workspace_root=None, refresh_timestamp=True):
    descriptor = _script_descriptor(record_or_descriptor)
    sources = descriptor['data_sources']
    check = {'status': 'not_applicable' if not sources else 'ok', 'added_sources': [],
             'missing_sources': [], 'empty_sources': []}
    if not sources:
        return _stamp_source_check(owner_home, descriptor, [], check, workspace_root, refresh_timestamp)
    store = DatasourceStore(owner_home)
    # 当前Store总是包含全部登记连接，停用状态由options投影，不传尚不存在的include_disabled参数。
    rows = store.list_datasources(workspace_root=workspace_root)
    by_id, bindings = {row['datasource_id']: row for row in rows}, []
    for source in sources:
        row = by_id.get(source['source_id'])
        binding = _source_binding(source['source_id'], {**(row or {}).get('options', {}), **source['query_scope']})
        bindings.append(binding)
        scope = source.get('query_scope')
        if (row is None and not rows and isinstance(scope, dict) and scope
                and not any(key in scope for key in ('device_name', 'log_source_name', 'path', 'file_path',
                    'database_path', 'query', 'sql', 'access'))
                and not source.get('device_name') and not source.get('log_source_name')):
            continue
        changed = row and any(binding[key] and binding[key] != row.get('options', {}).get(key, '')
                              for key in ('device_name', 'log_source_name') if key in row.get('options', {}))
        issue = ('missing_sources' if row is None or not _source_enabled(row) or changed else
                 _source_content_status(owner_home, store, source, row, workspace_root))
        if issue:
            check[issue].append(binding)
    bound = {item['source_id'] for item in bindings}
    systems, devices = {source['system'] for source in sources}, {row['device_name'] for row in bindings if row['device_name']}
    for row in rows:
        options = row.get('options', {})
        if (row['datasource_id'] not in bound and _source_enabled(row)
                and (options.get('system', row['kind']) in systems or options.get('device_name') in devices)):
            check['added_sources'].append(_source_binding(row['datasource_id'], options))
    check['status'] = next((status for key, status in (('missing_sources', 'source_missing'),
        ('empty_sources', 'source_empty'), ('added_sources', 'source_added')) if check[key]), 'ok')
    return _stamp_source_check(owner_home, descriptor, bindings, check, workspace_root, refresh_timestamp)


# LLM: 派生字段不写回或授权；默认稳定展示时间，显式check_sources刷新，实际源健康每次重查。
# 函数用途: 给所有脚本统一投影展示字段及源差异。
def project_script_asset_fields(owner_home: str | Path, record: Mapping, *, workspace_root=None, refresh_timestamp=False):
    descriptor = _script_descriptor(record)
    bindings, check = evaluate_script_source_check(owner_home, descriptor, workspace_root=workspace_root, refresh_timestamp=refresh_timestamp)
    return {'script_name': descriptor['name'], 'summary': descriptor['summary'], 'source_bindings': bindings,
            'source_check': check, **{key: ', '.join(dict.fromkeys(row[key] for row in bindings if row[key]))
                                     for key in ('device_name', 'log_source_name')}}


# LLM: 通道回调由宿主原投递链注入，owner必须一致；不从通知正文、配置文件或脚本取得发送权限。
# 函数用途: 在本调用栈绑定可用IM outbox的入队回调，退出不保留通道句柄。
@contextmanager
def bind_asset_im_notifier(owner_home, notifier):
    token = _ASSET_IM_NOTIFIER.set((Path(owner_home).absolute(), notifier))
    try:
        yield
    finally:
        _ASSET_IM_NOTIFIER.reset(token)


# LLM: 本Owner固定nofollow账本先落盘；可用的宿主outbox回调仅入队，缺通道或失败不影响原业务；queued不冒称已送达。
# 函数用途: 记录源差异或工作流断点通知，并尽力交给当前活跃IM通道。
def emit_asset_im_notification(owner_home: str | Path, *, event_type: str, title: str, message: str, payload: Mapping):
    notice = clean_json({'event_type': event_type, 'title': title, 'message': message,
                         'payload': dict(payload), 'created_at': datetime.now(timezone.utc).isoformat()})
    result = {'recorded': False, 'delivery_status': 'no_channel'}
    try:
        append_text_beneath(owner_home, ('workspace', 'runtime', 'notifications', 'im_alerts.jsonl'),
                            json.dumps(notice, ensure_ascii=False, allow_nan=False) + '\n')
        result['recorded'] = True
    except (OSError, ValueError):
        result['record_error'] = 'NOTIFICATION_STORE_UNAVAILABLE'
    binding = _ASSET_IM_NOTIFIER.get()
    if binding and binding[0] == Path(owner_home).absolute() and callable(binding[1]):
        try:
            sent = binding[1](notice)
            result['delivery_status'] = (sent if sent in ('queued', 'no_channel', 'unavailable')
                                         else ('queued' if sent is None or bool(sent) else 'unavailable'))
        except Exception:
            result['delivery_status'] = 'unavailable'
    return result


# 兼容任务文档中的大小写拼法，生产调用统一使用小写名称。
emit_Asset_im_notification = emit_asset_im_notification


# LLM: 显式空列表/被动dict清除本次预检声明，非空列表补充检查；原SDK仍使用原授权合同，覆盖不持久化。
# 函数用途: 按本次有效覆盖检查源健康，不复用旧展示结果。
def _execution_source_check(owner_home, record, overrides, workspace_root):
    if overrides is None or not isinstance(overrides, (dict, list, tuple)):
        return evaluate_script_source_check(owner_home, record, workspace_root=workspace_root)[1]
    descriptor = _script_descriptor(record)
    sources = {} if not overrides or isinstance(overrides, dict) else {source['source_id']: source for source in descriptor['data_sources']}
    for value in (() if isinstance(overrides, dict) else overrides):
        source = DataSourceSpec.from_dict(value).to_dict()
        sources[source['source_id']] = source
    descriptor['data_sources'] = list(sources.values())
    return evaluate_script_source_check(owner_home, descriptor, workspace_root=workspace_root)[1]


# LLM: 冻结版本来自宿主运行快照，比较后将同记录送原runner，不允许按ID重新加载其它版本。
# 函数用途: 检查实际执行代码是否仍符合运行时冻结的脚本版本。
def _execution_pin_matches(script_id, record, expected):
    actual = {'script_id': script_id, 'revision': record.get('revision', 0),
              'code_sha256': hashlib.sha256(record['code'].encode('utf-8')).hexdigest(),
              'source_scope': record.get('source_scope', 'owner')}
    return expected is None or all(expected.get(key) == value for key, value in actual.items())


# LLM: 硬门失败零执行且保留明确机器码、合同与源状态，不伪造退出成功或调用模型恢复。
# 函数用途: 构造源或版本预检失败的标准脚本结果。
def _preflight_failure(script_id, record, code, check):
    from .models import ScriptExecutionResult
    stamp = time.time()
    return ScriptExecutionResult(script_id, False, None, 0, stamp, stamp, {}, '', code, code, True,
        {'script_id': script_id, 'error_code': code, 'io_contract': record.get('io_contract', {}),
         'source_check': check}, source_check=check)


# LLM: 入口受原审批；版本/源硬门在runner前，执行消费同份记录，原SDK授权/沙箱/清理不变；workspace仅宿主给定。
# 函数用途: 为工具和工作流统一增加源差异硬门，并返回原脚本结果结构。
def run_script_with_datasources(
    owner_home, script_id, inputs=None, *, runner=None, workspace_root=None, expected_script_pin=None,
    timeout_seconds=None, override_data_sources=None, network_access=None, options=None,
):
    from .script_runner import ScriptRunner, _EntrypointMissing, load_script

    run_opts = dict(options or {})
    for key, val in (('timeout_seconds', timeout_seconds), ('override_data_sources', override_data_sources),
                     ('network_access', network_access)):
        if val is not None:
            run_opts[key] = val
    with bind_script_workspace(owner_home, workspace_root):
        try:
            record = load_script(owner_home, script_id)[1]
        except _EntrypointMissing:
            code = 'SCRIPT_REVISION_DRIFT' if expected_script_pin and expected_script_pin.get('code_sha256') else 'ENTRYPOINT_MISSING'
            return _preflight_failure(script_id, {}, code, {})
        if not _execution_pin_matches(script_id, record, expected_script_pin):
            return _preflight_failure(script_id, record, 'SCRIPT_REVISION_DRIFT', record.get('source_check', {}))
        check = _execution_source_check(owner_home, record, run_opts.get('override_data_sources'), workspace_root)
        status = check['status']
        if status in ('source_missing', 'source_empty', 'source_added'):
            emit_asset_im_notification(owner_home, event_type=status, title=str(record.get('name', script_id)),
                message=f'{script_id}: {status}', payload={'script_id': script_id, 'source_check': check})
        if status in ('source_missing', 'source_empty'):
            code = 'DATASOURCE_MISSING' if status == 'source_missing' else 'DATASOURCE_EMPTY'
            return _preflight_failure(script_id, record, code, check)
        with bind_script_execution_record(owner_home, script_id, workspace_root, record):
            result = (runner or ScriptRunner(owner_home, workspace_root=workspace_root)).run_script(script_id, inputs, **run_opts)
        if isinstance(result, dict):
            return {**result, 'source_check': check}
        result.source_check = check
        return result


# LLM: 签名与完整快照摘要使用相同严格规范JSON；不把dict插入顺序、空白或非有限浮点当授权依据。
# 函数用途: 生成固定UTF-8字节用于HMAC和冻结快照比较。
def _canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


# LLM: 每层dict转只读代理、列表转tuple并复制，外部引用不能改变缓存中的访问等级或表范围。
# 函数用途: 将已验签的JSON绑定深度冻结成独立只读对象。
def _freeze_bindings(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_bindings(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_bindings(item) for item in value)
    return value


# LLM: 缺失、坏JSON、非有限/过期时间和不正确schema均拒绝；所有错误只暴露同一稳定权限码。
# 函数用途: 通过原nofollow读口取得一个结构有效的run清单。
def _read_manifest(path):
    data = json.loads(read_text_beneath(path.parent, (path.name,)) or '{}')
    if (not isinstance(data, dict) or type(data.get('schema_version')) is not int or data['schema_version'] != 1
            or not isinstance(data.get('bindings'), dict) or type(data.get('expires_at')) not in (int, float)
            or not math.isfinite(data['expires_at']) or data['expires_at'] <= time.time()):
        raise ValueError('invalid runtime manifest')
    return data


# LLM: 只在可信bootstrap首次加载使用临时run密钥，立即从环境移除；只读缓存仅保存原path/全文摘要/冻结绑定，不保存重签密钥。
# 函数用途: 验证本run初始HMAC并建立一次性冻结快照，后续任何清单漂移或换路径拒绝。
def _manifest_bindings(raw):
    global _FROZEN_MANIFEST
    path = Path(raw)
    if not path.is_absolute():
        raise ValueError('invalid runtime path')
    if _FROZEN_MANIFEST is not None and (raw != _FROZEN_MANIFEST[0] or MANIFEST_SECRET_ENV in os.environ):
        raise ValueError('runtime manifest changed')
    data = _read_manifest(path)
    digest = hashlib.sha256(_canonical(data)).digest()
    if _FROZEN_MANIFEST is None:
        key = bytes.fromhex(os.environ.pop(MANIFEST_SECRET_ENV, ''))
        if len(key) != 32:
            raise ValueError('invalid runtime key')
        expected = hmac.new(key, _canonical(data['bindings']), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, data.get('signature', '')):
            raise ValueError('invalid runtime signature')
        _FROZEN_MANIFEST = (raw, digest, _freeze_bindings(data['bindings']))
    elif not hmac.compare_digest(digest, _FROZEN_MANIFEST[1]):
        raise ValueError('runtime manifest changed')
    return _FROZEN_MANIFEST[2]


# LLM: 标准DataSourceSpec与任务access_contract是两个显式输入协议；冲突ID/访问等级或重复声明拒绝，不猜权限。
# 函数用途: 把声明归一为唯一ID到read/read_write的映射。
def declared_datasources(declared_specs):
    if not isinstance(declared_specs, (tuple, list)):
        raise ValueError('DATASOURCE_INVALID_CONTRACT')
    result = {}
    for item in declared_specs:
        if isinstance(item, DataSourceSpec):
            item = asdict(item)
        if not isinstance(item, dict):
            raise ValueError('DATASOURCE_INVALID_CONTRACT')
        source_id = item.get('source_id', item.get('id'))
        if 'source_id' in item and 'id' in item and item['source_id'] != item['id']:
            raise ValueError('DATASOURCE_CONTRACT_ID_CONFLICT')
        safe_asset_id(source_id)
        nested = item.get('query_scope', {}).get('access')
        access = item.get('access', nested or 'read')
        if access not in ('read', 'read_write') or nested is not None and nested != access or source_id in result:
            raise ValueError('DATASOURCE_INVALID_CONTRACT')
        result[source_id] = access
    return result


# LLM: 只有声明过的ID可解析秘密；尚未登记的旧查询源保留缺失标记，真正调用SDK时明确失败而非越权回退。
# 函数用途: 冻结当前run所需的连接和认证快照。
def _resolve_bindings(store, declared_specs, workspace_root):
    result = {}
    for source_id, access in declared_datasources(declared_specs).items():
        try:
            spec = store.get_datasource_spec(source_id, workspace_root=workspace_root)
        except KeyError:
            result[source_id] = {'access': access, 'spec': None, 'secret': {}}
            continue
        result[source_id] = {'access': access, 'spec': asdict(spec), 'secret': store.resolve_secret(spec.credential_ref)}
    return result


# LLM: 宿主绑定也深度冻结；ContextVar隔离线程/异步任务并在退出恢复，Store与workspace仍仅受信调用者提供。
# 函数用途: 临时绑定只读连接快照供SDK受控调用及测试。
@contextmanager
def bind_runtime_datasources(store, declared_specs, *, workspace_root=None):
    token = _RUNTIME_BINDINGS.set(_freeze_bindings(_resolve_bindings(store, declared_specs, workspace_root)))
    try:
        yield
    finally:
        _RUNTIME_BINDINGS.reset(token)


# LLM: 宿主ContextVar优先；子进程首次验签后只返回冻结绑定，每次重读比较原清单，删除环境或文件不能重置权限。
# 函数用途: 取得当前只读数据源绑定，篡改/过期/坏签名统一PermissionError。
def current_datasource_bindings():
    bound = _RUNTIME_BINDINGS.get()
    if bound is not None:
        return bound
    raw = os.environ.get(MANIFEST_ENV)
    if not raw and _FROZEN_MANIFEST is None:
        return {}
    try:
        if not raw:
            raise ValueError('runtime manifest removed')
        return _manifest_bindings(raw)
    except (OSError, ValueError, TypeError, KeyError):
        raise PermissionError('DATASOURCE_INVALID_RUNTIME_MANIFEST') from None


# LLM: 根和文件由宿主冻结目录给定；仅解析声明连接，捕获秘密值先于其它可能失败的准备步骤。
# 类用途: 保存本次脚本的manifest生命周期、沙箱根与SDK启动包装。
class ScriptDatasourceRuntime:
    # LLM: SDK连接只支持可信预载Python；shell已登记连接在解密前拒绝，旧被动来源保留但不投递签名环境。
    # 函数用途: 为Python创建独立签名快照，普通shell及旧查询来源不接收重签能力。
    def __init__(self, owner_home, metadata, folder, environment, timeout, capture, *, workspace_root=None):
        self.owner_home = Path(owner_home)
        self.folder = Path(folder)
        self.parts = None
        for key in (MANIFEST_ENV, MANIFEST_SECRET_ENV):
            environment.pop(key, None)
        contract = metadata.get('access_contract')
        sources = contract.get('data_sources', ()) if contract is not None else metadata.get('data_sources', ())
        if metadata.get('interpreter', 'python3') != 'python3':
            for source_id in declared_datasources(sources):
                try:
                    DatasourceStore(owner_home).get_datasource_spec(source_id, workspace_root=workspace_root)
                except KeyError:
                    continue
                raise PermissionError('DATASOURCE_PYTHON_RUNTIME_REQUIRED')
            self.read_roots, self.write_roots = self._roots({})
            return
        bindings = _resolve_bindings(DatasourceStore(owner_home), sources, workspace_root)
        capture['datasource_secrets'] = secret_values([row['secret'] for row in bindings.values()])
        self.read_roots, self.write_roots = self._roots(bindings)
        self.name = 'datasources-' + uuid.uuid4().hex + '.json'
        self.parts = (*self.folder.relative_to(self.owner_home).parts, self.name)
        run_secret = os.urandom(32).hex()
        signature = hmac.new(bytes.fromhex(run_secret), _canonical(bindings), hashlib.sha256).hexdigest()
        manifest = {'schema_version': 1, 'expires_at': time.time() + timeout + 5, 'bindings': bindings, 'signature': signature}
        write_text_atomic_beneath(self.owner_home, self.parts, json.dumps(manifest, ensure_ascii=False, allow_nan=False), file_mode=0o400)
        environment[MANIFEST_ENV] = str(self.folder / self.name)
        environment[MANIFEST_SECRET_ENV] = run_secret

    # LLM: SDK源码只读；数据库仅显式文件及其SQLite旁件，不能因为读一个库就开放整个Owner目录。
    # 函数用途: 给原沙箱增补本次声明连接必需的最小根。
    def _roots(self, bindings):
        reads, writes = [Path(__file__).resolve().parents[2]], []
        for row in bindings.values():
            spec = row['spec']
            if not spec or spec['kind'] != 'sqlite' or spec['database_path'] == ':memory:':
                continue
            path = Path(spec['database_path'])
            files = [path, *(Path(str(path) + suffix) for suffix in ('-wal', '-shm', '-journal'))]
            reads.extend(files)
            if row['access'] == 'read_write' and not spec['read_only']:
                writes.extend(files)
        return tuple(reads), tuple(writes)

    # LLM: Python一律在用户代码/guard前预载当前SDK；绑定后不得改用shell解释器，原审计钩子保持。
    # 函数用途: 通过可信bootstrap冻结SDK数据源权限，再运行原脚本或原沙箱guard。
    def wrap_command(self, command, interpreter, *, guarded=False):
        if interpreter != 'python3':
            if self.parts is not None:
                raise PermissionError('DATASOURCE_PYTHON_RUNTIME_REQUIRED')
            return command
        preload = 'import agent_py_agent.runtime_sdk;'
        bootstrap = 'import runpy,sys;sys.path.insert(0,sys.argv.pop(1));' + preload + 'runpy.run_path(sys.argv.pop(1),run_name="__main__")'
        return [*command[:3], '-c', bootstrap, str(Path(__file__).resolve().parents[3]), *command[3:]]

    # LLM: 只清理本对象创建的manifest，无绑定shell没有清单；nofollow失败明确报告，不悄悄保留秘密文件。
    # 函数用途: 在脚本所有退出路径删除临时绑定。
    def close(self):
        if self.parts is not None:
            unlink_file_beneath(self.owner_home, self.parts)
