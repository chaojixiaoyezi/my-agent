# LLM: 本 Store 是 owner 插件安装事实的唯一文件入口；不承担认证、工具执行或操作历史，管理入口必须先授权。
# 模块用途: 在固定目录锁内校验版本、保存包并原子提交默认停用状态，失败按实际读回区分提交结果。

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

from .common.directory_lock import locked_private_directory
from .common.nofollow_fs import (
    read_bytes_beneath,
    write_bytes_atomic_beneath,
    write_text_atomic_beneath,
)
from .common.strict_json import load_strict_json
from .plugin_installation import (
    PluginInstallation,
    PluginInstallationError,
    PluginInstallReceipt,
    PluginInstallRequest,
    PluginInstallResult,
    admit_installation,
)
from .plugin_package import PackageReadLimits, PluginPackageSnapshot, inspect_plugin_package
from .user_space.owner_resolver import OwnerHomeResult

_SCHEMA = "plugin_installations.v1"
_STATE_LIMIT = 16 * 1024 * 1024


# LLM: 只保留必要 owner 身份与规范地址；缺失查询不创建目录，blob 目录不参与决定安装清单。
# 类用途: 管理一个用户的本地安装事实，为后续启停提供同一持久权威。
class PluginInstallStore:
    # LLM: owner 须来自宿主既有解析；构造不初始化 Agent，也不把绝对路径写入状态。
    # 函数用途: 绑定可信 owner、固定安装目录和包读取预算。
    def __init__(self, owner: OwnerHomeResult, *, limits: PackageReadLimits | None = None) -> None:
        self.root = owner.plugins_dir
        self._anchor = owner.root
        self._parts = owner.plugins_dir.relative_to(owner.root).parts
        self._owner = owner.identity
        self._limits = limits or PackageReadLimits()

    # LLM: 一次原子文件读取得到完整投影；包缺失需由 package_bytes 报告，不能因此丢掉已提交历史。
    # 函数用途: 读取安装清单，损坏、未知版本或跨 owner 内容均明确拒绝。
    def snapshot(self) -> tuple[PluginInstallation, ...]:
        try:
            content = read_bytes_beneath(
                self._anchor, (*self._parts, "installations.json"), max_bytes=_STATE_LIMIT
            )
            if content is None:
                return ()
            payload = load_strict_json(content)
            if (
                not isinstance(payload, dict)
                or set(payload) != {"schema_version", "owner", "installations"}
                or payload["schema_version"] != _SCHEMA
            ):
                raise ValueError("安装表协议无效")
            if payload["owner"] != asdict(self._owner):
                raise PluginInstallationError("owner_mismatch", "安装表不属于当前用户。")
            if not isinstance(payload["installations"], list):
                raise ValueError("安装清单无效")
            entries = tuple(
                PluginInstallation.from_payload(row) for row in payload["installations"]
            )
            if len({item.manifest.plugin_id for item in entries}) != len(entries):
                raise ValueError("插件身份重复")
            if len({item.last_commit.operation_id for item in entries}) != len(entries):
                raise ValueError("安装提交身份重复")
            return entries
        except PluginInstallationError:
            raise
        except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
            raise PluginInstallationError(
                "invalid_state", "安装状态不可读，未覆盖原记录。"
            ) from exc

    # LLM: 当前事实保留不代表包仍完整；内容读取只取摘要地址，并验证同一静态描述，不执行包。
    # 函数用途: 取出已安装记录引用的完整包，缺失或篡改时明确拒绝。
    def package_bytes(self, installation: PluginInstallation) -> bytes:
        try:
            data = read_bytes_beneath(
                self._anchor,
                self._blob_parts(installation.package_sha256),
                max_bytes=self._limits.archive_bytes,
            )
            if data is None or hashlib.sha256(data).hexdigest() != installation.package_sha256:
                raise ValueError("包内容不匹配")
            package = inspect_plugin_package(data, limits=self._limits)
            if package.manifest != installation.manifest:
                raise ValueError("包描述不匹配")
            return data
        except (OSError, ValueError) as exc:
            raise PluginInstallationError(
                "package_integrity", "已登记的插件包缺失或损坏。"
            ) from exc

    # LLM: 准入和来源授权归外层；锁内先包后表，清理失败不能覆盖锁体已裁定的提交状态和回执。
    # 函数用途: 保存一个本地候选包为停用状态，重复请求复读回执，不重置既有安装。
    def install(self, request: PluginInstallRequest) -> PluginInstallResult:
        verified = inspect_plugin_package(request.package.archive_bytes, limits=self._limits)
        if verified.manifest != request.package.manifest:
            raise PluginInstallationError("package_integrity", "候选包与声明不一致。")
        result = None
        operation_error = None
        try:
            with locked_private_directory(
                self._anchor, relative_parts=self._parts, lock_name=".plugins.lock"
            ):
                try:
                    result = self._install_locked(request)
                except PluginInstallationError as exc:
                    operation_error = exc
                    raise
            return result
        except OSError as exc:
            if operation_error is not None:
                raise PluginInstallationError(
                    operation_error.reason,
                    str(operation_error),
                    commit_state=operation_error.commit_state,
                    receipt=operation_error.receipt,
                ) from exc
            if result is not None:
                raise PluginInstallationError(
                    "cleanup_failed",
                    "安装处理已结束，但存储锁清理失败。",
                    commit_state=result.commit_state,
                    receipt=result.receipt,
                ) from exc
            raise PluginInstallationError(
                "storage_unavailable", "安装存储不可用，安装记录未提交。"
            ) from exc

    # LLM: 锁内复读最新记录；原请求已命中回执后，即使包损坏也保留其已提交事实，不自动修包或重做。
    # 函数用途: 在完整临界区内完成准入、保存包及提交安装表。
    def _install_locked(self, request: PluginInstallRequest) -> PluginInstallResult:
        package = request.package
        entries = self.snapshot()
        replay = admit_installation(request, entries)
        if replay is not None:
            try:
                self.package_bytes(replay.installation)
            except PluginInstallationError as exc:
                raise PluginInstallationError(
                    exc.reason, str(exc), commit_state=replay.commit_state, receipt=replay.receipt
                ) from exc
            return replay
        receipt = PluginInstallReceipt(
            request.operation_id,
            request.input_digest,
            package.manifest.plugin_id,
            package.sha256,
            request.expected_revision,
            request.expected_revision + 1,
        )
        installed = PluginInstallation(
            package.manifest, package.sha256, receipt.after_revision, receipt
        )
        updated = (*entries, installed)
        text = self._state_text(updated)
        self._save_blob(package)
        self._commit(entries, updated, text, receipt)
        return PluginInstallResult(installed, "installed", "committed", receipt)

    # LLM: 包地址只由本次重新核验的完整摘要决定；已存在但损坏的 blob 不覆盖，未引用包不是安装事实。
    # 函数用途: 保存完整包字节，为随后的安装表提交准备内容引用。
    def _save_blob(self, package: PluginPackageSnapshot) -> None:
        parts = self._blob_parts(package.sha256)
        try:
            current = read_bytes_beneath(self._anchor, parts, max_bytes=self._limits.archive_bytes)
        except ValueError as exc:
            raise PluginInstallationError(
                "package_integrity", "已有插件包超过读取预算，未覆盖。"
            ) from exc
        if current is not None:
            if current != package.archive_bytes:
                raise PluginInstallationError(
                    "package_integrity", "已有插件包内容与摘要不符，未覆盖。"
                )
            return
        write_bytes_atomic_beneath(self._anchor, parts, package.archive_bytes)

    # LLM: owner 从绑定身份写入，所有提交均保存完整清单及每插件最后回执；不另写索引或操作历史。
    # 函数用途: 生成完整且有界的权威状态，超限在发布包和状态之前拒绝。
    def _state_text(self, entries: tuple[PluginInstallation, ...]) -> str:
        value = {
            "schema_version": _SCHEMA,
            "owner": asdict(self._owner),
            "installations": [row.to_payload() for row in entries],
        }
        text = (
            json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
            + "\n"
        )
        if len(text.encode("utf-8")) > _STATE_LIMIT:
            raise PluginInstallationError("state_limit", "安装表超过存储预算。")
        return text

    # LLM: 原子替换后可能刷盘或返回失败；同锁读回只裁决可见提交，不能把未知变成未发生或自动重做。
    # 函数用途: 提交安装表，异常时保留原请求回执并区分已提交、未提交和无法确认。
    def _commit(
        self,
        before: tuple[PluginInstallation, ...],
        after: tuple[PluginInstallation, ...],
        text: str,
        receipt: PluginInstallReceipt,
    ) -> None:
        try:
            write_text_atomic_beneath(self._anchor, (*self._parts, "installations.json"), text)
        except OSError as exc:
            state = "unknown"
            try:
                current = self.snapshot()
                if current == after:
                    state = "committed"
                elif current == before:
                    state = "not_committed"
            except PluginInstallationError:
                pass
            raise PluginInstallationError(
                "commit_unconfirmed",
                "安装提交遇到存储错误，请按原回执核对。",
                commit_state=state,
                receipt=receipt,
            ) from exc

    # LLM: 摘要已由包读取器或记录合同校验；此地址不依赖插件 ID、来源文件名或目录扫描。
    # 函数用途: 生成同一 owner 下的固定包内容地址。
    def _blob_parts(self, digest: str) -> tuple[str, ...]:
        return (*self._parts, "packages", f"{digest}.zip")
