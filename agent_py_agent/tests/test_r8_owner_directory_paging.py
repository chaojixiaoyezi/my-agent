"""冷目录发现分片推进，分页不能通过截断未排序目录来漏掉旧owner。"""
from agent_py_agent.agent import owner_wake_discovery as discovery


def test_cold_owner_page_bounds_scan_and_eventually_covers_all(tmp_path, monkeypatch):
    owners = tmp_path / 'owners'
    for number in reversed(range(500)):
        (owners / 'providers' / 'feishu' / 'users' / f'u{number:04d}').mkdir(parents=True)
    visits = []
    original = discovery._candidate_owner_homes

    def counted(root):
        for row in original(root):
            visits.append(row)
            yield row

    monkeypatch.setattr(discovery, '_candidate_owner_homes', counted)
    cursor, found = None, []
    for _ in range(140):
        before = len(visits)
        page = discovery.discover_owner_home_page(owners, limit=4, after_cursor=cursor)
        assert len(visits) - before <= 128
        assert len(page.targets) <= 4
        found.extend(target.identity.owner_id for target in page.targets)
        cursor = page.next_cursor
        if cursor is None:
            break
    else:
        raise AssertionError('分页未收敛')
    assert found == [f'u{number:04d}' for number in range(500)]
    assert len(visits) == 500


def test_removed_owner_not_returned_during_reconciliation(tmp_path):
    owners = tmp_path / 'owners'
    for number in range(300):
        (owners / 'local' / f'u{number:04d}').mkdir(parents=True)
    cursor = None
    for _ in range(10):
        page = discovery.discover_owner_home_page(owners, limit=100, after_cursor=cursor)
        cursor = page.next_cursor
        if cursor is None:
            break
    (owners / 'local' / 'u0000').rmdir()
    page = discovery.discover_owner_home_page(owners, limit=4)
    assert all(target.identity.owner_id != 'u0000' for target in page.targets)
    assert page.next_cursor is not None
