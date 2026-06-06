
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..gateway_parts.io import update_json_file_atomic
from ..runtime_errors import runtime_error_report
from .identity import agent_identity_keys
from .models import AGENT_CAPABILITY_STATUS_AVAILABLE, AgentCapability
from .store_evidence import CollaborationEvidenceStore


def _capability_roster_load_error(path: Path, exc: BaseException) -> dict[str, Any]:
    report = runtime_error_report(exc, context="collaboration.agent_capabilities.read")
    report["path"] = str(path)
    return report


class CollaborationCapabilityStore(CollaborationEvidenceStore):
    def register_agent(self, capability: AgentCapability) -> AgentCapability:
        def updater(data: dict[str, Any]) -> dict[str, Any]:
            return {**data, capability.agent_id: capability.to_dict()}

        update_json_file_atomic(self.capabilities_path, updater)
        return capability

    def agent_capabilities(self) -> list[AgentCapability]:
        capabilities, _load_error = self.agent_capabilities_report()
        return capabilities

    def agent_capabilities_report(self) -> tuple[list[AgentCapability], dict[str, Any] | None]:
        if not self.capabilities_path.exists():
            return [], None
        try:
            data = json.loads(self.capabilities_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError(f"agent capabilities roster is {type(data).__name__}, expected object")
        except (OSError, UnicodeError, ValueError) as exc:
            return [], _capability_roster_load_error(self.capabilities_path, exc)
        return [AgentCapability.from_dict(item) for item in data.values() if isinstance(item, dict)], None

    def agent_identity_keys(self, values: object = ()) -> set[str]:
        return agent_identity_keys(values)

    def match_agents(
        self,
        *,
        required_capabilities: list[str] | tuple[str, ...] | None = None,
        exclude_agent_id: str = "",
        limit: int = 20,
    ) -> list[AgentCapability]:
        matches, _load_error = self.match_agents_report(
            required_capabilities=required_capabilities,
            exclude_agent_id=exclude_agent_id,
            limit=limit,
        )
        return matches

    def match_agents_report(
        self,
        *,
        required_capabilities: list[str] | tuple[str, ...] | None = None,
        exclude_agent_id: str = "",
        limit: int = 20,
    ) -> tuple[list[AgentCapability], dict[str, Any] | None]:
        required = {str(item) for item in (required_capabilities or []) if str(item or "").strip()}
        capabilities, load_error = self.agent_capabilities_report()
        matches = [item for item in capabilities if self._matches(item, required, exclude_agent_id)]
        matches.sort(key=lambda item: (item.load, item.agent_id))
        matches = matches if limit <= 0 else matches[:limit]
        return matches, load_error

    def _matches(self, capability: AgentCapability, required: set[str], exclude_agent_id: str) -> bool:
        if capability.agent_id == exclude_agent_id:
            return False
        if capability.status != AGENT_CAPABILITY_STATUS_AVAILABLE:
            return False
        return not required or required.issubset(set(capability.capabilities))
