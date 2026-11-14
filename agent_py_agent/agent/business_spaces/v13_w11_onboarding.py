"""W11初始化引导与租户品牌；状态只读真实模型、数据源和空间事实。

本模块只供统一业务调度器在已签发的实时上下文内调用：读取路径不发起模型请求或
网络探测，写入路径使用原V13Store的quota、幂等回执和owner隔离。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from ..conversation.business_access import BusinessError
from ..gateway_parts.business_api_v13 import (
    HostAPIError,
    v13_call_frozen,
    v13_require_access,
)
from .v13_store import v13_store_for

# 品牌logo只接受可安全内联展示的图片类型；该集合只是白名单，不授予读取权限。
_IMAGE_MIME_TYPES = ('image/png', 'image/svg+xml', 'image/jpeg', 'image/webp')


# LLM: 时间只来自本次调用注入时钟，持久与回执统一为UTC ISO时间。
# 函数用途: 生成当前记录时间。
def _now(call):
    return datetime.fromtimestamp(call.clock(), timezone.utc).isoformat()


# LLM: 动作、owner、主体、参数和信封CAS一起进入幂等指纹，不能同键换操作。
# 函数用途: 生成当前写事务的稳定参数摘要。
def _digest(call, facts):
    value = [call.action, call.owner_id, call.subject_id, facts, call.expected_revision]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


# LLM: 领域错误码与宿主错误码字段不同，但都按原transport映射，不在此处猜HTTP状态。
# 函数用途: 读取一个异常携带的稳定错误码。
def _error_code(error):
    return getattr(error, 'error_code', None) or getattr(error, 'code', '')


# LLM: 模型目录只读本人配置；读取失败或缺来源时明确返回未知，不伪造已配置。
# 函数用途: 判断当前owner是否已有带密钥的服务商或显式选中的档案。
def _model_facts(call):
    try:
        from ..plugin_host.model_settings_v2_handlers import ModelSettingsV2Handlers
        data = ModelSettingsV2Handlers(call.context.host.home_paths).list_providers()
    except (AttributeError, ImportError, OSError, TypeError, ValueError, KeyError):
        return False, False
    providers = data.get('providers')
    if type(providers) is not list:
        return False, False
    selected = data.get('selected_profile_id')
    configured = any(row.get('has_key') is True for row in providers)
    configured = configured or (isinstance(selected, str) and selected not in ('', 'default'))
    return bool(configured), True


# LLM: 数据源清单只枚举owner已登记记录，连接测试和远程请求不在引导状态内发生。
# 函数用途: 判断当前owner是否登记过连接器。
def _connector_facts(call):
    try:
        from ..assets.datasource_store import DatasourceStore
        rows = DatasourceStore(call.owner_home).list_datasources()
    except (AttributeError, ImportError, OSError, TypeError, ValueError, KeyError):
        return False, False
    return bool(rows), True


# LLM: 空间来源仍走原冻结spaces.list及实时授权；只有来源未就绪才降级为partial。
# 函数用途: 读取当前owner可见的活跃空间。
def _space_facts(call):
    try:
        listing = v13_call_frozen(call, 'spaces.list', {'limit': 100})
    except (HostAPIError, BusinessError) as error:
        if _error_code(error) != 'HOST_NOT_READY':
            raise
        return [], False
    items = listing.get('items') if isinstance(listing, dict) else None
    if type(items) is not list:
        return [], False
    return items, True


# LLM: 品牌只投影契约声明字段；未配置就是configured=false与null，不编造产品名或颜色。
# 函数用途: 由持久记录生成公开品牌视图。
def _branding_view(row):
    if row is None:
        return {'configured': False, 'product_name': None, 'primary_color': None,
                'logo': None, 'logo_url': None, 'revision': 0, 'updated_at': None}
    logo = row.get('logo')
    logo = None if logo is None else {key: logo[key] for key in
        ('artifact_id', 'version_ref', 'name', 'mime_type', 'size_bytes', 'content_sha256')}
    return {'configured': bool(row.get('product_name') or row.get('primary_color') or logo),
            'product_name': row.get('product_name'), 'primary_color': row.get('primary_color'),
            'logo': logo, 'logo_url': _logo_url(logo), 'revision': row['revision'],
            'updated_at': row.get('updated_at')}


# LLM: 前端读取logo走原产物内容路由；URL只由已核owner与版本的产物ID拼成。
# 函数用途: 生成原生图片内容地址。
def _logo_url(logo):
    if logo is None:
        return None
    return '/client/business/artifacts/' + logo['artifact_id'] + '/content?version_ref=' + logo['version_ref']


# LLM: 图片类型与版本都来自冻结artifacts.get的真实返回，字段用返回版本重钉。
# 函数用途: 校验并投影品牌引用的logo产物。
def _logo_artifact(call):
    artifact_id = call.params.get('logo_artifact_id')
    version_ref = call.params.get('logo_version_ref')
    if artifact_id is None:
        if version_ref is not None:
            raise BusinessError('INVALID_SCHEMA')
        return None
    query = {'artifact_id': artifact_id}
    if version_ref is not None:
        query['version_ref'] = version_ref
    try:
        result = v13_call_frozen(call, 'artifacts.get', query)
    except (HostAPIError, BusinessError) as error:
        if _error_code(error) == 'OBSERVATION_NOT_FOUND':
            raise BusinessError('RESOURCE_NOT_FOUND') from error
        raise
    artifact = result['artifact']
    if result.get('available') is not True:
        raise BusinessError('RESOURCE_NOT_FOUND')
    if artifact['mime_type'] not in _IMAGE_MIME_TYPES:
        raise BusinessError('INVALID_SCHEMA')
    return {'artifact_id': artifact['artifact_id'], 'version_ref': result['version_ref'],
            'name': artifact['name'], 'mime_type': artifact['mime_type'],
            'size_bytes': artifact['size_bytes'], 'content_sha256': artifact['content_sha256']}


# LLM: 清单固定六步，done只由本次读取的真实来源决定；缺来源时coverage=partial。
# 函数用途: 汇总初始化引导状态与已完成步骤。
def status(call):
    store = v13_store_for(call)
    has_llm, llm_available = _model_facts(call)
    has_connectors, connectors_available = _connector_facts(call)
    spaces, spaces_available = _space_facts(call)
    onboarding = store.get('onboarding', call.owner_id)
    if onboarding is not None and onboarding.get('owner_id') != call.owner_id:
        raise BusinessError('RESOURCE_NOT_FOUND')
    sample_loaded = bool(onboarding and onboarding.get('sample_batch_id'))
    rules = [row for row in store.list('monitor_rules')
             if row.get('owner_id') == call.owner_id and row.get('enabled') is True]
    branding = store.get('branding', call.owner_id)
    if branding is not None and branding.get('owner_id') != call.owner_id:
        raise BusinessError('RESOURCE_NOT_FOUND')
    steps = [
        {'step': 'llm_configured', 'done': has_llm},
        {'step': 'connector_added', 'done': has_connectors},
        {'step': 'space_created', 'done': bool([row for row in spaces if row.get('state') == 'active'])},
        {'step': 'sample_data_loaded', 'done': sample_loaded},
        {'step': 'first_rule_enabled', 'done': bool(rules)},
        {'step': 'branding_set', 'done': _branding_view(branding)['configured']},
    ]
    available = llm_available and connectors_available and spaces_available
    return {'completed_steps': [item['step'] for item in steps if item['done']],
            'steps': steps, 'has_connectors': has_connectors, 'has_llm': has_llm,
            'sample_data_loaded': sample_loaded,
            'coverage': 'complete' if available else 'partial'}


# LLM: 未配置品牌返回configured=false的完整空视图；已有记录仍核owner后才投影。
# 函数用途: 读取当前租户品牌配置。
def branding_get(call):
    row = v13_store_for(call).get('branding', call.owner_id)
    if row is not None:
        if row.get('owner_id') != call.owner_id:
            raise BusinessError('RESOURCE_NOT_FOUND')
        v13_require_access(call, row)
    return {'branding': _branding_view(row)}


# LLM: logo先在原产物路由上传，再由冻结artifacts.get核owner、版本和图片类型；记录级CAS在锁内比较。
# 函数用途: 保存当前租户品牌配置。
def branding_update(call):
    logo = _logo_artifact(call)
    params = call.params

    # LLM: 旧版本不得覆盖并发新版本；省略字段保留原值，撤销授权在写入前复验。
    # 函数用途: 在品牌集合锁内提交一个新版本记录。
    def mutate(records):
        v13_require_access(call)
        previous = records.get(call.owner_id)
        if previous is not None:
            if previous.get('owner_id') != call.owner_id:
                raise BusinessError('RESOURCE_NOT_FOUND')
            v13_require_access(call, previous)
        revision = 0 if previous is None else previous['revision']
        if call.expected_revision != revision:
            raise BusinessError('REVISION_CONFLICT')
        now = _now(call)
        row = {'owner_id': call.owner_id,
               'product_name': previous.get('product_name') if previous else None,
               'primary_color': previous.get('primary_color') if previous else None,
               'logo': previous.get('logo') if previous else None,
               'revision': revision + 1, 'created_at': previous['created_at'] if previous else now,
               'updated_at': now}
        for key in ('product_name', 'primary_color'):
            if key in params:
                row[key] = params[key]
        if logo is not None:
            row['logo'] = logo
        records[call.owner_id] = row
        return {'branding': _branding_view(row)}

    return v13_store_for(call).transaction('branding', mutate, idempotency_key=call.idempotency_key,
                                           params_digest=_digest(call, params))
