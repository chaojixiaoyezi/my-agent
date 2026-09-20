"""One serialized permission queue shared by foreground and delegated turns."""

# LLM: 队列只负责 TUI 展示与控制，原服务端账本拥有授权；控制器保留 typed 请求/决定，不能让并发请求替换当前面板。
# 模块用途: 将主代理和多个子代理同时出现的工具审批排成一列，逐个显示和精确回写。

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ...agent.contracts.tool_approval import ToolApprovalDecision, ToolApprovalRequest

_RESOLVED_EXTERNAL_LIMIT = 512


# LLM: 宿主复用回合适配器的审批接口，不创建模型回合、正文或生命周期；真实归属保留在服务端原请求。
# 类用途: 给主/子后台审批提供纯 UI 宿主，不伪造前台回合。
class ExternalTuiPermissionOwner:
    # LLM: runtime 与 request id 只路由显示；服务端原请求由决定回写闭包另行冻结，不以界面编号授权。
    # 函数用途: 创建一个不带模型输出的审批面板宿主。
    def __init__(self, runtime: object, request_id: str) -> None:
        self.runtime = runtime
        self.request_id = str(request_id or "external-permission")
        self._lock = threading.RLock()

    # LLM: 外部审批没有正文候选可冻结；空操作保留控制器接口，并阻止面板修改真实 transcript。
    # 函数用途: 兼容普通回合审批控制器的边界回调。
    def _complete_active_assistant(self, *, process: bool = False) -> None:
        del process


# LLM: 条目保存服务端原请求，控制器只持有装饰过标题的显示请求；回写不能交换两者。
# 类用途: 保存一条等待排队显示的主/子后台审批及真实回写身份。
@dataclass(frozen=True)
class _ExternalPermissionEntry:
    key: str
    run_id: str
    request: ToolApprovalRequest
    controller: object


# LLM: 每个 root TuiRuntime 仅一个协调器，负责控制器排队和外部请求显示去重，不创建或批准 binding。
# 类用途: 管理当前审批、后续队列和主/子后台审批同步。
class TuiPermissionCoordinator:
    # LLM: The runtime lock is the sole queue lock, matching TuiRuntime event
    # ordering and avoiding a second independently synchronized UI state.
    # 函数用途: 初始化空审批队列。
    def __init__(self, runtime: object) -> None:
        self.runtime = runtime
        self._active: object | None = None
        self._queued: deque[object] = deque()
        self._external: dict[str, _ExternalPermissionEntry] = {}
        self._resolved_external: set[str] = set()
        self._resolved_order: deque[str] = deque()

    # LLM: Enqueue is idempotent by controller identity. Only the head may emit
    # permission_requested, so reducer state can never be replaced by a peer.
    # 函数用途: 将一个主代理或子代理审批控制器加入全局显示队列。
    def enqueue(self, controller: object) -> None:
        activate = False
        with self.runtime._lock:
            if self._active is controller or controller in self._queued:
                return
            if self._active is None:
                self._active = controller
                activate = True
            else:
                self._queued.append(controller)
        if activate:
            self._activate(controller)

    # LLM: Release removes exactly one controller. If it owned the visible
    # overlay, the next surviving controller is activated only after release.
    # 函数用途: 审批完成、取消或失效后切换到下一条等待请求。
    def release(self, controller: object) -> None:
        next_controller: object | None = None
        with self.runtime._lock:
            if self._active is controller:
                self._active = None
                next_controller = self._next_locked()
            else:
                try:
                    self._queued.remove(controller)
                except ValueError:
                    pass
            for key, entry in tuple(self._external.items()):
                if entry.controller is controller:
                    self._external.pop(key, None)
                    self._remember_resolved_locked(key)
        if next_controller is not None:
            self._activate(next_controller)

    # LLM: Selection and decisions route only to the one visible controller;
    # queued requests cannot accept keystrokes before their own panel appears.
    # 函数用途: 返回当前正在显示的审批控制器。
    def active_controller(self) -> object | None:
        with self.runtime._lock:
            return self._active

    # LLM: Controller activation check is identity-only and has no permission
    # semantics; it prevents a queued controller from publishing overlay events.
    # 函数用途: 判断指定控制器是否拥有当前审批面板。
    def is_active(self, controller: object) -> bool:
        with self.runtime._lock:
            return self._active is controller

    # LLM: 同步只接受服务端投影，每项绑定原请求回写；消失项仅本地收起，不能向原主/子执行写迟到取消。
    # 函数用途: 把一次 Gateway 待审批列表同步进 TUI 全局审批队列。
    def sync_external(
        self,
        value: object,
        *,
        decision_writer: Callable[[str, ToolApprovalRequest, ToolApprovalDecision], object],
        controller_factory: Callable[[ExternalTuiPermissionOwner], object],
    ) -> bool:
        rows = _external_rows(value)
        incoming = {row[0] for row in rows}
        with self.runtime._lock:
            vanished = [
                entry.controller
                for key, entry in self._external.items()
                if key not in incoming
            ]
        changed = bool(vanished)
        for controller in vanished:
            dismiss = getattr(controller, "cancel_pending", None)
            if callable(dismiss):
                dismiss(decision_value="unavailable")
        for key, run_id, label, request in rows:
            with self.runtime._lock:
                known = key in self._external or key in self._resolved_external
            if known:
                continue
            owner = ExternalTuiPermissionOwner(self.runtime, f"agent-approval:{key}")
            controller = controller_factory(owner)
            configure = getattr(controller, "configure_gateway_sink", None)
            opener = getattr(controller, "open", None)
            if not callable(configure) or not callable(opener):
                continue
            display_request = _display_request(request, label)

            # LLM: 回写只使用闭包冻结的服务端原始请求与 typed 决定，不能用装饰过的标题或前台回合代换身份。
            # 函数用途: 将当前面板的选择精确回写到对应主/子后台审批记录。
            def write_decision(
                _display: ToolApprovalRequest,
                decision: ToolApprovalDecision,
                *,
                selected_run: str = run_id,
                canonical: ToolApprovalRequest = request,
            ) -> None:
                result = decision_writer(selected_run, canonical, decision)
                if not isinstance(result, Mapping) or result.get("ok") is not True:
                    raise OSError("agent approval decision was not confirmed")

            configure(write_decision)
            entry = _ExternalPermissionEntry(key, run_id, request, controller)
            with self.runtime._lock:
                self._external[key] = entry
            try:
                opener(display_request)
            except (RuntimeError, TypeError, ValueError):
                with self.runtime._lock:
                    self._external.pop(key, None)
                continue
            changed = True
        return changed

    # LLM: Activation happens outside the runtime lock because a controller
    # publishes reducer events. A controller cleared by a race is released and
    # the next queued request is tried.
    # 函数用途: 让队首控制器打开审批面板。
    def _activate(self, controller: object) -> None:
        activate = getattr(controller, "activate_pending", None)
        if callable(activate) and activate():
            return
        self.release(controller)

    # LLM: Caller holds the runtime lock. Queue order is FIFO across foreground
    # and child requests, matching the user's observed arrival order.
    # 函数用途: 取出下一条等待审批并设为当前项。
    def _next_locked(self) -> object | None:
        while self._queued:
            candidate = self._queued.popleft()
            self._active = candidate
            return candidate
        return None

    # LLM: Resolved external ids are bounded UI dedupe only. They cannot grant
    # future calls because every decision still requires a live server record.
    # 函数用途: 记住本次 TUI 生命周期已收口的外部审批，避免短暂重放。
    def _remember_resolved_locked(self, key: str) -> None:
        if key in self._resolved_external:
            return
        self._resolved_external.add(key)
        self._resolved_order.append(key)
        while len(self._resolved_order) > _RESOLVED_EXTERNAL_LIMIT:
            expired = self._resolved_order.popleft()
            self._resolved_external.discard(expired)


# LLM: This stateless mixin keeps TuiRuntime below its size guard while routing
# all permission keystrokes to the coordinator's single visible controller.
# 类用途: 为 TuiRuntime 提供审批选择、反馈和决定的薄委托入口。
class TuiPermissionRuntimeMixin:
    _permission_coordinator: TuiPermissionCoordinator

    # LLM: Selection reaches only the queue head; no queued request can consume
    # navigation keys before its overlay is visible.
    # 函数用途: 上下移动当前审批选项。
    def move_permission_selection(self, delta: int) -> bool:
        controller = self._permission_coordinator.active_controller()
        mover = getattr(controller, "move_selection", None)
        return bool(mover(delta)) if callable(mover) else False

    # LLM: Feedback mode is owned by the visible typed request only.
    # 函数用途: 展开或收起当前审批的补充说明。
    def toggle_permission_feedback(self) -> bool:
        controller = self._permission_coordinator.active_controller()
        toggler = getattr(controller, "toggle_feedback", None)
        return bool(toggler()) if callable(toggler) else False

    # LLM: Text remains ordinary feedback and cannot select a decision.
    # 函数用途: 更新当前审批选项的补充说明草稿。
    def update_permission_feedback(self, permission_id: str, text: str) -> bool:
        controller = self._permission_coordinator.active_controller()
        updater = getattr(controller, "update_feedback", None)
        return bool(updater(permission_id, text)) if callable(updater) else False

    # LLM: A typed decision routes to the exact active permission id and its
    # controller-owned sink; labels and display order have no authority.
    # 函数用途: 提交当前审批决定。
    def resolve_permission(
        self,
        permission_id: str,
        decision: str,
        *,
        feedback: str = "",
    ) -> bool:
        controller = self._permission_coordinator.active_controller()
        resolver = getattr(controller, "resolve", None)
        return bool(
            resolver(permission_id, decision, feedback=feedback)
        ) if callable(resolver) else False


# LLM: 每行重新校验共享合同，按 run/permission 去重；代理类型只生成显示标签，不能改变请求或审批归属。
# 函数用途: 清洗 Gateway 返回的主/子后台审批列表。
def _external_rows(
    value: object,
) -> list[tuple[str, str, str, ToolApprovalRequest]]:
    if not isinstance(value, (list, tuple)):
        return []
    rows: list[tuple[str, str, str, ToolApprovalRequest]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        run_id = str(item.get("run_id") or "").strip()
        raw_request = item.get("request")
        if not run_id or not isinstance(raw_request, Mapping):
            continue
        try:
            request = ToolApprovalRequest.from_mapping(raw_request)
        except (TypeError, ValueError):
            continue
        if request.binding.get("run_id") != run_id:
            continue
        name = str(item.get("agent_name") or run_id).strip()[:96]
        label = "主代理" if item.get("agent_kind") == "main" else f"子代理 {name}"
        rows.append((f"{run_id}:{request.permission_id}", run_id, label, request))
    rows.sort(key=lambda row: (row[3].requested_at, row[0]))
    return rows


# LLM: 标签只装饰显示标题和说明，绑定、编号、选项及时间保持原合同；回写必须使用未装饰的原请求。
# 函数用途: 给审批面板标明请求来自主代理还是某个子代理。
def _display_request(request: ToolApprovalRequest, label: str) -> ToolApprovalRequest:
    payload = request.to_dict()
    payload["title"] = f"{label} · {request.title}"
    payload["description"] = f"{label}: {request.description}"
    return ToolApprovalRequest.from_mapping(payload)


__all__ = [
    "ExternalTuiPermissionOwner",
    "TuiPermissionCoordinator",
    "TuiPermissionRuntimeMixin",
]
