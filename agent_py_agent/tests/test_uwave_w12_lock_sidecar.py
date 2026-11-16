from agent_py_agent.agent.common.json_io import terminal_json_path


def test_cleanup_requires_explicit_commit_and_absence(tmp_path):
    body = tmp_path / 'body.json'
    lock = tmp_path / 'body.json.lock'
    with terminal_json_path(body):
        pass
    assert lock.exists()
    body.write_text('{}')
    with terminal_json_path(body) as cleanup:
        cleanup()
    assert lock.exists()
    with terminal_json_path(body) as cleanup:
        body.unlink()
        cleanup()
    assert not lock.exists()


def test_cleanup_exception_preserves_lock(tmp_path):
    body = tmp_path / 'body.json'
    try:
        with terminal_json_path(body) as cleanup:
            cleanup()
            raise ValueError('not committed')
    except ValueError:
        pass
    assert body.with_suffix('.json.lock').exists()


def test_cleanup_preserves_replaced_inode(tmp_path):
    body = tmp_path / 'body.json'
    lock = body.with_suffix('.json.lock')
    with terminal_json_path(body) as cleanup:
        lock.unlink()
        lock.write_text('successor')
        cleanup()
    assert lock.read_text() == 'successor'


def test_old_waiter_reopens_successor_inode(tmp_path, monkeypatch):
    import os
    import threading

    from agent_py_agent.agent.common import json_io

    body = tmp_path / 'body.json'
    opened = threading.Event()
    proceed = threading.Event()
    entered = threading.Event()
    original = json_io._open_private_lock_descriptor
    waiter_ident = []
    first = [True]

    def observe(path):
        fd = original(path)
        if threading.get_ident() in waiter_ident and first[0]:
            first[0] = False
            opened.set()
            assert proceed.wait(2)
        return fd

    monkeypatch.setattr(json_io, '_open_private_lock_descriptor', observe)

    def wait():
        waiter_ident.append(threading.get_ident())
        fd = json_io._acquire_verified_lock_descriptor(body)
        try:
            entered.set()
        finally:
            json_io._funlock_descriptor(fd)
            os.close(fd)

    with terminal_json_path(body) as cleanup:
        thread = threading.Thread(target=wait)
        thread.start()
        assert opened.wait(2)
        cleanup()
    with terminal_json_path(body):
        proceed.set()
        assert not entered.wait(0.05)
    thread.join(2)
    assert entered.is_set()
