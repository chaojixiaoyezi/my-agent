"""watch_stream 的 ToolSpec(单独成文件:spec 文本长,和执行逻辑分开)。"""

from __future__ import annotations

from copy import deepcopy

from ..common.audit_activation import AUDIT_SOURCE_OPEN_FIELDS
from ..tooling.models import ToolSpec, TrustedParameterBinding

_DESCRIPTION = (
    "数据源持续采集与可审计交付工具，支持 HTTP 游标流、本地文件/持续增长日志和"
    " mode=poll 快照接口，以及 prepare 现场验证的 mode=adapter 动态请求源。工具负责记录边界、"
    "游标、耐久队列、重投、背压、覆盖账和原文引用；"
    "不负责业务定性，也不按内容或分数选择处理路线。"
    "普通 watch 可以按结构化 spec 做可审计宽筛；/audit 保证档中每条完整记录都会进入耐久"
    "队列，只有提交与 ack_id 对应的结构化 verdict 后才算已判。命名 Audit 的根代理只协调、"
    "检查和汇报；每个现场已确认的来源必须由一个独立叶子子代理打开并持续消费，单个来源"
    "工作者不能再打开第二路。Agent 仍依据用户目标和当前可用工具自主决定分析、复核和汇报。"
    "open 只返回来源采集状态，不会发布或修改命名 Audit 的生效要求；该变化只能由"
    " publish_audit_update 的成功结构化结果证明。"
)

_PARAMETERS = {
    "action": "必填；open(打开/续接)/ sample(只读查看来源样本)/ "
    "configure(仅普通 watch:提交结构化宽筛 spec)/ "
    "pull(拉候选批)/ verdict(/audit 保证档:逐条交结论销账)/ "
    "inspect(/audit:按 ack_id 复查原始记录和结论)/ status(看覆盖)/ "
    "close(收尾)/ list(本 owner 全部盯守)",
    "verdicts": "verdict 使用的判断数组 [{"
    '"verdict_token":"vt-…","verdict":"hit|clear|unsure","score":0,'
    '"note":"判断理由"}…]。本轮已完成的每条记录都要单列并原样携带相邻 '
    "verdict_token；clear 且没有额外依据时可省略 note，hit/unsure 必须说明理由。"
    "程序不会用默认值替模型补结论，而是把每条返回机械绑定为独立结论，"
    "逐条绑定、逐条记账。首次判断同时提交当前 delivery_ref；程序按令牌绑定准确的"
    " ack_id/source_ref/event_sha256，首次判断应省略这三个机械标识。若显式提交，"
    "程序会逐条校验，"
    "填错就整次拒绝。历史复核没有 delivery_ref，必须准确提供两者。程序不解释业务内容。"
    "未给 score_range 时总分范围默认 0–100；用户自定义总分范围时提交"
    ' score_range={"min":…,"max":…}。用户定义了评分维度时可同时提交 dimensions，'
    "每项保存名称、分数、范围和理由。程序只校验形状、数值范围和 ack 对应关系，"
    "不解释分数、不改写 verdict、不自动路由。当前令牌可以分多次提交；每次部分提交成功"
    "后必须重新 pull 剩余欠账并使用新返回的 delivery_ref/verdict_token，不能复用旧引用。"
    "delivery_ref 必须与当前 pull 一起提交，用来证明这是当前交付批次；令牌提交时"
    "程序为每一行绑定规范 ack_id、原文引用和哈希，不能借此跨记录、跨批或跨来源改绑。"
    "全部欠账销清后本批才签收。Agent 后续发现已签收判断有误或需复核时，"
    "对准确 ack_id 再次提交同一结构并加 review=true，同时提供原条的"
    " source_ref/event_sha256，不带 delivery_ref；需要升级时可在这一行内联 finding。"
    "程序只追加新版本、保留原判断，不重复计数或重新消费原记录；"
    "record_finding 只记结论，不能替代 review 更正 verdict。hit 是模型已经确认满足"
    "本次 Audit 汇报条件的结构化结论，因此该行必须同时携带 finding。"
    "宿主由结构化 hit 机械绑定 requires_llm_report=true；模型无需重复填写这个布尔值，"
    "但若显式填写只能为 true。宿主不从 note 或原文猜是否命中",
    "delivery_ref": "verdict 可选:当前 pull 返回的不透明交付引用。首次判断时程序按"
    "每行 verdict_token 从可信交付账绑定 ack_id、source_ref 和 event_sha256；调用方可省略"
    "这三个机械字段，"
    "但显式提供就必须准确。部分签收、重投或接管后旧引用立即失效；还有欠账时"
    "先重新 pull，再把新引用用于下一次 verdict",
    "ack_id": "inspect 必填:pull/attention/结论账本中的签收令牌(如 12:0)。"
    "返回该 owner 本路的原始日志、哈希、字节数、源游标、分数、结论、理由、模型、"
    "首次签收、复核和真实投递状态，不推进游标",
    "source_ref": "inspect 可直接传的稳定原文引用，例如 "
    "audit://ws-ab12cd34ef/candidate/12:0。程序从该结构化引用精确取得 watch_id 和 "
    "ack_id；若同时显式传入二者但不一致会拒绝，不从说明文字或业务字段猜记录",
    "url": "open 必填且一次只接受一个来源地址。HTTP 取数方式必须来自刚刚读过的文档或"
    "实际试通结果，不能猜固定接口。文档写成任意参数名=<next> 或 <cursor> 时可原样传入，"
    "分页大小可写任意参数名=<limit>；程序只把这些结构占位绑定到该具体来源，不预设参数名。"
    "或本地文件 file:///var/log/app.log(也可直接给绝对路径;读大文件/盯日志都用它),"
    "或快照接口(配 mode=poll)。多个来源分别发起 open，每次一个 URL；不要把 URL 数组"
    "序列化进这个字符串",
    "mode": "open 可选:cursor=现场确认过的整数增量游标流；poll=现场确认过的快照型接口；"
    "adapter=现场编写并验证的纯请求/响应适配器，用于时间窗、字符串 token、页码、offset、"
    "编号区间或多参数推进。"
    "poll 每隔 poll_query_seconds 查询并把整份响应作为一条记录；不给时等同 cursor。程序不在"
    "这些方式间猜测或自动改型。"
    "文件源自动识别",
    "http_request": "HTTP open 可选结构对象，保存该具体来源刚刚试通的请求事实：method、"
    "普通 headers、JSON 请求体、游标和分页大小放在 query 还是 json_body，以及 SecretRef。"
    "它不是来源类型模板。GET + URL 中的 <next>/<cursor>/<limit> 可省略对应绑定；其他请求按"
    "实际文档填写。敏感值只允许通过 secret_bindings 的 env:/file: 引用，不保存明文",
    "source_adapter": "仅 mode=adapter 使用。path 是当前命名 Audit 工作区内刚刚写入并试通的"
    "相对 Python 脚本路径，sha256 钉住准确版本。脚本只根据不透明 checkpoint 规划下一次"
    "请求并把响应拆成完整 records，不能联网、不能读取密钥、不能写文件；网络、安全闸、"
    "落盘和 checkpoint 提交仍由宿主完成。脚本从 stdin 读取一个 JSON 对象并只向 stdout"
    "输出一个 JSON 对象：phase=plan 时输入 checkpoint/now_unix/page_limit/base_request，"
    "输出 request={url?,headers?,json_body?} 和可选 context；phase=accept 时输入同一"
    "checkpoint、context 和真实 response，输出 records、推进后的 checkpoint、has_more，"
    "以及与非空 records 一一对应的 record_keys。record_keys 必须表示来源投递位置：重叠"
    "窗口再次读到同一位置时保持相同，不同位置即使正文相同也必须不同；不得用正文哈希或"
    "设备业务 ID 静默合并。checkpoint 必须是 JSON 对象，可保存时间窗、"
    "页码、offset、编号区间、字符串 token 或它们的组合；脚本负责依据现场协议处理重叠窗，"
    "宿主只按已验证的来源位置键阻止同一位置重放，不按内容擅自合并记录",
    "record_list_field": "HTTP 游标源 open 可选且通常不要传:顶层记录数组字段原名，"
    "不是 JSONPath。程序会先从 items 或唯一数组自动识别；只有接口确有多个顶层数组、"
    "自动识别明确失败时才填写",
    "cursor_field": "HTTP 游标源 open 可选且通常不要传:顶层整数游标字段原名，"
    "不是记录内部的 seq/id，也不是 JSONPath。程序会先从 next_cursor/next 自动识别；"
    "只有非标准或多个顶层整数候选导致明确失败时才填写",
    "cursor_semantics": "HTTP 游标源 open 可选:next_position=字段表示下一条位置；"
    "last_seen=字段表示本页最后一条位置。标准 next_cursor/next 可自动识别，"
    "非标准字段无法唯一推断时必须显式给出",
    "has_more_field": "HTTP 游标源 open 可选:顶层布尔型“还有下一页”字段名。"
    "标准 has_more 自动识别；该字段只控制翻页结束，不参与业务判断",
    "record_boundary": '仅 file 来源 open 可选。默认 {"mode":"line"} 表示一行一条；'
    '多行记录可用 {"mode":"delimiter","delimiter":"\\n---END---\\n"} 指定精确分隔符。'
    "分隔符只定义完整记录边界，不表达日志含义或判断规则；已有游标后不可更换",
    "watch_id": "sample/configure/pull/status/close 用;open 的返回里给出(也可用 url 代替)。"
    "结构化绑定的来源工作者省略时，由宿主从当前任务范围补入唯一 watch_id",
    "source_id": "open 可选:当前 owner 长期认识的稳定来源编号。不给时按 owner 与规范化"
    "来源地址生成稳定编号；/audit 首次绑定后不可在续跑中静默改写",
    "source_profile_ref": "open 可选:指向 owner 工作区、文档或 Memory 中来源说明的引用。"
    "只给模型按需读取，不复制成 Audit 专用知识库；/audit 首次绑定后保持不变",
    "document_refs": "open 可选:字段表、设备文档或接口文档的引用数组。引用按本次 Audit"
    "首次打开时钉住；更新后的文档由新的 Audit 明确选择，不能静默替换运行中绑定",
    "sample_count": "sample 可选:只读取样事件数(20-1000,默认 200)，不推进正式游标",
    "spec": "仅普通 watch 的 configure 可选参数：结构化宽筛 JSON，可包含 result_field、"
    "target_values、target_value_contains、normal_values、normal_value_contains、"
    "ignore_fields、passthrough、max_per_pull。/audit 不接受该参数，也不会按来源 spec"
    "过滤完整记录；Audit 判断要求应放在用户的命名任务正文中",
    "judgment_note": "仅普通 watch 的 configure 可选参数：随普通 watch 保存的说明文本。"
    "/audit 不接受，也不会从数据源配置继承业务判断规则",
    "max_wait_seconds": "pull 长轮询等待上限(0-55,建议 30-55):块内持续消费流,出现候选立即返回",
    "watch_window_seconds": "open 可选:本路要求盯满的时长(秒),status/pull 会算 remaining/complete",
    "poll_query_seconds": "open 可选(mode=poll 用):快照接口的查询间隔秒数(5-86400,默认 60)",
    "rare_threshold": "可选调参:宽筛模式下签名窗口计数≤该值才算候选(默认 3)",
    "max_candidates_per_pull": "仅普通 watch 的 open 可选:保存宽筛每批候选上限(1-50)；"
    "/audit 的完整记录批量不使用这个筛选参数",
    "target_records": "pull 可选:声明本次想领取的完整记录条数(1-100000)。"
    "status/pull 的 batch_context 会给出"
    "待判体积、预计每条 token、当前剩余上下文和安全批量；实际交付仍由累计字节、"
    "统一上下文预算和完整记录边界兜底。它不筛选或截断记录",
    "guarantee_batch_max_tokens": "open 可选的纯资源边界：Audit 单次交给模型的完整记录"
    "批次最多占用多少估算 token（默认 45000，可按部署模型实测调整）。它不按内容筛选，"
    "也不截断单条记录；超时恢复还会在这个上限内进一步缩小当前在途批",
    "full_read_per_pull": "可选调参:正常量直通的每批全读上限(默认 48;0=关直通恒走宽筛)",
}

_SCORE_RANGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "min": {"type": "number"},
        "max": {"type": "number"},
    },
    "required": ["min", "max"],
}

_DIMENSIONS_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "name": {"type": "string"},
            "score": {"type": "number"},
            "range": _SCORE_RANGE_SCHEMA,
            "reason": {"type": "string"},
        },
        "required": ["name", "score"],
    },
}

_INLINE_FINDING_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "claim": {"type": "string", "minLength": 1, "maxLength": 2000},
        "kind": {"type": "string"},
        "confidence": {"type": "string"},
        "finding_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "stage": {"type": "string"},
        "needs_evidence": {"type": "boolean"},
        "urgency": {"type": "string", "enum": ["normal", "urgent"]},
        "requires_llm_report": {
            "type": "boolean",
            "description": (
                "可选；verdict=hit 时通常省略，宿主由 hit 机械绑定为 true。"
                "若显式提供则只能为 true，不能用 false 制造互相矛盾的结论。"
            ),
        },
        "evidence_refs": {
            "type": "array",
            "maxItems": 20,
            "items": {"type": "string"},
        },
    },
    "required": ["claim"],
}

_VERDICT_CONCLUSION_PROPERTIES = {
    "verdict": {
        "type": "string",
        "enum": ["hit", "clear", "unsure"],
        "description": (
            "相对当前 Audit 任务要求的判断：hit=该记录满足任务定义的命中/关注条件；"
            "clear=证据足以判断该记录不满足；unsure=现有证据不足或冲突，不能确定。"
            "这是模型的业务判断，宿主只保存和核对记录身份。"
        ),
    },
    "score": {
        "type": "number",
        "description": (
            "按当前用户或任务约定的评分口径填写；未约定时使用 0–100。"
            "除非任务明确规定，程序不会把分数解释成风险、置信度或 verdict，"
            "也不要求分数与 hit/clear 单调绑定。"
        ),
    },
    "score_range": _SCORE_RANGE_SCHEMA,
    "dimensions": _DIMENSIONS_SCHEMA,
    "note": {
        "type": "string",
        "description": "hit/unsure 必填判断依据；clear 没有额外依据时可省略。",
    },
}

_VERDICT_ROW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "ack_id": {
            "type": "string",
            "description": "当前批首次判断应省略并由宿主按 verdict_token 绑定；历史复核时原样提供。",
        },
        "verdict_token": {
            "type": "string",
            "pattern": "^vt-[0-9a-f]{24}$",
            "description": "当前批本轮提交的每行必填；从相邻 candidate 原样复制。遗漏行保持待判，历史复核改用 ack_id。",
        },
        "source_ref": {
            "type": "string",
            "minLength": 1,
            "description": "仅历史复核 review=true 且没有 delivery_ref 时提交；首次判断省略，宿主从可信交付绑定。",
        },
        "event_sha256": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$",
            "description": "仅历史复核 review=true 且没有 delivery_ref 时提交；首次判断省略，宿主从可信交付绑定。",
        },
        **_VERDICT_CONCLUSION_PROPERTIES,
        "review": {
            "type": "boolean",
            "description": "只用于已签收记录的复核/更正；须用显式 ack_id、source_ref、event_sha256，不带 delivery_ref。",
        },
        "finding": _INLINE_FINDING_SCHEMA,
    },
    "required": ["verdict", "score"],
}


def _request_value_binding_schema(*, cursor: bool) -> dict[str, object]:
    """Expose the same location-specific shape that the HTTP renderer enforces.

    ``name`` and ``path`` are alternatives, not two optional hints.  Publishing
    that distinction in the model-facing schema prevents a provider from
    producing a request that the shared execution validator must immediately
    reject.
    """

    cursor_fields: dict[str, object] = (
        {
            "initial": {"type": "integer", "minimum": 0},
            "offset": {"type": "integer"},
        }
        if cursor
        else {}
    )
    return {
        "type": "object",
        "oneOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "location": {"const": "query"},
                    "name": {"type": "string", "minLength": 1, "maxLength": 128},
                    **cursor_fields,
                },
                "required": ["location", "name"],
            },
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "location": {"const": "json_body"},
                    "path": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 16,
                        "items": {"type": "string", "minLength": 1},
                    },
                    **cursor_fields,
                },
                "required": ["location", "path"],
            },
        ],
    }


def _secret_binding_schema() -> dict[str, object]:
    common_secret = {"secret_ref": {"type": "string", "minLength": 1}}
    return {
        "type": "object",
        "oneOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "location": {"enum": ["header", "query"]},
                    "name": {"type": "string", "minLength": 1, "maxLength": 128},
                    **common_secret,
                },
                "required": ["location", "name", "secret_ref"],
            },
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "location": {"const": "json_body"},
                    "path": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 16,
                        "items": {"type": "string", "minLength": 1},
                    },
                    **common_secret,
                },
                "required": ["location", "path", "secret_ref"],
            },
        ],
    }


_PARAMETER_SCHEMA = {
    "action": {
        "type": "string",
        "enum": [
            "open",
            "sample",
            "configure",
            "pull",
            "verdict",
            "inspect",
            "status",
            "close",
            "list",
        ],
    },
    "ack_id": {"type": "string"},
    "source_ref": {
        "type": "string",
        "pattern": "^audit://ws-[0-9a-f]{10}/candidate/[0-9]+:[0-9]+$",
    },
    "verdicts": {"type": "array", "items": _VERDICT_ROW_SCHEMA},
    "delivery_ref": {
        "type": "string",
        "pattern": "^ad-[0-9a-f]{24}$",
    },
    "url": {"type": "string"},
    "mode": {"type": "string", "enum": ["cursor", "poll", "adapter"]},
    "http_request": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "method": {"type": "string", "enum": ["GET", "POST"]},
            "headers": {
                "type": "object",
                "additionalProperties": {"type": "string"},
            },
            "json_body": {"type": "object"},
            "cursor_binding": _request_value_binding_schema(cursor=True),
            "page_size_binding": _request_value_binding_schema(cursor=False),
            "secret_bindings": {
                "type": "array",
                "maxItems": 32,
                "items": _secret_binding_schema(),
            },
        },
    },
    "source_adapter": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "path": {"type": "string", "minLength": 1, "maxLength": 512},
            "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        },
        "required": ["path", "sha256"],
    },
    "record_list_field": {"type": "string"},
    "cursor_field": {"type": "string"},
    "cursor_semantics": {
        "type": "string",
        "enum": ["next_position", "last_seen"],
    },
    "has_more_field": {"type": "string"},
    "record_boundary": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "mode": {"type": "string", "enum": ["line", "delimiter"]},
            "delimiter": {"type": "string"},
        },
        "required": ["mode"],
    },
    "watch_id": {"type": "string"},
    "source_id": {"type": "string", "minLength": 1, "maxLength": 128},
    "source_profile_ref": {"type": "string", "maxLength": 2048},
    "document_refs": {
        "type": "array",
        "maxItems": 64,
        "items": {"type": "string", "minLength": 1, "maxLength": 2048},
    },
    "sample_count": {"type": "integer"},
    "spec": {"type": "object"},
    "judgment_note": {"type": "string"},
    "max_wait_seconds": {"type": "integer"},
    "watch_window_seconds": {"type": "integer"},
    "poll_query_seconds": {"type": "integer"},
    "rare_threshold": {"type": "integer"},
    "max_candidates_per_pull": {"type": "integer", "minimum": 1, "maximum": 50},
    "target_records": {"type": "integer", "minimum": 1, "maximum": 100000},
    "guarantee_batch_max_tokens": {
        "type": "integer",
        "minimum": 1000,
        "maximum": 200000,
    },
    "full_read_per_pull": {"type": "integer"},
}

_PARAMETER_DETAILS = {
    "spec": "只影响普通 watch 的候选宽筛，不是业务结论；/audit 恒为全量耐久交付。",
    "judgment_note": "只适用于普通 watch；/audit 使用当前命名任务目标和必要任务上下文。",
    "max_wait_seconds": "等待期间工具在代码层持续拉流喂引擎(游标不停),只是不打扰你;没候选也会在到点返回覆盖账目。",
    "watch_window_seconds": "盯守验收口径:窗口没走完(window_complete=false)就不要交完成报告。",
    "url": "文件源读到文件尾(reached_stream_end)且是一次性文件任务时,判完积压即可 close;"
    "持续增长的日志会自动从断点续读,轮转/截断如实记缺口(gap_events)。",
    "record_boundary": "完整记录不会为了批次大小被切开；末尾未完成片段会持久保存，"
    "重启后从同一片段继续。来源被替换且片段无法对应时会明确失败而不是拼接或跳过。",
    "record_list_field": "只声明响应信封中的结构位置，不保存样本内容，也不表达记录语义。",
    "cursor_field": "只声明传输游标的结构位置；游标推进由程序对账，含糊时 fail-closed。",
    "cursor_semantics": "只在结构无法唯一证明时显式填写；不会据此解释记录内容。",
    "http_request": "只保存本来源已经试通的传输事实。程序按结构注入游标、页长和密钥引用，"
    "不根据 URL、设备名或日志内容选择请求方式。cursor_binding.offset 只在接口文档或"
    "实际探针已经证明请求位置需要相对响应位置做整数偏移时填写；程序不会自行回退游标。",
    "source_adapter": "只保存 Audit 工作区相对路径和脚本哈希；脚本在无网络只读 sandbox 中"
    "运行，不能代替宿主发请求或提交游标。",
}


_SURFACE_ACTIONS = {
    "ordinary": (
        "open",
        "sample",
        "configure",
        "pull",
        "verdict",
        "inspect",
        "status",
        "close",
        "list",
    ),
    # The same first run may bind and then consume. Configuration and lifecycle
    # cancellation remain outside a source worker's authority.
    "audit_binding": ("open", "pull", "verdict", "inspect", "status", "list"),
    "audit_source": ("pull", "verdict", "inspect", "status", "list"),
    "audit_coordinator": ("inspect", "status", "list"),
    # A prepare turn may prove one transport, but it is not a long-running
    # source worker and does not configure the ordinary-watch screening path.
    "audit_prepare": ("open", "pull", "inspect", "status", "close", "list"),
}
_ACTION_LABELS = {
    "open": "open(打开或续接一条来源)",
    "sample": "sample(读取已打开来源的样本)",
    "configure": "configure(配置普通 watch 宽筛)",
    "pull": "pull(领取完整记录批次)",
    "verdict": "verdict(提交逐条结论)",
    "inspect": "inspect(按引用复查)",
    "status": "status(查看覆盖状态)",
    "close": "close(关闭当前 watch)",
    "list": "list(列出当前 owner 的 watch)",
}

_OPEN_PARAMETERS = {
    "url",
    "mode",
    "http_request",
    "source_adapter",
    "record_list_field",
    "cursor_field",
    "cursor_semantics",
    "has_more_field",
    "record_boundary",
    "source_id",
    "source_profile_ref",
    "document_refs",
    "watch_window_seconds",
    "poll_query_seconds",
    "rare_threshold",
    "max_candidates_per_pull",
    "guarantee_batch_max_tokens",
    "full_read_per_pull",
}
_SOURCE_PARAMETERS = {
    "watch_id",
    "delivery_ref",
    "verdicts",
    "ack_id",
    "source_ref",
    "max_wait_seconds",
    "target_records",
}
_PREPARE_OPEN_PARAMETERS = _OPEN_PARAMETERS - {
    "rare_threshold",
    "max_candidates_per_pull",
    "guarantee_batch_max_tokens",
    "full_read_per_pull",
}
_PREPARE_SOURCE_PARAMETERS = {
    "watch_id",
    "ack_id",
    "max_wait_seconds",
    "target_records",
}


def _surface_parameter_names(surface: str) -> set[str]:
    if surface == "ordinary":
        return set(_PARAMETERS)
    if surface == "audit_binding":
        return {"action", *_OPEN_PARAMETERS, *_SOURCE_PARAMETERS}
    if surface == "audit_source":
        return {"action", *_SOURCE_PARAMETERS}
    if surface == "audit_coordinator":
        return {"action", "watch_id", "ack_id", "source_ref"}
    if surface == "audit_prepare":
        return {
            "action",
            *_PREPARE_OPEN_PARAMETERS,
            *_PREPARE_SOURCE_PARAMETERS,
        }
    raise ValueError(f"unknown watch_stream surface: {surface}")


def _surface_examples(surface: str, examples: list[str]) -> list[str]:
    actions = set(_SURFACE_ACTIONS[surface])
    return [
        example
        for example in examples
        if any(f'"action":"{action}"' in example for action in actions)
    ]


def build_watch_stream_spec(*, surface: str = "ordinary") -> ToolSpec:
    actions = _SURFACE_ACTIONS.get(surface)
    if actions is None:
        raise ValueError(f"unknown watch_stream surface: {surface}")
    names = _surface_parameter_names(surface)
    parameters = _surface_parameters(surface, actions, names)
    parameter_schema = {
        name: deepcopy(value) for name, value in _PARAMETER_SCHEMA.items() if name in names
    }
    parameter_schema["action"] = {"type": "string", "enum": list(actions)}
    return _watch_stream_tool_spec(
        surface,
        names,
        parameters,
        parameter_schema,
        _surface_description(surface, parameters),
    )


def _surface_parameters(
    surface: str,
    actions: tuple[str, ...],
    names: set[str],
) -> dict[str, str]:
    parameters = {name: value for name, value in _PARAMETERS.items() if name in names}
    parameters["action"] = "必填；当前运行只允许：" + " / ".join(
        _ACTION_LABELS[action] for action in actions
    )
    if surface == "audit_prepare":
        parameters["url"] = (
            parameters["url"]
            + " 准备轮试通 HTTP 时，必须原样保留现场已确认 URL 中的 <next>、<cursor>、"
            "<limit> 占位符；想少取样本应在 open 成功后的 pull 使用 target_records，"
            "不能把 <limit> 改成固定数字。显式游标绑定只能放在 "
            "http_request.cursor_binding，cursor_binding 不是顶层参数。"
        )
        parameters["http_request"] = (
            parameters["http_request"]
            + " cursor_binding/page_size_binding 都是这个对象的嵌套字段，不能提升到"
            " watch_stream 顶层。GET 占位 URL 已完整表达请求时，省略本对象即可。"
        )
        parameters["source_id"] = (
            "open 必填：由当前准备轮明确选择并长期保持不变的稳定来源编号。"
            "它只用于机械绑定来源、断点和专属工作者，不表达设备类型或判断规则。"
        )
        parameters["source_profile_ref"] = (
            "open 可选："
            + parameters["source_profile_ref"]
            + " 如果主代理在 prepare 中形成了适合该来源工作者长期读取的说明，可传当前"
            " Audit 工作区内真实文件；没有这类文件时不要为了满足底座格式专门制造。"
        )
    return parameters


def _surface_description(surface: str, parameters: dict[str, str]) -> str:
    description = _DESCRIPTION
    if surface == "audit_coordinator":
        description += (
            " 当前运行是命名 Audit 协调者；公开结构只提供 list/status/inspect，"
            "不能领取来源批次或提交首次结论。inspect 优先原样传观察事件的 source_ref。"
        )
    elif surface == "audit_source":
        description += (
            " 当前运行是已绑定来源工作者；公开结构只提供自己来源的领取、逐条结论和只读检查动作。"
            " 动作参数互斥：pull 只传 watch_id、可选 max_wait_seconds/target_records，绝不能"
            "携带 delivery_ref 或 verdicts；verdict 只传 watch_id、刚才"
            " pull 返回的 delivery_ref，以及逐条判断参数。首次判断在 verdicts 中为本轮"
            "已完成的每条原样复制 verdict_token；省略 ack_id/source_ref/"
            "event_sha256。每条 hit 都要携带 finding；宿主会由结构化 hit 机械绑定"
            " requires_llm_report=true，模型无需重复填写。"
            "令牌不能重复或跨批；遗漏行保持待判并在下一次 pull 取得新令牌。"
            "不要为尚未 pull 的"
            "批次猜测 delivery_ref。如果提交后才发现某条判错，必须对该条用"
            " review=true 和准确 ack_id/source_ref/event_sha256 追加更正；"
            "不要用 record_finding 代替 verdict 更正。"
        )
        parameters["max_wait_seconds"] = (
            parameters["max_wait_seconds"] + " 仅 action=pull 可用；其他 action 必须省略。"
        )
        parameters["target_records"] = (
            parameters["target_records"]
            + " 仅 action=pull 可用；通常省略并让当前上下文/时间预算决定，不能和 verdict 一起传。"
        )
        parameters["delivery_ref"] = (
            parameters["delivery_ref"]
            + " 仅 action=verdict 可用，且只能原样复制紧邻成功 pull 的返回；pull/status/inspect 必须省略。"
        )
        parameters["verdicts"] = (
            parameters["verdicts"] + " 仅 action=verdict 可用；pull/status/inspect 必须省略。"
        )
    elif surface == "audit_binding":
        description += (
            " 当前运行是来源工作者的首次绑定阶段；open 的现场传输参数由宿主从已发布绑定补入，"
            "模型只需选择 action=open，不要重写地址或请求结构。"
        )
    elif surface == "audit_prepare":
        description += (
            " 当前运行是命名 Audit 的准备轮；公开结构只允许临时打开一条现场来源、"
            "领取完整样本、只读检查并关闭。不要调用 sample/configure/verdict，也不要把"
            " sample_count 混入 open。先选择稳定 source_id；主代理可按现场需要编写脚本、"
            "测试、说明、Skill 或笔记，底座不规定业务资料格式。最终一次 open 只需带"
            "这个 source_id、可选资料引用和准备发布的完整传输参数；最终传输绑定仍由"
            " publish_audit_update 单独发布，临时 watch 本身不代表配置已生效。"
        )
    return description


def _watch_stream_tool_spec(
    surface: str,
    names: set[str],
    parameters: dict[str, str],
    parameter_schema: dict[str, object],
    description: str,
) -> ToolSpec:
    return ToolSpec(
        name="watch_stream",
        category="web",
        # 同一工具包含读写动作。顶层保守按 mutating 进入统一副作用/幂等入口；
        # 只有明确的只读 action 才可在持有已校验参数的运行门中降级。
        effect="mutating",
        effect_by_parameter={
            "action": {
                "sample": "read_only",
                "inspect": "read_only",
                "status": "read_only",
                "list": "read_only",
                "open": "mutating",
                "configure": "mutating",
                "pull": "mutating",
                "verdict": "mutating",
                "close": "mutating",
            }
        },
        idempotency_scope="operation",
        output_trust="external_data",
        description=description,
        use_cases=[
            "持续采集 HTTP 游标流并保留断点、覆盖率和完整原文",
            "定时读取快照接口(mode=poll)，把每次完整响应作为记录",
            "读取一次性大文件或持续增长日志，并在重启后从记录边界续接",
            "/audit 保证档中逐条交付、逐条签收并按 source_ref 复查",
            "由 create_subagents 的 audit_source_id 建立的来源叶子只传 action=open；"
            "运行时从该 Audit 的已发布绑定补入全部传输字段",
        ],
        avoid_when=[
            "一次性读取普通网页/文档/API → 用 web_fetch",
            "定向带参数查一次接口做交叉印证 → 用 web_fetch(本工具是持续盯守)",
            "读普通小文件 → 用 read_file",
        ],
        keywords=[
            "数据流",
            "盯守",
            "监控",
            "游标",
            "增量",
            "高吞吐",
            "stream",
            "watch",
            "pull",
            "候选",
            "初筛",
            "背压",
            "判据",
            "spec",
            "日志",
            "大文件",
            "tail",
            "定时查",
            "轮询",
            "poll",
            "快照",
        ],
        parameters=parameters,
        parameter_schema=parameter_schema,
        required_parameters=["action"],
        local_file_url_parameters=("url",),
        parameter_details={
            name: f"{parameters.get(name, '')} {value}".strip()
            for name, value in _PARAMETER_DETAILS.items()
            if name in names
        },
        examples=_surface_examples(
            surface,
            _watch_stream_examples(),
        ),
        trusted_parameter_bindings=_trusted_parameter_bindings(surface, names),
    )


def _watch_stream_examples() -> list[str]:
    return [
        '{"tool":"watch_stream","action":"open","url":"https://source.example/events?position=<next>&page_size=<limit>","mode":"cursor","source_id":"source-a","source_profile_ref":"docs/sources/source-a.md","document_refs":["docs/sources/source-a-fields.xlsx"],"watch_window_seconds":1200}',
        '{"tool":"watch_stream","action":"open","url":"https://source.example/query","http_request":{"method":"POST","json_body":{"page":{}},"cursor_binding":{"location":"json_body","path":["page","cursor"],"initial":0},"page_size_binding":{"location":"json_body","path":["page","size"]}},"record_list_field":"records","cursor_field":"next","cursor_semantics":"next_position"}',
        '{"tool":"watch_stream","action":"open","url":"file:///var/log/app/events.log","watch_window_seconds":3600}',
        '{"tool":"watch_stream","action":"open","url":"file:///var/log/app/multiline.log","record_boundary":{"mode":"delimiter","delimiter":"\\n---END---\\n"}}',
        '{"tool":"watch_stream","action":"open","url":"http://192.168.1.50:9100/health","mode":"poll","poll_query_seconds":60}',
        '{"tool":"watch_stream","action":"sample","watch_id":"ws-ab12cd34ef","sample_count":300}',
        '{"tool":"watch_stream","action":"configure","watch_id":"ws-ab12cd34ef","spec":{"result_field":"state","normal_values":["ready"],"ignore_fields":["trace_id"]}}',
        '{"tool":"watch_stream","action":"pull","watch_id":"ws-ab12cd34ef","max_wait_seconds":45}',
        '{"tool":"watch_stream","action":"verdict","delivery_ref":"ad-0123456789abcdef01234567","verdicts":[{"verdict_token":"vt-0123456789abcdef01234567","verdict":"hit","score":18,"score_range":{"min":0,"max":20},"dimensions":[{"name":"维度A","score":8,"range":{"min":0,"max":10},"reason":"记录中的对应事实"},{"name":"维度B","score":10,"range":{"min":0,"max":10},"reason":"关联证据"}],"note":"结合两个维度得到总分","finding":{"claim":"该记录满足当前 Audit 的汇报条件","kind":"hit"}}]}',
        '{"tool":"watch_stream","action":"verdict","verdicts":[{"ack_id":"12:0","source_ref":"audit://ws-ab12cd34ef/candidate/12:0","event_sha256":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef","review":true,"verdict":"hit","score":95,"note":"后续证据证明首次判断需更正","finding":{"claim":"补充证据已证明该记录满足当前 Audit 的汇报条件","kind":"hit","stage":"review"}}]}',
        '{"tool":"watch_stream","action":"inspect","source_ref":"audit://ws-ab12cd34ef/candidate/12:0"}',
        '{"tool":"watch_stream","action":"list"}',
    ]


# LLM: 宿主事实只补给已经由结构化任务范围绑定的来源工作者；prepare、普通 watch 和
# 协调者必须显式选择来源，不能被旧任务属性悄悄改绑。
# 函数用途: 为不同工具表面声明最小可信补参范围，避免一次 prepare 探针的 watch_id
# 泄露到下一次 open，同时保留来源叶子的单 watch 硬边界。
def _trusted_parameter_bindings(
    surface: str,
    names: set[str],
) -> dict[str, TrustedParameterBinding]:
    if surface not in {"audit_binding", "audit_source"}:
        return {}
    bindings: dict[str, TrustedParameterBinding] = {}
    if surface == "audit_binding":
        bindings.update(
            {
                name: TrustedParameterBinding(
                    source_refs=(f"run_scope.task_attributes.audit_source_open.{name}",),
                    when=(("action", "open"),),
                )
                for name in AUDIT_SOURCE_OPEN_FIELDS
                if name in names
            }
        )
    if "watch_id" in names:
        bindings["watch_id"] = TrustedParameterBinding(
            source_refs=("run_scope.task_attributes.audit_source_watch_id",),
        )
    return bindings


__all__ = ["build_watch_stream_spec"]
