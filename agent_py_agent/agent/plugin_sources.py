# LLM: 安装包与配置共用原 PathAccessPolicy 和 no-follow 文件原语；只读一次字节快照，不取得管理授权。
# 模块用途: 在固定工作根和读取预算内读取明确来源，拒绝链接及不支持安全打开的平台。

from __future__ import annotations

from pathlib import Path

from .common.nofollow_fs import read_bytes_beneath
from .path_access_policy import PathAccessPolicy


# LLM: 解析后的路径逐段从文件系统锚打开，不将任意 parent 当受信根；权限或能力不足不能降级普通 open。
# 函数用途: 在既有路径权限内读取一个有界普通文件，配置和安装不得复读变化的来源。
def read_plugin_source(source: str, workspace: Path, policy: PathAccessPolicy, *, max_bytes: int) -> bytes:
    path = Path(source).expanduser()
    if not path.is_absolute():
        path = workspace / path
    if path.is_symlink():
        raise ValueError("插件来源不能是链接")
    path = path.resolve(strict=True)
    if not policy.check(path).allowed:
        raise ValueError("插件来源未获授权")
    content = read_bytes_beneath(Path(path.anchor), path.parts[1:], max_bytes=max_bytes, require_dir_fd=True)
    if content is None:
        raise ValueError("插件来源不存在")
    return content
