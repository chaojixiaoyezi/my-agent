from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.__main__ import build_parser
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
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
    assert paths.outputs_dir == home / "tasks" / "2026-05-13" / "task-v2" / "output"
    assert paths.work_dir == home / "tasks" / "2026-05-13" / "task-v2" / "work"
    assert paths.compact_dir.is_dir()
    assert paths.summaries_dir.is_dir()
