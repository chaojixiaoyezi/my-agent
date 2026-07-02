"""watch_stream 的 ToolSpec(单独成文件:spec 文本长,和执行逻辑分开)。"""

from __future__ import annotations

from ..tooling.models import ToolSpec


_DESCRIPTION = (
    "高频数据流盯守的摄取层:打开一路 HTTP 游标源(GET ?since=<游标>&limit=<n>),"
    "pull 会在代码层持续消费全部事件并做【纯结构化】预聚合/去重/降噪(签名稀有度初筛,"
    "绝不做语义定性),把每秒上百条压成每批几条的候选给你研判——比逐条 web_fetch 快几十倍"
    "且游标不掉队。候选是否真命中必须你自己判(同时看触发端和结果端字段)。"
    "游标与统计跨轮持久,断线/换人(补岗)自动从断点续。"
)

_PARAMETERS = {
    "action": "open(打开/续接)/ pull(拉候选批,默认)/ status(看覆盖)/ close(收尾)/ list(本 owner 全部盯守)",
    "url": "open 必填:源的 pull 端点(不带 since/limit 查询参数),如 http://host:8901/pull",
    "watch_id": "pull/status/close 用;open 的返回里给出(也可用 url 代替)",
    "max_wait_seconds": "pull 长轮询等待上限(0-55,建议 30-55):块内持续消费流,出现候选立即返回",
    "watch_window_seconds": "open 可选:本路要求盯满的时长(秒),status/pull 会算 remaining/complete",
    "rare_threshold": "可选调参:签名窗口计数≤该值才算候选(默认 3)",
    "max_candidates_per_pull": "可选调参:每批候选上限(默认 8)",
}

_PARAMETER_SCHEMA = {
    "action": {"type": "string", "enum": ["open", "pull", "status", "close", "list"]},
    "url": {"type": "string"},
    "watch_id": {"type": "string"},
    "max_wait_seconds": {"type": "integer"},
    "watch_window_seconds": {"type": "integer"},
    "rare_threshold": {"type": "integer"},
    "max_candidates_per_pull": {"type": "integer"},
    "window_seconds": {"type": "integer"},
    "page_limit": {"type": "integer"},
}

_PARAMETER_DETAILS = {
    "max_wait_seconds": "等待期间工具在代码层持续拉流喂引擎(游标不停),只是不打扰你;没候选也会在到点返回覆盖账目。",
    "watch_window_seconds": "盯守验收口径:窗口没走完(window_complete=false)就不要交完成报告。",
}


def build_watch_stream_spec() -> ToolSpec:
    return ToolSpec(
        name="watch_stream",
        category="web",
        effect="read_only",
        description=_DESCRIPTION,
        use_cases=[
            "盯守类任务:数据源吞吐高(每秒几十/上百条),逐条读根本读不过来",
            "长时间驻守监控一路 /pull 游标接口,要求全程覆盖不漏(游标跟到流末尾)",
            "编队分工:每个子代理用它盯一路源,按批研判候选并实时上报命中",
        ],
        avoid_when=[
            "一次性读取普通网页/文档/API → 用 web_fetch",
            "低频源(几分钟一条)且只需扫几次 → web_fetch 按游标手动读即可",
        ],
        keywords=["数据流", "盯守", "监控", "游标", "增量", "高吞吐", "stream", "watch", "pull", "候选", "初筛", "背压"],
        parameters=_PARAMETERS,
        parameter_schema=_PARAMETER_SCHEMA,
        parameter_details=_PARAMETER_DETAILS,
        examples=[
            '{"tool":"watch_stream","action":"open","url":"http://192.168.1.50:8901/pull","watch_window_seconds":1200}',
            '{"tool":"watch_stream","action":"pull","watch_id":"ws-ab12cd34ef","max_wait_seconds":45}',
            '{"tool":"watch_stream","action":"list"}',
        ],
    )


__all__ = ["build_watch_stream_spec"]
