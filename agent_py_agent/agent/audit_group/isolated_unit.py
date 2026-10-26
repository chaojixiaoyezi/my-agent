# LLM: 源码只经匿名stdin送给独立沙箱进程，不落caller可读文件/argv/env；无沙箱必须拒绝执行。
# 模块用途: 在调用者workspace执行冻结脚本并只接收有界JSON，不泄露stderr和源码。
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

from ..attempt.sandbox import (
    AttemptExecutionSandbox,
    AttemptSandboxSpec,
    system_read_roots_for_platform,
)
from ..common.cancellation import raise_if_cancelled
from ..plugin_sandbox import resolve_python_runtime_prefixes
from ..tooling.process_output_capture import ProcessOutputCapture
from ..workflows.script_runner import _cleanup_process, _outer_seatbelt_active
from .access_policy import AuditGroupError

_BOOTSTRAP = """import io,json,os,sys
_cfg=json.load(sys.stdin)
os.close(0)
sys.stdin=io.StringIO(json.dumps(_cfg['inputs']))
_code=compile(_cfg.pop('code'),'<audit-group-unit>','exec')
# LLM: Python审计钩子仅补充已通过就绪门的原生/外层Seatbelt，不替代内核隔离。
# 函数用途: 在源代码运行前阻止文件修改、套接字和子进程。
def _install_guard(roots):
    realpath, fspath, fsdecode, separator = os.path.realpath, os.fspath, os.fsdecode, os.sep
    read_roots=tuple(realpath(root) for root in roots)
    writes=os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND
    mutations=frozenset(('os.remove','os.rename','os.rmdir','os.mkdir','os.symlink','os.link',
        'os.truncate','os.chmod','os.chown','os.utime','os.setxattr','os.removexattr',
        'os.system','os.exec','os.spawn','os.posix_spawn','os.fork','os.forkpty','os.kill','os.killpg'))
    # LLM: 捕获不可变策略；stdout/stderr保留已有FD，不能为用户代码打开新的可写文件。
    # 函数用途: 在实际Python操作发生前拒绝危险副作用。
    def guard(event,args):
        if event in mutations or event.startswith(('socket.','subprocess.','ctypes.','mmap.')):
            raise PermissionError('audit-group operation denied')
        if event=='import' and args[0] in ('_posixsubprocess','_ctypes','ctypes'):
            raise PermissionError('audit-group native operation denied')
        if event=='open':
            path,mode,flags=args
            if (isinstance(mode,str) and any(c in mode for c in 'wax+')) or (isinstance(flags,int) and flags&writes):
                raise PermissionError('audit-group file write denied')
            if not isinstance(path,int):
                target=realpath(fsdecode(fspath(path)))
                if not any(target==root or target.startswith(root.rstrip(separator)+separator) for root in read_roots):
                    raise PermissionError('audit-group file read denied')
    sys.addaudithook(guard)
_install_guard(_cfg.pop('read_roots'))
del _install_guard
_scope={'__name__':'__main__','__file__':'<audit-group-unit>'}
del _cfg
exec(_code,_scope)
"""
MAX_RESULT_BYTES = 1024 * 1024


# LLM: 整个进程无文件写根，只读系统/解释器/caller工作区；结果仅经匿名管道交宿主审核发表。
# 函数用途: 构造强制allowlist平台沙箱，网络固定关闭。
def execution_sandbox(workspace, staging):
    roots = resolve_python_runtime_prefixes(executable=sys.executable, strict=False)
    return AttemptExecutionSandbox(AttemptSandboxSpec(attempt_view=workspace, staging_root=staging,
        shared_workspace=workspace, owner_home=workspace, network_access=False,
        read_only_root=True, read_mode="allowlist", implicit_attempt_write_roots=False,
        extra_write_roots=(), public_read_roots=(*roots, workspace, staging),
        system_read_roots=system_read_roots_for_platform()))


# LLM: 独立进程只看匿名代码流和明确inputs；不组装主头像Prompt、不加载source配置或凭据。
# 类用途: 提供脚本及结构化技能共用的宿主隔离执行单元。
class IsolatedScriptUnit:
    # LLM: 始终先build_argv/require_ready；仅其成功且识别外层Seatbelt时省内层包装，任何原生拒绝仍原样传播。
    # 函数用途: 用原沙箱就绪门和额外Python只读守卫执行一次匿名代码流。
    def execute(self, code, inputs, *, workspace, staging, timeout_s):
        sandbox = execution_sandbox(workspace, staging)
        base_argv = [sys.executable, "-I", "-B", "-c", _BOOTSTRAP]
        argv = sandbox.build_argv(base_argv)
        if argv and os.path.basename(argv[0]) == "sandbox-exec" and _outer_seatbelt_active():
            argv = base_argv
        payload = json.dumps({"code": code, "inputs": inputs,
            "read_roots": [str(root) for root in (*sandbox.spec.public_read_roots, *sandbox.spec.system_read_roots)]},
            ensure_ascii=False, allow_nan=False).encode()
        environment = {"PATH": os.defpath, "HOME": str(workspace), "TMPDIR": str(staging),
                       "PYTHONIOENCODING": "utf-8", "MY_AGENT_SCRIPT_INPUT_JSON": json.dumps(inputs, ensure_ascii=False)}
        with tempfile.TemporaryFile(dir=staging) as stdin:
            stdin.write(payload)
            stdin.seek(0)
            process = subprocess.Popen(argv, cwd=workspace, env=environment, stdin=stdin,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            capture = None
            try:
                capture = ProcessOutputCapture(process, max_bytes=MAX_RESULT_BYTES)
                _wait(process, capture, timeout_s)
            finally:
                cleanup = _cleanup_process(process)
                if capture is not None:
                    capture.finish(1)
                if cleanup:
                    raise AuditGroupError("AUDIT_GROUP_EXECUTION_CLEANUP_UNVERIFIED", status_code=503)
            stdout, _stderr, facts = capture.result()
            if not facts["complete"] or facts["streams_invalid_utf8"]["stdout"]:
                raise AuditGroupError("AUDIT_GROUP_OUTPUT_LIMIT", status_code=409)
            try:
                result = json.loads(stdout)
                if type(result) is not dict:
                    raise ValueError()
                return result
            except (ValueError, UnicodeError, RecursionError):
                raise AuditGroupError("AUDIT_GROUP_INVALID_OUTPUT", status_code=409) from None


# LLM: 等待期间持续检查取消/输出预算，总期限使用单调钟；只回收本次创建的进程组。
# 函数用途: 等待脚本退出，超时/失败仅返回固定安全错误码。
def _wait(process, capture, timeout_s):
    deadline = time.monotonic() + timeout_s
    while process.poll() is None:
        raise_if_cancelled()
        if sum(capture.result()[2]["bytes_seen"].values()) > MAX_RESULT_BYTES:
            raise AuditGroupError("AUDIT_GROUP_OUTPUT_LIMIT", status_code=409)
        if time.monotonic() >= deadline:
            raise AuditGroupError("AUDIT_GROUP_TIMEOUT", status_code=408)
        time.sleep(0.01)
    if process.returncode:
        raise AuditGroupError("AUDIT_GROUP_EXECUTION_FAILED", status_code=409)
