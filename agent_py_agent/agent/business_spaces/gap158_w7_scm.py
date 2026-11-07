"""有界拉取 Git 投影；临时订阅不授予新的文件或进程权限。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import select
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

from ..common.nofollow_fs import open_directory_beneath
from ..gateway_parts.business_api_v13 import V13Call, v13_require_access
from ..path_access_policy import PathAccessPolicy
from ..plugin_host.errors import HostAPIError

# 进程结束后所有编号失效；未知写回执在订阅存在期间绝不淘汰或重做。
_subscriptions = {}
_owner_states = {}
_lock = threading.RLock()


# LLM: W0 未合并只采用冻结合同的同名默认值，依赖内部导入错误不能被吞掉。
# 函数用途: 读取正式上界或计划授权的精确兼容值。
def _limits():
    try:
        from . import gap158_limits
    except ImportError as error:
        if error.name not in (__package__, __package__ + '.gap158_limits'):
            raise
        return SimpleNamespace(SCM_MAX_SUBSCRIPTIONS_PER_OWNER=8, SCM_MAX_STATUS_ENTRIES=2000,
            SCM_MAX_LOG_ENTRIES=50, SCM_MAX_PATHS_PER_ACTION=200, SCM_GIT_TIMEOUT_SECONDS=15,
            SCM_MIN_REFRESH_INTERVAL_SECONDS=1.0, FS_WATCH_MAX_ENTRIES_PER_SCAN=5000)
    return gap158_limits


# LLM: 仅复用正式 FS 身份接口，缺依赖不构造伪造代次。
# 函数用途: 取得工作根和撤销代次读取端口。
def _fs():
    """共享可信工作根和原 owner epoch，不从参数建立身份。"""
    try:
        from . import gap158_w7_fs_watch
    except ImportError:
        raise HostAPIError('HOST_NOT_READY', 503) from None
    return gap158_w7_fs_watch


# LLM: 词法拒绝必须早于文件和进程副作用，保持跨平台路径边界。
# 函数用途: 把安全的工作区相对路径拆成片段。
def _parts(value):
    """先做纯字面检查；坏路径不进入文件系统和 Git。"""
    if (not isinstance(value, str) or not value or len(value) > 4096
            or value.startswith(('/', '-')) or '\x00' in value or '\\' in value
            or PureWindowsPath(value).drive):
        raise HostAPIError('PATH_OUTSIDE_WORKSPACE', 403)
    parts = value.split('/')
    if any(part in ('', '..') or part.casefold() == '.git' or part.startswith('-') for part in parts):
        raise HostAPIError('PATH_OUTSIDE_WORKSPACE', 403)
    if any(part == '.' for part in parts) and value != '.':
        raise HostAPIError('PATH_OUTSIDE_WORKSPACE', 403)
    return () if value == '.' else tuple(parts)


# LLM: key 同时含规范 home 和可信 owner，不能接受请求参数指定归属。
# 函数用途: 为进程内状态取得稳定归属键。
def _owner_key(call):
    return str(Path(call.owner_home).resolve()), call.owner_id


# LLM: 所有状态仅在模块锁内访问；满容量拒绝新 owner，绝不淘汰未知回执。
# 函数用途: 按规范 home 和 owner 保存当前代次的有限订阅及控制回执。
def _owner_state(call, epoch):
    owner = _owner_key(call)
    state = _owner_states.get(owner)
    if state is None:
        if len(_owner_states) >= _limits().SCM_MAX_STATUS_ENTRIES:
            raise HostAPIError('HOST_NOT_READY', 503)
        state = SimpleNamespace(epoch=epoch, subscriptions={}, controls={})
        _owner_states[owner] = state
    elif state.epoch != epoch:
        # 仅清理该 owner 的有界索引；其他 owner 的资源和回执完全不枚举。
        for sid in state.subscriptions:
            _subscriptions.pop(sid, None)
        state.epoch, state.subscriptions, state.controls = epoch, {}, {}
    return state


# LLM: 回执归属稳定 owner/home/epoch，不能依赖每次请求新建的 host。
# 函数用途: 检查幂等键并取得当前 owner 的控制回执。
def _control_receipts(call, epoch):
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', call.idempotency_key):
        raise HostAPIError('INVALID_SCHEMA')
    return _owner_state(call, epoch).controls


# LLM: 每次重新鉴权并只清理当前 owner 的旧代次状态，不扫描全局资源。
# 函数用途: 核验本次身份与订阅的工作根、撤销代次。
def _authorize(call, sub=None):
    """每次请求及每个进程前重核原身份、工作根和撤销代次。"""
    v13_require_access(call)
    epoch, key = _fs().current_epoch(call), _owner_key(call)
    _owner_state(call, epoch)
    if sub is not None and (sub.owner != key or sub.epoch != epoch
            or Path(_fs().workspace_root(call)).absolute() != sub.workspace):
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    return epoch


# LLM: 外来编号与不存在编号一致，不能经全局索引泄露资源归属。
# 函数用途: 从当前 owner 的有界索引取得订阅。
def _get(call):
    epoch = _authorize(call)
    sub = _owner_state(call, epoch).subscriptions.get(call.params['subscription_id'])
    if sub is None or sub.owner != _owner_key(call):
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    _authorize(call, sub)
    return sub


# LLM: 策略来自当前可信宿主，每次重算，不缓存过期权限。
# 函数用途: 构造工作区读取和写入权限检查器。
def _policy(call):
    host = call.context.host
    return PathAccessPolicy.from_config(host.config, agent_home_root=host.home_paths.root,
                                       owner_scope_root=call.owner_home)


@dataclass
class _Subscription:
    owner: tuple
    epoch: int
    workspace: Path
    repo_root: str
    identity: tuple = ()
    repository: dict = field(default_factory=dict)
    digest: str = ''
    repo_seq: int = 0
    seq: int = 0
    refreshed_at: float = float('-inf')
    ring: deque = field(default_factory=deque)
    receipts: dict = field(default_factory=dict)


# LLM: 只信任已打开 FD 的设备与 inode，不把路径字符串当身份。
# 函数用途: 提取仓库的物理身份。
def _identity(descriptor):
    info = os.fstat(descriptor)
    return info.st_dev, info.st_ino


# LLM: FD 生命周期限定在单次调用内，失败统一拒绝越界，不跟随替换后的父链。
# 函数用途: 安全打开原仓库并在调用结束关闭描述符。
@contextmanager
def _repo(call, sub):
    """持有完整父链 nofollow 的仓库 FD，绑定原 inode，不接受外置元数据。"""
    _authorize(call, sub)
    parts, descriptor = _parts(sub.repo_root), -1
    root = sub.workspace.joinpath(*parts)
    try:
        descriptor = open_directory_beneath(sub.workspace, parts, require_dir_fd=True)
        if sub.identity and _identity(descriptor) != sub.identity:
            raise HostAPIError('PATH_OUTSIDE_WORKSPACE', 403)
        if not _policy(call).check(root).allowed:
            raise HostAPIError('PATH_OUTSIDE_WORKSPACE', 403)
        _metadata(root, descriptor)
        yield root, descriptor
    except HostAPIError:
        raise
    except FileNotFoundError:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404) from None
    except (OSError, ValueError):
        raise HostAPIError('PATH_OUTSIDE_WORKSPACE', 403) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


# LLM: 逐个 nofollow 打开并核对 inode；链接、特殊文件、跨设备和超预算均不交给 Git。
# 函数用途: 有界检查完整元数据树并从同一个 FD 读取配置。
def _scan_metadata(metadata):
    limits = _limits()
    budget = getattr(limits, 'FS_WATCH_MAX_ENTRIES_PER_SCAN', 5000)
    stack, count, config = [], 0, None
    device = os.fstat(metadata).st_dev
    forbidden = {('commondir',), ('config.worktree',), ('shallow',),
        ('objects', 'info', 'alternates'), ('objects', 'info', 'http-alternates'),
        ('info', 'attributes')}
    first = os.dup(metadata)
    try:
        iterator = os.scandir(first)
    except BaseException:
        os.close(first)
        raise
    stack.append((first, iterator, ()))
    try:
        while stack:
            parent, iterator, prefix = stack[-1]
            entry = next(iterator, None)
            if entry is None:
                iterator.close()
                os.close(parent)
                stack.pop()
                continue
            count += 1
            if count > budget:
                raise HostAPIError('HOST_NOT_READY', 503)
            path = (*prefix, entry.name)
            if path in forbidden:
                raise HostAPIError('PATH_OUTSIDE_WORKSPACE', 403)
            info = _leaf_info(parent, entry.name)
            directory = stat.S_ISDIR(info.st_mode)
            if (info.st_dev != device or not (directory or stat.S_ISREG(info.st_mode))
                    or not directory and info.st_nlink != 1):
                raise HostAPIError('PATH_OUTSIDE_WORKSPACE', 403)
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if directory:
                flags |= os.O_DIRECTORY
            child = os.open(entry.name, flags, dir_fd=parent)
            try:
                opened = os.fstat(child)
                if ((opened.st_dev, opened.st_ino, opened.st_mode, opened.st_nlink)
                        != (info.st_dev, info.st_ino, info.st_mode, info.st_nlink)):
                    raise HostAPIError('PATH_OUTSIDE_WORKSPACE', 403)
                if directory:
                    # 深度也有界，避免目录链用光 FD；采用既有路径操作上界而非新增魔数。
                    if len(path) > limits.SCM_MAX_PATHS_PER_ACTION:
                        raise HostAPIError('HOST_NOT_READY', 503)
                    scan = os.scandir(child)
                    stack.append((child, scan, path))
                    child = -1
                elif path == ('config',):
                    chunks, total = [], 0
                    byte_budget = _output_budget()
                    while True:
                        chunk = os.read(child, min(65536, byte_budget + 1 - total))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        total += len(chunk)
                        if total > byte_budget:
                            raise HostAPIError('HOST_NOT_READY', 503)
                    config = b''.join(chunks)
            finally:
                if child >= 0:
                    os.close(child)
        return config
    finally:
        for parent, iterator, _ in reversed(stack):
            iterator.close()
            os.close(parent)


# LLM: 使用仓库 FD 而非重新解析路径；配置续行、可执行节和外置库失败关闭。
# 函数用途: 核验有界元数据树并限制 Git 可读取的仓库配置。
def _metadata(root, descriptor):
    if not stat.S_ISDIR(os.stat('.git', dir_fd=descriptor, follow_symlinks=False).st_mode):
        raise HostAPIError('PATH_OUTSIDE_WORKSPACE', 403)
    metadata = os.open('.git', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
    try:
        if os.fstat(metadata).st_dev != os.fstat(descriptor).st_dev:
            raise HostAPIError('PATH_OUTSIDE_WORKSPACE', 403)
        config = _scan_metadata(metadata)
        for name in ('objects', 'refs', 'info'):
            try:
                folder = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=metadata)
            except FileNotFoundError:
                continue
            os.close(folder)
        for name in ('HEAD', 'index', 'packed-refs', 'config'):
            try:
                info = os.stat(name, dir_fd=metadata, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise HostAPIError('PATH_OUTSIDE_WORKSPACE', 403)
        if config is not None:
            section = ''
            allowed = {'core': {'repositoryformatversion', 'filemode', 'bare', 'logallrefupdates',
                'ignorecase', 'precomposeunicode', 'symlinks', 'hookspath', 'fsmonitor',
                'attributesfile', 'worktree'}, 'user': {'name', 'email'},
                'branch': {'remote', 'merge'}, 'remote': {'url', 'fetch'}}
            for line in config.decode('utf-8', 'strict').splitlines():
                line = line.strip()
                if not line or line.startswith(('#', ';')):
                    continue
                if line.endswith('\\'):
                    raise HostAPIError('PATH_OUTSIDE_WORKSPACE', 403)
                if line.startswith('['):
                    match = re.fullmatch(r'\[([A-Za-z]+)(?:\s+"[^"\\]*")?\]', line)
                    if match is None or match[1].lower() not in allowed:
                        raise HostAPIError('PATH_OUTSIDE_WORKSPACE', 403)
                    section = match[1].lower()
                elif re.split(r'\s|=', line, maxsplit=1)[0].lower() not in allowed.get(section, set()):
                    raise HostAPIError('PATH_OUTSIDE_WORKSPACE', 403)
    finally:
        os.close(metadata)


# LLM: stdout 和 stderr 共用由合同 DTO 宽度推导的有限字节预算。
# 函数用途: 计算单次 Git 进程最多接收的输出量。
def _output_budget():
    """从 W0 项数和 DTO 单项宽度推导字节上界，不收集无界输出。"""
    limits = _limits()
    return (limits.SCM_MAX_STATUS_ENTRIES * (4096 * 2 + 512)
            + limits.SCM_MAX_LOG_ENTRIES * (500 * 4 + 256 * 4 + 128) + 4096)


# LLM: 固定 argv 覆盖钩子、监视器和属性源，不引入 shell 或可执行仓库配置。
# 函数用途: 生成受约束的 Git 命令行。
def _argv(arguments, hooks):
    """固定命令、字面 pathspec、空属性树；hooks 实参必须是真空目录。"""
    return ['git', '--no-pager', '--no-replace-objects', '--literal-pathspecs',
        '--attr-source=4b825dc642cb6eb9a060e54bf8d69288fbee4904',
        '-c', 'core.hooksPath=' + hooks, '-c', 'core.fsmonitor=false',
        '-c', 'core.attributesFile=' + os.devnull, '-c', 'core.worktree=.',
        '-c', 'core.bare=false', '-c', 'core.untrackedCache=false',
        '-c', 'log.showSignature=false', '-c', 'diff.external=',
        '-c', 'submodule.recurse=false', '-c', 'color.ui=false', *arguments]


# LLM: 启动前再核元数据；双管道共用超时和输出预算，失败终止子进程。
# 函数用途: 在仓库 FD 锚定的目录运行一次固定 Git 命令。
def _git(descriptor, arguments, hooks):
    """FD 锚定工作目录，双管道共用截止时间和字节预算，超量立即终止。"""
    # 再核对每次进程入口，覆盖状态到日志之间的替换；不宣称消除了核验到 exec 的并发竞态。
    _metadata(None, descriptor)
    argv = _argv(arguments, hooks)
    command = [sys.executable, '-c',
        'import os,sys; os.fchdir(int(sys.argv[1])); os.execvpe(sys.argv[2],sys.argv[2:],os.environ)',
        str(descriptor), *argv]
    env = {'PATH': os.defpath, 'LC_ALL': 'C', 'GIT_DIR': '.git', 'GIT_WORK_TREE': '.',
        'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_SYSTEM': os.devnull,
        'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_ATTR_NOSYSTEM': '1',
        'GIT_OPTIONAL_LOCKS': '0', 'GIT_NO_LAZY_FETCH': '1', 'GIT_TERMINAL_PROMPT': '0',
        'GIT_ALLOW_PROTOCOL': '', 'GIT_NO_REPLACE_OBJECTS': '1'}
    budget, output = _output_budget(), bytearray()
    deadline = time.monotonic() + _limits().SCM_GIT_TIMEOUT_SECONDS
    with subprocess.Popen(command, cwd='/', pass_fds=(descriptor,), env=env,
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE) as process:
        pipes, total, truncated = [process.stdout, process.stderr], 0, False
        try:
            while pipes:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, _limits().SCM_GIT_TIMEOUT_SECONDS)
                ready, _, _ = select.select(pipes, [], [], remaining)
                if not ready:
                    raise subprocess.TimeoutExpired(argv, _limits().SCM_GIT_TIMEOUT_SECONDS)
                for stream in ready:
                    chunk = os.read(stream.fileno(), min(65536, budget + 1 - total))
                    if not chunk:
                        pipes.remove(stream)
                        continue
                    total += len(chunk)
                    if stream is process.stdout:
                        output.extend(chunk[:max(0, budget - len(output))])
                    if total > budget:
                        truncated = True
                        process.kill()
                        break
                if truncated:
                    break
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
            return bytes(output), truncated, process.returncode
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


# LLM: 只对已打开父目录做 nofollow 查询，不替换全局 stat 能力标志。
# 函数用途: 读取单个目录项的安全元数据。
def _leaf_info(parent, name):
    """单独保留元数据接缝，测试不替换全局 os.stat 的平台能力标识。"""
    return os.stat(name, dir_fd=parent, follow_symlinks=False)


# LLM: 逐文件检查防止递归 add 扩大写入范围，普通叶必须单链接且策略允许。
# 函数用途: 在 Git 接收路径前检查父链、类型和权限。
def _validate_path(call, sub, root, value, *, write=False):
    """只允许普通单链接叶或删除的叶；目录不能借递归 add 绕过逐文件安全门。"""
    parts = _parts(value)
    if not parts:
        raise HostAPIError('PATH_OUTSIDE_WORKSPACE', 403)
    target = root.joinpath(*parts)
    if not target.is_relative_to(sub.workspace):
        raise HostAPIError('PATH_OUTSIDE_WORKSPACE', 403)
    policy = _policy(call)
    if not (policy.check_write(target) if write else policy.check(target)).allowed:
        raise HostAPIError('PATH_OUTSIDE_WORKSPACE', 403)
    parent = -1
    try:
        parent = open_directory_beneath(root, parts[:-1], require_dir_fd=True)
        try:
            info = _leaf_info(parent, parts[-1])
        except FileNotFoundError:
            return parts
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise HostAPIError('PATH_OUTSIDE_WORKSPACE', 403)
    except HostAPIError:
        raise
    except FileNotFoundError:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404) from None
    except OSError:
        raise HostAPIError('PATH_OUTSIDE_WORKSPACE', 403) from None
    finally:
        if parent >= 0:
            os.close(parent)
    return parts


# LLM: 按 NUL 记录解析并限制数量；不安全路径不进入 DTO，覆盖缺口显式截断。
# 函数用途: 把 Git 状态输出转换成有限仓库投影。
def _status(call, sub, root, raw, truncated):
    """NUL 协议保留空格与换行；重命名消费原路径，状态项上界按记录计数。"""
    repository = {'repo_root': sub.repo_root, 'branch': '', 'head': '', 'upstream': '',
                  'ahead': 0, 'behind': 0, 'working': [], 'staged': [],
                  'committed': [], 'truncated': truncated}
    chunks, count = iter(raw.split(b'\x00')), 0
    for chunk in chunks:
        if not chunk:
            continue
        text = chunk.decode('utf-8', 'replace')
        if text.startswith('# '):
            if text.startswith('# branch.oid '):
                head = text[13:]
                repository['head'] = '' if head == '(initial)' else head[:64]
            elif text.startswith('# branch.head '):
                repository['branch'] = text[14:][:256]
            elif text.startswith('# branch.upstream '):
                repository['upstream'] = text[18:][:256]
            elif text.startswith('# branch.ab '):
                match = re.fullmatch(r'# branch.ab \+(\d+) -(\d+)', text)
                if match:
                    repository['ahead'], repository['behind'] = int(match[1]), int(match[2])
            continue
        orig = None
        if text.startswith('1 '):
            fields = text.split(' ', 8)
        elif text.startswith('2 '):
            fields = text.split(' ', 9)
            orig = next(chunks, b'').decode('utf-8', 'replace')
        elif text.startswith('u '):
            fields = text.split(' ', 10)
        elif text.startswith('? '):
            fields = ['?', '??', text[2:]]
        else:
            repository['truncated'] = True
            continue
        count += 1
        if count > _limits().SCM_MAX_STATUS_ENTRIES:
            repository['truncated'] = True
            break
        if len(fields) < 3:
            repository['truncated'] = True
            continue
        path, code = fields[-1], fields[1]
        try:
            _validate_path(call, sub, root, path)
            if orig is not None:
                _validate_path(call, sub, root, orig)
        except HostAPIError:
            repository['truncated'] = True
            continue
        item = {'path': path, 'status': code[:8]}
        if orig is not None:
            item['orig_path'] = orig
        if code == '??' or len(code) > 1 and code[1] != '.':
            repository['working'].append(item)
        if code != '??' and code[0] != '.':
            repository['staged'].append(deepcopy(item))
    return repository


# LLM: 两个固定只读进程各自重验权限，超时不能伪造干净状态。
# 函数用途: 读取当前仓库的状态和有限提交日志。
def _projection(call, sub, root, descriptor, hooks):
    """状态和日志各运行一次固定 argv，读超时不能伪造干净投影。"""
    _authorize(call, sub)
    try:
        raw, truncated, rc = _git(descriptor, ['status', '--porcelain=v2', '-z', '--branch',
            '--ignore-submodules=all', '--untracked-files=all'], hooks)
        if rc and not truncated:
            raise HostAPIError('HOST_NOT_READY', 503)
        repository = _status(call, sub, root, raw, truncated)
        if repository['head']:
            _authorize(call, sub)
            raw, limited, rc = _git(descriptor, ['log', '-n', str(_limits().SCM_MAX_LOG_ENTRIES),
                '-z', '--no-show-signature', '--no-ext-diff', '--no-textconv',
                '--format=%H%x00%s%x00%an%x00%at'], hooks)
            if rc and not limited:
                raise HostAPIError('HOST_NOT_READY', 503)
            fields = raw.split(b'\x00')
            repository['truncated'] |= limited
            for offset in range(0, len(fields) - 3, 4):
                if len(repository['committed']) >= _limits().SCM_MAX_LOG_ENTRIES:
                    repository['truncated'] = True
                    break
                sha, subject, author, at = (part.decode('utf-8', 'replace') for part in fields[offset:offset + 4])
                if not re.fullmatch(r'[a-f0-9]{40,64}', sha) or not at.isdigit():
                    repository['truncated'] = True
                    continue
                repository['committed'].append({'sha': sha, 'subject': subject[:500],
                                                'author': author[:256], 'at': int(at)})
                repository['truncated'] |= len(subject) > 500 or len(author) > 256
        _authorize(call, sub)
        return repository
    except subprocess.TimeoutExpired:
        raise HostAPIError('HOST_NOT_READY', 503) from None


# LLM: 只有 DTO 摘要变化才递增 repo_seq，写后与 discard CAS 可强制刷新。
# 函数用途: 更新仓库投影和有限事件环。
def _refresh(call, sub, root, descriptor, hooks, *, force=False):
    """仅实际 DTO 摘要变化递增；强制刷新专用于写后观察和 discard CAS。"""
    now = call.clock()
    if not force and now - sub.refreshed_at < _limits().SCM_MIN_REFRESH_INTERVAL_SECONDS:
        return
    repository = _projection(call, sub, root, descriptor, hooks)
    digest = hashlib.sha256(json.dumps(repository, sort_keys=True, ensure_ascii=True,
                                       separators=(',', ':')).encode()).hexdigest()
    if digest != sub.digest:
        sub.repo_seq += 1
        sub.seq += 1
        sub.repository, sub.digest = repository, digest
        sub.ring.append({'seq': sub.seq, 'kind': 'repositoriesChanged',
                         'repo_seq': sub.repo_seq, 'repository': deepcopy(repository)})
        while len(sub.ring) > _limits().SCM_MAX_STATUS_ENTRIES:
            sub.ring.popleft()
    sub.refreshed_at = now


# LLM: 每次创建私有空目录，不把仓库目录或设备文件当作空钩子目录。
# 函数用途: 为本次 Git 调用提供临时钩子隔离目录。
@contextmanager
def _hooks(call):
    """每次调用创建私有空钩子目录；不使用已有仓库目录或 /dev/null 冒充。"""
    with tempfile.TemporaryDirectory(prefix='.scm-hooks-', dir=call.owner_home) as folder:
        yield folder


# LLM: 模块锁覆盖查回执、配额和发布订阅；重放不会再启动 Git。
# 函数用途: 为当前 owner 建立有限仓库订阅或返回原回执。
def subscribe(call: V13Call):
    """订阅编号 owner 隔离；相同幂等键复用已有订阅而不再运行 Git。"""
    with _lock:
        epoch, owner = _authorize(call), _owner_key(call)
        _parts(call.params['repo_root'])
        fingerprint = _fingerprint(call)
        controls = _control_receipts(call, epoch)
        key = call.subject_id, call.action, call.idempotency_key
        if key in controls:
            return _replay(controls[key], fingerprint)
        if len(controls) >= _limits().SCM_MAX_STATUS_ENTRIES:
            raise HostAPIError('LIMIT_EXCEEDED')
        state = _owner_state(call, epoch)
        if len(state.subscriptions) >= _limits().SCM_MAX_SUBSCRIPTIONS_PER_OWNER:
            raise HostAPIError('LIMIT_EXCEEDED')
        workspace = Path(_fs().workspace_root(call)).absolute()
        sub = _Subscription(owner, epoch, workspace, call.params['repo_root'])
        with _repo(call, sub) as (root, descriptor), _hooks(call) as hooks:
            sub.identity = _identity(descriptor)
            _refresh(call, sub, root, descriptor, hooks, force=True)
        sid = 'scm-' + uuid.uuid4().hex
        result = {'subscription_id': sid, 'repo_seq': sub.repo_seq,
                  'repository': deepcopy(sub.repository)}
        sub.receipts[(call.subject_id, call.action, call.idempotency_key)] = (fingerprint, result)
        _subscriptions[sid] = sub
        state.subscriptions[sid] = sub
        controls[key] = fingerprint, result
        return deepcopy(result)


# LLM: 返回深拷贝且有界的事件，缺失历史显式标记 overflow。
# 函数用途: 拉取并按节流刷新当前订阅的仓库变化。
def events(call: V13Call):
    """拉取有限事件；溢出显式返回最新完整投影，不把缺失历史伪装成完整。"""
    with _lock:
        sub = _get(call)
        after, limit = call.params['after_seq'], call.params.get('limit', 50)
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise HostAPIError('INVALID_SCHEMA')
        with _repo(call, sub) as (root, descriptor), _hooks(call) as hooks:
            _refresh(call, sub, root, descriptor, hooks)
        overflow = bool(sub.ring and after < sub.ring[0]['seq'] - 1)
        if overflow:
            rows = [{'seq': sub.seq, 'kind': 'overflow', 'repo_seq': sub.repo_seq,
                     'repository': deepcopy(sub.repository)}]
        else:
            rows = [deepcopy(row) for row in sub.ring if row['seq'] > after][:limit]
        return {'events': rows, 'next_seq': rows[-1]['seq'] if rows else max(after, sub.seq),
                'overflow': overflow}


# LLM: 删除前查稳定控制回执，外来编号与缺失编号一致；容量满不淘汰旧回执。
# 函数用途: 移除本 owner 订阅并保存幂等控制结果。
def unsubscribe(call: V13Call):
    """外来编号和缺失编号同样返回 false，不泄露资源存在性。"""
    with _lock:
        epoch = _authorize(call)
        controls = _control_receipts(call, epoch)
        key = call.subject_id, call.action, call.idempotency_key
        fingerprint = _fingerprint(call)
        if key in controls:
            return _replay(controls[key], fingerprint)
        if len(controls) >= _limits().SCM_MAX_STATUS_ENTRIES:
            raise HostAPIError('OUTCOME_UNKNOWN', 503)
        sid = call.params['subscription_id']
        state = _owner_state(call, epoch)
        sub = state.subscriptions.get(sid)
        removed = sub is not None and sub.owner == _owner_key(call)
        if removed:
            del _subscriptions[sid]
            del state.subscriptions[sid]
        result = {'removed': removed}
        controls[key] = fingerprint, result
        return result


# LLM: 只摘要规范 JSON 参数，重放键另含主体、动作和幂等键。
# 函数用途: 计算请求参数的稳定指纹。
def _fingerprint(call):
    return hashlib.sha256(json.dumps(call.params, sort_keys=True, ensure_ascii=True,
                                    separators=(',', ':')).encode()).hexdigest()


# LLM: 未知写结果不可重做，参数变动拒绝，成功 DTO 通过深拷贝返回。
# 函数用途: 核对指纹并重放已保存的领域回执。
def _replay(receipt, fingerprint):
    previous, result = receipt
    if previous != fingerprint:
        raise HostAPIError('IDEMPOTENCY_CONFLICT', 409)
    if result is None:
        raise HostAPIError('OUTCOME_UNKNOWN', 503, 'unknown')
    return deepcopy(result)


# LLM: 写前封存未知回执；逐叶复验，discard 先强刷 CAS，任何未知结果都不自动重试。
# 函数用途: 执行逐路径 stage、unstage 或 discard 并记录结果。
def _mutate(call, command):
    """逐叶写前重核；先封存未知回执再启动，超时和写后读失败绝不重做。"""
    with _lock:
        sub = _get(call)
        if not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', call.idempotency_key):
            raise HostAPIError('INVALID_SCHEMA')
        paths = call.params['paths']
        if not isinstance(paths, list) or len(paths) > _limits().SCM_MAX_PATHS_PER_ACTION:
            raise HostAPIError('INVALID_SCHEMA')
        key = call.subject_id, call.action, call.idempotency_key
        fingerprint = _fingerprint(call)
        if key in sub.receipts:
            return _replay(sub.receipts[key], fingerprint)
        if len(sub.receipts) >= _limits().SCM_MAX_STATUS_ENTRIES:
            raise HostAPIError('HOST_NOT_READY', 503)
        # 所有字面错误先收集；整批都坏时连 status/log 也不启动。
        valid, failed = [], []
        for path in paths:
            try:
                if not _parts(path):
                    raise HostAPIError('PATH_OUTSIDE_WORKSPACE', 403)
                valid.append(path)
            except HostAPIError as error:
                failed.append({'path': path, 'code': error.code})
        if not valid:
            result = {'succeeded': [], 'failed': failed, 'repo_seq': sub.repo_seq}
            sub.receipts[key] = fingerprint, result
            return deepcopy(result)
        with _repo(call, sub) as (root, descriptor), _hooks(call) as hooks:
            safe = []
            for path in valid:
                try:
                    _validate_path(call, sub, root, path, write=True)
                    safe.append(path)
                except HostAPIError as error:
                    failed.append({'path': path, 'code': error.code})
            if safe and command == ['checkout']:
                _refresh(call, sub, root, descriptor, hooks, force=True)
                if call.params['expected_repo_seq'] != sub.repo_seq:
                    error = HostAPIError('REVISION_CONFLICT', 409,
                        message='仓库状态已变化；当前 repo_seq=' + str(sub.repo_seq))
                    error.current_repo_seq = sub.repo_seq
                    error.details = {'current_repo_seq': sub.repo_seq}
                    raise error
            succeeded = []
            if safe:
                sub.receipts[key] = fingerprint, None
            try:
                for path in safe:
                    _authorize(call, sub)
                    _validate_path(call, sub, root, path, write=True)
                    _metadata(root, descriptor)
                    _, truncated, rc = _git(descriptor, [*command, '--', path], hooks)
                    if truncated:
                        raise HostAPIError('OUTCOME_UNKNOWN', 503, 'unknown')
                    if rc:
                        failed.append({'path': path, 'code': 'RESOURCE_NOT_FOUND'})
                    else:
                        succeeded.append(path)
                if safe:
                    _refresh(call, sub, root, descriptor, hooks, force=True)
            except (subprocess.TimeoutExpired, OSError):
                raise HostAPIError('OUTCOME_UNKNOWN', 503, 'unknown') from None
            except HostAPIError as error:
                if safe:
                    raise HostAPIError('OUTCOME_UNKNOWN', 503, 'unknown') from error
                raise
        result = {'succeeded': succeeded, 'failed': failed, 'repo_seq': sub.repo_seq}
        sub.receipts[key] = fingerprint, result
        return deepcopy(result)


# LLM: 只能通过统一写入门启动固定 add argv，不能扩大路径或重试未知结果。
# 函数用途: 暂存本次请求中的安全文件。
def stage(call: V13Call):
    return _mutate(call, ['add'])


# LLM: 只能通过统一写入门启动固定 restore --staged argv。
# 函数用途: 取消指定安全文件的暂存。
def unstage(call: V13Call):
    return _mutate(call, ['restore', '--staged'])


# LLM: 丢弃必须满足 expected_repo_seq CAS，超时后只保留未知结果而不重做。
# 函数用途: 恢复指定安全文件到索引内容。
def discard(call: V13Call):
    return _mutate(call, ['checkout'])
