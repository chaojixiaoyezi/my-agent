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


# LLM: 直接调用规划点的 _material；待办按用例顺序编号 t1..tN，期望写待办编号（或非选择键），这里换算成 todo_<序号>。
#   已完成/跳过的待办由产品自己剔除，题号只覆盖仍开着的项。
# 函数用途: 生成规划（planning）一条用例的真实请求材料与期望答案。
def planning(case: dict, shared: dict) -> tuple[dict, dict, dict]:
    from agent_py_agent.agent.agent_core import decision_planning

    items = [{"id": f"t{index}", "title": title, "status": status}
             for index, (title, status) in enumerate(case["todos"], start=1)]
    payload = {"run_id": "bench", "items": items,
               "display_plan": {"generation_id": "bench-plan", "revision": 1, "item_ids": [item["id"] for item in items]}}
    agent = SimpleNamespace(_current_user_prompt=case["prompt"], config=None)
    state, questions, _revision = decision_planning._material(agent, payload)
    index = {row["id"]: row["candidate"] for row in state["todos"]}
    return state, questions, {"priority": {index.get(value, value) for value in case["expected"]}}


# LLM: 直接调用外部材料阅读顺序点的 _material；页按用例顺序成为 page_<序号>，内容哈希由正文算出，不含 URL。
# 函数用途: 生成外部材料阅读顺序（external_material_order）一条用例的真实请求材料与期望答案。
def external_material_order(case: dict, shared: dict) -> tuple[dict, dict, dict]:
    from agent_py_agent.agent.agent_core.tool_context import external_material_order as module

    pages = [{"title": title, "preview": preview, "artifact_ref": f"artifact://bench/{case['id']}/{index}",
              "content_hash": hashlib.sha256(f"{title}\n{preview}".encode()).hexdigest()}
             for index, (title, preview) in enumerate(case["pages"], start=1)]
    record = SimpleNamespace(params=SimpleNamespace(user_prompt=case["prompt"]),
                             result=SimpleNamespace(metadata={"handler_details": {"pages": pages}}))
    state, questions, _revision = module._material(record, {"output_hash": "0" * 64, "scoped_call_id": "bench"})
    return state, questions, {f"page_{number}": set(values) for number, values in case["expected"].items()}


# LLM: 调用选模型点的真实模态过滤与 _material，再按 apply 模式换上 _APPLY_INSTRUCTIONS（正式采用时的题面）；
#   候选目录、当前模型都来自用例共享表，不读模型目录、不探针。期望写候选编号或保留类非选择键。
# 函数用途: 生成选模型（model_selection）一条用例的真实请求材料与期望答案。
def model_selection(case: dict, shared: dict) -> tuple[dict, dict, dict]:
    from agent_py_agent.agent import gateway_model_observation as module

    current = shared["current"]
    config = SimpleNamespace(model_name=current["model_name"], model_backend=current["model_backend"],
                             model_context_window_tokens=current["context_window_tokens"])
    context = SimpleNamespace(agent=SimpleNamespace(config=config), request={"prompt": case["prompt"]})
    thread = SimpleNamespace(summary=case.get("summary", ""), model_profile_id="", model_selection_revision=0)
    captured = SimpleNamespace(profile_id=current["profile_id"])
    candidates = {key: dict(row) for key, row in shared["candidates"].items()}
    candidates, _generations, hint, retained = module._filter_by_modality(context, candidates, {})
    if retained is not None:
        raise ValueError(f"{case['id']}: 模态过滤后没有可选候选")
    state, questions, _revision = module._material(context, thread, captured, (candidates, hint))
    questions["model"]["instructions"] = module._APPLY_INSTRUCTIONS
    return state, questions, {"model": set(case["expected"])}


# LLM: 用产品 render_lesson_draft 在内存里生成合法草稿（hash 与渲染一致），组装待确认的 SkillProposal 后调用 _facts/_material；
#   不写提案文件、不读 owner 目录。提案按用例顺序成为 proposal_<序号>，期望按序号写。
# 函数用途: 生成 Skill 提案审核顺序（skill_proposal_review）一条用例的真实请求材料与期望答案。
def skill_proposal_review(case: dict, shared: dict) -> tuple[dict, dict, dict]:
    from agent_py_agent.agent.capability import decision_skill_proposal_review as module
    from agent_py_agent.agent.capability.skill_proposals import (
        PROPOSAL_PENDING,
        SkillProposal,
        SkillProposalSource,
        SkillProposalTarget,
        render_lesson_draft,
    )

    pending = []
    for index, (skill_name, lesson, scenario) in enumerate(case["proposals"], start=1):
        candidate = SimpleNamespace(content=lesson, scope={"applies_when": scenario}, candidate_id=f"candidate-{index}",
                                    source_task_ids=(f"task-{index}",), source_run_ids=(f"run-{index}",))
        draft = render_lesson_draft(candidate, skill_name)
        pending.append(SkillProposal(f"proposal-{index}", 1, PROPOSAL_PENDING,
                                     SkillProposalSource(f"candidate-{index}", draft.sha256, (f"task-{index}",), (f"run-{index}",)),
                                     SkillProposalTarget(skill_name), draft,
                                     f"2026-10-0{index}T00:00:00+00:00", f"2026-10-0{index}T00:00:00+00:00"))
    pending = tuple(pending)
    state, questions, _revision = module._material(pending, module._facts(pending))
    return state, questions, {f"proposal_{number}": set(values) for number, values in case["expected"].items()}


# LLM: 按用例步骤造本轮工具归档：verify 步是 run_command 的验证事件，edit 步是写入工具留下的 stale 状态；最后一步是当前回执。
#   调用交付复核点的 _review_focuses/_material；期望按“项目根|kind|scope”写，这里按产品焦点顺序换算成 focus_<序号>。
# 函数用途: 生成交付复核焦点（delivery_quality）一条用例的真实请求材料与期望答案。
def delivery_quality(case: dict, shared: dict) -> tuple[dict, dict, dict]:
    from agent_py_agent.agent.agent_core.tool_context import decision_delivery_quality as module

    archive_rows = [_delivery_archive_row(index, step) for index, step in enumerate(case["steps"], start=1)]
    call = SimpleNamespace(run_id="run-1", tool_name="run_command", to_dict=lambda: {"tool_name": "run_command"})
    params = SimpleNamespace(archive_tool_calls=archive_rows, task_id="task-1", user_prompt=case["prompt"])
    record = SimpleNamespace(call=call, params=params)
    focuses = module._review_focuses(record, archive_rows[-1])
    state, questions, _revision = module._material(record, archive_rows[-1], 2000)
    keys = {f"{focus['root']}|{focus['kind']}|{focus['scope']}": f"focus_{order}" for order, focus in enumerate(focuses, 1)}
    return state, questions, {"review_focus": {keys.get(value, value) for value in case["expected"]}}


# 函数用途: 把一个用例步骤变成与产品归档同形的一条记录（验证事件或文件修改后的 stale 状态）。
def _delivery_archive_row(index: int, step: dict) -> dict:
    if "edit" in step:
        envelope = {"verification_state": [{"status": "stale", "root": step["edit"]}]}
        tool = "edit_file"
    else:
        verify = step["verify"]
        evidence = {"id": index, "kind": verify["kind"], "scope": verify["scope"], "status": verify["status"],
                    "root": verify["root"], "canonical_command": verify.get("command", "pytest -q"),
                    "created_at": f"2026-10-02T00:00:{index:02d}+00:00", "exit_code": 0 if verify["status"] == "passed" else 1}
        envelope = {"verification_evidence": evidence, "process": {"status": "exited", "return_code": evidence["exit_code"]}}
        tool = "run_command"
    return {"tool": tool, "id": f"call-{index}", "run_id": "run-1", "task_id": "task-1",
            "scoped_call_id": f"run-1:call-{index}", "output_hash": "a" * 64, "tool_result_envelope": envelope}


# LLM: 按产品观察记录形状造一条插件 read 观察（宿主铸的观察/候选编号、本地目标事实），再调用 _observation/_material；
#   候选按用例顺序成为 c<序号>，期望按序号写（或非选择键）。动作名只需非空，本函数不核对可用性。
# 函数用途: 生成动作候选（action_candidate）一条用例的真实请求材料与期望答案。
def action_candidate(case: dict, shared: dict) -> tuple[dict, dict, dict]:
    from agent_py_agent.agent.agent_core.tool_context import decision_action_candidate as module

    candidates = [{"candidate_id": f"cand-{index:016x}", "key": f"key-{index}", "role": role, "label": label,
                   "actions": ["plugin__browser_lite_bench__click"]}
                  for index, (role, label) in enumerate(case["candidates"], start=1)]
    observation = {"schema": module.OBSERVATION_SCHEMA, "observation_id": "obs-" + "a" * 24, "plugin_id": "browser-lite",
                   "activation_id": "activation-bench", "tool": "plugin__browser_lite_bench__read",
                   "target_kind": case.get("target_kind", "page"), "target_ref": "tab-bench", "target_ref_hash": "target-bench",
                   "generation": "gen-1", "content_hash": "c" * 24, "candidates": candidates}
    archive = {"tool_result_envelope": {"observation": observation}, "scoped_call_id": "run-1:call-1", "output_hash": "a" * 64}
    record = SimpleNamespace(params=SimpleNamespace(user_prompt=case["prompt"]))
    state, questions, _revision = module._material(record, archive, module._observation(archive), 2000)
    return state, questions, {"next_candidate": {f"c{value}" if str(value).isdigit() else value for value in case["expected"]}}


# LLM: 用轻量对象造工具快照（插件类工具带 provider_id）、Skill 列表与参数，调用能力推荐点的真实 _material；
#   候选按产品分组顺序成为 candidate_<序号>，期望按候选名（插件写 provider_id、Skill 写名字）写，这里换算成题号。
# 函数用途: 生成能力推荐（skill_tool）一条用例的真实请求材料与期望答案。
def skill_tool(case: dict, shared: dict) -> tuple[dict, dict, dict]:
    from agent_py_agent.agent.capability import decision_recommendation as module

    runtimes = []
    for name, description, provider in case["tools"]:
        spec = SimpleNamespace(name=name, description=description, category="plugins", schema_hash=f"schema-{name}",
                               hints=SimpleNamespace(provider_id=provider))
        runtimes.append(SimpleNamespace(model_spec=spec, exposure=SimpleNamespace(model_visible=True)))
    entries = [SimpleNamespace(stable_id=f"skill:{name}", name=name, description=description, when_to_use=when,
                               content_sha256=f"sha-{name}", tools_required=())
               for name, description, when in case.get("skills", [])]
    skills = SimpleNamespace(enabled_entries=lambda: list(entries), packages=[], fingerprint="skills-bench")
    snapshot = SimpleNamespace(runtimes=runtimes, snapshot_hash="snapshot-bench")
    params = SimpleNamespace(user_prompt=case["prompt"], allowed_tools=None, context_scope="default", task_attributes={})
    agent = SimpleNamespace(backend=_MAIN_BACKEND, config=_MAIN_CONFIG)
    policy = {"context_policy": "progressive", "optional_categories": ["plugins"]}
    state, questions, _revision = module._material(agent, params, snapshot, skills, policy, bool(entries))
    index = {question["instructions"]["candidate"]["name"]: key for key, question in questions.items()}
    return state, questions, {index[name]: set(values) for name, values in case["expected"].items()}


# LLM: 子代理选模的真实 _questions/_frozen_material；会加载真实后端的 _candidate_request_limits 用 mock 换成用例给的窗口与
#   输出上限（同字段），候选目录来自用例共享表。孩子按用例顺序成为题号 "0","1"...，期望按序号写候选编号或保留类非选择键。
# 函数用途: 生成子代理选模（subagent_model）一条用例的真实请求材料与期望答案。
def subagent_model(case: dict, shared: dict) -> tuple[dict, dict, dict]:
    from agent_py_agent.agent.agent_core.orchestration import decision_subagent as module

    candidates = {key: dict(row) for key, row in shared["candidates"].items()}
    limits = {key: {"runtime_config_revision": f"rev-{key}", "model_name": row["model_name"], "model_backend": row["model_backend"],
                    "context_window_tokens": row["context_window_tokens"], "output_cap_tokens": None,
                    "output_cap_status": "unknown", "output_cap_reason": "not_proven_by_original_provider_contract",
                    "provider_tool_support": "unknown_until_original_runner_probe"} for key, row in candidates.items()}
    eligible = {str(index): module.SubagentModelInput(raw={}, prepared=SimpleNamespace(goal=goal),
                                                      canonical=SimpleNamespace(id=f"child-{index}", goal=goal))
                for index, goal in enumerate(case["children"])}
    agent = SimpleNamespace(_current_run_params=None)
    stage = SimpleNamespace(deadline=time.monotonic() + 60)
    with mock.patch.object(module, "_candidate_request_limits", mock.Mock(return_value=limits)):
        questions, snapshots = module._questions(agent, eligible, candidates, stage)
    _revision, state = module._frozen_material(candidates, snapshots, questions)
    return state, questions, {key: set(values) for key, values in case["expected"].items()}


# 已有用例的点位 → 适配函数；没登记的点位没有基准，不能默认打开（见 decision_quality_bench.gate_failures）。
ADAPTERS = {"pre_recall": pre_recall, "recall": recall, "curator": curator, "curator_relation": curator_relation,
            "planning": planning, "external_material_order": external_material_order, "model_selection": model_selection,
            "skill_proposal_review": skill_proposal_review, "delivery_quality": delivery_quality,
            "action_candidate": action_candidate, "skill_tool": skill_tool, "subagent_model": subagent_model}
