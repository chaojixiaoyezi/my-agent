"""LLM: service classes for SimpleAgentRuntimeMixin.

Re-exports from split service modules for backward compatibility.
- ToolLoopService: tool-calling loop execution
- CompressionService: compression check, snapshot, and compression logic
- FinalizationService: result finalization, archiving, and token estimation
"""

from __future__ import annotations

from ._compression_service import CompressionService
from ._finalization_service import FinalizationService
from ._runtime_params import (
    ArchiveRunParams,
    CompressionContext,
    EstimateTokenParams,
    FinalizeContext,
    ToolLoopExecuteParams,
    WriteRecoverySnapshotParams,
)
from ._tool_loop_service import ToolLoopService
from .models import AgentRunResult

__all__ = [
    "AgentRunResult",
    "ArchiveRunParams",
    "CompressionContext",
    "CompressionService",
    "EstimateTokenParams",
    "FinalizeContext",
    "FinalizationService",
    "ToolLoopExecuteParams",
    "ToolLoopService",
    "WriteRecoverySnapshotParams",
]