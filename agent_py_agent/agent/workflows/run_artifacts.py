# LLM: 目标只由正式工作流上下文绑定；源码读取共用strict nofollow，先暂存后清理再原子发表，不扩沙箱。
# 模块用途: 将一次工作流脚本的有界文件集合归档到原run，并沿Owner配额计费。
from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from ..common.nofollow_fs import (
    NoFollowPathError,
    open_directory_beneath,
    read_bytes_beneath,
    write_bytes_atomic_beneath,
)
from ..user_space.owner_quota import OwnerQuotaChange, OwnerQuotaExceeded, OwnerQuotaUnavailable
from .models import safe_path

_RUN_OUT_TARGET = ContextVar('workflow_run_out_target', default=None)


def workflow_artifact_run_key(workflow_run_id: str) -> str:
    """长的自定义运行编号沿原运行索引点查，公开编号保持在接口的128字节内。"""
    if len(workflow_run_id) > 64:
        return '~' + hashlib.sha256(workflow_run_id.encode()).hexdigest()[:63]
    return workflow_run_id


def workflow_artifact_id(workflow_run_id: str, step_attempt_id: str, relative_path: str) -> str:
    """编号只定位原运行及其清单；同名文件在不同尝试中保持独立。"""
    run_key = workflow_artifact_run_key(workflow_run_id)
    file_key = hashlib.sha256(relative_path.encode('utf-8', 'surrogatepass')).hexdigest()[:24]
    return f'wfart-{run_key}-{step_attempt_id}-{file_key}'


def parse_workflow_artifact_id(value: str) -> tuple[str, str, str] | None:
    """路径不进入公开输入；解析结果仅用于定位，仍需重核业务归属、原清单及字节。"""
    if not isinstance(value, str) or len(value) > 128 or not value.startswith('wfart-'):
        return None
    parts = value[6:].rsplit('-', 2)
    if len(parts) != 3:
        return None
    run_key, attempt, file_key = parts
    if (not re.fullmatch(r'(?:[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}|~[0-9a-f]{63})', run_key)
            or not re.fullmatch('[0-9a-f]{32}', attempt) or not re.fullmatch('[0-9a-f]{24}', file_key)):
        return None
    return run_key, attempt, file_key


def workflow_artifact_run_ids(owner_home: Path, run_key: str) -> tuple[str, ...]:
    if not run_key.startswith('~'):
        return (run_key,)
    from .run_list_index import RunListIndex
    return RunListIndex(owner_home).artifact_run_ids(run_key)


# LLM: 机器码不由异常文字推导，所有归档失败均阻止步骤成功。
# 类用途: 携带工作流文件归档的稳定错误码。
class RunOutError(ValueError):
    # LLM: 不在错误消息中回显文件内容或凭据。
    # 函数用途: 构造明确的文件归档拒绝。
    def __init__(self, error_code):
        self.error_code = error_code
        super().__init__(error_code)

    # LLM: 最多投影八层显式cause，仅类型、整数errno和回滚标记；不持久化原始路径、内容、异常文案或notes。
    # 函数用途: 为正式runner提供有界、脱敏的归档首因诊断。
    def diagnostics(self):
        causes, seen, current, rollback_incomplete = [], set(), self, False
        for _ in range(8):
            if current is None or id(current) in seen:
                break
            seen.add(id(current))
            if current is not self:
                number = getattr(current, 'errno', None)
                causes.append({'exception_type': type(current).__name__,
                               'errno': number if type(number) is int else None})
            rollback_incomplete |= any(isinstance(note, str) and note.startswith('归档回滚未完成: ')
                                       for note in getattr(current, '__notes__', ()))
            current = current.__cause__
        return {'cause_chain': causes, 'rollback_incomplete': rollback_incomplete}


# LLM: 目标身份只在本次调用栈有效，不从业务inputs或脚本环境读取。
# 类用途: 冻结Owner、原run、步骤及原attempt身份。
@dataclass(frozen=True)
class RunOutTarget:
    owner_home: Path
    workflow_run_id: str
    step_id: str
    attempt_id: str


# LLM: ContextVar token在全部出口重置；debug显式清空继承目标，数据源包装不改变调用链。
# 函数用途: 为一次正式脚本步骤绑定受信归档目标。
@contextmanager
def bind_run_out_target(owner_home, workflow_run_id, step_id, attempt_id, *, enabled=True):
    target = None
    if enabled:
        home = Path(owner_home).absolute()
        for value in (workflow_run_id, step_id, attempt_id):
            safe_path(home, value)
        target = RunOutTarget(home, workflow_run_id, step_id, attempt_id)
    token = _RUN_OUT_TARGET.set(target)
    try:
        yield target
    finally:
        _RUN_OUT_TARGET.reset(token)


# LLM: 跨Owner上下文不降级成其它Owner的写入权限。
# 函数用途: 取得当前脚本Owner的正式归档目标，未绑定时返回None。
def current_run_out_target(owner_home):
    target = _RUN_OUT_TARGET.get()
    if target is not None and target.owner_home != Path(owner_home).absolute():
        raise RunOutError('SCRIPT_RUN_OUT_HARVEST_FAILED')
    return target


# LLM: 文件数/总字节先做元数据准入，迭代器和FD全部有界关闭；文件正文只经common nofollow读取。
# 函数用途: 生成有限普通单链接文件清单，不先读完无限文件再检查上限。
def _manifest(scratch, max_files, max_bytes):
    pending, files, total, entries = [(('run_out',), 0)], [], 0, 0
    while pending:
        parts, depth = pending.pop()
        if depth > 64:
            raise RunOutError('SCRIPT_RUN_OUT_LIMIT_EXCEEDED')
        descriptor = open_directory_beneath(scratch, parts, require_dir_fd=True)
        try:
            with os.scandir(descriptor) as children:
                for child in children:
                    entries += 1
                    if entries > max_files * 128 + 128:
                        raise RunOutError('SCRIPT_RUN_OUT_LIMIT_EXCEEDED')
                    info = child.stat(follow_symlinks=False)
                    path = (*parts, child.name)
                    if stat.S_ISDIR(info.st_mode):
                        pending.append((path, depth + 1))
                        continue
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise RunOutError('SCRIPT_RUN_OUT_LINK_REJECTED')
                    total += info.st_size
                    if len(files) >= max_files or total > max_bytes:
                        raise RunOutError('SCRIPT_RUN_OUT_LIMIT_EXCEEDED')
                    files.append((path, info.st_size))
        finally:
            os.close(descriptor)
    return sorted(files)


# LLM: 普通I/O归档失败、路径拒绝及quota拒绝分开表达；不把失败伪装成空产物成功。
# 函数用途: 将底层归档异常转为稳定的步骤错误。
def _run_out_error(error):
    if isinstance(error, RunOutError):
        return error
    if isinstance(error, (OwnerQuotaExceeded, OwnerQuotaUnavailable)):
        return RunOutError('SCRIPT_RUN_OUT_QUOTA_EXCEEDED')
    if isinstance(error, NoFollowPathError):
        return RunOutError('SCRIPT_RUN_OUT_LINK_REJECTED')
    return RunOutError('SCRIPT_RUN_OUT_HARVEST_FAILED')


# LLM: 暂存身份和最终集合属于一次attempt；只有整个发表及配额结算成功后才返回引用。
# 类用途: 持有一次暂存归档，支持清理失败与发布异常时的无部分集合回滚。
class RunArtifactBatch:
    # LLM: 构造只保存目标和配额，创建/读写在显式stage/publish里完成。
    # 函数用途: 冻结本次归档的目录及私有暂存名称。
    def __init__(self, target, quota):
        self.target, self.quota = target, quota
        self.parent_parts = ('data', 'workflow_runs', target.workflow_run_id, 'artifacts', target.step_id)
        self.staging_name = '.staging-' + uuid.uuid4().hex
        self.identity, self.artifacts, self.published = None, (), False

    # LLM: 所有目录打开都锚定Owner并strict nofollow；调用方负责关闭返回FD。
    # 函数用途: 打开本attempt的可信父目录。
    def _parent(self, *, create=False):
        return open_directory_beneath(self.target.owner_home, self.parent_parts, create=create, require_dir_fd=True)

    # LLM: 只操作本次inode；未确认final先原子撤回私有staging，再递归删除，清理失败不能留下半套final。
    # 函数用途: 撤回未确认发布并删除本次暂存，绝不删除已有attempt或其它运行。
    def _remove_owned(self):
        if self.identity is None or self.published:
            return
        parent = self._parent()
        try:
            for name in (self.target.attempt_id, self.staging_name):
                try:
                    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if (info.st_dev, info.st_ino) != self.identity:
                    if name == self.staging_name:
                        raise RunOutError('SCRIPT_RUN_OUT_HARVEST_FAILED')
                    continue
                if name == self.target.attempt_id:
                    try:
                        os.stat(self.staging_name, dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise RunOutError('SCRIPT_RUN_OUT_HARVEST_FAILED')
                    os.rename(name, self.staging_name, src_dir_fd=parent, dst_dir_fd=parent)
                    os.fsync(parent)
                    continue
                shutil.rmtree(self.staging_name, dir_fd=parent)
            os.fsync(parent)
        finally:
            os.close(parent)

    # LLM: 回滚是尽力且有界的物理清理；失败只加注首因，不用清理异常覆盖原错误或漏掉稳定码。
    # 函数用途: 统一回滚失败出口，私有staging残留交既有后续核对处理。
    def _rollback(self, error):
        try:
            self._remove_owned()
        except (OSError, ValueError, TypeError, AttributeError, RuntimeError) as cleanup_error:
            error.add_note('归档回滚未完成: ' + type(cleanup_error).__name__)
        return _run_out_error(error)

    # LLM: quota检查每个实际复制字节；源文件尺寸变化失败关闭，读取与写入均不跟随链接。
    # 函数用途: 在scratch消失前将全部有界产物写入私有暂存目录。
    def stage(self, scratch, *, max_files, max_bytes):
        try:
            if Path(self.quota.owner_root).resolve(strict=False) != self.target.owner_home.resolve(strict=False):
                raise RunOutError('SCRIPT_RUN_OUT_HARVEST_FAILED')
            manifest = _manifest(scratch, max_files, max_bytes)
            if not manifest:
                return False
            with self.quota.admission() as admission:
                try:
                    parent = self._parent(create=True)
                    try:
                        os.mkdir(self.staging_name, mode=0o700, dir_fd=parent)
                        info = os.stat(self.staging_name, dir_fd=parent, follow_symlinks=False)
                        self.identity = (info.st_dev, info.st_ino)
                    finally:
                        os.close(parent)
                    admission.check(tuple(OwnerQuotaChange(
                        self.target.owner_home.joinpath(*self.parent_parts, self.staging_name, *parts[1:]), size)
                        for parts, size in manifest))
                    artifacts = []
                    for parts, size in manifest:
                        data = read_bytes_beneath(scratch, parts, max_bytes=size, require_dir_fd=True)
                        if data is None or len(data) != size:
                            raise RunOutError('SCRIPT_RUN_OUT_HARVEST_FAILED')
                        relative = parts[1:]
                        write_bytes_atomic_beneath(self.target.owner_home,
                            (*self.parent_parts, self.staging_name, *relative), data)
                        relative_path = '/'.join(relative)
                        artifacts.append({'path': relative_path, 'size_bytes': len(data),
                                          'sha256': hashlib.sha256(data).hexdigest(),
                                          'artifact_id': workflow_artifact_id(self.target.workflow_run_id,
                                                                              self.target.attempt_id, relative_path)})
                    self.artifacts = tuple(artifacts)
                except BaseException as error:
                    self._rollback(error)
                    raise
            return True
        except (OSError, ValueError, TypeError, AttributeError, RuntimeError) as error:
            raise self._rollback(error) from error

    # LLM: 原子rename只在进程/数据源清理成功后执行；迁移同时申报暂存删除与正式增加，不能漏记或双计。
    # 函数用途: 发布完整文件集合，任何普通失败都移除尚未确认的结果。
    def publish(self):
        try:
            with self.quota.admission() as admission:
                try:
                    changes = tuple(change for item in self.artifacts for change in (
                        OwnerQuotaChange(self.target.owner_home.joinpath(*self.parent_parts, self.staging_name, item['path']), None),
                        OwnerQuotaChange(self.target.owner_home.joinpath(*self.parent_parts, self.target.attempt_id, item['path']), item['size_bytes']),
                    ))
                    admission.check(changes)
                    parent = self._parent()
                    try:
                        info = os.stat(self.staging_name, dir_fd=parent, follow_symlinks=False)
                        if (info.st_dev, info.st_ino) != self.identity or not stat.S_ISDIR(info.st_mode):
                            raise RunOutError('SCRIPT_RUN_OUT_HARVEST_FAILED')
                        try:
                            os.stat(self.target.attempt_id, dir_fd=parent, follow_symlinks=False)
                        except FileNotFoundError:
                            pass
                        else:
                            raise RunOutError('SCRIPT_RUN_OUT_HARVEST_FAILED')
                        os.rename(self.staging_name, self.target.attempt_id, src_dir_fd=parent, dst_dir_fd=parent)
                        os.fsync(parent)
                    finally:
                        os.close(parent)
                except BaseException as error:
                    self._rollback(error)
                    raise
            self.published = True
            return str(self.target.owner_home.joinpath(*self.parent_parts, self.target.attempt_id)), self.artifacts
        except (OSError, ValueError, TypeError, AttributeError, RuntimeError) as error:
            raise self._rollback(error) from error

    # LLM: 不确认发布的暂存必须删除；quota不可用也尝试物理清理，失败会显式传播而不伪报成功。
    # 函数用途: 在脚本清理或后续终结失败时丢弃本次暂存集合。
    def discard(self):
        if self.identity is None or self.published:
            return
        try:
            with self.quota.admission() as admission:
                try:
                    admission.check(tuple(OwnerQuotaChange(self.target.owner_home.joinpath(
                        *self.parent_parts, self.staging_name, item['path']), None) for item in self.artifacts))
                finally:
                    self._remove_owned()
        except (OSError, ValueError, TypeError, AttributeError, RuntimeError) as error:
            raise self._rollback(error) from error
