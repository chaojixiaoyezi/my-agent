# LLM: 安装包与配置共用原 PathAccessPolicy 和 no-follow 文件原语；只读一次字节快照，不取得管理授权。
# 模块用途: 在固定工作根和读取预算内读取明确来源，拒绝链接及不支持安全打开的平台。

from __future__ import annotations

from pathlib import Path

from .common.nofollow_fs import read_bytes_beneath
from .path_access_policy import PathAccessPolicy


# LLM: 来源读取失败的结构化原因：not_found / unauthorized / symlink，base 是本次解析相对路径用的工作区根（会话工作区，
#   不是客户端 shell 的当前目录）。仍是 ValueError 子类，旧的宽泛捕获照常生效；管理工具按 reason 给用户区分"不存在"与"没权限"。
#   allowed_root 只在越权时给出，且只取路径策略冻结的当前 owner 根（PathAccessPolicy.owner_scope_root），不含其它 owner 或宿主路径。
# 类用途: 让安装/更新回执能说清楚来源到底是找不到、越权还是链接，而不是一句泛化文案。
class PluginSourceError(ValueError):
    # 函数用途: 保存原因码、用户输入的来源、解析基准和（越权时）当前 owner 允许放包的根，不放包正文。
    def __init__(self, reason: str, message: str, *, source: str, base: Path, allowed_root: Path | None = None) -> None:
        super().__init__(message)
        self.reason, self.source, self.base, self.allowed_root = reason, source, base, allowed_root


# LLM: 安装与更新工具共用的结构化回执：reason=source_<原因>、source_base（解析基准）；越权且有 owner 根时多给 allowed_root。
#   展示层（plugin_management._reply）只读这些字段生成用户说明，不解析异常文本。
# 函数用途: 把来源读取失败转成回执信封里的结构化字段。
def plugin_source_error_envelope(exc: PluginSourceError) -> dict[str, str]:
    envelope = {"reason": f"source_{exc.reason}", "source_base": str(exc.base)}
    if exc.allowed_root is not None:
        envelope["allowed_root"] = str(exc.allowed_root)
    return envelope


# LLM: 只读结构化字段（allowed_root、source_base），不回显用户给的路径；模型看到的异常文本与用户看到的回执说明同源。
# 函数用途: 生成“插件来源未获授权”的中文说明，告诉用户该把包放到哪里。
def source_unauthorized_message(details: dict) -> str:
    root = str(details.get("allowed_root") or "")
    where = f"请把插件包放到 {root} 下再试" if root else "请把插件包放到当前会话工作区（或显式授权的目录）下再试"
    base = str(details.get("source_base") or "")
    relative = f"相对路径按当前会话工作区 {base} 解析" if base else "相对路径按当前会话工作区解析"
    return f"插件来源未获授权：这个路径在当前身份可访问的范围之外。{where}（{relative}）。"


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
        # 越权时给出当前 owner 允许放包的根目录：只用路径策略冻结的结构化根，不泄露其它 owner 或宿主私有路径。
        root = Path(policy.owner_scope_root) if policy.owner_scope_root is not None else None
        details = {"allowed_root": str(root or ""), "source_base": str(workspace)}
        raise PluginSourceError("unauthorized", source_unauthorized_message(details), source=source, base=workspace,
                                allowed_root=root)
    content = read_bytes_beneath(Path(path.anchor), path.parts[1:], max_bytes=max_bytes, require_dir_fd=True)
    if content is None:
        raise PluginSourceError("not_found", f"插件来源不存在：{source}。", source=source, base=workspace)
    return content
