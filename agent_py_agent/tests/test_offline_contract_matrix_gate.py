# LLM: 矩阵只检查真实合同路径，不执行业务测试；无尺寸产物应通过，真实缺项和CLI失败必须继续被检出。
# 模块用途: 验证离线合同覆盖、输出字段及尺寸退役后的正反例。
from __future__ import annotations

from pathlib import Path


# LLM: 检查实际仓库的合同索引和CLI出口，不再依赖已退役尺寸字段。
# 函数用途: 验证当前仓库的真实离线合同文件齐全。
def test_offline_contract_matrix_gate_passes_current_repo() -> None:
    from scripts.check_offline_contract_matrix import check_offline_contract_matrix, main

    repo_root = Path(__file__).resolve().parents[2]

    report = check_offline_contract_matrix(repo_root)

    assert report.ok is True
    assert report.missing_areas == ()
    assert main(["--repo-root", str(repo_root), "--json"]) == 0


# LLM: 空合同树必须失败；无需伪造尺寸报告使真实缺项分支可达。
# 函数用途: 保留真实合同文件缺失时的失败回归。
def test_offline_contract_matrix_gate_reports_missing_files(tmp_path: Path) -> None:
    from scripts.check_offline_contract_matrix import check_offline_contract_matrix

    report = check_offline_contract_matrix(tmp_path)

    assert report.ok is False
    assert "concurrency" in report.missing_areas
    assert report.findings[0]["code"] == "OFFLINE_MATRIX_FILE_MISSING"


def _required_matrix_paths() -> tuple[str, ...]:
    from scripts.check_offline_contract_matrix import REQUIRED_AREAS

    return tuple(path for paths in REQUIRED_AREAS.values() for path in paths)


# LLM: 这里只验证矩阵的文件存在性合同；临时占位不代表对应业务测试已经执行，不生成任何尺寸报告。
# 函数用途: 在临时目录准备真实REQUIRED_AREAS路径，可故意缺少一个文件以验证失败分支。
def _write_contract_paths(root: Path, *, missing: str | None = None) -> None:
    for rel in _required_matrix_paths():
        if rel == missing:
            continue
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# file-presence fixture, not executable contract evidence\n", encoding="utf-8")


# LLM: 尺寸机制退役后，真实合同文件齐全即可通过，不依赖报告、基线或假零值。
# 函数用途: 验证无尺寸产物的完整合同树可以通过矩阵检查。
def test_offline_contract_matrix_without_size_artifacts(tmp_path: Path) -> None:
    from scripts.check_offline_contract_matrix import check_offline_contract_matrix

    _write_contract_paths(tmp_path)
    report = check_offline_contract_matrix(tmp_path)
    assert report.ok is True
    assert report.missing_areas == ()
    assert report.findings == ()


# LLM: 公共JSON与报告对象都应移除尺寸派生字段，不接受恒定False/0作为兼容空壳。
# 函数用途: 验证CLI成功退出并仅输出真实合同覆盖字段。
def test_offline_contract_matrix_json_has_only_contract_fields(tmp_path: Path, capsys) -> None:
    import json
    from dataclasses import fields

    from scripts.check_offline_contract_matrix import check_offline_contract_matrix, main

    _write_contract_paths(tmp_path)
    assert main(["--repo-root", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"ok": True, "missing_areas": [], "findings": []}
    report = check_offline_contract_matrix(tmp_path)
    assert {item.name for item in fields(report)} == set(payload)


# LLM: 仅解除尺寸依赖，真实合同缺项必须仍是失败及非零CLI退出，不放宽为恒定通过。
# 函数用途: 验证缺一个真实合同文件时准确返回归属、路径和失败状态。
def test_offline_contract_matrix_missing_contract_still_fails_without_report(tmp_path: Path, capsys) -> None:
    import json

    from scripts.check_offline_contract_matrix import check_offline_contract_matrix, main

    missing = "agent_py_agent/agent/contracts/offline_recovery_contract.py"
    _write_contract_paths(tmp_path, missing=missing)
    report = check_offline_contract_matrix(tmp_path)
    assert report.ok is False
    assert report.missing_areas == ("recovery",)
    assert report.findings == ({"code": "OFFLINE_MATRIX_FILE_MISSING", "area": "recovery", "detail": missing},)
    assert main(["--repo-root", str(tmp_path), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
