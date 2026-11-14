# LLM: 连接规格为唯一权威；Owner私有nofollow Vault认证加密不抵御主密钥同时失窃，SQLite持FD核身份；迁移仍须版本CAS。
# 模块用途: 保存分层数据源和密文凭据，安全打开SQLite并准备/切换迁移，公开出口统一脱敏。
from __future__ import annotations

import base64
import errno
import hashlib
import hmac
import importlib
import ipaddress
import json
import os
import re
import socket
import sqlite3
import stat
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import MISSING, asdict, dataclass, field, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from ..common.json_io import locked_json_path
from ..common.nofollow_fs import (
    _supports_dir_fd,
    ensure_private_dir,
    list_names_beneath,
    open_directory_beneath,
    open_private_lock_beneath,
    open_readonly_file_beneath,
    read_text_beneath,
    write_text_atomic_beneath,
)
from . import _verify_directory_beneath, safe_asset_id

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows 无 fcntl。
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX 无 msvcrt。
    msvcrt = None

# 无 flock 时串行本进程的闸门；可重入以支持同一事务按顺序持有多个物理库闸门。
_FALLBACK_GATE_LOCK = threading.RLock()

# 物理库闸门等待上限 30 秒：切换等在途 SDK 写者收尾、写者等切换提交；超时明确失败，不无限占住宿主或脚本。
_GATE_WAIT_SECONDS = 30.0
# 写者复核读取的规格叶上限 1 MB：规格只含连接元数据，超限即视为绑定已变。
_MAX_SPEC_LEAF_BYTES = 1024 * 1024


# LLM: 时间仅作审计，不参与授权或CAS。
# 函数用途: 生成有时区的持久时间。
def _now():
    return datetime.now(timezone.utc).isoformat()


# LLM: JSON必须是有限值对象，错误不包含输入正文。
# 函数用途: 私有账本与公共规格使用同一严格序列化。
def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


# LLM: 主密钥在原创建锁内只对初始/仅旧字典Vault创建；已有密文但失钥拒绝，真实FD始终收紧0600。
# 函数用途: 创建或读取Owner主密钥，防止失钥后新保存静默混入另一代密钥。
def _vault_key(owner_home, *, create=False):
    parts = ('data', 'datasource_secrets', '.vault_key')
    if create:
        ensure_private_dir(owner_home)
        _verify_directory_beneath(owner_home, parts[:-1], create=True)
        with locked_json_path(owner_home.joinpath(*parts)):
            text = read_text_beneath(owner_home, parts)
            if text is None:
                _require_vault_initialization(owner_home, parts[:-1])
                text = os.urandom(32).hex()
                write_text_atomic_beneath(owner_home, parts, text, file_mode=0o600)
            descriptor = open_readonly_file_beneath(owner_home, parts)
            try:
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
    else:
        text = read_text_beneath(owner_home, parts)
    key = bytes.fromhex(text or '')
    if len(key) != 32:
        raise ValueError('DATASOURCE_VAULT_CORRUPTED')
    return key


# LLM: 读兼容和失钥检查共用保留信封形状；已识别的认证格式不因schema损坏降级为旧字典。
# 函数用途: 判定一条记录是否属于认证密文信封。
def _vault_envelope(data):
    schema = data.get('schema_version')
    return (isinstance(schema, str) and schema.startswith('datasource_vault.')
            or {'nonce', 'ciphertext', 'mac'}.issubset(data) or data.get('alg') == 'pbkdf2_sha256_ctr_hmac')


# LLM: 调用者持主密钥创建锁，逐个nofollow读取现存JSON；有密文或无法识别的记录时不生成替代主密钥。
# 函数用途: 区分全新/仅旧明文Vault与已加密但失钥的Vault。
def _require_vault_initialization(owner_home, parts):
    try:
        for name in list_names_beneath(owner_home, parts):
            if name.endswith('.json'):
                data = json.loads(read_text_beneath(owner_home, (*parts, name)) or '{}')
                if not isinstance(data, dict) or _vault_envelope(data):
                    raise ValueError('existing vault cannot be initialized')
    except (OSError, ValueError, TypeError):
        raise PermissionError('DATASOURCE_VAULT_CORRUPTED') from None


# LLM: v1指定SHA256计数流按8字节大端计数器从0开始；仅对先验MAC通过的密文解密，不是第三方标准AEAD实现。
# 函数用途: 对JSON字节施加可逆CTR异或流，认证由信封读写入口负责。
def _vault_xor(payload, enc_key):
    result = bytearray()
    for offset in range(0, len(payload), 32):
        block = hashlib.sha256(enc_key + (offset // 32).to_bytes(8, 'big')).digest()
        result.extend(value ^ mask for value, mask in zip(payload[offset:offset + 32], block))
    return bytes(result)


# LLM: 每次新nonce派生分离的enc/mac密钥，MAC覆盖原始nonce与密文字节；唯一明文元数据为排序凭据键名。
# 函数用途: 按任务v1格式生成认证密文信封，主密钥必要时首次落盘。
def _seal_secret(owner_home, secret):
    nonce = os.urandom(16)
    derived = hashlib.pbkdf2_hmac('sha256', _vault_key(owner_home, create=True), nonce, 10000, dklen=64)
    ciphertext = _vault_xor(_json(secret).encode('utf-8'), derived[:32])
    return {'schema_version': 'datasource_vault.v1', 'alg': 'pbkdf2_sha256_ctr_hmac',
            'nonce': base64.b64encode(nonce).decode('ascii'), 'ciphertext': base64.b64encode(ciphertext).decode('ascii'),
            'mac': hmac.new(derived[32:], nonce + ciphertext, hashlib.sha256).hexdigest(), 'credential_keys': sorted(secret)}


# LLM: 旧秘密dict只读兼容；已识别信封禁止降级，先compare_digest验MAC再解密，键名元数据须与认证正文一致。
# 函数用途: 解密本Owner凭据或读取旧字典，损坏统一固定权限错误且不回显值。
def _unseal_secret(owner_home, data):
    schema = data.get('schema_version')
    if not _vault_envelope(data):
        return data
    try:
        if schema != 'datasource_vault.v1' or data.get('alg') != 'pbkdf2_sha256_ctr_hmac':
            raise ValueError('invalid envelope')
        nonce, ciphertext = (base64.b64decode(data[key], validate=True) for key in ('nonce', 'ciphertext'))
        if len(nonce) != 16:
            raise ValueError('invalid nonce')
        derived = hashlib.pbkdf2_hmac('sha256', _vault_key(owner_home), nonce, 10000, dklen=64)
        mac = hmac.new(derived[32:], nonce + ciphertext, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(mac, data['mac']):
            raise ValueError('invalid mac')
        secret = json.loads(_vault_xor(ciphertext, derived[:32]).decode('utf-8'))
        if not isinstance(secret, dict) or data.get('credential_keys') != sorted(secret):
            raise ValueError('invalid keys')
        return secret
    except (OSError, ValueError, TypeError, KeyError):
        raise PermissionError('DATASOURCE_VAULT_CORRUPTED') from None


# LLM: 引用不是秘密字段；只按结构化键判秘密，不凭正文猜执行权限。
# 函数用途: 识别需要移入钥匙串的认证字段。
def _secret_key(key):
    key = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', '_', key).lower().replace('-', '_')
    return key in {'password', 'passwd', 'pass', 'secret', 'token', 'access_token', 'api_key', 'authorization', 'cookie', 'client_secret'}


# LLM: 凭据冲突按严格JSON表示比较，123/123.0或True/1不能借Python相等覆盖后丢失脱敏依据。
# 函数用途: 无歧义收集一个认证值，类型或表示冲突时不回显正文。
def _capture_secret(secrets, key, value):
    if key in secrets and _json(secrets[key]) != _json(value):
        raise DatasourceSpecValidationError('spec_payload', 'consistent credential values', 'conflicting values',
            '同一凭据字段出现冲突；统一规格内联凭据与显式凭据参数', code='DATASOURCE_SECRET_CONFLICT')
    secrets[key] = value


# LLM: URL密码与认证查询参数从原文提取，保留端点形状但不保留秘密；占位符不是新秘密。
# 函数用途: 把连接URL中的认证拆成秘密和安全模板。
def _clean_url(text, secrets):
    url = urlsplit(text)
    if not url.scheme or not url.netloc:
        return text
    netloc = url.netloc
    if url.password and unquote(url.password) != '<redacted>':
        _capture_secret(secrets, 'password', unquote(url.password))
        netloc = netloc.replace(':' + url.password + '@', ':<redacted>@', 1)
    query = []
    for key, value in parse_qsl(url.query, keep_blank_values=True):
        if _secret_key(key) and value != '<redacted>':
            _capture_secret(secrets, key, value)
            value = '<redacted>'
        query.append((key, value))
    return urlunsplit((url.scheme, netloc, url.path, urlencode(query), url.fragment))


# LLM: 递归提取认证键和URL；返回独立容器，不把秘密字段留在options或说明里。
# 函数用途: 从待保存规格中拆出秘密。
def _extract(value, secrets, prefix=''):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError('DATASOURCE_INVALID_JSON')
            if _secret_key(key):
                if item not in ('', None, '<redacted>'):
                    _capture_secret(secrets, prefix + key, item)
            else:
                result[key] = _extract(item, secrets, prefix + key + '.')
        return result
    if isinstance(value, (tuple, list)):
        return [_extract(item, secrets, prefix) for item in value]
    if isinstance(value, str):
        return re.sub(r'[a-zA-Z][a-zA-Z0-9+.-]*://[^\s]+', lambda m: _clean_url(m[0], secrets), value)
    return value


# LLM: 实际字符串及全部int/float凭据均参与脱敏，bool不误当数字口令；调用方包括迁移和脚本双流清洗。
# 函数用途: 递归收集秘密的字符串表示，供全部公开出口精确脱敏。
def secret_values(value):
    if isinstance(value, dict):
        return tuple(s for item in value.values() for s in secret_values(item))
    if isinstance(value, (tuple, list)):
        return tuple(s for item in value for s in secret_values(item))
    if type(value) in (int, float):
        return (str(value),)
    return (value,) if isinstance(value, str) and value else ()


# LLM: 不修改输入，字典键与数值秘密同样清洗；所有长短字符串与数值均直接匹配替换，无键名豁免。
# 函数用途: 将实际秘密值从结果对象、数值字段或文本中替换。
def redact_datasource_values(value, secrets):
    if isinstance(value, str):
        variants = {v for s in secrets for v in (s, quote(s, safe=''), json.dumps(s, ensure_ascii=True)[1:-1]) if v}
        for item in sorted(variants, key=len, reverse=True):
            value = value.replace(item, '<redacted>')
        return value
    if isinstance(value, dict):
        return {redact_datasource_values(k, secrets): redact_datasource_values(v, secrets) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [redact_datasource_values(v, secrets) for v in value]
    if type(value) in (int, float) and str(value) in secrets:
        return '<redacted>'
    return value


_SPEC_CONTENT_KEYS = {'title', 'description', 'dsn_template', 'host', 'database', 'username', 'options'}


# LLM: 只在DatasourceConnectionSpec顶层保护宿主结构属性，对用户内容与options嵌套数据执行完整递归脱敏。
# 函数用途: 清洗连接规格字典中的用户可控字段，保持port/revision/database_path等宿主结构属性不变。
def _redact_spec_fields(data, secrets):
    return {key: (redact_datasource_values(value, secrets) if key in _SPEC_CONTENT_KEYS else value)
            for key, value in data.items()}


# LLM: 只含连接规格和不透明凭据引用，kind开放；真实密码不得由此对象进入持久规格或公开输出。
# 类用途: 表达一个Owner或workspace的数据源连接及访问限制。
@dataclass(frozen=True)
class DatasourceConnectionSpec:
    datasource_id: str
    kind: str
    title: str
    description: str = ''
    scope: str = 'owner'
    dsn_template: str = ''
    database_path: str = ''
    host: str = ''
    port: int = 0
    database: str = ''
    username: str = ''
    credential_ref: str = ''
    read_only: bool = False
    allowed_tables: tuple[str, ...] = ()
    options: dict[str, object] = field(default_factory=dict)
    migrated_from_id: str = ''
    revision: int = 1
    created_at: str = ''
    updated_at: str = ''

    # LLM: 构造与磁盘恢复共用类型门，不接受bool作端口、路径作ID或字符串作只读布尔。
    # 函数用途: 校验数据源值对象，复制可变options。
    def __post_init__(self):
        safe_asset_id(self.datasource_id)
        if not isinstance(self.kind, str) or not isinstance(self.title, str) or not self.kind or not self.title or self.scope not in ('owner', 'workspace'):
            raise ValueError('DATASOURCE_INVALID_SPEC')
        if type(self.read_only) is not bool or type(self.port) is not int or not 0 <= self.port <= 65535:
            raise ValueError('DATASOURCE_INVALID_SPEC')
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError('DATASOURCE_INVALID_REVISION')
        for item in fields(self):
            if isinstance(item.default, str) and not isinstance(getattr(self, item.name), str):
                raise ValueError('DATASOURCE_INVALID_SPEC')
        if self.credential_ref:
            safe_asset_id(self.credential_ref)
        if not isinstance(self.options, dict) or not isinstance(self.allowed_tables, (list, tuple)) or not all(isinstance(t, str) and t for t in self.allowed_tables):
            raise ValueError('DATASOURCE_INVALID_SPEC')
        object.__setattr__(self, 'options', json.loads(_json(self.options)))
        object.__setattr__(self, 'allowed_tables', tuple(self.allowed_tables))


class DatasourceSpecValidationError(ValueError):
    """纯规格准入失败；不带输入值，保证尚未创建目录或写入凭据。"""

    def __init__(self, field_path, expected, actual_type, reason, *, code='DATASOURCE_INVALID_SPEC'):
        self.error_code = code
        self.details = {'field': field_path, 'expected_type': expected,
                        'actual_type': actual_type, 'reason': reason}
        super().__init__(code)


def prepare_datasource_spec(spec_payload, scope='owner', *, known_secrets=()):
    """共用无 I/O 规格准入；先拆凭据再校验，保留开放 kind/options。"""
    def reject(name, expected, value, reason, *, code='DATASOURCE_INVALID_SPEC'):
        raise DatasourceSpecValidationError('spec_payload' + ('.' + name if name else ''),
                                           expected, value, reason, code=code)

    secrets = {}
    try:
        data = json.loads(_json(spec_payload))
    except (ValueError, TypeError, OverflowError):
        reject('', 'object containing finite JSON values', type(spec_payload).__name__, '规格必须是合法 JSON 对象')
    try:
        data = _extract(data, secrets)
    except DatasourceSpecValidationError:
        raise
    except (ValueError, TypeError):
        reject('', 'valid connection values', 'invalid', '连接或内联凭据格式无效；检查连接 URL 和凭据字段')
    if not isinstance(data, dict):
        reject('', 'object', type(data).__name__, '规格必须是对象')
    definitions = {item.name: item for item in fields(DatasourceConnectionSpec)}
    for key in data:
        if key not in definitions:
            name = key if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', key) else '<unknown>'
            reject(name, 'declared connection field; extensions belong in options',
                   type(data[key]).__name__, '未知连接字段；扩展参数请放到 options 对象')
    for name, item in definitions.items():
        if name not in data:
            if item.default is MISSING and item.default_factory is MISSING:
                reject(name, 'non-empty string', 'missing', '缺少必填字段')
            continue
        value = data[name]
        expected = ('object' if name == 'options' else 'array of non-empty strings' if name == 'allowed_tables'
                    else 'boolean' if type(item.default) is bool else 'integer' if type(item.default) is int else 'string')
        valid = (isinstance(value, dict) if name == 'options' else
                 isinstance(value, (list, tuple)) and all(isinstance(v, str) and v for v in value) if name == 'allowed_tables' else
                 type(value) is bool if expected == 'boolean' else type(value) is int if expected == 'integer' else isinstance(value, str))
        if not valid:
            reject(name, expected, type(value).__name__, '字段类型不符合连接规格')
    for name in ('datasource_id', 'credential_ref'):
        if name in data and (name == 'datasource_id' or data[name]):
            try:
                safe_asset_id(data[name])
            except ValueError:
                reject(name, 'safe asset identifier', type(data[name]).__name__, '代号格式无效；使用字母、数字、连字符或下划线')
    for name in ('kind', 'title'):
        if not data[name]:
            reject(name, 'non-empty string', 'str', '必填字符串不能为空')
    if 'scope' in data and data['scope'] != scope:
        reject('scope', scope, type(data['scope']).__name__, '规格 scope 必须与动作 scope 一致', code='DATASOURCE_SCOPE_MISMATCH')
    if scope not in ('owner', 'workspace'):
        reject('scope', 'owner or workspace', type(scope).__name__, '不支持的保存范围')
    for name, minimum, maximum in (('port', 0, 65535), ('revision', 1, None)):
        if name in data and (data[name] < minimum or maximum is not None and data[name] > maximum):
            reject(name, f'integer >= {minimum}' + (f' and <= {maximum}' if maximum else ''), 'int', '数值超出允许范围',
                   code='DATASOURCE_INVALID_REVISION' if name == 'revision' else 'DATASOURCE_INVALID_SPEC')
    data['scope'] = scope
    data = _redact_spec_fields(data, (*secret_values(secrets), *known_secrets))
    try:
        return DatasourceConnectionSpec(**data), secrets
    except (ValueError, TypeError):
        reject('', 'valid redacted connection specification', 'invalid',
               '脱敏后连接规格无效；请勿让凭据值与数据源代号、类型或范围字段相同')


# LLM: 公开投影重新去掉内联认证；凭据清单仅接收顶层键名，默认无钥匙串上下文时为空。
# 函数用途: 生成保留旧字段并增加排序credential_keys的安全数据源摘要。
def DatasourcePublicSummary(spec, *, has_secret=False, resolved_secrets=(), credential_keys=()):
    secrets = {}
    data = _extract(asdict(spec), secrets)
    values = (*secret_values(secrets), *resolved_secrets)
    return {**_redact_spec_fields(data, values),
            'has_secret': bool(has_secret), 'credential_keys': sorted(credential_keys)}


# LLM: 数据库路径只允许当前可信Owner/workspace，逐段nofollow核父目录和叶类型；URI查询不能借路径扩权。
# 函数用途: 验证文件型数据库路径，不允许符号链接、硬链接或逃出可信根。
def database_path(path, owner_home, workspace_root=None, *, create_parent=False):
    candidate = Path(path).expanduser()
    candidate = candidate if candidate.is_absolute() else Path(owner_home) / candidate
    candidate = Path(os.path.abspath(candidate))
    roots = (Path(owner_home).absolute(), *((Path(workspace_root).absolute(),) if workspace_root else ()))
    root = next((root for root in roots if candidate.is_relative_to(root)), None)
    if root is None or candidate == root:
        raise ValueError('DATASOURCE_PATH_OUTSIDE_SCOPE')
    parts = candidate.relative_to(root).parts
    if not _supports_dir_fd():
        parent_dir = _verify_directory_beneath(root, parts[:-1], create=create_parent)
        try:
            info = (parent_dir / parts[-1]).lstat()
        except FileNotFoundError:
            return candidate
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('DATASOURCE_UNSAFE_DATABASE_PATH')
        return candidate
    descriptor = open_directory_beneath(root, parts[:-1], create=create_parent)
    try:
        try:
            info = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return candidate
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('DATASOURCE_UNSAFE_DATABASE_PATH')
    finally:
        os.close(descriptor)
    return candidate


# LLM: Windows只供SQLite逐段lstat拒绝符号链接及所有reparse point；不resolve，也不宣称具有POSIX目录FD的原子防护。
# 函数用途: 采集Windows数据库与完整父链的身份、ctime、类型及链接数，供打开前后复核。
def _windows_sqlite_path_state(path):
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('DATASOURCE_UNSAFE_DATABASE_PATH')
    result = []
    for component in (path, *path.parents):
        info = component.lstat()
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                or (component != path and not stat.S_ISDIR(info.st_mode))):
            raise ValueError('DATASOURCE_UNSAFE_DATABASE_PATH')
        result.append((info.st_dev, info.st_ino, info.st_ctime_ns, info.st_mode, info.st_nlink))
    return tuple(result)


# LLM: Windows3.12路径ctime为创建时间而fstat为变更时间，不能跨源比较；身份仍跨源严格匹配，路径快照与持有FD时间各自复核。
# 函数用途: 打开并持有数据库的只读校验FD，拒绝链接、特殊文件、硬链接及观察到的路径替换，调用方负责关闭。
def _open_sqlite_descriptor(path):
    if os.name != 'nt':
        return open_readonly_file_beneath(path.anchor, path.parts[1:])
    before = _windows_sqlite_path_state(path)
    if not stat.S_ISREG(before[0][3]) or before[0][4] != 1:
        raise ValueError('DATASOURCE_UNSAFE_DATABASE_PATH')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0)
                         | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        info = os.fstat(descriptor)
        # 跨源只比较同义的身份、类型/权限和硬链接数；ctime不删除，仍由同源路径快照和连接窗口FD复核检查。
        opened = (info.st_dev, info.st_ino, info.st_mode, info.st_nlink)
        expected = before[0][:2] + before[0][3:]
        if opened != expected or _windows_sqlite_path_state(path) != before:
            raise PermissionError('DATASOURCE_SQLITE_PATH_REPLACED')
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


# LLM: POSIX原逐层nofollow父链不变；Windows复用完整叶/父链lstat，同源ctime前后比较，不能尝试用os.open打开目录。
# 函数用途: 取得连接窗口前后的路径稳定性证据，Windows额外保留库叶以免丢失其ctime变化检查。
def _sqlite_directory_state(path):
    if os.name == 'nt':
        return tuple(stamp[:3] for stamp in _windows_sqlite_path_state(path))
    result = []
    for parent in path.parents:
        descriptor = open_directory_beneath(path.anchor, parent.parts[1:])
        try:
            info = os.fstat(descriptor)
            result.append((info.st_dev, info.st_ino, info.st_ctime_ns))
        finally:
            os.close(descriptor)
    return tuple(result)


# LLM: Linux通过/proc/self/fd快照读取存活常规文件FD的(dev,ino)，补足粗粒度ctime下的同毫秒ABA替换检测。
# 函数用途: 采集当前进程存活常规文件FD的身份映射。
def _live_regular_fds():
    if not Path('/proc/self/fd').is_dir():
        return None
    mapping = {}
    for entry in os.listdir('/proc/self/fd'):
        try:
            fd = int(entry)
            info = os.stat(f'/proc/self/fd/{fd}')
            if stat.S_ISREG(info.st_mode):
                mapping[fd] = (info.st_dev, info.st_ino)
        except OSError:
            continue
    return mapping


# LLM: 校验FD跨connect存活；POSIX原strict链不变，Windows走局部路径复核；连接后仍核dev/ino、ctime、父链及可用的/proc FD。
# 函数用途: 按ro/rw打开SQLite，连接窗口身份/元数据不稳定一律关闭并拒绝，成功连接由调用方关闭。
def open_sqlite(spec, *, read_only=False):
    if spec.database_path == ':memory:':
        return sqlite3.connect(':memory:')
    path = Path(spec.database_path)
    if not path.is_absolute():
        raise ValueError('DATASOURCE_UNSAFE_DATABASE_PATH')
    uri = path.as_uri() + ('?mode=ro' if read_only or spec.read_only else '?mode=rw')
    descriptor = _open_sqlite_descriptor(path)
    conn = None
    try:
        expected = os.fstat(descriptor)
        parents = _sqlite_directory_state(path)
        fds_before = _live_regular_fds()
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            fds_after = _live_regular_fds()
            if fds_before is not None and fds_after is not None:
                opened_inodes = {ino for fd, ino in fds_after.items() if fds_before.get(fd) != ino}
                if opened_inodes and (expected.st_dev, expected.st_ino) not in opened_inodes:
                    raise ValueError('fd inode mismatch')
            actual = os.stat(path, follow_symlinks=False)
            if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
                raise ValueError('replaced')
            held = os.fstat(descriptor)
            if held.st_ctime_ns != expected.st_ctime_ns or _sqlite_directory_state(path) != parents:
                raise ValueError('changed during connect')
            check = _open_sqlite_descriptor(path)
            try:
                verified = os.fstat(check)
                if (verified.st_dev, verified.st_ino) != (expected.st_dev, expected.st_ino):
                    raise ValueError('replaced')
            finally:
                os.close(check)
        except (OSError, ValueError):
            raise PermissionError('DATASOURCE_SQLITE_PATH_REPLACED') from None
        return conn
    except BaseException:
        if conn is not None:
            conn.close()
        raise
    finally:
        os.close(descriptor)


# LLM: 标识符仅转义双引号，不拼接未经转义的表名。
# 函数用途: 安全引用SQLite表或列名。
def sql_identifier(name):
    return '"' + name.replace('"', '""') + '"'


# LLM: 迁移验收比较全部用户schema、行数及内容摘要，不把SELECT 1或等行数当数据一致。
# 函数用途: 对当前SQLite读快照生成可复核的完整性摘要。
def sqlite_fingerprint(connection):
    schema = connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
    tables = [row[1] for row in schema if row[0] == 'table']
    counts, digests = {}, {}
    for name in tables:
        rows = connection.execute('SELECT * FROM ' + sql_identifier(name)).fetchall()
        counts[name] = len(rows)
        digests[name] = hashlib.sha256(repr(sorted(rows, key=repr)).encode()).hexdigest()
    return {'schema_hash': hashlib.sha256(repr(schema).encode()).hexdigest(), 'row_counts': counts, 'data_hashes': digests}


# LLM: 规格指纹覆盖完整无秘密规格(含revision与库路径)，宿主签发与SDK复核用同一规范JSON，不依赖叶文件排版。
# 函数用途: 计算一个连接规格的稳定指纹。
def spec_fingerprint(spec):
    return hashlib.sha256(_json(asdict(spec)).encode('utf-8')).hexdigest()


# LLM: 闸门按库文件(dev,ino)物理身份命名，别名路径落同一把锁；只接受现存普通文件，不跟随链接。
# 函数用途: 取得一个SQLite库文件的稳定物理身份。
def database_identity(path):
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('DATASOURCE_UNSAFE_DATABASE_PATH')
    return info.st_dev, info.st_ino


# LLM: 闸门叶固定在Owner私有data下；只创建或复用、从不替换或删除，避免持锁方与等待方落在不同inode。
# 函数用途: 给出物理库身份对应的闸门叶相对路径。
def _gate_parts(identity):
    return ('data', 'datasource_gates', f'{identity[0]}-{identity[1]}.lock')


# LLM: 签发的是精确闸门叶真实路径与身份；不授予父目录，脚本侧只读打开该叶取共享锁。
# 函数用途: 创建或复用一个物理库闸门并返回可签名的闸门事实。
def database_gate_facts(owner_home, database_file):
    identity = database_identity(database_file)
    descriptor = open_private_lock_beneath(owner_home, _gate_parts(identity))
    try:
        gate = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    path = Path(owner_home).absolute().joinpath(*_gate_parts(identity)).resolve(strict=True)
    return {'gate_path': str(path), 'gate_identity': [gate.st_dev, gate.st_ino], 'database_identity': list(identity)}


# LLM: 只收集现存普通SQLite文件；缺失/非普通文件上不可能有SDK写者在途，不为它们取锁也不报错。
# 函数用途: 计算一组规格或原始记录所涉及的物理库身份集合。
def _database_identities(rows):
    found = set()
    for row in rows:
        data = asdict(row) if isinstance(row, DatasourceConnectionSpec) else (row or {})
        path = data.get('database_path') or ''
        if data.get('kind') != 'sqlite' or path in ('', ':memory:'):
            continue
        try:
            found.add(database_identity(path))
        except (OSError, ValueError):
            continue
    return frozenset(found)


# LLM: Windows同一首字节保留跨进程读/写锁；仅两种OS原语都缺失时告警并降级为有期限的进程内互斥。
# 函数用途: 没有fcntl时仍能打开数据源，并在可用时使用Windows文件锁保护物理库。
@contextmanager
def _fallback_database_gate(descriptor, *, exclusive, deadline):
    if not _FALLBACK_GATE_LOCK.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise TimeoutError('DATASOURCE_GATE_BUSY')
    try:
        if msvcrt is None:
            from ..common.file_lock_support import warn_file_lock_unavailable_once
            warn_file_lock_unavailable_once()
            yield
            return
        mode = msvcrt.LK_NBLCK if exclusive else msvcrt.LK_NBRLCK
        while True:
            try:
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, mode, 1)
                break
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError('DATASOURCE_GATE_BUSY') from None
                time.sleep(0.01)
        try:
            yield
        finally:
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    finally:
        _FALLBACK_GATE_LOCK.release()


# LLM: POSIX共享/排他flock原样保留；无fcntl时在辅助上下文选择Windows锁或显式线程降级，均保证退出释放。
# 函数用途: 在截止时间内取得共享或排他文件锁。
@contextmanager
def _bounded_flock(descriptor, *, exclusive, deadline):
    if fcntl is None:
        with _fallback_database_gate(descriptor, exclusive=exclusive, deadline=deadline):
            yield
        return
    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    while True:
        try:
            fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise TimeoutError('DATASOURCE_GATE_BUSY') from None
            time.sleep(0.01)
    try:
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


# LLM: 切换/保存先排序取排他物理闸门再取规格锁；平台选择留在原有界锁入口，不提前解引用可缺失的fcntl。
# 函数用途: 按稳定顺序持有一组物理库排他闸门，返回已持有的身份集合。
@contextmanager
def _exclusive_database_gates(owner_home, identities):
    deadline = time.monotonic() + _GATE_WAIT_SECONDS
    with ExitStack() as stack:
        for identity in sorted(identities):
            descriptor = open_private_lock_beneath(owner_home, _gate_parts(identity))
            stack.callback(os.close, descriptor)
            stack.enter_context(_bounded_flock(descriptor, exclusive=True, deadline=deadline))
        yield frozenset(identities)


# LLM: 每次按路径重新打开规格叶：原子替换后残留的旧inode(链接数0)与内容指纹变化同样视为绑定已变，不信任先前读取。
# 函数用途: 在写者闸门内复核签名绑定的规格仍是当前规范规格。
def _require_current_spec_leaf(control):
    try:
        descriptor = os.open(control['spec_path'],
                             os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    except OSError:
        raise PermissionError('DATASOURCE_BINDING_CHANGED') from None
    with os.fdopen(descriptor, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink < 1:
            raise PermissionError('DATASOURCE_BINDING_CHANGED')
        raw = handle.read(_MAX_SPEC_LEAF_BYTES + 1)
    try:
        current = DatasourceConnectionSpec(**json.loads(raw.decode('utf-8')))
    except (TypeError, ValueError):
        raise PermissionError('DATASOURCE_BINDING_CHANGED') from None
    if (len(raw) > _MAX_SPEC_LEAF_BYTES or current.revision != control['revision']
            or not hmac.compare_digest(spec_fingerprint(current), control['spec_sha256'])):
        raise PermissionError('DATASOURCE_BINDING_CHANGED')


# LLM: 共享闸门覆盖复核至连接关闭，原叶身份检查保持；平台锁在有界入口选择，无fcntl也不跳过规格复核。
# 函数用途: 为一次可写SDK事务持有共享物理库闸门并复核冻结的规格事实。
@contextmanager
def shared_writer_gate(control):
    try:
        descriptor = os.open(control['gate_path'], os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0))
    except OSError:
        raise PermissionError('DATASOURCE_WRITER_GATE_UNAVAILABLE') from None
    try:
        gate = os.fstat(descriptor)
        if [gate.st_dev, gate.st_ino] != list(control['gate_identity']):
            raise PermissionError('DATASOURCE_WRITER_GATE_UNAVAILABLE')
        with _bounded_flock(descriptor, exclusive=False, deadline=time.monotonic() + _GATE_WAIT_SECONDS):
            _require_current_spec_leaf(control)
            yield
    finally:
        os.close(descriptor)


# LLM: 连接建立后核对库路径仍是闸门签发时的物理库；替换过的库文件不在该闸门保护内，直接拒绝。
# 函数用途: 确认本次写连接落在已持闸门对应的数据库上。
def require_gated_database(path, control):
    try:
        identity = database_identity(path)
    except (OSError, ValueError):
        raise PermissionError('DATASOURCE_BINDING_CHANGED') from None
    if list(identity) != list(control['database_identity']):
        raise PermissionError('DATASOURCE_BINDING_CHANGED')


# LLM: 只比较连接与访问策略，不把显示时间当身份；revision与scope由切换调用方另外硬核对。
# 函数用途: 识别终态写失败后已经生效的同一目标绑定。
def _same_binding(left, right):
    ignored = {'revision', 'created_at', 'updated_at'}
    return _json({k: v for k, v in asdict(left).items() if k not in ignored}) == _json({k: v for k, v in asdict(right).items() if k not in ignored})


# LLM: 状态账固定Owner根，目标仅为已校验规格；正式切换仍使用source原版本CAS，外库dump不证明远端已导入。
# 类用途: 提供数据源的准备、复制、验收与显式切换。
class _DatasourceMigration:
    # LLM: 每个转换先写完整私有状态；失败不回滚或改写原库，不允许跳过阶段。
    # 函数用途: 原子追加一个迁移阶段。
    def _migration_state(self, record, state):
        allowed = {None: {'PREPARED'}, 'PREPARED': {'COPYING', 'FAILED'}, 'COPYING': {'VALIDATING', 'FAILED'},
                   'VALIDATING': {'READY_TO_SWITCH', 'FAILED'}, 'READY_TO_SWITCH': {'SWITCHED', 'FAILED'}}
        if state not in allowed.get(record.get('state'), set()):
            raise ValueError('DATASOURCE_INVALID_MIGRATION_TRANSITION')
        candidate = {**record, 'state': state, 'history': [*record.get('history', []), {'state': state, 'at': _now()}]}
        self._write(self.owner_home, ('data', 'datasource_migrations', record['migration_id'] + '.json'), candidate)
        record.update(candidate)

    # LLM: 目标必须是新文件，O_EXCL且nofollow；复制SQLite一致读快照，全部连接在退出时关闭。
    # 函数用途: 将SQLite原库复制到新文件，保留原库与已有目标。
    def _copy_sqlite(self, source, target, record, workspace_root):
        from contextlib import closing
        target_path = database_path(target.database_path, self.owner_home, workspace_root, create_parent=True)
        descriptor = os.open(target_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        os.close(descriptor)
        with closing(open_sqlite(source, read_only=True)) as src, closing(open_sqlite(target)) as dst:
            src.execute('BEGIN')
            record['source_fingerprint'] = sqlite_fingerprint(src)
            src.backup(dst)
            dst.commit()
        return target

    # LLM: dump文件仅Owner私有；本地回放验收只证明导出可恢复，不声称Postgres/MySQL已导入或可切换。
    # 函数用途: 为外库迁移准备SQL导出和本地完整性验证报告。
    def _export_sqlite(self, source, record):
        from contextlib import closing
        with closing(open_sqlite(source, read_only=True)) as src:
            src.execute('BEGIN')
            record['source_fingerprint'] = sqlite_fingerprint(src)
            dump = '\n'.join(src.iterdump())
        parts = ('data', 'datasource_migrations', record['migration_id'] + '.sql')
        write_text_atomic_beneath(self.owner_home, parts, dump)
        with closing(sqlite3.connect(':memory:')) as check:
            check.executescript(dump)
            restored = sqlite_fingerprint(check)
        record['dump_path'] = str(self.owner_home.joinpath(*parts))
        record['external_target_validated'] = False
        return restored

    # LLM: 恢复switch只认同Owner原迁移ID/源ID/workspace；不得从磁盘记录扩大工作区或凭未验证状态切换。
    # 函数用途: 读取本次显式指定的已准备迁移。
    def _resume_migration(self, source_id, target_spec, workspace_root):
        migration_id = safe_asset_id(target_spec['migration_id'])
        record = self._read(self.owner_home, ('data', 'datasource_migrations', migration_id + '.json'))
        workspace = str(Path(workspace_root).absolute()) if workspace_root else ''
        if not record or record['source_id'] != source_id or record['workspace_root'] != workspace:
            raise ValueError('DATASOURCE_MIGRATION_SCOPE_MISMATCH')
        return record

    # LLM: 切换前重验源与目标内容，元数据版本CAS防并发改连接；调用者必须暂停绕过SDK的外部写者，不承诺在线双写迁移。
    # 函数用途: 仅对验证未漂移的SQLite迁移更新原逻辑代号。
    def _switch_migration(self, record, workspace_root):
        from contextlib import closing
        if record['state'] == 'SWITCHED':
            return
        if record['state'] != 'READY_TO_SWITCH' or record.get('external_target_validated') is False:
            raise ValueError('DATASOURCE_MIGRATION_NOT_SWITCHABLE')
        source = self.get_datasource_spec(record['source_id'], workspace_root=workspace_root)
        if source.scope != record['source_scope']:
            raise ValueError('DATASOURCE_MIGRATION_SOURCE_SCOPE_CHANGED')
        target = DatasourceConnectionSpec(**record['target_spec'])
        new = replace(target, datasource_id=source.datasource_id, scope=source.scope, migrated_from_id=source.datasource_id)
        database_path(source.database_path, self.owner_home, workspace_root)
        database_path(target.database_path, self.owner_home, workspace_root)
        root, parts = self._location(source.scope, workspace_root)
        leaf = (*parts, source.datasource_id + '.json')
        # 排序排他物理闸门先于规格锁：在途SDK写者收尾后才做最终指纹核对；锁内重读规格，经已准入的锁内保存路径提交，不重入flock。
        with _exclusive_database_gates(self.owner_home, _database_identities((source, target))) as gated, \
                locked_json_path(root.joinpath(*leaf)):
            try:
                current = self._read(root, leaf)
                unchanged = current is not None and DatasourceConnectionSpec(**current) == source
            except (TypeError, ValueError):
                unchanged = False
            if not unchanged:
                raise ValueError('DATASOURCE_REVISION_CONFLICT')
            with closing(open_sqlite(source, read_only=True)) as src, closing(open_sqlite(target, read_only=True)) as dst:
                expected = _json(record['source_fingerprint'])
                if _json(sqlite_fingerprint(src)) != expected or _json(sqlite_fingerprint(dst)) != expected:
                    raise ValueError('DATASOURCE_MIGRATION_CONTENT_CHANGED')
                if source.revision != record['source_revision'] and not (
                    source.revision == record['source_revision'] + 1 and _same_binding(source, new)
                ):
                    raise ValueError('DATASOURCE_REVISION_CONFLICT')
                try:
                    if source.revision == record['source_revision']:
                        payload = asdict(new)
                        spec, secrets, _, _ = self._admit_save(payload, source.scope, workspace_root)
                        self._save_admitted(root, leaf, spec, secrets, payload, record['source_revision'], gated)
                    else:
                        record['reconciled_committed_binding'] = True
                    self._migration_state(record, 'SWITCHED')
                except OSError:
                    record['switch_commit_uncertain'] = True
                    raise

    # LLM: 原迁移锁/CAS保持；归一化前解析源及目标引用的既有秘密，连同新秘密清洗目标与返回记录，不影响原状态账身份。
    # 函数用途: 串行准备或恢复迁移，同时遮蔽复用凭据的实际值。
    def migrate_datasource(self, source_id, target_spec, *, mode='prepare_and_validate', workspace_root=None, auto_switch=False):
        if mode not in ('prepare_and_validate', 'switch') or type(auto_switch) is not bool:
            raise ValueError('DATASOURCE_INVALID_MIGRATION_MODE')
        source = self.get_datasource_spec(source_id, workspace_root=workspace_root)
        known_values = (*secret_values(self.resolve_secret(source.credential_ref)),
                        *secret_values(self.resolve_secret(target_spec.get('credential_ref', ''))))
        migration_id = safe_asset_id(target_spec.get('migration_id', 'migration-' + uuid.uuid4().hex))
        _verify_directory_beneath(self.owner_home, ('data', 'datasource_migrations'), create=True)
        with locked_json_path(self.owner_home / 'data/datasource_migrations' / (migration_id + '.json')):
            if 'migration_id' in target_spec:
                record = self._resume_migration(source_id, target_spec, workspace_root)
            else:
                target, secrets = self._normalize({'read_only': source.read_only, 'allowed_tables': source.allowed_tables,
                                                  **target_spec}, source.scope, workspace_root, known_secrets=known_values)
                known_values = (*known_values, *secret_values(secrets))
                if secrets:
                    target = replace(target, credential_ref=self._save_secret(secrets))
                record = {'migration_id': migration_id, 'source_id': source_id, 'source_scope': source.scope,
                          'source_revision': source.revision, 'target_spec': asdict(target),
                          'workspace_root': str(Path(workspace_root).absolute()) if workspace_root else ''}
            known_values = (*known_values, *secret_values(self.resolve_secret(record['target_spec'].get('credential_ref', ''))))
            record['target_spec'] = _redact_spec_fields(record['target_spec'], known_values)
            return self._advance_migration(source, record, mode == 'switch' or auto_switch, workspace_root)

    # LLM: 真正提交前失败写FAILED；提交窗口的OS异常保留unknown，后续可按实际绑定恢复，不谎报已持久化SWITCHED。
    # 函数用途: 在迁移锁内推进状态并区分可确定失败与提交结果未知。
    def _advance_migration(self, source, record, switch, workspace_root):
        if record.get('state') in ('FAILED', 'SWITCHED'):
            return record
        try:
            if not record.get('state'):
                self._prepare_migration(source, record, workspace_root)
            if switch:
                self._switch_migration(record, workspace_root)
        except (OSError, ValueError, sqlite3.Error, KeyError) as error:
            if record.get('switch_commit_uncertain'):
                return {**record, 'effect_outcome': 'unknown', 'error_code': 'DATASOURCE_MIGRATION_COMMIT_UNCERTAIN'}
            if record.get('state') not in ('FAILED', 'SWITCHED'):
                record['error_code'] = 'DATASOURCE_MIGRATION_FAILED'
                record['error_type'] = type(error).__name__
                self._migration_state(record, 'FAILED')
        return record

    # LLM: 复制与验收阶段显式落账；只对实际SQLite内容比较通过的目标允许本地切换。
    # 函数用途: 顺序完成PREPARED、COPYING、VALIDATING和READY_TO_SWITCH。
    def _prepare_migration(self, source, record, workspace_root):
        from contextlib import closing
        self._migration_state(record, 'PREPARED')
        if source.kind != 'sqlite' or source.database_path == ':memory:':
            raise ValueError('DATASOURCE_MIGRATION_SOURCE_UNSUPPORTED')
        target = DatasourceConnectionSpec(**record['target_spec'])
        self._migration_state(record, 'COPYING')
        if target.kind == 'sqlite':
            self._copy_sqlite(source, replace(target, read_only=False), record, workspace_root)
            self._migration_state(record, 'VALIDATING')
            with closing(open_sqlite(target, read_only=True)) as conn:
                actual = sqlite_fingerprint(conn)
        elif target.kind in ('postgres', 'mysql'):
            actual = self._export_sqlite(source, record)
            self._migration_state(record, 'VALIDATING')
        else:
            raise ValueError('DATASOURCE_MIGRATION_TARGET_UNSUPPORTED')
        valid = _json(actual) == _json(record['source_fingerprint'])
        record['validation'] = {'ok': valid, 'row_counts': actual['row_counts'],
                                'scope': 'sql_dump_roundtrip' if target.kind != 'sqlite' else 'sqlite_copy'}
        if not valid:
            raise ValueError('DATASOURCE_MIGRATION_VALIDATION_FAILED')
        self._migration_state(record, 'READY_TO_SWITCH')


# LLM: 规格与秘密唯一落点固定于当前Owner；workspace仅覆盖规格，不提供另一个Owner的钥匙串入口。
# 类用途: 管理数据源公开规格、私有秘密、连接观察及迁移。
class DatasourceStore(_DatasourceMigration):
    # LLM: 仅绑定受信Owner绝对路径，不从规格恢复Owner路径，不启动连接。
    # 函数用途: 创建一个Owner的数据源存储句柄。
    def __init__(self, owner_home):
        self.owner_home = Path(owner_home).absolute()
        if self.owner_home.is_symlink():
            raise ValueError('DATASOURCE_UNSAFE_OWNER')

    # LLM: 所有读口固定受信根与安全段；损坏显式失败，只有真实缺失返回None。
    # 函数用途: 从nofollow私有JSON读取对象。
    def _read(self, root, parts):
        text = read_text_beneath(root, parts)
        if text is None:
            return None
        try:
            value = json.loads(text)
        except ValueError:
            raise ValueError('DATASOURCE_CORRUPT_RECORD') from None
        if not isinstance(value, dict):
            raise ValueError('DATASOURCE_CORRUPT_RECORD')
        return value

    # LLM: 父目录以原nofollow链创建，叶原子替换0600；不在异常中打印待写值。
    # 函数用途: 原子写入规格、秘密或迁移状态。
    def _write(self, root, parts, data):
        ensure_private_dir(root)
        write_text_atomic_beneath(root, parts, _json(data), file_mode=0o600)

    # LLM: scope只接受明确层级，workspace必须由宿主显式提供且不能为链接根。
    # 函数用途: 取得规格唯一层级根及相对目录。
    def _location(self, scope, workspace_root):
        if scope == 'owner':
            return self.owner_home, ('assets', 'datasources')
        if scope != 'workspace' or workspace_root is None:
            raise ValueError('DATASOURCE_WORKSPACE_REQUIRED')
        root = Path(workspace_root).absolute()
        if root.is_symlink() or not root.is_dir():
            raise ValueError('DATASOURCE_UNSAFE_WORKSPACE')
        return root, ('.agent', 'datasources')

    # LLM: 新值生成独立引用；指定旧引用只重新封装同值以兼容升级，认证密文先于公开引用原子提交，不写明文。
    # 函数用途: 将秘密加密写入Owner私有Vault并返回不透明引用。
    def _save_secret(self, secret, *, credential_ref=None):
        ref = safe_asset_id(credential_ref) if credential_ref else 'dscred-' + uuid.uuid4().hex
        self._write(self.owner_home, ('data', 'datasource_secrets', ref + '.json'), _seal_secret(self.owner_home, secret))
        return ref

    # LLM: 仅解析本Owner引用；v1先验MAC后解密，旧字典读兼容，损坏不降级且错误不带正文。
    # 函数用途: 取得内部凭据字典；真实缺失返回空对象，公开出口只用于脱敏。
    def resolve_secret(self, credential_ref):
        if not credential_ref:
            return {}
        safe_asset_id(credential_ref)
        try:
            data = self._read(self.owner_home, ('data', 'datasource_secrets', credential_ref + '.json')) or {}
            return _unseal_secret(self.owner_home, data)
        except (ValueError, TypeError):
            raise PermissionError('DATASOURCE_VAULT_CORRUPTED') from None

    # LLM: 布尔存在事实不提供跨Owner查找或正文；未知引用不映射其它凭据。
    # 函数用途: 判断当前Owner是否持有该数据源秘密。
    def has_secret(self, credential_ref):
        return bool(self.resolve_secret(credential_ref))

    # LLM: 先提取再使用新值及调用方已知秘密脱敏，避免迁移丢失新认证；路径仍只在原可信根内。
    # 函数用途: 归一化安全连接，可创建数据库父目录但不写规格、秘密或数据库正文。
    def _normalize(self, spec_payload, scope, workspace_root, *, known_secrets=()):
        spec, secrets = prepare_datasource_spec(spec_payload, scope, known_secrets=known_secrets)
        self._location(scope, workspace_root)
        if spec.kind == 'sqlite' and spec.database_path != ':memory:':
            path = spec.database_path or str(self.owner_home / 'data/datasources' / (spec.datasource_id + '.sqlite'))
            ensure_private_dir(self.owner_home)
            path = database_path(path, self.owner_home, workspace_root, create_parent=True)
            spec = replace(spec, database_path=str(path))
        return spec, secrets

    # LLM: 两个凭据参数合并且冲突拒绝；原锁/CAS保持，成功保存一律密文（旧引用同值升级），所有秘密参与脱敏。
    # 函数用途: 保存分层连接并返回安全摘要，兼容credential_values别名。
    def save_datasource(self, spec_payload, *, secret_payload=None, credential_values=None, scope='owner', workspace_root=None, expected_revision=None):
        spec, secrets, root, leaf = self._admit_save(spec_payload, scope, workspace_root, (secret_payload, credential_values))
        # 现有与新库的物理闸门先于规格锁；锁外读取只用于发现闸门，锁内重读并核对覆盖后才CAS写回。
        with _exclusive_database_gates(self.owner_home, _database_identities((self._read(root, leaf), spec))) as gated, \
                locked_json_path(root.joinpath(*leaf)):
            spec = self._save_admitted(root, leaf, spec, secrets, spec_payload, expected_revision, gated)
        return self.public_summary(spec)

    # LLM: 只做锁外归一化、秘密合并与目录准备，不取锁不写规格；保存与切换共用，避免切换在已持锁时重入save_datasource。
    # 函数用途: 准入一次规格保存并返回规格、秘密及其规格叶位置。
    def _admit_save(self, spec_payload, scope, workspace_root, secret_payloads=()):
        spec, secrets = self._normalize(spec_payload, scope, workspace_root)
        for payload in secret_payloads:
            if payload is not None and (not isinstance(payload, dict) or any(not isinstance(key, str) for key in payload)):
                raise ValueError('DATASOURCE_INVALID_SECRET')
            for key, value in (payload or {}).items():
                _capture_secret(secrets, key, value)
        root, parts = self._location(scope, workspace_root)
        ensure_private_dir(root)
        _verify_directory_beneath(root, parts, create=True)
        return spec, secrets, root, (*parts, spec.datasource_id + '.json')

    # LLM: 调用方必须已持该规格锁与所需排他闸门；当前库不在已持闸门内说明并发换库，按版本冲突拒绝，原CAS与秘密处理不变。
    # 函数用途: 在已准入的锁内完成一次规格CAS写入并返回新规格。
    def _save_admitted(self, root, leaf, spec, secrets, spec_payload, expected_revision, gated):
        old = self._read(root, leaf)
        if not _database_identities((old,)) <= gated:
            raise ValueError('DATASOURCE_REVISION_CONFLICT')
        revision = old['revision'] if old else 0
        if expected_revision is not None and (type(expected_revision) is not int or revision != expected_revision):
            raise ValueError('DATASOURCE_REVISION_CONFLICT')
        existing = self.resolve_secret(spec.credential_ref)
        known_values = (*secret_values(secrets), *secret_values(existing))
        spec = DatasourceConnectionSpec(**_redact_spec_fields(asdict(spec), known_values))
        spec = replace(spec, revision=revision + 1, created_at=old['created_at'] if old else _now(), updated_at=_now())
        if secrets:
            spec = replace(spec, credential_ref=self._save_secret(secrets))
        elif existing:
            self._save_secret(existing, credential_ref=spec.credential_ref)
        if spec.kind == 'sqlite' and not spec_payload.get('database_path') and spec.database_path != ':memory:':
            self._create_local(spec)
        self._write(root, leaf, asdict(spec))
        return spec

    # LLM: 只给可写SQLite文件签发：完整规格指纹、revision、精确规格叶真实路径与闸门事实；规格叶无秘密，不授予父目录。
    # 函数用途: 生成运行绑定中供SDK写者在共享闸门内复核的控制事实，非写绑定返回None。
    def writer_control(self, spec, *, workspace_root=None):
        if spec.read_only or spec.kind != 'sqlite' or spec.database_path in ('', ':memory:'):
            return None
        root, parts = self._location(spec.scope, workspace_root)
        leaf = root.joinpath(*parts, spec.datasource_id + '.json').resolve(strict=True)
        return {'spec_path': str(leaf), 'spec_sha256': spec_fingerprint(spec), 'revision': spec.revision,
                **database_gate_facts(self.owner_home, spec.database_path)}

    # LLM: 按原nofollow入口仅解析本Owner钥匙串一次；键名排序公开，实际凭据值只用于内存脱敏。
    # 函数用途: 返回保留credential_ref、has_secret并增加credential_keys的公开摘要。
    def public_summary(self, spec):
        credentials = self.resolve_secret(spec.credential_ref)
        return DatasourcePublicSummary(spec, has_secret=bool(credentials), resolved_secrets=secret_values(credentials), credential_keys=credentials.keys())

    # LLM: 默认本机库仅在缺失时排他创建0600，已有文件不截断，连接不会扩展至任意路径。
    # 函数用途: 初始化零配置SQLite文件。
    def _create_local(self, spec):
        from contextlib import closing
        if not Path(spec.database_path).exists():
            fd = os.open(spec.database_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            os.close(fd)
        with closing(open_sqlite(replace(spec, read_only=False))) as conn:
            conn.execute('SELECT 1')

    # LLM: workspace存在即优先，损坏不可回退Owner；只返回无秘密的原规格，SQLite路径每次重查。
    # 函数用途: 解析当前作用域下的有效连接。
    def get_datasource_spec(self, datasource_id, *, workspace_root=None):
        safe_asset_id(datasource_id)
        scopes = ('workspace', 'owner') if workspace_root is not None else ('owner',)
        for scope in scopes:
            root, parts = self._location(scope, workspace_root)
            data = self._read(root, (*parts, datasource_id + '.json'))
            if data is None:
                continue
            spec = DatasourceConnectionSpec(**data)
            if spec.datasource_id != datasource_id or spec.scope != scope:
                raise ValueError('DATASOURCE_RECORD_IDENTITY_MISMATCH')
            if spec.kind == 'sqlite' and spec.database_path != ':memory:':
                database_path(spec.database_path, self.owner_home, workspace_root)
            return spec
        raise KeyError('DATASOURCE_NOT_FOUND')

    # LLM: 只经nofollow枚举本作用域，workspace覆盖Owner同ID；结果无秘密也不创建目录。
    # 函数用途: 列出有效数据源公开摘要。
    def list_datasources(self, *, workspace_root=None):
        ids = set()
        for scope in (('owner', 'workspace') if workspace_root is not None else ('owner',)):
            root, parts = self._location(scope, workspace_root)
            try:
                names = list_names_beneath(root, parts)
            except FileNotFoundError:
                names = []
            ids.update(name[:-5] for name in names if name.endswith('.json'))
        specs = (self.get_datasource_spec(key, workspace_root=workspace_root) for key in sorted(ids))
        return [self.public_summary(spec) for spec in specs]

    # LLM: 连通性与结构验证分别标注，TCP成功不代表数据库认证成功，HTTP不绕过私网/SSRF限制。
    # 函数用途: 有界测试已登记连接，错误只返回类型与稳定码。
    def test_datasource(self, datasource_id, *, workspace_root=None):
        from contextlib import closing
        spec = self.get_datasource_spec(datasource_id, workspace_root=workspace_root)
        started = time.monotonic()
        result = {'ok': False, 'datasource_id': datasource_id, 'kind': spec.kind, 'details': {}, 'error_code': ''}
        try:
            if spec.kind == 'sqlite':
                with closing(open_sqlite(spec, read_only=True)) as conn:
                    assert conn.execute('SELECT 1').fetchone() == (1,)
                result.update(ok=True, details={'observation': 'sqlite_query'})
            else:
                result['details'] = _test_remote(spec, self.resolve_secret(spec.credential_ref))
                result['ok'] = bool(result['details'].get('connected'))
                result['error_code'] = '' if result['ok'] else result['details'].get('error_code', 'DATASOURCE_DRIVER_VALIDATION_ONLY')
        except (OSError, ValueError, sqlite3.Error) as error:
            result.update(error_code='DATASOURCE_CONNECT_FAILED', details={'exception_type': type(error).__name__})
        return {**result, 'latency_ms': (time.monotonic() - started) * 1000}

    # LLM: 仅真实SQLite只读schema查询，不写PRAGMA；表名引用转义，allowed_tables限制公开投影。
    # 函数用途: 返回表字段及准确行数，非SQLite明确报告未支持。
    def inspect_schema(self, datasource_id, *, workspace_root=None):
        from contextlib import closing
        spec = self.get_datasource_spec(datasource_id, workspace_root=workspace_root)
        if spec.kind != 'sqlite':
            return {'ok': False, 'datasource_id': datasource_id, 'kind': spec.kind, 'tables': [], 'error_code': 'DATASOURCE_DRIVER_UNSUPPORTED'}
        tables = []
        with closing(open_sqlite(spec, read_only=True)) as conn:
            names = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
            for (name,) in names:
                if spec.allowed_tables and name not in spec.allowed_tables:
                    continue
                columns = [{'name': row[1], 'type': row[2], 'notnull': bool(row[3]), 'pk': bool(row[5])}
                           for row in conn.execute('PRAGMA table_info(' + sql_identifier(name) + ')')]
                count = conn.execute('SELECT COUNT(*) FROM ' + sql_identifier(name)).fetchone()[0]
                tables.append({'name': name, 'columns': columns, 'row_count_estimate': count})
        return {'ok': True, 'datasource_id': datasource_id, 'kind': spec.kind, 'tables': tables}


# LLM: HTTP只检查公开目标与DNS结果，不发请求；TCP探测明确不证明DB认证，未知驱动保留开放类型但不能伪报支持。
# 函数用途: 返回非SQLite连接的有限结构或TCP观察。
def _test_remote(spec, credentials):
    if spec.kind == 'http_api':
        url = urlsplit(spec.dsn_template)
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password:
            raise ValueError('DATASOURCE_INVALID_HTTP_ENDPOINT')
        addresses = socket.getaddrinfo(url.hostname, url.port or (443 if url.scheme == 'https' else 80), type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise ValueError('DATASOURCE_PRIVATE_HTTP_HOST')
        return {'observation': 'url_and_public_dns_validation', 'connected': False}
    if spec.kind in ('postgres', 'mysql'):
        diagnostic = _test_database_driver(spec, credentials)
        if diagnostic is not None:
            return diagnostic
        endpoint = urlsplit(spec.dsn_template)
        host, port = spec.host or endpoint.hostname, spec.port or endpoint.port or (5432 if spec.kind == 'postgres' else 3306)
        if not host:
            raise ValueError('DATASOURCE_HOST_REQUIRED')
        with socket.create_connection((host, port), timeout=2):
            return {'observation': 'tcp_connect', 'tcp_connected': True, 'authenticated': False, 'connected': False}
    return {'observation': 'unsupported_driver', 'connected': False}


# LLM: 只导入固定支持的已装驱动，不依据options加载任意代码；凭据仅内存传参，驱动异常正文绝不外传。
# 函数用途: 有可用Postgres/MySQL驱动时执行真实SELECT 1，缺驱动才交TCP诊断。
def _test_database_driver(spec, credentials):
    from contextlib import closing
    names = ('psycopg', 'psycopg2') if spec.kind == 'postgres' else ('pymysql', 'mysql.connector')
    driver = None
    for name in names:
        try:
            driver = importlib.import_module(name)
            break
        except ModuleNotFoundError as error:
            if error.name not in (name, name.split('.')[0]):
                raise ValueError('DATASOURCE_DRIVER_IMPORT_FAILED') from None
    if driver is None:
        return None
    endpoint = urlsplit(spec.dsn_template)
    options = {'host': spec.host or endpoint.hostname, 'port': spec.port or endpoint.port or (5432 if spec.kind == 'postgres' else 3306),
               'user': spec.username or unquote(endpoint.username or ''), 'password': credentials.get('password', credentials.get('pass', ''))}
    options['dbname' if spec.kind == 'postgres' else 'database'] = spec.database or unquote(endpoint.path.lstrip('/'))
    options['connection_timeout' if name == 'mysql.connector' else 'connect_timeout'] = 2
    try:
        with closing(driver.connect(**options)) as conn, closing(conn.cursor()) as cursor:
            cursor.execute('SELECT 1')
            valid = cursor.fetchone() == (1,)
        return {'observation': 'sql_select_1', 'authenticated': valid, 'connected': valid, 'driver': name}
    except Exception as error:
        return {'observation': 'sql_query_failed', 'connected': False, 'authenticated': False,
                'error_code': 'DATASOURCE_CONNECT_FAILED', 'exception_type': type(error).__name__, 'driver': name}
