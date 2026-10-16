"""E1b：工具清单按内容哈希只存一份 + 快照只留摘要。

测试方法：
- 大小：用真实内置工具注册表造 run，断言快照里 tool_manifest 段与 md 对应段都小于常量阈值；
- 内容等价：按旧实现语义在内存里重建"内联完整清单"（tool_manifest_payload + tool_load_errors +
  tool_runtimes 重复份），去掉 run_id/snapshot_hash/tool_runtimes 后与解引用结果逐字段比较；
- 复用/隔离：同一用户多次请求只多 1 个文件；两个用户各自目录、互不引用；
- 篡改/缺文件：读取帮助函数报结构化错误（code 机器可判）；
- 旧格式：tool_manifest 自带 tools 的快照直接返回内联那份；
- save=False：不写任何文件（ref 留空、sha 照填），解引用报 tool_manifest_not_persisted；
- 路径信任：ref 指别的用户也读本用户；本用户归档没有就报错不读别处；归档整体搬迁后仍能读；
  清单目录被符号链接指向归档外时报 tool_manifest_outside_owner_archive；
- self_check / prompt_section：必填字段与提示段输出不变。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.contracts.tool_manifest_contract import tool_manifest_payload
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams
from agent_py_agent.agent.user_space.context_bundle import (
    MainContextBundleRequest,
    build_main_context_bundle,
)
from agent_py_agent.agent.user_space.context_bundle_tool_manifest import (
    ToolManifestLoadError,
    load_bundle_tool_manifest,
)
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.tests._tool_runtime_harness import (
    make_test_model_spec,
    runtime_snapshot_for_model_specs,
)

# 摘要只含工具名清单与引用，真实内置工具集下约几 KB；20KB 给工具数量增长留足余量，
# 同时远小于旧格式内联清单（合成回合约 878KB，实测快照约 928KB）。
TOOL_MANIFEST_SUMMARY_MAX_BYTES = 20_000


def _make_request(
    tmp_path: Path,
    home_name: str,
    request_id: str,
    *,
    snapshot: object | None = None,
) -> MainContextBundleRequest:
    """构造一个 save=True 的快照请求；root 与 home 都在 tmp_path 下按名字隔离。"""

    root = tmp_path / f"workspace-{request_id}"
    root.mkdir()
    return MainContextBundleRequest(
        root=root,
        home_paths=ensure_my_agent_home(tmp_path / home_name),
        user_prompt="E1b 工具清单测试请求",
        request_id=request_id,
        run_id=f"run-{request_id}",
        task_id=request_id,
        tool_runtime_snapshot=snapshot,
        save=True,
        created_at="2026-10-07T18:00:00+00:00",
    )


def _real_tool_snapshot(tmp_path: Path):
    """用真实内置工具注册表冻结一次工具快照（构造与可用性检查均无副作用）。"""

    registry = ToolRegistry(
        ToolRegistryParams(
            workspace_root=tmp_path,
            max_chars=1000,
            max_entries=50,
            max_matches=20,
            web_max_chars=1000,
            http_timeout=30,
            catalog_limit=20,
            retrieval_limit=10,
            vector_search_enabled=False,
        )
    )
    return registry.runtime_snapshot(run_id="run-real-tools")


def _two_tool_snapshot(run_id: str):
    return runtime_snapshot_for_model_specs(
        (
            make_test_model_spec("read_file", category="filesystem"),
            make_test_model_spec("write_file", category="filesystem"),
        ),
        run_id=run_id,
        allowed_tools=["read_file", "write_file"],
    )


def _one_tool_snapshot(run_id: str):
    return runtime_snapshot_for_model_specs(
        (make_test_model_spec("read_file", category="filesystem"),),
        run_id=run_id,
        allowed_tools=["read_file"],
    )


def _legacy_inline_manifest(request: MainContextBundleRequest) -> dict:
    """按旧实现语义重建"快照里内联的完整清单"，再去掉 run_id / snapshot_hash / tool_runtimes。

    旧实现：payload = tool_manifest_payload(snapshot)，随后 payload["tool_runtimes"] = payload["tools"]，
    payload["tool_load_errors"] = request.tool_runtime_errors。去三字段后应与解引用结果逐字段相等。
    """

    payload = tool_manifest_payload(request.tool_runtime_snapshot)
    payload["tool_runtimes"] = payload["tools"]
    payload["tool_load_errors"] = [dict(item) for item in request.tool_runtime_errors]
    for key in ("run_id", "snapshot_hash", "tool_runtimes"):
        payload.pop(key)
    return json.loads(json.dumps(payload, ensure_ascii=False))


def test_snapshot_manifest_summary_is_small_with_real_builtin_tools(tmp_path: Path) -> None:
    snapshot = _real_tool_snapshot(tmp_path)
    result = build_main_context_bundle(
        _make_request(tmp_path, "home-real", "req-real", snapshot=snapshot)
    )

    data = json.loads(Path(result.json_path).read_text(encoding="utf-8"))
    summary = data["tool_manifest"]
    summary_bytes = len(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    )
    assert summary_bytes < TOOL_MANIFEST_SUMMARY_MAX_BYTES

    md_text = Path(result.markdown_path).read_text(encoding="utf-8")
    section = md_text[md_text.index("## Tool Manifest") : md_text.index("## Acceptance Contract")]
    assert len(section.encode("utf-8")) < TOOL_MANIFEST_SUMMARY_MAX_BYTES

    full = load_bundle_tool_manifest(data, snapshot_path=result.json_path)
    assert full["tools"], "真实内置工具集应有可见工具"
    tools_bytes = len(json.dumps(full["tools"], ensure_ascii=False).encode("utf-8"))
    assert tools_bytes > summary_bytes * 10, "完整工具定义应远大于摘要（瘦身来源）"


def test_loaded_manifest_matches_legacy_inline_content(tmp_path: Path) -> None:
    request = _make_request(
        tmp_path, "home-equivalence", "req-equivalence", snapshot=_two_tool_snapshot("run-equiv")
    )
    request = replace(
        request, tool_runtime_errors=({"context": "probe", "category": "data_parse"},)
    )
    result = build_main_context_bundle(request)

    legacy = _legacy_inline_manifest(request)
    loaded = load_bundle_tool_manifest(result.bundle, snapshot_path=result.json_path)

    assert loaded == legacy
    assert loaded["tools"] == legacy["tools"]
    assert loaded["failure_taxonomy"] == legacy["failure_taxonomy"]
    assert loaded["failure_contracts"] == legacy["failure_contracts"]


def test_repeated_requests_reuse_single_manifest_file(tmp_path: Path) -> None:
    snapshot = _one_tool_snapshot("run-reuse")
    refs = []
    for index in range(3):
        result = build_main_context_bundle(
            _make_request(tmp_path, "home-reuse", f"req-reuse-{index}", snapshot=snapshot)
        )
        refs.append(result.bundle["tool_manifest"]["manifest_ref"])

    assert len(set(refs)) == 1, "同一工具集跨请求必须复用同一个清单文件"
    manifests_dir = Path(refs[0]).parent
    assert manifests_dir.name == "tool_manifests"
    assert [path.name for path in manifests_dir.glob("*.json")] == [Path(refs[0]).name]


def test_manifest_files_are_owner_isolated(tmp_path: Path) -> None:
    snapshot = _one_tool_snapshot("run-isolation")
    result_a = build_main_context_bundle(
        _make_request(tmp_path, "home-a", "req-a", snapshot=snapshot)
    )
    result_b = build_main_context_bundle(
        _make_request(tmp_path, "home-b", "req-b", snapshot=snapshot)
    )
    ref_a = Path(result_a.bundle["tool_manifest"]["manifest_ref"])
    ref_b = Path(result_b.bundle["tool_manifest"]["manifest_ref"])
    home_a = Path(result_a.bundle["workspace_refs"]["owner_home"]).resolve()
    home_b = Path(result_b.bundle["workspace_refs"]["owner_home"]).resolve()

    assert ref_a.is_file() and ref_b.is_file()
    assert ref_a.parent == home_a / "memory_archive" / "snapshots" / "tool_manifests"
    assert ref_b.parent == home_b / "memory_archive" / "snapshots" / "tool_manifests"
    assert ref_a != ref_b
    assert [path.name for path in ref_a.parent.glob("*.json")] == [ref_a.name]
    assert [path.name for path in ref_b.parent.glob("*.json")] == [ref_b.name]


def test_tampered_manifest_reports_sha256_mismatch(tmp_path: Path) -> None:
    result = build_main_context_bundle(
        _make_request(tmp_path, "home-tamper", "req-tamper", snapshot=_one_tool_snapshot("run-tamper"))
    )
    manifest_ref = Path(result.bundle["tool_manifest"]["manifest_ref"])
    content = json.loads(manifest_ref.read_text(encoding="utf-8"))
    content["visible_tools"] = list(content["visible_tools"]) + ["tampered_tool"]
    manifest_ref.write_text(
        json.dumps(content, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )

    with pytest.raises(ToolManifestLoadError) as excinfo:
        load_bundle_tool_manifest(result.bundle, snapshot_path=result.json_path)
    assert excinfo.value.code == "tool_manifest_sha256_mismatch"
    assert excinfo.value.manifest_ref == str(manifest_ref)


def test_missing_manifest_file_reports_structured_error(tmp_path: Path) -> None:
    result = build_main_context_bundle(_make_request(tmp_path, "home-missing", "req-missing"))
    Path(result.bundle["tool_manifest"]["manifest_ref"]).unlink()

    with pytest.raises(ToolManifestLoadError) as excinfo:
        load_bundle_tool_manifest(result.bundle, snapshot_path=result.json_path)
    assert excinfo.value.code == "tool_manifest_file_missing"


@pytest.mark.parametrize("version", [2, 3])
def test_legacy_inline_manifest_loads_without_error(version: int) -> None:
    legacy_manifest = {
        "schema_name": "tool_runtime_manifest",
        "schema_version": version,
        "tools": [{"name": "read_file", "visible_in_context": True}],
        "failure_taxonomy": ["WRITE_FORBIDDEN"],
    }
    bundle = {"tool_manifest": legacy_manifest}

    assert load_bundle_tool_manifest(bundle) is legacy_manifest


def test_summary_keeps_required_field_and_self_check(tmp_path: Path) -> None:
    result = build_main_context_bundle(_make_request(tmp_path, "home-selfcheck", "req-selfcheck"))

    manifest = result.bundle["tool_manifest"]
    assert isinstance(manifest, dict) and manifest
    checks = {item["name"]: item for item in result.bundle["self_check"]["checks"]}
    assert checks["required_tool_manifest"]["ok"] is True
    assert result.bundle["self_check"]["ok"] is True


def test_prompt_section_output_unchanged(tmp_path: Path) -> None:
    request = _make_request(tmp_path, "home-prompt", "req-prompt")
    result = build_main_context_bundle(request)
    home_root = result.bundle["workspace_refs"]["my_agent_home"]

    expected = "\n".join(
        [
            "# Main Agent Context Bundle v1",
            "- 这是主代理本轮运行的结构化上下文（context bundle，给模型看的任务交接包）。",
            "- 大文件、工具输出和历史正文只通过路径引用；需要正文时再显式读取。",
            "- 本轮 request/run/task 等运行标识只供 runtime 内部关联，完整值保存在 context_bundle_json；"
            "不得把它们当成用户可见的会话任务编号。",
            f"- primary_workspace_root: {Path(request.root).resolve()}",
            f"- my_agent_home: {home_root}",
            "- related_memory_count: 0",
            "- resume_context_injected: false",
            f"- context_bundle_json: {result.json_path}",
            "- self_check_ok: true",
        ]
    )
    assert result.prompt_section == expected


def test_save_false_build_writes_nothing_and_load_reports_not_persisted(tmp_path: Path) -> None:
    request = replace(_make_request(tmp_path, "home-unsaved", "req-unsaved"), save=False)
    result = build_main_context_bundle(request)

    manifest = result.bundle["tool_manifest"]
    assert manifest["manifest_ref"] == "", "不保存时 manifest_ref 必须留空"
    assert manifest["manifest_sha256"], "不保存时 sha 照填（只是没有落盘清单）"
    home_root = Path(result.bundle["workspace_refs"]["owner_home"])
    assert not (home_root / "memory_archive").exists(), "save=False 不得有任何落盘副作用"

    with pytest.raises(ToolManifestLoadError) as excinfo:
        load_bundle_tool_manifest(result.bundle)
    assert excinfo.value.code == "tool_manifest_not_persisted"


def _bundle_with_ref(result, ref: str) -> dict:
    """复制快照 bundle 并把 manifest_ref 改成指定值（模拟被篡改或随搬迁失效的绝对路径）。"""

    bundle = json.loads(json.dumps(result.bundle, ensure_ascii=False))
    bundle["tool_manifest"]["manifest_ref"] = ref
    return bundle


def test_load_ignores_ref_pointing_to_other_owner(tmp_path: Path) -> None:
    snapshot = _one_tool_snapshot("run-ref-other")
    result_a = build_main_context_bundle(
        _make_request(tmp_path, "home-ref-a", "req-ref-a", snapshot=snapshot)
    )
    result_b = build_main_context_bundle(
        _make_request(tmp_path, "home-ref-b", "req-ref-b", snapshot=snapshot)
    )
    ref_a = Path(result_a.bundle["tool_manifest"]["manifest_ref"])
    ref_b = Path(result_b.bundle["tool_manifest"]["manifest_ref"])
    assert ref_a != ref_b and ref_a.is_file() and ref_b.is_file()
    # 把 B 的清单改脏，并让 A 的 ref 指向 B：若读取信任 ref，就会读错文件或报 sha 不符。
    content_b = json.loads(ref_b.read_text(encoding="utf-8"))
    content_b["visible_tools"] = list(content_b["visible_tools"]) + ["other_owner_marker"]
    ref_b.write_text(json.dumps(content_b, ensure_ascii=False, sort_keys=True), encoding="utf-8")

    loaded = load_bundle_tool_manifest(
        _bundle_with_ref(result_a, str(ref_b)), snapshot_path=result_a.json_path
    )

    assert loaded == json.loads(ref_a.read_text(encoding="utf-8"))
    assert "other_owner_marker" not in loaded["visible_tools"]


def test_load_missing_local_manifest_does_not_fall_back_to_ref(tmp_path: Path) -> None:
    snapshot = _one_tool_snapshot("run-ref-fallback")
    result_a = build_main_context_bundle(
        _make_request(tmp_path, "home-fallback-a", "req-fallback-a", snapshot=snapshot)
    )
    result_b = build_main_context_bundle(
        _make_request(tmp_path, "home-fallback-b", "req-fallback-b", snapshot=snapshot)
    )
    ref_a = Path(result_a.bundle["tool_manifest"]["manifest_ref"])
    ref_b = Path(result_b.bundle["tool_manifest"]["manifest_ref"])
    ref_a.unlink()  # 本用户归档里没有清单；另一个用户目录里有一份同内容的
    assert ref_b.is_file()

    with pytest.raises(ToolManifestLoadError) as excinfo:
        load_bundle_tool_manifest(
            _bundle_with_ref(result_a, str(ref_b)), snapshot_path=result_a.json_path
        )
    assert excinfo.value.code == "tool_manifest_file_missing"


def test_archive_relocation_keeps_manifest_readable(tmp_path: Path) -> None:
    result = build_main_context_bundle(
        _make_request(tmp_path, "home-moved", "req-moved", snapshot=_one_tool_snapshot("run-moved"))
    )
    snapshot_path = Path(result.json_path)
    archive = snapshot_path.parents[3]  # <archive>/snapshots/context_bundles/<日期>/<req>.json
    moved = tmp_path / "relocated-archive"
    archive.rename(moved)
    assert not Path(result.bundle["tool_manifest"]["manifest_ref"]).exists(), "旧 ref 已随搬迁失效"

    moved_snapshot = moved / snapshot_path.relative_to(archive)
    loaded = load_bundle_tool_manifest(result.bundle, snapshot_path=moved_snapshot)

    sha = result.bundle["tool_manifest"]["manifest_sha256"]
    expected = json.loads(
        (moved / "snapshots" / "tool_manifests" / f"{sha}.json").read_text(encoding="utf-8")
    )
    assert loaded == expected
    assert loaded["tools"]


def test_symlinked_manifest_dir_outside_archive_is_rejected(tmp_path: Path) -> None:
    result = build_main_context_bundle(
        _make_request(tmp_path, "home-outside", "req-outside", snapshot=_one_tool_snapshot("run-outside"))
    )
    snapshot_path = Path(result.json_path)
    manifests = snapshot_path.parents[3] / "snapshots" / "tool_manifests"
    outside = tmp_path / "outside-manifests"
    manifests.rename(outside)
    manifests.symlink_to(outside, target_is_directory=True)
    assert (outside / f"{result.bundle['tool_manifest']['manifest_sha256']}.json").is_file()

    with pytest.raises(ToolManifestLoadError) as excinfo:
        load_bundle_tool_manifest(result.bundle, snapshot_path=snapshot_path)
    assert excinfo.value.code == "tool_manifest_outside_owner_archive"


def test_existing_corrupt_manifest_is_repaired_before_reference_reuse(tmp_path: Path) -> None:
    request = _make_request(tmp_path, 'home-repair', 'req-repair')
    first = build_main_context_bundle(request)
    path = Path(first.bundle['tool_manifest']['manifest_ref'])
    expected = path.read_bytes()
    path.write_bytes(b'{broken')

    rebuilt = build_main_context_bundle(request)

    assert path.read_bytes() == expected
    assert load_bundle_tool_manifest(rebuilt.bundle, snapshot_path=rebuilt.json_path)


@pytest.mark.parametrize('sha', ['../other', 'x' * 64, 'a' * 63, 'A' * 64])
def test_invalid_content_digest_is_rejected(tmp_path: Path, sha: str) -> None:
    result = build_main_context_bundle(_make_request(tmp_path, 'home-digest', 'req-digest'))
    result.bundle['tool_manifest']['manifest_sha256'] = sha

    with pytest.raises(ToolManifestLoadError) as excinfo:
        load_bundle_tool_manifest(result.bundle, snapshot_path=result.json_path)

    assert excinfo.value.code == 'tool_manifest_sha256_invalid'


def test_reuse_does_not_touch_manifest_outside_owner_archive(tmp_path: Path) -> None:
    request = _make_request(tmp_path, 'home-store-outside', 'req-store-outside')
    result = build_main_context_bundle(request)
    manifest = Path(result.bundle['tool_manifest']['manifest_ref'])
    outside = tmp_path / 'foreign-manifests'
    manifest.parent.rename(outside)
    manifest.parent.symlink_to(outside, target_is_directory=True)
    before = (outside / manifest.name).stat().st_mtime_ns

    with pytest.raises(ToolManifestLoadError) as excinfo:
        build_main_context_bundle(request)

    assert excinfo.value.code == 'tool_manifest_outside_owner_archive'
    assert (outside / manifest.name).stat().st_mtime_ns == before


def test_summary_preserves_current_permission_scope(tmp_path: Path) -> None:
    result = build_main_context_bundle(_make_request(tmp_path, 'home-scope', 'req-scope'))
    full = load_bundle_tool_manifest(result.bundle, snapshot_path=result.json_path)

    assert result.bundle['tool_manifest']['permission_scope'] == full['permission_scope']
    assert result.bundle['tool_manifest']['schema_version'] == 4
    assert full['schema_version'] == 3


def test_manifest_reuse_touch_failure_does_not_publish_snapshot(tmp_path, monkeypatch):
    from agent_py_agent.agent.user_space import context_bundle_tool_manifest as manifests

    first = _make_request(tmp_path, "home-touch", "req-old")
    old = build_main_context_bundle(first)
    manifest = Path(old.bundle["tool_manifest"]["manifest_ref"])
    before = {path: path.read_bytes() for path in manifest.parents[1].rglob("*.json")}
    fresh = _make_request(tmp_path, "home-touch", "req-new")

    def refuse_touch(path, *args, **kwargs):
        assert Path(path) == manifest
        raise OSError("isolated keepalive failure")

    monkeypatch.setattr(manifests.os, "utime", refuse_touch)
    with pytest.raises(ToolManifestLoadError, match="tool_manifest_reuse_unprotected"):
        build_main_context_bundle(fresh)
    after = {path: path.read_bytes() for path in manifest.parents[1].rglob("*.json")}
    assert after == before, "无法保留清单时不得发布新快照或替换latest引用"
    assert load_bundle_tool_manifest(old.bundle, snapshot_path=old.json_path)
