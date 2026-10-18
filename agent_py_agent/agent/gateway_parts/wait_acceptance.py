# LLM: 只加强内部C隔离接收的原队列/原回执成功边界；不产生第二回执、路由、owner资源或默认0 I/O。
# 模块用途: 返回隔离等待202之前，确认原接收文件和目录项耐久。
from __future__ import annotations

import os
import stat
from pathlib import Path

from .input_delivery_service import gateway_input_receipt_path, read_gateway_input_receipt
from .io import gateway_turn_transition, read_json_file_report
from .response_renderer import read_gateway_terminal_envelope_report


# LLM: 从可信冻结root父目录逐级dir_fd/O_NOFOLLOW打开，不跟子目录或叶链接；同步失败原样抛出。
# 函数用途: 把一份已写好的控制记录和到根父目录的目录项同步到磁盘。
def _sync_existing_control_file(root: Path, path: Path) -> None:
    relative = path.relative_to(root)
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("invalid control record path")
    directories = []
    file_fd = None
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        directories.append(os.open(root.parent, flags))
        directories.append(os.open(root.name, flags, dir_fd=directories[-1]))
        for part in relative.parts[:-1]:
            directories.append(os.open(part, flags, dir_fd=directories[-1]))
        file_fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directories[-1])
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise OSError("control record is not a regular file")
        os.fsync(file_fd)
        for directory_fd in reversed(directories):
            os.fsync(directory_fd)
    finally:
        if file_fd is not None:
            os.close(file_fd)
        for directory_fd in reversed(directories):
            os.close(directory_fd)


# LLM: 在调用方原T内验证原JSON/身份，再确认耐久；规范终态优先，坏文件不能靠exists/fsync冒充接收。
# 函数用途: 找可恢复的同请求控制文件，允许原认领移走pending但拒绝损坏或错身份记录。
def _accepted_request_path(paths, request_id: str, receipt=None) -> Path:
    for folder in (paths.terminal, paths.inbox, paths.processing, paths.done, paths.failed):
        path = folder / f"{request_id}.json"
        if path.exists():
            report = (read_gateway_terminal_envelope_report(path, request_id=request_id)
                      if folder == paths.terminal else read_json_file_report(path))
            payload = report.payload
            if report.load_error or report.lock_busy or not payload or payload.get("id") != request_id:
                raise OSError("accepted request control record is not recoverable")
            if receipt is not None and folder != paths.terminal:
                if any(payload.get(key) != receipt.prepared_request.get(key)
                       for key in ("kind", "user_id", "metadata")):
                    raise OSError("accepted request control record identity does not match")
            return path
    raise FileNotFoundError(f"accepted request control record is missing: {request_id}")


# LLM: 原T外记录等待（写者自取T→JSON），然后另取同原T同步；不在已持T时递归加锁。
# 函数用途: 接收成功后只给仍在原pending的请求记录当前隔离原因，不覆盖认领/取消。
def _record_current_wait(paths, request_id: str, wait: dict) -> None:
    from .request_worker import _PendingGatewayRequest, _record_admission_wait

    path = paths.inbox / f"{request_id}.json"
    report = read_json_file_report(path)
    if not (report.load_error or report.lock_busy) and report.payload:
        _record_admission_wait(_PendingGatewayRequest(path, report.payload),
                               reason=str(wait.get("reason") or ""),
                               error_code=str(wait.get("error_code") or ""), paths=paths)


# LLM: 仅POST等待成功边界使用；原T内重读有效记录后同步，active_pending/terminal_unknown不重排、不读owner。
# 函数用途: 以原请求号确认可恢复的原接收耐久，损坏/错身份/同步失败时拒绝成功202。
def confirm_wait_acceptance(paths, request_id: str, wait: dict, *, receipt=None):
    if not request_id or request_id != request_id.replace("/", "").replace("\\", ""):
        raise ValueError("invalid request id")
    if receipt is None or receipt.state == "queued":
        _record_current_wait(paths, request_id, wait)
    with gateway_turn_transition(paths, request_id):
        if receipt is not None:
            receipt = read_gateway_input_receipt(paths, request_id)
            if receipt is None:
                raise FileNotFoundError("original input receipt is missing")
            _sync_existing_control_file(paths.root, gateway_input_receipt_path(paths, request_id))
        if receipt is None or receipt.state == "queued":
            _sync_existing_control_file(paths.root, _accepted_request_path(paths, request_id, receipt))
    return receipt
