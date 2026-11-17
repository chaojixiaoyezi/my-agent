"""Wave S F04：一份损坏的保留学习候选不再拖垮 /skills、/skills learned 与 CLI learned list。

实测（克隆网关 web/alice）：proposals/ 下多了一份截断的候选（不完整 JSON，末尾半个 UTF-8 字符）后，
/skills、/skills learned、/skills proposals all 全部回 SKILL_CONTROL_READ_FAILED，原因只有 UnicodeDecodeError 文本，
不说是哪个文件；已发布的 Skill 看不到也管不了。只读报告只为数一下待确认候选就走了严格读取。
锁定：报告照常列出已发布 Skill，并给出指名该文件的单项诊断；候选列表本身仍严格拒绝，但报错指名文件，坏文件原样保留。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.skill_learning_proposals import list_learning_proposals
from agent_py_agent.agent.capability.skill_learning_report import learned_skills_report
from agent_py_agent.cli.skill_learning_commands import _list_lines
from agent_py_agent.tests.test_skill_chat_control import _run
from agent_py_agent.tests.test_skill_learning import _output, _publish_first
from agent_py_agent.tests.test_skill_learning import _runtime as _learning_runtime

_TORN = "learning-proposal-" + "f04" * 10 + "f0" + ".json"
_TORN_BYTES = '{"schema_version": "skill-learning-proposal.v1", "decision": {"name": "截断'.encode()[:-1]


# 函数用途: 先正常发布一个自学 Skill（同时留下一份已确认的候选），再放一份截断的候选。
def _with_torn_proposal(tmp_path):
    ctx = _learning_runtime(tmp_path, _output())
    _publish_first(ctx)
    folder = ctx.store.directory / "proposals"
    folder.mkdir(exist_ok=True)
    (folder / _TORN).write_bytes(_TORN_BYTES)
    return ctx


# 函数用途: 聊天 /skills learned 与 /skills 照常可用，列表里指名坏候选；坏文件不被改动。
def test_chat_listings_survive_a_torn_retained_proposal_and_name_it(tmp_path, monkeypatch):
    ctx = _with_torn_proposal(tmp_path)
    listing = _run(monkeypatch, ctx.home, "/skills learned")
    assert listing.ok, listing.message
    assert "csv-merge-by-date" in listing.message
    assert "SKILL_LEARNING_PROPOSAL_UNREADABLE" in listing.message and f"proposals/{_TORN}" in listing.message
    overview = _run(monkeypatch, ctx.home, "/skills")
    assert overview.ok, overview.message
    assert (ctx.store.directory / "proposals" / _TORN).read_bytes() == _TORN_BYTES


# 函数用途: CLI 报告给出结构化单项诊断并照常列出 Skill；候选列表仍严格拒绝，报错指名该文件。
def test_cli_report_names_the_torn_proposal_and_proposal_listing_stays_strict(tmp_path):
    ctx = _with_torn_proposal(tmp_path)
    report = learned_skills_report(ctx.store, SimpleNamespace(enable_self_learning=True))
    assert report["pending_proposals"] is None
    assert report["proposal_coverage"] == {
        "status": "unavailable", "code": "SKILL_LEARNING_PROPOSAL_UNREADABLE",
        "detail": {"folder": "proposals", "file": _TORN, "error": "UnicodeDecodeError"}}
    lines = _list_lines({**report, "owner_id": "local/main"})
    assert any(_TORN in line for line in lines) and any("csv-merge-by-date" in line for line in lines)
    with pytest.raises(ValueError, match=_TORN):
        list_learning_proposals(ctx.store)
