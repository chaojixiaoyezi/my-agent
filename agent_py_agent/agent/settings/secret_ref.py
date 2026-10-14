# LLM: SecretRef 解析和宿主来源名投影共用同一 env 语法；来源只给内部执行配置，不得从模型参数或正文恢复权限。
# 模块用途: 解析受管密钥引用、生成不含值的子进程擦洗上界，并为宿主日志脱敏。
"""密钥引用(SecretRef)解析 + 脱敏(#4 借鉴 通道运行时 secret-contract 的核心)。

让飞书等凭据字段的值不必内联明文进 YAML,而可写成间接引用:
  - ``env:FEISHU_APP_SECRET``  → 读环境变量(配 systemd EnvironmentFile,密钥不进源码树)
  - ``file:/etc/my-agent/feishu_secret`` → 读文件内容(去首尾空白)
  - 其余(普通字符串)         → 原样返回(兼容现状)

只做 env/file 两种来源(覆盖"密钥放源码树外"的核心诉求);通道运行时 还有 exec: 跑外部命令取密钥,
对本产品收益小、且引入子进程执行面,按简单优先暂不做。脱敏 redact() 供日志/回显用,杜绝明文泄漏。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import fields, is_dataclass, replace
from pathlib import Path

_ENV_PREFIX = "env:"
_FILE_PREFIX = "file:"


# LLM: 只按显式 env/file 来源解析；读取失败返回空给原缺凭据合同，不执行命令或打印明文。
# 函数用途: 让宿主用环境变量或文件提供密钥，普通文本保持原样。
def resolve_secret_ref(value: str) -> str:
    """把密钥引用解析成实际值。``env:NAME`` / ``file:/path`` / 普通字符串(原样)。
    解析失败(env 未设 / 文件读不到)返回空串,由上层按"凭据缺失"处理。"""
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if stripped.startswith(_ENV_PREFIX):
        return os.environ.get(stripped[len(_ENV_PREFIX):].strip(), "")
    if stripped.startswith(_FILE_PREFIX):
        path = stripped[len(_FILE_PREFIX):].strip()
        try:
            return Path(path).expanduser().read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return value  # 普通值原样返回(不 strip,保持现状行为)


# LLM: 与 resolver 共用前缀语法，只做引用类型识别，不证明凭据有效或授予读取权。
# 函数用途: 判断某个宿主配置值是否需要间接解析。
def is_secret_ref(value: str) -> bool:
    """是否为间接引用(env:/file:),用于判断该值是否需解析/不应直接当明文回显。"""
    if not isinstance(value, str):
        return False
    s = value.strip()
    return s.startswith(_ENV_PREFIX) or s.startswith(_FILE_PREFIX)


# LLM: 此处只产生日志摘要，不用于授权或子进程擦洗；调用方不得输出原密钥。
# 函数用途: 把宿主凭据替换为短脱敏值。
def redact(value: str, keep: int = 3) -> str:
    """脱敏:保留前 keep 位 + ``***``;空值返回空。用于日志/终端回显,杜绝明文密钥泄漏。"""
    if not value:
        return ""
    return f"{value[:keep]}***" if len(value) > keep else "***"


# LLM: 仅读取宿主 schema 声明的真实凭据来源；合并旧上界/api_key_env，不遍历 prompt、任意字串或任务正文。
# 函数用途: 收集子进程不可继承的准确环境来源名，支持已解析密钥和配置覆盖后的继承。
def managed_secret_environment_names(config: object) -> frozenset[str]:
    names = set(getattr(config, "host_credential_env_names", ()) or ())
    env_name = str(getattr(config, "api_key_env", "") or "").strip()
    if env_name:
        names.add(env_name)
    names.update(_managed_config_reference_names(config))
    return frozenset(name for name in names if isinstance(name, str) and name)


# LLM: 唯一字段权威是配置 schema 的 SecretRef/source metadata；来源账仅取同字段的 source，不解析其它键或自然语言。
# 函数用途: 从实际凭据字段及它们的宿主来源里收集准确环境名，不猜值、不递归普通配置内容。
def _managed_config_reference_names(config: object) -> set[str]:
    if not is_dataclass(config) or isinstance(config, type):
        return set()
    names: set[str] = set()
    sources = getattr(config, "config_sources", {}) or {}
    for item in fields(config):
        secret_ref = item.metadata.get("secret_ref", False)
        if not secret_ref and not item.metadata.get("credential_environment_source", False):
            continue
        if secret_ref and (name := _environment_reference_name(getattr(config, item.name))):
            names.add(name)
        source = sources.get(item.name, {}) if isinstance(sources, Mapping) else {}
        if isinstance(source, Mapping) and (name := _environment_reference_name(source.get("source"))):
            names.add(name)
    return names


# LLM: 输入是已声明凭据字段或其宿主 source；语法必须与原 resolver 的 strip/env 前缀完全一致。
# 函数用途: 取出一个明确环境引用的准确名称，普通凭据文本与文件引用返回空。
def _environment_reference_name(value: object) -> str:
    text = value.strip() if isinstance(value, str) else ""
    return text[len(_ENV_PREFIX):].strip() if text.startswith(_ENV_PREFIX) else ""


# LLM: 只有宿主 config 构建/克隆调用此入口；保留全部原字段，内部来源名是只增的安全上界，不是用户参数。
# 函数用途: 在选模型或清掉凭据引用前冻结来源名，使 owner、子孙和后台配置延续同一擦洗规则。
def freeze_managed_secret_environment(config):
    names = tuple(sorted(managed_secret_environment_names(config)))
    if getattr(config, "host_credential_env_names", ()) == names:
        return config
    return replace(config, host_credential_env_names=names)


__all__ = ["resolve_secret_ref", "is_secret_ref", "redact", "managed_secret_environment_names", "freeze_managed_secret_environment"]
