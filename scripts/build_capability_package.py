# LLM: 构建只消费开发者明确列出的私有文件，生成摘要和 v7 协议；不启动脚本、不安装包，旧 v1-v6 构建不变。
# 模块用途: 将领域方法、流程及配套资源构建成无进程能力包，沿现有安装器发布。

from __future__ import annotations

import argparse
import hashlib
import io
import json
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_py_agent.agent.capability_package_manifest import (
    CAPABILITY_PACKAGE_SCHEMA,
    MAX_CAPABILITY_FILES,
    validate_capability_path,
)
from agent_py_agent.agent.common.nofollow_fs import read_bytes_beneath
from agent_py_agent.agent.common.strict_json import load_strict_json
from agent_py_agent.agent.plugin_package import PackageReadLimits, inspect_plugin_package
from scripts.plugin_build import publish_artifact


# LLM: 输入省略生成字段，源文件经原 no-follow 读取器逐段拒绝链接；输出独占创建，资源始终无执行位。
# 函数用途: 有界读取明确资源，构建可重复 ZIP 并验证后写入目标文件，不执行包内内容。
def build_capability_package(declaration: dict, files_root: Path, output: Path) -> Path:
    fields = {"plugin_id", "version", "summary", "capability", "files", "settings_schema"}
    if not isinstance(declaration, dict) or set(declaration) != fields:
        raise ValueError("能力包声明字段不完整或含生成字段")
    items = declaration["files"]
    if (not isinstance(items, list) or not 1 <= len(items) <= MAX_CAPABILITY_FILES
            or any(not isinstance(item, dict) or set(item) != {"path"} for item in items)):
        raise ValueError("能力包 files 每项只声明 path")
    limits, root, contents, total = PackageReadLimits(), files_root.resolve(), {}, 0
    for item in items:
        path = item["path"]
        validate_capability_path(path)
        if path in contents:
            raise ValueError("能力资源重复")
        try:
            content = read_bytes_beneath(root, PurePosixPath(path).parts,
                                         max_bytes=min(limits.member_bytes, limits.expanded_bytes - total))
        except (OSError, ValueError) as exc:
            raise ValueError("能力资源必须是预算内且无链接的普通文件") from exc
        if content is None:
            raise ValueError("能力资源不存在")
        total += len(content)
        if len(content) > limits.member_bytes or total > limits.expanded_bytes:
            raise ValueError("能力资源超过读取预算")
        contents[path] = content
    manifest = {**declaration, "schema_version": CAPABILITY_PACKAGE_SCHEMA, "package_kind": "capability",
                "files": [{"path": path, "sha256": hashlib.sha256(content).hexdigest(), "executable": False}
                          for path, content in sorted(contents.items())]}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        members = {"plugin.json": json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode(), **contents}
        for path, content in sorted(members.items()):
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, content)
    payload = buffer.getvalue()
    inspect_plugin_package(payload)
    return publish_artifact(output.resolve(), payload)


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
