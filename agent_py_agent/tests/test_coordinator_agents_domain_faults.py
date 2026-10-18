# LLM: 真实HTTP/领域/nonce主链只在受控原边界抛错；native类型同对象断言不是物理runner排空证明。
# 模块用途: 防止源检查失败或门外未结异常被AGENTS确认服务洗成completed，保原已消费和部分事实。
import json

import pytest

from agent_py_agent.agent.capability.agents_source_repository import AgentsSourceRepository
from agent_py_agent.agent.gateway_parts.control_operation_service import (
    read_gateway_control_operation,
)
from agent_py_agent.tests.test_coordinator_agents_domain import cold, submit


@pytest.mark.parametrize('cold', ['feishu'], indirect=True)
@pytest.mark.parametrize('as_group', [False, True])
def test_real_agents_confirm_retains_first_source_failure(cold, monkeypatch, as_group):  # noqa: F811
    from agent_py_agent.agent.gateway_parts.multiprocess_local_control import (
        _LocalControlWriteSource,
    )
    from agent_py_agent.agent.gateway_parts.owner_management_control import (
        OwnerManagementIngressUnavailable,
    )
    armed, failures = [], []
    original_check, original_write = _LocalControlWriteSource._require_actor_current, AgentsSourceRepository.write_text
    def check(source):
        if not armed:
            return original_check(source)
        ingress = source.http_origin[7]
        ingress.active = False
        try:
            return original_check(source)
        except OwnerManagementIngressUnavailable as error:
            failures.append((source, error))
            raise
        finally:
            ingress.active = True
    def write(repo, *args, **kwargs):
        armed.append(True)
        try:
            return original_write(repo, *args, **kwargs)
        finally:
            armed.clear()
    monkeypatch.setattr(_LocalControlWriteSource, '_require_actor_current', check)
    monkeypatch.setattr(AgentsSourceRepository, 'write_text', write)
    status, preview = submit(cold, '/agents write "失败不能写入"', 'native-preview', as_group=as_group)
    assert status == 200 and preview['ok']
    status, rejected = submit(cold, preview['details']['confirm_command'], 'native-confirm', as_group=as_group)
    assert status == 503, (status, rejected)
    source, error = failures[0]
    assert source._native_source_failure is error
    receipt = read_gateway_control_operation(cold.server.paths, source.receipt.operation_id, read_only=True)
    assert receipt.state == 'executing' and receipt.dispatch is None
    repo_home = source.resource_home.owner_home_dir
    record = json.loads((repo_home/'persona'/('agents-control-'+preview['details']['nonce']+'.json')).read_text())
    assert record['state'] == 'consumed_unknown'
    assert (repo_home/'AGENTS.md').read_text() != '失败不能写入'
    assert submit(cold, preview['details']['confirm_command'], 'native-confirm', as_group=as_group)[0] == 202


@pytest.mark.parametrize('cold', ['feishu'], indirect=True)
@pytest.mark.parametrize('kind', ['bounded', 'commit', 'worker'])
def test_real_agents_does_not_complete_after_unobserved_common_native(cold, monkeypatch, kind):  # noqa: F811
    from agent_py_agent.agent.backends.bounded_call import BoundedCallExitUnconfirmedError
    from agent_py_agent.agent.common.global_write_commit import GlobalWriteCommitUnavailable
    from agent_py_agent.agent.common.global_write_guard import GlobalWriteInWorkerError
    errors = {'bounded': BoundedCallExitUnconfirmedError('synthetic-agents', 'worker_exit_unconfirmed'),
              'commit': GlobalWriteCommitUnavailable('synthetic-original-scope'),
              'worker': GlobalWriteInWorkerError('synthetic-original-global-guard')}
    error = errors[kind]
    def fail(_repo, *args, **kwargs):
        raise error
    monkeypatch.setattr(AgentsSourceRepository, 'write_text', fail)
    status, preview = submit(cold, '/agents write "未结不能重做"', 'outer-preview')
    assert status == 200 and preview['ok']
    status, response = submit(cold, preview['details']['confirm_command'], 'outer-confirm')
    assert status == 503 and response['error_code'] == error.error_code, (status, response)
