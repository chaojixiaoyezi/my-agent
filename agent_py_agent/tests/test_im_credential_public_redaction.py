"""公开消息脱敏从原事件到真实投递边界；只使用虚构凭据与假运输。"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.log_redaction import redact_sensitive_text, redact_sensitive_value
from agent_py_agent.agent.conversation.channels import project_user_reply
from agent_py_agent.agent.delivery import (
    ChannelAdapterRegistry,
    ChannelCapabilities,
    DeliveryContext,
    DeliveryService,
    ReplyEnvelope,
)
from agent_py_agent.agent.gateway_parts.http_handlers import _read_public_progress_events
from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter
from agent_py_agent.tests.test_im_progress_draft import Adapter


@pytest.mark.parametrize('key', ['PASS', 'lg_pass', 'DB_PASSWORD', 'api_pw', 'passphrase', 'account.pass'])
@pytest.mark.parametrize('secret', ['12', 'SyntheticOnly-987', 'synthetic phrase with spaces', 'x\\" y'])
def test_credential_aliases_and_quoted_values_are_redacted(key, secret):
    raw = f'{key}="{secret}" python3 task.py'
    assert secret not in redact_sensitive_text(raw)
    assert secret not in project_user_reply(raw).content
    assert redact_sensitive_value({key: secret})[key] == '<redacted>'


def test_partial_quoted_value_does_not_leak_tail_and_nonsecret_fields_remain():
    raw = 'LG_PASS="Synthetic phrase still streaming'
    assert 'Synthetic' not in project_user_reply(raw).content
    normal = 'max_tokens=123 token_count=17 password_hint=long_label model=ordinary'
    assert redact_sensitive_text(normal) == normal


@pytest.mark.parametrize('channel,target', [('feishu', 'oc_group'), ('feishu', 'ou_user'), ('chat', 'local-agent')])
def test_same_secret_is_hidden_in_progress_edit_final_and_chunk(tmp_path, channel, target):
    secret = 'SyntheticOnlyValue-987'
    raw = 'prefix ' * 60 + f'LG_PASS="{secret} with spaces" python3 task.py'
    writer = BufferedChunkStreamWriter(tmp_path/'chunks.jsonl', delivery_channel=channel)
    writer.set_verbose_level('full')
    writer.write_progress({'tool': 'run_command', 'status': '开始', 'detail': raw, 'output': raw}, raw)
    writer.close()
    assert secret not in writer.chunk_path.read_text()
    events, _ = _read_public_progress_events(writer.chunk_path, 0, channel=channel)
    assert secret not in str(events)
    adapter = Adapter()
    registry = ChannelAdapterRegistry()
    registry.register_adapter(channel, adapter, capabilities=ChannelCapabilities(edit_progress=True))
    service = DeliveryService(registry)
    context = DeliveryContext(channel=channel, target=target, mode='reply', idempotency_key='synthetic-preview')
    first = service.deliver_progress(context, ReplyEnvelope(content=raw))
    assert first.ok
    assert secret not in next(iter(adapter.created.values()))[2]
    assert service.deliver_progress(context, ReplyEnvelope(content=raw), message_id=first.message_id).ok
    assert secret not in adapter.edits[-1][1]
    final = service.deliver(context, ReplyEnvelope(content=raw))
    assert secret not in final.content


def test_tool_progress_sanitizes_complete_input_before_small_clip():
    from agent_py_agent.agent.agent_core.tool_loop.round_execution import _public_progress_text
    event = SimpleNamespace(request=SimpleNamespace(agent=SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=''))))
    text = 'LG_PASS="' + 'SyntheticSecretPart' * 80 + '" run_task'
    result = _public_progress_text(event, text, max_chars=100)
    assert 'SyntheticSecretPart' not in result
