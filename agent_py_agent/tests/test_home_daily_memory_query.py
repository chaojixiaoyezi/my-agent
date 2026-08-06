from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.memory_store.daily import DailyMemoryEvent, DailyMemoryStore
from agent_py_agent.agent.settings import AgentConfig
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


def _write_daily_record(
    home: Path,
    *,
    date_key: str = "2026-05-13",
    summary: str = "用户喜欢表格和干净目录",
) -> Path:
    path = home / "owners" / "local" / "main" / "memory" / "daily" / f"{date_key}.jsonl"
    DailyMemoryStore(path.parent).append(
        DailyMemoryEvent(
            event_type="conversation",
            summary=summary,
            actor="user",
            origin="user_explicit",
            session_id="session-daily-query",
            thread_id="thread-daily-query",
            created_at=f"{date_key}T12:00:00+00:00",
            extracted_at=f"{date_key}T12:01:00+00:00",
            curator_run_id="curator-run-daily-query",
        )
    )
    return path


def test_memory_search_does_not_treat_daily_as_formal_recall(tmp_path: Path):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    agent = SimpleAgent(AgentConfig(my_agent_home=str(home), memory_path="memory.jsonl", prompt_files=[]), repo)
    _write_daily_record(home)

    results = agent.recall("表格", top_k=3)

    assert results == []


def test_provider_memory_search_does_not_read_repo_memory_path(tmp_path: Path):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo_memory = repo / "memory.jsonl"
    repo_memory.parent.mkdir(parents=True, exist_ok=True)
    repo_memory.write_text(
        '{"role":"user","content":"repo local secret","kind":"note","tags":[],"created_at":1}\n',
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

    results = agent.recall("repo local secret", top_k=3)

    assert results == []


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
        "--actor",
        "user",
        "--event-type",
        "conversation",
    )

    assert code == 0
    assert payload["home"] == str(home.resolve())
    assert payload["records"][0]["summary"] == "用户喜欢表格和干净目录"
    assert payload["records"][0]["date"] == "2026-05-13"


def test_memory_daily_list_cli_reports_corrupt_daily_rows(tmp_path: Path, capsys):
    home = tmp_path / "home"
    config_path = _write_config(tmp_path, home)
    path = home / "owners" / "local" / "main" / "memory" / "daily" / "2026-05-13.jsonl"
    _write_daily_record(home, summary="good daily memory")
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{bad daily row}\n")

    code, payload = _run_cli_json(capsys, config_path, "memory-daily-list", "daily", "--date", "2026-05-13")

    assert code == 0
    assert [record["summary"] for record in payload["records"]] == ["good daily memory"]
    assert payload["load_errors"]
    assert payload["load_errors"][0]["context"] == "home_runtime_query.daily_memory"
    assert payload["load_errors"][0]["path"] == str(path)
    assert payload["load_errors"][0]["line"] == 2


def test_memory_daily_list_ignores_legacy_root_daily_memory(tmp_path: Path, capsys):
    home = tmp_path / "home"
    config_path = _write_config(tmp_path, home)
    _write_daily_record(home)
    legacy_daily = home / "memory" / "daily" / "2026-05-13.jsonl"
    legacy_daily.parent.mkdir(parents=True)
    legacy_daily.write_text('{"role":"user","kind":"note","content":"legacy root daily"}\n', encoding="utf-8")

    code, payload = _run_cli_json(
        capsys,
        config_path,
        "memory-daily-list",
        "legacy root",
        "--date",
        "2026-05-13",
    )

    assert code == 0
    assert payload["records"] == []


def test_memory_daily_list_uses_configured_provider_owner(tmp_path: Path, capsys):
    home = tmp_path / "home"
    config_path = _write_provider_config(tmp_path, home)
    provider_daily = home / "owners" / "providers" / "feishu" / "users" / "ou_123" / "memory" / "daily" / "2026-05-13.jsonl"
    local_daily = home / "owners" / "local" / "main" / "memory" / "daily" / "2026-05-13.jsonl"
    DailyMemoryStore(provider_daily.parent).append(
        DailyMemoryEvent(
            event_type="summary",
            summary="provider only",
            actor="main_agent",
            created_at="2026-05-13T12:00:00+00:00",
            extracted_at="2026-05-13T12:01:00+00:00",
            curator_run_id="curator-provider",
        )
    )
    DailyMemoryStore(local_daily.parent).append(
        DailyMemoryEvent(
            event_type="summary",
            summary="local only",
            actor="main_agent",
            created_at="2026-05-13T12:00:00+00:00",
            extracted_at="2026-05-13T12:01:00+00:00",
            curator_run_id="curator-local",
        )
    )

    code, payload = _run_cli_json(capsys, config_path, "memory-daily-list", "only", "--date", "2026-05-13")

    assert code == 0
    assert [record["summary"] for record in payload["records"]] == ["provider only"]
