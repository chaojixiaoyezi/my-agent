
from __future__ import annotations

"""log_ops 自适应层 LLM 工具(块C/E/F)——协同备课 + 分级汇报 + 跨源联合查询。

- 块C 探查器:log_source_sample(看真实样本) / log_profile_set(写监控方案) / log_profile_get(查方案)
- 块E 分级汇报:log_report(按 P0-P3 分级汇报,系统按阈值决定推不推用户,不让噪声淹没)
- 块F 联合分析:log_cross_query(跨多源查同一 IOC,拼跨源事件链)

复用 tools.py 的 _LogOpsTool 基类(统一精确错误码收口)与 _coerce_* 入参强健化。
"""

import re
from pathlib import Path
from typing import Any

from ..models import BaseTool, ToolSpec
from . import query
from . import baseline
from . import novelty
from .log_template import mine_templates
from .store import level_rank
from .triage import compile_profile_rules
from .tools import (
    _coerce_int,
    _coerce_sources,
    _coerce_time_range,
    _err,
    _LogOpsTool,
    _ok,
)

_PROFILE_FIELDS = ("splitter", "rules", "triage_hint", "reporting", "fields", "format_note", "baseline")
_LEVELS = ("P0", "P1", "P2", "P3")


def _summarize_baseline(model: baseline.SourceBaseline) -> dict[str, Any]:
    """把统计基线压成给 LLM 备课看的字段画像:每字段正常值数、是否高基数(异常检测会跳过)、几个常见值。"""
    out: dict[str, Any] = {}
    for key, fstat in model.fields.items():
        top = sorted(fstat.values.items(), key=lambda kv: -kv[1])[:5]
        out[key] = {"distinct_values": len(fstat.values), "high_cardinality": fstat.is_high_card(), "common_values": [v for v, _ in top]}
    return out


class LogSourceSampleTool(_LogOpsTool):
    spec = ToolSpec(
        name="log_source_sample",
        category="log_ops",
        effect="read_only",
        description=(
            "采样某源存档:返回头部+尾部真实记录、**ML 模板挖掘**(无监督聚类成最多 30 种模板+占比)、**字段正常值画像 "
            "field_baseline**(各字段正常值集合/是否高基数)。备课时:看模板+占比搞清格式;看 field_baseline 知道哪些字段"
            "的新值系统会自动统计异常检测(不用你定宽规则,见 baseline_hint),规则只留给明确威胁特征。只读。"
        ),
        use_cases=["接入新源后先采样,搞清它的格式和字段再定方案", "复核某源最近在吐什么样的数据"],
        avoid_when=["要拉初筛候选研判时用 log_alert_poll", "要按模式检索时用 log_source_query"],
        keywords=["采样", "样本", "格式", "sample", "备课", "分析", "探查", "format"],
        parameters={"source": "源 source_id 或原始 locator", "limit": "头/尾各取多少条,默认 20,上限 200", "monitor_id": "可选,默认 default"},
        parameter_details={"source": "必填。", "limit": "可选整数,默认 20。", "monitor_id": "可选。"},
        parameter_schema={"source": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}, "monitor_id": {"type": "string"}},
        required_parameters=["source"],
        examples=['{"tool": "log_source_sample", "source": "http://127.0.0.1:9001/poll", "limit": 20}'],
    )

    def _run(self, params: dict[str, Any]) -> Any:
        store = self.store(params)
        source = str(params.get("source") or "").strip()
        if not source:
            return _err(self.spec.name, "TOOL_INVALID_ARGUMENTS", "log_source_sample 需要 source。")
        sid = query.resolve_source_id(store, source)
        limit = _coerce_int(params.get("limit"), default=20, lo=1, hi=200)
        path = store.archive_path(sid)
        if not path.exists():
            return _ok(self.spec.name, {"source_id": sid, "samples_head": [], "samples_tail": [], "message": "该源还没存档(daemon 未起或尚未采到);稍等几秒再采样。"})
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = lines[-limit:] if len(lines) > limit else []
        recent = lines[-5000:]
        templates = mine_templates(recent, max_templates=30)  # ML前期:压成模板+占比,看清格式/分布
        field_baseline = _summarize_baseline(baseline.build_baseline(recent))  # 各字段正常值画像,指导少定宽规则
        return _ok(self.spec.name, {
            "source_id": sid, "total_archived": len(lines), "samples_head": lines[:limit], "samples_tail": tail,
            "templates": templates, "field_baseline": field_baseline,
            "baseline_hint": (
                "field_baseline 是各字段正常值画像。**low-cardinality 字段(high_cardinality=false,如 user/status/action)的"
                "新值/罕见值,系统已自动统计异常检测(daemon 层),不用为它们定'未见过就告警'类宽规则**——那种规则极易误报"
                "淹没(实测可占候选 74%)。规则只留给明确威胁特征(反弹shell/SQL注入/已知恶意域名/敏感文件访问等)。"
                "high_cardinality 字段(时间戳/id)异常检测会跳过,也别为它们定规则。"
            ),
        })


class LogProfileSetTool(_LogOpsTool):
    spec = ToolSpec(
        name="log_profile_set",
        category="log_ops",
        effect="mutating",
        requires_idempotency=True,
        description=(
            "给某个源设定/更新监控方案 profile,daemon 会按它执行。可设:splitter(切割方式)、"
            "rules(初筛规则,每条 {name,severity,pattern})、triage_hint(研判方向)、reporting(汇报策略)、"
            "baseline(正常基线)。备课分析清楚、确认无误后再写。多次调用按字段合并,version 自增。"
        ),
        use_cases=["备课分析完某源,把切割方式+初筛规则+研判方向固化成方案", "试跑后回看候选质量,迭代调整该源规则"],
        avoid_when=["还没看过真实样本就别凭空写方案", "只想查现有方案用 log_profile_get"],
        keywords=["方案", "profile", "规则", "切割", "定方案", "备课", "rules", "splitter", "研判方向"],
        parameters={
            "source": "源 source_id 或 locator",
            "splitter": "可选。切割方式 {type:line|json_lines|multiline_start|delimiter, params:{...}}",
            "rules": "可选。初筛规则数组,每条 {name, severity(high/medium/low), pattern(正则)}",
            "triage_hint": "可选。研判方向/上下文(给研判时看)",
            "reporting": "可选。汇报策略,如 {min_level: P1}",
            "baseline": "可选。正常基线,如 {normal_rate_per_min: 200}",
            "monitor_id": "可选,默认 default",
        },
        parameter_details={"source": "必填。", "rules": "可选数组;非法/危险(ReDoS)正则会被安全校验跳过。", "triage_hint": "可选字符串。"},
        parameter_schema={
            "source": {"type": "string"},
            "splitter": {"type": "object"},
            "rules": {"type": "array", "items": {"type": "object"}},
            "triage_hint": {"type": "string"},
            "reporting": {"type": "object"},
            "baseline": {"type": "object"},
            "monitor_id": {"type": "string"},
        },
        required_parameters=["source"],
        examples=['{"tool": "log_profile_set", "source": "http://127.0.0.1:9001/poll", "rules": [{"name":"auth_fail","severity":"high","pattern":"failed login"}], "triage_hint": "登录日志,重点看失败登录暴增"}'],
    )

    def _run(self, params: dict[str, Any]) -> Any:
        store = self.store(params)
        source = str(params.get("source") or "").strip()
        if not source:
            return _err(self.spec.name, "TOOL_INVALID_ARGUMENTS", "log_profile_set 需要 source。")
        sid = query.resolve_source_id(store, source)
        profile = store.read_profile(sid)
        for key in _PROFILE_FIELDS:
            if params.get(key) is not None:
                profile[key] = params[key]
        profile["version"] = int(profile.get("version", 0) or 0) + 1
        profile["created_by"] = "llm-onboarding"
        store.write_profile(sid, profile)
        valid_rules = len(compile_profile_rules(profile.get("rules", []) or []))
        return _ok(self.spec.name, {"source_id": sid, "version": profile["version"], "valid_rules": valid_rules, "message": f"方案已存,{valid_rules} 条规则通过安全校验生效。"})


class LogProfileGetTool(_LogOpsTool):
    spec = ToolSpec(
        name="log_profile_get",
        category="log_ops",
        effect="read_only",
        description="查某源(给 source)或所有源(不给 source)的当前监控方案 profile。备课后复核、值班时回看研判方向用。",
        use_cases=["复核刚定的方案对不对", "值班时回看某源该怎么研判"],
        avoid_when=["要改方案用 log_profile_set"],
        keywords=["方案", "查方案", "profile", "复核", "get"],
        parameters={"source": "可选。给则查单源,不给查所有源", "monitor_id": "可选,默认 default"},
        parameter_details={"source": "可选字符串。", "monitor_id": "可选。"},
        parameter_schema={"source": {"type": "string"}, "monitor_id": {"type": "string"}},
        required_parameters=[],
        examples=['{"tool": "log_profile_get"}'],
    )

    def _run(self, params: dict[str, Any]) -> Any:
        store = self.store(params)
        source = str(params.get("source") or "").strip()
        if source:
            sid = query.resolve_source_id(store, source)
            return _ok(self.spec.name, {"source_id": sid, "profile": store.read_profile(sid)})
        return _ok(self.spec.name, {"profiles": store.all_profiles()})


_ALERT_RE = re.compile(r"ALERT-\d+")


def _recent_alert_ids(store: Any) -> set[str]:
    """全部候选的 alert_id 集(evidence 真实性校验:evidence 引用的 ALERT 编号该是真实候选的)。
    必须查全量——只查最近窗口会把模型引用的早先真 ALERT 误判成编造(实测长跑候选涨到几万后 warning 误判爆炸)。"""
    total = store.count_candidates()
    return {aid for cand in store.read_candidates(offset=0, limit=total) if (aid := cand.get("alert_id"))}


def _verify_evidence(store: Any, level: str, claim_text: str, evidence: list) -> tuple[str, list[str]]:
    """证据强绑定校验(借鉴 agent_claw,对抗 detail 编造日志没有的情节):①结论里的攻击者 IP 必须在 evidence 有
    支撑 ②P0/P1 高危结论必须有 evidence ③evidence 引用的 ALERT 编号必须是真实候选的。返回 (status, issues);
    warning 不阻断(避免误伤),但回灌提示让模型补强证据或下调结论。"""
    issues: list[str] = []
    ev_text = " ".join(str(e) for e in evidence)
    unsupported = [ioc for ioc in novelty.extract_attacker_iocs(claim_text) if ioc not in ev_text]
    if unsupported:
        issues.append(f"结论里的攻击者 {unsupported[:5]} 在 evidence 找不到支撑(别下没有证据的判断)")
    if level in ("P0", "P1") and not evidence:
        issues.append(f"{level} 高危结论必须附真实日志行作 evidence")
    cand_ids = _recent_alert_ids(store) if evidence else set()
    fabricated = [e for e in evidence if (a := _ALERT_RE.findall(str(e))) and not any(x in cand_ids for x in a)]
    if fabricated:
        issues.append(f"{len(fabricated)} 条 evidence 引用的 ALERT 编号不在真实候选里(疑似编造)")
    return ("warning", issues) if issues else ("verified", [])


class LogReportTool(_LogOpsTool):
    spec = ToolSpec(
        name="log_report",
        category="log_ops",
        effect="mutating",
        requires_idempotency=True,
        description=(
            "向用户提交一条**分级**汇报。level:P0(紧急——攻击得手/数据外泄/在线失陷)、P1(重要——确认的攻击尝试)、"
            "P2(一般——可疑/扫描/探测)、P3(噪声)。系统按汇报阈值决定是否推送用户。**不要什么都报 P0**,按证据严肃定级。"
            "**evidence 必须放真实日志行(P0/P1 尤其必填):系统会校验你结论里的攻击者 IP 是否在 evidence 有支撑、"
            "evidence 引用的 ALERT 编号是否真实——别报日志里没有的情节(脑补'内网N台失陷/全网扩散'会被证据校验打回)。**"
        ),
        use_cases=["研判确认一个真实威胁,按严重度分级汇报给用户", "把一组关联事件作为一条 P0/P1 上报"],
        avoid_when=["还没研判清楚/只是猜测时别报", "低价值噪声别硬报高级别凑数"],
        keywords=["汇报", "上报", "report", "分级", "P0", "告警", "通知", "级别", "严重度"],
        parameters={
            "level": "P0/P1/P2/P3",
            "title": "一句话标题",
            "detail": "详情/研判结论",
            "evidence": "证据数组(真实日志行/IOC)",
            "sources": "涉及的源数组",
            "monitor_id": "可选,默认 default",
        },
        parameter_details={"level": "必填,P0-P3。", "title": "必填。", "evidence": "可选数组,放真实证据行。"},
        parameter_schema={
            "level": {"type": "string", "enum": list(_LEVELS)},
            "title": {"type": "string"},
            "detail": {"type": "string"},
            "evidence": {"type": "array", "items": {"type": "string"}},
            "sources": {"type": "array", "items": {"type": "string"}},
            "monitor_id": {"type": "string"},
        },
        required_parameters=["level", "title"],
        examples=['{"tool": "log_report", "level": "P0", "title": "DB主机数据外泄2.1GB到203.0.113.4", "detail": "确认外泄", "evidence": ["2026... ALERT-xxx Mass exfiltration 2.1GB POST to 203.0.113.4"], "sources": ["api-9004"]}'],
    )

    def _run(self, params: dict[str, Any]) -> Any:
        store = self.store(params)
        level = str(params.get("level") or "").strip().upper()
        title = str(params.get("title") or "").strip()
        if level not in _LEVELS:
            return _err(self.spec.name, "TOOL_INVALID_ARGUMENTS", "log_report 的 level 必须是 P0/P1/P2/P3。")
        if not title:
            return _err(self.spec.name, "TOOL_INVALID_ARGUMENTS", "log_report 需要 title。")
        floor = str(store.read_config().get("report_floor") or "P1")
        pushed = level_rank(level) >= level_rank(floor)
        detail = str(params.get("detail") or "")
        evidence = params.get("evidence") or []
        ev_status, ev_issues = _verify_evidence(store, level, f"{title} {detail}", evidence)
        store.append_report({
            "level": level,
            "title": title,
            "detail": detail,
            "evidence": evidence,
            "sources": params.get("sources") or [],
            "pushed": pushed,
            "evidence_status": ev_status,
            "evidence_issues": ev_issues,
        })
        msg = f"已推送用户(level {level} ≥ 阈值 {floor})。" if pushed else f"已记入审计但未推送(level {level} < 阈值 {floor})。"
        if ev_issues:
            msg += " ⚠️ 证据校验未通过:" + ";".join(ev_issues) + "。报告已记但请补强 evidence 或下调结论,别报没有证据支撑的事。"
        return _ok(self.spec.name, {"recorded": True, "level": level, "pushed": pushed, "report_floor": floor, "evidence_status": ev_status, "evidence_issues": ev_issues, "message": msg})


class LogCrossQueryTool(_LogOpsTool):
    spec = ToolSpec(
        name="log_cross_query",
        category="log_ops",
        effect="read_only",
        description=(
            "跨多个源联合查询同一 pattern(IP/IOC/关键词),看它在哪几个源出现过、各几次,拼接跨源事件链。"
            "发现某源异常时用它关联其他源,判断是不是协同攻击/横向移动。sources 给源列表或 ['*'] 查全部。"
        ),
        use_cases=["某 IP 在一个 API 暴力破解,查它在其他 API/源还干了啥,拼完整攻击链", "一个 IOC 跨源关联分析"],
        avoid_when=["只查单源用 log_source_query", "拉候选用 log_alert_poll"],
        keywords=["联合", "跨源", "关联", "cross", "事件链", "IOC", "协同", "横向", "联合分析"],
        parameters={
            "sources": "源列表(source_id/locator 数组)或 ['*'] 查全部",
            "pattern": "匹配模式(正则,如某 IP)",
            "time_range": "可选 [start_iso, end_iso]",
            "limit": "可选,每源最多返回多少行,默认 50,上限 500",
            "monitor_id": "可选,默认 default",
        },
        parameter_details={"sources": "必填数组或 ['*']。", "pattern": "必填正则。", "time_range": "可选 2 元素数组。"},
        parameter_schema={
            "sources": {"type": "array", "items": {"type": "string"}},
            "pattern": {"type": "string"},
            "time_range": {"type": "array", "items": {"type": "string"}},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            "monitor_id": {"type": "string"},
        },
        required_parameters=["sources", "pattern"],
        examples=['{"tool": "log_cross_query", "sources": ["*"], "pattern": "198\\\\.51\\\\.100\\\\.7"}'],
    )

    def _run(self, params: dict[str, Any]) -> Any:
        store = self.store(params)
        sources = _coerce_sources(params.get("sources"))
        pattern = str(params.get("pattern") or "")
        if not sources or not pattern:
            return _err(self.spec.name, "TOOL_INVALID_ARGUMENTS", "log_cross_query 需要 sources(列表或 ['*'])和 pattern。")
        request = query.QueryRequest(
            source="",
            pattern=pattern,
            time_range=_coerce_time_range(params.get("time_range")),
            limit=_coerce_int(params.get("limit"), default=50, lo=1, hi=500),
        )
        result = query.query_multi(store, sources, request)
        if not result.get("ok", False):
            return _err(self.spec.name, "TOOL_INVALID_ARGUMENTS", str(result.get("message") or "联合查询失败"), extra=result)
        return _ok(self.spec.name, result)


def adaptive_tools(workspace_root: Path) -> list[BaseTool]:
    """块C/E/F 的 5 个自适应层工具实例。"""
    return [
        LogSourceSampleTool(workspace_root),
        LogProfileSetTool(workspace_root),
        LogProfileGetTool(workspace_root),
        LogReportTool(workspace_root),
        LogCrossQueryTool(workspace_root),
    ]


ADAPTIVE_TOOL_NAMES = (
    "log_source_sample",
    "log_profile_set",
    "log_profile_get",
    "log_report",
    "log_cross_query",
)


__all__ = ["ADAPTIVE_TOOL_NAMES", "adaptive_tools"]
