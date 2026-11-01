# LLM: 仅执行授权nofollow快照；debug可禁摘要但不绕过数据源、沙箱、超时或清理，不冒称内核隔离。
# 模块用途: 执行Owner脚本并脱敏回执，正式运行回写摘要，内存调试不改资产计数和修订。
from __future__ import annotations

import inspect
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from ..assets.datasource_store import redact_datasource_values
from ..assets.delivery_contracts import AssetDisabledError
from ..assets.descriptor import (
    access_target_requires_network,
    execution_definition_ref,
    output_contract_fields,
)
from ..assets.script_store import ScriptStore
from ..attempt.sandbox import (
    AttemptExecutionSandbox,
    AttemptSandboxSpec,
    SandboxUnavailableError,
    gateway_bound_ports,
)
from ..common.log_redaction import redact_sensitive_value
from ..common.nofollow_fs import ensure_private_dir, read_text_beneath, write_text_atomic_beneath
from ..path_access_policy import (
    HostSandboxPaths,
    owner_home_containing,
    persona_process_protected_paths,
)
from ..plugin_sandbox import resolve_python_runtime_prefixes
from ..tooling._subprocess_env_scrub import scrub_subprocess_env
from ..tooling.write_boundary import SandboxWriteProtection, ordered_write_scope_roots
from .datasource_runtime import ScriptDatasourceRuntime
from .models import ScriptExecutionResult, payload, safe_path

# 原生工作流脚本标准输出与错误流保留的最大字节数。
MAX_OUTPUT_BYTES = 1024 * 1024
# 原生工作流步骤回执保留的输出预览最大字符数。
PREVIEW_CHARS = 4096
_TIMING_FIELDS = ('configuration_load_ms', 'directory_discovery_ms', 'environment_setup_ms',
                  'process_start_ms', 'execution_ms')
_JSON_OUTPUT_TYPES = {
    'string': (str,), 'integer': (int,), 'number': (int, float),
    'boolean': (bool,), 'object': (dict,), 'array': (list,), 'null': (type(None),),
}
_PYTHON_GUARD_CODE = """# LLM: Python守卫只作OS沙箱的补充，冻结路径与端口；用户代码执行前安装，不冒称内核隔离。
# 函数用途: 初始化脚本的文件metadata与网络出口检查。
def _init_guard():
    import errno, ipaddress, json, os, re, socket, sys, threading, types
    from pathlib import Path
    from urllib.parse import unquote
    cfg = json.loads(os.environ.pop("MY_AGENT_SCRIPT_GUARD_JSON", "{}"))
    entrypoint = sys.argv.pop(1)
    sys.argv[0] = entrypoint
    read_roots = tuple(Path(p) for p in cfg.get("read_roots", ()))
    runtime_roots = tuple(Path(p) for p in cfg.get("runtime_read_roots", ()))
    write_roots = tuple(Path(p) for p in cfg.get("write_roots", ()))
    write_rules = tuple((Path(root), allowed) for root, allowed in cfg.get("write_scope_rules", ()))
    protected_paths = tuple(Path(p) for p in cfg.get("protected_write_paths", ()))
    write_patterns = tuple(re.compile(p, re.IGNORECASE) for p in cfg.get("protected_write_patterns", ()))
    directory_write_patterns = tuple(re.compile(p, re.IGNORECASE) for p in cfg.get("protected_directory_write_patterns", ()))
    directory_unlink_patterns = tuple(re.compile(p, re.IGNORECASE) for p in cfg.get("protected_directory_unlink_patterns", ()))
    denied_roots = tuple(Path(p) for p in cfg.get("denied_roots", ()))
    exclusive_roots = tuple(Path(p) for p in cfg.get("host_exclusive_paths", ()))
    owner_scope = Path(cfg["owner_scope_root"]) if cfg.get("owner_scope_root") else None
    app_home = Path(cfg["agent_home_root"]) if owner_scope is not None else None
    protected_nodes = frozenset(tuple(part.casefold() for part in parent.parts)
        for root in (*protected_paths, *exclusive_roots) for parent in root.parents)
    allow_net = bool(cfg.get("network_access", False))
    denied_ports = {int(p) for p in cfg.get("denied_ports", ()) if int(p) > 0}
    block_subproc = bool(cfg.get("block_subprocess", False))
    _guard_tls = threading.local()
    fsdecode = os.fsdecode
    perm_err = PermissionError
    eacces = errno.EACCES
    w_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

    # LLM: 纯路径层级比较，不读取文件系统或匹配字符串前缀。
    # 函数用途: 判断路径是否在明确的根目录内。
    def _under(target, roots):
        return any(target == r or r in target.parents for r in roots)

    # LLM: 绝对保护沿宿主路径门的大小写口径，普通允许/禁止根仍使用原路径层级规则。
    # 函数用途: 拒绝保护子树的大小写别名，不把其祖先的业务文件一起拒绝。
    def _protected(target, roots):
        parts = tuple(part.casefold() for part in target.parts)
        return any(parts[:len(root.parts)] == tuple(part.casefold() for part in root.parts) for root in roots)

    # 冻结owner墙的app home身份在用户代码前取得；宿主解释器根只有严格位于app home内才参与豁免，宽根不能开放控制面。
    home_identity, home_nodes = None, {}
    if app_home is not None:
        home_info = os.stat(app_home)
        home_identity = (home_info.st_dev, home_info.st_ino)
    owner_visible = (owner_scope, app_home / "shared") if app_home is not None else ()
    home_runtime_roots = tuple(root for root in runtime_roots if app_home is not None and app_home in root.parents)

    # LLM: 规范拼写沿宿主大小写口径比较；firmlink、大小写等不经symlink的别名逐层比对目录身份，只缓存元数据事实不读内容。
    # 函数用途: 返回已解析目标相对规范app home的各段，不在app home内时返回None。
    def _app_home_parts(target):
        if _protected(target, (app_home,)):
            return target.parts[len(app_home.parts):]
        for index in range(len(target.parts), 0, -1):
            node = str(Path(*target.parts[:index]))
            if node not in home_nodes:
                previous = getattr(_guard_tls, "active", False)
                _guard_tls.active = True
                try:
                    info = os.lstat(node)
                    home_nodes[node] = (info.st_dev, info.st_ino) == home_identity
                except OSError:
                    home_nodes[node] = False
                finally:
                    _guard_tls.active = previous
            if home_nodes[node]:
                return target.parts[index:]
        return None

    # LLM: 沿PathAccessPolicy._owner_scope_decision：app home内只放本owner与shared，owners下其余子树恒拒，其它数据/控制路径
    #   仅由宿主计算的解释器运行根豁免；app home外保持原行为，正向授权根不是全局白名单，也不能越过本墙。
    # 函数用途: 判断已解析目标是否越过冻结owner读取边界，未冻结owner墙时不拒绝。
    def _owner_denied(target):
        parts = _app_home_parts(target) if owner_scope is not None else None
        if parts is None:
            return False
        canonical = app_home.joinpath(*parts)
        return not _under(canonical, owner_visible) and (
            _protected(canonical, (app_home / "owners",)) or not _under(canonical, home_runtime_roots))

    # 隐藏来源与越过owner墙的正根都不能进入metadata祖先豁免，nofollow链也不能直接打开它们；合法根的祖先只给metadata。
    read_ancestors = frozenset(parent for root in (*read_roots, *runtime_roots, *write_roots) if not _owner_denied(root)
        for parent in root.parents
        if not _protected(parent, exclusive_roots)
        and not any(_under(parent, (private,)) and not _under(root, (private,)) for private in denied_roots))

    # LLM: 原生OS路径不展开波浪号；仅内部解析暂时绕过metadata包装，用户PathLike先在TLS豁免之外转换。
    # 函数用途: 无递归地得到读取或写入操作的真实目标。
    def _resolve(raw):
        raw = fsdecode(raw)
        previous = getattr(_guard_tls, "active", False)
        _guard_tls.active = True
        try:
            return Path(raw).resolve(strict=False)
        finally:
            _guard_tls.active = previous

    # LLM: 已解析的路径只消费冻结根，不重复跟随lstat允许观察的symlink叶节点；宿主专有来源最先拒，owner墙先于正根附加授权。
    # 函数用途: 执行私有读取限制的路径裁决。
    def _check_read_target(target, raw):
        if _protected(target, exclusive_roots) or _owner_denied(target):
            raise perm_err(eacces, "Permission denied", str(raw))
        if _under(target, denied_roots) and not _under(target, (*read_roots, *runtime_roots)):
            raise perm_err(eacces, "Permission denied", str(raw))

    # LLM: 文件内容读取沿用真实目标边界，不能由符号链接逃出获准根。
    # 函数用途: 检查一次普通文件读取。
    def _check_read(raw):
        _check_read_target(_resolve(raw), raw)

    # LLM: 先拒绝绝对路径、祖先及原宿主记录/目录投影，再消费ordered规则；目录unlink与创建分责，不误拦同名普通文件。
    # 函数用途: 让Python回退同样保住冻结范围与任务保护，最具体规则生效且同层deny胜。
    def _check_write(raw, *, directory=False, unlink=False):
        target = _resolve(raw)
        if (_protected(target, (*protected_paths, *exclusive_roots))
                or tuple(part.casefold() for part in target.parts) in protected_nodes
                or any(pattern.search(str(target)) for pattern in write_patterns)):
            raise perm_err(eacces, "Permission denied", str(raw))
        if directory_write_patterns or unlink and directory_unlink_patterns:
            previous = getattr(_guard_tls, "active", False)
            _guard_tls.active = True
            try:
                directory = directory or target.is_dir()
            finally:
                _guard_tls.active = previous
            if directory and any(pattern.search(str(target)) for pattern in
                    (*directory_write_patterns, *(directory_unlink_patterns if unlink else ()))):
                raise perm_err(eacces, "Permission denied", str(raw))
        for root, allowed in reversed(write_rules):
            if _under(target, (root,)):
                if allowed:
                    return
                break
        raise perm_err(eacces, "Permission denied", str(raw))

    # LLM: 仅从内核FD路径表还原dir_fd真实目录；未知或非整数FD一律拒绝，不信任调用方传入的相对父目录。
    # 函数用途: 将dir_fd还原成绝对目录路径以供沙箱边界核验。
    def _fd_path(dir_fd):
        if type(dir_fd) is not int or dir_fd < 0:
            raise perm_err(eacces, "script dir_fd access denied")
        previous = getattr(_guard_tls, "active", False)
        _guard_tls.active = True
        try:
            if Path("/proc/self/fd").is_dir():
                return Path(os.readlink(f"/proc/self/fd/{dir_fd}"))
            import fcntl
            raw = fcntl.fcntl(dir_fd, getattr(fcntl, "F_GETPATH", 50), bytes(1024)).split(bytes([0]), 1)[0]
            if raw:
                return Path(fsdecode(raw))
        except Exception:
            pass
        finally:
            _guard_tls.active = previous
        raise perm_err(eacces, "script dir_fd access denied")

    # LLM: dir_fd和文件FD还原真实目标；目录创建与unlink标志来自审计事件，不从脚本参数自报保护豁免。
    # 函数用途: 将路径或文件描述符修改交给同一个写门并保留目录操作语义。
    def _check_mutation(path, dir_fd=None, *, directory=False, unlink=False):
        if type(path) is int:
            _check_write(str(_fd_path(path)), directory=directory, unlink=unlink)
            return
        text = fsdecode(path)
        if dir_fd not in (None, -1, getattr(os, "AT_FDCWD", -100)) and not Path(text).is_absolute():
            text = str(_fd_path(dir_fd) / text)
        _check_write(text, directory=directory, unlink=unlink)

    # LLM: metadata仅豁免必需祖先或既有FD；dir_fd先还原真实父路径，lstat保留symlink/..顺序，末尾斜杠和点必须跟随目标。
    # 函数用途: 阻断私有文件存在性、权限和属性探测，保留合法路径解析与nofollow目录链。
    def _check_stat(path, dir_fd=None, *, follow_symlinks=True):
        if dir_fd is not None and type(dir_fd) is not int:
            raise perm_err(eacces, "script dir_fd access denied")
        follow_symlinks = bool(follow_symlinks)
        if getattr(_guard_tls, "active", False) or type(path) is int:
            return
        text = fsdecode(path)
        _guard_tls.active = True
        try:
            if dir_fd is not None and dir_fd != getattr(os, "AT_FDCWD", None) and not Path(text).is_absolute():
                text = str(_fd_path(dir_fd) / text)
            lexical = Path(text).absolute()
            follow_leaf = follow_symlinks or text.endswith((os.sep, os.sep + '.', os.sep + '..')) or lexical.name == '..'
            target = _resolve(text) if follow_leaf else _resolve(lexical.parent) / lexical.name
            if target not in read_ancestors:
                _check_read_target(target, path)
        finally:
            _guard_tls.active = False

    original_stat, original_lstat, original_access, original_open = os.stat, os.lstat, os.access, os.open

    # LLM: 还原dir_fd相对路径；只读O_DIRECTORY允许沿read_ancestors逐层打开nofollow目录句柄，其余读写仍按冻结根裁决。
    # 函数用途: 包装os.open以支持SDK严格nofollow目录链并阻断dir_fd越界打开。
    def _guarded_open(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is not None and type(dir_fd) is not int:
            raise perm_err(eacces, "script dir_fd access denied")
        raw = os.fspath(path)
        flags, mode = int(flags), int(mode)
        if not getattr(_guard_tls, "active", False):
            text = fsdecode(raw)
            if dir_fd is not None and dir_fd != getattr(os, "AT_FDCWD", None) and not Path(text).is_absolute():
                text = str(_fd_path(dir_fd) / text)
            if flags & w_flags:
                _check_write(text)
            elif not ((flags & getattr(os, "O_DIRECTORY", 0)) and _resolve(text) in read_ancestors):
                _check_read(text)
        previous = getattr(_guard_tls, "active", False)
        _guard_tls.active = True
        try:
            return original_open(raw, flags, mode, dir_fd=dir_fd)
        finally:
            _guard_tls.active = previous

    # LLM: PathLike只转换一次，传入内核的路径与验证路径相同；保留FD与不跟随叶链接语义。
    # 函数用途: 包装stat并保留原返回值和关键字参数。
    def _guarded_stat(path, *, dir_fd=None, follow_symlinks=True):
        raw = path if type(path) is int else os.fspath(path)
        follow_symlinks = bool(follow_symlinks)
        _check_stat(raw, dir_fd, follow_symlinks=follow_symlinks)
        return original_stat(raw, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    # LLM: 检查链接叶本身但不允许父目录链接绕进私有根；原始系统函数只收到同一份路径。
    # 函数用途: 包装lstat并拒绝dir_fd旁路。
    def _guarded_lstat(path, *, dir_fd=None):
        raw = path if type(path) is int else os.fspath(path)
        _check_stat(raw, dir_fd, follow_symlinks=False)
        return original_lstat(raw, dir_fd=dir_fd)

    # LLM: 拒绝私有路径时抛PermissionError，合法路径原样返回布尔值，不伪造可达性。
    # 函数用途: 包装access的路径与关键字参数。
    def _guarded_access(path, mode, *, dir_fd=None, effective_ids=False, follow_symlinks=True):
        raw = path if type(path) is int else os.fspath(path)
        mode, effective_ids, follow_symlinks = int(mode), bool(effective_ids), bool(follow_symlinks)
        _check_stat(raw, dir_fd, follow_symlinks=follow_symlinks)
        return original_access(raw, mode, dir_fd=dir_fd, effective_ids=effective_ids, follow_symlinks=follow_symlinks)

    # LLM: 不发DNS查询；规范IPv4/IPv6、mapped与历史数值IPv4形式仍指向同一回环边界。
    # 函数用途: 判断一个目标主机名是否属于本机或回环地址。
    def _local_host(host):
        host = fsdecode(host).lower().rstrip('.')
        if host in ("", "0.0.0.0", "::", "::1", "localhost") or host.startswith(("127.", "::ffff:127.")):
            return True
        try:
            address = ipaddress.ip_address(host.split('%', 1)[0])
            mapped = getattr(address, "ipv4_mapped", None)
            return address.is_loopback or address.is_unspecified or bool(mapped and mapped.is_loopback)
        except ValueError:
            try:
                packed = socket.inet_aton(host)
                return packed[0] == 127 or packed == bytes(4)
            except OSError:
                return False

    # LLM: 先核网络总开关，再核宿主实际登记端口；非IP的AF_UNIX目标不从端口规则另行授权。
    # 函数用途: 在socket操作前拒绝禁网或本机Gateway连接。
    def _check_network(address):
        if not allow_net:
            raise perm_err("script network access denied")
        if not denied_ports or not isinstance(address, (tuple, list)) or len(address) < 2:
            return
        try:
            port = int(address[1])
        except (TypeError, ValueError):
            return
        if port in denied_ports and _local_host(address[0]):
            raise perm_err(f"script gateway port access denied: {port}")

    original_connect, original_connect_ex = socket.socket.connect, socket.socket.connect_ex
    original_sendto, original_create_connection = socket.socket.sendto, socket.create_connection

    # LLM: connect在进入原生方法前检查，不依赖原生方法是否发出审计事件。
    # 函数用途: 守住TCP或已连接UDP的目标地址。
    def _guarded_connect(client, address):
        _check_network(address)
        return original_connect(client, address)

    # LLM: connect_ex不能靠整数错误码旁路PermissionError，合法调用保留原整数结果。
    # 函数用途: 检查返回错误码形式的连接操作。
    def _guarded_connect_ex(client, address):
        _check_network(address)
        return original_connect_ex(client, address)

    # LLM: 同时支持sendto(data,address)与sendto(data,flags,address)，检查发生在原生发送之前。
    # 函数用途: 拒绝未连接UDP向禁用目标发包。
    def _guarded_sendto(client, data, *args, **kwargs):
        _check_network(args[-1] if args else kwargs.get("address"))
        return original_sendto(client, data, *args, **kwargs)

    # LLM: 高层入口先检查原始地址，后续解析得到的实际socket.connect仍再次检查。
    # 函数用途: 守住标准库便捷连接入口并保留所有可选参数。
    def _guarded_create_connection(address, *args, **kwargs):
        _check_network(address)
        return original_create_connection(address, *args, **kwargs)

    # LLM: 审计钩子沿原网络/子进程规则；修改操作先还原dir_fd或文件FD，不能用scratch同名路径穿过绝对保护。
    # 函数用途: 在低层文件修改与网络操作前执行真实目标边界检查。
    def _audit(event, args):
        if event in ("socket.connect", "socket.sendto"):
            _check_network(args[1])
        elif not allow_net and event in ("socket.bind", "socket.getaddrinfo"):
            raise perm_err("script network access denied")
        if block_subproc and event in (
            "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn",
            "os.fork", "os.forkpty", "os.spawn", "ctypes.dlopen", "gc.get_objects", "gc.get_referrers",
        ):
            raise perm_err(eacces, "Subprocess execution denied without inner OS sandbox")
        if event == "open":
            if getattr(_guard_tls, "active", False):
                return
            path_arg, mode, flags = args[0], args[1], args[2]
            if isinstance(path_arg, int):
                return
            is_w = bool(flags & w_flags) or any(c in str(mode or "") for c in ("w", "a", "+", "x"))
            (_check_write if is_w else _check_read)(path_arg)
        elif event in ("os.mkdir", "os.remove", "os.unlink", "os.rmdir", "os.chmod", "os.chown", "os.truncate", "os.utime"):
            fd_index = {"os.mkdir": 2, "os.remove": 1, "os.unlink": 1, "os.rmdir": 1,
                        "os.chmod": 2, "os.chown": 3, "os.utime": 3}.get(event)
            _check_mutation(args[0], args[fd_index] if fd_index is not None and len(args) > fd_index else None,
                            directory=event == "os.mkdir", unlink=event in ("os.remove", "os.unlink", "os.rmdir"))
        elif event in ("os.rename", "os.replace", "os.link"):
            _check_mutation(args[0], args[2] if len(args) > 2 else None, unlink=event != "os.link")
            _check_mutation(args[1], args[3] if len(args) > 3 else None, unlink=event != "os.link")
        elif event == "os.symlink":
            _check_write(args[0])
            _check_mutation(args[1], args[2] if len(args) > 2 else None)
        elif event in ("os.listdir", "os.scandir", "os.readlink"):
            if args and not getattr(_guard_tls, "active", False):
                _check_read(str(_fd_path(args[0])) if isinstance(args[0], int) else args[0])
        elif event == "sqlite3.connect":
            raw_db = str(args[0] or "") if args else ""
            if raw_db and raw_db != ":memory:" and not raw_db.startswith("file::memory:"):
                if raw_db.startswith("file:"):
                    db_path, _, query = raw_db[5:].partition("?")
                    if db_path and db_path != ":memory:" and "mode=memory" not in query:
                        (_check_read if "mode=ro" in query else _check_write)(unquote(db_path))
                else:
                    _check_write(raw_db)

    with open(entrypoint, "rb") as _f:
        code = compile(_f.read(), entrypoint, "exec")
    main_mod = types.ModuleType("__main__")
    main_mod.__file__ = entrypoint
    main_mod.__builtins__ = __builtins__
    sys.modules["__main__"] = main_mod
    for module in (os, sys.modules.get("posix")):
        if module is not None:
            module.stat, module.lstat, module.access, module.open = _guarded_stat, _guarded_lstat, _guarded_access, _guarded_open
    # 能力集合也含可直接调用的原生别名；替换而非追加，保留能力探测但不遗留未守卫入口。
    replacements = ((original_stat, _guarded_stat), (original_lstat, _guarded_lstat), (original_access, _guarded_access), (original_open, _guarded_open))
    for name in ("supports_dir_fd", "supports_fd", "supports_follow_symlinks", "supports_effective_ids"):
        functions = getattr(os, name, set())
        for original, guarded in replacements:
            if original in functions:
                functions.remove(original)
                functions.add(guarded)
    socket.socket.connect, socket.socket.connect_ex = _guarded_connect, _guarded_connect_ex
    socket.socket.sendto, socket.create_connection = _guarded_sendto, _guarded_create_connection
    sys.addaudithook(_audit)
    return code, main_mod.__dict__

_code, _main_globals = _init_guard()
del _init_guard
exec(_code, _main_globals)
"""


# LLM: 此异常只表示冻结代码为空或缺失，不把路径违规或坏描述符降级为旧格式。
# 类用途: 让缺失入口返回明确的可修复错误码。
class _EntrypointMissing(ValueError):
    pass


# LLM: 权威入口沿宿主workspace覆盖与nofollow；项目根只读代码，Owner仍为执行/权限根；缺失才读旧Owner格式。
# 函数用途: 取得脚本配置和冻结代码，返回目录仅供兼容调用方展示。
def load_script(owner_home, script_id, *, workspace_root=None):
    folder = safe_path(owner_home, 'scripts', script_id)
    try:
        asset = ScriptStore(owner_home, workspace_root=workspace_root).get_script(script_id)
    except ValueError as exc:
        if str(exc) == '脚本代码文件缺失':
            raise _EntrypointMissing(str(exc)) from exc
        raise
    if asset is None:
        raw = read_text_beneath(owner_home, ('scripts', script_id, 'meta.json'))
        if raw is None:
            raise _EntrypointMissing('脚本不存在')
        data = json.loads(raw)
        safe_path(folder, data['entrypoint'])
        data['code'] = read_text_beneath(owner_home, ('scripts', script_id, data['entrypoint']))
    else:
        data = payload(asset)
    metadata = {**data.get('descriptor', {}), **data}
    if not metadata.get('code'):
        raise _EntrypointMissing('脚本入口为空或缺失')
    return folder, metadata


# LLM: 仅识别macOS外层Seatbelt已生效时嵌套sandbox_apply返回71的精确内核事实；其它错误一律按未就绪失败关闭。
# 函数用途: 判断当前进程是否已处于禁止再次sandbox_apply的外层macOS Seatbelt沙箱内。
@lru_cache(maxsize=1)
def _outer_seatbelt_active():
    if sys.platform != 'darwin':
        return False
    try:
        res = subprocess.run([shutil.which('sandbox-exec') or '/usr/bin/sandbox-exec', '-p',
                              '(version 1)(allow default)(deny file-write* (literal "/dev/null"))', '/usr/bin/true'],
                             capture_output=True, timeout=5, check=False)
        return res.returncode == 71 and b'sandbox_apply: Operation not permitted' in (res.stderr or b'')
    except (OSError, subprocess.SubprocessError):
        return False


# LLM: 非strict且外层Seatbelt已锁死71时直接跳过内层sandbox_apply；其它情况必须先通过require_ready（未就绪直接抛SandboxUnavailableError失败关闭）。
# 函数用途: 决定是否需要再套一层子进程平台沙箱包装，或在需要包装时校验平台沙箱就绪。
def _can_apply_sandbox(sandbox: AttemptExecutionSandbox, *, strict: bool = False) -> bool:
    if not strict and _outer_seatbelt_active():
        return False
    sandbox.require_ready()
    return True


# LLM: 分类不授予资源或凭据，显式覆盖及空声明既有行为不变。
# 函数用途: 按共享目标type或旧kind推导网络开关。
def _network_access(metadata, override):
    if override is not None:
        if type(override) is not bool:
            raise ValueError('network_access必须是布尔值')
        return override
    targets = metadata.get('access_targets', ())
    return not targets or any(access_target_requires_network(target) for target in targets)


# LLM: 从宿主副本清洗凭据，仅添加引用占位符；工作目录、HOME与TMPDIR均指向本次冻结目录。
# 函数用途: 准备脚本输入和不携带宿主密钥的环境。
def _environment(metadata, folder, input_json, sources, *, host_credential_env_names=frozenset(), work_context=None):
    environment = scrub_subprocess_env(dict(os.environ), host_credential_env_names=host_credential_env_names)
    # 不继承另一运行或操作者环境伪造的工作上下文；只有本启动器可注入当前冻结值。
    for key in ('MY_AGENT_SCRIPT_WORK_CONTEXT_JSON', 'MY_AGENT_SCRIPT_WORK_CONTEXT_FILE'):
        environment.pop(key, None)
    environment.update(PYTHONIOENCODING='utf-8', PAGER='cat', GIT_PAGER='cat', HOME=str(folder), TMPDIR=str(folder),
                       MY_AGENT_SCRIPT_INPUT_JSON=input_json,
                       MY_AGENT_DATA_SOURCES_JSON=json.dumps(sources, ensure_ascii=False, allow_nan=False))
    if work_context is not None:
        environment['MY_AGENT_SCRIPT_WORK_CONTEXT_JSON'] = json.dumps(work_context, ensure_ascii=False, allow_nan=False)
    for ref in metadata.get('credential_refs', ()):
        if ref.get('credential_id') and ref.get('target'):
            environment['CREDENTIAL_REF_' + ref['credential_id'].upper()] = ref['target']
    return environment


# LLM: 摘要失败不能覆盖执行事实；按真实签名兼容并行Store的run_at和新增last_error，不重试已经进入的写操作。
# 函数用途: 尽力将成功、失败及超时累计到脚本摘要。
def _record_summary(owner_home, result):
    try:
        record = ScriptStore(owner_home).record_run_summary
        parameters = inspect.signature(record).parameters
        status = 'succeeded' if result.ok else 'timed_out' if result.error_code == 'SCRIPT_TIMEOUT' else 'failed'
        options = {'status': status, 'duration_ms': result.duration_ms}
        if 'run_at' in parameters:
            options['run_at'] = datetime.fromtimestamp(result.ended_at, timezone.utc).isoformat()
        if 'last_error' in parameters or any(p.kind == p.VAR_KEYWORD for p in parameters.values()):
            options['last_error'] = redact_sensitive_value(result.error_code or result.stderr_preview[:200])
        record(result.script_id, **options)
    except Exception:
        pass


# LLM: 只能由本模块处理其亲自创建的进程组，退出后也清理仍留存的同组子进程。
# 函数用途: 终止脚本进程树并回收直接子进程，避免超时后留下后台脚本。
def _terminate(process):
    try:
        if os.name == 'posix':
            os.killpg(process.pid, signal.SIGKILL)
        elif process.poll() is None:
            process.kill()
    except ProcessLookupError:
        pass
    process.wait()


# LLM: 清理拒绝不授予任何新信号权限；只尝试有限回收并保留原异常及进程组未验证事实。
# 函数用途: 清理自己创建的子进程，返回明确的次级错误而不是覆盖业务失败。
def _cleanup_process(process):
    try:
        _terminate(process)
    except OSError as exc:
        try:
            process.wait(timeout=0.1)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return {'error_code': 'SCRIPT_CLEANUP_ERROR', 'exception_type': type(exc).__name__,
                'errno': exc.errno, 'message': str(exc), 'leader_exited': process.returncode is not None,
                'group_cleanup_verified': False}
    return {}


# LLM: 原始失败及清理事实写入调用方持有的 capture，输出读取或关闭异常也不能丢失硬失败。
# 函数用途: 真正启动脚本并返回退出码、输出及超时或输出超限标记。
def _execute(command, folder, environment, input_json, timeout, capture):
    with tempfile.TemporaryFile() as stdin, tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        stdin.write(input_json.encode('utf-8'))
        stdin.seek(0)
        launching = time.monotonic()
        process = subprocess.Popen(command, cwd=folder, env=environment, stdin=stdin,
                                   stdout=stdout, stderr=stderr, start_new_session=os.name == 'posix')
        running = time.monotonic()
        capture['started'] = True
        capture['phase'] = 'started'
        capture.setdefault('timings', dict.fromkeys(_TIMING_FIELDS, 0.0))['process_start_ms'] += (running - launching) * 1000
        deadline, error = time.monotonic() + timeout, ''
        try:
            while process.poll() is None:
                if os.fstat(stdout.fileno()).st_size + os.fstat(stderr.fileno()).st_size > MAX_OUTPUT_BYTES:
                    error = 'SCRIPT_OUTPUT_LIMIT'
                    break
                if time.monotonic() >= deadline:
                    error = 'SCRIPT_TIMEOUT'
                    break
                time.sleep(0.01)
        finally:
            capture['error_code'] = error
            cleanup = _cleanup_process(process)
            capture.update(cleanup_error=cleanup, exit_code=process.returncode)
            capture['timings']['execution_ms'] += (time.monotonic() - running) * 1000
        if os.fstat(stdout.fileno()).st_size + os.fstat(stderr.fileno()).st_size > MAX_OUTPUT_BYTES:
            error = error or 'SCRIPT_OUTPUT_LIMIT'
        capture['error_code'] = error
        stdout.seek(0)
        stderr.seek(0)
        return process.returncode, stdout.read(MAX_OUTPUT_BYTES).decode('utf-8', 'replace'), stderr.read(PREVIEW_CHARS * 4).decode('utf-8', 'replace'), error, cleanup


# LLM: 解析前限制 JSON 结构深度，忽略字符串中的括号；不依赖解释器不稳定的 C 栈上限。
# 函数用途: 拒绝过深的脚本输出，避免解析与序列化递归失控。
def _check_json_depth(text):
    depth, quoted, escaped = 0, False, False
    for char in text:
        if quoted:
            previously_escaped = escaped
            escaped = not escaped and char == '\\'
            quoted = previously_escaped or char != '"'
            continue
        if char == '"':
            quoted = True
            continue
        depth += int(char in '[{') - int(char in ']}')
        if depth > 128:
            raise ValueError('JSON 输出结构过深')


# LLM: 只接受JSON类型名，可空与字段存在性分开处理，不隐式增加必选字段。
# 函数用途: 校验输出声明并返回必选字段、可空字段和类型表。
def _output_contract_fields(contract):
    return output_contract_fields(contract)


# LLM: 正常与恢复输出共用严格校验；bool不是数字，显式nullable只豁免null值。
# 函数用途: 解析标准输出并核对字段存在性、非空和可选类型。
def parse_script_output(stdout, contract):
    try:
        _check_json_depth(stdout)
        output = payload(json.loads(stdout))
        if not isinstance(output, dict):
            return {}, 'SCRIPT_INVALID_JSON'
    except (ValueError, TypeError, RecursionError):
        return {}, 'SCRIPT_INVALID_JSON'
    expected, nullable, output_types = _output_contract_fields(contract)
    if any(key not in output for key in expected):
        return output, 'SCRIPT_MISSING_OUTPUT_KEYS'
    if any(output[key] is None and key not in nullable for key in expected):
        return output, 'SCRIPT_NULL_OUTPUT_VALUE'
    for key, name in output_types.items():
        if key not in output or output[key] is None and key in nullable:
            continue
        if type(output[key]) not in _JSON_OUTPUT_TYPES[name]:
            return output, 'SCRIPT_OUTPUT_TYPE_MISMATCH'
    return output, ''


# LLM: 新控制字段仅在显式协议中解析；旧业务JSON即使含delivery_intents也原样保留；全批先验再有任何受理。
# 函数用途: 解开版本化结果，校验业务IO和不可变投递意向，不发送消息或登记成功。
def _parse_result_envelope(stdout, contract):
    try:
        _check_json_depth(stdout)
        value = json.loads(stdout)
    except (ValueError, TypeError, RecursionError):
        return {}, (), 'SCRIPT_INVALID_JSON'
    if not isinstance(value, dict) or 'protocol_version' not in value:
        output, error = parse_script_output(stdout, contract)
        return output, (), error
    if (value.get('protocol_version') != 'script-output.v1'
            or set(value) - {'protocol_version', 'result', 'delivery_intents'}
            or not isinstance(value.get('result'), dict)
            or not isinstance(value.get('delivery_intents', []), list)):
        return {}, (), 'SCRIPT_INVALID_JSON'
    output, error = parse_script_output(json.dumps(value['result'], allow_nan=False), contract)
    if error:
        return output, (), error
    intents = {}
    for intent in value.get('delivery_intents', []):
        if (not isinstance(intent, dict) or set(intent) - {'request_key', 'message', 'output_refs', 'status'}
                or not isinstance(intent.get('request_key'), str) or not intent['request_key'].strip()
                or not isinstance(intent.get('message', ''), str) or intent.get('status', 'requested') != 'requested'
                or not isinstance(intent.get('output_refs', []), (list, tuple))):
            return output, (), 'SCRIPT_INVALID_JSON'
        refs = intent.get('output_refs', [])
        if any(not isinstance(ref, str) or not ref or Path(ref).is_absolute()
               or '..' in Path(ref).parts or '\\' in ref for ref in refs):
            return output, (), 'SCRIPT_INVALID_JSON'
        normalized = {'request_key': intent['request_key'], 'message': intent.get('message', ''),
                      'output_refs': list(dict.fromkeys(refs)), 'status': 'requested'}
        if intent['request_key'] in intents and intents[intent['request_key']] != normalized:
            return output, (), 'SCRIPT_INVALID_JSON'
        intents[intent['request_key']] = normalized
    return output, tuple(intents.values()), ''


# LLM: 共享前缀保留别名与realpath；runner跳过缺失目录，额外请求其原有解释器安装根。
# 函数用途: 选择venv或便携Python启动路径及只读根，不改沙箱策略。
def _interpreter_runtime(interpreter: str) -> tuple[str | None, tuple[Path, ...]]:
    if interpreter != 'python3':
        return shutil.which(interpreter, path=os.defpath), ()
    if not sys.executable:
        return None, ()
    resolved_exec = Path(sys.executable).expanduser().resolve(strict=False)
    prefix_dir = Path(sys.prefix).expanduser().resolve(strict=False)
    roots = resolve_python_runtime_prefixes(executable=sys.executable, strict=False)
    candidate = prefix_dir / 'bin' / Path(sys.executable).name
    executable = str(resolved_exec)
    if candidate.is_file() and not candidate.is_symlink():
        executable = str(candidate)
    elif candidate.is_symlink():
        target = Path(os.path.normpath(os.path.join(str(candidate.parent), os.readlink(candidate))))
        if any(target.is_relative_to(r) for r in roots):
            executable = str(candidate)
    return executable, roots


# LLM: 容器祖先不是私有目录；macOS的/home数据卷别名仍是宽根，拒绝home严格祖先，不授予读写。
# 函数用途: 判断一个候选是否是不能当作私有目录挂载的宽容器根。
def _is_broad_container_root(path: Path, home_dir: Path) -> bool:
    resolved = path.resolve(strict=False)
    home = home_dir.resolve(strict=False)
    return (len(resolved.parts) <= 2
        or resolved in {Path(p).resolve(strict=False) for p in ('/private/tmp', '/private/var', '/home')}
        or resolved == home.parent
        or resolved != home and home.is_relative_to(resolved))


# LLM: 保留Owner与明确my-agent私有根，但不把宽容器或位于Owner外部的workspace祖先变成拒写挂载。
# 函数用途: 从宿主Owner布局生成不会意外重挂整台宿主的私有根列表。
def _script_private_roots(owner: Path, workspace: Path | None) -> tuple[Path, ...]:
    owner = owner.resolve(strict=False)
    home = Path.home().resolve(strict=False)
    workspace = workspace.resolve(strict=False) if workspace is not None else None
    owners = next((p for p in owner.parents[:4] if p.name == 'owners'), None)
    candidates = (owner, owner.parent, owner.parent.parent, *((owners, owners.parent) if owners is not None else ()))
    return tuple(dict.fromkeys(root for root in candidates
        if not _is_broad_container_root(root, home)
        and (workspace is None or root == owner or (owners is not None and root.is_relative_to(owners.parent))
             or not workspace.is_relative_to(root))))


# LLM: 只冻结宿主给出的有效owner墙与规范app home；非空墙必须位于该app home的规范owner布局内，缺根、相对路径或
#   owners容器失败关闭；空墙保留直接runner与管理员原行为。
# 函数用途: 一次性归一化并校验脚本执行的owner读取边界。
def _owner_read_boundary(owner_scope_root, agent_home_root):
    home = Path(agent_home_root).expanduser() if agent_home_root is not None and str(agent_home_root).strip() else None
    if owner_scope_root is None or not str(owner_scope_root).strip():
        return None, home.resolve(strict=False) if home is not None else None
    scope = Path(owner_scope_root).expanduser()
    if home is None or not scope.is_absolute() or not home.is_absolute():
        raise ValueError('owner读取边界需要宿主冻结的绝对owner墙与app home根')
    scope, home = scope.resolve(strict=False), home.resolve(strict=False)
    if owner_home_containing(scope, home) is None:
        raise ValueError('owner读取边界必须位于宿主app home的规范owner布局内')
    return scope, home


# LLM: 固化脚本仅接收 JSON；宿主必须在调用前完成 Owner、审批和工具权限检查。
# 类用途: 按 Owner 的脚本配置执行 Python、bash 或 sh，且不调用任何模型。
class ScriptRunner:
    # LLM: 仅绑定宿主正负路径快照与owner读取墙；新增保护参数默认为空，保留直接runner与restriction默认值，不从业务输入授权。
    # 函数用途: 保存Owner、数据源workspace、绝对保护、普通拒写范围及冻结owner墙。
    def __init__(
        self,
        owner_home: str | Path,
        *,
        use_sandbox: bool = True,
        strict_sandbox: bool = False,
        extra_write_roots: tuple[str | Path, ...] = (),
        public_read_roots: tuple[str | Path, ...] = (),
        restrict_to_roots: bool = False,
        workspace_root: str | Path | None = None,
        work_context=None,
        host_credential_env_names: frozenset[str] = frozenset(),
        host_exclusive_paths: tuple[str | Path, ...] = (),
        delivery_handler=None,
        write_protection: SandboxWriteProtection | None = None,
        host_path_protection: HostSandboxPaths | None = None,
        owner_scope_root: str | Path | None = None,
        agent_home_root: str | Path | None = None,
    ):
        self.owner_home = Path(owner_home).absolute()
        self.owner_scope_root, self.agent_home_root = _owner_read_boundary(owner_scope_root, agent_home_root)
        self.scripts_root = self.owner_home / 'scripts'
        self.use_sandbox, self.strict_sandbox = use_sandbox, strict_sandbox
        self.extra_write_roots = tuple(Path(p).expanduser().resolve(strict=False) for p in extra_write_roots)
        self.public_read_roots = tuple(Path(p).expanduser().resolve(strict=False) for p in public_read_roots)
        self.restrict_to_roots = bool(restrict_to_roots)
        self.workspace_root = Path(workspace_root).absolute() if workspace_root is not None else None
        self.work_context = payload(work_context) if work_context is not None else None
        self.host_credential_env_names = frozenset(host_credential_env_names)
        self.host_exclusive_paths = tuple(Path(path).expanduser().resolve(strict=False) for path in host_exclusive_paths)
        self.delivery_handler = delivery_handler
        protection = write_protection or SandboxWriteProtection()
        self.write_protection = SandboxWriteProtection(
            absolute_paths=tuple(Path(p).expanduser().resolve(strict=False) for p in protection.absolute_paths),
            denied_roots=tuple(Path(p).expanduser().resolve(strict=False) for p in protection.denied_roots))
        host = host_path_protection or HostSandboxPaths()
        self.host_path_protection = HostSandboxPaths(
            readonly=tuple(Path(p).expanduser().resolve(strict=False) for p in host.readonly),
            hidden=tuple(Path(p).expanduser().resolve(strict=False) for p in host.hidden),
            patterns=tuple(host.patterns), directory_write_patterns=tuple(host.directory_write_patterns),
            directory_unlink_patterns=tuple(host.directory_unlink_patterns))

    # LLM: 只消费构造期宿主冻结的workspace和scope；scratch由本次实际目录覆盖，身份不来自业务inputs。
    # 函数用途: 投影只读SDK工作上下文，无工作区时不伪造cwd作为工作区。
    def _work_context(self, folder):
        if self.workspace_root is None:
            if self.work_context is not None:
                raise ValueError('工作上下文缺少宿主workspace_root')
            return None
        workspace = self.workspace_root.resolve(strict=False)
        context = payload(self.work_context or {})
        if context.get('workspace_root') and Path(context['workspace_root']).resolve(strict=False) != workspace:
            raise ValueError('工作上下文workspace_root与宿主不一致')
        scope = context.get('effective_path_scope', {
            'read_roots': [str(path) for path in dict.fromkeys((workspace, *self.public_read_roots, *self.extra_write_roots))],
            'write_roots': [str(path) for path in self.extra_write_roots]})
        if not isinstance(scope, dict) or set(scope) != {'read_roots', 'write_roots'}:
            raise ValueError('工作上下文路径范围无效')
        for roots in scope.values():
            if not isinstance(roots, (list, tuple)) or any(not isinstance(path, str) or not Path(path).is_absolute() for path in roots):
                raise ValueError('工作上下文路径范围必须为绝对路径列表')
        return {'schema_version': 'script-work-context.v1', 'workspace_root': str(workspace),
            'scratch_dir': str(folder.resolve(strict=False)), 'run_id': context.get('run_id', ''),
            'task_id': context.get('task_id', ''), 'effective_path_scope': scope}

    # LLM: 仅消费本次IO声明的引用，并从原产物账核对ready/hash/归属；真实受理必须由宿主原投递入口返回operation引用。
    # 函数用途: 在正常退出且全批有效后交接请求，不把requested或上传完成伪装成sent。
    def _admit_delivery(self, intents, output, metadata, context, *, receipts=None):
        if not intents:
            return ()
        from ..artifacts.registry import resolve_artifact_record_report
        from ..conversation.channels import ChannelAttachment
        from ..delivery.attachment_content import verify_attachment_content

        if not context or not context.get('run_id') or not callable(self.delivery_handler):
            raise ValueError('投递意向缺少宿主原运行或投递入口')
        declared = set()
        for key in output_contract_fields(metadata.get('io_contract', {}))[0]:
            value = output.get(key)
            declared.update([value] if isinstance(value, str) else [item for item in value if isinstance(item, str)]
                            if isinstance(value, list) else [])
        checked = []
        for intent in intents:
            attachments = []
            for ref in intent['output_refs']:
                if ref not in declared:
                    raise ValueError('投递引用未在本次输出合同中声明')
                report = resolve_artifact_record_report(self.workspace_root, ref)
                row = report.record
                if report.errors or row is None or row.status != 'ready':
                    raise ValueError('投递引用未命中已登记ready产物')
                attachment = ChannelAttachment(row.artifact_id, row.path, Path(row.path).name,
                    row.kind, row.sha256, row.size_bytes, str(self.owner_home))
                verify_attachment_content(attachment)
                attachments.append(attachment)
            checked.append((intent, tuple(attachments)))
        receipts = receipts if receipts is not None else []
        for intent, attachments in checked:
            receipt = self.delivery_handler(run_id=context['run_id'], task_id=context.get('task_id', ''),
                request_key=intent['request_key'], message=intent['message'], attachments=attachments)
            if (not isinstance(receipt, dict) or not receipt.get('operation_ref')
                    or receipt.get('status') not in ('accepted', 'sent', 'unknown', 'partial', 'failed')):
                raise ValueError('宿主投递入口未返回原操作回执')
            receipts.append(payload(receipt))
        return tuple(receipts)

    # LLM: 原生沙箱与Python守卫共用冻结正负范围；scratch仅授权本次生成目录，owner数据无临时例外，finally沿原清理。
    # 函数用途: 冻结代码、绑定连接，投影宿主及人格保护并清理本次临时目录。
    @contextmanager
    def _prepare(self, script_id, inputs, timeout_seconds, override_data_sources, network_access, *, capture=None):
        capture = capture if capture is not None else {}
        timings = capture.setdefault('timings', dict.fromkeys(_TIMING_FIELDS, 0.0))
        clock = time.monotonic()
        _, metadata = load_script(self.owner_home, script_id, workspace_root=self.workspace_root)
        capture['definition_ref'] = execution_definition_ref(metadata, kind='script')
        output_contract_fields(metadata.get('io_contract', {}))
        if metadata.get('enabled', True) is False:
            raise AssetDisabledError('script', script_id)
        interpreter = metadata.get('interpreter', 'python3')
        if interpreter not in ('python3', 'bash', 'sh'):
            raise ValueError('仅支持 python3、bash、sh')
        executable, runtime_read_roots = _interpreter_runtime(interpreter)
        if not executable:
            raise ValueError('解释器不可用')
        declared_timeout = float(metadata.get('timeout_seconds') or 30)
        timeout = (
            declared_timeout
            if timeout_seconds is None
            else (min(declared_timeout, float(timeout_seconds)) if self.restrict_to_roots else float(timeout_seconds))
        )
        if not math.isfinite(timeout) or not 0 < timeout <= 3600:
            raise ValueError('脚本超时必须为 (0, 3600] 秒')
        sources = payload(metadata.get('data_sources', {}) if override_data_sources is None else override_data_sources)
        input_json = json.dumps(payload(inputs), ensure_ascii=False, allow_nan=False)
        timings['configuration_load_ms'] += (time.monotonic() - clock) * 1000
        clock = time.monotonic()
        root = self.owner_home / 'tmp' / 'script_exec'
        ensure_private_dir(root, canonical_root=self.owner_home)
        folder = Path(tempfile.mkdtemp(prefix=f'{script_id}_', dir=root))
        ds_runtime = None
        try:
            entrypoint = safe_path(folder, metadata.get('entrypoint', 'main.py'))
            write_text_atomic_beneath(self.owner_home, (*folder.relative_to(self.owner_home).parts, entrypoint.name),
                                      metadata['code'], file_mode=0o700)
            net_allowed = _network_access(metadata, network_access)
            denied_ports = gateway_bound_ports()
            context = self._work_context(folder)
            capture['work_context'] = context
            env = _environment(metadata, folder, input_json, sources,
                host_credential_env_names=self.host_credential_env_names, work_context=context)
            ds_runtime = ScriptDatasourceRuntime(self.owner_home, metadata, folder, env, timeout,
                capture if capture is not None else {}, workspace_root=self.workspace_root)
            private_roots = _script_private_roots(self.owner_home, self.workspace_root) if self.restrict_to_roots else ()
            host = self.host_path_protection
            protected_paths = tuple(dict.fromkeys((*self.write_protection.absolute_paths, *host.readonly)))
            denied_write_roots = tuple(dict.fromkeys((*private_roots, *self.write_protection.denied_roots)))
            hidden_paths = tuple(dict.fromkeys((*host.hidden, *self.host_exclusive_paths)))
            command = [executable, '-I', '-B', str(entrypoint)] if interpreter == 'python3' else [executable, str(entrypoint)]
            applied_sandbox = False
            if self.use_sandbox:
                shared_root = folder if self.restrict_to_roots else self.owner_home
                sandbox = AttemptExecutionSandbox(AttemptSandboxSpec(
                    attempt_view=folder, staging_root=folder, shared_workspace=shared_root, owner_home=shared_root,
                    extra_write_roots=(folder, *self.extra_write_roots, *ds_runtime.write_roots),
                    public_read_roots=tuple(dict.fromkeys((*runtime_read_roots, *self.public_read_roots, *ds_runtime.read_roots))),
                    private_read_roots=private_roots,
                    protected_write_paths=protected_paths,
                    write_scope_denied_roots=denied_write_roots,
                    protected_persona_root=self.owner_home,
                    hidden_paths=hidden_paths,
                    protected_write_patterns=host.patterns,
                    protected_directory_write_patterns=host.directory_write_patterns,
                    protected_directory_unlink_patterns=host.directory_unlink_patterns,
                    implicit_attempt_write_roots=True,
                    network_access=net_allowed, deny_gateway_ports=denied_ports if net_allowed else ()))
                if _can_apply_sandbox(sandbox, strict=self.strict_sandbox):
                    applied_sandbox = True
            enforces_scope = self.restrict_to_roots or bool(self.owner_scope_root or hidden_paths or protected_paths
                or denied_write_roots or host.patterns or host.directory_write_patterns or host.directory_unlink_patterns)
            guarded = enforces_scope and interpreter == 'python3'
            if guarded:
                guard = safe_path(folder, '_sandbox_guard.py')
                write_text_atomic_beneath(self.owner_home, (*folder.relative_to(self.owner_home).parts, guard.name),
                                          _PYTHON_GUARD_CODE, file_mode=0o700)
                resolved_folder = folder.resolve(strict=False)
                write_roots = (resolved_folder, *self.extra_write_roots, *ds_runtime.write_roots, Path('/dev'))
                persona_paths = tuple(path.resolve(strict=False) for path in persona_process_protected_paths(self.owner_home))
                env['MY_AGENT_SCRIPT_GUARD_JSON'] = json.dumps({
                    'read_roots': [str(p) for p in (resolved_folder, *self.public_read_roots, *self.extra_write_roots, *ds_runtime.read_roots)],
                    'runtime_read_roots': [str(p) for p in runtime_read_roots],
                    'owner_scope_root': str(self.owner_scope_root or ''),
                    'agent_home_root': str(self.agent_home_root or ''),
                    'write_roots': [str(p) for p in write_roots],
                    'write_scope_rules': [(str(root), allowed) for root, allowed in ordered_write_scope_roots(write_roots, denied_write_roots)],
                    'protected_write_paths': [str(p) for p in (*protected_paths, *persona_paths)],
                    'protected_write_patterns': host.patterns,
                    'protected_directory_write_patterns': host.directory_write_patterns,
                    'protected_directory_unlink_patterns': host.directory_unlink_patterns,
                    'denied_roots': [str(p) for p in private_roots],
                    'host_exclusive_paths': [str(p) for p in hidden_paths],
                    'network_access': net_allowed,
                    'denied_ports': list(denied_ports),
                    'block_subprocess': not applied_sandbox,
                })
                command = [executable, '-I', '-B', str(guard), str(entrypoint)]
            elif enforces_scope and not applied_sandbox:
                raise SandboxUnavailableError('SANDBOX_UNAVAILABLE: NESTED_SHELL_SANDBOX_UNAVAILABLE')
            command = ds_runtime.wrap_command(command, interpreter, guarded=bool(guarded))
            timings['environment_setup_ms'] += (time.monotonic() - clock) * 1000
            clock = time.monotonic()
            if applied_sandbox:
                command = sandbox.build_argv(command)
            timings['directory_discovery_ms'] += (time.monotonic() - clock) * 1000
            yield metadata, sources, (command, str(folder), env, input_json, timeout)
        finally:
            try:
                if ds_runtime is not None:
                    ds_runtime.close()
            finally:
                shutil.rmtree(folder, ignore_errors=True)

    # LLM: record_summary仅由可信执行器传入；False不改脚本修订/计数，沙箱、数据源、超时与清理保持原执行链。
    # 函数用途: 执行脚本并返回结构化结果，单步调试可显式禁止资产摘要写入。
    def run_script(self, script_id, inputs=None, *, timeout_seconds: int | float | None = None,
                   override_data_sources=None, network_access: bool | None = None, record_summary: bool = True):
        if type(record_summary) is not bool:
            raise ValueError('record_summary必须是布尔值')
        started_at, timer = time.time(), time.monotonic()
        metadata, sources, output, stdout, stderr, error, exit_code = {}, {}, {}, '', '', '', None
        cleanup, capture, execution_error = {}, {'phase': 'preflight', 'timings': dict.fromkeys(_TIMING_FIELDS, 0.0)}, {}
        delivery_receipts = []
        try:
            inputs = payload({} if inputs is None else inputs)
            with self._prepare(script_id, inputs, timeout_seconds, override_data_sources, network_access, capture=capture) as prepared:
                metadata, sources, execution = prepared
                exit_code, stdout, stderr, error, cleanup = _execute(*execution, capture)
            if cleanup:
                if exit_code is None and not error:
                    error = 'SCRIPT_CLEANUP_ERROR'
            if not error and exit_code != 0:
                error = 'SCRIPT_EXIT_NONZERO'
            if not error:
                capture['phase'] = 'finalize'
                output, intents, error = _parse_result_envelope(stdout, metadata.get('io_contract', {}))
            if cleanup:
                error = error or 'SCRIPT_CLEANUP_ERROR'
            if not error:
                self._admit_delivery(intents, output, metadata, capture.get('work_context'), receipts=delivery_receipts)
        except SandboxUnavailableError:
            raise
        except (OSError, ValueError, KeyError, TypeError) as exc:
            cleanup = capture.get('cleanup_error', cleanup)
            exit_code = capture.get('exit_code', exit_code)
            error = capture.get('error_code') or ('SCRIPT_CLEANUP_ERROR' if cleanup else 'SCRIPT_CONFIGURATION_ERROR')
            if isinstance(exc, _EntrypointMissing):
                error = 'ENTRYPOINT_MISSING'
            if isinstance(exc, AssetDisabledError):
                error = exc.error_code
            stderr = str(exc)
            execution_error = {'exception_type': type(exc).__name__, 'errno': getattr(exc, 'errno', None), 'message': str(exc)}
        if cleanup:
            stderr += '\n进程组清理失败: ' + cleanup['message']
        output, stdout, stderr, cleanup, execution_error, inputs, sources = redact_datasource_values(
            [output, stdout, stderr, cleanup, execution_error, inputs, sources], capture.get('datasource_secrets', ()))
        ended_at, elapsed = time.time(), (time.monotonic() - timer) * 1000
        context = {'script_id': script_id, 'error_code': error, 'exit_code': exit_code,
                   'stderr_preview': stderr[:PREVIEW_CHARS], 'stdout_tail': stdout[:PREVIEW_CHARS],
                   'stderr_tail': stderr[:PREVIEW_CHARS], 'input_payload': inputs, 'data_sources': sources, 'inputs': inputs,
                   'io_contract': metadata.get('io_contract', {})}
        if cleanup:
            context['cleanup_error'] = cleanup
        if execution_error:
            context['execution_error'] = execution_error
        result = ScriptExecutionResult(script_id, not error, exit_code, elapsed, started_at, ended_at,
                                       output, stdout[:PREVIEW_CHARS], stderr[:PREVIEW_CHARS], error,
                                       bool(error), redact_sensitive_value(context) if error else {})
        result.phase = capture['phase'] if error else 'terminal'
        result.effect_outcome = ('unknown' if cleanup or delivery_receipts else 'failed') if capture.get('started') and error else (
            'confirmed' if not error else 'not_started')
        result.execution_definition_ref = capture.get('definition_ref', '')
        result.preparation_timings = capture['timings']
        result.delivery_receipts = tuple(delivery_receipts)
        if record_summary:
            _record_summary(self.owner_home, result)
        return result
