"""W4: another scope's in-flight canonical commit no longer blocks capacity, selection or recovery reads."""
from __future__ import annotations

import pytest

from agent_py_agent.agent.subagents.models import SubAgentTask
from agent_py_agent.agent.subagents.services.persistence.run_index import (
    SubagentRunIndex,
    SubagentRunIndexUnavailable,
)


def _task(run_id: str, root: str, status: str = "RUNNING") -> SubAgentTask:
    return SubAgentTask(id=run_id, goal="work", thought="", plan=[], status=status, root_id=root, parent_id=root)


@pytest.fixture
def index(tmp_path):
    index = SubagentRunIndex(tmp_path)
    index.upsert(_task("run-a1", "root-a"))
    index.upsert(_task("run-b1", "root-b"))
    return index


def test_other_scope_pending_update_does_not_block_capacity_or_selection(index):
    index.mark_pending("run-b1")
    assert index.active_counts(("root-a",), "", None) == (2, 1)
    assert index.request_ids(("root-a",), "", None) == ["run-a1"]
    with pytest.raises(SubagentRunIndexUnavailable, match="pending canonical"):
        index.request_ids(("root-b",), "", None)


def test_brand_new_pending_run_is_counted_conservatively_and_blocks_selection(index):
    index.mark_pending("run-new")
    assert index.active_counts(("root-a",), "", None) == (3, 2)
    assert index.active_counts() == (3, 0)
    with pytest.raises(SubagentRunIndexUnavailable, match="pending canonical"):
        index.request_ids(("root-a",), "", None)
    index.upsert(_task("run-new", "root-b"))
    assert index.active_counts(("root-a",), "", None) == (3, 1)
    assert index.request_ids(("root-a",), "", None) == ["run-a1"]


def test_recovery_pages_skip_pending_rows_until_their_commit_lands(index):
    index.mark_pending("run-b1")
    assert index.recovery_ids(limit=10) == (["run-a1"], "")
    index.upsert(_task("run-b1", "root-b"))
    assert index.recovery_ids(limit=10) == (["run-a1", "run-b1"], "")


def test_partial_coverage_still_refuses_every_read(index):
    with index.connect() as db:
        db.execute("UPDATE meta SET value=0 WHERE key='complete'")
    for call in (lambda: index.active_counts(("root-a",), "", None),
                 lambda: index.request_ids(("root-a",), "", None),
                 lambda: index.recovery_ids(limit=10)):
        with pytest.raises(SubagentRunIndexUnavailable, match="coverage is partial"):
            call()
