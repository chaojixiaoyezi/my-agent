# LLM: 全部在 pytest 临时目录内复现；命令流用合成 HTTP 输出与 Event 定序，索引与脚本走原入口，凭据只用合成值且只比对结构化回执。
# 模块用途: 钉住 Conveyor M04 的四个底层不变量：L21 目标续跑扣下码已登记为待核状态并带继续条件；L24 终帧与 finished 同一临界区提交，
#   心跳在终帧取得写权后到期也不会再产出帧；L26/D10.19 整页预取后同 inode 截短仍走 INDEX_SOURCE_CHANGED 并正确续跑；
#   L22/D08.7 嵌套脚本启动前拒绝保留外码并附内因，启动、效果与回收回执共用同一次尝试的 launch_id，重试换新 id。
import errno
import io
import json
import re
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.tool_runtime_ledger import write_boundary_with_runtime_ledger
from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.common.cancellation import CancellationToken, bind_cancellation_token
from agent_py_agent.agent.contracts.error_taxonomy import error_contract
from agent_py_agent.agent.contracts.recovery import RecoveryAction
from agent_py_agent.agent.conversation.goal_progress_fuse import (
    WITHHELD_REASON_CODE,
    goal_withheld_notice,
)
from agent_py_agent.agent.gateway_parts import command_stream
from agent_py_agent.agent.gateway_parts.command_stream_protocol import CommandStreamFrame
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.tooling.host_output_redaction import turn_credential_redaction
from agent_py_agent.agent.tooling.scripts_tool import ScriptsTool
from agent_py_agent.agent.user_space.home_index_rebuild import reconcile_home_index_page
from agent_py_agent.agent.user_space.home_indexes import (
    RunIndexRef,
    TaskIndexRef,
    latest_run_refs,
    latest_task_refs,
    replace_home_index_snapshots,
)
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.agent.user_space.storage_lanes import StorageLaneBudget
from agent_py_agent.agent.workflows import datasource_runtime, script_runner
from agent_py_agent.agent.workflows.models import ScriptExecutionResult
from agent_py_agent.agent.workflows.script_runner import ScriptRunner
from agent_py_agent.tests.test_f155_w8_index_convergence import setup_workspace
from agent_py_agent.tests.test_fc2_credential_shell_boundary import _submit_custom_card
from agent_py_agent.tests.test_ingestion_task_credential_scope import _active, _agent

# 线程 Event 的有界等待上限：只防测试挂死，正常路径都由 Event 直接唤醒。
_WAIT_SECONDS = 10
_HEARTBEAT_THREAD = 'host-command-heartbeat'
_LAUNCH_ID = re.compile(r'[0-9a-f]{32}')


# ---------------------------------------------------------------- L21 ----

# LLM: 只读已登记合同与宿主固定生成的提示结构；分类表不授予自动续跑、重放或改写目标的权限。
# 函数用途: 证明扣下码按待核暂停登记并带继续条件，上层仍拿到暂停原因、原目标与唤醒身份。
def test_goal_continuation_withheld_is_registered_as_needs_review_with_continuation_condition():
    notice = goal_withheld_notice(SimpleNamespace(goal_id='goal-m04', revision=7),
                                  SimpleNamespace(wake_signal_id='wake-m04'),
                                  SimpleNamespace(turn_end_reason='max_rounds', runtime_reason='',
                                                  runtime_status='completed'))
    assert notice.code == WITHHELD_REASON_CODE == 'GOAL_CONTINUATION_WITHHELD'
    contract = error_contract(notice.code)
    assert contract.code == notice.code, contract
    assert (contract.category, contract.retryable, contract.recommended_action) == (
        'state', False, RecoveryAction.MANUAL_REVIEW.value)
    assert '/goal resume' in contract.recovery_hint and '/goal pause' in contract.recovery_hint
    details = dict(notice.details)
    assert (details['goal_id'], details['goal_revision'], details['wake_signal_id']) == ('goal-m04', '7', 'wake-m04')
    assert (details['turn_end_reason'], details['runtime_status']) == ('max_rounds', 'completed')


# ---------------------------------------------------------------- L24 ----

class _DueGatedFinished(threading.Event):
    """finished 标记：心跳线程的计时只在测试放行 due 时到期一次（按“尚未完成”返回），其余等待保持原 Event 语义。"""

    def __init__(self, due, due_returned):
        super().__init__()
        self._due, self._due_returned, self._expired = due, due_returned, False

    def wait(self, timeout=None):
        if threading.current_thread().name != _HEARTBEAT_THREAD or self._expired:
            return super().wait(timeout)
        self._expired = True
        self._due.wait(_WAIT_SECONDS)
        self._due_returned.set()
        return False


class _ContendedLock:
    """本流写锁的观测包装：心跳线程开始争用写权时打点，其余行为与 threading.Lock 相同。"""

    def __init__(self, contended):
        self._lock, self._contended = threading.Lock(), contended

    def __enter__(self):
        if threading.current_thread().name == _HEARTBEAT_THREAD:
            self._contended.set()
        self._lock.acquire()
        return self

    def __exit__(self, *_exc):
        self._lock.release()
        return False


class _Wire:
    """合成 HTTP 输出：终帧写入时先通知测试已取得写权，再等放行；timed_out 时按 socket 写超时失败。"""

    def __init__(self, final_has_write_right, release_final, final_write):
        self.kinds = []
        self._final_has_write_right, self._release_final = final_has_write_right, release_final
        self._final_write = final_write

    def write(self, raw):
        kind = json.loads(raw)['kind']
        if kind == 'result':
            self._final_has_write_right.set()
            self._release_final.wait(_WAIT_SECONDS)
            if self._final_write == 'timed_out':
                raise TimeoutError('timed out')
        self.kinds.append(kind)
        return len(raw)

    def flush(self):
        return None


# LLM: 原 serve_command_stream 在独立线程运行；只替换 finished 的心跳计时与写锁观测，裁决与写出仍是产品代码。
# 函数用途: 心跳在终帧已取得写权后到期并争用写权，证明终帧之后不再产出任何帧，慢写失败时也只有这一次终帧。
@pytest.mark.parametrize('final_write', ['delivered', 'timed_out'])
def test_heartbeat_due_after_final_frame_has_write_right_never_follows_it(tmp_path, monkeypatch, final_write):
    due, due_returned, contended = threading.Event(), threading.Event(), threading.Event()
    final_has_write_right, release_final = threading.Event(), threading.Event()
    produced = []
    encode = CommandStreamFrame.encode

    def observed(frame):
        produced.append(frame.kind)
        return encode(frame)

    class _Stream(command_stream._CommandStream):
        def __init__(self, handler, request_id):
            super().__init__(handler, request_id)
            self.finished = _DueGatedFinished(due, due_returned)
            self.lock = _ContendedLock(contended)

    monkeypatch.setattr(CommandStreamFrame, 'encode', observed)
    monkeypatch.setattr(command_stream, '_CommandStream', _Stream)
    wire = _Wire(final_has_write_right, release_final, final_write)
    handler = SimpleNamespace(wfile=wire, connection=SimpleNamespace(settimeout=lambda _seconds: None),
                              close_connection=False, send_response=lambda _code: None,
                              send_header=lambda *_args: None, end_headers=lambda: None)
    server = threading.Thread(target=command_stream.serve_command_stream,
                              args=(handler, gateway_paths_from_root(tmp_path), OwnerIdentity('local', 'main', 'main'),
                                    'm04-final-frame', lambda _permission, _token: {'state': 'succeeded'}),
                              kwargs={'interactive': False})
    server.start()
    try:
        assert final_has_write_right.wait(_WAIT_SECONDS)
        due.set()
        assert due_returned.wait(_WAIT_SECONDS) and contended.wait(_WAIT_SECONDS)
    finally:
        due.set()
        release_final.set()
        server.join(_WAIT_SECONDS)
    assert not server.is_alive()
    assert not any(thread.name == _HEARTBEAT_THREAD for thread in threading.enumerate())
    assert produced == ['connected', 'result'], produced
    assert wire.kinds == (['connected', 'result'] if final_write == 'delivered' else ['connected'])


# ---------------------------------------------------------- L26 / D10.19 ----

# 函数用途: 生成一行与权威状态不一致（active）的本 owner 任务索引。
def _row(home, root, status='active'):
    return json.dumps(dict(owner_id=home.owner_id, task_id='task', task_path=str(root),
                           status=status, title='task')).encode() + b'\n'


# LLM: 整个索引小于本解释器默认读缓冲，首个 readline 就把整页预取进缓冲；截短发生在第一次修复提交之后、同一 inode 上。
# 函数用途: 证明缓冲已读完整页时同 inode 截短仍报 INDEX_SOURCE_CHANGED，并按同身份的新长度续跑到与权威状态一致。
def test_same_inode_truncation_after_full_page_prefetch_is_detected_and_resumes(tmp_path):
    home, root = setup_workspace(tmp_path)
    good = _row(home, root)
    path = home.global_index_active_tasks_jsonl
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(good + b'{"owner_id":"local/other","task_id":"skip"}\n' * 20)
    assert path.stat().st_size < io.DEFAULT_BUFFER_SIZE
    inode = path.stat().st_ino
    truncated = []

    def truncate_after_first_repair(operation):
        receipt = operation()
        if not truncated:
            with path.open('r+b') as handle:
                handle.truncate(len(good))
            truncated.append(path.stat().st_size)
        return receipt

    reports = [reconcile_home_index_page(home, owner_id=home.owner_id, apply=True,
                                         budget=StorageLaneBudget(1000, 1024 * 1024, None),
                                         commit_transition=truncate_after_first_repair)]
    assert truncated == [len(good)] and path.stat().st_ino == inode
    assert any(err['code'] == 'INDEX_SOURCE_CHANGED' for err in reports[0]['errors']), reports[0]
    cursor = reports[0]['next_cursor']
    for _ in range(8):
        if not cursor:
            break
        reports.append(reconcile_home_index_page(home, owner_id=home.owner_id, apply=True, cursor=cursor,
                                                 budget=StorageLaneBudget(1000, 1024 * 1024, None)))
        assert reports[-1]['next_cursor'] != cursor, reports
        cursor = reports[-1]['next_cursor']
    assert cursor == '', reports
    # 截短抹掉了第一次修复写入的 done 行；只有重读同一身份的剩余字节才会再次修复，最终索引与权威状态一致。
    assert latest_task_refs(home)[0]['status'] == 'done', reports


# ---------------------------------------------------------- L22 / D08.7 ----

_NONZERO_WITH_CHILD = (
    "import subprocess, sys\n"
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
    "sys.exit(3)\n"
)


# LLM: 非携卡 bash 固化脚本在需要范围约束而系统沙箱未生效时必须启动前拒绝；runner 由宿主注入，不放宽执行环境。
# 函数用途: 建立一个会命中嵌套 shell 启动前拒绝的 ScriptsTool。
def _nested_shell_tool(tmp_path):
    owner = tmp_path / 'owner'
    workspace = owner / 'tasks' / '2026-11-19' / 'm04'
    workspace.mkdir(parents=True)
    ScriptStore(owner, workspace_root=workspace).save_script(
        AssetDescriptor('shell-scope', 'script', 'shell-scope'), "printf '{\"written\":true}'\n", interpreter='bash')
    runner = ScriptRunner(owner, workspace_root=workspace, use_sandbox=False, restrict_to_roots=True)
    return ScriptsTool(owner, workspace_root=workspace, runner=runner)


# LLM: 复用 FC2 原卡片提交、原凭据上下文与真实平台沙箱；脚本启动后派生子进程再以非零码退出，由 runner 回收整棵进程树。
# 函数用途: 返回携卡 ScriptsTool、凭据执行上下文与凭据引用，退出时撤销本次凭据使用与回合脱敏。
@contextmanager
def _credential_scripts(tmp_path):
    agent = _agent(tmp_path, 'user', full=False)
    _active(agent)
    ref = _submit_custom_card(agent, 'https://launch.example.test/api',
                              fields_spec=[{'name': 'secret', 'label': 'Token'}],
                              submitted_values={'secret': 'SYNTHETIC_M04_LAUNCH_SECRET'},
                              authentication='bearer', collection_mode='password')
    owner_home = Path(agent.home_paths.owner_home_dir)
    tool = ScriptsTool(owner_home, workspace_root=agent.tools.workspace_root,
                       runner=ScriptRunner(owner_home, workspace_root=agent.tools.workspace_root,
                                           read_scope_mode='exact', owner_scope_root=owner_home,
                                           agent_home_root=agent.home_paths.root))
    registered = tool.execute({'action': 'register', 'script_id': 'launch_probe', 'code': _NONZERO_WITH_CHILD,
                               'descriptor': {'name': '启动身份探针', 'access_targets': [], 'data_sources': [],
                                              'credential_refs': [], 'io_contract': {'inputs': [], 'outputs': ['ok']}}})
    assert registered.ok, registered.output
    boundary = write_boundary_with_runtime_ledger(agent, agent._current_run_params)
    redaction = boundary['_host_output_redaction']
    cred_use = redaction._credential_use.begin_invocation(redaction)
    try:
        yield tool, SimpleNamespace(credential_use=cred_use, execution_authority_check=None), ref
    finally:
        cred_use.disarm()
        turn_credential_redaction(agent, agent._current_run_params).close()


# LLM: 外层稳定码不变，内因、阶段、未启动事实与下一步都是结构化附加字段；runner 未进入时 launch_id 只能是空串。
# 函数用途: D08.7 第 1 例：启动前拒绝带本次尝试的 launch_id、inner_code、stage 与 not_started，且每次尝试换新 id。
def test_prelaunch_refusal_keeps_outer_code_with_inner_cause_and_launch_identity(tmp_path):
    tool = _nested_shell_tool(tmp_path)
    first, second = (tool.execute({'action': 'run', 'script_id': 'shell-scope'}) for _ in range(2))
    for outcome in (first, second):
        envelope = outcome.result_envelope
        assert not outcome.ok and outcome.error_code == envelope['error_code'] == 'SANDBOX_UNAVAILABLE', outcome.output
        assert envelope['inner_code'] == 'NESTED_SHELL_SANDBOX_UNAVAILABLE'
        assert (envelope['stage'], envelope['failure_stage']) == ('sandbox_prepare', 'runtime_gate')
        assert envelope['effect_outcome'] == outcome.effect_outcome == 'not_started'
        assert envelope['next_action'] == outcome.next_action and 'credential_ref' not in outcome.next_action
        assert _LAUNCH_ID.fullmatch(envelope['launch_id'])
        assert envelope['credential_execution']['launch_id'] == envelope['launch_id']
    assert first.result_envelope['launch_id'] != second.result_envelope['launch_id']
    assert first.result_envelope['launch_id'] not in second.output
    never_entered = tool.execute({'action': 'run', 'script_id': 'shell-scope', 'credential_ref': 'cred_' + '0' * 32})
    assert never_entered.error_code == 'CREDENTIAL_NOT_AUTHORIZED', never_entered.output
    assert never_entered.result_envelope['launch_id'] == ''


# LLM: 效果回执（runner 结果）与回收回执（凭据遥测）必须指向同一次启动；第二次同一逻辑任务的尝试换新 id，且不带旧 id。
# 函数用途: D08.7 第 2、3 例：启动后非零退出并回收进程树的两份回执共用 launch_id，重试的回执不含第一次的 id。
def test_post_start_receipts_share_launch_identity_and_retry_mints_a_new_one(tmp_path):
    with _credential_scripts(tmp_path) as (tool, context, ref):
        attempts = [tool.execute_scoped({'action': 'run', 'script_id': 'launch_probe', 'credential_ref': ref}, context)
                    for _ in range(2)]
    for outcome in attempts:
        envelope = outcome.result_envelope
        assert not outcome.ok and envelope['error_code'] == 'SCRIPT_EXIT_NONZERO', outcome.output
        assert envelope['exit_code'] == 3
        assert envelope['effect_outcome'] == outcome.effect_outcome and envelope['effect_outcome'] in ('failed', 'unknown')
        assert _LAUNCH_ID.fullmatch(envelope['launch_id'])
        reclaim = envelope['credential_execution']
        assert reclaim['launch_id'] == envelope['launch_id']
        assert (reclaim['process_tree_reaped'], reclaim['scratch_cleaned'], reclaim['fd_closed']) == (True, True, True)
    first_id, second_id = (outcome.result_envelope['launch_id'] for outcome in attempts)
    assert first_id != second_id
    assert first_id not in attempts[1].output


# ------------------------------------------------- M04-fix: D10.19 / D08.7 ----

# LLM: 原快照发布器在第一次修复提交后原子换掉已发布索引（新 inode）；已持有的旧 FD 读不到新内容，完成证明必须绑定已发布路径身份。
# 函数用途: 证明本页读到的源被原子替换时报 INDEX_SOURCE_CHANGED，并从偏移 0 重读新文件，使替换带入的过期行也被修复。
@pytest.mark.parametrize('phase', ['task', 'run'])
def test_atomic_index_replacement_during_page_is_detected_and_rereads_published_file(tmp_path, phase):
    home, first = setup_workspace(tmp_path)
    second = first.with_name('other-task')
    (second / 'work').mkdir(parents=True)
    (second / 'work' / 'state.json').write_text(json.dumps(dict(task_id='other-task', primary_run_id='other-run',
                                                                status='DONE')))

    def snapshot(first_status, second_status):
        tasks = [TaskIndexRef(home.owner_id, 'task', first, first_status if phase == 'task' else 'done', 'task')]
        runs = [RunIndexRef(home.owner_id, 'run', 'task', first / 'work', first_status if phase == 'run' else 'done')]
        if second_status:
            tasks.append(TaskIndexRef(home.owner_id, 'other-task', second,
                                      second_status if phase == 'task' else 'done', 'other-task'))
            runs.append(RunIndexRef(home.owner_id, 'other-run', 'other-task', second / 'work',
                                    second_status if phase == 'run' else 'done'))
        replace_home_index_snapshots(home, owners=(), task_refs=tuple(tasks), run_refs=tuple(runs), agent_refs=())

    snapshot('active', None)
    path = home.global_index_active_tasks_jsonl if phase == 'task' else home.global_index_active_runs_jsonl
    inode = path.stat().st_ino
    replaced = []

    def replace_after_first_repair(operation):
        receipt = operation()
        if not replaced:
            snapshot('done', 'active')
            replaced.append(path.stat().st_ino)
        return receipt

    report = reconcile_home_index_page(home, owner_id=home.owner_id, apply=True,
                                       budget=StorageLaneBudget(64, 1024 * 1024, None),
                                       commit_transition=replace_after_first_repair)
    assert replaced and replaced[0] != inode
    assert any(err['code'] == 'INDEX_SOURCE_CHANGED' for err in report['errors']), report
    assert report['partial'] is True, report
    refs = latest_task_refs(home) if phase == 'task' else latest_run_refs(home)
    # 替换带入的 other-task 过期 active 行必须在同一次重读里按权威 DONE 修复，而不是带着旧 inode 的证明宣告完成。
    assert {row['task_id']: row['status'] for row in refs} == {'task': 'done', 'other-task': 'done'}, (report, refs)


_LONG_RUNNING = "import time\ntime.sleep(30)\nprint('{\"ok\": true}')\n"


# LLM: 取消只经原 CancellationToken 与 runner 的原检查点触发；按 capture.started 区分子进程创建前后，不依赖计时。
# 函数用途: D08.7 取消两例：创建前取消报 not_started，创建后取消报 unknown；两者都带 CANCELLED、阶段、launch_id 与同 id 的回收回执。
@pytest.mark.parametrize('moment', ['before_spawn', 'after_spawn'])
def test_cancelled_run_keeps_phase_cleanup_receipt_and_launch_identity(tmp_path, monkeypatch, moment):
    token, captures = CancellationToken(), []
    original_execute, original_check = script_runner._execute, script_runner.raise_if_cancelled

    def observed_execute(command, folder, environment, input_json, timeout, capture, **kwargs):
        captures.append(capture)
        return original_execute(command, folder, environment, input_json, timeout, capture, **kwargs)

    def check():
        if captures and not token.cancelled and bool(captures[0].get('started')) == (moment == 'after_spawn'):
            token.cancel('m04 user stop')
        original_check()

    monkeypatch.setattr(script_runner, '_execute', observed_execute)
    monkeypatch.setattr(script_runner, 'raise_if_cancelled', check)
    with _credential_scripts(tmp_path) as (tool, context, ref), bind_cancellation_token(token):
        outcome = tool.execute_scoped({'action': 'run', 'script_id': 'launch_probe', 'credential_ref': ref}, context)
    assert token.cancelled and len(captures) == 1
    expected_effect, expected_stage = (('not_started', 'runtime_gate') if moment == 'before_spawn'
                                       else ('unknown', 'execution'))
    envelope = outcome.result_envelope
    assert not outcome.ok and outcome.error_code == envelope['error_code'] == 'CANCELLED', outcome.output
    assert envelope['effect_outcome'] == outcome.effect_outcome == expected_effect, outcome.output
    assert outcome.failure_stage == expected_stage and outcome.next_action
    assert bool(captures[0].get('started')) is (moment == 'after_spawn')
    assert envelope['phase'] == ('started' if moment == 'after_spawn' else 'preflight')
    assert _LAUNCH_ID.fullmatch(envelope['launch_id'])
    reclaim = envelope['credential_execution']
    assert reclaim['launch_id'] == envelope['launch_id'] and reclaim['effect_outcome'] == expected_effect
    assert (reclaim['process_tree_reaped'], reclaim['scratch_cleaned'], reclaim['fd_closed']) == (True, True, True)


# LLM: S1-P1-02 取消已在途时，拆除阶段删除本次 manifest 遇一次模拟 I/O 错误（EIO）也不能把取消改判为配置失败：
#   终态仍是 CANCELLED、phase=started、effect_outcome=unknown，清理错误按阶段与 errno 单独可查，其余拆除照常完成，
#   回收回执与本次 launch_id 一致且如实反映已核实的回收事实；取消不计入脚本失败摘要。未取消时同一清理错误保持原分类。
# 函数用途: 钉住取消终态跨越拆除清理错误保留，且独立清理错误的原有分类不变。
@pytest.mark.parametrize('cancel', [True, False])
def test_cancellation_survives_manifest_unlink_error_during_teardown(tmp_path, monkeypatch, cancel):
    token, captures, faults, summaries = CancellationToken(), [], [], []
    original_execute, original_check = script_runner._execute, script_runner.raise_if_cancelled
    original_unlink, original_summary = datasource_runtime.unlink_file_beneath, script_runner._record_summary

    def observed_execute(command, folder, environment, input_json, timeout, capture, **kwargs):
        captures.append(capture)
        return original_execute(command, folder, environment, input_json, timeout, capture, **kwargs)

    def check():
        if cancel and captures and captures[0].get('started') and not token.cancelled:
            token.cancel('m04 user stop')
        original_check()

    def unlink(root, parts, *args, **kwargs):
        if not faults and str(parts[-1]).startswith('datasources-'):
            faults.append(parts[-1])
            raise OSError(errno.EIO, 'simulated I/O error')
        return original_unlink(root, parts, *args, **kwargs)

    def summary(owner_home, result):
        summaries.append(result.error_code)
        return original_summary(owner_home, result)

    monkeypatch.setattr(script_runner, '_execute', observed_execute)
    monkeypatch.setattr(script_runner, 'raise_if_cancelled', check)
    monkeypatch.setattr(datasource_runtime, 'unlink_file_beneath', unlink)
    monkeypatch.setattr(script_runner, '_record_summary', summary)
    with _credential_scripts(tmp_path) as (tool, context, ref), bind_cancellation_token(token):
        outcome = tool.execute_scoped({'action': 'run', 'script_id': 'launch_probe', 'credential_ref': ref}, context)
    assert len(captures) == 1 and captures[0].get('started') is True and len(faults) == 1
    envelope = outcome.result_envelope
    if not cancel:
        assert not token.cancelled and not outcome.ok, outcome.output
        assert envelope['error_code'] == 'SCRIPT_CONFIGURATION_ERROR', outcome.output
        assert summaries == ['SCRIPT_CONFIGURATION_ERROR']
        return
    assert token.cancelled and summaries == []
    assert not outcome.ok and outcome.error_code == envelope['error_code'] == 'CANCELLED', outcome.output
    assert envelope['effect_outcome'] == outcome.effect_outcome == 'unknown' and envelope['phase'] == 'started'
    assert outcome.failure_stage == 'execution' and outcome.next_action
    assert _LAUNCH_ID.fullmatch(envelope['launch_id'])
    teardown, = envelope['escalation_context']['teardown_errors']
    assert (teardown['stage'], teardown['path_role'], teardown['errno']) == ('data_source_close', 'datasource_manifest', errno.EIO)
    assert teardown['exception_type'] == 'OSError' and 'simulated I/O error' in teardown['message']
    assert 'cleanup_error' not in envelope['escalation_context']
    reclaim = envelope['credential_execution']
    assert reclaim['launch_id'] == envelope['launch_id'] and reclaim['effect_outcome'] == 'unknown'
    assert (reclaim['process_tree_reaped'], reclaim['scratch_cleaned'], reclaim['fd_closed']) == (True, True, True)


# LLM: runner 结果经其 JSON codec 往返（如跨进程或旧记录读回）不能丢 launch_id；旧记录缺字段读成空串，不猜值。
# 函数用途: D08.7 序列化：声明字段随 to_dict/from_dict 往返，经序列化结果的 ScriptsTool 回执仍带原 id，旧结果可读。
def test_launch_identity_survives_result_codec_and_tool_roundtrip(tmp_path):
    identity = 'a' * 32
    wire = json.loads(json.dumps(ScriptExecutionResult('s', True, 0, 1.0, 0.0, 0.0, {}, '', '',
                                                       launch_id=identity).to_dict()))
    assert wire['launch_id'] == identity and ScriptExecutionResult.from_dict(wire).launch_id == identity
    legacy = {key: value for key, value in wire.items() if key != 'launch_id'}
    assert ScriptExecutionResult.from_dict(legacy).launch_id == ''
    assert 'launch_id' not in ScriptExecutionResult('s', True, 0, 1.0, 0.0, 0.0, {}, '', '').to_dict()

    owner = tmp_path / 'owners' / 'local' / 'main'
    work = owner / 'tasks' / 'roundtrip'
    work.mkdir(parents=True)
    ScriptStore(owner, workspace_root=work).save_script(
        AssetDescriptor('m04_echo', 'script', 'm04_echo', io_contract={'inputs': [], 'outputs': ['ok']}),
        "print('{\"ok\": true}')\n")
    produced = []

    class _SerializedRunner(ScriptRunner):
        def run_script(self, *args, **kwargs):
            result = super().run_script(*args, **kwargs)
            produced.append(result.launch_id)
            return json.loads(json.dumps(result.to_dict()))

    runner = _SerializedRunner(owner, workspace_root=work, read_scope_mode='exact', owner_scope_root=owner,
                               agent_home_root=tmp_path)
    outcome = ScriptsTool(owner, workspace_root=work, runner=runner).execute({'action': 'run', 'script_id': 'm04_echo'})
    assert outcome.ok, outcome.output
    assert len(produced) == 1 and _LAUNCH_ID.fullmatch(produced[0])
    assert outcome.result_envelope['launch_id'] == produced[0]


# LLM: Schema 与必填项校验在 runner 进入之前拒绝，launch_id 只能是显式空串；其它动作的拒绝回执不加该字段。
# 函数用途: D08.7 预派发拒绝：run 的校验拒绝带 launch_id=""，非 run 动作保持原回执形状。
@pytest.mark.parametrize('params', [{'action': 'run', 'inputs': {}},
                                    {'action': 'run', 'script_id': 'shell-scope', 'timeout_seconds': 'later'}])
def test_predispatch_run_refusal_reports_explicit_empty_launch_id(tmp_path, params):
    tool = _nested_shell_tool(tmp_path)
    outcome = tool.execute(params)
    assert outcome.error_code == 'TOOL_INVALID_ARGUMENTS' and outcome.effect_outcome == 'not_started', outcome.output
    assert outcome.result_envelope['launch_id'] == '' and json.loads(outcome.output)['launch_id'] == ''
    other = tool.execute({'action': 'get'})
    assert other.error_code == 'TOOL_INVALID_ARGUMENTS' and 'launch_id' not in other.result_envelope
