# LLM: 所有清理与竞态回归仅操作pytest临时Home，不调用真实家目录；保留原安全断言与真实系统调用。
# 模块用途: 验证白名单、隔离恢复、活动资格及清单独占发布，禁止为通过而弱化保护。
"""一次性清理必须先证明保护边界，所有数据均在pytest临时目录。"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/home_file_slimming_cleanup.py"
SPEC = importlib.util.spec_from_file_location("home_slimming", SCRIPT)
cleanup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cleanup)
DAY = 86400
NOW = time.time()
TODAY = time.strftime("%Y-%m-%d", time.localtime(NOW))
OLD_DATE = "2020-01-01"
OWNER = Path("owners/local/main")
CATEGORIES = (
    "dangling_orphan_agent_projections",
    "task_idle_tmp_and_empty_dirs",
    "stale_test_workspaces",
    "old_release_switch_backups",
    "archive_before_20261019_scratch_only",
)


# LLM: 测试helper只向传入的隔离根创建父目录和样本，不选择真实Home。
# 函数用途: 写入指定临时文件并返回路径。
def write(root, relative, text="scratch"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# LLM: 只修改测试树mtime，不跟随链接；NOW与产品调用使用同一时钟。
# 函数用途: 将样本文件及目录变成满足年龄门的历史对象。
def age_tree(path, days=10):
    paths = list(path.rglob("*")) if path.is_dir() else []
    for p in [*reversed(paths), path]:
        if not p.is_symlink():
            os.utime(p, (NOW - days * DAY, NOW - days * DAY))


# LLM: 快照只读普通样本文件的真实字节，不读取链接目标。
# 函数用途: 比较预演或应用前后的保留数据内容。
def snapshot(root):
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and not p.is_symlink()
    }


# LLM: fixture专属临时根，所有apply必须显式传入它。
# 函数用途: 创建空白样本Home。
@pytest.fixture
def home(tmp_path):
    path = tmp_path / "home"
    path.mkdir()
    return path


# LLM: 五类资格由真实目录、locator和老mtime构成，备份同时保留报告控制。
# 函数用途: 构建已知数量的可删历史样本。
@pytest.fixture
def baggage(home):
    orphan = OWNER / "agents/orphan"
    write(
        home,
        orphan / "workspace_locator.json",
        json.dumps({"workspace_root": f"/var/folders/no-such-slimming-{os.getpid()}/gone"}),
    )
    write(home, orphan / "state.json", "{}")
    tmp = OWNER / "tasks" / OLD_DATE / "old/tmp"
    write(home, tmp / "pytest/a.pyc", "12345")
    (home / tmp / "empty/sub").mkdir(parents=True)
    workspace = OWNER / "workspace/runtime/workspaces/test-ws-old"
    write(home, workspace / "cache.bin", "1234567")
    archive = Path("archive-before-20261019/tasks") / OLD_DATE / "task/tmp"
    write(home, archive / "cache.bin", "123456789")
    for i in range(4):
        base = Path(f"releases/switch-{i}")
        write(home, base / "pre-switch-conversations.tar.gz", "tar")
        write(home, base / "runtime-before.db", "db")
        write(home, base / "runtime-before.db-wal", "wal")
        write(home, base / "RESULT.md", "保留报告")
        age_tree(home / base, 10 - i)
    for p in (orphan, tmp, workspace, archive):
        age_tree(home / p)
    return home


# LLM: 预演不能写样本；批准后成功计数必须与删除事实和保留对象一致。
# 函数用途: 验证五类别精确规划、执行、目录修剪和再次执行幂等。
def test_five_categories_plan_exact_files_then_apply(baggage):
    before = snapshot(baggage)
    manifest = cleanup.run_cleanup(baggage, now=NOW)
    assert snapshot(baggage) == before
    expected = [2, 1, 1, 6, 1]
    assert [manifest["categories"][k]["planned_files"] for k in CATEGORIES] == expected
    result = cleanup.run_cleanup(baggage, apply=True, now=NOW, approved_manifest=manifest)
    assert [result["categories"][k]["deleted_files"] for k in CATEGORIES] == expected
    for category in CATEGORIES:
        row = result["categories"][category]
        assert row["deleted_bytes"] == row["planned_bytes"]
    assert not (baggage / OWNER / "agents/orphan").exists()
    assert (baggage / OWNER / "tasks" / OLD_DATE / "old/tmp").is_dir()
    assert not (baggage / OWNER / "tasks" / OLD_DATE / "old/tmp/empty").exists()
    assert (baggage / "releases/switch-3/runtime-before.db").is_file()
    assert (baggage / "releases/switch-2/pre-switch-conversations.tar.gz").is_file()
    assert (baggage / "releases/switch-0/RESULT.md").read_text() == "保留报告"
    assert all(
        row["deleted_files"] == 0
        for row in cleanup.run_cleanup(baggage, apply=True, now=NOW)["categories"].values()
    )


# LLM: 保护名即便嵌入tmp也保留，预演与应用两入口均检查真实字节。
# 函数用途: 参数化覆盖人格、记忆、技能、交付物和源材料红线。
@pytest.mark.parametrize(
    "protected",
    [
        "output/video.mp4",
        "inputs/source.bin",
        "src/main.py",
        "RESULT.md",
        "FINAL_REPORT.md",
        "memories.jsonl",
        "AGENTS.md",
        "SOUL.md",
        "USER.md",
        "memory/long_term/a.jsonl",
        "skills/custom/SKILL.md",
        "conversations/messages/a.jsonl",
    ],
)
def test_protected_data_inside_scratch_is_untouched(home, protected):
    base = OWNER / "tasks" / OLD_DATE / "task/tmp"
    keep = write(home, base / protected, "唯一正式内容")
    junk = write(home, base / "junk.pyc")
    age_tree(home / base)
    before = keep.read_bytes()
    cleanup.run_cleanup(home, now=NOW)
    assert junk.exists() and keep.read_bytes() == before
    cleanup.run_cleanup(home, apply=True, now=NOW)
    assert not junk.exists() and keep.read_bytes() == before


# LLM: 年龄门包含空目录和文件，今天任务与非tmp根均不得删除。
# 函数用途: 验证当前活动范围及近期样本受到保护。
@pytest.mark.parametrize("kind", ["today", "recent-file", "recent-empty-dir", "outside-tmp"])
def test_active_or_non_tmp_tasks_are_preserved(home, kind):
    date = TODAY if kind == "today" else OLD_DATE
    base = OWNER / "tasks" / date / "task"
    path = write(home, base / ("output/result.bin" if kind == "outside-tmp" else "tmp/a.pyc"))
    age_tree(home / base)
    if kind == "recent-file":
        os.utime(path, (NOW, NOW))
    if kind == "recent-empty-dir":
        (home / base / "tmp/recent").mkdir()
    before = snapshot(home)
    cleanup.run_cleanup(home, apply=True, now=NOW)
    assert snapshot(home) == before


# LLM: 孤儿条件为AND；任一未证成立都保留整个投影。
# 函数用途: 覆盖目标仍在、定位器坏、非临时路径、近期和交付物等拒绝分支。
@pytest.mark.parametrize(
    "reason",
    [
        "exists",
        "inside-home",
        "missing-locator",
        "malformed",
        "non-temp",
        "recent",
        "deliverable",
        "output-dir",
        "dangling-target-link",
    ],
)
def test_agent_requires_all_orphan_conditions(home, tmp_path, reason):
    base = OWNER / "agents/keep"
    target = tmp_path / "existing"
    target.mkdir()
    root = f"/tmp/no-such-slimming-{os.getpid()}-x"
    if reason == "exists":
        root = str(target)
    if reason == "inside-home":
        root = str(home / "missing")
    if reason == "non-temp":
        root = "/unknown/missing"
    if reason == "dangling-target-link":
        link = tmp_path / "target-link"
        link.symlink_to(tmp_path / "missing")
        root = str(link)
    write(home, base / "state.json", "{}")
    if reason != "missing-locator":
        write(
            home,
            base / "workspace_locator.json",
            "{" if reason == "malformed" else json.dumps({"workspace_root": root}),
        )
    if reason == "deliverable":
        write(home, base / "RESULT.md")
    if reason == "output-dir":
        (home / base / "output").mkdir()
    age_tree(home / base, 0 if reason == "recent" else 10)
    before = snapshot(home)
    cleanup.run_cleanup(home, apply=True, now=NOW)
    assert (home / base).is_dir() and snapshot(home) == before


# LLM: workspace前缀仅是必要条件，当前配置和链接引用优先保留。
# 函数用途: 验证canonical、未知、近期及被引用的工作区不进入删除表。
@pytest.mark.parametrize("reason", ["canonical", "config", "symlink-ref", "recent", "unknown"])
def test_workspace_guards(home, reason):
    name = (
        "main-a4e68bb5ead1"
        if reason == "canonical"
        else "customer"
        if reason == "unknown"
        else "test-ws-keep"
    )
    base = OWNER / "workspace/runtime/workspaces" / name
    write(home, base / "cache.bin")
    age_tree(home / base, 0 if reason == "recent" else 10)
    if reason == "config":
        write(home, "config/desktop.yaml", f"workspace_id: {name}\n")
    if reason == "symlink-ref":
        (home / "current-workspace").symlink_to(home / base)
    before = snapshot(home)
    cleanup.run_cleanup(home, apply=True, now=NOW)
    assert snapshot(home) == before


# LLM: 旧归档只授权显式scratch/cache，输出和消息不能被递归扫描带走。
# 函数用途: 验证旧归档垃圾删除与正式内容保留并存。
def test_archive_only_scratch_not_reports_or_messages(home):
    root = Path("archive-before-20261019")
    junk = write(home, root / "tasks/d/task/work/x.cache")
    cache = write(home, root / "misc/__pycache__/x.pyc")
    for path in (
        "tasks/d/task/output/film.mp4",
        "tasks/d/task/work/RESULT.md",
        "conversations/messages/thread.jsonl",
        "memories.jsonl",
        "AGENTS.md",
    ):
        write(home, root / path, "正式")
    age_tree(home / root)
    keep = {p: b for p, b in snapshot(home).items() if not p.endswith(("x.cache", "x.pyc"))}
    cleanup.run_cleanup(home, apply=True, now=NOW)
    assert not junk.exists() and not cache.exists()
    assert snapshot(home) == keep


# LLM: 真实符号链接不跟随，多链接分支仅合成元数据，不能宣称物理硬链接通过。
# 函数用途: 验证父链逃逸和多链接普通文件均被保留。
def test_symlink_parent_and_hardlink_metadata_are_never_followed(home, tmp_path, monkeypatch):
    external = tmp_path / "outside"
    write(external, "a.pyc", "外部")
    tmp = home / OWNER / "tasks" / OLD_DATE / "task/tmp"
    tmp.parent.mkdir(parents=True)
    tmp.symlink_to(external, target_is_directory=True)
    other = write(home, OWNER / "tasks" / OLD_DATE / "other/tmp/a.pyc")
    linked = write(home, other.with_name("linked").relative_to(home), "多链接内容")
    age_tree(other.parent)
    original = cleanup.os.stat

    # LLM: 仅修改指定叶子的nlink观察，其余stat和mtime门保持真实。
    # 函数用途: 在不能建硬链接的环境覆盖多链接保留分支。
    def multiple_links(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if str(path) == "linked":
            values = list(info)
            values[3] = 2
            return os.stat_result(values)
        return info

    monkeypatch.setattr(cleanup.os, "stat", multiple_links)
    cleanup.run_cleanup(home, apply=True, now=NOW)
    assert (external / "a.pyc").read_text() == "外部"
    assert linked.exists() and not other.exists()


# LLM: 已审清单只收紧当前候选，不接受新增单元或变更内容。
# 函数用途: 验证预演后改写和新增的垃圾不被旧批准删除。
def test_apply_does_not_expand_approved_plan_or_accept_changed_files(baggage):
    approved = cleanup.run_cleanup(baggage, now=NOW)
    changed = baggage / OWNER / "tasks" / OLD_DATE / "old/tmp/pytest/a.pyc"
    changed.write_text("新内容")
    extra = write(baggage, OWNER / "tasks" / OLD_DATE / "extra/tmp/a.pyc")
    age_tree(extra.parent)
    cleanup.run_cleanup(baggage, apply=True, now=NOW, approved_manifest=approved)
    assert changed.read_text() == "新内容" and extra.exists()


# LLM: 非有限年龄、短保留期和不足两套备份都须先拒绝，不能通过输入放宽红线。
# 函数用途: 验证不安全策略参数不会执行。
@pytest.mark.parametrize(
    "kwargs",
    [
        {"keep_release_backups": 1},
        {"min_task_tmp_age_days": -1},
        {"min_task_tmp_age_days": float("nan")},
    ],
)
def test_unsafe_policy_is_rejected(home, kwargs):
    with pytest.raises(ValueError):
        cleanup.run_cleanup(home, **kwargs)


# LLM: 子进程使用真实CLI和当前脚本，清单只落外部临时路径。
# 函数用途: 验证默认预演/批准执行回执可解析且统计有真实删除。
def test_cli_manifest_and_dry_run_apply_are_auditable(baggage, tmp_path):
    dry = tmp_path / "dry.json"
    applied = tmp_path / "applied.json"
    before = snapshot(baggage)
    for flag, output in (("--dry-run", dry), ("--apply", applied)):
        args = [
            sys.executable,
            "-B",
            str(SCRIPT),
            "--home-dir",
            str(baggage),
            flag,
            "--manifest-out",
            str(output),
        ]
        if flag == "--apply":
            args += ["--approved-manifest", str(dry)]
        result = subprocess.run(args, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        manifest = json.loads(output.read_text())
        assert manifest["mode"] == flag[2:]
        if flag == "--dry-run":
            assert snapshot(baggage) == before
    assert any(row["deleted_files"] for row in manifest["categories"].values())


# LLM: 规划快照不能永久授予资格，首checkpoint后的配置/目标变化必须复核。
# 函数用途: 验证晚引用及目标复活都撤销当前单元删除资格。
@pytest.mark.parametrize("change", ["workspace-config", "orphan-target"])
def test_eligibility_rechecked_after_plan_before_delete(home, change):
    if change == "workspace-config":
        base = OWNER / "workspace/runtime/workspaces/test-ws-racing"
        victim = write(home, base / "cache.bin")
    else:
        base = OWNER / "agents/racing"
        target = home.parent / "late-target"
        write(home, base / "workspace_locator.json", json.dumps({"workspace_root": str(target)}))
        victim = write(home, base / "state.json", "{}")
    age_tree(home / base)
    touched = []

    # LLM: 只在首checkpoint执行真实新增操作，后续checkpoint无副作用。
    # 函数用途: 模拟规划后另一方恢复工作目标或配置引用。
    def before_delete(_manifest):
        if not touched:
            touched.append(True)
            if change == "workspace-config":
                write(home, "config/desktop.yaml", "workspace_id: test-ws-racing\n")
            else:
                target.mkdir()

    cleanup.run_cleanup(home, apply=True, now=NOW, checkpoint=before_delete)
    assert victim.exists(), "预演后的配置引用或目标复活必须撤销删除资格"


# LLM: 已有配置链接不可读不能被解释为“无引用”。
# 函数用途: 验证坏配置使工作区清理失败关闭。
def test_unreadable_configuration_fails_closed_for_workspaces(home):
    victim = write(home, OWNER / "workspace/runtime/workspaces/test-ws-old/cache.bin")
    age_tree(victim.parent)
    path = home / "config/desktop.yaml"
    path.parent.mkdir()
    path.symlink_to(home / "missing")
    cleanup.run_cleanup(home, apply=True, now=NOW)
    assert victim.exists()


# LLM: 配置父目录被链接替换也属覆盖未知，不能继续删workspace。
# 函数用途: 验证父配置目录链接不被当成空配置目录。
def test_configuration_parent_symlink_fails_closed(home, tmp_path):
    victim = write(home, OWNER / "workspace/runtime/workspaces/test-ws-old/cache.bin")
    age_tree(victim.parent)
    config = tmp_path / "external-config"
    config.mkdir()
    (home / "config").symlink_to(config)
    cleanup.run_cleanup(home, apply=True, now=NOW)
    assert victim.exists()


# LLM: 预演后最新两套移走会改变排名，不能继续删除新的保底集合。
# 函数用途: 验证执行前重新计算整套备份保留次序。
def test_backup_ranking_is_revalidated_after_plan(baggage, tmp_path):
    moved = []

    # LLM: 真实移动完整备份套组，单次动作避免checkpoint重复触发。
    # 函数用途: 模拟管理员在规划后搬走最新两套备份。
    def after_plan(_manifest):
        if not moved:
            moved.append(True)
            for name in ("switch-3", "switch-2"):
                (baggage / "releases" / name).rename(tmp_path / name)

    report = cleanup.run_cleanup(baggage, apply=True, now=NOW, checkpoint=after_plan)
    assert report["categories"]["old_release_switch_backups"]["deleted_files"] == 0
    for name in ("switch-1", "switch-0"):
        assert (baggage / "releases" / name / "runtime-before.db").exists()
        assert (baggage / "releases" / name / "pre-switch-conversations.tar.gz").exists()


# LLM: 预先存在和Home内输出均不允许，检查CLI非零及旧文件内容。
# 函数用途: 验证清单启动参数的基础写入边界。
def test_cli_rejects_existing_manifest_and_in_home_manifest(home, tmp_path):
    for destination in (write(tmp_path, "keep.json", "原件"), home / "new.json"):
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(SCRIPT),
                "--home-dir",
                str(home),
                "--manifest-out",
                str(destination),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2
    assert (tmp_path / "keep.json").read_text() == "原件"


# LLM: 包装真实最后stat，仅在_delete_unit活栈内交错真实替换/迁移；不伪造stat或删除结果。
# 函数用途: 在删除器最后核验之后触发一次可重复的并发文件系统动作。
def race_after_delete_stat(monkeypatch, action):
    original_delete, original_stat = cleanup._delete_unit, cleanup.os.stat
    state = {"active": False, "fired": False}

    # LLM: 保留真实删除实现和异常，只标记其调用寿命。
    # 函数用途: 限定注入只能发生在删除阶段而非预演扫描。
    def deleting(*args, **kwargs):
        state["active"] = True
        try:
            return original_delete(*args, **kwargs)
        finally:
            state["active"] = False

    # LLM: 先取真实stat再交错，返回旧观察以覆盖最终check/use窗口。
    # 函数用途: 只对leaf.bin执行一次外部写者动作。
    def racing_stat(path, *args, **kwargs):
        value = original_stat(path, *args, **kwargs)
        if state["active"] and not state["fired"] and str(path) == "leaf.bin":
            state["fired"] = True
            action()
        return value

    monkeypatch.setattr(cleanup, "_delete_unit", deleting)
    monkeypatch.setattr(cleanup.os, "stat", racing_stat)
    return state


# LLM: 来源双审F1；批准清单始终由真实预演生成，不靠省略审批制造误删。
# 函数用途: 最后stat后换入新inode时必须保留新文件并明确报错。
def test_leaf_replacement_after_last_stat_is_not_deleted(home, tmp_path, monkeypatch):
    victim = write(home, OWNER / "tasks" / OLD_DATE / "task/tmp/leaf.bin", "old")
    age_tree(victim.parent)
    approved = cleanup.run_cleanup(home, now=NOW)
    replacement = write(tmp_path, "replacement", "unapproved-new-file")
    assert replacement.stat().st_ino != victim.stat().st_ino
    state = race_after_delete_stat(monkeypatch, lambda: os.replace(replacement, victim))
    result = cleanup.run_cleanup(home, apply=True, now=NOW, approved_manifest=approved)
    assert state["fired"]
    assert victim.exists(), "未批准新inode不能被删除"
    assert victim.read_text() == "unapproved-new-file"
    assert result["errors"] and result["categories"][CATEGORIES[1]]["deleted_files"] == 0


# LLM: 来源双审F1；真实rename整个已打开父目录，原tmp再建新目录以捕获只验证路径存在的伪修复。
# 函数用途: tmp迁到output后禁止删除交付区里的叶子。
def test_open_parent_moved_to_output_stops_deletion(home, monkeypatch):
    victim = write(home, OWNER / "tasks" / OLD_DATE / "task/tmp/leaf.bin", "keep")
    saved = victim.parent.parent / "output/saved"
    saved.parent.mkdir()
    age_tree(victim.parent)
    approved = cleanup.run_cleanup(home, now=NOW)

    # LLM: 真实迁移目录并在原处建替代对象，不改产品观察。
    # 函数用途: 模拟另一个整理者把临时文件提升为交付内容。
    def move_parent():
        victim.parent.rename(saved)
        victim.parent.mkdir()

    state = race_after_delete_stat(monkeypatch, move_parent)
    result = cleanup.run_cleanup(home, apply=True, now=NOW, approved_manifest=approved)
    assert state["fired"]
    assert (saved / victim.name).exists(), "output中的对象不能被删除"
    assert (saved / victim.name).read_text() == "keep"
    assert result["errors"] and result["categories"][CATEGORIES[1]]["deleted_files"] == 0


# LLM: 来源双审F2；在CLI进入run_cleanup时改输出路径，保留真实写入链；不接受错误回执掩盖文件损坏。
# 函数用途: 检查清单晚出现或父目录换链接均不覆盖其他文件。
@pytest.mark.parametrize("race", ["late-existing-output", "manifest-parent-symlink"])
def test_manifest_destination_remains_exclusive_and_outside_home(home, tmp_path, monkeypatch, race):
    protected = write(home, "persona/AGENTS.md", "formal-persona")
    reports = tmp_path / "reports"
    reports.mkdir()
    output = reports / "AGENTS.md"
    original = cleanup.run_cleanup

    # LLM: 新版先预留输出身份，外部写者先移走该对象再新建名字；不借共享FD原地写覆盖来伪造替换。
    # 函数用途: 在首checkpoint之前注入真实目录或文件替换。
    def after_validation(*args, **kwargs):
        if race == "late-existing-output":
            if output.exists():
                output.rename(reports / "reserved")
            output.write_text("other-writer-report")
        else:
            reports.rename(tmp_path / "reports-original")
            reports.symlink_to(protected.parent, target_is_directory=True)
        return original(*args, **kwargs)

    monkeypatch.setattr(cleanup, "run_cleanup", after_validation)
    status = cleanup.main(["--home-dir", str(home), "--dry-run", "--manifest-out", str(output)])
    assert protected.read_text() == "formal-persona"
    if race == "late-existing-output":
        assert output.read_text() == "other-writer-report"
    assert status != 0


# LLM: 来源双审F3；两入口相同临时目标与相同checkpoint，对归档也必须撤销孤儿资格。
# 函数用途: 目标复活后普通和归档投影都保留，回执0删除。
@pytest.mark.parametrize("archived", [False, True])
def test_orphan_target_reappearing_at_checkpoint_revokes_eligibility(home, tmp_path, archived):
    base = (Path("archive-before-20261019") if archived else OWNER) / "agents/orphan"
    target = tmp_path / "workspace-returned"
    write(home, base / "workspace_locator.json", json.dumps({"workspace_root": str(target)}))
    victim = write(home, base / "state.json", "{}")
    age_tree(home / base)
    approved = cleanup.run_cleanup(home, now=NOW)
    assert len(approved["candidates"]) == 1

    # LLM: 只改变候选之外的目标存在性，不改变投影摘要。
    # 函数用途: 在第一次执行checkpoint恢复工作目录。
    def revive(_manifest):
        target.mkdir(exist_ok=True)

    result = cleanup.run_cleanup(home, apply=True, now=NOW, approved_manifest=approved, checkpoint=revive)
    assert victim.exists(), "恢复目标后不可继续按孤儿删除"
    assert result["candidates"][0]["status"] == "skipped_target_reappeared"
    assert sum(row["deleted_files"] for row in result["categories"].values()) == 0


# LLM: 归档通用缓存入口不能绕过固定tasks的当天红线，两种日期格式共同验证。
# 函数用途: 保留归档当日任务中的老缓存。
@pytest.mark.parametrize("compact", [False, True])
def test_archive_today_cache_is_excluded(home, compact):
    date = TODAY.replace("-", "") if compact else TODAY
    victim = write(home, Path("archive-before-20261019/tasks") / date / "task/__pycache__/x.pyc")
    age_tree(home / "archive-before-20261019")
    result = cleanup.run_cleanup(home, apply=True, now=NOW)
    assert victim.exists()
    assert not result["candidates"]


# LLM: Astra-2 F4来源；环境选中外部配置是产品正式入口，不可用固定目录缺引用推导不活跃。
# 函数用途: 外部配置引用和不可读的已选配置都必须保留workspace。
@pytest.mark.parametrize("readable", [False, True])
def test_selected_external_configuration_protects_workspace(home, tmp_path, monkeypatch, readable):
    victim = write(home, OWNER / "workspace/runtime/workspaces/test-ws-live/local_store/local.db")
    age_tree(home / OWNER / "workspace/runtime/workspaces")
    config = tmp_path / "active.yaml"
    if readable:
        config.write_text("local_store_path: " + str(victim) + "\n")
    monkeypatch.setenv("MY_AGENT_CONFIG", str(config))
    result = cleanup.run_cleanup(home, apply=True, now=NOW)
    assert victim.exists()
    assert not result["candidates"]


# LLM: 覆盖隔离rename本身的间隙，不只在更早stat交错；恢复冲突必须保留隔离内容和原名的新对象。
# 函数用途: 验证移出后核验及内核no-replace恢复，包含output迁移与双写者冲突。
@pytest.mark.parametrize("race", ["replace", "parent-move", "restore-conflict"])
def test_quarantine_post_move_validation_and_nonclobber_recovery(home, tmp_path, monkeypatch, race):
    victim = write(home, OWNER / "tasks" / OLD_DATE / "task/tmp/leaf.bin", "old")
    saved = victim.parent.parent / "output/saved"
    saved.parent.mkdir()
    replacement = write(tmp_path, "replacement", "unapproved")
    age_tree(victim.parent)
    approved = cleanup.run_cleanup(home, now=NOW)
    original_rename = cleanup.os.rename
    fired = []

    # LLM: 真实rename前替换/移动，rename后冲突只创建新原名，不直接改产品判断。
    # 函数用途: 将竞态固定在隔离转移系统调用的边界。
    def racing_rename(source, destination, *args, **kwargs):
        if str(source) != "leaf.bin" or fired:
            return original_rename(source, destination, *args, **kwargs)
        fired.append(True)
        if race == "parent-move":
            victim.parent.rename(saved)
            victim.parent.mkdir()
        else:
            os.replace(replacement, victim)
        result = original_rename(source, destination, *args, **kwargs)
        if race == "restore-conflict":
            victim.write_text("new-name-owner")
        return result

    monkeypatch.setattr(cleanup.os, "rename", racing_rename)
    report = cleanup.run_cleanup(home, apply=True, now=NOW, approved_manifest=approved)
    assert fired and report["errors"]
    assert report["categories"][CATEGORIES[1]]["deleted_files"] == 0
    if race == "parent-move":
        assert (saved / victim.name).read_text() == "old"
    elif race == "replace":
        assert victim.read_text() == "unapproved"
    else:
        assert victim.read_text() == "new-name-owner"
        recovery = report["candidates"][0]["recovery_required"]
        assert len(recovery) == 1
        assert Path(recovery[0]["quarantine"]).read_text() == "unapproved"
        assert report["errors"][0]["code"] == "restore_conflict_preserved_in_quarantine"


# LLM: 目录隔离后也须复查为空，不能rmtree晚出现的正式数据；恢复原目录身份。
# 函数用途: 在空目录隔离后新增报告，验证它不被删除且恢复原处。
def test_quarantined_directory_gaining_report_is_restored(home, monkeypatch):
    base = home / OWNER / "tasks" / OLD_DATE / "task/tmp"
    empty = base / "empty"
    empty.mkdir(parents=True)
    age_tree(base)
    approved = cleanup.run_cleanup(home, now=NOW)
    original_rename = cleanup.os.rename
    fired = []

    # LLM: 先真实移出，再经隔离FD创建报告，验证后核而非伪造异常。
    # 函数用途: 模拟最后空目录检查之后出现新文件。
    def racing_rename(source, destination, *args, **kwargs):
        result = original_rename(source, destination, *args, **kwargs)
        if str(source) == "empty" and not fired:
            fired.append(True)
            directory = os.open(destination, os.O_RDONLY | os.O_DIRECTORY, dir_fd=kwargs["dst_dir_fd"])
            try:
                fd = os.open("RESULT.md", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
                with os.fdopen(fd, "w") as stream:
                    stream.write("keep-result")
            finally:
                os.close(directory)
        return result

    monkeypatch.setattr(cleanup.os, "rename", racing_rename)
    report = cleanup.run_cleanup(home, apply=True, now=NOW, approved_manifest=approved)
    assert fired and report["errors"]
    assert (empty / "RESULT.md").read_text() == "keep-result"
    assert report["categories"][CATEGORIES[1]]["deleted_directories"] == 0


# LLM: 不依赖环境继承，active_configs显式选择外部入口；坏配置禁止删除，正常配置按实际引用收紧。
# 函数用途: 验证库接口可接入调用者实际使用的配置路径。
def test_explicit_active_configuration_protects_workspace(home, tmp_path):
    victim = write(home, OWNER / "workspace/runtime/workspaces/test-ws-explicit/local_store/local.db")
    age_tree(victim.parent.parent)
    config = write(tmp_path, "external.yaml", "local_store_path: " + str(victim))
    report = cleanup.run_cleanup(home, apply=True, now=NOW, active_configs=[config])
    assert victim.exists() and not report["candidates"]


# LLM: 不可依赖操作系统fd计数接口；捕获本次成功open并在故障后直接fstat验证均已关闭。
# 函数用途: 完整祖先链首次fstat失败也不能泄漏已打开FD。
def test_open_chain_releases_fd_on_initial_fstat_error(home, monkeypatch):
    opened = []
    original_open, original_fstat = cleanup.os.open, cleanup.os.fstat

    # LLM: 记录真实open返回值，不改变FD语义。
    # 函数用途: 保存应在退出后关闭的目录句柄。
    def tracked_open(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        opened.append(fd)
        return fd

    # LLM: 仅对本次目录FD注入EIO，其他FD保持真实。
    # 函数用途: 重现打开根目录后首次元数据读取异常。
    def broken_fstat(fd):
        if fd in opened:
            raise OSError(5, "injected EIO")
        return original_fstat(fd)

    monkeypatch.setattr(cleanup.os, "open", tracked_open)
    monkeypatch.setattr(cleanup.os, "fstat", broken_fstat)
    with pytest.raises(OSError), cleanup._open_chain(home):
        pytest.fail("故障不能被吞掉")
    assert opened
    for fd in opened:
        with pytest.raises(OSError):
            original_fstat(fd)


# LLM: 独审指出相对路径在产品中以运行根解析；先调用真实解析器证明引用，而不是仅匹配配置文字。
# 函数用途: 相对运行根不明或绝对父根覆盖时，workspace删除都须失败关闭。
@pytest.mark.parametrize("value", ["relative", "absolute-parent"])
def test_active_runtime_path_semantics_preserve_workspace(home, tmp_path, monkeypatch, value):
    from types import SimpleNamespace

    from agent_py_agent.agent.user_space.runtime_paths import resolve_runtime_paths_for_agent

    workspace = home / OWNER / "workspace/runtime/workspaces/test-ws-relative"
    victim = write(workspace, "local_store/local.db", "active-database")
    age_tree(workspace)
    raw = "local_store/local.db" if value == "relative" else str(workspace.parent)
    config = write(tmp_path, "selected.yaml", "local_store_path: " + raw + "\n")
    monkeypatch.setenv("MY_AGENT_CONFIG", str(config))
    resolved = resolve_runtime_paths_for_agent(
        SimpleNamespace(local_store_path=raw), workspace,
        SimpleNamespace(owner_home_dir=home / OWNER),
    ).paths["local_store_path"]
    assert resolved == (victim if value == "relative" else workspace.parent)
    approved = cleanup.run_cleanup(home, now=NOW)
    report = cleanup.run_cleanup(home, apply=True, now=NOW, approved_manifest=approved)
    assert victim.exists(), "可读配置不代表原文未匹配的workspace已不活动"
    assert victim.read_text() == "active-database"
    assert report["categories"][CATEGORIES[2]]["deleted_files"] == 0


# LLM: R2-B1在第二次真实移出后的核验点注入故障，要求整单元零删除并逆序恢复，冲突分支保留双方。
# 函数用途: 防止第一条已删后第二条失败的半套清理。
@pytest.mark.parametrize("conflict", [False, True])
def test_batch_second_verification_failure_restores_all_without_clobber(home, monkeypatch, conflict):
    first = write(home, OWNER / "tasks" / OLD_DATE / "batch/tmp/a.bin", "first")
    second = write(home, first.with_name("b.bin").relative_to(home), "second")
    age_tree(first.parent)
    original_verify, original_restore = cleanup._verify_quarantined, cleanup._restore_entry
    verified, restored = [], []

    # LLM: 仅第二个移出文件核验失败，保留真实第一条检查及真实内核恢复。
    # 函数用途: 在物理删除屏障前制造后继条目故障。
    def fail_second(fd, name, expected):
        verified.append(name)
        if len(verified) == 2:
            if conflict:
                first.write_text("new-owner")
            raise cleanup.SafetyError("injected_second_verification_failure")
        return original_verify(fd, name, expected)

    # LLM: 只记录真实恢复的目标名，不改变系统调用成败。
    # 函数用途: 核实恢复按逆序发生。
    def record_restore(source_fd, source, destination_fd, destination):
        restored.append(str(destination))
        return original_restore(source_fd, source, destination_fd, destination)

    monkeypatch.setattr(cleanup, "_verify_quarantined", fail_second)
    monkeypatch.setattr(cleanup, "_restore_entry", record_restore)
    report = cleanup.run_cleanup(home, apply=True, now=NOW)
    assert report["errors"] and len(verified) == 2
    assert restored == ["b.bin", "a.bin"]
    assert report["categories"][CATEGORIES[1]]["deleted_files"] == 0
    assert second.read_text() == "second"
    if conflict:
        assert first.read_text() == "new-owner"
        recovery = report["candidates"][0]["recovery_required"]
        assert len(recovery) == 1
        assert Path(recovery[0]["quarantine"]).read_text() == "first"
    else:
        assert first.read_text() == "first"
        assert not list(home.parent.glob(".slimming-quarantine-*"))


# LLM: R2-B1用真实文件、目录、JSON和fsync观察第一条物理删除屏障，不把callback调用当持久化。
# 函数用途: 确认整单元隔离映射持久化后才开始删除。
def test_batch_mapping_is_fsynced_before_any_physical_delete(home, monkeypatch):
    first = write(home, OWNER / "tasks" / OLD_DATE / "batch/tmp/nested/a.bin", "first")
    second = write(home, first.with_name("b.bin").relative_to(home), "second")
    age_tree(first.parent.parent)
    original_sync, original_unlink = cleanup.os.fsync, cleanup.os.unlink
    synced, observed = [], []

    # LLM: 调用真实fsync，完成后才记录inode；目录同步和文件同步都不省略。
    # 函数用途: 为屏障断言保留持久化观察点。
    def synced_fd(fd):
        original_sync(fd)
        synced.append(os.fstat(fd).st_ino)

    # LLM: 第一个目标unlink之前读真实隔离清单，排除清单本身的收尾删除。
    # 函数用途: 验证映射身份、祖先和全体源移出事实早于物理删除。
    def guarded_unlink(path, *args, **kwargs):
        if str(path) != "manifest.json" and not observed:
            manifests = list(home.parent.glob(".slimming-quarantine-*/manifest.json"))
            assert len(manifests) == 1, "删除之前必须存在持久隔离清单"
            journal = manifests[0]
            mapping = json.loads(journal.read_text())
            assert journal.stat().st_ino in synced
            assert mapping["ancestors"] and len(mapping["entries"]) == 3
            assert not first.exists() and not second.exists()
            for entry in mapping["entries"]:
                assert entry["source"] and len(entry["identity"]) == 7
                assert (journal.parent / entry["quarantine_name"]).exists()
            observed.append(mapping)
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(cleanup.os, "fsync", synced_fd)
    monkeypatch.setattr(cleanup.os, "unlink", guarded_unlink)
    report = cleanup.run_cleanup(home, apply=True, now=NOW)
    assert observed and not report["errors"]
    assert report["categories"][CATEGORIES[1]]["deleted_files"] == 2
    assert not list(home.parent.glob(".slimming-quarantine-*"))


# LLM: 清单落盘与可选checkpoint任一失败均在物理屏障之前；故障仅注入一次以核真实恢复。
# 函数用途: 证明嵌套目录与所有文件都能因持久化故障一起回滚。
@pytest.mark.parametrize("failure", ["fsync", "checkpoint"])
def test_batch_persistence_failure_rolls_back_nested_tree(home, monkeypatch, failure):
    import stat

    first = write(home, OWNER / "tasks" / OLD_DATE / "batch/tmp/nested/a.bin", "first")
    write(home, first.with_name("b.bin").relative_to(home), "second")
    age_tree(first.parent.parent)
    before = snapshot(home)
    original_sync = cleanup.os.fsync
    fired = []

    # LLM: 只对隔离清单文件的首次同步注入EIO，不伪造恢复目录内容。
    # 函数用途: 重现写出映射但尚未持久化时的故障。
    def broken_sync(fd):
        if failure == "fsync" and not fired and stat.S_ISREG(os.fstat(fd).st_mode):
            fired.append(True)
            raise OSError(5, "injected journal fsync")
        return original_sync(fd)

    # LLM: 仅在所有源路径已移出时拒绝checkpoint，初始及错误回执保持正常。
    # 函数用途: 验证外部清单提交失败同样触发逆序恢复。
    def broken_checkpoint(_manifest):
        if failure == "checkpoint" and not first.exists() and not fired:
            fired.append(True)
            raise OSError(5, "injected checkpoint")

    monkeypatch.setattr(cleanup.os, "fsync", broken_sync)
    report = cleanup.run_cleanup(home, apply=True, now=NOW, checkpoint=broken_checkpoint)
    assert fired and report["errors"]
    assert snapshot(home) == before
    counts = report["categories"][CATEGORIES[1]]
    assert counts["deleted_files"] == counts["deleted_directories"] == 0
    assert not list(home.parent.glob(".slimming-quarantine-*"))


# LLM: R2-B2通过真实第二个open描述符验证同inode互斥，覆盖整个run_cleanup和save临界区。
# 函数用途: 扫描开始前已经锁住清单，退出后其它句柄才可拿锁。
def test_manifest_lock_covers_scan_and_save(home, tmp_path, monkeypatch):
    import fcntl

    output = tmp_path / "locked.json"
    original_run, original_save = cleanup.run_cleanup, cleanup.ManifestOutput.save
    checked = []

    # LLM: 独立open产生不同文件描述，真实LOCK_NB必须失败；只释放本探针FD。
    # 函数用途: 证明目标inode被当前运行持锁。
    def assert_locked():
        with output.open("rb") as other:
            with pytest.raises(BlockingIOError):
                fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        checked.append(True)

    # LLM: 包装真实规划入口，只在入口前加锁观察，不替代实现。
    # 函数用途: 检查扫描开始时的锁状态。
    def checked_run(*args, **kwargs):
        assert_locked()
        return original_run(*args, **kwargs)

    # LLM: 保存前后均独立竞争文件锁，不能提前解锁再做末次fsync。
    # 函数用途: 检查所有checkpoint仍处于同一锁临界区。
    def checked_save(self, payload):
        assert_locked()
        result = original_save(self, payload)
        assert_locked()
        return result

    monkeypatch.setattr(cleanup, "run_cleanup", checked_run)
    monkeypatch.setattr(cleanup.ManifestOutput, "save", checked_save)
    assert cleanup.main(["--home-dir", str(home), "--manifest-out", str(output)]) == 0
    assert len(checked) >= 3
    with output.open("rb") as other:
        fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(other.fileno(), fcntl.LOCK_UN)


# LLM: 用独立句柄抢先锁住刚出生的输出，验证获取失败/初始化异常/正常退出的关闭顺序，不mock flock成功。
# 函数用途: 清单FD的所有出口均无泄漏，拿到锁的出口先解锁后关闭。
@pytest.mark.parametrize("exit_kind", ["conflict", "initialize", "normal"])
def test_manifest_lock_release_order_and_no_fd_leak(home, tmp_path, monkeypatch, exit_kind):
    import fcntl

    output = tmp_path / "lifecycle.json"
    original_open, original_close = cleanup.os.open, cleanup.os.close
    original_flock, original_init = fcntl.flock, cleanup.ManifestOutput.__init__
    target, events, competitor = [], [], []

    # LLM: 保持O_EXCL和nofollow真实创建，只在返回前用第二个句柄取得竞争锁。
    # 函数用途: 将锁冲突固定在产品open与flock之间。
    def tracked_open(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        if str(path) == output.name:
            target.append(fd)
            if exit_kind == "conflict":
                other = original_open(output, os.O_RDONLY)
                competitor.append(other)
                original_flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd

    # LLM: 原样转发系统锁，只记录清单FD的真实调用次序。
    # 函数用途: 核实异常和正常退出的LOCK_UN。
    def tracked_flock(fd, mode):
        if fd in target:
            events.append(("lock", mode))
        return original_flock(fd, mode)

    # LLM: 原样关闭所有FD，只跟踪本次清单FD而非祖先链临时描述符。
    # 函数用途: 核实close不能早于unlock。
    def tracked_close(fd):
        if fd in target:
            events.append(("close", None))
        return original_close(fd)

    # LLM: 在拿锁之后的初始化位置失败，不在mock中替产品关闭句柄。
    # 函数用途: 检查构造异常的清理责任。
    def broken_init(self, *args, **kwargs):
        if exit_kind == "initialize":
            raise ValueError("injected initialization")
        return original_init(self, *args, **kwargs)

    monkeypatch.setattr(cleanup.os, "open", tracked_open)
    monkeypatch.setattr(cleanup.os, "close", tracked_close)
    monkeypatch.setattr(fcntl, "flock", tracked_flock)
    monkeypatch.setattr(cleanup.ManifestOutput, "__init__", broken_init)
    try:
        if exit_kind == "normal":
            with cleanup._manifest_output(home, output) as destination:
                destination.save({"test": "normal"})
        else:
            with pytest.raises((OSError, ValueError)), cleanup._manifest_output(home, output):
                pytest.fail("并发持锁或初始化失败必须拒绝")
    finally:
        for fd in competitor:
            original_flock(fd, fcntl.LOCK_UN)
            original_close(fd)
    assert len(target) == 1
    with pytest.raises(OSError):
        os.fstat(target[0])
    assert events[0] == ("lock", fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert events[-1] == ("close", None)
    if exit_kind != "conflict":
        assert events[-2] == ("lock", fcntl.LOCK_UN)


# LLM: R2-B3引用只给workspace父路径，原文不存在候选名，必须实际递归解析；JSON/YAML均走真实读取。
# 函数用途: 嵌套字典、列表和路径配置节不能漏掉活动引用。
@pytest.mark.parametrize("shape", ["json-list", "yaml-list", "paths-section"])
def test_nested_active_config_paths_protect_workspace(home, tmp_path, shape):
    workspace = home / OWNER / "workspace/runtime/workspaces/test-ws-nested"
    victim = write(workspace, "cache.bin", "active")
    age_tree(workspace)
    parent = str(workspace.parent)
    if shape == "json-list":
        text = json.dumps({"services": [{"nested": {"storage_root": parent}}]})
    elif shape == "yaml-list":
        text = "services:\n  - nested:\n      storage_home: " + parent + "\n"
    else:
        text = "services:\n  paths:\n    database: " + parent + "\n"
    config = write(tmp_path, "nested.yaml", text)
    result = cleanup.run_cleanup(home, apply=True, now=NOW, active_configs=[config])
    assert victim.read_text() == "active"
    assert result["categories"][CATEGORIES[2]]["deleted_files"] == 0


# LLM: 内联结构沿产品标准库literal语法，递归tuple/list/dict中的路径均须被提取。
# 函数用途: 防止只遍历最外层映射或只支持单一容器。
def test_recursive_config_references_include_tuple_and_path_sections(tmp_path):
    value = str(tmp_path.resolve())
    text = "outer: " + repr({"seq": ({"cache_home": value}, [{"paths": {"database": value}}])})
    refs, known = cleanup._config_references(text)
    assert known and Path(value) in refs


# LLM: 损坏/非映射/未知语法不能降级为只跳workspace；真实tmp垃圾同样必须零删除且ValueError直接传播。
# 函数用途: 对环境和显式两种选择入口验证全局失败关闭。
@pytest.mark.parametrize("source", ["environment", "explicit"])
@pytest.mark.parametrize("text", ["", "# only comment\n", "[]", '"scalar"',
                                  'cache_path: "unclosed', "illegal line", "outer:\n  bad line",
                                  "a: 1\na: 2\n", "outer:\n\tbad_path: /tmp/active"])
def test_bad_selected_config_aborts_all_cleanup(home, tmp_path, monkeypatch, source, text):
    victim = write(home, OWNER / "tasks" / OLD_DATE / "bad-config/tmp/leaf.bin", "keep")
    age_tree(victim.parent)
    config = write(tmp_path, "bad.yaml", text)
    kwargs = {"active_configs": [config]} if source == "explicit" else {}
    if source == "environment":
        monkeypatch.setenv("MY_AGENT_CONFIG", str(config))
    with pytest.raises(ValueError):
        cleanup.run_cleanup(home, apply=True, now=NOW, **kwargs)
    assert victim.read_text() == "keep"
    assert not list(home.parent.glob(".slimming-quarantine-*"))


# LLM: 以真实存在但不能nofollow读取的选中配置检验失败，不把不存在路径混同语法错误。
# 函数用途: 活动配置读取拒绝必须全局中止，不能继续清理tmp。
def test_unreadable_selected_config_aborts_all_cleanup(home, tmp_path):
    victim = write(home, OWNER / "tasks" / OLD_DATE / "bad-config/tmp/leaf.bin")
    age_tree(victim.parent)
    config = tmp_path / "selected.yaml"
    config.symlink_to(write(tmp_path, "target.yaml", "name: active\n"))
    with pytest.raises(ValueError):
        cleanup.run_cleanup(home, apply=True, now=NOW, active_configs=[config])
    assert victim.exists()


# LLM: 初始规划之后、第一删除之前配置变坏，fresh Planner的ValueError也不得被逐单元catch吞掉。
# 函数用途: 确认坏配置晚出现时立即终止整个apply。
def test_selected_config_becoming_invalid_aborts_before_first_delete(home, tmp_path):
    victim = write(home, OWNER / "tasks" / OLD_DATE / "bad-config/tmp/leaf.bin")
    age_tree(victim.parent)
    config = write(tmp_path, "selected.yaml", "name: active\n")

    # LLM: 仅修改指定临时配置，不改变候选本身。
    # 函数用途: 在规划checkpoint制造损坏配置。
    def corrupt(_manifest):
        config.write_text('name: "unclosed')

    with pytest.raises(ValueError):
        cleanup.run_cleanup(home, apply=True, now=NOW, active_configs=[config], checkpoint=corrupt)
    assert victim.exists()


# LLM: R2-B4默认输出和批准读取须使用相同结构化指纹，不拿无候选空清单验证格式。
# 函数用途: 验证新schema及字典指纹写出后能真正批准删除。
def test_manifest_new_schema_and_structured_fingerprint_roundtrip(home):
    victim = write(home, OWNER / "tasks" / OLD_DATE / "schema/tmp/leaf.bin")
    age_tree(victim.parent)
    approved = cleanup.run_cleanup(home, now=NOW)
    assert approved["schema"] == "my-agent.home_file_slimming_cleanup_manifest.v1"
    assert isinstance(approved["candidates"][0]["fingerprint"], dict)
    result = cleanup.run_cleanup(home, apply=True, now=NOW, approved_manifest=approved)
    assert not victim.exists() and not result["errors"]


# LLM: Legacy无schema只允许必需字段与列表别名；旧schema名兼容但不绕过版本/策略/指纹校验。
# 函数用途: 两个schema、两种列表名字及严格legacy都保留正向批准路径。
@pytest.mark.parametrize("schema", [None, "home-file-slimming.v1", "my-agent.home_file_slimming_cleanup_manifest.v1"])
@pytest.mark.parametrize("field", ["candidates", "entries"])
def test_manifest_supported_schema_and_legacy_alias_apply(home, schema, field):
    victim = write(home, OWNER / "tasks" / OLD_DATE / "schema/tmp/leaf.bin")
    age_tree(victim.parent)
    original = cleanup.run_cleanup(home, now=NOW)
    approved = {k: original[k] for k in ("home", "policy", "script_sha256", "mode")}
    approved[field] = original["candidates"]
    if schema is not None:
        approved["schema"] = schema
    result = cleanup.run_cleanup(home, apply=True, now=NOW, approved_manifest=approved)
    assert not victim.exists() and not result["errors"]


# LLM: 畸形清单必须统一ValueError而非KeyError/TypeError，当前真实候选始终保留；禁止只靠集合交集掩盖坏格式。
# 函数用途: 覆盖schema、legacy顶层、别名容器和每个元素的严格输入形状。
@pytest.mark.parametrize("bad", ["root-list", "schema", "schema-type", "unknown-key", "missing-field",
                                 "candidates-type", "entries-type", "entry-type", "path-type", "path-empty",
                                 "path-absolute", "path-traversal", "category-type", "category-unknown",
                                 "fingerprint-type", "fingerprint-empty", "aliases-disagree"])
def test_manifest_malformed_input_is_rejected_without_delete(home, bad):
    import copy

    victim = write(home, OWNER / "tasks" / OLD_DATE / "schema/tmp/leaf.bin")
    age_tree(victim.parent)
    original = cleanup.run_cleanup(home, now=NOW)
    approved = copy.deepcopy(original)
    entry = approved["candidates"][0]
    if bad == "root-list":
        approved = []
    elif bad == "schema":
        approved["schema"] = "foreign-tool.v1"
    elif bad == "schema-type":
        approved["schema"] = []
    elif bad == "unknown-key":
        approved = {k: approved[k] for k in ("home", "policy", "script_sha256", "mode", "candidates")}
        approved["delete_all"] = True
    elif bad == "missing-field":
        approved.pop("mode")
    elif bad == "candidates-type":
        approved["candidates"] = {}
    elif bad == "entries-type":
        approved["entries"] = "not a list"
    elif bad == "entry-type":
        approved["candidates"] = ["not an entry"]
    elif bad.startswith("path-"):
        entry["path"] = {"path-type": 1, "path-empty": "", "path-absolute": "/tmp/other",
                         "path-traversal": "a/../other"}[bad]
    elif bad.startswith("category-"):
        entry["category"] = 1 if bad == "category-type" else "foreign-category"
    elif bad.startswith("fingerprint-"):
        entry["fingerprint"] = "flat-hash" if bad == "fingerprint-type" else {}
    else:
        approved["entries"] = []
    with pytest.raises(ValueError):
        cleanup.run_cleanup(home, apply=True, now=NOW, approved_manifest=approved)
    assert victim.exists()


# LLM: 独审P1：显式JSON null不能等同未提供批准；分别通过真实CLI和库参数验证拒绝且不删除。
# 函数用途: 防止空批准绕过根节点和批准集合限制。
@pytest.mark.parametrize("entrypoint", ["cli", "library"])
def test_explicit_null_approval_is_rejected(home, tmp_path, entrypoint):
    victim = write(home, OWNER / "tasks" / OLD_DATE / "null-approval/tmp/leaf.bin", "keep")
    age_tree(victim.parent)
    if entrypoint == "cli":
        approved = write(tmp_path, "approved.json", "null")
        result = subprocess.run([
            sys.executable, "-B", str(SCRIPT), "--home-dir", str(home), "--apply",
            "--approved-manifest", str(approved), "--manifest-out", str(tmp_path / "out.json"),
        ], capture_output=True, text=True)
        assert result.returncode == 2, result.stdout + result.stderr
    else:
        with pytest.raises(ValueError):
            cleanup.run_cleanup(home, apply=True, now=NOW, approved_manifest=None)
    assert victim.read_text() == "keep"


# LLM: 独审P2：prepared不等同已删除；首个物理删除前异常须恢复，已删一条后的失败则明确保留残余恢复记录。
# 函数用途: 验证实际物理删除事实才是回滚屏障，含原位晚占用冲突。
@pytest.mark.parametrize("fault", ["verify", "conflict", "unlink", "partial-delete"])
def test_first_physical_delete_boundary_and_residual_recovery(home, monkeypatch, fault):
    first = write(home, OWNER / "tasks" / OLD_DATE / "barrier/tmp/a.bin", "first")
    second = write(home, first.with_name("b.bin").relative_to(home), "second")
    age_tree(first.parent)
    original_verify, original_unlink = cleanup._verify_quarantined, cleanup.os.unlink
    state = {"prepared": False, "fired": False, "deleted": 0}

    # LLM: 只观察结构化prepared状态，不直接改变清理器状态机。
    # 函数用途: 标记持久映射checkpoint已到达。
    def prepared(manifest):
        if manifest["candidates"][0].get("isolation", {}).get("state") == "prepared":
            state["prepared"] = True

    # LLM: 第一条最终核验发生在checkpoint之后；EIO不要求伪造对象身份。
    # 函数用途: 重现物理删除尚未发生时的迟到校验失败。
    def late_failure(fd, name, expected):
        if state["prepared"] and not state["fired"] and fault in {"verify", "conflict"}:
            state["fired"] = True
            if fault == "conflict":
                first.write_text("new-owner")
            raise OSError(5, "injected pre-unlink verification")
        return original_verify(fd, name, expected)

    # LLM: 目标unlink失败与清单收尾分开；只在真实成功返回后记录物理删除。
    # 函数用途: 区分首条删除未发生与已经部分删除的两种错误出口。
    def failing_unlink(path, *args, **kwargs):
        if str(path) != "manifest.json":
            if fault in {"unlink", "partial-delete"} and not state["fired"] and (
                fault == "unlink" or state["deleted"] == 1
            ):
                state["fired"] = True
                raise OSError(5, "injected physical delete")
            result = original_unlink(path, *args, **kwargs)
            state["deleted"] += 1
            return result
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(cleanup, "_verify_quarantined", late_failure)
    monkeypatch.setattr(cleanup.os, "unlink", failing_unlink)
    result = cleanup.run_cleanup(home, apply=True, now=NOW, checkpoint=prepared)
    assert state["fired"] and result["errors"]
    row = result["candidates"][0]
    if fault == "partial-delete":
        assert state["deleted"] == result["categories"][CATEGORIES[1]]["deleted_files"] == 1
        assert not first.exists()
        assert row["isolation"]["state"] == "partial_delete"
        assert len(row["recovery_required"]) == 1
        assert Path(row["recovery_required"][0]["quarantine"]).read_text() == "second"
    else:
        assert state["deleted"] == result["categories"][CATEGORIES[1]]["deleted_files"] == 0
        assert second.read_text() == "second"
        if fault == "conflict":
            assert first.read_text() == "new-owner"
            assert len(row["recovery_required"]) == 1
            assert Path(row["recovery_required"][0]["quarantine"]).read_text() == "first"
        else:
            assert first.read_text() == "first"
            assert not list(home.parent.glob(".slimming-quarantine-*"))
