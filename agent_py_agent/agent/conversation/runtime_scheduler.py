
from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from ..agent_core.agent_tree.status import agent_tree_status_payload
from ..settings.defaults import default_config_int
from .models import BackgroundMainAgentReport, ObservationEvent, ProgressPolicy, WakeSignal
from .runtime_utils import (
    claim_heartbeat_interval_seconds as compute_claim_heartbeat_interval_seconds,
)
from .runtime_utils import (
    first_root_task_id,
    now,
    observations_by_thread,
)
from .runtime_worker import BackgroundMainAgentRuntime
from .store import ConversationStore

if TYPE_CHECKING:
    from ..collaboration import CollaborationStore


class BackgroundMainAgentScheduler:
    def __init__(self, config: dict):
        runtime = config["runtime"]
        store = config["store"]
        collaboration_store = config.get("collaboration_store")
        self.runtime = runtime
        self.store = store
        self.collaboration_store = collaboration_store or getattr(self.runtime.agent, "collaboration_store", None)
        agent_config = getattr(getattr(self.runtime, "agent", None), "config", None)
        claim_ttl_seconds = config.get("claim_ttl_seconds", _agent_config_int(agent_config, "background_claim_ttl_seconds"))
        claim_heartbeat_interval_seconds = config.get(
            "claim_heartbeat_interval_seconds",
            _agent_config_int(agent_config, "background_claim_heartbeat_interval_seconds"),
        )
        self.claim_ttl_seconds = max(1, int(claim_ttl_seconds or 1))
        self.claim_heartbeat_interval_seconds = compute_claim_heartbeat_interval_seconds(
            ttl_seconds=self.claim_ttl_seconds,
            configured_interval_seconds=claim_heartbeat_interval_seconds,
        )
        self.last_progress_policy_load_errors: list[dict[str, object]] = []

    def tick(self, *, now: float | None = None) -> list[BackgroundMainAgentReport]:
        current = now if now is not None else __import__("time").time()
        self._process_collaboration_cases(now=current)
        reports: list[BackgroundMainAgentReport] = []
        reported = self._run_wake_signals(reports, current)
        self._run_observation_batches(reports, reported, current)
        self._run_due_policies(reports, reported, current)
        return reports

    def _process_collaboration_cases(self, *, now: float) -> None:
        if self.collaboration_store is None:
            return
        from ..collaboration import CollaborationCoordinator
        CollaborationCoordinator(store=self.collaboration_store, conversation_store=self.store).tick(now=now)

    def _run_wake_signals(self, reports: list[BackgroundMainAgentReport], current: float) -> set[str]:
        reported: set[str] = set()
        handled: set[str] = set()
        wake_signals = self.store.pending_wake_signals(limit=self._config_limit("conversation_pending_wake_limit"))
        for signal in wake_signals:
            if signal.wake_signal_id in handled:
                continue
            if signal.thread_id in reported:
                self._mark_signal(signal, current, handled)
                continue
            report = self._run_wake_signal(signal, now=current)
            if report is not None:
                reports.append(report)
                reported.add(report.thread_id)
                self._mark_sibling_signals(wake_signals, signal.thread_id, current, handled)
        return reported

    def _run_observation_batches(self, reports: list[BackgroundMainAgentReport], reported: set[str], current: float) -> None:
        pending_observations = self.store.unhandled_observations_requiring_main(
            limit=self._config_limit("conversation_unhandled_observation_limit")
        )
        for thread_id, thread_observations in observations_by_thread(pending_observations).items():
            if thread_id in reported:
                continue
            report = self._run_observation_batch(thread_id, thread_observations, now=current)
            if report is not None:
                reports.append(report)
                reported.add(report.thread_id)

    def _run_due_policies(self, reports: list[BackgroundMainAgentReport], reported: set[str], current: float) -> None:
        policies, load_errors = self.store.due_progress_policies_report(now=current)
        self.last_progress_policy_load_errors = load_errors
        for policy in policies:
            if policy.thread_id in reported:
                continue
            if report := self._run_due_policy(policy, now=current):
                reports.append(report)

    def _run_wake_signal(self, signal: WakeSignal, *, now: float) -> BackgroundMainAgentReport | None:
        report = self._run_claimed({"thread_id": signal.thread_id, "task_id": signal.root_task_id, "reason": "urgent_wake_signal" if signal.urgency == "urgent" else "wake_signal", "now": now, "wake_signal": signal})
        if report is not None:
            self.store.mark_wake_signal_handled(signal.wake_signal_id, now=now)
        return report

    def _run_observation_batch(self, thread_id: str, observations: list[ObservationEvent], *, now: float) -> BackgroundMainAgentReport | None:
        report = self._run_claimed({"thread_id": thread_id, "task_id": first_root_task_id(observations), "reason": "observation_requires_main_agent", "now": now})
        if report is not None:
            self.store.mark_observations_handled([item.observation_id for item in observations], now=now)
        return report

    def _run_due_policy(self, policy: ProgressPolicy, *, now: float) -> BackgroundMainAgentReport | None:
        report = self._run_claimed({"thread_id": policy.thread_id, "task_id": policy.task_id, "reason": "scheduled_progress_report", "route_channel": policy.route_channel, "route_target": policy.route_target, "now": now})
        if report is not None:
            self.store.mark_progress_reported(policy.policy_id, now=now)
        return report

    def _run_claimed(self, kwargs: dict) -> BackgroundMainAgentReport | None:
        claim = self.store.claim_background_run({
            "thread_id": kwargs.get("thread_id", ""),
            "task_id": kwargs.get("task_id", ""),
            "reason": kwargs.get("reason", ""),
            "lease_seconds": self.claim_ttl_seconds,
            "now": kwargs.get("now"),
        })
        if claim is None:
            return None
        return self._run_with_heartbeat(str(claim.get("claim_id") or ""), kwargs)

    def _run_with_heartbeat(self, claim_id: str, kwargs: dict) -> BackgroundMainAgentReport | None:
        heartbeat = self._start_heartbeat(claim_id, kwargs["thread_id"])
        status = "finished"
        error: BaseException | None = None
        try:
            return self.runtime.run_once(kwargs)
        except BaseException as exc:
            status = "failed"
            error = exc
            raise
        finally:
            heartbeat.stop()
            self.store.finish_background_run({
                "thread_id": kwargs["thread_id"],
                "claim_id": claim_id,
                "task_id": kwargs.get("task_id", ""),
                "status": status,
                "error": error,
                "runtime_facts": self._runtime_facts(),
                "now": now(),
            })

    def _start_heartbeat(self, claim_id: str, thread_id: str) -> _BackgroundClaimHeartbeat:
        heartbeat = _BackgroundClaimHeartbeat({"store": self.store, "thread_id": thread_id, "claim_id": claim_id, "lease_seconds": self.claim_ttl_seconds, "interval_seconds": self.claim_heartbeat_interval_seconds})
        heartbeat.start()
        return heartbeat

    def _mark_signal(self, signal: WakeSignal, current: float, handled: set[str]) -> None:
        self.store.mark_wake_signal_handled(signal.wake_signal_id, now=current)
        handled.add(signal.wake_signal_id)

    def _mark_sibling_signals(self, signals: list[WakeSignal], thread_id: str, current: float, handled: set[str]) -> None:
        for signal in signals:
            if signal.thread_id == thread_id and signal.wake_signal_id not in handled:
                self._mark_signal(signal, current, handled)

    def _config_limit(self, key: str) -> int:
        return _agent_config_int(getattr(getattr(self.runtime, "agent", None), "config", None), key)

    def _runtime_facts(self) -> dict[str, object]:
        agent = getattr(self.runtime, "agent", None)
        tree = agent_tree_status_payload(agent, {}) if agent is not None else {}
        return {
            "current_tool": str(getattr(agent, "_current_tool", "") or ""),
            "last_progress_at": float(getattr(agent, "_last_progress_at", 0.0) or 0.0),
            "last_progress_summary": str(getattr(agent, "_last_progress_summary", "") or ""),
            "tree_status_buckets": tree.get("status_buckets") if isinstance(tree.get("status_buckets"), dict) else {},
            "progress_policy_load_errors": list(self.last_progress_policy_load_errors),
        }


def _agent_config_int(config: object | None, key: str) -> int:
    if config is None:
        return default_config_int(key, minimum=0)
    try:
        return max(0, int(getattr(config, key)))
    except (TypeError, ValueError):
        return default_config_int(key, minimum=0)


class _BackgroundClaimHeartbeat(threading.Thread):
    def __init__(self, config: dict):
        thread_id = str(config.get("thread_id") or "")
        super().__init__(name=f"bg-claim-heartbeat-{thread_id}", daemon=True)
        self.store = config["store"]
        self.thread_id = thread_id
        self.claim_id = str(config.get("claim_id") or "")
        self.lease_seconds = max(1, int(config.get("lease_seconds") or 1))
        self.interval_seconds = max(0.05, float(config.get("interval_seconds") or 0.05))
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()
        self.join(timeout=self.interval_seconds + 5.0)

    def run(self) -> None:
        while not self.stop_event.wait(self.interval_seconds):
            renewed = self.store.renew_background_run_claim({"thread_id": self.thread_id, "claim_id": self.claim_id, "lease_seconds": self.lease_seconds, "now": now()})
            if renewed is None:
                return
