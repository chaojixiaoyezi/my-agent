
from __future__ import annotations

"""第②层 LLM 工具(6 个)—— 模型可调用,精确 parameter_schema,对齐原生 tool_use。

模型职责边界:模型只起停 daemon、查不丢对账、poll 少量候选研判、在存档里交叉验证。确定性采集
+ 扛量 + 不丢都在第①层 daemon 里,模型不碰全量日志(撑数天数月的关键)。

防幻觉:log_alert_poll / log_source_query / log_archive_stats / log_monitor_status 返回的全是
确定性真实数据(候选队列里 triage 命中的真实行 / 存档里 grep 的真实命中 / 文件真实行数对账)。
工具描述里明确要求:研判结论必须基于这些真实返回,不得凭空判断。
"""

import json
import time
from pathlib import Path
from typing import Any

from ..models import BaseTool, ToolExecutionResult, ToolSpec
from . import manager, novelty, query
from .store import LogOpsStore
from .triage import rule_catalog

_DEFAULT_SUBDIR = ".log_ops"
# poll 单次默认/上限给多少条候选(防一次拉爆 prompt;研判是小批多轮)。
_DEFAULT_POLL_LIMIT = 20
_MAX_POLL_LIMIT = 200
_DEFAULT_QUERY_LIMIT = 100


def _store(workspace_root: Path, monitor_id: str) -> LogOpsStore:
    return LogOpsStore(workspace_root / _DEFAULT_SUBDIR, monitor_id or "default")


def _monitor_id(params: dict[str, Any]) -> str:
    return str(params.get("monitor_id") or "default").strip() or "default"


def _coerce_sources(value: Any) -> list[str]:
    """sources 可能被模型发成 JSON 字符串/逗号分隔串/真列表,统一成字符串列表。"""
    if isinstance(value, list):
        return _clean_str_list(value)
    if isinstance(value, str):
        return _coerce_sources_from_str(value)
    return []


def _coerce_sources_from_str(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if text.startswith("["):
        parsed = _try_json_list(text)
        if parsed is not None:
            return _clean_str_list(parsed)
    # 逗号或换行分隔。
    return _clean_str_list(text.replace("\n", ",").split(","))


def _clean_str_list(items: list[Any]) -> list[str]:
    return [stripped for item in items if (stripped := str(item).strip())]


def _try_json_list(text: str) -> list[Any] | None:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else None


def _ok(tool: str, payload: dict[str, Any]) -> ToolExecutionResult:
    return ToolExecutionResult(tool, True, json.dumps(payload, ensure_ascii=False, sort_keys=True), result_envelope=payload)


def _err(tool: str, code: str, message: str, extra: dict[str, Any] | None = None) -> ToolExecutionResult:
    payload = {"ok": False, "error_code": code, "message": message}
    if extra:
        payload.update(extra)
    return ToolExecutionResult(tool, False, json.dumps(payload, ensure_ascii=False, sort_keys=True), error_code=code)


class _LogOpsTool(BaseTool):
    """共享 workspace_root + store 派生的基类。

    execute 是统一的「带精确错误码」入口:子类只实现 _run(纯业务),任何从 _run 逃逸
    的异常都在这里收口成已注册的精确码,绝不让裸异常回落到 UNKNOWN_ERROR(retryable=False)。
    根因(log_alert_poll 2 小时真机实锤):并发 daemon 写候选时 poll 读到半截多字节行 →
    UnicodeDecodeError 逃逸 → 无 error_code → 框架兜底 UNKNOWN_ERROR(不可重试)误导模型
    放弃值班。store 层已让读容错(errors="ignore"),这里再兜底所有 IO/状态类异常:
    LookupError/ValueError/TypeError 多半是入参问题 → TOOL_INVALID_ARGUMENTS(改参可重试);
    其余(OSError/解码/JSON 等运行时异常)→ TOOL_EXECUTION_FAILED(原样可重试)。两者都
    retryable=True,模型会重试而非放弃。
    """

    def __init__(self, workspace_root: Path):
        self.workspace_root = Path(workspace_root).resolve()

    def store(self, params: dict[str, Any]) -> LogOpsStore:
        return _store(self.workspace_root, _monitor_id(params))

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        try:
            return self._run(params if isinstance(params, dict) else {})
        except (LookupError, ValueError, TypeError) as exc:
            return _err(
                self.spec.name,
                "TOOL_INVALID_ARGUMENTS",
                f"{self.spec.name} 参数无效或缺失:{exc}",
            )
        except Exception as exc:  # noqa: BLE001 — 任何运行时异常都给精确可重试码,不逃逸成 UNKNOWN_ERROR
            return _err(
                self.spec.name,
                "TOOL_EXECUTION_FAILED",
                f"{self.spec.name} 执行时发生可恢复异常:{exc}",
            )

    def _run(self, params: dict[str, Any]) -> ToolExecutionResult:
        raise NotImplementedError


class LogMonitorStartTool(_LogOpsTool):
    spec = ToolSpec(
        name="log_monitor_start",
        category="log_ops",
        effect="mutating",
        requires_idempotency=True,
        description=(
            "启动常驻的确定性采集 daemon,监控指定日志源(文件/文件夹/API)。daemon 是独立进程,"
            "后台每隔几秒增量采集→全量落存档(一条不丢)→确定性规则初筛产候选告警,零 LLM 调用,"
            "可长期运行数天数月。返回 daemon 句柄/状态。"
        ),
        use_cases=[
            "开始高吞吐安全日志监控:把若干文件/文件夹/API 源交给后台 daemon 持续采集+初筛",
            "需要长期(数小时到数天)不丢地盯着日志,只在有候选告警时被叫出来研判",
        ],
        avoid_when=[
            "只想一次性在已有存档里查东西时用 log_source_query,不必起 daemon",
            "daemon 已在跑且源没变时不必重复调用(本工具幂等,会返回 already_running)",
        ],
        keywords=["日志", "监控", "采集", "log", "monitor", "daemon", "start", "安全", "告警", "常驻", "ingest"],
        parameters={
            "sources": "要监控的源列表。每项是文件路径、文件夹路径或 API URL。",
            "poll_interval_seconds": "可选。采集循环间隔秒数,默认 2 秒。",
            "api_fetch_mode": "可选。API 采集方式:since(默认,?since=cursor)或 range(?start=N&end=M 按编号区间分页)。",
            "report_floor": "可选。分级汇报阈值 P0/P1/P2/P3(默认 P1):低于此级别的 log_report 只记审计不推送用户。",
            "monitor_id": "可选。监控实例标识,默认 default。多套独立监控用不同 id 隔离。",
        },
        parameter_details={
            "sources": "必填。字符串数组,如 ['/var/log/a.log','/var/log/dir','http://127.0.0.1:9001/poll']。文件夹监控其下新文件;API 按所选模式轮询。",
            "poll_interval_seconds": "可选数字。两轮采集之间 sleep 多少秒,默认 2。源更新快可调小。",
            "api_fetch_mode": "可选字符串 since|range,默认 since。range 适合带递增编号、要按 [start,end] 区间查的 API。",
            "report_floor": "可选字符串 P0-P3,默认 P1。控制'什么级别才推送用户',防告警风暴。",
            "monitor_id": "可选字符串。同一 workspace 下多个独立监控用不同 id;同 id 重复 start 是幂等的。",
        },
        parameter_schema={
            "sources": {"type": "array", "items": {"type": "string"}},
            "poll_interval_seconds": {"type": "number"},
            "api_fetch_mode": {"type": "string", "enum": ["since", "range"]},
            "report_floor": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
            "monitor_id": {"type": "string"},
        },
        required_parameters=["sources"],
        examples=[
            '{"tool": "log_monitor_start", "sources": ["/var/log/auth.log", "/var/log/nginx", "http://127.0.0.1:9001/poll"]}'
        ],
    )

    def _run(self, params: dict[str, Any]) -> ToolExecutionResult:
        sources = _coerce_sources(params.get("sources"))
        if not sources:
            return _err(
                self.spec.name,
                "TOOL_INVALID_ARGUMENTS",
                "log_monitor_start 需要非空 sources(文件路径/文件夹路径/API URL 列表)。",
            )
        interval = _coerce_float(params.get("poll_interval_seconds"), default=2.0)
        store = self.store(params)
        _preinit_api_range(store, sources, params.get("api_fetch_mode"))
        result = manager.start_daemon(store, sources, poll_interval_seconds=interval)
        if not result.get("ok", False):
            return _err(self.spec.name, "LOG_OPS_DAEMON_ERROR", str(result.get("message") or "启动失败"), extra=result)
        store.update_config_field("report_floor", _norm_report_floor(params.get("report_floor")))
        result.setdefault(
            "hint",
            "daemon 已在后台采集。用 log_monitor_status 查不丢对账,log_alert_poll 拉新候选告警来研判。",
        )
        return _ok(self.spec.name, result)


class LogMonitorStatusTool(_LogOpsTool):
    spec = ToolSpec(
        name="log_monitor_status",
        category="log_ops",
        effect="read_only",
        description=(
            "查采集 daemon 状态与不丢证据:daemon 是否存活/心跳、每源已处理量与断点(offset/cursor/"
            "已处理文件数)、候选告警总数,以及关键的不丢对账——逐源比对'已采集行数 vs 存档行数'是否一致。"
        ),
        use_cases=[
            "确认 daemon 还活着、采到哪了、攒了多少候选告警",
            "核对不丢:看每源 collected==archived==存档文件行数 是否一致(no_loss=true)",
            "周期性巡检长跑监控的健康度",
        ],
        avoid_when=["要取具体候选告警内容来研判时用 log_alert_poll", "要在存档里查具体行时用 log_source_query"],
        keywords=["状态", "监控", "对账", "不丢", "status", "health", "offset", "cursor", "存活", "心跳", "进度"],
        parameters={"monitor_id": "可选。监控实例标识,默认 default。"},
        parameter_details={"monitor_id": "可选字符串。要查哪个监控实例,默认 default。"},
        parameter_schema={"monitor_id": {"type": "string"}},
        required_parameters=[],
        examples=['{"tool": "log_monitor_status"}'],
    )

    def _run(self, params: dict[str, Any]) -> ToolExecutionResult:
        store = self.store(params)
        recovery = manager.ensure_daemon_alive(store)  # 看门狗自愈:进程异常消失则自动重拉(尊重喊停/不擅起)
        liveness = manager.daemon_liveness(store)
        recon = manager.reconciliation(store)
        daemon_record = store.read_daemon()
        unread = max(0, store.count_candidates() - store.read_poll_cursor())
        started = daemon_record.get("started_at")
        uptime = (time.time() - float(started)) if started else None
        payload = {
            "ok": True,
            "monitor_id": store.monitor_id,
            "daemon_alive": liveness["alive"],
            "daemon_pid": liveness.get("pid", 0),
            "daemon_reason": liveness.get("reason", ""),
            # 确定的物理运行时长 —— 值守任务判断"已盯了多久"必须用它,别靠数轮次/感觉估(弱模型易高估早退)。
            "daemon_uptime_seconds": round(uptime, 1) if uptime is not None else None,
            "daemon_uptime_minutes": round(uptime / 60, 1) if uptime is not None else None,
            "heartbeat_age_seconds": liveness.get("heartbeat_age"),
            "cycles": daemon_record.get("cycles", 0),
            "poll_interval_seconds": daemon_record.get("poll_interval_seconds"),
            "candidates_total": recon["total_candidates"],
            "candidates_unread": unread,
            "no_loss": recon["no_loss"],
            "reconciliation": recon,
            "root": str(store.root),
        }
        if unread > 50:
            payload["backlog_hint"] = (
                f"还有 {unread} 条候选未研判。值守是清积压的活,积压未清就不算值守到位——别因'感觉盯了一阵'就收尾,"
                f"继续 log_alert_poll 把积压研判下去(或交给唤醒器下一轮)。"
            )
        if not recon["no_loss"]:
            payload["warning"] = "存在源 collected!=archived!=存档行数,疑似采集异常,请核查 per_source.last_error。"
        if recon.get("sources_circuit_open"):
            payload["circuit_alert"] = (
                f"数据源 {recon['sources_circuit_open']} 连续采集失败已熔断(疑似失联/被攻击者打掉/网络故障),"
                f"冷却期内暂停采集以免空烧;请核查这些源连通性并向用户报告。"
            )
        if recovery.get("restarted"):
            payload["daemon_recovered"] = (
                f"检测到 daemon 进程异常消失,看门狗已自动重拉(新 pid={recovery['pid']});采集已恢复,期间未采的会从断点续上。"
            )
        return _ok(self.spec.name, payload)


def _select_poll_batch(window: list[dict[str, Any]], limit: int) -> tuple[list[dict[str, Any]], int, int]:
    """从候选窗口顺序选 ≤limit 个值得研判的(非 low-only 误报),途中跳过 low 误报(消费但不研判,不让噪声拖死
    poll);选够 limit 就停,剩余"值得研判的"留给下次,不丢真威胁。返回 (batch, consumed=cursor推进量, skipped_low)。"""
    batch: list[dict[str, Any]] = []
    consumed = skipped = 0
    for cand in window:
        is_noise = str(cand.get("severity", "")).lower() == "low" and not cand.get("novel")
        if len(batch) >= limit and not is_noise:
            break  # 已选够,遇到下一个该研判的就停 → 留给下次,不丢
        consumed += 1
        if is_noise:
            skipped += 1
        else:
            batch.append(cand)
    return batch, consumed, skipped


class LogAlertPollTool(_LogOpsTool):
    spec = ToolSpec(
        name="log_alert_poll",
        category="log_ops",
        effect="mutating",
        # poll 推进「已读游标」是有副作用的（mutating），按 manifest 契约必须声明幂等策略：
        # side-effecting(mutating/dangerous)工具 requires_idempotency 必须为 True,否则
        # tool_manifest 门会判 TOOL_MANIFEST_IDEMPOTENCY_POLICY_MISSING **在 execute 之前**
        # 0.00s 拦死(该码此前未注册 → 兜底成 UNKNOWN_ERROR/retryable=False,误导主代理
        # 以为核心循环被永久阻塞而停手——日志运营 2 小时真机实锤)。框架对 mutating 工具
        # 会自动派生 idempotency_key,模型无需手填;同 offset 重读天然幂等,语义正确。
        requires_idempotency=True,
        description=(
            "拉取新的候选告警(确定性初筛命中的真实日志行)给你研判。拉过的会标记已读,下次只给更新的,"
            "不重复。每条候选带:原始日志行全文、命中规则、源标识、行号、时间戳、指纹,以及提取的攻击者 IOC 和 "
            "novel 标记。返回里 novel_attackers 列出本批**首次出现的攻击者来源**——是新攻击实体,务必逐个研判上报,"
            "别被'手法眼熟'骗成已知漏掉(海量候选长期值守下,新攻击者被当已知坍缩是头号漏报源)。研判须基于真实证据。"
        ),
        use_cases=[
            "周期性拉新候选告警来逐条研判是真威胁还是误报",
            "撑长时间监控:每隔一段时间 poll 一次,处理新攒下的候选",
        ],
        avoid_when=[
            "只想看总数/对账时用 log_monitor_status(poll 会推进已读游标)",
            "要在存档里交叉验证某条告警时用 log_source_query",
        ],
        keywords=["告警", "候选", "拉取", "poll", "alert", "研判", "队列", "新", "未读", "triage"],
        parameters={
            "limit": "可选。本次最多拉多少条新候选,默认 20,上限 200。",
            "monitor_id": "可选。监控实例标识,默认 default。",
            "peek": "可选。true 则只看不标记已读(下次仍会再给),默认 false。",
        },
        parameter_details={
            "limit": "可选整数。一次拉太多会撑爆上下文;研判建议小批多轮,默认 20。",
            "monitor_id": "可选字符串,默认 default。",
            "peek": "可选布尔。true=预览不推进已读游标;false(默认)=拉取并标记已读。",
        },
        parameter_schema={
            "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_POLL_LIMIT},
            "monitor_id": {"type": "string"},
            "peek": {"type": "boolean"},
        },
        required_parameters=[],
        examples=['{"tool": "log_alert_poll", "limit": 20}'],
    )

    def _run(self, params: dict[str, Any]) -> ToolExecutionResult:
        store = self.store(params)
        limit = _coerce_int(params.get("limit"), default=_DEFAULT_POLL_LIMIT, lo=1, hi=_MAX_POLL_LIMIT)
        peek = _coerce_bool(params.get("peek"), default=False)
        cursor = store.read_poll_cursor()
        total = store.count_candidates()
        # 扫描窗口,顺序选值得研判的候选,途中跳过 low 误报(消费但不研判)——不让海量误报(实测 unusual-ssh-user 类
        # 宽规则占 74%)把 poll 拖死、真威胁饿死;选够 limit 就停,剩余真候选留下次,不丢(整窗推进会漏 high,已弃)。
        _sev_rank = {"critical": 0, "high": 0, "medium": 1, "low": 2}
        window = store.read_candidates(offset=cursor, limit=max(limit * 5, 200))
        novelty.annotate_novelty(store, window, persist=False)  # 先标 novel(选择时豁免 novel 的 low),仅返回的持久
        batch, consumed, skipped_low = _select_poll_batch(window, limit)
        new_cursor = cursor + consumed
        if consumed and not peek:
            store.write_poll_cursor(new_cursor)
        batch.sort(key=lambda c: (0 if c.get("novel") else 1, _sev_rank.get(str(c.get("severity", "")).lower(), 2)))
        novelty_info = novelty.annotate_novelty(store, batch, persist=not peek)  # 仅返回研判的并入已见集
        payload = {
            "ok": True,
            "monitor_id": store.monitor_id,
            "returned": len(batch),
            "scanned": len(window),
            "skipped_low_severity": skipped_low,
            "candidates_total": total,
            "cursor_before": cursor,
            "cursor_after": (cursor if peek else new_cursor),
            "remaining_unread": max(0, total - (cursor if peek else new_cursor)),
            "peek": peek,
            "novel_attacker_count": novelty_info["novel_count"],
            "novel_attackers": novelty_info["novel_attackers"][:30],
            "alerts": batch,
        }
        if novelty_info["novel_count"]:
            payload["novelty_alert"] = (
                f"⚠️ 本批有 {novelty_info['novel_count']} 个首次出现的攻击者 IOC:"
                f"{', '.join(novelty_info['novel_attackers'][:20])}。这些是新攻击来源(此前没见过),"
                f"每条带 novel=true 的候选务必逐个研判——别因为'攻击手法眼熟'就归为已知忽略,"
                f"新 IOC = 新攻击实体,不同攻击者各自定级上报。"
            )
        if payload["remaining_unread"] > limit and not peek:
            payload["backlog_hint"] = (
                f"还有 {payload['remaining_unread']} 条候选未研判(本批取了 {len(batch)} 条)。值守的本质是清积压——"
                f"别 poll 一两批就觉得'看完了/无新威胁'而收尾,继续 log_alert_poll 把积压研判到接近清空,再交给下一轮。"
            )
        if not batch:
            payload["message"] = "暂无新候选告警(已读到队尾)。可稍后再 poll,或先 log_monitor_status 看 daemon 是否在采。"
        return _ok(self.spec.name, payload)


class LogSourceQueryTool(_LogOpsTool):
    spec = ToolSpec(
        name="log_source_query",
        category="log_ops",
        effect="read_only",
        description=(
            "联合查询:在某个源的全量存档里按 pattern(正则)+可选时间范围做确定性 grep,返回真实命中的"
            "原始日志行(带行号/时间戳)。研判时交叉验证用——这是确定性真实结果,不是臆测,研判结论应基于此。"
        ),
        use_cases=[
            "研判某条候选告警时,在同源存档里查同一 IP/用户/关键词的其他相关行,看上下文",
            "按时间窗口检索某段时间内匹配某模式的所有日志行",
            "交叉验证一个攻击指标(IOC)在存档里到底出现过几次、何时",
        ],
        avoid_when=["要拉初筛产出的候选告警时用 log_alert_poll", "要总量/对账时用 log_monitor_status / log_archive_stats"],
        keywords=["查询", "存档", "检索", "grep", "query", "search", "交叉验证", "证据", "正则", "时间范围", "archive"],
        parameters={
            "source": "要查的源:source_id 或原始 locator(文件路径/文件夹路径/API URL,与 start 时一致)。",
            "pattern": "匹配模式(正则;非法正则按字面子串匹配)。如 '198\\.51\\.100\\.7' 或 'ALERT-000123'。",
            "time_range": "可选。[start_iso, end_iso] 两元素数组,按行首 ISO 时间过滤。",
            "limit": "可选。最多返回多少匹配行,默认 100,上限 500。",
            "case_sensitive": "可选。是否区分大小写,默认 false。",
            "monitor_id": "可选。监控实例标识,默认 default。",
        },
        parameter_details={
            "source": "必填字符串。可用 log_monitor_status 的 per_source 里的 source_id,或直接给 start 时的 locator。",
            "pattern": "必填字符串。Python 正则;若正则语法非法则退化为字面子串匹配,不会报错。",
            "time_range": "可选字符串数组,2 个元素 [起,止],ISO8601(如 '2026-06-16T17:44:00')。只给一个就只约束那一侧。",
            "limit": "可选整数,默认 100,上限 500。命中超限会带 truncated=true。",
            "case_sensitive": "可选布尔,默认 false(忽略大小写)。",
            "monitor_id": "可选字符串,默认 default。",
        },
        parameter_schema={
            "source": {"type": "string"},
            "pattern": {"type": "string"},
            "time_range": {"type": "array", "items": {"type": "string"}},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            "case_sensitive": {"type": "boolean"},
            "monitor_id": {"type": "string"},
        },
        required_parameters=["source", "pattern"],
        examples=[
            '{"tool": "log_source_query", "source": "http://127.0.0.1:9001/poll", "pattern": "ALERT-000123"}',
            '{"tool": "log_source_query", "source": "/var/log/auth.log", "pattern": "Failed password", "time_range": ["2026-06-16T17:00:00", "2026-06-16T18:00:00"]}',
        ],
    )

    def _run(self, params: dict[str, Any]) -> ToolExecutionResult:
        source = str(params.get("source") or "").strip()
        pattern = str(params.get("pattern") or "")
        if not source or not pattern:
            return _err(
                self.spec.name,
                "TOOL_INVALID_ARGUMENTS",
                "log_source_query 需要 source(源标识)和 pattern(匹配模式)。",
            )
        request = query.QueryRequest(
            source=source,
            pattern=pattern,
            time_range=_coerce_time_range(params.get("time_range")),
            limit=_coerce_int(params.get("limit"), default=_DEFAULT_QUERY_LIMIT, lo=1, hi=500),
            case_sensitive=_coerce_bool(params.get("case_sensitive"), default=False),
        )
        result = query.query_archive(self.store(params), request)
        if not result.get("ok", False):
            return _err(self.spec.name, "TOOL_INVALID_ARGUMENTS", str(result.get("message") or "查询失败"), extra=result)
        return _ok(self.spec.name, result)


class LogArchiveStatsTool(_LogOpsTool):
    spec = ToolSpec(
        name="log_archive_stats",
        category="log_ops",
        effect="read_only",
        description=(
            "全量存档统计:每源存档真实行数、候选告警数、断点 cursor,以及总量与不丢对账结论。"
            "用来验证'一条不丢'(已采集行数==存档文件行数)和总体规模。"
        ),
        use_cases=["验证不丢:看每源 collected_lines 与 archive_file_lines 是否相等", "了解监控总体规模:总采集行数、总候选数、各源分布"],
        avoid_when=["要 daemon 存活/心跳这类运行态时用 log_monitor_status", "要具体告警内容时用 log_alert_poll"],
        keywords=["统计", "存档", "全量", "stats", "archive", "不丢", "行数", "总量", "对账", "规模"],
        parameters={"monitor_id": "可选。监控实例标识,默认 default。"},
        parameter_details={"monitor_id": "可选字符串,默认 default。"},
        parameter_schema={"monitor_id": {"type": "string"}},
        required_parameters=[],
        examples=['{"tool": "log_archive_stats"}'],
    )

    def _run(self, params: dict[str, Any]) -> ToolExecutionResult:
        store = self.store(params)
        recon = manager.reconciliation(store)
        payload = {
            "ok": True,
            "monitor_id": store.monitor_id,
            "no_loss": recon["no_loss"],
            "total_collected_lines": recon["total_collected_lines"],
            "total_archive_file_lines": recon["total_archive_file_lines"],
            "total_candidates": recon["total_candidates"],
            "per_source": recon["per_source"],
            "triage_rules": rule_catalog(),
        }
        return _ok(self.spec.name, payload)


class LogMonitorStopTool(_LogOpsTool):
    spec = ToolSpec(
        name="log_monitor_stop",
        category="log_ops",
        effect="mutating",
        requires_idempotency=True,
        description=(
            "停止采集 daemon(SIGTERM→宽限→SIGKILL)。状态文件(offset/cursor/已处理集合)保留,"
            "之后 log_monitor_start 可从断点续接续采,不重不丢。"
        ),
        use_cases=["监控任务结束,收尾停掉后台 daemon", "要重配源,先停再以新 sources 起"],
        avoid_when=["还要继续监控时别停", "daemon 本就没在跑时调用会返回 not_running(无害)"],
        keywords=["停止", "监控", "stop", "terminate", "收尾", "daemon", "关闭", "结束"],
        parameters={"monitor_id": "可选。监控实例标识,默认 default。"},
        parameter_details={"monitor_id": "可选字符串,默认 default。"},
        parameter_schema={"monitor_id": {"type": "string"}},
        required_parameters=[],
        examples=['{"tool": "log_monitor_stop"}'],
    )

    def _run(self, params: dict[str, Any]) -> ToolExecutionResult:
        result = manager.stop_daemon(self.store(params))
        return _ok(self.spec.name, result)


# ---------- 参数强健化(模型常把类型发偏,统一收敛) ----------


def _coerce_int(value: Any, *, default: int, lo: int, hi: int) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(out, hi))


def _coerce_float(value: Any, *, default: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out > 0 else default


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return default


def _coerce_time_range(value: Any) -> tuple[str, str] | None:
    if isinstance(value, str):
        value = _time_range_from_str(value)
    if not isinstance(value, (list, tuple)):
        return None
    items = [str(item).strip() for item in value]
    start = items[0] if len(items) >= 1 else ""
    end = items[1] if len(items) >= 2 else ""
    if not start and not end:
        return None
    return (start, end)


def _time_range_from_str(text: str) -> list[Any]:
    """把字符串形式的 time_range 解析成列表:'[a,b]' JSON 或 'a,b' 逗号分隔。解析失败给空列表。"""
    text = text.strip()
    if not text:
        return []
    if text.startswith("["):
        return _try_json_list(text) or []
    return [chunk.strip() for chunk in text.split(",")]


def _norm_report_floor(value: Any) -> str:
    """规范化分级汇报阈值;非法 → P1(默认)。"""
    floor = str(value or "P1").strip().upper()
    return floor if floor in ("P0", "P1", "P2", "P3") else "P1"


def _preinit_api_range(store: LogOpsStore, sources: list[str], api_fetch_mode: Any) -> None:
    """api_fetch_mode==range 时预写 api 源的 range 断点(start_daemon 的 _preinit 不覆盖已存在 state)。"""
    if str(api_fetch_mode or "").strip() != "range":
        return
    from .store import build_source_specs

    for spec in build_source_specs(sources):
        if spec.kind == "api" and not store.state_path(spec.source_id).exists():
            store.write_state(spec.source_id, {"cursor": 0, "mode": "range", "batch": 100})


def log_ops_tools(workspace_root: Path) -> list[BaseTool]:
    """构造全部 log_ops 工具实例(6 核心 + 5 自适应层),供 registry 注册。"""
    from .tools_adaptive import adaptive_tools  # 延迟 import 破循环(tools_adaptive 顶层 import 本模块)
    from .tools_orchestration import orchestration_tools

    return [
        LogMonitorStartTool(workspace_root),
        LogMonitorStatusTool(workspace_root),
        LogAlertPollTool(workspace_root),
        LogSourceQueryTool(workspace_root),
        LogArchiveStatsTool(workspace_root),
        LogMonitorStopTool(workspace_root),
        *adaptive_tools(workspace_root),
        *orchestration_tools(workspace_root),
    ]


LOG_OPS_TOOL_NAMES = (
    "log_monitor_start",
    "log_monitor_status",
    "log_alert_poll",
    "log_source_query",
    "log_archive_stats",
    "log_monitor_stop",
    "log_source_sample",
    "log_profile_set",
    "log_profile_get",
    "log_report",
    "log_cross_query",
    "log_assign",
    "log_heartbeat",
    "log_duty_roster",
    "log_watchdog_scan",
    "log_lead",
    "log_correlate",
    "log_wake_check",
    "log_wake_ack",
)


__all__ = [
    "LOG_OPS_TOOL_NAMES",
    "LogAlertPollTool",
    "LogArchiveStatsTool",
    "LogMonitorStartTool",
    "LogMonitorStatusTool",
    "LogMonitorStopTool",
    "LogSourceQueryTool",
    "log_ops_tools",
]
