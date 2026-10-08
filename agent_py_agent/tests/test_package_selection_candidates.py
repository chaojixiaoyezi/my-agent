"""选包只按输入预算收候选，推荐上限独立；全部传输为离线替身。"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.capability.package_selection import build_package_selection_material
from agent_py_agent.agent.capability.package_selection_runtime import (
    prepare_capability_package_selection,
)
from agent_py_agent.agent.capability.router import (
    CapabilityRouter,
    from_capability_package,
    score_card,
)
from agent_py_agent.agent.capability.skill_snapshot import SkillSnapshot
from agent_py_agent.tests.test_capability_package_selection import _package
from agent_py_agent.tests.test_capability_package_selection_runtime import _runtime, _task


def _cards(material):
    return json.loads(material.prompt[material.prompt.index("{"):])["candidates"]


def _router(packages, limit=5):
    snapshot = SkillSnapshot((), (), "fixture", "local/main", "/unused", packages=tuple(packages))
    return CapabilityRouter(config=CapabilityConfig(capability_candidate_limit=limit), skill_snapshot=snapshot)


def test_selection_includes_ten_packages_with_recommendation_limit_five():
    packages = [_package(f"pack-{index:02}") for index in range(10)]
    router = _router(packages)
    assert router.config.capability_candidate_limit == 5
    material = build_package_selection_material("核对", packages, max_input_tokens=10000)
    assert material.error_code == ""
    assert material.candidate_refs == tuple(package.to_ref() for package in packages)
    assert len(_cards(material)) == material.total_candidates == 10
    assert material.omitted_candidates == 0 and material.warning_codes == ()


def test_last_named_semantic_candidate_without_keyword_hits_is_not_filtered():
    packages = [replace(_package(f"pack-{index:02}", "description"), keywords=("unmatched",)) for index in range(9)]
    last = replace(_package("z-last", "solar workflow"), keywords=("unmatched",))
    packages.append(last)
    assert score_card("solar", from_capability_package(last))[0] > 0
    assert "solar" not in last.keywords
    material = build_package_selection_material("solar", packages, max_input_tokens=10000)
    assert material.candidate_refs == tuple(package.to_ref() for package in packages)
    assert _cards(material)[-1]["stable_id"] == last.stable_id


def test_tight_budget_ranks_keyword_match_then_stable_zero_score_ties():
    packages = [replace(_package(name, "description"), keywords=("unmatched",))
                for name in ("c-pack", "a-pack", "b-pack", "d-pack", "z-pack")]
    packages[-1] = replace(packages[-1], keywords=("solar",))
    expected = [packages[-1], *packages[:2]]
    scores = [score_card("solar", from_capability_package(package))[0] for package in packages]
    assert scores == [0, 0, 0, 0, 4]
    enough_for_three = build_package_selection_material("solar", expected, max_input_tokens=10000)
    budget = enough_for_three.estimated_input_tokens
    material = build_package_selection_material("solar", packages, max_input_tokens=budget)
    assert material.error_code == ""
    assert material.candidate_refs == tuple(package.to_ref() for package in expected)
    assert [card["stable_id"] for card in _cards(material)] == [package.stable_id for package in expected]
    assert material.response_schema["properties"]["selected_ids"]["items"]["enum"] == [package.stable_id for package in expected]
    assert material.total_candidates == 5 and material.omitted_candidates == 2
    assert material.warning_codes == ("CAPABILITY_SELECTION_CANDIDATES_OMITTED",)
    assert material.estimated_input_tokens <= budget
    again = build_package_selection_material("solar", packages, max_input_tokens=budget)
    assert material == again and material.candidate_digest == enough_for_three.candidate_digest


@pytest.mark.parametrize("limit,count", [(5, 5), (1, 1), (0, 10)])
def test_recommendation_segment_still_obeys_its_own_limit(limit, count):
    packages = [_package(f"pack-{index:02}") for index in range(10)]
    text = _router(packages, limit).render_package_recommendations("核对", context_window_tokens=200000)
    assert text.startswith("# 本轮能力包候选")
    assert sum(line.startswith("- ") for line in text.splitlines()) == count


def test_runtime_does_not_forward_recommendation_limit_to_selection(tmp_path, monkeypatch):
    fixture = _runtime(tmp_path, monkeypatch, selected=(), config="capability_candidate_limit: 1\n")
    prepare_capability_package_selection(fixture.agent, fixture.params)
    call, = fixture.agent.backend.calls
    assert call[0] == "selection"
    candidates = json.loads(call[1][call[1].index("{"):])["candidates"]
    assert len(candidates) == len(fixture.entries) == 2
    marker = _task(fixture).capability_selection
    assert marker.status == "finished" and marker.outcome == "empty"
    assert "CAPABILITY_SELECTION_CANDIDATES_OMITTED" not in marker.warning_codes
    assert not fixture.reads


def _ten_content_archives(root):
    from agent_py_agent.agent.capability.package_build import (
        build_capability_pack,
        read_declared_files,
    )
    from agent_py_agent.tests.test_pack_pick_bench import _packages

    archives = _packages(root)
    source = root / "source"
    for index in range(8):
        name = f"a-pack-{index}"
        declaration = {"plugin_id": name, "version": "0.1.0", "summary": "auxiliary",
                       "capability": {"description": "auxiliary", "keywords": ["auxiliary"], "entry_document": "CAPABILITY.md"},
                       "files": [{"path": "CAPABILITY.md"}], "settings_schema": {"type": "object", "properties": {}}}
        built = build_capability_pack(declaration, read_declared_files(source, ["CAPABILITY.md"]))
        (archives / f"{name}.zip").write_bytes(built.payload)
    return archives


def _observe_selection_schema(monkeypatch):
    from scripts.eval.pack_pick_runtime import FixtureBackend

    observed = []
    original = FixtureBackend.generate_structured

    def structured(self, prompt, *, response_schema, messages=None):
        observed.append(response_schema["properties"]["selected_ids"]["items"]["enum"])
        return original(self, prompt, response_schema=response_schema, messages=messages)

    monkeypatch.setattr(FixtureBackend, "generate_structured", structured)
    return observed


def _assert_c_output(out, samples, omitted):
    rows = [json.loads(line) for line in (out / "calls.jsonl").read_text().splitlines()]
    assert len(rows) == samples * 2
    selected = [row for row in rows if row["phase"] == "selection"]
    assert len(selected) == samples and all(row["request_ok"] and not row["selection_error"] for row in selected)
    assert all(("CAPABILITY_SELECTION_CANDIDATES_OMITTED" in row["selection_warnings"]) == omitted for row in selected)
    metadata = json.loads((out / "metadata.json").read_text())
    assert metadata["fake"] and metadata["arms"] == "C"
    assert len(metadata["packages"]) == 10 and metadata["capability_settings"]["capability_candidate_limit"] == 5


def test_fake_c_bench_keeps_ten_candidates_then_reports_budget_omissions(tmp_path, monkeypatch):
    from pathlib import Path

    from scripts.eval import pack_pick_bench as bench

    home = tmp_path / "home"
    home.mkdir()
    assert bench.main(["setup", "--home", str(home), "--packages", str(_ten_content_archives(tmp_path))]) == 0
    config = home / "owners/local/main/config/capability_config.yaml"
    config.parent.mkdir(exist_ok=True)
    config.write_text("capability_package_selection_max_input_tokens: 10000\ncapability_candidate_limit: 5\n")
    observed = _observe_selection_schema(monkeypatch)
    fixture_queries = Path(__file__).parent / "fixtures/pack_pick_queries.json"
    out = tmp_path / "c-all"
    assert bench.main(["run", "--home", str(home), "--queries", str(fixture_queries), "--arms", "C", "--fake", "--out", str(out)]) == 0
    assert [len(ids) for ids in observed] == [10] * 4
    _assert_c_output(out, 4, False)
    with bench.isolated_home(home):
        packages = bench.make_agent(home).current_skill_snapshot().packages
    ranked = sorted(packages, key=lambda package: -score_card("novel", from_capability_package(package))[0])
    budget = build_package_selection_material("novel", ranked[:3], max_input_tokens=10000).estimated_input_tokens
    config.write_text(f"capability_package_selection_max_input_tokens: {budget}\ncapability_candidate_limit: 5\n")
    query = tmp_path / "one.json"
    query.write_text(json.dumps([{"id": "tight", "query": "novel", "lang": "en", "query_domain": "novel", "positive_packs": ["novel"]}]))
    tight_out = tmp_path / "c-tight"
    assert bench.main(["run", "--home", str(home), "--queries", str(query), "--arms", "C", "--fake", "--out", str(tight_out)]) == 0
    assert observed[-1] == [package.stable_id for package in ranked[:3]]
    _assert_c_output(tight_out, 1, True)
