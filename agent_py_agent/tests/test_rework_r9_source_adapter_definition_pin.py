"""R9-R2 P1-R9-08 Audit：来源适配器执行的必须是已核验的那一份字节。

校验后、子进程启动前改写工作区脚本（模拟定义漂移），本次仍执行已核验快照；
快照位于 Audit 工作区外、唯一私有、只读单叶挂载，结束即删除；下一次请求按原引用重验失败。
"""
import json
import sys
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.workspace_paths import audit_workspace_path
from agent_py_agent.agent.ingestion import source_adapter
from agent_py_agent.agent.ingestion.source_adapter import SourceAdapterError
from agent_py_agent.agent.tooling.sandbox import find_bwrap

AUDIT_ID = 'audit-r9-definition-pin'
VERIFIED = b'''import json, os, sys
payload = json.load(sys.stdin)
try:
    with open(__file__, "ab"):
        snapshot_write = "allowed"
except OSError:
    snapshot_write = "denied"
json.dump({"request": {}, "context": {"body": "verified", "phase": payload["phase"], "cwd": os.getcwd(),
    "file": __file__, "isolated": sys.flags.isolated, "no_site": sys.flags.no_site,
    "snapshot_write": snapshot_write}}, sys.stdout)
'''
EDITED = b'import json, sys\njson.dump({"request": {}, "context": {"body": "edited"}}, sys.stdout)\n'
# 组件级替身：记录 argv/快照权限，按 --chdir 进入原 cwd 后原样执行 "--" 之后的命令。
FAKE_BWRAP = '''#!{python}
import json, os, stat, sys, time
args = sys.argv[1:]
split = args.index("--")
options, command = args[:split], args[split + 1:]
snapshot = command[-1]
record = {{"options": options, "command": command,
          "file_mode": stat.S_IMODE(os.stat(snapshot).st_mode),
          "dir_mode": stat.S_IMODE(os.stat(os.path.dirname(snapshot)).st_mode)}}
with open({log!r}, "w", encoding="utf-8") as handle:
    json.dump(record, handle)
if {mode!r} == "fail":
    raise SystemExit(3)
if {mode!r} == "hang":
    time.sleep(30)
os.chdir(options[options.index("--chdir") + 1])
os.execv(command[0], command)
'''
_LIMIT_ENV = ('MY_AGENT_TOOL_MAX_MEMORY_MB', 'MY_AGENT_TOOL_MAX_PIDS', 'MY_AGENT_TOOL_NICE',
              'MY_AGENT_TOOL_OOM_SCORE_ADJ')


@pytest.fixture
def case(tmp_path, monkeypatch):
    for name in _LIMIT_ENV:
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / 'owner'
    root = audit_workspace_path(home, AUDIT_ID)
    script = root / 'work' / 'sources' / 's1' / 'adapter.py'
    script.parent.mkdir(parents=True)
    script.write_bytes(VERIFIED)
    adapter = {'path': 'work/sources/s1/adapter.py', 'sha256': sha256(VERIFIED).hexdigest()}
    return SimpleNamespace(home=home, root=root.resolve(), script=script.resolve(), adapter=adapter,
                           log=tmp_path / 'fake-bwrap.json', tmp=tmp_path)


def _plan(case):
    return source_adapter.plan_source_request(owner_home=case.home, audit_id=AUDIT_ID,
        source_url='https://logs.example.test/search', request_facts={'method': 'GET'},
        adapter=case.adapter, checkpoint={}, page_limit=5, now_unix=1.0)


def _fake_bwrap(case, monkeypatch, mode='run'):
    fake = case.tmp / 'fake-bwrap'
    fake.write_text(FAKE_BWRAP.format(python=sys.executable, log=str(case.log), mode=mode), encoding='utf-8')
    fake.chmod(0o700)
    monkeypatch.setattr(source_adapter, 'find_bwrap', lambda: str(fake))
    monkeypatch.setattr(source_adapter, '_system_python', lambda: sys.executable)


def _edit_after_verification(case, monkeypatch):
    real = source_adapter.build_bwrap_argv

    def build_then_edit(spec):
        argv = real(spec)
        # 模拟定义漂移：哈希已核验、子进程尚未启动时改写工作区脚本。
        case.script.write_bytes(EDITED)
        return argv

    monkeypatch.setattr(source_adapter, 'build_bwrap_argv', build_then_edit)


def _snapshots(case):
    folder = case.home / 'tmp'
    return sorted(folder.glob('.source-adapter-*')) if folder.exists() else []


def test_verified_bytes_run_from_private_read_only_snapshot(case, monkeypatch):
    _fake_bwrap(case, monkeypatch)
    _edit_after_verification(case, monkeypatch)

    context = _plan(case).context

    assert context['body'] == 'verified' and context['phase'] == 'plan'
    assert case.script.read_bytes() == EDITED
    record = json.loads(case.log.read_text(encoding='utf-8'))
    snapshot = Path(record['command'][-1])
    # 原解释器隔离参数、原 cwd 不变，只把执行目标换成已核验快照。
    assert record['command'] == [sys.executable, '-I', '-S', str(snapshot)]
    assert context['file'] == str(snapshot) and context['cwd'] == str(case.script.parent)
    assert context['isolated'] == 1 and context['no_site'] == 1
    options = record['options']
    assert options[options.index('--chdir') + 1] == str(case.script.parent)
    # 唯一私有快照：Audit 工作区外、单叶只读挂载、0700 目录 + 0400 文件。
    assert not snapshot.is_relative_to(case.root) and snapshot.parent.parent == case.home.resolve() / 'tmp'
    assert snapshot.name == case.script.name
    assert (record['dir_mode'], record['file_mode']) == (0o700, 0o400)
    leaf = options.index(str(snapshot))
    assert options.count(str(snapshot)) == 2 and options[leaf - 1:leaf + 2] == ['--ro-bind', str(snapshot), str(snapshot)]
    assert str(snapshot.parent) not in options and str(snapshot.parent.parent) not in options
    # 网络与写限制不变：断网，唯一可写挂载仍是框架 scratch /tmp，工作区只读。
    assert '--unshare-net' in options and '--share-net' not in options
    assert [options[i + 2] for i, flag in enumerate(options) if flag == '--bind'] == ['/tmp']
    assert ['--ro-bind', str(case.root), str(case.root)] == options[
        options.index(str(case.root)) - 1:options.index(str(case.root)) + 2]
    # 子进程结束即清理，不留快照目录。
    assert not snapshot.exists() and not snapshot.parent.exists() and _snapshots(case) == []

    case.log.unlink()
    with pytest.raises(SourceAdapterError, match='哈希与已发布版本不一致'):
        _plan(case)
    assert not case.log.exists() and _snapshots(case) == []


@pytest.mark.parametrize('mode, code', [('fail', 'SOURCE_ADAPTER_INVALID'), ('hang', 'SOURCE_ADAPTER_TIMEOUT')])
def test_snapshot_is_removed_after_failed_or_expired_execution(case, monkeypatch, mode, code):
    _fake_bwrap(case, monkeypatch, mode=mode)
    monkeypatch.setattr(source_adapter, '_TIMEOUT_SECONDS', 1.0)

    with pytest.raises(SourceAdapterError) as failed:
        _plan(case)

    assert failed.value.code == code
    record = json.loads(case.log.read_text(encoding='utf-8'))
    assert (record['dir_mode'], record['file_mode']) == (0o700, 0o400)
    assert not Path(record['command'][-1]).exists() and _snapshots(case) == []


def test_script_load_is_bounded_before_any_snapshot_or_sandbox(case, monkeypatch):
    _fake_bwrap(case, monkeypatch)
    oversized = b'#' * (256 * 1024 + 1)
    case.script.write_bytes(oversized)
    case.adapter['sha256'] = sha256(oversized).hexdigest()

    with pytest.raises(SourceAdapterError, match='字节之间'):
        _plan(case)

    assert not case.log.exists() and _snapshots(case) == []


@pytest.mark.skipif(find_bwrap() is None, reason='native bwrap sandbox is unavailable on this platform')
def test_native_sandbox_executes_only_the_verified_read_only_snapshot(case, monkeypatch):
    _edit_after_verification(case, monkeypatch)

    context = _plan(case).context

    assert context['body'] == 'verified' and context['snapshot_write'] == 'denied'
    assert context['cwd'] == str(case.script.parent)
    assert context['isolated'] == 1 and context['no_site'] == 1
    assert not Path(context['file']).is_relative_to(case.root)
    assert not Path(context['file']).exists() and _snapshots(case) == []
    with pytest.raises(SourceAdapterError, match='哈希与已发布版本不一致'):
        _plan(case)
