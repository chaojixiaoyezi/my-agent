# LLM: This wrapper captures the original C source; no wire payload or bare ledger reconstructs write authority.
# 模块用途: Recheck original C/W/T/P3 and independent actual-host identity at each background command effect.
import os

from .io import read_json_file_report
from .multiprocess_native_host_authority import host_admission
from .multiprocess_records import record_digest
from .multiprocess_worker_commit import WorkerCommitRejected


# LLM: Only the C source wrapper is used; each P1/spawn/R callback rechecks the same host inside the original short gate.
# 类用途: Retain original C/T/P3 and add a separate actual-host guard for command creation.
class _HostSource:
    # LLM: No source/creator is reconstructed from wire; wrapper exists only in original C.
    # 函数用途: Capture original host session and immutable C source.
    def __init__(self, session):
        self.session = session
        self._session_origin = session
        self.original = session.host.source
        self.root, self.partition = self.original.root, self.original.partition
        self._origin, self._creator = self, os.getpid()

    # LLM: 原C或W source实际短门内复核固定P3/epoch和实际host；角色由原对象不由wire，closing读身份不恢复写权，同步C/W source tests。
    # 函数用途: Reject copy/fork/rebinding and host identity or original request drift.
    def guard(self):
        if (self._origin is not self or self._creator != os.getpid() or self.session is not self._session_origin
                or self.original is not self.session.host.source
                or self.root != self.original.root or self.partition != self.original.partition):
            raise WorkerCommitRejected('original_native_host_source_required')
        self.session._require_original()
        host = self.session.host
        if host._retired:
            raise WorkerCommitRejected('original_native_host_lane_retired')
        if not host._connected:
            raise WorkerCommitRejected('original_native_host_not_connected')
        self.session._require_host(host.process.pid)
        source = self.original
        p3 = host_admission(source, host.row)
        payload = read_json_file_report(source._transition.request_path, context='native.host.epoch').payload
        if record_digest(p3) != self.session.host.p3_digest or payload.get('lease_epoch') != self.session.host.row['lease_epoch']:
            raise WorkerCommitRejected('original_native_host_binding_required')

    # LLM: Row locators are not authority; exact original request/attempt/epoch/tool binding is immutable.
    # 函数用途: Validate a command row before touching P1.
    def bound(self, row):
        original = self.session.host.row
        if any(row[key] != original[key] for key in ('owner_sha','request_id','execution_attempt_id','lease_epoch','tool_call_id')) or row['kind'] != 'background_command':
            raise WorkerCommitRejected('original_native_host_binding_required')

    # LLM: Reserve uses original issuer and host guard in the actual C gate.
    # 函数用途: Persist a new command reservation through original P1.
    def reserve(self, row):
        self.bound(row)
        return self.original.reserve(row, source_guard=self.guard)

    # LLM: No bare-ledger fallback or new writer exists.
    # 函数用途: Advance the original command P1 with actual-host validation.
    def advance(self, row, state, registration=None):
        self.bound(row)
        return self.original.advance(row, state, registration, source_guard=self.guard)

    # LLM: Reads remain available during closing; no new write authority.
    # 函数用途: Read original canonical P1.
    def read(self, identifier):
        return self.original.read(identifier)

    # LLM: Failed cleanup does not bypass original active T/C requirements.
    # 函数用途: Attempt original abandonment without losing unknown facts.
    def abandon(self, row):
        return self.advance(row, 'abandoned')

    # LLM: Physical spawn/final R occurs in original C with host recheck, not once at ACK.
    # 函数用途: Wrap each actual short effect with both original W-source and host-sender gates.
    def perform_short_effect(self, row, phase, callback):
        self.bound(row)
        return self.original.perform_short_effect(row, phase, callback, source_guard=self.guard)
