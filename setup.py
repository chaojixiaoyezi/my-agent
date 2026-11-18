from __future__ import annotations

import hashlib
import json
import re
import runpy
import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py

_ROOT = Path(__file__).resolve().parent
_POLICY = runpy.run_path(str(_ROOT / "package_boundary_policy.py"))
is_dev_only_module = _POLICY["is_dev_only_module"]
forbidden_distribution_member = _POLICY["forbidden_distribution_member"]

_MANIFEST_REL_PATH = Path("agent_py_agent/config/builtin_browser_lite_manifest.json")
_MANIFEST_SCHEMA = "my-agent.builtin-browser-lite-manifest.v1"
_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}\Z")
_SDK_SOURCE_MAP = {
    "my_agent_plugin_api/nofollow_fs.py": "agent_py_agent/agent/common/nofollow_fs.py",
    "my_agent_plugin_api/path_access_policy.py": "agent_py_agent/agent/path_access_policy.py",
    "my_agent_plugin_api/workspace_read_context.py": "agent_py_agent/agent/workspace_read_context.py",
    "my_agent_plugin_api/workspace_write_context.py": "agent_py_agent/agent/workspace_write_context.py",
}


def _should_ignore_source_entry(path: Path, base: Path) -> bool:
    rel_parts = path.relative_to(base).parts
    if any(part == "__pycache__" or part.endswith(".egg-info") for part in rel_parts):
        return True
    return path.name == ".DS_Store" or path.name.startswith("._") or path.suffix in {".pyc", ".pyo"}


def build_builtin_browser_lite_manifest_bytes(root: Path = _ROOT) -> bytes | None:
    """从仓库内置 browser-lite 与插件 SDK 权威源码生成确定性 SHA-256 清单字节。"""
    browser_src = root / "plugins" / "browser-lite" / "src" / "browser_lite"
    if not browser_src.exists() and not browser_src.is_symlink():
        return None
    if browser_src.is_symlink() or not browser_src.is_dir():
        raise RuntimeError(f"Invalid builtin browser-lite source directory: {browser_src}")
    entry_members: dict[str, str] = {}
    for child in sorted(browser_src.rglob("*")):
        if child.is_dir() and not child.is_symlink():
            continue
        if _should_ignore_source_entry(child, browser_src):
            continue
        if child.is_symlink() or not child.is_file():
            raise RuntimeError(f"Non-regular file in builtin browser-lite source tree: {child}")
        member_name = "browser_lite/" + child.relative_to(browser_src).as_posix()
        if member_name.lower().endswith(".pth"):
            raise RuntimeError(f"Forbidden .pth file in builtin browser-lite source tree: {member_name}")
        entry_members[member_name] = hashlib.sha256(child.read_bytes()).hexdigest()
    if "browser_lite/server.py" not in entry_members or "browser_lite/declaration.json" not in entry_members:
        raise RuntimeError("Builtin browser-lite source tree is missing server.py or declaration.json")

    sdk_members: dict[str, str] = {
        "my_agent_plugin_api/__init__.py": hashlib.sha256(b"").hexdigest(),
    }
    for member_name, source_rel in sorted(_SDK_SOURCE_MAP.items()):
        source_path = root / source_rel
        if source_path.is_symlink() or not source_path.is_file():
            raise RuntimeError(f"Missing canonical plugin SDK source file: {source_rel}")
        sdk_members[member_name] = hashlib.sha256(source_path.read_bytes()).hexdigest()

    manifest_doc = {
        "schema_version": _MANIFEST_SCHEMA,
        "plugin_id": "browser-lite",
        "entry_distribution": "my-agent-browser-lite",
        "sdk_distribution": "my-agent-plugin-api",
        "entry_members": dict(sorted(entry_members.items())),
        "sdk_members": dict(sorted(sdk_members.items())),
    }
    return (json.dumps(manifest_doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def validate_builtin_browser_lite_manifest_bytes(raw: bytes) -> None:
    """校验随包内置 browser-lite 权威摘要清单结构完整且无非法路径。"""
    manifest_doc = json.loads(raw.decode("utf-8"))
    expected_keys = {
        "schema_version",
        "plugin_id",
        "entry_distribution",
        "sdk_distribution",
        "entry_members",
        "sdk_members",
    }
    if (
        not isinstance(manifest_doc, dict)
        or set(manifest_doc) != expected_keys
        or manifest_doc.get("schema_version") != _MANIFEST_SCHEMA
        or manifest_doc.get("plugin_id") != "browser-lite"
        or manifest_doc.get("entry_distribution") != "my-agent-browser-lite"
        or manifest_doc.get("sdk_distribution") != "my-agent-plugin-api"
    ):
        raise RuntimeError("Invalid builtin_browser_lite_manifest.json header metadata")
    for key, prefix, required in (
        ("entry_members", "browser_lite/", ("browser_lite/server.py", "browser_lite/declaration.json")),
        ("sdk_members", "my_agent_plugin_api/", ("my_agent_plugin_api/__init__.py",)),
    ):
        mapping = manifest_doc.get(key)
        if not isinstance(mapping, dict) or not mapping:
            raise RuntimeError(f"Invalid builtin_browser_lite_manifest.json {key}")
        for name, digest in mapping.items():
            if (
                not isinstance(name, str)
                or not name.startswith(prefix)
                or name.endswith("/")
                or name.lower().endswith(".pth")
                or any(part in {"", ".", ".."} for part in name.split("/"))
                or any(ch in name for ch in ("\\", ":", "\x00"))
                or not isinstance(digest, str)
                or not _SHA256_HEX_RE.fullmatch(digest)
            ):
                raise RuntimeError(f"Invalid member entry in builtin_browser_lite_manifest.json: {name!r}")
        if any(req not in mapping for req in required):
            raise RuntimeError(f"Missing required member in builtin_browser_lite_manifest.json {key}")


class ProductionBuildPy(_build_py):
    """Exclude source-present developer harness modules and stage canonical browser manifest."""

    def run(self) -> None:
        expected_manifest = build_builtin_browser_lite_manifest_bytes(_ROOT)
        source_manifest = _ROOT / _MANIFEST_REL_PATH
        if expected_manifest is not None:
            if not source_manifest.is_file() or source_manifest.read_bytes() != expected_manifest:
                source_manifest.parent.mkdir(parents=True, exist_ok=True)
                source_manifest.write_bytes(expected_manifest)
        else:
            if source_manifest.is_symlink() or not source_manifest.is_file():
                raise RuntimeError("Missing agent_py_agent/config/builtin_browser_lite_manifest.json")
            validate_builtin_browser_lite_manifest_bytes(source_manifest.read_bytes())

        # ``setuptools`` normally overlays the current source tree onto an
        # existing ``build/lib`` directory.  That leaves modules which were
        # deleted from the source tree in later wheels.  A distribution build
        # must be a projection of the current source tree, so rebuild its
        # staging directory from scratch every time.
        build_root = Path(self.build_lib)
        if build_root.exists():
            shutil.rmtree(build_root)
        super().run()
        # Remove source-present developer-only modules from the fresh staging
        # tree before setuptools assembles the wheel.
        if not build_root.exists():
            return
        for path in sorted(build_root.rglob("*"), reverse=True):
            if path.is_file():
                member = path.relative_to(build_root).as_posix()
                if forbidden_distribution_member(member):
                    path.unlink()
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        staged_manifest = build_root / _MANIFEST_REL_PATH
        if expected_manifest is not None:
            staged_manifest.parent.mkdir(parents=True, exist_ok=True)
            staged_manifest.write_bytes(expected_manifest)
        if not staged_manifest.is_file():
            raise RuntimeError("Staged wheel is missing builtin_browser_lite_manifest.json")
        validate_builtin_browser_lite_manifest_bytes(staged_manifest.read_bytes())

    def find_package_modules(self, package: str, package_dir: str):
        modules = super().find_package_modules(package, package_dir)
        return [
            item
            for item in modules
            if not is_dev_only_module(f"{item[0]}.{item[1]}")
        ]


if __name__ != "_manifest_only":
    setup(cmdclass={"build_py": ProductionBuildPy})
