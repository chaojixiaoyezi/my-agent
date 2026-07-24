"""watch_stream 的 ToolSpec(单独成文件:spec 文本长,和执行逻辑分开)。"""

from __future__ import annotations

from ..tooling.models import ToolSpec

_DESCRIPTION = (
    "数据源盯守研判的摄取层,吃三种源:①HTTP 游标流(GET ?since=<游标>&limit=<n>)"
    "②本地文件(file:///绝对路径——一次性大文件读完挑要紧的,或持续增长的日志 tail 续读)"
    "③快照接口(mode=poll:每隔 poll_query_seconds 查一次接口,整份响应作为一条记录研判)。"
    "工作方式:正常量【全量直通】——量在你判读预算内时每条记录都逐条给你认真读(不筛不压);"
    "量暴涨(洪水)自动切结构化宽筛(引擎按计数/形状宽抬候选、其余压组记账,绝不静默丢),"
    "判完积压又自动恢复全读。可先 sample 抓样本、configure 提交结构化判据 spec 和"
    " judgment_note(用户教的判读须知,持久化,教一次别重教)。代码只做结构化匹配/计数,"
    "绝不定性;【候选是否真命中由你逐条重判说了算,不由判据说了算】(同时看触发端和结果端"
    "字段)。游标/统计/spec/须知跨轮持久,断线/补岗自动续。盯守数据源必须用本工具,勿自写"
    "轮询脚本(实测误报泛滥)。持续盯守属长驻活:主代理收到这类任务应派 long_running "
    "子代理来干(子代理里用本工具长期 pull),自己保持空闲随时响应用户;子代理确认真事"
    "即 record_finding 入账并用 raise_event(urgency=urgent)叫回主代理。"
)

_PARAMETERS = {
    "action": "open(打开/续接)/ sample(抓样本学判据)/ configure(提交判据 spec/判读须知)/ "
    "pull(拉候选批,默认)/ verdict(/audit 保证档:逐条交结论销账)/ status(看覆盖)/ "
    "close(收尾)/ list(本 owner 全部盯守)",
    "verdicts": 'verdict 必填:本批候选的逐条结论数组 [{"ack_id":"12:0","verdict":"hit|clear|unsure",'
    '"note":"结果端依据(可选)"}…]。ack_id 原样取自 pull 候选行(别手编);hit 的先 record_finding '
    "再交;可分多次交,交齐才签收。判几条交几条,别攒",
    "url": "open 必填:HTTP 游标源的 pull 端点(不带 since/limit 参数,如 http://host:8901/pull),"
    "或本地文件 file:///var/log/app.log(也可直接给绝对路径;读大文件/盯日志都用它),"
    "或快照接口(配 mode=poll)",
    "mode": "open 可选:poll=快照型接口(响应是当前状态而非增量事件流),每隔 poll_query_seconds "
    "查一次、整份响应作为一条记录进研判;不给=增量游标流(默认)。文件源自动识别,无须 mode",
    "watch_id": "sample/configure/pull/status/close 用;open 的返回里给出(也可用 url 代替)",
    "sample_count": "sample 可选:取样事件数(20-1000,默认 200);样本越大字段分布越准",
    "spec": "configure 可选(与 judgment_note 至少给一):你从样本学出的结构化判据 JSON 对象 "
    '{"result_field":"结果端字段路径(压平点路径,如 a.b)",'
    '"target_values":["目标取值精确集合"],"target_value_contains":["目标结论记号子串"],'
    '"normal_values":["常态取值集合,取值不在集合内即抬候选"],'
    '"normal_value_contains":["结果端为变尾文本时的常态结论记号子串,不含任何记号即抬"],'
    '"ignore_fields":["高基数噪声字段"],"passthrough":true,"max_per_pull":8}'
    ";取值判据四者至少给一种,或用 passthrough:true(布尔/空写成 true/false/null 字符串)。"
    "【passthrough=内容型直通】:真目标与迷惑项 request/状态码几乎一样、成败只藏在响应正文的"
    "自然语言语义里(子串规则要么两个都中要么都不中、分不开)时声明 true——引擎把该源每条都"
    "递给你逐条读正文定真假,不做结构筛(结构分不开就别用结构规则替你拍板)。最稳是单独用"
    '{"passthrough":true}(可选 ignore_fields 去噪),别再叠会误命中真目标的 normal_* 规则',
    "judgment_note": "configure 可选:这个来源该怎么看的判读须知原文(用户教的样品说明/判据"
    "描述,≤2000字)。随 watch 持久化,重启/补岗/换人接手都会在载荷里原样带回——用户教一次,"
    "同一来源以后不用重教。用户描述过'这类事怎么算要紧'就把要点存进来",
    "max_wait_seconds": "pull 长轮询等待上限(0-55,建议 30-55):块内持续消费流,出现候选立即返回",
    "watch_window_seconds": "open 可选:本路要求盯满的时长(秒),status/pull 会算 remaining/complete",
    "poll_query_seconds": "open 可选(mode=poll 用):快照接口的查询间隔秒数(5-86400,默认 60)",
    "rare_threshold": "可选调参:宽筛模式下签名窗口计数≤该值才算候选(默认 3)",
    "max_candidates_per_pull": "可选调参:宽筛模式每批候选上限(默认 8)",
    "full_read_per_pull": "可选调参:正常量直通的每批全读上限(默认 48;0=关直通恒走宽筛)",
}

_PARAMETER_SCHEMA = {
    "action": {"type": "string", "enum": ["open", "sample", "configure", "pull", "verdict", "status", "close", "list"]},
    "verdicts": {"type": "array"},
    "url": {"type": "string"},
    "mode": {"type": "string", "enum": ["poll"]},
    "watch_id": {"type": "string"},
    "sample_count": {"type": "integer"},
    "spec": {"type": "object"},
    "judgment_note": {"type": "string"},
    "max_wait_seconds": {"type": "integer"},
    "watch_window_seconds": {"type": "integer"},
    "poll_query_seconds": {"type": "integer"},
    "rare_threshold": {"type": "integer"},
    "max_candidates_per_pull": {"type": "integer"},
    "full_read_per_pull": {"type": "integer"},
}

_PARAMETER_DETAILS = {
    "spec": "判据必须从样本数据本身推出(sample 的字段分布+原始样本),不要臆造字段名;"
    "样本里往往没有目标事件——用 normal_values 学常态、盯常态之外,别把样本可见取值当 "
    "target(高频取值会被频次校验拒)。判据只是宽筛,真假仍由你逐条重判。"
    "随 watch 持久化,格式漂移可重新 sample+configure 覆盖。",
    "judgment_note": "这是轻量记忆不是规则库:存的是判读理解(怎么看、什么算要紧),"
    "定性仍由你每条亲自判;与 spec 独立,可只存须知不配 spec。",
    "max_wait_seconds": "等待期间工具在代码层持续拉流喂引擎(游标不停),只是不打扰你;没候选也会在到点返回覆盖账目。",
    "watch_window_seconds": "盯守验收口径:窗口没走完(window_complete=false)就不要交完成报告。",
    "url": "文件源读到文件尾(reached_stream_end)且是一次性文件任务时,判完积压即可 close;"
    "持续增长的日志会自动从断点续读,轮转/截断如实记缺口(gap_events)。",
}


def build_watch_stream_spec() -> ToolSpec:
    return ToolSpec(
        name="watch_stream",
        category="web",
        effect="read_only",
        description=_DESCRIPTION,
        use_cases=[
            "盯守类任务:盯一条持续在涨的流/日志,把真要紧的事一条不漏挑出来报",
            "定时查接口:每隔一段时间查一次某接口(快照型,mode=poll),响应有异动要研判",
            "读大文件:给一个大日志/记录文件,读完把要紧的挑出来(file:///绝对路径)",
            "长时间驻守监控一路源,要求全程覆盖不漏(游标跟到流末尾,断点/补岗自动续)",
            "又花又杂的源(混高基数噪声字段/结果端是文本):先 sample+configure 学判据再盯",
            "编队分工:每个 long_running 子代理用它盯一路源,逐条研判并实时上报命中",
        ],
        avoid_when=[
            "一次性读取普通网页/文档/API → 用 web_fetch",
            "定向带参数查一次接口做交叉印证 → 用 web_fetch(本工具是持续盯守)",
            "读普通小文件 → 用 read_file",
        ],
        keywords=["数据流", "盯守", "监控", "游标", "增量", "高吞吐", "stream", "watch", "pull", "候选", "初筛", "背压", "判据", "spec", "日志", "大文件", "tail", "定时查", "轮询", "poll", "快照"],
        parameters=_PARAMETERS,
        parameter_schema=_PARAMETER_SCHEMA,
        parameter_details=_PARAMETER_DETAILS,
        examples=[
            '{"tool":"watch_stream","action":"open","url":"http://192.168.1.50:8901/pull","watch_window_seconds":1200}',
            '{"tool":"watch_stream","action":"open","url":"file:///var/log/app/events.log","watch_window_seconds":3600}',
            '{"tool":"watch_stream","action":"open","url":"http://192.168.1.50:9100/health","mode":"poll","poll_query_seconds":60}',
            '{"tool":"watch_stream","action":"sample","watch_id":"ws-ab12cd34ef","sample_count":300}',
            '{"tool":"watch_stream","action":"configure","watch_id":"ws-ab12cd34ef","spec":{"result_field":"outcome.state","normal_values":["ok","queued"],"ignore_fields":["trace_ref"]},"judgment_note":"用户教:state 不在 ok/queued 里都要人工看;amount>100000 的 charge 无论 state 都要报"}',
            '{"tool":"watch_stream","action":"configure","watch_id":"ws-ab12cd34ef","spec":{"passthrough":true,"ignore_fields":["trace_ref","conn_id"]},"judgment_note":"真目标和迷惑项状态码都像成功,成败只在 response.body 语义(生效/被降级/被后置拦截),每条都读正文判"}',
            '{"tool":"watch_stream","action":"pull","watch_id":"ws-ab12cd34ef","max_wait_seconds":45}',
            '{"tool":"watch_stream","action":"verdict","watch_id":"ws-ab12cd34ef","verdicts":[{"ack_id":"12:0","verdict":"hit","note":"结果端 state=escalated 明确生效"},{"ack_id":"12:1","verdict":"clear"}]}',
            '{"tool":"watch_stream","action":"list"}',
        ],
    )


__all__ = ["build_watch_stream_spec"]
