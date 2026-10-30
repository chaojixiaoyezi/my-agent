# LLM: 在pytest临时owner走真实候选追加及原线程CAS；冷引用是完整性证据，不是另一份提交权威。
# 模块用途: 核对操作证据、供应商密文的无损外置、封印、预算和损坏拒绝。
from __future__ import annotations

import json

import pytest

from agent_py_agent.agent.conversation import compact_checkpoint as checkpoints
from agent_py_agent.agent.conversation.compact_remote import provider_compaction_record
from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE
from agent_py_agent.agent.conversation.compact_summary_view import resolve_compact_summary_view
from agent_py_agent.agent.conversation.models import ConversationCompactCommit
from agent_py_agent.tests.test_compact_scoped_checkpoint import (
    _checkpoint_path,
    _message,
)
from agent_py_agent.tests.test_compact_scoped_checkpoint import compact_case as _compact_case

compact_case = _compact_case


# LLM: 后端只提供原兼容性事实；不调用网络，密文采用明确的合成测试串。
# 函数用途: 用真实writer和CAS保存带大型载荷的检查点，再返回原始热行及完整来源。
def _offloaded(case):
    case.agent.backend.provider_compaction_scope = lambda: {
        'protocol': 'openai_responses', 'endpoint': 'https://provider.invalid/v1',
    }
    cipher = provider_compaction_record(case.agent.backend, {
        'type': 'compaction', 'encrypted_content': '合成密文' * 22000,
    })
    evidence = {'schema': 'test-evidence', 'proof': '程序账本' * 10000, 'counts': {'succeeded': 13}}
    thread = case.store.threads.require(case.thread_id)
    message = _message(case, '需要保留完整证据')
    checkpoint_id = checkpoints.write_compact_checkpoint(case.agent, checkpoints.CompactCheckpointRequest(
        thread=thread, summary='服务端摘要', operation_evidence=evidence, compact_rows=(message,),
        retained_tail=(), source_end_byte_offset=100, projected_tokens_before=900,
        projected_tokens_after=300, policy=case.policy, forced=False, provider_compaction=cipher,
    ))
    thread = case.store.threads.update_compact_state(case.thread_id, commit=ConversationCompactCommit(
        summary='服务端摘要', operation_evidence=evidence, checkpoint_id=checkpoint_id,
        compacted_through_message_id=message.message_id, compacted_through_byte_offset=100,
        source_messages=1, source_tool_pairs=0,
    ), expected_generation=0)
    raw = json.loads(_checkpoint_path(case).read_text())
    return thread, raw, evidence, cipher


# LLM: 热账本不保存重载荷；显式完整链及选中的摘要视图都必须恢复原密文和证据而非占位符。
# 函数用途: 对比落盘热行与冷载荷水合后的真实值及私有权限。
def test_heavy_evidence_and_provider_payload_are_cold_and_lossless(compact_case):
    case = compact_case
    thread, raw, evidence, cipher = _offloaded(case)
    assert 'operation_evidence' not in raw and 'provider_compaction' not in raw
    for name in ('operation_evidence_ref', 'provider_compaction_ref'):
        ref = raw[name]
        assert len(ref['sha256']) == 64 and ref['byte_length'] > 100_000
        path = case.root / 'compact' / 'evidence' / case.thread_id / ref['ref']
        assert path.stat().st_mode & 0o777 == 0o600
    assert _checkpoint_path(case).stat().st_size < 16384
    full = checkpoints.committed_compact_checkpoint_chain(case.agent, thread)[-1]
    assert full['operation_evidence'] == evidence and full['provider_compaction'] == cipher
    view = resolve_compact_summary_view(case.agent, thread, THREAD_COMPACT_SCOPE)
    assert view.operation_evidence == evidence and view.provider_compaction == cipher


# LLM: 引用digest、长度和路径都纳入原v3内容身份；不允许通过修改旁车引用更换证据或扩大读路径。
# 函数用途: 验证新冷引用被原checkpoint_id封印。
@pytest.mark.parametrize('name', ['operation_evidence_ref', 'provider_compaction_ref'])
def test_cold_reference_is_bound_to_checkpoint_identity(compact_case, name):
    case = compact_case
    thread, raw, _, _ = _offloaded(case)
    assert name in raw
    raw[name]['sha256'] = '0' * 64
    _checkpoint_path(case).write_text(json.dumps(raw) + '\n')
    with pytest.raises(OSError):
        checkpoints.committed_compact_checkpoint_chain(case.agent, thread)


# LLM: 冷目录内容缺失、改写或链接均是损坏，不降级为空证据或丢弃密文；全路径遵循no-follow。
# 函数用途: 对真实外置文件作故障注入，确认读取明确拒绝。
@pytest.mark.parametrize('fault', ['corrupt', 'missing', 'symlink'])
def test_cold_payload_fault_fails_closed(compact_case, fault):
    case = compact_case
    thread, raw, _, _ = _offloaded(case)
    assert 'provider_compaction_ref' in raw
    ref = raw['provider_compaction_ref']
    path = case.root / 'compact' / 'evidence' / case.thread_id / ref['ref']
    if fault == 'corrupt':
        path.write_text('{}\n')
    else:
        saved = path.with_suffix('.saved')
        path.rename(saved)
        if fault == 'symlink':
            path.symlink_to(saved)
    with pytest.raises(OSError):
        resolve_compact_summary_view(case.agent, thread, THREAD_COMPACT_SCOPE)


# LLM: writer预算与原scanner单行上限一致；拒绝发生在候选追加和线程CAS前，不能产生不可读已提交头。
# 函数用途: 回归超长摘要在写入侧被阻止，原热账本和线程头不变。
@pytest.mark.parametrize('summary_bytes', [3 * 1024 * 1024, 8 * 1024 * 1024])
def test_checkpoint_line_budget_is_checked_before_candidate_publish(compact_case, summary_bytes):
    case = compact_case
    thread = case.store.threads.require(case.thread_id)
    message = _message(case, '短原文')
    request = checkpoints.CompactCheckpointRequest(
        thread=thread, summary='x' * summary_bytes, operation_evidence={}, compact_rows=(message,),
        retained_tail=(), source_end_byte_offset=100, projected_tokens_before=900,
        projected_tokens_after=300, policy=case.policy, forced=False,
    )
    with pytest.raises((ValueError, OSError), match='byte budget'):
        checkpoints.write_compact_checkpoint(case.agent, request)
    assert not _checkpoint_path(case).exists()
    current = case.store.threads.require(case.thread_id)
    assert (current.compact_checkpoint_id, current.compact_generation) == ('', 0)
