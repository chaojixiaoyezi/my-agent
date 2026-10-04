# LLM: 有意为 v1–v5 wheel 作者放宽本地工程范围；后端会执行工程代码，授权工作区由会话沙箱和调用方约束，脚本不做路径围栏。
# 模块用途: 有意允许 v1–v5 作者构建仓外受信本地工程；后端会执行构建钩子，授权工作区由会话沙箱和调用方守住，脚本不做路径围栏，安装/启用不用它。

"""为 v1–v5 wheel 插件作者构建受信本地工程。

有意将目标放宽为任意带 pyproject.toml 和 src 的本地工程，而非仅仓内 plugins/。
wheel 构建后端会执行工程代码和构建钩子；把工程限制在授权工作区内，是会话沙箱
和调用方的责任，本脚本不做路径围栏。这是构建期开发工具，安装和启用时不调用。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_py_agent.agent.plugin_manifest import (
    PLUGIN_PACKAGE_SCHEMA,
    PLUGIN_PACKAGE_SCHEMA_V2,
    PLUGIN_PACKAGE_SCHEMA_V3,
    PLUGIN_PACKAGE_SCHEMA_V4,
    PLUGIN_PACKAGE_SCHEMA_V5,
)
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.plugin_wheels import inspect_plugin_wheels
from scripts.plugin_build import build_wheel, publish_artifact, wheel_metadata

_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


# LLM: project 是开发者显式指定的受信本地工程，必须有 pyproject/src；不从插件声明选择宿主路径，不扩大模型路径授权。
#   wheel 后端会执行源码；仅构建期使用。包版本选择、依赖闭包、独占发布沿原合同，联测作者模板和既有插件包测试。
# 函数用途: 构建实际插件 wheel，验证完整依赖并独占发布 ZIP；TMPDIR 由调用方选在工作区内。
def build_plugin_package(project: Path, declaration_path: str, dependencies: tuple[Path, ...], output: Path) -> Path:
    project = project.resolve()
    if not (project / "pyproject.toml").is_file() or not (project / "src").is_dir():
        raise ValueError("本地插件工程必须包含 pyproject.toml 和 src 目录")
    with tempfile.TemporaryDirectory(prefix="my-agent-plugin-build-") as temporary:
        wheel = _build_source_wheel(project, Path(temporary))
        with ZipFile(wheel) as archive:
            manifest = json.loads(archive.read(declaration_path))
            wheel_names = archive.namelist()
        generated = {"schema_version", "version", "entry_wheel", "wheels"}
        if not isinstance(manifest, dict) or generated & set(manifest):
            raise ValueError("声明不能覆盖构建元数据")
        wheel_bytes = {"wheels/" + item.name: item.read_bytes() for item in (wheel, *dependencies)}
        if len(wheel_bytes) != len(dependencies) + 1:
            raise ValueError("依赖 wheel 文件重名")
        schema = _manifest_schema(manifest, wheel_names)
        manifest.update(schema_version=schema, version=wheel_metadata(wheel)["Version"],
                        entry_wheel="wheels/" + wheel.name,
                        wheels=[{"path": name, "sha256": hashlib.sha256(content).hexdigest()} for name, content in wheel_bytes.items()])
        payload = _package_payload(manifest, wheel_bytes)
        package = inspect_plugin_package(payload)
        inspect_plugin_wheels(package)
        return publish_artifact(output.resolve(), payload)


# LLM: 仅复制标准构建输入；自有仓内插件沿原统一许可，本地作者工程带有许可时原样保留，不把缓存/日志收进包。
# 函数用途: 在临时目录构建一个真实 wheel；有副作用：复制输入并执行离线构建后端。
def _build_source_wheel(project: Path, temporary: Path) -> Path:
    staging = temporary / "source"
    staging.mkdir()
    shutil.copyfile(project / "pyproject.toml", staging / "pyproject.toml")
    shutil.copytree(project / "src", staging / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"))
    for name in ("LICENSE", "NOTICE"):
        source = project / name if (project / name).is_file() else ROOT / name
        shutil.copyfile(source, staging / name)
    return build_wheel(staging, temporary / "wheels")


# LLM: 只按结构化扩展字段决定 v1–v5；补空扩展及 Skill 一致性规则与旧构建路径相同，不按样例名字选版本。
# 函数用途: 给声明选择最小适用 Python 清单版本，并补齐该版本必需的扩展字段。
def _manifest_schema(manifest: dict, wheel_names: list[str]) -> str:
    if manifest.get("skills"):
        _check_declared_skills(manifest, wheel_names)
    tools = manifest.get("tools") if isinstance(manifest.get("tools"), list) else []
    if any(isinstance(tool, dict) and ("observation" in tool or "observation_ref" in tool) for tool in tools):
        manifest.setdefault("panels", [])
        manifest.setdefault("skills", [])
        manifest.setdefault("host_api", [])
        return PLUGIN_PACKAGE_SCHEMA_V5
    if manifest.get("host_api"):
        manifest.setdefault("panels", [])
        manifest.setdefault("skills", [])
        return PLUGIN_PACKAGE_SCHEMA_V4
    if manifest.get("skills"):
        manifest.setdefault("panels", [])
        return PLUGIN_PACKAGE_SCHEMA_V3
    return PLUGIN_PACKAGE_SCHEMA_V2 if manifest.get("panels") else PLUGIN_PACKAGE_SCHEMA


# LLM: ZIP 元数据和排序保持原实现；归档字节返回后仍必须经产品读包/依赖校验，不能跳过验证直接发布。
# 函数用途: 编码包描述和已构建 wheel，不写最终产物。
def _package_payload(manifest: dict, wheel_bytes: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        _add_member(archive, "plugin.json", json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode())
        for name, content in wheel_bytes.items():
            _add_member(archive, name, content)
    return buffer.getvalue()


# LLM: Python 插件外层包和 wheel 一样必须固定归档元数据，否则同一源码会因构建时钟产生不同摘要。
# 函数用途: 以固定时间戳和普通只读文件权限写入一个插件 ZIP 成员。
def _add_member(archive: ZipFile, name: str, content: bytes) -> None:
    info = ZipInfo(name, date_time=_ZIP_TIME)
    info.compress_type = ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    archive.writestr(info, content)


# LLM: 宿主只按入口包 skills/<名称>/SKILL.md 定位随包 Skill；构建期要求 wheel 里的 Skill 目录与声明名单完全一致，
#   避免声明了却没打进包（启用后找不到），或打进了未声明的 Skill（用户看不到却被暴露）。
# 函数用途: 校验声明的随包 Skill 名单与 wheel 实际内容一致。
def _check_declared_skills(manifest: dict, wheel_names: list[str]) -> None:
    top = str(manifest.get("entry_module", "")).split(".")[0]
    prefix = f"{top}/skills/"
    present = {name[len(prefix):].split("/")[0] for name in wheel_names
               if name.startswith(prefix) and name.endswith("/SKILL.md") and name.count("/") == 3}
    declared = set(manifest["skills"]) if isinstance(manifest["skills"], list) else set()
    if present != declared:
        raise ValueError(f"随包 Skill 与声明不一致：wheel 中 {sorted(present)}，声明 {sorted(declared)}")


# LLM: 开发者显式指定全部本地 wheel 与输出；不自动联网补依赖，也不改变宿主插件安装表。
# 函数用途: 从命令行构建显式受信本地工程；不安装、不启用、不调用 my-agent 命令。
def main() -> None:
    parser = argparse.ArgumentParser(description="构建显式指定的受信本地 Python 插件安装包")
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--declaration", required=True, help="wheel 内声明 JSON 的路径")
    parser.add_argument("--wheel", type=Path, action="append", default=[], help="已构建依赖，可重复")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    print(build_plugin_package(arguments.project, arguments.declaration, tuple(arguments.wheel), arguments.output))


if __name__ == "__main__":
    main()
