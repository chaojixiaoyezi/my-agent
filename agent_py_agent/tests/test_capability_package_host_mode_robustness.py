# LLM: 钉住 ae 块 7 审阅模糊测试找到的 4 处崩溃（列表或对象放进集合、当字典键）：A prop_mention_warnings、
#   B shot_reference_warnings、stage_addresses、beat_warnings。宿主模式崩溃会让宿主只记 verifier_output_invalid、丢掉整次结论，
#   所以这些坏形状输入在 --host-json 下必须输出合格的 pack_verifier_result.v1 并退 0；普通模式也不能崩。只用公开合成资料。
# 模块用途: 证明检查器遇到坏形状的字段只报结构错误或跳过提醒，不会自己崩溃。

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_py_agent.tests.test_capability_package_examples import EXAMPLES

A = EXAMPLES / "drama-text-a"
B = EXAMPLES / "drama-workflow-b"
BAD_VALUES = (["X01"], {"a": 1})


# 函数用途: 读一份公开示例的可变副本。
def _example(package: Path, name: str) -> dict:
    return json.loads((package / "resources" / name).read_text(encoding="utf-8"))


# LLM: 在 pytest 临时目录写入输入并用隔离解释器跑检查器；返回（stdout 解析结果, 退出码），stderr 必须为空（没崩）。
# 函数用途: 跑一次检查器并做最基本的“没崩”核对。
def _run(tmp_path: Path, script: Path, files: dict, args: list[str]) -> tuple[dict, int]:
    for name, data in files.items():
        (tmp_path / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    process = subprocess.run([sys.executable, "-I", "-S", str(script), *args], cwd=tmp_path, capture_output=True,
                             text=True, encoding="utf-8", timeout=20)
    assert not process.stderr, process.stderr
    return json.loads(process.stdout), process.returncode


# LLM: 宿主合同：只有一个 v1 对象、valid 与 errors 一致、每个 code 是非空字符串、退出码 0。
# 函数用途: 断言一次宿主模式输出合格。
def _assert_host_contract(result: dict, code: int) -> None:
    assert code == 0 and result["schema"] == "pack_verifier_result.v1"
    assert result["valid"] == (not result["errors"])
    assert all(isinstance(item["code"], str) and item["code"] for item in result["errors"] + result["warnings"])


# 函数用途: A 包：把 SH01 起点道具的 prop_id 换成坏值，返回（文件, 参数）。
def _a_prop_id(bad: object) -> tuple[dict, list[str]]:
    source_raw = (A / "resources/example-source.json").read_bytes()
    delivery = _example(A, "example-delivery.json")
    delivery["source_sha256"] = hashlib.sha256(source_raw).hexdigest()
    delivery["shots"][0]["prop_states"]["start"][0]["prop_id"] = bad
    return {"s.json": json.loads(source_raw), "d.json": delivery}, ["--source", "s.json", "--delivery", "d.json"]


# 函数用途: B 包：人物参考的 subject_id 换成坏值。
def _b_subject_id(bad: object) -> tuple[dict, list[str]]:
    project = _example(B, "example-project.json")
    project["references"][0]["subject_id"] = bad
    return {"p.json": project}, ["--project", "p.json"]


# 函数用途: B 包：交接映射地址的 file_id 换成坏值（带基线和交接）。
def _b_handoff_file_id(bad: object) -> tuple[dict, list[str]]:
    project = _example(B, "example-project.json")
    handoff = json.loads((B / "templates/handoff.json").read_text(encoding="utf-8"))
    handoff["object_mappings"][0]["source"]["file_id"] = bad
    return ({"p.json": project, "b.json": copy.deepcopy(project), "h.json": handoff},
            ["--project", "p.json", "--baseline-project", "b.json", "--handoff", "h.json"])


# 函数用途: B 包：带基线时把一句台词的 text 换成坏值。
def _b_dialogue_text(bad: object) -> tuple[dict, list[str]]:
    project = _example(B, "example-project.json")
    baseline = copy.deepcopy(project)
    next(beat for beat in project["scenes"][0]["beats"] if beat["kind"] == "dialogue")["text"] = bad
    return {"p.json": project, "b.json": baseline}, ["--project", "p.json", "--baseline-project", "b.json"]


CASES = {"a_prop_id": (A / "scripts/check_delivery.py", _a_prop_id),
         "b_subject_id": (B / "scripts/check_continuity.py", _b_subject_id),
         "b_handoff_file_id": (B / "scripts/check_continuity.py", _b_handoff_file_id),
         "b_dialogue_text": (B / "scripts/check_continuity.py", _b_dialogue_text)}


@pytest.mark.parametrize("bad", BAD_VALUES, ids=["list", "object"])
@pytest.mark.parametrize("case", CASES)
def test_bad_shapes_never_crash_the_checker_in_host_mode(tmp_path, case, bad):
    script, build = CASES[case]
    files, args = build(bad)
    result, code = _run(tmp_path, script, files, [*args, "--host-json"])
    _assert_host_contract(result, code)
    assert result["valid"] is False, "坏形状本身是结构错误，要报出来，不是静默通过"


@pytest.mark.parametrize("case", CASES)
def test_bad_shapes_never_crash_the_checker_in_plain_mode(tmp_path, case):
    script, build = CASES[case]
    files, args = build(["X01"])
    report, code = _run(tmp_path, script, files, args)
    assert code == 1 and report["structure_valid"] is False
