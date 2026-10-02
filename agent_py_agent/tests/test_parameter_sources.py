"""参数中心 P17（2026-10-01）：capability / runtime_guard / log_analysis 三份配置纳入登记表与写入口。

锁定：登记表每条带 source 字段；新来源默认安全边界、capability 的显式 free 名单逐键放行（理由必填）；
写入口按 source 选目标文件（用户配置同目录的 <source>_config.yaml），用正式加载器回读，不一致恢复
原文件并报 PARAMETER_NOT_EFFECTIVE；账本与 agent 共用同一 settings-changes.jsonl；查看/搜索/回滚
覆盖四份配置；新来源键没有对应的用户文件时拒绝写（不自动创建）。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from agent_py_agent.agent.capability.config import load_capability_config
from agent_py_agent.agent.settings import parameter_changes as changes
from agent_py_agent.agent.settings.parameter_changes import ChangeOrigin
from agent_py_agent.agent.settings.parameter_registry import (
    SAFETY_BOUNDARY,
    parameter_registry,
    running_value,
)
from agent_py_agent.agent.settings.user_config_capability import packaged_config_path
from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool

_ORIGIN = ChangeOrigin("test")
_SOURCES = ("capability", "runtime_guard", "log_analysis")


def _user(tmp_path):
    """建用户配置与三份来源文件的副本（写入口要求用户配置存在、目标文件已存在）。"""
    path = tmp_path / "desktop.yaml"
    path.write_text('agent_name: "myagent"\n', encoding="utf-8")
    for source in _SOURCES:
        src = packaged_config_path().with_name(f"{source}_config.yaml")
        dst = tmp_path / f"{source}_config.yaml"
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def _main_agent(path):
    home = SimpleNamespace(owner_provider="local", owner_kind="main", owner_id="main")
    return SimpleNamespace(home_paths=home, config=SimpleNamespace(config_path=str(path)))


# ---- 查看 / 搜索 / 运行值 ----

def test_search_and_view_cover_all_four_sources(tmp_path, monkeypatch):
    """search 能搜到三份新配置的键；user_config view 带 source 字段与真实运行值。"""
    monkeypatch.delenv("MY_AGENT_CONFIG", raising=False)
    path = _user(tmp_path)
    tool = UserConfigTool(_main_agent(path))
    for query, key in (("capability_candidate_limit", "capability_candidate_limit"),
                       ("repeat_fail", "repeat_fail_threshold"),
                       ("query_max", "query_max_limit")):
        found = json.loads(tool.execute({"action": "search", "query": query}).output)["parameters"]
        assert found[0]["key"] == key and found[0]["source"] != "agent"
    view = json.loads(tool.execute({"action": "view", "key": "capability_candidate_limit"}).output)
    assert view["parameter"]["source"] == "capability"
    assert view["parameter"]["running_value"] == "5"
    assert view["fact"]["packaged_default"] == "5"


def test_running_value_reads_the_real_source_file():
    """running_value 按来源读各自随包文件，而不是 agent 配置对象（否则非 agent 键全是空）。"""
    reg = parameter_registry()
    assert running_value(reg["capability_candidate_limit"]) == 5
    assert running_value(reg["repeat_fail_threshold"]) == 10
    assert running_value(reg["query_max_limit"]) == 1000
    assert running_value(reg["max_tokens"], SimpleNamespace(max_tokens=999)) == 999


# ---- 安全等级：默认边界 + capability 显式 free 名单 ----

def test_extra_sources_default_to_boundary_except_the_explicit_free_list():
    """capability 的显式 free 名单（数值上限/展示开关）可改；其余新来源键一律安全边界。"""
    reg = parameter_registry()
    for key in ("capability_grant_max_skills", "capability_grant_expires_after_task",
                "decision_subagent_model_mode", "session_messaging_user_enabled",
                "session_task_max_chain_depth", "subagent_failure_auto_split_enabled"):
        assert reg[key].safety == SAFETY_BOUNDARY and not reg[key].writable, key
    for key in ("repeat_fail_threshold", "hard_failure_halt_enabled", "unknown_command_allowlist",
                "background_max_tool_rounds", "main_agent_auto_resume_attempt_limit"):
        assert not reg[key].writable, key
    for key in ("enabled", "response_execution_enabled", "data_dir", "dispatch_budget_per_hour"):
        assert not reg[key].writable, key
    for key in ("capability_candidate_limit", "subagent_heartbeat_timeout",
                "subagent_no_progress_attempt_limit", "decision_agent_timeout_max_seconds",
                "session_pair_hourly_limit", "enable_capability_package_selection"):
        assert reg[key].writable, key


def test_free_list_reasons_are_required_for_every_entry():
    """显式 free 名单每个键都必须有一句人话理由，且键都在登记表里（漏键/多键都会被拦）。"""
    from agent_py_agent.agent.settings import parameter_registry as registry_module

    assert all(str(reason).strip() for reason in registry_module._EXTRA_FREE_KEYS.values())
    assert set(registry_module._EXTRA_FREE_KEYS) <= set(parameter_registry())
    assert {key for key, spec in parameter_registry().items()
            if spec.source == "capability" and spec.writable} == set(registry_module._EXTRA_FREE_KEYS)


# ---- 写入链路（真实文件） ----

def test_capability_write_reset_and_revert_chain(tmp_path):
    path = _user(tmp_path)
    cap_path = tmp_path / "capability_config.yaml"
    report = changes.set_parameter("capability_candidate_limit", "8", user_path=path, origin=_ORIGIN)
    assert report["ok"] and report["saved"] == "8"
    assert load_capability_config(cap_path).capability_candidate_limit == 8
    entry = changes.parameter_history(user_path=path)[0]
    assert entry["key"] == "capability_candidate_limit" and entry["action"] == "set"
    reset = changes.reset_parameter("capability_candidate_limit", user_path=path, origin=_ORIGIN)
    assert reset["ok"] and load_capability_config(cap_path).capability_candidate_limit == 5
    again = changes.set_parameter("capability_candidate_limit", "9", user_path=path, origin=_ORIGIN)
    revert = changes.revert_change(again["change_id"], user_path=path, origin=_ORIGIN)
    assert revert["ok"] and load_capability_config(cap_path).capability_candidate_limit == 5


def test_effective_dispatches_the_loader_per_source(tmp_path):
    """回读加载器按来源分派：capability 用 load_capability_config、runtime_guard 用 policy、
    log_analysis 用 load_simple_yaml（这三份键本身是边界、写入口会拒，这里直接验证回读链路本身）。"""
    path = _user(tmp_path)
    cap_path = tmp_path / "capability_config.yaml"
    rg_path = tmp_path / "runtime_guard_config.yaml"
    la_path = tmp_path / "log_analysis_config.yaml"
    assert changes._effective(cap_path, "capability_candidate_limit") == 5
    assert changes._effective(rg_path, "repeat_fail_threshold") == 10
    assert changes._effective(la_path, "query_max_limit") == 1000
    # 写入链路对这两份配置整体走边界拒绝（runtime_guard/log_analysis 全部是安全边界）
    assert changes.set_parameter("repeat_fail_threshold", "20", user_path=path, origin=_ORIGIN)["code"] == "PARAMETER_BOUNDARY"
    assert changes.set_parameter("query_max_limit", "2000", user_path=path, origin=_ORIGIN)["code"] == "PARAMETER_BOUNDARY"


def test_extra_source_write_needs_the_source_file(tmp_path):
    """新来源键只写各自文件：用户没建这份文件就拒绝，绝不落到 agent 配置或随包文件里。"""
    path = tmp_path / "desktop.yaml"
    path.write_text('agent_name: "myagent"\n', encoding="utf-8")
    report = changes.set_parameter("capability_candidate_limit", "8", user_path=path, origin=_ORIGIN)
    assert report["code"] == "USER_CONFIG_MISSING"
    assert "capability_candidate_limit" not in path.read_text(encoding="utf-8")


def test_extra_source_rollback_on_reload_mismatch(tmp_path, monkeypatch):
    """回读不一致即恢复原文件并报 PARAMETER_NOT_EFFECTIVE（与主配置同一流程，不另写一套）。"""
    path = _user(tmp_path)
    cap_path = tmp_path / "capability_config.yaml"
    before = cap_path.read_text(encoding="utf-8")
    monkeypatch.setattr(changes, "_effective", lambda _path, _key: 1)
    report = changes.set_parameter("capability_candidate_limit", "8", user_path=path, origin=_ORIGIN)
    assert (report["ok"], report["code"]) == (False, "PARAMETER_NOT_EFFECTIVE")
    assert cap_path.read_text(encoding="utf-8") == before


def test_extra_source_boundary_refusals_leave_files_untouched(tmp_path):
    path = _user(tmp_path)
    before = {name: (tmp_path / f"{name}_config.yaml").read_text(encoding="utf-8") for name in _SOURCES}
    for key in ("capability_grant_max_skills", "repeat_fail_threshold", "enabled"):
        report = changes.set_parameter(key, "1", user_path=path, origin=_ORIGIN)
        assert (report["ok"], report["code"]) == (False, "PARAMETER_BOUNDARY"), key
    for name in _SOURCES:
        assert (tmp_path / f"{name}_config.yaml").read_text(encoding="utf-8") == before[name]
    assert changes.parameter_history(user_path=path) == []


# ---- user_config 工具链路 ----

def test_tool_set_and_revert_for_capability(tmp_path, monkeypatch):
    monkeypatch.delenv("MY_AGENT_CONFIG", raising=False)
    path = _user(tmp_path)
    tool = UserConfigTool(_main_agent(path))
    saved = tool.execute({"action": "set", "key": "capability_candidate_limit", "value": "7", "reason": "用户要求"})
    assert saved.ok and json.loads(saved.output)["saved_to"].endswith("capability_config.yaml")
    history = json.loads(tool.execute({"action": "history"}).output)["changes"]
    assert history[0]["actor"] == "model" and history[0]["key"] == "capability_candidate_limit"
    denied = tool.execute({"action": "set", "key": "repeat_fail_threshold", "value": "30"})
    assert not denied.ok and denied.error_code == "TOOL_PERMISSION_DENIED"
    reverted = tool.execute({"action": "revert", "change_id": history[0]["id"]})
    assert reverted.ok and load_capability_config(tmp_path / "capability_config.yaml").capability_candidate_limit == 5