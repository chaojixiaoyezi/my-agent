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
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict
from pathlib import Path
from types import MappingProxyType

from ..assets import safe_asset_id
from ..assets.datasource_store import DatasourceStore, secret_values
from ..assets.descriptor import DataSourceSpec
from ..common.nofollow_fs import read_text_beneath, unlink_file_beneath, write_text_atomic_beneath

_RUNTIME_BINDINGS = ContextVar('datasource_runtime_bindings', default=None)
MANIFEST_ENV = 'MY_AGENT_DATASOURCE_RUNTIME_MANIFEST'
MANIFEST_SECRET_ENV = 'MY_AGENT_DATASOURCE_RUNTIME_SECRET'
_FROZEN_MANIFEST = None


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
