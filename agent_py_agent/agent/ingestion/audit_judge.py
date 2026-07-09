"""/audit 保证档:每批候选在【全新聚焦上下文】里判读,治长盯守判读的"clear 惯性沟"。

真机实锤(/audit 全链路,与验收台 A–G 对照):入了队、判读工也判了的真命中,10 个里 6 个被判
成 clear 漏掉;把同一批被判 clear 的记录单独喂 MiniMax-M2.7 却逐条判 REAL(5/5)。差异不在模型
能力,在【判读被塞进长命子代理的累积会话】:隔离实验坐实——连续 ~25 轮 clear 之后,模型把明显
真事(response 正文明写"admin granted / desync confirmed / token leaks to attacker")也顺着判 clear,
甚至照抄前面那句 why。这是上下文累积出的惯性沟,不是判据/词典问题(禁加词典)。

修法:每批候选另起一个【无历史的聚焦模型调用】判读(和验收台 _call_minimax_verdicts 同款、
100% 召回的那条路),结论作为权威判据回填进 pull 载荷;子代理据此提交/上报,只在能从该条
response 举出具体反证时才改判。结构上永远给判读一个不受沟污染的锚——隔离实验:同样 25 轮 clear
惯性沟里,带上这个聚焦锚 3/3 都判回 hit(不带 1/3 仍漏)。

铁律沿用(No NL Judgment in Code):代码不做任何自然语言/关键词判定;判真假仍全交模型,这里只是
把"交给模型"从会污染的累积上下文换成一个干净的新上下文。判读须知/领域判据一律取自源自带的
note(source_envelope/judgment_note),代码不内置任何领域词。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

# 聚焦判读退空(return {})可观测化:上一版真机实锤=假后端 harness 绿、真网关红,根因是
# 退空静默——focus 在真 agent 上没发起模型调用也无声无息,验收台照不出。这条日志让每一次
# 「本该判却退回子代理自判」的降级都留痕(journalctl 可查),永不再让接线断裂隐身。
_focus_log = logging.getLogger("my_agent.ingestion.audit_focus")

# 无历史聚焦判读的系统指令(领域无关:请求/结果两端语义 + 逐条独立 + 只认 response 正文,
# 不是只看状态码)。领域判据从 {source_note} 注入,代码不硬编码任何领域词。
_FOCUS_SYSTEM = (
    "你是安全/异常值守的逐条判读器。只判【当前这批】候选,不带任何历史包袱——每一条都当作第一次"
    "见到,独立从它自己的 request(触发/输入端)+ response(结果/响应端)读语义再定性。\n"
    "结论三选一:真得手/真命中=hit;攻击尝试但被拦/失败/降级/回滚/no-op/未跨边界/未入账=clear;"
    "正文自相矛盾或真读不出成没成=unsure(存疑也是诚实结论)。\n"
    "判『得手/生效』读全 response 正文语义,不是只看状态码:很多迷惑项状态码像成功(200/ok),正文"
    "却说被拦下/被改写/被回滚/需人工复核——读到这类否定/未竟语义即使状态码成功也判 clear;反过来"
    "状态码不含成功字样但正文明说目标已达成也照判 hit。把去留押在你读懂的正文语义上。\n"
    "【绝不因为这批里前面几条都是 clear 就顺手把后面也判 clear——每条只看它自己的正文】。\n"
    "数据源判据说明(权威,严格照它定真假): {source_note}\n"
    "只输出一个 JSON:{{\"verdicts\":[{{\"ack_id\":\"..\",\"verdict\":\"hit|clear|unsure\","
    "\"evidence\":\"引用该条 response 里定性的原文片段\"}}, ...]}},为收到的【每一条】候选各给一行,"
    "ack_id 原样复制。不要输出别的。"
)

_MAX_EVIDENCE = 300


def focus_judge_candidates(
    agent: object, source_note: str, candidate_rows: list[dict[str, Any]]
) -> dict[str, dict[str, str]]:
    """一批候选行 → {ack_id: {"verdict","evidence"}},用【无历史的聚焦模型调用】判读。

    候选行取自 pull 载荷(每行含 ack_id + event)。任何前置条件不满足或调用/解析失败都返回 {}
    ——绝不阻断 pull、不丢批、不卡游标:回退到子代理自判(即今天的行为),聚焦判读只做增益不做拦路。
    """
    backend = getattr(agent, "backend", None)
    if backend is None or not callable(getattr(backend, "generate", None)):
        _focus_log.warning("focus_judge 退空:agent 无可调用 backend.generate(type=%s)", type(agent).__name__)
        return {}
    cand = [
        {"ack_id": str(row.get("ack_id")), "event": row.get("event")}
        for row in candidate_rows
        if isinstance(row, dict) and row.get("ack_id") and row.get("event") is not None
    ]
    if not cand:
        return {}
    system = _FOCUS_SYSTEM.format(source_note=(source_note or "(无,按通用安全判读)")[:2000])
    user = "candidates: " + json.dumps(cand, ensure_ascii=False)
    try:
        response = _focus_generate(agent, backend, system, user)
    except Exception as exc:  # noqa: BLE001 —— 判读只做增益不拦路,任何失败都退回子代理自判
        _focus_log.warning("focus_judge 退空:模型调用抛错 %s: %s", type(exc).__name__, str(exc)[:200])
        return {}
    text = str(getattr(response, "text", "") or "")
    verdicts = _parse_focus_verdicts(text, {c["ack_id"] for c in cand})
    if not verdicts:
        _focus_log.warning("focus_judge 退空:模型有响应但解析不出 verdicts(text_len=%d, n_cand=%d)", len(text), len(cand))
    return verdicts


def _focus_generate(agent: object, backend: object, system: str, user: str) -> object:
    """走【和主/子代理真实调模型同一条】provider-transient-auto-resume 路径发起聚焦判读调用。

    此前是裸 ``backend.generate(...)`` + 静默 except——全代码库其它模型调用
    (materialize/repair/compact/learning_review)都套这层重试,聚焦判读是唯一漏套的一条:
    真网关下遇限流/瞬时不可用会直接抛错退空(而非重试),且被 except 静默吞掉、无声无息。
    补齐这层让瞬时错误按同一阶梯重试而不是一撞就丢 focus;配合上面三处退空 warning,
    接线一旦断在真 agent 上会留痕(治「假后端 harness 绿、真网关红」)。判读语义/prompt
    一字未动,只改「怎么把请求发出去」+「断了要不要出声」。
    """
    from ..agent_core.provider_transient_auto_resume import (
        run_with_provider_transient_auto_resume,
    )

    return run_with_provider_transient_auto_resume(
        lambda: backend.generate(system, messages=[{"role": "user", "content": user}]),
        policy=getattr(agent, "runtime_guard_policy", None),
    )


def _parse_focus_verdicts(text: str, valid_acks: set[str]) -> dict[str, dict[str, str]]:
    """从模型输出里抽 {verdicts:[...]};只认合法 ack_id + 合法 verdict,其余丢弃(不猜、不补默认)。"""
    match = re.search(r"\{.*\"verdicts\".*\}", text, re.S)
    if not match:
        return {}
    try:
        rows = json.loads(match.group(0)).get("verdicts") or []
    except (json.JSONDecodeError, AttributeError):
        return {}
    out: dict[str, dict[str, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ack = str(row.get("ack_id") or "")
        kind = str(row.get("verdict") or "").strip().lower()
        if ack in valid_acks and kind in ("hit", "clear", "unsure"):
            out[ack] = {"verdict": kind, "evidence": str(row.get("evidence") or "")[:_MAX_EVIDENCE]}
    return out


__all__ = ["focus_judge_candidates"]
