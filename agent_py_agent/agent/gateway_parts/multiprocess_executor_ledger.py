# LLM: P1a 唯一权威为 workers/executors/w<index>/<sha256(reservation_id)>.json；沿 P0 v1/FSM。
#   构造无 I/O，修改持私有分区锁；文件与目录 fsync，未知/旧代记录不能被覆盖或删掉。
# 模块用途: 保存分区执行者预留，不创建第二个账本或复制命令正文。
import hashlib
import os
from contextlib import contextmanager
from pathlib import Path

from ..common.nofollow_fs import (
    open_directory_beneath,
    open_private_lock_beneath,
    read_bytes_beneath,
    write_bytes_atomic_beneath,
)
from .multiprocess_records import (
    MultiprocessRecordError,
    advance_executor,
    deserialize_record,
    serialize_record,
    validate_record,
)

try:
    import fcntl
except ImportError:
    fcntl = None

# v1 只有固定身份与状态，16 KiB 足够完整记录；超量拒绝，不截断或当空账。
_EXECUTOR_RECORD_MAX_BYTES = 16384


# LLM: 构造只冻结 C 给的根和分区，不允许初始化产生 I/O。
# 类用途: 在同一分区的规范路径读写执行者记录。
class ExecutorLedger:
    # LLM: 本接口由内部组合根提供，不解析环境或用户配置。
    # 函数用途: 保存受信根与固定分区。
    def __init__(self, root: Path, partition) -> None:
        self.root, self.partition = root, partition

    # LLM: 校验先于一切 I/O；相同 ID 的既有事实也不能重新启动，私有锁内排他预留。
    # 函数用途: 原子刷盘 reserved，完成之后调用方才可创建垫片。
    def reserve(self, record: dict):
        source = self._bound_record(record)
        if source["state"] != "reserved":
            raise MultiprocessRecordError("initial_state_not_reserved", "state")
        with self._locked():
            if self._read(source["reservation_id"]) is not None:
                raise MultiprocessRecordError("reservation_already_exists", "reservation_id")
            self._write(source)
        return source

    # LLM: 纯读取规范文件，保留旧代；不创建目录或将损坏/不可读解释成空账。
    # 函数用途: 给启动读回门及未来接手屏障返回完整记录。
    def read(self, reservation_id: str):
        return self._read(reservation_id)

    # LLM: 同一私有锁比较原完整记录沿P0 FSM；转换结果在任何写前再核原分区及v2域index，不接受陈旧、重绑或错误域槽；同步v1/显式v2 ledger tests。
    # 函数用途: 原子持久化 registered、released 或 abandoned。
    def advance(self, record: dict, state: str, registration: dict | None = None):
        source = self._bound_record(record)
        updated = self._bound_record(advance_executor(source, state, registration))
        with self._locked():
            if self._read(source["reservation_id"]) != source:
                raise MultiprocessRecordError("reservation_compare_conflict", "reservation_id")
            self._write(updated)
        return updated

    # LLM: 登记写可能已经 rename 后才报错，故锁内重读实际状态而非用旧 reserved 覆盖完整身份。
    # 函数用途: 失败后保留原绑定并标 abandoned；写不了时抛错，让调用方明确报告未确认。
    def abandon(self, record: dict):
        source = self._bound_record(record)
        with self._locked():
            actual = self._read(source["reservation_id"])
            if actual is None or _binding(actual) != _binding(source):
                raise MultiprocessRecordError("abandonment_binding_conflict", "reservation_id")
            updated = actual if actual["state"] == "abandoned" else advance_executor(actual, "abandoned")
            self._write(updated)
        return updated

    # LLM: v1/v2仍共用原路径/分区/C代次，v2只追加原内核域事实；不读配置、不迁移旧账、不扩owner权限；同步ledger tests。
    # 函数用途: 在创建目录之前拒绝非法结构、错代或错分区，真正请求权限仍由原入口守门。
    def _bound_record(self, record: dict) -> dict:
        source = validate_record(record)
        partition = self.partition
        valid = (source["schema_version"] in {"multiprocess_executor_reservation.v1", "multiprocess_executor_reservation.v2"}
                 and type(partition.count) is int and type(partition.index) is int
                 and 0 <= partition.index < partition.count and self.root.is_absolute() and fcntl is not None)
        if not valid:
            raise MultiprocessRecordError("executor_partition_invalid")
        if source["c_run_id"] != partition.c_run_id or int(source["owner_sha"], 16) % partition.count != partition.index:
            raise MultiprocessRecordError("executor_partition_conflict")
        domain = source.get('execution_domain')
        if domain is not None and domain['source_worker_index'] != partition.index:
            raise MultiprocessRecordError("executor_domain_partition_conflict")
        return source

    # LLM: v1/v2各自显式解码，ID仅定位规范P1；旧v1仍无域证明，不过滤旧代、不按数字重造namespace；同步ledger/接手读取。
    # 函数用途: 有界 no-follow 读回唯一 v1 文件，拒绝链接、损坏和超量记录。
    def _read(self, reservation_id: str):
        content = read_bytes_beneath(self.root, self._parts(reservation_id),
                                     max_bytes=_EXECUTOR_RECORD_MAX_BYTES, require_dir_fd=True)
        if content is None:
            return None
        record = deserialize_record(content)
        if record["schema_version"] not in {"multiprocess_executor_reservation.v1", "multiprocess_executor_reservation.v2"} or record["reservation_id"] != reservation_id:
            raise MultiprocessRecordError("executor_file_binding_conflict")
        return record

    # LLM: 复用原 no-follow/fsync/原子 rename，不复制 JSON 写法；本方法要求调用方持分区锁。
    # 函数用途: 私有权限写入规范字节，读者只见完整旧记录或完整新记录。
    def _write(self, record: dict) -> None:
        write_bytes_atomic_beneath(self.root, self._parts(record["reservation_id"]), serialize_record(record))

    # LLM: 目录不含代次，旧代事实永久留在同一分区命名空间；锁不是第二个账本。
    # 函数用途: 给读写和未来恢复提供唯一相对位置。
    def _parts(self, reservation_id: str) -> tuple:
        if type(reservation_id) is not str or not reservation_id:
            raise MultiprocessRecordError("reservation_id_required")
        name = hashlib.sha256(reservation_id.encode("utf-8")).hexdigest() + ".json"
        return ("workers", "executors", f"w{self.partition.index}", name)

    # LLM: 内部 POSIX 模式才到这里；逐段 no-follow 建目录并刷父目录，锁仅覆盖短状态转换。
    # 函数用途: 串行化本分区账本写者，不在用户命令执行期间持锁。
    @contextmanager
    def _locked(self):
        namespace = self._parts("lock")[:-1]
        _durable_namespace(self.root, namespace)
        descriptor = open_private_lock_beneath(self.root, (*namespace, "ledger.lock"))
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)


# LLM: schema及原请求归属仍不可变，registration内核域与进程槽仅在原CAS/FSM登记；排除注册槽不许可改schema/别请求，核abandonment tests。
# 函数用途: 比较不可重绑的 v1 身份字段。
def _binding(record: dict) -> dict:
    return {key: value for key, value in record.items() if key not in {"state", "process_identity", "pgid", "sid", "execution_domain"}}


# LLM: 先完整创建目录链再从叶到根刷盘，父目录必须在子目录链接创建之后 fsync。
# 函数用途: 持久化规范路径的每级目录链接，任一错误直接拒绝后续创建进程。
def _durable_namespace(root: Path, parts: tuple) -> None:
    for depth in range(len(parts), -1, -1):
        descriptor = open_directory_beneath(root, parts[:depth], create=depth == len(parts))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
