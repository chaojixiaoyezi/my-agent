# LLM: 决策质量基准（J12）的点位适配层：把 cases/<point>.json 里的中文结构化用例交给各点位真实的材料构造代码，
#   得到与产品逐字节同形的 state/questions，再把用例里写的期望答案换算成 {题目编号: 可接受答案集合}。
#   只用假对象（SimpleNamespace / 产品数据类），不建 Agent、不读写 owner 目录、不发网络请求。
#   依赖各点位的私有构造函数（_material 等）：构造函数改名或改签名时，test_decision_quality_bench.py 的离线用例会先失败。
#   新增点位时在 ADAPTERS 登记，并同步 decision_quality/thresholds.json、README 与测试。
# 模块用途: 为决策质量基准生成每个用例的真实请求材料和打分口径。
from __future__ import annotations

import hashlib
import time
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import mock

# 用例里假装的主模型身份：只进材料里的 primary_model 一类字段，不连任何模型。
_MAIN_BACKEND = SimpleNamespace(name="anthropic", model_name="MiniMax-M2.7")
_MAIN_CONFIG = SimpleNamespace(model_context_window_tokens=204_800, model_name="MiniMax-M2.7", model_backend="anthropic")
_PERSONAL = {"scope_type": "personal", "scope_key": "personal"}


# LLM: 只读用例字段；facts 是本文件共享的事实表，按编号取正文，编号也是 MemoryRecord.entry_id。
# 函数用途: 把用例里的事实编号列表变成产品的正式记忆记录。
def _records(facts: dict, ids: list) -> list:
    from agent_py_agent.agent.memory_store.jsonl import MemoryRecord

    return [MemoryRecord("user", facts[entry_id], entry_id=entry_id, kind="fact", version=1, attributes=dict(_PERSONAL))
            for entry_id in ids]


# LLM: 截获 supplement_recalled_memories 内联拼出的 state/questions：patch 掉 decide 与片段材料设置读取，
#   候选检索按用例 hits 写死（模拟语义召回）；截获后返回 observe 结果，函数原样返回原召回，不记访问。
# 函数用途: 生成召回前补充查询（pre_recall）一条用例的真实请求材料与期望答案。
def pre_recall(case: dict, shared: dict) -> tuple[dict, dict, dict]:
    from agent_py_agent.agent.agent_core.runtime.loop_models import RuntimeContextRequest
    from agent_py_agent.agent.memory_store import decision_recall
    from agent_py_agent.agent.memory_store.recall import MemoryRecallScope

    facts = shared["facts"]
    queries = decision_recall.supplemental_query_candidates(case["prompt"])
    texts = {text: int(query_id.removeprefix("query_")) for query_id, text in queries}
    base = _records(facts, case["base"])

    def search(query, top_k, predicate):
        rows = _records(facts, case.get("hits", {}).get(str(texts.get(query, 0)), []))
        return [row for row in rows if predicate(row)][:top_k]

    capture = mock.Mock(return_value=SimpleNamespace(mode="observe", status="success", may_apply=False, response=None))
    agent = SimpleNamespace(memory=SimpleNamespace(search_scoped_candidates=search), backend=_MAIN_BACKEND, config=_MAIN_CONFIG)
    request = RuntimeContextRequest(case["prompt"], None, False, request_id="bench", run_id="bench", task_id="bench",
                                    task_attributes={"agent_thread_id": "bench"})
    stage = SimpleNamespace(error_code="", enabled_points=("pre_recall",), deadline=time.monotonic() + 60, thread_id="bench")
    top_k = int(shared.get("top_k", 5))
    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(decision_recall, "decide", capture))
        stack.enter_context(mock.patch.object(decision_recall, "_fragment_material",
                                              mock.Mock(return_value=case.get("material", "query_text"))))
        decision_recall.supplement_recalled_memories(
            agent, request, base, recall_scope=MemoryRecallScope.from_runtime(), stage=stage, queries=queries,
            slots=top_k - len(base), search_top_k=top_k, remaining_chars=10_000 - sum(len(row.content) for row in base),
            refresh=lambda: base)
    if capture.call_args is None:
        raise ValueError(f"{case['id']}: 补充查询没有走到提问（检查片段、名额或预检结果）")
    kwargs = capture.call_args.kwargs
    return kwargs["state"], kwargs["questions"], {key: set(values) for key, values in case["expected"].items()}


# LLM: 直接调用召回后排序的 _material；题号 memory_<序号> 按 memories 顺序，期望按记忆编号写、这里换算成题号。
# 函数用途: 生成召回后排序（recall）一条用例的真实请求材料与期望答案。
def recall(case: dict, shared: dict) -> tuple[dict, dict, dict]:
    from agent_py_agent.agent.agent_core.runtime.loop_models import RuntimeContextRequest
    from agent_py_agent.agent.memory_store import decision_recall
    from agent_py_agent.agent.memory_store.recall import MemoryRecallScope

    records = _records(shared["facts"], case["memories"])
    agent = SimpleNamespace(backend=_MAIN_BACKEND, config=_MAIN_CONFIG)
    request = RuntimeContextRequest(case["prompt"], None, False)
    state, questions, _revision = decision_recall._material(agent, request, records, MemoryRecallScope.from_runtime())
    index = {entry_id: f"memory_{position}" for position, entry_id in enumerate(case["memories"])}
    return state, questions, {index[entry_id]: set(values) for entry_id, values in case["expected"].items()}


# LLM: 消息按用例顺序成为 item_<序号>；只给写了期望的题打分，其余题照常发出（与产品同一题面）。
# 函数用途: 生成整理标签（curator）一条用例的真实请求材料与期望答案。
def curator(case: dict, shared: dict) -> tuple[dict, dict, dict]:
    from agent_py_agent.agent.memory_store import decision_curator
    from agent_py_agent.agent.memory_store.curator_inputs import CuratorInputBatch

    messages = tuple(_message(f"m{index}", text) for index, text in enumerate(case["messages"]))
    state, questions, _sources, _revision = decision_curator._decision_material(CuratorInputBatch(messages, ()))
    expected = {}
    for position, fields in case["expected"].items():
        for field, values in fields.items():
            expected[f"item_{position}_{field}"] = set(values)
    return state, questions, expected


# LLM: 题号按产品挑对顺序（BM25 排序）生成，所以期望按“消息编号|正式条目编号”写，这里经题面 instructions 的
#   source_id / formal_ref 换算成 pair_<序号>；没写期望的对按用例 others（默认 no_match）打分。
# 函数用途: 生成记忆关系（curator_relation）一条用例的真实请求材料与期望答案。
def curator_relation(case: dict, shared: dict) -> tuple[dict, dict, dict]:
    from agent_py_agent.agent.memory_store import decision_curator_relation
    from agent_py_agent.agent.memory_store.curator_inputs import CuratorInputBatch

    messages = tuple(_message(key, text) for key, text in case["messages"].items())
    formal = tuple(_formal(key, text) for key, text in case["formal"].items())
    state, questions, _pairs, _revision, _incomplete = decision_curator_relation._relation_material(
        CuratorInputBatch(messages, (), formal))
    others = set(case.get("others", ["no_match"]))
    expected = {}
    for question_id, question in questions.items():
        key = f"{question['instructions']['source_id']}|{question['instructions']['formal_ref'].rsplit('#', 1)[1]}"
        expected[question_id] = set(case["expected"].get(key, others))
    return state, questions, expected


# LLM: 正文、预览与哈希一致，才算完整消息（关系点位要求）；时间固定，材料逐字节可复现。
# 函数用途: 造一条整理批次里的用户消息。
def _message(message_id: str, text: str):
    from agent_py_agent.agent.memory_store.curator_inputs import CuratorMessageInput

    return CuratorMessageInput(message_id, f"thread-{message_id}", "user", "internal", 1_790_000_000.0, text, text,
                               "sha256:" + hashlib.sha256(text.encode()).hexdigest(), {})


# LLM: 版本为整数 1、长度与正文一致、哈希与正文一致，满足关系点位的完整性校验。
# 函数用途: 造一条个人范围的完整正式长期事实。
def _formal(entry_id: str, text: str):
    from agent_py_agent.agent.memory_store.curator_formal import CuratorFormalMemoryInput

    return CuratorFormalMemoryInput("long_term", entry_id, f"memory/long_term/memory.jsonl#{entry_id}", text,
                                    "sha256:" + hashlib.sha256(text.encode()).hexdigest(), f"subject-{entry_id}",
                                    "personal", "personal", "2026-09-28T00:00:00+00:00",
                                    authority_version=1, content_chars=len(text))


# 已有用例的点位 → 适配函数；没登记的点位没有基准，不能默认打开（见 decision_quality_bench.gate_failures）。
ADAPTERS = {"pre_recall": pre_recall, "recall": recall, "curator": curator, "curator_relation": curator_relation}
