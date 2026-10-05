# LLM: 仅以公开合成材料验证 A 包的计量合同；不得读取真实任务产物或把组件结果计作 TUI 验收。
# 模块用途: 检查逐场镜头求和、显式目标、数值边界及原有引用拒绝，保证脚本只读输入并输出 JSON。

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


# LLM: 测试仅消费包内公开合成例子，每次反序列化均得到独立对象，不改原文件。
# 函数用途: 取得一组有效原文与交付，供单点改变计量事实。
def _documents() -> tuple[dict, dict]:
    return tuple(json.loads((PACKAGE / "resources" / name).read_text(encoding="utf-8"))
                 for name in ("example-source.json", "example-delivery.json"))


# LLM: 组件进程只读 pytest 临时输入，使用真实原文字节摘要；私有环境和真实业务文件不参与。
# 函数用途: 调用标准库脚本并核对退出码、合法 JSON、输入字节与文件清单保持不变。
def _check(tmp_path: Path, source: dict, delivery: dict) -> dict:
    delivery = copy.deepcopy(delivery)
    source_bytes = (json.dumps(source, ensure_ascii=False) + "\n").encode()
    delivery["source_sha256"] = hashlib.sha256(source_bytes).hexdigest()
    inputs = {"source.json": source_bytes,
              "delivery.json": (json.dumps(delivery, ensure_ascii=False) + "\n").encode()}
    for name, data in inputs.items():
        (tmp_path / name).write_bytes(data)
    environment = {"PATH": os.defpath}
    if "SYSTEMROOT" in os.environ:
        environment["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-X", "utf8", str(PACKAGE / "scripts/check_delivery.py"),
         "--source", "source.json", "--delivery", "delivery.json"],
        cwd=tmp_path, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=15,
    )
    assert not result.stderr, result.stderr
    report = json.loads(result.stdout)
    assert result.returncode == (0 if report["structure_valid"] else 1)
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == inputs
    return report


def test_duration_metrics_use_actual_shots_and_explicit_source_target(tmp_path):
    source, delivery = _documents()
    report = _check(tmp_path, source, delivery)

    assert report["structure_valid"], report["errors"]
    assert report["metrics"]["shot_seconds"] == 60.0
    assert report["metrics"]["scene_shot_seconds"] == {"S01": 20.0, "S02": 20.0, "S03": 20.0}
    assert report["metrics"]["target_declared"] is True
    assert report["metrics"]["target_seconds"] == 60.0
    assert report["metrics"]["target_delta_seconds"] == 0.0


def test_scene_duration_mismatch_is_rejected_even_when_total_matches(tmp_path):
    source, delivery = _documents()
    delivery["shots"][0]["seconds"] = 19
    delivery["shots"][1]["seconds"] = 21
    report = _check(tmp_path, source, delivery)

    assert not report["structure_valid"]
    mismatches = [row for row in report["errors"] if row["code"] == "scene_duration_mismatch"]
    assert {row["path"] for row in mismatches} == {"S01.seconds", "S02.seconds"}
    assert report["metrics"]["shot_seconds"] == report["metrics"]["target_seconds"] == 60.0


def test_scene_declaration_cannot_disguise_missing_shot_time(tmp_path):
    source, delivery = _documents()
    delivery["shots"][0]["seconds"] = 10
    report = _check(tmp_path, source, delivery)

    assert not report["structure_valid"]
    assert {row["code"] for row in report["errors"]} >= {
        "scene_duration_mismatch", "target_duration_mismatch",
    }
    assert report["metrics"]["scene_seconds"] == 60.0
    assert report["metrics"]["shot_seconds"] == 50.0
    assert report["metrics"]["target_delta_seconds"] == -10.0


def test_multiple_shots_per_scene_are_summed_without_count_assumptions(tmp_path):
    source, delivery = _documents()
    first = delivery["shots"][0]
    first["seconds"] = 7.5
    second = copy.deepcopy(first)
    second.update(id="SH01-detail", seconds=12.5, continuity_break="测试中的独立细节镜头")
    delivery["shots"].insert(1, second)
    report = _check(tmp_path, source, delivery)

    assert report["structure_valid"], report["errors"]
    assert report["metrics"]["scene_shot_seconds"]["S01"] == 20.0
    assert report["metrics"]["shots"] == 4


def test_roundoff_is_allowed_without_story_specific_tolerance(tmp_path):
    source, delivery = _documents()
    source["target_seconds"] = 0.9
    shots = []
    for index, scene in enumerate(delivery["scenes"]):
        scene["seconds"] = 0.3
        original = delivery["shots"][index]
        for suffix, value in (("a", 0.1), ("b", 0.2)):
            shot = copy.deepcopy(original)
            shot.update(id=original["id"] + suffix, seconds=value)
            shot["continuity_break"] = "计量样例中的独立切分"
            shots.append(shot)
    delivery["shots"] = shots
    report = _check(tmp_path, source, delivery)

    assert report["structure_valid"], report["errors"]
    assert report["metrics"]["shot_seconds"] == pytest.approx(0.9)


def test_small_real_target_mismatch_is_not_treated_as_creative_tolerance(tmp_path):
    source, delivery = _documents()
    source["target_seconds"] = 60.000001
    report = _check(tmp_path, source, delivery)

    assert not report["structure_valid"]
    assert {row["code"] for row in report["errors"]} == {"target_duration_mismatch"}


def test_tiny_durations_do_not_hide_real_mismatch_under_absolute_tolerance(tmp_path):
    source, delivery = _documents()
    source["target_seconds"] = 3e-12
    for scene in delivery["scenes"]:
        scene["seconds"] = 2e-12
    for shot in delivery["shots"]:
        shot["seconds"] = 1e-12
    report = _check(tmp_path, source, delivery)

    assert not report["structure_valid"]
    assert len([row for row in report["errors"] if row["code"] == "scene_duration_mismatch"]) == 3


def test_absent_target_is_distinct_from_invalid_target(tmp_path):
    source, delivery = _documents()
    del source["target_seconds"]
    report = _check(tmp_path, source, delivery)

    assert report["structure_valid"], report["errors"]
    assert report["metrics"]["target_declared"] is False
    assert report["metrics"]["target_seconds"] is None
    assert report["metrics"]["target_delta_seconds"] is None
    assert report["metrics"]["shot_seconds"] == 60.0


@pytest.mark.parametrize("target", [None, True, False, 0, -1, "60", [], {}, 10 ** 400])
def test_invalid_declared_target_is_rejected(tmp_path, target):
    source, delivery = _documents()
    source["target_seconds"] = target
    report = _check(tmp_path, source, delivery)

    assert not report["structure_valid"]
    assert {"code": "positive_target_seconds_required", "path": "source.target_seconds"} in report["errors"]
    assert report["metrics"]["target_declared"] is True
    assert report["metrics"]["target_seconds"] is None


@pytest.mark.parametrize("kind", ["scenes", "shots"])
@pytest.mark.parametrize("value", [None, True, 0, -1, "20", 10 ** 400])
def test_invalid_member_durations_remain_rejected(tmp_path, kind, value):
    source, delivery = _documents()
    delivery[kind][0]["seconds"] = value
    report = _check(tmp_path, source, delivery)

    assert not report["structure_valid"]
    assert "positive_seconds_required" in {row["code"] for row in report["errors"]}


def test_sum_overflow_is_structured_error_not_json_infinity(tmp_path):
    source, delivery = _documents()
    del source["target_seconds"]
    for collection in (delivery["scenes"], delivery["shots"]):
        for row in collection:
            row["seconds"] = 1e308
    report = _check(tmp_path, source, delivery)

    assert not report["structure_valid"]
    assert {row["path"] for row in report["errors"] if row["code"] == "duration_sum_overflow"} >= {
        "scenes", "shots",
    }
    assert report["metrics"]["scene_seconds"] is None
    assert report["metrics"]["shot_seconds"] is None


@pytest.mark.parametrize("kind", ["scenes", "shots"])
def test_duplicate_ids_remain_rejected(tmp_path, kind):
    source, delivery = _documents()
    delivery[kind].append(copy.deepcopy(delivery[kind][0]))
    report = _check(tmp_path, source, delivery)

    assert not report["structure_valid"]
    assert "invalid_or_duplicate_id" in {row["code"] for row in report["errors"]}


def test_unknown_scene_id_remains_rejected(tmp_path):
    source, delivery = _documents()
    delivery["shots"][0]["scene_id"] = "outside-the-document"
    report = _check(tmp_path, source, delivery)

    assert not report["structure_valid"]
    assert "unknown_scene" in {row["code"] for row in report["errors"]}
