# LLM: 增量结论账(收尾一公里根治,E1/E2 实锤):长跑子代理把活干完却在最终结果块上崩,
#   "结论只活在最终一次性输出里"→ 收尾崩/被取消=过程价值清零(某轮 12 个漏报里 10 个
#   来自两条被取消的路)。本工具让"确认一条结论就持久化一条"成为可能:确认即入账,
#   账在 findings.jsonl,收尾崩/重派/取消都不丢;整合/收口层从账合并,最终报告只是
#   账本的汇总视图。代码只搬运内容不定性(claim 写什么由模型定,零自然语言判断)。
# 模块用途: record_finding 工具——把一条已确认结论追加进本 run 的结论账本文件。
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

from ...common.json_io import append_jsonl_records
from ...tooling.models import BaseTool, ToolExecutionResult, ToolSpec
from ..runner.context import current_subagent_attempt_id, current_subagent_run_id

_TOOL_NAME = "record_finding"
_MAX_CLAIM_CHARS = 2000
_MAX_REFS = 20
_WATCH_ID_PATTERN = re.compile(r"^ws-[0-9a-f]{10}$")


class RecordFindingTool(BaseTool):
    def __init__(self, agent: object) -> None:
        self.agent = agent
        self.spec = build_record_finding_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        claim = str(params.get("claim") or "").strip()
        if not claim:
            return ToolExecutionResult(_TOOL_NAME, False, "缺少 claim:一句话写清你确认了什么。")
        ledger_path, ledger_scope = _findings_ledger_path(self.agent)
        if not ledger_path:
            return ToolExecutionResult(
                _TOOL_NAME, False, "当前上下文没有可用的结论账本(没有运行中的任务工作区)。"
            )
        record = _finding_record(self.agent, claim, params)
        try:
            path = Path(ledger_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            append_jsonl_records(path, [record])
        except OSError as exc:
            return ToolExecutionResult(_TOOL_NAME, False, f"结论账本写入失败: {exc}")
        payload = {
            "ok": True,
            "recorded": True,
            "finding_id": record["id"],
            "ledger_ref": ledger_path,
            "ledger_scope": ledger_scope,
            "guidance": (
                "已入账。这条结论已持久化:之后收尾崩、重派、任务被取消都不会丢,整合轮会从账合并。"
                "继续干活;最终结果块只需汇总,不必重复粘贴每条账。"
            ),
        }
        linked = _link_watch_feedback(self.agent, params)
        if linked is not None:
            payload["watch_feedback_linked"] = linked
        return ToolExecutionResult(_TOOL_NAME, True, json.dumps(payload, ensure_ascii=False, indent=2))


def _finding_record(agent: object, claim: str, params: dict[str, object]) -> dict[str, object]:
    refs_value = params.get("evidence_refs")
    refs = refs_value if isinstance(refs_value, list) else ([refs_value] if refs_value else [])
    evidence_refs = [str(item).strip() for item in refs if str(item or "").strip()][:_MAX_REFS]
    return {
        "version": 1,
        "id": f"rf-{uuid.uuid4().hex[:12]}",
        "kind": str(params.get("kind") or "finding").strip() or "finding",
        "claim": claim[:_MAX_CLAIM_CHARS],
        "evidence_refs": evidence_refs,
        "confidence": str(params.get("confidence") or "").strip(),
        "created_at": time.time(),
        "run_id": current_subagent_run_id(agent),
        "attempt_id": current_subagent_attempt_id(agent),
        "source": "record_finding_tool",
    }


# LLM: 摄取召回反馈接缝(B3):盯守候选被确认时把 (watch_id, stream_pos) 追加进该
#   watch 的反馈收件箱——引擎属主消费后将该事件的结构特征回灌预筛,自动抬同类、
#   抽检向盲区源倾斜。best-effort:参数缺/格式不符/watch 不存在都静默跳过(None),
#   绝不影响结论账主通道;返回 True/False 只作观测。
# 函数用途: 确认的盯守候选 → 反馈收件箱一行(召回自愈的对账通路)。
def _link_watch_feedback(agent: object, params: dict[str, object]) -> bool | None:
    watch_id = str(params.get("watch_id") or "").strip()
    raw_pos = params.get("stream_pos")
    if not watch_id or raw_pos is None:
        return None
    if not _WATCH_ID_PATTERN.fullmatch(watch_id):
        return False
    try:
        stream_pos = int(str(raw_pos).strip())
    except (TypeError, ValueError):
        return False
    owner_home = str(getattr(getattr(agent, "home_paths", None), "owner_home_dir", "") or "").strip()
    if not owner_home:
        return False
    from ...ingestion.watch_feedback import append_confirmation

    return append_confirmation(Path(owner_home), watch_id, stream_pos)


def _findings_ledger_path(agent: object) -> tuple[str, str]:
    """账本落点:子代理 runner 轮 → 本 run 的 findings.jsonl(权威通道);
    主代理长任务 → 任务工作区 work/shared/findings.jsonl(同名共享通道)。"""
    run_id = current_subagent_run_id(agent)
    run_path = _run_ledger_path(agent, run_id) if run_id else ""
    if run_path:
        return run_path, run_id
    task_root = str(getattr(agent, "_current_run_task_workspace", "") or "").strip()
    if task_root:
        return str(Path(task_root) / "work" / "shared" / "findings.jsonl"), "task_workspace"
    return "", ""


def _run_ledger_path(agent: object, run_id: str) -> str:
    manager = getattr(agent, "subagents", None)
    if manager is None or not callable(getattr(manager, "load", None)):
        return ""
    try:
        task = manager.load(run_id)
    except (FileNotFoundError, TypeError):
        return ""
    return str(getattr(task, "agent_run_findings_jsonl", "") or "").strip()


def build_record_finding_spec() -> ToolSpec:
    return ToolSpec(
        name=_TOOL_NAME,
        # collaboration = 默认 deferred 目录(tool_catalog_deferred_categories):不进主代理
        # 全局 catalog 正文(渐进式披露,防目录膨胀挤压上下文);子代理 runner 经 allowed_tools
        # 显式授权 + 执行合同"确认即记账"引导,不依赖全局目录曝光。
        category="collaboration",
        effect="mutating",
        # 追加式结论账(每次生成新 id 追加一行)对重试天然安全;side-effect 工具不声明
        # 幂等策略会被 tool_manifest 门整体 DENY——真机实锤:盯守主代理逐条入账被
        # TOOL_MANIFEST_IDEMPOTENCY_POLICY_MISSING 拦死,findings 恒空、只能改走 write_file。
        idempotency_scope="operation",
        description=(
            "把一条【已确认的结论/发现/完成事实】立刻写进本 run 的结论账本(findings.jsonl,追加一行)。"
            "确认一条记一条:之后收尾再崩、任务被取消,账还在,整合轮照样能收走;"
            "最终报告只是账本的汇总视图。"
        ),
        use_cases=[
            "盯守任务确认一条真命中:立刻入账(claim=事件ID+结果端理由),供主代理核验整合后自然汇报",
            "建站/编码任务完成一个模块并验证通过:入账一条完成事实(claim+产物路径)",
            "长分析任务得出一个中间结论:先入账再继续,防后面上下文压缩后忘掉",
        ],
        avoid_when=[
            "还没确认的猜测不要入账——拿不准的先验证",
            "不要把整篇报告塞进 claim:一条结论一行账,报告仍写产物文件",
        ],
        # 增补盯守特异词(盯守任务上推荐区能把本工具的详细说明浮现进 prompt);
        # 只放特异词,别放"上报/汇报"类通用词——通用词会让普通任务也拉进推荐区,prompt 白胖一截。
        keywords=["结论", "发现", "命中", "记账", "finding", "record", "确认", "落账", "持久化", "盯守", "watch_stream"],
        parameters={
            "claim": "必填:一句话写清确认了什么(结论本身,含关键标识如事件ID/模块名)",
            "evidence_refs": "可选:证据指针列表(文件路径/事件ID/URL)",
            "kind": "可选:结论类型标签,如 hit/module_done/analysis,默认 finding",
            "confidence": "可选:置信来源一句话(如'结果端字段 probe.verified=true')",
            "watch_id": "可选(盯守候选专用):该候选来自哪路 watch,从 pull 载荷原样复制",
            "stream_pos": "可选(盯守候选专用):候选行的 stream_pos 原样复制——系统据此把确认的结构特征回灌预筛,自动抬同类",
        },
        parameter_schema={
            "claim": {"type": "string"},
            "evidence_refs": {"type": "array", "items": {"type": "string"}},
            "kind": {"type": "string"},
            "confidence": {"type": "string"},
            "watch_id": {"type": "string"},
            "stream_pos": {"type": "integer"},
        },
        required_parameters=["claim"],
    )


__all__ = ["RecordFindingTool", "build_record_finding_spec"]
