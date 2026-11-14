"""业务媒体运输；复用原artifact登记、Owner配额与nofollow IO，不执行上传内容。"""
from __future__ import annotations

import hashlib
import json
import os
import re
from email import policy
from email.parser import BytesFeedParser
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ..artifacts.registry import (
    REGISTRY_RELATIVE_PATH,
    ArtifactRegistration,
    _record_from_payload,
    register_observed_artifact,
)
from ..artifacts.registry_reader import latest_records_from_lines
from ..common.directory_lock import locked_private_directory
from ..common.nofollow_fs import (
    open_readonly_file_beneath,
    read_bytes_beneath,
    write_bytes_atomic_beneath,
)
from ..conversation.business_access import BusinessError, require_access
from ..plugin_host.artifacts import _fingerprint
from ..plugin_host.business_action_specs import business_api, standalone_schema
from ..plugin_host.errors import HostAPIError
from ..plugin_host.schemas import canonical_json, validate_object
from ..user_space.owner_quota import OwnerQuotaChange, owner_quota_enforcer_from_policy
from .business_api import BusinessCallContext, _native_source, native_business_context
from .business_artifact_stream import VerifiedArtifactStream, verified_workflow_stream

UPLOAD_MAX_BYTES = 16 * 1024 * 1024
_ROOT = ('artifacts', 'business')


def select_range(value, if_range, etag, size):
    if value is None or if_range and if_range != etag:
        return 200, 0, size - 1
    match = re.fullmatch(r'bytes=([0-9]*)-([0-9]*)', value) if len(value) <= 128 else None
    if not match or not any(match.groups()) or size == 0:
        raise HostAPIError('RANGE_NOT_SATISFIABLE', 416)
    left, right = match.groups()
    if left:
        start = int(left)
        end = min(int(right), size - 1) if right else size - 1
    else:
        suffix = int(right)
        if suffix == 0:
            raise HostAPIError('RANGE_NOT_SATISFIABLE', 416)
        start, end = max(0, size - suffix), size - 1
    if start >= size or end < start:
        raise HostAPIError('RANGE_NOT_SATISFIABLE', 416)
    return 206, start, end


def _registry(services, *, max_bytes=None):
    return _registry_at(services.owner_home, _ROOT, max_bytes=max_bytes)


def _registry_at(owner_home, relative_root, *, max_bytes=None):
    root = owner_home.joinpath(*relative_root)
    parts = (*relative_root, *REGISTRY_RELATIVE_PATH.parts)
    if max_bytes is None:
        raw = read_bytes_beneath(owner_home, parts) or b''
    else:
        try:
            raw = read_bytes_beneath(owner_home, parts, max_bytes=max_bytes + 1) or b''
        except ValueError as error:
            raise HostAPIError('HOST_NOT_READY', 503, message=(
                f'业务媒体原登记簿无法在{max_bytes}字节预算内核验；请检查原登记源，不得当作不存在覆盖。')) from error
        if len(raw) > max_bytes:
            raise HostAPIError('HOST_NOT_READY', 503, message=(
                f'业务媒体原登记簿超过{max_bytes}字节读取预算；请检查原登记源，不得当作不存在覆盖。'))
    try:
        lines = raw.decode('utf-8').split('\n')
    except UnicodeDecodeError:
        raise HostAPIError('STORE_UNAVAILABLE', 503) from None
    records, errors = latest_records_from_lines(root / REGISTRY_RELATIVE_PATH,
        lines, _record_from_payload)
    if errors:
        raise HostAPIError('STORE_UNAVAILABLE', 503)
    return records, len(raw)


def _verified_blob(owner_home, parts, record, *, start=0, end=-1, max_bytes=None, page_proof=None):
    """只读取原登记路径的nofollow FD；登记与运输共用字节、尺寸及读取前后身份核验。"""
    if (record.path != str(owner_home.joinpath(*parts)) or record.status != 'ready'
            or not re.fullmatch(r'[0-9a-f]{64}', record.sha256)
            or record.size_bytes < 0 or max_bytes is not None and record.size_bytes > max_bytes):
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    if page_proof is not None:
        from .business_artifact_pages import read_verified_content_page
        return read_verified_content_page(owner_home, parts, record, page_proof, start=start, end=end)
    fd = open_readonly_file_beneath(owner_home, parts)
    try:
        before, digest, size, chunks = os.fstat(fd), hashlib.sha256(), 0, []
        while chunk := os.read(fd, 65536):
            if size + len(chunk) > record.size_bytes:
                raise HostAPIError('ARTIFACT_CHANGED', 409)
            digest.update(chunk)
            left, right = max(0, start - size), min(len(chunk), end + 1 - size)
            if left < right:
                chunks.append(chunk[left:right])
            size += len(chunk)
        if digest.hexdigest() != record.sha256 or size != record.size_bytes or _fingerprint(before) != _fingerprint(os.fstat(fd)):
            raise HostAPIError('ARTIFACT_CHANGED', 409)
    finally:
        os.close(fd)
    return b''.join(chunks)


def artifact_version_reference(owner_home, owner_id, version_ref):
    """按原registry定位版本；拒绝歧义，不从外部版本引用构造文件路径。"""
    records, _ = _registry_at(owner_home, _ROOT)
    matches = [row for row in records.values() if row.metadata.get('version_ref') == version_ref]
    if matches:
        if len(matches) != 1:
            return None
        record = matches[0]
        if (record.source != 'business_upload' or record.metadata.get('owner_id') != owner_id
                or not record.metadata.get('space_id')
                or not re.fullmatch(r'artifact-[0-9a-f]{40}', record.artifact_id)):
            return None
        _verified_blob(owner_home, (*_ROOT, record.artifact_id + '.blob'), record, max_bytes=UPLOAD_MAX_BYTES)
        return {'owner_id': owner_id, 'space_id': record.metadata['space_id'], 'ref': version_ref,
                'artifact_id': record.artifact_id, 'version_ref': version_ref,
                'content_sha256': record.sha256, 'state': 'available', 'kind': 'artifact_version'}
    # 保留Owner原registry中的旧文件引用；路径必须与原登记一致，且本次重新核字节。
    records, _ = _registry_at(owner_home, ())
    record = records.get(version_ref)
    if record is None or version_ref != os.path.basename(version_ref):
        return None
    if record.metadata.get('owner_id', owner_id) != owner_id:
        return None
    _verified_blob(owner_home, ('artifacts', version_ref), record)
    return {'owner_id': owner_id, 'ref': version_ref, 'artifact_id': record.artifact_id,
            'version_ref': version_ref, 'content_sha256': record.sha256,
            'space_id': record.metadata.get('space_id'), 'state': 'available', 'kind': 'artifact_version'}


def _check(context, services, space_id, task_id, *, upload=False):
    if (type(context) is not BusinessCallContext or context.owner_id != services.owner_id
            or context.host.home_paths.owner_home_dir != services.host.home_paths.owner_home_dir):
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    context.check('spaces.update' if upload else 'spaces.get', {'owner_id': services.owner_id, 'space_id': space_id})
    services.spaces.get(space_id, actor=services.actor(context))
    if task_id:
        reader = getattr(services.host, 'business_reference_reader', None)
        if not callable(reader):
            raise HostAPIError('HOST_NOT_READY', 503)
        row = reader('task', task_id, services.actor(context))
        if (not isinstance(row, dict) or row.get('owner_id') != services.owner_id
                or row.get('space_id') != space_id):
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)


def _view(record):
    data = {'artifact_id': record.artifact_id, 'version_ref': record.metadata['version_ref'],
        'name': record.metadata['display_name'], 'mime_type': record.mime_type, 'size_bytes': record.size_bytes,
        'content_sha256': record.sha256, 'task_id': record.task_id or None,
        'space_id': record.metadata['space_id'], 'available': record.status == 'ready'}
    return validate_object(data, standalone_schema({'$ref': '#/$defs/ArtifactView'}))


def business_upload_artifact(artifact_id, context, services):
    """在已声明的原上传主账准确查ID；空只证明此来源无记录，不证明其它产物来源为空。"""
    if not isinstance(artifact_id, str) or not re.fullmatch(r'artifact-[0-9a-f]{40}', artifact_id):
        raise HostAPIError('INVALID_SCHEMA', 400)
    if (type(context) is not BusinessCallContext or context.owner_id != services.owner_id
            or Path(context.host.home_paths.owner_home_dir).resolve() != services.owner_home.resolve()):
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    context.check('artifacts.get', {'owner_id': services.owner_id, 'artifact_id': artifact_id})
    from .business_upload_registry import lookup_business_upload

    record = lookup_business_upload(services.owner_home, artifact_id).record
    if record is None:
        return None
    if record.source != 'business_upload' or record.metadata.get('owner_id') != services.owner_id:
        raise HostAPIError('HOST_NOT_READY', 503, message=(
            '该ID在业务媒体原主账已有其它来源或归属不符的记录；请核对原记录，不能按缺失覆盖。'))
    _check(context, services, record.metadata.get('space_id'), record.task_id or None)
    return _view(record)


def _mime(data):
    for prefix, mime in ((b'\x89PNG\r\n\x1a\n', 'image/png'), (b'\xff\xd8\xff', 'image/jpeg'),
                         (b'GIF8', 'image/gif'), (b'%PDF-', 'application/pdf')):
        if data.startswith(prefix):
            return mime
    if len(data) >= 12 and data[4:8] == b'ftyp':
        return 'video/mp4'
    try:
        text = data.decode('utf-8')
        if all(ord(c) >= 32 or c in '\n\r\t' for c in text):
            return 'text/plain'
    except UnicodeError:
        pass
    return 'application/octet-stream'


# LLM: 保持 quota admission → business writer lock 顺序；同一锁覆盖账本读取与原子发表，失败不覆盖未知 blob。
# 函数用途: 在原配额和授权边界内写入业务媒体，并把完整登记原子发表到唯一账本。
def upload_artifact(source, fields, filename, context, services):
    schema = business_api()['media_routes'][0]['request_fields']
    validate_object({'file': 'binary', **fields}, standalone_schema(schema))
    sid, tid = fields['space_id'], fields.get('task_id')
    _check(context, services, sid, tid, upload=True)
    # 原文件写口是原子bytes提交；运输按64KiB有界读取，硬上限16MiB，绝不无界read。
    chunks, size = [], 0
    while True:
        chunk = source.read(min(65536, UPLOAD_MAX_BYTES + 1 - size))
        if not chunk:
            break
        size += len(chunk)
        if size > UPLOAD_MAX_BYTES:
            raise HostAPIError('PAYLOAD_TOO_LARGE', 413)
        chunks.append(chunk)
    data = b''.join(chunks)
    sha = hashlib.sha256(data).hexdigest()
    identity = hashlib.sha256(canonical_json([services.owner_id, context.subject_id, fields['idempotency_key']]).encode()).hexdigest()
    artifact_id = 'artifact-' + identity[:40]
    # 版本查询端口只接收version_ref；不同产物即使字节相同也必须能无歧义定位。
    version = 'version-' + hashlib.sha256(canonical_json([artifact_id, sha]).encode()).hexdigest()[:40]
    fingerprint = hashlib.sha256(canonical_json([services.owner_id, sid, tid, sha]).encode()).hexdigest()
    root = services.owner_home.joinpath(*_ROOT)
    path = root / (artifact_id + '.blob')
    with owner_quota_enforcer_from_policy(services.owner_home).admission() as admission:
        with locked_private_directory(services.owner_home, relative_parts=_ROOT, lock_name='.business-artifacts.lock'):
            _check(context, services, sid, tid, upload=True)
            records, registry_size = _registry(services)
            if old := records.get(artifact_id):
                if old.metadata.get('business_upload_fingerprint') != fingerprint:
                    raise HostAPIError('IDEMPOTENCY_CONFLICT', 409)
                return _view(old)
            # 原登记未确认而blob已存在，不拿新正文覆盖未知副作用，也不另分配ID重试。
            if path.exists() or path.is_symlink():
                raise HostAPIError('OUTCOME_UNKNOWN', 503, 'unknown')
            metadata = {'owner_id': services.owner_id, 'space_id': sid, 'version_ref': version,
                'display_name': (str(filename).replace('/', '_').replace('\\', '_').replace('\r', '_').replace('\n', '_')[:255] or 'upload'),
                'business_upload_fingerprint': fingerprint}
            request = ArtifactRegistration(root, path, artifact_id=artifact_id, task_id=tid or '',
                mime_type=_mime(data), status='ready', source='business_upload', metadata=metadata)
            admission.check([OwnerQuotaChange(path, final_size=len(data)),
                OwnerQuotaChange(root / REGISTRY_RELATIVE_PATH, final_size=registry_size + 8192)])
            _check(context, services, sid, tid, upload=True)
            write_bytes_atomic_beneath(services.owner_home, (*_ROOT, path.name), data)
            record = register_observed_artifact(request, observed_sha256=sha, observed_size_bytes=size,
                atomic_publish=True)
            _check(context, services, sid, tid, upload=True)
            return _view(record)


def _native_registry_content(artifact_id, version_ref, context, services, *, range_header, if_range,
                             page_proof=None, expected_space_id=None):
    from ..artifacts import registry_locator
    from ..conversation.registered_artifact_observation import registered_artifact
    from ..conversation.task_artifact_observation import (
        guard_artifact_content_budget,
        task_artifact,
    )
    from .business_api_v13_readers import _OwnerReaders

    store = getattr(services.host, 'conversation_store', None)
    if store is None:
        return None
    observation = services.observation
    source = _OwnerReaders(services.owner_home, services.owner_id, store, observation.runtime,
        workspace_task_path_template=getattr(services.host.config, 'workspace_task_path_template', None))
    authority = (observation, services.observation_actor(context), 'artifacts.get')
    workflow_source = artifact_id.startswith('wfart-')
    if workflow_source:
        from ..conversation.workflow_artifact_observation import workflow_artifact

        found = workflow_artifact(source, artifact_id, version_ref, with_content=False, authority=authority)
    else:
        found = registered_artifact(source, artifact_id, version_ref, with_content=False, authority=authority)
    record = content = None
    if found is not None:
        facts, record, content = found
        space_id, task_id = facts.space_id, facts.task_id
        if workflow_source and not facts.value['available']:
            raise HostAPIError('ARTIFACT_CONTENT_MISSING', 404)
    elif registry_locator.lookup(services.owner_home, artifact_id) is not None:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    else:
        present, artifact, task, scope = task_artifact(store, services.owner_home, services.owner_id, artifact_id, version_ref, authority=authority)
        if not present:
            return None
        if artifact is None or not artifact['available']:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        link, error = store.tasks.link_report(task.target_thread_id, task.task_id)
        if error or link is None:
            raise HostAPIError('STORE_UNAVAILABLE', 503)
        records, errors = _registry_at(services.owner_home, Path(link.task_path).relative_to(services.owner_home).parts)
        record = records.get(artifact_id)
        if errors or record is None:
            raise HostAPIError('STORE_UNAVAILABLE', 503)
        space_id, task_id = scope.space_id if scope else None, task.task_id
        from ..conversation.business_observability import ObservationRecord

        facts = ObservationRecord({}, services.owner_id, space_id, scope.project_ref if scope else None,
            task_id, task.target_thread_id)
    # 共用原观察逐记录裁决；不为授权重新打开内容，运输FD才完整核验一次hash。
    actor = services.observation_actor(context)
    params = {'artifact_id': artifact_id, 'version_ref': version_ref}
    def check_visible():
        observation.authorize_call('artifacts.get', params, actor=actor)
        require_access(actor, 'artifacts.get', service=observation, owner_id=facts.owner_id,
            space_id=facts.space_id, project_ref=facts.project_ref, task_id=facts.task_id,
            thread_id=facts.thread_id, run_id=facts.run_id, attempt_id=facts.attempt_id)
        if space_id is not None:
            _check(context, services, space_id, task_id)
        else:
            # 无空间主会话沿已核验的原run/TaskLink与逐记录授权；不能误借后台SessionTask读口。
            if (type(context) is not BusinessCallContext or context.owner_id != services.owner_id
                    or context.host.home_paths.owner_home_dir != services.host.home_paths.owner_home_dir):
                raise HostAPIError('RESOURCE_NOT_FOUND', 404)
            context.check('spaces.get', {'owner_id': services.owner_id, 'space_id': None})
        if workflow_source:
            latest, pinned, _ = workflow_artifact(source, artifact_id, version_ref, with_content=False, authority=authority)
            if (latest.owner_id, latest.task_id, latest.thread_id, pinned.path, pinned.sha256, pinned.size_bytes) != (
                    facts.owner_id, facts.task_id, facts.thread_id, record.path, record.sha256, record.size_bytes):
                raise HostAPIError('ARTIFACT_CHANGED', 409)
    check_visible()
    if expected_space_id is not None and space_id != expected_space_id:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    if record.status != 'ready':
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    etag = '"' + record.sha256 + '"'
    status, start, end = select_range(range_header, if_range, etag, record.size_bytes)
    parts = Path(record.path).relative_to(services.owner_home).parts
    if workflow_source and page_proof is not None:
        from .business_artifact_pages import read_verified_content_page
        content = read_verified_content_page(services.owner_home, parts, record, page_proof, start=start, end=end)
        check_visible()
        content_length = len(content)
    elif workflow_source:
        content = verified_workflow_stream(services.owner_home, parts, record,
            start=start, end=end, check_visible=check_visible)
        content_length = content.length
    else:
        guard_artifact_content_budget(services.owner_home, Path(record.path), record.size_bytes, UPLOAD_MAX_BYTES)
        content = _verified_blob(services.owner_home, parts, record, start=start, end=end, max_bytes=UPLOAD_MAX_BYTES, page_proof=page_proof)
        check_visible()
        content_length = len(content)
    headers = {'Content-Type': record.mime_type or 'application/octet-stream', 'Content-Length': str(content_length),
        'ETag': etag, 'Accept-Ranges': 'bytes', 'X-Content-Type-Options': 'nosniff', 'Content-Disposition': 'attachment'}
    if status == 206:
        headers['Content-Range'] = f'bytes {start}-{end}/{record.size_bytes}'
    return status, headers, content


class _UploadContentHeaders(dict):
    """原HTTP头只序列化dict项；原登记证明仅传给同次正文页工厂。"""

    registry_proof = None


def artifact_content(artifact_id, version_ref, context, services, *, range_header=None, if_range='',
                     page_proof=None, expected_space_id=None, registry_max_bytes=None):
    validate_object({'artifact_id': artifact_id}, standalone_schema(business_api()['media_routes'][1]['request_path']))
    validate_object({'version_ref': version_ref}, standalone_schema(business_api()['media_routes'][1]['request_query']))
    try:
        native = _native_registry_content(artifact_id, version_ref, context, services,
            range_header=range_header, if_range=if_range, page_proof=page_proof, expected_space_id=expected_space_id)
    except BusinessError as error:
        status = 404 if error.code in {'OBSERVATION_NOT_FOUND', 'BUSINESS_ACCESS_DENIED', 'SPACE_SCOPE_MISMATCH'} else 409 if error.code == 'STATE_CONFLICT' else 503
        reason = str(error.details.get('reason') or error.code)
        raise HostAPIError(error.code, status, message=(reason + '；未读取对外内容，请核验原登记或授权后重试。')[:256]) from error
    if native is not None:
        return native
    from .business_upload_registry import lookup_business_upload, verify_record_proof

    registry_proof = getattr(page_proof, 'registry_proof', None)
    if registry_proof is not None:
        record = verify_record_proof(services.owner_home, registry_proof)
    else:
        lookup = lookup_business_upload(services.owner_home, artifact_id,
            max_bytes=registry_max_bytes or 2 * 1024 * 1024)
        record, registry_proof = lookup.record, lookup.proof
    if record is None and getattr(services.host, 'conversation_store', None) is not None:
        # 正式详情读口决定未发现是否确已完整；旧来源未覆盖时不能伪装确定404。
        try:
            services.observation.query('artifacts.get', {'artifact_id': artifact_id, 'version_ref': version_ref},
                actor=services.observation_actor(context))
        except BusinessError as error:
            status = 404 if error.code in {'OBSERVATION_NOT_FOUND', 'BUSINESS_ACCESS_DENIED', 'SPACE_SCOPE_MISMATCH'} else 409 if error.code == 'STATE_CONFLICT' else 503
            raise HostAPIError(error.code, status, message=str(error.details.get('reason') or error.code)[:256]) from error
    if (record is None or record.status != 'ready' or record.metadata.get('owner_id') != services.owner_id
            or record.metadata.get('version_ref') != version_ref or record.source != 'business_upload'):
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    if expected_space_id is not None and record.metadata.get('space_id') != expected_space_id:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    try:
        _check(context, services, record.metadata['space_id'], record.task_id or None)
    except (HostAPIError, ValueError):
        raise HostAPIError('RESOURCE_NOT_FOUND', 404) from None
    expected = services.owner_home.joinpath(*_ROOT, artifact_id + '.blob')
    if record.path != str(expected) or record.size_bytes > UPLOAD_MAX_BYTES:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    etag = '"' + record.sha256 + '"'
    status, start, end = select_range(range_header, if_range, etag, record.size_bytes)
    try:
        content = _verified_blob(services.owner_home, (*_ROOT, expected.name), record,
                                 start=start, end=end, max_bytes=UPLOAD_MAX_BYTES, page_proof=page_proof)
    except HostAPIError:
        raise
    except (OSError, ValueError):
        raise HostAPIError('RESOURCE_NOT_FOUND', 404) from None
    _check(context, services, record.metadata['space_id'], record.task_id or None)
    headers = _UploadContentHeaders({'Content-Type': record.mime_type, 'Content-Length': str(len(content)), 'ETag': etag,
        'Accept-Ranges': 'bytes', 'X-Content-Type-Options': 'nosniff', 'Content-Disposition': 'attachment'})
    headers.registry_proof = registry_proof
    if status == 206:
        headers['Content-Range'] = f'bytes {start}-{end}/{record.size_bytes}'
    return status, headers, content


def _multipart(handler):
    lengths = handler.headers.get_all('Content-Length', [])
    if (len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,10}', lengths[0])
            or handler.headers.get('Transfer-Encoding') or handler.headers.get('Content-Encoding')
            or handler.headers.get_content_type() != 'multipart/form-data'):
        raise HostAPIError('INVALID_SCHEMA')
    length = int(lengths[0])
    if length > UPLOAD_MAX_BYTES + 65536:
        raise HostAPIError('PAYLOAD_TOO_LARGE', 413)
    boundary = handler.headers.get_param('boundary')
    if not boundary or not re.fullmatch(r'[A-Za-z0-9_+.,:=?-]{1,70}', boundary):
        raise HostAPIError('INVALID_SCHEMA')
    parser = BytesFeedParser(policy=policy.default)
    parser.feed(('Content-Type: multipart/form-data; boundary="' + boundary + '"\r\nMIME-Version: 1.0\r\n\r\n').encode())
    remaining = length
    while remaining:
        chunk = handler.rfile.read(min(65536, remaining))
        if not chunk:
            raise HostAPIError('INVALID_SCHEMA')
        parser.feed(chunk)
        remaining -= len(chunk)
    message = parser.close()
    if message.defects or not message.is_multipart() or (message.epilogue or '').strip():
        raise HostAPIError('INVALID_SCHEMA')
    fields, file, filename = {}, None, 'upload'
    parts = list(message.iter_parts())
    if len(parts) > 4:
        raise HostAPIError('INVALID_SCHEMA')
    for part in parts:
        name = part.get_param('name', header='content-disposition')
        if (part.defects or part.get_content_disposition() != 'form-data' or part.is_multipart()
                or part.get('Content-Transfer-Encoding') not in (None, 'binary', '8bit')):
            raise HostAPIError('INVALID_SCHEMA')
        if name == 'file' and file is None:
            file = part.get_payload(decode=True)
            filename = part.get_filename() or filename
        elif name in {'space_id', 'task_id', 'idempotency_key'} and name not in fields:
            raw = part.get_payload(decode=True)
            if len(raw) > 128:
                raise HostAPIError('INVALID_SCHEMA')
            fields[name] = raw.decode('utf-8')
        else:
            raise HostAPIError('INVALID_SCHEMA')
    if file is None:
        raise HostAPIError('INVALID_SCHEMA')
    return BytesIO(file), fields, filename


def handle_business_artifact_upload(handler, server):
    entered = False
    try:
        if not _native_source(handler):
            return
        context, services = native_business_context(handler, server)
        from .business_ui_http import enforce_browser_request_token
        enforce_browser_request_token(handler, server, context, services)
        source, fields, filename = _multipart(handler)
        entered = True
        result = upload_artifact(source, fields, filename, context, services)
        handler._send_json(201, result)
    except HostAPIError as error:
        handler._send_json(error.status, {'error': {'code': error.code}, 'effect_outcome': error.outcome})
    except (OSError, ValueError, UnicodeError):
        handler._send_json(503 if entered else 400, {'error': {'code': 'OUTCOME_UNKNOWN' if entered else 'INVALID_SCHEMA'},
            'effect_outcome': 'unknown' if entered else 'not_started'})


def handle_business_artifact_content(handler, server):
    content, response_started = None, False
    try:
        if not _native_source(handler):
            return
        context, services = native_business_context(handler, server)
        url = urlsplit(handler.path)
        match = re.fullmatch(r'/client/business/artifacts/([^/]+)/content', url.path)
        query = parse_qs(url.query, keep_blank_values=True, strict_parsing=True)
        if not match or set(query) != {'version_ref'} or len(query['version_ref']) != 1:
            raise HostAPIError('INVALID_SCHEMA')
        for name in ('Range', 'If-Range'):
            if len(handler.headers.get_all(name, [])) > 1:
                raise HostAPIError('RANGE_NOT_SATISFIABLE', 416)
        status, headers, content = artifact_content(match[1], query['version_ref'][0], context, services,
            range_header=handler.headers.get('Range'), if_range=handler.headers.get('If-Range', ''))
        if isinstance(content, VerifiedArtifactStream):
            # 返回解析器之后、发表HTTP头之前仍按原task/space/run逐项授权。
            content.check_visible()
        handler.send_response(status)
        for key, value in headers.items():
            handler.send_header(key, value)
        handler.send_header('Cache-Control', 'private, no-store')
        handler.end_headers()
        response_started = True
        if isinstance(content, VerifiedArtifactStream):
            for chunk in content.chunks():
                handler.wfile.write(chunk)
        else:
            for offset in range(0, len(content), 65536):
                handler.wfile.write(content[offset:offset + 65536])
    except HostAPIError as error:
        if response_started:
            handler.close_connection = True
        else:
            handler._send_json(error.status, {'error': error.to_payload()})
    except (FileNotFoundError, ValueError):
        if response_started:
            handler.close_connection = True
        else:
            handler._send_json(404, {'error': {'code': 'RESOURCE_NOT_FOUND'}})
    except OSError as error:
        if response_started:
            handler.close_connection = True
        else:
            # EACCES/ENOSPC等不是不存在；不回显原异常正文、路径或私有参数。
            diagnostic = f'{type(error).__name__}(errno={error.errno})'
            failure = HostAPIError('HOST_NOT_READY', 503, message=(
                'ARTIFACT_CONTENT_UNREADABLE；' + diagnostic +
                '；未发送内容；检查原文件读权限或临时空间后重试同一版本。'))
            handler._send_json(failure.status, {'error': failure.to_payload()})
    finally:
        if isinstance(content, VerifiedArtifactStream):
            content.close()
