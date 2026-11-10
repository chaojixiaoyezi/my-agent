"""临时claim投影的真实连接须先于目录释放；不依赖GC或POSIX允许unlink打开文件。"""
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from agent_py_agent.agent.conversation import claim_activity_projection as view


def test_process_exit_closes_sqlite_before_directory_cleanup(tmp_path):
    result = tmp_path / 'result.json'
    script = textwrap.dedent(f'''
        import atexit,json,shutil,sqlite3,sys
        from pathlib import Path
        sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})
        state={{}}
        def report():
            try: view._connection(); refused=False
            except sqlite3.OperationalError: refused=True
            try: state['conn'].execute('SELECT 1'); closed=False
            except sqlite3.ProgrammingError: closed=True
            Path({str(result)!r}).write_text(json.dumps({{'closed':closed,'gone':not state['directory'].exists(),'late_cache_refused':refused}}))
        atexit.register(report)
        from agent_py_agent.agent.conversation import claim_activity_projection as view
        with view._LOCK: state['conn']=view._connection()
        state['directory']=Path(view._CACHE[1].name)
        original=shutil.rmtree
        def guarded(path,*args,**kwargs):
            if Path(path)==state['directory']:
                try: state['conn'].execute('SELECT 1')
                except sqlite3.ProgrammingError: pass
                else: raise PermissionError('win_style_open_projection')
            return original(path,*args,**kwargs)
        shutil.rmtree=guarded
    ''')
    home, temp = tmp_path / 'home', tmp_path / 'temp'
    home.mkdir()
    temp.mkdir()
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home), MY_AGENT_HOME=str(home),
               TMPDIR=str(temp), TEMP=str(temp), TMP=str(temp))
    run = subprocess.run([sys.executable, '-I', '-B', '-c', script], cwd=tmp_path, env=env,
                         text=True, capture_output=True, timeout=10)
    assert run.returncode == 0, run.stderr
    assert json.loads(result.read_text()) == {'closed': True, 'gone': True, 'late_cache_refused': True}, run.stderr
    assert run.stderr == ''
    assert not list(temp.glob('my-agent-claim-view-*'))


@pytest.mark.parametrize('stage', ['connect', 'pragma', 'schema'])
def test_failed_initialization_closes_retained_real_connection(tmp_path, monkeypatch, stage):
    monkeypatch.setattr(view, '_CACHE', None)
    monkeypatch.setattr(view, '_PORTABLE', {})
    original_connect, original_directory = sqlite3.connect, view.tempfile.TemporaryDirectory
    connections, directories = [], []
    failure = sqlite3.OperationalError('synthetic initialization error')

    class Broken(sqlite3.Connection):
        def execute(self, sql, *args):
            if stage == 'pragma' and sql.startswith('PRAGMA'):
                raise failure
            return super().execute(sql, *args)

        def executescript(self, sql):
            if stage == 'schema':
                raise failure
            return super().executescript(sql)

    def connect(*args, **kwargs):
        if stage == 'connect':
            raise failure
        conn = original_connect(*args, **kwargs, factory=Broken)
        connections.append(conn)
        return conn

    def directory(*args, **kwargs):
        value = original_directory(*args, **kwargs, dir=tmp_path)
        directories.append(value)
        return value

    monkeypatch.setattr(view.sqlite3, 'connect', connect)
    monkeypatch.setattr(view.tempfile, 'TemporaryDirectory', directory)
    try:
        with pytest.raises(sqlite3.OperationalError) as caught:
            with view._LOCK:
                view._connection()
        assert caught.value is failure
        if connections:
            with pytest.raises(sqlite3.ProgrammingError):
                sqlite3.Connection.execute(connections[0], 'SELECT 1')
        assert view._CACHE is None and not Path(directories[0].name).exists()
    finally:
        for conn in connections:
            sqlite3.Connection.close(conn)
        for value in directories:
            value.cleanup()


@pytest.fixture
def isolated_cache(monkeypatch):
    monkeypatch.setattr(view, '_CACHE', None)
    monkeypatch.setattr(view, '_PORTABLE', {})
    yield
    view.close_claim_activity_projection()


def test_explicit_close_releases_streams_and_rebuilds_without_old_generation_refs(tmp_path, isolated_cache):
    import atexit
    import weakref

    from agent_py_agent.agent.io.bounded_directory import DirectoryCursor

    directory = tmp_path / 'claims'
    directory.mkdir()
    (directory / 'entry').touch()
    first = view._connection()
    callbacks = atexit._ncallbacks()
    for _ in range(3):
        with view._LOCK:
            conn = view._connection()
            target = Path(view._CACHE[1].name)
            owned = weakref.ref(view._CACHE[1])
            stream = DirectoryCursor()
            assert stream.next_entry(directory) is not None and stream.entries is not None
            view._PORTABLE['scope'] = stream, 0
        view.close_claim_activity_projection()
        view.close_claim_activity_projection()
        assert stream.entries is None and not view._PORTABLE and view._CACHE is None
        assert not target.exists() and owned() is None
        with pytest.raises(sqlite3.ProgrammingError):
            sqlite3.Connection.execute(conn, 'SELECT 1')
        assert atexit._ncallbacks() == callbacks
    assert view._connection() is not first


@pytest.mark.parametrize('closed_before_error', [False, True])
def test_failed_close_keeps_unready_resource_for_retry(tmp_path, monkeypatch, isolated_cache, closed_before_error):
    original = sqlite3.connect
    failure = sqlite3.OperationalError('synthetic schema failure')
    created = []

    class Broken(sqlite3.Connection):
        close_attempts = 0

        def execute(self, sql, *args):
            raise failure

        def close(self):
            self.close_attempts += 1
            if self.close_attempts == 1:
                if closed_before_error:
                    super().close()
                raise sqlite3.OperationalError('synthetic close failure')
            super().close()

    def connect(*args, **kwargs):
        conn = original(*args, **kwargs, **({'factory': Broken} if not created else {}))
        created.append(conn)
        return conn

    monkeypatch.setattr(view.sqlite3, 'connect', connect)
    with pytest.raises(sqlite3.OperationalError) as caught:
        with view._LOCK:
            view._connection()
    assert caught.value is failure and failure.__notes__
    old = created[0]
    target = Path(view._CACHE[1].name)
    assert view._CACHE[2] is old and view._CACHE[3] is False and target.exists()
    if closed_before_error:
        with pytest.raises(sqlite3.ProgrammingError):
            sqlite3.Connection.execute(old, 'SELECT 1')
    else:
        assert sqlite3.Connection.execute(old, 'SELECT 1').fetchone() == (1,)
    with view._LOCK:
        new = view._connection()
    assert new is not old and view._CACHE[3] is True and not target.exists()
    with pytest.raises(sqlite3.ProgrammingError):
        sqlite3.Connection.execute(old, 'SELECT 1')


def test_close_waits_for_actual_sql_under_the_existing_lock(monkeypatch, isolated_cache):
    import threading

    conn = view._connection()
    entered, release, attempted, closed = (threading.Event() for _ in range(4))
    original_lock = view._LOCK
    errors = []

    class Lock:
        def __enter__(self):
            if threading.current_thread().name == 'projection-close':
                attempted.set()
            return original_lock.__enter__()

        def __exit__(self, *args):
            return original_lock.__exit__(*args)

        def __getattr__(self, name):
            return getattr(original_lock, name)

    monkeypatch.setattr(view, '_LOCK', Lock())

    def blocked():
        entered.set()
        assert release.wait(2)
        return 1

    conn.create_function('blocked', 0, blocked)

    def reader():
        try:
            with view._LOCK:
                assert conn.execute('SELECT blocked()').fetchone() == (1,)
        except BaseException as exc:
            errors.append(exc)

    def closer():
        try:
            view.close_claim_activity_projection()
            closed.set()
        except BaseException as exc:
            errors.append(exc)

    reading = threading.Thread(target=reader)
    closing = threading.Thread(target=closer, name='projection-close')
    reading.start()
    try:
        assert entered.wait(1)
        closing.start()
        assert attempted.wait(1) and not closed.is_set()
    finally:
        release.set()
        reading.join(2)
        if closing.ident is not None:
            closing.join(2)
    assert not reading.is_alive() and not closing.is_alive() and not errors and closed.is_set()
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute('SELECT 1')


@pytest.mark.skipif(not hasattr(os, 'fork'), reason='requires POSIX fork')
def test_normal_child_exit_removes_only_its_own_cache(tmp_path):
    child_record = tmp_path / 'child.json'
    script = textwrap.dedent(f'''
        import json,os,sys
        from pathlib import Path
        sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})
        from agent_py_agent.agent.conversation import claim_activity_projection as view
        with view._LOCK: parent=view._connection()
        parent_directory=Path(view._CACHE[1].name)
        pid=os.fork()
        if pid==0:
            assert view._CACHE is None and not view._PORTABLE
            with view._LOCK: child=view._connection()
            child_directory=Path(view._CACHE[1].name)
            assert child_directory!=parent_directory
            Path({str(child_record)!r}).write_text(json.dumps(str(child_directory)))
            sys.exit(0)
        _,status=os.waitpid(pid,0)
        assert status==0 and parent_directory.exists()
        assert parent.execute('SELECT 1').fetchone()==(1,)
        assert not Path(json.loads(Path({str(child_record)!r}).read_text())).exists()
        view.close_claim_activity_projection()
        assert not parent_directory.exists()
        print('parent_preserved_child_cleaned')
    ''')
    home, temp = tmp_path / 'home', tmp_path / 'temp'
    home.mkdir()
    temp.mkdir()
    env = dict(os.environ, HOME=str(home), MY_AGENT_HOME=str(home), TMPDIR=str(temp))
    run = subprocess.run([sys.executable, '-I', '-B', '-c', script], cwd=tmp_path, env=env,
                         text=True, capture_output=True, timeout=10)
    assert run.returncode == 0 and run.stderr == '', run.stderr
    assert run.stdout.strip() == 'parent_preserved_child_cleaned'
    assert not list(temp.glob('my-agent-claim-view-*'))


def test_directory_cleanup_failure_is_retryable_without_reusing_closed_sqlite(monkeypatch, isolated_cache):
    conn = view._connection()
    directory = view._CACHE[1]
    path = Path(directory.name)
    original, attempts = directory.cleanup, []

    def cleanup():
        attempts.append(True)
        if len(attempts) == 1:
            raise PermissionError('synthetic temporary directory busy')
        return original()

    monkeypatch.setattr(directory, 'cleanup', cleanup)
    with pytest.raises(PermissionError):
        view.close_claim_activity_projection()
    assert path.exists() and view._CACHE[2] is None and view._CACHE[3] is False
    with pytest.raises(sqlite3.ProgrammingError):
        sqlite3.Connection.execute(conn, 'SELECT 1')
    assert view._connection() is not conn and not path.exists()
    assert len(attempts) == 2
