"""正文页的有界完整性证明；不拥有资产登记、授权或正文副本。"""
from __future__ import annotations

import codecs
import hashlib
import os
from dataclasses import dataclass, field

from ..common.nofollow_fs import open_readonly_file_beneath
from ..plugin_host.artifacts import _fingerprint
from ..plugin_host.errors import HostAPIError

# 业务资产分页哈希校验的固定分块字节大小（64 KiB）。
BLOCK_BYTES = 65536
# 单版业务资产允许核验并按页读取的最大总字节数（64 MiB）。
MAX_CONTENT_BYTES = 64 * 1024 * 1024
_PROOF_SEAL = object()


@dataclass(frozen=True, slots=True)
class VerifiedContentPages:
    sha256: str
    size_bytes: int
    blocks: tuple[bytes, ...]
    _seal: object = field(repr=False, compare=False)
    registry_proof: object | None = field(default=None, repr=False, compare=False)


def prepare_content_pages(chunks, *, sha256, size_bytes, prefix_bytes, registry_proof=None):
    """首读复用原已校验运输体，并把整版本SHA固定成有界分块摘要，不保留正文。"""
    if not 0 <= size_bytes <= MAX_CONTENT_BYTES:
        raise HostAPIError('PAYLOAD_TOO_LARGE', 413)
    digest, decoder, blocks, pending, prefix = hashlib.sha256(), codecs.getincrementaldecoder('utf-8')(), [], bytearray(), bytearray()
    size = 0
    for chunk in chunks:
        if not isinstance(chunk, bytes) or len(chunk) > BLOCK_BYTES or size + len(chunk) > size_bytes:
            raise HostAPIError('ARTIFACT_CHANGED', 409)
        text = decoder.decode(chunk)
        if '\x00' in text:
            raise UnicodeError('binary NUL in UTF-8 content')
        size += len(chunk)
        digest.update(chunk)
        prefix.extend(chunk[:max(0, prefix_bytes - len(prefix))])
        pending.extend(chunk)
        while len(pending) >= BLOCK_BYTES:
            blocks.append(hashlib.sha256(pending[:BLOCK_BYTES]).digest())
            del pending[:BLOCK_BYTES]
    decoder.decode(b'', final=True)
    if size != size_bytes or digest.hexdigest() != sha256:
        raise HostAPIError('ARTIFACT_CHANGED', 409)
    if pending:
        blocks.append(hashlib.sha256(pending).digest())
    return VerifiedContentPages(sha256, size_bytes, tuple(blocks), _PROOF_SEAL, registry_proof), bytes(prefix)


def read_verified_content_page(owner_home, parts, record, proof, *, start, end):
    """每页校验真实FD覆盖的完整块；同tick同stat改字节也不能返回篡改内容。"""
    if (not isinstance(proof, VerifiedContentPages) or proof._seal is not _PROOF_SEAL
            or proof.sha256 != record.sha256 or proof.size_bytes != record.size_bytes
            or not 0 <= proof.size_bytes <= MAX_CONTENT_BYTES
            or len(proof.blocks) != (proof.size_bytes + BLOCK_BYTES - 1) // BLOCK_BYTES
            or record.path != str(owner_home.joinpath(*parts)) or record.status != 'ready'):
        raise HostAPIError('ARTIFACT_CHANGED', 409)
    fd = open_readonly_file_beneath(owner_home, parts)
    try:
        before = os.fstat(fd)
        if before.st_size != proof.size_bytes:
            raise HostAPIError('ARTIFACT_CHANGED', 409)
        selected = []
        if end >= start:
            for index in range(start // BLOCK_BYTES, end // BLOCK_BYTES + 1):
                offset = index * BLOCK_BYTES
                expected = min(BLOCK_BYTES, proof.size_bytes - offset)
                os.lseek(fd, offset, os.SEEK_SET)
                block = bytearray()
                while len(block) < expected:
                    chunk = os.read(fd, expected - len(block))
                    if not chunk:
                        break
                    block.extend(chunk)
                if len(block) != expected or hashlib.sha256(block).digest() != proof.blocks[index]:
                    raise HostAPIError('ARTIFACT_CHANGED', 409)
                selected.append(bytes(block[max(0, start - offset):min(expected, end + 1 - offset)]))
        if _fingerprint(before) != _fingerprint(os.fstat(fd)):
            raise HostAPIError('ARTIFACT_CHANGED', 409)
        return b''.join(selected)
    finally:
        os.close(fd)
