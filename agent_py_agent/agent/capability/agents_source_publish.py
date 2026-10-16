# LLM: 默认来源的正文仅在私有准备件写入；发布只用link的排他语义，不截断任何公开文件。
# 模块用途: 把完整正文一次发布到缺失的默认来源，并保留可验证的回滚FD。
import os
import secrets
from contextlib import contextmanager
from dataclasses import dataclass

from ..common.nofollow_fs import NoFollowPathError, _openat_directory, _openat_file
from ._agents_contract_fields import AgentsContractError
from .agents_source_files import _write_all


# LLM: 只持有本事务的私有准备FD和发布责任；公开叶子的身份核验及回滚必须同步检查来源发布竞争测试。
# 类用途: 保存完整默认来源的排他发布材料，供成功收尾或失败条件撤回。
@dataclass
class PreparedDefaultSource:
    parent: int
    control: int
    temporary: str
    descriptor: int
    published: bool = False

    # LLM: link拒绝所有已存在目的名；成功后先记录责任，后续I/O失败也不能遗忘已发布文件。
    # 函数用途: 完整正文公开后不再写其FD，避免覆盖并发外部编辑。
    def publish(self, name: str) -> None:
        os.link(self.temporary, name, src_dir_fd=self.control,
                dst_dir_fd=self.parent, follow_symlinks=False)
        self.published = True
        self.release_private_link()
        os.fsync(self.parent)

    # LLM: 仅释放本次私有准备链接；发布中途失败时restore先重试这里，公开叶子的单链接校验保持严格。
    # 函数用途: 结束事务自己的双链接中间态，让后续回滚可以核验公开来源。
    def release_private_link(self) -> None:
        if self.temporary:
            os.unlink(self.temporary, dir_fd=self.control)
            self.temporary = ""
            os.fsync(self.control)


# LLM: 调用方已持配置锁和父目录FD；准备阶段对外没有空文件，正文fsync后才交回发布权。
# 函数用途: 管理私有临时文件，不把准备失败误认成已创建公开来源。
@contextmanager
def prepared_default_source(parent: int, body: bytes):
    control = descriptor = None
    prepared = None
    temporary = ".agents-create-" + secrets.token_hex(12)
    try:
        control = _openat_directory(parent, "persona")
        descriptor = _openat_file(control, temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        prepared = PreparedDefaultSource(parent, control, temporary, descriptor)
        _write_all(descriptor, body)
        os.fsync(descriptor)
        yield prepared
    except (OSError, NoFollowPathError) as error:
        raise AgentsContractError("AGENTS_SOURCE_UNSAFE_PATH", "locator") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        _close_prepared_control(control, prepared)


# LLM: 仅清理本次随机命名的私有准备件；无论unlink是否失败都关闭目录FD，不触碰公开叶子。
# 函数用途: 释放准备目录及未发布临时文件，保持异常路径资源收尾。
def _close_prepared_control(control, prepared):
    if control is None:
        return
    try:
        if prepared is not None and prepared.temporary:
            os.unlink(prepared.temporary, dir_fd=control)
    finally:
        os.close(control)
