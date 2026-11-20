# LLM: 仅在 pytest 临时 Owner 内复现跨用户监控投递；不用真实账号、凭据、通道或运行服务，全部经原 Repository/投递服务入口。
# 模块用途: 钉住 Conveyor M03（L10）不变量：复合监控安装已提交、回执未结算时，监控自身自然 tick 只推进运行字段，
#   重试可补发原安装回执且只结算一次；用户改 schedule、enabled、规则主体或改后复原（定义代次已前进）仍阻止复用旧回执。
#   M03-fix（Layer-2）另钉住：资产覆盖与启停沿调度器唯一代次写入器，旧运行晚到不写回；定义与回执事实按 JSON 类型比较；
#   调度器自身的错过宽限停用与已配置连续失败暂停属自然推进，只结算原安装回执一次；单任务与复合规则互转覆盖时
#   单任务身份代次不复用、收口核对规则身份；开工前就结束的运行（排队超期、Skill 快照不可用）同样只结算一次。
import time
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError
from agent_py_agent.agent.assets.script_store_support import _scheduler_job_matches
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.scheduler.monitor_state import _RUNTIME_FIELDS
from agent_py_agent.agent.scheduler.repository import (
    SchedulerJobCreateRequest,
    SchedulerRepository,
    SchedulerRunFinish,
)
from agent_py_agent.agent.scheduler.service import SchedulerService
from agent_py_agent.agent.user_space.runtime_paths import runtime_paths_for_agent
from agent_py_agent.tests.test_cross_owner_asset_delivery import kit as kit
from agent_py_agent.tests.test_cross_owner_asset_delivery import (
    round2_repo,
    round2_seed_monitor,
    round3_crash,
)


# LLM: 安装在子进程提交后即硬退出，回执停在未结算；随后只走原 reserve/claim/running/finish 一次真实到期运行。
# 函数用途: 返回已提交未结算的投递、接收方仓库、安装时与自然 tick 后的同一复合监控。
def _committed_install_after_natural_tick(kit):
    source = round2_seed_monitor(kit, True)
    sent = kit.service.send_bundle('local/main', 'local/alice', 'monitor', source['job_id'])
    round3_crash(kit, sent['delivery_id'], source['job_id'], point='committed', enabled=True)
    repo = round2_repo(kit.alice, 'local/alice')
    installed = repo.get_job(source['job_id'])
    envelope = kit.service._envelope(kit.alice, 'local/alice', sent['delivery_id'])
    assert envelope['status'] == 'pending'
    assert envelope['resolution']['expected_scheduler_jobs'][source['job_id']]['after'] is not None
    now = installed['next_run_at'] + 1
    run, = repo.reserve_due_runs(now=now)
    claim = repo.claim_run(run['run_id'], lease_seconds=60, now=now)
    repo.mark_run_running(run['run_id'], claim['claim_id'], now=now + 1)
    repo.finish_run(run['run_id'], claim['claim_id'], SchedulerRunFinish('done', now=now + 2))
    advanced = repo.get_job(source['job_id'])
    assert advanced['status'] == 'active' and advanced['run_count'] == 1
    rule, before = advanced['rules'][0], installed['rules'][0]
    # 自然 tick 只改运行字段：差异非空且全部落在调度器唯一运行字段表内。
    changed = {key for key in rule.keys() | before.keys() if rule.get(key) != before.get(key)}
    assert changed and changed <= _RUNTIME_FIELDS
    assert rule['run_count'] == 1 and rule['last_run_status'] == 'done'
    return sent['delivery_id'], repo, installed, advanced


# LLM: 读取两端回执、信封与接收方账本的原始字节，证明重复结算不改任何一份事实；尚无运行时历史文件不存在，记为 None。
# 函数用途: 汇总一次投递的持久事实快照。
def _settled_bytes(kit, repo, delivery_id):
    paths = (kit.main / 'data/asset_delivery/outbox' / (delivery_id + '.json'),
        kit.alice / 'data/asset_delivery/inbox' / delivery_id / 'envelope.json',
        repo.store_path, repo.history_path)
    return {path: path.read_bytes() if path.exists() else None for path in paths}


# LLM: 用户只经原 update_job 规则补丁修改；规则定义取自 API 返回的规则并按唯一运行字段表剥离，不手抄运行字段名单。
# 函数用途: 对已安装复合监控施加一次用户修改，并确认修改真实生效。
def _user_edit(repo, job, edit):
    rule = {key: value for key, value in job['rules'][0].items() if key not in _RUNTIME_FIELDS}
    original = rule['schedule']
    changed = {**original, 'every_seconds': original['every_seconds'] * 2}
    if edit == 'enabled':
        patches = [{**rule, 'enabled': False}]
    elif edit == 'rule_body':
        patches = [{**rule, 'tool': {**rule['tool'], 'arguments': {**rule['tool']['arguments'], 'edited_by': 'owner'}}}]
    else:
        patches = [{**rule, 'schedule': changed}]
        if edit == 'schedule_reverted':
            patches.append(rule)
    for patch in patches:
        job = repo.update_job(job['job_id'], patch={'rules': [patch]}, expected_version=job['version'])
    edited = job['rules'][0]
    if edit == 'schedule_reverted':
        assert edited['schedule'] == original and edited['enabled'] is True
        assert edited['definition_generation'] > 1
    else:
        assert {key: value for key, value in edited.items() if key not in _RUNTIME_FIELDS} != rule
    return job


# LLM: (a) 自然 tick 后重试接收：补发原安装回执一次，不重装、不改账本；第二次接收只读回同一结算。
# 函数用途: 复合监控自身运行推进不会被误当作用户改定义。
def test_natural_tick_then_accept_reissues_install_receipt_once(kit):
    delivery_id, repo, installed, advanced = _committed_install_after_natural_tick(kit)
    jobs_before = repo.list_jobs()[0]
    store_before, history_before = repo.store_path.read_bytes(), repo.history_path.read_bytes()
    first = kit.service.accept_delivery('local/alice', delivery_id)
    assert first['status'] == 'accepted'
    envelope = kit.service._envelope(kit.alice, 'local/alice', delivery_id)
    assert envelope['status'] == 'accepted'
    assert kit.service._origin_receipt(kit.main, envelope)['status'] == 'accepted'
    assert repo.store_path.read_bytes() == store_before
    assert repo.history_path.read_bytes() == history_before
    assert repo.list_jobs()[0] == jobs_before
    assert repo.get_job(installed['job_id']) == advanced
    settled = _settled_bytes(kit, repo, delivery_id)
    second = kit.service.accept_delivery('local/alice', delivery_id)
    assert second == first
    assert _settled_bytes(kit, repo, delivery_id) == settled


# LLM: (b) 自然 tick 后拒绝：安装已提交，拒绝不能写 rejected；同一守门先把原安装回执结算为 accepted 再报冲突。
# 函数用途: 拒绝入口与接收入口对自然推进的辨认一致，结算只发生一次。
def test_natural_tick_then_reject_settles_committed_install_receipt(kit):
    delivery_id, repo, installed, advanced = _committed_install_after_natural_tick(kit)
    store_before, history_before = repo.store_path.read_bytes(), repo.history_path.read_bytes()
    with pytest.raises(AssetDeliveryError) as failure:
        kit.service.reject_delivery('local/alice', delivery_id)
    assert failure.value.error_code == 'DELIVERY_STATE_CONFLICT'
    envelope = kit.service._envelope(kit.alice, 'local/alice', delivery_id)
    assert envelope['status'] == 'accepted'
    assert kit.service._origin_receipt(kit.main, envelope)['status'] == 'accepted'
    assert repo.store_path.read_bytes() == store_before
    assert repo.history_path.read_bytes() == history_before
    assert repo.get_job(installed['job_id']) == advanced
    settled = _settled_bytes(kit, repo, delivery_id)
    assert kit.service.accept_delivery('local/alice', delivery_id)['status'] == 'accepted'
    with pytest.raises(AssetDeliveryError) as repeated:
        kit.service.reject_delivery('local/alice', delivery_id)
    assert repeated.value.error_code == 'DELIVERY_STATE_CONFLICT'
    assert _settled_bytes(kit, repo, delivery_id) == settled


# LLM: (c) 自然 tick 后用户改定义：接收与拒绝都失败关闭，信封、两端回执、账本和历史逐字节不变；
#   改后复原的定义字段与安装时相同，只有定义代次前进，同样不能冒用旧回执。
# 函数用途: 用户修改不会被误当作自然 tick 而补发旧安装回执。
@pytest.mark.parametrize('edit', ['schedule', 'enabled', 'rule_body', 'schedule_reverted'])
def test_user_edit_after_install_keeps_receipt_blocked(kit, edit):
    delivery_id, repo, installed, advanced = _committed_install_after_natural_tick(kit)
    edited = _user_edit(repo, advanced, edit)
    assert edited['version'] > advanced['version']
    before = _settled_bytes(kit, repo, delivery_id)
    with pytest.raises(AssetDeliveryError) as accepted:
        kit.service.accept_delivery('local/alice', delivery_id)
    assert accepted.value.error_code == 'DELIVERY_INSTALLATION_STATE_CHANGED'
    with pytest.raises(AssetDeliveryError) as rejected:
        kit.service.reject_delivery('local/alice', delivery_id)
    assert rejected.value.error_code == 'DELIVERY_STATE_CONFLICT'
    assert _settled_bytes(kit, repo, delivery_id) == before
    envelope = kit.service._envelope(kit.alice, 'local/alice', delivery_id)
    assert envelope['status'] not in {'accepted', 'rejected'}
    assert kit.service._origin_receipt(kit.main, envelope)['status'] not in {'accepted', 'rejected'}


# LLM: 单任务取自身、复合监控取唯一规则；只读视图，不写账本。
# 函数用途: 返回本文件合成监控唯一的调度单元。
def _unit(job):
    return job['rules'][0] if job.get('rules') else job


# LLM: 发送方只经原 update_job 修改源监控；复合规则按唯一运行字段表剥离后整条提交，不手抄定义字段名单。
# 函数用途: 对发送方源监控施加一次定义修改并返回新版本。
def _sender_edit(kit, job, change):
    if job.get('rules'):
        rule = {key: value for key, value in job['rules'][0].items() if key not in _RUNTIME_FIELDS}
        change = {'rules': [{**rule, **change}]}
    return round2_repo(kit.main, 'local/main').update_job(job['job_id'], patch=change, expected_version=job['version'])


# LLM: 单次 at 取一小时后的真实未来时刻，不依赖固定日期或本机时区。
# 函数用途: 生成一个未来单次计划。
def _future_at():
    return {'kind': 'at', 'at': datetime.fromtimestamp(time.time() + 3600, timezone.utc).isoformat(), 'timezone': 'UTC'}


# LLM: 只走原 reserve/claim/running 一次到期运行且不收口；返回冻结 run 与领取凭据，供之后迟到收口。
# 函数用途: 让接收方监控进入一次正在执行的运行。
def _start_due_run(repo, job):
    now = job['next_run_at'] + 1
    run, = repo.reserve_due_runs(now=now)
    claim = repo.claim_run(run['run_id'], lease_seconds=1800, now=now)
    repo.mark_run_running(run['run_id'], claim['claim_id'], now=now + 1)
    return run, claim, now


# LLM: 接收方只经原 update_job 修改准确一条规则的工具输入；规则取自 API 返回并按唯一运行字段表剥离。
# 函数用途: 把规则工具输入 inputs.value 改成给定 JSON 值，并确认类型原样落盘。
def _edit_rule_input(repo, job, value):
    rule = {key: item for key, item in job['rules'][0].items() if key not in _RUNTIME_FIELDS}
    arguments = {**rule['tool']['arguments'], 'inputs': {'value': value}}
    edited = repo.update_job(job['job_id'], patch={'rules': [{**rule, 'tool': {**rule['tool'], 'arguments': arguments}}]},
        expected_version=job['version'])
    stored = edited['rules'][0]['tool']['arguments']['inputs']['value']
    assert type(stored) is type(value) and stored == value
    return edited


# LLM: 接收与拒绝都须失败关闭：返回原冲突码，信封、两端回执、账本与历史逐字节不变，回执不进入终态。
# 函数用途: 断言一次未结算投递的安装回执保持阻止。
def _assert_receipt_blocked(kit, repo, delivery_id):
    before = _settled_bytes(kit, repo, delivery_id)
    with pytest.raises(AssetDeliveryError) as accepted:
        kit.service.accept_delivery('local/alice', delivery_id)
    assert accepted.value.error_code == 'DELIVERY_INSTALLATION_STATE_CHANGED'
    with pytest.raises(AssetDeliveryError) as rejected:
        kit.service.reject_delivery('local/alice', delivery_id)
    assert rejected.value.error_code == 'DELIVERY_STATE_CONFLICT'
    assert _settled_bytes(kit, repo, delivery_id) == before
    envelope = kit.service._envelope(kit.alice, 'local/alice', delivery_id)
    assert envelope['status'] not in {'accepted', 'rejected'}
    assert kit.service._origin_receipt(kit.main, envelope)['status'] not in {'accepted', 'rejected'}


# LLM: P1-01 覆盖接收沿调度器唯一代次写入器：替换定义承接旧代并前进，旧运行晚到只进历史，不消费替换的未来单次计划。
# 函数用途: 覆盖安装时旧监控仍在运行，替换监控按自己的计划恰好预留一次。
@pytest.mark.parametrize('composite', [False, True])
def test_overwrite_keeps_replacement_one_shot_from_running_predecessor(kit, composite):
    source = round2_seed_monitor(kit, composite)
    first = kit.service.send_bundle('local/main', 'local/alice', 'monitor', source['job_id'])
    assert kit.service.accept_delivery('local/alice', first['delivery_id'], initial_enabled=True)['status'] == 'accepted'
    repo = round2_repo(kit.alice, 'local/alice')
    run, claim, now = _start_due_run(repo, repo.get_job(source['job_id']))
    source = _sender_edit(kit, source, {'schedule': _future_at()})
    second = kit.service.send_bundle('local/main', 'local/alice', 'monitor', source['job_id'])
    assert kit.service.accept_delivery('local/alice', second['delivery_id'], conflict_strategy='overwrite',
        initial_enabled=True)['status'] == 'accepted'
    replacement = repo.get_job(source['job_id'])
    assert replacement['status'] == 'active' and _unit(replacement)['schedule']['kind'] == 'at'
    assert replacement['next_run_at'] > now + 2
    repo.finish_run(run['run_id'], claim['claim_id'], SchedulerRunFinish('done', now=now + 2))
    final = repo.get_job(source['job_id'])
    assert final['status'] == 'active' and _unit(final)['enabled'] is True
    assert final['next_run_at'] == replacement['next_run_at']
    history = repo.history(job_id=source['job_id'])[0]
    assert [(row['run_id'], row['status']) for row in history] == [(run['run_id'], 'done')]
    fresh = repo.reserve_due_runs(now=replacement['next_run_at'] + 1)
    assert len(fresh) == 1 and fresh[0]['scheduled_for'] == replacement['next_run_at']
    assert fresh[0]['definition_generation'] == _unit(replacement)['definition_generation'] == run['definition_generation'] + 1


# LLM: P1-01 资产停用/启用与调度器 pause/resume 同用唯一代次写入器：每次启停推进定义代次，启停前已开工的旧运行晚到失败只进历史。
# 函数用途: 重新启用的监控不被启停前的旧运行按已配置失败暂停策略停掉。
@pytest.mark.parametrize('composite', [False, True])
def test_asset_reenable_is_not_paused_by_late_failure_of_earlier_run(kit, composite):
    source = round2_seed_monitor(kit, composite)
    source = _sender_edit(kit, source, {'policy': {**_unit(source)['policy'], 'failure_pause_after': 1}})
    sent = kit.service.send_bundle('local/main', 'local/alice', 'monitor', source['job_id'])
    assert kit.service.accept_delivery('local/alice', sent['delivery_id'], initial_enabled=True)['status'] == 'accepted'
    repo = round2_repo(kit.alice, 'local/alice')
    run, claim, now = _start_due_run(repo, repo.get_job(source['job_id']))
    generations = []
    for enabled in (False, True):
        kit.service.toggle_asset('local/alice', 'monitor', source['job_id'], enabled)
        current = repo.get_job(source['job_id'])
        assert current['enabled'] is enabled and current['status'] == ('active' if enabled else 'paused')
        generations.append(_unit(current)['definition_generation'])
    repo.finish_run(run['run_id'], claim['claim_id'], SchedulerRunFinish('failed', error_code='TASK_FAILED', now=now + 2))
    final = repo.get_job(source['job_id'])
    assert final['status'] == 'active' and _unit(final)['enabled'] is True
    assert _unit(final)['consecutive_failures'] == 0
    history = repo.history(job_id=source['job_id'])[0]
    assert [(row['run_id'], row['status']) for row in history] == [(run['run_id'], 'failed')]
    assert generations == [run['definition_generation'] + 1, run['definition_generation'] + 2]


# LLM: P1-01 形态互转：接收方经原 update_job 把单任务改成具名规则，再接受发送方单任务 at 覆盖。旧运行开工于转换前
#   （单任务身份）、具名规则下，或具名规则再改一次后（代次与替换单任务相同）开工，晚到收口都只进历史，不消费替换的未来单次计划。
# 函数用途: 钉住单任务身份退出后代次不复用，且收口核对冻结规则身份，替换单任务到点恰好预留一次。
@pytest.mark.parametrize('started', ['before_conversion', 'under_named_rule', 'under_edited_named_rule'])
def test_shape_changing_overwrite_keeps_replacement_one_shot_from_older_run(kit, started):
    source = round2_seed_monitor(kit, False)
    first = kit.service.send_bundle('local/main', 'local/alice', 'monitor', source['job_id'])
    assert kit.service.accept_delivery('local/alice', first['delivery_id'], initial_enabled=True)['status'] == 'accepted'
    repo = round2_repo(kit.alice, 'local/alice')
    if started == 'before_conversion':
        run, claim, now = _start_due_run(repo, repo.get_job(source['job_id']))
    single = repo.get_job(source['job_id'])
    rule = {'id': 'named', 'name': '接收方具名规则', 'use_llm': True,
        **{key: single[key] for key in ('prompt', 'schedule', 'execution', 'policy', 'precheck')}}
    converted = repo.update_job(source['job_id'], patch={'rules': [rule]}, expected_version=single['version'])
    if started == 'under_edited_named_rule':
        converted = repo.update_job(source['job_id'], patch={'rules': [{**rule, 'prompt': '接收方改过的规则'}]},
            expected_version=converted['version'])
    assert [item['id'] for item in converted['rules']] == ['named']
    if started != 'before_conversion':
        run, claim, now = _start_due_run(repo, converted)
        assert run['rule_id'] == 'named'
    source = _sender_edit(kit, source, {'schedule': _future_at()})
    second = kit.service.send_bundle('local/main', 'local/alice', 'monitor', source['job_id'])
    assert kit.service.accept_delivery('local/alice', second['delivery_id'], conflict_strategy='overwrite',
        initial_enabled=True)['status'] == 'accepted'
    replacement = repo.get_job(source['job_id'])
    assert replacement.get('rules') is None and replacement['schedule']['kind'] == 'at'
    assert replacement['status'] == 'active' and replacement['next_run_at'] > now + 2
    if started == 'under_edited_named_rule':
        assert replacement['definition_generation'] == run['definition_generation']
    repo.finish_run(run['run_id'], claim['claim_id'], SchedulerRunFinish('done', now=now + 2))
    final = repo.get_job(source['job_id'])
    assert final['status'] == 'active' and final['next_run_at'] == replacement['next_run_at']
    history = repo.history(job_id=source['job_id'])[0]
    assert [(row['run_id'], row['status']) for row in history] == [(run['run_id'], 'done')]
    fresh = repo.reserve_due_runs(now=replacement['next_run_at'] + 1)
    assert [(row['rule_id'], row['scheduled_for']) for row in fresh] == [('', replacement['next_run_at'])]


# LLM: P1-02 定义代次判定按 JSON 类型比较：对象键序与展示名不改代次，任意深度数字改布尔必改代次。
# 函数用途: 钉住调度器唯一代次写入器的定义比较语义。
def test_definition_generation_compares_json_types_at_any_depth(tmp_path):
    repo = SchedulerRepository(tmp_path / 'scheduler', owner_provider='local', owner_kind='main',
        owner_id='main', default_timezone='UTC')
    schedule = {'kind': 'every', 'every_seconds': 600, 'anchor_at': 1000, 'timezone': 'UTC'}
    rule = {'id': 'probe', 'name': '动作', 'schedule': schedule, 'use_llm': False,
        'tool': {'name': 'read_file', 'arguments': {'path': 'a.json', 'options': {'limit': 1, 'strict': 0}}}}
    job = repo.create_job(SchedulerJobCreateRequest(name='类型监控', prompt='动作', thread_id='thread-test',
        schedule=schedule, rules=[rule], now=900))[0]

    # LLM: 规则补丁取 API 返回规则并按唯一运行字段表剥离，只替换展示名与工具参数。
    # 函数用途: 经原 update_job 提交一次规则修改。
    def edit(current, arguments, name='动作'):
        patch = {key: value for key, value in current['rules'][0].items() if key not in _RUNTIME_FIELDS}
        patch.update(name=name, tool={'name': 'read_file', 'arguments': arguments})
        return repo.update_job(current['job_id'], patch={'rules': [patch]}, expected_version=current['version'], now=950)

    same = edit(job, {'options': {'strict': 0, 'limit': 1}, 'path': 'a.json'}, name='改名后的动作')
    assert same['rules'][0]['name'] == '改名后的动作' and same['rules'][0]['definition_generation'] == 1
    nested = edit(same, {'options': {'strict': False, 'limit': 1}, 'path': 'a.json'})
    assert nested['rules'][0]['definition_generation'] == 2
    reverted = edit(nested, {'options': {'strict': 0, 'limit': 1}, 'path': 'a.json'})
    assert reverted['rules'][0]['definition_generation'] == 3


# LLM: P1-02 回执事实比较与定义代次同一 JSON 类型语义：规则输入或 metadata 只改布尔/数字类型也不是同一安装，键序不算修改。
# 函数用途: 钉住安装事实比较不被 Python 的 1 == True 合并。
@pytest.mark.parametrize('location', ['tool_arguments', 'metadata'])
def test_receipt_fact_distinguishes_json_boolean_from_number(kit, location):
    expected = round2_seed_monitor(kit, True)
    node = expected.setdefault('metadata', {}) if location == 'metadata' else expected['rules'][0]['tool']['arguments']
    node['inputs'] = {'value': 1, 'flags': [0, 'x']}
    current = deepcopy(expected)
    changed = current['metadata'] if location == 'metadata' else current['rules'][0]['tool']['arguments']
    changed['inputs'] = {'flags': [0, 'x'], 'value': 1}
    assert _scheduler_job_matches(current, expected)
    changed['inputs']['value'] = True
    assert not _scheduler_job_matches(current, expected)
    changed['inputs'] = {'value': 1, 'flags': [False, 'x']}
    assert not _scheduler_job_matches(current, expected)


# LLM: P1-02 自然 tick 前把规则输入数字改成同值布尔：定义代次前进，接收与拒绝都不能复用旧安装回执。
# 函数用途: 用户改过的规则不会因 Python 布尔与数字相等而冒用旧回执。
@pytest.mark.parametrize('before,after', [(1, True), (0, False)])
def test_typed_rule_edit_before_first_tick_keeps_receipt_blocked(kit, before, after):
    source = round2_seed_monitor(kit, True)
    source = _sender_edit(kit, source, {'tool': {'name': 'workflows',
        'arguments': {'workflow_id': 'flow', 'inputs': {'value': before}}}})
    sent = kit.service.send_bundle('local/main', 'local/alice', 'monitor', source['job_id'])
    round3_crash(kit, sent['delivery_id'], source['job_id'], point='committed', enabled=True)
    repo = round2_repo(kit.alice, 'local/alice')
    installed = repo.get_job(source['job_id'])
    edited = _edit_rule_input(repo, installed, after)
    assert edited['run_count'] == 0
    _assert_receipt_blocked(kit, repo, sent['delivery_id'])
    assert _unit(edited)['definition_generation'] == _unit(installed)['definition_generation'] + 1


# LLM: P1-02 运行中把输入数字改成布尔：旧运行结果只进历史，不作为新定义的确认结果发布；回执保持阻止。
# 函数用途: 旧代运行晚到收口不发布到类型已改的规则。
def test_typed_rule_edit_during_run_withholds_old_result_and_receipt(kit):
    source = round2_seed_monitor(kit, True)
    source = _sender_edit(kit, source, {'tool': {'name': 'workflows',
        'arguments': {'workflow_id': 'flow', 'inputs': {'value': 1}}}})
    sent = kit.service.send_bundle('local/main', 'local/alice', 'monitor', source['job_id'])
    round3_crash(kit, sent['delivery_id'], source['job_id'], point='committed', enabled=True)
    repo = round2_repo(kit.alice, 'local/alice')
    run, claim, now = _start_due_run(repo, repo.get_job(source['job_id']))
    assert type(run['tool']['arguments']['inputs']['value']) is int
    repo.record_rule_result(run['run_id'], claim['claim_id'], {'value': 1})
    edited = _edit_rule_input(repo, repo.get_job(source['job_id']), True)
    repo.finish_run(run['run_id'], claim['claim_id'], SchedulerRunFinish('done', now=now + 2))
    final = repo.get_job(source['job_id'])
    assert _unit(final).get('result') is None
    history = repo.history(job_id=source['job_id'])[0]
    assert [(row['run_id'], row['status'], row['structured_result']) for row in history] == [
        (run['run_id'], 'done', {'value': 1})]
    _assert_receipt_blocked(kit, repo, sent['delivery_id'])
    assert _unit(edited)['definition_generation'] == run['definition_generation'] + 1


# LLM: 先经原 reserve 预留一次到期运行，再由原 SchedulerService.enqueue_ready_runs 在开工前结束它：排队超过已配置期限记
#   skipped/SCHEDULER_QUEUE_EXPIRED；服务未接 Skill 快照来源时记 failed/SCHEDULER_SKILL_SNAPSHOT_UNAVAILABLE。会话库取投递线程
#   所在的同一会话工作区；运行从未开工，开工计数不变。
# 函数用途: 让接收方监控经真实派发入口产生一次开工前终态。
def _finish_before_start(kit, repo, job, transition):
    now = job['next_run_at'] + 1
    run, = repo.reserve_due_runs(now=now)
    path = runtime_paths_for_agent(SimpleNamespace(), kit.alice, SimpleNamespace(owner_home_dir=kit.alice))['conversation_workspace']
    service = SchedulerService(repo, conversation_store=ConversationStore(path))
    expired = transition == 'queue_expired'
    assert service.enqueue_ready_runs(now=now + 11 if expired else now) == []
    history = repo.history(job_id=job['job_id'])[0]
    assert [(row['run_id'], row['status'], row['error_code'], row['started_at']) for row in history] == [(run['run_id'],
        'skipped' if expired else 'failed', 'SCHEDULER_QUEUE_EXPIRED' if expired else 'SCHEDULER_SKILL_SNAPSHOT_UNAVAILABLE', 0)]
    assert _unit(repo.get_job(job['job_id']))['run_count'] == 0


# LLM: P1-03 调度器自身停止（单次 at 错过宽限、达到已配置连续失败阈值）不推进定义代次、不是用户修改：
#   接收或拒绝都只结算原安装回执一次，暂停的调度账本与历史逐字节保持；单任务错过宽限是原有对照。
#   开工前终态（单次计划排队超过已配置期限、派发前 Skill 快照不可用且达失败暂停阈值）不推进开工计数，同样只结算一次。
# 函数用途: 安装已提交、回执未结算时的自然暂停不再把两端回执永久留在 pending。
@pytest.mark.parametrize('composite,transition', [(False, 'misfire'), (True, 'misfire'),
    (False, 'failure_pause'), (True, 'failure_pause'), (False, 'queue_expired'), (True, 'queue_expired'),
    (False, 'skill_unavailable'), (True, 'skill_unavailable')],
    ids=['single-misfire', 'composite-misfire', 'single-failure-pause', 'composite-failure-pause',
        'single-queue-expired', 'composite-queue-expired', 'single-skill-unavailable', 'composite-skill-unavailable'])
@pytest.mark.parametrize('action', ['accept', 'reject'])
def test_natural_pause_after_commit_settles_install_receipt_once(kit, composite, transition, action):
    source = round2_seed_monitor(kit, composite)
    policy = _unit(source)['policy']
    change = {'misfire': {'schedule': _future_at()},
        'queue_expired': {'schedule': _future_at(), 'policy': {**policy, 'queue_timeout_seconds': 10}}}.get(
        transition, {'policy': {**policy, 'failure_pause_after': 1}})
    source = _sender_edit(kit, source, change)
    sent = kit.service.send_bundle('local/main', 'local/alice', 'monitor', source['job_id'])
    round3_crash(kit, sent['delivery_id'], source['job_id'], point='committed', enabled=True)
    repo = round2_repo(kit.alice, 'local/alice')
    installed = repo.get_job(source['job_id'])
    if transition == 'misfire':
        assert _unit(installed)['policy']['misfire'] == 'skip'
        assert repo.reserve_due_runs(now=installed['next_run_at'] + installed['misfire_grace_seconds'] + 1) == []
    elif transition == 'failure_pause':
        run, claim, now = _start_due_run(repo, installed)
        repo.finish_run(run['run_id'], claim['claim_id'], SchedulerRunFinish('failed', error_code='TASK_FAILED', now=now + 2))
    else:
        _finish_before_start(kit, repo, installed, transition)
    paused = repo.get_job(source['job_id'])
    assert paused['status'] == 'paused' and _unit(paused)['enabled'] is (not composite)
    assert _unit(paused)['definition_generation'] == _unit(installed)['definition_generation']
    store_before, history_before = repo.store_path.read_bytes(), repo.history_path.read_bytes()
    if action == 'accept':
        assert kit.service.accept_delivery('local/alice', sent['delivery_id'])['status'] == 'accepted'
    else:
        with pytest.raises(AssetDeliveryError) as failure:
            kit.service.reject_delivery('local/alice', sent['delivery_id'])
        assert failure.value.error_code == 'DELIVERY_STATE_CONFLICT'
    envelope = kit.service._envelope(kit.alice, 'local/alice', sent['delivery_id'])
    assert envelope['status'] == 'accepted'
    assert kit.service._origin_receipt(kit.main, envelope)['status'] == 'accepted'
    assert repo.store_path.read_bytes() == store_before and repo.history_path.read_bytes() == history_before
    assert repo.get_job(source['job_id']) == paused
    settled = _settled_bytes(kit, repo, sent['delivery_id'])
    assert kit.service.accept_delivery('local/alice', sent['delivery_id'])['status'] == 'accepted'
    with pytest.raises(AssetDeliveryError) as repeated:
        kit.service.reject_delivery('local/alice', sent['delivery_id'])
    assert repeated.value.error_code == 'DELIVERY_STATE_CONFLICT'
    assert _settled_bytes(kit, repo, sent['delivery_id']) == settled


# LLM: P1-03 失败暂停只认已配置阈值与运行证据：阈值关闭或未达到时，同形暂停/停用不归一为自然推进。
# 函数用途: 钉住失败暂停证据的阈值边界，暂停原因文字与 failed 状态本身都不是证据。
@pytest.mark.parametrize('composite', [False, True])
@pytest.mark.parametrize('pause_after,matches', [(1, True), (0, False), (2, False)])
def test_failure_pause_evidence_requires_configured_threshold(kit, composite, pause_after, matches):
    expected = round2_seed_monitor(kit, composite)
    _unit(expected)['policy'] = {**_unit(expected)['policy'], 'failure_pause_after': pause_after}
    current = deepcopy(expected)
    ran = {'last_run_status': 'failed', 'last_error_code': 'TASK_FAILED', 'last_run_at': 1000.0, 'run_count': 1}
    current.update(ran, status='paused')
    _unit(current).update(ran, consecutive_failures=1)
    if composite:
        current['rules'][0]['enabled'] = False
    else:
        current['paused_reason'] = 'consecutive_failures'
    assert _scheduler_job_matches(current, expected) is matches


# LLM: P1-03 开工前终态不推进规则开工计数；规则停止只认安装快照之后新记下的证据：单次规则已耗尽且终态（状态或错误码）与快照
#   不同，或连续失败数在安装后增加并达到已配置阈值。快照里已有的同一终态、没有终态、单次计划未耗尽、或连续失败数未增加时，
#   同形停用仍不归一。
# 函数用途: 钉住开工前自然停止的证据边界，不以旧状态或旧计数授权回执复用。
@pytest.mark.parametrize('stop,before,after,next_run_at,matches', [
    ('one_shot', ('', '', 0), ('skipped', 'SCHEDULER_QUEUE_EXPIRED', 0), 0.0, True),
    ('one_shot', ('skipped', 'SCHEDULER_QUEUE_EXPIRED', 0), ('skipped', 'SCHEDULER_QUEUE_EXPIRED', 0), 0.0, False),
    ('one_shot', ('', '', 0), ('', '', 0), 0.0, False),
    ('one_shot', ('', '', 0), ('skipped', 'SCHEDULER_QUEUE_EXPIRED', 0), 5000.0, False),
    ('failure_pause', ('', '', 0), ('failed', 'SCHEDULER_SKILL_SNAPSHOT_UNAVAILABLE', 1), 5600.0, True),
    ('failure_pause', ('failed', 'SCHEDULER_SKILL_SNAPSHOT_UNAVAILABLE', 1),
        ('failed', 'SCHEDULER_SKILL_SNAPSHOT_UNAVAILABLE', 1), 5600.0, False),
], ids=['one-shot-new-outcome', 'one-shot-snapshot-outcome', 'one-shot-no-outcome', 'one-shot-not-consumed',
    'failure-new-failure', 'failure-snapshot-count'])
def test_prestart_stop_evidence_must_be_newer_than_install(kit, stop, before, after, next_run_at, matches):
    fields = ('last_run_status', 'last_error_code', 'consecutive_failures')
    expected = round2_seed_monitor(kit, True)
    rule = expected['rules'][0]
    if stop == 'one_shot':
        rule['schedule'] = _future_at()
    else:
        rule['policy'] = {**rule['policy'], 'failure_pause_after': 1}
    rule.update(zip(fields, before), next_run_at=5000.0)
    current = deepcopy(expected)
    current['status'] = 'paused'
    current['rules'][0].update(zip(fields, after), enabled=False, next_run_at=next_run_at)
    assert _scheduler_job_matches(current, expected) is matches


# LLM: 暂停原因文字不是证据：未达已配置阈值的失败后，本人以 consecutive_failures 为原因暂停仍是用户修改（定义代次前进），回执保持阻止。
# 函数用途: 自然暂停归一不能被同名暂停原因冒用。
def test_owner_pause_named_like_failure_pause_keeps_receipt_blocked(kit):
    source = round2_seed_monitor(kit, False)
    source = _sender_edit(kit, source, {'policy': {**source['policy'], 'failure_pause_after': 2}})
    sent = kit.service.send_bundle('local/main', 'local/alice', 'monitor', source['job_id'])
    round3_crash(kit, sent['delivery_id'], source['job_id'], point='committed', enabled=True)
    repo = round2_repo(kit.alice, 'local/alice')
    run, claim, now = _start_due_run(repo, repo.get_job(source['job_id']))
    repo.finish_run(run['run_id'], claim['claim_id'], SchedulerRunFinish('failed', error_code='TASK_FAILED', now=now + 2))
    failed = repo.get_job(source['job_id'])
    assert failed['status'] == 'active' and failed['consecutive_failures'] == 1
    paused = repo.pause_job(source['job_id'], expected_version=failed['version'], reason='consecutive_failures')
    assert paused['status'] == 'paused' and paused['paused_reason'] == 'consecutive_failures'
    _assert_receipt_blocked(kit, repo, sent['delivery_id'])
