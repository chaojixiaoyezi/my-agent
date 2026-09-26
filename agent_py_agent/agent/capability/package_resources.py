# LLM: 本模块声明唯一资源引用字段，并从当前受限快照解析包成员、沿原 task pins 固定身份；不安装、不执行、不新建资源状态账。
# 模块用途: 为模型和原写工具提供同源引用声明与原始资源字节，避免手抄脚本或隐式补全身份。
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

from ..capability_package_manifest import validate_capability_path
from ..tooling.content_transport_policy import FileSourceContent, FileSourceUnavailableError
from .package_snapshot import CapabilityPackageSnapshot
from .skill_snapshot import SkillSnapshotError
from .task_references import normalize_skill_reference, pin_package_reference

_RESOURCE_PROPERTIES = {
    "kind": {"type": "string", "const": "capability_package", "description": "来源类型，保留原值。"},
    "stable_id": {"type": "string", "minLength": 1, "description": "原引用的稳定能力标识，不自行拼接。"},
    "name": {"type": "string", "minLength": 1, "description": "原引用的展示名称，等于 package_id，也必须完整保留。"},
    "source": {"type": "string", "const": "capability_package", "description": "来源域，保留原值。"},
    "content_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$", "description": "原引用的整包字节摘要。"},
    "package_id": {"type": "string", "minLength": 1, "description": "当前已授权能力包的精确标识。"},
    "activation_id": {"type": "string", "pattern": "^[0-9a-f]{64}$", "description": "原引用固定的激活代次，不能改成新代次。"},
    "resource_path": {"type": "string", "minLength": 1, "description": "包清单声明的完整成员路径，不是工作区路径。"},
    "resource_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$", "description": "原引用的完整成员字节摘要。"},
}
_RESOURCE_FIELDS = frozenset(_RESOURCE_PROPERTIES)
_PACKAGE_FIELDS = _RESOURCE_FIELDS - {"resource_path", "resource_sha256"}


# LLM: properties 是资源字段的唯一声明，required 与解析器键集合由它派生；返回隔离副本供 core 注入，不替代权限和鲜活代次复核。
# 函数用途: 让模型与原参数门看到完整资源引用格式，缺字段直接报告准确位置，而不是进入写工具后猜错。
def package_resource_reference_schema() -> dict[str, object]:
    return {
        "type": "object",
        "description": "原样使用 skill_search 返回的完整 source_ref 对象，精确复制完整资源（不是正文预览）；不要自行拼字段或把归档地址当来源。只支持 overwrite，不授予脚本执行权。",
        "properties": deepcopy(_RESOURCE_PROPERTIES),
        "required": list(_RESOURCE_PROPERTIES),
        "additionalProperties": False,
    }


# LLM: 引用只投影快照声明，不能授予包或目标路径权限；调用方必须保留全部字段，不缩成包名。
# 函数用途: 为 skill_search 的文本或二进制成员生成可原样交给 write_file 的来源引用。
def package_resource_reference(package: CapabilityPackageSnapshot, member_path: str) -> dict[str, str]:
    member = package.resolve(member_path)
    if member is None:
        raise ValueError("CAPABILITY_RESOURCE_NOT_AVAILABLE")
    return {**package.to_ref(), "resource_path": member.path, "resource_sha256": member.sha256}


# LLM: 严格键集合来自同一资源 schema；已有包身份、精确成员路径/摘要仍独立复核，不忽略额外 owner、run 或旧代次，也不猜字段。
# 函数用途: 检查原样提交的资源引用，直接内部调用同样拒绝缺字段、身份矛盾、危险路径和错误摘要。
def _validated_reference(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != _RESOURCE_FIELDS:
        raise ValueError("CAPABILITY_RESOURCE_REFERENCE_INVALID")
    package_ref = normalize_skill_reference(value)
    if package_ref.get("kind") != "capability_package" or any(value[key] != package_ref[key] for key in _PACKAGE_FIELDS):
        raise ValueError("CAPABILITY_RESOURCE_REFERENCE_INVALID")
    path, digest = value["resource_path"], value["resource_sha256"]
    if not isinstance(path, str) or not isinstance(digest, str):
        raise ValueError("CAPABILITY_RESOURCE_REFERENCE_INVALID")
    validate_capability_path(path)
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("CAPABILITY_RESOURCE_REFERENCE_INVALID")
    return {**package_ref, "resource_path": path, "resource_sha256": digest}


# LLM: agent 是 composition root 绑定的真实主体；只用 current_skill_snapshot 的 owner/child 范围，参数不能指定身份或扩大允许包。
# 函数用途: 复核完整来源、读取原始字节并固定原任务引用；不创建目标目录，不启动脚本。
def resolve_package_resource(agent: object, source_ref: object) -> FileSourceContent:
    reference = _validated_reference(source_ref)
    provider = getattr(agent, "current_skill_snapshot", None)
    if not callable(provider):
        raise FileSourceUnavailableError("CAPABILITY_SNAPSHOT_UNAVAILABLE")
    try:
        snapshot = provider()
        if snapshot is None:
            raise SkillSnapshotError("CAPABILITY_SNAPSHOT_UNAVAILABLE")
        package = snapshot.resolve_package(reference["package_id"])
        if package is None:
            raise SkillSnapshotError("CAPABILITY_PACKAGE_NOT_AVAILABLE")
        if package_resource_reference(package, reference["resource_path"]) != reference:
            raise SkillSnapshotError("CAPABILITY_RESOURCE_REFERENCE_STALE")
        content = snapshot.read_in_package(package.package_id, reference["resource_path"])
        pin_package_reference(agent, package.to_ref())
        return FileSourceContent(content, reference)
    except (OSError, ValueError, SkillSnapshotError) as exc:
        raise FileSourceUnavailableError(str(exc)) from exc
