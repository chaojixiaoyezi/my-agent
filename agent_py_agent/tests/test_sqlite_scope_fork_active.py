"""MC4-403：活跃事务/半读游标期间 fork，不在多线程 pytest 自身 fork。"""

import os
import subprocess
import sys

import pytest


@pytest.mark.skipif(not hasattr(os, 'fork'), reason='目标平台没有 fork，须另做平台生命周期验收')
@pytest.mark.parametrize('scenario', ['transaction', 'cursor'])
@pytest.mark.parametrize('mode', ['cached', 'nested', 'overflow'])
def test_active_fork_return_never_operates_parent_connection(tmp_path, scenario, mode):
    result = subprocess.run(
        [sys.executable, '-m', 'agent_py_agent.tests.support.sqlite_active_fork_probe',
         str(tmp_path / 'fork.db'), scenario, str(tmp_path / 'child.json'), mode],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == 'active fork return and parent data verified'
