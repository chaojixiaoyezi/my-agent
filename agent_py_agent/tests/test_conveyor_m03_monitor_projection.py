# LLM: 仅在 pytest 临时 Owner 内复现跨用户监控投递；不用真实账号、凭据、通道或运行服务，全部经原 Repository/投递服务入口。
# 模块用途: 钉住 Conveyor M03（L10）不变量：复合监控安装已提交、回执未结算时，监控自身自然 tick 只推进运行字段，
#   重试可补发原安装回执且只结算一次；用户改 schedule、enabled、规则主体或改后复原（定义代次已前进）仍阻止复用旧回执。
import pytest

from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError
from agent_py_agent.agent.scheduler.monitor_state import _RUNTIME_FIELDS
from agent_py_agent.agent.scheduler.repository import SchedulerRunFinish
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


# LLM: 读取两端回执、信封与接收方账本的原始字节，证明重复结算不改任何一份事实。
# 函数用途: 汇总一次投递的持久事实快照。
def _settled_bytes(kit, repo, delivery_id):
    paths = (kit.main / 'data/asset_delivery/outbox' / (delivery_id + '.json'),
        kit.alice / 'data/asset_delivery/inbox' / delivery_id / 'envelope.json',
        repo.store_path, repo.history_path)
    return {path: path.read_bytes() for path in paths}


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
