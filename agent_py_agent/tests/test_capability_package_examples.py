"""三个真实内容样包的构建与私有脚本组件验证；不安装、不联网，不代表真实 TUI 验收。"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest

from agent_py_agent.agent.plugin_package import inspect_plugin_package
from scripts.build_capability_package import build_capability_package

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples" / "capability-packages"
PACKAGES = ("drama-text-a", "drama-workflow-b", "security-evidence")


# LLM: 只读取仓库内明确的公开合成夹具，不读取用户 home 或真实任务材料。
# 函数用途: 取得指定样包内的 JSON 测试数据。
def _fixture(package: str, name: str) -> dict:
    return json.loads((EXAMPLES / package / "resources" / name).read_text(encoding="utf-8"))


# LLM: 测试输入仅落在 pytest 临时目录，使用和样包一致的 UTF-8 JSON；返回真实写入路径。
# 函数用途: 为脚本组件创建可变的独立输入文件。
def _write_json(directory: Path, name: str, payload: dict) -> Path:
    path = directory / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


# LLM: 以隔离解释器执行样包脚本，清空私有环境、禁止字节码写入；这不是产品代理的业务执行证据。
# 函数用途: 运行纯标准库脚本并断言只读输入，不产生任何附加文件。
def _run(package: str, script: str, directory: Path, arguments: list[str]) -> subprocess.CompletedProcess:
    before = sorted(str(path.relative_to(directory)) for path in directory.rglob("*"))
    environment = {"PATH": os.defpath}
    if "SYSTEMROOT" in os.environ:
        environment["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-X", "utf8", str(EXAMPLES / package / "scripts" / script), *arguments],
        cwd=directory, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=15,
    )
    assert sorted(str(path.relative_to(directory)) for path in directory.rglob("*")) == before
    assert not result.stderr, result.stderr
    return result


# LLM: 只按测试数据里的结构化路径改一个事实，原夹具文件不改。
# 函数用途: 构造有针对性的断链和类型错误反例。
def _replace(payload: dict, path: tuple, value: object) -> dict:
    result = copy.deepcopy(payload)
    target = result
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    return result


# LLM: 文本检查固定同一原文字节，来源变更测试显式重写，不借用旧摘要掩盖改动。
# 函数用途: 将短剧 A 的原文和交付资料复制到临时目录并运行检查。
def _check_a(tmp_path: Path, delivery: dict, source: dict | None = None) -> dict:
    source = source or _fixture("drama-text-a", "example-source.json")
    source_path = _write_json(tmp_path, "source.json", source)
    delivery_path = _write_json(tmp_path, "delivery.json", delivery)
    result = _run("drama-text-a", "check_delivery.py", tmp_path,
                  ["--source", str(source_path), "--delivery", str(delivery_path)])
    parsed = json.loads(result.stdout)
    assert result.returncode == (0 if parsed["structure_valid"] else 1)
    return parsed


@pytest.mark.parametrize("package", PACKAGES)
def test_samples_build_reproducibly_and_expose_only_one_package(package, tmp_path):
    root = EXAMPLES / package
    declaration = json.loads((root / "declaration.json").read_text(encoding="utf-8"))
    first = build_capability_package(declaration, root, tmp_path / "first.zip")
    second = build_capability_package(declaration, root, tmp_path / "second.zip")
    assert first.read_bytes() == second.read_bytes()
    inspected = inspect_plugin_package(first.read_bytes())
    manifest = inspected.manifest
    assert manifest.is_content_only and manifest.plugin_id == package
    assert manifest.skills == () and manifest.tools == () and manifest.actions == ()
    assert manifest.to_payload()["schema_version"] == "plugin_package.v7"
    assert manifest.to_payload()["package_kind"] == "capability"
    assert manifest.capability.entry_document == "CAPABILITY.md"
    members = {item.path: item for item in manifest.files}
    assert {"CAPABILITY.md", "PROVENANCE.md", "LICENSE", "methods/workflow.md", "methods/review.md"} <= set(members)
    assert any(path.startswith("licenses/") for path in members)
    assert "declaration.json" not in members
    assert all(item.executable is False for item in members.values())
    with ZipFile(first) as archive:
        assert set(archive.namelist()) == {"plugin.json", *members}
        for name, member in members.items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == member.sha256
            assert archive.read(name) == (root / name).read_bytes()


def test_packages_keep_same_relative_method_independent(tmp_path):
    texts, digests = set(), set()
    for package in PACKAGES:
        root = EXAMPLES / package
        declaration = json.loads((root / "declaration.json").read_text(encoding="utf-8"))
        bundle = build_capability_package(declaration, root, tmp_path / f"{package}.zip")
        manifest = inspect_plugin_package(bundle.read_bytes()).manifest
        method = next(item for item in manifest.files if item.path == "methods/review.md")
        digests.add(method.sha256)
        with ZipFile(bundle) as archive:
            texts.add(archive.read(method.path))
    assert len(texts) == len(digests) == 3


def test_text_sample_checks_complete_source_and_shot_coverage(tmp_path):
    report = _check_a(tmp_path, _fixture("drama-text-a", "example-delivery.json"))
    assert report["structure_valid"]
    assert report["metrics"] == {"passages": 3, "covered_passages": 3, "omitted_passages": 0,
                                 "scenes": 3, "shots": 3, "scene_seconds": 60.0}
    assert {item["code"] for item in report["warnings"]} == {"creative_quality_and_media_not_checked"}


@pytest.mark.parametrize("path,value,code", [
    (("source_sha256",), "0" * 64, "source_digest_mismatch"),
    (("scenes", 0, "source_ids"), ["missing"], "unknown_reference"),
    (("scenes", 0, "source_ids"), ["P02"], "unaccounted_source"),
    (("scenes", 0, "character_ids"), 1, "references_required"),
    (("shots", 0, "scene_id"), "missing", "unknown_scene"),
    (("shots", 0, "visible_character_ids"), ["C02"], "unknown_reference"),
    (("shots", 0, "end_state"), "", "shot_state_required"),
    (("scenes", 0, "seconds"), True, "positive_seconds_required"),
    (("omitted_passages",), [{"source_id": "P01", "reason": "重复声明省略"}], "invalid_omission"),
])
def test_text_sample_rejects_changed_basis_and_broken_links(path, value, code, tmp_path):
    delivery = _replace(_fixture("drama-text-a", "example-delivery.json"), path, value)
    report = _check_a(tmp_path, delivery)
    assert not report["structure_valid"]
    assert code in {item["code"] for item in report["errors"]}


def test_text_sample_keeps_explicit_omissions_visible(tmp_path):
    source = _fixture("drama-text-a", "example-source.json")
    source["passages"].append({"id": "P04", "text": "原文另有一段风景描写。"})
    raw = (json.dumps(source, ensure_ascii=False, indent=2) + "\n").encode()
    delivery = _fixture("drama-text-a", "example-delivery.json")
    delivery["source_sha256"] = hashlib.sha256(raw).hexdigest()
    delivery["omitted_passages"] = [{"source_id": "P04", "reason": "本次短片省略独立风景段落。"}]
    report = _check_a(tmp_path, delivery, source)
    assert report["structure_valid"] and report["metrics"]["omitted_passages"] == 1
    assert "explicit_omissions_need_review" in {item["code"] for item in report["warnings"]}


def test_workflow_sample_keeps_media_unverified(tmp_path):
    path = _write_json(tmp_path, "project.json", _fixture("drama-workflow-b", "example-project.json"))
    result = _run("drama-workflow-b", "check_continuity.py", tmp_path, ["--project", str(path)])
    report = json.loads(result.stdout)
    assert result.returncode == 0 and report["structure_valid"]
    assert report["metrics"] == {"episodes": 1, "scenes": 1, "shots": 3, "shot_seconds": 60.0}
    assert sum(item["code"] == "reference_media_not_verified" for item in report["warnings"]) == 2


@pytest.mark.parametrize("path,value,code", [
    (("scenes", 0, "episode_id"), "missing", "unknown_reference"),
    (("scenes", 0, "prop_ids"), ["missing"], "unknown_reference"),
    (("scenes", 0, "beats", 1, "character_id"), "missing", "speaker_outside_scene"),
    (("shots", 0, "beat_ids"), [], "uncovered_beat"),
    (("shots", 0, "reference_ids"), ["missing"], "unknown_reference"),
    (("references", 0, "kind"), [], "unknown_reference_subject"),
    (("shots", 0, "seconds"), -1, "positive_seconds_required"),
])
def test_workflow_sample_rejects_cross_table_breaks(path, value, code, tmp_path):
    project = _replace(_fixture("drama-workflow-b", "example-project.json"), path, value)
    input_path = _write_json(tmp_path, "project.json", project)
    result = _run("drama-workflow-b", "check_continuity.py", tmp_path, ["--project", str(input_path)])
    report = json.loads(result.stdout)
    assert result.returncode == 1 and not report["structure_valid"]
    assert code in {item["code"] for item in report["errors"]}


def test_workflow_html_treats_story_text_as_data(tmp_path):
    project = _fixture("drama-workflow-b", "example-project.json")
    project["title"] = "<script>alert('示例')</script>"
    project["characters"][0]["name"] = '<img src="x" onerror="alert(1)">'
    path = _write_json(tmp_path, "project.json", project)
    result = _run("drama-workflow-b", "check_continuity.py", tmp_path,
                  ["--project", str(path), "--format", "html"])
    assert result.returncode == 0
    assert "<script>" not in result.stdout and "<img" not in result.stdout
    assert "&lt;script&gt;" in result.stdout and "&lt;img" in result.stdout
    assert "图片、声音、成片和创作质量尚未验证" in result.stdout


def test_evidence_sample_deduplicates_without_promoting_claims(tmp_path):
    document = _fixture("security-evidence", "example-evidence.json")
    document["verification"] = "confirmed"
    document["findings"][0]["verification"] = "confirmed"
    path = _write_json(tmp_path, "evidence.json", document)
    result = _run("security-evidence", "summarize_evidence.py", tmp_path, ["--input", str(path)])
    report = json.loads(result.stdout)
    assert result.returncode == 0 and report["structure_valid"]
    assert report["verification"] == "not_performed" and report["network_actions"] == 0
    assert report["authorization_verified"] is False
    assert len(report["grouped_evidence"]) == 1
    assert report["grouped_evidence"][0]["aliases"] == ["EV01", "EV02"]
    assert len(report["grouped_evidence"][0]["source_refs"]) == 2
    assert report["findings"][0]["evidence_ids"] == ["EV01"]
    assert report["findings"][0]["verification"] == "not_performed"
    assert report["findings"][0]["claim"] == document["findings"][0]["claim"]


def test_evidence_sample_never_deduplicates_different_assets(tmp_path):
    document = _fixture("security-evidence", "example-evidence.json")
    document["scope"]["asset_ids"].append("lab-other")
    document["evidence"][1]["asset_id"] = "lab-other"
    document["findings"][0]["evidence_ids"] = ["EV01"]
    path = _write_json(tmp_path, "evidence.json", document)
    result = _run("security-evidence", "summarize_evidence.py", tmp_path, ["--input", str(path)])
    report = json.loads(result.stdout)
    assert result.returncode == 0 and len(report["grouped_evidence"]) == 2


@pytest.mark.parametrize("path,value,code", [
    (("scope", "authorization_ref"), "", "authorization_reference_required"),
    (("scope", "asset_ids"), [], "asset_scope_required"),
    (("scope", "allowed_operations"), ["network_scan"], "unsupported_operation_scope"),
    (("evidence", 0, "sha256"), "0" * 64, "evidence_digest_mismatch"),
    (("evidence", 0, "asset_id"), "outside", "asset_outside_scope"),
    (("findings", 0, "evidence_ids"), ["missing"], "unknown_evidence"),
])
def test_evidence_sample_rejects_scope_and_receipt_errors(path, value, code, tmp_path):
    document = _replace(_fixture("security-evidence", "example-evidence.json"), path, value)
    input_path = _write_json(tmp_path, "evidence.json", document)
    result = _run("security-evidence", "summarize_evidence.py", tmp_path, ["--input", str(input_path)])
    report = json.loads(result.stdout)
    assert result.returncode == 1 and not report["structure_valid"]
    assert code in {item["code"] for item in report["errors"]}
    assert report["grouped_evidence"] == [] and report["verification"] == "not_performed"


def test_evidence_sample_rejects_a_finding_using_another_assets_evidence(tmp_path):
    document = _fixture("security-evidence", "example-evidence.json")
    document["scope"]["asset_ids"].append("lab-other")
    document["evidence"][1]["asset_id"] = "lab-other"
    path = _write_json(tmp_path, "evidence.json", document)
    result = _run("security-evidence", "summarize_evidence.py", tmp_path, ["--input", str(path)])
    report = json.loads(result.stdout)
    assert result.returncode == 1
    assert "cross_asset_evidence" in {item["code"] for item in report["errors"]}


@pytest.mark.parametrize("package,script,argument", [
    ("drama-text-a", "check_delivery.py", "--source"),
    ("drama-workflow-b", "check_continuity.py", "--project"),
    ("security-evidence", "summarize_evidence.py", "--input"),
])
@pytest.mark.parametrize("raw", ['{"scope": 1, "scope": 2}', '{"seconds": NaN}'])
def test_sample_scripts_reject_ambiguous_json(package, script, argument, raw, tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text(raw, encoding="utf-8")
    arguments = [argument, str(path)]
    if package == "drama-text-a":
        arguments += ["--delivery", str(EXAMPLES / package / "resources" / "example-delivery.json")]
    result = _run(package, script, tmp_path, arguments)
    assert result.returncode == 1
    assert json.loads(result.stdout)["errors"][0]["code"] == "invalid_input"
