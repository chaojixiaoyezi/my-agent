"""能力包 v2 块 5：钉住包的必需交付物本回合有没有交（只在本回合改过工作区时查，缺或打不开最多返工 2 次）。

复用块 3 的流程夹具（真实安装的包、规范任务根、替身检查程序）；只读结构化事实，不起 Gateway、不碰真实 owner home。
"""

from __future__ import annotations

import types

import pytest

from agent_py_agent.agent.capability.pack_verification_deliverables import (
    DELIVERABLE_MISSING,
    DELIVERABLE_UNREADABLE,
    missing_deliverables,
)
from agent_py_agent.agent.capability.pack_verification_hooks import (
    capture_baseline_before_tool,
    closeout_rework_block,
)
from agent_py_agent.agent.capability.pack_verification_ledger import PackVerificationLedger
from agent_py_agent.agent.capability.pack_verification_matching import file_readable
from agent_py_agent.agent.capability.pack_verification_report import (
    pack_verification_notice_text,
    run_pack_verification_facts,
)
from agent_py_agent.agent.capability_verification_manifest import (
    DeliverableDeclaration,
    DeliverableFieldMatch,
)
from agent_py_agent.tests.test_pack_verification_service import (
    _write,
    build_env,
    install_fake_runner,
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    return build_env(tmp_path, monkeypatch)


@pytest.fixture
def fake_runner(monkeypatch):
    return install_fake_runner(monkeypatch)


def test_missing_deliverable_reworks_at_most_twice(env, fake_runner):
    capture_baseline_before_tool(env.agent, env.params, "write_file")
    _write(env.workspace / "notes.txt", "draft")
    first = closeout_rework_block(env.agent, env.params)
    assert "story-content 的 delivery" in first and "没找到符合该模式的交付物" in first and "只要审阅" in first
    assert "没找到符合该模式的交付物" in closeout_rework_block(env.agent, env.params)
    assert closeout_rework_block(env.agent, env.params) == "", "缺交付物最多返工 2 次"
    facts = run_pack_verification_facts(env.agent, env.params)
    assert facts["deliverable_rework_count"] == 2 and facts["deliverables_missing"][0]["code"] == DELIVERABLE_MISSING
    assert "story-content 要求的交付物 delivery 没找到符合 out/** 的文件" in pack_verification_notice_text(facts)


def test_turn_without_workspace_changes_is_not_checked(env, fake_runner):
    assert closeout_rework_block(env.agent, env.params) == "", "纯问答回合没有基线，不查交付物"
    capture_baseline_before_tool(env.agent, env.params, "read_file")
    assert closeout_rework_block(env.agent, env.params) == ""
    assert run_pack_verification_facts(env.agent, env.params) is None


# LLM: A10-t102 形状（B8 重试点）：模型把交付物写成 delivery.v3——内容是含正确 schema 的 json，只是文件名
#   不匹配包的路径模式 out/**。修复前基线实测：交付物判 MISSING、提示说「本回合没有写出」（误导，文件其实写了）、
#   检查程序一个目标都不查（target_count=0、rework_count=0）；修复后提示带路径模式和改名指引，改名后通过。
# 函数用途: 复现「交付物文件名不匹配路径模式」的收尾行为（修复后断言）。
def test_unmatched_deliverable_name_reports_pattern_and_recovers_after_rename(env, fake_runner):
    capture_baseline_before_tool(env.agent, env.params, "write_file")
    _write(env.workspace / "delivery.v3", {"schema": "delivery.v1"})
    text = closeout_rework_block(env.agent, env.params)
    assert "没找到符合该模式的交付物" in text and "out/**" in text
    assert "如果交付物用了别的文件名，请按包的约定命名" in text
    facts = run_pack_verification_facts(env.agent, env.params)
    [closeout] = [row for row in env.ledger.records() if row["kind"] == "closeout"]
    assert closeout["target_count"] == 0 and facts["rework_count"] == 0, "不匹配模式的文件不进检查目标"
    assert [item["code"] for item in facts["deliverables_missing"]] == [DELIVERABLE_MISSING]
    assert facts["deliverable_rework_count"] == 1 and fake_runner == []
    _write(env.workspace / "out/delivery.json", {"schema": "delivery.v1"})
    assert closeout_rework_block(env.agent, env.params) == "", "按包的约定命名后收尾通过"
    facts = run_pack_verification_facts(env.agent, env.params)
    assert facts["deliverables_missing"] == [] and len(fake_runner) == 1
    assert [item["status"] for item in facts["results"]] == ["passed"]


def test_unreadable_versus_other_schema(env, fake_runner):
    capture_baseline_before_tool(env.agent, env.params, "write_file")
    broken = env.workspace / "out/d.json"
    broken.parent.mkdir(parents=True)
    broken.write_text("{not json")
    _write(env.workspace / "out/notes.json", {"schema": "other"})
    text = closeout_rework_block(env.agent, env.params)
    [item] = run_pack_verification_facts(env.agent, env.params)["deliverables_missing"]
    assert item["code"] == DELIVERABLE_UNREADABLE and item["paths"] == ["out/d.json"], "能打开但字段不匹配的不算这个交付物"
    assert "out/d.json 打不开或解析不了" in text


def test_delivered_file_clears_the_check_and_uncertain_old_files_do_not_count(env, fake_runner, monkeypatch):
    from agent_py_agent.agent.capability import pack_verification_matching as matching

    for index in range(3):
        _write(env.workspace / f"in/{index}.json", {"schema": "other"})
    _write(env.workspace / "out/old.json", {"schema": "delivery.v1"})
    monkeypatch.setattr(matching, "MAX_SCAN_MATCHED_FILES_COUNT", 2)
    capture_baseline_before_tool(env.agent, env.params, "write_file")
    monkeypatch.setattr(matching, "MAX_SCAN_MATCHED_FILES_COUNT", 512)
    assert "没找到符合该模式的交付物" in closeout_rework_block(env.agent, env.params), "漏扫的老文件不算本回合交付"
    _write(env.workspace / "out/new.json", {"schema": "delivery.v1"})
    env.params.run_id = "run-2"
    capture_baseline_before_tool(env.agent, env.params, "write_file")
    _write(env.workspace / "out/new.json", {"schema": "delivery.v1", "v": 2})
    assert closeout_rework_block(env.agent, env.params) == ""
    assert run_pack_verification_facts(env.agent, env.params)["deliverables_missing"] == []


def test_no_deliverable_rework_when_it_cannot_be_recorded(env, fake_runner, monkeypatch):
    capture_baseline_before_tool(env.agent, env.params, "write_file")
    _write(env.workspace / "notes.txt", "draft")
    original = PackVerificationLedger.append
    monkeypatch.setattr(PackVerificationLedger, "append",
                        lambda self, record: False if record.get("kind") == "deliverable_rework" else original(self, record))
    assert closeout_rework_block(env.agent, env.params) == ""


def test_sections_are_ordered_input_then_deliverable_then_verification(env, fake_runner):
    import json

    source = _write(env.workspace / "in/source.json", {"schema": "source.v1"})
    capture_baseline_before_tool(env.agent, env.params, "write_file")
    source.write_text(json.dumps({"schema": "source.v1", "edited": True}))
    text = closeout_rework_block(env.agent, env.params)
    assert text.index("输入原件") < text.index("交付物还没交")


def test_only_required_deliverables_are_checked(tmp_path):
    required = DeliverableDeclaration("must", ("*.json",), DeliverableFieldMatch("json", "schema", ("a",)), True)
    optional = DeliverableDeclaration("maybe", ("*.md",), None, False)
    package = types.SimpleNamespace(installation=types.SimpleNamespace(manifest=types.SimpleNamespace(plugin_id="p", version="1")),
                                    verification=types.SimpleNamespace(deliverables=(required, optional)))
    (tmp_path / "x.json").write_text('{"schema": "a"}')
    assert missing_deliverables((package,), ["x.json"], tmp_path) == []
    [item] = missing_deliverables((package,), [], tmp_path)
    assert item["deliverable_id"] == "must" and item["field_match"]["equals"] == ["a"]


def test_file_readable_follows_the_declared_format(tmp_path):
    good, bad, text = tmp_path / "g.json", tmp_path / "b.json", tmp_path / "t.txt"
    good.write_text("{}")
    bad.write_text("{")
    text.write_text("plain")
    json_match = DeliverableFieldMatch("json", "schema", ("x",))
    assert file_readable(good, json_match) and not file_readable(bad, json_match)
    assert file_readable(bad, None) and file_readable(text, DeliverableFieldMatch("yaml", "kind", ("x",)))
    assert not file_readable(tmp_path / "missing.json", None)
