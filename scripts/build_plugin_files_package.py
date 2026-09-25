# LLM: 只打包开发者显式列出的随包文件；摘要与协议版本由脚本生成，声明不能自带。构建期不执行任何随包文件，
#   产物交给宿主同一个读包校验器复核，失败不发布。成员时间戳固定，同一输入得到同一包字节（同一 sha256）。
# 模块用途: 把非 Python（任意语言）插件的声明和随包文件打成 plugin_package.v6 安装包，供 /plugins install 使用。

from __future__ import annotations

import argparse
import hashlib
import io
import json
import stat
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_py_agent.agent.plugin_manifest import PLUGIN_PACKAGE_SCHEMA_V6
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from scripts.plugin_build import publish_artifact

_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


# LLM: declaration 是去掉生成字段的 v6 描述：files 每项只写 path 与 executable，内容从 files_root/<path> 读取（解析后
#   必须仍在 files_root 内）；platforms 只能在声明或参数里二选一。面板/Skill/宿主 API 缺省补空列表。输出独占创建。
# 函数用途: 生成可交给现有安装器的 v6 插件 ZIP 并返回输出路径。有副作用：写输出文件。
def build_files_package(declaration: dict, files_root: Path, output: Path, platforms: tuple[str, ...] = ()) -> Path:
    if not isinstance(declaration, dict) or "schema_version" in declaration:
        raise ValueError("声明必须是对象，且不能自带协议版本")
    if platforms and "platforms" in declaration:
        raise ValueError("平台只能在声明或 --platform 中二选一")
    items = declaration.get("files")
    if not isinstance(items, list) or any(not isinstance(item, dict) or set(item) != {"path", "executable"} for item in items):
        raise ValueError("files 每项只能写 path 与 executable，摘要由脚本生成")
    root = files_root.resolve()
    contents = {}
    for item in items:
        source = (root / str(item["path"])).resolve()
        if not source.is_relative_to(root) or not source.is_file():
            raise ValueError(f"随包文件不存在或越出文件根目录：{item['path']}")
        contents[item["path"]] = source.read_bytes()
    manifest = {"panels": [], "skills": [], "host_api": [], **declaration, "schema_version": PLUGIN_PACKAGE_SCHEMA_V6,
                "files": [{**item, "sha256": hashlib.sha256(contents[item["path"]]).hexdigest()} for item in items]}
    if platforms:
        manifest["platforms"] = list(platforms)
    executables = {item["path"] for item in items if item["executable"] is True}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        _add_member(archive, "plugin.json", json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode(), 0o644)
        for path in sorted(contents):
            _add_member(archive, path, contents[path], 0o755 if path in executables else 0o644)
    payload = buffer.getvalue()
    if inspect_plugin_package(payload).manifest.entry is None:
        raise ValueError("产物不是 v6 非 Python 插件包")
    return publish_artifact(output.resolve(), payload)


# LLM: 不写目录成员（宿主读包器拒绝目录项）；权限位只是方便解压查看，宿主以描述里的 executable 为准。
# 函数用途: 以固定时间戳和普通文件类型写入一个 ZIP 成员。
def _add_member(archive: zipfile.ZipFile, name: str, content: bytes, mode: int) -> None:
    info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (stat.S_IFREG | mode) << 16
    archive.writestr(info, content)


# LLM: 开发者显式指定声明、文件根目录与输出；不联网、不编译、不改变宿主插件安装表。
# 函数用途: 从命令行生成非 Python 插件安装包并打印输出路径。
def main() -> None:
    parser = argparse.ArgumentParser(description="构建非 Python（任意语言）插件安装包（plugin_package.v6）")
    parser.add_argument("--declaration", type=Path, required=True, help="不含摘要与协议版本的 v6 声明 JSON")
    parser.add_argument("--files-root", type=Path, required=True, help="随包文件所在目录，files[].path 相对于它")
    parser.add_argument("--platform", action="append", default=[], help="目标平台标记（如 darwin-arm64），可重复")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    declaration = json.loads(arguments.declaration.read_text(encoding="utf-8"))
    print(build_files_package(declaration, arguments.files_root, arguments.output, tuple(arguments.platform)))


if __name__ == "__main__":
    main()
