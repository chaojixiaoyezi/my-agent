# LLM: 本模块共用包正文的原字节读取、字符分页及原 task pin；调用方先完成工具准入或宿主准备准入，并传本轮受限快照。
# 只读声明成员，沿 snapshot.read_in_package 核 owner/摘要/激活代次；不追链接、不执行资源、不生成工具回执或第二份状态。
# 宿主回调只能复核当前执行权，不能回读 TaskStore；原 pin 在任务锁内再次消费同一回调，停止后的结果不得迟到固定引用。
# 模块用途: 为原 skill_search get 和宿主入口加载提供同一页正文与准确来源，权限、总预算和展示归各自调用方。
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial

from ..common.cancellation import raise_if_cancelled
from ..settings.defaults import default_agent_config
from .package_resources import package_resource_reference
from .package_snapshot import CapabilityPackageSnapshot
from .skill_snapshot import SkillSnapshot, SkillSnapshotError


# LLM: 宿主将已选版本与只读回调传入；不保存或授予权限，执行检查可在原task锁内重复消费，版本不符必须拒绝。
# 类用途: 将当前页的版本核对、执行权复核和交付前验收一起传入共享读取入口。
@dataclass(frozen=True)
class PackagePageChecks:
    execution_authority_check: Callable[[], None] | None = None
    validate_page: Callable[[dict[str, object]], None] | None = None
    expected_package_sha256: str | None = None
    expected_activation_id: str | None = None


# LLM: continuation 只绑定声明身份，不读取成员或授予访问；search/get 共用原字段顺序和激活代次，修改须验证原工具回执。
# 函数用途: 生成继续检索或读取同一包的结构化参数，避免旧页码静默套到同名新安装。
def package_continuation(package: CapabilityPackageSnapshot, action: str, offset: int) -> dict[str, object]:
    return {"action": action, "package_id": package.package_id, "offset": offset,
            "expected_package_sha256": package.package_sha256, "expected_activation_id": package.activation_id}


# LLM: 只比较显式提供的版本字段并按固定顺序返回字段名，不回显期望值；不存在的字段保留原首次读取语义，不补猜代次。
# 函数用途: 列出与当前包身份不符的版本字段，模型工具据此给参数纠错，宿主读取据此拒绝。
def continuation_mismatches(package: CapabilityPackageSnapshot, params: Mapping[str, object]) -> list[str]:
    return [key for key, expected in (("expected_package_sha256", package.package_sha256),
                                      ("expected_activation_id", package.activation_id))
            if key in params and params[key] != expected]


# LLM: 宿主已选版本与当前包不符时仍是快照失效；模型工具改走 continuation_mismatches 的参数纠错，两边不得混用。
# 函数用途: 在宿主共享正文入口读取前拒绝来自旧版本或旧激活的页请求。
def validate_package_continuation(package: CapabilityPackageSnapshot, params: Mapping[str, object]) -> None:
    if continuation_mismatches(package, params):
        raise SkillSnapshotError("SKILL_SNAPSHOT_STALE")


# LLM: 调用方必须传当前 owner/child 范围的冻结快照；宿主只选 entry_document 并分配总预算，reader 不扩大 snapshot 或创建任务。
# 原工具先过 ToolExecutor，宿主先过自身 claim/权限门；页验收只能检查/投影当前页，失败须抛出且不得写文件或更改原payload。
# 取消和执行权在读取前后、页验收后及原任务锁内复查，无副作用读取失败不交付正文或pin。
# 函数用途: 返回一页原 UTF-8 文本、完整九字段来源及续读参数，成功交付前仅写原任务引用，不写业务文件或执行脚本。
def read_package_page(
    agent: object,
    snapshot: SkillSnapshot,
    package_id: str,
    *,
    resource_path: str = "",
    offset: int = 0,
    max_chars: int | None = None,
    checks: PackagePageChecks | None = None,
) -> dict[str, object]:
    from .task_references import pin_package_reference

    checks = checks or PackagePageChecks()
    check = partial(_check_read_authority, checks.execution_authority_check)
    check()
    package = snapshot.resolve_package(package_id)
    if package is None:
        raise SkillSnapshotError("CAPABILITY_PACKAGE_NOT_AVAILABLE")
    validate_package_continuation(package, {
        key: value for key, value in (("expected_package_sha256", checks.expected_package_sha256),
                                     ("expected_activation_id", checks.expected_activation_id)) if value is not None
    })
    if type(offset) is not int or offset < 0:
        raise ValueError("offset 必须为非负整数")
    member = package.resolve(resource_path)
    if member is None:
        raise SkillSnapshotError("CAPABILITY_RESOURCE_NOT_AVAILABLE")
    raw = snapshot.read_in_package(package.package_id, member.path)
    check()
    try:
        body = raw.decode("utf-8")
    except UnicodeError as exc:
        raise SkillSnapshotError("CAPABILITY_RESOURCE_NOT_TEXT") from exc
    config = getattr(agent, "config", None) or default_agent_config()
    limit = max(1, int(config.tool_read_max_chars))
    requested = limit if max_chars is None else max_chars
    if type(requested) is not int or requested < 0:
        raise ValueError("max_chars 必须为非负整数")
    if requested == 0 or offset > len(body):
        raise ValueError("max_chars 必须大于零，offset 不能超过正文长度")
    limit = min(requested, limit)
    window = body[offset:offset + limit]
    next_offset = offset + len(window)
    payload = {"source_ref": package_resource_reference(package, member.path),
               "kind": "capability_package", **package.to_ref(), "resource_path": member.path,
               "resource_sha256": member.sha256, "body": window, "offset": offset,
               "total_chars": len(body), "has_more": next_offset < len(body)}
    if payload["has_more"]:
        payload["continuation"] = {**package_continuation(package, "get", next_offset),
                                   "resource_path": member.path, "max_chars": limit}
    if checks.validate_page is not None:
        checks.validate_page(payload)
    check()
    if checks.execution_authority_check is None:
        pin_package_reference(agent, package.to_ref())
    else:
        pin_package_reference(agent, package.to_ref(), execution_authority_check=check)
    return payload


# LLM: 复用原取消令牌和调用方的只读执行权检查，不解析消息文本或读取 task 锁；同一闭包可以安全传入原 pin。
# 函数用途: 在包读取和引用提交边界阻止停止或换轮后的迟到结果。
def _check_read_authority(execution_authority_check: Callable[[], None] | None) -> None:
    raise_if_cancelled()
    if execution_authority_check is not None:
        execution_authority_check()
    raise_if_cancelled()


__all__ = [
    "PackagePageChecks", "continuation_mismatches", "package_continuation", "read_package_page",
    "validate_package_continuation",
]
