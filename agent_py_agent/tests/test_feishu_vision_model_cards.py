"""模型模态只沿显式私人表单和原档案保存，不由型号或工具协议推断。"""
from __future__ import annotations

import json

import pytest

from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.gateway_parts import model_control_cards, model_profile_service
from agent_py_agent.agent.settings.model_profiles import (
    execute_model_profile_operation,
    model_profiles_path,
    read_model_profiles,
)
from agent_py_agent.tests.test_im_model_cards import callback, setup, submit
from agent_py_agent.tests.test_model_profiles import add


# LLM: 只经原/model、签名回调打开私人表单；投递是原fixture替身，不代写模型目录。
# 函数用途: 给新增和编辑共用真实控制入口，便于核验授权、字段与持久结果。
def _open_form(base, scope, profile_id=''):
    assert model_profile_service.execute_model_text_control(base, parse_conversation_control('/model'), scope).ok
    decision = 'edit' if profile_id else 'add'
    assert submit(base, callback(base, decision=decision, fields={'profile_id': profile_id}))['ok']
    card = base.delivery_service.sent[-1][1]
    return {row['name']: row for row in card['elements'][1]['elements'] if row['tag'] == 'input'}


# LLM: 合成密钥只存在隔离表单，不能进入卡片预填、控制记录或列表。
# 函数用途: 返回原表单字段及本轮显式模态声明，不猜模型能力。
def _fields(modalities='text, image', **updates):
    return {'model_backend': 'openai_compatible', 'api_base': 'https://example.test/v1',
            'model_name': 'plain-name', 'model_context_window_tokens': '64000',
            'api_key': 'synthetic-vision-card-key', 'input_modalities': modalities, **updates}


@pytest.mark.parametrize('group', [False, True])
def test_card_add_explicit_modalities_persists_original_owner(tmp_path, monkeypatch, group):
    base, _, scope, host, thread = setup(tmp_path, monkeypatch, group=group)
    inputs = _open_form(base, scope)
    assert inputs['input_modalities']['default_value'] == ''
    assert '留空' in inputs['input_modalities']['label']['content']
    assert base.delivery_service.sent[-1][0].target == 'ou_actor'
    body = callback(base, decision='save', fields=_fields('text，image image'))
    assert submit(base, body)['ok']
    data = read_model_profiles(model_profiles_path(host.home_paths))
    profile = next(iter(data['profiles'].values()))
    assert profile['input_modalities'] == ['image', 'text']
    listing = execute_model_profile_operation(host, 'list', {}, thread_id=thread.thread_id)
    assert '输入模态：image, text' in model_profile_service.render_model_choices(listing)
    assert submit(base, body)['ok']
    with pytest.raises(ValueError):
        submit(base, {**body, 'fields': {**body['fields'], 'input_modalities': 'text'}})
    assert read_model_profiles(model_profiles_path(host.home_paths)) == data
    assert 'synthetic-vision-card-key' not in json.dumps(base.delivery_service.sent, default=str)
    for path in host.home_paths.owner_home_dir.rglob('model-*.json'):
        assert 'synthetic-vision-card-key' not in path.read_text()


def test_edit_modalities_prefills_roundtrips_then_explicit_empty_is_unknown(tmp_path, monkeypatch):
    base, _, scope, host, thread = setup(tmp_path, monkeypatch)
    profile_id, _ = add(host, input_modalities='text, image')
    inputs = _open_form(base, scope, profile_id)
    assert inputs['input_modalities']['default_value'] == 'image, text'
    assert inputs['api_key'].get('default_value', '') == ''
    form = _fields('', api_base='https://example.test/anthropic', api_key='')
    assert submit(base, callback(base, decision='save', fields=form))['ok']
    data = read_model_profiles(model_profiles_path(host.home_paths))
    assert 'input_modalities' not in data['profiles'][profile_id]
    listing = execute_model_profile_operation(host, 'list', {}, thread_id=thread.thread_id)
    assert '输入模态：未知（未声明）' in model_profile_service.render_model_choices(listing)
    assert _open_form(base, scope, profile_id)['input_modalities']['default_value'] == ''


@pytest.mark.parametrize('modalities', ['text', 'depth_map, image'])
def test_form_keeps_text_only_and_open_world_declarations(tmp_path, monkeypatch, modalities):
    base, _, scope, host, _ = setup(tmp_path, monkeypatch)
    _open_form(base, scope)
    assert submit(base, callback(base, decision='save', fields=_fields(modalities)))['ok']
    profile = next(iter(read_model_profiles(model_profiles_path(host.home_paths))['profiles'].values()))
    assert profile['input_modalities'] == sorted(modalities.replace(' ', '').split(','))


@pytest.mark.parametrize('bad', ['Image', '../image', 'image; text', ['image']])
def test_invalid_modality_does_not_mutate_catalog(tmp_path, monkeypatch, bad):
    base, _, scope, host, _ = setup(tmp_path, monkeypatch)
    _open_form(base, scope)
    before = read_model_profiles(model_profiles_path(host.home_paths))
    with pytest.raises(ValueError):
        submit(base, callback(base, decision='save', fields=_fields(bad)))
    assert read_model_profiles(model_profiles_path(host.home_paths)) == before


def test_existing_five_field_card_keeps_original_declared_modalities(tmp_path, monkeypatch):
    base, _, scope, host, _ = setup(tmp_path, monkeypatch)
    profile_id, _ = add(host, input_modalities='image')
    _open_form(base, scope, profile_id)
    old_fields = _fields(api_base='https://example.test/anthropic', api_key='')
    old_fields.pop('input_modalities')
    assert submit(base, callback(base, decision='save', fields=old_fields))['ok']
    assert read_model_profiles(model_profiles_path(host.home_paths))['profiles'][profile_id]['input_modalities'] == ['image']


@pytest.mark.parametrize('modalities, expected', [([], '未知（未声明）'), (['text'], 'text'), (['image'], 'image')])
def test_display_does_not_infer_vision_from_name_or_native_tools(modalities, expected):
    listing = {'profiles': [{'id': 'one', 'model_name': 'Vision-native-image',
                            'input_modalities': modalities, 'tool_call_mode': 'native'}]}
    card = model_control_cards.model_selection_card(listing, 'opaque-ref', '本人')
    text = json.dumps(card, ensure_ascii=False)
    assert '输入模态：' + expected in text
    if not modalities:
        assert '输入模态：image' not in text and '纯文本' not in text


# LLM: 640e478ee 起正式默认只能是目录引用或管理员初始共享，部署连接不再平铺成 default 行；
#   部署配置里的型号与模态声明都不能进入选择列表，也不能借型号名推断视觉能力。
# 函数用途: 验证部署配置的型号、模态与密钥都不出现在 /model 列表及渲染文本中。
@pytest.mark.parametrize('modalities', [[], ['text'], ['image', 'text']])
def test_deployment_default_displays_only_actual_config_declaration(tmp_path, monkeypatch, modalities):
    base, _, scope, host, thread = setup(tmp_path, monkeypatch)
    base.config.model_name = 'Vision-native-image'
    base.config.model_input_modalities = modalities
    listing = execute_model_profile_operation(host, 'list', {}, thread_id=thread.thread_id)
    assert all(row['id'] != 'default' for row in listing['profiles'])
    text = model_profile_service.render_model_choices(listing)
    assert 'Vision-native-image' not in text and '输入模态：' not in text
    assert 'deployment-secret' not in json.dumps(listing)
