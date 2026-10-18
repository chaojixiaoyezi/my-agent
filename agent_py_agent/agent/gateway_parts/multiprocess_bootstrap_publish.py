# LLM: P4沿原registry唯一当前登记；真实C原lease内可归档immutable generation三字段历史metadata，只定位count、不授alive/P2/ready；None原格式路径零extraIO。
# 模块用途: 用原账本规范编码和原子原语发布当前代次、实际端口及实例，完整旧记录防止迟到覆盖。
from pathlib import Path

from ..common.nofollow_fs import read_bytes_beneath, write_bytes_atomic_beneath
from .io import locked_file_transition
from .multiprocess_bootstrap import WorkerBootstrapError
from .multiprocess_executor_ledger import _durable_namespace
from .multiprocess_generation_metadata import (
    archive_registry_generations,
    generation_publication_scope,
    require_generation_source,
)
from .multiprocess_records import (
    MultiprocessRecordError,
    deserialize_record,
    serialize_record,
    validate_record,
)

# registry 只保存身份和账本引用；读写统一限制一 MiB，超量拒绝，不截断或当空登记。
_WORKER_REGISTRY_MAX_BYTES = 1024 * 1024


# LLM: 沿原结构化拒绝信封，不回显来源记录。
# 类用途: 表示 registry 不能确认发布或读回。
class WorkerRegistryError(WorkerBootstrapError):
    error_code = "WORKER_REGISTRY_REJECTED"


# LLM: 缺失才返回 None；损坏、链接或不可读不能当空账。只读原路径，不创建锁/目录。
# 函数用途: 严格读回完整当前登记，保留旧代以供调用方先做接手屏障。
def read_worker_registry(root):
    _require_root(root)
    try:
        content = read_bytes_beneath(root, ("workers", "registry.json"),
                                     max_bytes=_WORKER_REGISTRY_MAX_BYTES, require_dir_fd=True)
        return None if content is None else _registry(deserialize_record(content))
    except MultiprocessRecordError as error:
        raise WorkerRegistryError(error.reason, error.field) from error
    except OSError as error:
        raise WorkerRegistryError("registry_unreadable", "registry") from error


# LLM: 实际C已P2后发布，generation_source是首次捕获原lease并短门末核，expected原实读；None纯record接口不增历史IO或权限，原当前登记不变。
# 函数用途: 校验先于创建，比较旧登记后原子持久化并读回；写后异常不回滚、不宣称未写入。
def publish_worker_registry(root, record, expected, *, generation_source=None):
    _require_root(root)
    source = _registry(record)
    previous = None if expected is None else _registry(expected)
    content = serialize_record(source)
    if len(content) > _WORKER_REGISTRY_MAX_BYTES:
        raise WorkerRegistryError("registry_too_large", "registry")
    try:
        with generation_publication_scope(root, source, generation_source):
            with locked_file_transition(root / "workers" / "registry.json"):
                if generation_source is None:
                    return _publish_locked(root, (source, content), previous)
                return _publish_locked(root, (source, content), previous, generation_source)
    except OSError as error:
        raise WorkerRegistryError("registry_io_unconfirmed", "registry") from error
    except WorkerRegistryError:
        raise
    except ValueError as error:
        raise WorkerRegistryError("generation_metadata_unconfirmed", "c_run_id") from error


# LLM: 原资源锁内expected实读相等才归档P0历史count；actualsource短门末核后仍只原registry作为当前登记，不造第二执行账。
# 函数用途: 在原路径锁内比较、刷盘并严格读回，确认前不返回成功。
def _publish_locked(root, candidate, expected, generation_source=None):
    source, content = candidate
    raw_previous = None
    if generation_source is not None:
        raw_previous = read_bytes_beneath(root, ("workers", "registry.json"),
                                         max_bytes=_WORKER_REGISTRY_MAX_BYTES, require_dir_fd=True)
    actual_previous = read_worker_registry(root)
    if actual_previous != expected:
        raise WorkerRegistryError("registry_compare_conflict", "registry")
    if generation_source is not None:
        if read_bytes_beneath(root, ("workers", "registry.json"),
                              max_bytes=_WORKER_REGISTRY_MAX_BYTES, require_dir_fd=True) != raw_previous:
            raise WorkerRegistryError("registry_readback_conflict", "registry")
        archive_registry_generations(root, actual_previous, source, generation_source)
        require_generation_source(root, source, generation_source)
        if read_bytes_beneath(root, ("workers", "registry.json"),
                              max_bytes=_WORKER_REGISTRY_MAX_BYTES, require_dir_fd=True) != raw_previous:
            raise WorkerRegistryError("registry_readback_conflict", "registry")
    _durable_namespace(root, ("workers",))
    write_bytes_atomic_beneath(root, ("workers", "registry.json"), content)
    actual = read_worker_registry(root)
    if actual != source:
        raise WorkerRegistryError("registry_readback_conflict", "registry")
    return actual


# LLM: 由原 P0 唯一入口验证，其他合法记录不能冒充 registry；不接受输入默认补齐。
# 函数用途: 返回独立的 registry 副本，并将错误转换为本合同拒绝码。
def _registry(record):
    try:
        source = validate_record(record)
    except MultiprocessRecordError as error:
        raise WorkerRegistryError(error.reason, error.field) from error
    if source["schema_version"] != "multiprocess_worker_registry.v1":
        raise WorkerRegistryError("wrong_record_kind", "schema_version")
    return source


# LLM: 根来自 C 已冻结的 GatewayPaths，不 resolve 或从当前配置找替代根；错误先于一切 I/O。
# 函数用途: 拒绝相对根或含上跳的路径。
def _require_root(root):
    if not isinstance(root, Path) or not root.is_absolute() or ".." in root.parts:
        raise WorkerRegistryError("registry_root_invalid", "gateway_root")
