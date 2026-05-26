# LLM: Capability registry for collaboration routing; matching stays structural.
# 模块用途: 管理代理能力快照和基于能力的候选匹配。

from __future__ import annotations

from typing import Any

from ..gateway_parts.io import read_json_file, update_json_file_atomic
from .identity import agent_identity_aliases, capability_identity_aliases
from .models import AgentCapability
from .store_evidence import CollaborationEvidenceStore


class CollaborationCapabilityStore(CollaborationEvidenceStore):
    def register_agent(self, capability: AgentCapability) -> AgentCapability:
        def updater(data: dict[str, Any]) -> dict[str, Any]:
            return {**data, capability.agent_id: capability.to_dict()}

        update_json_file_atomic(self.capabilities_path, updater)
        return capability

    def agent_capabilities(self) -> list[AgentCapability]:
        data = read_json_file(self.capabilities_path)
        return [AgentCapability.from_dict(item) for item in data.values() if isinstance(item, dict)]

    def agent_identity_aliases(self, values: object = ()) -> set[str]:
        aliases = agent_identity_aliases(values)
        if aliases:
            aliases.update(self._registered_aliases_for(aliases))
        return aliases

    def match_agents(
        self,
        *,
        required_capabilities: list[str] | tuple[str, ...] | None = None,
        exclude_agent_id: str = "",
        limit: int = 20,
    ) -> list[AgentCapability]:
        required = {str(item) for item in (required_capabilities or []) if str(item or "").strip()}
        matches = [item for item in self.agent_capabilities() if self._matches(item, required, exclude_agent_id)]
        matches.sort(key=lambda item: (item.load, item.agent_id))
        return matches if limit <= 0 else matches[:limit]

    def _registered_aliases_for(self, aliases: set[str]) -> set[str]:
        result: set[str] = set()
        for capability in self.agent_capabilities():
            capability_aliases = capability_identity_aliases(capability)
            if aliases.intersection(capability_aliases):
                result.update(capability_aliases)
        return result

    def _matches(self, capability: AgentCapability, required: set[str], exclude_agent_id: str) -> bool:
        if capability.agent_id == exclude_agent_id:
            return False
        if capability.status not in {"available", "idle", "ready", ""}:
            return False
        return not required or required.issubset(set(capability.capabilities))
