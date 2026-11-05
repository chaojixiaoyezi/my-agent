# LLM: install/update 调用的纯内存导入器；复用 nofollow、清单构建及脱敏合同，不执行、下载或写来源树。
# 模块用途: 把 Claude 插件目录收成普通能力包，并逐项说明实际收了什么。
"""Claude 插件目录的确定性只读导入；来源内容不授予运行、联网或确认权限。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from ..capability_package_manifest import PackMCPServer, validate_capability_path
from ..common.log_redaction import redact_sensitive_text
from ..common.nofollow_fs import NoFollowPathError, open_directory_beneath, read_bytes_beneath
from ..common.strict_json import load_strict_json
from ..plugin_entry import PluginFile, validate_entry_files
from ..plugin_manifest import PluginPackageError
from ..plugin_package import PackageReadLimits
from ..tooling.package_build_tool import _VERSION
from .package_build import BuiltPackage, PackageBuildError, build_capability_pack
from .skills import split_skill_frontmatter

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..path_access_policy import PathAccessDecision

_RECEIPT_PATH = "import/RECEIPT.json"
_MANIFEST_PATH = ".claude-plugin/plugin.json"
_ROOT_VARIABLE = "${CLAUDE_PLUGIN_ROOT}/"
_EMPTY_SETTINGS = {"type": "object", "properties": {}, "additionalProperties": False}
_UNSUPPORTED_FIELDS = {"hooks": "hooks_unsupported", "lspServers": "lsp_unsupported",
                       "outputStyles": "output_style_unsupported", "monitors": "monitor_unsupported",
                       "userConfig": "user_config_unsupported", "dependencies": "dependencies_unsupported",
                       "settings": "settings_unsupported", "themes": "theme_unsupported", "workflows": "workflow_unsupported"}
_UNSUPPORTED_ROOTS = {"hooks": "hooks_unsupported", "bin": "bin_unsupported", "output-styles": "output_style_unsupported",
                      "monitors": "monitor_unsupported", "settings.json": "settings_unsupported", ".lsp.json": "lsp_unsupported",
                      "themes": "theme_unsupported", "workflows": "workflow_unsupported"}
_FETCHERS = frozenset({"npx", "uvx", "uv", "pipx", "bunx", "npm", "pnpm", "yarn", "docker", "podman"})


@dataclass(frozen=True)
class ExternalImportRequest:
    source_root: Path
    docs_only: bool = False
    network: bool = False
    limits: PackageReadLimits = field(default_factory=PackageReadLimits)
    path_check: Callable[[Path], PathAccessDecision] | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class ExternalImportResult:
    built: BuiltPackage
    receipt: dict


class ExternalImportError(ValueError):
    """导入没有写副作用；错误仅携带登记码和简短诊断，不回显源正文。"""

    # LLM: 调用方按冻结 code 组装失败回执；detail 只含短诊断，不含来源正文。
    # 函数用途: 保存导入失败的错误码和简短原因。
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code, self.detail = code, detail


# LLM: 各分类器共用的回执行构造器，只返回身份和冻结分类，不接收源配置值。
# 函数用途: 给一个组件记下去向和原因。
def _component(kind: str, name: str, path: str, status: str, code: str, target: str = "") -> dict:
    """只记录组件身份和分类；元数据值、env 与源正文不进入回执。"""
    return {"kind": kind, "name": _label(name), "source_path": path, "status": status, "code": code, "target_path": target}


# LLM: 只清理展示名，不能用于资源寻址或授予执行权限；无副作用。
# 函数用途: 把外来名称缩成安全的单行文字。
def _label(value: str) -> str:
    """生成有界单行展示名，不能把来源名称变成终端控制或绝对地址。"""
    return "".join(" " if unicodedata.category(c).startswith("C") else "-" if c in "/\\:" else c
                   for c in value)[:200].strip() or "component"


# LLM: walker 和组件路径共用的分量校验；不规范化危险名称，直接拒绝。
# 函数用途: 拦住会越界或污染显示的文件名。
def _check_name(name: str) -> None:
    """目录名只按实际分量校验，不清洗成另一个资源地址。"""
    if (not name or name in {".", ".."} or any(c in name for c in "/\\:\0")
            or any(ord(c) < 32 or ord(c) == 127 for c in name)):
        raise ExternalImportError("PLUGIN_IMPORT_PATH_UNSAFE", "unsafe_name")


# LLM: 使用 W1 路径合同，失败映射到导入错误码；不复制深度与字节上限。
# 函数用途: 检查相对地址是否超过包的路径预算。
def _check_path(path: str) -> None:
    """复用能力资源地址的深度、UTF-8 长度上限；不复制第二套路径规则。"""
    try:
        validate_capability_path(path)
    except (ValueError, UnicodeError) as exc:
        code = "PLUGIN_IMPORT_PATH_UNSAFE" if path.casefold() == "plugin.json" else "PLUGIN_IMPORT_LIMIT_EXCEEDED"
        raise ExternalImportError(code, "path_budget_or_reserved_name") from exc


# LLM: 仅通过 nofollow/dir_fd 读取来源；每目录一次枚举，每文件一次读取，异常拒绝整包并关闭 FD。
# 函数用途: 在文件类型和大小限制内读出目录快照。
def _walk(request: ExternalImportRequest) -> tuple[dict[str, bytes], list[dict]]:
    """每目录一次列名、每普通文件一次安全读取；先守类型与预算，再过滤隐藏项。"""
    if os.listdir not in os.supports_fd:
        raise ExternalImportError("PLUGIN_IMPORT_SOURCE_INVALID", "platform_unsupported")
    root = Path(request.source_root)
    if ".." in root.parts:
        raise ExternalImportError("PLUGIN_IMPORT_PATH_UNSAFE", "unsafe_root")
    if not root.is_absolute():
        root = Path(os.path.abspath(root))
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise ExternalImportError("PLUGIN_IMPORT_SOURCE_INVALID", "source_unreadable") from exc
    if stat.S_ISLNK(root_stat.st_mode):
        raise ExternalImportError("PLUGIN_IMPORT_PATH_UNSAFE", "unsafe_root")
    if not stat.S_ISDIR(root_stat.st_mode):
        raise ExternalImportError("PLUGIN_IMPORT_SOURCE_INVALID", "directory_required")
    contents, hidden, total, names_bytes = {}, [], 0, 0
    limits = request.limits

    # LLM: 只递归已打开的目录描述符；隐藏项也先检查类型，不跟随符号链接。
    # 函数用途: 按稳定顺序读一个目录，并累计整树预算。
    def visit(fd: int, parts: tuple[str, ...]) -> None:
        nonlocal total, names_bytes
        for name in sorted(os.listdir(fd)):
            _check_name(name)
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
                raise ExternalImportError("PLUGIN_IMPORT_PATH_UNSAFE", "special_file")
            path_parts = (*parts, name)
            path = "/".join(path_parts)
            _check_path(path)
            names_bytes += len(path.encode("utf-8"))
            if names_bytes > limits.directory_bytes:
                raise ExternalImportError("PLUGIN_IMPORT_LIMIT_EXCEEDED", "directory_bytes")
            exception = not parts and name in {".claude-plugin", ".mcp.json", ".lsp.json"}
            if name.startswith(".") and not exception:
                hidden.append(_component("hidden", name, path, "ignored", "hidden_entry_ignored"))
                continue
            if request.path_check is not None and not request.path_check(root.joinpath(*path_parts)).allowed:
                raise ExternalImportError("PLUGIN_IMPORT_PATH_UNSAFE", "member_unauthorized")
            if stat.S_ISDIR(st.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    opened = os.fstat(child)
                    if (st.st_dev, st.st_ino) != (opened.st_dev, opened.st_ino):
                        raise ExternalImportError("PLUGIN_IMPORT_PATH_UNSAFE", "directory_changed")
                    visit(child, path_parts)
                    current = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if (st.st_dev, st.st_ino) != (current.st_dev, current.st_ino):
                        raise ExternalImportError("PLUGIN_IMPORT_PATH_UNSAFE", "directory_changed")
                finally:
                    os.close(child)
                continue
            if len(contents) >= limits.members or st.st_size > limits.member_bytes or total + st.st_size > min(limits.archive_bytes, limits.expanded_bytes):
                raise ExternalImportError("PLUGIN_IMPORT_LIMIT_EXCEEDED", "file_budget")
            content = read_bytes_beneath(root, path_parts, max_bytes=limits.member_bytes, require_dir_fd=True)
            if content is None:
                raise ExternalImportError("PLUGIN_IMPORT_PATH_UNSAFE", "file_disappeared")
            current = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns) != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns):
                raise ExternalImportError("PLUGIN_IMPORT_PATH_UNSAFE", "file_changed")
            total += len(content)
            if total > min(limits.archive_bytes, limits.expanded_bytes):
                raise ExternalImportError("PLUGIN_IMPORT_LIMIT_EXCEEDED", "total_bytes")
            contents[path] = content

    try:
        fd = open_directory_beneath(Path(root.anchor), root.parts[1:], require_dir_fd=True)
        try:
            identity = os.fstat(fd)
            visit(fd, ())
            current = root.lstat()
            if (identity.st_dev, identity.st_ino) != (current.st_dev, current.st_ino):
                raise ExternalImportError("PLUGIN_IMPORT_PATH_UNSAFE", "root_changed")
        finally:
            os.close(fd)
    except ExternalImportError:
        raise
    except NoFollowPathError as exc:
        raise ExternalImportError("PLUGIN_IMPORT_PATH_UNSAFE", "nofollow_refused") from exc
    except OSError as exc:
        raise ExternalImportError("PLUGIN_IMPORT_SOURCE_INVALID", "source_unreadable") from exc
    return dict(sorted(contents.items())), hidden


# LLM: 只解析 walker 已读取的字节，严格 JSON 与 manifest 预算同时生效，不再访问文件。
# 函数用途: 从目录快照中读取一个小型 JSON 对象。
def _json_object(contents: dict[str, bytes], path: str, limits: PackageReadLimits) -> dict:
    """只解析本次安全读取的字节；描述文件最多 64 KiB，重复键和宽松 JSON 拒绝。"""
    raw = contents.get(path)
    if raw is None or len(raw) > min(65536, limits.manifest_bytes):
        raise ExternalImportError("PLUGIN_IMPORT_MANIFEST_INVALID", "manifest_missing_or_large")
    try:
        value = load_strict_json(raw)
        if not isinstance(value, dict):
            raise ValueError("object_required")
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ExternalImportError("PLUGIN_IMPORT_MANIFEST_INVALID", "manifest_json_invalid") from exc


# LLM: 只接 ./ 相对声明，危险分量直接拒绝；不能把外部路径清洗成可用成员。
# 函数用途: 检查组件声明是否留在插件目录里。
def _component_path(value: str) -> str:
    """组件声明只接 ./ 相对路径，不 resolve、不跟随路径或清洗越界分量。"""
    if not isinstance(value, str) or not value.startswith("./"):
        raise ExternalImportError("PLUGIN_IMPORT_MANIFEST_INVALID", "component_path_invalid")
    path = value[2:]
    for name in path.split("/"):
        try:
            _check_name(name)
        except ExternalImportError as exc:
            raise ExternalImportError("PLUGIN_IMPORT_MANIFEST_INVALID", "component_path_invalid") from exc
    _check_path(path)
    return path


# LLM: 供摘要和生成入口使用，只保留首行可读文字；不改变实际资源身份。
# 函数用途: 取一段简短、无控制字符的描述。
def _description(value: str, fallback: str) -> str:
    """描述第一行供摘要展示，控制字符换空格；空描述使用可读包名。"""
    line = value.splitlines()[0] if value.splitlines() else ""
    return "".join(" " if unicodedata.category(c).startswith("C") else c for c in line)[:200].strip() or fallback


# LLM: name 只按指定算法派生一次 ID，合法性交 W1 builder；不增加第二套 ID 规则。
# 函数用途: 确定导入后包名、版本和摘要。
def _identity(manifest: dict, root: Path) -> tuple[str, str, str]:
    """ID 派生只有一处，是否合法最终交给原清单构建，不复制 ID 正则。"""
    name, version = manifest.get("name", root.name), manifest.get("version", "0.1.0")
    if not isinstance(name, str) or not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise ExternalImportError("PLUGIN_IMPORT_MANIFEST_INVALID", "name_or_version_invalid")
    if not isinstance(manifest.get("description", ""), str):
        raise ExternalImportError("PLUGIN_IMPORT_MANIFEST_INVALID", "description_invalid")
    plugin_id = re.sub("[^a-z0-9]+", "-", name.lower()).strip("-")[:64]
    probe = {"plugin_id": plugin_id, "version": "0.1.0", "summary": "导入",
             "capability": {"description": "导入", "keywords": [], "entry_document": "CAPABILITY.md"},
             "files": [{"path": "CAPABILITY.md"}], "settings_schema": _EMPTY_SETTINGS}
    try:
        build_capability_pack(probe, {"CAPABILITY.md": b"# Import\n"})
    except (PackageBuildError, PluginPackageError, ValueError) as exc:
        raise ExternalImportError("PLUGIN_IMPORT_ID_INVALID", "derived_id_invalid") from exc
    return plugin_id, version, _description(manifest.get("description", ""), plugin_id)


# LLM: 只展开快照内的根 MCP 配置和显式路径；bundle/远程地址仅记录，不下载。
# 函数用途: 找齐目录声明的 MCP 服务器及其配置文件。
def _server_sources(manifest: dict, contents: dict[str, bytes], limits: PackageReadLimits) -> tuple[list[tuple[str, str, object]], set[str]]:
    """仅从本次快照展开根配置和显式引用；不读取树外路径或下载 bundle。"""
    sources, configs = [], set()

    # LLM: 展开已解析对象/数组与安全路径引用，配置路径去重防重复读取和循环引用。
    # 函数用途: 把一种 MCP 声明形状展开成统一候选表。
    def add(value: object, source: str) -> None:
        if isinstance(value, list):
            for index, item in enumerate(value):
                add(item, f"{source}[{index}]")
            return
        if isinstance(value, str):
            if value.lower().endswith((".mcpb", ".dxt")) or "://" in value:
                sources.append(("server", source, value))
                return
            path = _component_path(value)
            if path in configs:
                return
            configs.add(path)
            add(_json_object(contents, path, limits), path)
            return
        if not isinstance(value, dict):
            raise ExternalImportError("PLUGIN_IMPORT_MANIFEST_INVALID", "mcp_servers_invalid")
        servers = value.get("mcpServers", value)
        if not isinstance(servers, dict):
            raise ExternalImportError("PLUGIN_IMPORT_MANIFEST_INVALID", "mcp_servers_invalid")
        for name, server in sorted(servers.items()):
            sources.append((_label(name), source, server))

    if ".mcp.json" in contents:
        configs.add(".mcp.json")
        add(_json_object(contents, ".mcp.json", limits), ".mcp.json")
    if "mcpServers" in manifest:
        add(manifest["mcpServers"], _MANIFEST_PATH + "#mcpServers")
    return sorted(sources, key=lambda item: (item[0], item[1])), configs


# LLM: 候选只生成声明并复用 W1 runtime 校验；不探测解释器、不启动程序、不确认权限。
# 函数用途: 判断一个服务器能否安全映射成随包 stdio 工具。
def _server_candidate(name: str, value: object, contents: dict[str, bytes], network: bool) -> tuple[dict | None, str]:
    """将明确随包的 stdio 入口映射为 v10；其它形状只给不支持码，不启动解释器。"""
    if isinstance(value, str):
        return None, "mcp_bundle_unsupported" if value.lower().endswith((".mcpb", ".dxt")) else "mcp_remote_transport_unsupported" if "://" in value else "mcp_command_not_bundled"
    if not isinstance(value, dict):
        return None, "mcp_command_not_bundled"
    transport = value.get("type", "stdio")
    if not isinstance(transport, str):
        return None, "mcp_command_not_bundled"
    if "url" in value or transport != "stdio":
        return None, "mcp_remote_transport_unsupported"
    if value.get("env") or "headers" in value:
        return None, "mcp_env_unsupported"
    command, args = value.get("command"), value.get("args", [])
    if not isinstance(command, str) or not isinstance(args, list) or any(not isinstance(a, str) for a in args):
        return None, "mcp_command_not_bundled"
    if "cwd" in value or any("${" in (text[len(_ROOT_VARIABLE):] if text.startswith(_ROOT_VARIABLE) else text) for text in [command, *args]):
        return None, "mcp_variables_unsupported"
    if command.startswith(_ROOT_VARIABLE):
        path, entry_args, interpreter = command[len(_ROOT_VARIABLE):], args, ""
    elif (command and not any(c in command for c in "/\\:") and command.lower().removesuffix(".exe").removesuffix(".cmd") not in _FETCHERS
          and args and args[0].startswith(_ROOT_VARIABLE)):
        path, entry_args, interpreter = args[0][len(_ROOT_VARIABLE):], args[1:], command
    else:
        return None, "mcp_command_not_bundled"
    if path not in contents:
        return None, "mcp_command_not_bundled"
    root, _, leaf = path.rpartition("/")
    if not root or not leaf:
        return None, "mcp_runtime_tree_unsupported"
    fixed_args = []
    for arg in entry_args:
        if arg.startswith(_ROOT_VARIABLE):
            relative = arg[len(_ROOT_VARIABLE):]
            if not relative.startswith(root + "/"):
                return None, "mcp_runtime_tree_unsupported"
            fixed_args.append(relative[len(root) + 1:])
        else:
            fixed_args.append(arg)
    entry = {"kind": "interpreter" if interpreter else "executable", "command": leaf, "args": fixed_args}
    if interpreter:
        entry["interpreter"] = interpreter
    safe_name = re.sub("[^a-z0-9_-]", "-", name.lower()).strip("-_")[:32] or "server"
    server = {"name": safe_name, "root": root, "entry": entry, "platforms": ["any"],
              "permissions": {"network": network}, "tool_catalog": "pinned_on_enable", "tools": []}
    try:
        parsed = PackMCPServer.from_payload(server)
        runtime = tuple(PluginFile(p[len(root) + 1:], hashlib.sha256(data).hexdigest(), p == path and not interpreter)
                        for p, data in contents.items() if p.startswith(root + "/"))
        validate_entry_files(parsed.entry, runtime, parsed.platforms, ())
    except (ValueError, TypeError, KeyError):
        return None, "mcp_runtime_tree_unsupported"
    return server, "mcp_stdio_bundled"


# LLM: 候选按固定顺序选择最多一个，其余逐项记账；docs_only 永不生成 MCP 运行声明。
# 函数用途: 选出能导入的服务器，说明其它服务器为什么没收。
def _servers(request: ExternalImportRequest, manifest: dict, contents: dict[str, bytes]) -> tuple[dict | None, list[dict], set[str]]:
    """按稳定名称顺序保留首个支持的服务器；docs_only 从不产生运行声明。"""
    sources, configs = _server_sources(manifest, contents, request.limits)
    selected, rows = None, []
    for name, source, value in sources:
        if request.docs_only:
            server, code = None, "mcp_skipped_docs_only"
        else:
            server, code = _server_candidate(name, value, contents, request.network)
            if server is not None and selected is not None:
                server, code = None, "mcp_server_limit"
        if server is not None:
            selected = server
        rows.append(_component("mcp", name, source + "#" + name, "imported" if server else "unsupported", code,
                               server["root"] if server else ""))
    return selected, rows, configs


# LLM: 未映射元数据只记键名；启用、信任、审批字段不传给 builder，不保留其值。
# 函数用途: 说明来源清单里哪些字段没有采用。
def _metadata_rows(manifest: dict) -> tuple[list[dict], list[str]]:
    """映射表封闭、回执开放；展示元数据仅记字段身份，绝不采纳默认启用或审批字段。"""
    used = {"name", "version", "description", "keywords", "skills", "commands", "agents", "mcpServers"}
    metadata = {"author", "homepage", "repository", "license"}
    untrusted = {"defaultEnabled", "allowedTools", "enabled", "approval", "permissions", "autoApprove", "skipApproval", "trust"}
    rows, ignored = [], []
    for key in sorted(manifest):
        if key in used:
            continue
        code = _UNSUPPORTED_FIELDS.get(key)
        status = "unsupported" if code else "ignored"
        code = code or ("metadata_display_only" if key in metadata else "untrusted_field_ignored" if key in untrusted else "unknown_field_ignored")
        rows.append(_component("manifest_field", key, _MANIFEST_PATH + "#" + _label(key), status, code))
        ignored.append(_label(key))
    return rows, ignored


# LLM: 只重排内存快照；技能整树原样保留，命令/代理是参考文档，运行树与配置不混进文档。
# 函数用途: 收齐技能、参考文档和普通数据，并记录不支持的部分。
def _documents(manifest: dict, contents: dict[str, bytes], server: dict | None, configs: set[str]) -> tuple[dict[str, bytes], list[dict], list[tuple[str, str]]]:
    """技能整树原样保留；命令和代理只映射为文档，不依赖扩展名猜脚本执行权。"""
    roots = {kind: [kind] for kind in ("skills", "commands", "agents")}
    for kind in roots:
        if kind in manifest:
            values = manifest[kind] if isinstance(manifest[kind], list) else [manifest[kind]]
            roots[kind].extend(_component_path(value) for value in values)
        roots[kind] = sorted(set(roots[kind]))
    documents, rows, skills, claimed = {}, [], [], set()
    runtime_prefix = server["root"] + "/" if server else ""
    for path, data in contents.items():
        if runtime_prefix and path.startswith(runtime_prefix):
            claimed.add(path)
            continue
        if path == _MANIFEST_PATH or path in configs:
            claimed.add(path)
    for base in roots["skills"]:
        for path in contents:
            if not path.startswith(base + "/") or path in claimed:
                continue
            tail = path[len(base) + 1:].split("/")
            # 默认 skills/<name>/SKILL.md；显式根本身也是一个 Skill 时照样识别。
            skill_root = base if tail == ["SKILL.md"] else base + "/" + tail[0]
            entry = skill_root + "/SKILL.md"
            if entry not in contents or entry in claimed:
                continue
            try:
                meta, _ = split_skill_frontmatter(contents[entry].decode("utf-8"))
            except (ValueError, UnicodeError) as exc:
                raise ExternalImportError("PLUGIN_IMPORT_MANIFEST_INVALID", "skill_frontmatter_invalid") from exc
            name = meta.get("name", skill_root.rsplit("/", 1)[-1])
            description = meta.get("description", "")
            if not isinstance(name, str) or not isinstance(description, str):
                raise ExternalImportError("PLUGIN_IMPORT_MANIFEST_INVALID", "skill_metadata_invalid")
            name = _label(name)
            skills.append((name, _description(description, name)))
            rows.append(_component("skill", name, skill_root, "imported", "skill_imported", entry))
            for member, content in contents.items():
                if member.startswith(skill_root + "/") and member not in claimed:
                    documents[member] = content
                    claimed.add(member)
    for kind, code in (("commands", "command_as_document"), ("agents", "agent_as_document")):
        for base in roots[kind]:
            for path, data in contents.items():
                if path in claimed:
                    continue
                if (path.startswith(base + "/") and "/" not in path[len(base) + 1:] and path.endswith(".md")) or path == base and path.endswith(".md"):
                    documents[path], _ = data, claimed.add(path)
                    rows.append(_component(kind[:-1], PurePosixPath(path).stem, path, "mapped", code, path))
    for path, data in contents.items():
        if path in claimed:
            continue
        top = path.split("/")[0]
        if top in _UNSUPPORTED_ROOTS:
            code = _UNSUPPORTED_ROOTS[top]
            key = ("unsupported", top, top)
            if not any((r["kind"], r["source_path"], r["name"]) == key for r in rows):
                rows.append(_component("unsupported", top, top, "unsupported", code))
            continue
        # 没有 SKILL.md 的技能目录等未知形状仍被记账，作为纯数据保留，不静默遗漏。
        documents[path] = data
        if path != "CAPABILITY.md":
            rows.append(_component("document", PurePosixPath(path).name, path, "imported", "script_as_data", path))
    return documents, rows, sorted(skills)


# LLM: 与 package_build 共用识别器，只抛行号提示；不写文件，也不回显敏感值或源片段。
# 函数用途: 拦住即将打进包里的明显密钥。
def _reject_secrets(contents: dict[str, bytes]) -> None:
    """与 package_build 共用识别器；只回报行号，不把路径、密钥或源文字塞进异常。"""
    found = []
    for data in contents.values():
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if redact_sensitive_text(text, code_file=True) != text:
            found.extend(number for number, line in enumerate(text.splitlines(), 1)
                         if redact_sensitive_text(line, code_file=True) != line)
            if not found:
                found.append(1)
    if found:
        raise ExternalImportError("PACKAGE_BUILD_SECRET_FOUND", "lines:" + ",".join(map(str, sorted(set(found))[:5])))


# LLM: 输入仅来自清理后的快照分类，输出确定性 UTF-8 入口；不宣称程序已获准运行。
# 函数用途: 为没有入口文档的插件生成中文使用说明。
def _entry_document(plugin_id: str, description: str, skills: list[tuple[str, str]], rows: list[dict], server: dict | None) -> bytes:
    """生成稳定中文入口；命令只是参考，声明服务器也不等于已经获准运行。"""
    lines = [f"# {plugin_id}", "", f"适用：{description}", "", "## 技能"]
    lines.extend(f"- {name}：{summary}" for name, summary in skills)
    lines += ["", "## 命令与代理（只作参考文档）"]
    lines.extend(f"- {row['source_path']}：只作参考文档" for row in rows if row["kind"] in {"command", "agent"})
    if server:
        entry = server["entry"]
        lines += ["", "## MCP 工具", f"- {server['name']}：{entry.get('interpreter', '')} {entry['command']}；启用时需要你确认"]
    lines += ["", "## 未导入的部分"]
    lines.extend(f"- {code}" for code in sorted({row["code"] for row in rows if row["status"] in {"unsupported", "ignored"}}))
    return ("\n".join(lines) + "\n").encode("utf-8")


# LLM: install/update 共用纯导入入口，输出 W1 普通包与冻结回执；不保存、不执行、不下载、不确认。
# 函数用途: 把一个外来插件目录完整收成可审查的能力包。
def import_claude_plugin(request: ExternalImportRequest) -> ExternalImportResult:
    """只读来源树，在内存生成普通 v7/v10 包和回执；没有执行、下载、改名或保存副作用。"""
    if not isinstance(request, ExternalImportRequest) or type(request.docs_only) is not bool or type(request.network) is not bool:
        raise ExternalImportError("PLUGIN_IMPORT_SOURCE_INVALID", "request_invalid")
    contents, rows = _walk(request)
    manifest = _json_object(contents, _MANIFEST_PATH, request.limits) if _MANIFEST_PATH in contents else {}
    if _MANIFEST_PATH in contents and "name" not in manifest:
        raise ExternalImportError("PLUGIN_IMPORT_MANIFEST_INVALID", "name_required")
    plugin_id, version, description = _identity(manifest, Path(os.path.abspath(request.source_root)))
    metadata, ignored = _metadata_rows(manifest)
    rows += metadata
    server, server_rows, configs = _servers(request, manifest, contents)
    rows += server_rows
    documents, document_rows, skills = _documents(manifest, contents, server, configs)
    rows += document_rows
    if not documents and server is None:
        raise ExternalImportError("PLUGIN_IMPORT_NO_CONTENT", "no_content")
    if _RECEIPT_PATH in documents:
        raise ExternalImportError("PLUGIN_IMPORT_PATH_UNSAFE", "receipt_path_reserved")
    entry_code = "entry_document_source" if "CAPABILITY.md" in documents else "entry_document_generated"
    if "CAPABILITY.md" not in documents:
        documents["CAPABILITY.md"] = _entry_document(plugin_id, description, skills, rows, server)
    rows.append(_component("entry", plugin_id, "CAPABILITY.md" if entry_code == "entry_document_source" else "",
                           "imported", entry_code, "CAPABILITY.md"))
    keywords = manifest.get("keywords", [])
    if not isinstance(keywords, list) or any(not isinstance(word, str) for word in keywords):
        raise ExternalImportError("PLUGIN_IMPORT_MANIFEST_INVALID", "keywords_invalid")
    keywords = list(dict.fromkeys([*keywords, *(name for name, _ in skills), *plugin_id.split("-")]))[:16]
    # 元数据也会交给模型，空词或控制文字不能靠最终打包异常偷偷漏掉。
    if any(not word.strip() or len(word.encode("utf-8")) > 128 or any(ord(c) < 32 or ord(c) == 127 for c in word) for word in keywords):
        raise ExternalImportError("PLUGIN_IMPORT_MANIFEST_INVALID", "keywords_invalid")
    tree_lines = [f"{path}\0{hashlib.sha256(data).hexdigest()}\0{len(data)}\n" for path, data in contents.items()]
    receipt = {"schema": "external_plugin_import_receipt.v1", "source_kind": "claude_plugin",
               "source_tree_sha256": hashlib.sha256("".join(tree_lines).encode()).hexdigest(),
               "plugin_id": plugin_id, "version": version, "package_schema": "plugin_package.v10" if server else "plugin_package.v7",
               "docs_only": request.docs_only, "network": request.network,
               "components": sorted(rows, key=lambda row: (row["kind"], row["source_path"], row["name"])), "ignored_fields": ignored}
    documents[_RECEIPT_PATH] = (json.dumps(receipt, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    packed = {**documents, **({p: data for p, data in contents.items() if p.startswith(server["root"] + "/")} if server else {})}
    packed = dict(sorted(packed.items()))
    _reject_secrets(packed)
    executable = server["root"] + "/" + server["entry"]["command"] if server and server["entry"]["kind"] == "executable" else ""
    declaration = {"plugin_id": plugin_id, "version": version, "summary": description,
                   "capability": {"description": description, "keywords": keywords, "entry_document": "CAPABILITY.md"},
                   "files": [{"path": path, **({"executable": True} if path == executable else {})} for path in packed],
                   "settings_schema": _EMPTY_SETTINGS, **({"mcp_servers": [server]} if server else {})}
    try:
        built = build_capability_pack(declaration, packed)
    except (PackageBuildError, PluginPackageError, ValueError) as exc:
        raise ExternalImportError("PLUGIN_IMPORT_MANIFEST_INVALID", "package_invalid") from exc
    return ExternalImportResult(built, receipt)
