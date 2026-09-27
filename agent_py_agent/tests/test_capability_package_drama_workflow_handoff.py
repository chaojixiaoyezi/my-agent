# LLM: 仅以公开合成输入验证 B 包的显式交接合同；不得消费真实任务或执行宿主、模型及外部资源。
# 模块用途: 检查交接文件授权、字节摘要、结构化地址、阶段关系与独立 CLI 的失败边界。

from __future__ import annotations

import copy
import hashlib
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "examples/capability-packages/drama-workflow-b"
SCRIPT = PACKAGE / "scripts/check_continuity.py"


# LLM: run_name 不取 __main__，只加载独立脚本的函数；不读取安装包或启动真实业务。
# 函数用途: 每例重新加载正式脚本，让预算和 I/O 替身不会影响其它测试。
@pytest.fixture
def checker():
    return runpy.run_path(str(SCRIPT))


# LLM: 所有字节都来自本测试或公开合成项目；原始排版刻意保留，用于区分文件摘要与重编码摘要。
# 函数用途: 构造一个输入、一个项目和一段明确转换的最小交接，并写入 pytest 临时目录。
@pytest.fixture
def material(tmp_path):
    source = tmp_path / "source.json"
    source.write_bytes(b'{ "items": [ {"id":"SRC1","text":"fixture"}, {"id":"SRC2"} ], "a/b":{"~":7} }\n')
    project = tmp_path / "project.json"
    project.write_bytes((PACKAGE / "resources/example-project.json").read_bytes())
    project_data = json.loads(project.read_bytes())
    reference = {"file_id": "F1", "pointer": "/items/0", "object_id": "SRC1"}
    target = {"file_id": "F2", "pointer": "/shots/0", "object_id": project_data["shots"][0]["id"]}
    handoff = {
        "schema": "drama_workflow_handoff.v2",
        "files": [{"id": identifier, "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                  for identifier, path in (("F1", source), ("F2", project))],
        "stages": [{"id": "ST1", "scope": "公开合成资料转换", "input_file_ids": ["F1"],
                    "output_file_ids": ["F2"], "review_notes": "只做测试中的结构检查"}],
        "object_mappings": [{"stage_id": "ST1", "source": reference, "target": target, "reason": "合成映射"}],
        "omissions": [{"stage_id": "ST1", "source": {"file_id": "F1", "pointer": "/items/1", "object_id": "SRC2"},
                       "reason": "合成省略"}],
        "additions": [],
        "unresolved_differences": [],
    }
    return handoff, {"F1": source, "F2": project}


# LLM: 唯一执行对象是仓库公开的单文件脚本，输入仅为 pytest 夹具；不执行真实 case 的产物。
# 函数用途: 捕获独立 CLI 的原退出码和 JSON/HTML 输出。
def _run(tmp_path, handoff, bindings, *extra, script=SCRIPT):
    handoff_path = tmp_path / "handoff.json"
    handoff_path.write_text(json.dumps(handoff, ensure_ascii=False), encoding="utf-8")
    args = [sys.executable, str(script), "--project", str(bindings["F2"]), "--handoff", str(handoff_path)]
    for identifier, path in bindings.items():
        args += ["--input-file", f"{identifier}={path}"]
    return subprocess.run([*args, *extra], cwd=tmp_path, text=True, capture_output=True, timeout=10, check=False)


# LLM: 只读取结构化错误码，不根据解释文案推导状态。
# 函数用途: 保持参数化反例聚焦实际合同裁决。
def _codes(result):
    return {item["code"] for item in result["errors"]}


def test_bound_handoff_checks_original_bytes_without_modifying_inputs(checker, material):
    handoff, bindings = material
    before = {key: path.read_bytes() for key, path in bindings.items()}
    original = copy.deepcopy(handoff)
    result = checker["check_handoff"](handoff, bindings)
    assert result["structure_valid"], result
    assert result["metrics"]["files_checked"] == 2
    assert {key: path.read_bytes() for key, path in bindings.items()} == before
    assert handoff == original


@pytest.mark.parametrize("location,value,code", [
    (("schema",), "drama_workflow_handoff.v1", "unsupported_schema"),
    (("files", 0, "sha256"), "0" * 64, "sha256_mismatch"),
    (("files", 0, "sha256"), "0" * 63, "invalid_sha256"),
    (("files", 0, "id"), "", "invalid_or_duplicate_id"),
    (("stages", 0, "scope"), "", "text_required"),
    (("stages", 0, "input_file_ids"), ["MISSING"], "unknown_reference"),
    (("stages", 0, "input_file_ids"), ["F1", "F1"], "duplicate_reference"),
    (("stages", 0, "output_file_ids"), [], "nonempty_required"),
    (("object_mappings", 0, "stage_id"), "MISSING", "unknown_stage"),
    (("object_mappings", 0, "source", "file_id"), "F2", "file_outside_stage"),
    (("object_mappings", 0, "target", "file_id"), "F1", "file_outside_stage"),
    (("object_mappings", 0, "target", "object_id"), "SH01 / SH02", "object_id_mismatch"),
    (("object_mappings", 0, "target", "pointer"), "/absent", "pointer_not_found"),
    (("object_mappings", 0, "source", "pointer"), "/items/01", "invalid_pointer"),
    (("object_mappings", 0, "source", "pointer"), "/items/-", "invalid_pointer"),
    (("object_mappings", 0, "source", "pointer"), "/items/99", "pointer_not_found"),
    (("object_mappings", 0, "source", "pointer"), "/items/0/text/next", "pointer_not_found"),
    (("object_mappings", 0, "source", "pointer"), "/bad~2escape", "invalid_pointer"),
    (("object_mappings", 0, "source", "pointer"), "items/0", "invalid_pointer"),
    (("object_mappings", 0, "reason"), "", "text_required"),
    (("omissions", 0, "source", "file_id"), "F2", "file_outside_stage"),
    (("omissions", 0, "reason"), [], "text_required"),
])
def test_invalid_handoff_facts_fail_without_alias_or_semantic_guessing(checker, material, location, value, code):
    handoff, bindings = material
    parent = handoff
    for key in location[:-1]:
        parent = parent[key]
    parent[location[-1]] = value
    report = checker["check_handoff"](handoff, bindings)
    assert not report["structure_valid"]
    assert code in _codes(report), report


def test_pointer_resolves_escaped_keys_and_scalars_without_claiming_object_identity(checker, material):
    handoff, bindings = material
    handoff["object_mappings"][0]["source"] = {"file_id": "F1", "pointer": "/a~1b/~0"}
    assert checker["check_handoff"](handoff, bindings)["structure_valid"]
    handoff["object_mappings"][0]["source"]["object_id"] = "7"
    assert "object_id_mismatch" in _codes(checker["check_handoff"](handoff, bindings))


@pytest.mark.parametrize("section", ["files", "stages"])
def test_unique_file_and_stage_ids_are_required(checker, material, section):
    handoff, bindings = material
    handoff[section].append(copy.deepcopy(handoff[section][0]))
    assert "invalid_or_duplicate_id" in _codes(checker["check_handoff"](handoff, bindings))


def test_handoff_aliases_are_rejected_even_if_the_new_fields_are_also_present(checker, material):
    handoff, bindings = material
    handoff["object_mappings"][0]["source_file"] = "F1"
    assert "unexpected_field" in _codes(checker["check_handoff"](handoff, bindings))


@pytest.mark.parametrize("change", ["missing_binding", "extra_binding", "different_path", "invalid_digest", "wrong_schema"])
def test_invalid_grants_or_contract_never_open_bound_documents(checker, material, monkeypatch, change):
    handoff, bindings = material
    if change == "missing_binding":
        bindings.pop("F1")
    elif change == "extra_binding":
        bindings["OTHER"] = bindings["F1"].parent / "never-open.json"
    elif change == "different_path":
        handoff["files"][0]["path"] = "/never-open-private-document.json"
    elif change == "invalid_digest":
        handoff["files"][0]["sha256"] = "not-a-digest"
    else:
        handoff["schema"] = "drama_workflow_handoff.v1"
    calls = []
    monkeypatch.setitem(checker["check_handoff"].__globals__, "read_snapshot", lambda path: calls.append(path))
    assert not checker["check_handoff"](handoff, bindings)["structure_valid"]
    assert calls == []


@pytest.mark.parametrize("kind,code", [("missing", "input_not_found"), ("directory", "input_not_regular"),
                                     ("symlink", "input_not_authorized"), ("malformed", "input_invalid_json")])
def test_input_failures_remain_distinct(checker, material, kind, code):
    handoff, bindings = material
    path = bindings["F1"]
    path.unlink()
    if kind == "directory":
        path.mkdir()
    elif kind == "symlink":
        path.symlink_to(bindings["F2"])
    elif kind == "malformed":
        path.write_text('{"unfinished":', encoding="utf-8")
    assert code in _codes(checker["check_handoff"](handoff, bindings))


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}',
                                  b'{"x":1e9999}', b'"\xff"', b'[' * 65 + b'0' + b']' * 65])
def test_nonstandard_or_deep_json_cannot_become_handoff_input(checker, material, raw):
    handoff, bindings = material
    bindings["F1"].write_bytes(raw)
    handoff["files"][0]["sha256"] = hashlib.sha256(raw).hexdigest()
    report = checker["check_handoff"](handoff, bindings)
    assert not report["structure_valid"]
    assert _codes(report) & {"input_invalid_json", "input_depth_limit"}


@pytest.mark.parametrize("constant,value,code", [("MAX_DOCUMENT_BYTES", 8, "input_size_limit"),
                                               ("MAX_TOTAL_BYTES", 8, "input_total_size_limit"),
                                               ("MAX_FILES", 1, "file_count_limit"),
                                               ("MAX_POINTER_CHARS", 3, "pointer_limit")])
def test_bounds_are_enforced_before_unbounded_read_or_traversal(checker, material, monkeypatch, constant, value, code):
    handoff, bindings = material
    monkeypatch.setitem(checker["check_handoff"].__globals__, constant, value)
    assert code in _codes(checker["check_handoff"](handoff, bindings))


def test_additions_and_unresolved_keep_explicit_stage_references(checker, material):
    handoff, bindings = material
    target = copy.deepcopy(handoff["object_mappings"][0]["target"])
    handoff["additions"] = [{"stage_id": "ST1", "target": target, "reason": "合成新增声明"}]
    handoff["unresolved_differences"] = [{"stage_id": "ST1", "refs": [target],
                                        "difference": "语义需另审", "next_step": "审阅本次来源"}]
    report = checker["check_handoff"](handoff, bindings)
    assert report["structure_valid"]
    assert "unresolved_differences_present" in {item["code"] for item in report["warnings"]}
    handoff["unresolved_differences"][0]["refs"][0]["file_id"] = "UNKNOWN"
    assert "unknown_reference" in _codes(checker["check_handoff"](handoff, bindings))


def test_same_snapshot_supplies_hash_and_json_once(checker, material, monkeypatch):
    handoff, bindings = material
    globals_ = checker["check_handoff"].__globals__
    original = globals_["read_snapshot"]
    calls = []

    def observe(path, **kwargs):
        calls.append(Path(path))
        return original(path, **kwargs)

    monkeypatch.setitem(globals_, "read_snapshot", observe)
    assert checker["check_handoff"](handoff, bindings)["structure_valid"]
    assert sorted(calls) == sorted(bindings.values())


def test_cli_is_standalone_and_reports_separate_check_scopes(material, tmp_path):
    handoff, bindings = material
    standalone = tmp_path / "standalone.py"
    standalone.write_bytes(SCRIPT.read_bytes())
    before = {key: path.read_bytes() for key, path in bindings.items()}
    run = _run(tmp_path, handoff, bindings, script=standalone)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result["schema"] == "drama_workflow_check.v2"
    assert result["checks"] == {"project": "passed", "handoff": "passed"}
    assert result["structure_valid"]
    assert {key: path.read_bytes() for key, path in bindings.items()} == before


def test_project_only_cli_marks_handoff_not_requested(material, tmp_path):
    _, bindings = material
    run = subprocess.run([sys.executable, str(SCRIPT), "--project", str(bindings["F2"])],
                         cwd=tmp_path, capture_output=True, text=True, timeout=10, check=False)
    assert run.returncode == 0
    result = json.loads(run.stdout)
    assert result["checks"] == {"project": "passed", "handoff": "not_requested"}
    assert any(item["code"] == "handoff_not_checked" for item in result["warnings"])


def test_html_keeps_scope_failure_and_unresolved_visible_without_executing_text(material, tmp_path):
    handoff, bindings = material
    handoff["object_mappings"][0]["target"]["pointer"] = "/<script>not-a-path</script>"
    run = _run(tmp_path, handoff, bindings, "--format", "html")
    assert run.returncode == 1
    assert "pointer_not_found" in run.stdout and "handoff" in run.stdout
    assert "<script>" not in run.stdout


def test_cli_duplicate_bindings_do_not_silently_take_last_value(material, tmp_path):
    handoff, bindings = material
    run = _run(tmp_path, handoff, bindings, "--input-file", f"F1={bindings['F2']}")
    assert run.returncode == 1
    assert "duplicate_binding" in _codes(json.loads(run.stdout))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="平台无 FIFO")
def test_explicit_fifo_is_rejected_without_blocking(checker, material):
    handoff, bindings = material
    bindings["F1"].unlink()
    os.mkfifo(bindings["F1"])
    assert "input_not_regular" in _codes(checker["check_handoff"](handoff, bindings))


@pytest.mark.parametrize("error,code", [(PermissionError(), "input_permission_denied"), (OSError(), "input_io_error")])
def test_real_reader_classifies_os_failures_without_echoing_details(checker, material, monkeypatch, error, code):
    handoff, bindings = material

    def refuse(*args, **kwargs):
        raise error

    monkeypatch.setattr(os, "open", refuse)
    result = checker["check_handoff"](handoff, bindings)
    assert code in _codes(result)
    assert all("message" not in item for item in result["errors"])


def test_project_snapshot_is_reused_by_handoff_in_same_cli_evaluation(checker, material, tmp_path, monkeypatch):
    handoff, bindings = material
    path = tmp_path / "handoff.json"
    path.write_text(json.dumps(handoff), encoding="utf-8")
    globals_ = checker["evaluate_inputs"].__globals__
    original, calls = globals_["read_snapshot"], []

    def observe(path, **kwargs):
        calls.append(Path(path))
        return original(path, **kwargs)

    monkeypatch.setitem(globals_, "read_snapshot", observe)
    _, result = checker["evaluate_inputs"](bindings["F2"], path, [f"{key}={value}" for key, value in bindings.items()])
    assert result["structure_valid"]
    assert calls.count(bindings["F2"]) == 1
    assert len(calls) == 3


@pytest.mark.parametrize("snapshot_origin", ["project", "binding"])
@pytest.mark.parametrize("alias_kind,expected_code", [("missing_parent", "input_not_found"),
                                                      ("symlink_parent", "sha256_mismatch"),
                                                      ("symlink_bound_digest", "")])
def test_lexical_alias_cannot_reuse_another_path_snapshot(checker, material, tmp_path,
                                                       snapshot_origin, alias_kind, expected_code):
    _, bindings = material
    project = bindings["F2"]
    alias = tmp_path / "alias" / ".." / project.name
    if alias_kind != "missing_parent":
        actual_parent = tmp_path / "other"
        (actual_parent / "inside").mkdir(parents=True)
        (actual_parent / project.name).write_bytes(b'{"different_file": true}\n')
        (tmp_path / "alias").symlink_to(actual_parent / "inside", target_is_directory=True)
    digest = hashlib.sha256(project.read_bytes()).hexdigest()
    paths = {"ALIAS": alias} if snapshot_origin == "project" else {"FIRST": project, "ALIAS": alias}
    handoff = {"schema": "drama_workflow_handoff.v2",
               "files": [{"id": key, "path": str(path), "sha256": digest} for key, path in paths.items()],
               "stages": [{"id": "S", "scope": "合成路径检查", "input_file_ids": [],
                           "output_file_ids": list(paths), "review_notes": "只核实际绑定字节"}],
               "object_mappings": [], "omissions": [], "additions": [], "unresolved_differences": []}
    if alias_kind == "symlink_bound_digest":
        handoff["files"][-1]["sha256"] = hashlib.sha256(alias.read_bytes()).hexdigest()
    if snapshot_origin == "project":
        handoff_path = tmp_path / "handoff.json"
        handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
        _, report = checker["evaluate_inputs"](project, handoff_path, [f"{key}={path}" for key, path in paths.items()])
        assert report["checks"]["project"] == "passed"
        assert report["checks"]["handoff"] == ("failed" if expected_code else "passed")
    else:
        report = checker["check_handoff"](handoff, paths)
    assert report["structure_valid"] == (not expected_code)
    if expected_code:
        assert expected_code in _codes(report)


def test_cli_missing_parent_alias_is_not_reported_as_checked(material, tmp_path):
    handoff, bindings = material
    project = bindings["F2"]
    missing = tmp_path / "missing" / ".." / project.name
    handoff["files"][1]["path"] = str(missing)
    changed_bindings = {**bindings, "F2": missing}
    run = _run(tmp_path, handoff, changed_bindings, "--project", str(project))
    report = json.loads(run.stdout)
    assert run.returncode == 1
    assert report["checks"] == {"project": "passed", "handoff": "failed"}
    assert "input_not_found" in _codes(report)


def test_same_bound_path_is_read_once_for_multiple_file_ids(checker, material, monkeypatch):
    handoff, bindings = material
    handoff["files"].append({**handoff["files"][0], "id": "SAME"})
    bindings["SAME"] = bindings["F1"]
    globals_ = checker["check_handoff"].__globals__
    original, calls = globals_["read_snapshot"], []

    def observe(path, **kwargs):
        calls.append(Path(path))
        return original(path, **kwargs)

    monkeypatch.setitem(globals_, "read_snapshot", observe)
    report = checker["check_handoff"](handoff, bindings)
    assert report["structure_valid"]
    assert report["metrics"]["files_checked"] == 3
    assert calls == [bindings["F1"], bindings["F2"]]


def test_semantic_false_reason_is_not_promoted_to_verified_story_truth(checker, material):
    handoff, bindings = material
    handoff["object_mappings"][0]["reason"] = "这句话未经事实审阅，结构检查不能替它背书"
    result = checker["check_handoff"](handoff, bindings)
    assert result["structure_valid"]
    assert {"code": "handoff_semantics_and_execution_not_checked"} in result["warnings"]


@pytest.mark.parametrize("value", [None, [], {}, True, "not-json-object"])
def test_handoff_root_must_be_declared_object(checker, material, value):
    _, bindings = material
    assert not checker["check_handoff"](value, bindings)["structure_valid"]


def test_relative_paths_resolve_only_against_the_invocation_cwd(checker, material, monkeypatch):
    handoff, bindings = material
    monkeypatch.chdir(bindings["F1"].parent)
    for row in handoff["files"]:
        row["path"] = Path(row["path"]).name
    assert checker["check_handoff"](handoff, bindings)["structure_valid"]


@pytest.mark.parametrize("extra", [["--input-file", "missing-equals"], ["--input-file", "=input.json"],
                                    ["--input-file", "F3="]])
def test_cli_malformed_binding_is_structured_error(material, tmp_path, extra):
    handoff, bindings = material
    run = _run(tmp_path, handoff, bindings, *extra)
    assert run.returncode == 1
    assert "invalid_binding" in _codes(json.loads(run.stdout))


def test_file_hash_is_raw_bytes_not_json_reserialization(checker, material):
    handoff, bindings = material
    normalized = json.dumps(json.loads(bindings["F1"].read_bytes())).encode()
    handoff["files"][0]["sha256"] = hashlib.sha256(normalized).hexdigest()
    assert "sha256_mismatch" in _codes(checker["check_handoff"](handoff, bindings))


def test_excessive_rows_are_rejected_before_reading(checker, material, monkeypatch):
    handoff, bindings = material
    handoff["object_mappings"] *= 2
    monkeypatch.setitem(checker["check_handoff"].__globals__, "MAX_ROWS", 1)
    assert "row_count_limit" in _codes(checker["check_handoff"](handoff, bindings))


def test_excessive_stage_references_are_not_expanded(checker, material, monkeypatch):
    handoff, bindings = material
    handoff["stages"][0]["input_file_ids"] = ["F1"] * 33
    calls = []
    monkeypatch.setitem(checker["check_handoff"].__globals__, "read_snapshot", lambda path: calls.append(path))
    assert "reference_count_limit" in _codes(checker["check_handoff"](handoff, bindings))
    assert calls == []


def test_json_strings_do_not_consume_structural_depth_budget(checker, material):
    handoff, bindings = material
    document = json.loads(bindings["F1"].read_bytes())
    document["text"] = ('["{}]\\' * 80)
    raw = json.dumps(document).encode()
    bindings["F1"].write_bytes(raw)
    handoff["files"][0]["sha256"] = hashlib.sha256(raw).hexdigest()
    assert checker["check_handoff"](handoff, bindings)["structure_valid"]


def test_modified_open_file_is_not_reported_as_stable_snapshot(checker, material, monkeypatch):
    _, bindings = material
    original = os.fstat
    calls = 0

    def change_before_final_stat(descriptor):
        nonlocal calls
        calls += 1
        if calls == 2:
            bindings["F1"].write_bytes(b'{"changed":true}')
        return original(descriptor)

    monkeypatch.setattr(os, "fstat", change_before_final_stat)
    with pytest.raises(checker["InputProblem"]) as failure:
        checker["read_snapshot"](bindings["F1"])
    assert failure.value.code == "input_changed"


def test_bindings_without_handoff_do_not_imply_it_was_checked(checker, material):
    _, bindings = material
    _, report = checker["evaluate_inputs"](bindings["F2"], None, [f"F1={bindings['F1']}"])
    assert not report["structure_valid"]
    assert report["checks"]["project"] == "passed"
    assert report["checks"]["handoff"] == "failed"
    assert "handoff_required" in _codes(report)


def test_bad_json_stops_further_reads_instead_of_reusing_unaccounted_budget(checker, material, monkeypatch):
    handoff, bindings = material
    for row in handoff["files"]:
        raw = b"x" * 1000
        bindings[row["id"]].write_bytes(raw)
        row["sha256"] = hashlib.sha256(raw).hexdigest()
    globals_ = checker["check_handoff"].__globals__
    monkeypatch.setitem(globals_, "MAX_TOTAL_BYTES", 1500)
    original, calls = globals_["read_snapshot"], []

    def observe(path, **kwargs):
        calls.append(Path(path))
        return original(path, **kwargs)

    monkeypatch.setitem(globals_, "read_snapshot", observe)
    result = checker["check_handoff"](handoff, bindings)
    assert "input_invalid_json" in _codes(result)
    assert calls == [bindings["F1"]]
