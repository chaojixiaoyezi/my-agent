"""Adapter and user identity config normalizers."""

# LLM: Identity field normalization is separate from runtime limits to keep config services readable.
# 模块用途: 归一化外部适配器账号字段和本地用户身份字段。

from __future__ import annotations

import os

from ._coercion import CoercionService


# LLM: AdapterFieldsService 属于 配置系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: AdapterFieldsService 封装 配置系统 的一组相关操作，供上层组合调用。
class AdapterFieldsService:
    """Normalize adapter-related config fields (feishu, qq, etc.)."""

    # LLM: AdapterFieldsService.normalize 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 归一化 AdapterFieldsService 负责的配置字段并追加告警。
    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        warnings: list[str] = []
        out = dict(data)

        def apply(key: str, coerced: object, warn: str | None) -> None:
            out[key] = coerced
            if warn:
                warnings.append(warn)

        for key in ("feishu_app_id", "feishu_app_secret", "feishu_verification_token", "feishu_encrypt_key"):
            out[key] = _string_config_value(out.get(key, defaults.feishu_app_id if key == "feishu_app_id" else ""))

        v, w = CoercionService.coerce_int(
            "feishu_callback_port", out.get("feishu_callback_port"),
            defaults.feishu_callback_port, min_val=1024, max_val=65535,
        )
        apply("feishu_callback_port", v, w)

        for key in ("qq_app_id", "qq_app_secret"):
            env_key = key.upper()
            env_val = os.environ.get(env_key, "")
            if env_val:
                out[key] = env_val
            else:
                out[key] = _string_config_value(out.get(key, defaults.qq_app_id if key == "qq_app_id" else ""))

        return out, warnings


# LLM: UserFieldsService 属于 配置系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: UserFieldsService 封装 配置系统 的一组相关操作，供上层组合调用。
class UserFieldsService:
    """Normalize user-related config fields."""

    # LLM: UserFieldsService.normalize 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 归一化 UserFieldsService 负责的配置字段并追加告警。
    @staticmethod
    def normalize(data: dict[str, object], defaults: object) -> tuple[dict[str, object], list[str]]:
        warnings: list[str] = []
        out = dict(data)
        _normalize_user_id(out, defaults, warnings)
        _normalize_user_data_root(out, defaults, warnings)
        _normalize_user_auth(out, defaults, warnings)
        _normalize_admin_user(out, defaults)
        return out, warnings


def _normalize_user_id(out: dict[str, object], defaults: object, warnings: list[str]) -> None:
    raw_user_id = out.get("user_id", defaults.user_id)
    if isinstance(raw_user_id, str) and raw_user_id.strip():
        out["user_id"] = raw_user_id.strip()
        return
    out["user_id"] = defaults.user_id
    warnings.append(f"user_id: expected a non-empty string, got {raw_user_id!r}; using default")


def _normalize_user_data_root(out: dict[str, object], defaults: object, warnings: list[str]) -> None:
    raw_user_data_root = out.get("user_data_root", defaults.user_data_root)
    if isinstance(raw_user_data_root, str) and raw_user_data_root.strip():
        out["user_data_root"] = raw_user_data_root.strip()
        return
    out["user_data_root"] = defaults.user_data_root
    warnings.append(f"user_data_root: expected a non-empty string, got {raw_user_data_root!r}; using default")


def _normalize_user_auth(out: dict[str, object], defaults: object, warnings: list[str]) -> None:
    value, warn = CoercionService.coerce_bool("auth_enabled", out.get("auth_enabled"), defaults.auth_enabled)
    out["auth_enabled"] = value
    if warn:
        warnings.append(warn)


def _normalize_admin_user(out: dict[str, object], defaults: object) -> None:
    raw_admin = out.get("admin_user_id", defaults.admin_user_id)
    out["admin_user_id"] = raw_admin.strip() if isinstance(raw_admin, str) and raw_admin.strip() else defaults.admin_user_id


def _string_config_value(value: object) -> str:
    return value if isinstance(value, str) else ""


__all__ = ["AdapterFieldsService", "UserFieldsService"]
