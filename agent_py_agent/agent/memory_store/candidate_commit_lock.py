# LLM: 提交与同步硬删除共用候选锁所有权；保持原quota→policy→candidate锁序，不另建候选账。
# 模块用途: 协调候选提交和正式删除后的同步清理。
"""候选提交临界区及同步硬删除回调的锁所有权。"""
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import local

from ..common.json_io import locked_json_path

_OWNED = local()


# LLM: 必须真正取得文件锁后登记当前线程的规范路径；异常时清除，不向其他线程继承。
# 函数用途: 持有候选提交锁并标记同步清理回调可以复用它。
@contextmanager
def candidate_commit_lock(path: Path) -> Iterator[None]:
    key = path.resolve()
    with locked_json_path(path):
        paths = getattr(_OWNED, "paths", None)
        if paths is None:
            paths = _OWNED.paths = set()
        paths.add(key)
        try:
            yield
        finally:
            paths.remove(key)


# LLM: 只允许同线程、同规范路径且仍处于真实提交锁内的脱敏回调复用；普通调用照常加锁。
# 函数用途: 避免正式删除回调重复获取不可重入的候选文件锁。
@contextmanager
def candidate_redaction_lock(path: Path) -> Iterator[None]:
    if path.resolve() in getattr(_OWNED, "paths", ()):
        yield
        return
    with locked_json_path(path):
        yield
