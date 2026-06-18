"""archive 轮转接入集成测试:轮转 + gzip + 删段后,no_loss 对账仍成立、query 仍可查。"""

import agent.tooling.log_ops.store as store_mod
from agent.common.rotating_log import RotatePolicy


def _small_rotate(monkeypatch, **kw):
    pol = RotatePolicy(max_bytes=kw.get("max_bytes", 200), backup_count=kw.get("backup_count", 3),
                       compress=kw.get("compress", True), max_total_bytes=kw.get("max_total_bytes", 0))
    monkeypatch.setattr(store_mod, "_ARCHIVE_ROTATE", pol)


def test_archive_rotation_preserves_reconciliation(tmp_path, monkeypatch):
    """轮转 + backup_count 删段后,count_archive_lines(累计口径)仍 == 总写入行数(no_loss 根本)。"""
    _small_rotate(monkeypatch, max_bytes=200, backup_count=2)
    store = store_mod.LogOpsStore(tmp_path, "m1")
    store.ensure_dirs()
    sid = "src1"
    total = 0
    for batch in range(50):
        store.append_archive(sid, [f"2026-06-18 evt{batch}_{i} userX login ok" for i in range(10)])
        total += 10
    # 在线段(留 2 个 backup)行数 < 总数,但累计对账口径不丢
    assert store.count_archive_lines(sid) == total  # 500


def test_archive_rotation_actually_rotates(tmp_path, monkeypatch):
    """确认真的发生了轮转(产生历史段 .1/.gz),不是退化成单文件。"""
    _small_rotate(monkeypatch, max_bytes=200, backup_count=10)
    store = store_mod.LogOpsStore(tmp_path, "m2")
    store.ensure_dirs()
    sid = "src1"
    for batch in range(30):
        store.append_archive(sid, [f"line{batch}_{i} payload data here" for i in range(10)])
    archive_dir = store.archive_path(sid).parent
    segments = list(archive_dir.glob(store.archive_path(sid).name + ".*"))
    seg_files = [s for s in segments if not s.name.endswith(".rotmeta.json")]
    assert len(seg_files) >= 1  # 有历史段
    assert any(s.name.endswith(".gz") for s in seg_files)  # 老段被 gzip


def test_archive_rotation_query_reads_all_segments(tmp_path, monkeypatch):
    """轮转后最新数据在主文件、历史数据在段里;iter_archive_lines 能读到刚写的最新行。"""
    _small_rotate(monkeypatch, max_bytes=2000, backup_count=50)  # 段大些+backup足够,留全300行
    store = store_mod.LogOpsStore(tmp_path, "m3")
    store.ensure_dirs()
    sid = "src1"
    for batch in range(30):
        store.append_archive(sid, [f"2026-06-18 batch{batch}_evt{i}" for i in range(10)])
    seg_files = [s for s in store.archive_path(sid).parent.glob(store.archive_path(sid).name + ".*")
                 if not s.name.endswith(".rotmeta.json")]
    assert len(seg_files) >= 2  # 确有多段轮转(否则没在测"读所有段")
    all_lines = list(store.iter_archive_lines(sid))
    assert len(all_lines) == 300  # backup 足够,全部在线行可遍历
    recent = [ln for ln in all_lines if "batch29_" in ln]
    assert len(recent) == 10  # 最新批可查(在主文件)
    early = [ln for ln in all_lines if "batch0_" in ln]
    assert len(early) == 10  # 最老批(历史段/gz)也可查


def test_archive_budget_prunes_but_reconciliation_holds(tmp_path, monkeypatch):
    """总字节预算触发删最老段:在线行数变少,但累计对账(count_archive_lines)仍等于总写入。"""
    _small_rotate(monkeypatch, max_bytes=200, backup_count=100, max_total_bytes=600)
    store = store_mod.LogOpsStore(tmp_path, "m4")
    store.ensure_dirs()
    sid = "src1"
    total = 0
    for batch in range(60):
        store.append_archive(sid, [f"evt{batch}_{i} some log payload xyz" for i in range(10)])
        total += 10
    online = len(list(store.iter_archive_lines(sid)))
    assert online < total  # 预算逼着删了最老段
    assert store.count_archive_lines(sid) == total  # 但"一条不丢"的累计账目不破
