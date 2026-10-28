# LLM: 内部handler仅接已核owner；W4/W6先核来源/能力/线程/幂等，路径不能扩权，所有用户相对路径逐段nofollow。
# 模块用途: 提供通用文件预览、20MiB上传下载及有界只读Git观察，不执行仓库自定义diff或fsmonitor。
from __future__ import annotations

import base64
import codecs
import os
import re
import select
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from os import stat as _stat
from pathlib import Path

from ..common.nofollow_fs import (
    open_directory_beneath,
    open_readonly_file_beneath,
    write_bytes_atomic_beneath,
)
from ..conversation.workspace_context import validate_workspace_directory
from ..path_access_policy import HOST_STATE_OWNER_DIRS
from ..user_space.owner_resolver import OwnerIdentity
from .authorization import workspace_directory
from .capabilities_v2_handlers import _parts
from .errors import HostAPIError

_MAX, _GIT_MAX = 20 * 1024 * 1024, 256 * 1024
# LLM: 内外工作根及子路径均按Unicode casefold与inode拒绝.git及宿主控制目录/runs，不能误杀普通前缀。
# 函数用途: 拒绝把当前家目录内的运行状态当作工作区文件。
def _forbid_internal(home, target):
    if any(part.casefold() == ".git" for part in target.parts):
        raise HostAPIError("PATH_OUTSIDE_WORKSPACE", 403)
    t_parts = tuple(p.casefold() for p in target.parts)
    for base in (home, home.resolve(strict=False)):
        h_parts = tuple(p.casefold() for p in base.parts)
        if t_parts[:len(h_parts)] == h_parts:
            rel = t_parts[len(h_parts):]
            if any(rel[:len(parts)] == tuple(p.casefold() for p in parts) for parts in (*HOST_STATE_OWNER_DIRS, ("runs",))):
                raise HostAPIError("WORKSPACE_PATH_INVALID")
    inodes = set()
    for parts in (*HOST_STATE_OWNER_DIRS, ("runs",)):
        try:
            st = home.joinpath(*parts).lstat()
            inodes.add((st.st_dev, st.st_ino))
        except OSError:
            pass
    if inodes:
        for node in (target, *target.parents):
            try:
                st = node.lstat()
                if (st.st_dev, st.st_ino) in inodes:
                    raise HostAPIError("WORKSPACE_PATH_INVALID")
            except HostAPIError:
                raise
            except OSError:
                pass
# LLM: 目录I/O前先拒绝.git组件，再核owner内外根与控制目录；持有字面路径FD，resolve不能放行根链接。
# 函数用途: 验证根并在全程持有目录FD，所有异常关闭资源。
@contextmanager
def _directory(context, payload, parts=()):
    descriptor = -1
    try:
        home = context.owner_home.home_dir
        root = home / Path(payload["workspace_dir"]).expanduser()
        _forbid_internal(home, root.joinpath(*parts))
        if ".." in root.parts:
            raise HostAPIError("WORKSPACE_PATH_INVALID")
        if root.is_relative_to(home):
            validate_workspace_directory(root, owner_home=home, is_admin=context.identity == OwnerIdentity.local_main(),
                                         home_root=context.owner_home.root)
        else:
            root = workspace_directory(context, str(root))
        _forbid_internal(home, root.joinpath(*parts))
        prefix = root.parts[1:]
        descriptor = open_directory_beneath(root.anchor, (*prefix, *parts), require_dir_fd=True)
        yield root, prefix, descriptor
    except HostAPIError:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        raise HostAPIError("WORKSPACE_PATH_INVALID") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
# LLM: 限额严格整型，bool/零/超上限均拒绝，不能借大整数变成无界读取。
# 函数用途: 检查数量或字节预算。
def _limit(value, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise HostAPIError("INVALID_SCHEMA")
    return value
# LLM: 逐项先滤控制目录和任意大小写.git再lstat，不跟随链接，仅展示普通单链接文件/目录。
# 函数用途: 返回查询匹配的直接子项，跳过Git内部噪声。
def list_workspace_files(context, payload):
    parts, items = _parts(payload["relative_path"]) if payload.get("relative_path") else (), []
    limit = _limit(payload.get("limit", 200), 2000)
    with _directory(context, payload, parts) as (root, _, descriptor):
        with os.scandir(descriptor) as entries:
            for entry in entries:
                try:
                    _forbid_internal(context.owner_home.home_dir, root.joinpath(*parts, entry.name))
                except HostAPIError:
                    continue
                info = entry.stat(follow_symlinks=False)
                if entry.name.casefold() == ".git" or payload.get("query", "").casefold() not in entry.name.casefold():
                    continue
                if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode) and info.st_nlink == 1):
                    continue
                items.append({"name": entry.name, "relative_path": "/".join((*parts, entry.name)),
                              "kind": "dir" if stat.S_ISDIR(info.st_mode) else "file",
                              "size_bytes": info.st_size, "modified_at": info.st_mtime})
                if len(items) == limit:
                    break
        return {"workspace_dir": str(root), "relative_path": "/".join(parts), "items": items}
# LLM: 相对文件另核控制目录，只读普通单链接文件；至多读取预算+1字节，不把预览行数当总行数。
# 函数用途: 读取一份有界字节预览。
def _read(home, root, prefix, path, limit):
    parts = _parts(path)
    _forbid_internal(home, root.joinpath(*parts))
    with os.fdopen(open_readonly_file_beneath(root.anchor, (*prefix, *parts)), "rb") as stream:
        return stream.read(limit + 1), os.fstat(stream.fileno()).st_size
# LLM: 经当前home的控制目录检查后预览；UTF8切在多字节中不误判binary，截断总行数未知。
# 函数用途: 返回文本行范围或二进制预览。
def read_workspace_file(context, payload):
    limit = _limit(payload.get("max_bytes", 262144), _MAX)
    with _directory(context, payload) as (root, prefix, _):
        raw, size = _read(context.owner_home.home_dir, root, prefix, payload["path"], limit)
        truncated, raw = len(raw) > limit, raw[:limit]
        try:
            content = codecs.getincrementaldecoder("utf-8")().decode(raw, final=not truncated)
            if "\x00" in content:
                raise UnicodeError
        except UnicodeError:
            return {"path": payload["path"], "is_binary": True, "content": base64.b64encode(raw).decode(),
                    "total_lines": None, "start_line": None, "end_line": None, "size_bytes": size, "truncated": truncated}
        lines = content.splitlines(keepends=True)
        start = _limit(payload.get("start_line", 1), 2**31 - 1)
        end = _limit(payload.get("end_line", max(start, len(lines))), 2**31 - 1)
        if end < start:
            raise HostAPIError("INVALID_SCHEMA")
        return {"path": payload["path"], "is_binary": False, "content": "".join(lines[start - 1:end]),
                "total_lines": None if truncated else len(lines), "start_line": start, "end_line": min(end, len(lines)),
                "size_bytes": size, "truncated": truncated}
# LLM: 上传固定在uploads且拒绝内部控制目标；解码前后都限额，原子nofollow写提交后故障保持unknown。
# 函数用途: 保存通用模板或媒体，不按扩展名另造领域接口。
def upload_workspace_file(context, payload):
    parts = _parts(payload.get("path", payload.get("filename", "")))
    parts = parts if parts[0] == "uploads" else ("uploads", *parts)
    encoded = payload.get("data_base64")
    if not isinstance(encoded, str) or len(encoded) > 4 * ((_MAX + 2) // 3):
        raise HostAPIError("PAYLOAD_TOO_LARGE", 413)
    try:
        raw = base64.b64decode(encoded, validate=True)
    except ValueError:
        raise HostAPIError("INVALID_SCHEMA") from None
    if len(raw) > _MAX:
        raise HostAPIError("PAYLOAD_TOO_LARGE", 413)
    with _directory(context, payload) as (root, prefix, _):
        _forbid_internal(context.owner_home.home_dir, root.joinpath(*parts))
        try:
            write_bytes_atomic_beneath(root.anchor, (*prefix, *parts), raw)
        except OSError:
            raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown") from None
    return {"path": "/".join(parts), "size_bytes": len(raw)}
# LLM: 下载目标经过同一控制目录门，不默默截断大文件，20MiB上限按原字节计算。
# 函数用途: 下载完整通用文件。
def download_workspace_file(context, payload):
    with _directory(context, payload) as (root, prefix, _):
        raw, size = _read(context.owner_home.home_dir, root, prefix, payload["path"], _MAX)
        if len(raw) > _MAX:
            raise HostAPIError("PAYLOAD_TOO_LARGE", 413)
        return {"path": payload["path"], "data_base64": base64.b64encode(raw).decode(), "size_bytes": size}
# LLM: 宿主构造的Git路径规格标记类；普通用户str不会被误认为宿主规格，始终保持--literal-pathspecs。
# 类用途: 结构化区分宿主生成的:(literal)/:(exclude,icase)规格与用户原始路径字符串。
class _Spec(str):
    pass
# LLM: 固定argv禁shell、外部diff/fsmonitor和锁写；管道最多读256KiB+1，超时/超量立即清理子进程。
# 函数用途: 在已持目录FD内执行一条有界只读Git命令。
def _git(descriptor, arguments):
    output, deadline = bytearray(), time.monotonic() + 5.0
    env = {"PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_OPTIONAL_LOCKS": "0", "GIT_ATTR_NOSYSTEM": "1", "GIT_NO_LAZY_FETCH": "1"}
    lit = () if any(isinstance(a, _Spec) for a in arguments) else ("--literal-pathspecs",)
    git = ["git", "--no-pager", *lit, "--attr-source=4b825dc642cb6eb9a060e54bf8d69288fbee4904", "-c", "core.attributesFile=" + os.devnull, "-c", "core.fsmonitor=false", "-c", "color.ui=never", "-c", "color.diff=never", "-c", "color.status=never", "-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false", "-c", "diff.srcPrefix=a/", "-c", "diff.dstPrefix=b/", "-c", "core.worktree=.", *arguments]
    command = [sys.executable, "-c", "import os,sys; os.fchdir(int(sys.argv[1])); os.execvpe(sys.argv[2],sys.argv[2:],os.environ)", str(descriptor), *git]
    with subprocess.Popen(command, cwd="/", pass_fds=(descriptor,), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as process:
        try:
            while select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))[0]:
                chunk = os.read(process.stdout.fileno(), min(65536, _GIT_MAX + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > _GIT_MAX:
                    process.kill()
                    break
            else:
                raise subprocess.TimeoutExpired(command, 5.0)
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
            if process.returncode and len(output) <= _GIT_MAX:
                raise HostAPIError("WORKSPACE_GIT_FAILED")
        except subprocess.TimeoutExpired:
            raise HostAPIError("WORKSPACE_GIT_TIMEOUT", 504) from None
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    return bytes(output[:_GIT_MAX]).decode("utf-8", "replace"), len(output) > _GIT_MAX
_ESC = {b"n": b"\n", b"t": b"\t", b"r": b"\r", b"b": b"\b", b"f": b"\f", b"v": b"\v", b"a": b"\a", b"\\": b"\\", b'"': b'"'}
_PFX = ("a/", "b/", "i/", "w/", "c/", "o/", "1/", "2/")
# LLM: 解码Git双引号与八进制字节/C转义路径，按需剥离展示前缀，未知前缀返回None触发整块拒绝。
# 函数用途: 还原Git diff头中的带引号或转义路径。
def _unq(token, strip_prefix=True):
    if token.startswith('"') or token.endswith('"'):
        if not re.fullmatch(r'"(?:\\.|[^"\\])*"', token):
            return None
        raw = re.sub(rb"\\([0-7]{1,3}|.)", lambda m: bytes([int(m[1], 8)]) if m[1].isdigit() else _ESC.get(m[1], m[1]), token[1:-1].encode())
        token = raw.decode("utf-8", "replace")
    return token if not strip_prefix else (token[2:] if token.startswith(_PFX) else None)
# LLM: 过滤落入宿主控制目录或runs的Git变更与diff块，按行首diff头与C转义路径严格拦截。
# 函数用途: 拆解Git状态与diff并剔除受保护子目录条目。
def _git_filter(home, root, status, diff):
    # LLM: 逐项校验相对路径是否触碰宿主受保护目录，任一为None或越界即整块拒绝。
    # 函数用途: 判断Git状态或diff条目涉及的路径集合是否全部安全。
    def _ok(paths):
        try:
            for p in paths:
                if p is None:
                    return False
                if p:
                    _forbid_internal(home, root / p)
            return True
        except HostAPIError:
            return False
    entries, changed = iter(status.split("\x00")), []
    branch = next(entries, "").removeprefix("## ").split("...")[0].removeprefix("No commits yet on ")
    for entry in filter(None, entries):
        code, rel = entry[:2], entry[3:]
        old = next(entries, "") if ("R" in code or "C" in code) else ""
        if _ok((rel, old)):
            changed.append({"status": code.strip(), "path": rel})
    kept = []
    for chunk in re.split(r"(?m)(?=^diff --git )", diff):
        if not chunk.startswith("diff --git "):
            continue
        head, hdr = chunk.split("\n", 1)[0][11:], chunk.split("\n@@", 1)[0].splitlines()[1:]
        ren = [_unq(ln.split(" ", 2)[2], False) for ln in hdr if ln.startswith(("rename from ", "rename to ", "copy from ", "copy to "))]
        pm = [_unq(ln[4:].split("\t", 1)[0]) for ln in hdr if ln.startswith(("--- ", "+++ ")) and ln[4:].strip() != "/dev/null"]
        known = {p for p in (*ren, *pm) if p}
        pairs = [(lp, rp) for m in re.finditer(r" ", head) if (lp := _unq(head[:m.start()])) is not None and (rp := _unq(head[m.end():])) is not None]
        hp = next((p for p in pairs if known and set(p) <= known), next((p for p in pairs if not known and (p[0] == p[1] or p[0].rpartition("/")[0] == p[1].rpartition("/")[0])), pairs[0] if pairs else (None,)))
        if len(hp) == 2 and _ok((*hp, *ren, *pm)):
            kept.append(chunk)
    return branch, changed, "".join(kept)
# LLM: Git启动前核目标控制目录和nofollow；NUL分隔保留换行/空格文件名，状态不足时不宣称clean。
# 函数用途: 返回Git分支、变更清单和有界diff；workspace.git.inspect可由注册层映射同一函数。
def inspect_workspace_git(context, payload):
    path = "/".join(_parts(payload["path"])) if payload.get("path") else ""
    with _directory(context, payload) as (root, prefix, descriptor):
        home = context.owner_home.home_dir
        _forbid_internal(home, root / path)
        # 外置gitdir/commondir和对象库链接不在单项目文件根内，保守拒绝而不扩大目录授权。
        if not stat.S_ISDIR(_stat(".git", dir_fd=descriptor, follow_symlinks=False).st_mode):
            raise HostAPIError("WORKSPACE_GIT_EXTERNAL_METADATA_DENIED", 403)
        for folder, directories, names in os.walk(root / ".git", followlinks=False):
            for name in (*directories, *names):
                info = (Path(folder) / name).lstat()
                if stat.S_ISLNK(info.st_mode) or stat.S_ISREG(info.st_mode) and info.st_nlink != 1 or name in {"commondir", "alternates", "attributes"}:
                    raise HostAPIError("WORKSPACE_GIT_EXTERNAL_METADATA_DENIED", 403)
        if path:
            parent = open_directory_beneath(root.anchor, (*prefix, *_parts(path)[:-1]), require_dir_fd=True)
            try:
                if stat.S_ISLNK(os.stat(_parts(path)[-1], dir_fd=parent, follow_symlinks=False).st_mode):
                    raise HostAPIError("WORKSPACE_PATH_INVALID")
            except FileNotFoundError:
                pass
            finally:
                os.close(parent)
        ex = [_Spec(f":(exclude,icase){home.joinpath(*p).relative_to(root).as_posix()}")
              for p in (*HOST_STATE_OWNER_DIRS, ("runs",)) if home.joinpath(*p).is_relative_to(root)]
        specs = [_Spec(f":(literal){path or '.'}"), *ex]
        status, limited = _git(descriptor, ["status", "--porcelain=v1", "-b", "-z", "--ignore-submodules=all", "--", *specs])
        diff, truncated = _git(descriptor, ["diff", "--no-color", "--no-ext-diff", "--no-textconv", "--ignore-submodules=all", "--", *specs])
        branch, changed, diff = _git_filter(home, root, status, diff)
        return {"branch": branch, "clean": not changed and not diff and not limited and not truncated,
                "changed_files": changed, "diff": diff, "truncated": limited or truncated}
