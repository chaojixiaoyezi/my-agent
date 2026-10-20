# LLM: consumer与metadata发现共用纯时间判据；不读取配置/文件、不创建任务或授执行权，原batch/lease及来源末门仍由调用方负责。
# 模块用途: 统一策展的本地日期、间隔与下次时间边界，避免UTC发现和实际运行时区分叉。
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


# LLM: 完整保留consumer原时钟语义，空时区为服务器local，不新增环境优先级或坏时区fallback。
# 函数用途: 将同一时刻换成策展实际使用的日期和小时。
def localized_now(value: datetime | None, timezone_name: str) -> datetime:
    current = value or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if not timezone_name:
        return current.astimezone()
    return current.astimezone(ZoneInfo(timezone_name))


# LLM: 原consumer已规定坏/naive耐久时间视为缺失；发现层不得以不同parse规则延长资格。
# 函数用途: 解析带时区的原成功或失败时间。
def parse_time(value: str) -> datetime | None:
    if not str(value or "").strip():
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


# LLM: 仅判断原成功后时间窗，真实输入/batch条件仍在各调用方，不把mtime粗筛当作consumer已采集。
# 函数用途: 判断策展间隔是否已经过去。
def interval_elapsed(state, now: datetime, interval_seconds: int) -> bool:
    last_success = parse_time(state.last_success_at)
    return last_success is None or (now-last_success).total_seconds() >= interval_seconds


# LLM: 原consumer日终判据唯一实现，日期和小时必须来自同一本地clock，不能另读UTC日期。
# 函数用途: 判断本地今天是否已到收尾时间且尚未完成。
def daily_finalize_due(state, now: datetime, finalize_hour: int) -> bool:
    return now.hour >= finalize_hour and state.last_daily_finalize_date != now.date().isoformat()


# LLM: 只提供缓存失效时间而非timer/执行许可；空时区由naive目标日期timestamp重算系统规则，不能沿用当前fixed-offset跨DST。
# 函数用途: 计算下一次本地小时或次日零点对应的绝对时刻。
def next_local_boundary(current: datetime, timezone_name: str, hour: int, *, next_day: bool = False) -> float:
    day = current.date()+timedelta(days=int(next_day))
    target = datetime.combine(day,time(hour=hour))
    if timezone_name:
        target = target.replace(tzinfo=ZoneInfo(timezone_name))
    if target.timestamp() <= current.timestamp():
        target += timedelta(days=1)
    return target.timestamp()
