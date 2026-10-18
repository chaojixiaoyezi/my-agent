# LLM: 只回收原C内存定位表；canonical必须在原T读取并匹配原P3/attempt，不结清pin、不改预留或终态。
# 模块用途: 让短提交表随原请求终态回收，未知事实保留防重放标记。
from itertools import islice

from . import multiprocess_admission as ma
from .io import try_gateway_turn_transition
from .multiprocess_native_reclaim_io import read_json_file_report, read_reservation
from .multiprocess_records import MultiprocessRecordError, record_digest, release_admission

# 机会回收只占有限工作量；这是内部资源边界，不改变请求/权限判定。
# 参数用途: 每次机会回收最多扫描32个原短操作；只是内部资源预算，不改变授权判定。
_RECLAIM_SCAN_COUNT = 32


# LLM: 有界轮转软表；单候选helper持完整T读回/清理，no-follow有限源，坏源隔离；原issuer核验始终在外围，同步reclaim测试。
# 函数用途: 损坏或忙的旧候选保留，不拖住健康消息，不写P3、不结清原pin。
def reclaim_terminal_operations(issuer):
    issuer._require_current()
    controller, paths = issuer._controller, issuer.control.paths
    with controller._mutex:
        registry = getattr(controller, '_native_short_operations', {})
        candidates = list(islice(registry.items(), _RECLAIM_SCAN_COUNT))
        # 移到尾部只轮转定位表，不重造对象、权限或业务状态。
        for key, operation in candidates:
            del registry[key]
            registry[key] = operation
    for key, operation in candidates:
        try:
            _reclaim_candidate(controller, paths, registry, key, operation)
        except (OSError, ValueError, MultiprocessRecordError):
            # 仅候选读取/锁/合同拒绝；原issuer核验在外围，不把业务授权失败当健康。
            continue


# LLM: 一个原候选的T读回及原controller/operation门完整共用；清软引用只在原精确P3/attempt匹配后，不写业务状态或释放pin。
# 函数用途: 拆平机会回收单项，忙源或损坏源原样保留。
def _reclaim_candidate(controller, paths, registry, key, operation):
    request_id, attempt = key[2].stem, key[3]
    with try_gateway_turn_transition(paths, request_id) as acquired:
        if not acquired:
            return
        report = read_json_file_report(
            paths.terminal / f'{request_id}.json', root=paths.root, context='native.operation.reclaim',
        )
        if report.load_error is not None or not report.payload:
            return
        if report.payload.get('execution_attempt_id') != attempt:
            return
        reservation = read_reservation(paths.root, ma.reservation_file(paths, request_id))
        if reservation is None:
            return
        # 纯校验原canonical及已释放行的精确terminal_fact；不写P3释放。
        release_admission(reservation, report.payload)
        occupied = {**reservation, 'state': 'occupied', 'terminal_fact': None}
        if record_digest(occupied) != operation._p3_digest:
            return
        # 原T仍持有；避免规范事实读回与清标记之间被合法写者改动。
        with controller._mutex:
            if registry.get(key) is not operation:
                return
            with operation._lock:
                operation._used = True
                operation._sources = None
            del registry[key]
