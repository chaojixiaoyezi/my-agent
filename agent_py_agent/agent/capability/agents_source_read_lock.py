# LLM: 本地来源读边界复用唯一owner锁；只读不得创建sidecar，修改需核读写并发与no-follow测试。
# 模块用途: 协调同进程来源读写并保留无副作用读取。
"""Cooperative local source read/write boundary without creating read-side files."""
import os
import threading
from contextlib import contextmanager

from ..common.nofollow_fs import NoFollowPathError, open_directory_beneath
from ._agents_contract_fields import AgentsContractError

_HELD = threading.local()


# LLM: owner目录本身为无副作用同步锚点；来源写者在旧配置锁外持EX，读者持SH，网络抓取不得持锁。
# 函数用途: 让无配置默认读者和已有来源写者共享边界，同时保留默认只读不创建控制件的合同。
@contextmanager
def source_read_boundary(home, *, writing=False):
    descriptor = None
    try:
        descriptor = open_directory_beneath(home.anchor, home.parts[1:], require_dir_fd=True)
        with _held_home_boundary(home, descriptor, writing):
            yield
    except (ImportError, OSError, NoFollowPathError) as error:
        raise AgentsContractError('AGENTS_SOURCE_UNSAFE_PATH', 'configuration') from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


# LLM: 外层拥有FD关闭；重入仅复用同线程同进程锁，拒绝SH升级EX，首次持锁前后复核目录身份。
# 函数用途: 管理目录锁与线程重入状态，不创建任何锁文件。
@contextmanager
def _held_home_boundary(home, descriptor, writing):
    import fcntl
    info = os.fstat(descriptor)
    key = (os.getpid(), info.st_dev, info.st_ino)
    held = _thread_holds()
    if key in held:
        if writing and not held[key]:
            raise AgentsContractError('AGENTS_SOURCE_LOCK_UPGRADE', 'configuration')
        yield
        return
    fcntl.flock(descriptor, fcntl.LOCK_EX if writing else fcntl.LOCK_SH)
    _verify_home(home, info)
    held[key] = writing
    try:
        yield
        _verify_home(home, info)
    finally:
        del held[key]


# LLM: 仅复用本进程本线程已持锁，不将线程状态当跨进程权限；key包含pid以拒绝fork继承误认。
# 函数用途: 防止读者回调在自己已有的EX锁上再次flock自锁。
def _thread_holds():
    if not hasattr(_HELD, 'locks'):
        _HELD.locks = {}
    return _HELD.locks


# LLM: 等锁期间或业务执行中目录被替换时不能把旧目录锁当作新目录保护。
# 函数用途: 复核仍在使用原owner目录，而不是同名替换对象。
def _verify_home(home, expected):
    current = open_directory_beneath(home.anchor, home.parts[1:], require_dir_fd=True)
    try:
        actual = os.fstat(current)
        if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
            raise AgentsContractError('AGENTS_SOURCE_IDENTITY_CHANGED', 'configuration')
    finally:
        os.close(current)
