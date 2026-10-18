# LLM: W只能结清自己真实shutdown/join和原空P1范围；任何P1树必须由原C/kernel P6证明，W不凭cache/心跳/空future假fulltree；Count0不调用，同步原current drain/held负例。
# 模块用途: 给已停止认领的原worker核进程内资源和唯一持久责任。
import time

from .multiprocess_drain_runtime import _local_call_items
from .multiprocess_drain_sources import durable_drain_snapshot, durable_items, same_durable_snapshot
from .multiprocess_stop_drain import STOP_DRAIN_KINDS, DrainInventory, InFlightItem, assess_drain
from .multiprocess_worker_dispatch import WorkerRequestPump


# LLM: 只能原实际pump/frozen launch；原pool close确实wait=True，仍未知source/cleanup/任何P1树均不得报告drained，不松原C末门或P3。
# 函数用途: 在真实请求线程收口后评估本worker能否安全报告排空。
def worker_pump_drain_assessment(pump):
    if type(pump) is not WorkerRequestPump:
        raise ValueError('drain_original_worker_pump_required')
    scope = pump.frozen.launch.partition
    if pump.claiming_stopped is not True:
        raise ValueError('drain_worker_claiming_not_stopped')
    pump.close()
    stopped, group, active, controls = pump.drain_snapshot()
    control_stopped, control_groups, control_active, faults = controls
    items = [*_local_call_items(scope)]
    for index, original in enumerate((group, *control_groups)):
        original.stop()
        items.append(InFlightItem('foreground' if index == 0 else 'control', scope, f'original-pool/{index}',
                                  original.observe(time.monotonic())))
    if active or control_active or faults or not control_stopped:
        items.append(InFlightItem('control', scope, 'original-pump', 'responsibility_pending'))
    try:
        before = durable_drain_snapshot(pump.paths, scope.count)
        items.extend(durable_items(scope, before))
        for target in before[2]:
            if target.scope['worker_index'] == scope.index:
                items.extend(InFlightItem('executor_tree', scope, identifier, 'original_c_tree_proof_required')
                             for identifier in target.scope['executor_reservation_ids'])
        after = durable_drain_snapshot(pump.paths, scope.count)
        if not same_durable_snapshot(before, after):
            raise ValueError('drain_original_sources_changed')
    except (OSError, ValueError, TypeError, KeyError) as error:
        items.append(InFlightItem('foreground', scope, 'original-sources', type(error).__name__))
        return assess_drain(DrainInventory(scope, stopped, (), tuple(items), None))
    return assess_drain(DrainInventory(scope, stopped, STOP_DRAIN_KINDS, tuple(items), None))
