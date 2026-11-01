# LLM: SDK导入先固定已签run上下文；每次取句柄复核清单且仅复制已冻结绑定，SQLite语义authorizer保持，不代替内核沙箱。
# 模块用途: 以不可篡改合同提供逻辑代号句柄和参数化SQLite查询。
from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

from ..assets.datasource_store import (
    DatasourceConnectionSpec,
    DatasourcePublicSummary,
    open_sqlite,
    secret_values,
)
from .datasource_runtime import current_datasource_bindings
from .models import (
    SCRIPT_OUTPUT_PROTOCOL_VERSION as SCRIPT_OUTPUT_PROTOCOL_VERSION,
)
from .models import (
    SCRIPT_WORK_CONTEXT_SCHEMA_VERSION,
    DeliveryIntent,
    ScriptWorkContext,
    ScriptWorkContextError,
)

# 可信bootstrap在用户代码之前导入SDK；宿主无run环境时不创建或缓存空上下文。
current_datasource_bindings()

_NOT_DECLARED = 'DATASOURCE_NOT_DECLARED_IN_SCRIPT_CONTRACT'
_READ_ONLY = 'DATASOURCE_READ_ONLY_VIOLATION'
_READ_ACTIONS = frozenset(getattr(sqlite3, name) for name in (
    'SQLITE_SELECT', 'SQLITE_READ', 'SQLITE_FUNCTION', 'SQLITE_RECURSIVE', 'SQLITE_TRANSACTION', 'SQLITE_SAVEPOINT'))
_MUTATING_START = re.compile(r'\A(?:\s|--[^\n]*(?:\n|$)|/\*.*?\*/)*(?:INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|TRUNCATE|ATTACH|DETACH|VACUUM|REINDEX|ANALYZE)\b', re.I | re.S)

WORK_CONTEXT_ENV = 'MY_AGENT_SCRIPT_WORK_CONTEXT_JSON'
WORK_CONTEXT_FILE_ENV = 'MY_AGENT_SCRIPT_WORK_CONTEXT_FILE'
# 脚本工作上下文 JSON 的最大字节数。
_MAX_WORK_CONTEXT_BYTES = 64 * 1024


def get_work_context() -> ScriptWorkContext:
    """解析宿主注入的显式视图；缺失失败，不以脚本临时 cwd 猜业务根。"""
    inline = os.environ.get(WORK_CONTEXT_ENV)
    source = os.environ.get(WORK_CONTEXT_FILE_ENV)
    if inline is None and source is None:
        raise ScriptWorkContextError('SCRIPT_WORK_CONTEXT_MISSING')
    try:
        if inline is not None and source is not None:
            raise ValueError('ambiguous source')
        if source is not None:
            path = Path(source)
            if not path.is_absolute():
                raise ValueError('absolute context source required')
            with path.open('rb') as stream:
                raw = stream.read(_MAX_WORK_CONTEXT_BYTES + 1)
        else:
            raw = inline.encode('utf-8')
        if len(raw) > _MAX_WORK_CONTEXT_BYTES:
            raise ValueError('context too large')
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get('schema_version') != SCRIPT_WORK_CONTEXT_SCHEMA_VERSION:
            raise ValueError('invalid context schema')
        return ScriptWorkContext(workspace_root=Path(data['workspace_root']), scratch_dir=Path(data['scratch_dir']),
                                 run_id=data.get('run_id', ''), task_id=data.get('task_id', ''),
                                 effective_path_scope=data['effective_path_scope'])
    except (OSError, RuntimeError, ValueError, TypeError, KeyError):
        raise ScriptWorkContextError('SCRIPT_WORK_CONTEXT_INVALID') from None


def resolve_workspace_path(relative_path: str, *, write: bool = False) -> Path:
    """仅解析，不创建文件；同时检查真实工作区和原读写范围，包括符号链接目标。"""
    if (not isinstance(relative_path, str) or not relative_path or relative_path != relative_path.strip()
            or any(ord(c) < 32 for c in relative_path) or Path(relative_path).is_absolute()
            or PureWindowsPath(relative_path).drive or relative_path.startswith(('~', '\\'))
            or '..' in relative_path.replace('\\', '/').split('/') or type(write) is not bool):
        raise ScriptWorkContextError('SCRIPT_WORKSPACE_PATH_INVALID')
    context = get_work_context()
    try:
        path = (context.workspace_root / relative_path).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        raise ScriptWorkContextError('SCRIPT_WORKSPACE_PATH_INVALID') from None
    if not path.is_relative_to(context.workspace_root):
        raise ScriptWorkContextError('PATH_OUTSIDE_WORKSPACE')
    roots = context.effective_path_scope['write_roots' if write else 'read_roots']
    if not any(path.is_relative_to(root) for root in roots):
        raise ScriptWorkContextError('PATH_SCOPE_VIOLATION')
    return path


def emit_delivery_request(message: str = '', output_refs: tuple[str, ...] = (), *, request_key: str) -> DeliveryIntent:
    """构造不可变意向，无 IO 或隐式队列；同 key 同输入得到相同值，由宿主按原 run 幂等受理。"""
    return DeliveryIntent(request_key=request_key, message=message, output_refs=output_refs)


# LLM: 公共句柄只展示脱敏规格，秘密及内部规格不进入repr；不能由句柄绕过query_sql的声明门。
# 类用途: 描述当前脚本已授权的数据源，不直接暴露原生可写Connection。
@dataclass(frozen=True)
class DatasourceHandle:
    datasource_id: str
    kind: str
    read_only: bool
    _spec: DatasourceConnectionSpec = field(repr=False)
    _secret: dict = field(default_factory=dict, repr=False)

    # LLM: 独立安全投影同时提供排序凭据键名，实际值仍仅用于脱敏，不进入公开config。
    # 函数用途: 查看连接摘要和凭据字段名称，不返回凭据正文。
    @property
    def config(self):
        return DatasourcePublicSummary(self._spec, has_secret=bool(self._secret),
                                       resolved_secrets=secret_values(self._secret), credential_keys=self._secret.keys())


# LLM: 只为构造单次句柄复制只读JSON绑定；不把缓存内部容器直接交给可变options或句柄，不能反向修改授权快照。
# 函数用途: 将深度冻结的映射/tuple转成独立的普通JSON容器。
def _copy_binding(value):
    if isinstance(value, Mapping):
        return {key: _copy_binding(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_copy_binding(item) for item in value]
    return value


# LLM: 每次先复核签名快照，ID/访问合同门保持；仅从只读绑定复制句柄，不信任磁盘中后来改写的权限。
# 函数用途: 解析本run合同允许的独立数据源句柄。
def get_datasource(datasource_id: str) -> DatasourceHandle:
    bindings = current_datasource_bindings()
    if datasource_id not in bindings:
        raise PermissionError(_NOT_DECLARED)
    row = bindings[datasource_id]
    if row['spec'] is None:
        raise KeyError('DATASOURCE_NOT_FOUND')
    if row['access'] not in ('read', 'read_write'):
        raise PermissionError('DATASOURCE_INVALID_CONTRACT')
    spec = DatasourceConnectionSpec(**_copy_binding(row['spec']))
    if spec.datasource_id != datasource_id:
        raise PermissionError('DATASOURCE_RUNTIME_ID_MISMATCH')
    return DatasourceHandle(datasource_id, spec.kind, spec.read_only or row['access'] == 'read', spec, _copy_binding(row['secret']))


# LLM: 原生SQLite在编译SQL时逐动作检查，读合同只允许读动作；ATTACH/扩展加载/路径PRAGMA即使read_write也不能越出绑定。
# 类用途: 对实际执行的SQL施加只读和表白名单约束。
class _SqlAuthorization:
    # LLM: 单次查询独享拒绝原因，不跨查询缓存权限判断。
    # 函数用途: 初始化本次查询的授权上下文。
    def __init__(self, handle):
        self.handle, self.denied = handle, ''

    # LLM: 返回SQLite原生允许/拒绝码；不解析摘要、不把未知写动作当只读，拒绝原因留给统一异常出口。
    # 函数用途: 检查每个SQLite语义动作。
    def __call__(self, action, first, second, database, trigger):
        if self.handle.read_only and action not in _READ_ACTIONS:
            return self._deny(_READ_ONLY)
        if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH) or action == sqlite3.SQLITE_FUNCTION and (second or '').lower() in ('load_extension', 'readfile', 'writefile') or action == sqlite3.SQLITE_PRAGMA and (first or '').lower() in ('writable_schema', 'temp_store_directory', 'data_store_directory'):
            return self._deny('DATASOURCE_SQL_SCOPE_VIOLATION')
        if action in (sqlite3.SQLITE_READ, sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
            allowed = self.handle._spec.allowed_tables
            if allowed and first not in allowed:
                return self._deny('DATASOURCE_TABLE_NOT_ALLOWED')
        return sqlite3.SQLITE_OK

    # LLM: 保留第一次拒绝码，后续SQLite异常统一转换成同一权限异常。
    # 函数用途: 记录授权拒绝并返回原生拒绝值。
    def _deny(self, code):
        self.denied = self.denied or code
        return sqlite3.SQLITE_DENY


# LLM: 参数绑定不拼接值，单条execute禁止多语句；先声明门、再只读前检及原生authorizer，事务结束与连接关闭覆盖所有出口。
# 函数用途: 使用逻辑代号查询SQLite，返回列名到值的字典列表。
def query_sql(datasource_id: str, sql: str, params: Sequence[object] | Mapping[str, object] | None = None) -> list[dict[str, object]]:
    handle = get_datasource(datasource_id)
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError('DATASOURCE_SQL_REQUIRED')
    if handle.read_only and _MUTATING_START.match(sql):
        raise PermissionError(_READ_ONLY)
    if handle.kind != 'sqlite':
        raise NotImplementedError('DATASOURCE_SQL_DRIVER_UNSUPPORTED')
    if params is not None and (not isinstance(params, (Mapping, Sequence)) or isinstance(params, (str, bytes))):
        raise ValueError('DATASOURCE_SQL_INVALID_PARAMS')
    authorizer = _SqlAuthorization(handle)
    with closing(open_sqlite(handle._spec, read_only=handle.read_only)) as conn:
        conn.row_factory = sqlite3.Row
        conn.set_authorizer(authorizer)
        try:
            with conn:
                cursor = conn.execute(sql, params if params is not None else ())
                return [dict(row) for row in cursor.fetchall()] if cursor.description else []
        except sqlite3.DatabaseError:
            if authorizer.denied:
                raise PermissionError(authorizer.denied) from None
            raise
