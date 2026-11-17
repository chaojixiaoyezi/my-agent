"""my-agent gateway timeline: read-only stage latency timeline for one gateway request (S9)."""
# 模块用途: 冷读规范Gateway根与owners目录，按请求编号输出平台→适配器→队列→回合→每轮模型→工具→投递的阶段时间线；
#   不构造Agent、不加锁、不写任何文件。
from __future__ import annotations

import json
from pathlib import Path


# LLM: 未给编号时只在done/failed里取最近修改的一条；不扫描归档或其它状态目录。
# 函数用途: 选出最近结束的请求编号。
def _latest_request_id(root: Path) -> str:
    newest: tuple[float, str] | None = None
    for state in ("done", "failed"):
        try:
            entries = list((root / "requests" / state).glob("*.json"))
        except OSError:
            continue
        for entry in entries:
            try:
                modified = entry.stat().st_mtime
            except OSError:
                continue
            if newest is None or modified > newest[0]:
                newest = (modified, entry.stem)
    return newest[1] if newest else ""


# LLM: 纯渲染；只打印时间线里已有的编号、时间、耗时与固定代码，不读文件。
# 函数用途: 把时间线结果渲染成人读文本。
def render_timeline(result: dict) -> str:
    if not result.get("ok"):
        return f"timeline request={result.get('request_id') or '-'} error={result.get('error_code')}"
    lines = [f"timeline request={result['request_id']} state={result.get('request_state')} origin={result.get('origin_stage')}"]
    detail_skip = {"stage", "at", "at_iso", "offset_ms", "since_previous_ms"}
    for stage in result.get("stages") or []:
        previous = stage.get("since_previous_ms")
        detail = " ".join(f"{key}={value}" for key, value in stage.items() if key not in detail_skip)
        lines.append(f"  {stage['stage']:<20} {stage['at_iso']}  +{stage['offset_ms']:>10.1f} ms"
                     f"  Δ{'-' if previous is None else format(previous, '.1f'):>9}  {detail}".rstrip())
    summary = result.get("summary") or {}
    lines.append("summary " + " ".join(f"{key}={value}" for key, value in summary.items()))
    if result.get("coverage"):
        lines.append("coverage " + json.dumps(result["coverage"], ensure_ascii=False, sort_keys=True))
    if result.get("warnings"):
        lines.append("warnings " + ",".join(result["warnings"]))
    return "\n".join(lines)


# LLM: 冷读同CLI配置的规范Gateway根；owners目录取当前home；输出只读诊断，找不到请求时退出码1。
# 函数用途: 处理 my-agent gateway timeline 命令。
def cmd_gateway_timeline(args) -> int:
    from ..agent.observability.message_stage_timeline import message_stage_timeline
    from ..agent.user_space.home_layout import home_paths
    from .gateway_coordinated_run import _cold_gateway_paths

    root = _cold_gateway_paths(args).root
    request_id = str(getattr(args, "request_id", "") or "").strip() or _latest_request_id(root)
    result = message_stage_timeline(gateway_root=root, owners_dir=home_paths().owners_dir, request_id=request_id)
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(render_timeline(result))
    return 0 if result.get("ok") else 1


__all__ = ["cmd_gateway_timeline", "render_timeline"]
