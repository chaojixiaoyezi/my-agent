# LLM: 只用公开合成项目验证 B 包的基线对比（0.2.0 起）、交接覆盖/真实性和检查器身份；不读保留集或真实任务产物。
#   0.3.0 起台词、节拍编号、对应关系、schema、时长的未列出改动是 error，另见 test_capability_package_drama_workflow_v03.py。
# 模块用途: 证明“改了原设定却没人发现”这类改动会被逐 ID 报出，提醒不改变结构结论，交接里的虚假新增/省略报错。

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys

import pytest

from agent_py_agent.tests.test_capability_package_drama_workflow_handoff import PACKAGE, SCRIPT


# LLM: 公开示例项目的独立副本，测试只改结构化字段。
# 函数用途: 读一份可变的合成项目。
def _project() -> dict:
    return json.loads((PACKAGE / "resources/example-project.json").read_text(encoding="utf-8"))


# LLM: 只在 pytest 临时目录写明确输入，经原 CLI 运行；断言退出码与 structure_valid 一致。
# 函数用途: 写出若干 JSON 文件并带给定参数运行检查器，返回报告。
def _run(tmp_path, files: dict, *args) -> dict:
    for name, data in files.items():
        (tmp_path / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    process = subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), *args], cwd=tmp_path, text=True,
                             capture_output=True, timeout=15, check=False)
    assert not process.stderr, process.stderr
    report = json.loads(process.stdout)
    assert process.returncode == (0 if report["structure_valid"] else 1)
    return report


# LLM: 只取指定 scope 的 warning（码, 路径）对。
# 函数用途: 便于断言基线或交接范围内的提醒。
def _warnings(report: dict, scope: str) -> list[tuple[str, str]]:
    return [(item["code"], item.get("path")) for item in report["warnings"] if item["scope"] == scope]


# LLM: 合成示例的第一句台词所在节拍；换说话人时取同场另一位角色。
# 函数用途: 返回 (场次, 节拍, 另一位角色 ID)。
def _first_dialogue(project: dict) -> tuple[dict, dict, str]:
    scene = project["scenes"][0]
    beat = next(row for row in scene["beats"] if row["kind"] == "dialogue")
    other = next(value for value in scene["character_ids"] if value != beat["character_id"])
    return scene, beat, other


def test_identical_baseline_passes_with_no_baseline_warnings(tmp_path):
    report = _run(tmp_path, {"p.json": _project(), "b.json": _project()},
                  "--project", "p.json", "--baseline-project", "b.json")
    assert report["structure_valid"] and report["checks"]["baseline"] == "passed"
    assert _warnings(report, "baseline") == []
    assert {kind: report["baseline_metrics"][kind]["count"] for kind in ("added", "removed", "modified")} == \
        {"added": 0, "removed": 0, "modified": 0}


@pytest.mark.parametrize("edit_text", [False, True])
def test_speaker_swap_on_same_beat_is_flagged(tmp_path, edit_text):
    project = _project()
    scene, beat, other = _first_dialogue(project)
    beat["character_id"] = other
    if edit_text:
        beat["text"] += "（改写）"
    report = _run(tmp_path, {"p.json": project, "b.json": _project()},
                  "--project", "p.json", "--baseline-project", "b.json")
    # 只换说话人仍只提醒；连台词一起改、交接没列出时，0.3.0 起是 baseline_beat_changed 错误。
    assert report["structure_valid"] is not edit_text
    assert ({item["code"] for item in report["errors"]} == {"baseline_beat_changed"}) is edit_text
    assert ("dialogue_speaker_changed", f"{scene['id']}.{beat['id']}") in _warnings(report, "baseline")
    assert report["baseline_metrics"]["modified"]["items"] == [f"beats:{scene['id']}.{beat['id']}"]


def test_renumbered_beat_with_same_text_and_new_speaker_is_flagged(tmp_path):
    project = _project()
    scene, beat, other = _first_dialogue(project)
    old_id = beat["id"]
    beat["id"], beat["character_id"] = old_id + "-R", other
    for shot in project["shots"]:
        shot["beat_ids"] = [beat["id"] if value == old_id else value for value in shot["beat_ids"]]
    report = _run(tmp_path, {"p.json": project, "b.json": _project()},
                  "--project", "p.json", "--baseline-project", "b.json")
    warnings = _warnings(report, "baseline")
    assert ("dialogue_speaker_changed", f"{scene['id']}.{old_id}-R") in warnings
    assert ("baseline_object_removed", f"beats:{scene['id']}.{old_id}") in warnings


def test_beat_kind_change_and_removed_object_are_flagged(tmp_path):
    project = _project()
    scene, beat, _ = _first_dialogue(project)
    beat["kind"] = "action"
    beat.pop("character_id")
    removed = project["references"].pop()
    for shot in project["shots"]:
        shot["reference_ids"] = [value for value in shot["reference_ids"] if value != removed["id"]]
    report = _run(tmp_path, {"p.json": project, "b.json": _project()},
                  "--project", "p.json", "--baseline-project", "b.json")
    warnings = _warnings(report, "baseline")
    assert ("beat_kind_changed", f"{scene['id']}.{beat['id']}") in warnings
    assert ("baseline_object_removed", f"references:{removed['id']}") in warnings


@pytest.mark.parametrize("baseline,code", [
    ({"schema": "drama_workflow_project.v2"}, "unsupported_schema"),
    ([1, 2], "unsupported_schema"),
])
def test_baseline_must_be_a_project_v1(tmp_path, baseline, code):
    report = _run(tmp_path, {"p.json": _project(), "b.json": baseline},
                  "--project", "p.json", "--baseline-project", "b.json")
    assert not report["structure_valid"] and report["checks"]["baseline"] == "failed"
    assert {(item["code"], item["scope"]) for item in report["errors"]} == {(code, "baseline")}


def test_unreadable_baseline_fails_only_the_baseline_scope(tmp_path):
    report = _run(tmp_path, {"p.json": _project()}, "--project", "p.json", "--baseline-project", "missing.json")
    assert report["checks"] == {"project": "passed", "handoff": "not_requested", "baseline": "failed"}
    assert report["errors"][0]["cause"] == "input_not_found"


# LLM: 交接覆盖用例：输入/输出都是项目 v1，同一阶段；地址用 JSON Pointer。
# 函数用途: 构造一份摘要正确、映射可定制的最小交接。
def _handoff(tmp_path, before: dict, after: dict, **rows) -> tuple[dict, dict]:
    files = {"before.json": before, "after.json": after}
    for name, data in files.items():
        (tmp_path / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    handoff = {"schema": "drama_workflow_handoff.v2",
               "files": [{"id": fid, "path": name, "sha256": hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()}
                         for fid, name in (("F1", "before.json"), ("F2", "after.json"))],
               "stages": [{"id": "ST1", "scope": "合成修改", "input_file_ids": ["F1"], "output_file_ids": ["F2"],
                           "review_notes": "测试"}],
               "object_mappings": [], "omissions": [], "additions": [], "unresolved_differences": []}
    handoff.update(rows)
    return handoff, {}


# LLM: 带交接与两个文件绑定运行原 CLI。
# 函数用途: 返回交接模式下的报告。
def _run_handoff(tmp_path, handoff: dict) -> dict:
    return _run(tmp_path, {"handoff.json": handoff}, "--project", "after.json", "--handoff", "handoff.json",
                "--input-file", "F1=before.json", "--input-file", "F2=after.json")


def test_real_change_without_any_handoff_address_is_reported(tmp_path):
    after = _project()
    scene, beat, other = _first_dialogue(after)
    beat["character_id"] = other
    handoff, _ = _handoff(tmp_path, _project(), after)
    report = _run_handoff(tmp_path, handoff)
    assert report["structure_valid"] and report["checks"]["handoff"] == "passed"
    item = next(row for row in report["warnings"] if row["code"] == "change_not_declared_in_handoff")
    assert (item["file_id"], item["change"], item["object"]) == ("F2", "modified", f"beats:{scene['id']}.{beat['id']}")


@pytest.mark.parametrize("pointer,covered", [("/scenes/0/beats/1", True), ("/scenes/0", True), ("", False)])
def test_handoff_address_covers_the_change_only_when_it_points_at_it(tmp_path, pointer, covered):
    after = _project()
    scene, beat, other = _first_dialogue(after)
    position = scene["beats"].index(beat)
    beat["character_id"] = other
    pointer = pointer.replace("/beats/1", f"/beats/{position}")
    mapping = {"stage_id": "ST1", "source": {"file_id": "F1", "pointer": pointer},
               "target": {"file_id": "F2", "pointer": pointer}, "reason": "用户要求换说话人"}
    handoff, _ = _handoff(tmp_path, _project(), after, object_mappings=[mapping])
    report = _run_handoff(tmp_path, handoff)
    assert report["structure_valid"]
    assert ("change_not_declared_in_handoff" in [row["code"] for row in report["warnings"]]) is not covered


def test_claimed_addition_that_already_existed_is_an_error(tmp_path):
    before, after = _project(), _project()
    target = {"file_id": "F2", "pointer": "/props/0", "object_id": after["props"][0]["id"]}
    handoff, _ = _handoff(tmp_path, before, after,
                          additions=[{"stage_id": "ST1", "target": target, "reason": "声称新增"}])
    report = _run_handoff(tmp_path, handoff)
    assert not report["structure_valid"]
    assert {(row["code"], row["scope"]) for row in report["errors"]} == {("declared_addition_already_present", "handoff")}


def test_claimed_omission_that_is_still_present_is_an_error(tmp_path):
    before, after = _project(), _project()
    source = {"file_id": "F1", "pointer": "/props/0", "object_id": before["props"][0]["id"]}
    handoff, _ = _handoff(tmp_path, before, after,
                          omissions=[{"stage_id": "ST1", "source": source, "reason": "声称省略"}])
    report = _run_handoff(tmp_path, handoff)
    assert {row["code"] for row in report["errors"]} == {"declared_omission_still_present"}


def test_real_addition_declared_in_handoff_is_accepted(tmp_path):
    before, after = _project(), copy.deepcopy(_project())
    new = copy.deepcopy(after["props"][0])
    new["id"] = new["id"] + "-NEW"
    after["props"].append(new)
    target = {"file_id": "F2", "pointer": f"/props/{len(after['props']) - 1}", "object_id": new["id"]}
    handoff, _ = _handoff(tmp_path, before, after,
                          additions=[{"stage_id": "ST1", "target": target, "reason": "真实新增"}])
    report = _run_handoff(tmp_path, handoff)
    assert report["structure_valid"]
    assert "change_not_declared_in_handoff" not in [row["code"] for row in report["warnings"]]


def test_report_carries_checker_identity_and_project_digest(tmp_path):
    report = _run(tmp_path, {"p.json": _project()}, "--project", "p.json")
    declaration = json.loads((PACKAGE / "declaration.json").read_text(encoding="utf-8"))
    assert report["checker"] == {"package_id": declaration["plugin_id"], "package_version": declaration["version"],
                                 "script_sha256": hashlib.sha256(SCRIPT.read_bytes()).hexdigest()}
    assert report["metrics"]["project_sha256"] == hashlib.sha256((tmp_path / "p.json").read_bytes()).hexdigest()
    assert report["checks"]["baseline"] == "not_requested"
