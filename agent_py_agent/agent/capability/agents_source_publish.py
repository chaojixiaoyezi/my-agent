# LLM: 默认来源算法共用，C只门控准备/发布/私有清理，不跨yield持Root；native primary时只关准确FD，不改公开对象或重包异常。
# 模块用途: 准备完整正文再排他发布，区分私有清理权与FD回收。
import os
import secrets
import sys
from contextlib import contextmanager
from dataclasses import dataclass

from ..common.nofollow_fs import NoFollowPathError, _openat_directory, _openat_file
from ._agents_contract_fields import AgentsContractError
from .agents_source_files import _write_all
from .persona_storage_io import _PERSONA_NATIVE_ERRORS, persona_io


# LLM: 默认来源算法共用，C只门控准备/发布/私有清理，不跨yield持Root；native primary时只关准确FD，不改公开对象或重包异常。
# 类用途: 准备完整正文再排他发布，区分私有清理权与FD回收。
@dataclass
class PreparedDefaultSource:
    parent: int
    control: int
    temporary: str
    descriptor: int
    published: bool = False
    coordinator_io: bool = False

    # LLM: 默认来源算法共用，C只门控准备/发布/私有清理，不跨yield持Root；native primary时只关准确FD，不改公开对象或重包异常。
    # 函数用途: 准备完整正文再排他发布，区分私有清理权与FD回收。
    def publish(self, name: str) -> None:
        if self.coordinator_io:
            return persona_io(lambda: self._publish_current(name), coordinator_io=True)
        self._publish_current(name)

    # LLM: 默认来源算法共用，C只门控准备/发布/私有清理，不跨yield持Root；native primary时只关准确FD，不改公开对象或重包异常。
    # 函数用途: 准备完整正文再排他发布，区分私有清理权与FD回收。
    def _publish_current(self, name):
        os.link(self.temporary, name, src_dir_fd=self.control,
                dst_dir_fd=self.parent, follow_symlinks=False)
        self.published = True
        self.release_private_link()
        os.fsync(self.parent)

    # LLM: 默认来源算法共用，C只门控准备/发布/私有清理，不跨yield持Root；native primary时只关准确FD，不改公开对象或重包异常。
    # 函数用途: 准备完整正文再排他发布，区分私有清理权与FD回收。
    def release_private_link(self) -> None:
        if self.coordinator_io:
            return persona_io(self._release_current, coordinator_io=True)
        self._release_current()

    # LLM: 默认来源算法共用，C只门控准备/发布/私有清理，不跨yield持Root；native primary时只关准确FD，不改公开对象或重包异常。
    # 函数用途: 准备完整正文再排他发布，区分私有清理权与FD回收。
    def _release_current(self):
        if self.temporary:
            os.unlink(self.temporary, dir_fd=self.control)
            self.temporary = ""
            os.fsync(self.control)


# LLM: 默认来源算法共用，C只门控准备/发布/私有清理，不跨yield持Root；native primary时只关准确FD，不改公开对象或重包异常。
# 函数用途: 准备完整正文再排他发布，区分私有清理权与FD回收。
@contextmanager
def prepared_default_source(parent: int, body: bytes, *, coordinator_io=False):
    control = descriptor = None
    prepared = None
    temporary = ".agents-create-" + secrets.token_hex(12)
    try:
        control = _openat_directory(parent, "persona")
        descriptor = persona_io(lambda: _openat_file(control, temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600), coordinator_io=coordinator_io)
        prepared = PreparedDefaultSource(parent, control, temporary, descriptor, **({} if not coordinator_io else {"coordinator_io": True}))
        persona_io(lambda: _write_prepared_body(descriptor, body), coordinator_io=coordinator_io)
        yield prepared
    except (OSError, NoFollowPathError) as error:
        if coordinator_io and isinstance(error, _PERSONA_NATIVE_ERRORS):
            raise
        raise AgentsContractError("AGENTS_SOURCE_UNSAFE_PATH", "locator") from error
    finally:
        if coordinator_io:
            _finish_prepared_source(descriptor, control, prepared, sys.exception())
        else:
            if descriptor is not None:
                os.close(descriptor)
            _close_prepared_control(control, prepared)


# LLM: 默认来源算法共用，C只门控准备/发布/私有清理，不跨yield持Root；native primary时只关准确FD，不改公开对象或重包异常。
# 函数用途: 准备完整正文再排他发布，区分私有清理权与FD回收。
def _close_prepared_control(control, prepared, *, coordinator_io=False, primary=None):
    if control is None:
        return
    try:
        if coordinator_io and isinstance(primary, _PERSONA_NATIVE_ERRORS):
            return
        if prepared is not None and prepared.temporary:
            persona_io(lambda: os.unlink(prepared.temporary, dir_fd=control), coordinator_io=coordinator_io)
    finally:
        if coordinator_io:
            active = sys.exception() or primary
            try:
                os.close(control)
            except OSError:
                if not isinstance(active, _PERSONA_NATIVE_ERRORS):
                    raise
        else:
            os.close(control)


# LLM: 默认来源算法共用，C只门控准备/发布/私有清理，不跨yield持Root；native primary时只关准确FD，不改公开对象或重包异常。
# 函数用途: 准备完整正文再排他发布，区分私有清理权与FD回收。
def _write_prepared_body(descriptor, body):
    _write_all(descriptor, body)
    os.fsync(descriptor)


# LLM: 只供prepared_default_source的三个原FD/责任引用收尾；本词法native不授清理权，secondary close不覆盖primary。
# 函数用途: 总是尝试关正文及control FD，并按当前来源决定私有准备件是否可清理。
def _finish_prepared_source(descriptor, control, prepared, primary):
    try:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                if not isinstance(primary, _PERSONA_NATIVE_ERRORS):
                    raise
    finally:
        _close_prepared_control(control, prepared, coordinator_io=True, primary=primary)
