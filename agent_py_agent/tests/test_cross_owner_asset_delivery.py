# LLM: 仅在pytest临时Owner执行跨用户资产合同；不用真实账号、凭据、通道或运行服务。
# 模块用途: 验证投递安全边界、依赖闭包、默认禁用及真实执行入口。
from dataclasses import replace

import pytest

from agent_py_agent.agent.assets import topology
from agent_py_agent.agent.assets.descriptor import AssetDescriptor, AssetRefs
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.workflow_store import WorkflowDefinition, WorkflowStore


# LLM: descriptor的未知字段仍严格；新enabled省略兼容旧资产，布尔类型不得宽松转换。
# 函数用途: 钉住启停状态的JSON往返与旧数据默认启用。
def test_descriptor_enabled_roundtrip_and_legacy_default():
    raw = {'id': 'script_a', 'kind': 'script', 'name': '合成脚本'}
    assert getattr(AssetDescriptor.from_dict(raw), 'enabled', None) is True
    disabled = AssetDescriptor.from_dict({**raw, 'enabled': False})
    assert disabled.enabled is False and disabled.to_dict()['enabled'] is False
    with pytest.raises(ValueError):
        AssetDescriptor.from_dict({**raw, 'enabled': 'false'})


# LLM: 直接沿原Store保存，不引入投递影子位置；开关方法必须保存并返回正式描述符。
# 函数用途: 验证脚本禁用状态不会在保存、加载或切换时丢失。
def test_script_store_persists_and_toggles_enabled(tmp_path):
    assert 'enabled' in AssetDescriptor.__dataclass_fields__
    store = ScriptStore(tmp_path)
    descriptor = AssetDescriptor.from_dict({'id': 'sample', 'kind': 'script', 'name': '脚本', 'enabled': False})
    record = store.save_script(descriptor, 'print("{}")\n')
    assert record['enabled'] is False
    result = store.set_enabled('sample', True)
    assert result.enabled is True and store.get_script('sample')['enabled'] is True


# LLM: WorkflowDefinition与其descriptor共享生效状态，任一显式False不得因旧格式缺省True而复活。
# 函数用途: 验证工作流双层序列化、原存储格式及显式开启。
def test_workflow_enabled_is_preserved_in_definition_and_store(tmp_path):
    assert 'enabled' in WorkflowDefinition.__dataclass_fields__
    definition = WorkflowDefinition.from_dict({'descriptor': {'id': 'flow', 'kind': 'workflow', 'name': '流程'},
        'steps': [], 'enabled': False})
    assert definition.enabled is False and definition.descriptor.enabled is False
    store = WorkflowStore(tmp_path)
    assert store.save_workflow(definition).enabled is False
    assert store.get_workflow('flow').descriptor.enabled is False
    assert store.set_enabled('flow', True).enabled is True
    assert WorkflowDefinition(replace(definition.descriptor, enabled=False), ()).enabled is False


# LLM: 提取器只返回根可达实际资产，依赖在前；不能调用用于展示的指数级简单环枚举。
# 函数用途: 验证闭包包含传递依赖、排除无关资产并保持依赖顺序。
def test_closure_is_dependency_first_without_cycle_enumeration(monkeypatch):
    extract = getattr(topology, 'extract_asset_closure', None)
    assert callable(extract), '缺少投递闭包公共入口'
    monkeypatch.setattr(topology, '_cycles', lambda *args: pytest.fail('闭包不得枚举所有简单环'))
    script = AssetDescriptor('collect', 'script', '采集')
    skill = AssetDescriptor('analyze', 'skill', '分析', refs=AssetRefs(scripts=('collect',)))
    flow = AssetDescriptor('flow', 'workflow', '流程', refs=AssetRefs(skills=('analyze',)))
    unused = AssetDescriptor('other', 'script', '无关')
    assert [row.id for row in extract((flow, unused, script, skill), 'workflow:flow')] == ['collect', 'analyze', 'flow']


# LLM: 只有根可达的缺失依赖硬失败；错误必须提供机器码和明确引用，不吞成空闭包。
# 函数用途: 验证缺依赖与不相关缺依赖的区别。
def test_closure_missing_dependency_is_structured():
    extract = getattr(topology, 'extract_asset_closure', None)
    assert callable(extract), '缺少投递闭包公共入口'
    root = AssetDescriptor('flow', 'workflow', '流程', refs=AssetRefs(scripts=('missing',)))
    with pytest.raises(ValueError) as error:
        extract((root,), 'workflow:flow')
    assert error.value.error_code == 'ASSET_DEPENDENCY_MISSING'
    assert error.value.missing_refs == ['script:missing']
    assert [row.id for row in extract((root, AssetDescriptor('ok', 'script', '正常')), 'script:ok')] == ['ok']


# LLM: 执行器替身只记录是否触达真实子进程层；禁用脚本必须在临时执行目录/数据源绑定前拒绝。
# 函数用途: 验证enabled不是仅供展示的标签。
def test_disabled_script_never_reaches_process_execution(tmp_path, monkeypatch):
    from agent_py_agent.agent.workflows import script_runner

    ScriptStore(tmp_path).save_script(AssetDescriptor('stop', 'script', '关闭', enabled=False), 'print("{}")')
    calls = []

    # LLM: 替代有外部副作用的执行叶，不替换资产读取或禁用判定。
    # 函数用途: 记录禁用闸遗漏而不真的启动脚本。
    def execute(*args):
        calls.append(args)
        return 0, '{}', '', '', {}

    monkeypatch.setattr(script_runner, '_execute', execute)
    outcome = script_runner.ScriptRunner(tmp_path, use_sandbox=False).run_script('stop')
    assert outcome.ok is False and outcome.error_code == 'ASSET_DISABLED_BY_OWNER'
    assert calls == [] and not (tmp_path / 'data/script_exec').exists()


# LLM: run与resume均检查当前canonical开关，冻结旧快照不能在Owner停用后绕过运行闸。
# 函数用途: 验证工作流关闭后新建和续跑均拒绝。
@pytest.mark.parametrize('resume', [False, True])
def test_disabled_workflow_rejects_new_and_resumed_runs(tmp_path, resume):
    from agent_py_agent.agent.workflows.engine import WorkflowEngine

    store = WorkflowStore(tmp_path)
    definition = store.save_workflow(WorkflowDefinition(AssetDescriptor('flow', 'workflow', '流程'), ()))
    engine = WorkflowEngine(tmp_path)
    record = engine.run_workflow('flow') if resume else None
    store.set_enabled('flow', False)
    with pytest.raises(ValueError) as error:
        engine.resume_workflow_run(record.workflow_run_id) if resume else engine.run_workflow(definition)
    assert error.value.error_code == 'ASSET_DISABLED_BY_OWNER'


# LLM: 步骤引用关闭的脚本或Skill属于硬门，不走on_error=skip或分析兜底。
# 函数用途: 验证工作流引用资产的真实禁用检查发生在执行前。
@pytest.mark.parametrize('kind', ['script', 'skill'])
def test_workflow_step_cannot_execute_disabled_dependency(tmp_path, monkeypatch, kind):
    from agent_py_agent.agent.assets.workflow_store import WorkflowStepSpec
    from agent_py_agent.agent.workflows.engine import WorkflowEngine

    if kind == 'script':
        ScriptStore(tmp_path).save_script(AssetDescriptor('dep', kind, '依赖', enabled=False), 'print("{}")')
        step = WorkflowStepSpec('s', '步骤', 'deterministic_script', script_id='dep', on_error='skip')
    else:
        path = tmp_path / 'skills/dep/SKILL.md'
        path.parent.mkdir(parents=True)
        path.write_text('---\nname: dep\ndescription: fixture\nenabled: false\n---\n合成技能。')
        step = WorkflowStepSpec('s', '步骤', 'llm_skill_analysis', skill_ids=('dep',), on_error='skip')
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda *args: pytest.fail('禁用Skill不得分析'))
    monkeypatch.setattr(engine.runner, 'run_script', lambda *args, **kwargs: pytest.fail('禁用脚本不得执行'))
    result = engine.run_workflow(WorkflowDefinition(AssetDescriptor('flow', 'workflow', '流程'), (step,)))
    assert result.status == 'failed' and result.steps[0].error_code == 'ASSET_DISABLED_BY_OWNER'


# LLM: 测试沿原SkillsService、adapter和snapshot读口，不创建第二份技能索引或仅假造enabled条目。
# 函数用途: 验证frontmatter及canonical侧文件禁用同样阻断模型注入和读取。
@pytest.mark.parametrize('location', ['frontmatter', 'sidecar'])
def test_disabled_skill_is_not_active_in_adapter_or_snapshot(tmp_path, location):
    import json

    from agent_py_agent.agent.assets.skill_adapter import get_owner_skill_descriptor
    from agent_py_agent.agent.capability.skill_snapshot import SkillSnapshotError
    from agent_py_agent.tests.test_ctxopt_skill_catalog import service

    home = tmp_path / 'owner'
    path = home / 'skills/dep/SKILL.md'
    path.parent.mkdir(parents=True)
    header = '\nenabled: false' if location == 'frontmatter' else ''
    path.write_text(f'---\nname: dep\ndescription: fixture{header}\n---\n合成技能。')
    if location == 'sidecar':
        (path.parent / 'skill_meta.json').write_text(json.dumps({'enabled': False}))
    assert get_owner_skill_descriptor(home, 'dep')['enabled'] is False
    snapshot = service(home, tmp_path / 'workspace').snapshot_for()
    assert snapshot.resolve('owner:dep', enabled_only=False).enabled is False
    assert snapshot.resolve('owner:dep') is None
    with pytest.raises(SkillSnapshotError) as error:
        snapshot.read_body('owner:dep')
    assert error.value.error_code == 'ASSET_DISABLED_BY_OWNER'


# LLM: 缺实现时以明确断言报红，不能把导入错误当扫描器生效证据。
# 函数用途: 取得待实现的原始字节扫描公共入口。
@pytest.fixture
def sanitize():
    import importlib
    import importlib.util

    name = 'agent_py_agent.agent.assets.delivery_sanitizer'
    assert importlib.util.find_spec(name) is not None, '缺少原始字节投递扫描器'
    module = importlib.import_module(name)
    assert callable(getattr(module, 'sanitize_bundle_files', None))
    return module.sanitize_bundle_files


# LLM: 全部密钥均为测试合成，无真实凭据；明确匹配要求的六种形状以及JSON带引号键。
# 函数用途: 验证默认硬拒绝且错误回执绝不回显秘密。
@pytest.mark.parametrize('secret', ['sk-' + 'A' * 24, 'AKIA' + 'B' * 16, 'ghp_' + 'C' * 24,
    'xoxb-' + 'D' * 16, 'eyJ' + 'E' * 16 + '.' + 'F' * 16 + '.' + 'G' * 16,
    'password = "SyntheticPassword123"', '"api_key": "SyntheticApiKey123"'])
def test_secret_scan_rejects_raw_bytes_without_echo(sanitize, tmp_path, secret):
    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError

    with pytest.raises(AssetDeliveryError) as error:
        sanitize({'script/example/main.py': ('# sample\n' + secret).encode()}, owner_home=tmp_path)
    assert error.value.error_code == 'ASSET_DELIVERY_SECRET_DETECTED'
    matches = error.value.details['redacted_matches']
    assert matches and matches[0]['line_number'] == 2
    assert secret not in str(error.value.to_dict())


# LLM: 环境只设置合成变量，不读取或持久化真实环境秘密；允许占位替换必须真正去掉原字节。
# 函数用途: 验证环境秘密匹配、显式替换和主机路径便携化。
def test_secret_replacement_environment_and_paths(sanitize, tmp_path, monkeypatch):
    secret = 'synthetic-only-environment-credential-9284'
    monkeypatch.setenv('FIXTURE_DELIVERY_TOKEN', secret)
    raw = f'value = "{secret}"\npath = "{tmp_path}/workspace/data"\n'.encode()
    result = sanitize({'script/a/main.py': raw}, owner_home=tmp_path, allow_secret_placeholder_replace=True)
    assert secret.encode() not in result['script/a/main.py']
    assert b'${REDACTED_SECRET}' in result['script/a/main.py']
    assert str(tmp_path).encode() not in result['script/a/main.py']
    assert b'${OWNER_HOME}/workspace/data' in result['script/a/main.py']


# LLM: 模板占位符不是秘密；未知格式二进制不应因UTF8解码而损坏。
# 函数用途: 验证安全模板和二进制资源无损保留。
def test_secret_placeholders_and_binary_are_preserved(sanitize, tmp_path):
    text = b'password = "${MY_PASSWORD}"\nsecret = "YOUR_SECRET_VALUE"\ntoken = "<CONFIGURED_TOKEN>"\n'
    files = {'skill/a/SKILL.md': text, 'skill/a/templates/image.bin': bytes([0, 255, 128, 254])}
    assert sanitize(files, owner_home=tmp_path) == files


# LLM: 全部账号为临时根中的严格LocalUserStore记录，身份/目录绑定按原权威schema，不接真实账号或通道。
# 函数用途: 建立三位合成本地用户及一个已知飞书用户供服务测试。
@pytest.fixture
def kit(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.agent.assets.delivery_service import AssetDeliveryService
    from agent_py_agent.agent.user_space.local_user_store import LocalUser, LocalUserStore

    root = tmp_path / 'host'
    main = root / 'owners/local/main'
    main.mkdir(parents=True)
    store = LocalUserStore(root)
    homes = {}
    for uid in ('alice', 'bob'):
        home = root / 'owners/local' / uid
        home.mkdir(parents=True)
        user = LocalUser(uid, 'enabled', 1, uid, '2026-10-25', '2026-10-25', 1)
        store.save(uid, {'schema': 'local-user.v1', 'user': user.to_dict(),
            'provision': {'phase': 'committed', 'operation_id': 'fixture',
                'home_identity': store.directory_identity(home.relative_to(root).parts)},
            'operations': {'fixture': user.to_dict()}})
        homes[uid] = home
    feishu = root / 'owners/providers/feishu/users/ou_fixture'
    feishu.mkdir(parents=True)
    return SimpleNamespace(root=root, main=main, feishu=feishu, service=AssetDeliveryService(root), **homes)


# LLM: 用真实Store和Skill正文构造完整闭包，测试不绕过领域定义去伪造可安装包。
# 函数用途: 创建一个工作流、脚本及带资源的Skill样本。
def seed_delivery_bundle(kit):
    from agent_py_agent.agent.assets.workflow_store import WorkflowStepSpec

    ScriptStore(kit.main).save_script(AssetDescriptor('collect', 'script', '采集'), 'print("{}")\n')
    path = kit.main / 'skills/guide/SKILL.md'
    path.parent.mkdir(parents=True)
    path.write_text('---\nname: guide\ndescription: fixture\n---\n合成分析方法。\n')
    (path.parent / 'references').mkdir()
    (path.parent / 'references/rules.txt').write_text('合成规则。')
    WorkflowStore(kit.main).save_workflow(WorkflowDefinition(AssetDescriptor('flow', 'workflow', '流程'),
        (WorkflowStepSpec('s', '分析', 'hybrid', script_id='collect', skill_ids=('owner:guide',)),)))


# LLM: 不创建锁的只读入口必须保持冷Owner目录和文件原样；目标排除发送方本人。
# 函数用途: 验证冷读无副作用及准确目标发现。
def test_delivery_cold_reads_and_targets(kit):
    before = {str(path.relative_to(kit.root)): path.read_bytes() for path in kit.root.rglob('*') if path.is_file()}
    targets = kit.service.list_targets('local/main')
    assert targets.get('ok') is True
    assert {row['owner_id'] for row in targets['targets']} == {
        'local/alice', 'local/bob', 'providers/feishu/users/ou_fixture'}
    assert kit.service.list_inbox('local/alice')['deliveries'] == []
    assert kit.service.get_blocklist('local/alice')['blocked_owners'] == []
    assert {str(path.relative_to(kit.root)): path.read_bytes() for path in kit.root.rglob('*') if path.is_file()} == before


# LLM: 正常投递只能入箱，接受后才写canonical资产且默认全部关闭；回执不能代替磁盘及原Store读回。
# 函数用途: 验证闭包发送、独立安装、来源元数据、禁用和显式开启全链路。
def test_delivery_bundle_installs_canonical_assets_disabled_by_default(kit):
    import json

    seed_delivery_bundle(kit)
    preview = kit.service.preview_bundle('local/main', 'workflow', 'flow')
    assert preview.get('ok') is True
    assert preview['closure_summary']['total_assets_count'] == 3
    sent = kit.service.send_bundle('local/main', 'local/alice', 'workflow', 'flow')
    assert sent.get('ok') is True and sent['status'] == 'pending'
    assert not (kit.alice / 'workflows/flow/workflow.json').exists()
    delivery = sent['delivery_id']
    assert (kit.alice / 'data/asset_delivery/inbox' / delivery / 'envelope.json').is_file()
    assert (kit.main / 'data/asset_delivery/outbox' / (delivery + '.json')).is_file()
    accepted = kit.service.accept_delivery('local/alice', delivery)
    assert accepted.get('ok') is True and accepted['status'] == 'accepted'
    assert ScriptStore(kit.alice).get_script('collect')['enabled'] is False
    assert WorkflowStore(kit.alice).get_workflow('flow').enabled is False
    assert (kit.alice / 'skills/guide/references/rules.txt').read_text() == '合成规则。'
    meta = json.loads((kit.alice / 'skills/guide/skill_meta.json').read_text())
    assert meta['enabled'] is False and meta['metadata']['delivery_provenance']['delivery_id'] == delivery
    assert kit.service.toggle_asset('local/alice', 'script', 'collect', True)['ok'] is True
    assert ScriptStore(kit.alice).get_script('collect')['enabled'] is True
    receipt = json.loads((kit.main / 'data/asset_delivery/outbox' / (delivery + '.json')).read_text())
    assert receipt['schema_version'] == 'asset_delivery_receipt.v1' and receipt['status'] == 'accepted'


# LLM: 同包所有ID映射必须统一重写，不能只改根名；原有接收方脚本保留，不隐式覆盖。
# 函数用途: 验证keep_both_rename对工作流步骤和refs的闭包重写。
def test_delivery_conflict_renames_dependencies_and_rewrites_references(kit):
    seed_delivery_bundle(kit)
    ScriptStore(kit.alice).save_script(AssetDescriptor('collect', 'script', '已有'), 'print("old")')
    sent = kit.service.send_bundle('local/main', 'local/alice', 'workflow', 'flow')
    assert sent.get('ok') is True
    result = kit.service.accept_delivery('local/alice', sent['delivery_id'])
    assert result.get('ok') is True
    flow = WorkflowStore(kit.alice).get_workflow('flow')
    renamed = flow.steps[0].script_id
    assert renamed.startswith('collect_from_') and renamed != 'collect'
    assert flow.descriptor.refs.scripts == (renamed,)
    assert ScriptStore(kit.alice).get_script('collect')['code'] == 'print("old")'
    assert ScriptStore(kit.alice).get_script(renamed)['enabled'] is False


# LLM: 屏蔽检查先于入箱和通知，拒绝不安装资产；飞书只确认结构化事件，不冒充真实消息已送达。
# 函数用途: 验证屏蔽、解除、拒绝和飞书通知事件。
def test_delivery_block_reject_and_feishu_event(kit):
    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError

    seed_delivery_bundle(kit)
    assert kit.service.update_blocklist('local/alice', 'add', 'local/main').get('ok') is True
    with pytest.raises(AssetDeliveryError) as error:
        kit.service.send_bundle('local/main', 'local/alice', 'workflow', 'flow')
    assert error.value.error_code == 'DELIVERY_BLOCKED_BY_RECIPIENT'
    assert kit.service.list_inbox('local/alice')['deliveries'] == []
    kit.service.update_blocklist('local/alice', 'remove', 'local/main')
    sent = kit.service.send_bundle('local/main', 'local/alice', 'workflow', 'flow')
    assert kit.service.reject_delivery('local/alice', sent['delivery_id'], '合成拒绝')['status'] == 'rejected'
    assert not (kit.alice / 'scripts/collect/script.json').exists()
    sent = kit.service.send_bundle('local/main', 'providers/feishu/users/ou_fixture', 'workflow', 'flow')
    assert sent.get('ok') is True
    event = sent['notification_event']
    assert event['channel'] == 'feishu' and event['recipient_open_id'] == 'ou_fixture'
    assert '/delivery accept ' + sent['delivery_id'] in event['text']


# LLM: 通过公共服务切换真实canonical侧文件，旧快照仍须按当前状态拒读，不只改展示字典。
# 函数用途: 验证安装后的Skill可以显式启停且不损坏正文/来源记录。
def test_delivery_skill_toggle_updates_runtime_gate(kit, tmp_path):
    from agent_py_agent.agent.assets.skill_adapter import get_owner_skill_descriptor
    from agent_py_agent.agent.capability.skill_snapshot import SkillSnapshotError
    from agent_py_agent.tests.test_ctxopt_skill_catalog import service

    seed_delivery_bundle(kit)
    sent = kit.service.send_bundle('local/main', 'local/alice', 'skill', 'guide')
    kit.service.accept_delivery('local/alice', sent['delivery_id'])
    body = (kit.alice / 'skills/guide/SKILL.md').read_bytes()
    assert kit.service.toggle_asset('local/alice', 'skill', 'guide', True)['ok'] is True
    snapshot = service(kit.alice, tmp_path / 'workspace').snapshot_for()
    assert snapshot.resolve('owner:guide') is not None
    assert get_owner_skill_descriptor(kit.alice, 'guide')['enabled'] is True
    kit.service.toggle_asset('local/alice', 'skill', 'guide', False)
    assert (kit.alice / 'skills/guide/SKILL.md').read_bytes() == body
    with pytest.raises(SkillSnapshotError) as error:
        snapshot.read_body('owner:guide')
    assert error.value.error_code == 'ASSET_DISABLED_BY_OWNER'


# LLM: 工具构造器绑定可信Owner，Schema不得开放sender、角色或自动接收；读动作沿真实领域入口。
# 函数用途: 验证工具身份封闭、普通发送入箱与接收默认关闭。
def test_delivery_tool_binds_owner_and_rejects_forged_identity(kit):
    import importlib.util
    import json

    name = 'agent_py_agent.agent.tooling.asset_delivery_tool'
    assert importlib.util.find_spec(name) is not None, '缺少asset_delivery工具'
    from agent_py_agent.agent.tooling.asset_delivery_tool import AssetDeliveryTool

    seed_delivery_bundle(kit)
    sender = AssetDeliveryTool(kit.root, 'local/main')
    receiver = AssetDeliveryTool(kit.root, 'local/alice')
    props = sender.model_spec.input_schema['properties']
    assert not {'sender_owner_id', 'owner_id', 'sender_role', 'auto_accept', 'auto_enable', 'initial_enabled'} & props.keys()
    invalid = sender.execute({'action': 'send', 'sender_role': 'admin_provision',
        'recipient_owner_id': 'local/alice', 'asset_kind': 'workflow', 'asset_id': 'flow'})
    assert invalid.ok is False and invalid.error_code == 'TOOL_INVALID_ARGUMENTS'
    outcome = sender.execute({'action': 'send', 'recipient_owner_id': 'local/alice',
        'asset_kind': 'workflow', 'asset_id': 'flow'})
    assert outcome.ok is True
    delivery = json.loads(outcome.output)['delivery_id']
    accepted = receiver.execute({'action': 'accept', 'delivery_id': delivery})
    assert accepted.ok is True
    assert WorkflowStore(kit.alice).get_workflow('flow').enabled is False


# LLM: slash解析不接触磁盘，普通文本和伪前缀不获控制权；处理器首个身份来自宿主实参。
# 函数用途: 验证任务单命令语法、明确参数错误与独立领域处理器。
@pytest.mark.parametrize('suffix', ['inbox', 'targets', 'preview workflow:flow',
    'send local/alice workflow:flow 合成备注', 'accept dlv_fixture overwrite',
    'reject dlv_fixture 合成原因', 'enable script:collect', 'disable skill:guide',
    'block local/bob', 'unblock local/bob'])
def test_delivery_slash_parser_and_handler(kit, suffix):
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control

    command = parse_conversation_control('/delivery ' + suffix)
    assert command is not None and command.kind == 'delivery' and command.valid
    from agent_py_agent.agent.conversation.control_delivery_command import execute_delivery_control

    if suffix == 'targets':
        result = execute_delivery_control(kit.root, 'local/main', command)
        assert result.ok is True and result.kind == 'delivery'
    invalid = parse_conversation_control('/delivery accept x invalid_strategy')
    assert invalid is not None and not invalid.valid
    assert parse_conversation_control('请 /delivery inbox') is None
    assert parse_conversation_control('/deliveryevil inbox') is None


# LLM: 保留真实有限配额锁，仅在第二次准入前抛断言防止旧实现永久挂住测试；不把无限配额结果外推有限配额。
# 函数用途: 复现并阻止Skill切换对同一Owner重复取得不可重入quota锁。
def test_skill_toggle_uses_one_real_quota_admission(kit, monkeypatch):
    from contextlib import contextmanager

    from agent_py_agent.agent.assets import delivery_service
    from agent_py_agent.agent.user_space import owner_quota

    seed_delivery_bundle(kit)
    sent = kit.service.send_bundle('local/main', 'local/alice', 'skill', 'guide')
    kit.service.accept_delivery('local/alice', sent['delivery_id'])
    original = owner_quota.OwnerQuotaEnforcer.admission
    depth = 0

    # LLM: 真实锁仍由原admission取得，只把会挂死的重复调用改为可复现失败。
    # 函数用途: 检测有限配额调用链内的嵌套准入。
    @contextmanager
    def guarded(enforcer):
        nonlocal depth
        assert depth == 0, 'nested non-reentrant owner quota admission'
        depth += 1
        try:
            with original(enforcer) as admission:
                yield admission
        finally:
            depth -= 1

    # LLM: 合成有限配额替代策略读取，不替换实际磁盘计量与文件锁。
    # 函数用途: 给测试Owner启用100MiB原生配额。
    def limited(home):
        return owner_quota.OwnerQuotaEnforcer(home, max_bytes=100 * 1024 * 1024)

    monkeypatch.setattr(owner_quota.OwnerQuotaEnforcer, 'admission', guarded)
    monkeypatch.setattr(owner_quota, 'owner_quota_enforcer_from_policy', limited)
    monkeypatch.setattr(delivery_service, 'owner_quota_enforcer_from_policy', limited)
    assert kit.service.toggle_asset('local/alice', 'skill', 'guide', True)['ok'] is True


# LLM: 故障发生于发件回执真实写点之前，不替换打包/接收方暂存写入；失败不能遗留可累积的跨Owner负载。
# 函数用途: 验证发送途中异常和上次崩溃暂存都可安全清理。
def test_failed_send_cleans_stage_and_next_send_reaps_crash_orphan(kit, monkeypatch):
    seed_delivery_bundle(kit)
    original = kit.service._put_json

    # LLM: 精确模拟本操作发件回执IO失败，不注入其它Owner或静默成功。
    # 函数用途: 在接收方暂存已写完时触发回滚。
    def fail_receipt(home, parts, value, admission):
        if home == kit.main and parts[:3] == ('data', 'asset_delivery', 'outbox'):
            raise OSError('synthetic receipt fault')
        return original(home, parts, value, admission)

    monkeypatch.setattr(kit.service, '_put_json', fail_receipt)
    with pytest.raises(OSError):
        kit.service.send_bundle('local/main', 'local/alice', 'workflow', 'flow')
    staging = kit.alice / 'data/asset_delivery/staging'
    assert not list(staging.iterdir())
    assert kit.service.list_inbox('local/alice')['deliveries'] == []
    orphan = staging / ('0' * 32)
    orphan.mkdir()
    (orphan / 'payload.bin').write_bytes(b'crash fixture')
    monkeypatch.setattr(kit.service, '_put_json', original)
    assert kit.service.send_bundle('local/main', 'local/alice', 'workflow', 'flow')['ok'] is True
    assert not orphan.exists() and not list(staging.iterdir())


# LLM: 仅使用大小写不同的安全ID，不依赖当前文件系统大小写行为；跳过须引用实际保留的canonical身份。
# 函数用途: 验证skip策略不会把同包工作流指向不存在或身份不匹配的资产。
def test_skip_casefold_conflict_rewrites_to_existing_canonical_id(kit):
    from agent_py_agent.agent.assets.workflow_store import WorkflowStepSpec

    ScriptStore(kit.main).save_script(AssetDescriptor('COLLECT', 'script', '来包'), 'print("{}")')
    ScriptStore(kit.alice).save_script(AssetDescriptor('collect', 'script', '已有'), 'print("{}")')
    WorkflowStore(kit.main).save_workflow(WorkflowDefinition(AssetDescriptor('flow', 'workflow', '流程'),
        (WorkflowStepSpec('s', '步骤', 'deterministic_script', script_id='COLLECT'),)))
    sent = kit.service.send_bundle('local/main', 'local/alice', 'workflow', 'flow')
    kit.service.accept_delivery('local/alice', sent['delivery_id'], 'skip')
    assert WorkflowStore(kit.alice).get_workflow('flow').steps[0].script_id == 'collect'
    assert ScriptStore(kit.alice).get_script('collect')['name'] == '已有'


# LLM: 旧入口也是新包声明附件时必须保留；不实际执行导入代码，仅核对canonical资源字节。
# 函数用途: 验证overwrite旧入口清理不会吞掉新脚本模块。
def test_overwrite_preserves_new_resource_named_as_previous_entrypoint(kit):
    ScriptStore(kit.alice).save_script(AssetDescriptor('s', 'script', '旧脚本'), 'print("{}")', entrypoint='helper.py')
    ScriptStore(kit.main).save_script(AssetDescriptor('s', 'script', '新脚本'), 'import helper\nprint("{}")')
    helper = kit.main / 'scripts/s/helper.py'
    helper.write_bytes(b'VALUE = "fixture"\n')
    sent = kit.service.send_bundle('local/main', 'local/alice', 'script', 's')
    assert kit.service.accept_delivery('local/alice', sent['delivery_id'], 'overwrite')['ok'] is True
    assert (kit.alice / 'scripts/s/helper.py').read_bytes() == helper.read_bytes()


# LLM: 用内存附属资源构造casefold重复，避免在大小写不敏感宿主伪造两文件；正式提交叶替身禁止任何坏journal发表。
# 函数用途: 验证最终完整事务计划必须在写盘之前拒绝重复资源目标。
def test_final_delivery_plan_rejects_casefold_duplicate_resource(kit, monkeypatch):
    from agent_py_agent.agent.assets import workspace_assets
    from agent_py_agent.agent.assets.script_store_support import commit_delivery_items
    from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaEnforcer

    monkeypatch.setattr(workspace_assets, '_commit_staged', lambda *args: pytest.fail('ambiguous plan reached commit'))
    draft = {'descriptor': AssetDescriptor('s', 'script', '脚本').to_dict(), 'code': 'print("{}")'}
    with OwnerQuotaEnforcer(kit.alice, max_bytes=0).admission() as admission, pytest.raises(ValueError):
        commit_delivery_items(kit.alice, [draft], {('script', 's'): {'MAIN.py': b'print("fixture")'}}, admission, lambda _: None)
    assert not (kit.alice / '.asset-batch.json').exists()


# LLM: 精确模拟跨Owner两端回执之间的IO失败，不能把pending旧副本当成推翻已拒绝的授权。
# 函数用途: 验证拒绝已写发送方后，接收方回执重试不会变成安装。
def test_failed_reject_ack_cannot_be_reversed_by_accept(kit, monkeypatch):
    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError

    seed_delivery_bundle(kit)
    sent = kit.service.send_bundle('local/main', 'local/alice', 'workflow', 'flow')
    original = kit.service._put_json

    # LLM: 仅拦截最终拒绝回执，来源回执和原pending包裹保持真实。
    # 函数用途: 留下两端状态尚未对齐的拒绝场景。
    def fail_ack(home, parts, value, admission):
        if home == kit.alice and value.get('status') == 'rejected':
            raise OSError('synthetic recipient ack fault')
        return original(home, parts, value, admission)

    monkeypatch.setattr(kit.service, '_put_json', fail_ack)
    with pytest.raises(OSError):
        kit.service.reject_delivery('local/alice', sent['delivery_id'])
    monkeypatch.setattr(kit.service, '_put_json', original)
    with pytest.raises(AssetDeliveryError) as error:
        kit.service.accept_delivery('local/alice', sent['delivery_id'])
    assert error.value.error_code == 'DELIVERY_STATE_CONFLICT'
    assert not (kit.alice / 'workflows/flow/workflow.json').exists()


# LLM: 安装后回执失败不意味着未安装；用户随后编辑的正式资产不得被重命名重装或覆盖。
# 函数用途: 验证不确定提交的重试识别当前canonical变化并失败关闭。
def test_accept_retry_does_not_duplicate_assets_edited_after_commit(kit, monkeypatch):
    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError

    seed_delivery_bundle(kit)
    sent = kit.service.send_bundle('local/main', 'local/alice', 'script', 'collect')
    original = kit.service._resolve_delivery
    monkeypatch.setattr(kit.service, '_resolve_delivery', lambda *args: (_ for _ in ()).throw(OSError('synthetic ack fault')))
    with pytest.raises(OSError):
        kit.service.accept_delivery('local/alice', sent['delivery_id'])
    ScriptStore(kit.alice).set_enabled('collect', True)
    monkeypatch.setattr(kit.service, '_resolve_delivery', original)
    with pytest.raises(AssetDeliveryError) as error:
        kit.service.accept_delivery('local/alice', sent['delivery_id'])
    assert error.value.error_code == 'DELIVERY_INSTALLATION_STATE_CHANGED'
    assert [row['id'] for row in ScriptStore(kit.alice).list_scripts()] == ['collect']


# LLM: data_sources.query_scope和steps.inputs_map属于业务值，不因与资产字段同名而成为可重写引用。
# 函数用途: 验证冲突映射只改显式依赖及工作流步引用。
def test_reference_rewrite_preserves_unrelated_business_values():
    from agent_py_agent.agent.assets.script_store_support import rewrite_delivery_refs

    value = {'kind': 'workflow', 'refs': {'scripts': ['s']},
        'data_sources': [{'query_scope': {'script_id': 's'}}],
        'steps': [{'script_id': 's', 'inputs_map': {'record': {'script_id': 's'}}}]}
    result = rewrite_delivery_refs(value, {'script:s': 's_renamed'}, {})
    assert result['refs']['scripts'] == ['s_renamed'] and result['steps'][0]['script_id'] == 's_renamed'
    assert result['data_sources'] == value['data_sources']
    assert result['steps'][0]['inputs_map'] == value['steps'][0]['inputs_map']


# LLM: 此用例是范围限制的失败关闭证据，不是监控安装成功验收；不允许只保存绑定就返回accepted。
# 函数用途: 验证缺少真实Scheduler事务时整包保持pending且不安装其脚本依赖。
def test_monitor_without_scheduler_transaction_fails_before_any_install(kit):
    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError
    from agent_py_agent.agent.assets.monitor_adapter import save_monitor_binding

    ScriptStore(kit.main).save_script(AssetDescriptor('collect', 'script', '依赖'), 'print("{}")')
    save_monitor_binding(kit.main, 'job_fixture', name='监控模板', script_ids=['collect'],
        schedule_spec={'kind': 'every', 'every_seconds': 60, 'enabled': False})
    sent = kit.service.send_bundle('local/main', 'local/alice', 'monitor', 'job_fixture')
    with pytest.raises(AssetDeliveryError) as error:
        kit.service.accept_delivery('local/alice', sent['delivery_id'])
    assert error.value.error_code == 'ASSET_DELIVERY_MONITOR_REBIND_REQUIRED'
    assert not (kit.alice / 'data/monitors/job_fixture.json').exists()
    assert not (kit.alice / 'scripts/collect/script.json').exists()
    assert kit.service.list_inbox('local/alice')['deliveries'][0]['status'] == 'pending'


# LLM: 六个可选参数继续接受旧位置及命名调用，值对象必须严格拒绝未知或重复赋值；不修改管理员可信调用边界。
# 函数用途: 验证发送选项归并保持公开调用行为和自动接收默认关闭。
def test_send_optional_argument_compatibility_and_admin_boundaries(kit):
    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError

    seed_delivery_bundle(kit)
    sent = kit.service.send_bundle('local/main', 'local/alice', 'script', 'collect',
        'fixture', True, False, True, False, 'admin_provision')
    assert sent['status'] == 'accepted' and ScriptStore(kit.alice).get_script('collect')['enabled'] is False
    with pytest.raises(TypeError):
        kit.service.send_bundle('local/main', 'local/bob', 'script', 'collect', 'note', note='duplicate')
    with pytest.raises(TypeError):
        kit.service.send_bundle('local/main', 'local/bob', 'script', 'collect', unknown_option=True)
    with pytest.raises(AssetDeliveryError) as error:
        kit.service.send_bundle('local/alice', 'local/bob', 'script', 'collect', auto_accept=True)
    assert error.value.error_code == 'DELIVERY_ADMIN_PROVISION_REQUIRED'


# LLM: 普通用户工具使用真实宿主Owner后缀，不受plugin开关控制；无绑定/伪绑定或子代理不能借cwd取得权限。
# 函数用途: 验证工具注册在本地普通用户与管理员都能准确绑定身份。
@pytest.mark.parametrize('owner_id,kind', [('local/main', 'main_agent'), ('local/alice', 'user')])
def test_delivery_tool_registration_uses_trusted_owner_binding(kit, owner_id, kind):
    from types import SimpleNamespace

    from agent_py_agent.agent.tooling.asset_delivery_tool import register_asset_delivery_tool

    tools = []
    registry = SimpleNamespace(owner_type=kind, register=tools.append)
    home = kit.main if kind == 'main_agent' else kit.alice
    params = SimpleNamespace(protected_persona_root=str(home), owner_scope_root='', operation_owner_id=owner_id)
    register_asset_delivery_tool(registry, params)
    assert len(tools) == 1 and tools[0].owner_id == owner_id
    params.operation_owner_id = 'local/bob'
    register_asset_delivery_tool(registry, params)
    assert len(tools) == 1


# LLM: 只保留重绑定需求ID，不复制可执行credential_refs或发送方凭据文件；预览应在清空引用前记下需求。
# 函数用途: 验证凭据引用移除后需求摘要仍准确提示接收方重新授权。
def test_required_credential_summary_survives_portable_descriptor_cleanup(kit):
    from agent_py_agent.agent.assets.descriptor import CredentialBindingRef

    descriptor = AssetDescriptor('needs_auth', 'script', '需授权脚本',
        credential_refs=(CredentialBindingRef('credential_fixture'),))
    ScriptStore(kit.main).save_script(descriptor, 'print("{}")')
    preview = kit.service.preview_bundle('local/main', 'script', 'needs_auth')
    assert preview['required_capabilities_summary']['required_grants'] == ['credential_fixture']
    assert preview['bundled_assets'][0]['descriptor']['credential_refs'] == []


# LLM: 内存目录模拟大小写敏感系统可存在的双ID，避免在当前文件系统伪造共存；遍历顺序不得改变精确目标。
# 函数用途: 验证overwrite/skip精确ID优先，不覆盖或复用另一个大小写等价资产。
@pytest.mark.parametrize('ids', [('collect', 'COLLECT'), ('COLLECT', 'collect')])
@pytest.mark.parametrize('strategy', ['overwrite', 'skip'])
def test_casefold_multiple_candidates_preserve_exact_target(kit, monkeypatch, ids, strategy):
    from agent_py_agent.agent.assets import script_store_support as support

    records = {'script:' + identifier: {'id': identifier} for identifier in ids}
    monkeypatch.setattr(support, '_raw_catalog', lambda _: records)
    envelope = {'sender_owner_id': 'local/main', 'delivery_id': 'dlv_fixture',
        'bundled_assets': [{'kind': 'script', 'id': 'collect'}]}
    mapping, _, _ = support.delivery_conflict_mapping(kit.alice, envelope, strategy)
    assert mapping['script:collect'] == 'collect'


# LLM: 多候选但没有精确ID时不能随遍历选择；错误发生在草稿/事务写入前。
# 函数用途: 验证歧义大小写目标失败关闭且提供领域错误码。
@pytest.mark.parametrize('strategy', ['overwrite', 'skip'])
def test_casefold_ambiguous_target_without_exact_match_is_rejected(kit, monkeypatch, strategy):
    from agent_py_agent.agent.assets import script_store_support as support
    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError

    records = {'script:' + identifier: {'id': identifier} for identifier in ('collect', 'COLLECT')}
    monkeypatch.setattr(support, '_raw_catalog', lambda _: records)
    envelope = {'sender_owner_id': 'local/main', 'delivery_id': 'dlv_fixture',
        'bundled_assets': [{'kind': 'script', 'id': 'Collect'}]}
    with pytest.raises(AssetDeliveryError) as error:
        support.delivery_conflict_mapping(kit.alice, envelope, strategy)
    assert error.value.error_code == 'ASSET_DELIVERY_CONFLICT_AMBIGUOUS'
