"""时区感知时钟(审计 #21):面向几十国用户按其 IANA 时区渲染日期/时间/周历。

时区解析顺序及无效配置的降级原则：
  1. 环境变量 AGENT_TIMEZONE(最高优先,Supervisor/容器注入)
  2. 传入的配置时区名(来自 AgentConfig.timezone)
  3. 回退服务器本地时间(datetime.now().astimezone(),仍是 tz-aware)
非法时区串只告警并安全回退,绝不因坏配置崩进程。周起始随 locale 可配(monday/sunday/saturday)——
美国/中东周日或周六起始地区"本周"不再按服务器周一算错。基于 stdlib zoneinfo;生产环境无系统
zoneinfo 数据时由 tzdata 包兜底(见 pyproject 依赖)。
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# locale 周起始 → Python weekday() 索引(Mon=0..Sun=6)。未知值退回 monday。
_WEEK_START_INDEX = {"monday": 0, "saturday": 5, "sunday": 6}


def resolve_zone(name: str) -> ZoneInfo | None:
    """校验 IANA 时区名:合法返回 ZoneInfo,空名返回 None(=服务器本地),非法告警并返回 None。"""
    candidate = (name or "").strip()
    if not candidate:
        return None
    try:
        return ZoneInfo(candidate)
    except Exception as exc:  # ZoneInfoNotFoundError / 坏串 / 缺数据,一律安全回退
        logger.warning("非法时区 '%s':%s,回退服务器本地时间", candidate, exc)
        return None


def now(tz_name: str = "") -> datetime:
    """当前 tz-aware 时间:env AGENT_TIMEZONE > tz_name > 服务器本地。"""
    chosen = os.environ.get("AGENT_TIMEZONE", "").strip() or tz_name
    zone = resolve_zone(chosen)
    if zone is not None:
        return datetime.now(zone)
    return datetime.now().astimezone()  # 未配时区:服务器本地(仍 tz-aware,可带 %Z)


def week_start_date(today: date, week_start: str = "monday") -> date:
    """按 locale 周起始返回本周起始日期。monday/sunday/saturday,未知值退回 monday。"""
    start_index = _WEEK_START_INDEX.get((week_start or "monday").strip().lower(), 0)
    delta = (today.weekday() - start_index) % 7
    return today - timedelta(days=delta)
