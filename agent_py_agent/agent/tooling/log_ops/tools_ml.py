
from __future__ import annotations

"""ML 引擎 LLM 工具 —— log_ml_analyze(降维+三路评级)+ log_ml_label(outcome 标注回流驯化)。

- log_ml_analyze:候选喂 ml_engine 漏斗(指纹簇+实体簇 → 三路评级),出带解释的异常评级+协调计划;
  评级用**按私有标注自适应调过的融合权重**打分(会学)。
- log_ml_label:给簇打 outcome 标注(success/attempt/failure/other),写私有标注库(owner 隔离),回流让
  下次评级的融合权重自适应——co-teaming 闭环的 M1 落地。

复用 tools.py 的 _LogOpsTool 基类(统一精确错误码)。
"""

import time
from pathlib import Path
from typing import Any

from ..models import BaseTool, ToolSpec
from .tools import _LogOpsTool, _coerce_int, _err, _ok
from ...ml_engine import detection, engine, experience, feedback, rule_feedback, rule_store
from ...ml_engine.models import AnomalyBand, OutcomeLabel, SignalOutcome
from ...ml_engine.reducer import reduce_by_entity, reduce_candidates

_OUTCOME_VALUES = {"success", "attempt", "failure", "other"}
_BAND_VALUES = {"critical", "high", "medium", "low"}


class LogMlAnalyzeTool(_LogOpsTool):
    spec = ToolSpec(
        name="log_ml_analyze",
        category="log_ops",
        effect="read_only",
        description=(
            "对 log_ops 候选队列跑 ML 引擎漏斗(指纹簇+实体簇降维 → 三路融合评级 → 协调派工计划):把海量候选"
            "压成信号簇,按 severity/count/突发/统计异常/**实体扇出**三路评分分级(critical/high/medium/low),"
            "给出带**解释**(narrative/uncertainty/三路分解)的异常评级 + **该派几个子代理研判**。评分用按你的"
            "outcome 标注**自适应调过的融合权重**(见 log_ml_label)。按分降序、截断到 limit。只读,不改候选/存档。"
        ),
        use_cases=[
            "候选积压时先跑 ML 分诊,按评级优先研判 critical/high,别逐条看",
            "多源大量候选要决定派几个子代理、派给谁时,看 coordination_plan",
            "疑似'一个IP扫多个目标'时看实体簇的 fan_out 扇出",
        ],
        avoid_when=[
            "只拉少量候选逐条研判用 log_alert_poll",
            "查单源真实样本用 log_source_sample",
        ],
        keywords=["ML", "评级", "分诊", "降维", "聚类", "triage", "ml_analyze", "异常评分", "派工", "漏斗", "扇出"],
        parameters={
            "limit": "最多返回多少条评级,默认 200,上限 1000",
            "max_candidates": "最多读多少候选参与分析,默认 50000",
            "monitor_id": "可选,默认 default",
        },
        parameter_details={
            "limit": "可选整数,默认 200。",
            "max_candidates": "可选整数,默认 50000(防一次读爆)。",
            "monitor_id": "可选。",
        },
        parameter_schema={
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            "max_candidates": {"type": "integer", "minimum": 1, "maximum": 1000000},
            "monitor_id": {"type": "string"},
        },
        required_parameters=[],
        examples=['{"tool": "log_ml_analyze", "limit": 50}'],
    )

    def _run(self, params: dict[str, Any]) -> Any:
        store = self.store(params)
        limit = _coerce_int(params.get("limit"), default=200, lo=1, hi=1000)
        max_cand = _coerce_int(params.get("max_candidates"), default=50000, lo=1, hi=1000000)
        candidates = store.read_candidates(offset=0, limit=max_cand)
        clusters = reduce_candidates(candidates) + reduce_by_entity(candidates)
        weights = feedback.learn_weights(engine.DEFAULT_FUSION_WEIGHTS, feedback.read_labels(store.root))
        result = engine.analyze(clusters, weights=weights, limit=limit)
        payload = result.to_dict()
        payload["ok"] = True
        payload["read_candidates"] = len(candidates)
        payload["fusion_weights"] = weights
        return _ok(self.spec.name, payload)


def _build_label(cluster_id: str, outcome: str, params: dict[str, Any]) -> OutcomeLabel:
    """组装 OutcomeLabel(corrected_band 可选,非法 band 忽略)。"""
    band_raw = str(params.get("corrected_band") or "").strip().lower()
    return OutcomeLabel(
        cluster_id=cluster_id,
        outcome=SignalOutcome(outcome),
        corrected_band=AnomalyBand(band_raw) if band_raw in _BAND_VALUES else None,
        rationale=str(params.get("rationale") or ""),
        labeled_by=str(params.get("labeled_by") or "agent"),
        labeled_at=time.time(),
    )


class LogMlLabelTool(_LogOpsTool):
    spec = ToolSpec(
        name="log_ml_label",
        category="log_ops",
        effect="mutating",
        requires_idempotency=True,
        description=(
            "给一个信号簇打 outcome 标注(success 已得手 / attempt 企图 / failure 被挡 / other),回流驯化 ML "
            "引擎:下次 log_ml_analyze 的三路融合权重据标注自适应(真威胁多→更敏感抓行为异常;误报多→更依赖规则"
            "严重度)。可选 corrected_band 覆盖 ML 评级、rationale 写明理由。标注**私有(owner 隔离),不跨用户**。"
        ),
        use_cases=["研判完一个簇,把结论(成功/企图/误报)回流让引擎学", "ML 评级偏了,用 corrected_band 纠正并留痕"],
        avoid_when=["还没研判清楚别乱标(标错会带歪引擎)", "查评级用 log_ml_analyze"],
        keywords=["标注", "label", "outcome", "回流", "驯化", "纠正", "反馈", "学习", "成功", "企图"],
        parameters={
            "cluster_id": "要标注的簇 id(来自 log_ml_analyze 的 assessments)",
            "outcome": "success/attempt/failure/other",
            "corrected_band": "可选,覆盖评级 critical/high/medium/low",
            "rationale": "可选,标注理由",
            "monitor_id": "可选,默认 default",
        },
        parameter_details={"cluster_id": "必填。", "outcome": "必填,四选一。", "corrected_band": "可选。"},
        parameter_schema={
            "cluster_id": {"type": "string"},
            "outcome": {"type": "string", "enum": ["success", "attempt", "failure", "other"]},
            "corrected_band": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
            "rationale": {"type": "string"},
            "monitor_id": {"type": "string"},
        },
        required_parameters=["cluster_id", "outcome"],
        examples=['{"tool": "log_ml_label", "cluster_id": "api1:sql_injection", "outcome": "attempt", "rationale": "确认SQL注入企图,被WAF挡"}'],
    )

    def _run(self, params: dict[str, Any]) -> Any:
        cluster_id = str(params.get("cluster_id") or "").strip()
        outcome = str(params.get("outcome") or "").strip().lower()
        if not cluster_id or outcome not in _OUTCOME_VALUES:
            return _err(self.spec.name, "TOOL_INVALID_ARGUMENTS", "log_ml_label 需要 cluster_id 和合法 outcome(success/attempt/failure/other)。")
        store = self.store(params)
        store.ensure_dirs()
        feedback.append_label(store.root, _build_label(cluster_id, outcome, params))
        labels = feedback.read_labels(store.root)
        weights = feedback.learn_weights(engine.DEFAULT_FUSION_WEIGHTS, labels)
        return _ok(self.spec.name, {
            "ok": True, "cluster_id": cluster_id, "outcome": outcome,
            "label_count": len(labels), "adjusted_weights": weights,
            "message": f"已标注,私有标注库现 {len(labels)} 条;下次评级融合权重已自适应。",
        })


class LogRuleAuthorTool(_LogOpsTool):
    spec = ToolSpec(
        name="log_rule_author",
        category="log_ops",
        effect="mutating",
        requires_idempotency=True,
        description=(
            "现编一条**声明式检测规则**监控某种情况,不写代码:group_by(按字段分组)× aggregate(count/distinct/"
            "sum/rate)× window_seconds × threshold × 判据[绝对阈值 | baseline_deviation 偏离自己基线倍数]。"
            "几十上百种都能表达——横向扫描={group_by:[ip],aggregate:distinct,agg_field:dst,threshold:50};"
            "数据外泄={group_by:[ip],aggregate:sum,agg_field:bytes,window_seconds:300,threshold:1000000000};"
            "暴力破解={group_by:[ip],match_any:[auth_failure_burst],aggregate:count,threshold:20};"
            "**突增类**用 baseline_deviation:true(threshold 变'偏离自己基线的倍数',不同源各按自己基线)——"
            "流量突增={group_by:[ip],aggregate:count,baseline_deviation:true,threshold:3};用户数据突增="
            "{group_by:[user],aggregate:sum,agg_field:bytes,baseline_deviation:true,threshold:5}。默认先回测再部署,规则私有(owner 隔离)。"
        ),
        use_cases=["预设规则盖不住的新情况,agent 现编一条检测规则部署监控", "调某检测的分组/阈值/时窗"],
        avoid_when=["查已有规则命中用 log_rule_eval", "明确威胁特征(注入/反弹shell)用 log_profile_set 正则规则"],
        keywords=["检测规则", "现编", "声明式", "rule_author", "监控", "扇出", "外泄", "暴力破解", "聚合", "阈值"],
        parameters={
            "rule": "规则声明对象:{name, group_by:[字段], aggregate:count/distinct/sum/rate, agg_field, window_seconds, threshold, match_any:[规则名], severity, baseline_deviation:bool(true=threshold是偏离自己基线的倍数,用于突增类)}",
            "backtest": "可选,默认 true:先拿历史候选回测估命中量",
            "monitor_id": "可选,默认 default",
        },
        parameter_details={"rule": "必填对象,见 description 示例。", "backtest": "可选布尔,默认 true。", "monitor_id": "可选。"},
        parameter_schema={"rule": {"type": "object"}, "backtest": {"type": "boolean"}, "monitor_id": {"type": "string"}},
        required_parameters=["rule"],
        examples=['{"tool": "log_rule_author", "rule": {"name":"横向扫描","group_by":["ip"],"aggregate":"distinct","agg_field":"dst","window_seconds":60,"threshold":50,"severity":"high"}}'],
    )

    def _run(self, params: dict[str, Any]) -> Any:
        rule_raw = params.get("rule")
        if not isinstance(rule_raw, dict):
            return _err(self.spec.name, "TOOL_INVALID_ARGUMENTS", "log_rule_author 需要 rule 对象。")
        rule = detection.parse_rule(rule_raw)
        if isinstance(rule, str):
            return _err(self.spec.name, "TOOL_INVALID_ARGUMENTS", f"规则非法:{rule}")
        store = self.store(params)
        store.ensure_dirs()
        payload: dict[str, Any] = {"ok": True, "rule_id": rule.rule_id, "name": rule.name}
        if params.get("backtest", True):
            bt = detection.backtest_rule(rule, store.read_candidates(offset=0, limit=200000))
            payload["backtest"] = {"tested": bt.tested_records, "hits": bt.hit_count, "groups_hit": bt.distinct_groups_hit}
        rule_store.append_rule(store.root, rule)
        payload["message"] = f"规则「{rule.name}」已部署(私有);{('回测命中 ' + str(payload['backtest']['hits']) + ' 次') if 'backtest' in payload else '未回测'}。"
        return _ok(self.spec.name, payload)


def _eval_all_rules(rules: list[Any], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """对候选跑所有规则,扁平成命中列表。"""
    out: list[dict[str, Any]] = []
    for rule in rules:
        for hit in detection.evaluate_rule(rule, candidates):
            out.append({
                "rule_id": hit.rule_id, "rule_name": hit.rule_name, "group": hit.group_key,
                "value": hit.agg_value, "threshold": hit.threshold, "severity": hit.severity,
            })
    return out


class LogRuleEvalTool(_LogOpsTool):
    spec = ToolSpec(
        name="log_rule_eval",
        category="log_ops",
        effect="read_only",
        description=(
            "对当前候选跑所有已部署的声明式检测规则(log_rule_author 现编的),返回每条规则的命中(哪个分组/聚合值/"
            "超的阈值)。看 agent 现编的检测规则现在抓到了什么。只读。"
        ),
        use_cases=["看现编检测规则当前命中情况", "复核某规则有没有误报"],
        avoid_when=["要现编新规则用 log_rule_author", "要 ML 三路评级用 log_ml_analyze"],
        keywords=["规则命中", "rule_eval", "检测命中", "评估规则", "声明式检测"],
        parameters={"max_candidates": "最多读多少候选,默认 50000", "monitor_id": "可选,默认 default"},
        parameter_details={"max_candidates": "可选整数,默认 50000。", "monitor_id": "可选。"},
        parameter_schema={"max_candidates": {"type": "integer", "minimum": 1, "maximum": 1000000}, "monitor_id": {"type": "string"}},
        required_parameters=[],
        examples=['{"tool": "log_rule_eval"}'],
    )

    def _run(self, params: dict[str, Any]) -> Any:
        store = self.store(params)
        rules = rule_store.read_rules(store.root)
        if not rules:
            return _ok(self.spec.name, {"ok": True, "rules": 0, "hit_count": 0, "hits": [], "message": "还没现编检测规则,用 log_rule_author 加。"})
        max_cand = _coerce_int(params.get("max_candidates"), default=50000, lo=1, hi=1000000)
        hits = _eval_all_rules(rules, store.read_candidates(offset=0, limit=max_cand))
        return _ok(self.spec.name, {"ok": True, "rules": len(rules), "hit_count": len(hits), "hits": hits[:100]})


_EXPERIENCE_SUBDIR = "ml_experience"


class LogExperienceExportTool(_LogOpsTool):
    spec = ToolSpec(
        name="log_experience_export",
        category="log_ops",
        effect="mutating",
        requires_idempotency=True,
        description=(
            "把当前现编的检测规则 + 调好的融合权重 + 已知威胁指纹**脱敏沉淀**成领域经验包,供新任务/新用户"
            "冷启动复用,不重训。脱敏=只导出通用聚合声明/权重/指纹(不含原始日志/具体 IP,天然不可追溯)。"
            "默认私有(同用户跨时间/跨子代理复用,无隐私问题);跨用户共享需把经验库配成共享路径 + 授权(opt-in)。"
        ),
        use_cases=["一个领域(如 web_api_logs)研判积累了规则和经验,沉淀成包给后续任务/同类用户复用"],
        avoid_when=["要加载已有经验用 log_experience_load", "还没现编任何规则时沉淀意义不大"],
        keywords=["经验沉淀", "experience", "领域包", "复用", "不重训", "导出", "脱敏"],
        parameters={"domain": "逻辑领域名(如 web_api_logs/iot_device_logs),同类任务用同名才能复用", "monitor_id": "可选,默认 default"},
        parameter_details={"domain": "必填,逻辑领域名。", "monitor_id": "可选。"},
        parameter_schema={"domain": {"type": "string"}, "monitor_id": {"type": "string"}},
        required_parameters=["domain"],
        examples=['{"tool": "log_experience_export", "domain": "web_api_logs"}'],
    )

    def _run(self, params: dict[str, Any]) -> Any:
        domain = str(params.get("domain") or "").strip()
        if not domain:
            return _err(self.spec.name, "TOOL_INVALID_ARGUMENTS", "log_experience_export 需要 domain(逻辑领域名)。")
        store = self.store(params)
        rules = rule_store.read_rules(store.root)
        labels = feedback.read_labels(store.root)
        weights = feedback.learn_weights(engine.DEFAULT_FUSION_WEIGHTS, labels)
        pack = experience.export_pack(domain, rules, weights, [lb.cluster_id for lb in labels])
        experience.save_pack(self.workspace_root / _EXPERIENCE_SUBDIR, pack)
        return _ok(self.spec.name, {
            "ok": True, "domain": domain, "exported_rules": len(rules),
            "fingerprints": len(pack.known_fingerprints), "fusion_weights": weights,
            "message": f"领域「{domain}」经验已脱敏沉淀:{len(rules)} 规则 + 权重 + {len(pack.known_fingerprints)} 指纹。",
        })


def _apply_pack_rules(store: Any, rules: list[Any]) -> int:
    """把领域包规则应用到私有规则库,按 rule_id 去重(已有的不重复)。返回新增数。"""
    existing = {r.rule_id for r in rule_store.read_rules(store.root)}
    applied = 0
    for rule in rules:
        if rule.rule_id not in existing:
            rule_store.append_rule(store.root, rule)
            applied += 1
    return applied


class LogExperienceLoadTool(_LogOpsTool):
    spec = ToolSpec(
        name="log_experience_load",
        category="log_ops",
        effect="mutating",
        requires_idempotency=True,
        description=(
            "冷启动按领域加载经验包(出厂内置 + 前人沉淀),把脱敏检测规则应用到私有规则库 + 返回调好的融合权重,"
            "新任务**不从零**。同类领域(同 domain 名)的前人经验直接迁移过来,边干边继续学。"
        ),
        use_cases=["接手一个新监控任务,先 load 该领域经验包,带着前人规则+权重起步,不重训"],
        avoid_when=["要沉淀当前经验用 log_experience_export"],
        keywords=["经验加载", "冷启动", "experience_load", "复用", "迁移", "不重训", "领域包"],
        parameters={"domain": "逻辑领域名(与 export 时一致才能复用)", "monitor_id": "可选,默认 default"},
        parameter_details={"domain": "必填,逻辑领域名。", "monitor_id": "可选。"},
        parameter_schema={"domain": {"type": "string"}, "monitor_id": {"type": "string"}},
        required_parameters=["domain"],
        examples=['{"tool": "log_experience_load", "domain": "web_api_logs"}'],
    )

    def _run(self, params: dict[str, Any]) -> Any:
        domain = str(params.get("domain") or "").strip()
        if not domain:
            return _err(self.spec.name, "TOOL_INVALID_ARGUMENTS", "log_experience_load 需要 domain。")
        pack = experience.load_pack(self.workspace_root / _EXPERIENCE_SUBDIR, domain)
        if pack is None:
            return _ok(self.spec.name, {"ok": True, "loaded": False, "domain": domain, "message": f"领域「{domain}」暂无经验包,从零开始(边干边沉淀)。"})
        store = self.store(params)
        store.ensure_dirs()
        applied = _apply_pack_rules(store, experience.rules_from_pack(pack))
        return _ok(self.spec.name, {
            "ok": True, "loaded": True, "domain": domain, "version": pack.version,
            "applied_rules": applied, "fusion_weights": pack.fusion_weights,
            "known_fingerprints": len(pack.known_fingerprints),
            "message": f"冷启动加载领域「{domain}」经验(v{pack.version}):{applied} 条规则就绪,不从零。",
        })


def _eff_dict(eff: Any) -> dict[str, Any]:
    return {"rule_id": eff.rule_id, "total_feedback": eff.total_feedback, "false_positives": eff.false_positives,
            "false_positive_rate": eff.false_positive_rate, "suggested_action": eff.suggested_action}


class LogRuleFeedbackTool(_LogOpsTool):
    spec = ToolSpec(
        name="log_rule_feedback",
        category="log_ops",
        effect="mutating",
        requires_idempotency=True,
        description=(
            "标注某检测规则的一次命中是误报还是真实威胁,回流让规则自适应:误报多的规则会被建议升阈值"
            "(log_rule_tune)。co-teaming 闭环延伸到规则层——agent 现编的检测规则越用越准。"
        ),
        use_cases=["研判完一条规则命中,标它误报/真实让规则学", "某规则老误报,标注后用 log_rule_tune 自调"],
        avoid_when=["标 ML 评级 outcome 用 log_ml_label", "查规则命中用 log_rule_eval"],
        keywords=["规则反馈", "误报", "rule_feedback", "标注", "自适应", "false_positive"],
        parameters={
            "rule_id": "规则 id(来自 log_rule_eval 的命中)",
            "group_key": "命中的分组(如 ip=1.2.3.4),可选",
            "false_positive": "true=误报,false=真实威胁",
            "monitor_id": "可选,默认 default",
        },
        parameter_details={"rule_id": "必填。", "false_positive": "必填布尔。", "group_key": "可选。", "monitor_id": "可选。"},
        parameter_schema={
            "rule_id": {"type": "string"}, "group_key": {"type": "string"},
            "false_positive": {"type": "boolean"}, "monitor_id": {"type": "string"},
        },
        required_parameters=["rule_id", "false_positive"],
        examples=['{"tool": "log_rule_feedback", "rule_id": "rule-123", "group_key": "ip=10.0.0.1", "false_positive": true}'],
    )

    def _run(self, params: dict[str, Any]) -> Any:
        rule_id = str(params.get("rule_id") or "").strip()
        if not rule_id:
            return _err(self.spec.name, "TOOL_INVALID_ARGUMENTS", "log_rule_feedback 需要 rule_id。")
        store = self.store(params)
        store.ensure_dirs()
        fb = rule_feedback.RuleFeedback(rule_id, str(params.get("group_key") or ""), bool(params.get("false_positive")), time.time())
        rule_feedback.append_feedback(store.root, fb)
        rules = {r.rule_id: r for r in rule_store.read_rules(store.root)}
        eff = rule_feedback.assess_rule(rules[rule_id], rule_feedback.read_feedbacks(store.root)) if rule_id in rules else None
        return _ok(self.spec.name, {"ok": True, "rule_id": rule_id, "false_positive": fb.false_positive,
                                    "effectiveness": _eff_dict(eff) if eff else None})


def _tune_rules(store: Any, rules: list[Any], feedbacks: list[Any], auto: bool) -> list[dict[str, Any]]:
    """评估每规则误报率,auto 时对 raise_threshold 的规则升阈值产新版本(append,read_rules 取最新)。"""
    out: list[dict[str, Any]] = []
    for rule in rules:
        eff = rule_feedback.assess_rule(rule, feedbacks)
        applied = False
        if auto and eff.suggested_action == "raise_threshold":
            rule_store.append_rule(store.root, rule_feedback.tune_rule(rule, eff))
            applied = True
        out.append({"rule_id": rule.rule_id, "version": rule.version, "fp_rate": eff.false_positive_rate,
                    "action": eff.suggested_action, "auto_applied": applied})
    return out


class LogRuleTuneTool(_LogOpsTool):
    spec = ToolSpec(
        name="log_rule_tune",
        category="log_ops",
        effect="mutating",
        requires_idempotency=True,
        description=(
            "看所有现编检测规则的误报率 + 自适应建议(误报率高=阈值太松→建议升阈值)。auto_apply=true 时自动对"
            "误报率高的规则升阈值×1.5 产新版本(version+1),让规则越用越准。误报标注来自 log_rule_feedback。"
        ),
        use_cases=["攒了一批规则误报标注后,看哪些规则该调 + 一键自调阈值", "复核规则效果(误报率)"],
        avoid_when=["还没标注误报(log_rule_feedback)时调了没数据", "要现编新规则用 log_rule_author"],
        keywords=["规则调优", "rule_tune", "误报率", "自调阈值", "版本化", "自适应"],
        parameters={"auto_apply": "可选,默认 false:true=自动对误报率高的规则升阈值产新版本", "monitor_id": "可选,默认 default"},
        parameter_details={"auto_apply": "可选布尔,默认 false(只看建议不改)。", "monitor_id": "可选。"},
        parameter_schema={"auto_apply": {"type": "boolean"}, "monitor_id": {"type": "string"}},
        required_parameters=[],
        examples=['{"tool": "log_rule_tune", "auto_apply": true}'],
    )

    def _run(self, params: dict[str, Any]) -> Any:
        store = self.store(params)
        rules = rule_store.read_rules(store.root)
        if not rules:
            return _ok(self.spec.name, {"ok": True, "assessed": 0, "tunings": [], "message": "还没现编检测规则。"})
        tunings = _tune_rules(store, rules, rule_feedback.read_feedbacks(store.root), bool(params.get("auto_apply")))
        applied = sum(1 for t in tunings if t["auto_applied"])
        return _ok(self.spec.name, {"ok": True, "assessed": len(tunings), "auto_applied": applied, "tunings": tunings})


def ml_tools(workspace_root: Path) -> list[BaseTool]:
    """ML 引擎工具实例(评级 + 标注 + 现编检测 + 规则评估 + 经验沉淀/加载 + 规则反馈/调优)。"""
    return [
        LogMlAnalyzeTool(workspace_root),
        LogMlLabelTool(workspace_root),
        LogRuleAuthorTool(workspace_root),
        LogRuleEvalTool(workspace_root),
        LogExperienceExportTool(workspace_root),
        LogExperienceLoadTool(workspace_root),
        LogRuleFeedbackTool(workspace_root),
        LogRuleTuneTool(workspace_root),
    ]


ML_TOOL_NAMES = (
    "log_ml_analyze", "log_ml_label", "log_rule_author", "log_rule_eval",
    "log_experience_export", "log_experience_load", "log_rule_feedback", "log_rule_tune",
)


__all__ = ["ML_TOOL_NAMES", "ml_tools"]
