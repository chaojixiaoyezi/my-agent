# LLM: G3 宿主客户端的凭据选择；本机凭据只经 G1 load 读取，配置 token 优先且只发一个头，不创建或修复凭据。
#   凭据读不到时按 G2b 开关 gateway_require_local_credential 分流：开关关（默认，G2a 阶段）降级为不带凭据继续发送，
#   只记一次结构化 warning；开关开才维持原行为——在网络请求前抛 G1 原因码。目前只有客户端读这个开关，服务端强制属于 G2b（未落地）。
# 模块用途: 给 TUI、CLI 和 IM 的现有身份头入口提供同一凭据；读取失败按 G2b 开关决定降级或拒绝。
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

from ..user_space.home_layout import home_paths
from ..user_space.home_root import configured_home_root
from .local_client_token import LocalClientCredentialError, load_local_client_credential

logger = logging.getLogger(__name__)

# 同一进程内同一原因只记一次 warning，避免 TUI/适配器高频轮询刷屏；锁保护集合的读写。
_WARNED_CREDENTIAL_REASONS: set[str] = set()
_CREDENTIAL_WARNINGS_LOCK = threading.Lock()


# LLM: 仅保存宿主进程内的路径和配置 token；repr 不显示它们，不进状态、环境、argv 或持久队列。
# 类用途: 在原请求头构造时选择配置 token 或数据根里的本机凭据；失败时按 G2b 开关决定降级还是拒绝。
@dataclass(frozen=True)
class GatewayClientCredentials:
    data_root: Path = field(repr=False)
    configured_token: str = field(default="", repr=False)
    require_local_credential: bool = False

    # LLM: 配置 token 非空就沿原部署合同使用，不再读本机文件。本机凭据失败时：开关开=抛 G1 原因码（零请求）；
    #   开关关=记一次 warning 后返回空头（照常发送，服务端按 G2a 计入无凭据）。不降级成配置 token、不重试。
    # 函数用途: 返回唯一的 X-Gateway-Token 头；读取失败按 G2b 开关降级或拒绝，绝不回显凭据内容或路径。
    def headers(self) -> dict[str, str]:
        if self.configured_token:
            return {"X-Gateway-Token": self.configured_token}
        try:
            token = load_local_client_credential(self.data_root)
        except LocalClientCredentialError as exc:
            if self.require_local_credential:
                exc.args = (credential_error_message(exc),)
                raise
            _warn_credential_degraded(exc.reason_code)
            return {}
        return {"X-Gateway-Token": token}


# LLM: 结构化 warning 只带 G1 原因码，不带凭据内容、路径或底层异常；同一进程同一原因只记一次（线程安全）。
#   本函数只写日志，不改请求、不写状态；开关开时不会走到这里（那条路直接抛错）。
# 函数用途: 凭据不可用时提示一次宿主原因，但不打断本次请求；首次之后静默，避免轮询刷屏。
def _warn_credential_degraded(reason_code: str) -> None:
    with _CREDENTIAL_WARNINGS_LOCK:
        if reason_code in _WARNED_CREDENTIAL_REASONS:
            return
        _WARNED_CREDENTIAL_REASONS.add(reason_code)
    logger.warning(
        "本机客户端凭据不可用（reason_code=%s）；本次请求不带 X-Gateway-Token 继续发送，"
        "Gateway 会按无凭据来源计数。",
        reason_code,
    )


# LLM: 文案只读取 G1 结构化原因；不插入底层异常、文件路径、内容或配置值，供控制和插件回执共用。
# 函数用途: 告诉用户为什么没有发送请求以及应让宿主检查什么。
def credential_error_message(exc: LocalClientCredentialError) -> str:
    if exc.reason_code == "LOCAL_CLIENT_CREDENTIAL_MISSING":
        action = "请先启动一次 Gateway，再重启客户端。"
    elif exc.reason_code == "LOCAL_CLIENT_CREDENTIAL_PERMISSIONS":
        action = "请让宿主检查 Gateway 凭据的属主及私有权限，再重启客户端。"
    else:
        action = "请让宿主检查 Gateway 凭据文件完整性和可读性，再重启客户端。"
    return f"请求未发送（{exc.reason_code}）。{action}"


# LLM: 数据根优先沿原 Agent.home_paths.root，不能拿工作目录、队列根或 owner home 当全局 secrets 根；轻量替身用原配置解析器。
#   G2b 开关从 AgentConfig 读取，缺省 False（G2a 阶段不强制）；目前只有客户端读它，服务端强制等 G2b 落地。
# 函数用途: 取得当前宿主客户端的凭据来源，不在构造阶段读秘密或发请求。
def gateway_client_credentials(agent: object) -> GatewayClientCredentials:
    config = getattr(agent, "config", None)
    root = getattr(getattr(agent, "home_paths", None), "root", None)
    if not isinstance(root, (str, Path)):
        root = home_paths(configured_home_root(config)).root
    token = getattr(config, "gateway_auth_token", "")
    require = bool(getattr(config, "gateway_require_local_credential", False))
    return GatewayClientCredentials(
        Path(root), token if isinstance(token, str) else "", require,
    )
