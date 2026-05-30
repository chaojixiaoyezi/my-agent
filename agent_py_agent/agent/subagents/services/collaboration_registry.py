# LLM: Collaboration registry snapshots subagent routing facts after task creation.
# 模块用途: 登记子代理开放世界能力和来源提示，供协作请求自动路由。

from __future__ import annotations

import time
from typing import Any


def register_collaboration_agent_capability(manager: Any, task: Any) -> None:
    store = getattr(manager, "collaboration_store", None)
    if store is None or not hasattr(store, "register_agent"):
        return
    try:
        from ...collaboration import AgentCapability

        store.register_agent(
            AgentCapability(
                agent_id=str(getattr(task, "id", "") or ""),
                role=str(getattr(task, "role", "") or ""),
                capabilities=tuple(collaboration_capabilities_for_task(task)),
                sources=tuple(collaboration_sources_for_task(task)),
                status="available",
                load=0.0,
                updated_at=time.time(),
                metadata={
                    "agent_name": str(getattr(task, "agent_name", "") or ""),
                    "run_id": str(getattr(task, "id", "") or ""),
                    "depth": int(getattr(task, "depth", 0) or 0),
                },
            )
        )
    except (AttributeError, OSError, TypeError, ValueError):
        return


def collaboration_capabilities_for_task(task: Any) -> list[str]:
    capabilities: list[str] = []
    attrs = getattr(task, "attributes", {}) if isinstance(getattr(task, "attributes", {}), dict) else {}
    for value in _capability_sources(task, attrs):
        _extend_capabilities(capabilities, value)
    for tool in _string_items(getattr(task, "allowed_tools", [])):
        _add_capability(capabilities, tool)
        for alias in _capability_aliases_for_tool(tool):
            _add_capability(capabilities, alias)
    return capabilities


def _capability_sources(task: Any, attrs: dict[str, object]) -> tuple[object, ...]:
    return (
        getattr(task, "role", ""),
        getattr(task, "agent_name", ""),
        attrs.get("capabilities"),
        attrs.get("collaboration_capabilities"),
        attrs.get("provided_capabilities"),
        attrs.get("required_capabilities"),
    )


def collaboration_sources_for_task(task: Any) -> list[str]:
    attrs = getattr(task, "attributes", {}) if isinstance(getattr(task, "attributes", {}), dict) else {}
    sources: list[str] = []
    for key in ("sources", "source_ids", "source_refs"):
        _extend_capabilities(sources, attrs.get(key))
    return sources


def _capability_aliases_for_tool(tool: str) -> list[str]:
    name = tool.lower()
    aliases: list[str] = []
    # LLM: Generic write aliases keep collaboration routing independent of retired special writer names.
    if any(token in name for token in ("read", "search", "query", "fetch", "list", "get", "http", "browser")):
        aliases.append("query")
    if "submit_collaboration_result" in name or ("evidence" in name and any(token in name for token in ("submit", "add", "record"))):
        aliases.extend(["evidence", "evidence_submission"])
    if any(token in name for token in ("write", "append", "replace", "save", "create", "structured data")):
        aliases.extend(["write", "artifact_write"])
    if any(token in name for token in ("send", "notify", "message")):
        aliases.append("notify")
    if any(token in name for token in ("dispatch", "subagent", "delegate")):
        aliases.append("delegate")
    return aliases


def _extend_capabilities(target: list[str], value: object) -> None:
    for item in _string_items(value):
        _add_capability(target, item)


def _add_capability(target: list[str], value: object) -> None:
    text = str(value or "").strip()
    if not text:
        return
    for candidate in (text, text.lower()):
        if candidate and candidate not in target:
            target.append(candidate)


def _string_items(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item or "").strip()]
    text = str(value or "").strip()
    return [text] if text else []
