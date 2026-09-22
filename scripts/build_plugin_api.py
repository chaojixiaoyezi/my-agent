# LLM: SDK 是固定共用源码的构建期原字节投影；不要递归打包宿主、替换 import 或在运行时复制源码。
# 模块用途: 构建没有宿主依赖的插件 SDK wheel，并核对其中源码和成员边界。

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.plugin_build import build_wheel, publish_artifact, wheel_metadata

SDK_SOURCES = {
    "path_access_policy.py": "agent_py_agent/agent/path_access_policy.py",
    "workspace_read_context.py": "agent_py_agent/agent/workspace_read_context.py",
    "nofollow_fs.py": "agent_py_agent/agent/common/nofollow_fs.py",
}


# LLM: 只允许固定 SDK 包成员和标准 dist-info；源码逐字节相同，防止投影时混入宿主模块或私有文件。
# 函数用途: 检查实际构建 wheel 的依赖与源码边界。
def verify_sdk_wheel(wheel: Path, source_bytes: dict[str, bytes]) -> None:
    metadata = wheel_metadata(wheel)
    if metadata["Name"] != "my-agent-plugin-api" or metadata.get_all("Requires-Dist"):
        raise ValueError("SDK 必须无宿主或第三方运行依赖")
    expected = {f"my_agent_plugin_api/{name}": content for name, content in source_bytes.items()}
    expected["my_agent_plugin_api/__init__.py"] = b""
    with ZipFile(wheel) as archive:
        names = archive.namelist()
        metadata_root = next(name.rsplit("/", 1)[0] + "/" for name in names if name.endswith(".dist-info/METADATA"))
        if len(names) != len(set(names)) or {name for name in names if not name.startswith(metadata_root)} != set(expected):
            raise ValueError("SDK wheel 包含声明以外的文件")
        if any(archive.read(name) != content for name, content in expected.items()):
            raise ValueError("SDK 源码与宿主 canonical 源码不一致")


# LLM: 构建在临时工程完成且不导入宿主；发行版本唯一来自 SDK pyproject，最终文件不覆盖已有版本。
# 函数用途: 投影固定共用源码及许可，经标准构建和校验后发布独立 SDK wheel。
def build_plugin_api(wheel_dir: Path) -> Path:
    source_bytes = {name: (ROOT / source).read_bytes() for name, source in SDK_SOURCES.items()}
    with tempfile.TemporaryDirectory(prefix="my-agent-plugin-api-") as temporary:
        staging = Path(temporary) / "source"
        package = staging / "src/my_agent_plugin_api"
        package.mkdir(parents=True)
        (package / "__init__.py").write_bytes(b"")
        for name, content in source_bytes.items():
            (package / name).write_bytes(content)
        shutil.copyfile(ROOT / "plugins/sdk/pyproject.toml", staging / "pyproject.toml")
        for name in ("LICENSE", "NOTICE"):
            shutil.copyfile(ROOT / name, staging / name)
        wheel = build_wheel(staging, Path(temporary) / "wheels")
        verify_sdk_wheel(wheel, source_bytes)
        return publish_artifact(wheel_dir.resolve() / wheel.name, wheel.read_bytes())


# LLM: 入口只接受显式开发输出目录，不连接 Gateway 或修改用户插件安装状态。
# 函数用途: 从命令行构建 SDK，打印最终 wheel 位置。
def main() -> None:
    parser = argparse.ArgumentParser(description="从唯一共用源码构建插件 SDK wheel")
    parser.add_argument("--wheel-dir", type=Path, required=True, help="保存 wheel 的开发输出目录")
    arguments = parser.parse_args()
    print(build_plugin_api(arguments.wheel_dir))


if __name__ == "__main__":
    main()
