# LLM: 只用显式临时home与原Store/Manager校验子孙身份；不替换actor解析、创建门或provider，不读默认数据根。
# 模块用途: 回归本机canonical子代理根恢复，同时锁定循环、跨owner、坏谱系与外部缺身份拒绝。
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.conversation.host_actor import HostActorError, task_host_actor
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


# LLM: home显式指定并核实际解析结果，不能依赖pytest外不可用的conftest环境；原父/根/线程均走真实落盘。
# 函数用途: 建立本机主根、父与重新以父run为根的孩子，不造任何子代理TaskLink。
def _family(tmp_path):
    home = (tmp_path / 'explicit-test-home').resolve()
    assert home != (Path.home() / '.my-agent').resolve()
    agent = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(home), execution_mode='local_unmanaged'),
                        tmp_path / 'repo')
    assert agent.home_paths.root == home
    source = agent.conversation_store.threads.get_or_create({'channel': 'chat', 'channel_conversation_id': 'local-source',
        'owner_id': agent.home_paths.owner_id})
    agent.conversation_store.tasks.bind({'thread_id': source.thread_id, 'task_id': 'local-main', 'goal': '离线本机验证'})
    parent = agent.subagents.create_run(goal='原父', parent_id='local-main', root_id='local-main',
        attributes={'conversation_thread_id': source.thread_id})
    child = agent.subagents.create_run(goal='原孩子', parent_id=parent.id, root_id=parent.id,
        attributes={'conversation_thread_id': source.thread_id})
    return agent, source, parent, child


# LLM: 仅在pytest临时原canonical文件注入坏字段，不走产品写口伪造合法证明；load仍读真实损坏记录。
# 函数用途: 构造磁盘损坏负例，让身份恢复在模型或副作用开始前失败。
def _damage(task, **changes):
    from agent_py_agent.agent.subagents.services.agent_run_state import (
        canonical_state_path_for_task,
    )

    path = canonical_state_path_for_task(task)
    value = json.loads(path.read_text())
    value.update(changes)
    path.write_text(json.dumps(value))


def test_local_recursive_root_uses_canonical_parent_without_new_task_link(tmp_path):
    agent, _source, parent, child = _family(tmp_path)
    grandchild = agent.subagents.create_run(goal='原孙', parent_id=child.id, root_id=child.id,
        attributes=child.attributes)
    before = {p: p.read_bytes() for p in agent.conversation_store.storage.tasks_dir.glob('*.json')}
    assert task_host_actor(agent, child) is None
    assert task_host_actor(agent, grandchild) is None
    assert agent.conversation_store.tasks.load(parent.id) is None
    assert agent.conversation_store.tasks.load(child.id) is None
    assert agent.conversation_store.tasks.load(grandchild.id) is None
    assert {p: p.read_bytes() for p in agent.conversation_store.storage.tasks_dir.glob("*.json")} == before


@pytest.mark.parametrize('damage,code', [
    ('cycle', 'CHANNEL_ACTOR_IDENTITY_CONFLICT'),
    ('cross_owner', 'CHANNEL_ACTOR_SCOPE_MISMATCH'),
    ('other_root', 'CHANNEL_ACTOR_IDENTITY_CONFLICT'),
    ('missing_parent', 'CHANNEL_ACTOR_IDENTITY_INVALID'),
    ('wrong_agent_thread', 'CHANNEL_ACTOR_SCOPE_MISMATCH'),
    ('wrong_run_id', 'CHANNEL_ACTOR_IDENTITY_INVALID'),
    ('actor_conflict', 'CHANNEL_ACTOR_IDENTITY_CONFLICT'),
])
def test_damaged_canonical_ancestry_is_rejected(tmp_path, damage, code):
    agent, _source, parent, child = _family(tmp_path)
    if damage == 'cycle':
        _damage(parent, parent_id=child.id)
    elif damage == 'cross_owner':
        _damage(parent, owner='other-owner')
    elif damage == 'other_root':
        _damage(parent, root_id='unrelated-root')
    elif damage == 'missing_parent':
        _damage(parent, parent_id='missing-parent')
    elif damage == 'wrong_agent_thread':
        _damage(parent, agent_thread_id=child.agent_thread_id)
    elif damage == 'wrong_run_id':
        _damage(parent, id='not-the-loaded-parent')
    else:
        _damage(parent, host_channel_actor={'owner_id': parent.owner, 'source_thread_id': _source.thread_id,
            'entry_request_id': 'other-request', 'channel': 'feishu', 'conversation_id': 'oc_external', 'actor_id': 'ou_external'})
    with pytest.raises(HostActorError) as caught:
        task_host_actor(agent, child)
    assert caught.value.error_code == code


@pytest.mark.parametrize('channel', ['feishu', 'unknown-external'])
def test_local_owner_cannot_label_external_ancestry_as_empty_actor(tmp_path, channel):
    agent, _source, parent, child = _family(tmp_path)
    external = agent.conversation_store.threads.get_or_create({'channel': channel,
        'channel_conversation_id': 'oc_external', 'channel_user_id': 'ou_external', 'owner_id': agent.home_paths.owner_id})
    _damage(parent, attributes={**parent.attributes, 'conversation_thread_id': external.thread_id})
    with pytest.raises(HostActorError) as caught:
        task_host_actor(agent, child)
    assert caught.value.error_code == 'CHANNEL_ACTOR_IDENTITY_REQUIRED'


def test_external_owner_without_actor_is_not_local_source_proof(tmp_path):
    agent, _source, _parent, child = _family(tmp_path)
    agent.home_paths = replace(agent.home_paths, owner_provider='feishu', owner_kind='user')
    with pytest.raises(HostActorError) as caught:
        task_host_actor(agent, child)
    assert caught.value.error_code == 'CHANNEL_ACTOR_IDENTITY_REQUIRED'



def test_local_child_projection_cannot_hide_external_original_main_root(tmp_path):
    agent, source, parent, child = _family(tmp_path)
    external = agent.conversation_store.threads.get_or_create({'channel': 'feishu',
        'channel_conversation_id': 'oc_external', 'channel_user_id': 'ou_external', 'owner_id': agent.home_paths.owner_id})
    agent.conversation_store.tasks.bind({'thread_id': external.thread_id, 'task_id': 'external-main', 'goal': '未知外部原根'})
    _damage(parent, parent_id='external-main', root_id='external-main',
        attributes={**parent.attributes, 'conversation_thread_id': source.thread_id})
    with pytest.raises(HostActorError) as caught:
        task_host_actor(agent, child)
    assert caught.value.error_code == 'CHANNEL_ACTOR_IDENTITY_REQUIRED'
