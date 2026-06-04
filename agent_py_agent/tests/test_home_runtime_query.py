from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.agent_core._runtime_params import ArchiveRunParams
from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
    write_run_task_workspace_if_needed,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.memory_archive import RawMemoryEvent, append_raw_event
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.home_runtime_query import (
    TaskWorkspaceQuery,
    list_task_workspaces,
)
from agent_py_agent.agent.user_space.run_workspace import (
    EnsureRunWorkspaceRequest,
    ensure_run_workspace,
)
from agent_py_agent.cli.parser import build_parser


def _write_config(tmp_path: Path, home: Path) -> Path:
    config_path = tmp_path / "agent_config.yaml"
    config_path.write_text(
        f'workspace_root: "workspace"\n'
        f'my_agent_home: "{home}"\n'
        'model_backend: "echo"\n'
        'memory_path: "memory.jsonl"\n'
        'prompt_files: []\n'
        'subagent_workspace: "subagents"\n'
        'local_store_path: "local_store/local.db"\n'
        'local_store_files_dir: "local_store/files"\n'
        'local_store_events_path: "local_store/events.jsonl"\n',
        encoding="utf-8",
    )
    return config_path


def _write_provider_config(tmp_path: Path, home: Path) -> Path:
    config_path = _write_config(tmp_path, home)
    with config_path.open("a", encoding="utf-8") as fh:
        fh.write('my_agent_owner_provider: "feishu"\n')
        fh.write('my_agent_owner_kind: "user"\n')
        fh.write('my_agent_owner_id: "ou_123"\n')
    return config_path


def _run_cli_json(capsys, config_path: Path, *argv: str) -> tuple[int, dict]:
    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), *argv, "--json"])
    code = args.func(args)
    captured = capsys.readouterr()
    return code, json.loads(captured.out)


def test_task_workspace_list_uses_configured_provider_owner_only(tmp_path: Path, capsys):
    home = tmp_path / "home"
    config_path = _write_provider_config(tmp_path, home)
    provider_root = home / "owners" / "providers" / "feishu" / "users" / "ou_123"
    local_root = home / "owners" / "local" / "main"
    provider_paths = ensure_run_workspace(
        EnsureRunWorkspaceRequest(
            home=provider_root,
            template="tasks/{date}/{task_slug}",
            task_name="provider-task",
            user_prompt="provider",
            request_id="req-provider",
            run_id="run-provider",
            task_id="provider-task",
            created_at="2026-05-13T01:00:00+00:00",
        )
    )
    ensure_run_workspace(
        EnsureRunWorkspaceRequest(
            home=local_root,
            template="tasks/{date}/{task_slug}",
            task_name="local-task",
            user_prompt="local",
            request_id="req-local",
            run_id="run-local",
            task_id="local-task",
            created_at="2026-05-13T01:00:00+00:00",
        )
    )
    ensure_run_workspace(
        EnsureRunWorkspaceRequest(
            home=home,
            template="tasks/{date}/{task_slug}",
            task_name="sample-task",
            user_prompt="previous",
            request_id="req-sample",
            run_id="run-previous",
            task_id="sample-task",
            created_at="2026-05-13T01:00:00+00:00",
        )
    )

    code, payload = _run_cli_json(capsys, config_path, "task-workspace-list", "--date", "2026-05-13")

    assert code == 0
    assert [task["state"]["task_id"] for task in payload["tasks"]] == ["provider-task"]
    assert payload["tasks"][0]["root"] == str(provider_paths.root)


def test_task_workspace_list_reports_corrupt_task_state(tmp_path: Path, capsys):
    home = tmp_path / "home"
    config_path = _write_config(tmp_path, home)
    paths = ensure_run_workspace(
        EnsureRunWorkspaceRequest(
            home=home / "owners" / "local" / "main",
            template="tasks/{date}/{task_slug}",
            task_name="bad-state-task",
            user_prompt="bad state",
            request_id="req-bad-state",
            run_id="run-bad-state",
            task_id="bad-state-task",
            created_at="2026-05-13T01:00:00+00:00",
        )
    )
    paths.state_json.write_text("{bad state json}\n", encoding="utf-8")

    code, payload = _run_cli_json(capsys, config_path, "task-workspace-list", "--date", "2026-05-13")

    assert code == 0
    assert payload["tasks"][0]["state"] == {}
    assert payload["tasks"][0]["state_load_error"]["context"] == "home_runtime_query.task_state"
    assert payload["tasks"][0]["state_load_error"]["path"] == str(paths.state_json)


def test_owner_tool_policy_isolated_per_provider_user(tmp_path: Path):
    home = tmp_path / "home"
    user_a = SimpleAgent(
        AgentConfig(
            my_agent_home=str(home),
            prompt_files=[],
            my_agent_owner_provider="feishu",
            my_agent_owner_kind="user",
            my_agent_owner_id="ou_a",
        ),
        tmp_path / "repo-a",
    )
    user_b = SimpleAgent(
        AgentConfig(
            my_agent_home=str(home),
            prompt_files=[],
            my_agent_owner_provider="feishu",
            my_agent_owner_kind="user",
            my_agent_owner_id="ou_b",
        ),
        tmp_path / "repo-b",
    )
    policy_path = Path(user_a.home_paths.owner_tool_policy_json)
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["disabled_tools"] = ["web_search"]
    policy_path.write_text(json.dumps(policy, ensure_ascii=False), encoding="utf-8")

    refreshed_a = SimpleAgent(
        AgentConfig(
            my_agent_home=str(home),
            prompt_files=[],
            my_agent_owner_provider="feishu",
            my_agent_owner_kind="user",
            my_agent_owner_id="ou_a",
        ),
        tmp_path / "repo-a",
    )

    assert "web_search" in refreshed_a.owner_policy.disabled_tools
    assert "web_search" in refreshed_a.tools.disabled_tool_names
    assert "web_search" not in user_b.owner_policy.disabled_tools
    assert "web_search" not in user_b.tools.disabled_tool_names


def test_memory_resume_reads_home_task_workspace_by_task_id(tmp_path: Path, capsys):
    home = tmp_path / "home"
    config_path = _write_config(tmp_path, home)
    agent = SimpleAgent(AgentConfig(my_agent_home=str(home), prompt_files=[]), tmp_path / "workspace")
    paths = ensure_run_workspace(
        EnsureRunWorkspaceRequest(
            home=agent.home_paths.root,
            template=agent.config.workspace_task_path_template,
            task_name="示例网站 E2E",
            user_prompt="继续示例网站",
            request_id="req-shop",
            run_id="run-shop",
            task_id="示例网站 E2E",
            source="run",
            created_at="2026-05-13T01:00:00+00:00",
        )
    )

    code, payload = _run_cli_json(capsys, config_path, "memory-resume", "--task-id", "示例网站 E2E")

    assert code == 0
    assert payload["task_fact_sources"][0]["exists"] is True
    assert payload["task_fact_sources"][0]["source"] == "home_task_workspace"
    assert payload["task_fact_sources"][0]["state_path"] == str(paths.state_json)
    assert str(paths.timeline_jsonl) in payload["resume"]["recommended_read_paths"]


def test_saved_run_workspace_writer_uses_local_main_owner_home(tmp_path: Path):
    home = tmp_path / "home"
    agent = SimpleAgent(AgentConfig(my_agent_home=str(home), prompt_files=[]), tmp_path / "workspace")

    root = Path(
        write_run_task_workspace_if_needed(
            agent,
            ArchiveRunParams(
                do_save=True,
                user_prompt="写一份报告",
                final_response="完成",
                archive_tool_calls=[],
                run_request_id="req-owner",
                run_id="run-owner",
                task_id="owner-task",
                source="run",
            ),
        )
    )

    assert root.is_relative_to(agent.home_paths.owner_home_dir)
    assert not (agent.home_paths.root / "tasks").exists()
    assert (agent.home_paths.owner_home_dir / "tasks").exists()


def test_memory_resume_reads_configured_provider_owner_archive_only(tmp_path: Path, capsys):
    home = tmp_path / "home"
    config_path = _write_provider_config(tmp_path, home)
    provider_root = home / "owners" / "providers" / "feishu" / "users" / "ou_123"
    local_root = home / "owners" / "local" / "main"
    append_raw_event(
        provider_root,
        RawMemoryEvent(
            event_id="provider-archive-1",
            session_id="provider-session",
            request_id="provider-request",
            run_id="provider-run",
            speaker="user",
            target="assistant",
            action="message",
            status="ok",
            task_id="provider-task",
            content_preview="provider handoff：继续这个飞书用户自己的任务。",
            source="run",
            created_at="2026-05-13T01:00:00+00:00",
        ),
    )
    append_raw_event(
        local_root,
        RawMemoryEvent(
            event_id="local-archive-should-not-leak",
            session_id="local-session",
            request_id="local-request",
            run_id="local-run",
            speaker="user",
            target="assistant",
            action="message",
            status="ok",
            task_id="local-task",
            content_preview="provider handoff：这是本地 CLI 主账号的同名内容，不能串给飞书用户。",
            source="run",
            created_at="2026-05-13T01:00:00+00:00",
        ),
    )

    code, payload = _run_cli_json(capsys, config_path, "memory-resume", "provider handoff")

    assert code == 0
    assert [item["id"] for item in payload["archive_matches"]] == ["provider-archive-1"]
    assert payload["archive_roots"] == [str(provider_root.resolve())]


def test_memory_resume_task_id_is_scoped_to_configured_provider_owner(tmp_path: Path, capsys):
    home = tmp_path / "home"
    config_path = _write_provider_config(tmp_path, home)
    provider_a = home / "owners" / "providers" / "feishu" / "users" / "ou_123"
    provider_b = home / "owners" / "providers" / "feishu" / "users" / "ou_456"
    paths_a = ensure_run_workspace(
        EnsureRunWorkspaceRequest(
            home=provider_a,
            template="tasks/{date}/{task_slug}",
            task_name="shared-task",
            user_prompt="provider a",
            request_id="req-a",
            run_id="run-a",
            task_id="shared-task",
            created_at="2026-05-13T01:00:00+00:00",
        )
    )
    ensure_run_workspace(
        EnsureRunWorkspaceRequest(
            home=provider_b,
            template="tasks/{date}/{task_slug}",
            task_name="shared-task",
            user_prompt="provider b",
            request_id="req-b",
            run_id="run-b",
            task_id="shared-task",
            created_at="2026-05-13T01:00:00+00:00",
        )
    )

    code, payload = _run_cli_json(capsys, config_path, "memory-resume", "--task-id", "shared-task")

    assert code == 0
    assert payload["task_fact_sources"][0]["state_path"] == str(paths_a.state_json)
    assert payload["task_fact_sources"][0]["run_id"] == "run-a"


def test_memory_doctor_reports_home_runtime_status(tmp_path: Path, capsys):
    home = tmp_path / "home"
    config_path = _write_config(tmp_path, home)

    code, payload = _run_cli_json(capsys, config_path, "memory-doctor")

    assert code == 0
    assert payload["home"]["root"] == str(home.resolve())
    assert payload["home"]["entry_files"]["memory_md"]["exists"] is True
    assert payload["home"]["directories"]["memory_daily"]["exists"] is True
    assert payload["home"]["owner"]["tasks"]["exists"] is True
    assert payload["home"]["directories"]["workspace_tasks"]["exists"] is False
    assert payload["routing"]["index"]["path"] == str(home.resolve() / "memory" / "routing" / "INDEX.md")
    assert payload["routing"]["route_count"] >= 1
    assert payload["ok"] is True


def test_memory_doctor_reports_v2_owner_shared_and_system_status(tmp_path: Path, capsys):
    home = tmp_path / "home"
    config_path = _write_config(tmp_path, home)

    code, payload = _run_cli_json(capsys, config_path, "memory-doctor")

    assert code == 0
    assert payload["home"]["owner"]["home_dir"]["exists"] is True
    assert payload["home"]["owner"]["daily_memory"]["exists"] is True
    assert payload["home"]["shared"]["skills"]["exists"] is True
    assert payload["home"]["system"]["schema_version"]["exists"] is True
    assert payload["home"]["schema"]["schema_version"] == "my-agent-home.v2"


def test_home_status_reports_corrupt_schema_version(tmp_path: Path):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.home_runtime_query import home_runtime_status

    home = ensure_my_agent_home(tmp_path)
    home.system_schema_version_json.write_text("{bad-json}\n", encoding="utf-8")

    status = home_runtime_status(home)

    assert status["schema"] == {}
    assert status["schema_load_error"]["context"] == "home_runtime_status.schema"
    assert status["schema_load_error"]["path"] == str(home.system_schema_version_json)


def test_home_status_reports_corrupt_owner_lifecycle(tmp_path: Path):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.home_runtime_query import home_runtime_status

    home = ensure_my_agent_home(tmp_path)
    status_path = home.owner_home_dir / "owner_status.json"
    status_path.write_text("{bad-json}\n", encoding="utf-8")

    status = home_runtime_status(home)

    lifecycle = status["owner"]["lifecycle"]
    assert lifecycle["status"] == "UNKNOWN"
    assert lifecycle["load_error"]["context"] == "home_runtime_status.owner_lifecycle"
    assert lifecycle["load_error"]["path"] == str(status_path)


def test_memory_doctor_reports_corrupt_owner_lifecycle(tmp_path: Path):
    from agent_py_agent.agent.user_space.home_doctor import build_home_doctor_report
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path)
    status_path = home.owner_home_dir / "owner_status.json"
    status_path.write_text("{bad-json}\n", encoding="utf-8")

    report = build_home_doctor_report(home)

    findings = [finding for finding in report["findings"] if finding["kind"] == "owner_lifecycle_load_error"]
    assert findings
    assert findings[0]["path"] == str(status_path)
    assert report["home"]["owner"]["lifecycle"]["status"] == "UNKNOWN"


def test_home_status_reports_current_owner_identity(tmp_path: Path, capsys):
    home = tmp_path / "home"
    config_path = _write_provider_config(tmp_path, home)

    code, payload = _run_cli_json(capsys, config_path, "home-status")

    assert code == 0
    assert payload["home"]["owner_identity"] == {
        "provider": "feishu",
        "owner_kind": "user",
        "owner_id": "providers/feishu/users/ou_123",
        "owner_home": str(home.resolve() / "owners" / "providers" / "feishu" / "users" / "ou_123"),
    }


def test_home_status_reports_provider_identity_indexes(tmp_path: Path):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.home_runtime_query import home_runtime_status
    from agent_py_agent.agent.user_space.identity_store import (
        ProviderIdentityRecord,
        link_provider_identity,
    )

    home = ensure_my_agent_home(tmp_path)
    link_provider_identity(
        home,
        ProviderIdentityRecord(provider="feishu", provider_subject_id="ou_1", owner_kind="user", owner_id="ou_1"),
    )

    status = home_runtime_status(home)

    assert status["identity"]["provider_index_files"] == 1
    assert status["identity"]["provider_identity"]["exists"] is True


def test_task_workspace_list_cli_shows_home_tasks(tmp_path: Path, capsys):
    home = tmp_path / "home"
    config_path = _write_config(tmp_path, home)
    agent = SimpleAgent(AgentConfig(my_agent_home=str(home), prompt_files=[]), tmp_path / "workspace")
    paths = ensure_run_workspace(
        EnsureRunWorkspaceRequest(
            home=agent.home_paths.root,
            template=agent.config.workspace_task_path_template,
            task_name="调试任务",
            user_prompt="调试",
            request_id="req-debug",
            run_id="run-debug",
            task_id="调试任务",
            created_at="2026-05-13T01:00:00+00:00",
        )
    )

    code, payload = _run_cli_json(capsys, config_path, "task-workspace-list", "--date", "2026-05-13")

    assert code == 0
    assert payload["tasks"][0]["root"] == str(paths.root)
    assert payload["tasks"][0]["state"]["run_id"] == "run-debug"


def test_run_workspace_creates_v2_task_ledgers(tmp_path: Path):
    home = tmp_path / "home"
    paths = ensure_run_workspace(
        EnsureRunWorkspaceRequest(
            home=home,
            template="tasks/{date}/{task_slug}",
            task_name="长任务分析",
            user_prompt="分析多个项目",
            request_id="req-v2",
            run_id="run-v2",
            task_id="task-v2",
            created_at="2026-05-13T01:00:00+00:00",
        )
    )

    assert paths.collab_blackboard_md.exists()
    assert paths.collab_messages_jsonl.exists()
    assert paths.collab_evidence_packets_dir.is_dir()
    assert paths.artifact_manifest_json.exists()
    assert paths.output_dir == home / "tasks" / "2026-05-13" / "task-v2" / "output"
    assert paths.work_dir == home / "tasks" / "2026-05-13" / "task-v2" / "work"
    assert paths.compact_dir.is_dir()
    assert paths.summaries_dir.is_dir()


def test_task_workspace_payload_uses_single_output_name(tmp_path: Path):
    home = tmp_path / "home"
    paths = ensure_run_workspace(
        EnsureRunWorkspaceRequest(
            home=home,
            template="tasks/{date}/{task_slug}",
            task_name="目录命名",
            user_prompt="检查目录命名",
            request_id="req-output",
            run_id="run-output",
            task_id="task-output",
            created_at="2026-05-13T01:00:00+00:00",
        )
    )

    items = list_task_workspaces(home, TaskWorkspaceQuery(date_key="2026-05-13"))

    assert items[0]["output_dir"] == str(paths.output_dir)
    assert "outputs_dir" not in items[0]
