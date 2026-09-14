"""未登记格式的通用兜底打开器 + 打开器热加载的钉子测试。

钉死三件事，对齐 长期助手/工具运行时/sample-app 运行时遇到新格式的可靠做法：
1. 未登记格式不拒绝：有合同声明就用兜底打开器按"长相"（文本/zip）尽量校验；没声明落第 0 层放行。
2. 运行时丢一个打开器脚本即可支持新格式深度校验，不改主代码、不重启。
3. 坏脚本被隔离：加载异常只记录、不打断主链路，已注册的好打开器不丢。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from agent_py_agent.agent.contracts import artifact_openers as openers_mod
from agent_py_agent.agent.contracts.artifact_acceptance import validate_artifact
from agent_py_agent.agent.contracts.artifact_acceptance_models import ArtifactAcceptanceRequest


def test_unknown_format_with_required_strings_uses_fallback_text_check(tmp_path: Path) -> None:
    target = tmp_path / "report.weirdext"
    target.write_text("标题\n这里没有必须的关键字\n", encoding="utf-8")
    report = validate_artifact(
        ArtifactAcceptanceRequest(path=target, validation_contract={"required_strings": ["必须出现的句子"]})
    )
    assert not report.ok
    assert any(f.code == "ARTIFACT_REQUIRED_TEXT_MISSING" for f in report.findings)


def test_unknown_format_without_contract_passes_layer_zero(tmp_path: Path) -> None:
    target = tmp_path / "thing.zzz"
    target.write_text("anything", encoding="utf-8")
    report = validate_artifact(ArtifactAcceptanceRequest(path=target))
    assert report.ok


def test_unknown_zip_format_checks_required_members(tmp_path: Path) -> None:
    archive = tmp_path / "pack.myzip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("a.txt", "x")
    report = validate_artifact(
        ArtifactAcceptanceRequest(path=archive, validation_contract={"required_files": ["b.txt"]})
    )
    assert not report.ok
    assert any(f.code == "ARTIFACT_REQUIRED_MEMBER_MISSING" for f in report.findings)


def test_plugin_opener_is_hot_loaded_from_directory(tmp_path: Path, monkeypatch) -> None:
    openers_dir = tmp_path / "openers"
    openers_dir.mkdir()
    (openers_dir / "myfmt.py").write_text(
        "from agent_py_agent.agent.contracts.artifact_openers import OpenedArtifact, GenericView\n"
        "def register(register_opener):\n"
        "    def open_myfmt(path):\n"
        "        return OpenedArtifact('myfmt', view=GenericView(text=path.read_text(encoding='utf-8')))\n"
        "    register_opener('myfmt', open_myfmt)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MY_AGENT_OPENERS_DIR", str(openers_dir))
    _reset_opener_state()
    openers_mod.discover_openers(force=True)
    assert openers_mod.opener_for("myfmt") is not None


def test_broken_opener_script_is_isolated(tmp_path: Path, monkeypatch) -> None:
    openers_dir = tmp_path / "openers"
    openers_dir.mkdir()
    (openers_dir / "good.py").write_text(
        "from agent_py_agent.agent.contracts.artifact_openers import OpenedArtifact, GenericView\n"
        "def register(register_opener):\n"
        "    register_opener('goodfmt', lambda path: OpenedArtifact('goodfmt', view=GenericView(text='ok')))\n",
        encoding="utf-8",
    )
    (openers_dir / "broken.py").write_text("import nonexistent_module_zzz\n", encoding="utf-8")
    monkeypatch.setenv("MY_AGENT_OPENERS_DIR", str(openers_dir))
    _reset_opener_state()
    openers_mod.discover_openers(force=True)
    assert openers_mod.opener_for("goodfmt") is not None
    assert any("broken.py" in str(err.get("path", "")) for err in openers_mod.OPENER_LOAD_ERRORS)


def test_plugin_cannot_override_builtin_opener(tmp_path: Path) -> None:
    _reset_opener_state()
    openers_mod.register_opener("csv", lambda path: None)
    # 内置 csv 打开器仍是原来的，没被插件覆盖
    assert openers_mod.opener_for("csv") is openers_mod.open_csv
    assert any("built-in" in str(err.get("error", "")) for err in openers_mod.OPENER_LOAD_ERRORS)


def _reset_opener_state() -> None:
    openers_mod._PLUGIN_OPENERS.clear()
    openers_mod.OPENER_LOAD_ERRORS.clear()
    openers_mod._DISCOVERED = False
