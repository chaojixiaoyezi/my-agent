"""能力包 v2 块 3：按包声明认文件（路径模式 + 字段匹配）与有界工作区快照的组件验证（只读文件，不运行任何程序）。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability import pack_verification_matching as matching
from agent_py_agent.agent.capability.pack_verification_matching import (
    WorkspaceScan,
    field_matches,
    file_matches,
    path_matches,
    scan_workspace,
    workspace_relpath,
)
from agent_py_agent.agent.capability_verification_manifest import DeliverableFieldMatch

SCHEMA = DeliverableFieldMatch("json", "schema", ("delivery.v1",))


@pytest.mark.parametrize("pattern,path,expected", [
    ("**/*.json", "a.json", True),
    ("**/*.json", "x/y/a.json", True),
    ("**/*.json", "a.json.bak", False),
    ("out/*.json", "out/a.json", True),
    ("out/*.json", "out/x/a.json", False),
    ("out/**", "out/a/b.txt", True),
    ("out/**", "out", False),
    ("out/**/final.json", "out/final.json", True),
    ("out/**/final.json", "out/a/b/final.json", True),
    ("v?.json", "v1.json", True),
    ("v?.json", "v10.json", False),
    ("a+b(1).json", "a+b(1).json", True),
    ("a+b(1).json", "aab(1).json", False),
])
def test_path_patterns_follow_glob_semantics(pattern, path, expected):
    assert path_matches(pattern, path) is expected


def test_field_match_reads_json_top_level_field_only(tmp_path, monkeypatch):
    good, other, broken, nested = (tmp_path / name for name in ("g.json", "o.json", "b.json", "n.json"))
    good.write_text(json.dumps({"schema": "delivery.v1"}))
    other.write_text(json.dumps({"schema": "other.v1"}))
    broken.write_text("{not json")
    nested.write_text(json.dumps({"meta": {"schema": "delivery.v1"}}))
    assert field_matches(good, SCHEMA)
    assert not field_matches(other, SCHEMA) and not field_matches(broken, SCHEMA) and not field_matches(nested, SCHEMA)
    assert field_matches(broken, None), "没声明字段匹配只按路径认"
    assert field_matches(broken, DeliverableFieldMatch("yaml", "schema", ("x",))), "宿主不认识的格式只按路径认"
    monkeypatch.setattr(matching, "MAX_FIELD_MATCH_FILE_BYTES", 4)
    assert not field_matches(good, SCHEMA), "超过上限的文件不解析"


def test_file_matches_needs_path_and_field(tmp_path):
    declaration = SimpleNamespace(path_patterns=("out/*.json",), field_match=SCHEMA)
    target = tmp_path / "out" / "d.json"
    target.parent.mkdir()
    target.write_text(json.dumps({"schema": "delivery.v1"}))
    assert file_matches(declaration, "out/d.json", target)
    assert not file_matches(declaration, "d.json", target)
    target.write_text(json.dumps({"schema": "x"}))
    assert not file_matches(declaration, "out/d.json", target)


def test_scan_records_only_matching_regular_files_and_is_deterministic(tmp_path):
    (tmp_path / "b").mkdir()
    (tmp_path / "a.json").write_text("{}")
    (tmp_path / "b" / "c.json").write_text("[]")
    (tmp_path / "b" / "skip.txt").write_text("x")
    (tmp_path / "link.json").symlink_to(tmp_path / "a.json")
    scan = scan_workspace(tmp_path, ("**/*.json",))
    assert list(scan.files) == ["a.json", "b/c.json"] and not scan.truncated
    assert scan.files["a.json"].size == 2 and len(scan.files["a.json"].sha256) == 64
    assert WorkspaceScan.from_payload(json.loads(json.dumps(scan.to_payload()))) == scan
    assert WorkspaceScan.from_payload({"files": "bad"}) == WorkspaceScan()


def test_scan_limits_mark_truncated(tmp_path, monkeypatch):
    for index in range(5):
        (tmp_path / f"{index}.json").write_text("{}")
    monkeypatch.setattr(matching, "MAX_SCAN_MATCHED_FILES_COUNT", 3)
    limited = scan_workspace(tmp_path, ("*.json",))
    assert limited.truncated and len(limited.files) == 3
    monkeypatch.setattr(matching, "MAX_SCAN_MATCHED_FILES_COUNT", 512)
    monkeypatch.setattr(matching, "MAX_SCAN_VISITED_ENTRIES_COUNT", 2)
    assert scan_workspace(tmp_path, ("*.json",)).truncated
    monkeypatch.setattr(matching, "MAX_SCAN_VISITED_ENTRIES_COUNT", 20_000)
    monkeypatch.setattr(matching, "MAX_SCAN_HASHED_FILE_BYTES", 1)
    big = scan_workspace(tmp_path, ("0.json",)).files["0.json"]
    assert big.sha256 == "" and big.size == 2, "太大的文件只记大小，不算摘要"


def test_workspace_relpath_rejects_outside_paths(tmp_path):
    inside = tmp_path / "ws" / "a.json"
    assert workspace_relpath(inside, tmp_path / "ws") == "a.json"
    assert workspace_relpath(tmp_path / "other.json", tmp_path / "ws") is None
