# LLM: 仅执行宿主授权脚本的nofollow代码快照；数据源按冻结合同注入，严格模式强制平台沙箱，不冒称内核隔离。
# 模块用途: 执行Owner脚本、注入run级数据源、清洗全部返回内容并回写安全摘要。
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
from ..assets.descriptor import access_target_requires_network
from ..assets.script_store import ScriptStore
from ..attempt.sandbox import (
    AttemptExecutionSandbox,
    AttemptSandboxSpec,
    SandboxUnavailableError,
    gateway_bound_ports,
)
from ..common.log_redaction import redact_sensitive_value
from ..common.nofollow_fs import ensure_private_dir, read_text_beneath, write_text_atomic_beneath
from ..plugin_sandbox import resolve_python_runtime_prefixes
from ..tooling._subprocess_env_scrub import scrub_subprocess_env
from .datasource_runtime import ScriptDatasourceRuntime
from .models import ScriptExecutionResult, payload, safe_path

# 原生工作流脚本标准输出与错误流保留的最大字节数。
MAX_OUTPUT_BYTES = 1024 * 1024
# 原生工作流步骤回执保留的输出预览最大字符数。
PREVIEW_CHARS = 4096
_JSON_OUTPUT_TYPES = {
    'string': (str,), 'integer': (int,), 'number': (int, float),
    'boolean': (bool,), 'object': (dict,), 'array': (list,), 'null': (type(None),),
}
_PYTHON_GUARD_CODE = """# LLM: Python守卫只作OS沙箱的补充，冻结路径与端口；用户代码执行前安装，不冒称内核隔离。
# 函数用途: 初始化脚本的文件metadata与网络出口检查。
def _init_guard():
    import errno, ipaddress, json, os, socket, sys, threading, types
    from pathlib import Path
    from urllib.parse import unquote
    cfg = json.loads(os.environ.pop("MY_AGENT_SCRIPT_GUARD_JSON", "{}"))
    entrypoint = sys.argv.pop(1)
    sys.argv[0] = entrypoint
    read_roots = tuple(Path(p) for p in cfg.get("read_roots", ()))
    write_roots = tuple(Path(p) for p in cfg.get("write_roots", ()))
    denied_roots = tuple(Path(p) for p in cfg.get("denied_roots", ()))
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

    read_ancestors = frozenset(parent for root in (*read_roots, *write_roots) for parent in root.parents
        if not any(_under(parent, (private,)) and not _under(root, (private,)) for private in denied_roots))

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

    # LLM: 已解析的路径只消费冻结根，不重复跟随lstat允许观察的symlink叶节点。
    # 函数用途: 执行私有读取限制的纯路径裁决。
    def _check_read_target(target, raw):
        if _under(target, denied_roots) and not _under(target, read_roots):
            raise perm_err(eacces, "Permission denied", str(raw))

    # LLM: 文件内容读取沿用真实目标边界，不能由符号链接逃出获准根。
    # 函数用途: 检查一次普通文件读取。
    def _check_read(raw):
        _check_read_target(_resolve(raw), raw)

    # LLM: 写检查与读检查共用有界重入解析，写权限仍只来自冻结write_roots。
    # 函数用途: 检查一次文件修改。
    def _check_write(raw):
        target = _resolve(raw)
        if not _under(target, write_roots):
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

    # LLM: 审计钩子保留原内容读写及子进程规则，并覆盖绕过Python socket子类的原生网络调用。
    # 函数用途: 在低层操作发生前执行脚本边界检查。
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
            if args and not isinstance(args[0], int):
                _check_write(args[0])
        elif event in ("os.rename", "os.replace", "os.symlink", "os.link"):
            for item in args[:2]:
                if not isinstance(item, int):
                    _check_write(item)
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


# LLM: 权威入口先走Store恢复及nofollow；仅None可读旧meta.json，旧Store的明确缺代码错误只分类、不绕过验证。
# 函数用途: 取得脚本配置和冻结代码，返回目录仅供兼容调用方展示。
def load_script(owner_home, script_id):
    folder = safe_path(owner_home, 'scripts', script_id)
    try:
        asset = ScriptStore(owner_home).get_script(script_id)
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
def _environment(metadata, folder, input_json, sources):
    environment = scrub_subprocess_env(dict(os.environ))
    environment.update(PYTHONIOENCODING='utf-8', PAGER='cat', GIT_PAGER='cat', HOME=str(folder), TMPDIR=str(folder),
                       MY_AGENT_SCRIPT_INPUT_JSON=input_json,
                       MY_AGENT_DATA_SOURCES_JSON=json.dumps(sources, ensure_ascii=False, allow_nan=False))
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
        process = subprocess.Popen(command, cwd=folder, env=environment, stdin=stdin,
                                   stdout=stdout, stderr=stderr, start_new_session=os.name == 'posix')
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
    if not isinstance(contract, dict):
        raise ValueError('输出合同必须为对象')
    expected = contract.get('expected_output_keys', contract.get('outputs', ()))
    nullable = contract.get('nullable_outputs', ())
    output_types = contract.get('output_types', {})
    if not isinstance(expected, (list, tuple)) or not all(isinstance(key, str) for key in expected):
        raise ValueError('必选输出字段必须为字符串列表')
    if not isinstance(nullable, (list, tuple)) or not all(isinstance(key, str) for key in nullable):
        raise ValueError('可空输出字段必须为字符串列表')
    if not isinstance(output_types, dict) or any(
        not isinstance(key, str) or not isinstance(name, str) or name not in _JSON_OUTPUT_TYPES
        for key, name in output_types.items()
    ):
        raise ValueError('输出类型必须为字段名到JSON类型名的对象')
    return expected, nullable, output_types


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


# LLM: 固化脚本仅接收 JSON；宿主必须在调用前完成 Owner、审批和工具权限检查。
# 类用途: 按 Owner 的脚本配置执行 Python、bash 或 sh，且不调用任何模型。
class ScriptRunner:
    # LLM: 初始化只绑定宿主Owner/workspace及沙箱策略，不从脚本或输入取得新工作区授权。
    # 函数用途: 保存Owner、数据源workspace和默认沙箱选项。
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
    ):
        self.owner_home = Path(owner_home).absolute()
        self.scripts_root = self.owner_home / 'scripts'
        self.use_sandbox, self.strict_sandbox = use_sandbox, strict_sandbox
        self.extra_write_roots = tuple(Path(p).expanduser().resolve(strict=False) for p in extra_write_roots)
        self.public_read_roots = tuple(Path(p).expanduser().resolve(strict=False) for p in public_read_roots)
        self.restrict_to_roots = bool(restrict_to_roots)
        self.workspace_root = Path(workspace_root).absolute() if workspace_root is not None else None

    # LLM: 禁用闸先于解释器、临时目录和数据源绑定；其余冻结/清理与原沙箱权限合同不变。
    # 函数用途: 冻结代码、绑定声明连接、构造沙箱命令并清理临时秘密。
    @contextmanager
    def _prepare(self, script_id, inputs, timeout_seconds, override_data_sources, network_access, *, capture=None):
        _, metadata = load_script(self.owner_home, script_id)
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
        root = self.owner_home / 'data' / 'script_exec'
        ensure_private_dir(root, canonical_root=self.owner_home)
        folder = Path(tempfile.mkdtemp(prefix=f'{script_id}_', dir=root))
        ds_runtime = None
        try:
            entrypoint = safe_path(folder, metadata.get('entrypoint', 'main.py'))
            write_text_atomic_beneath(self.owner_home, (*folder.relative_to(self.owner_home).parts, entrypoint.name),
                                      metadata['code'], file_mode=0o700)
            net_allowed = _network_access(metadata, network_access)
            denied_ports = gateway_bound_ports()
            env = _environment(metadata, folder, input_json, sources)
            ds_runtime = ScriptDatasourceRuntime(self.owner_home, metadata, folder, env, timeout,
                capture if capture is not None else {}, workspace_root=self.workspace_root)
            private_roots = _script_private_roots(self.owner_home, self.workspace_root) if self.restrict_to_roots else ()
            command = [executable, '-I', '-B', str(entrypoint)] if interpreter == 'python3' else [executable, str(entrypoint)]
            applied_sandbox = False
            if self.use_sandbox:
                shared_root = folder if self.restrict_to_roots else self.owner_home
                sandbox = AttemptExecutionSandbox(AttemptSandboxSpec(
                    attempt_view=folder, staging_root=folder, shared_workspace=shared_root, owner_home=shared_root,
                    extra_write_roots=(folder, *self.extra_write_roots, *ds_runtime.write_roots),
                    public_read_roots=tuple(dict.fromkeys((*runtime_read_roots, *self.public_read_roots, *ds_runtime.read_roots))),
                    private_read_roots=private_roots,
                    write_scope_denied_roots=private_roots,
                    implicit_attempt_write_roots=True,
                    network_access=net_allowed, deny_gateway_ports=denied_ports if net_allowed else ()))
                if _can_apply_sandbox(sandbox, strict=self.strict_sandbox):
                    applied_sandbox = True
            if self.restrict_to_roots and interpreter == 'python3':
                guard = safe_path(folder, '_sandbox_guard.py')
                write_text_atomic_beneath(self.owner_home, (*folder.relative_to(self.owner_home).parts, guard.name),
                                          _PYTHON_GUARD_CODE, file_mode=0o700)
                resolved_folder = folder.resolve(strict=False)
                env['MY_AGENT_SCRIPT_GUARD_JSON'] = json.dumps({
                    'read_roots': [str(p) for p in (resolved_folder, *runtime_read_roots, *self.public_read_roots, *self.extra_write_roots, *ds_runtime.read_roots)],
                    'write_roots': [str(p) for p in (resolved_folder, *self.extra_write_roots, *ds_runtime.write_roots, Path('/dev'))],
                    'denied_roots': [str(p) for p in private_roots],
                    'network_access': net_allowed,
                    'denied_ports': list(denied_ports),
                    'block_subprocess': not applied_sandbox,
                })
                command = [executable, '-I', '-B', str(guard), str(entrypoint)]
            elif self.restrict_to_roots and not applied_sandbox:
                raise SandboxUnavailableError('SANDBOX_UNAVAILABLE: NESTED_SHELL_SANDBOX_UNAVAILABLE')
            command = ds_runtime.wrap_command(command, interpreter, guarded=self.restrict_to_roots)
            if applied_sandbox:
                command = sandbox.build_argv(command)
            yield metadata, sources, (command, str(folder), env, input_json, timeout)
        finally:
            try:
                if ds_runtime is not None:
                    ds_runtime.close()
            finally:
                shutil.rmtree(folder, ignore_errors=True)

    # LLM: 禁用错误按原结构化码返回且绝不执行，单次覆盖和秘密清洗不变；摘要不得把禁用变成成功。
    # 函数用途: 执行脚本，保留失败与清理事实并返回不含解析秘密的完整回执。
    def run_script(self, script_id, inputs=None, *, timeout_seconds: int | float | None = None,
                   override_data_sources=None, network_access: bool | None = None):
        started_at, timer = time.time(), time.monotonic()
        metadata, sources, output, stdout, stderr, error, exit_code = {}, {}, {}, '', '', '', None
        cleanup, capture, execution_error = {}, {}, {}
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
                output, error = parse_script_output(stdout, metadata.get('io_contract', {}))
            if cleanup:
                error = error or 'SCRIPT_CLEANUP_ERROR'
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
        _record_summary(self.owner_home, result)
        return result
