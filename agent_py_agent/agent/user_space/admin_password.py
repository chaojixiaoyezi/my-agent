# LLM: 本模块是管理员密码与失败节流的唯一权威：config/admin-password.json 只存 scrypt 派生值和参数（0600，目录 0700），
#   明文只在调用栈里用于一次派生；返回值、日志、异常文本都不能带密码或散列。失败节流按渠道身份持久计数，
#   锁定期内既不校验也不加计数；拒绝文案不区分“密码错”与“被锁”，只附剩余锁定时间。改动时同步检查
#   gateway_parts/admin_control_service.py、cli/admin_identity_commands.py 与 test_admin_identity_store.py。
# 模块用途: 让本机 CLI 设置、查看、清除管理员密码，并让 Gateway 的 /admin、/approve 校验密码、挡住暴力猜测。

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from ..common.json_io import locked_json_path
from ..common.nofollow_fs import read_text_beneath, unlink_file_beneath, write_text_atomic_beneath

ADMIN_PASSWORD_SCHEMA = "admin_password.v1"
ADMIN_PASSWORD_ATTEMPTS_SCHEMA = "admin_password_attempts.v1"
ADMIN_PASSWORD_MIN_LENGTH = 8
ADMIN_PASSWORD_MAX_FAILURES = 5
ADMIN_PASSWORD_FAILURE_WINDOW_SECONDS = 600.0
ADMIN_PASSWORD_LOCK_SECONDS = 600.0
CONFIG_DIR_NAME = "config"
_PASSWORD_FILE = "admin-password.json"
_ATTEMPTS_FILE = "admin-password-attempts.json"
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16
_HASH_BYTES = 32
_SCRYPT_MAXMEM = 64 * 1024 * 1024
_REFUSAL_TEXT = "管理员身份验证未通过。"


# LLM: 文案固定且不含密码；调用方只能展示或记录错误类型，不能把异常当作授权结果。
# 类用途: 表示密码不符合要求，或密码/节流记录不可读、已损坏、无法写入。
class AdminPasswordError(ValueError):
    pass


# LLM: ok 是唯一授权结果；retry_after_seconds>0 只说明当前处于锁定期，不透露密码是否正确。
# 类用途: 表示一次管理员密码校验的结论和剩余锁定秒数。
@dataclass(frozen=True)
class AdminPasswordCheck:
    ok: bool
    retry_after_seconds: float = 0.0


# LLM: 路径只由可信 home 根推导，不接受请求传入的路径。
# 函数用途: 返回管理员密码文件位置。
def admin_password_path(home_root: str | Path) -> Path:
    return Path(home_root) / CONFIG_DIR_NAME / _PASSWORD_FILE


# LLM: 规则只看长度和字符类别，不做强度猜测；首尾空白与控制字符会让 IM 里的 `/admin <密码>` 无法无歧义地还原，所以拒绝。
# 函数用途: 检查新密码是否可以设置，不合格时抛出固定中文说明。
def validate_admin_password(password: object) -> str:
    if not isinstance(password, str) or len(password) < ADMIN_PASSWORD_MIN_LENGTH:
        raise AdminPasswordError(f"管理员密码至少 {ADMIN_PASSWORD_MIN_LENGTH} 个字符。")
    if password != password.strip():
        raise AdminPasswordError("管理员密码首尾不能有空白字符。")
    if any(not character.isprintable() for character in password):
        raise AdminPasswordError("管理员密码不能包含换行、制表符或其他控制字符。")
    return password


# LLM: 有副作用：用随机盐派生 scrypt 值后原子替换 admin-password.json（0600），并把 config 目录收紧到 0700；
#   旧密码被整体替换，已绑定的 IM 身份和失败计数不受影响。返回值只含公开状态。
# 函数用途: 设置或更换管理员密码，只由本机 CLI 调用。
def set_admin_password(home_root: str | Path, password: object, *, now: float | None = None) -> dict[str, object]:
    secret = validate_admin_password(password)
    salt = secrets.token_bytes(_SALT_BYTES)
    record = {
        "schema": ADMIN_PASSWORD_SCHEMA,
        "algorithm": "scrypt",
        "n": _SCRYPT_N,
        "r": _SCRYPT_R,
        "p": _SCRYPT_P,
        "salt": base64.b64encode(salt).decode("ascii"),
        "hash": base64.b64encode(_derive(secret, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _HASH_BYTES)).decode("ascii"),
        "updated_at": time.time() if now is None else float(now),
    }
    write_private_config_json(home_root, _PASSWORD_FILE, record)
    return admin_password_status(home_root)


# LLM: 有副作用：只删除密码文件，不删绑定与节流记录；返回删除前是否存在。
# 函数用途: 清除管理员密码，之后 /admin 与 /approve 一律验证不通过。
def clear_admin_password(home_root: str | Path) -> bool:
    existed = admin_password_path(home_root).exists()
    unlink_file_beneath(home_root, (CONFIG_DIR_NAME, _PASSWORD_FILE))
    return existed


# LLM: 只读；返回值不含盐、散列或参数以外的任何秘密，损坏文件报告 state=invalid 而不是当作已设置。
# 函数用途: 给 CLI 展示管理员密码是否已设置及更新时间。
def admin_password_status(home_root: str | Path) -> dict[str, object]:
    try:
        record = _read_password_record(home_root)
    except AdminPasswordError:
        return {"configured": False, "state": "invalid"}
    if record is None:
        return {"configured": False, "state": "missing"}
    return {"configured": True, "state": "configured", "algorithm": "scrypt", "updated_at": record["updated_at"]}


# LLM: 有副作用：在节流文件锁内完成“查锁定→派生比较→记失败或清零”，并写回 admin-password-attempts.json（0600）。
#   attempt_key 必须是可信渠道身份（如 feishu:ou_x）；锁定期内直接拒绝、不派生也不加计数；未设置或损坏的密码文件
#   与错误密码走同一分支（先做一次等价派生），不暴露差别。节流文件损坏时抛 AdminPasswordError，调用方须拒绝而不是放行。
# 函数用途: 校验一次管理员密码，并按渠道身份执行“10 分钟内错 5 次锁 10 分钟”。
def verify_admin_password(
    home_root: str | Path,
    password: object,
    *,
    attempt_key: str,
    now: float | None = None,
) -> AdminPasswordCheck:
    key = str(attempt_key or "").strip()
    if not key:
        raise ValueError("admin password attempt key is required")
    moment = time.time() if now is None else float(now)
    attempts_path = Path(home_root) / CONFIG_DIR_NAME / _ATTEMPTS_FILE
    attempts_path.parent.mkdir(parents=True, exist_ok=True)
    with locked_json_path(attempts_path):
        identities = _read_attempts(home_root)
        entry = identities.get(key) or {}
        locked_until = float(entry.get("locked_until") or 0.0)
        if locked_until > moment:
            return AdminPasswordCheck(False, locked_until - moment)
        if _password_matches(home_root, password):
            if identities.pop(key, None) is not None:
                _write_attempts(home_root, identities, moment)
            return AdminPasswordCheck(True)
        failures = [value for value in entry.get("failures") or () if moment - ADMIN_PASSWORD_FAILURE_WINDOW_SECONDS < value <= moment]
        failures.append(moment)
        if len(failures) >= ADMIN_PASSWORD_MAX_FAILURES:
            identities[key] = {"failures": [], "locked_until": moment + ADMIN_PASSWORD_LOCK_SECONDS}
        else:
            identities[key] = {"failures": failures, "locked_until": 0.0}
        _write_attempts(home_root, identities, moment)
        return AdminPasswordCheck(False, max(0.0, float(identities[key]["locked_until"]) - moment))


# LLM: 文案只含通用拒绝与可选的剩余分钟数；不能加“密码错误”“已锁定”等能区分原因的字样。
# 函数用途: 把一次失败的校验结果变成给用户看的固定拒绝文案。
def admin_password_refusal_message(check: AdminPasswordCheck) -> str:
    if check.retry_after_seconds <= 0:
        return _REFUSAL_TEXT
    minutes = max(1, math.ceil(check.retry_after_seconds / 60.0))
    return f"{_REFUSAL_TEXT}请约 {minutes} 分钟后再试。"


# LLM: 有副作用：经 no-follow 原子写入 home/config 下的一个文件（0600），并把 config 目录收紧到 0700；只供本模块与绑定模块使用。
# 函数用途: 私密地原子保存一个管理员身份相关的 JSON 文件。
def write_private_config_json(home_root: str | Path, file_name: str, payload: dict[str, object]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    write_text_atomic_beneath(home_root, (CONFIG_DIR_NAME, file_name), text)
    os.chmod(Path(home_root) / CONFIG_DIR_NAME, 0o700)


# LLM: 缺失返回 None；链接、非普通文件、坏 UTF-8、坏 JSON 或非对象都抛 AdminPasswordError，调用方按损坏处理。
# 函数用途: 私密地读取 home/config 下的一个 JSON 对象。
def read_private_config_json(home_root: str | Path, file_name: str) -> dict[str, object] | None:
    try:
        text = read_text_beneath(home_root, (CONFIG_DIR_NAME, file_name))
        payload = None if text is None else json.loads(text)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise AdminPasswordError("管理员身份记录无法读取。") from exc
    if payload is not None and not isinstance(payload, dict):
        raise AdminPasswordError("管理员身份记录格式无效。")
    return payload


# LLM: 参数上限防止坏文件让一次校验占用过多内存或时间；任何字段不合规都视为损坏，不能退化为“无密码可登录”。
# 函数用途: 读取并校验密码记录，缺失返回 None。
def _read_password_record(home_root: str | Path) -> dict[str, object] | None:
    payload = read_private_config_json(home_root, _PASSWORD_FILE)
    if payload is None:
        return None
    try:
        n, r, p = (int(payload["n"]), int(payload["r"]), int(payload["p"]))
        salt = base64.b64decode(str(payload["salt"]), validate=True)
        digest = base64.b64decode(str(payload["hash"]), validate=True)
        updated_at = float(payload["updated_at"])
    except (KeyError, TypeError, ValueError, binascii.Error) as exc:
        raise AdminPasswordError("管理员密码记录已损坏。") from exc
    valid = (
        payload.get("schema") == ADMIN_PASSWORD_SCHEMA
        and payload.get("algorithm") == "scrypt"
        and n >= 2**10 and n & (n - 1) == 0 and 1 <= r <= 32 and 1 <= p <= 16
        and 128 * n * r * p <= _SCRYPT_MAXMEM // 2
        and len(salt) >= _SALT_BYTES and 16 <= len(digest) <= 64
        and math.isfinite(updated_at)
    )
    if not valid:
        raise AdminPasswordError("管理员密码记录已损坏。")
    return {"n": n, "r": r, "p": p, "salt": salt, "hash": digest, "updated_at": updated_at}


# LLM: 未设置或损坏时仍做一次同成本派生再返回 False，避免用耗时区分“没设密码”和“密码错”。
# 函数用途: 用恒定时间比较判断明文是否与已保存的派生值一致。
def _password_matches(home_root: str | Path, password: object) -> bool:
    secret = password if isinstance(password, str) else ""
    try:
        record = _read_password_record(home_root)
    except AdminPasswordError:
        record = None
    if record is None:
        _derive(secret, bytes(_SALT_BYTES), _SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _HASH_BYTES)
        return False
    digest = _derive(secret, record["salt"], record["n"], record["r"], record["p"], len(record["hash"]))
    return hmac.compare_digest(digest, record["hash"])


# LLM: 纯函数；只在内存中派生，不记录任何输入。
# 函数用途: 计算 scrypt 派生值。
def _derive(secret: str, salt: bytes, n: int, r: int, p: int, length: int) -> bytes:
    return hashlib.scrypt(secret.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=length, maxmem=_SCRYPT_MAXMEM)


# LLM: 损坏的节流记录必须抛错让调用方拒绝，不能当作“没有失败记录”而放开暴力猜测。
# 函数用途: 读取各渠道身份的失败时间与锁定到期时间。
def _read_attempts(home_root: str | Path) -> dict[str, dict[str, object]]:
    payload = read_private_config_json(home_root, _ATTEMPTS_FILE)
    if payload is None:
        return {}
    identities = payload.get("identities")
    if payload.get("schema") != ADMIN_PASSWORD_ATTEMPTS_SCHEMA or not isinstance(identities, dict):
        raise AdminPasswordError("管理员密码失败记录已损坏。")
    result: dict[str, dict[str, object]] = {}
    for key, entry in identities.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("failures", []), list):
            raise AdminPasswordError("管理员密码失败记录已损坏。")
        try:
            failures = [float(value) for value in entry.get("failures", [])]
            locked_until = float(entry.get("locked_until") or 0.0)
        except (TypeError, ValueError) as exc:
            raise AdminPasswordError("管理员密码失败记录已损坏。") from exc
        result[str(key)] = {"failures": failures, "locked_until": locked_until}
    return result


# LLM: 有副作用：在调用方已持节流文件锁时写回；顺手丢掉窗口外且未锁定的旧身份，文件不会无限增长。
# 函数用途: 保存失败节流记录。
def _write_attempts(home_root: str | Path, identities: dict[str, dict[str, object]], moment: float) -> None:
    active = {
        key: entry
        for key, entry in identities.items()
        if float(entry.get("locked_until") or 0.0) > moment
        or any(moment - ADMIN_PASSWORD_FAILURE_WINDOW_SECONDS < value for value in entry.get("failures") or ())
    }
    write_private_config_json(
        home_root,
        _ATTEMPTS_FILE,
        {"schema": ADMIN_PASSWORD_ATTEMPTS_SCHEMA, "identities": active, "updated_at": moment},
    )


__all__ = [
    "ADMIN_PASSWORD_LOCK_SECONDS",
    "ADMIN_PASSWORD_MAX_FAILURES",
    "ADMIN_PASSWORD_MIN_LENGTH",
    "AdminPasswordCheck",
    "AdminPasswordError",
    "admin_password_path",
    "admin_password_refusal_message",
    "admin_password_status",
    "clear_admin_password",
    "read_private_config_json",
    "set_admin_password",
    "validate_admin_password",
    "verify_admin_password",
    "write_private_config_json",
]
