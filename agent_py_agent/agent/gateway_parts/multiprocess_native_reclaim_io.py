# LLM: 机会GC专用只读入口；原T仍由调用方持有，JSON锁竞争跳过，严格no-follow/普通文件与实际字节上限不授予权限。
# 模块用途: 避免旧FIFO或终态JSON忙锁堵住原生消息；不改变业务准入的保守读取。
import json

from ..common.nofollow_fs import read_bytes_beneath
from .io import GatewayJsonReadReport, try_locked_file_transition
from .multiprocess_records import deserialize_record

# 只限制机会回收资源；超限保留定位标记，不截断、不产生终态/释放证明。
# 参数用途: 机会回收终态只读上限16MiB字节；超限保留原责任定位，不截断事实。
_TERMINAL_READ_LIMIT_BYTES = 16 * 1024 * 1024
# 参数用途: 机会回收P3只读上限16384字节；超限保留原责任定位，不生成释放事实。
_P3_READ_LIMIT_BYTES = 16384


# LLM: 保持原JSON锁身份但不排队；O_NONBLOCK/no-follow核普通文件，解析递归故障只转坏源，不吞issuer拒绝。
# 函数用途: 有限读取终态对象，忙锁返回不可采用报告，损坏向候选隔离层传播。
def read_json_file_report(path, *, root, context):
    try:
        path.lstat()
    except FileNotFoundError:
        return GatewayJsonReadReport({})
    with try_locked_file_transition(path) as acquired:
        if not acquired:
            return GatewayJsonReadReport({}, {'context': context, 'error_code': 'reclaim_json_lock_busy'}, lock_busy=True)
        content = read_bytes_beneath(root, path.relative_to(root).parts,
                                     max_bytes=_TERMINAL_READ_LIMIT_BYTES, require_dir_fd=True)
        if content is None:
            return GatewayJsonReadReport({})
        try:
            payload = json.loads(content.decode('utf-8'))
        except RecursionError as error:
            raise ValueError('reclaim_terminal_parser_recursion') from error
        if type(payload) is not dict:
            raise ValueError('reclaim_terminal_object_required')
        return GatewayJsonReadReport(payload)


# LLM: P3只用canonical路径与P0反序列化；特殊文件/超限/递归坏源均拒绝，不吞原issuer业务拒绝。
# 函数用途: 在原T内有界读取完整普通P3，局部解析故障转坏源而非空账。
def read_reservation(root, path):
    content = read_bytes_beneath(root, path.relative_to(root).parts,
                                 max_bytes=_P3_READ_LIMIT_BYTES, require_dir_fd=True)
    if content is None:
        return None
    try:
        return deserialize_record(content)
    except RecursionError as error:
        raise ValueError('reclaim_reservation_parser_recursion') from error
