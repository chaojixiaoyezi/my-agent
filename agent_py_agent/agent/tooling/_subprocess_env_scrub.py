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

# 安全前缀:这些一定放行(命令干活/定位所必需),即便名字里恰好含上面的子串也不误剥。
_SAFE_PREFIXES = ("PATH", "HOME", "USER", "LANG", "LC_", "TERM", "TMP", "SHELL", "PWD",
                  "PYTHON", "PIP_", "VIRTUAL_ENV", "XDG_", "SSL_CERT", "LOGNAME")


def _is_self_managed_credential(name: str) -> bool:
    """是否我们自管的凭据(精确名或通用密钥子串)。安全前缀优先放行,不误剥。"""
    if any(name.startswith(p) for p in _SAFE_PREFIXES):
        return False
    if name in _SELF_MANAGED_CREDENTIAL_ENV:
        return True
    upper = name.upper()
    return any(s in upper for s in _SECRET_NAME_SUBSTRINGS)


def scrub_subprocess_env(env: dict[str, str], *, passthrough: frozenset[str] = frozenset()) -> dict[str, str]:
    """从传给子进程的环境里剥掉自管凭据。passthrough=operator/skill 显式放行的变量名——但
    【不能覆盖自管凭据】(抄 GHSA 修复):第三方 key 放行有效,自管凭据的放行声明一律无视。
    返回擦洗后的新 dict(不改原 env)。"""
    scrubbed: dict[str, str] = {}
    for name, value in env.items():
        if _is_self_managed_credential(name):
            # 自管凭据:即便在 passthrough 里也删(GHSA:声明覆盖不了自管凭据)。
            if name in _SELF_MANAGED_CREDENTIAL_ENV or not _passthrough_allows_nonself(name, passthrough):
                continue
        scrubbed[name] = value
    return scrubbed


def _passthrough_allows_nonself(name: str, passthrough: frozenset[str]) -> bool:
    """非精确自管名(仅子串命中的第三方密钥)才允许被显式放行透传;精确自管名永不放行。"""
    return name in passthrough


__all__ = ["scrub_subprocess_env"]
