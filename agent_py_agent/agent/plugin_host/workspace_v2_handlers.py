# LLM: 内部handler仅接已核owner；W4/W6先核来源/能力/线程/幂等，路径不能扩权，所有用户相对路径逐段nofollow。
# 模块用途: 提供通用文件预览、20MiB上传下载及有界只读Git观察，不执行仓库自定义diff或fsmonitor。
from __future__ import annotations

import base64
import codecs
import os
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
from .authorization import workspace_directory
from .capabilities_v2_handlers import _parts
from .errors import HostAPIError

_MAX, _GIT_MAX = 20 * 1024 * 1024, 256 * 1024
# LLM: owner内目录以其家为上限；外部目录必须过原管理员门；从真实文件系统根逐段打开，避免父链接逃逸。
# 函数用途: 验证根并在全程持有目录FD，所有异常关闭资源。
@contextmanager
def _directory(context, payload, parts=()):
    descriptor = -1
    try:
        root = context.owner_home.home_dir / Path(payload["workspace_dir"]).expanduser()
        if ".." in root.parts:
            raise HostAPIError("WORKSPACE_PATH_INVALID")
        if not root.is_relative_to(context.owner_home.home_dir):
            root = workspace_directory(context, str(root))
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
# LLM: 逐项lstat不跟随链接，普通单链接文件和目录才展示，达到限额即停止。
# 函数用途: 返回查询匹配的直接子项，跳过Git内部噪声。
def list_workspace_files(context, payload):
    parts, items = _parts(payload["relative_path"]) if payload.get("relative_path") else (), []
    limit = _limit(payload.get("limit", 200), 2000)
    with _directory(context, payload, parts) as (root, _, descriptor):
        with os.scandir(descriptor) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                if entry.name == ".git" or payload.get("query", "").casefold() not in entry.name.casefold():
                    continue
                if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode) and info.st_nlink == 1):
                    continue
                items.append({"name": entry.name, "relative_path": "/".join((*parts, entry.name)),
                              "kind": "dir" if stat.S_ISDIR(info.st_mode) else "file",
                              "size_bytes": info.st_size, "modified_at": info.st_mtime})
                if len(items) == limit:
                    break
        return {"workspace_dir": str(root), "relative_path": "/".join(parts), "items": items}
# LLM: 只读普通单链接文件；实际读取至多预算+1字节，保留真实文件大小，不把预览行数当总行数。
# 函数用途: 读取一份有界字节预览。
def _read(root, prefix, path, limit):
    with os.fdopen(open_readonly_file_beneath(root.anchor, (*prefix, *_parts(path))), "rb") as stream:
        return stream.read(limit + 1), os.fstat(stream.fileno()).st_size
# LLM: UTF8前缀切在多字节字符中不误判binary；截断时总行数未知，媒体仅回base64不执行。
# 函数用途: 返回文本行范围或二进制预览。
def read_workspace_file(context, payload):
    limit = _limit(payload.get("max_bytes", 262144), _MAX)
    with _directory(context, payload) as (root, prefix, _):
        raw, size = _read(root, prefix, payload["path"], limit)
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
# LLM: 上传固定在uploads内；解码前后都限额，原子no-follow写拒绝父/叶链接，提交后故障保持unknown。
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
        try:
            write_bytes_atomic_beneath(root.anchor, (*prefix, *parts), raw)
        except OSError:
            raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown") from None
    return {"path": "/".join(parts), "size_bytes": len(raw)}
# LLM: 下载不默默截断大文件，20MiB上限按解码后的原字节计算。
# 函数用途: 下载完整通用文件。
def download_workspace_file(context, payload):
    with _directory(context, payload) as (root, prefix, _):
        raw, size = _read(root, prefix, payload["path"], _MAX)
        if len(raw) > _MAX:
            raise HostAPIError("PAYLOAD_TOO_LARGE", 413)
        return {"path": payload["path"], "data_base64": base64.b64encode(raw).decode(), "size_bytes": size}
# LLM: 固定argv禁shell、外部diff/fsmonitor和锁写；管道最多读256KiB+1，超时/超量立即清理子进程。
# 函数用途: 在已持目录FD内执行一条有界只读Git命令。
def _git(descriptor, arguments):
    output, deadline = bytearray(), time.monotonic() + 5.0
    env = {"PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_OPTIONAL_LOCKS": "0", "GIT_ATTR_NOSYSTEM": "1", "GIT_NO_LAZY_FETCH": "1"}
    git = ["git", "--no-pager", "--literal-pathspecs", "--attr-source=4b825dc642cb6eb9a060e54bf8d69288fbee4904", "-c", "core.attributesFile=" + os.devnull, "-c", "core.fsmonitor=false", "-c", "core.worktree=.", *arguments]
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
# LLM: NUL分隔保留换行/空格文件名；路径字面匹配且前置nofollow，Git状态不足时不宣称clean。
# 函数用途: 返回Git分支、变更清单和有界diff；workspace.git.inspect可由注册层映射同一函数。
def inspect_workspace_git(context, payload):
    path = "/".join(_parts(payload["path"])) if payload.get("path") else ""
    with _directory(context, payload) as (root, prefix, descriptor):
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
        status, limited = _git(descriptor, ["status", "--porcelain=v1", "-b", "-z", "--ignore-submodules=all", "--", path or "."])
        diff, truncated = _git(descriptor, ["diff", "--no-ext-diff", "--no-textconv", "--ignore-submodules=all", "--", path or "."])
        entries, changed = iter(status.split("\x00")), []
        branch = next(entries, "").removeprefix("## ").split("...")[0].removeprefix("No commits yet on ")
        for entry in entries:
            if entry:
                changed.append({"status": entry[:2].strip(), "path": entry[3:]})
                if "R" in entry[:2] or "C" in entry[:2]:
                    next(entries, None)
        return {"branch": branch, "clean": not changed and not limited and not truncated,
                "changed_files": changed, "diff": diff, "truncated": limited or truncated}
