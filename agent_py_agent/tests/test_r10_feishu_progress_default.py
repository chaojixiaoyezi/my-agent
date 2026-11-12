"""飞书默认展示迁移只影响原线程/配置，不覆盖更新后的显式关闭。"""
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.conversation.verbose import (
    effective_verbose_level,
    initial_verbose_settings,
)
from scripts.private_release import installer


def test_old_and_new_feishu_default_manual_off_survives_cold_load(tmp_path):
    store = ConversationStore(tmp_path / 'conversations')
    spec = {'channel': 'feishu', 'channel_conversation_id': 'chat', 'channel_user_id': 'u', 'canonical_user_id': 'u'}
    old = store.threads.get_or_create(spec)
    assert old.verbose_level == 'off'
    settings = initial_verbose_settings(SimpleNamespace(), {}, channel='feishu')
    adopted = store.threads.get_or_create({**spec, **settings})
    assert adopted.verbose_level == 'stream'
    assert adopted.metadata['verbose_level_source'] == 'feishu_default'
    assert effective_verbose_level(adopted, {}, channel='feishu') == 'stream'
    selected = store.threads.update_verbose_level(old.thread_id, 'off')
    assert selected.metadata['verbose_level_source'] == 'user'
    cold = ConversationStore(store.storage.root)
    assert cold.threads.get_or_create({**spec, **settings}).verbose_level == 'off'
    assert effective_verbose_level(cold.threads.require(old.thread_id), {}, channel='feishu') == 'off'
    fresh = store.threads.get_or_create({**spec, 'channel_conversation_id': 'new', **settings})
    assert fresh.verbose_level == 'stream'


def test_default_adoption_rechecks_latest_explicit_off(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / 'conversations')
    thread = store.threads.get_or_create({'channel': 'feishu', 'channel_conversation_id': 'c', 'channel_user_id': 'u'})
    store.threads.update_verbose_level(thread.thread_id, 'off')
    # stale candidate不得越过正式update_atomic中的最新user来源。
    result = store.threads.adopt_verbose_default(thread, {'verbose_level': 'stream', 'verbose_level_source': 'feishu_default'})
    assert result.verbose_level == 'off' and result.metadata['verbose_level_source'] == 'user'


def _installation(tmp_path):
    prefix = tmp_path / 'prefix'
    config = prefix / 'data/config/agent_config.yaml'
    config.parent.mkdir(parents=True)
    config.write_text('model: keep\nim_progress_cards_enabled: false # 保留注释\n')
    (prefix / installer.MARKER).write_text(json.dumps({'schema': installer.SCHEMA}))
    return prefix, config


def test_installer_progress_once_and_post_upgrade_user_false(tmp_path):
    prefix, config = _installation(tmp_path)
    template = 'im_progress_cards_enabled: true\n'
    installer.migrate_feishu_progress_default(prefix, tmp_path / 'v1', config, template)
    assert config.read_text() == 'model: keep\nim_progress_cards_enabled: true # 保留注释\n'
    record = json.loads((prefix / installer.MARKER).read_text())
    assert record['config_migrations']['feishu-progress-default-v1']['phase'] == 'completed'
    config.write_text('model: keep\nim_progress_cards_enabled: false\n')
    installer.migrate_feishu_progress_default(prefix, tmp_path / 'v2', config, template)
    assert 'false' in config.read_text()


def test_installer_progress_resume_after_config_committed(tmp_path, monkeypatch):
    prefix, config = _installation(tmp_path)
    native = installer._atomic_write_config
    def interrupted(path, content):
        if path == prefix / installer.MARKER and '"phase": "completed"' in content:
            raise OSError('injected metadata publication interruption')
        native(path, content)
    monkeypatch.setattr(installer, '_atomic_write_config', interrupted)
    with pytest.raises(OSError):
        installer.migrate_feishu_progress_default(prefix, tmp_path / 'v1', config, 'im_progress_cards_enabled: true\n')
    assert 'true' in config.read_text()
    monkeypatch.setattr(installer, '_atomic_write_config', native)
    installer.migrate_feishu_progress_default(prefix, tmp_path / 'v1', config, 'im_progress_cards_enabled: true\n')
    assert json.loads((prefix / installer.MARKER).read_text())['config_migrations']['feishu-progress-default-v1']['phase'] == 'completed'


@pytest.mark.parametrize('value,changed', [(' ["offline_access"]', True), ('\n  - offline_access', True), (' []', False), (' ["custom"]', False)])
def test_oauth_only_exact_old_default_once(tmp_path, value, changed):
    prefix, _ = _installation(tmp_path)
    config = prefix / 'data/config/capability_config.yaml'
    original = 'other: keep\nfeishu_oauth_scopes:' + value + '\n'
    config.write_text(original)
    package = tmp_path / 'package'
    (package / 'config').mkdir(parents=True)
    (package / 'config/capability_config.yaml').write_text('feishu_oauth_scopes: ["offline_access", "im:chat:read"]\n')
    installer.migrate_feishu_oauth_defaults(prefix, tmp_path / 'v1', package)
    assert ('im:chat:read' in config.read_text()) is changed
    assert 'other: keep\n' in config.read_text()
    config.write_text(original)
    installer.migrate_feishu_oauth_defaults(prefix, tmp_path / 'v2', package)
    assert config.read_text() == original


@pytest.mark.parametrize('line', ['feishu_chat_history_enabled: false # 历史模板\n', 'other: keep\n'])
def test_history_default_adopts_once_and_keeps_later_off(tmp_path, line):
    prefix, _ = _installation(tmp_path)
    config = prefix / 'data/config/capability_config.yaml'
    config.write_text(line)
    package = tmp_path / 'package'
    (package / 'config').mkdir(parents=True)
    (package / 'config/capability_config.yaml').write_text('feishu_chat_history_enabled: true\n')
    installer.migrate_feishu_history_default(prefix, tmp_path / 'v1', package)
    assert 'feishu_chat_history_enabled: true' in config.read_text()
    if '# 历史模板' in line:
        assert '# 历史模板' in config.read_text()
    config.write_text('feishu_chat_history_enabled: false\n')
    installer.migrate_feishu_history_default(prefix, tmp_path / 'v2', package)
    assert config.read_text() == 'feishu_chat_history_enabled: false\n'


from agent_py_agent.tests import test_s2w2_tui_network_mode as network_tests  # noqa: E402

startup_gateway = network_tests.startup_gateway


def test_feishu_real_http_default_status_off_and_new(startup_gateway):
    from agent_py_agent.cli.chat_parts.gateway_client import NetworkGatewayObserver
    observer = NetworkGatewayObserver(SimpleNamespace(
        config=SimpleNamespace(gateway_auth_token=startup_gateway.token),
        gateway_request_identity=lambda: {'user_id': 'local-agent', 'channel': 'feishu'}),
        f'http://127.0.0.1:{startup_gateway.server.server.server_address[1]}')
    def command(text, message_id):
        status, body = observer.request_json('POST', '/control', {
            'command': text, 'user_id': 'local-agent', 'channel': 'feishu',
            'conversation_id': 'feishu-default-wire', 'metadata': {'message_id': message_id}},
            total_deadline_seconds=5)
        assert status == 200 and body.get('ok'), body
        return body
    assert 'stream' in command('/status', 'status1')['message']
    command('/verbose off', 'off')
    assert 'off' in command('/status', 'status2')['message']
    command('/new', 'new')
    assert 'off' in command('/status', 'status3')['message']


def test_off_batch_never_creates_tool_card_and_persists_gate(tmp_path, monkeypatch):
    from agent_py_agent.agent.adapter.progress_draft import GatewayProgressBatch
    from agent_py_agent.tests.test_v13_w6_feishu_progress_card import _fixture
    adapter, store, worker, _, batches, _, _ = _fixture(tmp_path, monkeypatch)
    batches[0] = GatewayProgressBatch(('等待结果',), 1, steps=({'step_id': 'tool', 'tool_name': 'read_file'},),
                                    render_kind='card', verbose_level='off')
    worker.run_once()
    assert not any('card' in options for _, _, options in adapter.created)
    draft = store.pending()[0].progress_draft
    assert draft.get('verbose_level') == 'off' and draft.get('render_kind') == 'text', store.pending()[0]


@pytest.mark.parametrize('field', [
    'feishu_oauth_scopes:\n- offline_access # scope item\n',
    'feishu_oauth_scopes: # scope heading\n  - offline_access # scope item\n',
    'feishu_oauth_scopes: [offline_access] # scope item\n',
])
def test_oauth_scope_node_preserves_neighbor_history_and_comments(tmp_path, field):
    yaml = pytest.importorskip("yaml")
    prefix, _ = _installation(tmp_path)
    config = prefix / 'data/config/capability_config.yaml'
    original = 'before: {keep: [1, true]}\n' + field + '# neighbor comment\nfeishu_chat_history_enabled: false # history\nafter: keep\n'
    config.write_text(original)
    package = tmp_path / 'package'
    (package / 'config').mkdir(parents=True)
    defaults = ['offline_access', 'im:chat:read']
    (package / 'config/capability_config.yaml').write_text('feishu_chat_history_enabled: true\nfeishu_oauth_scopes: ' + json.dumps(defaults) + '\n')
    installer.migrate_feishu_oauth_defaults(prefix, tmp_path / 'v1', package)
    migrated = config.read_text()
    assert yaml.safe_load(migrated) == {**yaml.safe_load(original), 'feishu_oauth_scopes': defaults}
    assert '# scope item' in migrated and '# neighbor comment' in migrated and '# history' in migrated
    if '# scope heading' in original:
        assert '# scope heading' in migrated
    installer.migrate_feishu_history_default(prefix, tmp_path / 'v1', package)
    assert yaml.safe_load(config.read_text()) == {**yaml.safe_load(original), 'feishu_oauth_scopes': defaults, 'feishu_chat_history_enabled': True}
    facts = json.loads((prefix / installer.MARKER).read_text())['config_migrations']
    assert facts['feishu-oauth-default-scopes-v1']['phase'] == facts['feishu-chat-history-default-v1']['phase'] == 'completed'


@pytest.mark.parametrize('failure_stage', ['config', 'completed'])
def test_oauth_scope_prepared_retry_keeps_other_migrations(tmp_path, monkeypatch, failure_stage):
    yaml = pytest.importorskip("yaml")
    prefix, _ = _installation(tmp_path)
    marker = prefix / installer.MARKER
    original_record = json.loads(marker.read_text())
    preserved = {'phase': 'completed', 'evidence': 'earlier-migration'}
    marker.write_text(json.dumps({**original_record, 'config_migrations': {'earlier': preserved}}))
    config = prefix / 'data/config/capability_config.yaml'
    original = 'feishu_oauth_scopes:\n- offline_access\nfeishu_chat_history_enabled: false\n'
    config.write_text(original)
    package = tmp_path / 'package'
    (package / 'config').mkdir(parents=True)
    (package / 'config/capability_config.yaml').write_text('feishu_oauth_scopes: [offline_access, "im:chat:read"]\n')
    native = installer._atomic_write_config
    def interrupted(path, content):
        if ((failure_stage == 'config' and path == config)
                or (failure_stage == 'completed' and path == marker and '"completed_at"' in content)):
            raise OSError('injected interruption')
        native(path, content)
    monkeypatch.setattr(installer, '_atomic_write_config', interrupted)
    with pytest.raises(OSError):
        installer.migrate_feishu_oauth_defaults(prefix, tmp_path / 'v1', package)
    facts = json.loads(marker.read_text())['config_migrations']
    assert facts['earlier'] == preserved and facts['feishu-oauth-default-scopes-v1']['phase'] == 'prepared'
    assert yaml.safe_load(config.read_text())['feishu_chat_history_enabled'] is False
    monkeypatch.setattr(installer, '_atomic_write_config', native)
    installer.migrate_feishu_oauth_defaults(prefix, tmp_path / 'v1', package)
    assert yaml.safe_load(config.read_text()) == {'feishu_oauth_scopes': ['offline_access', 'im:chat:read'], 'feishu_chat_history_enabled': False}
    facts = json.loads(marker.read_text())['config_migrations']
    assert facts['earlier'] == preserved and facts['feishu-oauth-default-scopes-v1']['phase'] == 'completed'


def test_installer_scope_migration_subprocess_without_pyyaml(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    prefix, _ = _installation(tmp_path)
    config = prefix / 'data/config/capability_config.yaml'
    config.write_text('feishu_oauth_scopes:\n- offline_access # preserve\nfeishu_chat_history_enabled: false\n')
    package = tmp_path / 'package'
    (package / 'config').mkdir(parents=True)
    (package / 'config/capability_config.yaml').write_text('feishu_oauth_scopes: ["offline_access", "im:chat:read"]\nfeishu_chat_history_enabled: true\n')
    script = '''
import builtins, sys
native = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'yaml' or name.startswith('yaml.'):
        raise ModuleNotFoundError('PyYAML intentionally unavailable')
    return native(name, *args, **kwargs)
builtins.__import__ = guarded
from pathlib import Path
from scripts.private_release import installer
prefix, package = map(Path, sys.argv[1:])
installer.migrate_feishu_oauth_defaults(prefix, Path('candidate'), package)
installer.migrate_feishu_history_default(prefix, Path('candidate'), package)
from agent_py_agent.agent.settings.config_io import parse_simple_yaml_text
value = parse_simple_yaml_text((prefix / 'data/config/capability_config.yaml').read_text())
assert value == {'feishu_oauth_scopes': ['offline_access', 'im:chat:read'], 'feishu_chat_history_enabled': True}
assert 'yaml' not in sys.modules
'''
    result = subprocess.run([sys.executable, '-c', script, str(prefix), str(package)],
        cwd=Path(__file__).resolve().parents[2], env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[2])},
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert '# preserve' in config.read_text()
