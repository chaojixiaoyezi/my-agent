# 模块用途: PP1118 Round-2 Slice 4 验收测试（发行宿主 wheel 内置 browser-lite + SDK 完整来源核验与跨调用会话零明文缓存）。
"""验证独立 wheel 安装宿主下的内置 browser-lite 权威清单、全量可执行成员校验及跨调用会话元数据缓存边界。"""

from __future__ import annotations

import base64
import csv
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import venv
import zipfile
from pathlib import Path
from typing import Any

import pytest

from agent_py_agent.agent.conversation.tool_approval_scope import tool_approval_scope
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.plugin_runtime import (
    _credential_variants_from_values,
    _verify_builtin_browser_lite_wheel,
    plugin_tool_name,
)
from agent_py_agent.agent.plugin_wheels import inspect_plugin_wheels
from agent_py_agent.agent.tooling import host_output_redaction
from agent_py_agent.tests.test_background_tool_approval import background as _background_fixture
from agent_py_agent.tests.test_browser_lite_package import gone, needs_browser
from agent_py_agent.tests.test_fc3_card_browser_http import (
    PROTECTED_RECORD_MARKER,
    _all_secret_variants,
    _HostCredentialUseAdapter,
    _install_and_enable_browser_lite,
    _invocation_context,
    _LoopbackProtectedSite,
    _setup_im_card,
)
from scripts.build_plugin_api import ROOT, build_plugin_api
from scripts.build_plugin_package import _ZIP_TIME, build_plugin_package
from scripts.check_distribution_boundary import (
    forbidden_members,
    missing_runtime_resource_members,
    source_mismatched_members,
    source_missing_members,
)

background = _background_fixture

SYNTHETIC_CASE_USER = "portal_operator_r2"
SYNTHETIC_CASE_SECRET = "Synthetic#R2+Secret/88=Ω"


def _build_wheel_offline(source_tree: Path, wheel_dir: Path) -> Path:
    wheel_dir.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("PIP_")}
    env.update(
        PIP_CONFIG_FILE=os.devnull,
        PIP_DISABLE_PIP_VERSION_CHECK="1",
        PYTHONDONTWRITEBYTECODE="1",
        SOURCE_DATE_EPOCH="315532800",
    )
    if importlib.util.find_spec("pip") is not None:
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-index",
            "--no-deps",
            "--no-build-isolation",
            "--no-cache-dir",
            "--wheel-dir",
            str(wheel_dir),
            str(source_tree),
        ]
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=180)
    else:
        cmd = [
            sys.executable,
            "-c",
            "import sys, setuptools.build_meta as bm; bm.build_wheel(sys.argv[1])",
            str(wheel_dir.resolve()),
        ]
        proc = subprocess.run(cmd, cwd=source_tree, env=env, capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    wheels = list(wheel_dir.glob("*.whl"))
    assert len(wheels) == 1
    return wheels[0]


@pytest.fixture(scope="module")
def official_browser_bundle(tmp_path_factory) -> Path:
    import scripts.build_plugin_api as api_mod
    import scripts.build_plugin_package as pkg_mod
    import scripts.plugin_build as pb_mod

    build_root = tmp_path_factory.mktemp("r2-official-browser-bundle")
    if importlib.util.find_spec("pip") is not None:
        sdk_wheel = build_plugin_api(build_root / "sdk")
        return build_plugin_package(
            ROOT / "plugins" / "browser-lite",
            "browser_lite/declaration.json",
            (sdk_wheel,),
            build_root / "browser-lite.zip",
        )
    orig_pb, orig_api, orig_pkg = pb_mod.build_wheel, api_mod.build_wheel, pkg_mod.build_wheel
    pb_mod.build_wheel = api_mod.build_wheel = pkg_mod.build_wheel = _build_wheel_offline
    try:
        sdk_wheel = build_plugin_api(build_root / "sdk")
        return build_plugin_package(
            ROOT / "plugins" / "browser-lite",
            "browser_lite/declaration.json",
            (sdk_wheel,),
            build_root / "browser-lite.zip",
        )
    finally:
        pb_mod.build_wheel, api_mod.build_wheel, pkg_mod.build_wheel = orig_pb, orig_api, orig_pkg


def _stage_repo_tree(dest: Path) -> Path:
    """将仓库受控文件复制到临时构建目录，确保测试构建 wheel 绝不向工作树写入临时文件。"""
    listed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    rel_paths = {os.fsdecode(raw) for raw in listed.stdout.split(b"\0") if raw}
    rel_paths.add("agent_py_agent/config/builtin_browser_lite_manifest.json")
    for rel in sorted(rel_paths):
        src = ROOT / rel
        if not src.exists() and not src.is_symlink():
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if src.is_symlink():
            target.symlink_to(os.readlink(src))
        elif src.is_file():
            target.write_bytes(src.read_bytes())
    return dest


def _build_host_wheel_from_tree(source_tree: Path, wheel_dir: Path) -> Path:
    wheel = _build_wheel_offline(source_tree, wheel_dir)
    assert wheel.name.startswith("my_agent-")
    return wheel


@pytest.fixture(scope="module")
def built_host_wheel(tmp_path_factory) -> Path:
    work = tmp_path_factory.mktemp("r2-host-wheel-build")
    staged = _stage_repo_tree(work / "source")
    return _build_host_wheel_from_tree(staged, work / "dist")


def _record_digest_pair(data: bytes) -> tuple[str, str]:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
    return f"sha256={encoded}", str(len(data))


def _rebuild_wheel_with_valid_record(
    original_wheel_bytes: bytes,
    *,
    replace_files: dict[str, bytes] | None = None,
    extra_files: dict[str, bytes] | None = None,
) -> bytes:
    """构造带合法 RECORD 摘要与尺寸的衍生测试 wheel，使通用包/wheel 预检可通过，专门检验来源门。"""
    replacements = dict(replace_files or {})
    additions = dict(extra_files or {})
    entries: list[tuple[str, bytes]] = []
    record_path = ""
    with zipfile.ZipFile(io.BytesIO(original_wheel_bytes), "r") as src:
        for info in src.infolist():
            if info.is_dir():
                continue
            name = info.filename
            if name.endswith(".dist-info/RECORD"):
                record_path = name
                continue
            body = replacements.get(name, src.read(name))
            entries.append((name, body))
    for name, body in sorted(additions.items()):
        entries.append((name, body))
    assert record_path, "missing dist-info RECORD"

    rec_buf = io.StringIO()
    writer = csv.writer(rec_buf, lineterminator="\n")
    for name, body in entries:
        digest, length = _record_digest_pair(body)
        writer.writerow([name, digest, length])
    writer.writerow([record_path, "", ""])
    entries.append((record_path, rec_buf.getvalue().encode("utf-8")))

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as dst:
        for name, body in entries:
            zi = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.create_system = 3
            zi.external_attr = (0o100644) << 16
            dst.writestr(zi, body)
    return out.getvalue()


def _repack_bundle_wheels(
    base_bundle: Path,
    *,
    transform_wheels: dict[str, bytes] | None = None,
    extra_wheels: dict[str, bytes] | None = None,
    update_requires_dist: tuple[str, str] | None = None,
) -> bytes:
    """重写外层插件包内的指定 wheel 并同步更新 plugin.json 的 wheels SHA-256 清单。"""
    replacements = dict(transform_wheels or {})
    extras = dict(extra_wheels or {})
    wheel_map: dict[str, bytes] = {}
    with zipfile.ZipFile(base_bundle, "r") as outer:
        manifest = json.loads(outer.read("plugin.json").decode("utf-8"))
        for item in outer.infolist():
            if item.filename == "plugin.json":
                continue
            wheel_map[item.filename] = replacements.get(item.filename, outer.read(item.filename))
    for path, content in sorted(extras.items()):
        wheel_map[path] = content
    if update_requires_dist is not None:
        wheel_path, req_line = update_requires_dist
        with zipfile.ZipFile(io.BytesIO(wheel_map[wheel_path]), "r") as whl:
            meta_name = next(n for n in whl.namelist() if n.endswith(".dist-info/METADATA"))
            meta_text = whl.read(meta_name).decode("utf-8")
        head, sep, tail = meta_text.partition("\n\n")
        updated_meta = f"{head}\nRequires-Dist: {req_line}{sep}{tail}".encode()
        wheel_map[wheel_path] = _rebuild_wheel_with_valid_record(
            wheel_map[wheel_path],
            replace_files={meta_name: updated_meta},
        )
    manifest["wheels"] = [
        {"path": path, "sha256": hashlib.sha256(content).hexdigest()}
        for path, content in wheel_map.items()
    ]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as dst:
        pinfo = zipfile.ZipInfo("plugin.json", date_time=_ZIP_TIME)
        pinfo.compress_type = zipfile.ZIP_DEFLATED
        pinfo.create_system = 3
        pinfo.external_attr = (0o100644) << 16
        dst.writestr(pinfo, json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        for path, content in wheel_map.items():
            winfo = zipfile.ZipInfo(path, date_time=_ZIP_TIME)
            winfo.compress_type = zipfile.ZIP_DEFLATED
            winfo.create_system = 3
            winfo.external_attr = (0o100644) << 16
            dst.writestr(winfo, content)
    raw = buf.getvalue()
    pkg = inspect_plugin_package(raw)
    inspect_plugin_wheels(pkg)
    return raw


def _make_minimal_extra_wheel(dist_name: str = "helper_mod", version: str = "1.0.0") -> bytes:
    dist_info = f"{dist_name}-{version}.dist-info"
    files = {
        f"{dist_name}/__init__.py": b"MARKER = 1\n",
        f"{dist_info}/METADATA": (
            f"Metadata-Version: 2.1\nName: {dist_name}\nVersion: {version}\n\n"
        ).encode(),
        f"{dist_info}/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
        f"{dist_info}/top_level.txt": f"{dist_name}\n".encode(),
    }
    rec_buf = io.StringIO()
    writer = csv.writer(rec_buf, lineterminator="\n")
    for name, body in sorted(files.items()):
        digest, length = _record_digest_pair(body)
        writer.writerow([name, digest, length])
    writer.writerow([f"{dist_info}/RECORD", "", ""])
    files[f"{dist_info}/RECORD"] = rec_buf.getvalue().encode("utf-8")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for name, body in sorted(files.items()):
            zi = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.create_system = 3
            zi.external_attr = (0o100644) << 16
            zf.writestr(zi, body)
    return out.getvalue()


def _object_contains_any_secret(root_obj: Any, needles: tuple[str, ...]) -> list[str]:
    """遍历对象/字典/序列图，返回任何命中的敏感字符串或其编码变体。"""
    seen: set[int] = set()
    hits: set[str] = set()

    def walk(item: Any, depth: int) -> None:
        if depth > 12 or item is None or isinstance(item, (bool, int, float)):
            return
        if isinstance(item, str):
            for token in needles:
                if token and token in item:
                    hits.add(token)
            return
        if isinstance(item, (bytes, bytearray)):
            decoded = bytes(item).decode("utf-8", errors="ignore")
            for token in needles:
                if token and token in decoded:
                    hits.add(token)
            return
        obj_id = id(item)
        if obj_id in seen:
            return
        seen.add(obj_id)
        if isinstance(item, dict):
            for k, v in list(item.items()):
                walk(k, depth + 1)
                walk(v, depth + 1)
        elif isinstance(item, (list, tuple, set, frozenset)):
            for sub in list(item):
                walk(sub, depth + 1)

    walk(root_obj, 0)
    return sorted(hits)


def test_host_wheel_build_packages_deterministic_browser_manifest_and_passes_boundary(
    built_host_wheel,
    tmp_path,
):
    assert forbidden_members(built_host_wheel) == []
    assert source_missing_members(built_host_wheel, ROOT) == []
    assert source_mismatched_members(built_host_wheel, ROOT) == []
    assert missing_runtime_resource_members(built_host_wheel, ROOT) == []

    with zipfile.ZipFile(built_host_wheel, "r") as whl:
        names = set(whl.namelist())
        assert "agent_py_agent/config/builtin_browser_lite_manifest.json" in names
        assert not any(name.startswith("plugins/") for name in names)
        wheel_manifest_bytes = whl.read("agent_py_agent/config/builtin_browser_lite_manifest.json")

    repo_manifest_bytes = (ROOT / "agent_py_agent/config/builtin_browser_lite_manifest.json").read_bytes()
    assert wheel_manifest_bytes == repo_manifest_bytes
    doc = json.loads(wheel_manifest_bytes.decode("utf-8"))
    assert doc["schema_version"] == "my-agent.builtin-browser-lite-manifest.v1"
    assert doc["entry_distribution"] == "my-agent-browser-lite"
    assert doc["sdk_distribution"] == "my-agent-plugin-api"
    assert "browser_lite/server.py" in doc["entry_members"]
    assert "browser_lite/declaration.json" in doc["entry_members"]
    assert "my_agent_plugin_api/workspace_read_context.py" in doc["sdk_members"]

    # 验证当仓库源码更新或随包 JSON 过期时，setup.py build_py 会确定性刷新清单
    staged = _stage_repo_tree(tmp_path / "refresh_tree")
    stale_path = staged / "agent_py_agent/config/builtin_browser_lite_manifest.json"
    stale_path.write_text('{"stale": true}\n', encoding="utf-8")
    refreshed_wheel = _build_host_wheel_from_tree(staged, tmp_path / "refresh_dist")
    assert stale_path.read_bytes() == repo_manifest_bytes
    with zipfile.ZipFile(refreshed_wheel, "r") as whl:
        assert whl.read("agent_py_agent/config/builtin_browser_lite_manifest.json") == repo_manifest_bytes


def test_clean_wheel_installed_host_without_repo_sources_verifies_official_and_rejects_missing_or_corrupt_manifest(
    built_host_wheel,
    official_browser_bundle,
    tmp_path,
):
    clean_site = tmp_path / "standalone_site"
    with zipfile.ZipFile(built_host_wheel, "r") as whl:
        whl.extractall(clean_site)

    assert not (clean_site / "plugins").exists()
    assert not (clean_site / "packages").exists()
    pkg_root = clean_site / "agent_py_agent"
    manifest_file = pkg_root / "config" / "builtin_browser_lite_manifest.json"
    assert manifest_file.is_file()

    bundle_bytes = official_browser_bundle.read_bytes()
    entry_wheel = inspect_plugin_package(bundle_bytes).manifest.entry_wheel

    # 1) 在完全隔离的子进程中仅从解压后的 host wheel 导入 agent_py_agent，确认无邻近仓库目录时官方包通过
    probe_code = """
import json, sys
from pathlib import Path

site_dir = Path(sys.argv[1]).resolve()
bundle_path = Path(sys.argv[2]).resolve()
entry_wheel = sys.argv[3]
sys.path[:] = [str(site_dir), *[p for p in sys.path if "my-agent" not in p or "site-packages" in p]]

import agent_py_agent.agent.plugin_runtime as pr
mod_path = Path(pr.__file__).resolve()
assert mod_path.is_relative_to(site_dir), f"unexpected import path: {mod_path}"
assert not (site_dir / "plugins").exists()
assert not (site_dir / "packages").exists()

raw = bundle_path.read_bytes()
ok_official = pr._verify_builtin_browser_lite_wheel(raw, entry_wheel)
print(json.dumps({"import_path": str(mod_path), "ok_official": ok_official}))
"""
    proc = subprocess.run(
        [sys.executable, "-I", "-c", probe_code, str(clean_site), str(official_browser_bundle), entry_wheel],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    probe_out = json.loads(proc.stdout.strip())
    assert probe_out["ok_official"] is True

    # 2) 同一无源码安装布局下：随包清单损坏、摘要不匹配或缺失均立即失败关闭
    original_manifest = manifest_file.read_bytes()
    corrupted_doc = json.loads(original_manifest.decode("utf-8"))
    corrupted_doc["sdk_members"]["my_agent_plugin_api/workspace_read_context.py"] = "0" * 64
    manifest_file.write_text(json.dumps(corrupted_doc), encoding="utf-8")
    assert _verify_builtin_browser_lite_wheel(bundle_bytes, entry_wheel, _package_root=pkg_root) is False

    manifest_file.write_bytes(b"{not-valid-json")
    assert _verify_builtin_browser_lite_wheel(bundle_bytes, entry_wheel, _package_root=pkg_root) is False

    manifest_file.unlink()
    assert _verify_builtin_browser_lite_wheel(bundle_bytes, entry_wheel, _package_root=pkg_root) is False

    # 恢复正确宿主清单后再次通过
    manifest_file.write_bytes(original_manifest)
    assert _verify_builtin_browser_lite_wheel(bundle_bytes, entry_wheel, _package_root=pkg_root) is True

    # 3) 仓库源码存在但随包清单摘要不一致时，同样要求严格一致并失败关闭
    repo_sim = tmp_path / "repo_sim"
    (repo_sim / "plugins/browser-lite/src").mkdir(parents=True)
    for src_file in (ROOT / "plugins/browser-lite/src/browser_lite").iterdir():
        if src_file.is_file():
            dst_file = repo_sim / "plugins/browser-lite/src/browser_lite" / src_file.name
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            dst_file.write_bytes(src_file.read_bytes())
    sim_pkg = repo_sim / "agent_py_agent"
    for rel in (
        "agent/common/nofollow_fs.py",
        "agent/path_access_policy.py",
        "agent/workspace_read_context.py",
        "agent/workspace_write_context.py",
    ):
        target = sim_pkg / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / "agent_py_agent" / rel).read_bytes())
    sim_manifest = sim_pkg / "config" / "builtin_browser_lite_manifest.json"
    sim_manifest.parent.mkdir(parents=True, exist_ok=True)
    sim_manifest.write_text(json.dumps(corrupted_doc), encoding="utf-8")
    assert _verify_builtin_browser_lite_wheel(bundle_bytes, entry_wheel, _package_root=sim_pkg) is False


def test_pth_sdk_extra_module_duplicate_and_extra_wheel_provenance_matrix(
    official_browser_bundle,
    tmp_path,
):
    base_pkg = inspect_plugin_package(official_browser_bundle.read_bytes())
    entry_wheel = base_pkg.manifest.entry_wheel
    sdk_wheel = next(w.path for w in base_pkg.manifest.wheels if w.path != entry_wheel)
    with zipfile.ZipFile(official_browser_bundle, "r") as outer:
        orig_entry_bytes = outer.read(entry_wheel)
        orig_sdk_bytes = outer.read(sdk_wheel)

    # (a) 构造带根级 .pth 自动启动钩子的合法 wheel：先在独立 venv 证明 python -I 启动确实会触发 .pth
    pth_wheel_bytes = _rebuild_wheel_with_valid_record(
        orig_entry_bytes,
        extra_files={
            "auto_startup_marker.pth": (
                b"import os; p = os.environ.get('R2_PTH_MARKER_FILE'); "
                b"open(p, 'w').write('ran') if p else None\n"
            ),
        },
    )
    has_host_pip = importlib.util.find_spec("pip") is not None
    venv_dir = tmp_path / "pth_iso_venv"
    venv.EnvBuilder(with_pip=not has_host_pip, symlinks=True).create(venv_dir)
    venv_py = venv_dir / "bin" / "python"
    pth_whl_file = tmp_path / Path(entry_wheel).name
    pth_whl_file.write_bytes(pth_wheel_bytes)
    pip_cmd = (
        [
            sys.executable,
            "-m",
            "pip",
            "--isolated",
            "--python",
            str(venv_py),
            "install",
            "--no-index",
            "--no-deps",
            "--no-cache-dir",
            str(pth_whl_file),
        ]
        if has_host_pip
        else [
            str(venv_py),
            "-m",
            "pip",
            "--isolated",
            "install",
            "--no-index",
            "--no-deps",
            "--no-cache-dir",
            str(pth_whl_file),
        ]
    )
    pip_res = subprocess.run(pip_cmd, capture_output=True, text=True, timeout=60)
    assert pip_res.returncode == 0, pip_res.stdout + pip_res.stderr
    marker_file = tmp_path / "pth_marker.txt"
    iso_env = {**os.environ, "R2_PTH_MARKER_FILE": str(marker_file)}
    subprocess.run([str(venv_py), "-I", "-c", "pass"], env=iso_env, check=True, timeout=15)
    assert marker_file.read_text(encoding="utf-8") == "ran"

    pth_bundle_bytes = _repack_bundle_wheels(
        official_browser_bundle,
        transform_wheels={entry_wheel: pth_wheel_bytes},
    )
    dist_info_pth_bundle_bytes = _repack_bundle_wheels(
        official_browser_bundle,
        transform_wheels={
            entry_wheel: _rebuild_wheel_with_valid_record(
                orig_entry_bytes,
                extra_files={"my_agent_browser_lite-0.1.0.dist-info/hook.pth": b"import sys\n"},
            )
        },
    )

    with zipfile.ZipFile(io.BytesIO(orig_sdk_bytes), "r") as s_zip:
        orig_sdk_mod = s_zip.read("my_agent_plugin_api/workspace_read_context.py")
    modified_sdk_bundle_bytes = _repack_bundle_wheels(
        official_browser_bundle,
        transform_wheels={
            sdk_wheel: _rebuild_wheel_with_valid_record(
                orig_sdk_bytes,
                replace_files={
                    "my_agent_plugin_api/workspace_read_context.py": orig_sdk_mod + b"\n# custom sdk delta\n",
                },
            )
        },
    )
    extra_top_mod_bundle = _repack_bundle_wheels(
        official_browser_bundle,
        transform_wheels={
            entry_wheel: _rebuild_wheel_with_valid_record(
                orig_entry_bytes,
                extra_files={"extra_top_module.py": b"VALUE = 1\n"},
            )
        },
    )
    extra_pkg_mod_bundle = _repack_bundle_wheels(
        official_browser_bundle,
        transform_wheels={
            entry_wheel: _rebuild_wheel_with_valid_record(
                orig_entry_bytes,
                extra_files={"browser_lite/extra_submodule.py": b"VALUE = 2\n"},
            )
        },
    )
    extra_dep_wheel_bundle = _repack_bundle_wheels(
        official_browser_bundle,
        extra_wheels={"wheels/helper_mod-1.0.0-py3-none-any.whl": _make_minimal_extra_wheel()},
        update_requires_dist=(entry_wheel, "helper_mod==1.0.0"),
    )

    dup_wbuf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(orig_entry_bytes), "r") as src_w, zipfile.ZipFile(dup_wbuf, "w") as dst_w:
        for item in src_w.infolist():
            dst_w.writestr(item, src_w.read(item.filename))
        with pytest.warns(UserWarning, match="Duplicate"):
            dst_w.writestr("browser_lite/server.py", b"# duplicate entry\n")
    dup_outer_buf = io.BytesIO()
    with zipfile.ZipFile(official_browser_bundle, "r") as src_o, zipfile.ZipFile(dup_outer_buf, "w") as dst_o:
        dup_bytes = dup_wbuf.getvalue()
        manifest = json.loads(src_o.read("plugin.json").decode("utf-8"))
        for w in manifest["wheels"]:
            if w["path"] == entry_wheel:
                w["sha256"] = hashlib.sha256(dup_bytes).hexdigest()
        dst_o.writestr("plugin.json", json.dumps(manifest).encode("utf-8"))
        for item in src_o.infolist():
            if item.filename == "plugin.json":
                continue
            dst_o.writestr(
                item.filename,
                dup_bytes if item.filename == entry_wheel else src_o.read(item.filename),
            )
    duplicate_entry_bundle = dup_outer_buf.getvalue()

    for label, candidate_bytes in (
        ("root_pth", pth_bundle_bytes),
        ("dist_info_pth", dist_info_pth_bundle_bytes),
        ("modified_sdk", modified_sdk_bundle_bytes),
        ("extra_top_mod", extra_top_mod_bundle),
        ("extra_pkg_mod", extra_pkg_mod_bundle),
        ("extra_dep_wheel", extra_dep_wheel_bundle),
        ("duplicate_zip_entry", duplicate_entry_bundle),
    ):
        assert _verify_builtin_browser_lite_wheel(candidate_bytes, entry_wheel) is False, label


@pytest.mark.parametrize("mutation_kind", ["pth_hook", "modified_sdk", "extra_module", "standalone_manifest_gate"])
def test_end_to_end_fill_rejects_unapproved_bundle_or_missing_manifest_with_zero_decrypt_and_zero_mcp_frames(
    mutation_kind,
    background,
    built_host_wheel,
    official_browser_bundle,
    tmp_path,
    monkeypatch,
):
    site = _LoopbackProtectedSite()
    try:
        base_pkg = inspect_plugin_package(official_browser_bundle.read_bytes())
        entry_wheel = base_pkg.manifest.entry_wheel
        sdk_wheel = next(w.path for w in base_pkg.manifest.wheels if w.path != entry_wheel)
        with zipfile.ZipFile(official_browser_bundle, "r") as outer:
            orig_entry_bytes = outer.read(entry_wheel)
            orig_sdk_bytes = outer.read(sdk_wheel)

        if mutation_kind == "pth_hook":
            bundle_raw = _repack_bundle_wheels(
                official_browser_bundle,
                transform_wheels={
                    entry_wheel: _rebuild_wheel_with_valid_record(
                        orig_entry_bytes,
                        extra_files={"auto_startup_marker.pth": b"import sys\n"},
                    )
                },
            )
            bundle_file = tmp_path / "candidate.zip"
            bundle_file.write_bytes(bundle_raw)
        elif mutation_kind == "modified_sdk":
            with zipfile.ZipFile(io.BytesIO(orig_sdk_bytes), "r") as s_zip:
                orig_sdk_mod = s_zip.read("my_agent_plugin_api/workspace_read_context.py")
            bundle_raw = _repack_bundle_wheels(
                official_browser_bundle,
                transform_wheels={
                    sdk_wheel: _rebuild_wheel_with_valid_record(
                        orig_sdk_bytes,
                        replace_files={
                            "my_agent_plugin_api/workspace_read_context.py": orig_sdk_mod + b"\n# custom sdk delta\n",
                        },
                    )
                },
            )
            bundle_file = tmp_path / "candidate.zip"
            bundle_file.write_bytes(bundle_raw)
        elif mutation_kind == "extra_module":
            bundle_raw = _repack_bundle_wheels(
                official_browser_bundle,
                transform_wheels={
                    entry_wheel: _rebuild_wheel_with_valid_record(
                        orig_entry_bytes,
                        extra_files={"extra_top_module.py": b"VALUE = 1\n"},
                    )
                },
            )
            bundle_file = tmp_path / "candidate.zip"
            bundle_file.write_bytes(bundle_raw)
        else:
            bundle_file = official_browser_bundle

        ref, _card_path, _submitted = _setup_im_card(
            background,
            site.origin + "/signin",
            authentication="collected",
        )
        scope = tool_approval_scope(background.agent, background.task_id)
        case_dir = tmp_path / "install_case"
        case_dir.mkdir(parents=True, exist_ok=True)
        _svc, owner, workspace, registry, client, _repo = _install_and_enable_browser_lite(
            background, case_dir, bundle_file,
        )
        try:
            cred_use = _HostCredentialUseAdapter(
                background.agent,
                owner_id=owner.owner_id,
                actor_id="ou_synthetic",
                thread_id=scope.thread_id,
            )
            snapshot = registry.runtime_snapshot(run_id=background.task_id)
            ctx = _invocation_context(snapshot, workspace, credential_use=cred_use)
            fill_tool = registry.tools[plugin_tool_name("browser-lite", "fill")]

            mcp_frames: list[tuple[str, Any]] = []
            orig_request = fill_tool.transport.request

            def spy_request(method: str, req_params: Any = None, **kwargs: Any):
                mcp_frames.append((method, req_params))
                return orig_request(method, req_params, **kwargs)

            monkeypatch.setattr(fill_tool.transport, "request", spy_request)

            if mutation_kind != "standalone_manifest_gate":
                outcome = fill_tool.execute_scoped(
                    {"selector": "#password", "credential_ref": ref, "field": "secret"},
                    ctx,
                )
                assert not outcome.ok, mutation_kind
                assert outcome.error_code == "CREDENTIAL_NOT_AUTHORIZED", mutation_kind
                assert outcome.effect_outcome == "not_started", mutation_kind
                assert cred_use.decrypt_count == 0, mutation_kind
                assert mcp_frames == [], mutation_kind
                assert len(client._credential_session_bindings) == 0
                return

            clean_site = tmp_path / "host_wheel_layout"
            with zipfile.ZipFile(built_host_wheel, "r") as whl:
                whl.extractall(clean_site)
            standalone_pkg_root = clean_site / "agent_py_agent"
            standalone_manifest = standalone_pkg_root / "config" / "builtin_browser_lite_manifest.json"
            valid_manifest_bytes = standalone_manifest.read_bytes()
            monkeypatch.setattr(
                "agent_py_agent.agent.plugin_runtime._verify_builtin_browser_lite_wheel",
                lambda archive_bytes, ew: _verify_builtin_browser_lite_wheel(
                    archive_bytes, ew, _package_root=standalone_pkg_root,
                ),
            )

            # 清单损坏 -> 0 C1 解密、0 MCP 帧
            standalone_manifest.write_bytes(b'{"schema_version":"broken"}')
            corrupt_out = fill_tool.execute_scoped(
                {"selector": "#password", "credential_ref": ref, "field": "secret"},
                ctx,
            )
            assert not corrupt_out.ok and corrupt_out.error_code == "CREDENTIAL_NOT_AUTHORIZED"
            assert corrupt_out.effect_outcome == "not_started"
            assert cred_use.decrypt_count == 0
            assert mcp_frames == []

            # 清单缺失 -> 0 C1 解密、0 MCP 帧
            standalone_manifest.unlink()
            missing_out = fill_tool.execute_scoped(
                {"selector": "#password", "credential_ref": ref, "field": "secret"},
                ctx,
            )
            assert not missing_out.ok and missing_out.error_code == "CREDENTIAL_NOT_AUTHORIZED"
            assert missing_out.effect_outcome == "not_started"
            assert cred_use.decrypt_count == 0
            assert mcp_frames == []

            # 恢复官方宿主清单 -> 来源核验通过，进入远端 inspect 阶段（尚未打开页面故返回 NO_PAGE，仍未解密）
            standalone_manifest.write_bytes(valid_manifest_bytes)
            ok_verify_out = fill_tool.execute_scoped(
                {"selector": "#password", "credential_ref": ref, "field": "secret"},
                ctx,
            )
            assert len(mcp_frames) == 1
            assert mcp_frames[0][1]["_meta"]["credential_fill"]["phase"] == "inspect"
            assert ok_verify_out.reported_error_code == "NO_PAGE"
            assert cred_use.decrypt_count == 0
        finally:
            registry.close_mcp_clients()
    finally:
        site.close()


def test_cross_call_browser_session_cache_retains_zero_secrets_or_variants_across_fill_idle_close_and_turn_end(
    background,
    official_browser_bundle,
    tmp_path,
    monkeypatch,
):
    site = _LoopbackProtectedSite()
    _svc, owner, workspace, registry, client, _repo = _install_and_enable_browser_lite(
        background, tmp_path, official_browser_bundle,
    )
    try:
        ref, _card_path, submitted = _setup_im_card(
            background,
            site.origin + "/signin",
            authentication="collected",
            submit_values={"username": SYNTHETIC_CASE_USER, "secret": SYNTHETIC_CASE_SECRET},
        )
        raw_pair = (submitted["username"], submitted["secret"])
        all_needles = tuple(
            dict.fromkeys((*raw_pair, *_all_secret_variants(raw_pair), *_credential_variants_from_values(raw_pair)))
        )
        assert len(all_needles) >= 8

        scope = tool_approval_scope(background.agent, background.task_id)
        cred_use = _HostCredentialUseAdapter(
            background.agent,
            owner_id=owner.owner_id,
            actor_id="ou_synthetic",
            thread_id=scope.thread_id,
        )
        snapshot = registry.runtime_snapshot(run_id=background.task_id)
        ctx = _invocation_context(snapshot, workspace, credential_use=cred_use)

        fill_tool = registry.tools[plugin_tool_name("browser-lite", "fill")]
        read_tool = registry.tools[plugin_tool_name("browser-lite", "read")]
        close_tool = registry.tools[plugin_tool_name("browser-lite", "close")]

        echo_b64 = base64.b64encode(SYNTHETIC_CASE_SECRET.encode("utf-8")).decode("ascii").rstrip("=")
        remote_mode = {"state": "active"}

        def fake_call_tool(
            tool_name: str,
            arguments: dict[str, Any],
            *,
            transport: Any = None,
            request_meta: dict[str, Any] | None = None,
        ):
            _ = transport
            cred_meta = (request_meta or {}).get("credential_fill") or {}
            phase = cred_meta.get("phase")
            if tool_name == "fill" and phase == "inspect":
                return {
                    "isError": False,
                    "content": json.dumps(
                        {
                            "origin": site.origin,
                            "target_id": "target-r2",
                            "document_id": "doc-r2",
                            "generation": "1",
                        }
                    ),
                }
            if tool_name == "fill" and phase == "apply":
                assert cred_meta.get("field_value") == SYNTHETIC_CASE_SECRET
                return {
                    "isError": False,
                    "content": json.dumps({"filled": "input", "credential_applied": True}),
                }
            if tool_name == "read":
                if remote_mode["state"] == "idle_reaped":
                    return {
                        "isError": True,
                        "content": json.dumps({"code": "NO_PAGE", "message": "浏览器已被空闲回收。"}),
                    }
                return {
                    "isError": False,
                    "content": json.dumps({"items": [{"id": "echo", "text": f"回显:{echo_b64}"}]}),
                }
            if tool_name == "close":
                return {"isError": False, "content": json.dumps({"closed": True, "message": "已关闭"})}
            raise AssertionError(f"unexpected tool call: {tool_name} {phase}")

        monkeypatch.setattr(client, "call_tool", fake_call_tool)

        # 1) 执行授权 fill：成功后跨调用字典仅存非秘密元数据，不含任何原值或变体
        filled = fill_tool.execute_scoped(
            {"selector": "#password", "credential_ref": ref, "field": "secret"},
            ctx,
        )
        assert filled.ok, filled.output
        assert len(client._credential_session_bindings) == 1
        only_entry = next(iter(client._credential_session_bindings.values()))
        assert set(only_entry.keys()) == {"credential_ref", "generation", "origin", "consumer"}
        assert "variants" not in only_entry
        assert _object_contains_any_secret(client._credential_session_bindings, all_needles) == []
        assert _object_contains_any_secret(fill_tool.__dict__, all_needles) == []

        # 2) 后续同会话 read：当次鲜活重算局部变体完成回显净化，调用返回后缓存依然零秘密/零变体
        read_ok = read_tool.execute_scoped({}, ctx)
        assert read_ok.ok, read_ok.output
        assert "[REDACTED]" in read_ok.output
        assert _object_contains_any_secret(read_ok.output, all_needles) == []
        assert _object_contains_any_secret(client._credential_session_bindings, all_needles) == []

        # 3) 浏览器空闲超时回收（NO_PAGE）：宿主自动移除该失效会话绑定，零残留
        remote_mode["state"] = "idle_reaped"
        read_idle = read_tool.execute_scoped({}, ctx)
        assert not read_idle.ok
        assert len(client._credential_session_bindings) == 0
        assert _object_contains_any_secret(client._credential_session_bindings, all_needles) == []

        # 4) 再次授权绑定后显式 close + 结束宿主回合 + 停止客户端：全部位置零秘密残留
        remote_mode["state"] = "active"
        assert fill_tool.execute_scoped(
            {"selector": "#password", "credential_ref": ref, "field": "secret"},
            ctx,
        ).ok
        assert len(client._credential_session_bindings) == 1
        assert _object_contains_any_secret(client._credential_session_bindings, all_needles) == []

        closed = close_tool.execute_scoped({}, ctx)
        assert closed.ok
        assert len(client._credential_session_bindings) == 0

        prod_turn = cred_use.turn_redaction._delegate
        if prod_turn is not None and hasattr(prod_turn, "close"):
            prod_turn.close()
        cred_use.turn_redaction._values.clear()
        client.stop()

        assert _object_contains_any_secret(client._credential_session_bindings, all_needles) == []
        if prod_turn is not None:
            assert _object_contains_any_secret(prod_turn.__dict__, all_needles) == []
        _ = host_output_redaction
    finally:
        registry.close_mcp_clients()
        site.close()


@needs_browser
def test_real_headless_browser_fill_works_under_standalone_host_wheel_manifest_and_cleans_session_cache(
    background,
    built_host_wheel,
    official_browser_bundle,
    tmp_path,
    monkeypatch,
):
    clean_site = tmp_path / "real_browser_standalone_site"
    with zipfile.ZipFile(built_host_wheel, "r") as whl:
        whl.extractall(clean_site)
    standalone_pkg_root = clean_site / "agent_py_agent"
    monkeypatch.setattr(
        "agent_py_agent.agent.plugin_runtime._verify_builtin_browser_lite_wheel",
        lambda archive_bytes, ew: _verify_builtin_browser_lite_wheel(
            archive_bytes, ew, _package_root=standalone_pkg_root,
        ),
    )

    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    site = _LoopbackProtectedSite()
    _svc, owner, workspace, registry, client, _repo = _install_and_enable_browser_lite(
        background, run_dir, official_browser_bundle,
    )
    try:
        ref, _card_path, submitted = _setup_im_card(
            background,
            site.origin + "/signin",
            authentication="collected",
        )
        raw_pair = (submitted["username"], submitted["secret"])
        all_needles = tuple(dict.fromkeys((*raw_pair, *_all_secret_variants(raw_pair))))
        scope = tool_approval_scope(background.agent, background.task_id)
        cred_use = _HostCredentialUseAdapter(
            background.agent,
            owner_id=owner.owner_id,
            actor_id="ou_synthetic",
            thread_id=scope.thread_id,
        )
        snapshot = registry.runtime_snapshot(run_id=background.task_id)
        ctx = _invocation_context(snapshot, workspace, credential_use=cred_use)

        open_tool = registry.tools[plugin_tool_name("browser-lite", "open")]
        fill_tool = registry.tools[plugin_tool_name("browser-lite", "fill")]
        click_tool = registry.tools[plugin_tool_name("browser-lite", "click")]
        read_tool = registry.tools[plugin_tool_name("browser-lite", "read")]
        close_tool = registry.tools[plugin_tool_name("browser-lite", "close")]

        opened = open_tool.execute_scoped({"url": site.origin + "/signin"}, ctx)
        assert opened.ok, opened.output
        browser_pid = int(json.loads(json.loads(opened.output)["result"])["browser_pid"])

        assert fill_tool.execute_scoped(
            {"selector": "#username", "credential_ref": ref, "field": "username"}, ctx,
        ).ok
        assert fill_tool.execute_scoped(
            {"selector": "#password", "credential_ref": ref, "field": "secret"}, ctx,
        ).ok
        assert _object_contains_any_secret(client._credential_session_bindings, all_needles) == []

        clicked = click_tool.execute_scoped({"selector": "#submit"}, ctx)
        assert clicked.ok, clicked.output
        account_read = read_tool.execute_scoped({"selector": "#record"}, ctx)
        assert account_read.ok and PROTECTED_RECORD_MARKER in account_read.output
        assert _object_contains_any_secret(client._credential_session_bindings, all_needles) == []

        assert close_tool.execute_scoped({}, ctx).ok
        assert gone(browser_pid)
        assert len(client._credential_session_bindings) == 0
    finally:
        registry.close_mcp_clients()
        site.close()
