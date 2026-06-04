from __future__ import annotations

"""Subagent lifecycle service used by SimpleAgent.

The service owns spawn/run/probe/finalize wiring for one subagent attempt.
SimpleAgent keeps the public method names, while lifecycle behavior lives here
so it can be tested and evolved without growing another mixin body.
"""

from .params import SpawnSubagentsParams, SubagentRunParams
from .run_flow import run_subagent_flow
from .spawn_flow import (
    SpawnSubagentsFlowRequest,
    spawn_subagents_flow,
)


class SubagentLifecycleService:
    def __init__(self, agent) -> None:
        self.agent = agent

    def spawn_subagents(self, options: SpawnSubagentsParams):
        if not self.agent.config.enable_subagents:
            raise RuntimeError("配置已禁用 subagent。")
        return spawn_subagents_flow(
            SpawnSubagentsFlowRequest(
                agent=self.agent,
                options=options,
                workflow_mode=self._config_workflow_dispatch_mode(),
            )
        )

    def run_subagent(self, options: SubagentRunParams):
        return run_subagent_flow(self, options)

    def prepare_attempt(self, run_id: str, *, dry_run: bool, active_attempt_id: str, retry_reason: str) -> str:
        if dry_run or active_attempt_id:
            return active_attempt_id
        prepared = self.agent.subagents.prepare_runner_attempt(run_id, retry_reason=retry_reason)
        return prepared.runner_active_attempt_id

    def build_prompt(self, run_id: str, max_cards: int, instruction: str):
        return self.agent._build_subagent_prompt(run_id, max_cards, instruction)

    def record_dry_run(self, run_id: str, active_attempt_id: str, prompt: str):
        return self.agent._record_subagent_dry_run(run_id, active_attempt_id, prompt)

    def probe_channel(self, params):
        return self.agent._probe_subagent_channel(params)

    def handle_run_failure(self, params):
        return self.agent._handle_subagent_run_failure(params)

    def finalize_run(self, params):
        return self.agent._finalize_subagent_run(params)

    def _config_workflow_dispatch_mode(self) -> str:
        return config_workflow_dispatch_mode(getattr(self.agent.config, "subagent_workflow_mode", ""))


def config_workflow_dispatch_mode(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == "auto":
        return "auto"
    if normalized == "manual":
        return "plan"
    return "off"


__all__ = ["SubagentLifecycleService", "config_workflow_dispatch_mode"]
