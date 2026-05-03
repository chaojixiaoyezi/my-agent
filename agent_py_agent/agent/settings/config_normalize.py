"""Backward compatibility shim for config_normalize -> normalize module rename.

LLM: This file was renamed from config_normalize.py to normalize.py.
Tests still import from the old name for backward compatibility.
"""
from __future__ import annotations

from agent_py_agent.agent.settings.normalize import (
    normalize_agent_config,
    normalize_subagent_workflow_config,
)

__all__ = [
    "normalize_agent_config",
    "normalize_subagent_workflow_config",
]
