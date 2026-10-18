# LLM: 提交与同步硬删除共用候选锁所有权；保持原quota→policy→candidate锁序，不另建候选账。
# LLM: C只透传原scope的准确FD出生，原_OWNED仍唯一锁复用事实；不存source或新token，默认False原路径。
# 模块用途: 协调原候选锁和同步清理，C锁等待仍在Root外。
"""候选提交临界区及同步硬删除回调的锁所有权。"""
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import local

from ..common.global_write_commit import execute_global_write
from ..common.json_io import locked_json_path

_OWNED = local()


# LLM: 必须真正取得文件锁后登记当前线程的规范路径；异常时清除，不向其他线程继承。
# LLM: coordinator_io只让FD出生消费当次原scope，原候选锁跨业务/embedding寿命不缩短。
# 函数用途: 持有原候选提交锁并记录同线程同步清理复用事实。
@contextmanager
def candidate_commit_lock(path: Path, *, coordinator_io: bool = False) -> Iterator[None]:
    key = path.resolve()
    options = {} if not coordinator_io else {"birth_commit": _coordinator_lock_birth}
    with locked_json_path(path, **options):
        paths = getattr(_OWNED, "paths", None)
        if paths is None:
            paths = _OWNED.paths = set()
        paths.add(key)
        try:
            yield
        finally:
            paths.remove(key)


# LLM: 只允许同线程、同规范路径且仍处于真实提交锁内的脱敏回调复用；普通调用照常加锁。
# LLM: 已持原candidate锁才复用，C未持锁仍原双层锁与准确出生门；不另建锁所有权。
# 函数用途: 在原正式删除回调中避免自锁，普通路径仍真实取锁。
@contextmanager
def candidate_redaction_lock(path: Path, *, coordinator_io: bool = False) -> Iterator[None]:
    if path.resolve() in getattr(_OWNED, "paths", ()):
        yield
        return
    options = {} if not coordinator_io else {"birth_commit": _coordinator_lock_birth}
    with locked_json_path(path, **options):
        yield


# LLM: 不保存来源；只在每次准确lock FD出生调用原global scope，缺源沿原typed拒绝。
# 函数用途: 让C候选锁出生接受当前真实末门，等待和身份核验留门外。
def _coordinator_lock_birth(operation):
    return execute_global_write(operation, require_scope=True)
