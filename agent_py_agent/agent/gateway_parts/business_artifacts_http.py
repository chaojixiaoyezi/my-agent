"""业务媒体运输；复用原artifact登记、Owner配额与nofollow IO，不执行上传内容。"""
from __future__ import annotations

import hashlib
import json
import os
import re
from email import policy
from email.parser import BytesFeedParser
from io import BytesIO
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
from ..plugin_host.artifacts import _fingerprint
from ..plugin_host.business_action_specs import business_api, standalone_schema
from ..plugin_host.errors import HostAPIError
from ..plugin_host.schemas import canonical_json, validate_object
from ..user_space.owner_quota import OwnerQuotaChange, owner_quota_enforcer_from_policy
from .business_api import BusinessCallContext, _native_source, native_business_context

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


def _registry(services):
    root = services.owner_home.joinpath(*_ROOT)
    parts = (*_ROOT, *REGISTRY_RELATIVE_PATH.parts)
    raw = read_bytes_beneath(services.owner_home, parts) or b''
    records, errors = latest_records_from_lines(root / REGISTRY_RELATIVE_PATH,
        raw.decode('utf-8').split('\n'), _record_from_payload)
    if errors:
        raise HostAPIError('STORE_UNAVAILABLE', 503)
    return records, len(raw)


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
    artifact_id, version = 'artifact-' + identity[:40], 'version-' + sha[:40]
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


def artifact_content(artifact_id, version_ref, context, services, *, range_header=None, if_range=''):
    validate_object({'artifact_id': artifact_id}, standalone_schema(business_api()['media_routes'][1]['request_path']))
    validate_object({'version_ref': version_ref}, standalone_schema(business_api()['media_routes'][1]['request_query']))
    records, _ = _registry(services)
    record = records.get(artifact_id)
    if (record is None or record.metadata.get('owner_id') != services.owner_id
            or record.metadata.get('version_ref') != version_ref or record.source != 'business_upload'):
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
        fd = open_readonly_file_beneath(services.owner_home, (*_ROOT, expected.name))
    except (OSError, ValueError):
        raise HostAPIError('RESOURCE_NOT_FOUND', 404) from None
    try:
        before, digest, size, chunks = os.fstat(fd), hashlib.sha256(), 0, []
        while chunk := os.read(fd, 65536):
            if size + len(chunk) > UPLOAD_MAX_BYTES:
                raise HostAPIError('ARTIFACT_CHANGED', 409)
            digest.update(chunk)
            left, right = max(0, start - size), min(len(chunk), end + 1 - size)
            if left < right:
                chunks.append(chunk[left:right])
            size += len(chunk)
        if digest.hexdigest() != record.sha256 or size != record.size_bytes or _fingerprint(before) != _fingerprint(os.fstat(fd)):
            raise HostAPIError('ARTIFACT_CHANGED', 409)
        _check(context, services, record.metadata['space_id'], record.task_id or None)
    finally:
        os.close(fd)
    content = b''.join(chunks)
    headers = {'Content-Type': record.mime_type, 'Content-Length': str(len(content)), 'ETag': etag,
        'Accept-Ranges': 'bytes', 'X-Content-Type-Options': 'nosniff', 'Content-Disposition': 'attachment'}
    if status == 206:
        headers['Content-Range'] = f'bytes {start}-{end}/{size}'
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
        handler.send_response(status)
        for key, value in headers.items():
            handler.send_header(key, value)
        handler.send_header('Cache-Control', 'private, no-store')
        handler.end_headers()
        for offset in range(0, len(content), 65536):
            handler.wfile.write(content[offset:offset + 65536])
    except HostAPIError as error:
        handler._send_json(error.status, {'error': {'code': error.code}})
    except (OSError, ValueError):
        handler._send_json(404, {'error': {'code': 'RESOURCE_NOT_FOUND'}})
