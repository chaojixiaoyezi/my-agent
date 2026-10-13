"""真实文件下载入口复用 Gateway 身份解析，模拟供应商字节而不发送线上消息。"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.adapter.feishu import _fetch_media_to_dir
from agent_py_agent.agent.adapter.manager import ChannelManager, _inbound_media_destination
from agent_py_agent.agent.adapter.protocol import IncomingMessage
from agent_py_agent.agent.conversation.input_media import owner_input_media_root
from agent_py_agent.agent.path_access_policy import PathAccessPolicy
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.admin_channel_identity import bind_admin_channel_identity
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home


# LLM: 仅供应商资源下载替身，归属解析、私有落盘和路径授权使用生产实现。
# 函数用途: 准备两个身份共用的通道管理器，不能从 adapter 工作目录选择 owner。
def _manager(tmp_path, monkeypatch):
    root=tmp_path/'data'
    root.mkdir()
    agent=SimpleNamespace(home_paths=SimpleNamespace(root=root), config=AgentConfig())
    manager=ChannelManager(delivery_state_dir=tmp_path/'delivery')
    manager._inbound_media_agent=agent
    adapter=SimpleNamespace(workspace_root=tmp_path/'wrong-admin-workspace',
        fetch_media_to=lambda message_id,media,destination: _fetch_media_to_dir(message_id,media,destination,'test-token'))
    manager._adapters['feishu']=adapter
    monkeypatch.setattr('agent_py_agent.agent.adapter.feishu._fetch_feishu_resource',lambda *_: b'own attachment')
    return manager,agent


def _message(actor, *, group='', message_id='om_same'):
    return IncomingMessage(channel='feishu',user_id=actor,content='[文件]',message_id=message_id,
        conversation_id=group or 'private-'+actor, metadata={'media':{'file_key':'test-key','file_name':'example.txt'},
        'feishu_chat_type':'group' if group else 'p2p','feishu_chat_id':group or 'private-'+actor})


def test_two_users_receive_same_file_in_their_own_home(tmp_path,monkeypatch):
    manager,agent=_manager(tmp_path,monkeypatch)
    files=[]
    for actor in ('ou_one','ou_two'):
        msg=_message(actor)
        manager._maybe_download_media(msg,retry_on_error=True)
        owner=resolve_owner_home(agent.home_paths.root,OwnerIdentity.provider_user('feishu',actor))
        destination=owner_input_media_root(owner.home_dir)/'om_same-example.txt'
        assert destination.read_bytes()==b'own attachment'
        assert str(destination) in msg.content and 'wrong-admin-workspace' not in msg.content
        assert destination.stat().st_mode & 0o777==0o600
        files.append((owner,destination))
    for owner,destination in files:
        from agent_py_agent.agent.tooling._filesystem_read import (
            ReadFileTool,
            filesystem_access_options,
        )
        policy=PathAccessPolicy.from_values(owner_scope_root=str(owner.home_dir))
        assert policy.check(destination).allowed
        reader=ReadFileTool(owner.home_dir,1000,[owner.home_dir],filesystem_access_options(owner_scope_root=str(owner.home_dir)))
        assert reader.execute({'path':str(destination)}).ok
        other=next(path for _,path in files if path!=destination)
        assert policy.check(other).code=='PATH_CROSS_OWNER_BLOCKED'
        denied=reader.execute({'path':str(other)})
        assert not denied.ok and denied.error_code=='PATH_CROSS_OWNER_BLOCKED'
    assert not (tmp_path/'wrong-admin-workspace').exists()


def test_group_owner_and_bound_admin_private_use_original_routing(tmp_path,monkeypatch):
    manager,agent=_manager(tmp_path,monkeypatch)
    bind_admin_channel_identity(agent.home_paths.root,'feishu','ou_admin')
    for message,identity in [(_message('ou_admin'),OwnerIdentity.local_main()),
            (_message('ou_admin',group='oc_team'),OwnerIdentity.provider_group('feishu','oc_team')),
            (_message('ou_other',group='oc_team'),OwnerIdentity.provider_group('feishu','oc_team'))]:
        manager._maybe_download_media(message,retry_on_error=True)
        owner=resolve_owner_home(agent.home_paths.root,identity)
        assert str(owner_input_media_root(owner.home_dir)) in message.content


def test_missing_actor_or_host_never_falls_back_to_adapter_cwd(tmp_path,monkeypatch):
    manager,_=_manager(tmp_path,monkeypatch)
    with pytest.raises(RuntimeError,match='可信'):
        manager._maybe_download_media(_message(''),retry_on_error=True)
    manager._inbound_media_agent=None
    with pytest.raises(ValueError,match='OWNER_UNAVAILABLE'):
        manager._maybe_download_media(_message('ou_one'),retry_on_error=True)


def test_owner_attachment_directory_symlink_cannot_redirect_download(tmp_path,monkeypatch):
    manager,agent=_manager(tmp_path,monkeypatch)
    msg=_message('ou_one')
    owner=resolve_owner_home(agent.home_paths.root,OwnerIdentity.provider_user('feishu','ou_one'))
    owner.home_dir.mkdir(parents=True)
    foreign=tmp_path/'foreign'
    foreign.mkdir()
    (owner.home_dir/'media').symlink_to(foreign,target_is_directory=True)
    with pytest.raises(OSError):
        _inbound_media_destination(agent,msg)
    assert not list(foreign.iterdir())


def test_retry_is_same_owner_and_unsafe_message_id_is_only_filename_data(tmp_path,monkeypatch):
    manager,agent=_manager(tmp_path,monkeypatch)
    for _ in range(2):
        msg=_message('ou_one',message_id='../../om_1')
        manager._maybe_download_media(msg,retry_on_error=True)
    owner=resolve_owner_home(agent.home_paths.root,OwnerIdentity.provider_user('feishu','ou_one'))
    assert [p.name for p in owner_input_media_root(owner.home_dir).iterdir()]==['om_1-example.txt']


def test_rich_post_all_attachments_keep_owner_and_same_name_files_do_not_overwrite(tmp_path, monkeypatch):
    from agent_py_agent.agent.adapter.protocol import feishu_to_incoming
    manager, agent = _manager(tmp_path, monkeypatch)
    for actor in ('ou_one', 'ou_two'):
        message = feishu_to_incoming({'event': {'sender': {'sender_id': {'open_id': actor}}, 'message': {
            'message_id': 'om_rich', 'chat_id': 'private-'+actor, 'chat_type': 'p2p', 'message_type': 'post',
            'content': {'content': [[{'tag': 'code_block', 'language': 'python', 'text': 'print(123)'}],
                [{'tag': 'file', 'file_key': 'key-1', 'file_name': 'same.txt'},
                 {'tag': 'file', 'file_key': 'key-2', 'file_name': 'same.txt'}]]},
        }}})
        assert message is not None and 'print(123)' in message.content
        manager._maybe_download_media(message, retry_on_error=True)
        owner = resolve_owner_home(agent.home_paths.root, OwnerIdentity.provider_user('feishu', actor))
        paths = sorted(owner_input_media_root(owner.home_dir).iterdir())
        assert len(paths) == 2 and paths[0].name != paths[1].name
        assert all(str(path) in message.content and path.read_bytes() == b'own attachment' for path in paths)


def test_partial_multi_attachment_failure_does_not_submit_partial_paths(tmp_path, monkeypatch):
    manager, _ = _manager(tmp_path, monkeypatch)
    message = _message('ou_one')
    message.metadata['media'] = {'items': [{'file_key': 'key-1'}, {'file_key': 'key-2'}]}
    adapter = manager._adapters['feishu']
    original = adapter.fetch_media_to
    adapter.fetch_media_to = lambda message_id, media, dest: None if media['file_key'] == 'key-2' else original(message_id, media, dest)
    with pytest.raises(OSError):
        manager._maybe_download_media(message, retry_on_error=True)
    assert '[已下载到:' not in message.content
