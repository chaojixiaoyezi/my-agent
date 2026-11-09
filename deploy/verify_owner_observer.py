"""镜像构建与隔离 Linux 基础回归共用的原生观察器探针，不进入生产镜像。"""
from __future__ import annotations

import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path


def verify(journal, root):
    root.mkdir(parents=True)
    (root / 'old').mkdir()
    (root / 'old/child').write_bytes(b'child')
    (root / 'inplace').write_bytes(b'aaa')
    (root / 'mode').write_bytes(b'mode')
    (root / 'delete').write_bytes(b'delete')
    first = journal.query(root)
    assert first.clock and not first.complete
    ready = journal.query(root, first.clock)
    assert ready.complete, ready
    previous = (root / 'inplace').stat()
    script = '''import os,pathlib,sys
r=pathlib.Path(sys.argv[1]);p=r/'inplace'
with p.open('r+b') as f:f.write(b'bbb')
os.utime(p,ns=(int(sys.argv[2]),int(sys.argv[3])))
os.chmod(r/'mode',0o400)
(r/'delete').unlink();(r/'old').rename(r/'renamed')
(r/'new').mkdir();(r/'new/child').write_bytes(b'new')
'''
    subprocess.run([sys.executable, '-c', script, str(root), str(previous.st_atime_ns), str(previous.st_mtime_ns)],
                   check=True, timeout=10, env={**os.environ})
    batch = journal.query(root, ready.clock)
    assert batch.complete, batch
    changes = {value['name']: value for value in batch.paths}
    assert changes['inplace']['exists'] and changes['mode']['exists']
    assert not changes['delete']['exists'] and not changes['old/child']['exists']
    assert changes['renamed/child']['exists'] and changes['new/child']['exists']
    quiet = journal.query(root, batch.clock)
    assert quiet.complete and not quiet.paths, quiet
    shutil.rmtree(root)
    lost = journal.query(root, quiet.clock)
    assert not lost.complete
    recovered = journal.query(root, lost.clock)
    assert recovered.clock and not recovered.complete
    resumed = journal.query(root, recovered.clock)
    assert resumed.complete, resumed
    journal.release(root)
    assert root not in journal._roots


def verify_parent_death():
    program = """import json,sys
from pathlib import Path
from owner_change_journal import OwnerChangeJournal
observer=OwnerChangeJournal()
initial=observer.query(Path(sys.argv[1]))
assert initial.clock,initial
print(json.dumps({'pid':observer._process.pid,'directory':str(observer._directory)}),flush=True)
sys.stdin.read()
"""
    with tempfile.TemporaryDirectory(prefix='observer-parent-fixture-') as directory:
        worker = subprocess.Popen([sys.executable, '-c', program, str(Path(directory) / 'tree')],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env={**os.environ, 'PYTHONPATH': str(Path(__file__).parent)})
        descriptor = None
        payload = None
        try:
            payload = json.loads(worker.stdout.readline(4096))
            descriptor = os.pidfd_open(payload['pid'])
            worker.kill()
            worker.wait(timeout=5)
            wait = select.poll()
            wait.register(descriptor, select.POLLIN)
            assert wait.poll(5000), 'parent SIGKILL left the private observer running'
        finally:
            if worker.poll() is None:
                worker.kill()
                worker.wait(timeout=5)
            if descriptor is not None:
                try:
                    signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.close(descriptor)
            if payload is not None:
                shutil.rmtree(payload['directory'], ignore_errors=True)



if __name__ == '__main__':
    from owner_change_journal import OwnerChangeJournal

    observer = OwnerChangeJournal()
    try:
        with tempfile.TemporaryDirectory(prefix='owner-observer-probe-') as directory:
            verify(observer, Path(directory) / 'tree')
    finally:
        observer.close()
    if '--skip-parent-death' not in sys.argv[1:]:
        verify_parent_death()
    print('owner_observer_native_probe_passed')
