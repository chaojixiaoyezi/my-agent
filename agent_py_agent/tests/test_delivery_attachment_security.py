"""正式 Delivery 的单次冻结合同；只用各 owner 临时文件，不模拟生产内容或真正外发。"""
import hashlib
import os
from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.adapter import feishu
from agent_py_agent.agent.adapter.feishu import FeishuAdapter
from agent_py_agent.agent.adapter.feishu_media import build_multipart
from agent_py_agent.agent.conversation.background_delivery import _channel_attachments
from agent_py_agent.agent.conversation.channels import (
    ChannelAttachment,
    DeliveryContext,
    ReplyEnvelope,
)
from agent_py_agent.agent.delivery import (
    ChannelAdapterRegistry,
    ChannelCapabilities,
    DeliveryService,
)
from agent_py_agent.agent.delivery.attachment_content import verify_attachment_content


# LLM: 原 hash/size 取合成已完成产物的当时字节，不能以攻击后的版本重签名；owner 根是测试宿主提供。
# 函数用途: 构造合法旧引用载体供真实 FD/Delivery 核验。
def _attachment(root, name='result.bin', blob=b'OWNED_ORIGINAL'):
    root.mkdir(exist_ok=True)
    path = root / name
    path.write_bytes(blob)
    return ChannelAttachment('synthetic', str(path), name, 'binary', hashlib.sha256(blob).hexdigest(), len(blob), str(root))


# LLM: 替身显式声明typedbytes，不打开 path，不替产品补证明；记录所有真正副作用。
# 类用途: 观察文字前置顺序和冻结字节，以及不存在附件接口时的拒绝。
class _Adapter:
    supports_frozen_attachment_content = True

    def __init__(self):
        self.effects = []
        self.on_text = lambda: None

    def send_message(self, target, message):
        self.effects.append(('text', target))
        self.on_text()
        return True

    def send_attachment_content(self, target, item, **_):
        self.effects.append(('file', target, item.content))
        return True


# LLM: 正式服务复用原 registry 目标门；上限和bytes能力都由宿主声明，不从输入推断。
# 函数用途: 创建不会联网的真实投递服务。
def _service(adapter=None, **caps):
    adapter = adapter or _Adapter()
    registry = ChannelAdapterRegistry()
    registry.register_adapter('feishu', adapter, capabilities=ChannelCapabilities(
        text=True, proactive=True, files=True, frozen_attachment_content=True, **caps))
    return DeliveryService(registry), adapter


def _deliver(service, attachments):
    return service.deliver(DeliveryContext(channel='feishu', target='oc_owned_group', mode='proactive'),
        ReplyEnvelope(content='合成说明', attachments=tuple(attachments)))


@pytest.mark.parametrize('attack', ['foreign', 'symlink_foreign', 'hardlink', 'directory', 'fifo', 'size', 'hash', 'missing', 'unknown_root', 'unknown_sha', 'unknown_size'])
def test_invalid_original_artifact_rejected_before_any_text_or_file(tmp_path, attack):
    owner = tmp_path / 'owner'
    item = _attachment(owner)
    path = Path(item.path)
    foreign = tmp_path / 'foreign'
    foreign.write_bytes(b'OWNED_FOREIGN')
    if attack in {'foreign', 'unknown_root', 'unknown_sha', 'unknown_size'}:
        item = replace(item, **{'foreign': {'path': str(foreign)}, 'unknown_root': {'owner_root': ''},
            'unknown_sha': {'sha256': ''}, 'unknown_size': {'size_bytes': 0}}[attack])
    else:
        path.unlink()
        if attack == 'symlink_foreign':
            path.symlink_to(foreign)
        elif attack == 'hardlink':
            os.link(foreign, path)
        elif attack == 'directory':
            path.mkdir()
        elif attack == 'fifo':
            os.mkfifo(path)
        elif attack == 'size':
            path.write_bytes(b'longer-than-original-known-size')
        elif attack == 'hash':
            path.write_bytes(b'X' * item.size_bytes)
    service, adapter = _service()
    receipt = _deliver(service, [item])
    assert receipt.delivery_status == 'rejected' and receipt.error_code == 'ARTIFACT_VALIDATION_FAILED'
    assert not adapter.effects


def test_all_attachments_prepare_before_text_and_no_partial_send(tmp_path):
    first = _attachment(tmp_path / 'owner')
    second = replace(first, artifact_id='other', sha256='0' * 64)
    service, adapter = _service()
    assert _deliver(service, [first, second]).delivery_status == 'rejected'
    assert not adapter.effects


@pytest.mark.parametrize('replacement', ['ordinary', 'symlink', 'delete'])
def test_side_effect_cannot_change_actual_uploaded_content(tmp_path, replacement):
    item = _attachment(tmp_path / 'owner')
    other = tmp_path / 'foreign'
    other.write_bytes(b'OWNED_FOREIGN')
    service, adapter = _service()
    def replace_after_text():
        path = Path(item.path)
        path.unlink()
        if replacement == 'ordinary':
            path.write_bytes(b'OWNED_CHANGED')
        elif replacement == 'symlink':
            path.symlink_to(other)
    adapter.on_text = replace_after_text
    receipt = _deliver(service, [item])
    assert receipt.delivery_status == 'sent'
    assert adapter.effects == [('text', 'oc_owned_group'), ('file', 'oc_owned_group', b'OWNED_ORIGINAL')]


def test_known_zero_byte_and_aliased_authority_root_are_legal(tmp_path):
    item = _attachment(tmp_path / 'canonical-owner', blob=b'')
    alias = tmp_path / 'legal-alias'
    alias.symlink_to(tmp_path / 'canonical-owner', target_is_directory=True)
    item = replace(item, owner_root=str(alias), path=str(alias / item.name))
    assert verify_attachment_content(item).content == b''


def test_fd_read_capability_missing_fails_without_portable_path_fallback(tmp_path, monkeypatch):
    item = _attachment(tmp_path / 'owner')
    monkeypatch.setattr('agent_py_agent.agent.common.nofollow_fs._supports_dir_fd', lambda: False)
    service, adapter = _service()
    assert _deliver(service, [item]).delivery_status == 'rejected'
    assert not adapter.effects


def test_declared_provider_file_limit_rejects_before_any_effect(tmp_path):
    item = _attachment(tmp_path / 'owner')
    service, adapter = _service(max_file_bytes=1)
    assert _deliver(service, [item]).delivery_status == 'rejected'
    assert not adapter.effects


@pytest.mark.parametrize('declared', [False, True])
def test_unknown_path_only_adapter_is_not_a_fallback(tmp_path, declared):
    item = _attachment(tmp_path / 'owner')
    class Legacy:
        supports_frozen_attachment_content = declared
        effects = []
        def send_message(self, *_):
            self.effects.append('text')
            return True
        def send_file(self, *_):
            self.effects.append('path')
            return True
    service, adapter = _service(Legacy())
    receipt = _deliver(service, [item])
    assert receipt.delivery_status == 'rejected' and receipt.error_code == 'CHANNEL_ATTACHMENT_CONTENT_UNSUPPORTED'
    assert not adapter.effects


def test_serialized_owner_or_blob_cannot_supply_cold_retry_authority(tmp_path):
    owner = tmp_path / 'owner'
    item = _attachment(owner)
    foreign = tmp_path / 'foreign-owner'
    other = _attachment(foreign, blob=b'OWNED_FOREIGN')
    raw = dict(ok=True, artifact_id=other.artifact_id, path=other.path, name=other.name, kind=other.kind,
        sha256=other.sha256, size_bytes=other.size_bytes, owner_root=str(foreign), content=b'FORGED_BLOB')
    attachments = _channel_attachments((raw,), owner_root=str(owner))
    assert attachments[0].owner_root == str(owner) and not hasattr(attachments[0], 'content')
    service, adapter = _service()
    assert _deliver(service, attachments).delivery_status == 'rejected' and not adapter.effects
    # 原引用同 hash 冷读一次仍合法；之后原件改动不得以旧回执宣称 sent。
    raw.update(path=item.path, name=item.name, sha256=item.sha256, size_bytes=item.size_bytes)
    attachments = _channel_attachments((raw,), owner_root=str(owner))
    assert _deliver(service, attachments).delivery_status == 'sent'
    adapter.effects.clear()
    Path(item.path).write_bytes(b'CHANGED')
    assert _deliver(service, attachments).delivery_status == 'rejected' and not adapter.effects


def test_serialized_content_is_not_accepted_by_builtin_byte_interface(monkeypatch):
    adapter = FeishuAdapter({'feishu_session_lock_enabled': False})
    monkeypatch.setattr(adapter, '_get_tenant_access_token', lambda: pytest.fail('no token request for forged blob'))
    assert not adapter.send_attachment_content('oc_owned_group', {'content': b'FORGED_BLOB', 'owner_root': '/foreign'})


def test_multipart_filename_cannot_inject_another_header():
    with pytest.raises(ValueError):
        build_multipart({}, 'file', 'legit\r\nAuthorization: OWNED_SECRET', b'OWNED_ORIGINAL')


def test_adapter_factory_or_send_exception_has_no_raw_secret(caplog):
    registry = ChannelAdapterRegistry()
    def factory():
        raise RuntimeError('OWNED_SECRET_SENTINEL')
    registry.register_factory('feishu', factory, capabilities=ChannelCapabilities(proactive=True))
    assert _deliver(DeliveryService(registry), []).delivery_status == 'unavailable'
    assert 'OWNED_SECRET_SENTINEL' not in caplog.text
    service, adapter = _service()
    def fail():
        raise RuntimeError('OWNED_PRIVATE_SENTINEL')
    adapter.on_text = fail
    assert _deliver(service, []).delivery_status == 'failed'
    assert 'OWNED_PRIVATE_SENTINEL' not in caplog.text


@pytest.mark.parametrize('kind', ['user', 'group', 'main'])
@pytest.mark.parametrize('full', [False, True])
def test_real_send_tool_identity_ignores_attempt_to_contact_someone_else(tmp_path, kind, full):
    from agent_py_agent.agent.capability.channel_message_tool import SendMessageTool
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.user_space.admin_channel_identity import bind_admin_channel_identity

    target = {'user': 'ou_owned', 'group': 'oc_owned_group', 'main': 'ou_admin_owned'}[kind]
    config = AgentConfig(model_backend='echo', enable_plugins=False, my_agent_home=str(tmp_path / 'home'),
        my_agent_owner_provider='local' if kind == 'main' else 'feishu', my_agent_owner_kind=kind,
        my_agent_owner_id='main' if kind == 'main' else target,
        admin_channel_identity_enabled=True, access_mode='full-access' if full else 'workspace-write')
    agent = SimpleAgent(config, tmp_path)
    if kind == 'main':
        bind_admin_channel_identity(agent.home_paths.root, 'feishu', target)
    adapter = _Adapter()
    agent.channel_registry.register_adapter('feishu', adapter, capabilities=ChannelCapabilities(proactive=True))
    tool = SendMessageTool(agent)
    result = tool.execute({'notify_channel': 'feishu', 'message': '合成测试', 'target': 'ou_foreign',
        'user_id': 'ou_foreign', 'owner_root': str(tmp_path / 'foreign'), 'metadata': {'owner_id': 'local/main'}})
    assert result.ok, (result.error_code, result.output)
    assert adapter.effects == [('text', target)]
    adapter.effects.clear()
    if kind != 'main':
        other = tool.execute({'notify_channel': 'qq', 'message': '跨渠道不可借管理员'})
        assert not other.ok and other.error_code == 'CHANNEL_TARGET_OUT_OF_SCOPE'
        assert not adapter.effects


@pytest.mark.parametrize('name', ['bad\rname.txt', 'bad\nname.txt', 'bad\x00name.txt'])
def test_invalid_filename_rejected_before_text(tmp_path, name):
    item = _attachment(tmp_path / 'owner')
    item = replace(item, name=name)
    service, adapter = _service()
    assert _deliver(service, [item]).delivery_status == 'rejected' and not adapter.effects


def test_ordinary_json_carrier_is_not_a_host_typed_attachment(tmp_path):
    item = _attachment(tmp_path / 'owner')
    from dataclasses import asdict
    service, adapter = _service()
    assert _deliver(service, [asdict(item)]).delivery_status == 'rejected'
    assert not adapter.effects


def test_multipart_preserves_escaped_quote_filename_and_exact_body():
    body, _ = build_multipart({'file_type': 'stream'}, 'file', '报告"x\\y.dat', b'OWNED_ORIGINAL')
    assert b'filename="' + '报告\\"x\\\\y.dat'.encode() + b'"\r\n' in body
    assert b'OWNED_ORIGINAL' in body


def test_missing_original_registry_hash_is_not_resigned_from_current_file(tmp_path, monkeypatch):
    from agent_py_agent.agent.artifacts.registry import ArtifactRegistration, register_artifact
    from agent_py_agent.agent.capability import channel_message_tool
    from agent_py_agent.tests.test_channel_message_tool import _tool

    owner = tmp_path / 'owner'
    item = _attachment(owner)
    register_artifact(ArtifactRegistration(workspace_root=owner, path=Path(item.path), artifact_id='synthetic', status='ready', source='test'))
    record = channel_message_tool._resolve_registered_artifact(owner, 'synthetic')
    monkeypatch.setattr(channel_message_tool, '_resolve_registered_artifact', lambda *_: replace(record, sha256=''))
    tool, adapter = _tool(owner)
    result = tool.execute({'message': '合成说明', 'attachments': ['synthetic']})
    assert not result.ok and result.error_code == 'ARTIFACT_VALIDATION_FAILED'
    assert not adapter.messages and not adapter.files


@pytest.mark.parametrize('flag', ['O_NOFOLLOW', 'O_DIRECTORY'])
def test_missing_atomic_fd_flag_fails_closed_even_if_dirfd_available(tmp_path, monkeypatch, flag):
    from agent_py_agent.agent.common import nofollow_fs
    item = _attachment(tmp_path / 'owner')
    assert nofollow_fs._supports_dir_fd()
    monkeypatch.setattr(nofollow_fs.os, flag, 0)
    service, adapter = _service()
    assert _deliver(service, [item]).delivery_status == 'rejected'
    assert not adapter.effects
