# LLM: 安装事实只描述宿主已保存的包和最后提交，不复制 OperationStore 的运行/租约/UNKNOWN 状态机。
# 模块用途: 定义安装请求、默认停用记录、提交回执与纯准入判断；读写及系统锁由 Store 负责。

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass

from .plugin_manifest import PluginManifest
from .plugin_package import PluginPackageSnapshot

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_OPERATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


# LLM: 请求只携带已读取快照和宿主原操作身份，不含来源路径或客户端声明的 owner；安装仍会复核内容绑定。
# 类用途: 固定一次安装的包、操作标识与期望版本，便于失败后原请求重放。
@dataclass(frozen=True)
class PluginInstallRequest:
    package: PluginPackageSnapshot
    operation_id: str
    expected_revision: int

    # LLM: JSON 或调用方不能把布尔值当版本；包描述与字节的匹配由 Store 在写入前复验。
    # 函数用途: 拒绝缺失包及无效操作身份。
    def __post_init__(self) -> None:
        validate_install_identity(self.operation_id, self.expected_revision)
        if not isinstance(self.package, PluginPackageSnapshot):
            raise ValueError("安装请求缺少包快照")

    # LLM: 摘要包含动作、插件、包和期望版本；源路径变化不改变同一快照的语义，摘要不是授权。
    # 函数用途: 为原请求回执生成可重复比较的输入标识。
    @property
    def input_digest(self) -> str:
        return install_input_digest(
            self.package.manifest.plugin_id, self.package.sha256, self.expected_revision
        )


# LLM: 最后回执与安装记录同次保存；它只能证明这一条提交，不是永久幂等历史，也不提供执行权限。
# 类用途: 保存安装操作的输入摘要和前后版本，供响应丢失后的原请求核对。
@dataclass(frozen=True)
class PluginInstallReceipt:
    operation_id: str
    input_digest: str
    plugin_id: str
    package_sha256: str
    before_revision: int
    after_revision: int

    # LLM: 同一次安装只能前进一版；稳定标识和摘要不可从日志或中文说明推导。
    # 函数用途: 拒绝无效提交标识、摘要和非单调版本。
    def __post_init__(self) -> None:
        validate_install_identity(self.operation_id, self.before_revision)
        if not isinstance(self.plugin_id, str) or not self.plugin_id:
            raise ValueError("安装回执缺少插件身份")
        if any(
            not isinstance(value, str) or not _DIGEST.fullmatch(value)
            for value in (self.input_digest, self.package_sha256)
        ):
            raise ValueError("安装回执摘要无效")
        if type(self.after_revision) is not int or self.after_revision != self.before_revision + 1:
            raise ValueError("安装回执版本无效")
        if self.input_digest != install_input_digest(
            self.plugin_id, self.package_sha256, self.before_revision
        ):
            raise ValueError("安装回执输入摘要不匹配")


# LLM: v1 当前只支持默认停用安装；不能根据文件中自报 enabled/activation 开放运行，启停将在同一权威上显式扩展。
# 类用途: 保存完整静态描述、内容地址、版本和最后提交，避免扫描包目录猜测有效安装。
@dataclass(frozen=True)
class PluginInstallation:
    manifest: PluginManifest
    package_sha256: str
    revision: int
    last_commit: PluginInstallReceipt
    enabled: bool = False
    activation_id: str = ""

    # LLM: 记录与最后回执的包、插件及版本必须一致；包自述不能成为宿主激活状态。
    # 函数用途: 校验读取到的安装事实，损坏状态不能降级为空表。
    def __post_init__(self) -> None:
        if not isinstance(self.manifest, PluginManifest) or not isinstance(
            self.last_commit, PluginInstallReceipt
        ):
            raise ValueError("安装记录缺少已验证声明或回执")
        if self.enabled is not False or self.activation_id != "":
            raise ValueError("当前安装协议不接受激活状态")
        if (
            type(self.revision) is not int
            or self.revision != self.last_commit.after_revision
            or self.manifest.plugin_id != self.last_commit.plugin_id
            or self.package_sha256 != self.last_commit.package_sha256
        ):
            raise ValueError("安装记录与提交回执不一致")

    # LLM: 输出是独立 JSON 投影，不包含来源路径、密钥或伪造 run/attempt。
    # 函数用途: 为同一次原子替换生成完整安装记录。
    def to_payload(self) -> dict:
        return {
            "manifest": self.manifest.to_payload(),
            "package_sha256": self.package_sha256,
            "revision": self.revision,
            "last_commit": asdict(self.last_commit),
            "enabled": self.enabled,
            "activation_id": self.activation_id,
        }

    # LLM: 严格字段集合防止忽略未来生命周期协议；不能补默认值使旧 binary 覆盖新记录。
    # 函数用途: 从持久 JSON 读取一条完整安装事实。
    @classmethod
    def from_payload(cls, value: object) -> PluginInstallation:
        if not isinstance(value, dict) or set(value) != {
            "manifest",
            "package_sha256",
            "revision",
            "last_commit",
            "enabled",
            "activation_id",
        }:
            raise ValueError("安装记录字段无效")
        if not isinstance(value["last_commit"], dict):
            raise ValueError("安装提交回执无效")
        return cls(
            manifest=PluginManifest.from_payload(value["manifest"]),
            package_sha256=value["package_sha256"],
            revision=value["revision"],
            last_commit=PluginInstallReceipt(**value["last_commit"]),
            enabled=value["enabled"],
            activation_id=value["activation_id"],
        )


# LLM: committed 只证明原子记录可读且匹配回执，不扩大为环境可运行；unchanged 不冒充旧请求重放。
# 类用途: 区分新提交、原请求重放和当前已安装但没有改动。
@dataclass(frozen=True)
class PluginInstallResult:
    installation: PluginInstallation
    outcome: str
    commit_state: str
    receipt: PluginInstallReceipt | None


# LLM: 领域失败的提交状态必须由边界事实给出；未知不能转换为未执行或自动重试，正文不参与决策。
# 类用途: 向管理适配器报告失败原因、是否提交以及本次需要核对的回执。
class PluginInstallationError(ValueError):
    # LLM: 不把来源路径或包正文放入文案；receipt 在未提交/未知时是候选标识，不能单独当成功证明。
    # 函数用途: 创建带稳定分类的安装失败。
    def __init__(
        self,
        reason: str,
        message: str,
        *,
        commit_state: str = "not_committed",
        receipt: PluginInstallReceipt | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.commit_state = commit_state
        self.receipt = receipt


# LLM: operation_id 来自宿主原操作入口，expected_revision 来自明确读取；本验证不授予任何管理权限。
# 函数用途: 检查安装请求及回执使用的身份和版本格式。
def validate_install_identity(operation_id: str, expected_revision: int) -> None:
    if not isinstance(operation_id, str) or not _OPERATION.fullmatch(operation_id):
        raise ValueError("安装操作标识无效")
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("安装期望版本无效")


# LLM: 请求和持久回执共用规范输入，不能分别定义摘要；字段只包含安装语义，不包含运行或授权身份。
# 函数用途: 对动作、插件、包及期望版本生成稳定的安装输入摘要。
def install_input_digest(plugin_id: str, package_sha256: str, expected_revision: int) -> str:
    value = {
        "action": "install",
        "plugin_id": plugin_id,
        "package_sha256": package_sha256,
        "expected_revision": expected_revision,
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


# LLM: 最后回执只证明匹配的那次提交；找不到旧请求且版本已变必须冲突，不能推断此前未执行。
# 函数用途: 根据 Store 在锁内读取的快照，纯计算重放、版本冲突或无改动结果，不读写文件。
def admit_installation(
    request: PluginInstallRequest,
    entries: tuple[PluginInstallation, ...],
) -> PluginInstallResult | None:
    for entry in entries:
        if entry.last_commit.operation_id == request.operation_id:
            if entry.last_commit.input_digest != request.input_digest:
                raise PluginInstallationError(
                    "operation_conflict", "同一安装操作标识不能改换输入。"
                )
            return PluginInstallResult(entry, "replayed", "committed", entry.last_commit)
    existing = next(
        (row for row in entries if row.manifest.plugin_id == request.package.manifest.plugin_id),
        None,
    )
    revision = existing.revision if existing is not None else 0
    if request.expected_revision != revision:
        raise PluginInstallationError("revision_conflict", "安装版本已变化，请先读取当前状态。")
    if existing is None:
        return None
    if existing.package_sha256 != request.package.sha256:
        raise PluginInstallationError("package_conflict", "已安装的插件包不同，不能原地替换。")
    return PluginInstallResult(existing, "unchanged", "not_committed", None)
