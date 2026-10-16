# LLM: 唯一repository的本地写辅助；可选回调只包有界同FD事务，不签授权或另建配置。
# 模块用途: 拆平同FD本地写回，不改变CAS/摘要/权限/硬帽检查顺序。
from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

from ._agents_contract_fields import AgentsContractError
from .agents_source_contract import validate_source_snapshot
from .agents_source_files import (
    AGENTS_SOURCE_MAX_BYTES,
    read_descriptor,
    read_source_bytes,
    replace_descriptor_text,
    source_descriptor,
)
from .agents_source_limits import enforce_agents_write_limit


# LLM: 原A配置锁内只运输同FD事务到已绑定file commit；URL拒写、CAS/SHA/身份/许可与补偿均在真实提交内。
# 函数用途: 在已持配置锁时写当前安全本地文件。
def write_text_locked(repository, content: str, context):
    return repository.commit_file(lambda: _write_text_locked(repository, content, context))


# LLM: 调用方已持来源事务；保持同FD/CAS/前像复核与补偿顺序，不在文件提交内等待网络或资源退役。
# 函数用途: 在真正文件提交阶段重新检查授权绑定的正文。
def _write_text_locked(repository, content, context):
    current, record = repository._configuration_record()
    validate_source_snapshot(current, repository.owner, context.target_cas)
    if current.kind == "url":
        raise AgentsContractError("AGENTS_WRITE_SOURCE_READ_ONLY", "kind")
    repository.require_permission(current)
    path = Path(current.locator)
    body = content.encode("utf-8")
    with source_descriptor(path, writable=True) as (descriptor, identity):
        repository._check_identity(record, identity)
        previous = read_descriptor(descriptor, AGENTS_SOURCE_MAX_BYTES)
        _require_previous_digest(previous, context)
        enforce_agents_write_limit(repository._decode(previous), content, repository.prompt_max_chars)
        repository._scan(content)
        _, actual_identity = read_source_bytes(path)
        if identity != actual_identity:
            raise AgentsContractError("AGENTS_SOURCE_IDENTITY_CHANGED", "locator")
        repository.require_permission(current)
        validate_source_snapshot(repository.configuration(), repository.owner, context.target_cas)
        _require_previous_digest(read_descriptor(descriptor, AGENTS_SOURCE_MAX_BYTES), context)
        replace_descriptor_text(descriptor, body, previous)
    return repository._ready(current, body)


# LLM: 摘要只取已核验FD的实际前像，不信模型自报正文或只检查长度。
# 函数用途: 拒绝授权之后发生的内容改变。
def _require_previous_digest(previous: bytes, context) -> None:
    if sha256(previous).hexdigest() != context.content_sha256:
        raise AgentsContractError("AGENTS_SOURCE_CONTENT_MISMATCH", "content_sha256")


# LLM: 普通许可之后加来源专用禁区；Full Access不等于跨owner或宿主控制件许可，不读任何正文。
# 函数用途: 在唯一repository的当前策略上执行本地来源路径裁决。
def require_local_permission(repository, path: Path) -> None:
    from ..path_access_policy import _host_write_block, owner_home_containing
    from .agents_source_contract import _validate_locator
    from .agents_source_repository import _CONTROL_ROOTS, _SECRET_NAMES, _SECRET_PARTS
    _validate_locator("local_path", str(path))
    decision = repository.path_policy.check(path)
    if not decision.allowed:
        raise AgentsContractError("AGENTS_SOURCE_PERMISSION_REVOKED", "locator")
    root = repository.path_policy.agent_home_root
    if root is not None:
        # 共享宿主配置/状态权威只做拒绝裁决；不能套check_write把受控AGENTS自身误拒。
        if _host_write_block(path, root) is not None:
            raise AgentsContractError("AGENTS_SOURCE_CONTROL_PATH_FORBIDDEN", "locator")
        owner_home = owner_home_containing(path, root)
        if owner_home is not None and owner_home != repository.owner_home:
            raise AgentsContractError("AGENTS_SOURCE_CROSS_OWNER_FORBIDDEN", "locator")
        if path.is_relative_to(root) and not path.is_relative_to(repository.owner_home):
            raise AgentsContractError("AGENTS_SOURCE_CONTROL_PATH_FORBIDDEN", "locator")
    parts = tuple(part.casefold() for part in path.parts)
    if any(part in _SECRET_PARTS for part in parts) or path.name.casefold() in _SECRET_NAMES:
        raise AgentsContractError("AGENTS_SOURCE_CONTROL_PATH_FORBIDDEN", "locator")
    if path.is_relative_to(repository.owner_home):
        relative = path.relative_to(repository.owner_home)
        if relative.parts and relative.parts[0].casefold() in _CONTROL_ROOTS:
            raise AgentsContractError("AGENTS_SOURCE_CONTROL_PATH_FORBIDDEN", "locator")
        if relative.as_posix().casefold() in {"soul.md", "user.md", "memory.md", "memory-hot.md"}:
            raise AgentsContractError("AGENTS_SOURCE_CONTROL_PATH_FORBIDDEN", "locator")


# LLM: 旧Persona宿主授权已经核验；来源锁覆盖提交及回滚，交回带restore的对象，禁止调用方裸unlink。
# 函数用途: 给旧默认人格事务提供保持inode的受控正文写入器。
@contextmanager
def default_persona_writer(repository, path: Path, previous: str):
    from .agents_source_files import source_configuration_lock
    with source_configuration_lock(repository.owner_home), source_descriptor(path, writable=True) as opened:
        current, record = repository._configuration_record()
        if current.kind != "owner_default" or current.locator != str(path):
            raise AgentsContractError("AGENTS_SOURCE_CONFIG_CONFLICT", "kind")
        repository.require_permission(current)
        repository._check_identity(record, opened[1])
        raw = read_descriptor(opened[0], AGENTS_SOURCE_MAX_BYTES)
        # 旧Persona的Path.read_text按通用换行读取；只比较相同口径，实际提交/恢复仍走这个FD。
        if raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n") != previous:
            raise AgentsContractError("AGENTS_SOURCE_CONTENT_MISMATCH", "content_sha256")
        yield _DefaultPersonaWriter(repository, path, opened, current, raw, previous)


# LLM: 仅持锁事务内的预期正文版本，不是第二来源/授权权威；成功写入才推进前像，恢复不得覆盖外部编辑。
# 类用途: 记住这个Persona事务实际写出的版本，用同FD做提交和有条件回滚。
@dataclass
class _DefaultPersonaWriter:
    repository: object
    path: Path
    opened: tuple
    snapshot: object
    original: bytes
    previous: str
    expected: bytes = field(init=False)
    written: bool = False

    # LLM: 预期前像必须是context已核验FD实际字节，不能从清洗后的Persona文本重建。
    # 函数用途: 保存事务第一次读取的原始正文，包括BOM和换行。
    def __post_init__(self):
        self.expected = self.original

    # LLM: callable接口只代理原同FD写入，不另外发行写授权。
    # 函数用途: 兼容Persona事务的正文提交调用。
    def __call__(self, text: str) -> None:
        self.write(text)

    # LLM: 恢复沿用本事务实际提交状态；未写入时不改外部正文。
    # 函数用途: 让Persona恢复统一走同FD前像协议而不是裸unlink。
    def restore(self) -> None:
        self.write(self.previous)

    # LLM: 保持本事务FD不变；来源权限/CAS和字节在账本暂存完成后再次核验。
    # 函数用途: 拒绝把已被外部修改的默认正文登记为刚写入的版本。
    def verify(self):
        current = self.repository.configuration()
        validate_source_snapshot(current, self.repository.owner, self.snapshot.configuration_cas)
        self.repository.require_permission(current)
        raw, _ = read_source_bytes(self.path, expected_identity=self.opened[1])
        if raw != self.expected:
            raise AgentsContractError("AGENTS_SOURCE_CONTENT_MISMATCH", "content_sha256")

    # LLM: 最终同FD写前复核配置CAS/权限/身份/正文；恢复只针对本事务成功写出的版本，冲突不覆盖用户编辑。
    # 函数用途: 安全提交正文，或者在仍持有自己的版本时恢复原字节。
    def write(self, text: str) -> None:
        restoring = text == self.previous
        if restoring and not self.written:
            return  # 正文尚未由本事务提交，不能把外部编辑当成待恢复材料。
        current = self.repository.configuration()
        validate_source_snapshot(current, self.repository.owner, self.snapshot.configuration_cas)
        self.repository.require_permission(current)
        read_source_bytes(self.path, expected_identity=self.opened[1])
        previous = read_descriptor(self.opened[0], AGENTS_SOURCE_MAX_BYTES)
        if previous != self.expected:
            raise AgentsContractError("AGENTS_SOURCE_CONTENT_MISMATCH", "content_sha256")
        body = self.original if restoring else text.encode("utf-8")
        replace_descriptor_text(self.opened[0], body, previous)
        self.expected, self.written = body, not restoring
