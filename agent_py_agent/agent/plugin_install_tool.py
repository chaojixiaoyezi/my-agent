# LLM: 安装 handler 只能由已授权宿主经原 ToolExecutor 调用；只读一次授权包字节，安装事实仍归 PluginInstallStore。
# 模块用途: 把默认停用安装接到原工具副作用合同，不向模型开放内部管理工具。

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from .common.nofollow_fs import read_bytes_beneath
from .path_access_policy import PathAccessPolicy
from .plugin_install_store import PluginInstallStore
from .plugin_installation import PluginInstallationError, PluginInstallRequest
from .plugin_package import PackageReadLimits, inspect_plugin_package
from .tooling.models import (
    ApprovalPolicy,
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ToolHandlerOutcome,
    ToolModelSpec,
    ToolRuntimePolicy,
)

PLUGIN_INSTALL_TOOL = "plugin_install"


# LLM: 模型不可见由组装处 exposure 明确指定；never 只免重复询问已明确提交的管理员安装，不豁免身份、输入或路径硬门。
# 类用途: 执行一次静态包保存，不创建虚拟环境、不导入或启动插件。
class PluginInstallTool(BaseTool):
    # LLM: 依赖是宿主固定的 owner Store、路径策略、工作根和操作 ID，不接受模型或包自报 owner。
    # 函数用途: 准备本次安装工具声明，构造不读写文件。
    def __init__(self, store: PluginInstallStore, policy: PathAccessPolicy,
                 workspace: Path, operation_id: str) -> None:
        self.store, self.policy, self.workspace, self.operation_id = store, policy, workspace, operation_id
        self.model_spec = ToolModelSpec(
            PLUGIN_INSTALL_TOOL, "保存经过授权的本地插件包，默认停用。",
            {"type": "object", "properties": {key: {"type": "string"} for key in (
                "source", "catalog_revision", "workspace", "permission_digest",
            )}, "required": ["source", "catalog_revision", "workspace", "permission_digest"],
             "additionalProperties": False},
        )
        self.runtime_policy = ToolRuntimePolicy(
            effect_resolver=EffectResolverPolicy("mutating"), approval_policy=ApprovalPolicy("never"),
            idempotency_policy=IdempotencyPolicy("operation"), mutates_workspace=False,
        )

    # LLM: 源权限与严格打开先于安装写入，bytes 固定后不再打开来源；清理/提交不确定不能由异常文案推断未执行。
    # 函数用途: 读取包、检查当前安装版本并提交默认停用记录，返回可对账的领域回执。
    def execute(self, params: dict) -> ToolHandlerOutcome:
        try:
            package = self._read_source(params["source"])
        except (OSError, ValueError, RuntimeError):
            return ToolHandlerOutcome(PLUGIN_INSTALL_TOOL, False, "插件包来源不可读、未获授权或格式无效。",
                                      error_code="TOOL_INVALID_ARGUMENTS", effect_outcome="not_started")
        try:
            previous = next((entry for entry in self.store.snapshot()
                             if entry.manifest.plugin_id == package.manifest.plugin_id), None)
            installed = self.store.install(PluginInstallRequest(
                package, self.operation_id, previous.revision if previous is not None else 0,
            ))
        except PluginInstallationError as exc:
            return ToolHandlerOutcome(
                PLUGIN_INSTALL_TOOL, False, "插件安装未得到成功结果，请查看该请求状态。",
                error_code="TOOL_EXECUTION_FAILED", effect_outcome="unknown" if exc.commit_state == "unknown" else "failed",
                result_envelope={"plugin_install": {"reason": exc.reason, "commit_state": exc.commit_state,
                                                    "receipt": asdict(exc.receipt) if exc.receipt else None}},
            )
        except Exception:  # noqa: BLE001 进入安装存储后意外异常保留未知，不自动执行第二次
            return ToolHandlerOutcome(PLUGIN_INSTALL_TOOL, False, "插件安装结果尚未确认。",
                                      error_code="TOOL_EXECUTION_FAILED", effect_outcome="unknown")
        return ToolHandlerOutcome(
            PLUGIN_INSTALL_TOOL, True, "插件包已保存，当前停用。",
            result_envelope={"plugin_install": {
                "plugin_id": installed.installation.manifest.plugin_id, "enabled": False,
                "outcome": installed.outcome, "commit_state": installed.commit_state,
                "receipt": asdict(installed.receipt) if installed.receipt else None,
            }},
        )

    # LLM: 解析后的实际路径逐段从文件系统锚打开；不把任意 source.parent 当受信根，能力不足不降级为普通 open。
    # 函数用途: 在原路径权限内读取一份有界普通文件快照，避免链接替换和 FIFO 阻塞。
    def _read_source(self, source: str):
        path = Path(source).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        if path.is_symlink():
            raise ValueError("插件包来源不能是链接")
        path = path.resolve(strict=True)
        if not self.policy.check(path).allowed:
            raise ValueError("插件包来源未获授权")
        limits = PackageReadLimits()
        content = read_bytes_beneath(Path(path.anchor), path.parts[1:],
                                     max_bytes=limits.archive_bytes, require_dir_fd=True)
        if content is None:
            raise ValueError("插件包来源不存在")
        return inspect_plugin_package(content, limits=limits)
