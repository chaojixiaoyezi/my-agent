
from __future__ import annotations

"""持续值守类长任务的「时长未达即引导继续」软门(uncontracted 路径专用,保守、幂等)。

实锤背景(日志运营 2 小时真机测试):给主代理「用 log_ops 持续监控研判,运营至少 2 小时,
每隔约 5 分钟 wait→log_alert_poll 拉候选研判,持续到约 2 小时后再收尾」这类**无终点、靠
时长驱动**的值守任务,主代理 log_monitor_start 后只写了个中间值班笔记 shift_notes.md 到
work/,就被 uncontracted closeout「见产物可打开即判完成」判完成、发 [MAIN_AGENT_DELIVERY_
COMPLETE] 提前退出——只跑了约 5 轮 13 分钟,根本没撑到 2 小时。确定性 daemon 工作完美(不丢),
问题纯在 LLM 主代理这层撑不住持续值守。

本门只做一件很窄的事——当且仅当三件事同时成立时,**引导一次**让模型继续值班循环:
  ①任务 prompt 里有明确的**持续值守意图**(持续/值班/监控/运营/盯着/keep monitoring /
    on duty / continuously 等),不是泛泛提一句"监控"——要带「持续值守」语气;
  ②任务 prompt 里给了明确的**时长要求**(至少 N 小时 / N 小时 / for N hours / N 分钟 ...),
    能解析出一个目标秒数;
  ③本 run 实际运行时长**远未达到**要求时长(默认达到 90% 即视为够了,放行)。

与 verification_evidence_gate 同款语义:引导是**幂等一次**——注入一条「继续值班」提醒后,
本 run 第二次到达 closeout 直接放行(写进 advisories 供把关)。这样即便运行时长取不到、
或主代理坚持要收尾,也绝不会卡死,最多多提醒一轮。**软引导**:它不阻断 uncontracted 的
其它客观事实门,只在那些都放行后、模型想提前交付时,把「这是持续值守任务、还没到时长、
继续 wait→poll→研判→status 循环、不要提前写交付报告」喂回去,让模型「想提前交付→被引导
继续→继续值班循环」。

边界(关键,绝不误伤):①必须同时命中持续值守意图**和**可解析时长才可能触发——普通一次性
任务(写报告/改代码/查资料)既无值守意图也无"运营 N 小时"时长,永不触发;②时长够了(达到
目标的 90%)立即放行;③幂等一次,提醒过即放行。纯加法、零配置;text/native 路径同样适用。

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

_VIGIL_GAP_MARKER = "[duration-vigil-rework]"


def duration_vigil_rework(
    request: object,
    report: dict[str, Any],
    *,
    now: Callable[[], float] | None = None,
) -> bool:
    """持续值守任务 + 有时长要求 + 实际运行远未达标 → 引导继续一次(返回 True)。

    幂等:本 run 已引导过则直接 False(放行)。非值守/无时长/取不到起跑点/已达标都 False。
    """
    params = getattr(request, "params", None)
    if params is None:
        return False
    if _rework_already_emitted(params):
        return False
    if not _task_is_vigil_with_intent(params):
        return False
    required_seconds = _required_duration_seconds(params)
    if required_seconds <= 0:
        return False
    elapsed = _elapsed_run_seconds(report, now=now)
    if elapsed is None:
        return False
    if elapsed >= required_seconds * _DURATION_SATISFIED_RATIO:
        return False
    required_minutes = round(required_seconds / 60.0, 1)
    elapsed_minutes = round(elapsed / 60.0, 1)
    report["duration_vigil_gate"] = {
        "allowed": False,
        "finding": "VIGIL_DURATION_NOT_REACHED",
        "required_seconds": round(required_seconds, 1),
        "elapsed_seconds": round(elapsed, 1),
        "required_minutes": required_minutes,
        "elapsed_minutes": elapsed_minutes,
        "message_zh": (
            "这是持续值守类任务(要求持续监控/值班/运营一段明确时长),当前实际运行时长"
            f"约 {elapsed_minutes} 分钟,远未达到要求的约 {required_minutes} 分钟。"
            "请不要提前交付或写交付报告,继续值班循环:wait 一段时间 → log_alert_poll 拉新候选研判 → "
            "必要时 log_source_query 交叉验证 → log_monitor_status 看不丢对账,如此往复直到达到要求时长。"
        ),
    }
    _append_vigil_rework_context(params, report)
    report["ok"] = False
    from .artifacts import _write_report

    _write_report(_report_root(report), report)
    return True


def _rework_already_emitted(params: object) -> bool:
    context = getattr(params, "tool_context", None)
    if not isinstance(context, list):
        return False
    return any(_VIGIL_GAP_MARKER in str(item) for item in context)


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


def _append_vigil_rework_context(params: object, report: dict[str, Any]) -> None:
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
                "report_ref": report.get("report_ref", ""),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n这是一个**持续值守类**任务(要求持续监控/值班/运营一段明确时长),不是写一份产物就算完成。"
        f"当前实际运行约 {elapsed_minutes} 分钟,远没到要求的约 {required_minutes} 分钟。"
        "请**不要**现在就交付或写交付报告——继续值班循环:用 wait 等一段时间(如约 5 分钟)→ "
        "log_alert_poll 拉新候选逐条研判 → 需要时 log_source_query 交叉验证 → log_monitor_status 看不丢对账,"
        "如此 wait→poll→研判→status 往复,把值班坚持到要求的时长后再收尾交付。"
        "(中途可把阶段性研判写进值班笔记,但写笔记不等于任务完成。)"
    )


def _report_root(report: dict[str, Any]) -> Path:
    return Path(str(report.get("workspace_root") or "."))


__all__ = ["duration_vigil_rework"]
