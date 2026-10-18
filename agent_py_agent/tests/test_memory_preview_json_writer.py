"""原无尾换行JSON写者只增加显式一次replace，不改变默认重试/截止/清理。"""

import json
import stat

import pytest

from agent_py_agent.agent.common import json_io


@pytest.mark.parametrize("once", [False, True])
def test_original_json_writer_preserves_actual_replace_failure_identity(tmp_path, monkeypatch, once):
    path = tmp_path / "receipt.json"
    path.write_text('{"old": true}')
    original, calls, sleeps = path.read_bytes(), [], []
    error = PermissionError("synthetic original replace failure")

    def fail_replace(_tmp, target):
        assert target == path
        calls.append(target)
        raise error

    monkeypatch.setattr(type(path), "replace", fail_replace)
    monkeypatch.setattr(json_io.time, "sleep", sleeps.append)
    options = {"replace_once": True} if once else {}
    with pytest.raises(PermissionError) as caught:
        json_io.write_private_json_file_atomic_no_newline_unlocked(path, {"new": True}, **options)
    assert caught.value is error and path.read_bytes() == original
    assert len(calls) == (1 if once else 8)
    assert sleeps == ([] if once else [0.01 * (n + 1) for n in range(8)])
    assert sorted(p.name for p in tmp_path.iterdir()) == [path.name]


def test_once_json_writer_preserves_bytes_mode_and_expired_deadline(tmp_path):
    path = tmp_path / "receipt.json"
    payload = {"中文": "原格式", "z": 1}
    json_io.write_private_json_file_atomic_no_newline_unlocked(path, payload, replace_once=True)
    assert path.read_bytes() == json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    target = tmp_path / "new" / "expired.json"
    with pytest.raises(BlockingIOError):
        json_io.write_private_json_file_atomic_no_newline_unlocked(target, payload, deadline=0, replace_once=True)
    assert not target.parent.exists()
