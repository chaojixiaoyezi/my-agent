# LLM: 本 Store 是 owner 插件安装事实的唯一文件入口；不承担认证、工具执行或操作历史，管理入口必须先授权。
# 模块用途: 在固定目录锁内提交包、配置与激活，撤销不等待资源配额，失败按实际读回区分结果。

from __future__ import annotations

import hashlib
from contextlib import nullcontext

from .common.directory_lock import locked_private_directory
from .common.nofollow_fs import (
    read_bytes_beneath,
    write_bytes_atomic_beneath,
    write_text_atomic_beneath,
)
from .plugin_activation import PluginActivationRequest, prepare_activation
from .plugin_configuration import PluginConfigureRequest, prepare_configuration
from .plugin_installation import (
    PluginCommitReceipt,
    PluginInstallation,
    PluginInstallationError,
    PluginInstallRequest,
    PluginMutationResult,
    admit_installation,
)
from .plugin_installation_state import (
    INSTALLATION_STATE_LIMIT,
    PluginInstallationState,
    decode_installation_state,
    encode_installation_state,
)
from .plugin_package import PackageReadLimits, PluginPackageSnapshot, inspect_plugin_package
from .user_space.owner_quota import (
    OwnerQuotaChange,
    OwnerQuotaExceeded,
    OwnerQuotaUnavailable,
    owner_quota_enforcer_from_policy,
)
from .user_space.owner_resolver import OwnerHomeResult


# LLM: 只保留必要 owner 身份与规范地址；缺失查询不创建目录，blob 目录不参与决定安装清单。
# 类用途: 管理一个用户的本地安装事实，为后续启停提供同一持久权威。
class PluginInstallStore:
    # LLM: owner 须来自宿主既有解析；配额与安装地址沿同一 owner，构造不初始化 Agent 或把绝对路径写入状态。
    # 函数用途: 绑定可信 owner、原配额文件、固定安装目录和包读取预算。
    def __init__(self, owner: OwnerHomeResult, *, limits: PackageReadLimits | None = None) -> None:
        self.root = owner.plugins_dir
        self._anchor = owner.root
        self._parts = owner.plugins_dir.relative_to(owner.root).parts
        self._owner = owner.identity
        self._owner_home = owner.home_dir
        self._quota_path = owner.quota_json
        self._limits = limits or PackageReadLimits()

    # LLM: 一次原子文件读取得到完整投影；包缺失需由 package_bytes 报告，不能因此丢掉已提交历史。
    # 函数用途: 读取安装清单，损坏、未知版本或跨 owner 内容均明确拒绝。
    def snapshot(self) -> tuple[PluginInstallation, ...]:
        return self._read_state().entries

    # LLM: 解码 v1/v2 不写迁移；真正提交才连同旧来源写 v3，不能将损坏表当空表。
    # 函数用途: 读取原表及迁移信息，保持只读查询无副作用。
    def _read_state(self) -> PluginInstallationState:
        try:
            content = read_bytes_beneath(
                self._anchor, (*self._parts, "installations.json"), max_bytes=INSTALLATION_STATE_LIMIT
            )
            return decode_installation_state(content, self._owner)
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

    # LLM: 授权归外层；复验包内容后沿原 quota→目录锁顺序提交，不能把配置更新重置成新安装。
    # 函数用途: 保存一个本地候选包为停用状态，重复请求复读回执，不重置既有安装。
    def install(self, request: PluginInstallRequest) -> PluginMutationResult:
        verified = inspect_plugin_package(request.package.archive_bytes, limits=self._limits)
        if verified.manifest != request.package.manifest:
            raise PluginInstallationError("package_integrity", "候选包与声明不一致。")
        return self._write(lambda quota: self._install_locked(request, quota))

    # LLM: 配置仍属于同一安装表；值验证、版本 CAS 和提交均在原锁内，包不在此更新或执行。
    # 函数用途: 完整替换停用插件的私有配置，重送复读原回执。
    def configure(self, request: PluginConfigureRequest) -> PluginMutationResult:
        return self._write(lambda quota: self._update_locked(quota, lambda entries: prepare_configuration(request, entries)))

    # LLM: 新资源阶段保持 quota→插件锁；撤销只关闭有界既有记录，不能等正在准备的 quota 或以空间不足维持执行权。
    # 函数用途: 在同一安装表预留、发布或撤销固定代次，不启动进程或声称清理完成。
    def change_activation(self, request: PluginActivationRequest) -> PluginMutationResult:
        return self._write(
            lambda quota: self._update_locked(quota, lambda entries: prepare_activation(request, entries)),
            reserve_quota=request.action != "revoke",
        )

    # LLM: 停用空代也须在原安装锁线性化，不能凭先前快照宣布后来启用的插件已停；不写记录、不等待配额。
    # 函数用途: 确认同一版本确实没有激活，防止无资源停用与并发启用交错时误报成功。
    def confirm_inactive(self, expected: PluginInstallation) -> PluginMutationResult:
        # LLM: 回调只在原插件锁内读取同一权威，完整记录相等才能确认无须清理；异常由原写入口保留。
        # 函数用途: 对空激活做一次无写入的版本核验。
        def confirm(_quota):
            current = next((row for row in self.snapshot() if row.manifest.plugin_id == expected.manifest.plugin_id), None)
            if current != expected or current.activation is not None:
                raise PluginInstallationError("revision_conflict", "安装或激活已变化，请读取最新状态。")
            return PluginMutationResult(current, "unchanged", "not_committed", None)

        return self._write(confirm, reserve_quota=False)

    # LLM: 此读取不是准入锁；资源层须在原预留/发送临界区调用，允许阶段只能选 preparing/active，不能授权 revoked。
    # 函数用途: 拒绝缺失、损坏、换代及已撤销的原激活，握手可沿同一代跨越发布，业务默认只接受 active。
    def require_activation(self, plugin_id: str, activation_id: str, *,
                           phases: frozenset[str] = frozenset({"active"})) -> PluginInstallation:
        if not isinstance(phases, frozenset) or not phases or not phases <= {"preparing", "active"}:
            raise ValueError("插件准入阶段无效")
        entry = next((row for row in self.snapshot() if row.manifest.plugin_id == plugin_id), None)
        if (entry is None or not activation_id or entry.activation_id != activation_id
                or entry.activation is None or entry.activation.phase not in phases):
            raise PluginInstallationError("activation_unavailable", "原插件激活不可用或已撤销。")
        return entry

    # LLM: 新资源复用原 quota→插件锁；撤销跳过 quota 但仍用同一插件锁，不增锁/操作表，清理错误保留原裁决。
    # 函数用途: 在完整临界区执行安装领域更新，并保留确定或未知提交事实。
    def _write(self, mutation, *, reserve_quota: bool = True) -> PluginMutationResult:
        result = None
        operation_error = None
        try:
            admission = (owner_quota_enforcer_from_policy(self._owner_home, quota_path=self._quota_path).admission()
                         if reserve_quota else nullcontext(None))
            with admission as quota, locked_private_directory(
                self._anchor, relative_parts=self._parts, lock_name=".plugins.lock"
            ):
                try:
                    result = mutation(quota)
                except PluginInstallationError as exc:
                    operation_error = exc
                    raise
            return result
        except (OwnerQuotaExceeded, OwnerQuotaUnavailable) as exc:
            raise PluginInstallationError("quota_unavailable", "当前用户配额不足或配额状态不可读。") from exc
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

    # LLM: 锁内复读最新记录；原请求命中后保留提交事实，新写入在原配额锁内计算完整包与安装表增长。
    # 函数用途: 在完整临界区内完成准入、保存包及提交安装表。
    def _install_locked(self, request: PluginInstallRequest, quota) -> PluginMutationResult:
        package = request.package
        state = self._read_state()
        entries = state.entries
        replay = admit_installation(request, entries)
        if replay is not None:
            try:
                self.package_bytes(replay.installation)
            except PluginInstallationError as exc:
                raise PluginInstallationError(
                    exc.reason, str(exc), commit_state=replay.commit_state, receipt=replay.receipt
                ) from exc
            return replay
        receipt = PluginCommitReceipt(
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
        text = encode_installation_state(updated, self._owner, state.migration_json)
        quota.check((
            OwnerQuotaChange(self._anchor.joinpath(*self._blob_parts(package.sha256)), len(package.archive_bytes)),
            OwnerQuotaChange(self.root / "installations.json", len(text.encode("utf-8"))),
        ))
        self._save_blob(package)
        self._commit(entries, updated, text, receipt)
        return PluginMutationResult(installed, "installed", "committed", receipt)

    # LLM: 纯领域迁移接收同一完整快照；配置/激活/版本/回执同次提交，撤销只豁免新增资源配额，不豁免表上限。
    # 函数用途: 保存一条已通过 CAS 的安装更新，迁移来源和其他插件保持。
    def _update_locked(self, quota, prepare) -> PluginMutationResult:
        state = self._read_state()
        result = prepare(state.entries)
        if result.receipt is None or result.outcome == "replayed":
            return result
        updated = tuple(result.installation if row.manifest.plugin_id == result.installation.manifest.plugin_id else row
                        for row in state.entries)
        text = encode_installation_state(updated, self._owner, state.migration_json)
        if quota is not None:
            quota.check((OwnerQuotaChange(self.root / "installations.json", len(text.encode("utf-8"))),))
        self._commit(state.entries, updated, text, result.receipt)
        return result

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

    # LLM: 原子替换后可能刷盘或返回失败；同锁读回只裁决可见提交，不能把未知变成未发生或自动重做。
    # 函数用途: 提交安装表，异常时保留原请求回执并区分已提交、未提交和无法确认。
    def _commit(
        self,
        before: tuple[PluginInstallation, ...],
        after: tuple[PluginInstallation, ...],
        text: str,
        receipt: PluginCommitReceipt,
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
