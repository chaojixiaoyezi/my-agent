# LLM: learnpack 打包工具 package_build（只注册给本机管理员主代理，默认收起）。它把模型整理好的目录打成能力包或文件型插件包，
#   存进宿主的 learnpack 存储（<owner home>/data/learnpack/，模型写不进去），不安装、不启用。
#   读文件前每个路径都过同一注册表里 read_file 的路径裁决（模型自己读不到的文件打不进包），按"不跟随链接"读取；
#   文本文件里认出密钥就拒绝整包（不替她改内容）。插件第一期只收 v6 文件包（声明里不能有 events/tool_gates/permissions）。
#   回执是结构化事实：包身份与摘要、两个开关现读值与开关命令原文、下一步怎么装；模型照回执提醒用户，不凭记忆。
#   改输入字段或回执字段要同步 learn-external-agent 技能正文与 test_package_build_tool。
# 模块用途: 让 my-agent 把学来的方法或自己写的小工具打成可安装的包，并告诉用户接下来怎么装。
from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from ..capability.learnpack_build_notes import build_notes
from ..capability.learnpack_store import BuildProvenance, BuildRecord, LearnpackStore
from ..capability.package_build import (
    KIND_CAPABILITY_PACK,
    KIND_PLUGIN,
    BuiltPackage,
    PackageBuildError,
    build_capability_pack,
    build_files_plugin,
    capability_pack_paths,
    files_plugin_paths,
    read_declared_files,
)
from ..capability.self_install_switches import read_self_install_switches, switch_facts
from ..common.log_redaction import redact_sensitive_text
from ..plugin_manifest import PluginPackageError
from ..user_space.owner_access import is_complete_local_admin_owner
from .models import (
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ToolHandlerOutcome,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)

PACKAGE_BUILD_TOOL = "package_build"
# 能力包省略 files 时自动收录目录里的普通文件，最多这么多个（与能力包协议上限一致）。
_MAX_LISTED_FILE_COUNT = 4095
# 回执里最多列出的包内文件名个数，超出只给总数。
_RECEIPT_FILE_LIMIT_COUNT = 40
# 能力包声明省略 settings_schema 时用的空设置结构。
_EMPTY_SETTINGS = {"type": "object", "properties": {}, "additionalProperties": False}
# 文件型插件第一期不收的 v8 声明键（事件订阅、收紧钩子、权限需求）。
_V8_KEYS = ("events", "tool_gates", "permissions")
# 她声明的来源与许可证会进宿主回执和 /plugins# 列表：单行、最多 200 个字符。
_PROVENANCE_MAX_CHARS = 200
# 宿主校验失败时带给她的具体原因最多这么多个字符（单行）。
_CAUSE_MAX_CHARS = 200
# 声明里其它字符串（说明、关键词、动作说明等）每段最多 1000 个字符。
_DECLARED_TEXT_MAX_CHARS = 1000
# 版本号只用字母、数字和 . + - _，最多 64 个字符（它会进宿主回执的"包名 版本（摘要）"）。
_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}")
# 展示文字里不收的 Unicode 类别：控制字符（含换行、ESC、C1）、格式字符（含改显示方向的）、行与段落分隔符、单独的代理字符。
_UNSAFE_TEXT_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs"})


# LLM: 一次打包请求的结构化输入；declaration 是模型给的声明（能力包可省 files/settings_schema）；source_dir 是她写的路径，
#   相对路径到读文件时才按 read_file 的当前工作目录解析（_source_root）。
# 类用途: 解析后的打包请求。
@dataclass(frozen=True)
class _BuildRequest:
    kind: str
    source_dir: Path
    declaration: dict
    provenance: BuildProvenance


# LLM: 只认本机管理员（构造时不判断，执行时用 agent.home_paths 判断）；读权限借用同一注册表的 read_file 裁决。
# 类用途: 把整理好的目录打成包并存进宿主 learnpack 存储。
class PackageBuildTool(BaseTool):
    model_spec = ToolModelSpec(
        name=PACKAGE_BUILD_TOOL,
        description=(
            "把整理好的目录打成能力包（kind=capability_pack，方法、模板、检查清单、离线小工具）或文件型插件包"
            "（kind=plugin，要运行的程序/工具）。只打包、不安装：成功后回执给出 sha256、两个自动装开关的当前状态、"
            "开关命令原文和下一步（用 package_install 传这个 sha256 安装）。source_dir 写目录路径：写文件回执里的相对路径"
            "按当前工作目录算，也可以写绝对路径；目录要在你读得到的地方（任务工作区即可）；"
            "能力包 declaration 至少写 plugin_id、version、summary、capability（description、keywords、entry_document），"
            "files 可省（自动收录目录里的普通文件，隐藏文件与根目录 declaration.json 除外）；插件 declaration 要写全（不读目录里的"
            " declaration.json）：plugin_id、version、summary、entry、files（每项 path 与 executable）、platforms、actions、"
            "default_action、tools、settings_schema，照内置技能 write-my-agent-plugin 的模板写，不带 events/tool_gates/permissions。"
            "origin 写学自哪里（仓库地址@提交，或“自己写的”），license 写来源的许可证。"
            "文件里有密钥或模型读不到的文件会整包拒绝。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": [KIND_CAPABILITY_PACK, KIND_PLUGIN]},
                "source_dir": {"type": "string", "description": "要打包的目录：写文件回执里的相对路径，或绝对路径"},
                "declaration": {"type": "object", "description": "包声明（不含摘要和协议版本）"},
                "origin": {"type": "string", "description": "学自哪里：仓库地址@提交，或“自己写的”"},
                "license": {"type": "string", "description": "来源的许可证，如 MIT、Apache-2.0；自己写的填“自有”"},
            },
            "required": ["kind", "source_dir", "declaration", "origin", "license"],
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="capability",
            use_cases=("学完外部 agent 后把方法做成能力包", "把自己写的离线小工具或服务做成插件包"),
            avoid_when=("只是普通写文件或给用户交付文档",),
            keywords=("能力包", "插件", "打包", "learnpack"),
            default_deferred=True,
            deferred_summary="把整理好的能力包或插件目录打成包（不安装），回执给出包指纹、开关状态和怎么装",
        ),
    )
    # 打包只写宿主 learnpack 存储（内容寻址，重放无害）；按原操作账去重。
    runtime_policy = ToolRuntimePolicy(effect_resolver=EffectResolverPolicy("mutating"),
                                       idempotency_policy=IdempotencyPolicy("operation"))
    # 本轮执行目录：注册表每次调用按当前写边界给工具副本设置（与读写文件工具同一来源，见 registry_invoke 的边界工具名单）；
    # 直接调用（没经注册表）时为空，退回 read_file 的基准目录。
    workspace_root: Path | None = None

    # LLM: 构造不读写；身份、读权限与开关都在执行时现取。
    # 函数用途: 绑定当前 owner 的 agent（取身份、owner 目录、注册表与开关）。
    def __init__(self, agent: object) -> None:
        self._agent = agent

    # LLM: 失败都给登记过的 PACKAGE_BUILD_* 码且没有写任何东西；成功时只写宿主 learnpack 存储。有写文件副作用。
    # 函数用途: 校验、读文件、打包、存储并返回回执。
    def execute(self, params: dict) -> ToolHandlerOutcome:
        home = getattr(self._agent, "home_paths", None)
        if not is_complete_local_admin_owner(home):
            return _error("TOOL_PERMISSION_DENIED", "打包与自己装包第一期只对本机管理员开放。")
        try:
            request = _parse(params, _run_id(self._agent))
            contents = _read_files(self._agent, request, self.workspace_root)
            built = _build(request, contents)
        except PackageBuildError as exc:
            return _error(exc.code, str(exc))
        except PluginPackageError as exc:
            return _error("PACKAGE_BUILD_OUTPUT_INVALID", f"打出的包没通过宿主校验：{exc}{_cause_text(exc)}")
        store = LearnpackStore(home.owner_home_dir)
        try:
            record = store.save_build(built, request.provenance)
        except OSError:
            return _error("PACKAGE_BUILD_STORE_FAILED", "包已打好但没能存进宿主存储，请稍后重试。")
        return _receipt(record, built, switch_facts(read_self_install_switches(self._agent)),
                        build_notes(self._agent, store, record))


# LLM: 只做结构检查与默认值填充，不读文件；source_dir 不能为空（相对路径留到读文件时解析）；插件不收 v8 键。纯函数。
# 函数用途: 把工具参数整理成 _BuildRequest。
def _parse(params: dict, run_id: str) -> _BuildRequest:
    kind = str(params.get("kind") or "")
    declaration = params.get("declaration")
    raw_dir = str(params.get("source_dir") or "")
    if kind not in {KIND_CAPABILITY_PACK, KIND_PLUGIN} or not isinstance(declaration, dict):
        raise PackageBuildError("PACKAGE_BUILD_DECLARATION_INVALID", "kind 只能是 capability_pack 或 plugin，declaration 必须是对象。")
    if not raw_dir.strip():
        raise PackageBuildError("PACKAGE_BUILD_SOURCE_DENIED", "source_dir 要写要打包的目录（用写文件回执里的路径）。")
    if kind == KIND_PLUGIN and any(key in declaration for key in _V8_KEYS):
        raise PackageBuildError("PACKAGE_BUILD_KIND_UNSUPPORTED",
                                "第一期她自己做的插件只支持文件型 v6 包，声明里不能有 events、tool_gates、permissions。")
    if not isinstance(declaration.get("version"), str) or _VERSION.fullmatch(declaration["version"]) is None:
        raise PackageBuildError("PACKAGE_BUILD_DECLARATION_INVALID", "version 只能用字母、数字和 . + - _，最多 64 个字符。")
    if not _declared_texts_ok(declaration):
        raise PackageBuildError("PACKAGE_BUILD_DECLARATION_INVALID",
                                f"声明里的文字每段最多 {_DECLARED_TEXT_MAX_CHARS} 个字符，不能有换行、控制字符或改变显示方向的格式字符。")
    origin, license_name = str(params.get("origin") or "").strip(), str(params.get("license") or "").strip()
    if not (_display_text_ok(origin) and _display_text_ok(license_name)):
        raise PackageBuildError("PACKAGE_BUILD_DECLARATION_INVALID",
                                f"origin（学自哪里）和 license（许可证）都要写，各自一行、不超过 {_PROVENANCE_MAX_CHARS} "
                                "个字符，不能有换行、控制字符或改变显示方向的格式字符。")
    if kind == KIND_CAPABILITY_PACK:
        declaration = {"settings_schema": _EMPTY_SETTINGS, **declaration}
    return _BuildRequest(kind, Path(raw_dir).expanduser(), declaration, BuildProvenance(origin, license_name, run_id))


# LLM: 比包清单展示文字（plugin_manifest._text：非空、无控制字符）更严：再挡 C1 控制、格式字符（如改显示方向）、行/段落分隔符和
#   单独的代理字符，并限长。她声明的来源与许可证会被宿主原样放进回执和列表，这样造不出"像宿主写的"额外几行，也不会把控制字符
#   送到终端（复审 5、6 轮）。纯函数。
# 函数用途: 判断来源或许可证文字能不能安全展示（必须非空）。
def _display_text_ok(value: str) -> bool:
    return bool(value) and _safe_text(value, _PROVENANCE_MAX_CHARS)


# LLM: 同 _display_text_ok 的字符规则，允许空串（声明里有可留空的字段）；只看长度与 Unicode 类别。纯函数。
# 函数用途: 判断一段文字能不能安全展示。
def _safe_text(value: str, limit: int) -> bool:
    return len(value) <= limit and not any(unicodedata.category(char) in _UNSAFE_TEXT_CATEGORIES for char in value)


# LLM: 声明里所有字符串（键和值，含版本、说明、关键词、动作与参数说明）都是她写的，宿主会拿去展示（回执、/plugins info、
#   列表），所以逐个按展示规则检查，而不是挑字段补（复审 6 轮）。迭代展开，不递归。纯函数。
# 函数用途: 检查声明里的每一段文字都能安全展示。
def _declared_texts_ok(declaration: dict) -> bool:
    pending, texts = [declaration], []
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend([*value.keys(), *value.values()])
        elif isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, str):
            texts.append(value)
    return all(_safe_text(text, _DECLARED_TEXT_MAX_CHARS) for text in texts)


# LLM: 能力包没列 files 时自动收录，收录到的文件名也按声明文字同一条展示规则检查（复审 7 轮）；每个路径先过 read_file 的路径
#   裁决再按无链接读取；读完做密钥检查。只读文件。
# 函数用途: 读出要打进包的文件字节。
def _read_files(agent: object, request: _BuildRequest, base: Path | None) -> dict[str, bytes]:
    check = _path_check(agent)
    root = _source_root(agent, request.source_dir, base).resolve(strict=False)
    _require_allowed(check, root, str(request.source_dir))
    if request.kind == KIND_CAPABILITY_PACK and "files" not in request.declaration:
        listed = _list_files(root)
        if not all(_safe_text(path, _DECLARED_TEXT_MAX_CHARS) for path in listed):
            raise PackageBuildError("PACKAGE_BUILD_DECLARATION_INVALID",
                                    "目录里有文件名带换行、控制字符或改变显示方向的字符，改名后再打包。")
        request.declaration["files"] = [{"path": path} for path in listed]
    paths = (capability_pack_paths(request.declaration) if request.kind == KIND_CAPABILITY_PACK
             else files_plugin_paths(request.declaration))
    for path in paths:
        _require_allowed(check, root / path, path)
    contents = read_declared_files(root, paths)
    _reject_secrets(contents)
    return contents


# LLM: 打包规则只在 capability/package_build；这里只按类型分派。纯函数。
# 函数用途: 按类型调用唯一的打包实现。
def _build(request: _BuildRequest, contents: dict[str, bytes]) -> BuiltPackage:
    if request.kind == KIND_CAPABILITY_PACK:
        return build_capability_pack(request.declaration, contents)
    return build_files_plugin(request.declaration, contents)


# LLM: 相对路径和读文件、写文件工具同一口径：base 是注册表给本次调用的执行目录（复审：不能用注册表构造时的基准目录，
#   会话带自己的执行目录时两者不同）；直接调用没有 base 时才退回 read_file 的基准目录。拿不到就拒绝（fail closed）。
#   只拼路径，权限仍由 _require_allowed 裁决。
# 函数用途: 把她写的 source_dir 落成绝对路径。
def _source_root(agent: object, source_dir: Path, base: Path | None) -> Path:
    if source_dir.is_absolute():
        return source_dir
    if base is None:
        tools = getattr(getattr(agent, "tools", None), "tools", None)
        base = getattr(tools.get("read_file") if isinstance(tools, dict) else None, "workspace_root", None)
    if base is None:
        raise PackageBuildError("PACKAGE_BUILD_SOURCE_DENIED", "当前拿不到工作目录，source_dir 请写绝对路径。")
    return Path(base) / source_dir


# LLM: 读权限的唯一来源是同一注册表里 read_file 的 check_path_access；拿不到就拒绝（fail closed）。只读。
# 函数用途: 取得当前 owner 的读路径裁决函数。
def _path_check(agent: object):
    tools = getattr(getattr(agent, "tools", None), "tools", None)
    reader = tools.get("read_file") if isinstance(tools, dict) else None
    check = getattr(reader, "check_path_access", None)
    if not callable(check):
        raise PackageBuildError("PACKAGE_BUILD_SOURCE_DENIED", "当前拿不到读文件权限裁决，不能打包。")
    return check


# LLM: 裁决结果只看结构化 allowed 字段，拒绝时只报路径不报内容。只读。
# 函数用途: 某个路径读权限不允许时，抛 PACKAGE_BUILD_SOURCE_DENIED。
def _require_allowed(check, path: Path, shown: str) -> None:
    if not check(path.resolve(strict=False)).allowed:
        raise PackageBuildError("PACKAGE_BUILD_SOURCE_DENIED", f"没有权限读取：{shown}（她自己读不到的文件不能打进包）")


# LLM: 不跟随链接地遍历目录：隐藏文件/目录和根目录的 declaration.json 跳过；遇到链接整包拒绝（不静默漏文件）。只读。
# 函数用途: 列出能力包要自动收录的相对路径（排序）。
def _list_files(root: Path) -> list[str]:
    listed: list[str] = []
    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(name for name in directories if not name.startswith("."))
        if any(os.path.islink(os.path.join(current, name)) for name in [*directories, *files]):
            raise PackageBuildError("PACKAGE_BUILD_FILE_INVALID", "目录里有链接；请换成普通文件再打包。")
        relative = Path(current).relative_to(root)
        listed += [(relative / name).as_posix() for name in files if not name.startswith(".")]
    listed = sorted(path for path in listed if path != "declaration.json")
    if not 1 <= len(listed) <= _MAX_LISTED_FILE_COUNT:
        raise PackageBuildError("PACKAGE_BUILD_FILE_INVALID", f"目录里要有 1 到 {_MAX_LISTED_FILE_COUNT} 个普通文件。")
    return listed


# LLM: 只对能按 UTF-8 解码的文件做检查，复用日志脱敏的同一套密钥识别；命中就整包拒绝并只报文件名（不回显内容）。只读。
# 函数用途: 拒绝带密钥的文件进包。
def _reject_secrets(contents: dict[str, bytes]) -> None:
    flagged = []
    for path, content in contents.items():
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if redact_sensitive_text(text, code_file=True) != text:
            flagged.append(path)
    if flagged:
        raise PackageBuildError("PACKAGE_BUILD_SECRET_FOUND",
                                f"这些文件里像是有密钥或口令，已整包拒绝：{'、'.join(flagged[:10])}；换成占位符后再打包。")


# LLM: 包是用她自己的声明打出来的，校验器的内部原因（如"包描述字段不完整或存在未知字段"、KeyError 的字段名）直接给她改声明用；
#   只取异常链上一层的类型名和文字，压成一行、限长，按展示规则过滤（不合规就不带）。纯函数。
# 函数用途: 把宿主校验失败的具体原因接在错误回执后面。
def _cause_text(exc: BaseException) -> str:
    cause = exc.__cause__
    if cause is None:
        return ""
    text = " ".join(f"{type(cause).__name__}: {cause}".split())[:_CAUSE_MAX_CHARS]
    return f"（具体原因：{text}）" if _safe_text(text, _CAUSE_MAX_CHARS) else ""


# LLM: 回执字段是给模型照抄的结构化事实；next_step 写清用 package_install 装、开关关着时会给用户确认行；notes 来自
#   capability/learnpack_build_notes：现在装着的版本、她做过的版本，以及软提醒 warnings（code + 一句话，没有就是空列表，
#   不挡打包）。纯函数。
# 函数用途: 生成成功回执。
def _receipt(record: BuildRecord, built: BuiltPackage, switches: dict[str, object],
             notes: dict[str, object]) -> ToolHandlerOutcome:
    shown = list(built.files[:_RECEIPT_FILE_LIMIT_COUNT])
    payload = {
        "ok": True,
        "build": {"kind": record.kind, "package_id": record.package_id, "version": record.version,
                  "sha256": record.sha256, "file_count": record.file_count, "files": shown,
                  "files_truncated": len(built.files) > len(shown), "origin": record.origin, "license": record.license},
        "switches": switches,
        "installed_version": notes["installed_version"],
        "installed_by_me": notes["installed_by_me"],
        "previous_versions": notes["previous_versions"],
        "warnings": notes["warnings"],
        "next_step": (f"调用 package_install，参数 sha256={record.sha256} 安装。对应开关开着就直接装上；"
                      "关着会生成待确认安装单，回执里有要用户原样发的那一行确认。照回执原文提醒用户。"),
    }
    return ToolHandlerOutcome(PACKAGE_BUILD_TOOL, True, json.dumps(payload, ensure_ascii=False),
                              result_envelope={PACKAGE_BUILD_TOOL: payload})


# LLM: 失败时一律没写任何东西（not_started）。纯组装。
# 函数用途: 生成失败回执（没有写任何东西）。
def _error(code: str, message: str) -> ToolHandlerOutcome:
    return ToolHandlerOutcome(PACKAGE_BUILD_TOOL, False, message, error_code=code, effect_outcome="not_started")


# LLM: 只读线程本地的当前运行参数，用于打包记录追溯，不参与权限判断。
# 函数用途: 取当前运行编号（取不到为空串）。
def _run_id(agent: object) -> str:
    return str(getattr(getattr(agent, "_current_run_params", None), "run_id", "") or "")


__all__ = ["PACKAGE_BUILD_TOOL", "PackageBuildTool"]
