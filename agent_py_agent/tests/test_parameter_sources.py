"""参数中心 P17 修订（2026-10-01）：capability / runtime_guard 两份配置纳入登记表与写入口。

锁定：登记表每条带 source 字段；新来源默认安全边界、capability 的显式 free 名单逐键放行（理由必填）；
capability 写入目标=运行时实际读取的那份文件（agent.capability_config_path 或 default_capability_config_path(agent.root)，
由调用方按 capability_config_for_agent 同一逻辑解析），文件不存在时新建、只写被改的键，写后用 load_capability_config
回读，不一致恢复原文件或删除新建文件并报 PARAMETER_NOT_EFFECTIVE；runtime_guard 运行时没有用户覆盖层，
修改一律拒绝（PARAMETER_SOURCE_READ_ONLY），只读来源显示随包值并标明“随包默认、不可覆盖”；
账本与 agent 共用同一 settings-changes.jsonl（记在 agent 用户配置旁）；查看/搜索/回滚覆盖三份配置
（log_analysis_config.yaml 已于 2026-10-01 删除，见 test_log_analysis_config_removed）；
运行值：capability 按运行时路径读（owner 有覆盖时显示覆盖值），只读来源固定读随包默认。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.capability.config import load_capability_config
from agent_py_agent.agent.capability.runtime_config_reload import (
    capability_config_for_agent,
    capability_config_path_for,
)
from agent_py_agent.agent.settings import parameter_changes as changes
from agent_py_agent.agent.settings.parameter_changes import ChangeOrigin, WritePaths
from agent_py_agent.agent.settings.parameter_registry import (
    SAFETY_BOUNDARY,
    parameter_registry,
    running_value,
)
from agent_py_agent.agent.settings.user_config_capability import packaged_config_path
from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool

_ORIGIN = ChangeOrigin("test")
def _user(tmp_path):
    """建用户配置（agent 主配置）文件副本；capability 运行时路径单独给，不假设与用户配置同目录。"""
    path = tmp_path / "desktop.yaml"
    path.write_text('agent_name: "myagent"\n', encoding="utf-8")
    return path


def _paths(path, capability_path=None):
    return WritePaths(user_path=path, capability_path=capability_path)


def _main_agent(path, root=None, capability_path=None):
    home = SimpleNamespace(owner_provider="local", owner_kind="main", owner_id="main")
    return SimpleNamespace(home_paths=home, root=str(root or Path(path).parent),
                           capability_config_path=capability_path,
                           config=SimpleNamespace(config_path=str(path)))


def _capability_file(tmp_path):
    """capability 运行时路径：默认候选在 <root>/agent_py_agent/config/ 或 <root>/config/ 下。"""
    root = tmp_path / "owner-home"
    cap_dir = root / "config"
    cap_dir.mkdir(parents=True, exist_ok=True)
    src = packaged_config_path().with_name("capability_config.yaml")
    cap_path = cap_dir / "capability_config.yaml"
    cap_path.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return root, cap_path


# ---- 查看 / 搜索 / 运行值 ----

def test_search_and_view_cover_all_three_sources(tmp_path, monkeypatch):
    """search 能搜到两份新配置的键；user_config view 带 source 字段与真实运行值。"""
    monkeypatch.delenv("MY_AGENT_CONFIG", raising=False)
    path = _user(tmp_path)
    _, cap_path = _capability_file(tmp_path)
    tool = UserConfigTool(_main_agent(path, capability_path=str(cap_path)))
    for query, key in (("capability_candidate_limit", "capability_candidate_limit"),
                       ("repeat_fail", "repeat_fail_threshold")):
        found = json.loads(tool.execute({"action": "search", "query": query}).output)["parameters"]
        assert found[0]["key"] == key and found[0]["source"] != "agent"
    view = json.loads(tool.execute({"action": "view", "key": "capability_candidate_limit"}).output)
    assert view["parameter"]["source"] == "capability"
    assert view["parameter"]["running_value"] == "5"
    assert view["fact"]["packaged_default"] == "5"


def test_running_value_reads_the_runtime_capability_file():
    """running_value 对 capability 键从随包文件读默认（没给 capability_path 时回落登记默认）；只读来源固定随包值。"""
    reg = parameter_registry()
    assert running_value(reg["capability_candidate_limit"]) == 5
    assert running_value(reg["repeat_fail_threshold"]) == 10
    assert running_value(reg["max_tokens"], SimpleNamespace(max_tokens=999)) == 999


def test_log_analysis_config_removed_from_packaged_config():
    """死配置 log_analysis_config.yaml 已删除（2026-10-01）：随包 config 目录不再有该文件，登记表也没有该来源键。"""
    from agent_py_agent.agent.settings.parameter_registry import parameter_registry as _registry

    assert not packaged_config_path().with_name("log_analysis_config.yaml").exists()
    assert all(spec.source != "log_analysis" for spec in _registry().values())


def test_running_value_shows_owner_override_when_capability_file_has_one(tmp_path):
    """owner 有覆盖时运行值显示覆盖值（capability 走运行时路径，不是随包文件）。"""
    _, cap_path = _capability_file(tmp_path)
    reg = parameter_registry()
    cap_path.write_text('capability_candidate_limit: 9\n', encoding="utf-8")
    assert running_value(reg["capability_candidate_limit"], capability_path=cap_path) == 9
    assert running_value(reg["capability_candidate_limit"]) == 5  # 不给路径仍是随包默认


def test_capability_config_path_for_matches_the_runtime_entry(tmp_path):
    """capability_config_path_for 与 capability_config_for_agent 用同一路径：写进去运行时入口能读到。"""
    root, cap_path = _capability_file(tmp_path)
    agent = _main_agent(_user(tmp_path), root=str(root))
    assert capability_config_path_for(agent) == cap_path
    assert capability_config_for_agent(agent).capability_candidate_limit == 5


# ---- 安全等级：默认边界 + capability 显式 free 名单 ----

def test_extra_sources_default_to_boundary_except_the_explicit_free_list():
    """capability 的显式 free 名单（数值上限/展示开关）可改；其余新来源键一律安全边界。"""
    reg = parameter_registry()
    for key in ("capability_grant_max_skills", "capability_grant_expires_after_task",
                "decision_subagent_model_mode", "session_messaging_user_enabled",
                "session_task_max_chain_depth", "subagent_failure_auto_split_enabled",
                # C16：开不开由用户决定，模型不能改（P8/P17 验收后续，2026-10-02）。
                "enable_capability_package_selection"):
        assert reg[key].safety == SAFETY_BOUNDARY and not reg[key].writable, key
    for key in ("repeat_fail_threshold", "hard_failure_halt_enabled", "unknown_command_allowlist",
                "background_max_tool_rounds", "main_agent_auto_resume_attempt_limit"):
        assert not reg[key].writable, key
    for key in ("capability_candidate_limit", "subagent_heartbeat_timeout",
                "subagent_no_progress_attempt_limit", "decision_agent_timeout_max_seconds",
                "session_pair_hourly_limit"):
        assert reg[key].writable, key


def test_free_list_reasons_are_required_for_every_entry():
    """显式 free 名单每个键都必须有一句人话理由，且键都在登记表里（漏键/多键都会被拦）。"""
    from agent_py_agent.agent.settings import parameter_registry as registry_module

    assert all(str(reason).strip() for reason in registry_module._EXTRA_FREE_KEYS.values())
    assert set(registry_module._EXTRA_FREE_KEYS) <= set(parameter_registry())
    assert {key for key, spec in parameter_registry().items()
            if spec.source == "capability" and spec.writable} == set(registry_module._EXTRA_FREE_KEYS)


# ---- 写入链路（真实文件）：capability 走运行时路径 ----

def test_capability_write_writes_the_runtime_file_and_runtime_entry_reads_it(tmp_path):
    """写进 owner 实际路径后，运行时入口 capability_config_for_agent 能读到新值（新 agent 无缓存快照）。"""
    path = _user(tmp_path)
    root, cap_path = _capability_file(tmp_path)
    report = changes.set_parameter("capability_candidate_limit", "8", paths=_paths(path, cap_path), origin=_ORIGIN)
    assert report["ok"] and report["saved"] == "8"
    assert load_capability_config(cap_path).capability_candidate_limit == 8
    # 运行时入口（同一路径解析）读到新值：新 agent 实例没有缓存旧快照。
    agent = _main_agent(path, root=str(root))
    assert capability_config_for_agent(agent).capability_candidate_limit == 8
    entry = changes.parameter_history(user_path=path)[0]
    assert entry["key"] == "capability_candidate_limit" and entry["action"] == "set"


def test_capability_write_creates_the_file_when_missing(tmp_path):
    """capability 目标文件不存在时新建，只写被改的键；写后运行时入口能读到。"""
    path = _user(tmp_path)
    root = tmp_path / "owner-home"
    (root / "config").mkdir(parents=True, exist_ok=True)
    cap_path = root / "config" / "capability_config.yaml"
    report = changes.set_parameter("capability_candidate_limit", "8", paths=_paths(path, cap_path), origin=_ORIGIN)
    assert report["ok"]
    assert cap_path.is_file()
    assert "capability_candidate_limit: 8" in cap_path.read_text(encoding="utf-8")
    agent = _main_agent(path, root=str(root))
    assert capability_config_for_agent(agent).capability_candidate_limit == 8
    # 只写被改的键：文件里没有其它 capability 键。
    assert set(load_capability_config(cap_path).__dict__) - {"config_warnings"} - {
        "capability_candidate_limit"} == set() or True  # load 后未覆盖的键都是默认值，写进文件的只有被改的键
    assert [line for line in cap_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")] == ["capability_candidate_limit: 8"]


def test_capability_write_reset_and_revert_chain(tmp_path):
    """capability 键真实文件写→reset→revert 全链路；reset/revert 后运行时入口读回默认。"""
    path = _user(tmp_path)
    root, cap_path = _capability_file(tmp_path)
    report = changes.set_parameter("capability_candidate_limit", "8", paths=_paths(path, cap_path), origin=_ORIGIN)
    assert report["ok"] and load_capability_config(cap_path).capability_candidate_limit == 8
    reset = changes.reset_parameter("capability_candidate_limit", paths=_paths(path, cap_path), origin=_ORIGIN)
    assert reset["ok"] and load_capability_config(cap_path).capability_candidate_limit == 5
    again = changes.set_parameter("capability_candidate_limit", "9", paths=_paths(path, cap_path), origin=_ORIGIN)
    revert = changes.revert_change(again["change_id"], paths=_paths(path, cap_path), origin=_ORIGIN)
    assert revert["ok"] and load_capability_config(cap_path).capability_candidate_limit == 5
    agent = _main_agent(path, root=str(root))
    assert capability_config_for_agent(agent).capability_candidate_limit == 5


def test_effective_dispatches_the_loader_per_source(tmp_path):
    """回读加载器按来源分派：capability 用 load_capability_config、runtime_guard 用 policy（键是只读来源，写入口会拒，这里直接验证回读链路本身）。"""
    path = _user(tmp_path)
    _, cap_path = _capability_file(tmp_path)
    rg_path = tmp_path / "runtime_guard_config.yaml"
    rg_path.write_text(packaged_config_path().with_name("runtime_guard_config.yaml").read_text(encoding="utf-8"),
                       encoding="utf-8")
    assert changes._effective(cap_path, "capability_candidate_limit") == 5
    assert changes._effective(rg_path, "repeat_fail_threshold") == 10


def test_readonly_sources_reject_modification_with_structured_code(tmp_path):
    """runtime_guard 修改一律拒绝，给出结构化原因 PARAMETER_SOURCE_READ_ONLY，文件不动。"""
    path = _user(tmp_path)
    for key in ("repeat_fail_threshold", "hard_failure_halt_enabled"):
        report = changes.set_parameter(key, "20", paths=_paths(path), origin=_ORIGIN)
        assert (report["ok"], report["code"]) == (False, "PARAMETER_SOURCE_READ_ONLY"), key
        assert "只能查看和搜索" in report["error"]
    assert changes.reset_parameter("repeat_fail_threshold", paths=_paths(path), origin=_ORIGIN)["code"] == "PARAMETER_SOURCE_READ_ONLY"
    assert changes.parameter_history(user_path=path) == []


def test_capability_rollback_on_reload_mismatch_restores_or_removes(tmp_path, monkeypatch):
    """capability 回读不一致即恢复原文件（原本存在）或删除新建文件（原本不存在），并报 PARAMETER_NOT_EFFECTIVE。"""
    path = _user(tmp_path)
    _, cap_path = _capability_file(tmp_path)
    before = cap_path.read_text(encoding="utf-8")
    monkeypatch.setattr(changes, "_effective", lambda _p, _k: 1)
    report = changes.set_parameter("capability_candidate_limit", "8", paths=_paths(path, cap_path), origin=_ORIGIN)
    assert (report["ok"], report["code"]) == (False, "PARAMETER_NOT_EFFECTIVE")
    assert cap_path.read_text(encoding="utf-8") == before
    # 新建文件场景：不一致时删除新建文件。
    fresh = tmp_path / "fresh" / "config" / "capability_config.yaml"
    report = changes.set_parameter("capability_candidate_limit", "8", paths=_paths(path, fresh), origin=_ORIGIN)
    assert (report["ok"], report["code"]) == (False, "PARAMETER_NOT_EFFECTIVE")
    assert not fresh.exists()


def test_boundary_refusals_leave_files_untouched(tmp_path):
    """capability 边界键拒绝时文件不动、不新建；账本为空。"""
    path = _user(tmp_path)
    _, cap_path = _capability_file(tmp_path)
    before = cap_path.read_text(encoding="utf-8")
    for key in ("capability_grant_max_skills",):
        report = changes.set_parameter(key, "1", paths=_paths(path, cap_path), origin=_ORIGIN)
        assert (report["ok"], report["code"]) == (False, "PARAMETER_BOUNDARY"), key
    assert cap_path.read_text(encoding="utf-8") == before
    assert changes.parameter_history(user_path=path) == []


# ---- user_config 工具链路 ----

def test_tool_set_and_revert_for_capability(tmp_path, monkeypatch):
    """user_config 工具 set/history/revert 覆盖 capability 键；只读来源拒绝（TOOL_PERMISSION_DENIED）。"""
    monkeypatch.delenv("MY_AGENT_CONFIG", raising=False)
    path = _user(tmp_path)
    root, cap_path = _capability_file(tmp_path)
    tool = UserConfigTool(_main_agent(path, root=str(root), capability_path=str(cap_path)))
    saved = tool.execute({"action": "set", "key": "capability_candidate_limit", "value": "7", "reason": "用户要求"})
    assert saved.ok and json.loads(saved.output)["saved_to"].endswith("capability_config.yaml")
    history = json.loads(tool.execute({"action": "history"}).output)["changes"]
    assert history[0]["actor"] == "model" and history[0]["key"] == "capability_candidate_limit"
    denied = tool.execute({"action": "set", "key": "repeat_fail_threshold", "value": "30"})
    assert not denied.ok and denied.error_code == "TOOL_PERMISSION_DENIED"
    reverted = tool.execute({"action": "revert", "change_id": history[0]["id"]})
    assert reverted.ok and load_capability_config(cap_path).capability_candidate_limit == 5


def test_tool_view_marks_readonly_sources(tmp_path, monkeypatch):
    """user_config view 对 runtime_guard 键标 source_readonly（随包默认、不可覆盖）。"""
    monkeypatch.delenv("MY_AGENT_CONFIG", raising=False)
    path = _user(tmp_path)
    tool = UserConfigTool(_main_agent(path))
    for key in ("repeat_fail_threshold",):
        view = json.loads(tool.execute({"action": "view", "key": key}).output)
        assert view["parameter"]["source_readonly"] == "随包默认、不可覆盖（该配置运行时没有用户覆盖层）"
        assert view["fact"]["source"] == "packaged_default"
        assert view["parameter"]["writable"] is False


# ---- P8/P17 验收后续（2026-10-02）：capability 唯一位置 ----

def test_default_capability_config_path_is_the_single_user_location(tmp_path):
    """default_capability_config_path 只返回 <root>/config/capability_config.yaml；
    旧候选 <root>/agent_py_agent/config/capability_config.yaml 不再参与，即使那里有文件也不改变结果。"""
    from agent_py_agent.agent.capability.runtime_config_reload import default_capability_config_path

    root = tmp_path / "owner-home"
    legacy = root / "agent_py_agent" / "config" / "capability_config.yaml"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("capability_candidate_limit: 9\n", encoding="utf-8")
    assert default_capability_config_path(root) == root / "config" / "capability_config.yaml"


def test_resolve_capability_config_path_prefers_user_then_bundled(tmp_path):
    """resolve 的语义：用户位置存在用用户位置，否则回落随包默认（只读来源）。"""
    from agent_py_agent.agent.capability.runtime_config_reload import (
        bundled_capability_config_path,
        resolve_capability_config_path,
    )

    user = tmp_path / "config" / "capability_config.yaml"
    assert resolve_capability_config_path(user) == bundled_capability_config_path()
    user.parent.mkdir(parents=True)
    user.write_text("capability_candidate_limit: 9\n", encoding="utf-8")
    assert resolve_capability_config_path(user) == user
    assert resolve_capability_config_path(root=tmp_path) == user


def test_legacy_location_non_bundled_file_triggers_a_warning(tmp_path):
    """旧候选位置（<root>/agent_py_agent/config/...）存在非随包默认文件时给结构化告警，不静默合并；
    那里就是随包默认本身时（开发模式 root 是仓库目录）不告警。"""
    from agent_py_agent.agent.capability.config import CapabilityConfig
    from agent_py_agent.agent.capability.runtime_config_reload import (
        bundled_capability_config_path,
        capability_config_for_agent,
    )

    root = tmp_path / "owner-home"
    legacy = root / "agent_py_agent" / "config" / "capability_config.yaml"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("capability_candidate_limit: 9\n", encoding="utf-8")
    config = capability_config_for_agent(SimpleNamespace(root=root))
    assert isinstance(config, CapabilityConfig)
    assert any("旧位置" in warning and "已不再读取" in warning for warning in config.config_warnings)

    # 旧位置就是随包默认：复制随包文件过去，不该告警（开发模式 root=仓库目录是常态）。
    bundled = bundled_capability_config_path()
    clean_root = tmp_path / "repo-root"
    same = clean_root / "agent_py_agent" / "config" / "capability_config.yaml"
    same.parent.mkdir(parents=True)
    same.write_bytes(bundled.read_bytes())
    assert capability_config_for_agent(SimpleNamespace(root=clean_root)) == CapabilityConfig()


def test_capability_write_refuses_the_bundled_default_file(tmp_path):
    """随包默认文件只读、永不被写：capability_path 解析成随包默认时拒绝（CAPABILITY_IS_PACKAGED），文件不动。"""
    from agent_py_agent.agent.capability.runtime_config_reload import bundled_capability_config_path

    path = _user(tmp_path)
    bundled = bundled_capability_config_path()
    before = bundled.read_bytes()
    report = changes.set_parameter("capability_candidate_limit", "8", paths=_paths(path, bundled), origin=_ORIGIN)
    assert (report["ok"], report["code"]) == (False, "CAPABILITY_IS_PACKAGED")
    assert "随包默认" in report["error"] and "只读" in report["error"]
    assert bundled.read_bytes() == before


def test_capability_write_creates_dir_with_0700_and_file_0600(tmp_path):
    """参数中心新建 capability 目录用 0700、文件用 0600（与主配置写回保留权限同一安全口径）。"""
    import os
    import stat

    path = _user(tmp_path)
    root = tmp_path / "owner-home"
    cap_path = root / "config" / "capability_config.yaml"
    report = changes.set_parameter("capability_candidate_limit", "8", paths=_paths(path, cap_path), origin=_ORIGIN)
    assert report["ok"]
    assert stat.S_IMODE(cap_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(cap_path.stat().st_mode) == 0o600
    assert os.access(cap_path.parent, os.W_OK)


def test_enable_capability_package_selection_user_can_write_but_model_cannot(tmp_path):
    """C16：开不开由用户决定——用户在 /settings 作用域可写该键，模型（无作用域）一律按边界拒绝。"""
    path = _user(tmp_path)
    root, cap_path = _capability_file(tmp_path)
    with changes.user_settings_write_scope():
        report = changes.set_parameter(
            "enable_capability_package_selection", "true", paths=_paths(path, cap_path), origin=_ORIGIN)
    assert report["ok"] and report["saved"] == "true"
    assert load_capability_config(cap_path).enable_capability_package_selection is True
    denied = changes.set_parameter(
        "enable_capability_package_selection", "false", paths=_paths(path, cap_path), origin=_ORIGIN)
    assert (denied["ok"], denied["code"]) == (False, "PARAMETER_BOUNDARY")
    assert "模型不能改" in denied["error"]