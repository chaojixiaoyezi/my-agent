"""两个 Owner 与两种用户偏好不串读、不串写、不改变共享全局设置。"""
from __future__ import annotations

import hashlib
from dataclasses import replace
from types import SimpleNamespace

from agent_py_agent.agent.adapter.inbound_notes import append_inbound_note, read_inbound_notes
from agent_py_agent.agent.adapter.topic_seed import remember_topic_seed, take_topic_seed
from agent_py_agent.agent.conversation.channels import DeliveryContext
from agent_py_agent.agent.scheduler import alert_topic_anchors as anchors
from agent_py_agent.agent.scheduler import delivery_policy


# LLM: 各资料有独立临时 Owner 根；相同的外部消息 ID 不能合并私有账本。
# 函数用途: 验证便签、锚点和种子均按显式 Owner 隔离。
def test_two_owners_keep_their_notes_anchors_and_seeds_separate(tmp_path):
    left, right = tmp_path / 'owner-left', tmp_path / 'owner-right'
    for home, label in ((left, 'left'), (right, 'right')):
        append_inbound_note(home, channel='feishu', chat_id='same', message_id='same', media_kind='file',
            file_name=label + '.txt', relative_path=f'media/input/{label}.txt', size_bytes=1,
            sha256=hashlib.sha256(label.encode()).hexdigest())
        key = hashlib.sha256(f'source|{label}|same|event'.encode()).hexdigest()
        anchors.reserve(home, key, now='now')
        assert isinstance(anchors.mark_sent(home, key, root_message_id='om_' + label, thread_id='', now='sent'), dict)
        assert isinstance(remember_topic_seed(home, chat_id='same', root_message_id='root', original_text=label), dict)
    assert read_inbound_notes(left)[0]['file_name'] == 'left.txt'
    assert read_inbound_notes(right)[0]['file_name'] == 'right.txt'
    args = {'chat_id': 'same', 'root_message_id': 'root', 'thread_id': 'topic', 'message_id': 'first'}
    assert take_topic_seed(left, **args)['original_text'] == 'left'
    assert take_topic_seed(right, **args)['original_text'] == 'right'
    before = {path.relative_to(right).as_posix(): path.read_bytes()
              for path in right.rglob('*.json')}
    anchors.mark_unknown(left, hashlib.sha256(b'left-only').hexdigest(), now='unknown')
    after = {path.relative_to(right).as_posix(): path.read_bytes()
             for path in right.rglob('*.json')}
    assert before == after


# LLM: 用户偏好来自单次可信 context，不能用共享 config 反写保存；这里只测试选择而不授权外发。
# 函数用途: 验证两种同时存在的用户偏好不互相覆盖全局默认。
def test_two_context_preferences_do_not_mutate_global_config():
    decide = getattr(delivery_policy, 'alert_topic_enabled', None)
    assert callable(decide)
    config = SimpleNamespace(im_topic_aggregation_enabled=True)
    agent = SimpleNamespace(config=config, home_paths=SimpleNamespace(owner_kind='group'))
    enabled = DeliveryContext('feishu', 'oc_same', reply_policy='thread')
    disabled = replace(enabled, reply_policy='reference')
    assert decide(agent, enabled) is True
    assert decide(agent, disabled) is False
    assert decide(agent, enabled) is True and config.im_topic_aggregation_enabled is True
    config.im_topic_aggregation_enabled = False
    assert decide(agent, enabled) is True and decide(agent, disabled) is False
    assert config.im_topic_aggregation_enabled is False
