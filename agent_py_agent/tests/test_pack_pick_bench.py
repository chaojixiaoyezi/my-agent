"""w2：隔离安装、真实 Gateway 首请求、假传输；不把脚本化回答当召回率。"""
from __future__ import annotations

import importlib
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability import router
from agent_py_agent.agent.capability.package_build import build_capability_pack, read_declared_files


def bench():
    return importlib.import_module("scripts.eval.pack_pick_bench")


def test_judgement_reads_structured_get_and_keeps_only_tool_identity():
    module = bench()
    calls = [{"name": "skill_search", "input": {"action": "get", "package_id": "novel", "query": "SECRET"}},
             {"name": "skill_search", "input": {"action": "search", "package_id": "other"}},
             {"name": "write_file", "input": {"path": "SECRET", "content": "SECRET"}},
             {"name": "skill_search", "input": {"action": "get", "skill_id": "capability:wrong"}}]
    facts = module.judge(["novel"], calls)
    assert facts["opened_packs"] == ["novel"] and facts["matched"] and not facts["misopened"]
    assert facts["tools"][1] == {"name": "skill_search", "action": "search", "package_id": "other"}
    assert "SECRET" not in json.dumps(facts)
    assert module.judge([], calls)["misopened"]
    assert module.judge(["different"], calls)["misopened"]
    assert not module.judge(["novel"], calls[1:])["matched"]


def test_summary_separates_selection_main_languages_denominators_and_unknown_tokens():
    module = bench()
    base = {"arm": "A", "phase": "main", "lang": "zh", "positive_packs": ["novel"],
            "matched": True, "misopened": False, "input_tokens": 100, "output_tokens": 10,
            "cache_read_input_tokens": 70, "cache_write_input_tokens": 0, "status": "ok"}
    rows = [base, dict(base, matched=False, input_tokens=None),
            dict(base, positive_packs=[], misopened=True),
            dict(base, arm="C", phase="selection", lang="fr"),
            dict(base, arm="C", phase="main", lang="fr", matched=False)]
    text = module.summary(rows)
    assert "| A | main | zh | 1/2 (50.00%) | 1/1 (100.00%) | 100.00 (2/3)" in text
    assert "| C | selection | fr | 1/1 (100.00%)" in text
    assert "| C | main | fr | 0/1 (0.00%)" in text
    from scripts.eval.pack_pick_results import observation_fields, usage_fields
    from scripts.eval.pack_pick_runtime import BenchCase, FirstCallTap, _record_row

    retry = [dict(base, id="same", repeat=1, thread_id="thread", matched=False, status="failed"),
             dict(base, id="same", repeat=1, thread_id="thread")]
    assert "| A | main | zh | 1/1 (100.00%)" in module.summary(retry)
    unknown = SimpleNamespace(call_id="probe", status="finished", total_latency_seconds=0.2,
                              provider_attempt_count=2, provider_usage_fields=["output_tokens"], error_code="",
                              accounted_input_tokens=900, cached_input_tokens=20, cache_creation_input_tokens=3,
                              output_tokens=5, backend="fake", model="fake",
                              metadata={"purpose": "probe:tool_capability"})
    assert usage_fields(unknown)["input_tokens"] is None
    case = BenchCase({"id": "x", "lang": "zh", "query_domain": "novel", "positive_packs": ["novel"]}, "C", 1)
    probe = _record_row(case, FirstCallTap(), ("request", "thread"), unknown)
    assert probe["phase"] == "probe:tool_capability" and not probe["matched"]
    assert "| C | probe:tool_capability | zh | 未适用 | 未适用" in module.summary([probe])
    missing = dict(base, phase="selection", record_kind="observation", **observation_fields())
    assert "未知 (0/0)" in module.summary([missing])


def test_home_guard_refuses_real_descendant_alias_missing_and_ancestor(tmp_path, monkeypatch):
    module = bench()
    personal = tmp_path / "personal"
    real = personal / ".my-agent"
    (real / "owners").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: personal))
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    for path in (real, real / "owners", alias, personal, tmp_path / "absent"):
        with pytest.raises(ValueError):
            module.safe_home(path)
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    assert module.safe_home(isolated) == isolated.resolve()


@pytest.mark.parametrize("arm", ["A", "B", "C"])
@pytest.mark.parametrize("failure", [False, True])
def test_arm_writes_fresh_switch_restores_bytes_and_keeps_budget_cache(tmp_path, arm, failure):
    module = bench()
    from agent_py_agent.agent.capability.config import CapabilityConfig
    from agent_py_agent.agent.capability.self_install_switches import read_fresh_capability_switch
    from agent_py_agent.agent.settings.config_io import load_simple_yaml

    config = CapabilityConfig(capability_bundle_max_tokens=987)
    snapshot = SimpleNamespace(config=config)
    path = tmp_path / "config/capability_config.yaml"
    path.parent.mkdir()
    saved = (f"# 原注释\r\nenable_capability_package_selection: {str(arm != 'C').lower()}\r\n"
             "capability_bundle_max_tokens: 3211\r\ncapability_candidate_limit: 9\r\n"
             "fixture_extra: ['keep', 'unknown']\r\n").encode()
    path.write_bytes(saved)
    agent = SimpleNamespace(root=tmp_path, _capability_config_runtime_snapshot=snapshot)
    original = router._CAPABILITY_PACKAGE_USAGE_INSTRUCTIONS
    with pytest.raises(RuntimeError, match="fixture") if failure else nullcontext():
        with module.arm_context(agent, arm):
            changed = router._CAPABILITY_PACKAGE_USAGE_INSTRUCTIONS
            assert ("用户的需求和某个能力包沾边" in changed) == (arm == "B")
            assert changed.splitlines()[2:] == original.splitlines()[2:]
            assert read_fresh_capability_switch(agent, "enable_capability_package_selection") == (arm == "C")
            raw = load_simple_yaml(path)
            assert raw["enable_capability_package_selection"] == (arm == "C")
            assert raw["capability_bundle_max_tokens"] == 3211 and raw["capability_candidate_limit"] == 9
            assert raw["fixture_extra"] == ["keep", "unknown"]
            assert agent._capability_config_runtime_snapshot.config.capability_bundle_max_tokens == 987
            if failure:
                raise RuntimeError("fixture")
    assert original == router._CAPABILITY_PACKAGE_USAGE_INSTRUCTIONS
    assert agent._capability_config_runtime_snapshot is snapshot
    assert path.read_bytes() == saved and not config.enable_capability_package_selection


@pytest.mark.parametrize("arm", ["A", "B", "C"])
@pytest.mark.parametrize("failure", [False, True])
def test_arm_removes_new_file_and_uses_explicit_user_path(tmp_path, arm, failure):
    module = bench()
    from agent_py_agent.agent.capability.self_install_switches import read_fresh_capability_switch

    path = tmp_path / "explicit/capability_config.yaml"
    agent = SimpleNamespace(root=tmp_path, capability_config_path=path)
    assert not hasattr(agent, "_capability_config_runtime_snapshot")
    with pytest.raises(RuntimeError, match="fixture") if failure else nullcontext():
        with module.arm_context(agent, arm):
            assert path.is_file()
            assert not (tmp_path / "config/capability_config.yaml").exists()
            assert read_fresh_capability_switch(agent, "enable_capability_package_selection") == (arm == "C")
            if failure:
                raise RuntimeError("fixture")
    assert not path.exists()
    assert not hasattr(agent, "_capability_config_runtime_snapshot")


@pytest.mark.parametrize("kind", ["real", "outside", "symlink"])
def test_arm_refuses_real_owner_and_escaped_configuration(tmp_path, monkeypatch, kind):
    module = bench()
    from agent_py_agent.agent.capability.config import CapabilityConfig

    personal = tmp_path / "personal"
    root = personal / ".my-agent/owners/local/main" if kind == "real" else tmp_path / "isolated"
    root.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: personal))
    outside = tmp_path / "outside"
    outside.mkdir()
    path = root / "config/capability_config.yaml"
    if kind == "outside":
        path = outside / "capability_config.yaml"
    if kind == "symlink":
        (root / "config").symlink_to(outside, target_is_directory=True)
    agent = SimpleNamespace(root=root, capability_config_path=path,
                            _capability_config_runtime_snapshot=SimpleNamespace(config=CapabilityConfig()))
    with pytest.raises(ValueError):
        with module.arm_context(agent, "C"):
            pytest.fail("不安全路径进入实验臂")
    assert not path.exists()


def _packages(root):
    source, packages = root / "source", root / "packages"
    source.mkdir()
    packages.mkdir()
    for name in ("novel", "drama"):
        (source / "CAPABILITY.md").write_text(f"# {name}\nPRIVATE-ENTRY-{name}", encoding="utf-8")
        declaration = {"plugin_id": name, "version": "0.1.0", "summary": name,
                       "capability": {"description": name, "keywords": [name], "entry_document": "CAPABILITY.md"},
                       "files": [{"path": "CAPABILITY.md"}], "settings_schema": {"type": "object", "properties": {}}}
        built = build_capability_pack(declaration, read_declared_files(source, ["CAPABILITY.md"]))
        (packages / f"{name}.zip").write_bytes(built.payload)
    return packages


def _observe_backend(monkeypatch):
    from scripts.eval.pack_pick_runtime import FixtureBackend

    observed = []
    initialize = FixtureBackend.__init__

    def observe(self, positive):
        initialize(self, positive)
        result = self.generate.return_value
        result.tool_use_blocks.append({"id": "fixture-write", "name": "write_file",
                                       "input": {"path": "SHOULD-NOT-EXIST.txt", "content": "SECRET-TOOL-BODY"}})

        def capture(prompt, **kwargs):
            surface = str(prompt) + json.dumps(kwargs.get("messages"), ensure_ascii=False)
            observed.append(surface)
            assert "### 能力包使用规则" in prompt
            assert any(tool.get("name") == "skill_search" for tool in kwargs["tools"])
            return result

        self.generate.side_effect = capture

    monkeypatch.setattr(FixtureBackend, "__init__", observe)
    return observed


def test_setup_and_fake_abc_use_real_gateway_ledger_fresh_sessions_and_zero_handlers(tmp_path, monkeypatch):
    module = bench()
    home = tmp_path / "home"
    home.mkdir()
    packages = _packages(tmp_path)
    assert module.main(["setup", "--home", str(home), "--packages", str(packages)]) == 0
    config_path = home / "owners/local/main/config/capability_config.yaml"
    config_path.parent.mkdir(exist_ok=True)
    saved_config = b"# restored exactly\nenable_capability_package_selection: true\ncapability_bundle_max_tokens: 3000\n"
    config_path.write_bytes(saved_config)
    queries = Path(__file__).parent / "fixtures" / "pack_pick_queries.json"
    out = tmp_path / "out"
    original = router._CAPABILITY_PACKAGE_USAGE_INSTRUCTIONS
    from agent_py_agent.agent.tooling.executor import ToolExecutor

    observed = _observe_backend(monkeypatch)

    # setup 已结束；模型哪怕请求写文件，也不能进入任何工具 handler。
    monkeypatch.setattr(ToolExecutor, "execute", lambda *args, **kwargs: pytest.fail("工具被执行"))
    assert module.main(["run", "--home", str(home), "--queries", str(queries), "--arms", "A,B,C",
                        "--out", str(out), "--repeat", "2", "--fake"]) == 0
    rows = [json.loads(line) for line in (out / "calls.jsonl").read_text().splitlines()]
    assert len(rows) == 32
    primary = [row for row in rows if row["phase"] == "main"]
    assert len(primary) == len({row["thread_id"] for row in primary}) == 24
    # 产品 Anthropic 口径总输入 = 未缓存 41 + 缓存读 13，不能把未缓存数冒充总输入。
    assert all(row["input_tokens"] == 54 and row["output_tokens"] == 7 for row in primary)
    assert all(row["cache_read_input_tokens"] == 13 for row in primary)
    selected = [row for row in rows if row["phase"] == "selection"]
    assert len(selected) == 8 and all(row["input_tokens"] == 17 for row in selected)
    assert all(row["arm"] == "C" and row["record_kind"] == "model_call" for row in selected)
    assert config_path.read_bytes() == saved_config
    assert all(row["matched"] == bool(row["positive_packs"]) for row in rows)
    assert all(row["status"] == "finished" and not row["misopened"] for row in rows)
    assert "PRIVATE-ENTRY" not in (out / "calls.jsonl").read_text()
    assert original == router._CAPABILITY_PACKAGE_USAGE_INSTRUCTIONS
    assert (out / "summary.md").is_file()
    assert len(observed) == 24
    assert sum("用户的需求和某个能力包沾边" in surface for surface in observed) == 8
    assert sum("PRIVATE-ENTRY" in surface for surface in observed) == 6  # C 的三条对口句各两遍。
    assert not list(home.rglob("SHOULD-NOT-EXIST.txt"))
    assert "SECRET-TOOL-BODY" not in (out / "calls.jsonl").read_text()
    assert not any(b"SECRET-TOOL-BODY" in path.read_bytes() for path in home.rglob("*") if path.is_file())
    from agent_py_agent.agent.conversation.store import ConversationStore

    store = module.make_agent(home).conversation_store
    for row in primary:
        events, errors = store.model_usage.events_report(row["thread_id"])
        assert not errors
        assert sum(event.model_calls["physical_model_attempt_count"] for event in events) == (2 if row["arm"] == "C" else 1)


def test_bad_queries_and_run_refuse_before_any_model_or_output(tmp_path, monkeypatch):
    module = bench()
    home = tmp_path / "home"
    home.mkdir()
    query_file = tmp_path / "bad.json"
    query_file.write_text(json.dumps([{"id": "x", "query": "x", "lang": "zh", "query_domain": "x", "positive_packs": "bad"}]))
    out = tmp_path / "out"
    assert module.main(["run", "--home", str(home), "--queries", str(query_file), "--out", str(out), "--fake"]) != 0
    assert not out.exists()
    query_file.write_text("[]")
    assert module.main(["run", "--home", str(home), "--queries", str(query_file), "--out", str(out), "--fake"]) != 0
    assert module.main(["setup", "--home", str(home), "--packages", str(_packages(tmp_path))]) == 0
    query_file.write_text(json.dumps([{"id": "x", "query": "x", "lang": "zh", "query_domain": "novel", "positive_packs": ["novel"]}]))
    from scripts.eval.pack_pick_runtime import FixtureBackend

    initialize = FixtureBackend.__init__

    def fail(self, positive):
        initialize(self, positive)
        self.generate.side_effect = RuntimeError("fixture-provider-failure")

    monkeypatch.setattr(FixtureBackend, "__init__", fail)
    original = router._CAPABILITY_PACKAGE_USAGE_INSTRUCTIONS
    assert module.main(["run", "--home", str(home), "--queries", str(query_file), "--out", str(out), "--arms", "B", "--fake"]) == 1
    rows = [json.loads(line) for line in (out / "calls.jsonl").read_text().splitlines()]
    assert rows and all(not row["request_ok"] and not row["matched"] for row in rows)
    assert all(row["input_tokens"] is None for row in rows)
    assert original == router._CAPABILITY_PACKAGE_USAGE_INSTRUCTIONS
    assert "0/1 (0.00%)" in (out / "summary.md").read_text()
