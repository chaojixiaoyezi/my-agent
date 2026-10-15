"""WS 观测只用合成数据；不启动 SDK 连接，不读取真实配置或卡片。"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
from io import StringIO
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.adapter import feishu_ws
from agent_py_agent.agent.adapter import feishu_ws_observation as observation
from agent_py_agent.agent.adapter.protocol import provider_message_reference

CANARY = 'ARBITRARY_FORM_SECRET_CANARY_7c8d'


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    attempts = []
    def forbidden(*_args, **_kwargs):
        attempts.append(True)
        pytest.fail('WS 观测测试禁止建立任何网络连接')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    return attempts


# LLM: 帧的 payload 属性禁止读取；测试只允许观察固定帧头并将同一对象交回原调用。
# 类用途: 防止未来诊断代码为了提取事件类型而读取表单或正文。
class MetadataOnlyFrame:
    def __init__(self, kind='event'):
        self.headers = [SimpleNamespace(key=key, value=value) for key, value in (
            ('type', kind), ('message_id', CANARY + '-message'), ('trace_id', CANARY + '-trace'),
            ('unrecognized-' + CANARY, CANARY))]

    @property
    def payload(self):
        raise AssertionError('观测层不得读取 payload')


def test_frame_hook_delegates_same_object_once_and_logs_only_safe_metadata(caplog):
    frame, seen, result = MetadataOnlyFrame(CANARY), [], object()
    async def original(value):
        seen.append(value)
        return result
    client = SimpleNamespace(_handle_data_frame=original, _conn_id=CANARY + '-connection')
    with caplog.at_level(logging.INFO):
        observation.install_ws_observation(client, card_handler_registered=True)
        installed = client._handle_data_frame
        observation.install_ws_observation(client, card_handler_registered=True)
        assert client._handle_data_frame is installed
        assert asyncio.run(client._handle_data_frame(frame)) is result
    assert seen == [frame]
    assert 'frame_type=unknown' in caplog.text and 'stage=returned' in caplog.text
    assert provider_message_reference(CANARY + '-message') in caplog.text
    assert provider_message_reference(CANARY + '-connection') in caplog.text
    assert CANARY not in caplog.text


def test_frame_hook_preserves_original_exception_and_observation_failure(monkeypatch, caplog):
    failure = RuntimeError(CANARY)
    async def original(_frame):
        raise failure
    client = SimpleNamespace(_handle_data_frame=original, _conn_id=CANARY)
    with caplog.at_level(logging.INFO):
        observation.install_ws_observation(client, card_handler_registered=True)
        with pytest.raises(RuntimeError) as caught:
            asyncio.run(client._handle_data_frame(MetadataOnlyFrame()))
    assert caught.value is failure and 'stage=exception' in caplog.text
    assert CANARY not in caplog.text
    monkeypatch.setattr(observation.logger, 'info', lambda *_: (_ for _ in ()).throw(OSError(CANARY)))
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(client._handle_data_frame(MetadataOnlyFrame()))
    assert caught.value is failure


def test_unknown_sdk_entry_only_reports_unavailable_and_stage_is_fixed(caplog):
    client = SimpleNamespace(_conn_id=CANARY)
    with caplog.at_level(logging.INFO):
        observation.install_ws_observation(client, card_handler_registered=False)
        observation.log_card_stage(client, CANARY, message_id=CANARY)
    assert 'frame_hook=False' in caplog.text and 'stage=unknown' in caplog.text
    assert CANARY not in caplog.text


# LLM: 假卡仍由正式归一化、去重和回执代码处理；值及表单仅用于断言日志不泄漏，不代表真实 provider 点击。
# 函数用途: 构造包含任意秘密 canary 的 typed 卡片输入，不访问真实卡或凭据。
def _card_event():
    return SimpleNamespace(event=SimpleNamespace(
        action=SimpleNamespace(value={'kind': 'my_agent_task_credential', 'reference': CANARY,
            'decision': 'add'}, form_value={'password': CANARY}),
        operator=SimpleNamespace(open_id=CANARY),
        context=SimpleNamespace(open_message_id='om_synthetic', open_chat_id='oc_synthetic')))


@pytest.mark.parametrize(('response', 'stage'), [
    ({'toast': {'type': 'success', 'content': CANARY}}, 'success'),
    ({'toast': {'type': 'warning', 'content': CANARY}}, 'warning'),
    ({'toast': {'type': 'error', 'content': CANARY}}, 'rejected'),
    ({'toast': {'type': CANARY, 'content': CANARY}}, 'no_response'),
    (None, 'no_response'),
])
def test_card_handler_logs_fixed_response_stage_without_content(monkeypatch, caplog, response, stage):
    monkeypatch.setattr(feishu_ws, '_card_response', lambda value: value)
    client = feishu_ws.FeishuWsClient(app_id='synthetic', app_secret=CANARY, on_payload=lambda _: None)
    client._client = SimpleNamespace(_conn_id=CANARY)
    client.on_card_action = lambda _norm: response
    with caplog.at_level(logging.INFO):
        assert client._handle_card_action(_card_event()) == response
        if stage == 'success':
            assert client._handle_card_action(_card_event()) == response
            assert 'stage=duplicate_cached' in caplog.text
    assert 'stage=entered' in caplog.text and f'stage={stage}' in caplog.text
    assert CANARY not in caplog.text


def test_card_invalid_inflight_and_exception_are_observable_without_secret(monkeypatch, caplog):
    client = feishu_ws.FeishuWsClient(app_id='synthetic', app_secret=CANARY, on_payload=lambda _: None)
    client._client = SimpleNamespace(_conn_id=CANARY)
    client.on_card_action = lambda _: (_ for _ in ()).throw(RuntimeError(CANARY))
    with caplog.at_level(logging.INFO):
        assert client._handle_card_action(SimpleNamespace()) is None
        assert client._handle_card_action(_card_event()) is None
        monkeypatch.setattr(client, '_claim_message', lambda _: 'inflight')
        assert client._handle_card_action(_card_event()) is None
    assert all(f'stage={stage}' in caplog.text for stage in ('invalid', 'exception', 'claim_rejected'))
    assert CANARY not in caplog.text


# LLM: 使用已安装官方 SDK 的原 dispatcher 和原帧处理；Client 不初始化、不连接，仅替换最终写入为内存收集。
# 函数用途: 对照 event 与 card 的真实 SDK 分支，防止诊断改变 handler 次数、原 CARD 跳过行为或 ACK。
def _sdk_case():
    import lark_oapi as lark
    from lark_oapi.ws.client import Client

    owner = feishu_ws.FeishuWsClient(app_id='synthetic', app_secret=CANARY, on_payload=lambda _: None)
    calls, acknowledgements = [], []
    owner.on_card_action = lambda norm: calls.append(norm) or {'toast': {'type': 'success', 'content': 'synthetic'}}
    handler = lark.EventDispatcherHandler.builder('', '').register_p2_card_action_trigger(owner._handle_card_action).build()
    client = object.__new__(Client)
    client._event_handler, client._conn_id = handler, CANARY + '-connection'
    async def fake_write(data):
        acknowledgements.append(data)
    client._write_message = fake_write
    owner._client = client
    return client, calls, acknowledgements


# LLM: 合成原始帧包含表单 canary；序列化仅供原 SDK 消费，观测层不能解码或替代分发。
# 函数用途: 构造两种运输类型共用的新版回调载荷，验证原 SDK 合同。
def _sdk_frame(kind):
    from lark_oapi.ws.pb.pbbp2_pb2 import Frame
    body = {'schema': '2.0', 'header': {'event_type': 'card.action.trigger'}, 'event': {
        'operator': {'open_id': 'ou_synthetic'}, 'action': {'value': {'kind': 'my_agent_model_configuration',
            'reference': CANARY, 'decision': 'add'}, 'form_value': {'arbitrary_field': CANARY}},
        'context': {'open_message_id': 'om_synthetic', 'open_chat_id': 'oc_synthetic'}}}
    frame = Frame(SeqID=1, LogID=1, service=1, method=1, payload=json.dumps(body).encode())
    for key, value in {'type': kind, 'message_id': CANARY + '-message', 'trace_id': CANARY + '-trace', 'sum': '1', 'seq': '0'}.items():
        header = frame.headers.add()
        header.key, header.value = key, value
    return frame


@pytest.mark.parametrize(('kind', 'expected'), [('event', 1), ('card', 0)])
def test_official_sdk_event_dispatch_and_card_ack_remain_unchanged(caplog, kind, expected):
    client, calls, acknowledgements = _sdk_case()
    frame = _sdk_frame(kind)
    original = frame.SerializeToString()
    with caplog.at_level(logging.INFO):
        observation.install_ws_observation(client, card_handler_registered=True)
        asyncio.run(client._handle_data_frame(frame))
    assert len(calls) == len(acknowledgements) == expected
    if not expected:
        assert frame.SerializeToString() == original
        assert 'feishu_ws_card' not in caplog.text
    else:
        assert 'feishu_ws_card stage=success' in caplog.text
    assert f'frame_type={kind}' in caplog.text and CANARY not in caplog.text


def test_start_installs_observation_on_the_single_sdk_instance(monkeypatch, caplog):
    import lark_oapi as lark
    observed = []
    def fake_start(client):
        observed.append(client)
        assert client._my_agent_frame_observed is True
        assert 'p2.card.action.trigger' in client._event_handler._callback_processor_map
    monkeypatch.setattr(lark.ws.Client, 'start', fake_start)
    monkeypatch.setattr(feishu_ws, '_patch_ws_connect', lambda _: None)
    client = feishu_ws.FeishuWsClient(app_id='synthetic', app_secret=CANARY, on_payload=lambda _: None)
    client.on_card_action = lambda _: None
    with caplog.at_level(logging.INFO):
        client.start()
    assert observed == [client._client]
    assert 'frame_hook=True card_handler_registered=True' in caplog.text
    assert CANARY not in caplog.text


# LLM: 安装正式 logger 过滤器并保留两个真实 StreamHandler 格式化路径；只输出到内存，退出恢复全局 logger。
# 函数用途: 验证 SDK 自带 handler 和额外 handler 都不能绕过任意异常、标识与 traceback 的安全边界。
@pytest.fixture
def sdk_log_capture(monkeypatch):
    sdk_logger = logging.getLogger('Lark')
    streams = [StringIO(), StringIO()]
    handlers = [logging.StreamHandler(stream) for stream in streams]
    monkeypatch.setattr(sdk_logger, 'handlers', handlers)
    monkeypatch.setattr(sdk_logger, 'filters', [])
    monkeypatch.setattr(sdk_logger, 'level', logging.INFO)
    monkeypatch.setattr(sdk_logger, 'propagate', False)
    monkeypatch.setattr(feishu_ws, '_lark_noise_filtered', False)
    feishu_ws._suppress_lark_processor_not_found()
    return sdk_logger, streams


def test_real_sdk_dispatch_exception_logs_fixed_facts_and_preserves_failure_ack(sdk_log_capture, no_network):
    client, _calls, acknowledgements = _sdk_case()
    def fail(_payload):
        raise ValueError(CANARY + '-arbitrary-field')
    client._event_handler = SimpleNamespace(_do_without_validation=fail)
    observation.install_ws_observation(client, card_handler_registered=True)
    asyncio.run(client._handle_data_frame(_sdk_frame('event')))
    assert len(acknowledgements) == 1 and not no_network
    from lark_oapi.ws.pb.pbbp2_pb2 import Frame
    ack = Frame()
    ack.ParseFromString(acknowledgements[0])
    assert json.loads(ack.payload)['code'] == 500
    for stream in sdk_log_capture[1]:
        rendered = stream.getvalue()
        assert 'stage=dispatch_failed error_type=ValueError' in rendered
        assert CANARY not in rendered and 'Traceback' not in rendered


def test_real_sdk_outer_invalid_type_logs_fixed_error_without_ack(sdk_log_capture, no_network):
    client, _calls, acknowledgements = _sdk_case()
    observation.install_ws_observation(client, card_handler_registered=True)
    asyncio.run(client._handle_message(_sdk_frame(CANARY + '-type').SerializeToString()))
    assert not acknowledgements and not no_network
    for stream in sdk_log_capture[1]:
        assert 'stage=frame_failed error_type=ValueError' in stream.getvalue()
        assert CANARY not in stream.getvalue()


def test_real_sdk_connect_error_keeps_code_and_exception_without_url(sdk_log_capture, no_network):
    from lark_oapi.ws.exception import ClientException
    client, _calls, _acks = _sdk_case()
    failure = ClientException(1000040350, 'wss://example.invalid/' + CANARY)
    async def fail():
        raise failure
    client._connect = fail
    with pytest.raises(ClientException) as caught:
        asyncio.run(client._try_connect(0))
    assert caught.value is failure and not no_network
    for stream in sdk_log_capture[1]:
        rendered = stream.getvalue()
        assert 'stage=connect_failed error_type=ClientException provider_code=1000040350' in rendered
        assert CANARY not in rendered and 'wss://' not in rendered


def test_real_sdk_http_dispatch_error_clears_traceback_but_keeps_response(sdk_log_capture, no_network):
    import lark_oapi as lark
    from lark_oapi.core.model import RawRequest
    handler = lark.EventDispatcherHandler.builder('', '').build()
    def fail(_body):
        raise ValueError(CANARY)
    handler._decrypt = fail
    request = RawRequest()
    request.body, request.uri, request.headers = b'{}', CANARY, {'X-Request-Id': CANARY}
    result = handler.do(request)
    assert result.status_code == 500 and not no_network
    for stream in sdk_log_capture[1]:
        assert 'stage=event_failed error_type=ValueError' in stream.getvalue()
        assert CANARY not in stream.getvalue() and 'Traceback' not in stream.getvalue()


def test_other_sdk_source_logs_are_retained_with_shared_redaction(sdk_log_capture):
    sdk_log_capture[0].warning('unrelated SDK diagnostic %s', 'retained')
    for stream in sdk_log_capture[1]:
        assert 'unrelated SDK diagnostic retained' in stream.getvalue()
