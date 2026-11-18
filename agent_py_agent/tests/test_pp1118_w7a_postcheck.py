"""W7a 定时任务产物后验契约与纯只读评估器单元测试。"""

from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path

import pytest

from agent_py_agent.agent.scheduler.postcheck import (
    POSTCHECK_CHECK_CODES,
    POSTCHECK_SCHEMA,
    evaluate_postcheck,
    normalize_postcheck,
)
from agent_py_agent.agent.scheduler.schedule import ScheduleValidationError
from agent_py_agent.agent.tooling.input_schema import (
    canonicalize_tool_input_schema,
    validate_tool_input,
)


def _write(path: Path, data: str | bytes, *, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _assert_all_schema_properties_described(node: object, path: str = "$") -> None:
    if not isinstance(node, dict):
        return
    props = node.get("properties")
    if isinstance(props, dict):
        for name, child in props.items():
            assert isinstance(child, dict), f"{path}.{name} 必须是 schema 对象"
            desc = child.get("description")
            assert isinstance(desc, str) and desc.strip(), f"{path}.{name} 缺少 description"
            _assert_all_schema_properties_described(child, f"{path}.{name}")
    items = node.get("items")
    if isinstance(items, dict):
        _assert_all_schema_properties_described(items, f"{path}.items")


def test_postcheck_schema_is_valid_and_described() -> None:
    wrapper = canonicalize_tool_input_schema(
        {
            "type": "object",
            "properties": {"postcheck": POSTCHECK_SCHEMA},
            "additionalProperties": False,
        }
    )
    _assert_all_schema_properties_described(wrapper)
    assert validate_tool_input({"postcheck": None}, wrapper).ok
    assert validate_tool_input(
        {
            "postcheck": {
                "artifacts": [
                    {
                        "path": "out/*.json",
                        "min_bytes": 0,
                        "min_lines": 1,
                        "max_age_seconds": 60,
                        "json_pointer": "/status",
                        "equals": "complete",
                    }
                ]
            }
        },
        wrapper,
    ).ok
    assert not validate_tool_input({"postcheck": {"artifacts": []}}, wrapper).ok
    assert not validate_tool_input(
        {"postcheck": {"artifacts": [{"path": "a.txt", "extra": 1}]}},
        wrapper,
    ).ok


def test_postcheck_module_uses_stdlib_and_schedule_error_only() -> None:
    source = Path(__file__).resolve().parents[1] / "agent" / "scheduler" / "postcheck.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                assert top in sys.stdlib_module_names, f"不允许非标准库导入: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                assert node.level == 1 and node.module == "schedule"
                assert [alias.name for alias in node.names] == ["ScheduleValidationError"]
            else:
                top = (node.module or "").split(".")[0]
                assert top in sys.stdlib_module_names, f"不允许非标准库导入: {node.module}"


def test_normalize_postcheck_preserves_zero_false_null_and_empty_pointer() -> None:
    assert normalize_postcheck(None) is None
    raw = {
        "artifacts": [
            {"path": "summary.json", "json_pointer": "", "equals": None},
            {"path": "flags.json", "json_pointer": "/ok", "equals": False},
            {"path": "rows.json", "json_pointer": "/rows", "equals": 0, "min_value": 0},
            {"path": "out/*.csv", "min_bytes": 0, "min_lines": 0, "max_age_seconds": 1},
        ]
    }
    norm = normalize_postcheck(raw)
    assert norm is not None
    assert norm["artifacts"][0] == {"path": "summary.json", "json_pointer": "", "equals": None}
    assert norm["artifacts"][1] == {"path": "flags.json", "json_pointer": "/ok", "equals": False}
    assert norm["artifacts"][2] == {
        "path": "rows.json",
        "json_pointer": "/rows",
        "equals": 0,
        "min_value": 0,
    }
    assert norm["artifacts"][3] == {
        "path": "out/*.csv",
        "min_bytes": 0,
        "min_lines": 0,
        "max_age_seconds": 1,
    }


@pytest.mark.parametrize(
    ("payload", "expected_field"),
    [
        ("bad", "postcheck"),
        ({}, "postcheck.artifacts"),
        ({"artifacts": [], "extra": 1}, "postcheck.extra"),
        ({"artifacts": []}, "postcheck.artifacts"),
        ({"artifacts": [{"path": f"f{i}.txt", "min_bytes": 1} for i in range(9)]}, "postcheck.artifacts"),
        ({"artifacts": ["not-dict"]}, "postcheck.artifacts[0]"),
        ({"artifacts": [{"path": "a.txt", "min_bytes": 1, "unknown_k": 1}]}, "postcheck.artifacts[0].unknown_k"),
        ({"artifacts": [{"path": "a.txt"}]}, "postcheck.artifacts[0]"),
        ({"artifacts": [{"path": "", "min_bytes": 1}]}, "postcheck.artifacts[0].path"),
        ({"artifacts": [{"path": " " * 3, "min_bytes": 1}]}, "postcheck.artifacts[0].path"),
        ({"artifacts": [{"path": "a" * 513, "min_bytes": 1}]}, "postcheck.artifacts[0].path"),
        ({"artifacts": [{"path": "/etc/passwd", "min_bytes": 1}]}, "postcheck.artifacts[0].path"),
        ({"artifacts": [{"path": "C:/temp/a.txt", "min_bytes": 1}]}, "postcheck.artifacts[0].path"),
        ({"artifacts": [{"path": "out\\a.txt", "min_bytes": 1}]}, "postcheck.artifacts[0].path"),
        ({"artifacts": [{"path": "../secret.txt", "min_bytes": 1}]}, "postcheck.artifacts[0].path"),
        ({"artifacts": [{"path": "a/../b.txt", "min_bytes": 1}]}, "postcheck.artifacts[0].path"),
        ({"artifacts": [{"path": ".", "min_bytes": 1}]}, "postcheck.artifacts[0].path"),
        ({"artifacts": [{"path": "out/", "min_bytes": 1}]}, "postcheck.artifacts[0].path"),
        ({"artifacts": [{"path": "dir*/file.txt", "min_bytes": 1}]}, "postcheck.artifacts[0].path"),
        ({"artifacts": [{"path": "a.txt", "min_bytes": True}]}, "postcheck.artifacts[0].min_bytes"),
        ({"artifacts": [{"path": "a.txt", "min_bytes": -1}]}, "postcheck.artifacts[0].min_bytes"),
        ({"artifacts": [{"path": "a.txt", "min_bytes": 1.5}]}, "postcheck.artifacts[0].min_bytes"),
        ({"artifacts": [{"path": "a.txt", "min_bytes": None}]}, "postcheck.artifacts[0].min_bytes"),
        ({"artifacts": [{"path": "a.txt", "min_lines": False}]}, "postcheck.artifacts[0].min_lines"),
        ({"artifacts": [{"path": "a.txt", "min_lines": -1}]}, "postcheck.artifacts[0].min_lines"),
        ({"artifacts": [{"path": "a.txt", "min_lines": 10_000_001}]}, "postcheck.artifacts[0].min_lines"),
        ({"artifacts": [{"path": "a.txt", "max_age_seconds": True}]}, "postcheck.artifacts[0].max_age_seconds"),
        ({"artifacts": [{"path": "a.txt", "max_age_seconds": 0}]}, "postcheck.artifacts[0].max_age_seconds"),
        ({"artifacts": [{"path": "a.txt", "max_age_seconds": 604_801}]}, "postcheck.artifacts[0].max_age_seconds"),
        ({"artifacts": [{"path": "a.json", "json_pointer": "/status"}]}, "postcheck.artifacts[0].json_pointer"),
        ({"artifacts": [{"path": "a.json", "json_pointer": "status", "equals": "ok"}]}, "postcheck.artifacts[0].json_pointer"),
        ({"artifacts": [{"path": "a.json", "json_pointer": "/bad~2", "equals": "ok"}]}, "postcheck.artifacts[0].json_pointer"),
        ({"artifacts": [{"path": "a.json", "json_pointer": "/bad~", "equals": "ok"}]}, "postcheck.artifacts[0].json_pointer"),
        ({"artifacts": [{"path": "a.json", "json_pointer": 123, "equals": "ok"}]}, "postcheck.artifacts[0].json_pointer"),
        ({"artifacts": [{"path": "a.json", "equals": "ok"}]}, "postcheck.artifacts[0].equals"),
        ({"artifacts": [{"path": "a.json", "min_value": 1}]}, "postcheck.artifacts[0].min_value"),
        ({"artifacts": [{"path": "a.json", "json_pointer": "/a", "equals": {"nested": 1}}]}, "postcheck.artifacts[0].equals"),
        ({"artifacts": [{"path": "a.json", "json_pointer": "/a", "equals": [1]}]}, "postcheck.artifacts[0].equals"),
        ({"artifacts": [{"path": "a.json", "json_pointer": "/a", "equals": float("nan")}]}, "postcheck.artifacts[0].equals"),
        ({"artifacts": [{"path": "a.json", "json_pointer": "/a", "min_value": True}]}, "postcheck.artifacts[0].min_value"),
        ({"artifacts": [{"path": "a.json", "json_pointer": "/a", "min_value": "5"}]}, "postcheck.artifacts[0].min_value"),
        ({"artifacts": [{"path": "a.json", "json_pointer": "/a", "min_value": float("inf")}]}, "postcheck.artifacts[0].min_value"),
    ],
)
def test_normalize_postcheck_errors_name_exact_field_path(payload: object, expected_field: str) -> None:
    with pytest.raises(ScheduleValidationError) as exc_info:
        normalize_postcheck(payload)
    message = str(exc_info.value)
    assert expected_field in message
    assert "下一步" in message


def test_evaluate_valid_empty_pattern_passes(tmp_path: Path) -> None:
    now = 10_000.0
    _write(
        tmp_path / "summary.json",
        json.dumps({"status": "complete", "rows": 0, "a/b": {"c~d": True}}),
        mtime=now - 5,
    )
    _write(tmp_path / "rows.csv", b"", mtime=now - 5)

    obs = evaluate_postcheck(
        {
            "artifacts": [
                {"path": "summary.json", "json_pointer": "/status", "equals": "complete", "max_age_seconds": 60},
                {"path": "summary.json", "json_pointer": "/rows", "equals": 0, "min_value": 0},
                {"path": "summary.json", "json_pointer": "/a~1b/c~0d", "equals": True},
                {"path": "rows.csv", "min_bytes": 0, "min_lines": 0},
            ]
        },
        root=tmp_path,
        now=now,
        read_bytes=65_536,
    )
    assert obs["status"] == "passed"
    assert obs["failure_kind"] == ""
    assert obs["evaluated_at"] == now
    assert all(check["code"] == "" for check in obs["checks"])


def test_check_code_artifact_missing(tmp_path: Path) -> None:
    now = 5_000.0
    obs = evaluate_postcheck(
        {
            "artifacts": [
                {"path": "missing.txt", "min_bytes": 1},
                {"path": "out/*.jsonl", "min_lines": 1},
            ]
        },
        root=tmp_path,
        now=now,
        read_bytes=65_536,
    )
    assert obs["status"] == "failed"
    assert obs["failure_kind"] == "business"
    assert [c["code"] for c in obs["checks"]] == ["ARTIFACT_MISSING", "ARTIFACT_MISSING"]
    assert all(c["observed"]["match_count"] == 0 for c in obs["checks"])


def test_check_code_artifact_too_small(tmp_path: Path) -> None:
    now = 5_000.0
    _write(tmp_path / "out.bin", b"abc", mtime=now - 2)
    obs = evaluate_postcheck(
        {"artifacts": [{"path": "out.bin", "min_bytes": 10}]},
        root=tmp_path,
        now=now,
        read_bytes=65_536,
    )
    assert obs["status"] == "failed"
    assert obs["failure_kind"] == "business"
    assert obs["checks"][0]["code"] == "ARTIFACT_TOO_SMALL"
    assert obs["checks"][0]["observed"]["bytes"] == 3


def test_check_code_artifact_too_few_lines_and_bounded_readinto(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 5_000.0
    _write(tmp_path / "short.txt", b"only_one_line", mtime=now - 1)
    obs_short = evaluate_postcheck(
        {"artifacts": [{"path": "short.txt", "min_lines": 2}]},
        root=tmp_path,
        now=now,
        read_bytes=65_536,
    )
    assert obs_short["status"] == "failed"
    assert obs_short["failure_kind"] == "business"
    assert obs_short["checks"][0]["code"] == "ARTIFACT_TOO_FEW_LINES"
    assert obs_short["checks"][0]["observed"]["lines_seen"] == 1

    # Verify early stop: a 256 KiB file with min_lines=3 stops after the first 64 KiB readinto chunk.
    many_lines = ("row\n" * 65_536).encode("utf-8")
    _write(tmp_path / "big.txt", many_lines, mtime=now - 1)
    bytes_read_calls: list[int] = []
    real_fdopen = os.fdopen

    class _CountingReader:
        def __init__(self, inner) -> None:
            self._inner = inner

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, *args):
            return self._inner.__exit__(*args)

        def fileno(self) -> int:
            return self._inner.fileno()

        def readinto(self, buf) -> int:
            n = self._inner.readinto(buf)
            bytes_read_calls.append(n)
            return n

        def read(self, size: int = -1) -> bytes:
            data = self._inner.read(size)
            bytes_read_calls.append(len(data))
            return data

    monkeypatch.setattr(os, "fdopen", lambda fd, mode="r", *a, **kw: _CountingReader(real_fdopen(fd, mode, *a, **kw)))
    obs_big = evaluate_postcheck(
        {"artifacts": [{"path": "big.txt", "min_lines": 3}]},
        root=tmp_path,
        now=now,
        read_bytes=len(many_lines),
    )
    assert obs_big["status"] == "passed"
    assert bytes_read_calls == [65_536]
    assert obs_big["checks"][0]["observed"]["lines_seen"] >= 3

    # Budget exhaustion before EOF is NOT proof of too few lines -> unknown/postcheck_unavailable.
    _write(tmp_path / "many_short.txt", "line\n" * 40, mtime=now - 1)
    obs_budget = evaluate_postcheck(
        {"artifacts": [{"path": "many_short.txt", "min_lines": 35}]},
        root=tmp_path,
        now=now,
        read_bytes=50,
    )
    assert obs_budget["status"] == "unknown"
    assert obs_budget["failure_kind"] == "postcheck_unavailable"
    assert obs_budget["checks"][0]["code"] == "ARTIFACT_UNREADABLE"


def test_check_code_artifact_stale(tmp_path: Path) -> None:
    now = 10_000.0
    _write(tmp_path / "feed.log", "line1\n", mtime=now - 120)
    obs = evaluate_postcheck(
        {"artifacts": [{"path": "feed.log", "min_lines": 1, "max_age_seconds": 60}]},
        root=tmp_path,
        now=now,
        read_bytes=65_536,
    )
    assert obs["status"] == "stalled"
    assert obs["failure_kind"] == "stalled"
    assert obs["checks"][0]["code"] == "ARTIFACT_STALE"
    assert obs["checks"][0]["observed"]["age_seconds"] == 120.0

    _write(tmp_path / "exact_age.log", "line1\n", mtime=now - 60)
    obs_ok = evaluate_postcheck(
        {"artifacts": [{"path": "exact_age.log", "min_lines": 1, "max_age_seconds": 60}]},
        root=tmp_path,
        now=now,
        read_bytes=65_536,
    )
    assert obs_ok["status"] == "passed"


def test_check_code_artifact_json_mismatch_variants(tmp_path: Path) -> None:
    now = 10_000.0
    _write(tmp_path / "bool_num.json", json.dumps({"flag": 1, "zero": False, "items": ["x"]}), mtime=now - 1)
    _write(tmp_path / "bad.json", "{not-json", mtime=now - 1)
    _write(tmp_path / "nonutf8.json", b"\xff\xfe\xfd", mtime=now - 1)
    _write(tmp_path / "nan.json", '{"val": NaN}', mtime=now - 1)

    for artifact in (
        {"path": "bool_num.json", "json_pointer": "/flag", "equals": True},
        {"path": "bool_num.json", "json_pointer": "/zero", "equals": 0},
        {"path": "bool_num.json", "json_pointer": "/flag", "min_value": 2},
        {"path": "bool_num.json", "json_pointer": "/zero", "min_value": 0},
        {"path": "bool_num.json", "json_pointer": "/items/01", "equals": "x"},
        {"path": "bool_num.json", "json_pointer": "/items/1", "equals": "x"},
        {"path": "bool_num.json", "json_pointer": "/missing", "equals": "x"},
        {"path": "bad.json", "json_pointer": "", "equals": "x"},
        {"path": "nonutf8.json", "json_pointer": "", "equals": "x"},
        {"path": "nan.json", "json_pointer": "/val", "min_value": 0},
    ):
        obs = evaluate_postcheck({"artifacts": [artifact]}, root=tmp_path, now=now, read_bytes=65_536)
        assert obs["status"] == "failed", f"expected failed for {artifact}"
        assert obs["failure_kind"] == "business"
        assert obs["checks"][0]["code"] == "ARTIFACT_JSON_MISMATCH"


def test_check_code_artifact_outside_root_symlink_escapes(tmp_path: Path) -> None:
    now = 10_000.0
    root = tmp_path / "run_root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    _write(outside / "secret.txt", "TOP_SECRET_VALUE", mtime=now - 1)
    _write(root / "inside.txt", "ok\n", mtime=now - 1)

    (root / "escape_out.txt").symlink_to(outside / "secret.txt")
    (root / "escape_in.txt").symlink_to(root / "inside.txt")
    (root / "dangling.txt").symlink_to(outside / "missing.txt")
    (root / "escape_dir").symlink_to(outside)
    (root / "glob_dir").mkdir()
    (root / "glob_dir" / "evil.log").symlink_to(outside / "secret.txt")

    for rel_path in (
        "escape_out.txt",
        "escape_in.txt",
        "dangling.txt",
        "escape_dir/secret.txt",
        "glob_dir/*.log",
    ):
        obs = evaluate_postcheck(
            {"artifacts": [{"path": rel_path, "min_bytes": 1}]},
            root=root,
            now=now,
            read_bytes=65_536,
        )
        assert obs["status"] == "unknown", f"expected unknown for {rel_path}"
        assert obs["failure_kind"] == "postcheck_unavailable"
        assert obs["checks"][0]["code"] == "ARTIFACT_OUTSIDE_ROOT"
        assert "TOP_SECRET_VALUE" not in json.dumps(obs)


def test_check_code_artifact_unreadable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 10_000.0
    (tmp_path / "dir_not_file").mkdir()
    obs_dir = evaluate_postcheck(
        {"artifacts": [{"path": "dir_not_file", "min_bytes": 0}]},
        root=tmp_path,
        now=now,
        read_bytes=65_536,
    )
    assert obs_dir["status"] == "unknown"
    assert obs_dir["failure_kind"] == "postcheck_unavailable"
    assert obs_dir["checks"][0]["code"] == "ARTIFACT_UNREADABLE"

    # JSON larger than read_bytes cannot be read safely under host budget -> ARTIFACT_UNREADABLE.
    _write(tmp_path / "huge.json", json.dumps({"status": "complete", "pad": "x" * 200}), mtime=now - 1)
    obs_budget = evaluate_postcheck(
        {"artifacts": [{"path": "huge.json", "json_pointer": "/status", "equals": "complete"}]},
        root=tmp_path,
        now=now,
        read_bytes=32,
    )
    assert obs_budget["status"] == "unknown"
    assert obs_budget["failure_kind"] == "postcheck_unavailable"
    assert obs_budget["checks"][0]["code"] == "ARTIFACT_UNREADABLE"

    # Simulated OS permission error during open.
    _write(tmp_path / "locked.txt", "hello\n", mtime=now - 1)
    real_open = os.open

    def _deny_open(path, flags, *args, **kwargs):
        if Path(path).name == "locked.txt":
            raise PermissionError("simulated EACCES")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", _deny_open)
    obs_perm = evaluate_postcheck(
        {"artifacts": [{"path": "locked.txt", "min_lines": 1}]},
        root=tmp_path,
        now=now,
        read_bytes=65_536,
    )
    assert obs_perm["status"] == "unknown"
    assert obs_perm["checks"][0]["code"] == "ARTIFACT_UNREADABLE"


def test_glob_picks_newest_within_64_and_rejects_65_entries_closing_scandir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 20_000.0
    dir64 = tmp_path / "d64"
    dir64.mkdir()
    for i in range(63):
        _write(dir64 / f"part-{i:02d}.json", json.dumps({"seq": i}), mtime=now - 100 + i)
    _write(dir64 / "part-newest.json", json.dumps({"seq": 999}), mtime=now - 2)

    scandir_events: list[str] = []
    real_scandir = os.scandir

    class _TrackedScandir:
        def __init__(self, path) -> None:
            self._inner = real_scandir(path)

        def __enter__(self):
            scandir_events.append("enter")
            self._inner.__enter__()
            return self

        def __exit__(self, *args):
            scandir_events.append("exit")
            return self._inner.__exit__(*args)

        def __iter__(self):
            return iter(self._inner)

    monkeypatch.setattr(os, "scandir", _TrackedScandir)
    obs64 = evaluate_postcheck(
        {"artifacts": [{"path": "d64/*.json", "json_pointer": "/seq", "equals": 999}]},
        root=tmp_path,
        now=now,
        read_bytes=65_536,
    )
    assert obs64["status"] == "passed"
    assert obs64["checks"][0]["path"] == "d64/part-newest.json"
    assert obs64["checks"][0]["observed"]["match_count"] == 64
    assert scandir_events == ["enter", "exit"]

    # Adding a 65th entry trips ARTIFACT_SCAN_BUDGET_EXHAUSTED and still closes scandir.
    _write(dir64 / "extra-65.tmp", "x", mtime=now - 1)
    scandir_events.clear()
    obs65 = evaluate_postcheck(
        {"artifacts": [{"path": "d64/*.json", "json_pointer": "/seq", "equals": 999}]},
        root=tmp_path,
        now=now,
        read_bytes=65_536,
    )
    assert obs65["status"] == "unknown"
    assert obs65["failure_kind"] == "postcheck_unavailable"
    assert obs65["checks"][0]["code"] == "ARTIFACT_SCAN_BUDGET_EXHAUSTED"
    assert scandir_events == ["enter", "exit"]


def test_status_precedence_business_over_stalled_over_unavailable(tmp_path: Path) -> None:
    now = 30_000.0
    _write(tmp_path / "stale.txt", "ok\n", mtime=now - 500)
    (tmp_path / "unavail_dir").mkdir()

    # Business (MISSING) beats STALE and UNREADABLE.
    obs_bus = evaluate_postcheck(
        {
            "artifacts": [
                {"path": "unavail_dir", "min_bytes": 0},
                {"path": "stale.txt", "min_lines": 1, "max_age_seconds": 60},
                {"path": "missing.txt", "min_bytes": 1},
            ]
        },
        root=tmp_path,
        now=now,
        read_bytes=65_536,
    )
    assert (obs_bus["status"], obs_bus["failure_kind"]) == ("failed", "business")

    # STALE beats UNREADABLE.
    obs_stale = evaluate_postcheck(
        {
            "artifacts": [
                {"path": "unavail_dir", "min_bytes": 0},
                {"path": "stale.txt", "min_lines": 1, "max_age_seconds": 60},
            ]
        },
        root=tmp_path,
        now=now,
        read_bytes=65_536,
    )
    assert (obs_stale["status"], obs_stale["failure_kind"]) == ("stalled", "stalled")

    # Single artifact: business mismatch (TOO_SMALL) beats STALE.
    _write(tmp_path / "small_stale.txt", b"x", mtime=now - 500)
    obs_single = evaluate_postcheck(
        {"artifacts": [{"path": "small_stale.txt", "min_bytes": 100, "max_age_seconds": 60}]},
        root=tmp_path,
        now=now,
        read_bytes=65_536,
    )
    assert (obs_single["status"], obs_single["failure_kind"]) == ("failed", "business")
    assert obs_single["checks"][0]["code"] == "ARTIFACT_TOO_SMALL"


def test_observation_contains_numbers_and_relative_paths_only_zero_file_content(tmp_path: Path) -> None:
    now = 40_000.0
    secret_text = "sk-live-DO-NOT-LEAK-9876543210"
    secret_num = 987654321
    _write(
        tmp_path / "nested" / "report.json",
        json.dumps({"status": "incomplete", "secret": secret_text, "pin": secret_num}),
        mtime=now - 12,
    )
    _write(
        tmp_path / "nested" / "notes.log",
        f"{secret_text}\nsecond line\n",
        mtime=now - 3,
    )

    obs = evaluate_postcheck(
        {
            "artifacts": [
                {"path": "nested/report.json", "json_pointer": "/status", "equals": "complete"},
                {"path": "nested/*.log", "min_bytes": 1, "min_lines": 2, "max_age_seconds": 60},
            ]
        },
        root=tmp_path,
        now=now,
        read_bytes=65_536,
    )
    serialized = json.dumps(obs, ensure_ascii=False)
    assert secret_text not in serialized
    assert str(secret_num) not in serialized
    assert str(tmp_path) not in serialized
    assert str(tmp_path.resolve()) not in serialized
    assert set(POSTCHECK_CHECK_CODES) == {
        "ARTIFACT_MISSING",
        "ARTIFACT_TOO_SMALL",
        "ARTIFACT_TOO_FEW_LINES",
        "ARTIFACT_STALE",
        "ARTIFACT_JSON_MISMATCH",
        "ARTIFACT_OUTSIDE_ROOT",
        "ARTIFACT_UNREADABLE",
        "ARTIFACT_SCAN_BUDGET_EXHAUSTED",
    }
    for check in obs["checks"]:
        assert not str(check["path"]).startswith("/")
        for val in check["observed"].values():
            assert type(val) in (int, float)
