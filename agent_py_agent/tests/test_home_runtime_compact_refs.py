from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.user_space.home_runtime_query import (
    TaskWorkspaceQuery,
    home_task_workspace_payload,
    list_task_workspaces,
)
from agent_py_agent.agent.user_space.run_workspace import (
    EnsureRunWorkspaceRequest,
    ensure_run_workspace,
)
from agent_py_agent.agent.user_space.task_compact_rollup import sync_task_compact_rollup


def test_task_workspace_payload_exposes_compact_rollup_refs(tmp_path: Path) -> None:
    home = tmp_path / "home"
    owner_home = home / "owners" / "local" / "main"
    paths = ensure_run_workspace(
        EnsureRunWorkspaceRequest(
            home=owner_home,
            template="tasks/{date}/{task_slug}",
            task_name="多子代理恢复",
            user_prompt="恢复多子代理任务",
            request_id="req-compact",
            run_id="run-compact",
            task_id="task-compact",
            created_at="2026-05-13T01:00:00+00:00",
        )
    )
    rollup = sync_task_compact_rollup(paths.root)

    items = list_task_workspaces(home, TaskWorkspaceQuery(date_key="2026-05-13"))
    payload = home_task_workspace_payload(home, "task-compact")

    assert items[0]["compact"]["task_rollup_json"] == str(rollup.rollup_json)
    assert items[0]["compact"]["continue_packet"]
    assert payload is not None
    assert str(rollup.rollup_json) in payload["recommended_read_paths"]


def test_task_workspace_payload_reports_broken_compact_latest_pointer(tmp_path: Path) -> None:
    home = tmp_path / "home"
    owner_home = home / "owners" / "local" / "main"
    paths = ensure_run_workspace(
        EnsureRunWorkspaceRequest(
            home=owner_home,
            template="tasks/{date}/{task_slug}",
            task_name="多子代理恢复",
            user_prompt="恢复多子代理任务",
            request_id="req-compact",
            run_id="run-compact",
            task_id="task-compact",
            created_at="2026-05-13T01:00:00+00:00",
        )
    )
    rollup = sync_task_compact_rollup(paths.root)
    (rollup.compact_root / "latest.txt").write_text("missing-compact-package\n", encoding="utf-8")

    items = list_task_workspaces(home, TaskWorkspaceQuery(date_key="2026-05-13"))
    payload = home_task_workspace_payload(home, "task-compact")

    assert items[0]["compact"]["latest_package"] == str(rollup.compact_package_dir.resolve())
    assert items[0]["compact"]["load_errors"]
    assert items[0]["compact"]["load_errors"][0]["context"] == "home_runtime_compact_refs.latest_pointer"
    assert payload is not None
    assert payload["compact"]["load_errors"] == items[0]["compact"]["load_errors"]
