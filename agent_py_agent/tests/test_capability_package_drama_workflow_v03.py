# LLM: 只用公开合成项目验证 B 包 0.3.0 新增的确定性检查和 --host-json 宿主核验输出（能力包 v2 块 7）；每项正例、反例各一，
#   对应 capability-packs-v2-design/attribution.md 的 K/P 类失败。不读保留集或真实任务产物。
# 模块用途: 证明缺表/缺外键、编造参考 ID、节拍缺角色、出镜人物缺参考、未列出的基线改动和“声称改了其实没改”都按结构化字段判定；
#   宿主模式下交接文件只按摘要对应宿主给的文件，不需要 --input-file。

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_py_agent.tests.test_capability_package_drama_workflow_handoff import PACKAGE, SCRIPT


# 函数用途: 读一份可变的公开合成项目。
def _project() -> dict:
    return json.loads((PACKAGE / "resources/example-project.json").read_text(encoding="utf-8"))


# LLM: 只在 pytest 临时目录写明确输入，经原 CLI 运行；普通模式断言退出码与 structure_valid 一致，宿主模式断言写出即退 0。
# 函数用途: 写出若干 JSON（dict 或原始字节）并带参数运行检查器，返回报告。
def _run(tmp_path: Path, files: dict, *args: str) -> dict:
    for name, data in files.items():
        raw = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode("utf-8")
        (tmp_path / name).write_bytes(raw)
    process = subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), *args], cwd=tmp_path, text=True,
                             capture_output=True, timeout=15, check=False)
    assert not process.stderr, process.stderr
    report = json.loads(process.stdout)
    if "--host-json" in args:
        assert process.returncode == 0 and report["valid"] == (not report["errors"])
    else:
        assert process.returncode == (0 if report["structure_valid"] else 1)
    return report


# 函数用途: 取某一类（errors 或 warnings）里某个码的全部条目。
def _found(report: dict, kind: str, code: str) -> list[dict]:
    return [item for item in report[kind] if item["code"] == code]


# 函数用途: 按 ID 取可变的行。
def _row(project: dict, table: str, identifier: str) -> dict:
    return next(row for row in project[table] if row["id"] == identifier)


# 函数用途: 按 ID 取第一场的可变节拍。
def _beat(project: dict, identifier: str) -> dict:
    return next(row for row in project["scenes"][0]["beats"] if row["id"] == identifier)


# LLM: 交接里 F1 是基线、F2 是本次项目，摘要按实际写出的字节算；宿主模式靠摘要对应，普通模式另给 --input-file。
# 函数用途: 写出基线、项目和一份可定制的交接，返回交接文件名。
# LLM: 示例项目自带三条被镜头引用的 planned 参考（REF-C01/C02/L01）；0.3.3 起它们必须在交接里登记，
#   所以默认交接把它们逐条列进 additions（与 B05-t402 的正面样本一致），需要时调用方可以覆盖。
# 函数用途: 返回示例项目 planned 参考的默认 additions 登记。
def _planned_reference_additions(before: dict) -> list[dict]:
    referenced = {value for shot in before.get("shots", [])
                  for value in (shot.get("reference_ids") if isinstance(shot.get("reference_ids"), list) else [])}
    return [{"stage_id": "ST1", "target": {"file_id": "F2", "pointer": f"/references/{index}"},
             "reason": f"{row['id']} 尚未制作，登记为本阶段计划项"}
            for index, row in enumerate(before.get("references", []))
            if row.get("state") == "planned" and row.get("id") in referenced]


def _handoff(tmp_path: Path, before: dict, after: dict, **rows) -> dict:
    files = {"b.json": before, "p.json": after}
    for name, data in files.items():
        (tmp_path / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    handoff = {"schema": "drama_workflow_handoff.v2",
               "files": [{"id": fid, "path": name, "sha256": hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()}
                         for fid, name in (("F1", "b.json"), ("F2", "p.json"))],
               "stages": [{"id": "ST1", "scope": "按用户要求整理", "input_file_ids": ["F1"], "output_file_ids": ["F2"],
                           "review_notes": "测试"}],
               "object_mappings": [], "omissions": [], "additions": _planned_reference_additions(before),
               "unresolved_differences": []}
    handoff.update(rows)
    return handoff


BASELINE = ("--project", "p.json", "--baseline-project", "b.json")


def test_example_is_clean_under_all_new_checks(tmp_path):
    report = _run(tmp_path, {"p.json": _project(), "b.json": _project()}, *BASELINE)
    assert report["structure_valid"], report["errors"]
    assert report["checker"]["package_version"] == "0.3.3"
    new = {"missing_table", "missing_foreign_key", "unknown_reference_mention", "beat_character_missing",
           "shot_character_reference_missing", "baseline_relation_changed", "baseline_beat_changed",
           "baseline_schema_or_duration_changed", "handoff_claim_without_change"}
    assert not new & {item["code"] for item in report["errors"] + report["warnings"]}


# ---- missing_table / missing_foreign_key（B05-t4、B05-t6：缺 locations/episodes/references 表和外键）----

@pytest.mark.parametrize("table", ["episodes", "locations", "references"])
def test_absent_table_is_missing_table_but_wrong_type_keeps_list_required(tmp_path, table):
    project = _project()
    project.pop(table)
    report = _run(tmp_path, {"p.json": project}, "--project", "p.json")
    assert {"code": "missing_table", "path": table, "scope": "project"} in report["errors"]
    project = _project()
    project[table] = {}
    report = _run(tmp_path, {"p.json": project}, "--project", "p.json")
    assert not _found(report, "errors", "missing_table") and _found(report, "errors", "list_required")


@pytest.mark.parametrize("table,identifier,key", [("scenes", "SC01", "location_id"), ("scenes", "SC01", "episode_id"),
                                                  ("shots", "SH01", "scene_id"), ("references", "REF-C01", "subject_id")])
def test_absent_foreign_key_is_missing_foreign_key(tmp_path, table, identifier, key):
    project = _project()
    _row(project, table, identifier).pop(key)
    report = _run(tmp_path, {"p.json": project}, "--project", "p.json")
    assert [item["path"] for item in _found(report, "errors", "missing_foreign_key")] == [f"{identifier}.{key}"]


def test_present_but_unknown_foreign_key_keeps_its_original_code(tmp_path):
    project = _project()
    _row(project, "scenes", "SC01")["location_id"] = "L99"
    report = _run(tmp_path, {"p.json": project}, "--project", "p.json")
    assert not _found(report, "errors", "missing_foreign_key")
    assert {"code": "unknown_reference", "path": "SC01.location_id", "scope": "project"} in report["errors"]


# ---- unknown_reference_mention（B05-t4、B10-t5：编造不存在的 REF-C02）----

def test_reference_id_mentioned_in_free_text_must_exist(tmp_path):
    project = _project()
    project["missing_items"] = ["REF-C09 的人物图还没有", "REF-C01 已有计划"]
    report = _run(tmp_path, {"p.json": project}, "--project", "p.json")
    assert [(item["path"], item["reference_id"]) for item in _found(report, "errors", "unknown_reference_mention")] == [
        ("/missing_items/0", "REF-C09")]


def test_no_reference_table_means_no_prefix_and_no_mention_scan(tmp_path):
    project = _project()
    project["references"] = []
    for shot in project["shots"]:
        shot["reference_ids"] = []
    project["missing_items"] = ["REF-C09 的人物图还没有"]
    report = _run(tmp_path, {"p.json": project}, "--project", "p.json")
    assert report["structure_valid"] and not _found(report, "errors", "unknown_reference_mention")


def test_reference_id_invented_in_handoff_text_is_an_error(tmp_path):
    handoff = _handoff(tmp_path, _project(), _project(), unresolved_differences=[
        {"stage_id": "ST1", "refs": [{"file_id": "F2", "pointer": "/shots/0"}], "difference": "REF-C07 缺图",
         "next_step": "补 REF-C01 的图"}])
    report = _run(tmp_path, {"h.json": handoff}, *BASELINE, "--handoff", "h.json", "--host-json")
    # 宿主条目从 0.3.2 起可带可选 hint，所以只断言 code 和 location 命中，不锁死条目形状。
    assert any(item.get("code") == "unknown_reference_mention"
               and item.get("location") == "/unresolved_differences/0/difference" for item in report["errors"])
    assert len(_found(report, "errors", "unknown_reference_mention")) == 1


# ---- beat_character_missing（B08-t4、B10-t5：有动作的节拍没有角色 ID）----

@pytest.mark.parametrize("value", [None, []])
def test_action_beat_without_characters_warns(tmp_path, value):
    project = _project()
    beat = _beat(project, "B01")
    if value is None:
        beat.pop("character_ids")
    else:
        beat["character_ids"] = value
    report = _run(tmp_path, {"p.json": project}, "--project", "p.json")
    assert report["structure_valid"]
    assert [item["path"] for item in _found(report, "warnings", "beat_character_missing")] == ["B01"]


def test_action_beat_character_outside_the_scene_is_an_error(tmp_path):
    project = _project()
    _beat(project, "B01")["character_ids"] = ["C99"]
    report = _run(tmp_path, {"p.json": project}, "--project", "p.json")
    assert {"code": "unknown_reference", "path": "B01.character_ids", "scope": "project"} in report["errors"]


# ---- shot_character_reference_missing（B08-t4：出镜人物没有人物参考）----

def test_on_screen_character_without_a_shot_reference_is_an_error(tmp_path):
    project = _project()
    _row(project, "shots", "SH03")["reference_ids"] = ["REF-L01"]
    report = _run(tmp_path, {"p.json": project}, "--project", "p.json")
    assert not report["structure_valid"]
    assert [(item["path"], item["character_id"]) for item in _found(report, "errors", "shot_character_reference_missing")] == [
        ("SH03", "C02")]
    assert not _found(report, "warnings", "shot_character_reference_missing")


def test_project_without_any_character_reference_plan_does_not_warn(tmp_path):
    project = _project()
    project["references"] = [row for row in project["references"] if row["kind"] != "character"]
    for shot in project["shots"]:
        shot["reference_ids"] = ["REF-L01"]
    report = _run(tmp_path, {"p.json": project}, "--project", "p.json")
    assert not _found(report, "warnings", "shot_character_reference_missing")


# ---- 基线改动没在交接里列出（B05-t4、B10-t6）----

def _seconds_changed() -> dict:
    project = _project()
    _row(project, "shots", "SH01")["seconds"] = 25
    _row(project, "shots", "SH02")["seconds"] = 15
    return project


def _relation_changed() -> dict:
    project = _project()
    _row(project, "shots", "SH02")["reference_ids"] = ["REF-L01", "REF-C01"]
    return project


def _beat_renumbered() -> dict:
    project = _project()
    _beat(project, "B03")["id"] = "B03a"
    _row(project, "shots", "SH03")["beat_ids"] = ["B03a"]
    return project


def _dialogue_rewritten() -> dict:
    project = _project()
    _beat(project, "B02")["text"] = "这件包裹不是这一班的，快拿走。"
    return project


@pytest.mark.parametrize("make,expected", [
    (_seconds_changed, {("baseline_schema_or_duration_changed", "shots:SH01"), ("baseline_schema_or_duration_changed", "shots:SH02")}),
    (_relation_changed, {("baseline_relation_changed", "shots:SH02")}),
    (_beat_renumbered, {("baseline_beat_changed", "beats:SC01.B03"), ("baseline_beat_changed", "beats:SC01.B03a"),
                        ("baseline_relation_changed", "shots:SH03")}),
    (_dialogue_rewritten, {("baseline_beat_changed", "beats:SC01.B02")}),
])
def test_unlisted_baseline_change_is_an_error(tmp_path, make, expected):
    report = _run(tmp_path, {"p.json": make(), "b.json": _project()}, *BASELINE)
    assert {(item["code"], item["path"]) for item in report["errors"]} == expected
    assert report["checks"]["baseline"] == "failed" and report["checks"]["project"] == "passed"


def test_reordering_a_shots_references_is_not_a_relation_change(tmp_path):
    project = _project()
    shot = _row(project, "shots", "SH02")
    shot["reference_ids"] = list(reversed(shot["reference_ids"]))
    report = _run(tmp_path, {"p.json": project, "b.json": _project()}, *BASELINE)
    assert report["structure_valid"], report["errors"]


def test_episode_target_change_is_a_duration_change(tmp_path):
    project = _project()
    _row(project, "episodes", "EP01")["target_seconds"] = 61
    report = _run(tmp_path, {"p.json": project, "b.json": _project()}, *BASELINE)
    assert {"code": "baseline_schema_or_duration_changed", "path": "episodes:EP01", "pointer": "/episodes/0/target_seconds",
            "scope": "baseline"} in report["errors"]


@pytest.mark.parametrize("mode", ["host", "bindings"])
def test_baseline_change_listed_in_the_handoff_is_accepted(tmp_path, mode):
    after = _seconds_changed()
    mappings = [{"stage_id": "ST1", "source": {"file_id": "F1", "pointer": f"/shots/{position}/seconds"},
                 "target": {"file_id": "F2", "pointer": f"/shots/{position}/seconds"}, "reason": "20 → 新秒数：用户要求调整节奏"}
                for position in (0, 1)]
    handoff = _handoff(tmp_path, _project(), after, object_mappings=mappings)
    if mode == "host":
        report = _run(tmp_path, {"h.json": handoff}, *BASELINE, "--handoff", "h.json", "--host-json")
        assert report["valid"], report["errors"]
    else:
        report = _run(tmp_path, {"h.json": handoff}, *BASELINE, "--handoff", "h.json",
                      "--input-file", "F1=b.json", "--input-file", "F2=p.json")
        assert report["structure_valid"], report["errors"]


def test_handoff_address_elsewhere_does_not_list_the_change(tmp_path):
    after = _seconds_changed()
    mapping = {"stage_id": "ST1", "source": {"file_id": "F1", "pointer": "/shots/0/seconds"},
               "target": {"file_id": "F2", "pointer": "/shots/0/seconds"}, "reason": "20 → 25：用户要求"}
    handoff = _handoff(tmp_path, _project(), after, object_mappings=[mapping])
    report = _run(tmp_path, {"h.json": handoff}, *BASELINE, "--handoff", "h.json", "--host-json")
    assert [item["location"] for item in report["errors"]] == ["shots:SH02"]


def test_stale_handoff_digest_gets_its_own_warning(tmp_path):
    after = _seconds_changed()
    mappings = [{"stage_id": "ST1", "source": {"file_id": "F1", "pointer": f"/shots/{position}/seconds"},
                 "target": {"file_id": "F2", "pointer": f"/shots/{position}/seconds"}, "reason": "20 → 新秒数：用户要求"}
                for position in (0, 1)]
    handoff = _handoff(tmp_path, _project(), after, object_mappings=mappings)
    fresh = _run(tmp_path, {"h.json": handoff}, *BASELINE, "--handoff", "h.json", "--host-json")
    assert fresh["valid"] and {"code": "handoff_target_not_matched", "location": "files"} not in fresh["warnings"]
    _row(after, "shots", "SH03")["reference_ids"].reverse()  # 交接写完后项目又改了一下，F2 摘要过期
    stale = _run(tmp_path, {"p.json": after, "h.json": handoff}, *BASELINE, "--handoff", "h.json", "--host-json")
    assert {"code": "handoff_target_not_matched", "location": "files"} in stale["warnings"]
    assert {item["code"] for item in stale["errors"]} == {"baseline_schema_or_duration_changed"}


# ---- handoff_claim_without_change（B08-t4：声称补填了其实原件本来就有）----

def test_mapping_between_identical_objects_is_a_false_change_claim(tmp_path):
    mapping = {"stage_id": "ST1", "source": {"file_id": "F1", "pointer": "/props/0", "object_id": "P01"},
               "target": {"file_id": "F2", "pointer": "/props/0", "object_id": "P01"}, "reason": "补填道具连续性说明"}
    handoff = _handoff(tmp_path, _project(), _project(), object_mappings=[mapping])
    report = _run(tmp_path, {"h.json": handoff}, *BASELINE, "--handoff", "h.json", "--host-json")
    assert any(item.get("code") == "handoff_claim_without_change"
               and item.get("location") == "object_mappings[0]" for item in report["errors"])


def test_mapping_with_a_real_change_is_not_a_false_claim(tmp_path):
    after = _project()
    _row(after, "props", "P01")["continuity_note"] = "红色绳结，右侧打结"
    mapping = {"stage_id": "ST1", "source": {"file_id": "F1", "pointer": "/props/0", "object_id": "P01"},
               "target": {"file_id": "F2", "pointer": "/props/0", "object_id": "P01"}, "reason": "补填打结位置"}
    handoff = _handoff(tmp_path, _project(), after, object_mappings=[mapping])
    report = _run(tmp_path, {"h.json": handoff}, *BASELINE, "--handoff", "h.json", "--host-json")
    assert report["valid"], report["errors"]


# ---- 交接模板的 <…> 提示照抄不算填写 ----

def test_copied_template_hint_in_handoff_text_is_a_placeholder(tmp_path):
    template = json.loads((PACKAGE / "templates/handoff.json").read_text(encoding="utf-8"))
    mapping = {"stage_id": "ST1", "source": {"file_id": "F1", "pointer": "/props/0"},
               "target": {"file_id": "F2", "pointer": "/props/0"}, "reason": template["object_mappings"][0]["reason"]}
    after = _project()
    _row(after, "props", "P01")["continuity_note"] = "红色绳结，右侧打结"
    handoff = _handoff(tmp_path, _project(), after, object_mappings=[mapping])
    report = _run(tmp_path, {"h.json": handoff}, *BASELINE, "--handoff", "h.json", "--host-json")
    assert any(item.get("code") == "placeholder_text"
               and item.get("location") == "object_mappings[0].reason" for item in report["errors"])


# ---- --host-json：pack_verifier_result.v1，交接按摘要对应 ----

def test_host_json_outputs_only_structured_v1_fields(tmp_path):
    report = _run(tmp_path, {"p.json": _project(), "b.json": _project()}, *BASELINE, "--host-json")
    assert set(report) == {"schema", "valid", "errors", "warnings", "metrics"}
    assert report["schema"] == "pack_verifier_result.v1" and report["valid"] is True
    assert all(set(item) == {"code", "location"} for item in report["warnings"])
    assert report["metrics"] == {"episodes": 1, "scenes": 1, "shots": 3, "shot_seconds": 60.0}


def test_host_mode_binds_handoff_files_by_digest_and_skips_unknown_ones(tmp_path):
    handoff = _handoff(tmp_path, _project(), _project())
    handoff["files"].append({"id": "F0", "path": "story.txt", "sha256": "0" * 64})
    handoff["stages"][0]["input_file_ids"].append("F0")
    host = _run(tmp_path, {"h.json": handoff}, *BASELINE, "--handoff", "h.json", "--host-json")
    assert host["valid"], host["errors"]
    assert {"code": "handoff_file_not_available", "location": "files[F0]"} in host["warnings"]
    plain = _run(tmp_path, {"h.json": handoff}, *BASELINE, "--handoff", "h.json")
    assert {item["code"] for item in plain["errors"]} == {"input_not_authorized"}, "普通模式仍要 --input-file 授权"


def test_host_mode_unreadable_target_is_a_structured_error(tmp_path):
    report = _run(tmp_path, {"p.json": b"{broken"}, "--project", "p.json", "--host-json")
    assert report["valid"] is False and {"code": "target_unreadable", "location": "$"} in report["errors"]


def test_plain_report_and_exit_code_are_unchanged_without_host_json(tmp_path):
    project = copy.deepcopy(_project())
    project.pop("locations")
    report = _run(tmp_path, {"p.json": project}, "--project", "p.json")
    assert report["schema"] == "drama_workflow_check.v2" and not report["structure_valid"]
