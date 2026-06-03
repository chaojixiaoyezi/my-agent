from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.__main__ import build_parser


def _write_config(tmp_path: Path, extra: str = "") -> Path:
    config_path = tmp_path / "agent_config.yaml"
    config_path.write_text(
        'workspace_root: "workspace"\n'
        'model_backend: "echo"\n'
        f"{extra}",
        encoding="utf-8",
    )
    return config_path


def _workspace(config_path: Path) -> Path:
    return config_path.parent / "workspace"


def _write_route_index(root: Path) -> Path:
    authority = root / "references" / "memory" / "routing.md"
    authority.parent.mkdir(parents=True, exist_ok=True)
    authority.write_text("长期规则索引说明。", encoding="utf-8")
    index = root / "memory" / "routing" / "INDEX.md"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(
        """# Memory Routes

## memory.routing
topic: 长期规则索引
trigger_keywords: 长期规则, 规则索引
aliases: memory index, 规则导航
when_to_read: 用户讨论 memory index 或长期规则路由时读取
authority_path: references/memory/routing.md
scope: global
priority: 30
stale_check: monthly
last_verified_at: 2026-04-30
""",
        encoding="utf-8",
    )
    return index


def _run_cli_json(capsys, config_path: Path, *argv: str) -> tuple[int, dict]:
    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), *argv, "--json"])
    code = args.func(args)
    captured = capsys.readouterr()
    return code, json.loads(captured.out)


def test_memory_route_missing_index_does_not_crash(tmp_path, capsys):
    config_path = _write_config(tmp_path)

    code, payload = _run_cli_json(
        capsys,
        config_path,
        "memory-route",
        "memory index",
        "--index",
        "memory/routing/NOTFOUND.md",
    )

    assert code == 0
    assert payload["ok"] is False
    assert payload["index"]["exists"] is False
    assert payload["matches"] == []
    assert "not found" in payload["diagnostics"]["messages"][0]


def test_memory_doctor_json_contains_warnings_and_routes(tmp_path, capsys):
    config_path = _write_config(
        tmp_path,
        "memory_archive_level: bad\n"
        "memory_rule_routing_mode: strict; rm -rf /\n",
    )
    root = _workspace(config_path)
    _write_route_index(root)
    hook_dir = root / "memory" / "hooks"
    raw_dir = root / "audit"
    hook_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    (hook_dir / "2026-04-30.jsonl").write_text("{}\n", encoding="utf-8")
    (raw_dir / "2026-04-30.jsonl").write_text("{}\n", encoding="utf-8")

    code, payload = _run_cli_json(capsys, config_path, "memory-doctor")

    assert code == 0
    assert {item["field_name"] for item in payload["warnings"]} == {
        "memory_archive_level",
        "memory_rule_routing_mode",
    }
    assert payload["routing"]["route_count"] == 1
    assert payload["routing"]["routes"][0]["route_id"] == "memory.routing"
    assert payload["archive"]["hook"]["file_count"] == 1
    assert payload["archive"]["raw"]["file_count"] == 1


def test_memory_route_json_matches_one_rule(tmp_path, capsys):
    config_path = _write_config(tmp_path)
    _write_route_index(_workspace(config_path))

    code, payload = _run_cli_json(capsys, config_path, "memory-route", "以后 memory index 需要代码路由")

    assert code == 0
    assert payload["ok"] is True
    assert [match["route_id"] for match in payload["matches"]] == ["memory.routing"]
    assert payload["candidate_paths"] == ["references/memory/routing.md"]
    assert payload["required_read_paths"] == []
