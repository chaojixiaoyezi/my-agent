"""正式工作流下载的私有临时快照；完整校验后才允许发送，有界内存且不重开源路径。"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile

from ..common.nofollow_fs import open_readonly_file_beneath
from ..plugin_host.artifacts import _fingerprint
from ..plugin_host.errors import HostAPIError

WORKFLOW_DOWNLOAD_MAX_BYTES = 64 * 1024 * 1024
CHUNK_BYTES = 65536


class VerifiedArtifactStream:
    """仅持有本次匿名临时文件；调用方发送完成、拒绝或断连都必须close。"""

    def __init__(self, stream, length, check_visible):
        self._stream = stream
        self.length = length
        self.check_visible = check_visible

    def chunks(self):
        self._stream.seek(0)
        while chunk := self._stream.read(CHUNK_BYTES):
            yield chunk

    def close(self):
        self._stream.close()


def verified_workflow_stream(owner_home, parts, record, *, start, end, check_visible):
    """同一源FD完整hash并拷贝所需区间；发送只读私有副本，不会泄漏校验后变更的源字节。"""
    check_visible()
    if (record.path != str(owner_home.joinpath(*parts)) or record.status != 'ready'
            or not re.fullmatch(r'[0-9a-f]{64}', record.sha256) or record.size_bytes < 0):
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    if record.size_bytes > WORKFLOW_DOWNLOAD_MAX_BYTES:
        raise HostAPIError('PAYLOAD_TOO_LARGE', 413,
            message='WORKFLOW_ARTIFACT_SIZE_LIMIT；正式归档超过64MiB，未发送内容；重新发布符合上限的产物，不得原样重试。')
    # TemporaryFile创建0600私有文件并尽早unlink；不使用可由请求指定的缓存路径。
    stream = tempfile.TemporaryFile(mode='w+b')  # noqa: SIM115 — 成功时所有权交给发送器；失败在下方关闭。
    try:
        descriptor = open_readonly_file_beneath(owner_home, parts)
        try:
            before, digest, size, selected = os.fstat(descriptor), hashlib.sha256(), 0, 0
            if before.st_size != record.size_bytes:
                raise HostAPIError('ARTIFACT_CHANGED', 409)
            while chunk := os.read(descriptor, CHUNK_BYTES):
                if size + len(chunk) > record.size_bytes:
                    raise HostAPIError('ARTIFACT_CHANGED', 409)
                digest.update(chunk)
                left, right = max(0, start - size), min(len(chunk), end + 1 - size)
                if left < right:
                    stream.write(chunk[left:right])
                    selected += right - left
                size += len(chunk)
            if (size != record.size_bytes or digest.hexdigest() != record.sha256
                    or _fingerprint(before) != _fingerprint(os.fstat(descriptor))):
                raise HostAPIError('ARTIFACT_CHANGED', 409)
        finally:
            os.close(descriptor)
        stream.flush()
        check_visible()
        stream.seek(0)
        return VerifiedArtifactStream(stream, selected, check_visible)
    except BaseException:
        stream.close()
        raise
