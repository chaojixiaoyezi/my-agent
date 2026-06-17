
from __future__ import annotations

"""SourceProfile 存储 round-trip + 默认空 + all_profiles 只列已写方案的源。"""

from pathlib import Path

from agent_py_agent.agent.tooling.log_ops.store import LogOpsStore, build_source_specs


def _store(tmp_path: Path) -> LogOpsStore:
    return LogOpsStore(tmp_path / ".log_ops", "m")


def test_profile_default_empty(tmp_path: Path) -> None:
    # 没有方案 → 空 dict(daemon 走默认 line + DEFAULT_RULES)。
    assert _store(tmp_path).read_profile("sid-x") == {}


def test_profile_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    profile = {
        "version": 1,
        "created_by": "llm-onboarding",
        "splitter": {"type": "multiline_start", "params": {"start_pattern": "^\\d"}},
        "rules": [{"name": "r1", "severity": "high", "pattern": "boom"}],
        "triage_hint": "重点看 boom",
        "reporting": {"min_level": "P1"},
    }
    store.write_profile("sid-x", profile)
    assert store.read_profile("sid-x") == profile
    # 新 store 对象读回(落盘续接)。
    store2 = LogOpsStore(store.root.parent, "m")
    assert store2.read_profile("sid-x") == profile


def test_all_profiles_lists_only_written(tmp_path: Path) -> None:
    store = _store(tmp_path)
    specs = build_source_specs(["/x.log", "/y.log"])
    store.write_config(specs, poll_interval_seconds=2.0)
    store.write_profile(specs[0].source_id, {"version": 1, "triage_hint": "x"})
    profiles = store.all_profiles()
    assert specs[0].source_id in profiles  # 写了方案的出现
    assert specs[1].source_id not in profiles  # 没写方案的不出现
