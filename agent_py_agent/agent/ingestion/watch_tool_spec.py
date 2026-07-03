"""watch_stream 的 ToolSpec(单独成文件:spec 文本长,和执行逻辑分开)。"""

from __future__ import annotations

from ..tooling.models import ToolSpec


_DESCRIPTION = (
    "高频数据流盯守的摄取层:打开一路 HTTP 游标源(GET ?since=<游标>&limit=<n>),"
    "按「学判据 → 配过滤器 → 再监控」干活:先 sample 抓样本,由你看懂这个源(哪个字段是"
    "结果端、什么取值是目标/常态、哪些是高基数噪声字段),configure 提交结构化判据 spec;"
    "之后 pull 在代码层持续消费全部事件,按 spec 精准抬候选(另有通用稀有度兜底),"
    "把每秒上百条压成每批几条给你研判——比逐条 web_fetch 快几十倍且游标不掉队。"
    "代码只做结构化匹配/计数,绝不做语义定性;候选是否真命中必须你自己判"
    "(同时看触发端和结果端字段)。游标/统计/spec 跨轮持久,断线/换人(补岗)自动续。"
)

_PARAMETERS = {
    "action": "open(打开/续接)/ sample(抓样本学判据)/ configure(提交判据 spec)/ "
    "pull(拉候选批,默认)/ status(看覆盖)/ close(收尾)/ list(本 owner 全部盯守)",
    "url": "open 必填:源的 pull 端点(不带 since/limit 查询参数),如 http://host:8901/pull",
    "watch_id": "sample/configure/pull/status/close 用;open 的返回里给出(也可用 url 代替)",
    "sample_count": "sample 可选:取样事件数(20-1000,默认 200);样本越大字段分布越准",
    "spec": "configure 必填:你从样本学出的结构化判据 JSON 对象 "
    '{"result_field":"结果端字段路径(压平点路径,如 a.b)",'
    '"target_values":["目标取值精确集合"],"target_value_contains":["目标结论记号子串"],'
    '"normal_values":["常态取值集合,取值不在集合内即抬候选"],'
    '"normal_value_contains":["结果端为变尾文本时的常态结论记号子串,不含任何记号即抬"],'
    '"ignore_fields":["高基数噪声字段"],"max_per_pull":8}'
    ";取值判据四者至少给一种(布尔/空写成 true/false/null 字符串)",
    "max_wait_seconds": "pull 长轮询等待上限(0-55,建议 30-55):块内持续消费流,出现候选立即返回",
    "watch_window_seconds": "open 可选:本路要求盯满的时长(秒),status/pull 会算 remaining/complete",
    "rare_threshold": "可选调参:签名窗口计数≤该值才算候选(默认 3)",
    "max_candidates_per_pull": "可选调参:每批候选上限(默认 8)",
}

_PARAMETER_SCHEMA = {
    "action": {"type": "string", "enum": ["open", "sample", "configure", "pull", "status", "close", "list"]},
    "url": {"type": "string"},
    "watch_id": {"type": "string"},
    "sample_count": {"type": "integer"},
    "spec": {"type": "object"},
    "max_wait_seconds": {"type": "integer"},
    "watch_window_seconds": {"type": "integer"},
    "rare_threshold": {"type": "integer"},
    "max_candidates_per_pull": {"type": "integer"},
    "window_seconds": {"type": "integer"},
    "page_limit": {"type": "integer"},
}

_PARAMETER_DETAILS = {
    "spec": "判据必须从样本数据本身推出(sample 的字段分布+原始样本),不要臆造字段名;"
    "样本里往往没有目标事件——用 normal_values 学常态、盯常态之外。配好后随 watch 持久化,"
    "格式漂移可重新 sample+configure 覆盖。",
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
            "又花又杂的源(混高基数噪声字段/结果端是文本):先 sample+configure 学判据再盯",
            "编队分工:每个子代理用它盯一路源,按批研判候选并实时上报命中",
        ],
        avoid_when=[
            "一次性读取普通网页/文档/API → 用 web_fetch",
            "低频源(几分钟一条)且只需扫几次 → web_fetch 按游标手动读即可",
        ],
        keywords=["数据流", "盯守", "监控", "游标", "增量", "高吞吐", "stream", "watch", "pull", "候选", "初筛", "背压", "判据", "spec"],
        parameters=_PARAMETERS,
        parameter_schema=_PARAMETER_SCHEMA,
        parameter_details=_PARAMETER_DETAILS,
        examples=[
            '{"tool":"watch_stream","action":"open","url":"http://192.168.1.50:8901/pull","watch_window_seconds":1200}',
            '{"tool":"watch_stream","action":"sample","watch_id":"ws-ab12cd34ef","sample_count":300}',
            '{"tool":"watch_stream","action":"configure","watch_id":"ws-ab12cd34ef","spec":{"result_field":"outcome.state","normal_values":["ok","queued"],"ignore_fields":["trace_ref"]}}',
            '{"tool":"watch_stream","action":"pull","watch_id":"ws-ab12cd34ef","max_wait_seconds":45}',
            '{"tool":"watch_stream","action":"list"}',
        ],
    )


__all__ = ["build_watch_stream_spec"]
