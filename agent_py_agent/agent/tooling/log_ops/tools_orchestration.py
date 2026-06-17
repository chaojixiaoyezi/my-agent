
from __future__ import annotations

"""log_ops 编排层 LLM 工具 —— 多层代理(主→子→孙)值守的职责台账接口。

- log_assign:主代理派子代理/孙代理盯某些源,登记进职责台账(谁/盯哪些/上级/owner)。
- log_heartbeat:被派的代理周期写心跳+进度(我还活着、研判到哪、发现多少)。
- log_duty_roster:查台账全局视图——每源谁在盯、哪个代理心跳停(挂)、哪些源没人认领(漏)。

审计性 + 续接的闭环:派活→心跳→看门狗据 roster 的 stalled/gaps 重派接管。复用 DutyRegistry(纯落盘)。
"""

import re
import time
from pathlib import Path
from typing import Any

from ..models import BaseTool, ToolSpec
from . import query, wake, watchdog
from .duty_registry import Assignment, DutyRegistry
from .query import resolve_source_id
from .tools import _coerce_sources, _err, _LogOpsTool, _ok


def _registry(tool: _LogOpsTool, params: dict[str, Any]) -> DutyRegistry:
    return DutyRegistry(tool.store(params).root)


def _correlate_lead(store: Any, ioc: str, min_sources: int) -> dict[str, Any] | None:
    """对一个 IOC 跨所有源查,跨 ≥min_sources 源则返回攻击链 dict,否则 None。"""
    result = query.query_multi(store, ["*"], query.QueryRequest(source="", pattern=re.escape(ioc), limit=50))
    if int(result.get("sources_with_hits", 0)) >= min_sources:
        return {
            "ioc": ioc,
            "sources_with_hits": result["sources_with_hits"],
            "total_matched": result["total_matched"],
            "per_source": result["per_source"],
        }
    return None


def _quota_denied(name: str, registry: DutyRegistry, params: dict[str, Any]) -> Any | None:
    """新派活时查资源配额,超额返回 _ok(assigned False) 拒派;更新已有/未设配额返回 None 放行。"""
    agent_id = str(params.get("agent_id") or "").strip()
    owner = str(params.get("owner") or "local").strip() or "local"
    max_per_owner = max(0, int(params.get("max_per_owner") or 0))
    max_global = max(0, int(params.get("max_global") or 0))
    if registry.get(agent_id) is not None or not (max_per_owner or max_global):
        return None  # 重派已有(幂等)不占新额度;没设配额=不限
    q = registry.quota_check(owner, max_per_owner=max_per_owner, max_global=max_global)
    if q["allowed"]:
        return None
    return _ok(name, {
        "assigned": False,
        "quota_exceeded": True,
        "reason": q["reason"],
        "global_active": q["global_active"],
        "owner_active": q["owner_active"],
        "message": f"资源配额超额({q['reason']}),未派。全局活跃 {q['global_active']}/{max_global or '∞'}、owner {owner} 活跃 {q['owner_active']}/{max_per_owner or '∞'}。等闲代理 done 或调高配额再派。",
    })


class LogAssignTool(_LogOpsTool):
    spec = ToolSpec(
        name="log_assign",
        category="log_ops",
        effect="mutating",
        requires_idempotency=True,
        description=(
            "派一个子代理/孙代理去盯某些源,登记进职责台账。台账是多层代理值守的审计+续接地基:"
            "记下谁盯哪些源、上级是谁、属于哪个用户(owner)。同一 agent_id 重复 assign 是更新(幂等)。"
        ),
        use_cases=["主代理把某几个源的监控派给一个子代理", "子代理再把细分面派给孙代理"],
        avoid_when=["代理写自己的进度用 log_heartbeat", "查全局谁盯啥用 log_duty_roster"],
        keywords=["派活", "分派", "assign", "职责", "台账", "子代理", "孙代理", "盯", "duty"],
        parameters={
            "agent_id": "被派代理的标识(同时作台账条目 id;同 id 重派=更新)",
            "targets": "盯哪些源(source_id 或 locator 数组)",
            "role": "可选。child(子代理,默认) | grandchild(孙代理)",
            "parent_agent_id": "可选。上级代理标识(主代理/子代理)",
            "owner": "可选。所属用户(IM 私聊身份),默认 local",
            "max_per_owner": "可选。该 owner 活跃职责上限(0=不限),超额不派(多主代理别挤垮机器)",
            "max_global": "可选。全局活跃职责上限(0=不限),跨所有 owner",
            "monitor_id": "可选,默认 default",
        },
        parameter_details={"agent_id": "必填。", "targets": "必填数组。", "role": "可选。", "owner": "可选,多主代理隔离用。", "max_per_owner": "可选资源配额。"},
        parameter_schema={
            "agent_id": {"type": "string"},
            "targets": {"type": "array", "items": {"type": "string"}},
            "role": {"type": "string", "enum": ["child", "grandchild"]},
            "parent_agent_id": {"type": "string"},
            "owner": {"type": "string"},
            "max_per_owner": {"type": "integer", "minimum": 0},
            "max_global": {"type": "integer", "minimum": 0},
            "monitor_id": {"type": "string"},
        },
        required_parameters=["agent_id", "targets"],
        examples=['{"tool": "log_assign", "agent_id": "child-api1", "targets": ["http://127.0.0.1:9001/poll"], "role": "child", "parent_agent_id": "main"}'],
    )

    def _run(self, params: dict[str, Any]) -> Any:
        agent_id = str(params.get("agent_id") or "").strip()
        targets_raw = _coerce_sources(params.get("targets"))
        if not agent_id or not targets_raw:
            return _err(self.spec.name, "TOOL_INVALID_ARGUMENTS", "log_assign 需要 agent_id 和非空 targets。")
        store = self.store(params)
        registry = DutyRegistry(store.root)
        owner = str(params.get("owner") or "local").strip() or "local"
        denied = _quota_denied(self.spec.name, registry, params)
        if denied is not None:
            return denied  # 资源配额超额:不派,返回 assigned False 让上层排队
        targets = [resolve_source_id(store, t) for t in targets_raw]  # locator/source_id 统一成 source_id,与台账巡检一致
        role = str(params.get("role") or "child").strip() or "child"
        registry.register(Assignment(
            assignment_id=agent_id,
            agent_id=agent_id,
            targets=targets,
            role=role,
            parent_agent_id=str(params.get("parent_agent_id") or "").strip(),
            owner=owner,
        ))
        return _ok(self.spec.name, {"assigned": True, "assignment_id": agent_id, "targets": targets, "role": role, "owner": owner, "message": f"已登记:{agent_id} 盯 {len(targets)} 个源。代理请周期 log_heartbeat 写心跳。"})


class LogHeartbeatTool(_LogOpsTool):
    spec = ToolSpec(
        name="log_heartbeat",
        category="log_ops",
        effect="mutating",
        requires_idempotency=True,
        description=(
            "被派的代理周期写心跳 + 进度到职责台账(我还活着、研判到哪、发现多少告警)。看门狗据心跳判断"
            "代理是否卡死/挂了——所以值班代理要规律调用,别让心跳停。台账里没这条 assignment 则返回未登记。"
        ),
        use_cases=["子代理/孙代理每轮值班后写一次心跳+进度", "汇报研判进度(poll_cursor/发现告警数)"],
        avoid_when=["登记新职责用 log_assign", "查全局用 log_duty_roster"],
        keywords=["心跳", "heartbeat", "进度", "存活", "alive", "值班", "台账"],
        parameters={
            "assignment_id": "自己的职责条目 id(= 被 assign 时的 agent_id)",
            "progress": "可选。进度对象,如 {poll_cursor, candidates_seen, alerts_found}",
            "monitor_id": "可选,默认 default",
        },
        parameter_details={"assignment_id": "必填。", "progress": "可选对象,合并进台账。"},
        parameter_schema={
            "assignment_id": {"type": "string"},
            "progress": {"type": "object"},
            "monitor_id": {"type": "string"},
        },
        required_parameters=["assignment_id"],
        examples=['{"tool": "log_heartbeat", "assignment_id": "child-api1", "progress": {"poll_cursor": 1200, "alerts_found": 4}}'],
    )

    def _run(self, params: dict[str, Any]) -> Any:
        assignment_id = str(params.get("assignment_id") or "").strip()
        if not assignment_id:
            return _err(self.spec.name, "TOOL_INVALID_ARGUMENTS", "log_heartbeat 需要 assignment_id。")
        progress = params.get("progress") if isinstance(params.get("progress"), dict) else {}
        ok = _registry(self, params).heartbeat(assignment_id, progress)
        if not ok:
            return _err(self.spec.name, "TOOL_INVALID_ARGUMENTS", f"台账里没有 {assignment_id} 这条职责,请先 log_assign 登记。")
        return _ok(self.spec.name, {"assignment_id": assignment_id, "recorded": True, "message": "心跳已记。"})


class LogDutyRosterTool(_LogOpsTool):
    spec = ToolSpec(
        name="log_duty_roster",
        category="log_ops",
        effect="read_only",
        description=(
            "查职责台账全局视图:总数/活跃/卡住(心跳超时)的代理,每条职责的代理+心跳年龄+进度,以及"
            "没被任何代理认领的源(漏检)。主代理巡检多层代理值守、看门狗判断谁挂了/哪漏了据此。"
        ),
        use_cases=["主代理巡检:每个源都有人盯吗?有代理挂了吗?", "看门狗检测 stalled/漏检"],
        avoid_when=["派活用 log_assign", "写心跳用 log_heartbeat"],
        keywords=["台账", "巡检", "roster", "全局", "谁盯", "漏检", "卡住", "审计", "值守"],
        parameters={"owner": "可选。只看该主代理(IM 用户)的职责(多主代理隔离);空=全局", "monitor_id": "可选,默认 default"},
        parameter_details={"owner": "可选,多主代理隔离用。", "monitor_id": "可选。"},
        parameter_schema={"owner": {"type": "string"}, "monitor_id": {"type": "string"}},
        required_parameters=[],
        examples=['{"tool": "log_duty_roster"}', '{"tool": "log_duty_roster", "owner": "userA"}'],
    )

    def _run(self, params: dict[str, Any]) -> Any:
        store = self.store(params)
        source_ids = [spec.source_id for spec in store.source_specs()]
        owner = str(params.get("owner") or "").strip()
        return _ok(self.spec.name, DutyRegistry(store.root).roster(source_ids, owner=owner))


class LogWatchdogScanTool(_LogOpsTool):
    spec = ToolSpec(
        name="log_watchdog_scan",
        category="log_ops",
        effect="mutating",
        requires_idempotency=True,
        description=(
            "看门狗扫一遍职责台账:心跳停的代理(挂了)标记 stalled 并产出'重派接管(带断点 resume_from 续接)'动作,"
            "没人盯的源产出'补派'动作;不健康时自动记一条 P1 告警推送你。主代理巡检或定时调,据返回 actions 重派/"
            "补派,保证几个月值守不断档——挂了立即有人接、漏了立即补,不靠 LLM 死盯。"
        ),
        use_cases=["定时巡检多层代理值守健康度", "发现挂掉的代理并拿到重派动作(含断点续接依据)"],
        avoid_when=["只看不处理用 log_duty_roster(只读,不标记不告警)"],
        keywords=["看门狗", "watchdog", "巡检", "重派", "续接", "挂了", "漏检", "健康", "故障", "接管"],
        parameters={"owner": "可选。只扫该主代理(IM 用户)的职责(多主代理隔离:A 的看门狗只重派 A);空=全局", "monitor_id": "可选,默认 default"},
        parameter_details={"owner": "可选,多主代理隔离用。", "monitor_id": "可选。"},
        parameter_schema={"owner": {"type": "string"}, "monitor_id": {"type": "string"}},
        required_parameters=[],
        examples=['{"tool": "log_watchdog_scan"}', '{"tool": "log_watchdog_scan", "owner": "userA"}'],
    )

    def _run(self, params: dict[str, Any]) -> Any:
        store = self.store(params)
        owner = str(params.get("owner") or "").strip()
        result = watchdog.scan(store, owner=owner)
        if not result.healthy:
            scope = f"[{owner}] " if owner else ""
            store.append_report({
                "level": "P1",
                "title": f"看门狗{scope}:{len(result.stalled)} 个代理心跳停、{len(result.coverage_gaps)} 个源漏检",
                "detail": f"stalled={result.stalled} gaps={result.coverage_gaps}",
                "evidence": [],
                "sources": result.coverage_gaps,
                "pushed": True,
                "source": "watchdog",
                "owner": owner,
            })
        return _ok(self.spec.name, result.to_dict())


class LogLeadTool(_LogOpsTool):
    spec = ToolSpec(
        name="log_lead",
        category="log_ops",
        effect="mutating",
        requires_idempotency=True,
        description=(
            "子代理发现可疑 IOC(IP/域名/文件 hash 等)上报一条线索到关联池。**子代理只盯自己单源、视野单一,发现可疑"
            "只负责上报+丢进线索池,不自己跨源查**——跨源串联是主代理/关联代理的活(谁发现谁上报,串联归上层)。"
        ),
        use_cases=["子代理在自己源里发现可疑 IP/域名,上报线索等主代理跨源串联", "记录待关联的 IOC"],
        avoid_when=["跨源拼链用 log_correlate(主代理的活)", "确认威胁直接报用户用 log_report"],
        keywords=["线索", "lead", "IOC", "可疑", "上报", "关联池", "子代理"],
        parameters={"ioc": "可疑指标(IP/域名/hash)", "ioc_type": "可选 ip/domain/hash/user", "source": "可选,在哪个源发现", "note": "可选上下文", "agent_id": "可选,上报代理", "monitor_id": "可选"},
        parameter_details={"ioc": "必填。", "ioc_type": "可选。"},
        parameter_schema={"ioc": {"type": "string"}, "ioc_type": {"type": "string"}, "source": {"type": "string"}, "note": {"type": "string"}, "agent_id": {"type": "string"}, "monitor_id": {"type": "string"}},
        required_parameters=["ioc"],
        examples=['{"tool": "log_lead", "ioc": "198.51.100.7", "ioc_type": "ip", "source": "api-9005", "note": "SSH爆破来源"}'],
    )

    def _run(self, params: dict[str, Any]) -> Any:
        ioc = str(params.get("ioc") or "").strip()
        if not ioc:
            return _err(self.spec.name, "TOOL_INVALID_ARGUMENTS", "log_lead 需要 ioc。")
        self.store(params).append_lead({
            "ioc": ioc,
            "ioc_type": str(params.get("ioc_type") or ""),
            "source": str(params.get("source") or ""),
            "note": str(params.get("note") or ""),
            "agent_id": str(params.get("agent_id") or ""),
        })
        return _ok(self.spec.name, {"recorded": True, "ioc": ioc, "message": "线索已入池,主代理用 log_correlate 跨源串联。"})


class LogCorrelateTool(_LogOpsTool):
    spec = ToolSpec(
        name="log_correlate",
        category="log_ops",
        effect="read_only",
        description=(
            "主代理/关联代理读关联线索池,对每个可疑 IOC 跨所有源查一遍,拼出攻击链(同一 IOC 在哪几个源出现、各几次)。"
            "**跨源串联是主代理的活**——子代理只用 log_lead 上报线索不串联。跨 ≥min_sources(默认2)源的 IOC 即一条协同攻击链。"
        ),
        use_cases=["主代理周期把线索池里的 IOC 跨源串成攻击链", "判断某可疑 IP 是不是协同攻击(多源都有)"],
        avoid_when=["子代理上报单线索用 log_lead", "查单个已知 pattern 用 log_cross_query"],
        keywords=["关联", "串联", "correlate", "攻击链", "线索池", "跨源", "主代理"],
        parameters={"min_sources": "可选,算攻击链的最少源数,默认 2", "monitor_id": "可选"},
        parameter_details={"min_sources": "可选整数,默认 2。"},
        parameter_schema={"min_sources": {"type": "integer", "minimum": 1}, "monitor_id": {"type": "string"}},
        required_parameters=[],
        examples=['{"tool": "log_correlate"}'],
    )

    def _run(self, params: dict[str, Any]) -> Any:
        store = self.store(params)
        min_sources = max(1, int(params.get("min_sources") or 2))
        leads = store.read_leads()
        chains: list[dict[str, Any]] = []
        seen: set[str] = set()
        for lead in leads:
            ioc = str(lead.get("ioc") or "").strip()
            if not ioc or ioc in seen:
                continue
            seen.add(ioc)
            chain = _correlate_lead(store, ioc, min_sources)
            if chain is not None:
                chains.append(chain)
        return _ok(self.spec.name, {"leads_total": len(leads), "unique_iocs": len(seen), "attack_chains": chains})


class LogWakeCheckTool(_LogOpsTool):
    spec = ToolSpec(
        name="log_wake_check",
        category="log_ops",
        effect="read_only",
        description=(
            "按需唤醒门控:确定性判断现在是否该拉起 LLM 研判(不靠常驻 LLM 几月烧死)。返回 should_wake + 原因"
            "(urgent_signal 高危秒级 / batch_full 候选攒够 / idle_timeout 保底 / no_work 没事)。cron 周期调:should_wake "
            "false 就退(省烧),true 才拉短命 run 研判候选→log_report 上报→log_wake_ack 推进游标→退。无限期值守靠它省烧。"
        ),
        use_cases=["cron/gateway 周期检查是否有事要唤醒 LLM 研判", "无限期值守省烧:有事才醒,没事不烧"],
        avoid_when=["研判完推进游标用 log_wake_ack", "直接看候选/状态用 log_monitor_status"],
        keywords=["唤醒", "wake", "按需", "门控", "省烧", "cron", "研判", "调度", "常驻", "醒"],
        parameters={
            "batch_threshold": "可选,未研判候选攒够多少就唤醒,默认 20",
            "max_idle_seconds": "可选,有候选时距上次研判多久必唤醒(保底),默认 900",
            "monitor_id": "可选,默认 default",
        },
        parameter_details={"batch_threshold": "可选整数。", "max_idle_seconds": "可选秒数。"},
        parameter_schema={
            "batch_threshold": {"type": "integer", "minimum": 1},
            "max_idle_seconds": {"type": "number", "minimum": 0},
            "monitor_id": {"type": "string"},
        },
        required_parameters=[],
        examples=['{"tool": "log_wake_check"}'],
    )

    def _run(self, params: dict[str, Any]) -> Any:
        store = self.store(params)
        kw: dict[str, Any] = {}
        if params.get("batch_threshold") is not None:
            kw["batch_threshold"] = max(1, int(params["batch_threshold"]))
        if params.get("max_idle_seconds") is not None:
            kw["max_idle_seconds"] = max(0.0, float(params["max_idle_seconds"]))
        return _ok(self.spec.name, wake.evaluate_wake(store, **kw).to_dict())


class LogWakeAckTool(_LogOpsTool):
    spec = ToolSpec(
        name="log_wake_ack",
        category="log_ops",
        effect="mutating",
        requires_idempotency=True,
        description=(
            "研判 run 处理完候选后推进研判游标(跨 run 续接的关键:下次 log_wake_check 不会重复唤醒同一批)。"
            "不传 reviewed_through 则推进到当前全部候选已研判;同时清空紧急队列(已处理)、记上次研判时间,然后可优雅退出。"
        ),
        use_cases=["短命研判 run 上报完后推进游标+记时间+清紧急,优雅退出", "标记某批候选已研判"],
        avoid_when=["只查是否要唤醒用 log_wake_check", "上报告警用 log_report"],
        keywords=["唤醒", "确认", "ack", "游标", "研判", "推进", "续接", "退出", "清紧急"],
        parameters={
            "reviewed_through": "可选,已研判到第几条候选(默认=当前全部候选)",
            "reported": "可选,本次上报条数(累计进总数)",
            "clear_urgent": "可选,是否清空紧急队列,默认 true",
            "monitor_id": "可选,默认 default",
        },
        parameter_details={"reviewed_through": "可选整数。", "reported": "可选整数。", "clear_urgent": "可选布尔。"},
        parameter_schema={
            "reviewed_through": {"type": "integer", "minimum": 0},
            "reported": {"type": "integer", "minimum": 0},
            "clear_urgent": {"type": "boolean"},
            "monitor_id": {"type": "string"},
        },
        required_parameters=[],
        examples=['{"tool": "log_wake_ack", "reported": 3}'],
    )

    def _run(self, params: dict[str, Any]) -> Any:
        store = self.store(params)
        total = store.count_candidates()
        through = params.get("reviewed_through")
        cursor = max(0, min(int(through), total)) if through is not None else total
        prev = wake.read_review_state(store)
        reported = int(params.get("reported") or 0) + int(prev.get("reported") or 0)
        wake.write_review_state(store, cursor=cursor, reported=reported, at=time.time())
        cleared = wake.clear_urgent(store) if params.get("clear_urgent", True) else 0
        return _ok(self.spec.name, {"review_cursor": cursor, "reported": reported, "urgent_cleared": cleared, "total_candidates": total, "message": f"研判游标推进到 {cursor}/{total},紧急队列清 {cleared} 条,可优雅退出。"})


def orchestration_tools(workspace_root: Path) -> list[BaseTool]:
    """编排层 8 个工具实例(职责台账 3 + 看门狗 1 + 关联 2 + 按需唤醒 2)。"""
    return [
        LogAssignTool(workspace_root),
        LogHeartbeatTool(workspace_root),
        LogDutyRosterTool(workspace_root),
        LogWatchdogScanTool(workspace_root),
        LogLeadTool(workspace_root),
        LogCorrelateTool(workspace_root),
        LogWakeCheckTool(workspace_root),
        LogWakeAckTool(workspace_root),
    ]


ORCHESTRATION_TOOL_NAMES = ("log_assign", "log_heartbeat", "log_duty_roster", "log_watchdog_scan", "log_lead", "log_correlate", "log_wake_check", "log_wake_ack")

__all__ = ["ORCHESTRATION_TOOL_NAMES", "orchestration_tools"]
