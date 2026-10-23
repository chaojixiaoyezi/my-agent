# LLM: Read only the suffix needed for the newest nonblank physical-LF record; never search other owners or cache file contents.
# 模块用途: 为JSONL时间线共享反向有界尾行读取，不解释JSON或事件签名。
from __future__ import annotations

from pathlib import Path

# 尾部行反向分块读取时单次读取的块字节数。
_BLOCK_BYTES = 8192


# LLM: Assemble complete binary LF lines before strict UTF-8 decoding; IO/decoding failures remain the caller's responsibility.
# 函数用途: 从给定文件末尾按8KiB读取最后一条非空白物理行。
def read_last_nonblank_line(path: Path) -> str | None:
    fragments: list[bytes] = []
    with path.open("rb", buffering=0) as stream:
        position = stream.seek(0, 2)
        while position > 0:
            size = min(_BLOCK_BYTES, position)
            position -= size
            stream.seek(position)
            parts = stream.read(size).split(b"\n")
            # The first fragment may start inside a UTF-8 codepoint; defer decoding it.
            for fragment in reversed(parts[1:]):
                fragments.append(fragment)
                line = b"".join(reversed(fragments)).decode("utf-8")
                fragments.clear()
                if line.strip():
                    return line.removesuffix("\r")
            fragments.append(parts[0])
    line = b"".join(reversed(fragments)).decode("utf-8")
    return line.removesuffix("\r") if line.strip() else None
