from __future__ import annotations

"""collect_curator_evidence.py 的聚焦测试(双 ACK 冻结合同 seq1379/1382/1385/1389)。

覆盖: 默认生产 home/子路径/祖先/symlink 拒绝、output 越界(symlink/落入 home)拒绝、
无网络无执行、正文与秘密不泄漏、缺失/坏 JSONL fail-closed、reason 聚合与 cursor/lease
汇总、原子输出无半文件、重复运行确定性、fixture smoke。全部数据在临时目录构造。
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PKG_ROOT / "scripts" / "b_acceptance" / "collect_curator_evidence.py"

_SENTINEL_BODY = "这是绝不能泄漏的正文内容SECRET-7f3a"
_SENTINEL_LONG = "x" * 300


def _load_module():
    spec = importlib.util.spec_from_file_location("collect_curator_evidence", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["collect_curator_evidence"] = module
    spec.loader.exec_module(module)
    return module


MOD = _load_module()
CliError = MOD.CliError
collect_evidence = MOD.collect_evidence


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _write_state(home: Path, **overrides: object) -> None:
    state = {
        "lease_id": "lease-1",
        "run_id": "run-1",
        "reason": "interval",
        "pending_reasons": [],
        "pending_requested_at": "2026-08-12T09:33:38Z",
        "per_thread_cursors": {"thread-1": "msg-38e"},
        "last_processed_audit_event_id": "evt-9",
        "processed_messages": 10,
        "processed_audit_events": 7,
        "candidate_count": 2,
        "daily_event_count": 3,
        "last_daily_finalize_date": "2026-08-12",
        "committed_at": "2026-08-12T09:38:55Z",
    }
    state.update(overrides)
    path = home / "memory" / "curator" / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def _make_fixture_home(home: Path) -> None:
    """构造贴近真实的隔离 home(10 文件齐全)。"""
    _write_state(home)
    _write_jsonl(
        home / "memory" / "curator" / "runs" / "2026-08-12.jsonl",
        [
            {
                "run_id": "run-1",
                "lease_id": "lease-1",
                "status": "succeeded",
                "reason": "interval",
                "provider": "opencode",
                "model": "deepseek-v4-flash",
                "started_at": "2026-08-12T09:33:40Z",
                "finished_at": "2026-08-12T09:38:55Z",
                "phase": "final",
                "processed_messages": 10,
                "processed_audit_events": 7,
                "daily_events": 3,
                "candidates": 2,
                "promoted": 1,
                "failure_code": "",
                "warnings": [],
                "cursor_before": {"audit": "evt-2", "thread-1": "msg-1"},
                "cursor_after": {"audit": "evt-9", "thread-1": "msg-38e"},
                "recovery": {},
                "schema_version": "curator-run/1",
                "record_id": "rec-1",
            },
            {
                "run_id": "run-2",
                "lease_id": "lease-2",
                "status": "failed",
                "reason": "interval",
                "provider": "opencode",
                "model": "deepseek-v4-flash",
                "started_at": "2026-08-12T09:40:00Z",
                "finished_at": "2026-08-12T09:40:05Z",
                "phase": "final",
                "processed_messages": 0,
                "processed_audit_events": 0,
                "daily_events": 0,
                "candidates": 0,
                "promoted": 0,
                "failure_code": "CURATOR_COMMIT_FAILED",
                "warnings": [],
                "cursor_before": {},
                "cursor_after": {},
                "recovery": {},
                "schema_version": "curator-run/1",
                "record_id": "rec-2",
            },
            {
                "run_id": "run-3",
                "lease_id": "lease-3",
                "status": "succeeded",
                "reason": "turn_threshold",
                "provider": "opencode",
                "model": "deepseek-v4-flash",
                "started_at": "2026-08-12T10:00:00Z",
                "finished_at": "2026-08-12T10:00:30Z",
                "phase": "final",
                "processed_messages": 4,
                "processed_audit_events": 2,
                "daily_events": 0,
                "candidates": 1,
                "promoted": 1,
                "failure_code": "",
                "warnings": [],
                "cursor_before": {"audit": "evt-9"},
                "cursor_after": {"audit": "evt-11"},
                "recovery": {},
                "schema_version": "curator-run/1",
                "record_id": "rec-3",
            },
        ],
    )
    _write_jsonl(
        home / "memory" / "daily" / "2026-08-12.jsonl",
        [
            {"daily_event_id": "d-1", "kind": "summary"},
            {"daily_event_id": "d-2", "kind": "summary"},
            {"daily_event_id": "d-3", "kind": "summary"},
        ],
    )
    _write_jsonl(
        home / "memory" / "candidates.jsonl",
        [
            {
                "candidate_id": "cand-1",
                "status": "promoted",
                "scope": {"owner": "user-1", "scope": "personal"},
                "promotion_mode": "auto_eligible",
                "proposed_action": "add",
                "promotion_target": "long_term",
                "origin": "user_explicit",
                "confidence": 0.99,
                "content": _SENTINEL_BODY,
                "source_message_refs": [{"message_id": "msg-38e", "quote": _SENTINEL_BODY}],
            },
            {
                "candidate_id": "cand-2",
                "status": "pending",
                "scope": {"owner": "user-1", "scope": "personal"},
                "promotion_mode": "manual_required",
                "proposed_action": "add",
                "promotion_target": "long_term",
                "origin": "model_inferred",
                "confidence": 0.6,
                "content": _SENTINEL_BODY,
            },
        ],
    )
    long_term = home / "memory" / "long_term" / "memory.jsonl"
    long_term.parent.mkdir(parents=True, exist_ok=True)
    long_term.write_text(
        json.dumps({"entry_id": "lt-1", "content": _SENTINEL_BODY, "kind": "fact"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    lessons = home / "memory" / "lessons"
    lessons.mkdir(parents=True, exist_ok=True)
    (lessons / "lesson-1.md").write_text("# lesson\n", encoding="utf-8")
    _write_jsonl(
        home / "audit" / "events.jsonl",
        [
            {"event_id": "evt-1", "type": "memory_curator.run"},
            {"event_id": "evt-9", "type": "memory_curator.run"},
            {"event_id": "evt-11", "type": "memory_curator.run"},
        ],
    )


def _run(home: Path, output: Path) -> dict[str, object]:
    # 测试夹具负责搭好父目录(产品 CLI 的"父目录须已存在"由 test_rejects_missing_output_parent 单独覆盖)。
    output.parent.mkdir(parents=True, exist_ok=True)
    return collect_evidence(home, output)


# ---------------------------------------------------------------- 路径隔离

def test_rejects_default_production_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(MOD, "DEFAULT_PRODUCTION_HOMES", ("/root/.my-agent",))
    with pytest.raises(CliError):
        _run(Path("/root/.my-agent"), Path("/tmp/out.json"))


def test_rejects_expanded_user_production_home(tmp_path: Path) -> None:
    with pytest.raises(CliError):
        _run(Path.home() / ".my-agent", tmp_path / "out.json")


def test_rejects_production_home_subpath(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(MOD, "DEFAULT_PRODUCTION_HOMES", ("/root/.my-agent",))
    with pytest.raises(CliError):
        _run(Path("/root/.my-agent/user1"), Path("/tmp/out.json"))


def test_rejects_home_ancestor_of_production(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prod = tmp_path / "prod" / ".my-agent"
    monkeypatch.setattr(MOD, "DEFAULT_PRODUCTION_HOMES", (str(prod),))
    with pytest.raises(CliError):
        _run(tmp_path, tmp_path / "out.json")


def test_rejects_symlink_home_into_production(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prod = tmp_path / "prod"
    prod.mkdir()
    (prod / ".my-agent").mkdir()
    link = tmp_path / "link-home"
    link.symlink_to(prod / ".my-agent")
    monkeypatch.setattr(MOD, "DEFAULT_PRODUCTION_HOMES", (str(prod / ".my-agent"),))
    with pytest.raises(CliError):
        _run(link, tmp_path / "out.json")


def test_rejects_output_inside_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    # --output 位于 --home 内 → 拒绝
    with pytest.raises(CliError):
        _run(home, home / "index.json")


def test_rejects_output_parent_symlink_escape(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    inside = home / "inside"
    inside.mkdir()
    out_dir = tmp_path / "out-link"
    out_dir.symlink_to(inside, target_is_directory=True)
    with pytest.raises(CliError):
        _run(home, out_dir / "index.json")


def test_rejects_output_file_symlink_escape(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    victim = home / "victim.json"
    victim.write_text("{}", encoding="utf-8")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    out_file = out_dir / "index.json"
    out_file.symlink_to(victim)
    with pytest.raises(CliError):
        _run(home, out_file)


def test_rejects_missing_output_parent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    with pytest.raises(CliError):
        collect_evidence(home, tmp_path / "not-there" / "index.json")


def test_rejects_missing_home(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        _run(tmp_path / "absent", tmp_path / "out.json")


# ---------------------------------------------------------------- 汇总正确性

def test_summary_aggregates_by_reason(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _make_fixture_home(home)
    output = tmp_path / "out" / "index.json"
    result = _run(home, output)

    summary = result["summary"]
    by_reason = summary["runs_by_reason"]
    assert by_reason["interval"]["runs"] == 2
    assert by_reason["interval"]["status_counts"] == {"succeeded": 1, "failed": 1}
    assert by_reason["interval"]["failure_codes"] == {"CURATOR_COMMIT_FAILED": 1}
    assert by_reason["interval"]["providers"] == ["opencode"]
    assert by_reason["interval"]["models"] == ["deepseek-v4-flash"]
    assert by_reason["interval"]["processed_messages_total"] == 10
    assert by_reason["turn_threshold"]["runs"] == 1
    assert by_reason["turn_threshold"]["status_counts"] == {"succeeded": 1}

    state = summary["state"]
    assert state["present"] is True
    assert state["lease_id"] == "lease-1"
    assert state["per_thread_cursors"] == ["thread-1=msg-38e"]
    assert state["last_processed_audit_event_id"] == "evt-9"

    assert summary["daily"] == {"present": True, "count": 3}
    assert summary["candidates"]["count"] == 2
    assert summary["candidates"]["by_status"] == {"promoted": 1, "pending": 1}
    assert summary["long_term"]["count"] == 1
    assert summary["lessons"]["count"] == 1
    assert summary["audit"]["events"] == 3
    assert summary["audit"]["cursor_found"] is True
    assert result["incomplete"] == []


def test_bad_jsonl_marks_parse_error_and_continues(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _make_fixture_home(home)
    runs = home / "memory" / "curator" / "runs" / "2026-08-13.jsonl"
    runs.write_text("{broken json\n" + '{"run_id":"r-x","status":"succeeded","reason":"daily",'
                    '"provider":"p","model":"m","phase":"final","processed_messages":0,'
                    '"processed_audit_events":0,"daily_events":0,"candidates":0,"promoted":0,'
                    '"failure_code":"","warnings":[],"cursor_before":{},"cursor_after":{},'
                    '"recovery":{},"schema_version":"curator-run/1","record_id":"r-x","lease_id":"l-x"}\n',
                    encoding="utf-8")
    result = _run(home, tmp_path / "out" / "index.json")
    assert result["inputs"]["runs"]["status"] == "parse_error"
    assert "runs" in result["incomplete"]
    # 好行仍被汇总: daily reason 出现。
    assert result["summary"]["runs_by_reason"]["daily"]["runs"] == 1


def test_empty_dir_reported_truthfully(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    result = _run(home, tmp_path / "out" / "index.json")
    assert result["inputs"]["state"]["status"] == "missing"
    assert result["inputs"]["runs"]["status"] == "missing"
    assert result["summary"]["state"]["present"] is False
    assert result["summary"]["candidates"]["present"] is False
    assert result["summary"]["daily"] == {"present": False, "count": 0}
    assert sorted(result["incomplete"]) == [
        "audit",
        "candidates",
        "daily",
        "lessons",
        "long_term",
        "runs",
        "state",
    ]


def test_empty_jsonl_dir_is_empty_not_missing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "memory" / "curator" / "runs").mkdir(parents=True)
    result = _run(home, tmp_path / "out" / "index.json")
    assert result["inputs"]["runs"]["status"] == "empty"
    assert result["inputs"]["runs"]["files"] == 0


# ---------------------------------------------------------------- 脱敏

def test_no_content_or_secret_leak(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _make_fixture_home(home)
    output = tmp_path / "out" / "index.json"
    result = _run(home, output)
    dumped = json.dumps(result, ensure_ascii=False)
    assert _SENTINEL_BODY not in dumped
    assert _SENTINEL_LONG not in dumped
    raw = output.read_text(encoding="utf-8")
    assert _SENTINEL_BODY not in raw


def test_cursor_long_values_dropped(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _write_state(home, per_thread_cursors={"thread-1": _SENTINEL_LONG})
    result = _run(home, tmp_path / "out" / "index.json")
    cursors = result["summary"]["state"]["per_thread_cursors"]
    assert cursors == []
    assert _SENTINEL_LONG not in json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------- 原子写与确定性

def test_atomic_write_leaves_no_partial_on_failure(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    output = tmp_path / "out" / "index.json"
    with pytest.raises(CliError):
        collect_evidence(home, output)  # 父目录不存在 → 合同拒绝, 不得写任何文件
    assert not output.exists()
    assert not (tmp_path / "out").exists()


def test_deterministic_across_runs(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _make_fixture_home(home)
    first = _run(home, tmp_path / "out" / "a.json")
    second = _run(home, tmp_path / "out" / "b.json")
    assert first == second
    assert (tmp_path / "out" / "a.json").read_bytes() == (tmp_path / "out" / "b.json").read_bytes()


def test_no_temp_files_left_after_success(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _make_fixture_home(home)
    _run(home, tmp_path / "out" / "index.json")
    leftovers = list((tmp_path / "out").glob(".collect-*.tmp"))
    assert leftovers == []


# ---------------------------------------------------------------- 静态安全

def test_no_network_or_subprocess_primitives() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    for banned in ("subprocess", "socket", "urllib", "requests", "http.client"):
        assert banned not in source, f"脚本禁止使用 {banned}"


def test_no_import_of_tested_home_code() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "importlib" not in source
    assert "exec(" not in source


# ---------------------------------------------------------------- 输出契约

def test_output_is_index_not_verdict(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _make_fixture_home(home)
    result = _run(home, tmp_path / "out" / "index.json")
    # 顶层不出现裁决键(状态枚举如 failed/succeeded 属证据事实, 允许)。
    top = set(result.keys())
    assert not (top & {"verdict", "passed", "accepted", "rejected"})
    assert result["summary"].get("verdict") is None
    assert "note" in result
    assert "不替代" in str(result["note"])
