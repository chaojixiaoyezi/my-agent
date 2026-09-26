# LLM: 本模块只从当前受限 Skill 快照解析精确包成员，沿原 task pins 固定身份；不安装、不执行、不新建资源状态账。
# 模块用途: 把已授权能力包的原始资源交给现有文件写工具，确保模型不用复制或改写脚本正文。
from __future__ import annotations

from collections.abc import Mapping

from ..capability_package_manifest import validate_capability_path
from ..tooling.content_transport_policy import FileSourceContent, FileSourceUnavailableError
from .package_snapshot import CapabilityPackageSnapshot
from .skill_snapshot import SkillSnapshotError
from .task_references import normalize_skill_reference, pin_package_reference

_PACKAGE_FIELDS = frozenset({"kind", "stable_id", "name", "source", "content_sha256", "package_id", "activation_id"})
_RESOURCE_FIELDS = _PACKAGE_FIELDS | {"resource_path", "resource_sha256"}


# LLM: 引用只投影快照声明，不能授予包或目标路径权限；调用方必须保留全部字段，不缩成包名。
# 函数用途: 为 skill_search 的文本或二进制成员生成可原样交给 write_file 的来源引用。
def package_resource_reference(package: CapabilityPackageSnapshot, member_path: str) -> dict[str, str]:
    member = package.resolve(member_path)
    if member is None:
        raise ValueError("CAPABILITY_RESOURCE_NOT_AVAILABLE")
    return {**package.to_ref(), "resource_path": member.path, "resource_sha256": member.sha256}


# LLM: 只接受已有包引用合同加精确成员路径/摘要；不忽略额外 owner、run 或旧代次字段，也不做文本猜测。
# 函数用途: 检查模型原样提交的资源引用，拒绝缺字段、身份矛盾、危险路径和错误摘要。
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
