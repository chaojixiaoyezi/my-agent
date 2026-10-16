# LLM: 仅旧Persona默认来源首次创建；配置仍以唯一来源repository为准，不另发授权。
# 模块用途: 防止默认文件缺失时绕过来源CAS、覆盖用户抢先创建的文件或误删外部正文。
import os
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..common.nofollow_fs import open_directory_beneath
from ._agents_contract_fields import AgentsContractError
from .agents_source_contract import validate_source_snapshot
from .agents_source_files import (
    read_source_bytes,
    source_configuration_lock,
    source_descriptor,
)
from .agents_source_limits import enforce_agents_write_limit
from .agents_source_publish import PreparedDefaultSource, prepared_default_source


# LLM: 版本快照前冻结CAS与父目录；只在实际创建至账本提交期间持配置锁，快照回调可合法切源。
# 函数用途: 准备一次缺失默认文件的受控创建及有条件回滚。
@contextmanager
def missing_default_writer(repository, path: Path, previous: str):
    if previous:
        raise AgentsContractError("AGENTS_SOURCE_CONTENT_MISMATCH", "content_sha256")
    snapshot = repository.configuration()
    if snapshot.kind != "owner_default" or snapshot.locator != str(path):
        raise AgentsContractError("AGENTS_SOURCE_CONFIG_CONFLICT", "kind")
    repository.require_permission(snapshot)
    with ExitStack() as stack:
        parent = open_directory_beneath(path.anchor, path.parts[1:-1], require_dir_fd=True)
        stack.callback(os.close, parent)
        yield _MissingDefaultWriter(repository, path, snapshot, stack, parent)


# LLM: 只持本事务FD和预期字节；未创建时恢复为空操作，不根据path.exists认领外部文件。
# 类用途: 延迟到版本快照结束后创建默认文件，保存可验证的回滚责任。
@dataclass
class _MissingDefaultWriter:
    repository: object
    path: Path
    snapshot: object
    stack: ExitStack
    parent: int
    opened: tuple | None = None
    expected: bytes = b""
    prepared: PreparedDefaultSource | None = None

    # LLM: 配置锁持到账本结束；私有准备完整正文，排他发布后不再写公开FD，核对权限期间的外部变动。
    # 函数用途: 在原默认来源仍有效时发布正文，不覆盖公开文件的新编辑。
    def __call__(self, text: str) -> None:
        self.stack.enter_context(source_configuration_lock(self.repository.owner_home))
        current, record = self.repository._configuration_record()
        validate_source_snapshot(current, self.repository.owner, self.snapshot.configuration_cas)
        self.repository.require_permission(current)
        self._check_parent()
        enforce_agents_write_limit("", text, self.repository.prompt_max_chars)
        self.repository._scan(text)
        self.repository.require_permission(current)
        self.expected = text.encode("utf-8")
        self.prepared = self.stack.enter_context(prepared_default_source(self.parent, self.expected))
        self._check_parent()
        self.repository.require_permission(current)
        self.prepared.publish(self.path.name)
        self._verify_published(record)
        self.repository.require_permission(current)
        self._verify_published(record)

    # LLM: 同一父目录和实际准备FD绑定身份；权限回调之后重新核对公开名与字节，过程中没有正文写入。
    # 函数用途: 阻止公开叶子换身或同inode编辑被记成自己的成功提交。
    def _verify_published(self, record) -> None:
        with source_descriptor(self.path, expected_parent=self.parent) as (descriptor, identity):
            before, after = os.fstat(self.prepared.descriptor), os.fstat(descriptor)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise AgentsContractError("AGENTS_SOURCE_IDENTITY_CHANGED", "locator")
            self.opened = (self.prepared.descriptor, identity)
            if record is not None:
                self.repository._check_identity(record, identity)
        raw, _ = read_source_bytes(self.path, expected_identity=identity)
        if raw != self.expected:
            raise AgentsContractError("AGENTS_SOURCE_CONTENT_MISMATCH", "content_sha256")

    # LLM: 配置锁仍覆盖账本准备；重新检查权限/CAS/父目录及创建FD对应正文，不发生写入。
    # 函数用途: 在账本提交前拒绝最后阶段的外部改写或撤权。
    def verify(self):
        current, record = self.repository._configuration_record()
        validate_source_snapshot(current, self.repository.owner, self.snapshot.configuration_cas)
        self.repository.require_permission(current)
        self._check_parent()
        self._verify_published(record)

    # LLM: 新打开父目录只作身份比对；不因路径被替换把已有FD写入另一个目录。
    # 函数用途: 确认版本快照前后默认来源父目录未被替换。
    def _check_parent(self) -> None:
        current = open_directory_beneath(self.path.anchor, self.path.parts[1:-1], require_dir_fd=True)
        try:
            before, after = os.fstat(self.parent), os.fstat(current)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise AgentsContractError("AGENTS_SOURCE_IDENTITY_CHANGED", "locator")
        finally:
            os.close(current)

    # LLM: 发布责任独立于后续读取成功；先完成事务自己的私有链接清理，再按原FD核对，不放宽外部硬链接限制。
    # 函数用途: 即使发布中途或首次核验失败，也不能遗忘本次创建的回滚责任。
    def restore(self) -> None:
        if self.prepared is None or not self.prepared.published:
            return
        self._check_parent()
        self.prepared.release_private_link()
        self._verify_published(None)
        from .agents_source_rollback import withdraw_created_source
        withdraw_created_source(self.parent, self.path.name, self.opened[0], self.expected)
