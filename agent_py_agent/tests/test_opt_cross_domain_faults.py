# LLM: 只用当前owner形状的内存替身核对投影边界；不读取真实home、不执行迁移或网络。
# 模块用途: 覆盖DoctorService的范围隔离、固定旧退出码、缓存键和失败传播。
from __future__ import annotations

import json
from argparse import Namespace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.user_space import home_doctor as doctor
from agent_py_agent.cli import memory_admin_commands as admin


# LLM: 数据只含健康摘要，无候选正文；所有写入方法如被误调用都会立即失败。
# 函数用途: 构造诊断所需的最小显式Agent形状。
@pytest.fixture
def doctor_agent(tmp_path, monkeypatch):
    home = SimpleNamespace(owner_id="local/main")
    agent = SimpleNamespace(root=tmp_path, home_paths=home,
                            memory_candidates=SimpleNamespace(stats=lambda: {"count": 0}),
                            memory_curator=SimpleNamespace(status=lambda: {"status": "idle"}))
    common = {"config": {}, "warnings": [], "routing": {"load_error": "missing"}, "archive": {}}
    monkeypatch.setattr(doctor.DoctorService, "_common", lambda self, args, runtime: dict(common))
    monkeypatch.setattr(doctor, "build_home_doctor_report", lambda home: {"home": {"root": "fixture"}})
    report = SimpleNamespace(ok=False, to_dict=lambda: {"ok": False})
    service = SimpleNamespace(plan=lambda: report)
    monkeypatch.setattr(admin, "_migration_service", lambda agent: service)
    monkeypatch.setattr(admin, "_retention_service", lambda agent: service)
    monkeypatch.setattr(admin, "make_agent", lambda args: agent)
    return agent


# LLM: 相同的失败健康结果，在历史runtime投影中固定0，在规范memory投影中仍为1。
# 函数用途: 防止合并服务时混淆两种既有退出码。
def test_cli_doctor_legacy_and_canonical_failure_exit(doctor_agent, capsys):
    assert admin.cmd_memory_v2_doctor(Namespace(compat_view="v1", json=True)) == 0
    old = json.loads(capsys.readouterr().out)
    assert old["ok"] is False and "home" in old and "warnings" in old
    assert admin.cmd_memory_v2_doctor(Namespace(compat_view="", scope="memory", json=True)) == 1
    new = json.loads(capsys.readouterr().out)
    assert new["ok"] is False and "candidate" in new and "config_warnings" in new
    assert "home_doctor" not in new


# LLM: runtime不依赖memory迁移工厂；单个范围不能多读另一facet。
# 函数用途: 确认runtime投影不调用memory组件。
def test_cli_runtime_scope_does_not_plan_memory(doctor_agent, monkeypatch):
    forbidden = Mock(side_effect=AssertionError("不能初始化memory组件"))
    monkeypatch.setattr(admin, "_migration_service", forbidden)
    result = doctor.DoctorService(doctor_agent).report(Namespace(), scope="runtime")
    assert result["scope"] == "runtime" and result["runtime"]["ok"] is False
    forbidden.assert_not_called()


# LLM: all必须保持两个独立facet及其公共范围，不因一个失败把另一个伪造为成功。
# 函数用途: 验证整体健康是两个原条件的合取。
def test_cli_all_scope_has_both_facets(doctor_agent):
    result = doctor.DoctorService(doctor_agent, migration_factory=admin._migration_service, retention_factory=admin._retention_service).report(Namespace(), scope="all")
    assert result["ok"] is False
    assert {"runtime", "memory", "common"} <= set(result)
    assert set(result["common"]) == {"config", "routing", "archive", "warnings"}
    for facet in (result["runtime"], result["memory"]):
        assert not {"config", "routing", "archive", "warnings", "config_warnings"} & facet.keys()


# LLM: 未知范围是合同错误，不降级成成功或默认memory。
# 函数用途: 钉住封闭scope输入。
def test_cli_unknown_doctor_scope_is_rejected(doctor_agent):
    with pytest.raises(ValueError):
        doctor.DoctorService(doctor_agent).report(Namespace(), scope="invented")


# LLM: 请求缓存须区分owner、验证根、索引和读取范围；替身不访问真实home或运行模型。
# 函数用途: 验证精确相同请求复用，范围或身份变化重新读取。
def test_cli_doctor_common_cache_is_request_and_scope_bound(tmp_path, monkeypatch):
    agent = SimpleNamespace(root=tmp_path, home_paths=SimpleNamespace(owner_id="local/main"), config=object())
    monkeypatch.setattr(doctor, "resolve_route_index_target", lambda root, index, home_paths: SimpleNamespace(authority_root=root, path=root / (index or "index.md")))
    monkeypatch.setattr(doctor, "_memory_config_payload", lambda config: {})
    monkeypatch.setattr(doctor, "_config_warnings", lambda config: [])
    routing, archive = Mock(return_value={"load_error": ""}), Mock(return_value={})
    monkeypatch.setattr(doctor, "_build_routing_doctor", routing)
    monkeypatch.setattr(doctor, "_build_archive_doctor", archive)
    service = doctor.DoctorService(agent)
    args = Namespace(index=None)
    assert service._common(args, runtime=True) is service._common(args, runtime=True)
    assert routing.call_count == archive.call_count == 1
    service._common(args, runtime=False)
    service._common(Namespace(index="other.md"), runtime=True)
    agent.home_paths.owner_id = "local/other"
    service._common(args, runtime=True)
    assert routing.call_count == archive.call_count == 4


# LLM: 跨域新错误分类只声明恢复口径，不因匹配错误码授予重试或迁移权。
# 函数用途: 防止三项新错误码落入未知类或自动重放。
@pytest.mark.parametrize("code", ["STORE_SCHEMA_NEWER", "TOOL_SCHEMA_INVALID_MODE_COMBINATION", "MEMORY_COMMIT_OBLIGATION_QUARANTINED"])
def test_cli_cross_domain_error_contracts_are_nonretryable(code):
    from agent_py_agent.agent.contracts.error_taxonomy import error_contract
    contract = error_contract(code)
    assert contract.code == code
    assert contract.retryable is False


# LLM: 调用者修改公开报告不能污染同一次请求内的缓存健康值；这不扩大为跨CLI实例缓存。
# 函数用途: 复现并隔离可变诊断结果的共享引用。
def test_cli_doctor_report_mutation_does_not_poison_cached_health(doctor_agent):
    service = doctor.DoctorService(doctor_agent)
    first = service.report(Namespace(), legacy=True)
    first["routing"]["load_error"] = ""
    second = service.report(Namespace(), legacy=True)
    assert second["routing"]["load_error"] == "missing"
    assert second["ok"] is False
