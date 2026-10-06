# LLM: 开发者命令行薄壳：读声明、按"不跟随链接"读出列出的文件、调产品里唯一的打包实现（capability/package_build），
#   再独占写出产物。不启动脚本、不安装包；打包规则只在产品里改，这里不留第二份。
# 模块用途: 将领域方法、流程及配套资源构建成无进程能力包，沿现有安装器发布。

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_py_agent.agent.capability.package_build import (
    build_capability_pack,
    capability_pack_paths,
    read_declared_files,
)
from agent_py_agent.agent.common.strict_json import load_strict_json
from scripts.plugin_build import publish_artifact


# LLM: 声明外形先校验再读文件；源文件逐段拒绝链接；输出独占创建，资源始终无执行位。
# 函数用途: 有界读取明确资源，构建可重复 ZIP 并验证后写入目标文件，不执行包内内容。
def build_capability_package(declaration: dict, files_root: Path, output: Path) -> Path:
    contents = read_declared_files(files_root, capability_pack_paths(declaration))
    return publish_artifact(output.resolve(), build_capability_pack(declaration, contents).payload)


# LLM: 命令参数只选择构建输入输出，不修改正式能力安装或任何运行配置。
# 函数用途: 从声明文件构建能力包，并显示已写入的包地址。
def main() -> None:
    parser = argparse.ArgumentParser(description="构建无进程能力包（plugin_package.v7）")
    parser.add_argument("--declaration", type=Path, required=True)
    parser.add_argument("--files-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    declaration = load_strict_json(arguments.declaration.read_bytes())
    print(build_capability_package(declaration, arguments.files_root, arguments.output))


if __name__ == "__main__":
    main()
