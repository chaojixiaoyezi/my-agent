"""能力包 v2 块 4：输入原件清单、就地修改判定与返工（input_policy=preserve_originals）。

复用块 3 的流程夹具（真实安装的包、规范任务根、替身检查程序）；只读结构化事实，不起 Gateway、不碰真实 owner home。
"""

from __future__ import annotations

import json
import shutil
import types
from pathlib import Path

import pytest

from agent_py_agent.agent.capability import pack_verification_originals as originals_module
from agent_py_agent.agent.capability.pack_verification_hooks import (
    capture_baseline_before_tool,
    closeout_rework_block,
)
from agent_py_agent.agent.capability.pack_verification_inputs import (
    modified_originals,
    preserving_packages,
)
from agent_py_agent.agent.capability.pack_verification_ledger import (
    PackVerificationLedger,
    pack_verification_root,
)
from agent_py_agent.agent.capability.pack_verification_matching import (
    FileState,
    WorkspaceScan,
    scan_workspace,
)
from agent_py_agent.agent.capability.pack_verification_originals import (
    OriginalFile,
    capture_task_originals,
    load_task_originals,
    original_copy_path,
)
from agent_py_agent.agent.capability.pack_verification_report import (
    pack_verification_notice_text,
    run_pack_verification_facts,
)
from agent_py_agent.tests.test_pack_verification_service import (
    _write,
    build_env,
    install_fake_runner,
)

PACK_ROOT = Path("data/pack_verification")
DECLARATION = types.SimpleNamespace(path_patterns=("**/*.json",), field_match=None)


@pytest.fixture
def env(tmp_path, monkeypatch):
    return build_env(tmp_path, monkeypatch)


@pytest.fixture
def fake_runner(monkeypatch):
    return install_fake_runner(monkeypatch)


def _pack_root(env) -> Path:
    return env.task_root / PACK_ROOT


def test_originals_are_recorded_once_per_task_with_private_copies(env):
    source = _write(env.workspace / "in/source.json", {"schema": "source.v1"})
    _write(env.workspace / "notes.txt", "x")
    capture_baseline_before_tool(env.agent, env.params, "write_file")
    originals = load_task_originals(_pack_root(env))
    assert list(originals) == ["in/source.json"] and originals["in/source.json"].copied
    assert originals["in/source.json"].packages == ("story-content",)
    copy = original_copy_path(_pack_root(env), originals["in/source.json"])
    assert copy.read_bytes() == source.read_bytes() and (copy.stat().st_mode & 0o777) == 0o600
    assert ((_pack_root(env) / "originals.json").stat().st_mode & 0o777) == 0o600
    _write(env.workspace / "out/new.json", {"schema": "source.v1"})
    env.params.run_id = "run-2"
    capture_baseline_before_tool(env.agent, env.params, "write_file")
    assert list(load_task_originals(_pack_root(env))) == ["in/source.json"], "后一回合写出的文件不会成为原件"


def test_copy_limits_and_tampered_copy(tmp_path, monkeypatch):
    workspace, pack_root = tmp_path / "ws", tmp_path / "pack"
    workspace.mkdir()
    for name, size in (("a.json", 3), ("b.json", 5), ("c.json", 4)):
        (workspace / name).write_text("x" * size)
    monkeypatch.setattr(originals_module, "MAX_ORIGINAL_COPY_BYTES", 4)
    monkeypatch.setattr(originals_module, "MAX_ORIGINAL_COPIES_TOTAL_BYTES", 6)
    monkeypatch.setattr(originals_module, "MAX_ORIGINAL_FILES_COUNT", 2)
    capture_task_originals((pack_root, workspace), scan_workspace(workspace, ("*.json",)), [("p", [DECLARATION])])
    originals = load_task_originals(pack_root)
    assert list(originals) == ["a.json", "b.json"], "最多记 MAX_ORIGINAL_FILES_COUNT 个"
    assert originals["a.json"].copied and not originals["b.json"].copied, "超过单个上限只记摘要"
    assert json.loads((pack_root / "originals.json").read_text())["truncated"] is True
    copy = original_copy_path(pack_root, originals["a.json"])
    copy.write_text("tampered")
    assert original_copy_path(pack_root, originals["a.json"]) is None, "副本被改过就当没有"


def test_modified_originals_only_counts_changes_made_this_run(tmp_path):
    workspace = tmp_path / "ws"
    names = ("edited", "deleted", "restored", "untouched", "earlier", "prior", "fixed")
    edited, deleted, restored, untouched, earlier, prior, fixed = (_write(workspace / f"{name}.json", name) for name in names)
    scan = scan_workspace(workspace, ("*.json",))
    originals = {relpath: OriginalFile(relpath, state.sha256, state.size, False, ("p",))
                 for relpath, state in scan.files.items()}
    # prior、fixed 在本回合开始前就已被改过：prior 本回合没再动，fixed 本回合改回了原样，两者都不该算到本回合头上
    _write(prior, "changed in an earlier run")
    _write(fixed, "changed in an earlier run")
    baseline = WorkspaceScan({key: value for key, value in scan_workspace(workspace, ("*.json",)).files.items()
                              if key != "earlier.json"})
    _write(fixed, "fixed")
    edited.write_text("changed")
    deleted.unlink()
    _write(restored, "tmp")
    _write(restored, "restored")
    earlier.write_text("changed before this run")
    items = modified_originals(originals, baseline, (workspace, None, frozenset({"p"})))
    assert [(item["path"], bool(item["current_sha256"])) for item in items] == [("deleted.json", False), ("edited.json", True)]
    assert modified_originals(originals, baseline, (workspace, None, frozenset({"other"}))) == [], "只对声明了保留原件的钉住包生效"
    assert untouched.exists()


def test_preserving_packages_reads_the_structured_policy():
    def package(plugin_id, policy):
        return types.SimpleNamespace(installation=types.SimpleNamespace(manifest=types.SimpleNamespace(plugin_id=plugin_id)),
                                     verification=types.SimpleNamespace(input_policy=policy))
    packages = (package("a", "preserve_originals"), package("b", ""), package("c", "future_policy"))
    assert preserving_packages(packages) == frozenset({"a"})


def test_closeout_reworks_once_for_in_place_edits_and_cp_restore_clears_it(env, fake_runner):
    source = _write(env.workspace / "in/source.json", {"schema": "source.v1"})
    capture_baseline_before_tool(env.agent, env.params, "write_file")
    source.write_text(json.dumps({"schema": "source.v1", "edited": True}))
    _write(env.workspace / "out/d.json", {"schema": "delivery.v1"})  # 交付物齐全，只看输入原件这一段
    text = closeout_rework_block(env.agent, env.params)
    copy = original_copy_path(_pack_root(env), load_task_originals(_pack_root(env))["in/source.json"])
    assert "in/source.json" in text and str(copy) in text and f'cp "{copy}" "in/source.json"' in text
    assert closeout_rework_block(env.agent, env.params) == "", "输入原件返工只有一次"
    facts = run_pack_verification_facts(env.agent, env.params)
    assert facts["input_rework_count"] == 1 and [item["path"] for item in facts["inputs_modified"]] == ["in/source.json"]
    assert "任务开始时的输入 in/source.json 被就地改了（原件副本已保存）" in pack_verification_notice_text(facts)
    shutil.copyfile(copy, source)
    assert closeout_rework_block(env.agent, env.params) == ""
    assert run_pack_verification_facts(env.agent, env.params)["inputs_modified"] == [], "恢复原样后不再报"


def test_no_input_rework_when_it_cannot_be_recorded(env, fake_runner, monkeypatch):
    source = _write(env.workspace / "in/source.json", {"schema": "source.v1"})
    capture_baseline_before_tool(env.agent, env.params, "write_file")
    source.write_text("{}")
    _write(env.workspace / "out/d.json", {"schema": "delivery.v1"})
    original = PackVerificationLedger.append
    monkeypatch.setattr(PackVerificationLedger, "append",
                        lambda self, record: False if record.get("kind") == "input_rework" else original(self, record))
    assert closeout_rework_block(env.agent, env.params) == ""


def test_input_and_deliverable_problems_are_combined_in_one_prompt(env, fake_runner):
    source = _write(env.workspace / "in/source.json", {"schema": "source.v1"})
    capture_baseline_before_tool(env.agent, env.params, "write_file")
    source.write_text(json.dumps({"schema": "source.v1", "edited": True}))
    _write(env.workspace / "out/d.json", {"schema": "delivery.v1", "bad": True})
    text = closeout_rework_block(env.agent, env.params)
    assert text.index("输入原件") < text.index("placeholder_text"), "先恢复原件，再修交付物"
    ledger = PackVerificationLedger(_pack_root(env) / "run-1.jsonl")
    assert ledger.count("input_rework") == 1 and ledger.count("rework") == 1


def test_facts_report_modified_inputs_even_without_results_or_missing_deliverables(tmp_path):
    from agent_py_agent.agent.capability.pack_verification_report import pack_verification_facts

    ledger = PackVerificationLedger(tmp_path / "run-1.jsonl")
    ledger.append({"kind": "input_check", "items": [{"path": "in/source.json", "copy_path": "x"}]})
    ledger.append({"kind": "deliverable_check", "items": []})
    facts = pack_verification_facts(ledger)
    assert facts is not None and [item["path"] for item in facts["inputs_modified"]] == ["in/source.json"], (
        "只有输入原件被改（没有检查结果、也不缺交付物）时，最终事实也要报出来")
    assert facts["results"] == [] and facts["deliverables_missing"] == []


def test_storage_resolves_the_canonical_task_root(env, monkeypatch):
    child_workspace = env.task_root / "work" / "agents" / "child-run"
    monkeypatch.setattr("agent_py_agent.agent.agent_core.run_task_workspace_writer.current_run_task_workspace_root",
                        lambda agent, params=None: child_workspace)
    owner = types.SimpleNamespace(home_dir=env.task_root.parents[2])
    assert pack_verification_root(env.agent, env.params, owner) == _pack_root(env), "子代理落在规范任务根，不落进 work/"
    monkeypatch.setattr("agent_py_agent.agent.agent_core.run_task_workspace_writer.current_run_task_workspace_root",
                        lambda agent, params=None: env.workspace)
    assert pack_verification_root(env.agent, env.params, owner) is None, "不在规范任务根下就不落盘"
    capture_baseline_before_tool(env.agent, env.params, "write_file")
    assert not (env.workspace / "data").exists()


def test_baseline_file_state_round_trip_keeps_original_shape():
    state = FileState("a" * 64, 3)
    assert WorkspaceScan.from_payload(WorkspaceScan({"x.json": state}).to_payload()).files["x.json"] == state
