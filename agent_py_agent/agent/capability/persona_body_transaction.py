# LLM: 正文写入与补偿必须绑定同一FD和发布责任；普通Persona不获得AGENTS来源授权，保留群lease。
# 模块用途: 提交普通人格正文并在失败时只撤回本事务拥有的文件。
"""Identity-owned ordinary Persona writes; no authority or source configuration."""
import os
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


# LLM: 调用方已完成Persona权限、配额和目标锁；这里只持文件身份，不新建来源或授权。
# 函数用途: 为USER/SOUL和无来源服务的AGENTS提供明确创建责任及条件恢复。
@contextmanager
def persona_body_writer(path, previous, existed):
    stack = ExitStack()
    try:
        parent = open_directory_beneath(path.anchor, path.parts[1:-1], require_dir_fd=True)
        stack.callback(os.close, parent)
        opened = stack.enter_context(source_descriptor(path, writable=True, expected_parent=parent)) if existed else None
        original = read_descriptor(opened[0], AGENTS_SOURCE_MAX_BYTES) if opened else b''
        if original.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n') != previous:
            raise OSError('PERSONA_BODY_CHANGED')
        yield _BodyWriter(path, parent, stack, opened, original)
    finally:
        # 只收尾资源，不把无关的Persona账本异常注入来源上下文并改写成来源错误。
        stack.close()


# LLM: expected仅描述本事务FD实际字节；未写入不恢复，路径出现不代表拥有创建权。
# 类用途: 把普通人格写入与后续提交核验、回滚绑定在同一文件身份上。
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

    # LLM: 现有FD保留原始换行，首次创建没有前像。
    # 函数用途: 初始化当前预期正文。
    def __post_init__(self):
        self.expected = self.original

    # LLM: 缺失正文排他发布；现有正文写前检查身份并登记恢复责任，部分失败不能遗忘已写字节。
    # 函数用途: 提交一次普通人格正文，不收养外部抢先创建者。
    def __call__(self, text):
        body = text.encode('utf-8')
        if self.opened is None:
            self._create(body)
        else:
            self.verify()
            previous = self.expected
            self.expected, self.written = body, True
            replace_descriptor_text(self.opened[0], body, previous)
        self.verify()

    # LLM: 与来源创建共用完整准备/排他link原语，不复用来源授权；发布责任由prepared立即记录。
    # 函数用途: 完整正文刷盘后再公开，避免暴露空文件或丢失部分发布责任。
    def _create(self, body):
        try:
            os.mkdir('persona', mode=0o700, dir_fd=self.parent)
        except FileExistsError:
            pass
        self.expected = body
        self.prepared = self.stack.enter_context(prepared_default_source(self.parent, body))
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

    # LLM: 未发布则空操作；冲突拒绝补偿，创建件先捕获再校验且保留恢复材料。
    # 函数用途: 只撤回本次写出的版本，不删除外部文件。
    def restore(self):
        if self.prepared is not None and self.prepared.published:
            self.prepared.release_private_link()
            self.verify()
            withdraw_created_source(self.parent, self.path.name, self.prepared.descriptor, self.expected)
        elif self.written:
            self.verify()
            replace_descriptor_text(self.opened[0], self.original, self.expected)
            self.expected, self.written = self.original, False
