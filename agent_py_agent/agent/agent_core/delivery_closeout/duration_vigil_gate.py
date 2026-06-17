
from __future__ import annotations

"""持续值守类长任务的「时长未达即持续拦」软门(uncontracted 路径专用,保守、带防紧密循环节流)。

实锤背景(日志运营 2 小时真机测试):给主代理「用 log_ops 持续监控研判,运营至少 2 小时,
每隔约 5 分钟 wait→log_alert_poll 拉候选研判,持续到约 2 小时后再收尾」这类**无终点、靠
时长驱动**的值守任务,主代理 log_monitor_start 后只写了个中间值班笔记 shift_notes.md 到
work/,就被 uncontracted closeout「见产物可打开即判完成」判完成、发 [MAIN_AGENT_DELIVERY_
COMPLETE] 提前退出——只跑了约 5 轮 13 分钟,根本没撑到 2 小时。确定性 daemon 工作完美(不丢),
问题纯在 LLM 主代理这层撑不住持续值守。

本门只做一件很窄的事——当且仅当三件事同时成立时,**持续引导**让模型继续值班循环:
  ①任务 prompt 里有明确的**持续值守意图**(持续/值班/监控/运营/盯着/keep monitoring /
    on duty / continuously 等),不是泛泛提一句"监控"——要带「持续值守」语气;
  ②任务 prompt 里给了明确的**时长要求**(至少 N 小时 / N 小时 / for N hours / N 分钟 ...),
    能解析出一个目标秒数;
  ③本 run 实际运行时长**远未达到**要求时长(默认达到 90% 即视为够了,放行)。

与上一版(幂等一次,引导后第二次想交付就放行)的关键区别——**持续拦**:
持续值守任务的本质是「靠时长驱动、没到点就不算完成」,引导一次就放行等于纵容提前退出。
所以只要「实际运行时长 < 要求时长」就**每次想 DELIVERY_COMPLETE 都拦+引导**,直到时长达标
才放行。**这不是死循环**——时间在持续流逝,最终一定达标放行。

**但要防紧密循环烧 token**(核心安全阀):若主代理被拦后**没有取得任何新的值守进展**就立即
重试交付(典型表现:被拦→不 wait/不 poll→马上又想交付,如此空转狂刷),则放它通过,不再
重复拦截。判据=两次拦截之间「值守动作」(wait / log_alert_poll / log_source_query /
log_monitor_status)的累计次数有没有增加:
  - 增加了 → 主代理在正经周期性值守(wait→poll→研判),继续拦,把它留在岗位上;
  - 没增加 → 主代理在紧密空转(没干值守活就想退),放行,绝不陪它烧 token。
引导话术明确要求「用 wait 工具等待 N 分钟再继续」(值班是周期性的,不是连续狂跑),
这样配合节流:正常低频值守(每轮都先 wait 再 poll)会被持续留岗直到时长达标;一旦退化成
紧密空转,安全阀立刻放行。务实地兼顾了「拦住提前退出」与「不烧 token」。

边界(关键,绝不误伤):①必须同时命中持续值守意图**和**可解析时长才可能触发——普通一次性
任务(写报告/改代码/查资料)既无值守意图也无"运营 N 小时"时长,永不触发;②时长够了(达到
目标的 90%)立即放行;③紧密空转(无新值守进展)放行;④取不到起跑点保守放行。纯加法、零配置;
text/native 路径同样适用。

运行时长怎么取:run 工作区 work/timeline.jsonl 的首条 created_at(run_workspace_saved 事件,
任务首次落盘即写,compact 续跑复用同目录不覆盖首条 → 跨续跑就是任务真正的起跑点)。取不到
则保守放行(宁可不引导,绝不凭空卡完成中的任务)。
"""

import json as _json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# 「持续值守意图」信号:带明确的「持续盯着/值守」语气,不是裸"监控"二字。覆盖中英。
_VIGIL_INTENT_PATTERNS = (
    r"持续(值班|值守|监控|运营|盯|运行|跑|工作)",
    r"(值班|值守|站岗|盯班|蹲守)",
    r"(一直|不间断|不停|连续)(地|的)?(值|盯|监控|运营|运行|跑)",
    r"(运营|运行|监控|盯|值守)(至少|起码|不少于|持续|约)?\s*\d",
    r"keep\s+(monitoring|watching|running|polling|on)",
    r"\bon\s+duty\b",
    r"\bcontinuously\b",
    r"\bstay\s+on\b",
    r"\bkeep\s+an?\s+eye\b",
)

# 「时长要求」信号 + 单位换算。从 prompt 抽出一个目标秒数(取命中的最大值,贴合"至少 N")。
_DURATION_PATTERNS = (
    (r"(\d+(?:\.\d+)?)\s*(?:个)?\s*小时", 3600.0),
    (r"(\d+(?:\.\d+)?)\s*(?:个)?\s*分钟", 60.0),
    (r"(\d+(?:\.\d+)?)\s*hours?\b", 3600.0),
    (r"(\d+(?:\.\d+)?)\s*hrs?\b", 3600.0),
    (r"(\d+(?:\.\d+)?)\s*minutes?\b", 60.0),
    (r"(\d+(?:\.\d+)?)\s*mins?\b", 60.0),
)

# 达到目标时长的这个比例即视为「够了」,放行(留 10% 余量,避免临界反复引导)。
_DURATION_SATISFIED_RATIO = 0.9

# 「无限期值守」信号:没有固定时长、没时间预算,要一直盯到用户喊停。区别于"值守 N 小时"(有预算到点收尾)。
# 无限期模式下时长门**永不因达标放行**,只认喊停信号(_stop_signal)才放行。
_INDEFINITE_PATTERNS = (
    r"(长期|无限期|无限制)",
    r"没有?\s*(时间|时长)?\s*(预算|期限|终点|结束|限制)",
    r"直到(我|用户|你)?(喊停|叫停|说停|改目标|换任务|让你停|不需要)",
    r"(一直|持续).{0,6}(到|至).{0,4}(喊停|叫停|改目标|换任务)",
    r"(until|till)\s+(i|you|user|we)\b",
    r"\bindefinit(e|ely)\b",
    r"\bno\s+(time\s+)?(budget|deadline|end|limit)\b",
)

# 喊停标记文件相对路径(网关收到用户"停/换任务/改目标"时写它);存在即放行,优雅收尾。
_STOP_FLAG_REL = ("work", "stop_vigil.flag")

_VIGIL_GAP_MARKER = "[duration-vigil-rework]"

# 「值守进展」工具:两次拦截之间这些工具的累计调用次数有没有增加,决定是否还继续拦(节流)。
# wait(周期性等待)+ 三个真正在值班循环里推进研判的 log_ops 工具。log_monitor_start/stop
# 不算(起停只一次性,不代表持续值守动作);写笔记 write_file 也不算(写笔记≠值守研判)。
_VIGIL_PROGRESS_TOOLS = frozenset(
    {"wait", "log_alert_poll", "log_source_query", "log_monitor_status"}
)


def duration_vigil_rework(
    request: object,
    report: dict[str, Any],
    *,
    now: Callable[[], float] | None = None,
) -> bool:
    """持续值守任务 + 有时长要求 + 实际运行远未达标 → 持续引导继续(返回 True),直到时长达标放行。

    防紧密循环节流:两次拦截之间若无新的值守进展(wait/poll/query/status 累计次数没增加),
    则放行(False),不陪空转烧 token。非值守/无时长/取不到起跑点/已达标都 False(放行)。
    """
    params = getattr(request, "params", None)
    if params is None:
        return False
    if not _task_is_vigil_with_intent(params):
        return False
    indefinite = _is_indefinite_vigil(params)
    required_seconds = _required_duration_seconds(params)
    if not indefinite and required_seconds <= 0:
        return False  # 既无明确时长又非无限期 → 本门不管
    if _stop_signal(report):
        return False  # 用户喊停(网关写标记)→ 优雅放行收尾(无限期模式靠它退出)
    elapsed = _elapsed_run_seconds(report, now=now)
    if not indefinite:
        # 有明确时长:取不到起跑点保守放行,或已达 90% 放行(原逻辑)。
        if elapsed is None or elapsed >= required_seconds * _DURATION_SATISFIED_RATIO:
            return False
    # 走到这:无限期未喊停,或有时长未达标。先过节流闸——若上次拦截后无新值守进展(紧密空转)就放行,
    # 不烧 token。第一次进来(还没拦过)progress_at_last_block 为 None,直接拦。
    progress_now = _vigil_progress_count(params)
    progress_at_last_block = _last_block_progress_count(params)
    if progress_at_last_block is not None and progress_now <= progress_at_last_block:
        return False
    _write_vigil_block(
        params,
        report,
        {"indefinite": indefinite, "required_seconds": required_seconds, "elapsed": elapsed, "progress_now": progress_now},
    )
    return True


def _last_block_progress_count(params: object) -> int | None:
    """读取上一次拦截时写进 tool_context 的「值守进展累计次数」;从未拦过则 None。

    取最后一条 vigil 标记里的 progress_at_block 数字。容错:解析失败按 0 计(视为拦过一次、
    基线为 0),这样只要之后有任何值守进展就继续拦,无进展就放行——仍满足节流语义。
    """
    context = getattr(params, "tool_context", None)
    if not isinstance(context, list):
        return None
    latest: int | None = None
    for item in context:
        text = str(item)
        if _VIGIL_GAP_MARKER not in text:
            continue
        latest = _extract_progress_at_block(text)
    return latest


def _extract_progress_at_block(marker_text: str) -> int:
    """从一条 vigil 标记文本里抽出 JSON 的 progress_at_block(解析失败按 0)。"""
    start = marker_text.find("{")
    end = marker_text.find("}", start)
    if start == -1 or end == -1:
        return 0
    try:
        payload = _json.loads(marker_text[start : end + 1])
    except _json.JSONDecodeError:
        return 0
    if isinstance(payload, dict):
        try:
            return int(payload.get("progress_at_block", 0))
        except (TypeError, ValueError):
            return 0
    return 0


def _vigil_progress_count(params: object) -> int:
    """本 run 至今累计的「值守进展」工具成功调用次数(wait/poll/query/status)。"""
    count = 0
    for record in getattr(params, "archive_tool_calls", []) or []:
        if not isinstance(record, dict):
            continue
        if record.get("ok") is False:
            continue
        if str(record.get("tool") or "").strip() in _VIGIL_PROGRESS_TOOLS:
            count += 1
    return count


def _task_is_vigil_with_intent(params: object) -> bool:
    text = _prompt_text(params)
    if not text:
        return False
    lowered = text.casefold()
    return any(re.search(pattern, lowered) for pattern in _VIGIL_INTENT_PATTERNS)


def _prompt_text(params: object) -> str:
    return "\n".join(
        part
        for part in (
            str(getattr(params, "root_user_prompt", "") or ""),
            str(getattr(params, "user_prompt", "") or ""),
        )
        if part
    )


def _required_duration_seconds(params: object) -> float:
    text = _prompt_text(params)
    if not text:
        return 0.0
    lowered = text.casefold()
    values = [
        value
        for pattern, unit_seconds in _DURATION_PATTERNS
        for match in re.finditer(pattern, lowered)
        if (value := _scaled_duration(match.group(1), unit_seconds)) is not None
    ]
    return max(values, default=0.0)


def _scaled_duration(raw: str, unit_seconds: float) -> float | None:
    try:
        return float(raw) * unit_seconds
    except (TypeError, ValueError):
        return None


def _elapsed_run_seconds(report: dict[str, Any], *, now: Callable[[], float] | None) -> float | None:
    started = _run_started_epoch(report)
    if started is None:
        return None
    current = (now or _default_now)()
    return max(0.0, current - started)


def _default_now() -> float:
    return datetime.now(timezone.utc).timestamp()


# LLM: run 真正的起跑点取自 work/timeline.jsonl 的**首条** created_at——它是
#   run_workspace_saved 事件(任务首次落盘即写),compact 续跑复用同一任务目录、append
#   新事件但不覆盖首条,所以跨续跑这条始终是任务最早起跑时间(正是 2 小时值守要对的基准)。
# 函数用途: 回答"这轮(含续跑)任务最早是什么时候开始跑的",取不到返回 None(保守放行)。
def _run_started_epoch(report: dict[str, Any]) -> float | None:
    root = _report_root(report)
    timeline = root / "work" / "timeline.jsonl"
    for created_at in _timeline_created_ats(timeline):
        epoch = _parse_iso_epoch(created_at)
        if epoch is not None:
            return epoch
    return None


def _timeline_created_ats(timeline: Path) -> list[str]:
    try:
        if not timeline.is_file():
            return []
        lines = timeline.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    created_ats: list[str] = []
    for line in lines:
        text = line.strip()
        if not text:
            continue
        try:
            payload = _json.loads(text)
        except _json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and (value := str(payload.get("created_at") or "").strip()):
            created_ats.append(value)
    return created_ats


def _parse_iso_epoch(text: str) -> float | None:
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _append_vigil_rework_context(params: object, report: dict[str, Any], progress_now: int) -> None:
    context = getattr(params, "tool_context", None)
    if not isinstance(context, list):
        return
    gate = report.get("duration_vigil_gate") if isinstance(report.get("duration_vigil_gate"), dict) else {}
    required_minutes = gate.get("required_minutes", 0)
    elapsed_minutes = gate.get("elapsed_minutes", 0)
    context.append(
        f"{_VIGIL_GAP_MARKER}\n"
        + _json.dumps(
            {
                "ok": False,
                "finding": "VIGIL_DURATION_NOT_REACHED",
                "required_minutes": required_minutes,
                "elapsed_minutes": elapsed_minutes,
                # 节流基线:记下本次拦截时已累计的值守进展次数。下次 closeout 时若这个数没涨
                # (说明被拦后没干值守活就又想交付),安全阀放行不再拦,避免紧密循环烧 token。
                "progress_at_block": progress_now,
                "report_ref": report.get("report_ref", ""),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n这是一个**持续值守类**任务(要求持续监控/值班/运营一段明确时长),不是写一份产物就算完成。"
        f"当前实际运行约 {elapsed_minutes} 分钟,远没到要求的约 {required_minutes} 分钟。"
        "请**不要**现在就交付或写交付报告——继续值班循环,而且**每一轮都要先用 wait 工具等待一段时间"
        "(如约 5 分钟)再继续**(值班是周期性的,不是连续不停狂跑;不 wait 直接空转重试交付不会让你提前完成)。"
        "一轮的标准节奏:wait 约 5 分钟 → log_alert_poll 拉新候选逐条研判 → 需要时 log_source_query 交叉验证 → "
        "log_monitor_status 看不丢对账,如此 wait→poll→研判→status 往复,把值班坚持到要求的时长后再收尾交付。"
        "(中途可把阶段性研判写进值班笔记,但写笔记不等于任务完成。)"
    )


def _report_root(report: dict[str, Any]) -> Path:
    return Path(str(report.get("workspace_root") or "."))


def _is_indefinite_vigil(params: object) -> bool:
    """无限期值守:在已确认值守意图的前提下,prompt 还含"长期/没期限/直到喊停"信号(无固定时长)。"""
    text = _prompt_text(params)
    if not text:
        return False
    lowered = text.casefold()
    return any(re.search(pattern, lowered) for pattern in _INDEFINITE_PATTERNS)


def _stop_signal(report: dict[str, Any]) -> bool:
    """用户喊停标记(网关收到"停/换任务/改目标"写 work/stop_vigil.flag);存在即放行,优雅收尾。"""
    flag = _report_root(report).joinpath(*_STOP_FLAG_REL)
    try:
        return flag.is_file()
    except OSError:
        return False


def _write_vigil_block(params: object, report: dict[str, Any], ctx: dict[str, Any]) -> None:
    """写拦截报告 + 注入返工引导。ctx={indefinite,required_seconds,elapsed,progress_now};无限期与有时长两套话术。"""
    indefinite = bool(ctx.get("indefinite"))
    required_seconds = float(ctx.get("required_seconds") or 0.0)
    elapsed = ctx.get("elapsed")
    progress_now = int(ctx.get("progress_now") or 0)
    elapsed_minutes = round((elapsed or 0.0) / 60.0, 1)
    gate: dict[str, Any] = {"allowed": False, "elapsed_minutes": elapsed_minutes, "vigil_progress_calls": progress_now}
    if indefinite:
        gate["finding"] = "VIGIL_INDEFINITE_NOT_STOPPED"
        gate["mode"] = "indefinite"
        gate["message_zh"] = (
            f"这是**无限期**持续值守任务(没有固定时长、没时间预算,要一直盯到用户喊停)。已运行约 {elapsed_minutes} "
            "分钟。请不要提前交付或收尾,继续值班循环:wait 约5分钟 → log_alert_poll 研判 → log_monitor_status "
            "看对账,如此往复,直到用户明确说'停/换任务/改目标'为止。"
        )
    else:
        required_minutes = round(required_seconds / 60.0, 1)
        gate["finding"] = "VIGIL_DURATION_NOT_REACHED"
        gate["mode"] = "duration"
        gate["required_minutes"] = required_minutes
        gate["message_zh"] = (
            f"这是持续值守类任务,当前实际运行约 {elapsed_minutes} 分钟,远未达到要求的约 {required_minutes} 分钟。"
            "请不要提前交付,继续值班循环:wait 约5分钟 → log_alert_poll 研判 → 必要时 log_source_query 交叉验证 → "
            "log_monitor_status 看不丢对账,如此往复直到达到要求时长。"
        )
    report["duration_vigil_gate"] = gate
    _append_vigil_rework_context(params, report, progress_now)
    report["ok"] = False
    from .artifacts import _write_report

    _write_report(_report_root(report), report)


__all__ = ["duration_vigil_rework"]
