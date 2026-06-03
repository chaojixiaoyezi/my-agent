
from __future__ import annotations

"""action service Params bundles shared by records and handlers."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...models import SubAgentTask
    from ...reports import ActionPlanItem


@dataclass(frozen=True)
class RecordAfterTaskActionParams:
    """Params bundle for creating a post-mutation action apply record."""

    action: ActionPlanItem
    task: SubAgentTask
    before_status: str
    before_channel_status: str
    message: str
    evidence_paths: list[str] | None = None
