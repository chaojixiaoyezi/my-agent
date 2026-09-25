# LLM: 安装包与配置共用原 PathAccessPolicy 和 no-follow 文件原语；只读一次字节快照，不取得管理授权。
# 模块用途: 在固定工作根和读取预算内读取明确来源，拒绝链接及不支持安全打开的平台。

from __future__ import annotations

from pathlib import Path

from .common.nofollow_fs import read_bytes_beneath
from .path_access_policy import PathAccessPolicy


# LLM: 来源读取失败的结构化原因：not_found / unauthorized / symlink，base 是本次解析相对路径用的工作区根（会话工作区，
#   不是客户端 shell 的当前目录）。仍是 ValueError 子类，旧的宽泛捕获照常生效；管理工具按 reason 给用户区分"不存在"与"没权限"。
# 类用途: 让安装/更新回执能说清楚来源到底是找不到、越权还是链接，而不是一句泛化文案。
class PluginSourceError(ValueError):
    # 函数用途: 保存原因码、用户输入的来源和解析基准，不放包正文。
    def __init__(self, reason: str, message: str, *, source: str, base: Path) -> None:
        super().__init__(message)
        self.reason, self.source, self.base = reason, source, base


# LLM: 解析后的路径逐段从文件系统锚打开，不将任意 parent 当受信根；权限或能力不足不能降级普通 open。相对路径只按传入的
#   workspace（Gateway 已校验的会话工作区根）解析；找不到时报 not_found 并带出解析基准，越权报 unauthorized，链接报 symlink。
# 函数用途: 在既有路径权限内读取一个有界普通文件，配置和安装不得复读变化的来源。
def read_plugin_source(source: str, workspace: Path, policy: PathAccessPolicy, *, max_bytes: int) -> bytes:
    path = Path(source).expanduser()
    if not path.is_absolute():
        path = workspace / path
    if path.is_symlink():
        raise PluginSourceError("symlink", "插件来源不能是链接。", source=source, base=workspace)
    try:
        path = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PluginSourceError(
            "not_found", f"插件来源不存在：{source}（相对路径按当前会话工作区 {workspace} 解析，可改用绝对路径）。",
            source=source, base=workspace,
        ) from exc
    if not policy.check(path).allowed:
        raise PluginSourceError(
            "unauthorized", "插件来源未获授权：该路径在当前身份可访问范围之外（WorkspaceOnly 下只能读 owner 目录内的包）。",
            source=source, base=workspace,
        )
    content = read_bytes_beneath(Path(path.anchor), path.parts[1:], max_bytes=max_bytes, require_dir_fd=True)
    if content is None:
        raise PluginSourceError("not_found", f"插件来源不存在：{source}。", source=source, base=workspace)
    return content
