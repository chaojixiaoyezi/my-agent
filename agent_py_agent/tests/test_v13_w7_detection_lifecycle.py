"""检测生命周期的真实 HTTP 往返、耐久事务、回放与异步回写回归。"""
from __future__ import annotations

import ast
import copy
import json
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.business_spaces import v13_w7_detection_lifecycle as lifecycle
from agent_py_agent.agent.business_spaces.v13_store import V13Store
from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.agent.gateway_parts.business_api_v13 import V13Call
from agent_py_agent.agent.plugin_host.contracts import DomainResult
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.tests import test_v13_w0_harness as harness

v13_native = harness.v13_native
v13_host = harness.v13_host


# LLM: 每次都由真实鉴权 HTTP 消费请求，再验证标准信封。
# 函数用途: 发送业务请求并核验成功或失败状态。
def _send(native, action, params, *, key=None, revision=None, user='business-alice', status=200):
    code, body = harness.v13_call(native, action, params, user=user,
        idempotency_key=key, expected_revision=revision)
    assert code == status, body
    assert body['state'] == ('accepted' if status == 202 else 'completed' if status == 200 else 'failed'), body
    if status in {200, 202}:
        assert body['error'] is None, body
    else:
        assert body['data'] == {}, body
    return body


# LLM: 空间由冻结真实入口创建，不预置虚构业务空间。
# 函数用途: 创建当前 HTTP 用户的测试空间。
def _space(native):
    token = uuid.uuid4().hex
    return _send(native, 'spaces.create', {'name': '检测空间-' + token},
                 key='lifecycle-space-' + token)['data']['space_id']


# LLM: 测试草案是输入，不是替代服务器返回的假响应。
# 函数用途: 组装合法的规则草案。
def _draft():
    return {'name': '固定规则', 'severity': 'medium', 'query': {'language': 'nl', 'text': '固定查询'}}


# LLM: 原生入口必须实际保存 pending 提案。
# 函数用途: 创建可继续评审的提案。
def _proposal(native, sid, *, key=None):
    data = _send(native, 'detection_proposals.create',
        {'space_id': sid, 'title': '检测提案', 'rule': _draft(), 'mitre_techniques': ['T1059']},
        key=key or 'proposal-create-' + uuid.uuid4().hex)['data']
    return data['proposal']


# LLM: 规则由 monitors 原生创建，测试不通过手写 JSON 绕过其领域检查。
# 函数用途: 创建可供调优与基线关联的规则。
def _rule(native, sid, *, scheduled=False):
    params = {**_draft(), 'space_id': sid}
    if scheduled:
        params.update(enabled=True, schedule={'kind': 'every', 'every_seconds': 60})
    return _send(native, 'monitors.rules.create', params,
                 key='monitor-create-' + uuid.uuid4().hex)['data']['rule']


# LLM: owner 路径从已真实解析的 HTTP 服务中取，不按用户名拼路径。
# 函数用途: 取得本测试唯一调用者的 owner Store。
def _store(native):
    services = list(native[0]._business_services.values())
    assert len(services) == 1
    return V13Store(services[0].owner_home, owner_id=services[0].owner_id)


# LLM: W8 种子完整使用真实字段名和规范实体结构。
# 函数用途: 生成一条可被聚合的规范告警。
def _alert(owner, sid, rule_id, alert_id, *, value='host-a', at='2026-10-01T01:00:00+00:00'):
    return {'alert_id': alert_id, 'owner_id': owner, 'space_id': sid, 'rule_id': rule_id,
        'title': '检测事件', 'description': '', 'severity': 'medium', 'status': 'new',
        'verdict': 'true_positive', 'ai_confidence': None, 'disposition': None, 'source': 'monitor',
        'external_ref': None, 'entities': [{'type': 'host', 'value': value}], 'techniques': ['T1059'],
        'assignee': None, 'case_id': None, 'tags': [], 'evidence_refs': [], 'acknowledged_at': None,
        'resolved_at': None, 'revision': 1, 'created_at': at, 'updated_at': at}


# LLM: 唯一可控信号是 override_verdict，不增加虚构 verdict 或 entity 字段。
# 函数用途: 生成一条真实 W8 形状反馈。
def _feedback(alert, override_id, *, verdict='false_positive', at='2026-10-01T02:00:00+00:00'):
    return {'override_id': override_id, 'alert_id': alert['alert_id'], 'owner_id': alert['owner_id'],
        'space_id': alert['space_id'], 'rule_id': alert['rule_id'], 'original_verdict': 'true_positive',
        'override_verdict': verdict, 'reason': '分析师复核', 'analyst': 'analyst-a', 'source': 'override',
        'created_at': at, 'updated_at': at, 'revision': 1}


# LLM: 使用真实 Store 事务种子，不手写底座文件或回执。
# 函数用途: 保存告警和反馈以驱动 HTTP 调优。
def _seed_feedback(native, sid, rule_id, *, count=2):
    store = _store(native)
    alerts = [_alert(store.owner_id, sid, rule_id, 'alert-' + str(index), value='host-' + str(index))
              for index in range(count)]
    harness.v13_seed(store.owner_home, 'soc_alerts', {row['alert_id']: row for row in alerts})
    facts = [_feedback(row, 'override-' + str(index)) for index, row in enumerate(alerts)]
    harness.v13_seed(store.owner_home, 'soc_feedback', {row['override_id']: row for row in facts})
    return alerts


# LLM: 最小直接调用仍使用真实签发上下文和真实空间，只替换注入时钟。
# 函数用途: 提供事务失败与异步回写的隔离领域夹具。
@pytest.fixture
def domain(v13_host):
    from agent_py_agent.agent.user_space.owner_resolver import (
        home_paths_with_owner,
        resolve_owner_home,
    )
    v13_host.home_paths = home_paths_with_owner(v13_host.home_paths, resolve_owner_home(v13_host.home_paths.root))
    context, services = api.local_business_context(v13_host)
    request = {'action': 'spaces.create', 'request_id': 'domain-space', 'params': {'name': '事务空间'},
               'idempotency_key': 'domain-space-create-key'}
    code, body = api.execute_business(request, context, services)
    assert code == 200, body
    sid = body['data']['space_id']

    # LLM: 身份和 owner_home 来自签发上下文，不能从调用 params 伪造。
    # 函数用途: 构造可信领域调用对象。
    def make(action, params, *, key='domain-request-key', revision=None):
        return V13Call(action, params, 'domain-request', context.owner_id, context.subject_id,
            context, services, services.owner_home, key, revision, lambda: 1793670000.0)

    return make, sid


# LLM: 原 issue 示例无需默认空间解析，也不许空成功掩盖未保存提案。
# 函数用途: 验证 pending 列表、过滤、分页游标及失效条件。
def test_list_example_status_pending_without_space_id(v13_native):
    sid = _space(v13_native)
    first = _proposal(v13_native, sid)
    _proposal(v13_native, sid)
    data = _send(v13_native, 'detection_proposals.list', {'status': 'pending', 'limit': 1})['data']
    assert len(data['items']) == 1 and data['next_cursor'] is not None
    second = _send(v13_native, 'detection_proposals.list',
        {'status': 'pending', 'limit': 1, 'cursor': data['next_cursor']})['data']
    assert len(second['items']) == 1 and data['items'] != second['items']
    assert first in data['items'] + second['items']
    failure = _send(v13_native, 'detection_proposals.list',
        {'status': 'approved', 'cursor': data['next_cursor']}, status=503)
    assert failure['error']['code'] == 'CURSOR_EXPIRED'


# LLM: 同键等参回放必须保持完整业务结果，换参必须明确冲突。
# 函数用途: 验证创建状态和幂等冲突。
def test_create_starts_pending(v13_native):
    sid = _space(v13_native)
    first = _proposal(v13_native, sid, key='proposal-fixed-create-key')
    assert first['status'] == 'pending' and first['promoted_rule_id'] is None
    assert _proposal(v13_native, sid, key='proposal-fixed-create-key') == first
    bad = _send(v13_native, 'detection_proposals.create',
        {'space_id': sid, 'title': '其它提案', 'rule': _draft()}, key='proposal-fixed-create-key', status=409)
    assert bad['error']['code'] == 'IDEMPOTENCY_CONFLICT'


# LLM: 缺省只能选择当前 owner 唯一活动空间，零个和多个候选都不能猜测。
# 函数用途: 经真实 HTTP 验证可选空间创建的明确解析策略。
@pytest.mark.parametrize('count,status,code', [(0, 404, 'RESOURCE_NOT_FOUND'), (1, 200, None), (2, 409, 'STATE_CONFLICT')])
def test_create_optional_space_uses_unique_active_space(v13_native, count, status, code):
    spaces = [_space(v13_native) for _index in range(count)]
    result = _send(v13_native, 'detection_proposals.create', {'title': '缺省空间提案', 'rule': _draft()},
        key='default-space-create-key', status=status)
    if status == 200:
        assert result['data']['proposal']['space_id'] == spaces[0]
    else:
        assert result['error']['code'] == code


# LLM: 缺记录与跨 owner 都是 404，不泄露提案存在性。
# 函数用途: 验证 get 的可见性边界。
def test_get_and_404(v13_native):
    row = _proposal(v13_native, _space(v13_native))
    assert _send(v13_native, 'detection_proposals.get', {'proposal_id': row['proposal_id']})['data']['proposal'] == row
    for proposal_id, user in [('absent', 'business-alice'), (row['proposal_id'], 'business-bob')]:
        body = _send(v13_native, 'detection_proposals.get', {'proposal_id': proposal_id}, user=user, status=404)
        assert body['error']['code'] == 'RESOURCE_NOT_FOUND'


# LLM: 同键重放不得增加第二条评论或改写 author。
# 函数用途: 验证评论原子追加与作者绑定。
def test_comment_appends_with_author(v13_native):
    row = _proposal(v13_native, _space(v13_native))
    params = {'proposal_id': row['proposal_id'], 'text': '已核对数据源'}
    data = _send(v13_native, 'detection_proposals.comment', params, key='proposal-comment-key')['data']
    assert data['proposal']['comments'][0]['author'] == row['author']
    assert len(data['proposal']['comments']) == 1
    assert _send(v13_native, 'detection_proposals.comment', params, key='proposal-comment-key')['data'] == data


# LLM: 记录 CAS 不得与集合版本混淆；其它提案写入不能使本记录 CAS 失效。
# 函数用途: 验证评审转换、旧版本拒绝与 rejected 终态。
def test_decide_transitions_and_terminal_conflict(v13_native):
    sid = _space(v13_native)
    row = _proposal(v13_native, sid)
    _proposal(v13_native, sid)
    params = {'proposal_id': row['proposal_id'], 'decision': 'request_changes'}
    first = _send(v13_native, 'detection_proposals.decide', params,
        key='proposal-decide-changes', revision=row['revision'])['data']['proposal']
    assert first['status'] == 'changes_requested'
    stale = _send(v13_native, 'detection_proposals.decide', params,
        key='proposal-decide-stale', revision=row['revision'], status=409)
    assert stale['error']['code'] == 'REVISION_CONFLICT'
    rejected = _send(v13_native, 'detection_proposals.decide', {**params, 'decision': 'reject'},
        key='proposal-decide-reject', revision=first['revision'])['data']['proposal']
    failed = _send(v13_native, 'detection_proposals.decide', {**params, 'decision': 'approve'},
        key='proposal-decide-terminal', revision=rejected['revision'], status=409)
    assert failed['error']['code'] == 'STATE_CONFLICT'


# LLM: 晋升必须创建原生可读规则，且同键回放不能重复创建。
# 函数用途: 验证批准前门禁、origin、CAS 和正式晋升。
def test_promote_requires_approved_and_creates_monitor_rule_once(v13_native):
    sid = _space(v13_native)
    row = _proposal(v13_native, sid)
    params = {'proposal_id': row['proposal_id']}
    failed = _send(v13_native, 'detection_proposals.promote', params,
        key='promotion-unapproved-key', revision=row['revision'], status=409)
    assert failed['error']['code'] == 'STATE_CONFLICT'
    approved = _send(v13_native, 'detection_proposals.decide', {**params, 'decision': 'approve'},
        key='promotion-approve-key', revision=row['revision'])['data']['proposal']
    result = _send(v13_native, 'detection_proposals.promote', params,
        key='promotion-success-key', revision=approved['revision'])['data']
    assert result['proposal']['status'] == 'promoted'
    assert result['rule']['origin'] == 'proposal'
    assert result['proposal']['promoted_rule_id'] == result['rule']['rule_id']
    assert _send(v13_native, 'detection_proposals.promote', params,
        key='promotion-success-key', revision=approved['revision'])['data'] == result
    listed = _send(v13_native, 'monitors.rules.list', {'space_id': sid})['data']['items']
    assert listed == [result['rule']]


# LLM: 只按 override_verdict 和规范 entities 推导建议，不采信展示 verdict。
# 函数用途: 验证真实误报证据、最新改判去重及稳定编号。
def test_tuning_suggestions_derive_from_false_positive_feedback(v13_native):
    sid = _space(v13_native)
    rule = _rule(v13_native, sid)
    alerts = _seed_feedback(v13_native, sid, rule['rule_id'])
    first = _send(v13_native, 'detection_tuning.list', {'rule_id': rule['rule_id']})['data']
    assert len(first['items']) == 2
    assert {row['field'] for row in first['items']} == {'host'}
    assert {row['value'] for row in first['items']} == {'host-0', 'host-1'}
    assert all(row['false_positive_count'] == 1 for row in first['items'])
    store = _store(v13_native)
    before = store.revision('detection_tuning')
    assert _send(v13_native, 'detection_tuning.list', {'rule_id': rule['rule_id']})['data'] == first
    assert store.revision('detection_tuning') == before
    later = _feedback(alerts[0], 'override-later', verdict='true_positive', at='2026-10-02T01:00:00+00:00')
    harness.v13_seed(store.owner_home, 'soc_feedback', {later['override_id']: later})
    second = _send(v13_native, 'detection_tuning.list', {'rule_id': rule['rule_id']})['data']['items']
    assert len(second) == 1 and second[0]['value'] == 'host-1'


# LLM: 摘要计数来自当前真实建议，预计降噪没有回测分母时必须为 null。
# 函数用途: 验证建议统计不编造降噪效果。
def test_tuning_summary_counts(v13_native):
    sid = _space(v13_native)
    rule = _rule(v13_native, sid)
    _seed_feedback(v13_native, sid, rule['rule_id'])
    summary = _send(v13_native, 'detection_tuning.summary', {'space_id': sid})['data']
    assert summary == {'rules_with_suggestions': 1, 'open': 2, 'applied': 0, 'dismissed': 0,
                       'estimated_alert_reduction': None, 'coverage': 'complete'}


# LLM: stale CAS 不能附加排除项；成功后规则和建议通过同一 HTTP 入口回读。
# 函数用途: 验证调优记录 CAS、source、原子状态和幂等重放。
def test_apply_adds_exclusion_with_rule_cas(v13_native):
    sid = _space(v13_native)
    rule = _rule(v13_native, sid)
    _seed_feedback(v13_native, sid, rule['rule_id'], count=1)
    suggestion = _send(v13_native, 'detection_tuning.list', {'rule_id': rule['rule_id']})['data']['items'][0]
    params = {'rule_id': rule['rule_id'], 'suggestion_id': suggestion['suggestion_id']}
    stale = _send(v13_native, 'detection_tuning.apply', params, key='tuning-apply-stale-key',
                  revision=rule['revision'] + 1, status=409)
    assert stale['error']['code'] == 'REVISION_CONFLICT'
    first = _send(v13_native, 'detection_tuning.apply', params, key='tuning-apply-success',
                  revision=rule['revision'])['data']
    assert first['suggestion']['status'] == 'applied'
    assert len(first['rule']['exclusions']) == 1
    assert first['rule']['exclusions'][0]['source'] == 'tuning'
    assert first['rule']['exclusions'][0]['value'] == suggestion['value']
    assert _send(v13_native, 'detection_tuning.apply', params, key='tuning-apply-success',
                 revision=rule['revision'])['data'] == first
    assert _send(v13_native, 'monitors.rules.get', {'rule_id': rule['rule_id']})['data']['rule'] == first['rule']


# LLM: 调优后的原生调度必须包含排除项，不能只改 monitor_rules JSON 让执行仍用旧提示词。
# 函数用途: 经真实 HTTP 和 SchedulerRepository 验证调优原生镜像重绘。
def test_apply_rerenders_native_scheduler_instruction(v13_native):
    from agent_py_agent.agent.assets.script_store_support import delivery_scheduler
    sid = _space(v13_native)
    rule = _rule(v13_native, sid, scheduled=True)
    assert rule['scheduler_job_id'] is not None
    _seed_feedback(v13_native, sid, rule['rule_id'], count=1)
    row = _send(v13_native, 'detection_tuning.list', {'rule_id': rule['rule_id']})['data']['items'][0]
    store = _store(v13_native)
    scheduler = delivery_scheduler(store.owner_home, store.owner_id)
    before = scheduler.get_job(rule['scheduler_job_id'])
    result = _send(v13_native, 'detection_tuning.apply',
        {'rule_id': rule['rule_id'], 'suggestion_id': row['suggestion_id']},
        key='tuning-scheduler-instruction', revision=rule['revision'])['data']
    after = scheduler.get_job(rule['scheduler_job_id'])
    assert after['job_id'] == result['rule']['scheduler_job_id']
    assert after['version'] > before['version']
    assert json.loads(after['prompt'].partition('\n')[2])['exclusions'] == result['rule']['exclusions']


# LLM: 后续新增同实体证据也不能使 dismissed 重开。
# 函数用途: 验证 sticky dismissed、无重复读写及禁止再次应用。
def test_dismiss_is_sticky(v13_native):
    sid = _space(v13_native)
    rule = _rule(v13_native, sid)
    alerts = _seed_feedback(v13_native, sid, rule['rule_id'], count=1)
    row = _send(v13_native, 'detection_tuning.list', {'rule_id': rule['rule_id']})['data']['items'][0]
    params = {'rule_id': rule['rule_id'], 'suggestion_id': row['suggestion_id'], 'reason': '已确认业务用途'}
    first = _send(v13_native, 'detection_tuning.dismiss', params, key='tuning-dismiss-key')['data']
    assert first['suggestion']['status'] == 'dismissed'
    store = _store(v13_native)
    new_alert = {**alerts[0], 'alert_id': 'alert-new'}
    new_feedback = _feedback(new_alert, 'override-new')
    harness.v13_seed(store.owner_home, 'soc_alerts', {'alert-new': new_alert})
    harness.v13_seed(store.owner_home, 'soc_feedback', {'override-new': new_feedback})
    revision = store.revision('detection_tuning')
    rows = _send(v13_native, 'detection_tuning.list', {'rule_id': rule['rule_id']})['data']['items']
    assert rows == [first['suggestion']]
    assert store.revision('detection_tuning') == revision
    failed = _send(v13_native, 'detection_tuning.apply',
        {'rule_id': rule['rule_id'], 'suggestion_id': row['suggestion_id']}, key='tuning-dismissed-apply',
        revision=rule['revision'], status=409)
    assert failed['error']['code'] == 'STATE_CONFLICT'


# LLM: 预览必须在建议尚未物化时也零写，不仅仅比较规则字段。
# 函数用途: 验证 auto_tune 全量应用与 dry_run 的全部业务文件零变化。
def test_auto_tune_applies_all_open_exclusions_or_previews(v13_native):
    sid = _space(v13_native)
    rule = _rule(v13_native, sid)
    _seed_feedback(v13_native, sid, rule['rule_id'])
    store = _store(v13_native)
    root = store.owner_home / 'data' / 'business_v13'
    before = {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}
    params = {'rule_id': rule['rule_id'], 'dry_run': True}
    preview = _send(v13_native, 'detection_tuning.auto_tune', params, key='auto-tune-preview-key',
        revision=rule['revision'])['data']
    assert preview['dry_run'] and preview['rule'] is None and len(preview['applied']) == 2
    after = {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}
    assert after == before
    first = _send(v13_native, 'detection_tuning.auto_tune', {**params, 'dry_run': False},
        key='auto-tune-apply-key', revision=rule['revision'])['data']
    assert len(first['applied']) == 2 and all(row['status'] == 'applied' for row in first['applied'])
    assert len(first['rule']['exclusions']) == 2
    assert {row['source'] for row in first['rule']['exclusions']} == {'auto_tune'}
    assert _send(v13_native, 'detection_tuning.auto_tune', {**params, 'dry_run': False},
        key='auto-tune-apply-key', revision=rule['revision'])['data'] == first


# LLM: 基线必须使用准确窗口与真实分母，缺失反馈不能被解释为零误报。
# 函数用途: 验证实算基线、坏数据排除及反馈最新改判去重。
def test_baselines_list_and_create(v13_native):
    sid = _space(v13_native)
    rule = _rule(v13_native, sid)
    alerts = _seed_feedback(v13_native, sid, rule['rule_id'])
    store = _store(v13_native)
    broken = {**alerts[0], 'alert_id': 'broken', 'created_at': None}
    harness.v13_seed(store.owner_home, 'soc_alerts', {'broken': broken, 'duplicate': alerts[0]})
    last = _feedback(alerts[1], 'override-final', verdict='true_positive', at='2026-10-01T03:00:00+00:00')
    harness.v13_seed(store.owner_home, 'soc_feedback', {'override-final': last})
    window = {'since': '2026-10-01T00:00:00+00:00', 'until': '2026-10-03T00:00:00+00:00'}
    params = {'space_id': sid, 'rule_id': rule['rule_id'], 'window': window, 'metric': 'alerts_per_day'}
    first = _send(v13_native, 'detection_proposals.baselines.create', params, key='baseline-alert-rate')['data']['baseline']
    assert first['value'] == 1.0
    ratio = _send(v13_native, 'detection_proposals.baselines.create', {**params, 'metric': 'fp_ratio'},
        key='baseline-fp-ratio')['data']['baseline']
    assert ratio['value'] == 0.5
    assert _send(v13_native, 'detection_proposals.baselines.create', params,
        key='baseline-alert-rate')['data']['baseline'] == first
    listed = _send(v13_native, 'detection_proposals.baselines.list', {'space_id': sid})['data']['items']
    assert len(listed) == 2 and first in listed and ratio in listed
    bad = _send(v13_native, 'detection_proposals.baselines.create',
        {**params, 'window': {'since': window['until'], 'until': window['since']}}, key='baseline-bad-window', status=400)
    assert bad['error']['code'] == 'INVALID_SCHEMA'


class _Catalog:
    """宿主协议替身只提供合成来源，不替业务 handler 或 HTTP 编造结果。"""

    # LLM: 数据仅为 provider 输入，不是网关响应。
    # 函数用途: 初始化有限合成目录与调用计数。
    def __init__(self):
        self.calls = []
        self.items = [{'item_id': 'community-' + str(index), 'kind': 'detection', 'title': '社区规则 ' + str(index),
            'summary': '固定 Sigma 规则', 'author': None, 'tags': ['endpoint'], 'format': 'sigma',
            'version': '1', 'updated_at': '2026-10-01T00:00:00+00:00'} for index in range(2)]

    # LLM: 仅实现本任务约定的来源 list 协议，不访问网络。
    # 函数用途: 返回一页合成目录输入。
    def list(self, kind, *, query, tag, cursor, limit):
        self.calls.append(('list', kind, query, tag, cursor, limit))
        offset = 0 if cursor is None else int(cursor)
        rows = self.items[offset:offset + limit]
        return {'items': copy.deepcopy(rows),
                'next_cursor': str(offset + limit) if offset + limit < len(self.items) else None}

    # LLM: 正文由 provider 保持原字节语义，安装 handler 不解析部分 YAML。
    # 函数用途: 按规范 ID 返回元数据和 Sigma 文本。
    def get(self, kind, item_id):
        self.calls.append(('get', kind, item_id))
        row = next((row for row in self.items if row['item_id'] == item_id), None)
        if row is None:
            return None
        return {'item': copy.deepcopy(row), 'document': 'title: Fixed\ndetection:\n  condition: selection\n',
                'metadata': {'severity': 'high'}}


# LLM: 所有读取都经过 operator 显式绑定的 provider，未绑定不得返回空列表。
# 函数用途: 验证目录依赖、分页和准确查询条件。
def test_community_list_requires_provider_and_pages(domain):
    make, _sid = domain
    call = make('community_detections.list', {'limit': 1, 'query': '终端', 'tag': 'endpoint'})
    with pytest.raises(HostAPIError) as caught:
        lifecycle.list_community(call)
    assert caught.value.code == 'HOST_NOT_READY'
    provider = _Catalog()
    call.context.host.business_v13_providers = {'community_catalog': provider}
    first = lifecycle.list_community(call)
    second = lifecycle.list_community(replace(call, params={**call.params, 'cursor': first['next_cursor']}))
    assert first['next_cursor'] == '1' and second['next_cursor'] is None
    assert first['items'] != second['items']
    assert provider.calls[0] == ('list', 'detection', '终端', 'endpoint', None, 1)


# LLM: Sigma 文本作为查询原样传递，缺 ID 保持 404。
# 函数用途: 验证社区条目到闭合 RuleDraft 的转换。
def test_community_get_converts_sigma_to_rule_draft(domain):
    make, _sid = domain
    call = make('community_detections.get', {'detection_id': 'community-0'})
    call.context.host.business_v13_providers = {'community_catalog': _Catalog()}
    result = lifecycle.get_community(call)
    assert result['rule']['query'] == {'language': 'sigma', 'text': 'title: Fixed\ndetection:\n  condition: selection\n'}
    assert result['rule']['severity'] == 'high'
    with pytest.raises(BusinessError) as caught:
        lifecycle.get_community(replace(call, params={'detection_id': 'absent'}))
    assert caught.value.code == 'RESOURCE_NOT_FOUND'


# LLM: 重放不再次读取可能已被移除的 provider 文档，也不重复创建原生规则。
# 函数用途: 验证社区安装默认禁用、origin 和文档快照幂等。
def test_install_creates_disabled_by_default_rule_once(domain):
    make, sid = domain
    call = make('community_detections.install', {'space_id': sid, 'detection_id': 'community-0'})
    provider = _Catalog()
    call.context.host.business_v13_providers = {'community_catalog': provider}
    first = lifecycle.install_community(call)
    assert first['rule']['enabled'] is False and first['rule']['origin'] == 'community'
    provider.items = []
    assert lifecycle.install_community(call) == first
    assert len(provider.calls) == 1
    assert len(V13Store(call.owner_home).list('monitor_rules')) == 1


# LLM: 无 provider 的动作既不能执行，也不能通过扩展目录声称已就绪。
# 函数用途: 经真实 HTTP 验证社区依赖拒绝与发现行为。
def test_unbound_community_absent_from_discovery(v13_native):
    data = _send(v13_native, 'system.capabilities.extensions', {})['data']
    assert not any(name.startswith('community_detections.') for name in data['supported_actions'])
    body = _send(v13_native, 'community_detections.list', {}, status=503)
    assert body['error']['code'] == 'HOST_NOT_READY'


# LLM: 单元边界只替换异步派发端口；目标预留、状态转换与回写都使用真实 Store。
# 函数用途: 验证三种评估模式、批量 pending 选择与同键不扩批。
def test_evaluate_modes_and_run_eval_batch(domain, monkeypatch):
    make, sid = domain
    dispatched = []

    # LLM: 替身只模拟受理 ID，不返回任何评估业务结论。
    # 函数用途: 记录真实输入并提供稳定作业标识。
    def start(call, kind, instruction, *, space_id=None):
        dispatched.append((call, kind, instruction, space_id))
        return DomainResult(202, {'state': 'accepted', 'operation_id': 'operation-' + call.idempotency_key,
            'job_id': lifecycle._operation_id(call, 'job'), 'task_id': 'task-' + call.idempotency_key})

    monkeypatch.setattr(lifecycle._api(), 'v13_start_model_job', start)
    for index, mode in enumerate(('eval', 'rule', 'backtest')):
        row = lifecycle.create_proposal(make('detection_proposals.create',
            {'space_id': sid, 'title': mode, 'rule': _draft()}, key='mode-create-' + str(index)))['proposal']
        params = {'space_id': sid, 'proposal_id': row['proposal_id'], 'mode': mode}
        if mode == 'backtest':
            params['window'] = {'since': '2026-10-01T00:00:00+00:00', 'until': '2026-10-02T00:00:00+00:00'}
        result = lifecycle.evaluate(make('detection_proposals.evaluate', params, key='mode-eval-' + str(index)))
        assert result.http_status == 202 and len(result.data['job_ids']) == 1
        current = lifecycle.get_proposal(make('detection_proposals.get', {'proposal_id': row['proposal_id']}))['proposal']
        assert current['status'] == 'evaluating' and current['evaluations'][0]['mode'] == mode
    pending = lifecycle.create_proposal(make('detection_proposals.create',
        {'space_id': sid, 'title': '批量', 'rule': _draft()}, key='batch-proposal-first'))['proposal']
    batch = make('detection_proposals.evaluate', {'space_id': sid}, key='batch-evaluation-key')
    first = lifecycle.evaluate(batch)
    assert len(first.data['job_ids']) == 1
    assert pending['proposal_id'] in dispatched[-1][2]
    lifecycle.create_proposal(make('detection_proposals.create',
        {'space_id': sid, 'title': '后来新增', 'rule': _draft()}, key='batch-proposal-later'))
    assert lifecycle.evaluate(batch).data == first.data
    assert len(first.data['job_ids']) == 1
    implicit = lifecycle.evaluate(make('detection_proposals.evaluate', {}, key='batch-default-space-key'))
    assert len(implicit.data['job_ids']) == 1
    assert dispatched[-1][0].params['space_id'] == sid and dispatched[-1][3] == sid


# LLM: 测试 W0 真实派发链的受理结果，不用单元替身冒充端到端。
# 函数用途: 验证真实 HTTP 评估返回 accepted 和稳定作业编号。
def test_evaluate_accepts_through_real_http(v13_native, monkeypatch):
    import traceback

    from agent_py_agent.agent.conversation import session_task_dispatch
    frozen = lifecycle._api().v13_call_frozen
    original_report = session_task_dispatch.runtime_error_report
    failures = []

    # LLM: 仅观察原异常的真实 traceback，错误分类和回执仍由原报告函数生成。
    # 函数用途: 保存派发内部捕获的原始失败位置。
    def report(error, *, context):
        failures.append({'dispatch_traceback': ''.join(traceback.format_exception(error))})
        return original_report(error, context=context)

    # LLM: 观察器仍执行原调用、原参数和原结果，只给失败附上准确边界证据。
    # 函数用途: 捕获真实原生调用失败位置而不替换业务行为。
    def observe(call, action, params, *, idempotency_key=None, expected_revision=None):
        try:
            return frozen(call, action, params, idempotency_key=idempotency_key,
                          expected_revision=expected_revision)
        except (HostAPIError, BusinessError, OSError, ValueError, TypeError, RuntimeError) as error:
            failures.append({'action': action, 'traceback': traceback.format_exc(),
                             'outcome': repr(getattr(error, 'outcome', None))})
            raise

    monkeypatch.setattr(lifecycle._api(), 'v13_call_frozen', observe)
    monkeypatch.setattr(session_task_dispatch, 'runtime_error_report', report)
    sid = _space(v13_native)
    row = _proposal(v13_native, sid)
    params = {'space_id': sid, 'proposal_id': row['proposal_id'], 'mode': 'eval'}
    try:
        first = _send(v13_native, 'detection_proposals.evaluate', params, key='http-evaluate-native-key', status=202)['data']
    except AssertionError as error:
        error.add_note(json.dumps(failures, ensure_ascii=False))
        raise
    assert len(first['job_ids']) == 1
    assert _send(v13_native, 'detection_proposals.evaluate', params,
        key='http-evaluate-native-key', status=202)['data'] == first


# LLM: 冻结契约是非空短字符串，不额外把业务技术标签收紧为 MITRE 正则。
# 函数用途: 验证合法自定义技术标签能创建和回读。
@pytest.mark.parametrize('technique', ['custom_technique', 'local_tag'])
def test_custom_technique_matches_frozen_contract(domain, technique):
    make, sid = domain
    row = lifecycle.create_proposal(make('detection_proposals.create',
        {'space_id': sid, 'title': '自定义标签', 'rule': _draft(),
         'mitre_techniques': [technique]}))['proposal']
    assert row['mitre_techniques'] == [technique]
    saved = lifecycle.get_proposal(make('detection_proposals.get', {'proposal_id': row['proposal_id']}))['proposal']
    assert saved == row


# LLM: 没有任何目标便没有真实 operation，不得返回凭空构造的 accepted 身份。
# 函数用途: 验证空评估批次明确拒绝且不派发模型。
def test_empty_evaluation_batch_does_not_fake_acceptance(domain, monkeypatch):
    make, sid = domain
    dispatched = []

    # LLM: 捕获是否调用派发，不能提供任何假业务结论。
    # 函数用途: 阻止空批次偷偷启动模型。
    def start(call, kind, instruction, *, space_id=None):
        dispatched.append(kind)
        raise AssertionError('空目标不得派发')

    monkeypatch.setattr(lifecycle._api(), 'v13_start_model_job', start)
    with pytest.raises(BusinessError) as caught:
        lifecycle.evaluate(make('detection_proposals.evaluate', {'space_id': sid}))
    assert caught.value.code == 'STATE_CONFLICT'
    assert dispatched == []


# LLM: 原规则和最多 200 建议共享同一 CAS 事务；重放不能再改 revision 或追加排除项。
# 函数用途: 经真实 HTTP 验证自动调优超过旧 100 条限制及 200 条契约边界。
@pytest.mark.parametrize('count', [101, 200])
def test_auto_tune_full_contract_batch_is_atomic_and_replay_safe(v13_native, count):
    sid = _space(v13_native)
    rule = _rule(v13_native, sid)
    _seed_feedback(v13_native, sid, rule['rule_id'], count=count)
    params = {'rule_id': rule['rule_id'], 'dry_run': False}
    first = _send(v13_native, 'detection_tuning.auto_tune', params,
        key='full-contract-batch-key', revision=rule['revision'])['data']
    assert len(first['applied']) == count
    saved = _store(v13_native).get('monitor_rules', rule['rule_id'])
    assert len(saved['exclusions']) == count and saved['revision'] == rule['revision'] + 1
    suggestions = _store(v13_native).list('detection_tuning')
    assert len(suggestions) == count and all(row['status'] == 'applied' for row in suggestions)
    assert _send(v13_native, 'detection_tuning.auto_tune', params,
        key='full-contract-batch-key', revision=rule['revision'])['data'] == first
    assert _store(v13_native).get('monitor_rules', rule['rule_id']) == saved


# LLM: 种子只建立 W0 已受理作业的关联，评估结论仍交被测回写函数验证。
# 函数用途: 准备一条待回写提案和规范作业终态。
def _evaluation_fixture(domain):
    make, sid = domain
    base = make('detection_proposals.create', {'space_id': sid, 'title': '评估', 'rule': _draft()})
    row = lifecycle.create_proposal(base)['proposal']
    stored = V13Store(base.owner_home).get('detection_proposals', row['proposal_id'])
    stored['status'] = 'evaluating'
    stored['evaluations'] = [{'job_id': 'job-evaluation', 'mode': 'eval', 'state': 'accepted',
                              'summary': None, 'at': row['created_at']}]
    harness.v13_seed(base.owner_home, 'detection_proposals', {row['proposal_id']: stored})
    job = {'job_id': 'job-evaluation', 'owner_id': base.owner_id, 'space_id': sid,
           'kind': 'detection_proposal_evaluation', 'state': 'completed', 'result': {
               'proposal_id': row['proposal_id'], 'mode': 'eval', 'matches': None,
               'estimated_false_positive_ratio': None, 'findings': ['没有可访问的历史数据'], 'coverage': 'unknown'}}
    return make('async_jobs.get', {'job_id': job['job_id']}), row, job


# LLM: W0 终态提交之前必须校验领域绑定，不能先把错结果记为 completed。
# 函数用途: 验证完成前验证器拒绝错模式和错提案，且不变更业务状态。
@pytest.mark.parametrize('field,value', [('proposal_id', 'another-proposal'), ('mode', 'backtest')])
def test_evaluation_validator_rejects_before_completion(domain, field, value):
    call, row, job = _evaluation_fixture(domain)
    from agent_py_agent.agent.gateway_parts.business_api_v13_w7 import V13_JOB_KINDS
    validator = V13_JOB_KINDS['detection_proposal_evaluation']['validate_result']
    assert validator(call, job, job['result']) is True
    job['result'][field] = value
    with pytest.raises((BusinessError, ValueError, HostAPIError)):
        validator(call, job, job['result'])
    assert V13Store(call.owner_home).get('detection_proposals', row['proposal_id'])['status'] == 'evaluating'


# LLM: 完成回写只能更新准确 job_id，重读不得重复追加或提升覆盖。
# 函数用途: 验证评估结果回写、失败回收与终态单调性。
@pytest.mark.parametrize('state', ['completed', 'failed'])
def test_complete_evaluation_bound_and_replay_safe(domain, state):
    call, row, job = _evaluation_fixture(domain)
    job['state'] = state
    if state == 'failed':
        job['result'] = None
    first = lifecycle.complete_evaluation(call, job)
    assert first['updated'] is True
    proposal = first['proposal']
    assert proposal['status'] == 'pending' and len(proposal['evaluations']) == 1
    assert proposal['evaluations'][0]['state'] == state
    assert proposal['evaluations'][0]['summary'] == job['result']
    assert lifecycle.complete_evaluation(call, job) == {'updated': False}
    late = lifecycle.complete_evaluation(call, {**job, 'state': 'running'})
    assert late == {'updated': False}
    assert V13Store(call.owner_home).get('detection_proposals', row['proposal_id'])['revision'] == proposal['revision']


# LLM: 合法 schema 不足以授权跨提案或错模式回写。
# 函数用途: 验证作业结果的准确领域绑定。
@pytest.mark.parametrize('field,value', [('proposal_id', 'another-proposal'), ('mode', 'backtest')])
def test_complete_evaluation_rejects_wrong_binding(domain, field, value):
    call, row, job = _evaluation_fixture(domain)
    job['result'][field] = value
    with pytest.raises(BusinessError) as caught:
        lifecycle.complete_evaluation(call, job)
    assert caught.value.code == 'DOMAIN_RESULT_INVALID'
    assert V13Store(call.owner_home).get('detection_proposals', row['proposal_id'])['status'] == 'evaluating'


# LLM: 只种入规范已持久化终态，真实 HTTP get 必须触发 W0 注册观察器并更新提案。
# 函数用途: 验证终态重读恢复路径，而非只直接调用 complete_evaluation。
@pytest.mark.parametrize('state', ['completed', 'failed'])
def test_async_jobs_get_writes_bound_proposal_through_http(v13_native, state):
    sid = _space(v13_native)
    public = _proposal(v13_native, sid)
    store = _store(v13_native)
    proposal = store.get('detection_proposals', public['proposal_id'])
    job_id, at = 'job-http-terminal-observation', proposal['created_at']
    proposal.update(status='evaluating', revision=proposal['revision'] + 1, evaluations=[{
        'job_id': job_id, 'mode': 'eval', 'state': 'accepted', 'summary': None, 'at': at}])
    result = {'proposal_id': proposal['proposal_id'], 'mode': 'eval', 'matches': None,
              'estimated_false_positive_ratio': None, 'findings': ['缺少可回测证据'], 'coverage': 'unknown'}
    job = {'job_id': job_id, 'owner_id': store.owner_id, 'kind': 'detection_proposal_evaluation',
        'action': 'detection_proposals.evaluate', 'subject_id': public['author'], 'space_id': sid,
        'agent_id': 'agent-terminal-fixture', 'task_id': 'task-terminal-fixture',
        'operation_id': 'operation-terminal-fixture', 'state': state,
        'result': result if state == 'completed' else None,
        'error': None if state == 'completed' else {'code': 'JOB_TASK_FAILED', 'message': '任务失败'},
        'revision': 1, 'created_at': at, 'updated_at': at}
    harness.v13_seed(store.owner_home, 'detection_proposals', {proposal['proposal_id']: proposal})
    harness.v13_seed(store.owner_home, 'async_jobs', {job_id: job})
    observed = _send(v13_native, 'async_jobs.get', {'job_id': job_id})['data']
    current = _send(v13_native, 'detection_proposals.get', {'proposal_id': proposal['proposal_id']})['data']['proposal']
    assert observed['state'] == state and observed['result'] == job['result']
    assert current['status'] == 'pending' and current['evaluations'][0]['summary'] == job['result']
    assert current['evaluations'][0]['state'] == state and len(current['evaluations']) == 1
    assert _send(v13_native, 'async_jobs.get', {'job_id': job_id})['data'] == observed
    assert _send(v13_native, 'detection_proposals.get', {'proposal_id': proposal['proposal_id']})['data']['proposal'] == current


# LLM: 结构化输出校验不可被额外字段、负数或非有限数字绕过。
# 函数用途: 固定模型结果 schema 的拒绝边界。
@pytest.mark.parametrize('change', [{'extra': True}, {'matches': -1}, {'estimated_false_positive_ratio': float('nan')}])
def test_finalize_evaluation_rejects_invalid(change):
    result = {'proposal_id': 'proposal-example', 'mode': 'eval', 'matches': None,
              'estimated_false_positive_ratio': None, 'findings': [], 'coverage': 'unknown'}
    result.update(change)
    with pytest.raises((ValueError, HostAPIError)):
        lifecycle.finalize_evaluation(json.dumps(result))


# LLM: 同一原生规则创建路径必须在两个集合提交前统一收口。
# 函数用途: 准备事务故障测试用的已批准提案调用。
def _approved_call(domain):
    make, sid = domain
    row = lifecycle.create_proposal(make('detection_proposals.create',
        {'space_id': sid, 'title': '原子晋升', 'rule': _draft()}))['proposal']
    approved = lifecycle.decide(make('detection_proposals.decide',
        {'proposal_id': row['proposal_id'], 'decision': 'approve'}, revision=row['revision']))['proposal']
    return make('detection_proposals.promote', {'proposal_id': row['proposal_id']},
                key='atomic-promotion-key', revision=approved['revision'])


# LLM: 注入的是原子写叶的真实失败，不用忽略异常的外层成功冒充通过。
# 函数用途: 验证多集合提交前失败和提交后物化失败均可同键安全恢复。
@pytest.mark.parametrize('phase', ['before_commit', 'after_commit'])
def test_promote_atomic_failure_and_retry(domain, monkeypatch, phase):
    from agent_py_agent.agent.business_spaces import v13_store as storage
    call = _approved_call(domain)
    original = storage.write_bytes_atomic_beneath
    failures = []

    # LLM: 只在本次晋升的准确两集合事务观察点失败一次。
    # 函数用途: 注入提交点前后故障。
    def fail(root, parts, data):
        payload = json.loads(data) if parts[-1] == '.transaction.json' else None
        before = payload is not None and set(payload.get('states', {})) == {'detection_proposals', 'monitor_rules'}
        after = parts[-1] == 'monitor_rules.json'
        if not failures and ((phase == 'before_commit' and before) or (phase == 'after_commit' and after)):
            failures.append(phase)
            raise OSError('injected write failure')
        return original(root, parts, data)

    monkeypatch.setattr(storage, 'write_bytes_atomic_beneath', fail)
    with pytest.raises(OSError):
        lifecycle.promote(call)
    assert failures == [phase]
    store = V13Store(call.owner_home)
    proposal = store.get('detection_proposals', call.params['proposal_id'])
    rules = store.list('monitor_rules')
    if phase == 'before_commit':
        assert proposal['status'] == 'approved' and rules == []
    else:
        assert proposal['status'] == 'promoted' and len(rules) == 1
        assert proposal['promoted_rule_id'] == rules[0]['rule_id']
    first = lifecycle.promote(call)
    assert lifecycle.promote(call) == first
    assert len(store.list('monitor_rules')) == 1


# LLM: 参数闭合、缺 CAS 和未知动作须在领域写入前拒绝。
# 函数用途: 经真实 HTTP 验证错误信封。
@pytest.mark.parametrize('action,params,key,revision,code', [
    ('detection_proposals.create', {'title': 'test', 'rule': _draft(), 'unknown': True}, 'schema-error-key', None, 'INVALID_SCHEMA'),
    ('detection_proposals.decide', {'proposal_id': 'missing', 'decision': 'approve'}, 'missing-cas-key', None, 'INVALID_SCHEMA'),
    ('detection_proposals.unknown', {}, None, None, 'UNSUPPORTED_ACTION'),
])
def test_envelopes(v13_native, action, params, key, revision, code):
    body = _send(v13_native, action, params, key=key, revision=revision, status=400)
    assert body['error']['code'] == code


# LLM: 别名只映射规范动作，不建立另一套提案状态或授权路径。
# 函数用途: 经真实 HTTP 验证 monitors.proposals 别名和发现映射。
def test_monitors_proposals_aliases_resolve_to_detection_proposals(v13_native):
    sid = _space(v13_native)
    row = _proposal(v13_native, sid)
    assert _send(v13_native, 'monitors.proposals.get', {'proposal_id': row['proposal_id']})['data']['proposal'] == row
    assert _send(v13_native, 'monitors.proposals.list', {'status': 'pending'})['data']['items'] == [row]


# LLM: 结构规则防止再次引入注册器循环依赖及可变参数旁路。
# 函数用途: 固定新模块的静态边界与注释要求。
def test_lifecycle_static_boundaries():
    source = Path(lifecycle.__file__).read_text()
    tree = ast.parse(source)
    assert not any(isinstance(node, ast.ImportFrom) and node.module
                   and node.module.endswith('business_api_v13') for node in tree.body)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.args.vararg is None and node.args.kwarg is None
            prefix = '\n'.join(source.splitlines()[max(0, node.lineno - 4):node.lineno - 1])
            assert '# LLM:' in prefix and '# 函数用途:' in prefix


# LLM: 原生手动创建路径不依赖分片 readiness 的 HTTP 预检，可独立验证来源计算。
# 函数用途: 为直接领域回归创建真实规则与 W8 来源。
def _direct_sources(domain):
    from agent_py_agent.agent.business_spaces.v13_w7_monitors import create_rule
    make, sid = domain
    call = make('monitors.rules.create', {**_draft(), 'space_id': sid}, key='direct-monitor-key')
    rule = create_rule(call)['rule']
    alerts = [_alert(call.owner_id, sid, rule['rule_id'], 'direct-alert-' + str(index),
                     value='direct-host-' + str(index)) for index in range(2)]
    harness.v13_seed(call.owner_home, 'soc_alerts', {row['alert_id']: row for row in alerts})
    feedback = [_feedback(row, 'direct-feedback-' + str(index)) for index, row in enumerate(alerts)]
    harness.v13_seed(call.owner_home, 'soc_feedback', {row['override_id']: row for row in feedback})
    return make, sid, call, rule, alerts


# LLM: 明显异常先排除，重复告警不翻倍，虚构反馈字段不能生成建议。
# 函数用途: 验证来源清洗、实际数字与覆盖降级。
def test_direct_source_anomalies_and_real_baselines(domain):
    make, sid, call, rule, alerts = _direct_sources(domain)
    broken = {**alerts[0], 'alert_id': 'broken', 'created_at': None}
    huge_id = {**alerts[0], 'alert_id': 'x' * 129}
    harness.v13_seed(call.owner_home, 'soc_alerts', {'duplicate': alerts[0], 'broken': broken, 'huge': huge_id})
    fabricated = {'override_id': 'fake', 'owner_id': call.owner_id, 'alert_id': alerts[0]['alert_id'],
                  'space_id': sid, 'rule_id': rule['rule_id'], 'verdict': 'false_positive', 'source': 'override',
                  'created_at': '2026-10-01T02:00:00+00:00'}
    harness.v13_seed(call.owner_home, 'soc_feedback', {'fake': fabricated})
    rows, coverage = lifecycle._derived_tuning(make('detection_tuning.list', {'rule_id': rule['rule_id']}))
    assert len(rows) == 2 and coverage == 'partial'
    assert all(row['false_positive_count'] == 1 for row in rows.values())
    window = {'since': '2026-10-01T00:00:00+00:00', 'until': '2026-10-03T00:00:00+00:00'}
    baseline_call = make('detection_proposals.baselines.create',
        {'space_id': sid, 'rule_id': rule['rule_id'], 'metric': 'alerts_per_day', 'window': window})
    assert lifecycle.create_baseline(baseline_call)['baseline']['value'] == 1.0
    ratio = lifecycle.create_baseline(replace(baseline_call,
        params={**baseline_call.params, 'metric': 'fp_ratio'}, idempotency_key='direct-fp-baseline'))
    assert ratio['baseline']['value'] == 1.0
    empty = replace(baseline_call, params={**baseline_call.params, 'metric': 'fp_ratio',
        'window': {'since': '2025-10-01T00:00:00+00:00', 'until': '2025-10-02T00:00:00+00:00'}},
        idempotency_key='empty-fp-baseline')
    with pytest.raises(BusinessError) as caught:
        lifecycle.create_baseline(empty)
    assert caught.value.code == 'STATE_CONFLICT'


# LLM: 零写测试在新派生建议尚未物化、专用编排锁尚不存在时执行。
# 函数用途: 验证 dry_run 不创建业务锁、回执、审计或建议文件。
def test_direct_dry_run_has_zero_writes(domain):
    make, _sid, call, rule, _alerts = _direct_sources(domain)
    root = call.owner_home / 'data' / 'business_v13'
    before = {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}
    preview_call = make('detection_tuning.auto_tune', {'rule_id': rule['rule_id'], 'dry_run': True},
                        revision=rule['revision'])
    result = lifecycle.auto_tune(preview_call)
    after = {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}
    assert len(result['applied']) == 2 and result['dry_run'] and result['rule'] is None
    assert after == before


# LLM: 来源更新不能撤销分析师的明确忽略决定。
# 函数用途: 验证未先 list 也能直接忽略派生建议且后续重建零重复写。
def test_direct_dismiss_is_sticky_before_materialization(domain):
    make, _sid, call, rule, alerts = _direct_sources(domain)
    listed = make('detection_tuning.list', {'rule_id': rule['rule_id']})
    derived, _coverage = lifecycle._derived_tuning(listed)
    row = sorted(derived.values(), key=lambda item: item['suggestion_id'])[0]
    dismiss = make('detection_tuning.dismiss', {'rule_id': rule['rule_id'],
        'suggestion_id': row['suggestion_id'], 'reason': '保留的业务实体'})
    first = lifecycle.dismiss_tuning(dismiss)['suggestion']
    assert first['status'] == 'dismissed'
    selected_alert = next(alert for alert in alerts if alert['entities'][0]['value'] == row['value'])
    new = {**selected_alert, 'alert_id': 'new-evidence'}
    harness.v13_seed(call.owner_home, 'soc_alerts', {new['alert_id']: new})
    feedback = _feedback(new, 'new-feedback')
    harness.v13_seed(call.owner_home, 'soc_feedback', {feedback['override_id']: feedback})
    materialized = lifecycle.list_tuning(listed)['items']
    assert first in materialized
    store = V13Store(call.owner_home)
    revision = store.revision('detection_tuning')
    assert lifecycle.list_tuning(listed)['items'] == materialized
    assert store.revision('detection_tuning') == revision


# LLM: 模拟实时 space 撤权时，基础请求事实仍允许，但准确 record 事实必须拒绝。
# 函数用途: 构造带可切换准确空间授权的已签发调用。
def _revocable_comment(domain):
    make, sid = domain
    row = lifecycle.create_proposal(make('detection_proposals.create',
        {'space_id': sid, 'title': '撤权回归', 'rule': _draft()}))['proposal']
    call = make('detection_proposals.comment', {'proposal_id': row['proposal_id'], 'text': '核对'},
                key='revocable-comment-key')
    policy = {'allowed': True}

    # LLM: 缺空间事实的基础检查不能代替精确 record 授权。
    # 函数用途: 在运行中对实际空间撤权。
    def authorize(action, facts):
        return facts.get('space_id') != sid or policy['allowed']

    context = api.bind_business_context(call.context.host, authorize=authorize, subject_id=call.subject_id)
    return replace(call, context=context), policy


# LLM: 同键回放必须重新核当前真实记录，不能直接漏出第一次成功回执。
# 函数用途: 验证空间撤权会拒绝已成功写入的同键重放。
def test_replay_rechecks_actual_record_space_permission(domain):
    call, policy = _revocable_comment(domain)
    first = lifecycle.comment(call)
    policy['allowed'] = False
    with pytest.raises(HostAPIError) as caught:
        lifecycle.comment(call)
    assert caught.value.code == 'CAPABILITY_DENIED'
    policy['allowed'] = True
    assert lifecycle.comment(call) == first


# LLM: 执行期间撤权必须在日志提交前再次拒绝，不能只在入口检查一次。
# 函数用途: 验证提交末核拒绝不会保存领域记录或成功回执。
def test_commit_rechecks_actual_record_space_permission(domain, monkeypatch):
    call, policy = _revocable_comment(domain)
    project = lifecycle._proposal_view

    # LLM: 领域 mutate 已完成但 Store 尚未提交时改变 live 授权。
    # 函数用途: 注入提交前撤权观察点。
    def revoke(row):
        result = project(row)
        policy['allowed'] = False
        return result

    monkeypatch.setattr(lifecycle, '_proposal_view', revoke)
    with pytest.raises(HostAPIError) as caught:
        lifecycle.comment(call)
    assert caught.value.code == 'CAPABILITY_DENIED'
    assert V13Store(call.owner_home).get('detection_proposals', call.params['proposal_id'])['comments'] == []
