"""告警根与主群 M0 的首消息种子、重试恢复和准确线程隔离。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from agent_py_agent.agent.adapter import topic_seed
from agent_py_agent.agent.adapter.manager import _ChannelIngressCoordinator
from agent_py_agent.tests.test_inbound_media_ownership import _manager, _message


# LLM: 输入只含临时 Owner 的合成告警和附件引用。
# 函数用途: 保存一个有上下文、来源和 run 引用的话题根。
def _save(home, *, text='原始告警正文'):
    return topic_seed.remember_topic_seed(home, chat_id='oc_fixture', root_message_id='om_root',
        title='示例告警', source='fixture-source', event_id='evt-1', original_text=text,
        run_refs=('run-one',), attachments=({'relative_path': 'media/input/a.txt', 'file_name': 'a.txt'},))


# LLM: 只把种子交给第一个精确消息；同一消息重放必须返回相同资料，不能丢掉已消费上下文。
# 函数用途: 验证一次注入、首消息恢复及不同 root 不串读。
def test_seed_is_once_per_thread_but_first_message_retry_is_lossless(tmp_path):
    assert isinstance(_save(tmp_path), dict)
    args = {'chat_id': 'oc_fixture', 'root_message_id': 'om_root', 'thread_id': 'omt_fixture', 'message_id': 'om_first'}
    seed = topic_seed.take_topic_seed(tmp_path, **args)
    assert isinstance(seed, dict)
    assert seed['title'] == '示例告警' and seed['source'] == 'fixture-source' and seed['event_id'] == 'evt-1'
    assert seed['original_text'] == '原始告警正文' and seed['run_refs'] == ['run-one']
    assert seed['attachments'][0]['relative_path'] == 'media/input/a.txt'
    assert topic_seed.take_topic_seed(tmp_path, **args) == seed
    assert topic_seed.take_topic_seed(tmp_path, **{**args, 'message_id': 'om_second'}) is None
    assert topic_seed.take_topic_seed(tmp_path, **{**args, 'chat_id': 'oc_other'}) is None
    assert topic_seed.take_topic_seed(tmp_path, **{**args, 'thread_id': 'omt_other'}) is None


# LLM: UTF-8 字节而非字符上限防止中文原文突破 4KiB，保存不暴露凭据赋值。
# 函数用途: 验证根正文有界并保留规范相对附件定位。
def test_seed_root_text_has_a_4kib_byte_limit(tmp_path):
    result = _save(tmp_path, text='中文说明' * 2000)
    assert isinstance(result, dict)
    assert 0 < len(result['original_text'].encode()) <= 4096


# LLM: 两条消息确定竞争同一临时种子时，必须仅有一条获得首消息身份。
# 函数用途: 验证并发首消息只注入一次。
def test_two_first_messages_do_not_both_claim_the_seed(tmp_path):
    assert isinstance(_save(tmp_path), dict)
    def take(message_id):
        return topic_seed.take_topic_seed(tmp_path, chat_id='oc_fixture', root_message_id='om_root',
            thread_id='omt_fixture', message_id=message_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(take, ('first-a', 'first-b')))
    assert sum(row is not None for row in results) == 1


# LLM: 使用生产 ingress 还原所需的准确字段，不改变身份、请求或媒体下载器。
# 函数用途: 将合成 IncomingMessage 作为可重放的持久入站资料。
def _record(msg):
    return SimpleNamespace(kind='message', content=msg.content, channel=msg.channel, user_id=msg.user_id,
        provider_message_id=msg.message_id, conversation_id=msg.conversation_id,
        timestamp=msg.timestamp, metadata=dict(msg.metadata))


# LLM: 真正 prepare_payload 在完整下载后写入 M0；第一次 prepare 后故意不提交 payload，再重放同条持久行。
# 函数用途: 验证主群正文、下载附件跨新话题且准备重试不丢种子。
def test_ingress_prepare_bridges_m0_and_attachment_and_retries_losslessly(tmp_path, monkeypatch):
    manager, _ = _manager(tmp_path, monkeypatch)
    coordinator = _ChannelIngressCoordinator(manager)
    root = _message('ou_one', group='oc_fixture', message_id='om_root')
    root.content = '原始主群说明'
    coordinator.prepare_payload(_record(root))
    child = _message('ou_one', group='oc_fixture', message_id='om_first')
    child.content = '接着处理'
    child.conversation_id = 'oc_fixture:thread:omt_fixture'
    child.metadata.pop('media')
    child.metadata.update(feishu_thread_id='omt_fixture', feishu_root_id='om_root')
    record = _record(child)
    first = coordinator.prepare_payload(record)
    assert '原始主群说明' in first['prompt']
    assert 'om_root-example.txt' in first['prompt']
    assert first == coordinator.prepare_payload(record)
    child.message_id = 'om_later'
    later = coordinator.prepare_payload(_record(child))
    assert later['prompt'] == '接着处理'
    assert first['metadata']['channel_conversation_id'] == child.conversation_id
