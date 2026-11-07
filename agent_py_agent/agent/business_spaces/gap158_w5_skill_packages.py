"""Owner 技能包管理；文件、学习登记和使用记录仍沿原生权威。

归档先完整预检，再在同一文件系统暂存、校验和替换。资源入口始终从认证的
owner 根逐段 no-follow 打开；技能脚本只是数据，本模块不赋予执行权限。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import io
import json
import math
import os
import re
import secrets
import shutil
import stat
import struct
import threading
import time
import zipfile
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime
from functools import wraps
from pathlib import Path
from types import SimpleNamespace

from ..assets import asset_root_lock
from ..capability.skill_learning_store import LearnedSkill, SkillLearningStore, _registry_from
from ..capability.skill_service import _MAX_SCAN_DEPTH, _MAX_SKILLS_PER_ROOT_COUNT, _validate_card
from ..capability.skill_usage_ledger import (
    MAX_USAGE_LEDGER_EVENTS,
    _normalize_by_skill,
    _summary_sequence,
)
from ..capability.skills import MAX_SKILL_BYTES, parse_skill_file, skill_catalog_roots
from ..common.nofollow_fs import (
    NoFollowPathError,
    open_directory_beneath,
    open_readonly_file_beneath,
    read_bytes_beneath,
    write_bytes_atomic_beneath,
)
from ..contracts.gates.skill_guard import SkillGuardRequest, evaluate_skill_guard_gate
from ..conversation.business_access import BusinessError
from ..gateway_parts.business_api_v13_contracts import V13Call
from ..plugin_host.errors import HostAPIError
from ..user_space.owner_quota import OwnerQuotaChange, owner_quota_enforcer_from_policy
from . import gap158_w5_mcp_servers as mcp

try:
    from .gap158_limits import SKILL_PACKAGE_FILE_MAX_BYTES, SKILL_ZIP_MAX_COMPRESSION_RATIO
except ImportError:
    # W0 未合入时由宿主明确授权沿用同值，不另建数值配置权威。
    SKILL_PACKAGE_FILE_MAX_BYTES, SKILL_ZIP_MAX_COMPRESSION_RATIO = 1048576, 100

_RESOURCE_DIRS = frozenset(('references', 'templates', 'scripts'))
_PAGE_KEY = os.urandom(32)
_PAGES = OrderedDict()
_PAGE_LOCK = threading.RLock()


def _name(value, *, upload=False):
    pattern = r'[a-z][a-z0-9_-]{0,63}' if upload else r'[\w][\w.-]{0,127}'
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None or '..' in value:
        raise BusinessError('INVALID_SCHEMA')
    return value


def _parts(value):
    if (not isinstance(value, str) or not value or len(value) > 4096
            or '\\' in value or '\0' in value or value.startswith(('/', '-'))):
        raise BusinessError('PATH_OUTSIDE_WORKSPACE')
    parts = tuple(value.split('/'))
    if (len(parts) < 2 or parts[0] not in _RESOURCE_DIRS
            or any(not p or p in {'.', '..'} or p.startswith(('.', '-'))
                   or any(ord(c) < 32 for c in p) for p in parts)):
        raise BusinessError('PATH_OUTSIDE_WORKSPACE')
    return parts


def _limits(call):
    config = call.context.host.config
    try:
        count = int(getattr(config, 'skill_guard_max_files', 50))
        size_kb = int(getattr(config, 'skill_guard_max_size_kb', 1024))
    except (TypeError, ValueError, OverflowError) as error:
        raise BusinessError('HOST_NOT_READY') from error
    # 旧配置以零关闭扫描配额时，归档解析仍必须保留有限预算。
    return max(1, count or 50), max(1, size_kb or 1024) * 1024


def _guard_config(call):
    count, size = _limits(call)
    return SimpleNamespace(skill_guard_max_files=count, skill_guard_max_size_kb=size // 1024)


def _entry_error(entry, reason):
    raise BusinessError('ARCHIVE_ENTRY_REJECTED', details={'entry': entry[:4096], 'reason': reason})


def _decode(value, maximum):
    if not isinstance(value, str) or len(value) > ((maximum + 2) // 3) * 4:
        raise BusinessError('LIMIT_EXCEEDED', details={'limit': maximum})
    try:
        payload = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as error:
        raise BusinessError('INVALID_SCHEMA') from error
    if len(payload) > maximum:
        raise BusinessError('LIMIT_EXCEEDED', details={'limit': maximum})
    return payload


def _archive_directory(raw, max_entries):
    """按真实中央目录边界计数，不让伪造 EOCD 先触发 ZipInfo 对象分配。"""
    eocd = raw.rfind(b'PK\x05\x06', max(0, len(raw) - 65557))
    if eocd < 0 or eocd + 22 > len(raw):
        _entry_error('', 'invalid_zip')
    disk, start, local_count, count, directory_size, offset, comment = struct.unpack_from('<4H2IH', raw, eocd + 4)
    if (disk or start or local_count != count or count == 65535
            or offset == 4294967295 or directory_size == 4294967295
            or eocd + 22 + comment != len(raw) or offset + directory_size != eocd):
        _entry_error('', 'unsupported_zip_layout')
    if count > max_entries:
        _entry_error('', 'too_many_entries')
    # 每步只读固定头并跳过有界变长字段，不复制名称、注释或扩展字段。
    position, observed = offset, 0
    while position < eocd:
        if position + 46 > eocd or raw[position:position + 4] != b'PK\x01\x02':
            _entry_error('', 'invalid_central_directory')
        observed += 1
        if observed > max_entries:
            _entry_error('', 'too_many_entries')
        name_size, extra_size, comment_size, disk_start = struct.unpack_from('<4H', raw, position + 28)
        compressed, expanded = struct.unpack_from('<2I', raw, position + 20)
        local_offset = struct.unpack_from('<I', raw, position + 42)[0]
        if (disk_start or compressed == 4294967295 or expanded == 4294967295
                or local_offset == 4294967295 or local_offset >= offset):
            _entry_error('', 'unsupported_zip_layout')
        position += 46 + name_size + extra_size + comment_size
        if position > eocd:
            _entry_error('', 'invalid_central_directory')
    if position != offset + directory_size or observed != count:
        _entry_error('', 'entry_count_mismatch')
    return observed


def _archive(call, name):
    raw = _decode(call.params['archive_b64'], 50331648)
    max_entries, max_size = _limits(call)
    count = _archive_directory(raw, max_entries)
    files, directories, folded, kinds, total = {}, set(), {}, {}, 0
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) != count:
                _entry_error('', 'entry_count_mismatch')
            for entry in entries:
                original = entry.orig_filename
                if '\0' in original:
                    _entry_error(original, 'nul')
                if '\\' in original:
                    _entry_error(original, 'backslash')
                if original.startswith('/') or re.match(r'^[A-Za-z]:', original):
                    _entry_error(original, 'absolute_path')
                is_dir = entry.is_dir()
                path = original[:-1] if is_dir else original
                parts = path.split('/')
                if any(p == '..' for p in parts):
                    _entry_error(original, 'parent_segment')
                if any(not p or p == '.' or any(ord(c) < 32 for c in p) for p in parts):
                    _entry_error(original, 'invalid_path')
                mode = stat.S_IFMT(entry.external_attr >> 16)
                if mode not in (0, stat.S_IFREG, stat.S_IFDIR):
                    _entry_error(original, 'symlink_or_device')
                if (mode == stat.S_IFDIR and not is_dir) or (mode == stat.S_IFREG and is_dir):
                    _entry_error(original, 'mode_mismatch')
                if path in kinds:
                    _entry_error(original, 'duplicate')
                # 隐含父目录不算重复，但大小写不同的父目录同样不可别名。
                if path.casefold() in folded and folded[path.casefold()] != path:
                    _entry_error(original, 'casefold_duplicate')
                for index in range(1, len(parts) + 1):
                    prefix = '/'.join(parts[:index])
                    previous = folded.get(prefix.casefold())
                    if previous is not None and previous != prefix:
                        _entry_error(original, 'casefold_duplicate')
                    folded[prefix.casefold()] = prefix
                    if index < len(parts) and kinds.get(prefix) is False:
                        _entry_error(original, 'file_directory_conflict')
                if not is_dir and any(p.startswith(path + '/') for p in kinds):
                    _entry_error(original, 'file_directory_conflict')
                kinds[path] = is_dir
                total += entry.file_size
                if entry.file_size > SKILL_PACKAGE_FILE_MAX_BYTES:
                    _entry_error(original, 'file_too_large')
                if total > max_size:
                    _entry_error(original, 'too_many_bytes')
                if entry.file_size > max(1, entry.compress_size) * SKILL_ZIP_MAX_COMPRESSION_RATIO:
                    _entry_error(original, 'compression_ratio')
                if entry.flag_bits & 1:
                    _entry_error(original, 'encrypted')
            # 全部路径及预算通过后才解压到内存；CRC/本地头错误也不留下暂存目录。
            for entry in entries:
                if entry.is_dir():
                    directories.add(entry.filename.rstrip('/'))
                    continue
                with archive.open(entry) as stream:
                    payload = stream.read(min(SKILL_PACKAGE_FILE_MAX_BYTES, max_size) + 1)
                if len(payload) != entry.file_size:
                    _entry_error(entry.orig_filename, 'size_mismatch')
                files[entry.filename] = payload
    except BusinessError:
        raise
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, OSError, ValueError):
        _entry_error('', 'invalid_zip_data')
    # 接收平铺包及唯一的同名外层目录，不隐式剥掉任意路径前缀。
    if 'SKILL.md' not in files and files and all(path.startswith(name + '/') for path in files):
        files = {path[len(name) + 1:]: payload for path, payload in files.items()}
        directories = {path[len(name) + 1:] for path in directories if path.startswith(name + '/')}
    if 'SKILL.md' not in files:
        _entry_error('SKILL.md', 'missing_skill_md')
    return files, directories


def _registry(call):
    raw = read_bytes_beneath(call.owner_home, ('data', 'skill_learning', 'registry.json'),
                             max_bytes=MAX_SKILL_BYTES, require_dir_fd=True)
    try:
        return _registry_from(json.loads(raw)).skills if raw is not None else {}
    except (ValueError, TypeError, KeyError) as error:
        raise BusinessError('HOST_NOT_READY') from error


def _canonical_owner(call):
    from ..user_space.owner_resolver import OwnerIdentity, resolve_owner_home

    paths = call.context.host.home_paths
    provider, kind = getattr(paths, 'owner_provider', ''), getattr(paths, 'owner_kind', '')
    if not provider or not kind:
        raise BusinessError('HOST_NOT_READY')
    identity = OwnerIdentity(provider, kind, call.owner_id.rsplit('/', 1)[-1])
    owner = resolve_owner_home(paths.root, identity)
    if owner.owner_id != call.owner_id or owner.home_dir.absolute() != Path(call.owner_home).absolute():
        raise BusinessError('RESOURCE_NOT_FOUND')
    return owner


def _plugin_sources(call):
    """管理写入不能复用发现层的降级空表；直接只读原安装表，坏表明确拒绝。"""
    from ..plugin_install_store import PluginInstallStore
    from ..plugin_skills import plugin_skill_dir
    from ..user_space.owner_resolver import OwnerIdentity, resolve_owner_home

    owner = _canonical_owner(call)
    owners = [(owner, False)]
    if owner.identity.owner_kind == 'user':
        owners.append((resolve_owner_home(owner.root, OwnerIdentity.local_main()), True))
    roots, names = [], set()
    try:
        for source_owner, public_only in owners:
            rows = PluginInstallStore(source_owner).snapshot()
            if len(rows) > _MAX_SKILLS_PER_ROOT_COUNT:
                raise BusinessError('HOST_NOT_READY')
            for row in rows:
                if public_only and row.installation_scope != 'public' or not row.enabled:
                    continue
                declarations = getattr(row.manifest, 'skills', ())
                if len(declarations) > _MAX_SKILLS_PER_ROOT_COUNT:
                    raise BusinessError('HOST_NOT_READY')
                if declarations:
                    names.update(value.casefold() for value in declarations)
                    root = plugin_skill_dir(source_owner, row)
                    # 声明仍活跃但原内容缺失，来源覆盖未知，不能降为“没有冲突”。
                    with _directory(root, ()):
                        pass
                    roots.append((root, 'plugin:' + row.manifest.plugin_id))
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as error:
        if isinstance(error, BusinessError):
            raise
        raise BusinessError('HOST_NOT_READY') from error
    return roots, names


def _session_workspaces(call):
    """流式检查认证 owner 的保留线程；限制单文件和不同工作根，不限制历史总量。"""
    from ..gateway_parts.owner_conversation_store import owner_conversation_store_from_config

    host = call.context.host
    store = getattr(host, 'conversation_store', None)
    if store is None:
        store = owner_conversation_store_from_config(host.config, host.home_paths, initialize=False)
    directory = Path(store.storage.threads_dir)
    try:
        descriptor = open_directory_beneath(directory, (), require_dir_fd=True)
    except FileNotFoundError:
        return ()
    workspaces = set()
    try:
        before = _stamp(os.fstat(descriptor))
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if entry.name.startswith('.') or not entry.name.endswith('.json'):
                    continue
                raw = read_bytes_beneath(directory, (entry.name,), max_bytes=SKILL_PACKAGE_FILE_MAX_BYTES,
                                         require_dir_fd=True)
                if raw is None:
                    raise BusinessError('HOST_NOT_READY')
                row = json.loads(raw)
                if not isinstance(row, dict):
                    raise BusinessError('HOST_NOT_READY')
                if row.get('owner_id') not in (None, '', call.owner_id):
                    continue
                if (row.get('owner_id') != call.owner_id or not row.get('owner_home')
                        or Path(row['owner_home']).absolute() != Path(call.owner_home).absolute()):
                    raise BusinessError('HOST_NOT_READY')
                additional = row.get('runtime_workspace_roots', [])
                if not isinstance(additional, list) or len(additional) > _MAX_SKILLS_PER_ROOT_COUNT:
                    raise BusinessError('HOST_NOT_READY')
                for value in (row.get('workspace_root', ''), row.get('cwd', ''), *additional):
                    if not isinstance(value, str) or '\0' in value:
                        raise BusinessError('HOST_NOT_READY')
                    if value:
                        path = Path(value)
                        if not path.is_absolute():
                            raise BusinessError('HOST_NOT_READY')
                        workspaces.add(path)
                        if len(workspaces) > _MAX_SKILLS_PER_ROOT_COUNT:
                            raise BusinessError('HOST_NOT_READY')
        if before != _stamp(os.fstat(descriptor)):
            raise BusinessError('HOST_NOT_READY')
        # 目录原地改名后读完旧 FD，也不能宣称已经覆盖当前来源。
        with _directory(directory, ()) as current:
            if before != _stamp(os.fstat(current)):
                raise BusinessError('HOST_NOT_READY')
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
        if isinstance(error, BusinessError):
            raise
        raise BusinessError('HOST_NOT_READY') from error
    finally:
        os.close(descriptor)
    return tuple(workspaces)


def _roots(call, plugin_roots=()):
    host, paths = call.context.host, call.context.host.home_paths
    service = getattr(host, 'skills_service', None)
    if (getattr(paths, 'owner_id', None) != call.owner_id
            or Path(paths.owner_home_dir).absolute() != Path(call.owner_home).absolute()):
        raise BusinessError('RESOURCE_NOT_FOUND')
    workspaces = {Path(getattr(paths, 'owner_workspace_dir', Path(call.owner_home) / 'workspace')),
                  *_session_workspaces(call)}
    for value in (getattr(host, 'effective_workspace_root', None), getattr(service, 'workspace_root', None)):
        if value is not None:
            workspaces.add(Path(value))
    if len(workspaces) > _MAX_SKILLS_PER_ROOT_COUNT:
        raise BusinessError('HOST_NOT_READY')
    roots = []

    def add(values):
        for path, source in values:
            if len(roots) >= _MAX_SKILLS_PER_ROOT_COUNT:
                raise BusinessError('HOST_NOT_READY')
            roots.append((path, source))

    extras = [(Path(getattr(paths, key)), source) for key, source in
              (('shared_skills_dir', 'shared'), ('shared_builtin_dir', 'builtin')) if hasattr(paths, key)]
    # 管理写入不受发现服务的展示过滤影响，固定 builtin/shared/workspace 根始终需要覆盖。
    for workspace in workspaces:
        add(skill_catalog_roots(call.owner_home, workspace, extras))
    if service is not None:
        if Path(service.home_paths.owner_home_dir).absolute() != Path(call.owner_home).absolute():
            raise BusinessError('HOST_NOT_READY')
        for workspace in workspaces:
            add((root.path, root.source) for root in service.base_roots(workspace))
        if service.plugin_roots is not None:
            add(service.plugin_roots())
    else:
        provider = getattr(host, '_plugin_skill_roots', None)
        if callable(provider):
            add(provider())
    add(plugin_roots)
    import agent_py_agent
    add(((Path(agent_py_agent.__file__).parent / 'skills' / 'builtin', 'builtin'),))
    return tuple(dict.fromkeys((Path(path).absolute(), source) for path, source in roots))


def _protected(call, name):
    if name == 'learned' or any(item.casefold() == name.casefold() for item in _registry(call)):
        return True
    plugin_roots, plugin_names = _plugin_sources(call)
    if name.casefold() in plugin_names:
        return True
    for root, source in _roots(call, plugin_roots):
        if source == 'owner' and root == Path(call.owner_home) / 'skills':
            continue
        # 来源树只做有限检查；超预算拒绝写入，不把未覆盖区域当不存在。
        pending, examined = [()], 0
        while pending:
            parts = pending.pop()
            try:
                descriptor = open_directory_beneath(root, parts, require_dir_fd=True)
            except FileNotFoundError:
                continue
            try:
                with os.scandir(descriptor) as entries:
                    for entry in entries:
                        examined += 1
                        if examined > _MAX_SKILLS_PER_ROOT_COUNT:
                            raise BusinessError('HOST_NOT_READY')
                        if entry.name.startswith('.'):
                            continue
                        info = entry.stat(follow_symlinks=False)
                        if stat.S_ISLNK(info.st_mode):
                            raise BusinessError('HOST_NOT_READY')
                        if stat.S_ISDIR(info.st_mode):
                            if entry.name.casefold() == name.casefold():
                                return True
                            if len(parts) < _MAX_SCAN_DEPTH:
                                pending.append((*parts, entry.name))
                            else:
                                raise BusinessError('HOST_NOT_READY')
                        elif entry.name == 'SKILL.md':
                            raw = read_bytes_beneath(root, (*parts, 'SKILL.md'), max_bytes=MAX_SKILL_BYTES,
                                                     require_dir_fd=True)
                            from ..capability.skills import split_skill_frontmatter
                            metadata, _ = split_skill_frontmatter(raw.decode('utf-8'))
                            if str(metadata.get('name') or '').casefold() == name.casefold():
                                return True
            finally:
                os.close(descriptor)
    return False


@contextmanager
def _directory(home, parts, *, create=False):
    descriptor = open_directory_beneath(home, tuple(parts), create=create, require_dir_fd=True)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _tree(call, name, *, include_paths=False):
    max_entries, maximum = _limits(call)
    rows, directories, pending, total, entries = [], [], [()], 0, 0
    while pending:
        parts = pending.pop()
        with _directory(call.owner_home, ('skills', name, *parts)) as descriptor:
            with os.scandir(descriptor) as children:
                for child in children:
                    entries += 1
                    if entries > max_entries * 64:
                        raise BusinessError('LIMIT_EXCEEDED', details={'limit': max_entries * 64})
                    relative = (*parts, child.name)
                    info = child.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        directories.append('/'.join(relative))
                        pending.append(relative)
                    elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                        raw = read_bytes_beneath(call.owner_home, ('skills', name, *relative),
                            max_bytes=SKILL_PACKAGE_FILE_MAX_BYTES, require_dir_fd=True)
                        total += len(raw)
                        if total > maximum or len(rows) >= max_entries:
                            raise BusinessError('LIMIT_EXCEEDED', details={'limit': maximum})
                        rows.append(('/'.join(relative), hashlib.sha256(raw).hexdigest()))
                    else:
                        raise NoFollowPathError('技能资源不是普通文件或目录')
    digest = hashlib.sha256(json.dumps([sorted(rows), sorted(directories)],
        ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    result = digest, len(rows), total
    return (*result, [path for path, _digest in rows]) if include_paths else result


def _owner_skill(call, name, *, writable=False):
    _name(name)
    with _directory(call.owner_home, ('skills', name)):
        raw = read_bytes_beneath(call.owner_home, ('skills', name, 'SKILL.md'),
                                 max_bytes=SKILL_PACKAGE_FILE_MAX_BYTES, require_dir_fd=True)
    if raw is None:
        raise BusinessError('RESOURCE_NOT_FOUND')
    if writable and _protected(call, name):
        raise BusinessError('STATE_CONFLICT')
    return name


def _invalidate(call):
    service = getattr(call.context.host, 'skills_service', None)
    if service is not None:
        service.clear_cache()


def _receipt(call, mutate):
    """先耐久预占，再锁内变更文件，最后封原回执；未知请求不得再次执行。"""
    from ..gateway_parts.business_api_v13 import v13_require_access

    v13_require_access(call)
    precommit_denial = [None]
    def work():
        from ..gateway_parts.business_api_v13 import v13_require_access

        effect = False

        def mark_effect():
            nonlocal effect
            effect = True

        gate = owner_quota_enforcer_from_policy(call.owner_home)
        try:
            # FS 不在 Store 回调内执行；仍沿 quota→学习状态→资产根锁，准入退出核实际差额。
            with gate.admission() as admission:
                home = Path(call.owner_home)
                with _directory(home, ('data', 'skill_learning'), create=True):
                    pass
                learning = SkillLearningStore(home / 'data' / 'skill_learning', home / 'skills')
                with learning.state_lock(admission=admission), asset_root_lock(home):
                    v13_require_access(call)
                    result = mutate(admission, mark_effect)
                    _invalidate(call)
            return result
        except BaseException as error:
            if effect:
                raise HostAPIError('OUTCOME_UNKNOWN', 503, 'unknown') from error
            if isinstance(error, (BusinessError, HostAPIError)) and error.code == 'CAPABILITY_DENIED':
                precommit_denial[0] = error
            raise
    def guarded_work():
        # 先把未提交的原文件系统错误按既有领域边界归类，再交回执封存；未知事实保持 HostAPIError。
        return _boundary(lambda _call: work())(call)

    try:
        return mcp.receipted(call, guarded_work, collection='skill_operations', scope='owner')
    except HostAPIError as error:
        if error.code == 'OUTCOME_UNKNOWN' and precommit_denial[0] is not None:
            # 撤权发生在目标变更前时保留真实拒绝；封存被当前授权拒绝也不删除占位或准许重做。
            raise precommit_denial[0] from None
        raise


def _boundary(function):
    @wraps(function)
    def invoke(call: V13Call) -> dict:
        from ..gateway_parts.business_api_v13 import v13_require_access

        v13_require_access(call)
        try:
            return function(call)
        except (BusinessError, HostAPIError):
            raise
        except FileNotFoundError as error:
            raise BusinessError('RESOURCE_NOT_FOUND') from error
        except (NoFollowPathError, NotADirectoryError, IsADirectoryError) as error:
            code = ('PATH_OUTSIDE_WORKSPACE' if call.action in
                    {'skills_pkg.files.read', 'skills_pkg.files.write', 'skills_pkg.files.delete'}
                    else 'STATE_CONFLICT' if call.action in {'skills_pkg.upload_zip', 'skills_pkg.dirs.create'}
                    else 'HOST_NOT_READY')
            raise BusinessError(code) from error
        except (ValueError, TypeError, UnicodeError) as error:
            raise BusinessError('INVALID_SCHEMA') from error
        except OSError as error:
            raise BusinessError('HOST_NOT_READY') from error
    return invoke


@_boundary
# LLM: 复用 owner 配额、原生校验和耐久回执；包 CAS 与发布必须绑定同一父目录身份。
# 函数用途: 安全安装或替换技能包，父目录被换掉就拒绝发布。
def upload_zip(call: V13Call) -> dict:
    name = _name(call.params['name'], upload=True)
    files, directories = _archive(call, name)
    if type(call.params.get('replace', False)) is not bool:
        raise BusinessError('INVALID_SCHEMA')
    def install(admission, mark_effect):
        from ..gateway_parts.business_api_v13 import v13_require_access

        if call.params.get('replace') and re.fullmatch(r'[0-9a-f]{64}', call.params.get('expected_sha256', '')) is None:
            raise BusinessError('REVISION_CONFLICT')
        if _protected(call, name):
            raise BusinessError('STATE_CONFLICT')
        stage, backup = '.w5-stage-' + secrets.token_hex(16), '.w5-old-' + secrets.token_hex(16)
        moved, published = False, False
        chain = _parent_chain(call.owner_home, ('skills',), allow_missing=True)
        with _directory(call.owner_home, ('skills',), create=True) as parent:
            chain = _bound_parent(call.owner_home, ('skills',), parent, chain)
            try:
                old = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                old = None
            old_paths = []
            if old is not None:
                if not stat.S_ISDIR(old.st_mode):
                    raise NoFollowPathError('技能目标不是普通目录')
                if not call.params.get('replace'):
                    raise BusinessError('STATE_CONFLICT')
                if _tree(call, name)[0] != call.params['expected_sha256']:
                    raise BusinessError('REVISION_CONFLICT')
                old_paths = _tree(call, name, include_paths=True)[3]
            elif call.params.get('replace'):
                raise BusinessError('REVISION_CONFLICT')
            admission.check([OwnerQuotaChange(Path(call.owner_home) / 'skills' / stage / path, len(payload))
                             for path, payload in files.items()])
            os.mkdir(stage, mode=0o700, dir_fd=parent)
            try:
                for path in directories:
                    if path:
                        with _directory(call.owner_home, ('skills', stage, *path.split('/')), create=True):
                            pass
                for path, payload in files.items():
                    write_bytes_atomic_beneath(call.owner_home, ('skills', stage, *path.split('/')),
                                              payload, file_mode=0o644)
                staged = _tree(call, stage)
                location = Path(call.owner_home) / 'skills' / stage
                try:
                    card = parse_skill_file(location / 'SKILL.md', source='external', require_frontmatter=True)
                    _validate_card(card)
                except (OSError, ValueError, TypeError):
                    _entry_error('SKILL.md', 'invalid_skill')
                if card.name != name:
                    _entry_error('SKILL.md', 'name_mismatch')
                decision = evaluate_skill_guard_gate(location,
                    SkillGuardRequest(source='external', skill_name=name, config=_guard_config(call)))
                if not decision.allowed:
                    _entry_error('SKILL.md', 'skill_guard_denied')
                _bound_parent(call.owner_home, ('skills',), parent, chain)
                if _tree(call, stage) != staged:
                    _entry_error('', 'staging_changed')
                home = Path(call.owner_home)
                admission.check([
                    *[OwnerQuotaChange(home / 'skills' / stage / path, 0) for path in files],
                    *[OwnerQuotaChange(home / 'skills' / name / path, len(payload))
                      for path, payload in files.items()],
                    *[OwnerQuotaChange(home / 'skills' / name / path, 0)
                      for path in old_paths if path not in files],
                ])
                # 校验期间目标或归属可能变化，提交前在原锁内重核当前权威与摘要。
                v13_require_access(call)
                if _protected(call, name):
                    raise BusinessError('STATE_CONFLICT')
                _bound_parent(call.owner_home, ('skills',), parent, chain)
                if old is not None:
                    current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    if _stamp(current) != _stamp(old) or _tree(call, name)[0] != call.params['expected_sha256']:
                        raise BusinessError('REVISION_CONFLICT')
                    os.replace(name, backup, src_dir_fd=parent, dst_dir_fd=parent)
                    moved = True
                else:
                    try:
                        os.stat(name, dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise BusinessError('STATE_CONFLICT')
                os.replace(stage, name, src_dir_fd=parent, dst_dir_fd=parent)
                published = True
                mark_effect()
                os.fsync(parent)
                digest, count, size = _tree(call, name)
                return {'skill': {'name': name, 'source': 'owner', 'sha256': digest,
                    'files': count, 'bytes': size, 'installed_at': call.clock()}}
            except BaseException as error:
                if moved and not published:
                    try:
                        os.replace(backup, name, src_dir_fd=parent, dst_dir_fd=parent)
                    except OSError as rollback_error:
                        mark_effect()
                        raise HostAPIError('OUTCOME_UNKNOWN', 503, 'unknown') from rollback_error
                if published:
                    raise HostAPIError('OUTCOME_UNKNOWN', 503, 'unknown') from error
                raise
            finally:
                # 清理仅限本次随机私有目录；rmtree 使用 fd 防跟随目录链接。
                for temporary in (stage, backup if published else ''):
                    if temporary:
                        try:
                            shutil.rmtree(temporary, dir_fd=parent)
                        except FileNotFoundError:
                            pass
                        except OSError as error:
                            if published:
                                raise HostAPIError('OUTCOME_UNKNOWN', 503, 'unknown') from error
                            raise BusinessError('HOST_NOT_READY') from error
    return _receipt(call, install)


def _current(home, parts):
    raw = read_bytes_beneath(home, tuple(parts), max_bytes=SKILL_PACKAGE_FILE_MAX_BYTES,
                             require_dir_fd=True)
    return None if raw is None else hashlib.sha256(raw).hexdigest()


def _cas(expected, actual):
    if expected != (actual if actual is not None else 'absent'):
        raise BusinessError('REVISION_CONFLICT')


def _parent_chain(home, parts, *, allow_missing=False):
    """只比目录身份而非 mtime，自己的暂存文件不能使父链自冲突。"""
    chain = []
    for length in range(len(parts) + 1):
        try:
            descriptor = open_directory_beneath(home, tuple(parts[:length]), require_dir_fd=True)
        except FileNotFoundError:
            if allow_missing:
                break
            raise BusinessError('REVISION_CONFLICT') from None
        try:
            info = os.fstat(descriptor)
            chain.append((info.st_dev, info.st_ino, info.st_mode))
        finally:
            os.close(descriptor)
    return tuple(chain)


def _bound_parent(home, parts, descriptor, previous):
    current = _parent_chain(home, parts)
    actual = os.fstat(descriptor)
    if (current[:len(previous)] != previous
            or current[-1] != (actual.st_dev, actual.st_ino, actual.st_mode)):
        raise BusinessError('REVISION_CONFLICT')
    return current


def _current_at(parent, leaf):
    """CAS 读取与 replace/unlink 共用实际父 FD，拒绝链接和读取期间换叶。"""
    try:
        before = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise NoFollowPathError('技能资源不是独占普通文件')
    descriptor = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        if _stamp(before) != _stamp(os.fstat(descriptor)):
            raise BusinessError('REVISION_CONFLICT')
        digest, size = hashlib.sha256(), 0
        while True:
            chunk = os.read(descriptor, min(65536, SKILL_PACKAGE_FILE_MAX_BYTES - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > SKILL_PACKAGE_FILE_MAX_BYTES:
                raise BusinessError('LIMIT_EXCEEDED', details={'limit': SKILL_PACKAGE_FILE_MAX_BYTES})
            digest.update(chunk)
        if (_stamp(before) != _stamp(os.fstat(descriptor))
                or _stamp(before) != _stamp(os.stat(leaf, dir_fd=parent, follow_symlinks=False))):
            raise BusinessError('REVISION_CONFLICT')
        return digest.hexdigest()
    finally:
        os.close(descriptor)


# LLM: mkdir 前后复核同一技能父链；提交前换绑是冲突，已建目录后才发现则保留未知和耐久占位。
# 函数用途: 在仍绑定当前 owner 技能的目录内创建资源目录。
@_boundary
def dirs_create(call: V13Call) -> dict:
    name, directory = _name(call.params['name']), call.params['dir']
    if directory not in _RESOURCE_DIRS:
        raise BusinessError('INVALID_SCHEMA')

    def create(_admission, mark_effect):
        from ..gateway_parts.business_api_v13 import v13_require_access

        _owner_skill(call, name, writable=True)
        created = False
        parts = ('skills', name)
        try:
            chain = _parent_chain(call.owner_home, parts)
        except BusinessError as error:
            if error.code == 'REVISION_CONFLICT':
                raise BusinessError('STATE_CONFLICT') from None
            raise
        with _directory(call.owner_home, parts) as parent:
            try:
                v13_require_access(call)
                _bound_parent(call.owner_home, parts, parent, chain)
                try:
                    os.mkdir(directory, mode=0o700, dir_fd=parent)
                    created = True
                    mark_effect()
                except FileExistsError:
                    with _directory(call.owner_home, ('skills', name, directory)):
                        pass
                _bound_parent(call.owner_home, parts, parent, chain)
                os.fsync(parent)
            except BaseException as error:
                if created:
                    raise HostAPIError('OUTCOME_UNKNOWN', 503, 'unknown') from error
                if isinstance(error, BusinessError) and error.code == 'REVISION_CONFLICT':
                    raise BusinessError('STATE_CONFLICT') from None
                raise
        return {'created': created, 'path': directory}
    return _receipt(call, create)


@_boundary
def files_read(call: V13Call) -> dict:
    name, parts = _name(call.params['name']), _parts(call.params['path'])
    maximum = call.params.get('max_bytes', SKILL_PACKAGE_FILE_MAX_BYTES)
    if type(maximum) is not int or not 1 <= maximum <= SKILL_PACKAGE_FILE_MAX_BYTES:
        raise BusinessError('INVALID_SCHEMA')
    _owner_skill(call, name)
    descriptor = open_readonly_file_beneath(call.owner_home, ('skills', name, *parts))
    try:
        before = os.fstat(descriptor)
        if before.st_size > SKILL_PACKAGE_FILE_MAX_BYTES:
            raise BusinessError('HOST_NOT_READY')
        content, digest, size = bytearray(), hashlib.sha256(), 0
        while True:
            chunk = os.read(descriptor, min(maximum, SKILL_PACKAGE_FILE_MAX_BYTES - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > SKILL_PACKAGE_FILE_MAX_BYTES:
                raise BusinessError('HOST_NOT_READY')
            digest.update(chunk)
            content.extend(chunk[:max(0, maximum - len(content))])
        if _stamp(before) != _stamp(os.fstat(descriptor)):
            raise BusinessError('HOST_NOT_READY')
        return {'path': '/'.join(parts), 'content_b64': base64.b64encode(content).decode(),
                'sha256': digest.hexdigest(), 'size': size, 'truncated': size > len(content)}
    finally:
        os.close(descriptor)


@_boundary
def files_write(call: V13Call) -> dict:
    name, parts = _name(call.params['name']), _parts(call.params['path'])
    payload = _decode(call.params['content_b64'], SKILL_PACKAGE_FILE_MAX_BYTES)
    full = ('skills', name, *parts)

    def write(admission, mark_effect):
        from ..gateway_parts.business_api_v13 import v13_require_access

        _owner_skill(call, name, writable=True)
        expected = _current(call.owner_home, full)
        _cas(call.params['expected_sha256'], expected)
        admission.check([OwnerQuotaChange(Path(call.owner_home).joinpath(*full), len(payload))])
        chain = _parent_chain(call.owner_home, full[:-1], allow_missing=True)
        if len(chain) < len(full):
            # 后续会创建可见资源父目录；即使正文尚未替换，失败也不能保证目标树完全未变。
            mark_effect()
        with _directory(call.owner_home, full[:-1], create=True) as parent:
            chain = _bound_parent(call.owner_home, full[:-1], parent, chain)
            _cas(call.params['expected_sha256'], _current_at(parent, full[-1]))
            temporary = '.w5-file-' + secrets.token_hex(16)
            committed = False
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=parent)
            try:
                os.fchmod(descriptor, 0o644)
                with os.fdopen(descriptor, 'wb', closefd=False) as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(descriptor)
                _cas(call.params['expected_sha256'], _current_at(parent, full[-1]))
                v13_require_access(call)
                _bound_parent(call.owner_home, full[:-1], parent, chain)
                os.replace(temporary, full[-1], src_dir_fd=parent, dst_dir_fd=parent)
                committed = True
                mark_effect()
                os.fsync(parent)
            except BaseException as error:
                if committed:
                    raise HostAPIError('OUTCOME_UNKNOWN', 503, 'unknown') from error
                raise
            finally:
                os.close(descriptor)
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
        return {'path': '/'.join(parts), 'sha256': hashlib.sha256(payload).hexdigest(), 'size': len(payload)}
    return _receipt(call, write)


@_boundary
def files_delete(call: V13Call) -> dict:
    name, parts = _name(call.params['name']), _parts(call.params['path'])
    full = ('skills', name, *parts)

    def delete(admission, mark_effect):
        from ..gateway_parts.business_api_v13 import v13_require_access

        try:
            _owner_skill(call, name, writable=True)
        except (FileNotFoundError, BusinessError) as error:
            if isinstance(error, FileNotFoundError) or error.code == 'RESOURCE_NOT_FOUND':
                return {'deleted': False}
            raise
        actual = _current(call.owner_home, full)
        if actual is None:
            return {'deleted': False}
        _cas(call.params['expected_sha256'], actual)
        admission.check([OwnerQuotaChange(Path(call.owner_home).joinpath(*full), 0)])
        chain = _parent_chain(call.owner_home, full[:-1])
        with _directory(call.owner_home, full[:-1]) as parent:
            _cas(call.params['expected_sha256'], _current_at(parent, full[-1]))
            v13_require_access(call)
            _bound_parent(call.owner_home, full[:-1], parent, chain)
            os.unlink(full[-1], dir_fd=parent)
            mark_effect()
            try:
                os.fsync(parent)
            except OSError as error:
                raise HostAPIError('OUTCOME_UNKNOWN', 503, 'unknown') from error
        return {'deleted': True}
    return _receipt(call, delete)


def _signature(home, parts, *, file=False):
    """重新核整条父链；已打开的旧 FD 不能把被替换的目录晋升成新根。"""
    stamps = []
    for length in range(len(parts) + (0 if file else 1)):
        with _directory(home, parts[:length]) as descriptor:
            stamps.append(_stamp(os.fstat(descriptor)))
    if file:
        try:
            descriptor = open_readonly_file_beneath(home, parts)
        except FileNotFoundError:
            stamps.append(None)
        else:
            try:
                stamps.append(_stamp(os.fstat(descriptor)))
            finally:
                os.close(descriptor)
    return tuple(stamps)


def _page(call, binding, factory, check):
    """进程内短期迭代器只是分页游标，不是持久安装/授权注册表。

    每页最多消费 limit+1 个来源位置，缓存数量与生存期有限；丢失游标显式过期。
    """
    limit = call.params.get('limit', 50)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise BusinessError('INVALID_SCHEMA')
    binding = (call.owner_id, call.subject_id, call.action, str(call.owner_home), *binding)
    cursor = call.params.get('cursor')
    with _PAGE_LOCK:
        now = time.monotonic()
        for key, saved in list(_PAGES.items()):
            if now - saved['at'] > 300:
                _PAGES.pop(key)['iterator'].close()
        if cursor:
            try:
                prefix, key, signature = cursor.split('.')
                expected = hmac.digest(_PAGE_KEY, key.encode(), 'sha256').hex()
                if prefix != 'sp1' or not hmac.compare_digest(expected, signature):
                    raise ValueError('cursor')
                saved = _PAGES[key]
                if saved['binding'] != binding:
                    raise ValueError('binding')
                _PAGES.pop(key)
                if saved['stamp'] != check():
                    saved['iterator'].close()
                    raise ValueError('changed')
            except (ValueError, KeyError, OSError) as error:
                raise BusinessError('CURSOR_EXPIRED') from error
        else:
            saved = {'binding': binding, 'stamp': check(), 'iterator': factory(), 'buffer': [], 'at': now}
        rows, ended = [], False
        try:
            for _ in range(limit):
                try:
                    row = saved['buffer'].pop() if saved['buffer'] else next(saved['iterator'])
                except StopIteration:
                    ended = True
                    break
                if row is not None:
                    rows.append(row)
            if not ended:
                try:
                    saved['buffer'].append(next(saved['iterator']))
                except StopIteration:
                    ended = True
            if saved['stamp'] != check():
                raise BusinessError('CURSOR_EXPIRED')
        except BaseException:
            saved['iterator'].close()
            raise
        if ended:
            saved['iterator'].close()
            return {'items': rows, 'next_cursor': '', 'coverage': 'complete'}
        while len(_PAGES) >= 32:
            _PAGES.popitem(last=False)[1]['iterator'].close()
        key = secrets.token_hex(16)
        saved['at'] = now
        _PAGES[key] = saved
        token = 'sp1.' + key + '.' + hmac.digest(_PAGE_KEY, key.encode(), 'sha256').hex()
        return {'items': rows, 'next_cursor': token, 'coverage': 'partial'}


def _file_rows(call, name, directory):
    with _directory(call.owner_home, ('skills', name, directory)) as descriptor:
        with os.scandir(descriptor) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                if (entry.name.startswith(('.', '-')) or stat.S_ISLNK(info.st_mode)
                        or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                        or stat.S_ISREG(info.st_mode) and info.st_nlink != 1):
                    yield None
                    continue
                yield {'path': directory + '/' + entry.name, 'size': info.st_size if stat.S_ISREG(info.st_mode) else 0,
                       'is_dir': stat.S_ISDIR(info.st_mode)}


@_boundary
def files_list(call: V13Call) -> dict:
    name, directory = _name(call.params['name']), call.params['dir']
    if directory not in _RESOURCE_DIRS:
        raise BusinessError('INVALID_SCHEMA')
    _owner_skill(call, name)
    return _page(call, (name, directory), lambda: _file_rows(call, name, directory),
                 lambda: _signature(call.owner_home, ('skills', name, directory)))


class _JsonStream:
    """逐项消费规范 registry 的 JSON 对象，不为一页加载整个登记表。"""
    def __init__(self, stream):
        self.stream, self.buffer, self.ended = stream, '', False
        self.decoder = json.JSONDecoder()
        self.position_bytes = 0

    def fill(self):
        raw = self.stream.read(min(max(4096, len(self.buffer)), SKILL_PACKAGE_FILE_MAX_BYTES))
        self.buffer += raw
        self.ended = not raw
        self.position_bytes += len(raw.encode('utf-8'))
        if (self.position_bytes > SKILL_PACKAGE_FILE_MAX_BYTES
                or len(self.buffer.encode('utf-8')) > MAX_SKILL_BYTES):
            raise BusinessError('HOST_NOT_READY')

    def peek(self):
        while True:
            self.buffer = self.buffer.lstrip()
            if self.buffer or self.ended:
                return self.buffer[:1]
            self.fill()

    def take(self, character):
        if self.peek() != character:
            raise BusinessError('HOST_NOT_READY')
        self.buffer = self.buffer[1:]

    def value(self):
        self.peek()
        while True:
            try:
                value, end = self.decoder.raw_decode(self.buffer)
            except (ValueError, RecursionError):
                if self.ended:
                    raise BusinessError('HOST_NOT_READY') from None
                self.fill()
                continue
            if end == len(self.buffer) and not self.ended:
                self.fill()
                continue
            self.buffer = self.buffer[end:]
            return value


def _registered_rows(call):
    try:
        descriptor = open_readonly_file_beneath(call.owner_home, ('data', 'skill_learning', 'registry.json'))
    except FileNotFoundError:
        return
    with os.fdopen(descriptor, 'r', encoding='utf-8') as stream:
        source = _JsonStream(stream)
        source.take('{')
        first, found = True, False
        while source.peek() != '}':
            if not first:
                source.take(',')
            first = False
            key = source.value()
            if not isinstance(key, str):
                raise BusinessError('HOST_NOT_READY')
            source.take(':')
            if key != 'skills':
                source.value()
                continue
            if found:
                raise BusinessError('HOST_NOT_READY')
            found = True
            source.take('{')
            initial = True
            while source.peek() != '}':
                if not initial:
                    source.take(',')
                initial = False
                name = source.value()
                source.take(':')
                row = source.value()
                try:
                    item = LearnedSkill.from_record(row)
                except (TypeError, ValueError, KeyError) as error:
                    raise BusinessError('HOST_NOT_READY') from error
                if item.name != name:
                    raise BusinessError('HOST_NOT_READY')
                yield name, ('skills', name) if item.layout == 'owner' else ('skills', 'learned', name), 'learned'
                source.position_bytes = 0
            source.take('}')
        source.take('}')
        if source.peek() or not found:
            raise BusinessError('HOST_NOT_READY')


def _missing_reason(call, parts):
    try:
        with _directory(call.owner_home, parts):
            pass
    except FileNotFoundError:
        return 'directory_missing'
    if read_bytes_beneath(call.owner_home, (*parts, 'SKILL.md'), max_bytes=SKILL_PACKAGE_FILE_MAX_BYTES,
                          require_dir_fd=True) is None:
        return 'skill_md_missing'
    return None


def _missing_rows(call):
    seen = set()
    for name, parts, source in _registered_rows(call):
        if name in seen:
            raise BusinessError('HOST_NOT_READY')
        seen.add(name)
        if len(seen) > _MAX_SKILLS_PER_ROOT_COUNT:
            raise BusinessError('HOST_NOT_READY')
        reason = _missing_reason(call, parts)
        yield {'name': name, 'reason': reason, 'source': source} if reason else None
    # 复用原发现服务已经持有的索引；不能为只读缺失检查再全盘发现或创建新安装表。
    service = getattr(call.context.host, 'skills_service', None)
    cached = getattr(service, '_manifest_cache', None)
    manifest = cached[2] if cached else ()
    home = Path(call.owner_home).absolute()
    for item in manifest:
        path = Path(item.path).absolute()
        if item.root.source != 'owner' or not path.is_relative_to(home / 'skills'):
            continue
        name = path.parent.name
        if name in seen:
            yield None
            continue
        seen.add(name)
        reason = _missing_reason(call, path.parent.relative_to(home).parts)
        yield {'name': name, 'reason': reason, 'source': 'owner'} if reason else None


def _registry_signature(call):
    try:
        signature = _signature(call.owner_home, ('data', 'skill_learning', 'registry.json'), file=True)
    except FileNotFoundError:
        signature = (None,)
    service = getattr(call.context.host, 'skills_service', None)
    cached = getattr(service, '_manifest_cache', None)
    return signature, id(cached)


@_boundary
def missing(call: V13Call) -> dict:
    return _page(call, (), lambda: _missing_rows(call), lambda: _registry_signature(call))


def _usage_summary_bytes(call):
    try:
        return read_bytes_beneath(call.owner_home, ('data', 'skill_learning', 'usage_summary.json'),
                                 max_bytes=SKILL_PACKAGE_FILE_MAX_BYTES, require_dir_fd=True)
    except (OSError, ValueError, TypeError) as error:
        raise BusinessError('HOST_NOT_READY') from error


def _usage_watermark(raw):
    """用原 summary 校验水位与累计结构；不修复、不补造丢失明细。"""
    if raw is None:
        return None
    try:
        summary = json.loads(raw)
        if (not isinstance(summary, dict) or not isinstance(summary.get('totals'), dict)
                or not isinstance(summary.get('by_skill'), dict)
                or not isinstance(summary.get('daily_history'), dict)):
            raise ValueError('invalid usage summary')
        _summary_sequence(summary)
        _normalize_by_skill(summary)
        return summary['last_sequence']
    except (TypeError, ValueError, KeyError, AttributeError, RecursionError) as error:
        raise BusinessError('HOST_NOT_READY') from error


@_boundary
def usage(call: V13Call) -> dict:
    name = _name(call.params['name'])
    _owner_skill(call, name)
    now, since = float(call.clock()), call.params.get('since', 0)
    if type(since) not in (int, float) or not math.isfinite(since) or not 0 <= since <= now:
        raise BusinessError('INVALID_SCHEMA')
    summary_raw = _usage_summary_bytes(call)
    watermark = _usage_watermark(summary_raw)
    parts = ('data', 'skill_learning', 'usage_ledger.jsonl')
    try:
        descriptor = open_readonly_file_beneath(call.owner_home, parts)
    except FileNotFoundError:
        if _usage_summary_bytes(call) != summary_raw:
            raise BusinessError('HOST_NOT_READY') from None
        return {'name': name, 'loads': 0, 'window': {'since': since, 'until': now},
                'coverage': 'partial' if watermark else 'complete'}
    try:
        before = os.fstat(descriptor)
        start = max(0, before.st_size - SKILL_PACKAGE_FILE_MAX_BYTES)
        raw = os.pread(descriptor, min(before.st_size, SKILL_PACKAGE_FILE_MAX_BYTES), start)
        if _stamp(before) != _stamp(os.fstat(descriptor)):
            raise BusinessError('HOST_NOT_READY')
    finally:
        os.close(descriptor)
    if start:
        raw = raw.partition(b'\n')[2]
    lines = raw.splitlines()
    clamped = bool(start) or len(lines) > MAX_USAGE_LEDGER_EVENTS
    lines = lines[-MAX_USAGE_LEDGER_EVENTS:]
    counts, last, earliest, previous = 0, None, None, None
    identities = {name, 'owner:' + name, 'skill:' + name, 'skill:owner:' + name}
    first_sequence = None
    for line in lines:
        try:
            row = json.loads(line)
            timestamp = datetime.fromisoformat(row['at'].replace('Z', '+00:00'))
            if timestamp.tzinfo is None:
                raise ValueError('usage timezone missing')
            at = timestamp.timestamp()
            sequence = row.get('sequence')
            if (not math.isfinite(at) or not isinstance(row.get('skill_id'), str)
                    or row.get('event') not in {'manual_search', 'manual_get', 'host_injected', 'adopted'}
                    or sequence is not None and (type(sequence) is not int or sequence < 1
                                                or previous is not None and sequence <= previous)):
                raise ValueError('invalid usage')
        except (TypeError, ValueError, KeyError, AttributeError) as error:
            raise BusinessError('HOST_NOT_READY') from error
        if earliest is None:
            earliest, first_sequence = at, sequence
        earliest = min(earliest, at)
        if sequence is None or previous is not None and sequence != previous + 1:
            clamped = True
        previous = sequence
        if (since <= at <= now and row['skill_id'] in identities
                and row.get('kind', 'skill') == 'skill' and row['event'] in {'manual_get', 'host_injected'}):
            counts += 1
            last = at if last is None else max(last, at)
    # summary 水位高于保留明细末端，或明细已前进但汇总未同步，均不能声明完整。
    if watermark is not None and watermark != (previous or 0):
        clamped = True
    if _usage_summary_bytes(call) != summary_raw:
        raise BusinessError('HOST_NOT_READY')
    clamped = clamped or bool(earliest is not None and since < earliest and first_sequence != 1)
    result = {'name': name, 'loads': counts, 'window': {'since': max(since, earliest) if clamped and earliest else since,
              'until': now}, 'coverage': 'partial' if clamped else 'complete'}
    if last is not None:
        result['last_loaded_at'] = last
    return result


HANDLERS = {
    'skills_pkg.upload_zip': upload_zip,
    'skills_pkg.missing': missing,
    'skills_pkg.usage': usage,
    'skills_pkg.dirs.create': dirs_create,
    'skills_pkg.files.list': files_list,
    'skills_pkg.files.read': files_read,
    'skills_pkg.files.write': files_write,
    'skills_pkg.files.delete': files_delete,
}
