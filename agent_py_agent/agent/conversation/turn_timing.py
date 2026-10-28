# LLM: 会话层仅格式化既有计时事实；不导入CLI、不创建计时器或完成事件，历史非法时标不得补now。
# 模块用途: 为后台历史和终端共用冻结时间、耗时与终态的展示格式。
from __future__ import annotations

import hashlib
import math
from datetime import datetime


# LLM: 纯展示接口，独立于旧中文format_elapsed合同；不得根据格式化文案推断运行终态。
# 函数用途: 将耗时显示为秒、分钟或小时的简短英文单位。
def format_turn_duration(elapsed_seconds: float) -> str:
    if not math.isfinite(elapsed_seconds):
        return "未知"
    seconds = max(0, int(round(elapsed_seconds)))
    if 0 < elapsed_seconds < 1:
        seconds = 1
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


# LLM: 纯格式化给定终态和种子，不能用Python随机hash破坏跨进程回放；失败不显示成功。
# 函数用途: 生成Claude风格的单行回合耗时。
def format_turn_completion_line(elapsed_seconds: float, *, status: str = "done", seed_key: str = "", verb: str | None = None) -> str:
    duration = format_turn_duration(elapsed_seconds)
    if status in {"cancelled", "interrupted"}:
        return f"✻ Interrupted after {duration}"
    if status in {"failed", "error"}:
        return f"✻ Failed after {duration}"
    if status not in {"done", "succeeded", "completed"}:
        return f"✻ {status} after {duration}"
    verbs = ("Baked", "Brewed", "Churned", "Cogitated", "Cooked", "Crafted", "Crunched", "Sautéed", "Worked")
    index = int.from_bytes(hashlib.sha256(seed_key.encode("utf-8")).digest()[:8], "big") % len(verbs)
    return f"✻ {verb or verbs[index]} for {duration}"


# LLM: 默认墙钟只供实时调用；恢复必须传冻结时刻，不能用本函数给旧历史补造事实。
# 函数用途: 为成功回合统一输出Worked、耗时与带时区的完成日期。
def format_completion_line(elapsed_s: float, completed_at: object = None) -> str:
    if completed_at is None or completed_at == "":
        ended = datetime.now().astimezone()
    elif isinstance(completed_at, datetime):
        ended = completed_at.astimezone()
    elif isinstance(completed_at, str):
        ended = datetime.fromisoformat(completed_at.replace("Z", "+00:00")).astimezone()
    else:
        ended = datetime.fromtimestamp(completed_at).astimezone()
    return f"✻ Worked for {format_turn_duration(elapsed_s)} · 完成于 {ended:%Y-%m-%d %H:%M:%S %Z}"


# LLM: 结构化展示必须有可转换的冻结时标；非法历史返回空串而非补now，纯实时formatter仍支持默认墙钟。
# 函数用途: 在TUI消息尾标显示稳定动词、耗时与原完成时刻。
def render_turn_timing(data: dict) -> str:
    ended = data.get("completed_at")
    if type(ended) not in (int, float):
        return ""
    try:
        datetime.fromtimestamp(ended).astimezone()
    except (OverflowError, OSError, ValueError):
        return ""
    seconds = data.get("elapsed_seconds")
    if data.get("status") == "completed":
        return format_completion_line(seconds if seconds is not None else float("nan"), data["completed_at"])
    line = format_turn_completion_line(seconds if seconds is not None else float("nan"),
        status=str(data.get("status") or "unknown"), seed_key=str(data.get("logical_id") or ""))
    label = {"completed": "完成于", "failed": "失败于", "interrupted": "已中断于",
             "blocked": "受阻于", "unfinished": "未完成于"}.get(data.get("status"), "记录于")
    ended_text = datetime.fromtimestamp(ended).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    return f"{line} · {label} {ended_text}"
