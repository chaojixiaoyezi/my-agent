# LLM: B9 的 v8 索引、真实文件包/stdio 与旧 wheel 回归；全部样本只在工作树副本，不调用管理入口。
# 模块用途: 验证双语言作者模板的事件与收紧协议，保留用户确认边界和旧构建器字节对照。
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from agent_py_agent.agent.capability.skills import parse_skill_file
from agent_py_agent.agent.plugin_manifest import PluginManifest, PluginPackageError
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.plugin_wheels import inspect_plugin_wheels
from scripts.build_plugin_files_package import build_files_package
from scripts.build_plugin_package import _manifest_schema, build_plugin_package
from scripts.plugin_build import build_wheel

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "agent_py_agent/skills/builtin/plugins/write-my-agent-plugin"
# LLM: 历史 Git 字节的只读夹具。车道容器只拉一个提交、源码包环境根本没有 git 时，本地对象库
#   不一定有那两个提交；把字节存成夹具后这些用例不再依赖 git，来源提交与 sha256 见同目录 README。
# 常量用途: 指向夹具里旧 Python 模板与旧构建脚本的位置。
_LEGACY_FIXTURES = ROOT / "agent_py_agent/tests/fixtures/b9_legacy_git_bytes"
_LEGACY_PYTHON_TEMPLATE = _LEGACY_FIXTURES / "legacy_python_template"
# 末尾 .txt 是刻意的：夹具是"历史字节"而不是产品源码，避免被代码尺寸扫描当成在运代码。
# 用例只读它的字节再 compile/exec，扩展名不影响语义。
_LEGACY_BUILD_SCRIPT = _LEGACY_FIXTURES / "legacy_build_script/build_plugin_package.py.txt"
# 夹具的来源提交，校验用例用它去对象库里取原始字节作对照。
_LEGACY_TEMPLATE_COMMIT = "35f445792"
_LEGACY_BUILD_SCRIPT_COMMIT = "b453f8883"
# macOS 访达会在用户浏览过的目录里留 .DS_Store；清理临时目录时它可能刚被写进去，
# 于是 rmtree 撞上"目录非空"（Errno 66）。只对这一类已知噪声重试，其它错误照旧抛出。
FINDER_METADATA_NAMES = frozenset({".DS_Store", ".AppleDouble", ".LSOverride"})


# LLM: 临时目录在访达可达的工作树 tmp/ 下，清理必须对"删除瞬间被访达写入 .DS_Store"这种已知竞态免疫，
#   但不能整体 ignore_errors——真错误（权限、占用、其它文件）要被报出来，否则门禁会在无声中退化成假绿。
# 函数用途: 删除临时目录；首轮失败且现场只剩访达元数据时清掉它们再试一次，其它错误原样抛出。
def _rmtree_tolerating_finder_metadata(path: Path) -> None:
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
        return
    except OSError as first_error:
        leftovers = [item for item in path.rglob("*") if item.name in FINDER_METADATA_NAMES]
        if not leftovers:
            raise first_error
    for item in leftovers:
        item.unlink(missing_ok=True)
    shutil.rmtree(path)


# LLM: pytest basetemp 按车道规则保留；本组业务样本、wheel 和 pip 临时目录全部放工作树 tmp，结束自动清理。
#   夹具本体抽成普通上下文管理器，用例才能直接验“用完是否留下临时目录”；夹具只是它的一层包装。
# 函数用途: 给出隔离的临时工作目录，退出时用容忍访达元数据的清理删除它。
@contextlib.contextmanager
def _isolated_author_workspace(monkeypatch):
    scratch = ROOT / "tmp"
    scratch.mkdir(exist_ok=True)
    directory = tempfile.mkdtemp(prefix="b9-m1b9-", dir=scratch)
    try:
        root = Path(directory)
        monkeypatch.setattr(tempfile, "tempdir", str(root))
        monkeypatch.setenv("TMPDIR", str(root))
        yield root
    finally:
        _rmtree_tolerating_finder_metadata(Path(directory))


# LLM: 夹具只做一次转发，保持既有 author_workspace 名字不变，避免影响本文件其它用例。
# 函数用途: 给测试提供隔离的插件构建工作目录。
@pytest.fixture
def author_workspace(monkeypatch):
    with _isolated_author_workspace(monkeypatch) as root:
        yield root


# LLM: 这条用例模拟访达竞态：rmtree 首轮失败后目录里只剩 .DS_Store 时必须能清干净，
#   而现场还有普通文件时（不是访达噪声造成的非空）必须把原始错误抛出来——不能为了兼容 .DS_Store 吞掉真错误。
# 函数用途: 验证夹具清理对访达元数据容忍、对其它错误不宽容。
def test_cleanup_tolerates_finder_metadata_but_not_real_failures(tmp_path, monkeypatch):
    blocked = tmp_path / "with_metadata"
    blocked.mkdir()
    (blocked / "release-source").mkdir()
    (blocked / ".DS_Store").write_bytes(b"\x00\x01")
    first_attempt = {"count": 0}
    real_rmtree = shutil.rmtree

    # 模拟"删除过程中访达刚写进 .DS_Store"：首轮先抛 Errno 66，让重试路径拿到真实的现场。
    def flaky_rmtree(target, *args, **kwargs):
        first_attempt["count"] += 1
        if first_attempt["count"] == 1:
            raise OSError(66, "Directory not empty", str(target))
        return real_rmtree(target, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", flaky_rmtree)
    _rmtree_tolerating_finder_metadata(blocked)
    assert not blocked.exists(), "只剩 .DS_Store 时应当能清干净"
    assert first_attempt["count"] == 2, "首轮失败后应当重试一次"

    occupied = tmp_path / "with_real_content"
    occupied.mkdir()
    (occupied / "release-source").mkdir()
    (occupied / "release-source" / "leftover.py").write_text("x = 1\n", encoding="utf-8")
    (occupied / ".DS_Store").write_bytes(b"\x00\x01")
    always_fails = {"count": 0}

    def failing_rmtree(target, *args, **kwargs):
        always_fails["count"] += 1
        raise OSError(66, "Directory not empty", str(target))

    monkeypatch.setattr(shutil, "rmtree", failing_rmtree)
    with pytest.raises(OSError):
        _rmtree_tolerating_finder_metadata(occupied)
    assert always_fails["count"] == 1 or always_fails["count"] == 2, "真失败时要抛出，不能被吞掉"


# LLM: 夹具把临时目录建在工作树 tmp/ 下（沙箱只能写工作树），所以"跑完不留临时子目录"是它的验收点；
#   这里直接走真实夹具生命周期，确认退出后 tmp/ 下没有 b9-m1b9-* 残留（访达自己的 .DS_Store 不算，且已被 git 忽略）。
# 函数用途: 验证 author_workspace 用完即净，不把临时目录留在工作树。
def test_author_workspace_leaves_no_temp_directory_behind():
    scratch = ROOT / "tmp"
    with pytest.MonkeyPatch.context() as patch:
        with _isolated_author_workspace(patch) as root:
            assert root.is_dir(), "应当给出可用的临时目录"
    assert not root.exists(), "夹具退出后临时目录不应残留"
    assert not list(scratch.glob("b9-m1b9-*")), "tmp/ 下不应留下 b9-m1b9-* 临时目录"


# LLM: 复用产品 CLI，不替换构建后端或读包器；失败要带完整 stderr/stdout，不洗成成功。
# 函数用途: 执行一次离线构建命令，返回真实标准输出。
def _run(command: list[str], workspace: Path, input_text: str | None = None):
    result = subprocess.run(command, cwd=ROOT, input=input_text, capture_output=True,
                            text=True, timeout=120, env={"PATH": os.defpath, "TMPDIR": str(workspace),
                                                       "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert not result.stderr, result.stderr
    return result.stdout


# LLM: 固定一组协议请求验证初始化、单工具目录、Unicode/空值、错误及错误后的继续调用；无宿主、无安装表。
# 函数用途: 生成两个模板共享的真实 stdio 冒烟输入。
def _requests():
    calls = [
        {"method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
        {"method": "notifications/initialized"},
        {"method": "tools/list"},
        {"method": "tools/call", "params": {"name": "count_text", "arguments": {"text": "哥哥🙂\n"}}},
        {"method": "tools/call", "params": {"name": "count_text", "arguments": {"text": ""}}},
        {"method": "tools/call", "params": {"name": "count_text", "arguments": {"text": 9}}},
        {"method": "tools/call", "params": {"name": "absent", "arguments": {}}},
        {"method": "tools/call", "params": {"name": "count_text", "arguments": {"text": "x", "path": "secret"}}},
        {"method": "tools/call", "params": {"name": "count_text", "arguments": {"text": "x" * 4097}}},
        {"method": "tools/call", "params": {"name": "count_text", "arguments": {"text": "正常"}}},
    ]
    return "".join(json.dumps({"jsonrpc": "2.0", **call, **({"id": index} if index != 1 else {})},
                              ensure_ascii=False) + "\n" for index, call in enumerate(calls))


def test_skill_is_indexed_and_body_loads(author_workspace, skill_catalog_factory):
    builtin = author_workspace / "catalog/builtin/plugins/write-my-agent-plugin"
    shutil.copytree(SKILL, builtin)
    catalog = skill_catalog_factory(author_workspace / "catalog")
    snapshot = catalog.service.snapshot_for(catalog.workspace, force_reload=True)
    entry = snapshot.resolve("builtin:write-my-agent-plugin")
    assert entry is not None, snapshot.errors
    assert entry.name == "write-my-agent-plugin"
    assert entry.source == "builtin"
    assert entry.category == "plugins"
    assert "插件" in entry.description
    assert "用户" in entry.when_to_use and "my-agent 插件" in entry.when_to_use
    assert snapshot.read_body(entry.stable_id) == (SKILL / "SKILL.md").read_text(encoding="utf-8")


def test_skill_only_requires_authoring_tools():
    card = parse_skill_file(SKILL / "SKILL.md", source="builtin", require_frontmatter=True)
    assert card.tools_required
    assert set(card.tools_required) <= {"skill_search", "list_files", "read_file", "write_file",
                                        "edit_file", "apply_patch", "run_command"}
    body = card.path.read_text(encoding="utf-8")
    assert "请用 /plugins install <路径> 安装，启用时按界面提示输确认码" in body
    assert "不能替用户输确认码" in body
    assert "只能等于或严于默认" in body


def test_skill_documents_v8_and_production_boundary():
    body = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    assert all(token in body for token in ("v8", "events", "tool_gates", "permissions.network",
        "allow_as_is", "ask", "deny", "arguments: full", "plugin_events_disabled",
        "my-agent/events", "my-agent/tool-gate", "local/main", "强制沙箱", "会话和记忆"))
    assert "不能自行安装、启用" in body and "默认 `false`" in body


# LLM: 事件字段表是给模型读的合同，写错字段名会直接造出读不到数据的插件；这里让表与 B4 的唯一白名单同源比对。
#   B4 的 points.py 尚未合入本分支时不能凭缺失判失败（那是分支差异不是错），因此先按文件存在与否决定跳过，
#   17j 合入 B4 后同一用例会自动开始严格比对。
# 函数用途: 校验 author-contract 的事件字段表与 points.py 的 _EVENT_FACT_FIELDS 逐类逐字段一致。
def test_author_contract_event_table_matches_event_point_projection():
    points_path = ROOT / "agent_py_agent/agent/plugin_events/points.py"
    if not points_path.exists():
        pytest.skip("B4 的 plugin_events/points.py 尚未合入本分支；17j 合入后本用例自动生效")

    import ast

    tree = ast.parse(points_path.read_text(encoding="utf-8"))
    expected: dict[str, tuple[str, ...]] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "_EVENT_FACT_FIELDS" for target in node.targets):
            continue
        expected = {key.value: tuple(item.value for item in value.elts)
                    for key, value in zip(node.value.keys, node.value.values)}
    assert expected, "points.py 里找不到 _EVENT_FACT_FIELDS"

    table = (SKILL / "references/author-contract.md").read_text(encoding="utf-8")
    documented: dict[str, tuple[str, ...]] = {}
    for line in table.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 3 or not cells[0].startswith("`"):
            continue
        name = cells[0].strip("` ")
        if name not in expected:
            continue
        documented[name] = tuple(part.strip("` ") for part in cells[1].split("、"))

    assert set(documented) == set(expected), f"表里缺或多：{sorted(set(expected) ^ set(documented))}"
    for event_type, fields in expected.items():
        assert documented[event_type] == fields, f"{event_type} 字段不一致：表 {documented[event_type]} vs 实现 {fields}"


# LLM: 截断标记是 B5 宿主给的结构化事实，文档和模板都得用同一个字段名，写错了门就永远看不到截断；
#   B5 的 tool_gate.py 尚未合入本分支时不能凭缺失判失败（那是分支差异不是错），因此先按文件存在与否跳过，
#   合入 B5 后同一用例会自动开始严格比对。
# 函数用途: 校验文档写的字段名与 B5 实现同源，并确认合同与两个模板都按"截断只能更严"处理。
def test_author_contract_truncation_field_matches_tool_gate_implementation():
    gate_path = ROOT / "agent_py_agent/agent/plugin_events/tool_gate.py"
    if not gate_path.exists():
        pytest.skip("B5 的 plugin_events/tool_gate.py 尚未合入本分支；合入后本用例自动生效")

    source = gate_path.read_text(encoding="utf-8")
    assert 'facts["arguments_truncated"]' in source, "B5 实现未写入 arguments_truncated；字段名已变，请先对齐文档"

    contract = (SKILL / "references/author-contract.md").read_text(encoding="utf-8")
    assert "`call.arguments_truncated`" in contract, "作者合同缺截断字段的读法"
    assert "ARGUMENTS_TRUNCATED" in contract, "作者合同缺推荐原因码"
    assert "截断只能更严" in contract, "作者合同缺'截断只能更严'的推荐做法"

    for language, path in (("python", "templates/python/src/server.py"), ("node", "templates/node/src/server.js")):
        template = (SKILL / path).read_text(encoding="utf-8")
        assert "arguments_truncated" in template, f"{language} 模板未处理截断标记"
        assert "ARGUMENTS_TRUNCATED" in template, f"{language} 模板缺截断原因码"


def test_management_runtime_is_hidden_from_model(author_workspace, monkeypatch):
    from agent_py_agent.agent.path_access_policy import PathAccessPolicy
    from agent_py_agent.agent.plugin_enable_tool import PluginEnableTool
    from agent_py_agent.agent.plugin_management import PluginManagement

    tool = PluginEnableTool(object(), object(), object(), None, "synthetic-catalog")
    service = PluginManagement.__new__(PluginManagement)
    service.context = SimpleNamespace(workspace=author_workspace, enable_allowed=True,
        path_policy=PathAccessPolicy.from_values(mode="workspace"), owner=SimpleNamespace(owner_id="local/main"))
    monkeypatch.setattr(service, "_management_tool", lambda *args: tool)
    monkeypatch.setattr(service, "_allowed", lambda name: True)
    request = SimpleNamespace(command_name=tool.model_spec.name, request_id="synthetic-request", operation_id="synthetic-operation")
    binding = SimpleNamespace(request=request, run_id="synthetic-run", attempt_id="synthetic-attempt", task_id="synthetic-task")
    prepared = service._prepare(object(), binding, {})
    assert prepared.runtime_snapshot.runtimes[0].exposure.model_visible is False


@pytest.fixture(params=["python", "node"])
def built_template(request, author_workspace):
    language = request.param
    project = author_workspace / language
    shutil.copytree(SKILL / "templates" / language, project)
    bundle = author_workspace / f"{language}.zip"
    _run([sys.executable, str(ROOT / "scripts/build_plugin_files_package.py"), "--declaration",
          str(project / "declaration.json"), "--files-root", str(project), "--output", str(bundle)], author_workspace)
    package = inspect_plugin_package(bundle.read_bytes())
    return language, project, bundle, package


def test_templates_build_and_validate(built_template):
    language, project, bundle, package = built_template
    payload = package.manifest.to_payload()
    assert bundle.is_relative_to(ROOT)
    assert project.is_relative_to(ROOT)
    assert len(package.manifest.tools) == 1
    assert package.manifest.tools[0].requested_effect == "read_only"
    assert package.manifest.default_action == "count"
    assert PluginManifest.from_payload(payload).to_payload() == payload
    assert payload["schema_version"] == "plugin_package.v8"
    interpreter, script = ("python3", "src/server.py") if language == "python" else ("node", "src/server.js")
    assert payload["entry"] == {"kind": "interpreter", "interpreter": interpreter, "command": script, "args": []}
    assert {item["path"] for item in payload["files"]} == {"declaration.json", script}
    assert payload["permissions"] == {"network": False}
    assert payload["host_api"] == []
    assert payload["events"] == [{"type": "prompt_submitted", "content": "text"},
                                 {"type": "tool_call_started", "content": "none"}]
    assert payload["tool_gates"] == [{"id": "guard-rm", "tools": ["run_command"], "effects": [], "arguments": "full"}]


def test_templates_execute_packaged_stdio(built_template, author_workspace):
    language, project, bundle, package = built_template
    unpacked = author_workspace / "unpacked"
    with ZipFile(bundle) as archive:
        archive.extractall(unpacked)
    if language == "python":
        command = [sys.executable, "-I", str(unpacked / "src/server.py")]
    else:
        node = shutil.which("node")
        if node is None:
            pytest.skip("本机无 Node；v8 打包/清单仍由独立测试覆盖")
        command = [node, str(unpacked / "src/server.js")]
    replies = [json.loads(line) for line in _run(command, author_workspace, _requests() + _hook_requests()).splitlines()]
    assert [reply["id"] for reply in replies] == [0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22]
    assert replies[0]["result"]["protocolVersion"] == "2024-11-05"
    assert replies[0]["result"]["capabilities"]["experimental"] == {
        "my-agent/events": {"versions": ["1"]}, "my-agent/tool-gate": {"versions": ["1"]}}
    tool = package.manifest.tools[0]
    assert replies[1]["result"]["tools"] == [{"name": tool.name, "description": tool.description,
                                               "inputSchema": tool.input_schema}]
    assert json.loads(replies[2]["result"]["content"][0]["text"]) == {"characters": 4}
    assert json.loads(replies[3]["result"]["content"][0]["text"]) == {"characters": 0}
    assert all(reply["result"]["isError"] is True for reply in replies[4:8])
    assert json.loads(replies[8]["result"]["content"][0]["text"]) == {"characters": 2}
    assert replies[9]["result"] == {}
    assert replies[10]["result"] == {"verdict": "ask", "reason_code": "RM_RF", "message": "要删除整个目录，先确认一次"}
    assert replies[11]["result"] == {"verdict": "allow_as_is", "reason_code": "NO_MATCH"}
    assert replies[12]["result"] == {"verdict": "deny", "reason_code": "OUT_OF_SCOPE"}
    assert replies[13]["result"] == {"verdict": "ask", "reason_code": "ARGUMENTS_UNAVAILABLE"}
    assert replies[14]["result"] == {"verdict": "ask", "reason_code": "RM_RF",
                                     "message": "要删除整个目录，先确认一次；参数还被截断了"}
    assert replies[15]["result"] == {"verdict": "ask", "reason_code": "RM_RF", "message": "要删除整个目录，先确认一次"}
    assert replies[16]["result"] == {"verdict": "ask", "reason_code": "ARGUMENTS_TRUNCATED",
                                     "message": "参数被截断，看不全命令内容，先确认一次"}
    assert replies[17]["result"] == {"verdict": "deny", "reason_code": "OUT_OF_SCOPE"}
    assert replies[18]["result"] == {"verdict": "allow_as_is", "reason_code": "NO_MATCH"}
    assert replies[19]["result"] == {"verdict": "allow_as_is", "reason_code": "NO_MATCH"}
    # 参数级 deny 示例：看到畸形参数（空字节）就拒绝；打了截断标记也不降级（deny 早返回，不参与截断合并）。
    assert replies[20]["result"] == {"verdict": "deny", "reason_code": "MALFORMED_ARGUMENTS"}
    assert replies[21]["result"] == {"verdict": "deny", "reason_code": "MALFORMED_ARGUMENTS"}


# LLM: 只发送合成提示/结构化事实与精确工具参数；危险命令只是 JSON 数据，绝不执行。
# 函数用途: 对最终包验证观察空回执、三种合法裁决、参数缺失宁严，以及截断只能更严
#   （deny 保持、ask 保留原原因码、可放行才升 ask；1/"true" 这类真值按没截断处理）。
def _hook_requests():
    call = {"call_id": "synthetic-call", "tool": "run_command", "effect": "dangerous", "actor": "main",
            "interactive": True, "args_hash": "synthetic-hash"}
    frames = [
        {"method": "my-agent/events.observe", "params": {"events": [
            {"type": "prompt_submitted", "facts": {"chars": 4}, "text": "合成提示"},
            {"type": "tool_call_started", "facts": {"tool": "run_command", "args_hash": "synthetic-hash"}}]}},
        {"method": "my-agent/tool-gate.review", "params": {"gate_id": "guard-rm",
            "call": {**call, "arguments": {"command": "rm -rf build"}}}},
        {"method": "my-agent/tool-gate.review", "params": {"gate_id": "guard-rm",
            "call": {**call, "arguments": {"command": "printf safe"}}}},
        {"method": "my-agent/tool-gate.review", "params": {"gate_id": "unknown", "call": call}},
        {"method": "my-agent/tool-gate.review", "params": {"gate_id": "guard-rm", "call": call}},
        {"method": "my-agent/tool-gate.review", "params": {"gate_id": "guard-rm",
            "call": {**call, "arguments_truncated": True, "arguments": {"command": "rm -rf " + "x" * 4000}}}},
        {"method": "my-agent/tool-gate.review", "params": {"gate_id": "guard-rm",
            "call": {**call, "arguments_truncated": False, "arguments": {"command": "rm -rf build"}}}},
        {"method": "my-agent/tool-gate.review", "params": {"gate_id": "guard-rm",
            "call": {**call, "arguments_truncated": True, "arguments": {"command": "printf safe"}}}},
        {"method": "my-agent/tool-gate.review", "params": {"gate_id": "unknown",
            "call": {**call, "arguments_truncated": True, "arguments": {"command": "rm -rf build"}}}},
        {"method": "my-agent/tool-gate.review", "params": {"gate_id": "guard-rm",
            "call": {**call, "arguments_truncated": 1, "arguments": {"command": "printf safe"}}}},
        {"method": "my-agent/tool-gate.review", "params": {"gate_id": "guard-rm",
            "call": {**call, "arguments_truncated": "true", "arguments": {"command": "printf safe"}}}},
        {"method": "my-agent/tool-gate.review", "params": {"gate_id": "guard-rm",
            "call": {**call, "arguments": {"command": "printf a\x00b"}}}},
        {"method": "my-agent/tool-gate.review", "params": {"gate_id": "guard-rm",
            "call": {**call, "arguments_truncated": True, "arguments": {"command": "printf a\x00b"}}}},
    ]
    return "".join(json.dumps({"jsonrpc": "2.0", "id": index + 10, **frame}) + "\n"
                   for index, frame in enumerate(frames))


@pytest.mark.parametrize("key,value", [("approval", "never"), ("effect", "read_only"), ("auto_approve", True)])
def test_manifest_does_not_accept_invented_approval_fields(built_template, key, value):
    payload = built_template[3].manifest.to_payload()
    payload["tools"][0][key] = value
    with pytest.raises(PluginPackageError) as error:
        PluginManifest.from_payload(payload)
    assert error.value.reason == "invalid_manifest"


@pytest.mark.parametrize("bad_part", ["tool_event_text", "full_effects"])
def test_templates_reject_illegal_subscriptions(built_template, author_workspace, bad_part):
    project = built_template[1]
    declaration = json.loads((project / "declaration.json").read_text(encoding="utf-8"))
    if bad_part == "tool_event_text":
        declaration["events"][1]["content"] = "text"
    else:
        declaration["tool_gates"][0]["effects"] = ["dangerous"]
    output = author_workspace / "invalid.zip"
    with pytest.raises(PluginPackageError) as error:
        build_files_package(declaration, project, output)
    assert error.value.reason == "invalid_manifest"
    assert not output.exists()


@pytest.mark.parametrize("only", ["events", "tool_gates"])
def test_templates_allow_subscription_only_packages(built_template, author_workspace, only):
    project = built_template[1]
    declaration = json.loads((project / "declaration.json").read_text(encoding="utf-8"))
    declaration.update(tools=[], actions=[], default_action="", panels=[])
    declaration["tool_gates" if only == "events" else "events"] = []
    (project / "declaration.json").write_text(json.dumps(declaration), encoding="utf-8")
    output = build_files_package(declaration, project, author_workspace / "subscription-only.zip")
    manifest = inspect_plugin_package(output.read_bytes()).manifest
    assert manifest.to_payload()["schema_version"] == "plugin_package.v8"
    assert manifest.tools == () and manifest.panels == () and manifest.actions == ()
    assert manifest.events or manifest.tool_gates


def test_python_builder_rejects_missing_project_layout(author_workspace):
    project = author_workspace / "empty"
    project.mkdir()
    with pytest.raises(ValueError, match="pyproject.toml.*src"):
        build_plugin_package(project, "plugin_template/declaration.json", (), author_workspace / "empty.zip")


def test_node_package_is_reproducible_and_not_overwritten(author_workspace):
    project = author_workspace / "node"
    shutil.copytree(SKILL / "templates/node", project)
    declaration = json.loads((project / "declaration.json").read_text(encoding="utf-8"))
    first = build_files_package(declaration, project, author_workspace / "first.zip")
    second = build_files_package(declaration, project, author_workspace / "second.zip")
    assert first.read_bytes() == second.read_bytes()
    with pytest.raises(FileExistsError):
        build_files_package(declaration, project, first)


@pytest.mark.parametrize("extensions,expected", [
    ({}, "plugin_package.v1"),
    ({"panels": [{"id": "panel"}]}, "plugin_package.v2"),
    ({"skills": ["guide"]}, "plugin_package.v3"),
    ({"host_api": ["read"]}, "plugin_package.v4"),
    ({"tools": [{"observation": {"target_kind": "sample"}}]}, "plugin_package.v5"),
    ({"skills": ["guide"], "host_api": ["read"],
      "tools": [{"observation_ref": {"target_kind": "sample", "param": "candidate"}}]}, "plugin_package.v5"),
])
def test_python_version_selection_remains_structural(extensions, expected):
    manifest = {"entry_module": "plugin_template", **extensions}
    schema = _manifest_schema(manifest, ["plugin_template/skills/guide/SKILL.md"])
    assert schema == expected
    if expected in {"plugin_package.v3", "plugin_package.v4", "plugin_package.v5"}:
        assert "panels" in manifest
    if expected in {"plugin_package.v4", "plugin_package.v5"}:
        assert "skills" in manifest
    if expected == "plugin_package.v5":
        assert "host_api" in manifest


def test_builtin_seed_keeps_templates_and_references(author_workspace, monkeypatch, skill_catalog_factory):
    from agent_py_agent.agent.capability import builtin_seed

    root = author_workspace / "seed"
    monkeypatch.setattr(builtin_seed, "_BUILTIN_SRC", SKILL.parent)
    builtin_seed.sync_skill_index(root / "builtin", root / "shared", root / "indexes/skills.jsonl", root / "cache/fingerprint")
    seeded = root / "builtin/write-my-agent-plugin"
    originals = [path for path in SKILL.rglob("*") if path.is_file()]
    assert all((seeded / path.relative_to(SKILL)).read_bytes() == path.read_bytes() for path in originals)
    catalog = skill_catalog_factory(root)
    entry = catalog.snapshot.resolve("builtin:write-my-agent-plugin")
    assert entry is not None and entry.when_to_use


def test_python_builder_preserves_project_license(author_workspace):
    project = author_workspace / "python"
    _legacy_python_project(project)
    for name in ("LICENSE", "NOTICE"):
        (project / name).write_bytes((ROOT / name).read_bytes() + b"\nM5 local project fixture\n")
    bundle = build_plugin_package(project, "plugin_template/declaration.json", (), author_workspace / "licensed.zip")
    package = inspect_plugin_package(bundle.read_bytes())
    inspect_plugin_wheels(package)
    with ZipFile(bundle) as archive:
        wheel_bytes = archive.read(package.manifest.entry_wheel)
    with ZipFile(__import__("io").BytesIO(wheel_bytes)) as wheel:
        for name in ("LICENSE", "NOTICE"):
            member = next(path for path in wheel.namelist() if path.endswith("/licenses/" + name))
            assert wheel.read(member) == (project / name).read_bytes()


# LLM: provenance.json 记的来源提交与 sha256 必须和磁盘上的夹具字节对得上，否则"来源可复核"就是空话；
#   这条不依赖 git，在只有夹具副本的环境（车道/CI）里也能跑，正好补上"对象库里没有提交时无法自证"的空档。
# 函数用途: 按 provenance.json 逐条核对夹具文件的 sha256。
def test_legacy_fixture_provenance_matches_bytes():
    provenance = json.loads((_LEGACY_FIXTURES / "provenance.json").read_text(encoding="utf-8"))
    checked = 0
    for source in provenance["sources"].values():
        for entry in source["files"]:
            actual = hashlib.sha256((_LEGACY_FIXTURES / entry["fixture"]).read_bytes()).hexdigest()
            assert actual == entry["sha256"], f"夹具字节与 provenance.json 不一致: {entry['fixture']}"
            checked += 1
    assert checked >= 5, "provenance.json 记录的夹具条数异常"


# LLM: 夹具是历史字节的副本，会随时间漂移（有人手改、或换了来源）。本地对象库确实有那两个提交时，
#   逐字节核对夹具与 git 对象；没有时只跳过这一条校验，不跳过依赖夹具的两条原用例。
# 函数用途: 能用 git 就核对夹具字节来源，不能就跳过这一条校验并说明原因。
def test_legacy_fixture_matches_git_object_when_available():
    def object_available(revision: str) -> bool:
        probe = subprocess.run(["git", "cat-file", "-e", revision], cwd=ROOT, capture_output=True)
        return probe.returncode == 0

    if not object_available(_LEGACY_TEMPLATE_COMMIT) or not object_available(_LEGACY_BUILD_SCRIPT_COMMIT):
        pytest.skip(f"本地对象库没有 {_LEGACY_TEMPLATE_COMMIT} / {_LEGACY_BUILD_SCRIPT_COMMIT}，跳过夹具来源校验")

    template_prefix = SKILL.relative_to(ROOT).as_posix() + "/templates/python/"
    listed = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", _LEGACY_TEMPLATE_COMMIT, "--", template_prefix], cwd=ROOT
    )
    for name in listed.decode().splitlines():
        fixture = _LEGACY_PYTHON_TEMPLATE / name.removeprefix(template_prefix)
        original = subprocess.check_output(["git", "show", f"{_LEGACY_TEMPLATE_COMMIT}:{name}"], cwd=ROOT)
        assert fixture.read_bytes() == original, f"夹具与 {_LEGACY_TEMPLATE_COMMIT} 不一致: {name}"

    script_original = subprocess.check_output(
        ["git", "show", f"{_LEGACY_BUILD_SCRIPT_COMMIT}:scripts/build_plugin_package.py"], cwd=ROOT
    )
    assert _LEGACY_BUILD_SCRIPT.read_bytes() == script_original, "构建脚本夹具与来源提交不一致"


# LLM: 保留旧 wheel 构建器的许可回归；输入基线是"历史 Git 字节"，已按原字节存成夹具
#   （见 fixtures/b9_legacy_git_bytes/README.md），这样车道容器只拉一个提交、或源码包环境根本没有
#   git 时也能跑，不再报 "Not a valid object name"。绝不让 v8 混入 wheel 路径。
# 函数用途: 把夹具里的历史 Python 工程原样铺进工作区，继续测 LICENSE/NOTICE 保留行为。
def _legacy_python_project(project: Path) -> None:
    for source in _LEGACY_PYTHON_TEMPLATE.rglob("*"):
        if not source.is_file():
            continue
        target = project / source.relative_to(_LEGACY_PYTHON_TEMPLATE)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())


# LLM: 构建输入仅来自 Git 纳管的当前源码字节，不读取未纳管运行目录；构建副本、pip 临时文件都在测试工作区。
# 函数用途: 准备完整产品 wheel 工程，不在原仓库留下 build 或 egg-info。
def _production_source(workspace: Path) -> Path:
    source = workspace / "release-source"
    inputs = subprocess.check_output(["git", "ls-files", "-z", "--", "agent_py_agent",
                                      "pyproject.toml", "setup.py", "package_boundary_policy.py",
                                      "README.md", "LICENSE", "NOTICE"], cwd=ROOT)
    for name in inputs.decode().split("\0"):
        if not name or name.startswith("agent_py_agent/tests/"):
            continue
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    return source


def test_production_wheel_keeps_all_author_assets(author_workspace):
    from scripts.check_clean_package import check_zip_findings
    from scripts.check_distribution_boundary import (
        forbidden_members,
        missing_runtime_resource_members,
        source_mismatched_members,
    )

    source = _production_source(author_workspace)
    wheel = build_wheel(source, author_workspace / "release-wheels")
    assert not forbidden_members(wheel)
    assert not missing_runtime_resource_members(wheel, source)
    assert not source_mismatched_members(wheel, source)
    assert not check_zip_findings(wheel)
    with ZipFile(wheel) as archive:
        originals = [path for path in SKILL.rglob("*") if path.is_file()]
        assert all(archive.read(path.relative_to(ROOT).as_posix()) == path.read_bytes() for path in originals)


# LLM: 只从夹具读取原构建器（来源提交 b453f8883，字节见夹具 README），在同一工作树源码/解释器/
#   依赖下比较最终包字节；不改分支或还原产品文件，也不再依赖本地对象库有那个提交。
# 函数用途: 加载原构建函数作为对照，不启动插件、不安装或修改宿主状态。
def _baseline_builder():
    source = _LEGACY_BUILD_SCRIPT.read_bytes()
    namespace = {"__file__": str(ROOT / "scripts/build_plugin_package.py"), "__name__": "m5_baseline_builder"}
    exec(compile(source, namespace["__file__"], "exec"), namespace)
    return namespace["build_plugin_package"]


def test_existing_packages_match_baseline_bytes(author_workspace):
    from scripts.build_plugin_api import build_plugin_api

    baseline = _baseline_builder()
    sdk = build_plugin_api(author_workspace / "sdk")
    names = ("activity-line", "context-inspector", "browser-lite", "design-lite", "desktop-lite",
             "drama-media-shell", "genui-lite", "harness-console", "image-text")
    for name in names:
        project = ROOT / "plugins" / name
        metadata = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
        dependencies = (sdk,) if metadata["project"].get("dependencies") else ()
        declaration = next((project / "src").glob("*/declaration.json")).relative_to(project / "src").as_posix()
        before = baseline(project, declaration, dependencies, author_workspace / (name + "-before.zip"))
        after = build_plugin_package(project, declaration, dependencies, author_workspace / (name + "-after.zip"))
        assert before.read_bytes() == after.read_bytes(), name
