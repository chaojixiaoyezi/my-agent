# LLM: 同原正文FD/字节与排他发布算法；C固定叶提交，prepared单独词法收尾，native不注入原source_descriptor或继续恢复写。
# 模块用途: 按原身份写正文，失败保恢复责任并关闭原FD。
"""Identity-owned ordinary Persona writes; no authority or source configuration."""
import os
import sys
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass

from ..common.nofollow_fs import open_directory_beneath
from .agents_source_files import (
    AGENTS_SOURCE_MAX_BYTES,
    read_descriptor,
    replace_descriptor_text,
    source_descriptor,
)
from .agents_source_publish import prepared_default_source
from .agents_source_rollback import withdraw_created_source
from .persona_storage_io import _PERSONA_NATIVE_ERRORS, persona_io


# LLM: 同原正文FD/字节与排他发布算法；C固定叶提交，prepared单独词法收尾，native不注入原source_descriptor或继续恢复写。
# 函数用途: 按原身份写正文，失败保恢复责任并关闭原FD。
@contextmanager
def persona_body_writer(path, previous, existed, *, coordinator_io=False):
    stack = ExitStack()
    prepared_stack = ExitStack() if coordinator_io else stack
    primary = None
    try:
        parent = open_directory_beneath(path.anchor, path.parts[1:-1], require_dir_fd=True)
        stack.callback(os.close, parent)
        opened = persona_io(lambda: stack.enter_context(source_descriptor(path, writable=True, expected_parent=parent)), coordinator_io=coordinator_io) if existed else None
        original = read_descriptor(opened[0], AGENTS_SOURCE_MAX_BYTES) if opened else b''
        if original.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n') != previous:
            raise OSError('PERSONA_BODY_CHANGED')
        writer = _BodyWriter(path, parent, prepared_stack, opened, original, **({} if not coordinator_io else {"coordinator_io": True}))
        yield writer
    except BaseException as error:
        primary = error
        raise
    finally:
        # 只收尾资源，不把无关的Persona账本异常注入来源上下文并改写成来源错误。
        if coordinator_io:
            _close_persona_writer(prepared_stack, stack, primary)
        else:
            stack.close()


# LLM: 同原正文FD/字节与排他发布算法；C固定叶提交，prepared单独词法收尾，native不注入原source_descriptor或继续恢复写。
# 类用途: 按原身份写正文，失败保恢复责任并关闭原FD。
@dataclass
class _BodyWriter:
    path: object
    parent: int
    stack: ExitStack
    opened: tuple | None
    original: bytes
    expected: bytes = b''
    prepared: object = None
    written: bool = False
    coordinator_io: bool = False

    # LLM: 现有FD保留原始换行，首次创建没有前像。
    # 函数用途: 初始化当前预期正文。
    def __post_init__(self):
        self.expected = self.original

    # LLM: 同原正文FD/字节与排他发布算法；C固定叶提交，prepared单独词法收尾，native不注入原source_descriptor或继续恢复写。
    # 函数用途: 按原身份写正文，失败保恢复责任并关闭原FD。
    def __call__(self, text):
        body = text.encode('utf-8')
        if self.coordinator_io and self.opened is not None:
            return persona_io(lambda: self._write_existing(body), coordinator_io=True)
        if self.opened is None:
            self._create(body)
        else:
            self.verify()
            previous = self.expected
            self.expected, self.written = body, True
            replace_descriptor_text(self.opened[0], body, previous)
        self.verify()

    # LLM: 同原正文FD/字节与排他发布算法；C固定叶提交，prepared单独词法收尾，native不注入原source_descriptor或继续恢复写。
    # 函数用途: 按原身份写正文，失败保恢复责任并关闭原FD。
    def _write_existing(self, body):
        self.verify()
        previous = self.expected
        self.expected, self.written = body, True
        replace_descriptor_text(self.opened[0], body, previous)
        self.verify()

    # LLM: 同原正文FD/字节与排他发布算法；C固定叶提交，prepared单独词法收尾，native不注入原source_descriptor或继续恢复写。
    # 函数用途: 按原身份写正文，失败保恢复责任并关闭原FD。
    def _create(self, body):
        try:
            persona_io(lambda: os.mkdir('persona', mode=0o700, dir_fd=self.parent), coordinator_io=self.coordinator_io)
        except FileExistsError:
            pass
        self.expected = body
        self.prepared = self.stack.enter_context(prepared_default_source(self.parent, body, **({} if not self.coordinator_io else {"coordinator_io": True})))
        self.prepared.publish(self.path.name)
        self.written = True

    # LLM: 比较当前无跟随打开路径与仍持有的FD；字节相同不能代替inode身份。
    # 函数用途: 在写后及账本提交前拒绝外部换身或改写。
    def verify(self):
        owned = self.prepared.descriptor if self.prepared is not None else self.opened[0]
        with source_descriptor(self.path, expected_parent=self.parent) as (descriptor, _identity):
            before, current = os.fstat(owned), os.fstat(descriptor)
            if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
                raise OSError('PERSONA_BODY_IDENTITY_CHANGED')
            if read_descriptor(descriptor, AGENTS_SOURCE_MAX_BYTES) != self.expected:
                raise OSError('PERSONA_BODY_CHANGED')

    # LLM: 同原正文FD/字节与排他发布算法；C固定叶提交，prepared单独词法收尾，native不注入原source_descriptor或继续恢复写。
    # 函数用途: 按原身份写正文，失败保恢复责任并关闭原FD。
    def restore(self):
        if self.coordinator_io:
            return persona_io(lambda: self._restore_current(), coordinator_io=True)
        self._restore_current()

    # LLM: 同原正文FD/字节与排他发布算法；C固定叶提交，prepared单独词法收尾，native不注入原source_descriptor或继续恢复写。
    # 函数用途: 按原身份写正文，失败保恢复责任并关闭原FD。
    def _restore_current(self):
        if self.prepared is not None and self.prepared.published:
            self.prepared.release_private_link()
            self.verify()
            withdraw_created_source(self.parent, self.path.name, self.prepared.descriptor, self.expected)
        elif self.written:
            self.verify()
            replace_descriptor_text(self.opened[0], self.original, self.expected)
            self.expected, self.written = self.original, False


# LLM: 仅本Persona正文事务的两原stack；native只交prepared自己的退出，原source_descriptor仍close，不注入账本错误。
# 函数用途: 精确收回已持FD，失源保私有材料与本次真正primary，不新增统一清理注册器。
def _close_persona_writer(prepared_stack, stack, primary):
    try:
        if isinstance(primary, _PERSONA_NATIVE_ERRORS):
            prepared_stack.__exit__(type(primary), primary, primary.__traceback__)
        else:
            prepared_stack.close()
    finally:
        active = sys.exception() or primary
        try:
            stack.close()
        except OSError:
            if not isinstance(active, _PERSONA_NATIVE_ERRORS):
                raise
