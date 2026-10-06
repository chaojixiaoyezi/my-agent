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
from dataclasses import dataclass
from pathlib import Path

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


# LLM: 一次打包请求的结构化输入；declaration 是模型给的声明（能力包可省 files/settings_schema），source_dir 是绝对路径。
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
            "开关命令原文和下一步（用 package_install 传这个 sha256 安装）。source_dir 用绝对路径，目录要在你读得到的"
            "地方（任务工作区即可）；"
            "能力包 declaration 至少写 plugin_id、version、summary、capability（description、keywords、entry_document），"
            "files 可省（自动收录目录里的普通文件，隐藏文件与根目录 declaration.json 除外）；插件 declaration 必须列 files"
            "（每项 path 与 executable）。origin 写学自哪里（仓库地址@提交，或“自己写的”），license 写来源的许可证。"
            "文件里有密钥或模型读不到的文件会整包拒绝。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": [KIND_CAPABILITY_PACK, KIND_PLUGIN]},
                "source_dir": {"type": "string", "description": "要打包的目录绝对路径"},
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
            contents = _read_files(self._agent, request)
            built = _build(request, contents)
        except PackageBuildError as exc:
            return _error(exc.code, str(exc))
        except PluginPackageError as exc:
            return _error("PACKAGE_BUILD_OUTPUT_INVALID", f"打出的包没通过宿主校验：{exc}")
        try:
            record = LearnpackStore(home.owner_home_dir).save_build(built, request.provenance)
        except OSError:
            return _error("PACKAGE_BUILD_STORE_FAILED", "包已打好但没能存进宿主存储，请稍后重试。")
        return _receipt(record, built, switch_facts(read_self_install_switches(self._agent)))


# LLM: 只做结构检查与默认值填充，不读文件；source_dir 必须是绝对路径；插件不收 v8 键。纯函数。
# 函数用途: 把工具参数整理成 _BuildRequest。
def _parse(params: dict, run_id: str) -> _BuildRequest:
    kind = str(params.get("kind") or "")
    declaration = params.get("declaration")
    raw_dir = str(params.get("source_dir") or "")
    if kind not in {KIND_CAPABILITY_PACK, KIND_PLUGIN} or not isinstance(declaration, dict):
        raise PackageBuildError("PACKAGE_BUILD_DECLARATION_INVALID", "kind 只能是 capability_pack 或 plugin，declaration 必须是对象。")
    if not raw_dir or not Path(raw_dir).expanduser().is_absolute():
        raise PackageBuildError("PACKAGE_BUILD_SOURCE_DENIED", "source_dir 要写绝对路径（用写文件回执里的路径）。")
    if kind == KIND_PLUGIN and any(key in declaration for key in _V8_KEYS):
        raise PackageBuildError("PACKAGE_BUILD_KIND_UNSUPPORTED",
                                "第一期她自己做的插件只支持文件型 v6 包，声明里不能有 events、tool_gates、permissions。")
    origin, license_name = str(params.get("origin") or "").strip(), str(params.get("license") or "").strip()
    if not origin or not license_name:
        raise PackageBuildError("PACKAGE_BUILD_DECLARATION_INVALID", "origin（学自哪里）和 license（许可证）都要写。")
    if kind == KIND_CAPABILITY_PACK:
        declaration = {"settings_schema": _EMPTY_SETTINGS, **declaration}
    return _BuildRequest(kind, Path(raw_dir).expanduser(), declaration, BuildProvenance(origin, license_name, run_id))


# LLM: 能力包没列 files 时自动收录；每个路径先过 read_file 的路径裁决再按无链接读取；读完做密钥检查。只读文件。
# 函数用途: 读出要打进包的文件字节。
def _read_files(agent: object, request: _BuildRequest) -> dict[str, bytes]:
    check = _path_check(agent)
    root = request.source_dir.resolve(strict=False)
    _require_allowed(check, root, str(request.source_dir))
    if request.kind == KIND_CAPABILITY_PACK and "files" not in request.declaration:
        listed = _list_files(root)
        request.declaration["files"] = [{"path": path} for path in listed]
    paths = (capability_pack_paths(request.declaration) if request.kind == KIND_CAPABILITY_PACK
             else files_plugin_paths(request.declaration))
    for path in paths:
        _require_allowed(check, root / path, path)
    contents = read_declared_files(root, paths)
    _reject_secrets(contents)
    return contents


# 函数用途: 按类型调用唯一的打包实现。
def _build(request: _BuildRequest, contents: dict[str, bytes]) -> BuiltPackage:
    if request.kind == KIND_CAPABILITY_PACK:
        return build_capability_pack(request.declaration, contents)
    return build_files_plugin(request.declaration, contents)


# LLM: 读权限的唯一来源是同一注册表里 read_file 的 check_path_access；拿不到就拒绝（fail closed）。只读。
# 函数用途: 取得当前 owner 的读路径裁决函数。
def _path_check(agent: object):
    tools = getattr(getattr(agent, "tools", None), "tools", None)
    reader = tools.get("read_file") if isinstance(tools, dict) else None
    check = getattr(reader, "check_path_access", None)
    if not callable(check):
        raise PackageBuildError("PACKAGE_BUILD_SOURCE_DENIED", "当前拿不到读文件权限裁决，不能打包。")
    return check


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


# LLM: 回执字段是给模型照抄的结构化事实；next_step 写清用 package_install 装、开关关着时会给用户确认行。纯函数。
# 函数用途: 生成成功回执。
def _receipt(record: BuildRecord, built: BuiltPackage, switches: dict[str, object]) -> ToolHandlerOutcome:
    shown = list(built.files[:_RECEIPT_FILE_LIMIT_COUNT])
    payload = {
        "ok": True,
        "build": {"kind": record.kind, "package_id": record.package_id, "version": record.version,
                  "sha256": record.sha256, "file_count": record.file_count, "files": shown,
                  "files_truncated": len(built.files) > len(shown), "origin": record.origin, "license": record.license},
        "switches": switches,
        "next_step": (f"调用 package_install，参数 sha256={record.sha256} 安装。对应开关开着就直接装上；"
                      "关着会生成待确认安装单，回执里有要用户原样发的那一行确认。照回执原文提醒用户。"),
    }
    return ToolHandlerOutcome(PACKAGE_BUILD_TOOL, True, json.dumps(payload, ensure_ascii=False),
                              result_envelope={PACKAGE_BUILD_TOOL: payload})


# 函数用途: 生成失败回执（没有写任何东西）。
def _error(code: str, message: str) -> ToolHandlerOutcome:
    return ToolHandlerOutcome(PACKAGE_BUILD_TOOL, False, message, error_code=code, effect_outcome="not_started")


# 函数用途: 取当前运行编号（取不到为空串）。
def _run_id(agent: object) -> str:
    return str(getattr(getattr(agent, "_current_run_params", None), "run_id", "") or "")


__all__ = ["PACKAGE_BUILD_TOOL", "PackageBuildTool"]
