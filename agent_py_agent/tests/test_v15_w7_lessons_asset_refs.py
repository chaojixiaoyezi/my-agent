"""lesson引用往返、旧记录与确定性pitfall sidecar。"""
import json
from dataclasses import replace
from datetime import datetime, timezone

from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.pitfalls import read_known_pitfalls
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.capability.asset_evolution import route_evolution_lesson
from agent_py_agent.agent.memory_store.candidate_models import (
    CandidateObservation,
    MemoryCandidate,
    MemoryScope,
)
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.lessons import LessonRepository
from agent_py_agent.tests.test_v15_w7_evolution_routes import verified_target


# LLM: 从原CandidateService观察及读回，不手写候选JSON假装往返。
# 函数用途: 验证引用限制与候选持久化。
def test_candidate_asset_refs_roundtrip(tmp_path):
    service = CandidateService(tmp_path / 'memory' / 'candidates.jsonl')
    observation = CandidateObservation('lesson', '状态核对要固定输入与输出。', 'state-check', MemoryScope('personal', 'personal'),
        'tool_verified', evidence_refs=({'call_id': 'call'},), source_run_ids=('run-a',),
        promotion_target='lesson', asset_refs=('script:owner:sample', 'workflow:owner:flow', 'data_source:system', '/bad/path'))
    candidate = service.observe(observation)
    assert candidate.asset_refs == ['script:owner:sample', 'workflow:owner:flow', 'data_source:system']
    loaded = MemoryCandidate.from_record(candidate.to_record())
    assert loaded.asset_refs == candidate.asset_refs
    legacy = service.observe(CandidateObservation('lesson', '旧记录没有资产引用。', 'legacy-check', MemoryScope('personal', 'personal'),
        'tool_verified', evidence_refs=({'call_id': 'legacy'},), promotion_target='lesson'))
    raw = legacy.to_record()
    raw.pop('asset_refs')
    assert MemoryCandidate.from_record(raw).asset_refs == []


# LLM: 正式lesson仍需approved和原证据门，不用资产引用降低门槛。
# 函数用途: 验证正式lesson附带refs及旧文件读取。
def test_lesson_asset_refs_and_old_records_load(tmp_path):
    service = CandidateService(tmp_path / 'memory' / 'candidates.jsonl')
    candidate = service.observe(CandidateObservation('lesson', '固定脚本变更后核对原输入。', 'check', MemoryScope('personal', 'personal'),
        'user_explicit', source_message_refs=({'message_id': 'confirmed'},), evidence_refs=({'call_id': 'checked'},),
        promotion_target='lesson', asset_refs=('script:owner:sample',)))
    approved = replace(candidate, status='approved')
    repo = LessonRepository(tmp_path / 'memory' / 'lessons', tmp_path / 'memory' / 'routing' / 'INDEX.md')
    lesson = repo.promote(approved)
    assert repo.get(lesson.lesson_id).asset_refs == ('script:owner:sample',)
    path = tmp_path / lesson.path
    first, rest = path.read_text().split('\n', 1)
    meta = json.loads(first.removeprefix('<!-- my-agent-lesson-meta:').removesuffix(' -->'))
    meta.pop('asset_refs')
    path.write_text('<!-- my-agent-lesson-meta:' + json.dumps(meta) + ' -->\n' + rest)
    assert repo.get(lesson.lesson_id).asset_refs == ()


# LLM: 一次失败修复成功链可生成候选，但不能改正式描述符或伪造第二次独立观察。
# 函数用途: 验证单次例外、sidecar位置与重复后台幂等。
def test_route3_one_occurrence_sidecar_only(tmp_path):
    store = ScriptStore(tmp_path)
    before = store.save_script(AssetDescriptor('sample', 'script', 'sample', metadata={'keep': True}),
                               'def main(inputs):\n return {"result": "04:27"}\n')
    target = verified_target(before['code'], bug_fix=True)
    target.update(asset_id='sample', bug_fix=True)
    request = {'run_id': 'run-one', 'thread_id': 'thread-one', 'created_at': datetime.now(timezone.utc).isoformat(), 'evidence_tags': []}
    service = CandidateService(tmp_path / 'memory' / 'candidates.jsonl')
    observed = []

    # LLM: sink只接入唯一CandidateService，避免测试绕过原候选持久化。
    # 函数用途: 记录真实候选。
    def sink(observation):
        candidate = service.observe(observation)
        observed.append(candidate)
        return candidate
    result = route_evolution_lesson(tmp_path, request, target, lesson_sink=sink)
    assert result['status'] == 'lesson_candidate'
    assert observed[0].occurrence_count == 1 and observed[0].asset_refs == ['script:owner:sample']
    route_evolution_lesson(tmp_path, request, target, lesson_sink=sink)
    assert observed[-1].occurrence_count == 1
    after = store.get_script('sample')
    assert after['revision'] == before['revision'] and after['metadata'] == before['metadata']
    pitfalls = read_known_pitfalls(tmp_path, 'script', 'sample')
    assert len(pitfalls) == 1 and pitfalls[0]['source'] == 'route3'
    assert route_evolution_lesson(tmp_path, {**request, 'evidence_tags': ['query_failed']}, target, lesson_sink=sink) is None
