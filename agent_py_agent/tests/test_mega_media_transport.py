"""真实原registry/配额/nofollow与HTTP字节语义；不执行上传内容。"""
from io import BytesIO

import pytest

from agent_py_agent.agent.gateway_parts import business_artifacts_http as media
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.tests.test_mega_business_api_actions import host as host
from agent_py_agent.tests.test_mega_business_api_actions import native_http as native_http
from agent_py_agent.tests.test_mega_business_api_actions import native_request


@pytest.mark.parametrize('value,if_range,expected', [
    ('bytes=0-2', '', (206, 0, 2)), ('bytes=2-', '', (206, 2, 5)),
    ('bytes=-2', '', (206, 4, 5)), ('bytes=0-99', '', (206, 0, 5)),
    ('bytes=0-2', '"other"', (200, 0, 5)), (None, '', (200, 0, 5)),
])
def test_single_range_and_if_range(value, if_range, expected):
    assert media.select_range(value, if_range, '"hash"', 6) == expected


@pytest.mark.parametrize('value', ['bytes=8-9', 'bytes=2-1', 'bytes=-0', 'bytes=0-1,3-4', 'items=0-1', 'bytes='])
def test_invalid_range_is_416(value):
    with pytest.raises(HostAPIError) as error:
        media.select_range(value, '', '"hash"', 6)
    assert error.value.status == 416


def test_upload_original_registry_replay_hash_and_revocation(host):
    from agent_py_agent.agent.gateway_parts.business_api import (
        BusinessServices,
        bind_business_context,
        execute_business,
    )
    services = BusinessServices(host)
    allowed = [True]
    context = bind_business_context(host, authorize=lambda action, facts: allowed[0])
    _, created = execute_business({'action': 'spaces.create', 'request_id': 'create-one', 'params': {'name': '媒体'},
        'idempotency_key': 'media-space-key-001'}, context, services)
    fields = {'space_id': created['data']['space_id'], 'idempotency_key': 'media-upload-key-001'}
    upload = getattr(media, 'upload_artifact', None)
    assert callable(upload)
    first = upload(BytesIO(b'abcdef'), fields, '../not-a-path.txt', context, services)
    assert first['size_bytes'] == 6 and first['mime_type'] == 'text/plain'
    assert first == upload(BytesIO(b'abcdef'), fields, '../not-a-path.txt', context, services)
    with pytest.raises(HostAPIError) as conflict:
        upload(BytesIO(b'changed'), fields, 'other.txt', context, services)
    assert conflict.value.status == 409
    status, headers, content = media.artifact_content(first['artifact_id'], first['version_ref'], context, services,
        range_header='bytes=1-3')
    assert status == 206 and content == b'bcd' and headers['Content-Range'] == 'bytes 1-3/6'
    assert headers['ETag'] == '"' + first['content_sha256'] + '"'
    allowed[0] = False
    with pytest.raises(HostAPIError) as denied:
        media.artifact_content(first['artifact_id'], first['version_ref'], context, services)
    assert denied.value.status == 404


def test_native_multipart_and_exact_version_range_real_subrequests(native_http):
    import json
    status, _, raw = native_request(native_http, 'POST', '/client/business', {'action': 'spaces.create',
        'request_id': 'create-one', 'params': {'name': '视频空间'}, 'idempotency_key': 'media-native-space-key'})
    assert status == 200, raw
    sid = json.loads(raw)['data']['space_id']
    boundary = 'boundary-test'
    payload = (f'--{boundary}\r\nContent-Disposition: form-data; name="space_id"\r\n\r\n{sid}\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="idempotency_key"\r\n\r\nupload-native-key-one\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="../../not-a-path.html"\r\n'
        f'Content-Type: text/html\r\n\r\nabcdef\r\n--{boundary}--\r\n').encode()
    status, _, raw = native_request(native_http, 'POST', '/client/business/artifacts', raw=payload,
        headers={'Content-Type': 'multipart/form-data; boundary=' + boundary})
    assert status == 201, raw
    artifact = json.loads(raw)
    path = '/client/business/artifacts/' + artifact['artifact_id'] + '/content?version_ref=' + artifact['version_ref']
    status, headers, content = native_request(native_http, 'GET', path, headers={'Range': 'bytes=1-3'})
    assert status == 206 and content == b'bcd' and headers['Content-Range'] == 'bytes 1-3/6'
    assert headers['Content-Type'] == 'text/plain' and headers['X-Content-Type-Options'] == 'nosniff'
    assert native_request(native_http, 'GET', path, user='business-bob')[0] == 404
    assert native_request(native_http, 'GET', path, headers={'Range': 'bytes=10-11'})[0] == 416
    assert native_request(native_http, 'GET', path, headers={'Range': 'bytes=1-3', 'If-Range': '"other"'})[2] == b'abcdef'
