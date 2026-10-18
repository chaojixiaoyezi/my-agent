# LLM: 旧代缺原count的P1仅能限制启动；来源是原nofollow文件/目录双读，绝不构造ledger、P2范围或死亡/执行权；同步partition/readback tests。
# 模块用途: 保存能够定位但不能证明完整旧范围的原责任，并沿唯一复核调度只读检查它。
import hashlib
import os
import re

from ..common.nofollow_fs import _openat_file, _read_descriptor, open_directory_beneath
from ..user_space.owner_resolver import OwnerIdentity
from .multiprocess_records import deserialize_record
from .multiprocess_route import owner_digest

# P0单条记录的字节上限，与原发现入口一致；超限拒绝而不截断。
_UNRESOLVED_SOURCE_MAX_BYTES = 16384


# LLM: 此对象只作拒绝来源，不能复制成原引用或换内容/桶；字段仍由原P0验证，不增加耐久账或执行许可。
# 类用途: 持有一份原旧代文件的准确限制性读回。
class UnresolvedExecutorSource:
    # LLM: 仅原发现组合根调用；保存实际文件/目录身份和原字节，不从活进程/正文推旧count。
    # 函数用途: 固定未知旧域的物理桶、owner及规范源引用。
    def __init__(self, root, parts, content, file_identity, directory_identity):
        self.root, self.parts, self.content = root, parts, content
        self.file_identity, self.directory_identity = file_identity, directory_identity
        row = deserialize_record(content)
        self.generation, self.owner_sha = row['c_run_id'], row['owner_sha']
        self.index = int(parts[-2][1:])
        self.role = 'C' if self.owner_sha == owner_digest(OwnerIdentity.local_main()) else 'W'
        self._origin, self._creator_pid = self, os.getpid()
        self._facts = (root, parts, content, file_identity, directory_identity,
                       self.generation, self.owner_sha, self.index, self.role)

    # LLM: 复制/重绑/跨进程不能替换原拒绝来源；纯内存核验不授予业务权。
    # 函数用途: 拒绝限制对象换壳，保原字节和实际桶身份。
    def require_original(self):
        if (self._origin is not self or self._creator_pid != os.getpid()
                or self._facts != (self.root, self.parts, self.content, self.file_identity, self.directory_identity,
                                   self.generation, self.owner_sha, self.index, self.role)):
            raise ValueError('unresolved_executor_original_required')

    # LLM: 同原文件和目录身份/完整字节双核；消失、改字节或rename/restore不能解除未知责任。
    # 函数用途: 门外只读复核这份原责任仍未漂移，不探测/停止进程。
    def require_unchanged(self):
        self.require_original()
        current = read_executor_source(self.root, self.parts)
        if current != (self.content, self.file_identity, self.directory_identity):
            raise ValueError('unresolved_executor_source_changed')
        self.require_original()

    # LLM: key只定位拒绝/既有复核，不是P2 scope或角色权限。
    # 函数用途: 返回唯一原调度键，不创建第二进度状态。
    def key(self):
        self.require_original()
        return ('unresolved/' + '/'.join(self.parts)).encode()

    # LLM: 隔离同时保实际旧桶和当前owner映射；C主owner仍挡全部worker，不借未知count生成范围。
    # 函数用途: 返回限制键供原隔离集合消费。
    def restriction_keys(self):
        self.require_original()
        return {(self.generation, self.role, None if self.role == 'C' else self.index, self.owner_sha)}

    # LLM: 完整字节/文件/目录身份进入原源末检，不能仅比ID或owner洗掉漂移。
    # 函数用途: 返回内存比较事实，不持久化新账。
    def signature(self):
        self.require_original()
        return (self.parts, hashlib.sha256(self.content).hexdigest(), self.file_identity, self.directory_identity)


# LLM: 源读取锚定原descriptor，限定实际字节量，普通no-follow/单链接规则由既有原语守住；收尾再核规范路径。
# 函数用途: 完整读回原P1文件和父目录身份，不创建任何目录或文件。
def read_executor_source(root, parts):
    directory = open_directory_beneath(root, parts[:-1], require_dir_fd=True)
    descriptor = None
    try:
        directory_info = os.fstat(directory)
        descriptor = _openat_file(directory, parts[-1], os.O_RDONLY, 0o600)
        before = os.fstat(descriptor)
        content = _read_descriptor(descriptor, _UNRESOLVED_SOURCE_MAX_BYTES)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError('executor_source_changed')
        current = open_directory_beneath(root, parts[:-1], require_dir_fd=True)
        try:
            now = os.fstat(current)
            if (now.st_dev, now.st_ino) != (directory_info.st_dev, directory_info.st_ino):
                raise ValueError('executor_namespace_changed')
        finally:
            os.close(current)
        return content, (before.st_dev, before.st_ino), (directory_info.st_dev, directory_info.st_ino)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


# LLM: 限制来源仍完整核P0/文件名/旧代，不由JSON缺count解释当前代坏桶；不生成权限或P2目标。
# 函数用途: 在旧分区数确实未知时捕获原规范责任。
def capture_unresolved_source(root, parts, current_c_run_id):
    content, file_identity, directory_identity = read_executor_source(root, parts)
    row = deserialize_record(content)
    if (row.get('schema_version') not in {'multiprocess_executor_reservation.v1', 'multiprocess_executor_reservation.v2'}
            or row['c_run_id'] == current_c_run_id
            or re.fullmatch(r'w(0|[1-9][0-9]*)', parts[-2]) is None
            or hashlib.sha256(row['reservation_id'].encode()).hexdigest() + '.json' != parts[-1]):
        raise ValueError('unresolved_executor_source_invalid')
    domain = row.get('execution_domain')
    if domain is not None and domain['source_worker_index'] != int(parts[-2][1:]):
        raise ValueError('unresolved_executor_domain_partition_conflict')
    source = UnresolvedExecutorSource(root, parts, content, file_identity, directory_identity)
    source.require_unchanged()
    return source


# LLM: 复用唯一quarantine_recheck状态/退避，digest绑定原限制字节而非虚构P2 scope；此状态永不产生stopped回执。
# 函数用途: 为未知旧count来源初始化原复核调度，保持unknown。
def unresolved_recheck_state(source, current_c_run_id, now):
    from .multiprocess_takeover_recheck import _checked_state, quarantine_backoff

    source.require_original()
    state = {'schema_version': 'multiprocess_quarantine_recheck.v1', 'current_c_run_id': current_c_run_id,
             'old_c_run_id': source.generation, 'scope_digest': hashlib.sha256(source.content).hexdigest(),
             'status': 'quarantined', 'attempt': 0, 'next_check_at': now + quarantine_backoff(0),
             'last_verification_id': None, 'last_verdict': 'unknown'}
    _checked_state(state)
    return state
