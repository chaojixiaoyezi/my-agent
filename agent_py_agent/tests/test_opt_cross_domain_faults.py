# LLM: 只用当前owner形状的内存替身核对投影边界；不读取真实home、不执行迁移或网络。
# 模块用途: 覆盖DoctorService的范围隔离、固定旧退出码、缓存键和失败传播。
from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
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


# LLM: 只构造结构核验所需字段，不用清单的自报完成状态充当删除证据。
# 函数用途: 创建临时删除门测试行。
def _removal_row(path, old="subagent-run"):
    return {"id": "fixture", "implementation_worker": "sol-9", "retained_contract_correction": False,
            "remove_code": "remove_obsolete_generated_input_or_callsite",
            "locations": [{"path": path, "symbol": "生成内容中的 " + old}]}


# LLM: 历史请求ID不可改写，但同文件中的旧argv必须被抓住，不能扩大保留豁免。
# 函数用途: 防止持久ID白名单被用来隐藏真实旧CLI调用。
def test_cli_removal_gate_distinguishes_persisted_id_from_argv(tmp_path):
    from scripts.verify_optimization_removal import check_row
    relative = "agent_py_agent/cli/scenario_cases/subagent_cases.py"
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    row = _removal_row(relative)
    path.write_text('def sample(task):\n    return invoke(request_id=f"subagent-run:{task.id}")\n')
    assert check_row(tmp_path, row, {}, {})["status"] == "pass"
    path.write_text('def sample():\n    return invoke("subagent-run")\n')
    assert check_row(tmp_path, row, {}, {})["status"] == "fail"


# LLM: 下划线属于完整工具名，不能把list_tools解析成list而误伤有效业务字段。
# 函数用途: 固定删除门的精确标记边界。
def test_cli_removal_gate_uses_full_underscore_symbol(tmp_path):
    from scripts.verify_optimization_removal import check_row
    path = tmp_path / "producer.py"
    row = _removal_row("producer.py", "list_tools")
    path.write_text('invoke("list")\n')
    assert check_row(tmp_path, row, {}, {})["status"] == "pass"
    path.write_text('invoke("list_tools")\n')
    assert check_row(tmp_path, row, {}, {})["status"] == "fail"


# LLM: 无精确结构合同的领域行必须待复核，不能因没有实现的检查就返回成功。
# 函数用途: 防止全量383项欠账被静默抹平。
def test_cli_removal_gate_unknown_semantics_stay_pending(tmp_path):
    from scripts.verify_optimization_removal import check_row
    row = _removal_row("unknown.py")
    row["remove_code"] = "unencoded_domain_semantics"
    assert check_row(tmp_path, row, {}, {})["status"] == "pending_semantic_check"


# LLM: 删除门必须发现显式动态导入和getattr消费者；普通历史说明不当成执行消费者。
# 函数用途: 覆盖AST反射引用扫描，避免只grep静态import的漏检。
def test_cli_removal_gate_finds_explicit_dynamic_consumers(tmp_path):
    from scripts.verify_optimization_removal import forbidden_references
    path = tmp_path / "agent_py_agent/consumer.py"
    path.parent.mkdir()
    path.write_text('import importlib\nimportlib.import_module("agent_py_agent.cli.memory_doctor")\ngetattr(service, "cmd_memory_doctor")\n')
    hits = forbidden_references(tmp_path, modules=("agent_py_agent.cli.memory_doctor",), symbols=("cmd_memory_doctor",))
    assert {h["kind"] for h in hits} == {"dynamic_import", "dynamic_symbol"}


# LLM: 显式报告参数也不能覆盖产品JSON；测试用临时检出，不碰真实owner或源码清单。
# 函数用途: 验证删除门输出保护不只保护自身文件。
def test_cli_removal_gate_refuses_product_json_output(tmp_path, monkeypatch):
    from scripts import verify_optimization_removal as gate
    target = tmp_path / "product.json"
    target.write_bytes(b'{"keep":true}')
    monkeypatch.setattr(gate, "verify", lambda *args, **kwargs: {"status": "pass"})
    with pytest.raises(SystemExit) as exc:
        gate.main(["--repo", str(tmp_path), "--output", str(target)])
    assert exc.value.code == 2
    assert target.read_bytes() == b'{"keep":true}'


# LLM: 失败与未知状态的退出码独立，旧audit委托不能把它们转成0。
# 函数用途: 核对唯一删除门CLI的0/1/2及安全报告回读。
@pytest.mark.parametrize("status,code", [("pass", 0), ("fail", 1), ("incomplete", 2)])
def test_cli_removal_gate_exit_status_and_report(tmp_path, monkeypatch, status, code):
    from scripts import verify_optimization_removal as gate
    target = tmp_path / ".pytest_cache/report.json"
    monkeypatch.setattr(gate, "verify", lambda *args, **kwargs: {"status": status})
    assert gate.main(["--repo", str(tmp_path), "--output", str(target)]) == code
    assert json.loads(target.read_text())["status"] == status


# LLM: 两项保留合同必须是准确ID，不能仅凑够两个布尔标记。
# 函数用途: 验证清单篡改在装配目标CLI之前就被拒绝。
def test_cli_removal_gate_rejects_wrong_retained_ids(tmp_path, monkeypatch):
    from scripts import verify_optimization_removal as gate
    data = json.loads(gate.MANIFEST.read_text())
    for row in data["rows"]:
        if row["id"] == "SURF-DEL-195":
            row["retained_contract_correction"] = False
        if row["id"] == "SURF-DEL-017":
            row["retained_contract_correction"] = True
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data))
    forbidden = Mock(side_effect=AssertionError("不应装配parser"))
    monkeypatch.setattr(gate, "cli_snapshot", forbidden)
    with pytest.raises(ValueError, match="保留合同"):
        gate.verify(tmp_path, manifest_path=path)
    forbidden.assert_not_called()


# LLM: 跨域新错误分类只声明恢复口径，不因匹配错误码授予重试或迁移权。
# 函数用途: 防止三项新错误码落入未知类或自动重放。
@pytest.mark.parametrize("code", ["STORE_SCHEMA_NEWER", "TOOL_SCHEMA_INVALID_MODE_COMBINATION", "MEMORY_COMMIT_OBLIGATION_QUARANTINED"])
def test_cli_cross_domain_error_contracts_are_nonretryable(code):
    from agent_py_agent.agent.contracts.error_taxonomy import error_contract
    contract = error_contract(code)
    assert contract.code == code
    assert contract.retryable is False


# LLM: 报告目标即使硬链接到产品文件，也不能原地截断共享inode；只用临时样本复现。
# 函数用途: 钉住独审发现的报告硬链接覆盖反例。
def test_cli_removal_gate_report_does_not_truncate_hardlink(tmp_path, monkeypatch):
    import os

    from scripts import verify_optimization_removal as gate
    product = tmp_path / "product.json"
    product.write_bytes(b'{"keep":true}')
    target = tmp_path / ".pytest_cache/report.json"
    target.parent.mkdir()
    os.link(product, target)
    monkeypatch.setattr(gate, "verify", lambda *args, **kwargs: {"status": "pass"})
    assert gate.main(["--repo", str(tmp_path), "--output", str(target)]) == 0
    assert product.read_bytes() == b'{"keep":true}'
    assert json.loads(target.read_text())["status"] == "pass"
    assert not target.samefile(product)


# LLM: .pytest_cache本身被替换为指向产品目录的符号链接时，也必须拒绝写入。
# 函数用途: 验证报告目录的真实归属而不只校验用户输入前缀。
def test_cli_removal_gate_rejects_symlinked_report_directory(tmp_path, monkeypatch):
    from scripts import verify_optimization_removal as gate
    product = tmp_path / "product"
    product.mkdir()
    (tmp_path / ".pytest_cache").symlink_to(product, target_is_directory=True)
    monkeypatch.setattr(gate, "verify", lambda *args, **kwargs: {"status": "pass"})
    with pytest.raises(SystemExit) as exc:
        gate.main(["--repo", str(tmp_path), "--output", str(tmp_path / ".pytest_cache/report.json")])
    assert exc.value.code == 2
    assert not list(product.iterdir())


# LLM: 调用者修改公开报告不能污染同一次请求内的缓存健康值；这不扩大为跨CLI实例缓存。
# 函数用途: 复现并隔离可变诊断结果的共享引用。
def test_cli_doctor_report_mutation_does_not_poison_cached_health(doctor_agent):
    service = doctor.DoctorService(doctor_agent)
    first = service.report(Namespace(), legacy=True)
    first["routing"]["load_error"] = ""
    second = service.report(Namespace(), legacy=True)
    assert second["routing"]["load_error"] == "missing"
    assert second["ok"] is False


# LLM: 此单元观察验证原子替换原语，不冒充被主机拒绝的真实硬链接集成验证。
# 函数用途: 在所有平台都核对报告先写临时件再替换目标目录项。
def test_cli_removal_gate_report_uses_atomic_replace(tmp_path, monkeypatch):
    from scripts import verify_optimization_removal as gate
    target = tmp_path / ".pytest_cache/report.json"
    target.parent.mkdir()
    replace = Mock(wraps=gate.os.replace)
    monkeypatch.setattr(gate.os, "replace", replace)
    monkeypatch.setattr(gate, "verify", lambda *args, **kwargs: {"status": "pass"})
    assert gate.main(["--repo", str(tmp_path), "--output", str(target)]) == 0
    replace.assert_called_once()
    source, destination = replace.call_args.args
    assert destination == target
    assert target.parent == Path(source).parent
    assert source != str(target)
    assert json.loads(target.read_text())["status"] == "pass"
    assert list(target.parent.iterdir()) == [target]


# LLM: CLI文档根目录正确不能替代其余平台文档的语义核对；缺检查保持待复核。
# 函数用途: 防止RD-15以局部入口证明整条文档收口。
def test_cli_removal_gate_does_not_overclaim_rd15_documentation(tmp_path):
    from scripts.verify_optimization_removal import check_row
    (tmp_path / "CLI_REFERENCE.md").write_text("`my-agent memory`\n")
    row = _removal_row("CLI_REFERENCE.md")
    row.update(id="RD-15", remove_code="documentation_reconciliation", locations=[{"path": "CLI_REFERENCE.md"}, {"path": "AGENTS.md"}])
    result = check_row(tmp_path, row, {"canonical_roots": ["memory"]}, {})
    assert result["status"] == "pending_semantic_check"
    assert result["evidence"]["own_cli_roots_verified"] is True
    assert result["evidence"]["remaining_documentation"] == ["AGENTS.md"]


# LLM: 文本合同必须读取准确仓内文件；旧字面量未移除时不得放行。
# 函数用途: 支持集成者补充逐文档精确规则，不依赖自报完成字段。
def test_cli_removal_gate_checks_exact_documentation_text(tmp_path):
    from scripts.verify_optimization_removal import check_row
    path = tmp_path / "doc.md"
    row = _removal_row("doc.md")
    row.update(remove_code="documentation_reconciliation", verification_checks=[{"kind": "text_absent", "path": "doc.md", "text": "old-target"}])
    path.write_text("old-target")
    assert check_row(tmp_path, row, {}, {})["status"] == "fail"
    path.write_text("canonical-target")
    assert check_row(tmp_path, row, {}, {})["status"] == "pass"
