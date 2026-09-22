# LLM: 安装事实描述包、私有配置、激活与最后提交，不复制 OperationStore 的运行/租约/UNKNOWN 状态机。
# 模块用途: 定义唯一安装记录和提交回执；配置/激活迁移独立计算，读写及系统锁由 Store 负责。

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field

from .common.strict_json import load_strict_json
from .plugin_activation_record import PluginActivation, plugin_catalog_digest
from .plugin_manifest import PluginManifest, canonical_plugin_settings
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
        return plugin_input_digest(
            "install", self.package.manifest.plugin_id, self.package.sha256, self.expected_revision
        )


# LLM: 最后回执与安装记录同次保存；它只能证明这一条提交，不是永久幂等历史，也不提供执行权限。
# 类用途: 保存安装、配置或激活提交的输入摘要和前后版本，供响应丢失后的原请求核对。
@dataclass(frozen=True)
class PluginCommitReceipt:
    operation_id: str
    input_digest: str
    plugin_id: str
    package_sha256: str
    before_revision: int
    after_revision: int
    action: str = "install"
    settings_sha256: str = ""
    activation_sha256: str = ""

    # LLM: 每次提交只前进一版；动作与输入摘要同源，配置和激活各自绑定完整内容，不包含配置原值。
    # 函数用途: 拒绝无效提交标识、动作、摘要和非单调版本。
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
        lifecycle = self.action in {"prepare", "activate", "revoke"}
        if (self.action not in {"install", "configure", "prepare", "activate", "revoke"}
                or not isinstance(self.settings_sha256, str)
                or (self.action != "configure" and self.settings_sha256 != "")
                or (self.action == "configure" and not _DIGEST.fullmatch(self.settings_sha256))
                or not isinstance(self.activation_sha256, str)
                or (lifecycle and not _DIGEST.fullmatch(self.activation_sha256))
                or (not lifecycle and self.activation_sha256 != "")):
            raise ValueError("插件提交动作或内容摘要无效")
        if self.input_digest != plugin_input_digest(
            self.action, self.plugin_id, self.package_sha256, self.before_revision, self.settings_sha256,
            activation_sha256=self.activation_sha256,
        ):
            raise ValueError("安装回执输入摘要不匹配")


# LLM: v3 配置与激活仍在唯一安装表，enabled/activation_id 只投影该记录；配置值不得进入公开投影。
# 类用途: 保存静态描述、私有配置、激活、版本和最后提交，不扫描目录猜测有效安装。
@dataclass(frozen=True)
class PluginInstallation:
    manifest: PluginManifest
    package_sha256: str
    revision: int
    last_commit: PluginCommitReceipt
    settings_json: str | None = field(default=None, repr=False)
    settings_revision: int = 0
    activation: PluginActivation | None = None

    # LLM: 最后回执与包、版本、配置及激活匹配；私有配置读回须通过原 schema，损坏不能降级为空值。
    # 函数用途: 校验完整安装事实，不从包自述生成宿主激活状态。
    def __post_init__(self) -> None:
        if not isinstance(self.manifest, PluginManifest) or not isinstance(
            self.last_commit, PluginCommitReceipt
        ):
            raise ValueError("安装记录缺少已验证声明或回执")
        if (
            type(self.revision) is not int
            or self.revision != self.last_commit.after_revision
            or self.manifest.plugin_id != self.last_commit.plugin_id
            or self.package_sha256 != self.last_commit.package_sha256
        ):
            raise ValueError("安装记录与提交回执不一致")
        if type(self.settings_revision) is not int or not 0 <= self.settings_revision <= self.revision:
            raise ValueError("配置版本无效")
        if self.last_commit.action == "install" and (self.settings_json is not None or self.settings_revision != 0):
            raise ValueError("安装回执不能同时声明配置已保存")
        if self.settings_json is None:
            if self.settings_revision != 0 or self.last_commit.action == "configure":
                raise ValueError("配置状态不完整")
        else:
            if not isinstance(self.settings_json, str) or self.settings_revision <= 0:
                raise ValueError("配置值或版本无效")
            canonical = canonical_plugin_settings(load_strict_json(self.settings_json), self.manifest.settings_schema)
            if canonical != self.settings_json:
                raise ValueError("配置不是规范 JSON")
            if self.last_commit.action == "configure" and (
                self.settings_revision != self.revision
                or hashlib.sha256(canonical.encode()).hexdigest() != self.last_commit.settings_sha256
            ):
                raise ValueError("配置与提交回执不一致")
        self._validate_activation()

    # LLM: 激活必须绑定原包/配置/安装版本与最后提交；revoked 保留原代，不能据此推导进程清理成功。
    # 函数用途: 拒绝拼接不同版本或缺少阶段回执的激活记录。
    def _validate_activation(self) -> None:
        activation = self.activation
        action = self.last_commit.action
        if activation is None:
            if action not in {"install", "configure"}:
                raise ValueError("激活提交缺少原代记录")
            return
        if not isinstance(activation, PluginActivation):
            raise ValueError("安装激活记录无效")
        plan = activation.plan
        offset = self.revision - plan.installation_revision
        if (action != {"preparing": "prepare", "active": "activate", "revoked": "revoke"}[activation.phase]
                or activation.content_sha256 != self.last_commit.activation_sha256
                or plan.plugin_id != self.manifest.plugin_id or plan.package_sha256 != self.package_sha256
                or plan.settings_revision != self.settings_revision
                or offset != {"preparing": 1, "active": 2,
                              "revoked": 3 if activation.catalog_sha256 else 2}[activation.phase]
                or (activation.phase != "revoked" and self.last_commit.operation_id != plan.operation_id)
                or (activation.catalog_sha256 and activation.catalog_sha256 != plugin_catalog_digest(self.manifest))):
            raise ValueError("安装激活与版本或提交回执不一致")
        canonical_plugin_settings(load_strict_json(self.settings_json or "{}"), self.manifest.settings_schema)

    # LLM: 这是持久发布状态的只读投影，不证明当前 MCP 进程健康或授权；运行时仍须复核原代。
    # 函数用途: 让目录沿唯一激活记录显示是否已发布。
    @property
    def enabled(self) -> bool:
        return self.activation is not None and self.activation.phase == "active"

    # LLM: 身份只由原环境计划生成，撤销仍保留它供精确资源清理；未激活没有可调用代次。
    # 函数用途: 取得当前或待清理激活的固定身份。
    @property
    def activation_id(self) -> str:
        return self.activation.activation_id if self.activation is not None else ""

    # LLM: 本投影含私有配置，只可写入 owner 私有安装表；不能复用作工具结果、命令目录或用户消息。
    # 函数用途: 为同一次原子替换生成完整安装、配置与激活记录。
    def to_payload(self) -> dict:
        return {
            "manifest": self.manifest.to_payload(),
            "package_sha256": self.package_sha256,
            "revision": self.revision,
            "last_commit": asdict(self.last_commit),
            "settings_json": self.settings_json,
            "settings_revision": self.settings_revision,
            "activation": self.activation.to_payload() if self.activation is not None else None,
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
            "settings_json",
            "settings_revision",
            "activation",
        }:
            raise ValueError("安装记录字段无效")
        if not isinstance(value["last_commit"], dict) or set(value["last_commit"]) != {
            "operation_id", "input_digest", "plugin_id", "package_sha256", "before_revision",
            "after_revision", "action", "settings_sha256", "activation_sha256",
        }:
            raise ValueError("安装提交回执无效")
        return cls(
            manifest=PluginManifest.from_payload(value["manifest"]),
            package_sha256=value["package_sha256"],
            revision=value["revision"],
            last_commit=PluginCommitReceipt(**value["last_commit"]),
            settings_json=value["settings_json"],
            settings_revision=value["settings_revision"],
            activation=PluginActivation.from_payload(value["activation"]) if value["activation"] is not None else None,
        )


# LLM: committed 只证明原子记录可读且匹配回执，不扩大为环境可运行；unchanged 不冒充旧请求重放。
# 类用途: 区分新安装/配置提交、原请求重放和当前值无需改动。
@dataclass(frozen=True)
class PluginMutationResult:
    installation: PluginInstallation
    outcome: str
    commit_state: str
    receipt: PluginCommitReceipt | None


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
        receipt: PluginCommitReceipt | None = None,
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


# LLM: 安装/配置保持原摘要；激活提交另含完整目标摘要，不保存配置正文或授权身份。
# 函数用途: 对明确动作、插件、包、期望版本与内容身份生成稳定的输入摘要。
def plugin_input_digest(action: str, plugin_id: str, package_sha256: str, expected_revision: int,
                        settings_sha256: str = "", *, activation_sha256: str = "") -> str:
    value = {
        "action": action,
        "plugin_id": plugin_id,
        "package_sha256": package_sha256,
        "expected_revision": expected_revision,
    }
    if action == "configure":
        value["settings_sha256"] = settings_sha256
    if action in {"prepare", "activate", "revoke"}:
        value["activation_sha256"] = activation_sha256
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


# LLM: 最后回执只证明匹配的那次提交；找不到旧请求且版本已变必须冲突，不能推断此前未执行。
# 函数用途: 根据 Store 在锁内读取的快照，纯计算重放、版本冲突或无改动结果，不读写文件。
def admit_installation(
    request: PluginInstallRequest,
    entries: tuple[PluginInstallation, ...],
) -> PluginMutationResult | None:
    for entry in entries:
        if entry.last_commit.operation_id == request.operation_id:
            if entry.last_commit.input_digest != request.input_digest:
                raise PluginInstallationError(
                    "operation_conflict", "同一安装操作标识不能改换输入。"
                )
            return PluginMutationResult(entry, "replayed", "committed", entry.last_commit)
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
    return PluginMutationResult(existing, "unchanged", "not_committed", None)
