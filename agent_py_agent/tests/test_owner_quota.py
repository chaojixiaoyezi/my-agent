from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace


def _access(owner: Path, *, max_bytes: int, available: bool = True):
    from agent_py_agent.agent.tooling._filesystem_read import FileSystemAccessOptions

    return FileSystemAccessOptions(
        owner_scope_root=str(owner),
        owner_quota_max_bytes=max_bytes,
        owner_quota_policy_available=available,
    )


def test_owner_usage_counts_all_regular_files_but_not_symlink_targets(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.owner_quota import owner_logical_usage_bytes

    owner = tmp_path / "owner"
    outside = tmp_path / "outside.bin"
    owner.mkdir()
    outside.write_bytes(b"x" * 100)
    (owner / "a.bin").write_bytes(b"1234")
    (owner / "link").symlink_to(outside)

    assert owner_logical_usage_bytes(owner) == 4


def test_write_file_quota_is_cross_tool_instance_and_cross_thread_safe(tmp_path: Path) -> None:
    from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool, WriteFileToolOptions

    owner = tmp_path / "owner"
    owner.mkdir()
    options = WriteFileToolOptions(access_options=_access(owner, max_bytes=10))
    tools = [WriteFileTool(owner, [owner], options), WriteFileTool(owner, [owner], options)]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda item: item[0].execute(
                    {"path": item[1], "content": "12345678"}
                ),
                zip(tools, ("a.txt", "b.txt"), strict=True),
            )
        )

    assert sum(result.ok for result in results) == 1
    assert {result.error_code for result in results if not result.ok} == {
        "OWNER_DISK_QUOTA_EXCEEDED"
    }
    assert sum(path.stat().st_size for path in owner.glob("*.txt")) == 8


def test_concurrent_appends_project_current_size_under_owner_lock(tmp_path: Path) -> None:
    from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool, WriteFileToolOptions

    owner = tmp_path / "owner"
    owner.mkdir()
    target = owner / "shared.txt"
    target.write_text("1234", encoding="utf-8")
    options = WriteFileToolOptions(access_options=_access(owner, max_bytes=10))
    tools = [WriteFileTool(owner, [owner], options), WriteFileTool(owner, [owner], options)]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda tool: tool.execute(
                    {"path": "shared.txt", "content": "5678", "mode": "append"}
                ),
                tools,
            )
        )

    assert sum(result.ok for result in results) == 1
    assert {result.error_code for result in results if not result.ok} == {
        "OWNER_DISK_QUOTA_EXCEEDED"
    }
    assert target.stat().st_size == 8


def test_quota_policy_load_failure_blocks_owner_write(tmp_path: Path) -> None:
    from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool, WriteFileToolOptions

    owner = tmp_path / "owner"
    owner.mkdir()
    tool = WriteFileTool(
        owner,
        [owner],
        WriteFileToolOptions(access_options=_access(owner, max_bytes=100, available=False)),
    )

    result = tool.execute({"path": "blocked.txt", "content": "x"})

    assert not result.ok
    assert result.error_code == "OWNER_QUOTA_UNAVAILABLE"
    assert not (owner / "blocked.txt").exists()


def test_over_quota_owner_can_shrink_existing_file(tmp_path: Path) -> None:
    from agent_py_agent.agent.tooling._filesystem_edit import EditFileTool

    owner = tmp_path / "owner"
    owner.mkdir()
    target = owner / "large.txt"
    target.write_text("abcdefghij", encoding="utf-8")
    tool = EditFileTool(owner, [owner], _access(owner, max_bytes=5))

    result = tool.execute(
        {"path": "large.txt", "old_string": "abcdefghij", "new_string": "tiny"}
    )

    assert result.ok
    assert target.read_text(encoding="utf-8") == "tiny"


def test_apply_patch_rejects_over_quota_batch_before_first_file_changes(tmp_path: Path) -> None:
    from agent_py_agent.agent.tooling._filesystem_patch import ApplyPatchTool

    owner = tmp_path / "owner"
    owner.mkdir()
    tool = ApplyPatchTool(owner, [owner], _access(owner, max_bytes=7))
    patch = """*** Begin Patch
*** Add File: a.txt
+1234
*** Add File: b.txt
+5678
*** End Patch
"""

    result = tool.execute({"patch": patch})

    assert not result.ok
    assert result.error_code == "OWNER_DISK_QUOTA_EXCEEDED"
    assert not (owner / "a.txt").exists()
    assert not (owner / "b.txt").exists()


def test_write_outside_owner_root_does_not_consume_owner_quota(tmp_path: Path) -> None:
    from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool, WriteFileToolOptions

    owner = tmp_path / "owner"
    workspace = tmp_path / "workspace"
    owner.mkdir()
    workspace.mkdir()
    tool = WriteFileTool(
        workspace,
        [workspace],
        WriteFileToolOptions(access_options=_access(owner, max_bytes=1)),
    )

    result = tool.execute({"path": "repo.txt", "content": "outside-owner"})

    assert result.ok
    assert (workspace / "repo.txt").read_text(encoding="utf-8") == "outside-owner"


def test_max_active_agents_caps_main_plus_live_subagents() -> None:
    from agent_py_agent.agent.agent_core.orchestration_tools import _available_creation_slots

    agent = SimpleNamespace(
        config=SimpleNamespace(
            max_subagents=50,
            subagent_hierarchy_max_children_per_tool_call=20,
            task_max_subagents=0,
        ),
        owner_policy=SimpleNamespace(max_subagents=50, max_active_agents=2),
        subagents=SimpleNamespace(
            list_runs=lambda: [SimpleNamespace(id="subagent-1", status="RUNNING")]
        ),
    )

    slots, details = _available_creation_slots(agent)

    assert slots == 0
    assert details["active_agent_cap"] == 2
    assert details["owner_active"] == 1
