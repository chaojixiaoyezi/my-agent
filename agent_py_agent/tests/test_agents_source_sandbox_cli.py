"""真实模块入口、临时假binary：不执行mount/network，不把假version当完整readiness。"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(
    os.name == 'nt',
    reason='临时 fake binary 依赖 POSIX shebang；原生 Windows 不执行该脚本，Linux/macOS 仍运行两个 CLI 合同。',
)
@pytest.mark.parametrize('binary_only', [False, True])
def test_module_cli_defines_all_builders_before_fake_binary_probe(tmp_path, binary_only):
    fake = tmp_path / 'bwrap'
    trace = tmp_path / 'fake-invocations.jsonl'
    fake.write_text('#!' + sys.executable + '\n'
        'import json,os,sys\n'
        'with open(os.environ["FAKE_BWRAP_TRACE"],"a") as out: out.write(json.dumps(sys.argv[1:])+"\\n")\n'
        'if sys.argv[1:]==["--version"]:\n'
        ' print("bubblewrap fake-no-isolation")\n'
        ' raise SystemExit(0)\n'
        'print("controlled fake refuses isolation",file=sys.stderr)\n'
        'raise SystemExit(17)\n')
    fake.chmod(0o700)
    repository = Path(__file__).resolve().parents[2]
    # Mac上只把发现分支选到临时fake；runpy保留__main__的定义/执行顺序，不加载后再调用main。
    launcher = ('import platform,runpy,sys;platform.system=lambda:"Linux";'
        'sys.argv=["sandbox"]+sys.argv[1:];'
        'runpy.run_module("agent_py_agent.agent.tooling.sandbox",run_name="__main__")')
    home = tmp_path / 'explicit-home'
    home.mkdir()
    env = {'PATH': str(tmp_path) + os.pathsep + os.defpath, 'PYTHONPATH': str(repository),
        'MY_AGENT_HOME': str(home), 'TMPDIR': str(tmp_path), 'FAKE_BWRAP_TRACE': str(trace),
        'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUTF8': '1'}
    result = subprocess.run([sys.executable, '-B', '-c', launcher, *(['--binary-only'] if binary_only else [])],
        cwd=tmp_path, env=env, text=True, capture_output=True, timeout=20)
    assert 'NameError' not in result.stderr, result.stderr
    report = json.loads(result.stdout)
    calls = [json.loads(line) for line in trace.read_text().splitlines()]
    assert calls[0] == ['--version']
    if binary_only:
        assert result.returncode == 0 and report['ready'] is True
        assert report['code'] == 'SANDBOX_BINARY_READY'
        assert report['checks'] == ['binary']
        assert calls == [['--version']]
    else:
        assert result.returncode == 1 and report['ready'] is False
        assert report['code'] == 'BWRAP_ISOLATION_FAILED'
        assert report['checks'] == ['binary']
        assert any('--' in arguments and 'probe-ok' in arguments[-1] for arguments in calls)
        assert not list(tmp_path.rglob('probe-ok'))
