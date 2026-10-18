# LLM: 独立执行资源端点；同包kernel sender和原T/P3/C门唯一授信，命令仅经匿名FD，不放宽旧无FD责任端点。
# 模块用途: 由原C实际预留、创建、登记、放行执行者，回执只报告事实，不授予W写权限。
import json
import os
import re
import uuid
from pathlib import Path

from ..user_space.owner_resolver import OwnerIdentity
from .io import read_json_file_report
from .multiprocess_commit_session import _request_identity
from .multiprocess_executor_original_source import OriginalCExecutorSource
from .multiprocess_executor_runtime import _reservation
from .multiprocess_executor_start import ExecutorStartObserver, start_executor
from .multiprocess_kernel_message import KernelMessageSender
from .multiprocess_native_commit_session import NativeCommitSession, _decode_packet
from .multiprocess_route import owner_digest, validate_worker_route
from .multiprocess_worker_commit import WorkerCommitRejected
from .request_binding import GatewayActiveTurnTransition


# LLM: 仅受信C组合根创建；processes只是实际Popen/原source/原P1引用，不是第二业务状态机、许可或准入名额。
# 类用途: 为原W请求提供独立的原生执行及精确责任结束入口。
class NativeExecutorSession(NativeCommitSession):
    # LLM: 原端点/issuer不变，Linux C内域及handle引用保物理责任、wire无domain/callback字段；同步executor/P5/domain tests。
    # 函数用途: 固定原C来源并初始化实际执行者引用表。
    def __init__(self, issuer, receiver, sender, observer=None):
        super().__init__(issuer, receiver, sender)
        self._observer = observer if observer is not None else ExecutorStartObserver()
        self._processes = {}
        self._hosts = {}
        self._execution_domains = []
        self._domain_handles = {}

    # LLM: 等待只持端点串行门，不持T/C生命周期门；同次FD/sender认证后交原helper，轮询不缩短上下文/回执预算；同步executor协议测试。
    # 函数用途: 消费一条实际执行或无FD结束消息，回发事实回执；失败不重发、不自动结清pin。
    def serve_once(self, *, timeout=5, receive_timeout=None):
        self._require_original()
        with self._lock:
            if self._closed:
                raise WorkerCommitRejected('native_session_closed')
            issuer, receiver, sender = self._sources
            payload, peer_pid, fds = receiver.receive_fds(timeout=timeout if receive_timeout is None else receive_timeout)
            try:
                issuer._require_worker(peer_pid)
                request = _decode_packet(payload)
                result = self._consume_executor_request(request, fds, peer_pid, timeout)
                # 发送失败保留原责任及真实进程引用，不能假称W收到，更不能再执行一次命令。
                KernelMessageSender.send(sender, json.dumps(result, separators=(',', ':')).encode(), timeout=timeout)
                return result
            finally:
                for fd in fds:
                    os.close(fd)

    # LLM: 原serve同次核W并持原门才调用；helper保原seq/FD拒绝/finish/start路径，任何回执失败不重放。
    # 函数用途: 拆平已认证执行资源消息，原C仍是唯一物理执行者。
    def _consume_executor_request(self, request, fds, peer_pid, timeout):
        seq, op = _executor_request_identity(request, self._last_seq)
        if op == 'finish':
            if fds:
                raise WorkerCommitRejected('native_control_fds_unexpected')
            handle = request['handle']
            if type(handle) is not int or handle not in self._handles:
                raise WorkerCommitRejected('original_commit_handle_required')
            self._last_seq = seq
            pin_issuer, pin = self._handles[handle]
            pin_issuer._require_worker(peer_pid)
            pin_issuer.finish(pin)
            del self._handles[handle]
            result = {'op': 'finished', 'seq': seq, 'handle': handle}
        elif op in {'executor_poll', 'executor_signal'}:
            from .multiprocess_native_executor_process import handle_process_request

            if fds:
                raise WorkerCommitRejected('native_control_fds_unexpected')
            self._last_seq = seq
            result = handle_process_request(self, request)
        else:
            command = _read_executor_command(request, fds, timeout)
            self._last_seq = seq
            result = self._start(request, command, peer_pid)
        return result

    # LLM: 原canonical/T/P3选owner、真实C短门内创建；Linux非设施命令用同Popen/P1 v2域，cap失败无fallback；帧/ready等待门外，同步P5/原源 tests。
    # 函数用途: 保存原pin早于实际执行，走现有两阶段启动，留下真实P1和进程供后续管理。
    def _start(self, request, command, peer_pid):
        issuer = self._sources[0]
        path = issuer.control.paths.processing / (request['request_id'] + '.json')
        transition = GatewayActiveTurnTransition(path, request['request_id'], request['execution_attempt_id'])

        # LLM: 仅选择owner，后续原source的实际短门仍重新核原P3与route；此读回不是写许可。
        # 函数用途: 在原请求T里取受信owner三元组。
        def select_owner():
            payload = read_json_file_report(path, context='native.executor.owner').payload
            checked = validate_worker_route(payload, issuer._source_origin[0].partition)
            if not checked['ok']:
                raise WorkerCommitRejected('original_native_admission_required')
            epoch = payload.get('lease_epoch')
            if type(epoch) is not int or epoch < 1:
                raise WorkerCommitRejected('original_executor_request_required')
            return OwnerIdentity(*checked['owner']), epoch

        owner, epoch = transition('executor_commit', select_owner)
        source = OriginalCExecutorSource(issuer, owner, transition=transition)

        # LLM: 原pin创立与记住在同一T/C段，先于所有P1/物理执行；异常保留原关联而不是自动撤销。
        # 函数用途: 让失败或迟到回执仍可精确finish。
        def remember():
            pin = issuer.begin(owner, peer_pid=peer_pid)
            self._handles[request['seq']] = (issuer, pin)

        source._commit(remember)
        binding = {'owner_sha': owner_digest(owner), 'request_id': transition.request_id,
                   'execution_attempt_id': transition.execution_attempt_id,
                   'lease_epoch': epoch, 'tool_call_id': request['tool_call_id']}
        row = _reservation(source, binding, request['kind'], 'exec-' + uuid.uuid4().hex)
        if request['op'] == 'executor_delegate_start':
            from .multiprocess_native_host import start_native_host
            launched = start_native_host(self, source, row, command)
        else:
            from .multiprocess_native_linux_domain import prepare_native_domain
            command, row = prepare_native_domain(command, row, source, self._execution_domains)
            if command.native_domain is not None:
                self._domain_handles[request['seq']] = command.native_domain
                command.native_domain.retain_lifetime()
            try:
                launched = start_executor(source, row, command, self._observer)
            finally:
                if command.native_domain is not None:
                    command.native_domain.observe()
        if launched is None or not launched.ok:
            outcome = launched.error if launched is not None else {'ok': False, 'error_code': 'EXECUTOR_CONTEXT_REQUIRED'}
        else:
            token = launched.reservation['reservation_id']
            self._processes[token] = (launched.process, source, launched.reservation)
            outcome = {'ok': True, 'process_token': token, 'pid': launched.process.pid}
        return {'op': 'applied', 'seq': request['seq'], 'handle': request['seq'], 'result': outcome}


    # LLM: 未结domain pin须先等原内核barrier；原消息pin仍独立精确finish，close不signal/放弃活域，也不关闭原端点掩盖未知。
    # 函数用途: 只在原物理域已结清后关闭资源session。
    def close(self):
        self._require_original()
        for domain in self._execution_domains:
            domain.observe()
            if not domain.retire_lifetime():
                raise WorkerCommitRejected('original_native_executor_domain_pending')
        super().close()


# LLM: 信封仅定位原请求，序号防本session重复消费；路径/owner/命令/裸FD号/任意callback均不接受。
# 函数用途: 严格校验固定执行或finish消息，不由字段声称身份。
def _executor_request_identity(request, previous):
    if request.get('op') == 'finish':
        return _request_identity(request, previous)
    if request.get('op') in {'executor_poll', 'executor_signal'}:
        from .multiprocess_native_executor_process import require_process_request

        return require_process_request(request, previous)
    identifier, attempt, seq = request.get('request_id'), request.get('execution_attempt_id'), request.get('seq')
    if (set(request) != {'op', 'seq', 'request_id', 'execution_attempt_id', 'tool_call_id', 'kind', 'stdio', 'text'}
            or request.get('op') not in {'executor_start', 'executor_delegate_start'} or type(seq) is not int or seq <= previous
            or type(identifier) is not str or not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,255}', identifier)
            or type(attempt) is not str or not attempt or len(attempt) > 512
            or type(request.get('tool_call_id')) is not str or not request['tool_call_id']
            or len(request['tool_call_id']) > 512 or type(request.get('kind')) is not str):
        raise WorkerCommitRejected('commit_request_invalid')
    if request['op'] == 'executor_delegate_start' and request['kind'] != 'background_host':
        raise WorkerCommitRejected('commit_request_invalid')
    return seq, request['op']


# LLM: 仅匿名只读管道运输上下文，所有等待都在原T/C短门外；不打开wire路径、不落盘argv/env。
# 函数用途: 有界读取完整JSON后使用标准流资源组件，形成原启动器命令。
def _read_executor_command(request, fds, timeout):
    import fcntl
    import selectors
    import stat
    import time

    from ..common.executor_registration import ExecutorTextIO
    from .multiprocess_commit_session import _unique_object
    from .multiprocess_executor_start import ExecutorCommand, ExecutorIO, _valid_text_policy
    from .multiprocess_native_executor_io import decode_native_executor_io

    if not 1 <= len(fds) <= 4:
        raise WorkerCommitRejected('native_executor_context_fd_required')
    descriptor = fds[0]
    if (not stat.S_ISFIFO(os.fstat(descriptor).st_mode)
            or fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY):
        raise WorkerCommitRejected('native_executor_context_fd_invalid')
    os.set_blocking(descriptor, False)
    deadline, chunks, size, maximum = time.monotonic() + timeout, [], 0, 16 * 1024 * 1024
    with selectors.DefaultSelector() as selector:
        selector.register(descriptor, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise WorkerCommitRejected('native_executor_context_timeout')
            try:
                chunk = os.read(descriptor, min(65536, maximum + 1 - size))
            except BlockingIOError:
                continue
            if not chunk:
                break
            size += len(chunk)
            if size > maximum:
                raise WorkerCommitRejected('native_executor_context_size_invalid')
            chunks.append(chunk)
    try:
        context = json.loads(b''.join(chunks).decode('utf-8'), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise WorkerCommitRejected('native_executor_context_invalid') from error
    if (type(context) is not dict or set(context) != {'version', 'argv', 'cwd', 'env'}
            or type(context['version']) is not int or context['version'] != 1
            or type(context['argv']) is not list or not context['argv']
            or any(type(value) is not str or '\x00' in value for value in context['argv'])
            or not context['argv'][0] or type(context['cwd']) is not str
            or '\x00' in context['cwd'] or not Path(context['cwd']).is_absolute()
            or type(context['env']) is not dict
            or any(type(key) is not str or not key or '=' in key or '\x00' in key
                   or type(value) is not str or '\x00' in value for key, value in context['env'].items())):
        raise WorkerCommitRejected('native_executor_context_invalid')
    text = request['text']
    if text is not None:
        if type(text) is not dict or set(text) != {'encoding', 'errors', 'bufsize'}:
            raise WorkerCommitRejected('native_executor_text_invalid')
        text = ExecutorTextIO(**text)
        if not _valid_text_policy(text):
            raise WorkerCommitRejected('native_executor_text_invalid')
    try:
        stdio = decode_native_executor_io(request['stdio'], fds[1:])
    except ValueError as error:
        raise WorkerCommitRejected('native_executor_stdio_invalid') from error
    return ExecutorCommand(tuple(context['argv']), Path(context['cwd']), context['env'], ExecutorIO(stdio, text=text))
