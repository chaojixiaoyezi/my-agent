# LLM: 当前 owner 的配置目录唯一保存 IM 发送者临时偏好；有效期只决定新受理任务，不取消或重路由既有任务。
# 模块用途: 保存当天自动委派开关，到下一业务零点自动失效，Gateway 停机后恢复也不延长有效期。
from __future__ import annotations

import hashlib
import math
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ..conversation.channels import supports_transcript_delivery
from ..gateway_parts.io import read_json_file_report, update_json_file_atomic
from ..runtime_errors import DataCorruptionError

_SCHEMA = "im_user_preferences.v1"


# LLM: 地址只由宿主 home 固定，不接受用户或模型路径；调用方继续使用原 owner 授权与文件事务。
# 函数用途: 返回当前用户 IM 偏好的唯一配置文件位置，不创建文件。
def preference_path(agent: object) -> Path:
    return Path(agent.home_paths.owner_home_dir) / "config" / "im_preferences.json"


# LLM: 偏好身份只接受可信入站渠道与发送者；群共享 workspace 不代表发送者开关可影响其它人。
# 函数用途: 将原 IM 用户身份转换成有界配置键，拒绝本地 TUI 或缺发送者的入口。
def preference_key(channel: str, actor_id: str) -> str:
    if not channel or supports_transcript_delivery(channel) or not actor_id or len(actor_id) > 1024:
        raise ValueError("自动委派设置只支持具有发送者身份的 IM")
    return hashlib.sha256(f"{channel}\x1f{actor_id}".encode()).hexdigest()


# LLM: 新文件只在控制事务中创建；旧记录 owner/schema 不符和坏条目均拒绝，不按模型文本补字段或清空配置。
# 函数用途: 校验当前用户的偏好记录，供只读和原子控制更新共用。
def _validated(payload: dict, owner_id: str) -> dict:
    if not payload:
        return {"schema_version": _SCHEMA, "owner_id": owner_id, "users": {}}
    if (set(payload) != {"schema_version", "owner_id", "users"} or payload["schema_version"] != _SCHEMA
            or payload["owner_id"] != owner_id or not isinstance(payload["users"], dict) or len(payload["users"]) > 4096):
        raise DataCorruptionError("IM 偏好记录无效")
    for key, row in payload["users"].items():
        if (not isinstance(key, str) or len(key) != 64 or not isinstance(row, dict)
                or set(row) != {"enabled", "expires_at", "business_day", "timezone", "revision"}
                or type(row["enabled"]) is not bool or type(row["revision"]) is not int or row["revision"] < 1
                or type(row["expires_at"]) not in {int, float} or not math.isfinite(row["expires_at"])
                or not isinstance(row["timezone"], str) or not isinstance(row["business_day"], str)):
            raise DataCorruptionError("IM 发送者偏好无效")
        try:
            ZoneInfo(row["timezone"])
            datetime.fromisoformat(row["business_day"])
        except (KeyError, ValueError, TypeError) as exc:
            raise DataCorruptionError("IM 偏好业务时区无效") from exc
    return payload


# LLM: 只读无副作用；effective_enabled 根据原记录业务日和到期时刻计算，倒退到别日也不能复活上一日设置。
# 函数用途: 返回当前 IM 发送者的偏好、版本和同一部署时区，未设置也不误显示固定 UTC；不运行模型或扫描用户。
def delegation_preference(agent: object, channel: str, actor_id: str, *, now: float | None = None) -> dict:
    key = preference_key(channel, actor_id)
    report = read_json_file_report(preference_path(agent), context="settings.im_preferences.read")
    if report.load_error is not None or report.lock_busy:
        raise DataCorruptionError("IM 偏好记录不可读")
    payload = _validated(report.payload or {}, str(agent.home_paths.owner_id))
    row = payload["users"].get(key)
    if row is None:
        from ..scheduler.schedule import _canonical_zone_name

        return {"enabled": False, "expires_at": 0.0, "revision": 0,
                "timezone": _canonical_zone_name(str(agent.config.timezone or ""))}
    current = time.time() if now is None else now
    day = datetime.fromtimestamp(current, ZoneInfo(row["timezone"])).date().isoformat()
    return {**row, "enabled": bool(row["enabled"] and day == row["business_day"] and current < row["expires_at"])}


# LLM: 控制写入只改变本人偏好，使用原配置 JSON 原子锁；执行任务以后保存该快照，不重读它来改变旧任务。
# 函数用途: 开启到下一部署时区零点，或立即关闭；重置无需零点定时器，写入不影响在途任务。
def set_delegation_preference(agent: object, channel: str, actor_id: str, enabled: bool, *, now: float | None = None) -> dict:
    key = preference_key(channel, actor_id)
    if type(enabled) is not bool:
        raise ValueError("IM 委派开关必须是布尔值")
    current = time.time() if now is None else now
    from ..scheduler.schedule import _canonical_zone_name

    timezone = _canonical_zone_name(str(agent.config.timezone or ""))
    local = datetime.fromtimestamp(current, ZoneInfo(timezone))
    midnight = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), tzinfo=ZoneInfo(timezone))

    # LLM: updater 在原 JSON 锁内校验整个记录，再仅前进本人版本；不能覆盖其它群发送者或并发写入。
    # 函数用途: 在一次配置提交中保存新的每日有效期和版本。
    def update(payload):
        payload = _validated(payload, str(agent.home_paths.owner_id))
        if key not in payload["users"] and len(payload["users"]) >= 4096:
            raise ValueError("IM 发送者偏好数量超出上限")
        previous = payload["users"].get(key, {})
        row = {"enabled": enabled, "expires_at": midnight.timestamp() if enabled else 0.0,
               "business_day": local.date().isoformat(), "timezone": timezone, "revision": previous.get("revision", 0) + 1}
        return {**payload, "users": {**payload["users"], key: row}}

    update_json_file_atomic(preference_path(agent), update)
    return delegation_preference(agent, channel, actor_id, now=current)
