# LLM: 原共享四索引扫描不授写权；replace运输叶限同次PID/thread单次窗口，默认controller及生产Root末门保留，不改变due/sidecar/ref合同。
# 模块用途: 让owner私有维护下沉W后，共享压缩仍由同一个C写者完成，不建维护状态账。
from __future__ import annotations

import os
import threading

from ..user_space.home_index_compact import compact_global_indexes_if_due
from ..user_space.owner_resolver import OwnerIdentity
from .control_plane import GatewayControlPlane
from .multiprocess_global_write import GlobalWriteError
from .multiprocess_mode import current_worker_partition
from .multiprocess_route import owner_digest


# LLM: 原control引用/PID不是持久授权；每次commit仍由原controller真实lease/隔离门裁决，不能运输callback给W。
# 类用途: 保留生产C共享索引维护的唯一提交边界。
class CoordinatorIndexCompact:
    # LLM: 构造不读写index；可选同步末门只运输原atomic replace，生产loop用同Root/source，默认None保原调用与四源合同。
    # 函数用途: 捕获原C共享维护来源和本次生产关闭栅栏。
    def __init__(self, control, *, commit_transition=None):
        if type(control) is not GatewayControlPlane or current_worker_partition() is not None:
            raise GlobalWriteError("coordinator_required", "global_index_compact")
        if commit_transition is not None and not callable(commit_transition):
            raise TypeError("coordinator_index_commit_transition_required")
        self._commit_transition = commit_transition
        self.control, self.controller, self.pid = control, control.controller(), os.getpid()
        if self.controller is None:
            raise GlobalWriteError("coordinator_context_unavailable", "global_index_compact")

    # LLM: 只消费原四个index与原should_compact事实；扫描在锁外，最终canonical/sidecar写进入同一原C短门。
    # 函数用途: 按原阈值和冷却压缩共享查找投影。
    def run(self, home, *, now=None):
        if home.root != self.control.home_root:
            raise GlobalWriteError("coordinator_home_mismatch", "global_index_compact")
        return compact_global_indexes_if_due(home, now=now, commit_transition=self._commit)

    # LLM: 默认controller门保持；实际replace另包本次PID/thread单次窗口并finally失效，loop callback仍须末核C/lease/Root，长扫描门外。
    # 函数用途: 给原共享文件替换加同期叶限制，再消费当前生产者关闭门。
    def _commit(self, operation):
        if (self.pid != os.getpid() or current_worker_partition() is not None
                or self.control.controller() is not self.controller):
            raise GlobalWriteError("coordinator_context_unavailable", "global_index_compact")
        window = [True, os.getpid(), threading.get_ident(), False]

        # LLM: 私有staging仍是真实资源，不能凭句柄可能失效容许晚调；本原cell只消费一次且不授C权。
        # 函数用途: 在原窗口内调用一次真实replace，拒绝保留回调在关闭后再提交。
        def commit():
            if not window[0] or window[1:3] != [os.getpid(), threading.get_ident()] or window[3]:
                raise ValueError("coordinator_index_commit_window_required")
            window[3] = True
            return operation()

        try:
            if self._commit_transition is not None:
                try:
                    return self._commit_transition(commit)
                except ValueError as error:
                    raise GlobalWriteError("coordinator_context_unavailable", "global_index_compact") from error
            results = []
            decision = self.controller.launch_owner_writer(owner_digest(OwnerIdentity.local_main()),
                                                           lambda: results.append(commit()))
            if decision.get("status") != "started" or not results:
                raise GlobalWriteError("coordinator_context_unavailable", "global_index_compact")
            return results[0]
        finally:
            window[0] = False
