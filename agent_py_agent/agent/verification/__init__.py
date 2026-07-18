"""Owner-scoped passive verification evidence."""

from .repository import VerificationContext, VerificationEvidenceRepository
from .runtime import record_tool_verification

__all__ = ["VerificationContext", "VerificationEvidenceRepository", "record_tool_verification"]
