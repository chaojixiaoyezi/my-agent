# LLM: Standard-stream resources only; no RPC, command execution, authority or ledger lives here.
# 模块用途: Prepare W-owned stdio copies for native FD transport and decode C's received resources.
from __future__ import annotations

import io
import locale
import os
import subprocess
import sys
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import BinaryIO, TextIO

from ..common.executor_registration import ExecutorProcessOptions, ExecutorTextIO


# LLM: fds belong to the preparation context; successful PIPE parent streams belong to its caller.
# 类用途: Expose the fixed wire, transport descriptors and optional W-side PIPE parent streams.
@dataclass(frozen=True)
class NativeExecutorIO:
    wire: list[str | int]
    fds: tuple[int, ...]
    stdin: BinaryIO | TextIO | None
    stdout: BinaryIO | TextIO | None
    stderr: BinaryIO | TextIO | None


# LLM: Only explicitly owned descriptors are registered for cleanup; raw caller originals are never consumed.
# 函数用途: Prepare standard streams, close transport ends on success, and all owned resources on failure.
@contextmanager
def prepare_native_executor_io(options: ExecutorProcessOptions):
    """Yield transport resources; caller must close surviving PIPE parents after successful exit.

    The caller sends ``wire`` and ``fds`` together while inside this context. This
    component never sends them. Normal exit closes only the sending child ends
    and duplicated explicit/inherited sources. Any exception (including one
    thrown by the caller after sending) additionally closes all PIPE parents.
    """
    if not isinstance(options, ExecutorProcessOptions):
        raise ValueError("invalid_executor_io_options")
    if type(options.stdio) is not tuple or len(options.stdio) != 3:
        raise ValueError("invalid_executor_stdio")
    if options.text is not None and not isinstance(options.text, ExecutorTextIO):
        raise ValueError("invalid_executor_text_io")
    wire, fds, parents = [], [], []
    with ExitStack() as parent_cleanup, ExitStack() as child_cleanup:
        for number, source in enumerate(options.stdio):
            if type(source) is int and source == subprocess.DEVNULL:
                wire.append("devnull")
                parents.append(None)
                continue
            if type(source) is int and source == subprocess.STDOUT:
                if number != 2:
                    raise ValueError("stdout_reuse_only_valid_for_stderr")
                wire.append(wire[1])
                parents.append(None)
                continue
            if type(source) is int and source == subprocess.PIPE:
                child_fd, parent = _pipe(number, options.text, child_cleanup, parent_cleanup)
            else:
                descriptor = number if source is None else _source_fd(source)
                child_fd = os.dup(descriptor)
                child_cleanup.callback(os.close, child_fd)
                parent = None
            wire.append(len(fds))
            fds.append(child_fd)
            parents.append(parent)
        yield NativeExecutorIO(wire, tuple(fds), *parents)
        # Closing children is still part of successful preparation. If it fails,
        # parent_cleanup retains its callbacks and closes every generated stream.
        child_cleanup.close()
        parent_cleanup.pop_all()


# LLM: Reject bool and arbitrary negative values; file sources must provide an actual nonnegative integer FD.
# 函数用途: Resolve an explicit integer/file-object source without closing or changing the original.
def _source_fd(source) -> int:
    if type(source) is int:
        descriptor = source
    else:
        fileno = getattr(source, "fileno", None)
        if not callable(fileno):
            raise ValueError("invalid_executor_fd_source")
        descriptor = fileno()
    if type(descriptor) is not int or descriptor < 0:
        raise ValueError("invalid_executor_fd_source")
    return descriptor


# LLM: Register raw parent ownership before constructing buffered/text wrappers so every failure can roll back.
# 函数用途: Build a real pipe, retain its child end for transport and wrap its W-side parent like Popen.
def _pipe(number: int, text: ExecutorTextIO | None, children: ExitStack, parents: ExitStack):
    read_fd, write_fd = os.pipe()
    child_fd, parent_fd = (read_fd, write_fd) if number == 0 else (write_fd, read_fd)
    children.callback(os.close, child_fd)
    with ExitStack() as raw_guard:
        raw_guard.callback(os.close, parent_fd)
        buffering = -1 if text is None or text.bufsize == 1 else text.bufsize
        raw = parents.enter_context(open(parent_fd, "wb" if number == 0 else "rb", buffering=buffering))
        raw_guard.pop_all()  # The binary stream now owns parent_fd.
    if text is None:
        return child_fd, raw
    # Popen resolves None encoding through UTF8 mode or locale.getencoding, not
    # its locale-dependent environment as a new source of execution authority.
    encoding = text.encoding
    if encoding is None:
        encoding = "utf-8" if sys.flags.utf8_mode else locale.getencoding()
    if number == 0:
        parent = io.TextIOWrapper(raw, encoding=encoding, errors=text.errors,
                                  write_through=True, line_buffering=text.bufsize == 1)
    else:
        parent = io.TextIOWrapper(raw, encoding=encoding, errors=text.errors)
    parents.enter_context(parent)
    return child_fd, parent


# LLM: Wire is exactly three JSON scalars, every received position must be used, and decode never closes a copy.
# 函数用途: Map strict FD indices/DEVNULL into the three standard-stream arguments for existing ExecutorIO.
def decode_native_executor_io(wire, received_fds) -> tuple[int, int, int]:
    """Return stdin/stdout/stderr parameters; received-FD cleanup remains with C.

    An index may be reused (notably stderr=STDOUT). The index union must equal
    all received positions; extra or idle resources fail closed before use.
    No descriptor is opened, duplicated, or closed by this decoder.
    """
    if type(wire) is not list or len(wire) != 3:
        raise ValueError("invalid_executor_stdio_wire")
    if type(received_fds) is not tuple or len(received_fds) > 3:
        raise ValueError("invalid_executor_received_fds")
    if any(type(fd) is not int or fd < 0 for fd in received_fds):
        raise ValueError("invalid_executor_received_fds")
    if len(set(received_fds)) != len(received_fds):
        raise ValueError("duplicate_executor_received_fds")
    indices, stdio = set(), []
    for item in wire:
        if type(item) is str and item == "devnull":
            stdio.append(subprocess.DEVNULL)
        elif type(item) is int and 0 <= item < len(received_fds):
            indices.add(item)
            stdio.append(received_fds[item])
        else:
            raise ValueError("invalid_executor_stdio_wire")
    if indices != set(range(len(received_fds))):
        raise ValueError("unused_executor_received_fds")
    return tuple(stdio)
