from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.__main__ import build_parser
from agent_py_agent.agent.agent_core._runtime_params import ArchiveRunParams
from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
    write_run_task_workspace_if_needed,
)
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.memory_archive import RawMemoryEvent, append_raw_event
from agent_py_agent.agent.user_space.home_runtime_query import (
    TaskWorkspaceQuery,
    list_task_workspaces,
)
from agent_py_agent.agent.user_space.run_workspace import (
    EnsureRunWorkspaceRequest,
    ensure_run_workspace,
)

# LLM: home runtime query tests prove newly written home files are also queryable/resumable.
# 模块用途: 验证 daily memory、task workspace、doctor 和 CLI 调试入口的读取侧迁移。


# LLM: _write_config creates an isolated CLI profile with explicit home and workspace roots.
# 函数用途: 写入测试专用配置，让 CLI 命令使用 tmp_path 下的 my_agent_home。
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


# LLM: _run_cli_json keeps CLI assertions focused on payload shape.
# 函数用途: 执行 CLI 子命令并解析 JSON 输出。
def _run_cli_json(capsys, config_path: Path, *argv: str) -> tuple[int, dict]:
    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), *argv, "--json"])
    code = args.func(args)
    captured = capsys.readouterr()
    return code, json.loads(captured.out)


# LLM: _write_daily_record creates one owner daily ledger line without writing legacy memory.
# 函数用途: 向 V2 owner memory/daily 写入一条测试记忆记录。
def _write_daily_record(home: Path, *, date_key: str = "2026-05-13") -> Path:
    path = home / "owners" / "local" / "main" / "memory" / "daily" / f"{date_key}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "role": "user",
        "content": "用户喜欢表格和干净目录",
        "kind": "preference",
        "tags": ["ui"],
        "created_at": 1778640000.0,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


# LLM: memory search should read the new daily ledger even if old memory.jsonl is absent.
# 函数用途: 验证 JsonlMemory.search 会从 home daily mirror 回退读取记录。
def test_memory_search_reads_daily_when_legacy_memory_missing(tmp_path: Path):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    agent = SimpleAgent(AgentConfig(my_agent_home=str(home), memory_path="memory.jsonl", prompt_files=[]), repo)
    _write_daily_record(home)

    results = agent.recall("表格", top_k=3)

    assert [record.content for record in results] == ["用户喜欢表格和干净目录"]


# LLM: provider owners must not inherit the CLI/local legacy memory file.
# 函数用途: 验证外部用户主代理不会默认读取本地 CLI 主账号旧 memory_path。
def test_provider_memory_search_does_not_read_legacy_memory_path(tmp_path: Path):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    legacy = repo / "memory.jsonl"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        '{"role":"user","content":"local legacy secret","kind":"note","tags":[],"created_at":1}\n',
        encoding="utf-8",
    )
    agent = SimpleAgent(
        AgentConfig(
            my_agent_home=str(home),
            memory_path="memory.jsonl",
            prompt_files=[],
            my_agent_owner_provider="feishu",
            my_agent_owner_kind="user",
            my_agent_owner_id="ou_123",
        ),
        repo,
    )

    results = agent.recall("legacy secret", top_k=3)

    assert results == []


# LLM: memory-daily-list gives humans and frontend a small direct view into daily memory.
# 函数用途: 验证 CLI 能按日期、角色、类型和关键词筛选 daily 记忆。
def test_memory_daily_list_cli_filters_home_daily_records(tmp_path: Path, capsys):
    home = tmp_path / "home"
    config_path = _write_config(tmp_path, home)
    _write_daily_record(home)

    code, payload = _run_cli_json(
        capsys,
        config_path,
        "memory-daily-list",
        "表格",
        "--date",
        "2026-05-13",
        "--role",
        "user",
        "--kind",
        "preference",
    )

    assert code == 0
    assert payload["home"] == str(home.resolve())
    assert payload["records"][0]["content"] == "用户喜欢表格和干净目录"
    assert payload["records"][0]["date"] == "2026-05-13"


# LLM: CLI owner config should route daily queries to that provider owner's private daily ledger.
# 函数用途: 验证 provider owner 配置下，memory-daily-list 不读取 local/main 的私有记忆。
def test_memory_daily_list_uses_configured_provider_owner(tmp_path: Path, capsys):
    home = tmp_path / "home"
    config_path = _write_provider_config(tmp_path, home)
    provider_daily = home / "owners" / "providers" / "feishu" / "users" / "ou_123" / "memory" / "daily" / "2026-05-13.jsonl"
    local_daily = home / "owners" / "local" / "main" / "memory" / "daily" / "2026-05-13.jsonl"
    provider_daily.parent.mkdir(parents=True, exist_ok=True)
    local_daily.parent.mkdir(parents=True, exist_ok=True)
    provider_daily.write_text('{"role":"user","kind":"note","content":"provider only"}\n', encoding="utf-8")
    local_daily.write_text('{"role":"user","kind":"note","content":"local only"}\n', encoding="utf-8")

    code, payload = _run_cli_json(capsys, config_path, "memory-daily-list", "only", "--date", "2026-05-13")

    assert code == 0
    assert [record["content"] for record in payload["records"]] == ["provider only"]


# LLM: provider owner task listings stay inside that owner's task ledger.
# 函数用途: 验证外部用户主代理不会从 local/main 或旧顶层 tasks 看到别人的任务目录。
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
            task_name="legacy-task",
            user_prompt="legacy",
            request_id="req-legacy",
            run_id="run-legacy",
            task_id="legacy-task",
            created_at="2026-05-13T01:00:00+00:00",
        )
    )

    code, payload = _run_cli_json(capsys, config_path, "task-workspace-list", "--date", "2026-05-13")

    assert code == 0
    assert [task["state"]["task_id"] for task in payload["tasks"]] == ["provider-task"]
    assert payload["tasks"][0]["root"] == str(provider_paths.root)


# LLM: owner tool policy is per-owner, not a global process setting.
# 函数用途: 验证不同用户主代理各自读取自己的 tool_policy，不互相污染工具权限。
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


# LLM: memory-resume should use home task workspace when the task is not a legacy subagent.
# 函数用途: 验证 memory-resume 能从 home tasks 读取主代理任务工作区状态。
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


# LLM: saved root runs should now use the owner home even for the local/main owner.
# 函数用途: 验证主账号新写入任务工作区不再回落到旧顶层 tasks，旧路径只作为读取兼容存在。
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


# LLM: provider memory-resume must recover from the provider owner's archive, not local/main legacy archives.
# 函数用途: 验证外部用户主代理恢复时只读取自己的 owner-home raw archive，避免跨用户串记忆。
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


# LLM: same task ids under different provider owners must resolve to the current owner only.
# 函数用途: 验证跨用户同名任务恢复不会串到另一个 provider user 的任务工作区。
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


# LLM: memory-doctor should report whether the home runtime files and dirs exist.
# 函数用途: 验证 memory-doctor 的 JSON 输出包含 home runtime 健康状态。
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


# LLM: memory doctor should expose V2 owner/shared/system health without auto-migrating anything.
# 函数用途: 验证 home runtime status 能报告 V2 owner home、公共能力层和 schema 文件状态。
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


# LLM: home-status should make the current owner identity visible without opening policy files.
# 函数用途: 验证 CLI/前端一眼能看到当前 owner 是谁以及 owner home 在哪。
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


# LLM: home status should expose identity index health without scanning provider bodies.
# 函数用途: 验证 doctor/status 能看到 provider identity 分片数量，方便后续恢复入口查问题。
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


# LLM: memory doctor should surface legacy data as migration advice, not as a runtime blocker.
# 函数用途: 验证旧 memory/workspace 有数据时，doctor 给出迁移提示但不改变退出码。
def test_memory_doctor_reports_legacy_migration_advice(tmp_path: Path, capsys):
    home = tmp_path / "home"
    config_path = _write_config(tmp_path, home)
    legacy_daily = home / "memory" / "daily" / "2026-05-13.jsonl"
    legacy_daily.parent.mkdir(parents=True, exist_ok=True)
    legacy_daily.write_text('{"content":"legacy"}\n', encoding="utf-8")

    code, payload = _run_cli_json(capsys, config_path, "memory-doctor")

    assert code == 0
    assert payload["home"]["migration"]["legacy_daily_memory"]["record_count"] == 1
    assert payload["home_doctor"]["migration"]["pending_count"] >= 1
    assert payload["home"]["migration"]["legacy_daily_memory"]["target"] == str(
        home.resolve() / "owners" / "local" / "main" / "memory" / "daily"
    )


# LLM: home-migrate should copy legacy ledgers into owner home without deleting old files.
# 函数用途: 验证 home-migrate --apply 是非破坏性复制，目标存在时不覆盖。
def test_home_migrate_apply_copies_legacy_daily_to_owner_home(tmp_path: Path, capsys):
    home = tmp_path / "home"
    config_path = _write_config(tmp_path, home)
    legacy_daily = home / "memory" / "daily" / "2026-05-13.jsonl"
    legacy_daily.parent.mkdir(parents=True, exist_ok=True)
    legacy_daily.write_text('{"content":"legacy"}\n', encoding="utf-8")

    code, payload = _run_cli_json(capsys, config_path, "home-migrate", "--apply")

    target = home.resolve() / "owners" / "local" / "main" / "memory" / "daily" / "2026-05-13.jsonl"
    assert code == 0
    assert payload["migration"]["applied"] is True
    assert target.read_text(encoding="utf-8") == legacy_daily.read_text(encoding="utf-8")
    assert legacy_daily.exists()


# LLM: task-workspace-list is the direct debug surface for owner task folders.
# 函数用途: 验证 CLI 能列出 home task workspace，并展示 work/state、work/timeline 和 output 引用。
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


# LLM: task workspaces need the V2 collab/artifact/compact folders so agents share one predictable ledger.
# 函数用途: 验证创建任务工作区时同步创建协作白板、产物登记表和 compact 分区。
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
