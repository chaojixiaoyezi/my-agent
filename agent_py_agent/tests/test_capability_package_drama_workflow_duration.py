# LLM: 只用公开合成资料验证 B 包的时长对账；不消费真实任务产物，不改变宿主执行或完成合同。
# 模块用途: 覆盖跨场次分集计量、目标缺失与非法值、数值误差和既有引用校验的组合边界。

from __future__ import annotations

import copy
import json
import math
import runpy

import pytest

from agent_py_agent.tests.test_capability_package_examples import (
    EXAMPLES,
    _fixture,
    _run,
    _write_json,
)


# LLM: 只加载仓库中的纯检查函数，run_name 不取 __main__，不触发脚本 CLI 或私有安装读取。
# 函数用途: 让数值边界直接使用正式检查器，含严格 JSON 入口会更早拒绝的非有限值。
@pytest.fixture(scope="module")
def check_project():
    namespace = runpy.run_path(str(EXAMPLES / "drama-workflow-b" / "scripts" / "check_continuity.py"))
    return namespace["check_project"]


# LLM: 复制公开夜车例子生成多个场次和剧集；分组只能来自 scene.episode_id，不能读取叙事文字。
# 函数用途: 构造总秒数不变但分集可能错配的合成材料。
def _two_episodes() -> dict:
    project = _fixture("drama-workflow-b", "example-project.json")
    scene = project["scenes"][0]
    second_scene = copy.deepcopy(scene)
    second_scene["id"] = "SC02"
    second_scene["beats"] = [scene["beats"][2]]
    scene["beats"] = scene["beats"][:2]
    project["scenes"].append(second_scene)
    project["shots"][2]["scene_id"] = "SC02"
    second_episode = copy.deepcopy(project["episodes"][0])
    second_episode["id"] = "EP02"
    second_episode["target_seconds"] = 17.5
    project["episodes"].append(second_episode)
    last_scene = copy.deepcopy(second_scene)
    last_scene.update(id="SC03", episode_id="EP02")
    project["scenes"].append(last_scene)
    last_shot = copy.deepcopy(project["shots"][2])
    last_shot.update(id="SH04", scene_id="SC03", seconds=17.5)
    project["shots"].append(last_shot)
    return project


def test_duration_aggregates_all_scenes_by_explicit_episode(check_project):
    project = _two_episodes()
    project["episodes"][0]["scene_ids"] = ["SC03"]
    project["scenes"][0]["seconds"] = 999
    report = check_project(project)
    assert report["structure_valid"]
    assert report["metrics"]["episode_seconds"] == {"EP01": 60.0, "EP02": 17.5}
    assert report["metrics"]["shot_seconds"] == 77.5


def test_equal_total_cannot_hide_wrong_episode_allocation(check_project):
    project = _two_episodes()
    project["shots"][0]["seconds"] = 25
    project["shots"][-1]["seconds"] = 12.5
    report = check_project(project)
    assert not report["structure_valid"]
    assert report["metrics"]["shot_seconds"] == 77.5
    assert report["metrics"]["episode_seconds"] == {"EP01": 65.0, "EP02": 12.5}
    assert [error for error in report["errors"] if error["code"] == "episode_duration_mismatch"] == [
        {"code": "episode_duration_mismatch", "path": "EP01", "target_seconds": 60, "shot_seconds": 65.0},
        {"code": "episode_duration_mismatch", "path": "EP02", "target_seconds": 17.5, "shot_seconds": 12.5},
    ]


def test_episode_without_shots_does_not_borrow_another_episode_total(check_project):
    project = _two_episodes()
    project["scenes"][-1]["episode_id"] = "EP01"
    report = check_project(project)
    assert report["metrics"]["episode_seconds"]["EP02"] == 0.0
    assert {item["path"] for item in report["errors"] if item["code"] == "episode_duration_mismatch"} == {
        "EP01", "EP02",
    }


@pytest.mark.parametrize("target", [1e-10, 1e-20])
def test_positive_tiny_duration_does_not_get_an_absolute_error_allowance(check_project, target):
    project = _two_episodes()
    project["episodes"][1]["target_seconds"] = target
    project["shots"][-1]["seconds"] = target * 2
    report = check_project(project)
    assert not report["structure_valid"]
    assert any(error["code"] == "episode_duration_mismatch" and error["path"] == "EP02"
               for error in report["errors"])


def test_many_small_shots_keep_their_contribution_to_duration(check_project):
    project = _fixture("drama-workflow-b", "example-project.json")
    first = project["shots"][0]
    first.update(seconds=1.0, beat_ids=[beat["id"] for beat in project["scenes"][0]["beats"]])
    project["shots"] = [first, *[
        {**first, "id": f"small-{index}", "seconds": 1e-16} for index in range(12000)
    ]]
    expected = math.fsum(shot["seconds"] for shot in project["shots"])
    project["episodes"][0]["target_seconds"] = expected
    assert len(json.dumps(project).encode()) < 4 * 1024 * 1024
    report = check_project(project)
    assert report["structure_valid"]
    assert report["metrics"]["episode_seconds"] == {"EP01": expected}
    assert report["metrics"]["shot_seconds"] == expected


def test_missing_target_warns_without_inventing_one(check_project):
    project = _fixture("drama-workflow-b", "example-project.json")
    del project["episodes"][0]["target_seconds"]
    report = check_project(project)
    assert report["structure_valid"]
    assert report["metrics"]["episode_seconds"] == {"EP01": 60.0}
    assert {"code": "episode_target_missing", "path": "EP01.target_seconds"} in report["warnings"]
    assert "target_seconds" not in project["episodes"][0]


@pytest.mark.parametrize("value", [None, True, False, 0, -2, "60", [], float("nan"), float("inf"), 10**400])
def test_explicit_invalid_target_is_an_error_not_missing(check_project, value):
    project = _fixture("drama-workflow-b", "example-project.json")
    project["episodes"][0]["target_seconds"] = value
    report = check_project(project)
    assert not report["structure_valid"]
    assert {"code": "positive_target_seconds_required", "path": "EP01.target_seconds"} in report["errors"]
    assert "episode_target_missing" not in {item["code"] for item in report["warnings"]}
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize("last_seconds,valid", [(0.3, True), (0.300001, False)])
def test_float_roundoff_is_tolerated_but_real_difference_is_not(check_project, last_seconds, valid):
    project = _fixture("drama-workflow-b", "example-project.json")
    project["episodes"][0]["target_seconds"] = 0.6
    for shot, seconds in zip(project["shots"], [0.1, 0.2, last_seconds]):
        shot["seconds"] = seconds
    report = check_project(project)
    assert report["structure_valid"] is valid
    assert ("episode_duration_mismatch" in {item["code"] for item in report["errors"]}) is not valid


@pytest.mark.parametrize("value", [None, True, 0, -1, "20", float("nan"), float("inf"), 10**400])
def test_invalid_shot_keeps_episode_duration_unknown(check_project, value):
    project = _two_episodes()
    project["shots"][0]["seconds"] = value
    report = check_project(project)
    assert not report["structure_valid"]
    assert {"code": "positive_seconds_required", "path": "SH01"} in report["errors"]
    assert report["metrics"]["episode_seconds"] == {"EP01": None, "EP02": 17.5}
    assert "episode_duration_mismatch" not in {item["code"] for item in report["errors"]}
    json.dumps(report, allow_nan=False)


def test_overflow_cannot_become_a_matching_or_json_nonfinite_duration(check_project):
    project = _fixture("drama-workflow-b", "example-project.json")
    project["episodes"][0]["target_seconds"] = 1e308
    for shot in project["shots"]:
        shot["seconds"] = 1e308
    report = check_project(project)
    assert not report["structure_valid"]
    assert report["metrics"]["episode_seconds"] == {"EP01": None}
    assert report["metrics"]["shot_seconds"] is None
    assert {"code": "duration_sum_overflow", "path": "EP01.shots"} in report["errors"]
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize("reference", ["scene", "episode"])
def test_unknown_link_is_not_guessed_from_other_fields(check_project, reference):
    project = _fixture("drama-workflow-b", "example-project.json")
    if reference == "scene":
        project["shots"][0].update(scene_id="missing", episode_id="EP01")
    else:
        project["scenes"][0]["episode_id"] = "missing"
    report = check_project(project)
    assert not report["structure_valid"]
    assert report["metrics"]["episode_seconds"] == {"EP01": 40.0 if reference == "scene" else 0.0}
    assert ("unknown_scene" if reference == "scene" else "unknown_reference") in {
        item["code"] for item in report["errors"]
    }


def test_duration_match_does_not_weaken_existing_reference_checks(check_project):
    project = _fixture("drama-workflow-b", "example-project.json")
    project["scenes"][0]["prop_ids"] = ["missing"]
    project["scenes"][0]["beats"][1]["character_id"] = "missing"
    project["shots"][0]["beat_ids"] = ["missing"]
    project["shots"][1]["reference_ids"] = ["P01"]
    report = check_project(project)
    assert not report["structure_valid"]
    assert report["metrics"]["episode_seconds"] == {"EP01": 60.0}
    assert {"unknown_reference", "speaker_outside_scene", "beat_reference_required", "uncovered_beat"} <= {
        item["code"] for item in report["errors"]
    }


@pytest.mark.parametrize("target,exit_code", [(60, 0), (55, 1)])
def test_cli_reports_actual_result_without_changing_input(tmp_path, target, exit_code):
    project = _fixture("drama-workflow-b", "example-project.json")
    project["episodes"][0]["target_seconds"] = target
    path = _write_json(tmp_path, "project.json", project)
    original = path.read_bytes()
    result = _run("drama-workflow-b", "check_continuity.py", tmp_path, ["--project", str(path)])
    assert result.returncode == exit_code
    assert path.read_bytes() == original
    report = json.loads(result.stdout)
    assert report["metrics"]["episode_seconds"] == {"EP01": 60.0}
    assert report["structure_valid"] is (exit_code == 0)


def test_package_declares_new_version_without_adding_an_executor():
    package = EXAMPLES / "drama-workflow-b"
    declaration = json.loads((package / "declaration.json").read_text(encoding="utf-8"))
    assert declaration["version"] == "0.1.3"
    assert not {"entry", "tools", "actions", "skills", "wheels"} & declaration.keys()
