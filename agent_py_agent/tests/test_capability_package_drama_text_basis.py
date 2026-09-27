# LLM: 仅检查 A 包新版资料中的显式来源关系；公开合成内容没有语义真值判定，不读取真实 TUI 或保留集产物。
# 模块用途: 验证镜头来源、改编新增和未知的声明能被原包脚本核对，结构通过不冒充内容保真。

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "examples/capability-packages/drama-text-a"


# LLM: 使用公开 fixture 的独立副本并明确声明新版字段；不迁移或改写磁盘上的旧用户产物。
# 函数用途: 为来源关系反例准备可变的三场合成资料。
def _delivery() -> dict:
    delivery = json.loads((PACKAGE / "resources/example-delivery.json").read_text(encoding="utf-8"))
    delivery["schema"] = "drama_text_delivery.v3"
    scenes = {row["id"]: row for row in delivery["scenes"]}
    for shot in delivery["shots"]:
        shot["source_ids"] = copy.deepcopy(scenes[shot["scene_id"]]["source_ids"])
        shot["adaptations"] = []
        shot["unresolved"] = []
    return delivery


# LLM: 只为公开合成资料绑定真实输入字节摘要；旧用户产物和真实验收资料不参与。
# 函数用途: 把可变交付编码成 CLI 输入，复用同一个隔离调用入口。
def _check(tmp_path: Path, delivery: dict) -> dict:
    source = (PACKAGE / "resources/example-source.json").read_bytes()
    delivery["source_sha256"] = hashlib.sha256(source).hexdigest()
    inputs = {"source.json": source,
              "delivery.json": (json.dumps(delivery, ensure_ascii=False) + "\n").encode()}
    return _invoke(tmp_path, inputs)


# LLM: 只在 pytest 临时目录写明确输入，隔离执行包内同一脚本；核对字节不变和真实退出码，不代表 TUI 已执行。
# 函数用途: 保留坏 JSON 的原字节，经原 CLI 检查成功及失败回执的相同版本合同。
def _invoke(tmp_path: Path, inputs: dict[str, bytes]) -> dict:
    for name, data in inputs.items():
        (tmp_path / name).write_bytes(data)
    environment = {"PATH": os.defpath}
    if "SYSTEMROOT" in os.environ:
        environment["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    process = subprocess.run(
        [sys.executable, "-I", "-B", "-X", "utf8", str(PACKAGE / "scripts/check_delivery.py"),
         "--source", "source.json", "--delivery", "delivery.json"], cwd=tmp_path, env=environment,
        capture_output=True, text=True, encoding="utf-8", timeout=15,
    )
    assert not process.stderr, process.stderr
    report = json.loads(process.stdout)
    assert process.returncode == (0 if report["structure_valid"] else 1)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == inputs
    return report


def test_v3_accepts_declared_shot_sources_with_explicit_empty_notes(tmp_path):
    report = _check(tmp_path, _delivery())
    assert report["schema"] == "drama_text_check.v3"
    assert report["structure_valid"], report["errors"]
    assert "creative_quality_and_media_not_checked" in {row["code"] for row in report["warnings"]}


@pytest.mark.parametrize("field", ["source_ids", "adaptations", "unresolved"])
def test_v3_does_not_silently_fill_missing_basis_fields(tmp_path, field):
    delivery = _delivery()
    del delivery["shots"][0][field]
    report = _check(tmp_path, delivery)
    assert not report["structure_valid"]
    assert any(row["path"] == f"SH01.{field}" for row in report["errors"])


@pytest.mark.parametrize("source_ids", [["P02"], ["missing"], [False], [["P01"]], "P01"])
def test_v3_rejects_cross_scene_unknown_or_malformed_shot_sources(tmp_path, source_ids):
    delivery = _delivery()
    delivery["shots"][0]["source_ids"] = source_ids
    report = _check(tmp_path, delivery)
    assert not report["structure_valid"]
    assert any(row["path"].startswith("SH01") for row in report["errors"])


@pytest.mark.parametrize("field", ["adaptations", "unresolved"])
@pytest.mark.parametrize("value", [None, "没有", [""], ["  "], [False], [{}]])
def test_v3_notes_require_explicit_nonempty_text_entries(tmp_path, field, value):
    delivery = _delivery()
    delivery["shots"][0][field] = value
    report = _check(tmp_path, delivery)
    assert not report["structure_valid"]
    assert any(row["path"].startswith(f"SH01.{field}") for row in report["errors"])


@pytest.mark.parametrize("field,warning", [("adaptations", "shot_adaptations_need_review"),
                                          ("unresolved", "shot_basis_unresolved")])
def test_v3_keeps_explicit_additions_and_unknowns_visible(tmp_path, field, warning):
    delivery = _delivery()
    delivery["shots"][0]["source_ids"] = []
    delivery["shots"][0][field] = ["新增停顿用于转场。" if field == "adaptations" else "原文未说明门是否打开。"]
    report = _check(tmp_path, delivery)
    assert report["structure_valid"], report["errors"]
    assert any(row["code"] == warning and row["path"] == f"SH01.{field}" for row in report["warnings"])


def test_v3_allows_source_additions_and_unknowns_in_the_same_shot(tmp_path):
    delivery = _delivery()
    delivery["shots"][0]["adaptations"] = ["补写拾书动作以衔接后续装袋。"]
    delivery["shots"][0]["unresolved"] = ["原文未说明店门开合状态。"]
    report = _check(tmp_path, delivery)
    assert report["structure_valid"], report["errors"]
    assert report["metrics"]["covered_passages"] == 3
    assert {row["code"] for row in report["warnings"]} >= {
        "shot_adaptations_need_review", "shot_basis_unresolved",
    }


def test_v3_rejects_a_shot_without_any_declared_basis(tmp_path):
    delivery = _delivery()
    delivery["shots"][0]["source_ids"] = []
    report = _check(tmp_path, delivery)
    assert not report["structure_valid"]
    assert {"code": "shot_basis_required", "path": "SH01"} in report["errors"]


def test_v3_only_checks_declared_links_and_does_not_claim_semantic_truth(tmp_path):
    delivery = _delivery()
    delivery["shots"][0]["action"] = "不存在于原文的事情，但声明的编号依然合法。"
    report = _check(tmp_path, delivery)
    assert report["structure_valid"]
    assert {"code": "creative_quality_and_media_not_checked"} in report["warnings"]


@pytest.mark.parametrize("old_version", ["v1", "v2"])
def test_v3_requires_explicit_schema_instead_of_quietly_upgrading_old_versions(tmp_path, old_version):
    delivery = _delivery()
    delivery["schema"] = f"drama_text_delivery.{old_version}"
    report = _check(tmp_path, delivery)
    assert not report["structure_valid"]
    assert {"code": "unsupported_schema", "path": "schema"} in report["errors"]


@pytest.mark.parametrize("name,raw,code", [
    ("source.json", b"[]", "object_required"),
    ("delivery.json", b"[]", "object_required"),
    ("delivery.json", b"{", "invalid_input"),
    ("delivery.json", b'{"schema": "a", "schema": "b"}', "invalid_input"),
])
def test_v3_failure_reports_keep_the_same_explicit_schema(tmp_path, name, raw, code):
    source = (PACKAGE / "resources/example-source.json").read_bytes()
    delivery = _delivery()
    delivery["source_sha256"] = hashlib.sha256(source).hexdigest()
    inputs = {"source.json": source, "delivery.json": json.dumps(delivery).encode()}
    inputs[name] = raw
    report = _invoke(tmp_path, inputs)
    assert report["schema"] == "drama_text_check.v3"
    assert not report["structure_valid"]
    assert code in {row["code"] for row in report["errors"]}
