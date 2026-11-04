"""入站便签真实落盘、下载接线、并发与路径边界；仅使用临时 Owner。"""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_py_agent.agent.adapter import inbound_notes
from agent_py_agent.tests.test_inbound_media_ownership import _manager, _message


# LLM: 样本仅包含合成身份与 Owner 相对路径，不触及真实配置。
# 函数用途: 按正式 API 保存一条可复现便签。
def _append(home, index=0):
    return inbound_notes.append_inbound_note(home, channel='feishu', chat_id='oc_fixture',
        message_id=f'om_{index}', media_kind='file', file_name='../../doc\n.txt',
        relative_path=f'media/input/{index}-doc.txt', size_bytes=3,
        sha256=hashlib.sha256(str(index).encode()).hexdigest(), text_excerpt='说明\n下一行',
        received_at=f'2026-11-04T00:00:{index:02d}+00:00')


# LLM: 直接核对合同字段、顺序和磁盘内容，不以写入回执代替持久化证据。
# 函数用途: 验证有界追加、默认读取及显示名清洗。
def test_append_caps_at_30_and_reads_newest_first(tmp_path):
    assert isinstance(_append(tmp_path), dict)
    for index in range(1, 34):
        _append(tmp_path, index)
    data = json.loads((tmp_path / 'media/recent_inbound_notes.json').read_text())
    assert data['schema_version'] == 'inbound_notes.v1'
    assert len(data['entries']) == 30
    assert data['entries'][0]['message_id'] == 'om_4'
    assert data['entries'][-1]['message_id'] == 'om_33'
    rows = inbound_notes.read_inbound_notes(tmp_path)
    assert [row['message_id'] for row in rows] == ['om_33', 'om_32', 'om_31', 'om_30', 'om_29']
    assert all('/' not in row['file_name'] and '\n' not in row['file_name'] for row in rows)
    assert all(len(row['text_excerpt']) <= 200 and '\n' not in row['text_excerpt'] for row in rows)
    assert inbound_notes.read_inbound_notes(tmp_path, limit=0) == []


# LLM: 路径必须为单一 Owner 内的规范相对引用，不能靠字符串前缀判断。
# 函数用途: 拒绝绝对路径、盘符、控制字符与点穿越。
@pytest.mark.parametrize('path', ['/tmp/file', '../file', 'media/../file', 'C:\\file', 'media\\file', 'media/\x00file'])
def test_relative_path_scope_validation(tmp_path, path):
    with pytest.raises(ValueError):
        inbound_notes.append_inbound_note(tmp_path, channel='feishu', chat_id='chat', message_id='m',
            media_kind='file', file_name='doc', relative_path=path, size_bytes=0, sha256='a' * 64)
    assert not (tmp_path / 'media/recent_inbound_notes.json').exists()


# LLM: 并发只使用临时账本，真实路径锁须保护整个读改写。
# 函数用途: 验证并发不丢记录及同一下载重放不重复记账。
def test_concurrent_append_and_retry_are_idempotent(tmp_path):
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda index: _append(tmp_path, index), range(20)))
    assert len(inbound_notes.read_inbound_notes(tmp_path, limit=30)) == 20
    _append(tmp_path, 0)
    assert len(inbound_notes.read_inbound_notes(tmp_path, limit=30)) == 20


# LLM: 通过生产下载入口和真实身份解析验证便签，而非直接模拟成功。
# 函数用途: 下载成功后记录真实大小、哈希与当前 Owner 相对路径。
def test_download_records_actual_file_for_its_owner(tmp_path, monkeypatch):
    manager, _ = _manager(tmp_path, monkeypatch)
    msg = _message('ou_one')
    assert manager._maybe_download_media(msg, retry_on_error=True)
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
    owner = resolve_owner_home(manager._inbound_media_agent.home_paths.root, OwnerIdentity.provider_user('feishu', 'ou_one'))
    rows = inbound_notes.read_inbound_notes(owner.home_dir)
    assert len(rows) == 1
    assert rows[0]['sha256'] == hashlib.sha256(b'own attachment').hexdigest()
    assert rows[0]['size_bytes'] == len(b'own attachment')
    assert rows[0]['relative_path'] == 'media/input/om_same-example.txt'
    assert rows[0]['text_excerpt'] == 'own attachment'


# LLM: 只对便签 writer 注入一次模拟 I/O 故障，下载及真实字节仍走生产入口。
# 函数用途: 验证可选便签失败不会使下载失败或发布残缺路径。
def test_note_write_failure_does_not_fail_download(tmp_path, monkeypatch):
    manager, _ = _manager(tmp_path, monkeypatch)
    def fail_note(owner_home, *, channel, chat_id, message_id, media_kind, file_name,
                  relative_path, size_bytes, sha256, text_excerpt='', received_at=None):
        raise OSError('simulated I/O error')
    monkeypatch.setattr(inbound_notes, 'append_inbound_note', fail_note)
    msg = _message('ou_one')
    assert manager._maybe_download_media(msg, retry_on_error=True)
    assert '[已下载到:' in msg.content
