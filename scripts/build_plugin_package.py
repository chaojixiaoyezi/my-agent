# LLM: 只构建仓内自有受信插件；标准后端生产 wheel，原包/依赖校验器判定发布归档，不在产品安装期执行。
# 模块用途: 从包内唯一声明和已构建依赖生成可交给现有安装器的本地插件 ZIP。

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path
from zipfile import ZipFile

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


# LLM: project 限定仓内 plugins 的直属自有工程；仅复制 pyproject/src/统一许可，不打入工作区日志或缓存。
# 函数用途: 构建实际插件 wheel，读取声明、填入标准元数据后验证完整依赖并独占发布 ZIP。
def build_plugin_package(project: Path, declaration_path: str, dependencies: tuple[Path, ...], output: Path) -> Path:
    project = project.resolve()
    if project.parent != ROOT / "plugins" or not (project / "src").is_dir():
        raise ValueError("只允许构建仓内自有插件工程")
    with tempfile.TemporaryDirectory(prefix="my-agent-plugin-build-") as temporary:
        staging = Path(temporary) / "source"
        staging.mkdir()
        shutil.copyfile(project / "pyproject.toml", staging / "pyproject.toml")
        shutil.copytree(project / "src", staging / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"))
        for name in ("LICENSE", "NOTICE"):
            shutil.copyfile(ROOT / name, staging / name)
        wheel = build_wheel(staging, Path(temporary) / "wheels")
        with ZipFile(wheel) as archive:
            manifest = json.loads(archive.read(declaration_path))
            wheel_names = archive.namelist()
        generated = {"schema_version", "version", "entry_wheel", "wheels"}
        if not isinstance(manifest, dict) or generated & set(manifest):
            raise ValueError("声明不能覆盖构建元数据")
        wheel_bytes = {"wheels/" + item.name: item.read_bytes() for item in (wheel, *dependencies)}
        if len(wheel_bytes) != len(dependencies) + 1:
            raise ValueError("依赖 wheel 文件重名")
        # 声明了观察/观察引用的包用 v5，声明了宿主 API 权限的包用 v4，声明了随包 Skill 的包用 v3，声明了面板的包用 v2；
        # 其余保持 v1，已发布包重建后的描述不变
        tools = manifest.get("tools") if isinstance(manifest.get("tools"), list) else []
        if any(isinstance(tool, dict) and ("observation" in tool or "observation_ref" in tool) for tool in tools):
            if manifest.get("skills"):
                _check_declared_skills(manifest, wheel_names)
            manifest.setdefault("panels", [])
            manifest.setdefault("skills", [])
            manifest.setdefault("host_api", [])
            schema = PLUGIN_PACKAGE_SCHEMA_V5
        elif manifest.get("host_api"):
            if manifest.get("skills"):
                _check_declared_skills(manifest, wheel_names)
            manifest.setdefault("panels", [])
            manifest.setdefault("skills", [])
            schema = PLUGIN_PACKAGE_SCHEMA_V4
        elif manifest.get("skills"):
            _check_declared_skills(manifest, wheel_names)
            manifest.setdefault("panels", [])
            schema = PLUGIN_PACKAGE_SCHEMA_V3
        else:
            schema = PLUGIN_PACKAGE_SCHEMA_V2 if manifest.get("panels") else PLUGIN_PACKAGE_SCHEMA
        manifest.update(schema_version=schema, version=wheel_metadata(wheel)["Version"],
                        entry_wheel="wheels/" + wheel.name,
                        wheels=[{"path": name, "sha256": hashlib.sha256(content).hexdigest()} for name, content in wheel_bytes.items()])
        buffer = io.BytesIO()
        with ZipFile(buffer, "w") as archive:
            archive.writestr("plugin.json", json.dumps(manifest, ensure_ascii=False, sort_keys=True))
            for name, content in wheel_bytes.items():
                archive.writestr(name, content)
        payload = buffer.getvalue()
        package = inspect_plugin_package(payload)
        inspect_plugin_wheels(package)
        return publish_artifact(output.resolve(), payload)


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
# 函数用途: 从命令行生成自有插件安装包。
def main() -> None:
    parser = argparse.ArgumentParser(description="构建仓内自有插件安装包")
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--declaration", required=True, help="wheel 内声明 JSON 的路径")
    parser.add_argument("--wheel", type=Path, action="append", default=[], help="已构建依赖，可重复")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    print(build_plugin_package(arguments.project, arguments.declaration, tuple(arguments.wheel), arguments.output))


if __name__ == "__main__":
    main()
