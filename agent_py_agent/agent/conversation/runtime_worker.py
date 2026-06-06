
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..agent_core.runtime.loop_models import RunParams
from ..runtime_errors import DataCorruptionError
from .channels import ChannelSendRequest, FakeChannelHub
from .models import BackgroundMainAgentReport, WakeSignal
from .runtime_context import context_markdown
from .runtime_tool_policy import BackgroundToolPolicyRequest, background_allowed_tools
from .runtime_utils import background_prompt, default_route_target, now, wake_signal_payload
from .store_context import ConversationStore


@dataclass(frozen=True)
class BackgroundRunRequest:
    thread_id: str
    task_id: str = ""
    reason: str = "scheduled_progress_report"
    route_channel: str = "internal"
    route_target: str = ""
    now: float = 0.0
    wake_signal: dict[str, Any] | None = None


class BackgroundMainAgentRuntime:
    def __init__(self, *, agent: object, store: ConversationStore, channels: FakeChannelHub | None = None):
        self.agent = agent
        self.store = store
        self.channels = channels or FakeChannelHub()

    def run_once(self, params: dict) -> BackgroundMainAgentReport:
        request = _run_request(params)
        if callable(getattr(self.store, "load_thread_report", None)):
            thread, load_error = self.store.load_thread_report(request.thread_id)
            if load_error is not None:
                raise DataCorruptionError(str(load_error))
        else:
            thread = self.store.load_thread(request.thread_id)
        if thread is None:
            raise KeyError(f"unknown conversation thread: {request.thread_id}")
        response = self._run_agent(thread, request)
        target = request.route_target or default_route_target(thread, request.route_channel)
        self._record_response(request, response, target)
        return BackgroundMainAgentReport(thread_id=request.thread_id, task_id=request.task_id, reason=request.reason, response=response, route_channel=request.route_channel, route_target=target, created_at=request.now)

    def _run_agent(self, thread, request: BackgroundRunRequest) -> str:
        result = self.agent.run(
            background_prompt(request.reason),
            params=_run_params(thread.thread_id, request, self.agent),
            inject=[context_markdown(agent=self.agent, store=self.store, thread=thread, request=request)],
        )
        return str(getattr(result, "response", "") or "")

    def _record_response(self, request: BackgroundRunRequest, response: str, target: str) -> None:
        self.store.append_message({"thread_id": request.thread_id, "role": "assistant", "content": response, "channel": request.route_channel, "now": request.now, "metadata": {"reason": request.reason, "task_id": request.task_id}})
        self.channels.send(ChannelSendRequest(
            channel=request.route_channel,
            target=target,
            content=response,
            thread_id=request.thread_id,
            task_id=request.task_id,
        ))


def _run_request(kwargs: dict[str, Any]) -> BackgroundRunRequest:
    current = now(kwargs.get("now"))
    return BackgroundRunRequest(
        thread_id=str(kwargs.get("thread_id") or ""),
        task_id=str(kwargs.get("task_id") or ""),
        reason=str(kwargs.get("reason") or "scheduled_progress_report"),
        route_channel=str(kwargs.get("route_channel") or "internal"),
        route_target=str(kwargs.get("route_target") or ""),
        now=current,
        wake_signal=wake_signal_payload(kwargs.get("wake_signal")),
    )


def _run_params(thread_id: str, request: BackgroundRunRequest, agent: object | None = None) -> RunParams:
    config = getattr(agent, "config", None)
    return RunParams(
        save=False,
        source="background_main_agent",
        run_id=f"bg-main-{thread_id}",
        task_id=request.task_id or thread_id,
        allowed_tools=background_allowed_tools(
            config,
            request=BackgroundToolPolicyRequest(
                reason=request.reason,
                wake_signal=request.wake_signal,
                config=config,
                owner_policy=getattr(agent, "owner_policy", None),
                policy_snapshot=_policy_snapshot_from_request(request),
            ),
        ),
    )


def _policy_snapshot_from_request(request: BackgroundRunRequest) -> dict[str, Any]:
    wake = request.wake_signal if isinstance(request.wake_signal, dict) else {}
    snapshot = wake.get("policy_snapshot")
    return dict(snapshot) if isinstance(snapshot, dict) else {}
