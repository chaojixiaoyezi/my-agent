"""按原业务授权读取精确资产版本的文本页；不搬文件、不持HTTP凭据。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import struct
import threading
import time
from collections import OrderedDict

from ..conversation.business_access import BusinessError
from ..plugin_host.errors import HostAPIError
from ..plugin_host.schemas import canonical_json, validate_object
from ..tooling.models import (
    BaseTool,
    ConcurrencyPolicy,
    EffectResolverPolicy,
    OutputPolicy,
    ResourceScopePolicy,
    ToolHandlerOutcome,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)
from .business_artifact_pages import BLOCK_BYTES, prepare_content_pages

# 单次资产正文分页读取接口允许请求的最大页字节数（64 KiB）。
_ASSET_READ_MAX_PAGE_BYTES = 65536
# 资产分页首次定位登记记录时允许扫描的最大字节预算（2 MiB）。
_ASSET_REGISTRY_READ_BYTE_BUDGET = 2 * 1024 * 1024
# 进程内分块完整性证明 LRU 缓存最多保留的条目数。
_ASSET_PROOF_LIMIT_COUNT = 16
# 进程内分块完整性证明 LRU 缓存占用的最大内存字节上限（2 MiB）。
_PROOF_BYTES = 2 * 1024 * 1024
_PROOFS = OrderedDict()
_PROOF_LOCK = threading.Lock()


def _cached(key):
    with _PROOF_LOCK:
        item = _PROOFS.get(key)
        if item is not None:
            _PROOFS.move_to_end(key)
            return item[0]
    return None


def _remember(key, proof):
    cost = len(proof.blocks) * 80 + len(key.encode()) + 512
    with _PROOF_LOCK:
        _PROOFS.pop(key, None)
        while _PROOFS and (len(_PROOFS) >= _ASSET_PROOF_LIMIT_COUNT or sum(v[1] for v in _PROOFS.values()) + cost > _PROOF_BYTES):
            _PROOFS.popitem(last=False)
        if cost <= _PROOF_BYTES:
            _PROOFS[key] = (proof, cost)


class AssetPageError(HostAPIError):
    def __init__(self, code, field_path, reason, next_action, status=400):
        super().__init__(code, status, 'not_started', reason[:256])
        self.field_path, self.next_action = field_path, next_action


def _reject(code, field, reason, next_action, status=400):
    raise AssetPageError(code, field, reason, next_action, status)


_ID = {'type': 'string', 'minLength': 1, 'maxLength': 128, 'x-max-bytes': 128}


class ReadBusinessAssetTool(BaseTool):
    model_spec = ToolModelSpec(
        name='read_business_asset',
        description=('读取已授权业务资产的精确版本UTF-8正文页。space_id/asset_id/version_ref必填，project_ref须与资产一致；'
            'selection=accepted读取正式采用版本或任务已冻结的原版本，不能省略version_ref或改读latest；'
            'selection=review只表达显式审阅候选，不授额外权限，受管任务仍只能读原冻结版本。'
            '从assets.get及assets.versions.list取得准确ID/ref；下一页原样携带同一组参数和next_cursor。'
            '二进制应原授权下载或使用该格式读取器，不猜物理路径。read_artifact只读工具归档，不能代替本工具。'),
        input_schema={'type': 'object', 'properties': {
            'space_id': _ID, 'asset_id': _ID, 'version_ref': _ID,
            'project_ref': {'type': ['string', 'null'], 'minLength': 1, 'maxLength': 128},
            'selection': {'type': 'string', 'enum': ['accepted', 'review'], 'default': 'accepted'},
            'cursor': {'type': 'string', 'minLength': 1, 'maxLength': 4096},
            'max_bytes': {'type': 'integer', 'minimum': 4, 'maximum': _ASSET_READ_MAX_PAGE_BYTES, 'default': 8192}},
            'required': ['space_id', 'asset_id', 'version_ref'], 'additionalProperties': False},
        hints=ToolModelHints(category='business', keywords=('资产', '正式素材', '版本正文', 'asset', 'version', 'read_business_asset'),
            use_cases=('业务资料已上传/采纳，需要按准确版本读正文',), default_deferred=True,
            deferred_summary='按space/asset/version读取正式或明确审阅的业务资产正文页'))
    runtime_policy = ToolRuntimePolicy(effect_resolver=EffectResolverPolicy('read_only'),
        concurrency_policy=ConcurrencyPolicy('parallel_safe'), output_policy=OutputPolicy(trust='external_data'),
        resource_scopes=ResourceScopePolicy(mode='declared', static_scopes=('business:current_owner',)))

    def __init__(self, host):
        self.host = host
        self._cursor_key = secrets.token_bytes(32)

    def _binding(self, params):
        from ..runtime_context import current_subagent_run_id
        from .business_api import local_business_context
        context, services = local_business_context(self.host, model=True)
        frozen = context.business_context
        project = params.get('project_ref')
        if frozen is not None and (params['space_id'] != frozen.space_id or project != frozen.project_ref):
            _reject('ASSET_SCOPE_MISMATCH', '$.space_id/project_ref', '请求与本任务冻结的空间/项目不一致。',
                '使用原任务冻结的space/project，不得改owner、review标志或切到其它空间。', 403)
        if frozen is not None and params['version_ref'] not in frozen.asset_version_refs:
            _reject('VERSION_SCOPE_MISMATCH', '$.version_ref', '该版本不在本任务已冻结资产版本中。',
                '回到原冻结asset/version；需要审阅另一候选时由原有权入口另行明确派活。', 403)
        if frozen is None and current_subagent_run_id(self.host):
            _reject('ASSET_SCOPE_MISMATCH', '$.asset_id', '子代缺少可核实的原业务冻结范围。',
                '由原有权任务按正式业务入口冻结并派发资产引用，不从普通参数补身份。', 403)
        actor = services.actor(context)
        state = services.spaces.authorize_call('assets.get',
            {'space_id': params['space_id'], 'asset_id': params['asset_id']}, actor=actor)
        row = state['assets'][params['asset_id']]
        if row['project_ref'] != project:
            _reject('ASSET_SCOPE_MISMATCH', '$.project_ref', '显式项目与已授权资产归属不一致。',
                '从已授权assets.get读取准确project_ref后调用，不扩大当前范围。', 403)
        versions = [v for v in row['versions'] if v['version_ref'] == params['version_ref']]
        if len(versions) != 1:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        version = versions[0]
        if params.get('selection', 'accepted') == 'accepted' and (
                version['state'] not in {'accepted', 'superseded'} or frozen is None and row['accepted_version'] != params['version_ref']):
            _reject('VERSION_SCOPE_MISMATCH', '$.version_ref', '请求不是当前正式采用或原任务冻结的正式版本。',
                '执行请使用准确accepted_version；显式候选审阅用selection=review且仍需当前原授权。', 403)
        run = getattr(self.host, '_current_run_params', None)
        binding = [context.owner_id, context.subject_id, getattr(context.actor, 'source_thread_id', ''),
            getattr(run, 'request_id', ''), frozen.task_id if frozen else None,
            params['space_id'], project, params['asset_id'], params['version_ref'],
            params.get('selection', 'accepted'), version['artifact_ref']]
        return context, services, row, version, binding

    def _position(self, cursor, binding):
        if cursor is None:
            return 0
        try:
            data = base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True)
            if base64.urlsafe_b64encode(data).decode().rstrip('=') != cursor:
                raise ValueError('noncanonical encoding')
            if len(data) != 49:
                raise ValueError('length')
            raw, signature = data[:17], data[17:]
            authenticated = raw + canonical_json(binding).encode()
            if not hmac.compare_digest(signature, hmac.digest(self._cursor_key, authenticated, 'sha256')):
                raise ValueError('signature')
            version, offset, expires = struct.unpack('!BQQ', raw)
            if version != 1 or expires <= time.time():
                raise ValueError('expired version')
            return offset
        except (ValueError, TypeError, KeyError, UnicodeError, struct.error):
            _reject('CURSOR_EXPIRED', '$.cursor', '游标截断、过期或不属于当前原授权/精确版本。',
                '丢弃旧cursor，以同一已授权space/project/asset/version从首读重取；不能切latest。', 409)

    def _cursor(self, binding, offset):
        # 只运输固定长度的位置/期限；原身份与精确版本仍纳入MAC且每页重新授权。
        raw = struct.pack('!BQQ', 1, offset, int(time.time()) + 1200)
        signature = hmac.digest(self._cursor_key, raw + canonical_json(binding).encode(), 'sha256')
        return base64.urlsafe_b64encode(raw + signature).decode().rstrip('=')

    @staticmethod
    def _chunks(content):
        if isinstance(content, bytes):
            for start in range(0, len(content), BLOCK_BYTES):
                yield content[start:start + BLOCK_BYTES]
        else:
            try:
                yield from content.chunks()
            finally:
                content.close()

    def read_page(self, params):
        from .business_artifacts_http import artifact_content
        validate_object(params, self.model_spec.input_schema)
        context, services, row, version, binding = self._binding(params)
        offset = self._position(params.get('cursor'), binding)
        ref = version['artifact_ref']
        key = canonical_json([str(services.owner_home), context.owner_id, ref])
        proof = _cached(key) if params.get('cursor') is not None else None
        if params.get('cursor') is not None and proof is None:
            _reject('CURSOR_EXPIRED', '$.cursor', '原版本分块证明已淘汰或服务已重启。',
                '用同一已授权space/project/asset/version从首读重取，不换版本。', 409)
        maximum = params.get('max_bytes', 8192)
        if proof is not None and offset > proof.size_bytes:
            _reject('CURSOR_EXPIRED', '$.cursor', '游标字节位置超过原版本。', '对同一精确版本从首读重取。', 409)
        status, headers, content = artifact_content(ref['artifact_id'], ref['version_ref'], context, services,
            range_header=f'bytes={offset}-{offset + maximum - 1}' if proof and proof.size_bytes else None,
            page_proof=proof, expected_space_id=params['space_id'], registry_max_bytes=_ASSET_REGISTRY_READ_BYTE_BUDGET)
        mime = headers.get('Content-Type', 'application/octet-stream').split(';', 1)[0].lower()
        if (mime.startswith(('image/', 'audio/', 'video/')) or mime in {'application/pdf', 'application/zip', 'application/gzip'}):
            if not isinstance(content, bytes):
                content.close()
            _reject('BINARY_CONTENT', '$.version_ref', '该确切版本是二进制媒体，未把正文解释为UTF-8文本。',
                '沿原授权下载同一精确版本或使用适合该MIME的读取器，不猜物理路径。', 415)
        etag = headers.get('ETag', '')
        digest = etag[1:-1] if etag.startswith('"') and etag.endswith('"') else ''
        expected_hash = ref.get('content_sha256') or (proof.sha256 if proof is not None else digest)
        if (len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest) or digest != expected_hash):
            if not isinstance(content, bytes):
                content.close()
            raise HostAPIError('ARTIFACT_CHANGED', 409)
        if proof is None:
            size = int(headers['Content-Length'])
            try:
                proof, data = prepare_content_pages(self._chunks(content), sha256=expected_hash,
                    size_bytes=size, prefix_bytes=maximum, registry_proof=getattr(headers, 'registry_proof', None))
            except UnicodeError:
                _reject('BINARY_CONTENT', '$.version_ref', '该版本不是完整有效的UTF-8文本或含二进制NUL。',
                    '沿原授权下载该精确版本或使用相应格式读取器，不强制解码。', 415)
            finally:
                if not isinstance(content, bytes):
                    content.close()
            _remember(key, proof)
        else:
            if not isinstance(content, bytes):
                content.close()
                raise HostAPIError('ARTIFACT_CHANGED', 409)
            data = content
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError as error:
            if error.reason == 'unexpected end of data' and error.start >= len(data) - 3:
                data = data[:error.start]
                text = data.decode('utf-8')
            else:
                raise HostAPIError('ARTIFACT_CHANGED', 409) from error
        # 发送前再过同一原来源/当前权限与冻结版本门；证明缓存从不替代授权。
        self._binding(params)
        next_offset = offset + len(data)
        return {'schema': 'business_asset_page.v1', 'space_id': row['space_id'], 'project_ref': row['project_ref'],
            'asset_id': row['asset_id'], 'version_ref': version['version_ref'],
            'source_artifact': {**ref, 'content_sha256': expected_hash},
            'mime_type': mime, 'encoding': 'utf-8', 'byte_offset': offset, 'returned_bytes': len(data),
            'total_bytes': proof.size_bytes, 'complete': next_offset >= proof.size_bytes,
            'next_cursor': self._cursor(binding, next_offset) if next_offset < proof.size_bytes else None,
            'page_integrity_verified': True, 'verification': 'original_version_blocks', 'content': text}

    def execute(self, params):
        try:
            return ToolHandlerOutcome(self.model_spec.name, True, json.dumps(self.read_page(params), ensure_ascii=False))
        except (HostAPIError, BusinessError) as error:
            details = getattr(error, 'details', {}) or {}
            code = error.code
            payload = {'ok': False, 'error_code': code, 'effect_outcome': 'not_started',
                'field_path': getattr(error, 'field_path', details.get('field_path', '$')),
                'reason': getattr(error, 'message', details.get('reason', code)),
                'next_action': getattr(error, 'next_action', details.get('next_action',
                    '从当前已授权assets.get/versions.list核对准确引用及权限；保留原版本，不猜路径或切latest。'))}
            return ToolHandlerOutcome(self.model_spec.name, False, json.dumps(payload, ensure_ascii=False),
                error_code=code, reported_error_code=code, effect_outcome='not_started')
