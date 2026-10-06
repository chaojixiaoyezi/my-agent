# LLM: 开发者命令行薄壳：只打包开发者显式列出的随包文件，打包规则（清单、摘要、v6/v8 选择、成员顺序与权限位、复验）
#   只在产品 capability/package_build 里。构建期不执行任何随包文件；同一输入得到同一包字节（同一 sha256）。
#   读文件沿用本脚本原口径（解析后必须仍在文件根内的普通文件），不改变开发者现有用法。
# 模块用途: 把任意语言插件的声明和文件打成 v6 或带订阅的 v8 包，构建不授予启用权限。

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_py_agent.agent.capability.package_build import build_files_plugin, files_plugin_paths
from scripts.plugin_build import publish_artifact


# LLM: 声明外形先校验再读文件；来源不得越根；复验失败不发布，输出独占创建。
# 函数用途: 构建可复现 v6/v8 包，读取列出的来源并写输出 ZIP，不改变安装表。
def build_files_package(declaration: dict, files_root: Path, output: Path, platforms: tuple[str, ...] = ()) -> Path:
    root = files_root.resolve()
    contents = {}
    for path in files_plugin_paths(declaration, platforms):
        source = (root / str(path)).resolve()
        if not source.is_relative_to(root) or not source.is_file():
            raise ValueError(f"随包文件不存在或越出文件根目录：{path}")
        contents[path] = source.read_bytes()
    return publish_artifact(output.resolve(), build_files_plugin(declaration, contents, platforms).payload)


# LLM: 命令行与函数共用版本选择和静态复验，不联网、不编译、不执行插件或改安装表。
# 函数用途: 由声明 JSON 构建 v6/v8 插件包并打印输出路径。
def main() -> None:
    parser = argparse.ArgumentParser(description="构建任意语言插件包（v6；声明订阅或权限时为 v8）")
    parser.add_argument("--declaration", type=Path, required=True, help="不含摘要与版本的声明 JSON，可含 events/tool_gates/permissions")
    parser.add_argument("--files-root", type=Path, required=True, help="随包文件所在目录，files[].path 相对于它")
    parser.add_argument("--platform", action="append", default=[], help="目标平台标记（如 darwin-arm64），可重复")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    declaration = json.loads(arguments.declaration.read_text(encoding="utf-8"))
    print(build_files_package(declaration, arguments.files_root, arguments.output, tuple(arguments.platform)))


if __name__ == "__main__":
    main()
