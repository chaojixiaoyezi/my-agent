# LLM: Shell/PTY 子进程凭据过滤的唯一入口；显式传递不能覆盖本服务自管凭据，安全前缀不能绕过密钥名检查。
# 模块用途: 启动子进程前剥离模型与网关密钥，保留普通运行变量及操作者显式放行的第三方凭据。
from __future__ import annotations

"""子进程环境凭据擦洗(选项1-C,抄 长期助手 local.py + env_passthrough 的 GHSA 修复)。

真机实锤(bwrap --share-net 放行外网 + _subprocess_text_env 直接 dict(os.environ)):run_command
子进程能看到 MINIMAX_API_KEY / AGENT_API_KEY / 飞书 app_secret,一句 `curl 带 $KEY` 就外泄。
本模块在 spawn 前剥掉【我们自管的凭据】:命令干活要的普通变量(PATH/HOME/LANG…)留着,密钥不给。

抄 长期助手 GHSA-rhgp-j443-p4rf 那条:放行注册表【不能覆盖自管凭据】——第三方 key 可显式放行,
但我们自己的 provider/网关凭据永远删、任何声明都盖不掉(防恶意 skill/子代理把 KEY 声明成放行偷走)。
"""

import os

# 我们自管的凭据环境变量(精确名):模型 provider key、网关/通道密钥。永远剥,不可被放行覆盖。
_SELF_MANAGED_CREDENTIAL_ENV = frozenset(
    {
        "MINIMAX_API_KEY",
        "AGENT_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "OPENAI_API_KEY",
        "FEISHU_APP_SECRET",
        "FEISHU_ENCRYPT_KEY",
        "FEISHU_VERIFICATION_TOKEN",
        "QQ_APP_SECRET",
        "QQ_TOKEN",
        "MEMORY_SECRET",
        "AUDIT_SECRET",
        "AUDIT_TOKEN",
        "GITHUB_PERSONAL_ACCESS_TOKEN",
        "GH_TOKEN",
    }
)

# 通用密钥名子串:名字含这些的一律剥(兜住上面没枚举到的第三方密钥进子进程默认不给)。
_SECRET_NAME_SUBSTRINGS = ("API_KEY", "SECRET", "_TOKEN", "PASSWORD", "PASSWD", "CREDENTIAL", "PRIVATE_KEY")

# LLM: 名称检查统一不区分大小写；普通 PATH/HOME 无密钥片段自然保留，不引入前缀通行证。
# 函数用途: 判断变量名是否属于默认需要擦除的凭据。
def _is_self_managed_credential(name: str) -> bool:
    """按精确自管名或密钥片段识别，不因 HOME/USER/PYTHON 等前缀放行。"""
    upper = name.upper()
    if upper in _SELF_MANAGED_CREDENTIAL_ENV:
        return True
    return any(s in upper for s in _SECRET_NAME_SUBSTRINGS)


# LLM: 返回副本、不改宿主环境；自管名优先拒绝，第三方凭据只接受精确显式 passthrough。
# 函数用途: 为 Shell 与 PTY 构造去密钥的子进程环境。
def scrub_subprocess_env(env: dict[str, str], *, passthrough: frozenset[str] = frozenset()) -> dict[str, str]:
    """从传给子进程的环境里剥掉自管凭据。passthrough=operator/skill 显式放行的变量名——但
    【不能覆盖自管凭据】(抄 GHSA 修复):第三方 key 放行有效,自管凭据的放行声明一律无视。
    返回擦洗后的新 dict(不改原 env)。"""
    scrubbed: dict[str, str] = {}
    for name, value in env.items():
        if _is_self_managed_credential(name):
            # 自管凭据:即便在 passthrough 里也删(GHSA:声明覆盖不了自管凭据)。
            if name.upper() in _SELF_MANAGED_CREDENTIAL_ENV or not _passthrough_allows_nonself(name, passthrough):
                continue
        scrubbed[name] = value
    return scrubbed


# LLM: 调用方已排除自管凭据；不能用前缀或模糊匹配扩大透传范围。
# 函数用途: 检查操作者是否显式放行这个第三方变量。
def _passthrough_allows_nonself(name: str, passthrough: frozenset[str]) -> bool:
    """非精确自管名(仅子串命中的第三方密钥)才允许被显式放行透传;精确自管名永不放行。"""
    return name in passthrough


__all__ = ["scrub_subprocess_env"]
