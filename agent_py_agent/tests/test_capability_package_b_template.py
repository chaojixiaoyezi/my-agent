# LLM: B 包模板测试只填公开合成资料、调用原只读检查器和隔离的派工组件；不读真实任务、运行模型或增加领域验收门。
# 模块用途: 验证完整占位条目能形成有效制作资料，交接资源可随包读取，文档授权示例保持当前宿主接口。

from __future__ import annotations

import copy
import hashlib
import json
import re
import runpy
from zipfile import ZipFile

import pytest

from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.plugin_content_activation import PluginContentActivation
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_installation import PluginInstallRequest
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
from agent_py_agent.tests.test_capability_package_examples import (
    EXAMPLES,
    _fixture,
    _run,
    _write_json,
)
from scripts.build_capability_package import build_capability_package

PACKAGE = "drama-workflow-b"
PACKAGE_ROOT = EXAMPLES / PACKAGE


# LLM: 只沿模板已有键填写，缺失占位字段不能由测试偷偷补齐；动作和对白使用各自明确的条目。
# 函数用途: 用仓库公开例子填充模板，检验模型仅按模板填写时能否得到原检查器接受的格式。
def _filled_project() -> dict:
    template = json.loads((PACKAGE_ROOT / "templates/project.json").read_text(encoding="utf-8"))
    example = _fixture(PACKAGE, "example-project.json")
    result = {"schema": template["schema"], "title": example["title"]}
    for key in ("episodes", "characters", "locations", "props", "scenes", "shots", "references"):
        shape = template[key][0]
        result[key] = []
        for row in example[key]:
            filled = {field: copy.deepcopy(row[field]) for field in shape if field != "beats"}
            if key == "scenes":
                beat_shapes = {beat["kind"]: beat for beat in shape["beats"]}
                filled["beats"] = [
                    {field: copy.deepcopy(beat[field]) for field in beat_shapes[beat["kind"]]}
                    for beat in row["beats"]
                ]
            result[key].append(filled)
    return result


# LLM: 加载现有脚本中的纯函数，不执行其 CLI，也不创建第二份校验规则。
# 函数用途: 让字段反例复用样包当前的原始检查器。
@pytest.fixture
def check_project():
    return runpy.run_path(str(PACKAGE_ROOT / "scripts/check_continuity.py"))["check_project"]


def test_filled_template_passes_original_read_only_checker(tmp_path):
    project = _filled_project()
    path = _write_json(tmp_path, "project.json", project)
    before = path.read_bytes()

    completed = _run(PACKAGE, "check_continuity.py", tmp_path, ["--project", str(path)])

    result = json.loads(completed.stdout)
    assert completed.returncode == 0, result
    assert result["structure_valid"]
    assert result["metrics"]["episode_seconds"] == {"EP01": 60.0}
    assert result["metrics"]["shots"] == 3
    assert path.read_bytes() == before
    assert {item["code"] for item in result["warnings"]} == {
        "reference_media_not_verified", "creative_quality_and_media_not_checked",
    }


def test_unfilled_template_is_not_a_valid_delivery(check_project):
    template = json.loads((PACKAGE_ROOT / "templates/project.json").read_text(encoding="utf-8"))
    result = check_project(template)
    assert not result["structure_valid"]
    assert {"code": "invalid_or_duplicate_id", "path": "scenes[0]"} in result["errors"]


@pytest.mark.parametrize(("path", "code"), [
    (("scenes", 0, "episode_id"), "unknown_reference"),
    (("scenes", 0, "location_id"), "unknown_reference"),
    (("scenes", 0, "character_ids"), "references_required"),
    (("scenes", 0, "prop_ids"), "references_required"),
    (("scenes", 0, "beats"), "list_required"),
    (("scenes", 0, "beats", 1, "character_id"), "speaker_outside_scene"),
    (("scenes", 0, "beats", 0, "text"), "beat_text_required"),
    (("shots", 0, "beat_ids"), "beat_reference_required"),
    (("shots", 0, "reference_ids"), "references_required"),
    (("shots", 0, "seconds"), "positive_seconds_required"),
    (("references", 0, "subject_id"), "unknown_reference_subject"),
    (("references", 0, "state"), "invalid_reference_state"),
])
def test_missing_required_template_field_still_fails(check_project, path, code):
    project = _filled_project()
    target = project
    for part in path[:-1]:
        target = target[part]
    del target[path[-1]]
    result = check_project(project)
    assert not result["structure_valid"]
    assert code in {error["code"] for error in result["errors"]}


def test_placeholder_target_is_invalid_but_missing_target_keeps_original_warning(check_project):
    project = _filled_project()
    project["episodes"][0]["target_seconds"] = None
    assert {"code": "positive_target_seconds_required", "path": "EP01.target_seconds"} in check_project(project)["errors"]
    del project["episodes"][0]["target_seconds"]
    result = check_project(project)
    assert result["structure_valid"]
    assert {"code": "episode_target_missing", "path": "EP01.target_seconds"} in result["warnings"]


def test_template_does_not_require_invented_props_or_reference_plans(check_project):
    project = _filled_project()
    project["props"] = project["references"] = []
    for scene in project["scenes"]:
        scene["prop_ids"] = []
    for shot in project["shots"]:
        shot["reference_ids"] = []
    result = check_project(project)
    assert result["structure_valid"]
    assert {item["code"] for item in result["warnings"]} == {"creative_quality_and_media_not_checked"}


def test_handoff_records_raw_files_and_explicit_mapping_without_claiming_validation(tmp_path, check_project):
    handoff = json.loads((PACKAGE_ROOT / "templates/handoff.json").read_text(encoding="utf-8"))
    source = tmp_path / "source.json"
    source.write_bytes(b'{\r\n  "cast": [{"id": "SRC-C01", "name": "actor"}, {"id": "SRC-C99", "name": "other"}]\r\n}\r\n')
    project = _filled_project()
    output = _write_json(tmp_path, "project.json", project)
    file_shape = handoff["files"][0]
    handoff["files"] = [
        {field: values[field] for field in file_shape}
        for values in (
            {"id": "input-1", "path": source.name, "sha256": hashlib.sha256(source.read_bytes()).hexdigest()},
            {"id": "project-1", "path": output.name, "sha256": hashlib.sha256(output.read_bytes()).hexdigest()},
        )
    ]
    mapping = {"source_file_id": "input-1", "source_field": "cast.id", "source_object_id": "SRC-C01",
               "target_file_id": "project-1", "target_field": "characters.id", "target_object_id": "C01",
               "reason": "将来源角色编号对应到制作资料角色编号"}
    handoff["object_mappings"] = [{field: mapping[field] for field in handoff["object_mappings"][0]}]
    handoff["stages"][0].update(id="characters", scope="角色资料转换", input_file_ids=["input-1"],
                                 output_file_ids=["project-1"], review_notes="仅核对本条编号映射，其余语义尚未审阅")
    omitted = {"source_file_id": "input-1", "source_field": "cast.id", "source_object_id": "SRC-C99",
               "reason": "本次合成场次没有采用此来源角色"}
    added = {"target_file_id": "project-1", "target_field": "characters.id", "target_object_id": "C02",
             "reason": "公开合成示例中的另一角色，非该输入已提供事实"}
    handoff["omissions"] = [{field: omitted[field] for field in handoff["omissions"][0]}]
    handoff["additions"] = [{field: added[field] for field in handoff["additions"][0]}]
    handoff["unresolved_differences"][0].update(file_ids=["input-1", "project-1"], object_ids=["C01"],
                                               difference="来源未提供 visual_anchor", next_step="由制作方补充并审阅")
    saved = _write_json(tmp_path, "handoff.json", handoff)
    recovered = json.loads(saved.read_text(encoding="utf-8"))

    assert recovered["files"][0]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    normalized = json.dumps(json.loads(source.read_bytes())).encode()
    assert recovered["files"][0]["sha256"] != hashlib.sha256(normalized).hexdigest()
    assert recovered["object_mappings"][0] == mapping
    assert recovered["omissions"] == [omitted]
    assert recovered["additions"] == [added]
    assert recovered["stages"][0]["output_file_ids"] == ["project-1"]
    assert recovered["unresolved_differences"][0]["object_ids"] == ["C01"]
    assert check_project(project)["structure_valid"]
    assert {"code": "unsupported_schema", "path": "schema"} in check_project(recovered)["errors"]


def test_handoff_and_complete_template_are_declared_private_resources(tmp_path):
    declaration = json.loads((PACKAGE_ROOT / "declaration.json").read_text(encoding="utf-8"))
    bundle = build_capability_package(declaration, PACKAGE_ROOT, tmp_path / "package.zip")
    manifest = inspect_plugin_package(bundle.read_bytes()).manifest
    assert manifest.version == "0.1.2"
    assert manifest.is_content_only and not manifest.skills and not manifest.tools
    files = {member.path: member for member in manifest.files}
    with ZipFile(bundle) as archive:
        for name in ("templates/project.json", "templates/handoff.json"):
            raw = archive.read(name)
            assert raw == (PACKAGE_ROOT / name).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == files[name].sha256
            assert files[name].executable is False


@pytest.mark.parametrize("grant_mode", ["explicit", "omitted", "empty"])
def test_documented_batch_grant_pins_host_version_without_default_expansion(tmp_path, grant_mode):
    instructions = (PACKAGE_ROOT / "CAPABILITY.md").read_text(encoding="utf-8")
    example = re.search(r"```json\n(.*?)\n```", instructions, re.DOTALL)
    assert example is not None
    request = json.loads(example.group(1))
    assert request["items"][0]["allowed_skills"] == [f"capability:{PACKAGE}"]
    if grant_mode == "omitted":
        del request["items"][0]["allowed_skills"]
    elif grant_mode == "empty":
        request["items"][0]["allowed_skills"] = []
    request["defer_start"] = True
    agent = SimpleAgent(AgentConfig(enable_plugins=True, enable_subagents=True, prompt_files=[]), tmp_path / "repo")
    store = PluginInstallStore(resolve_owner_home(agent.home_paths.root))
    declaration = json.loads((PACKAGE_ROOT / "declaration.json").read_text(encoding="utf-8"))
    bundle = build_capability_package(declaration, PACKAGE_ROOT, tmp_path / "package.zip")
    package = inspect_plugin_package(bundle.read_bytes())
    installed = store.install(PluginInstallRequest(package, "install-test", 0)).installation
    activation = PluginContentActivation("enable-test", PACKAGE, installed.package_sha256,
                                         installed.revision, installed.settings_revision)
    enabled = store.change_activation(PluginActivationRequest("enable-test", installed.revision, activation)).installation

    result = CreateSubagentsTool(agent).execute(request)

    assert result.ok, result.output
    tasks = agent.subagents.list_runs()
    assert len(tasks) == 1
    task = tasks[0]
    assert task.allowed_skills == ([f"capability:{PACKAGE}"] if grant_mode == "explicit" else [])
    refs = task.attributes.get("skill_snapshot_refs", [])
    if grant_mode == "explicit":
        assert refs == [agent.current_skill_snapshot().resolve_package(PACKAGE).to_ref()]
        assert refs[0]["activation_id"] == enabled.activation_id
        assert refs[0]["content_sha256"] == enabled.package_sha256
    else:
        assert not refs
