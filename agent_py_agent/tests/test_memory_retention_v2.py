# LLM: 只操作pytest临时owner，沿真实plan/apply核验策略、候选、引用与可恢复回收；不清理真实记忆。
# 模块用途: 保护Memory v2留存的原有安全合同，义务候选不得被普通过期规则误删。
from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.memory_store.candidate_models import (
    CANDIDATE_STATUSES,
    CandidateObservation,
    MemoryScope,
)
from agent_py_agent.agent.memory_store.candidates import CandidateService, CandidateTransitionError
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
from agent_py_agent.agent.memory_store.retention import (
    RETENTION_SCHEMA_VERSION,
    MemoryRetentionService,
)
from agent_py_agent.agent.retrieval.vector_store import VectorStore
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

NOW = datetime(2026, 8, 4, tzinfo=timezone.utc)
OLD = "2024-01-01T00:00:00+00:00"


# LLM: 测试向量仅按输入长度确定性生成，不调用模型或网络，不承担正式语义嵌入能力。
# 类用途: 为硬删除回归提供可重现的本地向量生成器。
class _Embedder:
    # LLM: 每条输入恰好返回一个固定维度向量，只用于触发真实向量索引写入与清除链路。
    # 函数用途: 按文本长度构造两维测试向量。
    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, float(len(text) % 7)] for text in texts]


# LLM: 所有仓库限于 pytest 临时 home；候选、正式记忆及留存服务使用真实领域对象，不替换执行安全门。
# 函数用途: 装配临时 owner 的候选库、长期库与留存服务。
def _runtime(tmp_path: Path, *, conversation_roots: tuple[Path, ...] = ()):
    home = ensure_my_agent_home(tmp_path / "home")
    candidates = CandidateService(home.owner_memory_candidates_jsonl)
    long_term = JsonlMemory(
        home.owner_memory_long_term_jsonl,
        ops_path=home.owner_memory_ops_jsonl,
        candidate_service=candidates,
    )
    service = MemoryRetentionService(
        home_paths=home,
        candidates=candidates,
        long_term=long_term,
        conversation_roots=conversation_roots,
    )
    return home, candidates, long_term, service


# LLM: 只修改传入临时 home 的策略夹具；非法值有意保留给真实策略解析器拒绝，不在测试助手中纠正。
# 函数用途: 合并策略覆盖字段并写回测试 retention.json。
def _set_policy(home, **updates: object) -> dict[str, object]:
    payload = json.loads(home.owner_retention_json.read_text(encoding="utf-8"))
    payload.update(updates)
    home.owner_retention_json.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return payload


# LLM: 状态、更新时间和工具输出均为临时任务的显式夹具，不能凭文件年龄替代任务终态权威。
# 函数用途: 创建带状态、业务结果及可选工具正文的任务目录。
def _task(
    root: Path,
    task_id: str,
    status: str,
    updated_at: str,
    *,
    tool_output: bool = True,
) -> Path:
    task_root = root / "2026-01-01" / task_id
    state = task_root / "work" / "state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        json.dumps(
            {"task_id": task_id, "status": status, "updated_at": updated_at},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (task_root / "output").mkdir(parents=True)
    (task_root / "output" / "result.txt").write_text("结果", encoding="utf-8")
    if tool_output:
        output = task_root / "work" / "blobs" / "tool_outputs" / "large.txt"
        output.parent.mkdir(parents=True)
        output.write_text("大工具输出", encoding="utf-8")
    return task_root


# LLM: 仅为传入测试路径写文件并设置旧 mtime，不处理真实 owner 文件或推断保留授权。
# 函数用途: 创建一个便于留存规划命中的过期文件。
def _old_file(path: Path, content: str = "old") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    os.utime(path, (1, 1))
    return path


# LLM: 先经真实观察与审核状态机拒绝候选，再把临时账本更新时间设旧；不替代生产删除或资格判断。
# 函数用途: 准备一条可用于保留策略与隐私删除测试的陈旧拒绝候选。
def _reject_old_candidate(candidates: CandidateService):
    candidate = candidates.observe(
        CandidateObservation(
            candidate_type="long_term_fact",
            content="不应继续保留的候选正文。",
            subject_key="retention.rejected",
            scope=MemoryScope("personal", "personal"),
            origin="model_inferred",
            promotion_target="long_term",
        )
    )
    rejected = candidates.transition(
        candidate.candidate_id,
        "rejected",
        reviewer="admin",
        review_note="不适合作为长期记忆。",
    )
    old = replace(rejected, updated_at=OLD)
    candidates.path.write_text(
        json.dumps(old.to_record(), ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return old


# LLM: 用真实 ConversationStore 写入带结构化用户与时间的临时会话，不直接伪造会话索引。
# 函数用途: 创建一条用户历史消息并返回存储、线程与消息身份。
def _conversation(root: Path, *, user: str, at: float):
    store = ConversationStore(root)
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": user,
            "channel": "internal",
            "channel_conversation_id": f"chat-{user}",
            "channel_user_id": user,
            "now": at,
        }
    )
    message = store.messages.append(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": f"{user} 的历史原话",
            "now": at,
        }
    )
    return store, store.threads.load(thread.thread_id), message


# LLM: 按真实字节取快照，缺文件记 None，不以元数据相同冒充内容未变。
# 函数用途: 记录一组路径的内容以验证规划入口只读。
def _state(paths: list[Path]) -> dict[Path, bytes | None]:
    return {path: path.read_bytes() if path.is_file() else None for path in paths}


# LLM: 默认策略取自真实初始化文件，并通过真实只读规划核验，不在测试中复制替代解析规则。
# 函数用途: 验证 Memory v2 默认版本与各类必需保留期。
def test_retention_v2_default_policy_has_all_required_periods(tmp_path: Path):
    home, _candidates, _long_term, service = _runtime(tmp_path)
    policy = json.loads(home.owner_retention_json.read_text(encoding="utf-8"))

    assert policy["schema_version"] == RETENTION_SCHEMA_VERSION
    assert policy["conversation_days"] == 365
    assert policy["audit_days"] == 180
    assert policy["daily_days"] == 365
    assert policy["tool_output_days_after_terminal"] == 30
    assert policy["rejected_candidate_days"] == 30
    assert policy["curator_run_days"] == 90
    assert policy["compact_days"] == 365
    assert policy["completed_task_days"] == 365
    assert service.plan(now=NOW).ok is True


# LLM: 同一临时文件树先比较规划前后字节，再执行真实清理；活跃、法律保留及原始 audit 必须保留，不能全停假通过。
# 函数用途: 验证只读规划与跨候选、会话、任务、工具及归档的实际回收行为。
def test_retention_plan_is_read_only_and_apply_covers_every_authority(tmp_path: Path):
    conversation_root = (
        tmp_path
        / "home"
        / "owners"
        / "local"
        / "main"
        / "workspace"
        / "runtime"
        / "workspaces"
        / "demo"
        / "conversations"
    )
    home, candidates, _long_term, service = _runtime(
        tmp_path,
        conversation_roots=(conversation_root,),
    )
    _set_policy(
        home,
        conversation_days=30,
        audit_days=30,
        daily_days=30,
        tool_output_days_after_terminal=30,
        rejected_candidate_days=30,
        curator_run_days=30,
        compact_days=30,
        completed_task_days=365,
        legal_hold_task_ids=["held-task"],
    )
    old_audit = _old_file(home.owner_audit_dir / "2024-01-01.jsonl", "{}\n")
    fresh_audit = _old_file(home.owner_audit_dir / "fresh.jsonl", "{}\n")
    os.utime(fresh_audit, (NOW.timestamp(), NOW.timestamp()))
    old_daily = _old_file(home.owner_memory_daily_dir / "2024-01-01.jsonl", "{}\n")
    old_curator = _old_file(home.owner_memory_curator_runs_dir / "2024-01-01.jsonl", "{}\n")
    old_compact = _old_file(home.owner_compact_dir / "old.json", "{}")
    rejected = _reject_old_candidate(candidates)

    completed = _task(home.owner_tasks_dir, "completed-task", "DONE", OLD)
    tool_only = _task(
        home.owner_tasks_dir,
        "tool-only-task",
        "DONE",
        "2026-06-01T00:00:00+00:00",
    )
    active = _task(home.owner_tasks_dir, "active-task", "RUNNING", OLD)
    held = _task(home.owner_tasks_dir, "held-task", "DONE", OLD)

    old_store, old_thread, old_message = _conversation(
        conversation_root,
        user="old-user",
        at=datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp(),
    )
    _fresh_store, fresh_thread, _fresh_message = _conversation(
        conversation_root,
        user="fresh-user",
        at=datetime(2026, 8, 3, tzinfo=timezone.utc).timestamp(),
    )
    tracked = [
        old_audit,
        fresh_audit,
        old_daily,
        old_curator,
        old_compact,
        candidates.path,
        completed / "work" / "state.json",
        tool_only / "work" / "blobs" / "tool_outputs" / "large.txt",
        active / "work" / "blobs" / "tool_outputs" / "large.txt",
        held / "work" / "state.json",
        conversation_root / "threads" / f"{old_thread.thread_id}.json",
        conversation_root / "messages" / f"{old_thread.thread_id}.jsonl",
        conversation_root / "threads" / f"{fresh_thread.thread_id}.json",
    ]
    before = _state(tracked)

    plan = service.plan(now=NOW)

    assert plan.ok is True
    assert plan.applied is False
    assert _state(tracked) == before
    categories = {action.category for action in plan.actions}
    # audit 类别按 dev 2026-09-28 裁定一律跳过：O/audit/*.jsonl 是主代理每轮的记忆归档原始事件，
    # 类别名与实际数据不符、直接删不可恢复，要等存储登记表定了语义再处理。
    assert "audit" not in categories
    assert old_audit.exists()
    assert {
        "daily",
        "curator_run",
        "compact",
        "rejected_candidate",
        "completed_task",
        "tool_output",
        "conversation",
    } <= categories
    assert not any(action.path == active for action in plan.actions)
    assert not any(str(held) in str(action.path) for action in plan.actions)

    applied = service.apply(now=NOW)

    assert applied.ok is True
    assert applied.applied is True
    # audit 类别不再参与清理，两个文件都必须原地保留。
    assert old_audit.exists()
    assert fresh_audit.exists()
    assert not old_daily.exists()
    assert not old_curator.exists()
    assert not old_compact.exists()
    assert rejected.candidate_id not in {item.candidate_id for item in candidates.list()}
    assert not completed.exists()
    assert tool_only.exists()
    assert not (tool_only / "work" / "blobs" / "tool_outputs").exists()
    assert (active / "work" / "blobs" / "tool_outputs" / "large.txt").exists()
    assert held.exists()
    assert old_store.threads.load(old_thread.thread_id) is None
    assert not (conversation_root / "messages" / f"{old_thread.thread_id}.jsonl").exists()
    assert old_store.threads.load(fresh_thread.thread_id) is not None
    assert old_message.message_id not in (
        conversation_root / "user_latest_threads.json"
    ).read_text(encoding="utf-8")
    assert all(action.status in {"deleted", "trashed"} for action in applied.actions)


# LLM: 全局法律保留必须使规划无动作、执行不进入清理，且测试原件保留。
# 函数用途: 验证 legal_hold 在真实 plan/apply 两端生效。
def test_global_legal_hold_prevents_every_retention_action(tmp_path: Path):
    home, _candidates, _long_term, service = _runtime(tmp_path)
    old_audit = _old_file(home.owner_audit_dir / "2024-01-01.jsonl", "{}\n")
    _set_policy(home, audit_days=1, legal_hold=True)

    plan = service.plan(now=NOW)
    applied = service.apply(now=NOW)

    assert plan.legal_hold is True
    assert plan.actions == ()
    assert applied.applied is False
    assert applied.legal_hold is True
    assert old_audit.exists()


# LLM: 策略级损坏应拒绝整份执行，单个任务状态损坏仅隔离其子树；两者不能混成同一种全停或全放行。
# 函数用途: 验证非法策略关闭式失败与坏任务子树隔离。
def test_corrupt_policy_fails_closed_but_single_bad_task_is_isolated(tmp_path: Path):
    home, _candidates, _long_term, service = _runtime(tmp_path)
    old_audit = _old_file(home.owner_audit_dir / "2024-01-01.jsonl", "{}\n")
    policy = _set_policy(home, audit_days="30")

    invalid_policy = service.apply(now=NOW)

    assert invalid_policy.applied is False
    assert {error.error_code for error in invalid_policy.errors} == {
        "MEMORY_RETENTION_POLICY_INVALID"
    }
    assert old_audit.exists()

    policy["audit_days"] = 30
    home.owner_retention_json.write_text(json.dumps(policy), encoding="utf-8")
    state = home.owner_tasks_dir / "2026-01-01" / "broken" / "work" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text("{broken", encoding="utf-8")

    # R4（dev 2026-09-28 裁定）：单棵任务的 state 损坏只保护那一棵，不再连累整份计划——
    # 否则生产上会被少数坏目录永久卡住（真机已 29/29 次整份被拒）。policy 级失败仍整份拒绝（上面那条）。
    isolated_state = service.apply(now=NOW)

    assert isolated_state.applied is True
    assert "MEMORY_RETENTION_TASK_STATE_INVALID" in {
        error.error_code for error in isolated_state.errors
    }
    # 坏子树必须原地保留，绝不能被删或移走。
    assert state.exists()
    # audit 类别一律跳过（dev 2026-09-28 裁定），这个文件必须原地保留。
    assert old_audit.exists()


# LLM: 在真实规划后、执行前把任务重新置为 RUNNING，旧终态证明必须失效且工具恢复材料不能被删除。
# 函数用途: 验证执行端重新核对计划后变化的任务权威状态。
def test_apply_revalidates_terminal_state_changed_after_plan(
    tmp_path: Path,
    monkeypatch,
):
    from agent_py_agent.agent.memory_store import retention as retention_module

    home, _candidates, _long_term, service = _runtime(tmp_path)
    task = _task(
        home.owner_tasks_dir,
        "changing-task",
        "DONE",
        "2026-06-01T00:00:00+00:00",
    )
    _set_policy(home, completed_task_days=365, tool_output_days_after_terminal=30)
    state_path = task / "work" / "state.json"
    original = retention_module.execute_retention_plan

    # LLM: 只在执行接缝前真实改写测试任务状态，随后仍调用原执行器，不替换其状态与指纹复核。
    # 函数用途: 制造任务从已完成变为运行中的规划后竞争。
    def _change_then_execute(**kwargs):
        state_path.write_text(
            json.dumps(
                {
                    "task_id": "changing-task",
                    "status": "RUNNING",
                    "updated_at": "2026-08-04T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        return original(**kwargs)

    monkeypatch.setattr(retention_module, "execute_retention_plan", _change_then_execute)
    result = service.apply(now=NOW)

    assert result.applied is True
    assert result.ok is False
    assert result.actions[0].status == "state_changed"
    assert "MEMORY_RETENTION_STATE_CHANGED" in {
        error.error_code for error in result.errors
    }
    assert (task / "work" / "blobs" / "tool_outputs" / "large.txt").exists()


# LLM: 工具正文的旧 mtime 不得越过父任务非终态保护，真实规划与执行均不得删除恢复材料。
# 函数用途: 验证运行中任务始终保护过期工具输出。
def test_nonterminal_task_protects_tool_outputs_even_when_files_are_old(tmp_path: Path):
    home, _candidates, _long_term, service = _runtime(tmp_path)
    task = _task(home.owner_tasks_dir, "running-task", "RUNNING", OLD)
    output = task / "work" / "blobs" / "tool_outputs" / "large.txt"
    os.utime(output, (1, 1))
    _set_policy(home, tool_output_days_after_terminal=1)

    plan = service.plan(now=NOW)
    applied = service.apply(now=NOW)

    assert not any(action.category == "tool_output" for action in plan.actions)
    assert applied.ok is True
    assert output.exists()


# LLM: 使用真实长期库、SQLite 与向量索引验证硬删除；候选正文须脱敏，但用户历史会话必须逐字保留。
# 函数用途: 验证正式记忆隐私删除清除各权威副本及索引，不误删会话原文。
def test_hard_delete_scrubs_authority_indexes_candidate_and_not_conversation(tmp_path: Path):
    home = ensure_my_agent_home(tmp_path / "home")
    candidates = CandidateService(home.owner_memory_candidates_jsonl)
    local = LocalStore(
        tmp_path / "local" / "local.db",
        files_dir=tmp_path / "local" / "files",
        events_path=tmp_path / "local" / "events.jsonl",
        enable_fts=True,
    )
    memory = JsonlMemory(
        home.owner_memory_long_term_jsonl,
        local_store=local,
        ops_path=home.owner_memory_ops_jsonl,
        candidate_service=candidates,
        embedder=_Embedder(),
    )
    content = "需要被完整清除的正式长期记忆。"
    candidate = candidates.observe(
        CandidateObservation(
            candidate_type="long_term_fact",
            content=content,
            subject_key="privacy.delete",
            scope=MemoryScope("personal", "personal"),
            origin="model_inferred",
            promotion_target="long_term",
        )
    )
    record = memory.add(
        "system",
        content,
        kind="fact",
        source=f"candidate:{candidate.candidate_id}",
        attributes={
            "candidate_id": candidate.candidate_id,
            "subject_key": "privacy.delete",
            "scope_type": "personal",
            "scope_key": "personal",
        },
    )
    conversation = tmp_path / "conversation-sentinel.jsonl"
    conversation.write_text("用户历史真实消息\n", encoding="utf-8")
    conversation_before = conversation.read_bytes()
    record_id = local.make_record_id("memory", record.entry_id)
    local.upsert_record(
        source_type="memory",
        source_id=record.entry_id,
        title=record.kind,
        content=record.content,
        metadata={"kind": record.kind, "tags": record.tags or []},
    )
    assert local.get_record(record_id) is not None
    assert record.entry_id in VectorStore(
        home.owner_memory_long_term_dir / "memory_vectors.json"
    ).ids()
    service = MemoryRetentionService(
        home_paths=home,
        candidates=candidates,
        long_term=memory,
    )

    tombstone = service.hard_delete_long_term(
        record.entry_id,
        expected_version=record.version,
    )

    assert tombstone.action == "remove"
    assert memory.all() == []
    assert local.get_record(record_id) is None
    assert not any(path.read_text(encoding="utf-8") == content for path in local.files_dir.glob("*.txt"))
    assert record.entry_id not in VectorStore(
        home.owner_memory_long_term_dir / "memory_vectors.json"
    ).ids()
    redacted = candidates.get(candidate.candidate_id)
    assert redacted.content == ""
    assert redacted.content_hash
    from agent_py_agent.agent.memory_store.generation_manifest import MemoryGenerationStore

    store = MemoryGenerationStore(home.owner_memory_long_term_jsonl)
    tombstones = store.tombstones.load()
    assert record.entry_id in tombstones and tombstones[record.entry_id].action == "remove"
    assert all(event.action == "remove" for event in store.events())
    assert content not in home.owner_memory_long_term_jsonl.read_text(encoding="utf-8")
    assert content not in home.owner_memory_ops_jsonl.read_text(encoding="utf-8")
    assert conversation.read_bytes() == conversation_before


# LLM: 只在临时账本设置观察及更新时间；覆盖全部合法状态和三十天边界，不把新观察伪装成陈旧草稿。
# 函数用途: 准备陈旧与活跃候选并返回按保留策略应清理的精确集合。
def _retention_drafts(candidates):
    specs = [(status, 31) for status in sorted(CANDIDATE_STATUSES)]
    specs.extend((status, age) for status in sorted(CANDIDATE_STATUSES - {"promoted"}) for age in (29, 30))
    observed = candidates.observe_many([
        CandidateObservation(candidate_type="lesson", content=f"待保留经验 {index}", subject_key=f"retention.draft.{index}",
            scope=MemoryScope("personal", "personal"), origin="model_inferred", promotion_target="lesson")
        for index in range(len(specs))
    ])
    rows = [replace(row, status=status, updated_at=(NOW - timedelta(days=age)).isoformat(),
                     last_observed_at=(NOW - timedelta(days=age)).isoformat(),
                    status_history=[{**row.status_history[-1], "status": status}],
                    promoted_at=OLD if status == "promoted" else row.promoted_at,
                    promotion_ref="fixture:promoted" if status == "promoted" else row.promotion_ref)
            for row, (status, age) in zip(observed, specs, strict=True)]
    candidates.path.write_text("".join(json.dumps(row.to_record()) + "\n" for row in rows), encoding="utf-8")
    old_ids = {row.candidate_id for row, (status, age) in zip(rows, specs, strict=True)
               if status not in {"promoted", "approved"} and age > 30}
    return rows, old_ids


# LLM: plan 必须只读，apply 只能清理严格早于 cutoff 的非 promoted 状态；新鲜、边界与已晋升记录逐字保留。
# 函数用途: 验证长期不晋升草稿按默认三十天保留期清理。
def test_retention_cleans_all_old_unpromoted_drafts(tmp_path):
    _home, candidates, _memory, service = _runtime(tmp_path)
    rows, expected = _retention_drafts(candidates)
    before = candidates.path.read_bytes()
    plan = service.plan(now=NOW)
    assert plan.ok
    assert {action.record_id for action in plan.actions if action.operation == "candidate_delete"} == expected
    assert candidates.path.read_bytes() == before
    applied = service.apply(now=NOW)
    assert applied.ok and applied.applied
    assert {action.record_id for action in applied.actions if action.status == "deleted"} == expected
    assert {row.candidate_id: row.to_record() for row in candidates.list()} == {
        row.candidate_id: row.to_record() for row in rows if row.candidate_id not in expected}


# LLM: 直接仓库接口与扫描执行共享合法状态；混入 promoted 必须整批拒绝，不能发生部分删除。
# 函数用途: 验证保留规划与原批量清理接口的一致性和晋升保护。
def test_candidate_retention_apis_clean_old_drafts_and_protect_promoted(tmp_path):
    _home, candidates, _memory, _service = _runtime(tmp_path)
    rows, expected = _retention_drafts(candidates)
    assert set(candidates.retention_candidates(before=NOW - timedelta(days=30))) == expected
    promoted = next(row.candidate_id for row in rows if row.status == "promoted")
    before = candidates.path.read_bytes()
    with pytest.raises(CandidateTransitionError):
        candidates.delete_terminal([next(iter(expected)), promoted])
    assert candidates.path.read_bytes() == before
    assert candidates.delete_terminal(sorted(expected)) == len(expected)
    assert candidates.get(promoted).status == "promoted"


# LLM: 陈旧观察带正式目标版本约束时，只脱敏并转终态；重复清理仍保原绑定，不重新授权旧证据。
# 函数用途: 验证陈旧待审候选保留合法可读的证据墓碑。
@pytest.mark.parametrize("status", ["pending_review", "approved", "blocked_missing_evidence", "blocked_conflict", "observed"])
def test_retention_draft_binding_becomes_superseded_tombstone(tmp_path, status):
    from agent_py_agent.agent.memory_store.candidate_source_coverage import initial_source_coverage

    _home, candidates, _memory, service = _runtime(tmp_path)
    row = candidates.observe(CandidateObservation(candidate_type="lesson", content="不能重新授权的旧证据正文",
        subject_key="retention.bound", scope=MemoryScope("personal", "personal"), origin="model_inferred",
        promotion_target="lesson"))
    bindings = [{"key": ["memory-old", "message", "thread-old", "message-old"], "version": 1}]
    old = replace(row, status=status, updated_at=OLD, last_observed_at=OLD, target_evidence_bindings=bindings,
                  source_message_refs=[{"thread_id": "thread-old", "message_id": "message-old"}],
                  status_history=[{**row.status_history[-1], "status": status}])
    old = replace(old, learning_source_coverage=initial_source_coverage(old))
    candidates.path.write_text(json.dumps(old.to_record()) + "\n", encoding="utf-8")
    applied = service.apply(now=NOW)
    if status == "approved":
        assert applied.ok and not applied.actions
        assert candidates.get(row.candidate_id).content == old.content
        assert candidates.get(row.candidate_id).commit_obligation
        return
    assert applied.ok and [action.record_id for action in applied.actions] == [row.candidate_id]
    tombstone = candidates.get(row.candidate_id)
    assert tombstone.status == "superseded" and tombstone.content == ""
    assert tombstone.target_evidence_bindings == bindings
    assert not tombstone.source_message_refs and tombstone.content_redacted_at
    assert candidates.delete_terminal([row.candidate_id]) == 0
    assert candidates.retention_candidates(before=NOW + timedelta(days=365)) == []
    assert [a for a in service.plan(now=NOW + timedelta(days=365)).actions if a.operation == "candidate_delete"] == []
    assert [a for a in service.apply(now=NOW + timedelta(days=365)).actions if a.operation == "candidate_delete"] == []


# LLM: 即使调用者持有旧计划，账本更新也必须先被 fingerprint 拒绝；cutoff/status 守卫不因放宽状态而消失。
# 函数用途: 验证规划后候选变新时不清理。
def test_retention_draft_change_after_plan_is_preserved(tmp_path):
    from agent_py_agent.agent.memory_store.retention_apply import execute_retention_plan

    home, candidates, _memory, service = _runtime(tmp_path)
    rows, _expected = _retention_drafts(candidates)
    plan = service.plan(now=NOW)
    fresh = [replace(row, updated_at=NOW.isoformat()) for row in rows]
    candidates.path.write_text("".join(json.dumps(row.to_record()) + "\n" for row in fresh), encoding="utf-8")
    before = candidates.path.read_bytes()
    result = execute_retention_plan(home=home, candidates=candidates, plan=plan, now=NOW)
    assert result.actions and all(action.status == "state_changed" for action in result.actions)
    assert candidates.path.read_bytes() == before


# LLM: 陈旧观察上的真实审核发生在apply已读快照、删除尚未取锁的窗口；不伪造指纹掩盖TOCTOU。
# 函数用途: 证明刚获批并变新的候选不能被旧保留计划删除。
def test_retention_skips_candidate_updated_between_unlocked_read_and_locked_delete(tmp_path, monkeypatch):
    _home, candidates, _memory, service = _runtime(tmp_path)
    row = candidates.observe(CandidateObservation(candidate_type="lesson", content="刚获批的经验必须保留",
        subject_key="retention.atomic", scope=MemoryScope("personal", "personal"), origin="model_inferred",
        promotion_target="lesson"))
    candidates.path.write_text(json.dumps(replace(row, updated_at=OLD, last_observed_at=OLD).to_record()) + "\n", encoding="utf-8")
    plan = service.plan(now=NOW)
    assert [action.record_id for action in plan.actions] == [row.candidate_id]
    original = candidates.delete_terminal
    approved_state = []

    # LLM: 包装原删除而非替代锁内算法；审核使用真实状态机，保留审核后字节及 mtime 作为无重写证据。
    # 函数用途: 在进入删除锁之前完成一次新的用户审核。
    def _approve_before_delete(candidate_ids, **kwargs):
        candidates.transition(row.candidate_id, "approved", reviewer="user", review_note="approved just in time")
        approved_state.append((candidates.path.read_bytes(), candidates.path.stat().st_mtime_ns))
        return original(candidate_ids, **kwargs)

    monkeypatch.setattr(candidates, "delete_terminal", _approve_before_delete)
    result = service.apply(now=NOW)
    remaining = {item.candidate_id: item for item in candidates.list()}
    assert row.candidate_id in remaining
    assert remaining[row.candidate_id].status == "approved"
    assert remaining[row.candidate_id].content == row.content
    assert datetime.fromisoformat(remaining[row.candidate_id].updated_at) >= NOW - timedelta(days=30)
    assert [(action.record_id, action.status) for action in result.actions] == [(row.candidate_id, "state_changed")]
    assert [error.error_code for error in result.errors] == ["MEMORY_RETENTION_STATE_CHANGED"]
    assert (candidates.path.read_bytes(), candidates.path.stat().st_mtime_ns) == approved_state[0]


# LLM: 同批仅跳过锁前变动的一条；未变陈旧记录照常清理，既不虚报删除也不把整批独立动作误拦。
# 函数用途: 验证锁内 CAS 失败与成功动作能在同一份报告中准确共存。
def test_retention_atomic_snapshot_skip_does_not_block_unchanged_candidates(tmp_path, monkeypatch):
    _home, candidates, _memory, service = _runtime(tmp_path)
    rows, old_ids = _retention_drafts(candidates)
    changed = next(row for row in rows if row.status == "pending_review" and row.candidate_id in old_ids)
    original = candidates.delete_terminal

    # LLM: 在读取完成后只推进选中候选，其余行保持原字节事实，删除仍调用真实仓库。
    # 函数用途: 构造批次内一个陈旧快照与其余有效快照的竞争。
    def _approve_one_before_delete(candidate_ids, **kwargs):
        candidates.transition(changed.candidate_id, "approved", reviewer="user", review_note="approved just in time")
        return original(candidate_ids, **kwargs)

    monkeypatch.setattr(candidates, "delete_terminal", _approve_one_before_delete)
    result = service.apply(now=NOW)
    statuses = {action.record_id: action.status for action in result.actions}
    assert statuses == {cid: "state_changed" if cid == changed.candidate_id else "deleted" for cid in old_ids}
    assert len(result.actions) == len(old_ids)
    assert [error.error_code for error in result.errors] == ["MEMORY_RETENTION_STATE_CHANGED"]
    remaining = {row.candidate_id: row for row in candidates.list()}
    assert old_ids & remaining.keys() == {changed.candidate_id}
    assert remaining[changed.candidate_id].content == changed.content


# LLM: 每个真实读取入口都先冷后热并监视 deepcopy，不能靠预热或改写结果隐藏全账本复制。
# 函数用途: 验证候选统计、读取、保留规划及排除标记不复制整本候选。
@pytest.mark.parametrize("operation", ["stats", "get", "list", "readonly", "retention", "plan", "exclusions"])
def test_perf_e_cold_candidate_readers_do_not_copy_ledger(tmp_path, monkeypatch, operation):
    from copy import deepcopy
    from types import SimpleNamespace

    from agent_py_agent.agent.memory_store import candidates as module
    from agent_py_agent.agent.memory_store.learning_exclusions import sync_learning_exclusion_marks

    _home, candidates, _memory, service = _runtime(tmp_path)
    rows, old_ids = _retention_drafts(candidates)
    copies = []

    # LLM: 仅记录复制对象规模后调用原 deepcopy，不修改返回对象或绕过调用方的隔离语义。
    # 函数用途: 观察候选读取链路是否发生全表复制。
    def copy_spy(value):
        copies.append(len(value) if isinstance(value, (list, tuple)) else 1)
        return deepcopy(value)

    module._drop_candidate_load_cache(candidates.path)
    monkeypatch.setattr(module, "deepcopy", copy_spy)
    for _ in range(2):  # 同一入口先冷后热，不能靠预热隐藏首次全表复制。
        if operation == "stats":
            assert candidates.stats()["total"] == len(rows)
        elif operation == "get":
            assert candidates.get(rows[-1].candidate_id) == rows[-1]
        elif operation == "list":
            assert len(candidates.list(limit=1)) == 1
        elif operation == "readonly":
            reader = getattr(candidates, "readonly_rows", None)
            assert callable(reader), "missing readonly candidate scan"
            assert len(reader(limit=1)) == 1
        elif operation == "retention":
            assert set(candidates.retention_candidates(before=NOW - timedelta(days=30))) == old_ids
        elif operation == "plan":
            assert service.plan(now=NOW).ok
        else:
            thread = SimpleNamespace(thread_id="excluded-thread", channel_bindings=[
                SimpleNamespace(channel_conversation_id="excluded-chat")])
            conversations = SimpleNamespace(threads=SimpleNamespace(list_report=lambda **kw: ([thread], [])))
            assert sync_learning_exclusion_marks(conversation_store=conversations,
                candidate_service=candidates, excluded_conversation_ids=frozenset({"excluded-chat"}))["marked"] == 0
    assert all(size <= 1 for size in copies), copies
    if operation not in {"list", "get"}:
        assert copies == []


# LLM: 同一代账本重复 get 必须复用 ID 索引；计数真实属性访问而非仅比较耗时。
# 函数用途: 验证热 get 不逐条扫描候选 ID。
def test_perf_e_candidate_get_uses_same_generation_id_index(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store.candidate_models import MemoryCandidate

    _home, candidates, _memory, _service = _runtime(tmp_path)
    rows, _ = _retention_drafts(candidates)
    target = rows[-1].candidate_id
    candidates.get(target)
    original, lookups = MemoryCandidate.__getattribute__, []

    # LLM: 只计 candidate_id 的真实读取，其他属性和取值语义全部透传原实现。
    # 函数用途: 统计热 get 访问候选身份的次数。
    def tracked(row, name):
        if name == "candidate_id":
            lookups.append(True)
        return original(row, name)

    monkeypatch.setattr(MemoryCandidate, "__getattribute__", tracked)
    assert candidates.get(target).content == rows[-1].content
    assert len(lookups) <= 2, len(lookups)


# LLM: 只读借用必须尊重实例及子类的既有 load/list 覆盖，并仅调用一次，不能绕过扩展接缝。
# 函数用途: 验证 readonly_rows 对四种仓库读取覆盖保持兼容。
@pytest.mark.parametrize("override", ["load_instance", "load_subclass", "list_instance", "list_subclass"])
def test_perf_e_readonly_rows_preserves_override(tmp_path, monkeypatch, override):
    _home, candidates, _memory, _service = _runtime(tmp_path)
    rows, _ = _retention_drafts(candidates)
    expected, calls = rows[:1], []

    # LLM: 返回固定的临时候选投影并记录一次调用，不读取或改写生产账本。
    # 函数用途: 充当被覆盖的候选底层读取入口。
    def load():
        calls.append("load")
        return expected

    # LLM: 保存真实传入筛选参数并返回测试集合，供外层断言确认只读入口未绕过 list 覆盖。
    # 函数用途: 充当可观察的候选列表覆盖实现。
    def listed(**kwargs):
        calls.append(kwargs)
        return expected

    # LLM: 只覆盖既有底层读取扩展点，其他候选行为仍由原 CandidateService 提供。
    # 类用途: 验证只读借用兼容子类加载覆盖。
    class Loaded(CandidateService):
        # LLM: 委托测试 load 并保留其调用记录，不自行构造第二份仓库状态。
        # 函数用途: 提供子类的底层读取覆盖。
        def _load_unlocked(self):
            return load()

    # LLM: 仅替换 list 扩展点，保持真实服务的其余方法用于验证兼容分发。
    # 类用途: 验证只读借用兼容子类列表覆盖。
    class Listed(CandidateService):
        # LLM: 原样传递关键字参数给测试列表实现，不静默丢弃过滤条件。
        # 函数用途: 提供子类的列表读取覆盖。
        def list(self, **kwargs):
            return listed(**kwargs)

    if override.endswith("subclass"):
        candidates = (Loaded if override.startswith("load") else Listed)(candidates.path)
    else:
        monkeypatch.setattr(candidates, "_load_unlocked" if override.startswith("load") else "list",
            load if override.startswith("load") else listed)
    reader = getattr(candidates, "readonly_rows", None)
    assert callable(reader), "missing readonly candidate scan"
    assert reader(statuses={expected[0].status}, limit=1) == tuple(expected)
    assert len(calls) == 1


# LLM: 坏 JSON、坏 Schema 与重复身份均须由真实只读仓库拒绝，即使筛选集合为空也不能跳过完整性校验。
# 函数用途: 验证 readonly_rows 对三种账本损坏关闭式失败。
@pytest.mark.parametrize("bad_kind", ["json", "schema", "duplicate"])
def test_perf_e_readonly_rows_rejects_corruption(tmp_path, bad_kind):
    from agent_py_agent.agent.memory_store.candidates import CandidateStoreCorruptError

    _home, candidates, _memory, _service = _runtime(tmp_path)
    row = _reject_old_candidate(candidates)
    payload = {"json": "{broken\n", "schema": "{}\n",
        "duplicate": (json.dumps(row.to_record()) + "\n") * 2}[bad_kind]
    candidates.path.write_text(payload)
    reader = getattr(candidates, "readonly_rows", None)
    assert callable(reader), "missing readonly candidate scan"
    with pytest.raises(CandidateStoreCorruptError):
        reader(statuses=set())


# LLM: 真实批量合并只应一次构建查找索引；重复观察保持出现次数，不靠删记录或放宽结果断言达成性能目标。
# 函数用途: 验证候选批量观察避免逐输入全表扫描。
def test_perf_e_observe_batch_builds_lookup_indexes_once(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store.candidate_models import MemoryCandidate
    from agent_py_agent.agent.memory_store.candidates import merge_candidate_observations

    _home, candidates, _memory, _service = _runtime(tmp_path)
    seeds = [CandidateObservation(candidate_type="long_term_fact", content=f"索引记录{index}",
        subject_key=f"perf.lookup.{index}", scope=MemoryScope("personal", "personal"), origin="reviewed")
        for index in range(100)]
    rows = candidates.observe_many(seeds)
    original, reads = MemoryCandidate.__getattribute__, []

    # LLM: 记录真实候选 ID 属性访问并委托原实现，不影响合并身份或出现次数。
    # 函数用途: 统计批量合并构建与查找身份索引的读取规模。
    def tracked(row, name):
        if name == "candidate_id":
            reads.append(True)
        return original(row, name)

    monkeypatch.setattr(MemoryCandidate, "__getattribute__", tracked)
    merged, committed = merge_candidate_observations(rows, seeds[-12:])
    assert len(merged) == 100 and len(committed) == 12
    assert len(reads) < 600, len(reads)
    assert all(row.occurrence_count == 1 for row in committed)


# LLM: 旧文件可删除，但新鲜祖先与规范 tmp/cache 根必须保留，不能把文件年龄扩张成目录删除权。
# 函数用途: 验证删除旧临时文件后不会清除新鲜父链。
@pytest.mark.parametrize("category", ["tmp", "cache"])
def test_perf_e_retention_preserves_fresh_deleted_file_ancestors(tmp_path, category):
    home, _candidates, _memory, service = _runtime(tmp_path)
    root = home.owner_tmp_dir if category == "tmp" else home.owner_cache_dir
    old = _old_file(root / "old-parent" / "child" / "old.tmp")
    _set_policy(home, tmp_days=1, cache_days=1)
    assert service.apply(now=NOW).applied
    assert not old.exists()
    assert old.parent.is_dir() and list(old.parent.iterdir()) == []
    assert (root / "old-parent").is_dir()
    assert root.is_dir()


# LLM: 旧空目录只要存在新鲜祖先就受保护；无保护兄弟目录必须被清理，防止全停假绿。
# 函数用途: 验证空目录清扫逐层核对祖先年龄。
@pytest.mark.parametrize("category", ["tmp", "cache"])
@pytest.mark.parametrize("fresh_depth", [0, 1])
def test_perf_e_sweep_preserves_old_empty_dirs_beneath_fresh_ancestor(tmp_path, category, fresh_depth):
    home, _candidates, _memory, service = _runtime(tmp_path)
    root = home.owner_tmp_dir if category == "tmp" else home.owner_cache_dir
    outer = root / "outer"
    middle = outer / "middle"
    target = middle / "old-empty"
    target.mkdir(parents=True)
    sibling = root / "old-unprotected"
    sibling.mkdir()
    for directory in (outer, middle, target, sibling):
        os.utime(directory, (1, 1))
    fresh = (outer, middle)[fresh_depth]
    os.utime(fresh, (NOW.timestamp(), NOW.timestamp()))
    report = service.apply(now=NOW)
    assert report.applied and report.ok
    assert all(directory.is_dir() for directory in (root, outer, middle, target))
    assert not sibling.exists()


# LLM: 真实删除入口在目录时间恰等于 cutoff 时仍允许删除空子目录，但规范根始终保留。
# 函数用途: 锁定空目录年龄边界为包含等号。
def test_perf_e_empty_directory_cutoff_is_inclusive(tmp_path):
    from agent_py_agent.agent.memory_store.retention_apply import _remove_empty_ephemeral_dir

    root = tmp_path / "tmp"
    target = root / "boundary"
    target.mkdir(parents=True)
    cutoff = NOW.timestamp() - 3600
    os.utime(target, (cutoff, cutoff))
    assert _remove_empty_ephemeral_dir(root, ("boundary",), before=cutoff)
    assert root.is_dir() and not target.exists()


# LLM: 只删除旧空普通目录；新鲜、非空、符号链接及链接外目标必须保留，不能跟随链接越界清理。
# 函数用途: 验证 tmp/cache 空目录清扫的年龄、空目录与 nofollow 边界。
@pytest.mark.parametrize("category", ["tmp", "cache"])
def test_perf_e_retention_prunes_only_aged_empty_dirs_without_following_links(tmp_path, category):
    home, _candidates, _memory, service = _runtime(tmp_path)
    root = home.owner_tmp_dir if category == "tmp" else home.owner_cache_dir
    stale, fresh, occupied = (root / name for name in ("stale", "fresh", "occupied"))
    for path in (stale, fresh, occupied):
        path.mkdir()
    (occupied / "keep").write_text("new")
    for path in (stale, occupied):
        os.utime(path, (1, 1))
    os.utime(fresh, (NOW.timestamp(), NOW.timestamp()))
    external = tmp_path / "outside"
    (external / "old-empty").mkdir(parents=True)
    os.utime(external / "old-empty", (1, 1))
    (root / "link").symlink_to(external, target_is_directory=True)
    assert service.apply(now=NOW).applied
    assert not stale.exists()
    assert all(path.exists() for path in (root, fresh, occupied, external / "old-empty", root / "link"))


# LLM: 全局法律保留也覆盖附带空目录清扫，不能在未执行留存动作时偷偷删除空目录。
# 函数用途: 验证 legal_hold 保护旧空临时目录。
def test_perf_e_legal_hold_does_not_prune_empty_directories(tmp_path):
    home, _candidates, _memory, service = _runtime(tmp_path)
    stale = home.owner_tmp_dir / "held"
    stale.mkdir()
    os.utime(stale, (1, 1))
    _set_policy(home, legal_hold=True)
    assert not service.apply(now=NOW).applied
    assert stale.is_dir()


# LLM: 脱敏终态的未接受重放不能占用新观察键，否则会错误吞掉不同主题的独立观察。
# 函数用途: 验证候选合并保留旧脱敏行并接纳同键的真正独立主题。
def test_perf_e_redacted_candidate_does_not_occupy_unaccepted_observation_key(tmp_path):
    from agent_py_agent.agent.memory_store.candidates import merge_candidate_observations

    _home, candidates, _memory, _service = _runtime(tmp_path)
    first = CandidateObservation(candidate_type="long_term_fact", content="旧主题正文", subject_key="old.subject",
        scope=MemoryScope("personal", "personal"), origin="reviewed", observation_id="old-observation")
    original = candidates.observe(first)
    redacted = replace(original, content="", content_redacted_at=OLD, status="superseded")
    replay = replace(first, observation_id="new-observation")
    independent = replace(first, content="独立主题正文", subject_key="another.subject", observation_id="new-observation")
    rows, committed = merge_candidate_observations([redacted], [replay, independent])
    assert len(rows) == 2 and committed[0] is redacted
    assert committed[1].subject_key == "another.subject"
