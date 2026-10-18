# LLM: 只消费原C管理source与同owner服务；本栈固定preview双T/JSON try/fullraw CAS，不发行权限、不接任意path/state/callback或修复未知。
# 模块用途: 为当前真人候选确认提供单次预览消费和实际结果写回，普通Agent保原预览事务。
import os
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace

from ..common.json_io import (
    locked_json_path,
    serialize_private_json_no_newline,
    write_private_json_file_atomic_no_newline_unlocked,
)
from .control_operation_service import (
    gateway_control_operation_path,
    try_gateway_control_operation_transition,
)


# LLM: 仅固定事务拒绝的原业务码，不是来源失活或可重试许可；未知IO/native仍原对象传播。
# 类用途: 将同nonce自锁、争用与绑定拒绝交回原控制结果。
class MemoryPreviewTransactionRejected(ValueError):
    # LLM: code只作原控制展示，不能用于恢复来源或决定写权。
    # 函数用途: 保存本次固定预览拒绝码。
    def __init__(self, code):
        super().__init__(code)
        self.code = code


# LLM: 实例仅本次preview T持有期间有效，原source/服务和完整raw同对象冻结；关闭/复制/换线程不得续用，退出不补写。
# 类用途: 固定取消、消费和真实晋升结果的预览CAS，不能作为通用writer。
class _MemoryPreviewTransaction:
    # LLM: nonce/paths/command/scope只从已绑定原source导出；有界no-follow/原codec与完整身份读取共用唯一叶，不建第二预览记录。
    # 函数用途: 在已取得原preview T后捕获本次真实原件。
    def __init__(self, source, services, thread_id):
        from .memory_candidate_review import _bound_preview, _read_preview_raw

        self.source, self.services, self.thread_id = source, services, thread_id
        self.binding = source._management_memory_binding
        self.scope, self.command = self.binding.scope, source.http_origin[3]
        self.nonce = self.command.target_id
        self.pid, self.thread, self.origin, self.active = os.getpid(), threading.current_thread(), self, True
        self._receipt, self._expected_raw = _read_preview_raw(source.paths, self.nonce)
        try:
            self._details = dict(_bound_preview(self._receipt, services, self.scope, thread_id))
            owner = source.receipt_owner
            if ((self._receipt.owner_provider, self._receipt.owner_kind, self._receipt.owner_id)
                    != (owner.provider, owner.owner_kind, owner.owner_id)
                    or self._receipt.conversation_thread_id != thread_id):
                raise ValueError('preview original owner or thread changed')
        except ValueError as error:
            raise MemoryPreviewTransactionRejected('MEMORY_PREVIEW_BINDING_MISMATCH') from error

    # LLM: 原事务本身的身份只读核；来源检查仍交原source producer，不运行旧Context或恢复权限。
    # 函数用途: 拒绝已经离开preview T或转移执行线程的端口使用。
    def _require_original(self):
        if (self.origin is not self or not self.active or self.pid != os.getpid()
                or self.thread is not threading.current_thread()
                or self.source._management_memory_binding is not self.binding):
            raise ValueError('original memory preview transaction is unavailable')
        self.source._require_current()

    # LLM: 仅接原服务/paths/command/bound scope/thread精确调用，不以相同字段的替身复用本次固定端口。
    # 函数用途: 在共享算法进入C分支时核对本事务的原调用绑定。
    def require_binding(self, services, paths, command, scope, thread_id):
        self._require_original()
        if (services is not self.services or paths is not self.source.paths or command is not self.command
                or scope is not self.scope or thread_id != self.thread_id
                or services.owner_id != self.binding.home.owner_id):
            raise ValueError('original memory preview binding changed')

    # LLM: 仅返回原已读receipt，不读写另一份投影，不授予重放。
    # 函数用途: 给算法投影当前预览或已知结果。
    @property
    def receipt(self):
        return self._receipt

    # LLM: 返回副本防止业务算法更改本事务的原expected事实；固定写者仍按完整raw复核。
    # 函数用途: 给原候选算法提供本次预览详细事实。
    @property
    def details(self):
        return deepcopy(self._details)

    # LLM: payload门外按原结果builder预备；Root中仅JSON try/no-follow有界重读/fullraw CAS/原once writer，原deadline原值不变。
    # 函数用途: 原子替换这个固定nonce的业务结果，失败不更新本栈expected或补偿已发生事实。
    def _write(self, details, *, error=''):
        from .memory_candidate_review import (
            _bound_preview,
            _preview_receipt_result,
            _read_preview_raw,
        )

        self._require_original()
        result = _preview_receipt_result(details, error=error)
        updated = replace(self._receipt, result=result.to_dict(), updated_at=time.time())
        payload = updated.to_dict()
        next_raw = serialize_private_json_no_newline(payload).encode('utf-8')
        path = gateway_control_operation_path(self.source.paths, self.nonce)

        # LLM: 无外部路径或writer参数；JSON锁出生已在同source末门，只try，普通CAS/IO失败不得写首native槽。
        # 函数用途: 在原Root/P2窗口提交固定预览的一次替换。
        def commit():
            with locked_json_path(path, blocking=False):
                current, raw = _read_preview_raw(self.source.paths, self.nonce)
                _bound_preview(current, self.services, self.scope, self.thread_id)
                if raw != self._expected_raw:
                    raise ValueError('original memory preview raw changed')
                write_private_json_file_atomic_no_newline_unlocked(
                    path, payload, deadline=self.source.deadline, replace_once=True
                )

        self.source.perform(commit)
        self._receipt, self._expected_raw, self._details = updated, next_raw, dict(details)
        return result

    # LLM: 取消仅原cancel命令且仍preview，完全不查Candidate或做review；终态投影由算法在调用前分流。
    # 函数用途: 取消本次预览而保留原候选状态。
    def cancel(self):
        if self.command.operation != 'cancel' or self._details['state'] != 'preview':
            raise ValueError('original memory cancel is unavailable')
        return self._write(self._details | {'state': 'cancelled'})

    # LLM: 只有confirm/reject完整候选快照相等才消费；该写成功之前不能review/promote，state永不改transport。
    # 函数用途: 耐久记录本次精确确认已消费且结果尚未核实。
    def consume(self, candidate):
        from .memory_candidate_review import candidate_display, candidate_version

        value, error = candidate_display(candidate)
        from hashlib import sha256
        if (self.command.operation not in {'confirm', 'reject'} or self._details['state'] != 'preview'
                or candidate.candidate_id != self._details['candidate_id']
                or candidate_version(candidate) != self._details['version'] or error
                or value != self._details.get('value')
                or sha256(candidate.content.encode('utf-8')).hexdigest() != self._details['content_sha256']):
            raise ValueError('original memory candidate changed')
        return self._write(self._details | {'state': 'consumed_unknown',
            'consumed_request_id': str(self.scope.metadata['message_id'])}, error='MEMORY_CONTROL_RESULT_UNKNOWN')

    # LLM: 只接受本次原Candidate/MemoryPromotionResult结构事实；不能传state任意签完成，源/目标CAS仍各自原末门。
    # 函数用途: 把已审核或已晋升的真实结果写回已消费预览。
    def finish(self, reviewed, promotion=None):
        from ..memory_store.candidate_models import MemoryCandidate
        from ..memory_store.promotion import MemoryPromotionResult

        if (type(reviewed) is not MemoryCandidate or reviewed.candidate_id != self._details['candidate_id']
                or self._details['state'] != 'consumed_unknown'):
            raise ValueError('original memory review result is unavailable')
        if self.command.operation == 'reject' and promotion is None and reviewed.status == 'rejected':
            return self._write(self._details | {'state': 'rejected'})
        if (self.command.operation != 'confirm' or type(promotion) is not MemoryPromotionResult
                or promotion.candidate_id != reviewed.candidate_id):
            raise ValueError('original memory promotion result is unavailable')
        details = self._details | {'state': 'completed' if promotion.promoted else 'not_committed',
            'promotion': {key: getattr(promotion, key) for key in
                ('candidate_id', 'promoted', 'status', 'reason_code', 'formal_memory_changed')}}
        return self._write(details, error='' if promotion.promoted else promotion.reason_code)


# LLM: 原当前confirm T外只try另一个preview T一次；来源来自真实管理source，self-nonce先拒，不回调旧resolver自锁。
# 函数用途: 让C算法在原双T锁序中借用固定预览事务，退出即关闭端口且不补账。
@contextmanager
def memory_preview_transaction(source, services, thread_id):
    from .memory_candidate_review import MemoryCandidateServices
    from .multiprocess_local_control import _LocalControlWriteSource

    if type(source) is not _LocalControlWriteSource or type(services) is not MemoryCandidateServices:
        raise ValueError('original memory preview source is unavailable')
    source._require_current()
    if (source._management_memory_binding is None or source.http_origin[3].kind != 'memory'
            or source.http_origin[3].operation not in {'confirm', 'reject', 'cancel', 'receipt'}):
        raise ValueError('original memory preview action is unavailable')
    nonce = source.http_origin[3].target_id
    if nonce == source.receipt.operation_id:
        raise MemoryPreviewTransactionRejected('MEMORY_PREVIEW_BINDING_MISMATCH')
    with try_gateway_control_operation_transition(source.paths, nonce) as acquired:
        if not acquired:
            raise MemoryPreviewTransactionRejected('MEMORY_PREVIEW_BUSY')
        transaction = _MemoryPreviewTransaction(source, services, thread_id)
        try:
            yield transaction
        finally:
            transaction.active = False
